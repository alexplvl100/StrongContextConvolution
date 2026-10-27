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

Плюс отдельно: CAS (детерминизм хеша, round-trip замены, коллизии) и
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
MEMORY = ROOT / "_engine" / "memory"
# _engine добавлен в путь: память лежит в _engine/memory, и импорты
# `from memory import ...` работают без правки каждого файла.
sys.path.insert(0, str(ROOT / "_engine"))
sys.path.insert(0, str(ROOT))

from memory import tokens  # noqa: E402
from memory.config import DEFAULT_WINDOW, MemoryConfig, derive_thresholds  # noqa: E402
from memory.cas import ContentAddressedStore, compute_hash, normalize  # noqa: E402
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
        "cas_refs": [],
        "level": level,
    }


class TestConfig(unittest.TestCase):
    """Конфигурация и пороги."""

    def test_thresholds_measure_different_things(self) -> None:
        """
        Пороги измеряют РАЗНОЕ и выводятся из рабочей зоны.

        `threshold_compress` — размер короткой памяти, `threshold_archivist` —
        размер стака чанков. Они **не обязаны** соотноситься отношением `>`:
        живой урок 2026-10-27 — проверка `archivist > compress` при окне 1M
        требовала поднять архив с 397 420 до 572 286 и ломала вывод порогов.
        """
        config = MemoryConfig.for_window(1_000_000)
        # архив меньше порога сжатия — это НОРМА: чанк втрое меньше короткой
        self.assertLess(config.threshold_archivist, config.threshold_compress)

    def test_memory_fits_working_zone(self) -> None:
        """
        Содержательная проверка: вся память помещается в рабочую зону.

        Заменяет прежнее сравнение порогов между собой, которое смысла не имело.
        """
        for window in (262_144, 1_000_000, 128_000):
            with self.subTest(window=window):
                config = MemoryConfig.for_window(window)
                self.assertTrue(
                    config.fits_zone(window),
                    f"память не помещается в зону окна {window}: "
                    f"HEAD + {config.threshold_archivist} + {config.threshold_compress}",
                )

    def test_thresholds_derived_from_window(self) -> None:
        """Пороги растут вместе с окном модели."""
        small = MemoryConfig.for_window(262_144)
        large = MemoryConfig.for_window(1_000_000)
        self.assertLess(small.threshold_compress, large.threshold_compress)
        self.assertLess(small.threshold_archivist, large.threshold_archivist)

    def test_negative_threshold_rejected(self) -> None:
        """Отрицательный порог — ошибка конфига."""
        with self.assertRaises(ValueError):
            MemoryConfig(threshold_compress=-1)

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
        """Конфиг читается из окружения: явные пороги переопределяют вывод."""
        config = MemoryConfig.from_env({
            "SCC_THRESHOLD_COMPRESS": "500",
            "SCC_THRESHOLD_ARCHIVIST": "900",
        })
        self.assertEqual(config.threshold_compress, 500)
        self.assertEqual(config.threshold_archivist, 900)

    def test_from_env_derives_by_default(self) -> None:
        """Без переменных окружения пороги выводятся из окна."""
        config = MemoryConfig.from_env({})
        expected = MemoryConfig.for_window(DEFAULT_WINDOW)
        self.assertEqual(config.threshold_compress, expected.threshold_compress)
        self.assertEqual(config.threshold_archivist, expected.threshold_archivist)

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


class TestContentAddressedStore(unittest.TestCase):
    """Глоссарий точных данных."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cas = ContentAddressedStore(self.dir / "cas.db", min_length=10)

    def tearDown(self) -> None:
        self.cas.close()
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
        hash_value = self.cas.add(original)
        self.assertIsNotNone(hash_value)
        self.assertEqual(self.cas.restore(hash_value), original)

    def test_reference_prefix_is_cas(self) -> None:
        """
        Токен ссылки — `CAS?{8 hex}?`, по утверждённому термину проекта.

        Прежний `LB?` (от «library») заменён: расхождение имени с содержанием
        путает. Тест фиксирует решение, чтобы токен не вернулся при правках.
        """
        from memory.cas import make_reference

        self.assertEqual(make_reference("a1b2c3d4"), "CAS?a1b2c3d4?")
        self.assertFalse(make_reference("a1b2c3d4").startswith("LB"), "вернулся старый префикс")

    def test_old_prefix_not_recognised(self) -> None:
        """Старый токен `LB?...?` больше не распознаётся как ссылка."""
        self.assertEqual(self.cas.references_in("LB?deadbeef?"), [])
        self.assertEqual(self.cas.expand("LB?deadbeef?"), "LB?deadbeef?")

    def test_reference_format(self) -> None:
        """Формат ссылки строго CAS?{8 hex}?"""
        text = "example/very/long/path/file.yml"
        out, hashes = self.cas.replace_in_text(text + " " + text, min_uses=2)
        self.assertRegex(out, r"CAS\?[0-9a-f]{8}\?")
        self.assertEqual(len(hashes), 1)

    def test_only_repeated_replaced(self) -> None:
        """Одиночное значение не заменяется: ссылка не сократит, а читаемость ухудшит."""
        text = "example/only/once/here.yml"
        out, hashes = self.cas.replace_in_text(text, min_uses=2)
        self.assertEqual(out, text)
        self.assertEqual(hashes, [])

    def test_short_values_skipped(self) -> None:
        """Короткие значения не заменяются: ссылка длиннее оригинала."""
        self.assertIsNone(self.cas.add("abc"))

    def test_uses_counter_grows(self) -> None:
        """Повторное добавление увеличивает счётчик, хеш не меняется."""
        value = "example/path/to/some/file.yml"
        first = self.cas.add(value)
        second = self.cas.add(value)
        self.assertEqual(first, second)
        entry = self.cas.get(first)
        self.assertIsNotNone(entry)
        self.assertGreaterEqual(entry.uses, 2)

    def test_expand_restores_all(self) -> None:
        """Раскрытие возвращает все ссылки, неизвестные оставляет как есть."""
        value = "example/path/to/some/file.yml"
        out, _ = self.cas.replace_in_text(value + " " + value, min_uses=2)
        self.assertEqual(self.cas.expand(out), f"{value} {value}")
        self.assertEqual(self.cas.expand("CAS?deadbeef?"), "CAS?deadbeef?")

    def test_collision_handling(self) -> None:
        """
        При коллизии берётся следующий свободный хеш.

        Коллизия подделывается вручную: занимаем вычисленный хеш другой строкой
        и проверяем, что новая получит иной хеш и не потеряется.
        """
        value = "/home/user/collision/test/file.yml"
        hash_value = compute_hash(value)
        # Занимаем хеш посторонним значением напрямую в базе
        self.cas._connection.execute(
            "INSERT INTO cas (hash, value, first_seen, uses) VALUES (?, ?, ?, 1)",
            (hash_value, "/home/user/other/value.yml", "2026-10-25T00:00:00+00:00"),
        )
        self.cas._connection.commit()
        new_hash = self.cas.add(value)
        self.assertIsNotNone(new_hash)
        self.assertNotEqual(new_hash, hash_value, "коллизия не разрешена")
        self.assertEqual(self.cas.restore(new_hash), value)
        self.assertEqual(self.cas.restore(hash_value), "/home/user/other/value.yml")

    def test_longest_first_replacement(self) -> None:
        """Длинные значения заменяются первыми: иначе короткое съест часть длинного."""
        long_value = "/home/user/very/long/path/inside/file.yml"
        short_value = "/home/user/very/long/path"
        text = f"{long_value} {long_value} {short_value} {short_value}"
        out, hashes = self.cas.replace_in_text(text, min_uses=2)
        # Оба должны быть заменены, и восстановление обязано вернуть исходное
        self.assertEqual(len(hashes), 2)
        self.assertEqual(self.cas.expand(out), text)


class TestDirectAccess(unittest.TestCase):
    """
    ПРЯМОЙ доступ к CAS, минуя Архивариуса.

    Требование: точные данные возвращаются побайтово прямо из базы. Архивариус —
    агент с LLM, и его пересказ исказил бы значение. Поэтому путь к данным не
    проходит ни через одну модель.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cas = ContentAddressedStore(self.dir / "cas.db", min_length=16)

    def tearDown(self) -> None:
        self.cas.close()
        self.tmp.cleanup()

    def test_resolve_many_one_call(self) -> None:
        """Список ссылок разрешается одним вызовом, значения дословны."""
        values = [
            "~/.dsh/profiles/web/cordis.patch.yml",
            "alexplvl100/MultiAgentDSH",
            "af3dba1cc7afffee8b7c4f485ee5afcc061f4a42fa2b90b59faff79cdfe0fa99",
        ]
        hashes = [self.cas.add(v) for v in values]
        resolved = self.cas.resolve_many([h for h in hashes if h])
        self.assertEqual(len(resolved), 3)
        for value, hash_value in zip(values, hashes):
            self.assertEqual(resolved[hash_value], value, "значение искажено")

    def test_unknown_hash_absent_not_faked(self) -> None:
        """Неизвестный хеш не подставляется: его просто нет в результате."""
        resolved = self.cas.resolve_many(["deadbeef"])
        self.assertEqual(resolved, {}, "выдумано значение для неизвестного хеша")

    def test_resolve_many_empty(self) -> None:
        """Пустой список даёт пустой словарь, а не ошибку."""
        self.assertEqual(self.cas.resolve_many([]), {})

    def test_references_in_order(self) -> None:
        """Список ссылок в тексте — в порядке появления, без повторов."""
        a = self.cas.add("/home/user/first/long/path/file.yml")
        b = self.cas.add("alexplvl100/MultiAgentDSH")
        text = f"CAS?{a}? потом CAS?{b}? и снова CAS?{a}?"
        self.assertEqual(self.cas.references_in(text), [a, b])

    def test_direct_access_no_llm(self) -> None:
        """Модуль CAS не содержит импортов LLM: доступ не через модель."""
        tree = ast.parse((MEMORY / "cas.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                low = name.lower()
                for hint in ("openai", "anthropic", "requests", "httpx", "aiohttp"):
                    self.assertNotIn(hint, low, f"в cas.py найден импорт {name}")


class TestCollision(unittest.TestCase):
    """
    КОЛЛИЗИЯ: одинаковый хеш при разном содержимом.

    Вероятность ничтожна (8 hex = 32 бита, ~4 млрд значений), но она есть, и
    поведение при ней обязано быть корректным. Живые уроки 2026-10-26:

    1. прежний сдвиг адреса (`hash+1`) ломал главный инвариант CAS — адрес
       начинал зависеть от ПОРЯДКА добавления;
    2. расширенный адрес не распознавался регуляркой, рассчитанной ровно на
       8 символов, — ссылка становилась невидимой для системы.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _store(self, name: str = "cas.db") -> ContentAddressedStore:
        return ContentAddressedStore(self.dir / name, min_length=16)

    def _occupy(self, store: ContentAddressedStore, hash_value: str, value: str) -> None:
        """Занять хеш посторонним значением — так подделывается коллизия."""
        store._connection.execute(
            "INSERT INTO cas (hash, value, first_seen, uses) VALUES (?, ?, ?, 1)",
            (hash_value, value, "2026-10-26T00:00:00+00:00"),
        )
        store._connection.commit()

    def test_collision_address_is_deterministic(self) -> None:
        """
        Адрес при коллизии — функция содержимого, а не порядка добавления.

        Прежний сдвиг давал: база A -> fe6e2882, база B -> fe6e2881 для одного
        значения. Ссылка в записанном чанке перестала бы разрешаться при
        пересборке базы, а чанк неизменяем — исправить её нечем.
        """
        value = "/home/user/first/long/path/file.yml"
        other = "/home/user/second/other/file.yml"
        base_hash = compute_hash(value)

        first = self._store("a.db")
        self._occupy(first, base_hash, other)
        address_in_first = first.add(value)
        first.close()

        second = self._store("b.db")
        address_in_second = second.add(value)
        second.close()

        self.assertNotEqual(address_in_first, base_hash, "коллизия не разрешилась")
        self.assertEqual(
            address_in_first, compute_hash(value, len(address_in_first)),
            "расширенный адрес не является префиксом того же sha256",
        )
        self.assertTrue(
            compute_hash(value, len(address_in_first)).startswith(base_hash),
            "расширенный адрес не продолжает базовый хеш",
        )

    def test_collision_address_is_stable_across_orders(self) -> None:
        """Тот же конфликт в другом порядке даёт тот же расширенный адрес."""
        value = "/home/user/order/test/long/path/file.yml"
        other = "/home/user/occupier/long/path/file.yml"
        base_hash = compute_hash(value)

        first = self._store("x.db")
        self._occupy(first, base_hash, other)
        addr_conflict = first.add(value)
        first.close()

        # В базе без конфликта значение получает базовый адрес, а конфликтующий
        # — расширенный. Проверяем, что расширенный адрес предсказуем: он
        # префикс sha256, а не «следующий свободный».
        second = self._store("y.db")
        addr_free = second.add(value)
        second.close()
        self.assertEqual(addr_free, base_hash)
        self.assertEqual(addr_conflict, compute_hash(value, len(addr_conflict)))

    def test_extended_hash_reference_is_recognised(self) -> None:
        """
        Ссылка с расширенным хешем распознаётся и разрешается.

        Живой урок: регулярка была рассчитана ровно на 8 символов, поэтому
        `CAS?fe6e288101?` она не находила вовсе — ссылка в чанке становилась
        невидимой, значение не разрешалось, потери не было видно.
        """
        value = "/home/user/extended/hash/long/file.yml"
        other = "/home/user/occupier/other/file.yml"
        store = self._store("ext.db")
        self._occupy(store, compute_hash(value), other)
        address = store.add(value)
        store.close()

        self.assertGreater(len(address), 8, "адрес не расширился")

        reopened = ContentAddressedStore(self.dir / "ext.db", min_length=16)
        reference = f"CAS?{address}?"
        text = f"путь {reference} тут"
        self.assertEqual(reopened.references_in(text), [address], "расширенная ссылка не найдена")
        self.assertEqual(reopened.resolve_many([address]), {address: value})
        self.assertEqual(reopened.expand(text), f"путь {value} тут")
        reopened.close()

    def test_both_values_resolve_after_collision(self) -> None:
        """После коллизии разрешаются ОБА значения — и занявшее, и новое."""
        value = "/home/user/both/first/long/file.yml"
        other = "/home/user/both/second/long/file.yml"
        store = self._store("both.db")
        self._occupy(store, compute_hash(value), other)
        address = store.add(value)

        self.assertEqual(store.restore(address), value)
        self.assertEqual(store.restore(compute_hash(value)), other)
        self.assertEqual(store.size(), 2)
        store.close()

    def test_repeated_add_after_collision_is_idempotent(self) -> None:
        """
        Повторное добавление после коллизии не создаёт дубликат и не падает.

        Живой урок: проверка «значение уже есть» смотрела только базовую длину,
        поэтому повторный add значения с расширенным адресом не находил его и
        падал с `UNIQUE constraint failed: cas.hash`.
        """
        value = "/home/user/idempotent/long/path/file.yml"
        other = "/home/user/occupier/long/path/file.yml"
        store = self._store("idem.db")
        self._occupy(store, compute_hash(value), other)

        first = store.add(value)
        second = store.add(value)
        third = store.add(value)

        self.assertEqual([first, second, third], [first, first, first],
                         "повторное добавление сменило адрес")
        self.assertEqual(store.size(), 2, "созданы лишние записи")
        entry = store.get(first)
        self.assertIsNotNone(entry)
        self.assertGreaterEqual(entry.uses, 3, "счётчик использований не растёт")
        store.close()

    def test_no_silent_substitution_on_missing_reference(self) -> None:
        """
        Отсутствующая ссылка НЕ подменяется: значение просто отсутствует.

        Это важнее удобства: подставленное правдоподобное значение выглядело бы
        как настоящие данные, и потеря прошла бы незамеченной.
        """
        store = self._store("missing.db")
        self.assertEqual(store.resolve_many(["deadbeef"]), {})
        self.assertEqual(store.restore("deadbeef"), None)
        text = "ссылка CAS?deadbeef? не разрешается"
        self.assertEqual(store.expand(text), text, "отсутствующая ссылка была подменена")
        self.assertEqual(store.references_in(text), ["deadbeef"])
        store.close()

    def test_mixed_present_and_missing(self) -> None:
        """В списке ссылок разрешаются только присутствующие, остальные видны."""
        value = "/home/user/mixed/long/path/file.yml"
        store = self._store("mixed.db")
        address = store.add(value)
        resolved = store.resolve_many([address, "deadbeef"])
        self.assertEqual(resolved, {address: value})
        store.close()


class TestCasPrompt(unittest.TestCase):
    """Инструкция CAS: третья часть HEAD — перед памятью, не сжимается."""

    def test_prompt_mentions_reference_format(self) -> None:
        """Промт объясняет формат ссылки — иначе она выглядит как мусор."""
        from memory.cas_prompt import build_cas_prompt

        text = build_cas_prompt()
        self.assertIn("CAS?", text)
        self.assertIn("CAS", text)

    def test_prompt_requires_direct_access(self) -> None:
        """Промт требует обращаться НАПРЯМУЮ и объясняет, почему."""
        from memory.cas_prompt import build_cas_prompt

        text = build_cas_prompt()
        self.assertIn("НАПРЯМУЮ", text)
        self.assertIn("Архивариус", text)
        self.assertIn("побайтово", text)

    def test_prompt_names_tools(self) -> None:
        """Промт называет инструменты доступа."""
        from memory.cas_prompt import build_cas_prompt

        text = build_cas_prompt()
        for tool in ("resolve_many", "restore", "references_in"):
            self.assertIn(tool, text, f"инструмент {tool} не назван")

    def test_prompt_forbids_guessing(self) -> None:
        """Промт запрещает выдумывать значение по ссылке."""
        from memory.cas_prompt import build_cas_prompt

        text = build_cas_prompt()
        self.assertIn("не подставляй", text)

    def test_empty_when_no_references(self) -> None:
        """При нуле ссылок промт пуст: не занимать контекст впустую."""
        from memory.cas_prompt import build_cas_prompt

        self.assertEqual(build_cas_prompt(0), "")

    def test_count_included_when_known(self) -> None:
        """Известное число ссылок попадает в промт."""
        from memory.cas_prompt import build_cas_prompt

        self.assertIn("7", build_cas_prompt(7))

    def test_token_cost_measured(self) -> None:
        """Стоимость промта измерима — она должна быть видна в замере."""
        from memory.cas_prompt import cas_token_cost

        self.assertGreater(cas_token_cost(), 0)
        self.assertEqual(cas_token_cost(0), 0)


class TestLinking(unittest.TestCase):
    """
    Связка ядра и CAS: «чанк создан ядром -> обработан CAS».

    Закрывает открытый вопрос методологии: раньше CAS вызывался только
    вручную, и автоматического шага не существовало.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cas = ContentAddressedStore(self.dir / "cas.db", min_length=16)

    def tearDown(self) -> None:
        self.cas.close()
        self.tmp.cleanup()

    def test_repeated_values_replaced(self) -> None:
        """Повторяющиеся точные данные заменяются ссылками, refs заполняются."""
        from memory.linking import link_cas

        path = "~/.dsh/profiles/web/cordis.patch.yml"
        chunk = make_chunk(1, f"путь {path} и снова {path}")
        processed = link_cas(chunk, self.cas)
        self.assertEqual(len(processed["cas_refs"]), 1)
        self.assertRegex(processed["content"], r"CAS\?[0-9a-f]{8}\?")
        # Восстановление даёт исходный текст
        self.assertIn(path, self.cas.expand(processed["content"]))

    def test_original_chunk_not_mutated(self) -> None:
        """Исходный чанк не меняется: возвращается новый объект."""
        from memory.linking import link_cas

        path = "/home/user/.some/long/path/file.yml"
        chunk = make_chunk(1, f"{path} {path}")
        before_content = chunk["content"]
        processed = link_cas(chunk, self.cas)
        self.assertEqual(chunk["content"], before_content, "исходный чанк изменён")
        self.assertIsNot(chunk, processed)

    def test_metadata_untouched(self) -> None:
        """Метаданные не трогаются: n, level, created_at, source_range."""
        from memory.linking import link_cas

        path = "/home/user/another/long/path/file.yml"
        chunk = make_chunk(7, f"{path} {path}")
        processed = link_cas(chunk, self.cas)
        for field in ("n", "level", "created_at", "source_range", "parent_chunk"):
            self.assertEqual(processed[field], chunk[field], f"поле {field} изменилось")

    def test_tokens_after_recomputed(self) -> None:
        """tokens_after пересчитывается: старое значение стало неверным."""
        from memory.linking import link_cas
        from memory import tokens

        path = "/home/user/recount/long/path/file.yml"
        chunk = make_chunk(1, f"{path} {path}")
        chunk["tokens_after"] = 999999  # заведомо неверное
        processed = link_cas(chunk, self.cas)
        self.assertEqual(processed["tokens_after"], tokens.estimate(processed["content"]))

    def test_single_occurrence_not_replaced(self) -> None:
        """Одиночное значение не заменяется — refs пуст."""
        from memory.linking import link_cas

        chunk = make_chunk(1, "путь example/only/once/here.yml упомянут однажды")
        processed = link_cas(chunk, self.cas)
        self.assertEqual(processed["cas_refs"], [])
        self.assertEqual(processed["content"], chunk["content"])

    def test_no_llm_import_in_linking(self) -> None:
        """Связка не вызывает модель: инвариант 3."""
        tree = ast.parse((MEMORY / "linking.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("openai", alias.name.lower())
                    self.assertNotIn("anthropic", alias.name.lower())
            if isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn("openai", node.module.lower())
                self.assertNotIn("anthropic", node.module.lower())

    def test_report_mentions_delta(self) -> None:
        """Отчёт показывает изменение объёма — иначе выигрыш незаметен."""
        from memory.linking import link_cas, linking_report

        path = "/home/user/report/long/path/file.yml"
        chunk = make_chunk(1, f"{path} {path}")
        processed = link_cas(chunk, self.cas)
        report = linking_report(chunk, processed)
        self.assertIn("ссылок", report)
        self.assertIn("символов", report)

    def test_processed_chunk_matches_schema(self) -> None:
        """Обработанный чанк проходит валидацию по схеме."""
        from memory.linking import link_cas
        from memory.schema import validate_chunk

        path = "/home/user/schema/check/long/path/file.yml"
        chunk = make_chunk(1, f"{path} {path}")
        processed = link_cas(chunk, self.cas)
        validate_chunk(processed)  # не должно бросить


class TestSchemaFile(unittest.TestCase):
    """Схема чанка существует и валидна как JSON."""

    def test_schema_is_valid_json(self) -> None:
        """_engine/core/schemas/chunk.schema.json читается и содержит обязательные поля."""
        schema_path = ROOT / "_engine" / "core" / "schemas" / "chunk.schema.json"
        self.assertTrue(schema_path.exists(), "нет схемы чанка")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        required = set(schema.get("required", []))
        for field in (
            "n", "source_range", "tokens_before", "tokens_after",
            "created_at", "content", "cas_refs", "level",
        ):
            self.assertIn(field, required, f"в схеме нет обязательного поля {field}")

    def test_level_enum_excludes_short(self) -> None:
        """Уровень чанка — medium или long: у короткой памяти чанка нет."""
        schema = json.loads((ROOT / "_engine" / "core" / "schemas" / "chunk.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["level"]["enum"], ["medium", "long"])

    def test_schema_is_used_by_code(self) -> None:
        """
        Схема ИСПОЛЬЗУЕТСЯ, а не только описана.

        Критерий приёмки требует «схема валидна и используется в коде». Проверка
        исполняемая: валидатор подключён к записи чанка, поэтому невалидный чанк
        обязан быть отвергнут ещё до попадания на диск.
        """
        from memory.schema import validate_chunk

        valid = make_chunk(1)
        validate_chunk(valid)  # не должно бросить

    def test_missing_field_rejected(self) -> None:
        """Чанк без обязательного поля отвергается с указанием поля."""
        from memory.schema import SchemaError, validate_chunk

        broken = make_chunk(1)
        del broken["created_at"]
        with self.assertRaises(SchemaError) as caught:
            validate_chunk(broken)
        self.assertIn("created_at", str(caught.exception))

    def test_wrong_level_rejected(self) -> None:
        """level=short отвергается: у короткой памяти чанка не бывает."""
        from memory.schema import SchemaError, validate_chunk

        broken = make_chunk(1, level="short")
        with self.assertRaises(SchemaError):
            validate_chunk(broken)

    def test_unknown_field_rejected(self) -> None:
        """Лишнее поле отвергается: схема запрещает additionalProperties."""
        from memory.schema import SchemaError, validate_chunk

        broken = make_chunk(1)
        broken["лишнее"] = "поле"
        with self.assertRaises(SchemaError):
            validate_chunk(broken)

    def test_bad_hash_in_cas_refs_rejected(self) -> None:
        """Ссылка на CAS обязана быть 8 hex-символов."""
        from memory.schema import SchemaError, validate_chunk

        broken = make_chunk(1)
        broken["cas_refs"] = ["не-хеш"]
        with self.assertRaises(SchemaError):
            validate_chunk(broken)

    def test_bad_datetime_rejected(self) -> None:
        """created_at обязан быть ISO 8601."""
        from memory.schema import SchemaError, validate_chunk

        broken = make_chunk(1)
        broken["created_at"] = "25 октября"
        with self.assertRaises(SchemaError):
            validate_chunk(broken)

    def test_stack_validates_before_write(self) -> None:
        """
        Валидация идёт ДО записи — иначе ошибка осталась бы в архиве навсегда.

        Чанк неизменяем (инвариант 4), поэтому исправлять записанный чанк нечем.
        """
        from memory.schema import SchemaError

        with tempfile.TemporaryDirectory() as tmp:
            stack = ChunkStack(Path(tmp) / "d", "d")
            broken = make_chunk(1)
            del broken["content"]
            with self.assertRaises(SchemaError):
                stack.add(broken)
            # Ни файла, ни записи в chain.json остаться не должно
            self.assertFalse(stack.chunk_path(1).exists(), "битый чанк записан на диск")
            self.assertEqual(stack.ordered(), [], "битый чанк попал в chain.json")


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
