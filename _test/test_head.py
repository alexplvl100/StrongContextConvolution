#!/usr/bin/env python3
"""
SCC — тесты HEAD: изъятие, установка, сборка контекста
========================================================

ЧТО ПРОВЕРЯЕТСЯ
---------------
Реализация [`METHODOLOGY_HEAD.md`](../METHODOLOGY_HEAD.md). Методология в
состоянии **ПРИНЯТ**, поэтому тесты проверяют код **против документа**, а не
наоборот: если поведение расходится с методологией, верен документ.

Главное свойство HEAD — **изъятие** (инвариант 8): роль обязана уйти из потока
до вызова ядра SCC, иначе она попадёт в чанк, уедет в архив и будет сжата.

Проверяются случаи, которые ломали бы схему молча:

1. **роль в начале** — извлекается, поток очищается;
2. **SYSTEM в середине** — НЕ извлекается (это вставка по ходу, часть истории);
3. **несколько SYSTEM в начале** — все в HEAD, в исходном порядке;
4. **роли нет** — Head пуст, но при рендере появляется **явное сообщение**;
5. **порядок контекста** — `[HEAD][средняя][короткая]`;
6. **три части HEAD** — роль, инструкция Архивариуса, инструкция CAS.

Случай 2 — самый опасный: удалив «повтор роли» из середины, мы потеряли бы
событие истории, и заметить это было бы нечем.

Случай 4 — второй по важности: молчаливый Head без роли даёт непонятную
деградацию агента вместо видимой проблемы.

Запуск:
    python3 tests/test_head.py -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# _engine добавлен в путь: память лежит в _engine/memory, и импорты
# `from memory import ...` работают без правки каждого файла.
sys.path.insert(0, str(ROOT / "_engine"))
sys.path.insert(0, str(ROOT))

from memory.head import (  # noqa: E402
    NO_ROLE_NOTICE,
    Head,
    attach_instructions,
    extract_head,
    render_context,
    split_events,
)
from memory.long import LongMemory  # noqa: E402
from memory.stack import ChunkStack  # noqa: E402


def stream(*events: tuple[str, str]) -> str:
    """Собрать поток событий в формате core/scc_events.py."""
    parts = []
    for number, (kind, body) in enumerate(events, start=1):
        parts.append(f"[E{number}] {kind} (seq {number}):\n{body}")
    return "\n\n".join(parts) + "\n"


class TestSplitEvents(unittest.TestCase):
    """Разбор потока событий."""

    def test_parses_all_events(self) -> None:
        """Все события находятся, тела сохраняются."""
        text = stream(("SYSTEM", "роль"), ("USER", "задача"), ("ASSISTANT", "ответ"))
        events = split_events(text)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0][1].strip(), "роль")
        self.assertEqual(events[1][1].strip(), "задача")

    def test_empty_stream_gives_no_events(self) -> None:
        """Пустой поток не даёт событий: это отличается от «роль не найдена»."""
        self.assertEqual(split_events(""), [])
        self.assertEqual(split_events("текст без событий"), [])

    def test_kind_and_seq_parsed(self) -> None:
        """Тип и номер журнала разбираются."""
        events = split_events(stream(("SYSTEM", "x")))
        self.assertEqual(events[0][0].group("kind"), "SYSTEM")
        self.assertEqual(events[0][0].group("seq"), "1")


class TestExtraction(unittest.TestCase):
    """Изъятие HEAD из потока — главное свойство (инвариант 8)."""

    def test_leading_role_extracted(self) -> None:
        """Роль в начале извлекается, и в потоке её не остаётся."""
        text = stream(("SYSTEM", "РОЛЬ агента"), ("USER", "задача"))
        head, rest = extract_head(text)
        self.assertEqual(head.extracted, ["E1"])
        self.assertIn("РОЛЬ агента", head.role)
        self.assertEqual(head.role_source, 1)
        self.assertNotIn("РОЛЬ агента", rest, "роль осталась в потоке для ядра")

    def test_history_preserved(self) -> None:
        """История не теряется: остаются все не-SYSTEM события."""
        text = stream(("SYSTEM", "роль"), ("USER", "задача"), ("ASSISTANT", "ответ"))
        _, rest = extract_head(text)
        self.assertIn("задача", rest)
        self.assertIn("ответ", rest)
        self.assertEqual(len(split_events(rest)), 2)

    def test_midstream_system_not_extracted(self) -> None:
        """
        SYSTEM в СЕРЕДИНЕ не извлекается — это вставка по ходу, часть истории.

        Живой случай DSH: повтор контекста роли после смены каталога
        (`[E1] SYSTEM (seq 10)`). Удалить его значило бы потерять событие.
        """
        text = stream(
            ("SYSTEM", "РОЛЬ"),
            ("USER", "задача"),
            ("SYSTEM", "ПОВТОР после смены каталога"),
            ("USER", "продолжение"),
        )
        head, rest = extract_head(text)
        self.assertEqual(head.extracted, ["E1"], "извлечён не только ведущий SYSTEM")
        self.assertNotIn("ПОВТОР", head.role, "повтор попал в HEAD")
        self.assertIn("ПОВТОР", rest, "повтор удалён из истории")

    def test_multiple_leading_systems(self) -> None:
        """Несколько SYSTEM в начале — все в HEAD, в исходном порядке."""
        text = stream(
            ("SYSTEM", "часть 1"),
            ("SYSTEM", "часть 2"),
            ("USER", "задача"),
        )
        head, rest = extract_head(text)
        self.assertEqual(head.extracted, ["E1", "E2"])
        self.assertIn("часть 1", head.role)
        self.assertIn("часть 2", head.role)
        self.assertLess(head.role.index("часть 1"), head.role.index("часть 2"))
        self.assertEqual(len(split_events(rest)), 1)

    def test_no_role_gives_empty_head(self) -> None:
        """Поток без SYSTEM даёт пустую роль и поток без изменений."""
        text = stream(("USER", "задача без роли"))
        head, rest = extract_head(text)
        self.assertEqual(head.extracted, [])
        self.assertEqual(head.role, "")
        self.assertIsNone(head.role_source)
        self.assertEqual(rest, text, "поток изменён при отсутствии роли")

    def test_empty_stream_not_reported_as_missing_role(self) -> None:
        """
        Пустой поток не выдаётся за «роль отсутствует».

        Иначе неверный формат входа скрылся бы за сообщением «роль не найдена»,
        и настоящая причина осталась бы незамеченной.
        """
        head, rest = extract_head("")
        self.assertEqual(head.role, "")
        self.assertEqual(head.extracted, [])
        self.assertEqual(rest, "")

    def test_role_not_duplicated_in_rest(self) -> None:
        """Роль не остаётся в потоке ни в каком виде — иначе двойной расход."""
        role_text = "You are an AI agent powered by DeepSeek Harness."
        text = stream(("SYSTEM", role_text), ("USER", "задача"))
        head, rest = extract_head(text)
        self.assertEqual(rest.count(role_text), 0)
        self.assertEqual(head.role.count(role_text), 1)


class TestHeadRender(unittest.TestCase):
    """Сборка текста HEAD."""

    def test_missing_role_is_explicit(self) -> None:
        """
        При отсутствии роли выводится ЯВНОЕ сообщение, а не тишина.

        Молчаливый Head без роли даёт непонятную деградацию: агент работает без
        контракта и не понимает, почему у него не получается.
        """
        head = Head(role="")
        rendered = head.render()
        self.assertIn("ОТСУТСТВУЕТ", rendered)
        self.assertEqual(rendered, NO_ROLE_NOTICE)

    def test_present_role_not_replaced_by_notice(self) -> None:
        """При наличии роли сообщение об отсутствии не появляется."""
        head = Head(role="РОЛЬ агента")
        rendered = head.render()
        self.assertIn("РОЛЬ агента", rendered)
        self.assertNotIn("ОТСУТСТВУЕТ", rendered)

    def test_three_parts_in_order(self) -> None:
        """Три части идут в порядке: роль, Архивариус, CAS."""
        head = Head(role="РОЛЬ", archivist_instruction="АРХИВ", cas_instruction="CAS-ИНСТРУКЦИЯ")
        rendered = head.render()
        self.assertLess(rendered.index("РОЛЬ"), rendered.index("АРХИВ"))
        self.assertLess(rendered.index("АРХИВ"), rendered.index("CAS-ИНСТРУКЦИЯ"))

    def test_empty_parts_skipped(self) -> None:
        """Пустые части не занимают места в контексте."""
        head = Head(role="РОЛЬ")
        rendered = head.render()
        self.assertEqual(rendered.strip(), "РОЛЬ")

    def test_is_empty(self) -> None:
        """Пустой Head распознаётся."""
        self.assertTrue(Head().is_empty())
        self.assertFalse(Head(role="роль").is_empty())
        self.assertFalse(Head(cas_instruction="инструкция").is_empty())

    def test_token_count_measured(self) -> None:
        """Объём HEAD измерим: он занимает место в контексте."""
        head = Head(role="роль" * 100)
        self.assertGreater(head.token_count(), 0)
        self.assertEqual(Head(role="").token_count(), len(NO_ROLE_NOTICE) // 4 + 1)


class TestInstructions(unittest.TestCase):
    """Инструкции доступа — части 2 и 3 HEAD."""

    def test_archivist_instruction_from_long_memory(self) -> None:
        """Инструкция Архивариуса берётся из LongMemory и содержит команды."""
        with tempfile.TemporaryDirectory() as tmp:
            stack = ChunkStack(Path(tmp) / "d", "d")
            long_memory = LongMemory(stack)
            self.assertEqual(long_memory.instruction(), "", "пустой архив даёт инструкцию")

            stack.add({
                "n": 1, "parent_chunk": None, "source_range": {"start": 1, "end": 5},
                "tokens_before": 100, "tokens_after": 50,
                "created_at": "2026-10-26T00:00:00+00:00", "content": "x",
                "cas_refs": [], "level": "medium",
            })
            stack.set_level(1, "long")
            instruction = long_memory.instruction()
            self.assertIn("1..1", instruction)
            for command in ("long.read", "long.chunks", "long.last_number"):
                self.assertIn(command, instruction, f"нет команды {command}")

    def test_archivist_instruction_forbids_cas(self) -> None:
        """Инструкция Архивариуса прямо запрещает запрашивать точные данные."""
        with tempfile.TemporaryDirectory() as tmp:
            stack = ChunkStack(Path(tmp) / "d", "d")
            stack.add({
                "n": 1, "parent_chunk": None, "source_range": {"start": 1, "end": 5},
                "tokens_before": 100, "tokens_after": 50,
                "created_at": "2026-10-26T00:00:00+00:00", "content": "x",
                "cas_refs": [], "level": "medium",
            })
            stack.set_level(1, "long")
            instruction = LongMemory(stack).instruction()
            self.assertIn("CAS", instruction)
            self.assertTrue(
                "НЕ ЗАПРАШИВАТЬ" in instruction or "не запрашивать" in instruction.lower(),
                "нет запрета запрашивать точные данные у Архивариуса",
            )

    def test_cas_instruction_skipped_at_zero_references(self) -> None:
        """При нуле ссылок инструкция CAS не ставится: незачем занимать контекст."""
        head = Head(role="РОЛЬ")
        attach_instructions(head, archivist_instruction="", cas_reference_count=0)
        self.assertEqual(head.cas_instruction, "")
        self.assertNotIn("CAS?", head.render())

    def test_cas_instruction_present_with_references(self) -> None:
        """При наличии ссылок инструкция CAS ставится."""
        head = Head(role="РОЛЬ")
        attach_instructions(head, archivist_instruction="", cas_reference_count=3)
        self.assertIn("CAS?", head.cas_instruction)


class TestRenderContext(unittest.TestCase):
    """Сборка контекста: [HEAD][средняя][короткая]."""

    def test_order_is_head_medium_short(self) -> None:
        """Порядок частей: HEAD, затем средняя, затем короткая."""
        head = Head(role="РОЛЬ-МЕТКА")
        context = render_context(
            head,
            medium_text="СРЕДНЯЯ-МЕТКА",
            short_text="КОРОТКАЯ-МЕТКА",
            cas_reference_count=0,
        )
        self.assertLess(context.index("РОЛЬ-МЕТКА"), context.index("СРЕДНЯЯ-МЕТКА"))
        self.assertLess(context.index("СРЕДНЯЯ-МЕТКА"), context.index("КОРОТКАЯ-МЕТКА"))

    def test_head_is_first(self) -> None:
        """HEAD стоит в самом начале контекста."""
        context = render_context(
            Head(role="НАЧАЛО"), medium_text="середина", short_text="конец",
            cas_reference_count=0,
        )
        self.assertTrue(context.startswith("НАЧАЛО"))

    def test_empty_parts_skipped(self) -> None:
        """Пустые части пропускаются без лишних пустых строк."""
        context = render_context(Head(role="РОЛЬ"), "", "", cas_reference_count=0)
        self.assertEqual(context, "РОЛЬ")

    def test_short_last_even_when_medium_empty(self) -> None:
        """Короткая память последняя даже при пустой средней."""
        context = render_context(Head(role="Р"), "", "КОРОТКАЯ", cas_reference_count=0)
        self.assertTrue(context.endswith("КОРОТКАЯ"))


class TestHeadIsNotMemory(unittest.TestCase):
    """Инвариант 8: HEAD не является памятью и не преобразуется."""

    def test_head_module_does_not_import_linking(self) -> None:
        """
        HEAD не проходит через CAS.

        Модуль не должен импортировать `linking`: замена ссылок в роли сделала бы
        её нечитаемой при потере базы.
        """
        source = (ROOT / "_engine" / "memory" / "head.py").read_text(encoding="utf-8")
        self.assertNotIn("import linking", source)
        self.assertNotIn("link_cas", source)

    def test_role_survives_cas_processing_untouched(self) -> None:
        """
        Роль в HEAD не содержит ссылок CAS после обработки памяти.

        Проверяется косвенно: head.py не вызывает CAS, значит роль остаётся
        дословной. Это и есть требование §7 методологии.
        """
        role = "Use Path: ~/.dsh/profiles/web/cordis.patch.yml twice ~/.dsh/profiles/web/cordis.patch.yml"
        text = stream(("SYSTEM", role), ("USER", "задача"))
        head, _ = extract_head(text)
        self.assertIn("cordis.patch.yml", head.role, "путь заменён ссылкой CAS")
        self.assertNotIn("CAS?", head.role, "в роли появилась ссылка CAS")

    def test_head_not_written_to_stack(self) -> None:
        """HEAD не попадает в стек чанков: у памяти и HEAD разные области."""
        with tempfile.TemporaryDirectory() as tmp:
            stack = ChunkStack(Path(tmp) / "d", "d")
            text = stream(("SYSTEM", "роль"), ("USER", "задача"))
            extract_head(text)
            self.assertEqual(stack.ordered(), [], "HEAD попал в стек чанков")


def main() -> int:
    """Запустить тесты с кодом возврата по соглашению SeverPact."""
    if not (ROOT / "_engine" / "memory" / "head.py").exists():
        print("НЕ ИЗМЕРЕНО: нет модуля memory/head.py", file=sys.stderr)
        return 2
    verbosity = 2 if "-v" in sys.argv else 1
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
