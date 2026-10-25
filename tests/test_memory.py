#!/usr/bin/env python3
"""
SCC — тесты трёхуровневой памяти
=================================

ЧТО ПРОВЕРЯЕТСЯ
---------------
Тесты покрывают не «примеры работы», а **инварианты** из METHODOLOGY_MEMORY.md.
Если инвариант нарушится, схема потеряет контекст — поэтому каждый проверяется
отдельно:

1. ядро применяется только к короткой памяти (у короткой нет чанка);
2. повторная свёртка чанков недопустима — стек не даёт перезаписать номер;
3. оборот памяти без LLM — в исходниках `memory/` нет импортов LLM-клиентов;
4. чанк неизменяем: попытка записать тот же номер — ошибка;
5. стак детерминирован: порядок задаётся числовым `n`, не именем файла;
6. chain.json — источник правды о порядке;
7. Архивариус только читает: у архива нет метода возврата в среднюю память.

Плюс отдельно: глоссарий (детерминизм хеша, round-trip замены, коллизии) и
пороги (разные числа, проверка конфига).

Запуск:
    python3 tests/test_memory.py -v
Код возврата: 0 — прошли, 1 — падения, 2 — не удалось запустить.
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MEMORY = ROOT / "memory"
sys.path.insert(0, str(ROOT))

from memory import tokens  # noqa: E402
from memory.config import MemoryConfig  # noqa: E402
from memory.glossary import Glossary, compute_hash, normalize  # noqa: E402
from memory.long import LongMemory  # noqa: E402
from memory.medium import MediumMemory  # noqa: E402
from memory.short import ShortMemory  # noqa: E402
from memory.stack import ChunkError, ChunkStack  # noqa: E402


def make_chunk(n: int, content: str = "содержимое", level: str = "medium") -> dict:
    """Собрать чанк по схеме core/schemas/chunk.schema.json."""
    return {
        "n": n,
        "parent_chunk": n - 1 if n > 1 else None,
        "source_range": {"start": n * 10, "end": n * 10 + 9},
        "tokens_before": 1000,
        "tokens_after": tokens.estimate(content),
        "created_at": "2026-10-25T00:00:00+00:00",
        "content": content,
        "glossary_refs": [],
        "level": level,
    }


class TestConfig(unittest.TestCase):
    """Конфигурация и пороги."""

    def test_two_thresholds_are_different(self) -> None:
        """Пороги сжатия и архивации — разные числа (иначе архив бессмыслен)."""
        config = MemoryConfig()
        self.assertNotEqual(config.threshold_compress, config.threshold_archivist)
        self.assertGreater(config.threshold_archivist, config.threshold_compress)

    def test_archivist_must_exceed_compress(self) -> None:
        """Архив раньше первого сжатия — ошибка конфига, а не «странность»."""
        with self.assertRaises(ValueError):
            MemoryConfig(threshold_compress=1000, threshold_archivist=500)

    def test_positive_thresholds(self) -> None:
        """Нулевой или отрицательный порог отвергается."""
        with self.assertRaises(ValueError):
            MemoryConfig(threshold_compress=0)

    def test_config_is_immutable(self) -> None:
        """Правка порога создаёт копию, исходный конфиг не меняется."""
        config = MemoryConfig(threshold_compress=100)
        changed = config.with_thresholds(compress=200)
        self.assertEqual(config.threshold_compress, 100)
        self.assertEqual(changed.threshold_compress, 200)

    def test_from_env(self) -> None:
        """Конфиг читается из окружения."""
        config = MemoryConfig.from_env({"SCC_THRESHOLD_COMPRESS": "500", "SCC_THRESHOLD_ARCHIVIST": "900"})
        self.assertEqual(config.threshold_compress, 500)
        self.assertEqual(config.threshold_archivist, 900)

    def test_from_env_rejects_garbage(self) -> None:
        """Нечисловое значение — понятная ошибка, а не тихий дефолт."""
        with self.assertRaises(ValueError):
            MemoryConfig.from_env({"SCC_THRESHOLD_COMPRESS": "много"})


class TestTokens(unittest.TestCase):
    """Подсчёт токенов."""

    def test_empty_is_zero(self) -> None:
        """Пустой текст — ноль токенов, а не один."""
        self.assertEqual(tokens.estimate(""), 0)

    def test_rounds_up(self) -> None:
        """Округление вверх: недооценка опаснее переоценки."""
        self.assertEqual(tokens.estimate("abcde"), 2)

    def test_count_chunks(self) -> None:
        """Суммарный объём считается по полю content."""
        chunks = [{"content": "a" * 400}, {"content": "b" * 400}]
        self.assertEqual(tokens.count_chunks(chunks), 200)


class TestShortMemory(unittest.TestCase):
    """Короткая память."""

    def setUp(self) -> None:
        self.config = MemoryConfig(threshold_compress=100, threshold_archivist=5000)

    def test_no_chunk_until_compress(self) -> None:
        """Инвариант 1: у короткой памяти нет чанка — она сам контекст."""
        short = ShortMemory(self.config)
        short.append("текст")
        self.assertEqual(short.token_count(), tokens.estimate("текст"))

    def test_needs_compress_by_threshold(self) -> None:
        """Порог сжатия применяется к короткой памяти."""
        short = ShortMemory(self.config)
        self.assertFalse(short.needs_compress())
        short.append("x" * 500)
        self.assertTrue(short.needs_compress())

    def test_take_clears_only_after_call(self) -> None:
        """Контекст очищается только явным вызовом take."""
        short = ShortMemory(self.config)
        short.append("важное")
        self.assertEqual(short.text(), "важное")
        taken = short.take()
        self.assertEqual(taken, "важное")
        self.assertEqual(short.text(), "")

    def test_order_preserved(self) -> None:
        """Порядок сообщений сохраняется: контекст хронологичен."""
        short = ShortMemory(self.config)
        short.extend(["первое", "второе", "третье"])
        self.assertLess(short.text().index("первое"), short.text().index("третье"))


class TestStackDeterminism(unittest.TestCase):
    """Инварианты 5 и 6: детерминизм стака и chain.json."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_order_is_numeric_not_lexicographic(self) -> None:
        """
        Порядок задаёт числовое `n`, а не имя файла.

        Лексикографически chunk_10 идёт перед chunk_2 — это сломало бы
        хронологию. Инвариант 5 требует детерминированности от номеров.
        """
        stack = ChunkStack(self.dir / "d", "d")
        for n in (10, 2, 1):
            stack.add(make_chunk(n))
        self.assertEqual([e["n"] for e in stack.ordered()], [1, 2, 10])
        self.assertEqual(stack.load(1)["n"], 1)

    def test_deterministic_across_reloads(self) -> None:
        """Порядок одинаков при повторном чтении: стак — функция от номеров."""
        stack = ChunkStack(self.dir / "d", "d")
        for n in (3, 1, 2):
            stack.add(make_chunk(n))
        first = [e["n"] for e in stack.ordered()]
        second = [e["n"] for e in ChunkStack(self.dir / "d", "d").ordered()]
        self.assertEqual(first, second)

    def test_chain_json_is_source_of_truth(self) -> None:
        """chain.json содержит порядок и уровни; битый файл — ошибка."""
        stack = ChunkStack(self.dir / "d", "d")
        stack.add(make_chunk(1))
        chain = json.loads((self.dir / "d" / "chain.json").read_text(encoding="utf-8"))
        self.assertEqual(chain["last_chunk"], 1)
        self.assertEqual(chain["chunks"][0]["n"], 1)
        (self.dir / "d" / "chain.json").write_text("{битый", encoding="utf-8")
        with self.assertRaises(ChunkError):
            stack.ordered()


class TestImmutability(unittest.TestCase):
    """Инвариант 4: чанк неизменяем после записи."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_rewrite_same_number_forbidden(self) -> None:
        """Повторная запись того же номера — ошибка, а не тихая перезапись."""
        stack = ChunkStack(self.dir / "d", "d")
        stack.add(make_chunk(1, "первое"))
        with self.assertRaises(ChunkError):
            stack.add(make_chunk(1, "второе"))
        self.assertEqual(stack.load(1)["content"], "первое")

    def test_new_number_allowed(self) -> None:
        """Новое сжатие создаёт новый номер — это разрешено."""
        stack = ChunkStack(self.dir / "d", "d")
        stack.add(make_chunk(1))
        stack.add(make_chunk(2))
        self.assertEqual(stack.last_chunk_number(), 2)

    def test_level_change_does_not_touch_file(self) -> None:
        """Вынос в архив меняет уровень в chain.json, файл чанка не трогается."""
        stack = ChunkStack(self.dir / "d", "d")
        stack.add(make_chunk(1, "контент"))
        before = stack.chunk_path(1).read_text(encoding="utf-8")
        stack.set_level(1, "long")
        after = stack.chunk_path(1).read_text(encoding="utf-8")
        self.assertEqual(before, after, "файл чанка изменился при смене уровня")
        self.assertEqual(stack.long()[0]["n"], 1)


class TestLevels(unittest.TestCase):
    """Переходы между уровнями."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.config = MemoryConfig(threshold_compress=100, threshold_archivist=200)
        self.stack = ChunkStack(self.dir / "d", "d")
        self.short = ShortMemory(self.config)
        self.medium = MediumMemory(self.stack, self.config)
        self.long = LongMemory(self.stack, self.config)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_medium_accepts_only_medium(self) -> None:
        """Средняя память не принимает чанк архива."""
        with self.assertRaises(ValueError):
            self.medium.append_chunk(make_chunk(1, level="long"))

    def test_archive_transition_by_threshold(self) -> None:
        """Инвариант перехода: стак > порога -> вынос в архив."""
        self.assertFalse(self.medium.needs_archive())
        self.stack.add(make_chunk(1, "x" * 2000))
        self.assertTrue(self.medium.needs_archive())
        moved = self.medium.move_to_long(self.long)
        self.assertEqual(moved, 1)
        self.assertEqual(len(self.medium), 0)
        self.assertEqual(len(self.long), 1)

    def test_instruction_contains_last_number(self) -> None:
        """Инструкция несёт номер последнего чанка — без него запрос невозможен."""
        self.assertEqual(self.long.instruction(), "", "пустой архив не должен давать инструкцию")
        self.stack.add(make_chunk(1))
        self.medium.move_to_long(self.long)
        self.assertIn("1", self.long.instruction())

    def test_no_way_back_from_long(self) -> None:
        """
        Инвариант 2: переход «долгая -> средняя» отсутствует.

        У LongMemory не должно быть метода возврата в стак: любой возврат
        означал бы, что чанк снова может попасть под свёртку.
        """
        forbidden = {"move_to_medium", "restore_to_medium", "return_to_medium", "to_medium"}
        present = {name for name in dir(self.long) if name in forbidden}
        self.assertEqual(present, set(), f"у архива найден путь возврата: {present}")

    def test_archive_read_is_read_only(self) -> None:
        """Архивариус читает: read возвращает чанк и не меняет состояние."""
        self.stack.add(make_chunk(1, "архивное"))
        self.medium.move_to_long(self.long)
        before = len(self.long)
        self.assertEqual(self.long.read(1)["content"], "архивное")
        self.assertEqual(len(self.long), before)

    def test_read_missing_chunk_raises(self) -> None:
        """Запрос несуществующего чанка — понятная ошибка."""
        with self.assertRaises(KeyError):
            self.long.read(99)


class TestNoLlmInMemory(unittest.TestCase):
    """Инвариант 3: оборот памяти — без LLM."""

    # Имена, по которым узнаётся LLM-клиент. Проверка идёт по импортам AST,
    # поэтому комментарии и строки не дают ложных срабатываний.
    LLM_HINTS = ("openai", "anthropic", "urllib.request", "requests", "httpx", "aiohttp", "llm")

    def test_no_llm_imports(self) -> None:
        """В исходниках memory/ нет импортов LLM-клиентов и сетевых библиотек."""
        offenders: list[str] = []
        for path in sorted(MEMORY.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    low = name.lower()
                    if any(hint in low for hint in self.LLM_HINTS):
                        offenders.append(f"{path.name}: import {name}")
        self.assertEqual(offenders, [], "в memory/ найден LLM-импорт:\n" + "\n".join(offenders))

    def test_core_is_not_imported(self) -> None:
        """Память не импортирует ядро: уровни и свёртка разведены."""
        offenders: list[str] = []
        for path in sorted(MEMORY.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any("scc_" in a.name for a in node.names):
                    offenders.append(path.name)
                if isinstance(node, ast.ImportFrom) and node.module and "scc_" in node.module:
                    offenders.append(path.name)
        self.assertEqual(offenders, [], f"память импортирует ядро: {offenders}")


class TestGlossary(unittest.TestCase):
    """Глоссарий точных данных."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.glossary = Glossary(self.dir / "glossary.db", min_length=10)

    def tearDown(self) -> None:
        self.glossary.close()
        self.tmp.cleanup()

    def test_hash_is_stable(self) -> None:
        """Одна и та же строка даёт один и тот же хеш — иначе восстановление невозможно."""
        self.assertEqual(compute_hash("example/x/y.yml"), compute_hash("example/x/y.yml"))

    def test_normalization_strips_decoration(self) -> None:
        """Кавычки и бэктики не влияют на хеш: смысл один."""
        self.assertEqual(compute_hash('"example/x/y.yml"'), compute_hash("example/x/y.yml"))
        self.assertEqual(compute_hash("`example/x/y.yml`"), compute_hash("example/x/y.yml"))
        self.assertEqual(normalize("  a  b  "), "a b")

    def test_case_is_significant(self) -> None:
        """Регистр значим: в путях он важен."""
        self.assertNotEqual(compute_hash("<Пример>/x"), compute_hash("/home/user/x"))

    def test_round_trip(self) -> None:
        """Замена ссылкой и обратное восстановление дают исходную строку."""
        original = "example/very/long/path/file.yml"
        hash_value = self.glossary.add(original)
        self.assertIsNotNone(hash_value)
        self.assertEqual(self.glossary.restore(hash_value), original)

    def test_reference_format(self) -> None:
        """Формат ссылки строго LB?{8 hex}?"""
        text = "example/very/long/path/file.yml"
        out, hashes = self.glossary.replace_in_text(text + " " + text, min_uses=2)
        self.assertRegex(out, r"LB\?[0-9a-f]{8}\?")
        self.assertEqual(len(hashes), 1)

    def test_only_repeated_replaced(self) -> None:
        """Одиночное значение не заменяется: ссылка не сократит, а читаемость ухудшит."""
        text = "example/only/once/here.yml"
        out, hashes = self.glossary.replace_in_text(text, min_uses=2)
        self.assertEqual(out, text)
        self.assertEqual(hashes, [])

    def test_short_values_skipped(self) -> None:
        """Короткие значения не заменяются: ссылка длиннее оригинала."""
        self.assertIsNone(self.glossary.add("abc"))

    def test_uses_counter_grows(self) -> None:
        """Повторное добавление увеличивает счётчик, хеш не меняется."""
        value = "example/path/to/some/file.yml"
        first = self.glossary.add(value)
        second = self.glossary.add(value)
        self.assertEqual(first, second)
        entry = self.glossary.get(first)
        self.assertIsNotNone(entry)
        self.assertGreaterEqual(entry.uses, 2)

    def test_expand_restores_all(self) -> None:
        """Раскрытие возвращает все ссылки, неизвестные оставляет как есть."""
        value = "example/path/to/some/file.yml"
        out, _ = self.glossary.replace_in_text(value + " " + value, min_uses=2)
        self.assertEqual(self.glossary.expand(out), f"{value} {value}")
        self.assertEqual(self.glossary.expand("LB?deadbeef?"), "LB?deadbeef?")

    def test_collision_handling(self) -> None:
        """
        При коллизии берётся следующий свободный хеш.

        Коллизия подделывается вручную: занимаем вычисленный хеш другой строкой
        и проверяем, что новая получит иной хеш и не потеряется.
        """
        value = "/home/user/collision/test/file.yml"
        hash_value = compute_hash(value)
        # Занимаем хеш посторонним значением напрямую в базе
        self.glossary._connection.execute(
            "INSERT INTO glossary (hash, value, first_seen, uses) VALUES (?, ?, ?, 1)",
            (hash_value, "/home/user/other/value.yml", "2026-10-25T00:00:00+00:00"),
        )
        self.glossary._connection.commit()
        new_hash = self.glossary.add(value)
        self.assertIsNotNone(new_hash)
        self.assertNotEqual(new_hash, hash_value, "коллизия не разрешена")
        self.assertEqual(self.glossary.restore(new_hash), value)
        self.assertEqual(self.glossary.restore(hash_value), "/home/user/other/value.yml")

    def test_longest_first_replacement(self) -> None:
        """Длинные значения заменяются первыми: иначе короткое съест часть длинного."""
        long_value = "/home/user/very/long/path/inside/file.yml"
        short_value = "/home/user/very/long/path"
        text = f"{long_value} {long_value} {short_value} {short_value}"
        out, hashes = self.glossary.replace_in_text(text, min_uses=2)
        # Оба должны быть заменены, и восстановление обязано вернуть исходное
        self.assertEqual(len(hashes), 2)
        self.assertEqual(self.glossary.expand(out), text)


class TestSchemaFile(unittest.TestCase):
    """Схема чанка существует и валидна как JSON."""

    def test_schema_is_valid_json(self) -> None:
        """core/schemas/chunk.schema.json читается и содержит обязательные поля."""
        schema_path = ROOT / "core" / "schemas" / "chunk.schema.json"
        self.assertTrue(schema_path.exists(), "нет схемы чанка")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        required = set(schema.get("required", []))
        for field in (
            "n", "source_range", "tokens_before", "tokens_after",
            "created_at", "content", "glossary_refs", "level",
        ):
            self.assertIn(field, required, f"в схеме нет обязательного поля {field}")

    def test_level_enum_excludes_short(self) -> None:
        """Уровень чанка — medium или long: у短 короткой памяти чанка нет."""
        schema = json.loads((ROOT / "core" / "schemas" / "chunk.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["level"]["enum"], ["medium", "long"])


def main() -> int:
    """Запустить тесты с кодом возврата по соглашению SeverPact."""
    if not MEMORY.exists():
        print(f"НЕ ИЗМЕРЕНО: нет пакета {MEMORY}", file=sys.stderr)
        return 2
    verbosity = 2 if "-v" in sys.argv else 1
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
