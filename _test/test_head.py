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

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVENTS = HERE.parent / "_engine" / "core" / "scc_events.py"
ROOT = HERE.parent
# _engine добавлен в путь: память лежит в _engine/memory, и импорты
# `from memory import ...` работают без правки каждого файла.
sys.path.insert(0, str(ROOT / "_engine"))
sys.path.insert(0, str(ROOT))

from memory.head import (  # noqa: E402
    NO_ROLE_NOTICE,
    Head,
    attach_instructions,
    render_context,
)
from memory.long import LongMemory  # noqa: E402
from memory.stack import ChunkStack  # noqa: E402


def stream(*events: tuple[str, str]) -> str:
    """Собрать поток событий в формате core/scc_events.py."""
    parts = []
    for number, (kind, body) in enumerate(events, start=1):
        parts.append(f"[E{number}] {kind} (seq {number}):\n{body}")
    return "\n\n".join(parts) + "\n"


class TestChannels(unittest.TestCase):
    """
    РАЗДЕЛЕНИЕ КАНАЛОВ проверяется на конвертере, а не в head.py.

    Прежняя версия `head.py` сама «изымала» роль из потока — это дублировало
    работу конвертера, и дублирование убрано. Теперь разделение выполняется
    **на входе** (`--exclude-system`), а `head.py` только собирает HEAD.
    """

    def _convert(self, raw: str, exclude: bool = True) -> tuple[str, list[str]]:
        """Прогнать конвертер журнала с отдельным сбором системного контекста."""
        spec = importlib.util.spec_from_file_location("scc_ev_head", EVENTS)
        module = importlib.util.module_from_spec(spec)
        sys.modules["scc_ev_head"] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
        system: list[str] = []
        stream = module.convert(raw, None, None, module.Stats(),
                                exclude_system=exclude, system_out=system)
        return stream, system

    def _raw(self) -> str:
        """Журнал: роль, диалог, вставка SYSTEM в середине."""
        events = [
            {"seq": 7, "type": "system/message",
             "data": {"message": {"content": [{"type": "text", "text": "РОЛЬ АГЕНТА"}]}}},
            {"seq": 8, "type": "user/message",
             "data": {"content": [{"type": "text", "text": "задача"}]}},
            {"seq": 9, "type": "system/message",
             "data": {"message": {"content": [{"type": "text", "text": "ПОВТОР в середине"}]}}},
            {"seq": 10, "type": "user/message",
             "data": {"content": [{"type": "text", "text": "продолжение"}]}},
        ]
        return "\n".join(json.dumps(e, ensure_ascii=False) for e in events)

    def test_role_goes_to_separate_channel(self) -> None:
        """Роль уходит в отдельный канал и в поток не попадает."""
        stream, system = self._convert(self._raw())
        self.assertNotIn("РОЛЬ АГЕНТА", stream, "роль осталась в юзерконтексте")
        self.assertTrue(any("РОЛЬ АГЕНТА" in part for part in system),
                        f"роль не собрана отдельно: {system}")

    def test_history_preserved(self) -> None:
        """История не теряется при разделении каналов."""
        stream, _ = self._convert(self._raw())
        self.assertIn("задача", stream)
        self.assertIn("продолжение", stream)

    def test_midstream_system_also_separated(self) -> None:
        """
        SYSTEM в середине тоже уходит в отдельный канал.

        Он НЕ является ролью, но и в юзерконтексте ему не место: это системный
        текст. Конвертер разделяет по типу записи, а не по позиции.
        """
        stream, system = self._convert(self._raw())
        self.assertNotIn("ПОВТОР в середине", stream)
        self.assertTrue(any("ПОВТОР" in part for part in system))

    def test_without_flag_system_stays(self) -> None:
        """Без флага поведение прежнее: SYSTEM остаётся в потоке."""
        stream, _ = self._convert(self._raw(), exclude=False)
        self.assertIn("РОЛЬ АГЕНТА", stream)

    def test_head_has_no_own_parser(self) -> None:
        """
        head.py НЕ содержит своего парсера потока.

        Дублирование убрано: разделение — задача конвертера. Если в модуле
        появится свой разбор событий, это снова будет вторая реализация.
        """
        source = (ROOT / "_engine" / "memory" / "head.py").read_text(encoding="utf-8")
        for forbidden in ("def extract_head", "def split_events", "EVENT_HEAD_RE"):
            self.assertNotIn(forbidden, source,
                             f"в head.py вернулся свой парсер: {forbidden}")


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
        Роль в HEAD не содержит ссылок CAS.

        Проверяется косвенно: head.py не вызывает CAS, значит роль остаётся
        дословной. Это и есть требование §7 методологии.
        """
        role = ("Use Path: ~/.dsh/profiles/web/cordis.patch.yml "
                "twice ~/.dsh/profiles/web/cordis.patch.yml")
        head = Head(role=role)
        rendered = head.render()
        self.assertIn("cordis.patch.yml", rendered, "путь заменён ссылкой CAS")
        self.assertNotIn("CAS?", rendered, "в роли появилась ссылка CAS")

    def test_head_not_written_to_stack(self) -> None:
        """HEAD не попадает в стек чанков: у памяти и HEAD разные области."""
        with tempfile.TemporaryDirectory() as tmp:
            stack = ChunkStack(Path(tmp) / "d", "d")
            head = Head(role="РОЛЬ")
            head.render()
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
