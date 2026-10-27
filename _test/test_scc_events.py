#!/usr/bin/env python3
"""
SCC — тесты конвертера журнала в поток событий
==============================================

ЧТО ПРОВЕРЯЕТСЯ
---------------
Конвертер стоит первым в цепочке: если он ошибётся, дальше уйдут неверные
события, и свёртка будет сворачивать не то. Проверяются инварианты:

1. служебные записи журнала (step/start, turn/end, request/header) не попадают
   в поток — они не события, а разметка цикла;
2. reasoning НЕ выбрасывается, а помечается счётчиком: смысл теряется, факт
   «цикл был» — сохраняется;
3. вызовы инструментов перечисляются поимённо с аргументами;
4. диапазон seq фильтруется точно, без сдвига нумерации E;
5. пустой журнал даёт код 1 (нарушение), а не 0;
6. нечитаемый файл даёт код 2 (не измерено), а не 0.

Запуск:
    python3 tests/test_scc_events.py -v
Код возврата: 0 — прошли, 1 — падения, 2 — не удалось запустить.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = HERE.parent / "_engine" / "core"
EVENTS = CORE / "scc_events.py"


def load_module():
    """
    Загрузить scc_events.py как модуль (файл без пакета).

    Модуль обязан быть зарегистрирован в `sys.modules` ДО исполнения:
    `@dataclass` разрешает строковые аннотации через `sys.modules[cls.__module__]`,
    и без регистрации падает с AttributeError на None.
    """
    spec = importlib.util.spec_from_file_location("scc_events", EVENTS)
    module = importlib.util.module_from_spec(spec)
    sys.modules["scc_events"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


EV = load_module()


def journal(*events: dict) -> str:
    """Собрать журнал в формате JSONL."""
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in events)


class TestTextOf(unittest.TestCase):
    """Извлечение текста из блоков контента."""

    def test_collects_text_and_reasoning(self) -> None:
        """Текстовые и reasoning-блоки собираются, прочие игнорируются."""
        content = [
            {"type": "text", "text": "привет"},
            {"type": "tool-call", "name": "read"},
            {"type": "reasoning", "text": "думаю"},
        ]
        self.assertEqual(EV.text_of(content), "привет\nдумаю")

    def test_non_list_returns_empty(self) -> None:
        """Не-список не падает, а даёт пустую строку."""
        self.assertEqual(EV.text_of(None), "")
        self.assertEqual(EV.text_of("строка"), "")


class TestRenderEvent(unittest.TestCase):
    """Превращение записи журнала в событие потока."""

    def setUp(self) -> None:
        self.stats = EV.Stats()

    def test_service_events_are_skipped(self) -> None:
        """Служебная разметка не становится событием."""
        for kind in ("step/start", "step/end", "turn/start", "turn/end", "request/header"):
            self.assertIsNone(
                EV.render_event({"type": kind, "seq": 1, "data": {}}, 1, self.stats),
                f"{kind} не должен попадать в поток",
            )

    def test_user_message(self) -> None:
        """Сообщение пользователя попадает целиком."""
        event = {
            "type": "user/message",
            "seq": 8,
            "data": {"content": [{"type": "text", "text": "задача"}]},
        }
        rendered = EV.render_event(event, 1, self.stats)
        self.assertIn("[E1] USER (seq 8)", rendered)
        self.assertIn("задача", rendered)

    def test_system_message_is_event(self) -> None:
        """Шапка роли — тоже событие (требование правила 6 этапа I)."""
        event = {
            "type": "system/message",
            "seq": 3,
            "data": {"message": {"content": [{"type": "text", "text": "ты агент"}]}},
        }
        rendered = EV.render_event(event, 0, self.stats)
        self.assertIn("SYSTEM", rendered)
        self.assertIn("ты агент", rendered)

    def test_reasoning_is_counted_not_dropped(self) -> None:
        """ГЛАВНЫЙ инвариант: reasoning помечается, а не исчезает молча."""
        event = {
            "type": "assistant/message",
            "seq": 15,
            "data": {
                "message": {
                    "content": [
                        {"type": "reasoning", "text": "x" * 500},
                        {"type": "tool-call", "name": "read", "arguments": '{"file_path":"/a.txt"}'},
                    ]
                }
            },
        }
        rendered = EV.render_event(event, 3, self.stats)
        self.assertIn("reasoning=1 blocks/500 chars", rendered)
        self.assertEqual(self.stats.reasoning_blocks, 1)
        self.assertEqual(self.stats.reasoning_chars, 500)

    def test_tool_calls_listed_with_arguments(self) -> None:
        """Вызовы инструментов перечисляются с аргументами дословно."""
        event = {
            "type": "assistant/message",
            "seq": 15,
            "data": {
                "message": {
                    "content": [
                        {"type": "tool-call", "name": "read", "arguments": '{"file_path":"/data/x.txt"}'}
                    ]
                }
            },
        }
        rendered = EV.render_event(event, 3, self.stats)
        self.assertIn("tool_call read", rendered)
        self.assertIn("/data/x.txt", rendered)
        self.assertEqual(self.stats.tool_calls, 1)

    def test_tool_result_kept_whole(self) -> None:
        """Результат инструмента — это данные, он сохраняется целиком."""
        event = {
            "type": "tool/result",
            "seq": 26,
            "data": {"message": {"content": [{"type": "text", "text": "h_0: abc"}]}},
        }
        rendered = EV.render_event(event, 4, self.stats)
        self.assertIn("TOOL_RESULT (seq 26)", rendered)
        self.assertIn("h_0: abc", rendered)


class TestConvert(unittest.TestCase):
    """Сборка потока из журнала."""

    def test_sequence_filter_is_exact(self) -> None:
        """Фильтр seq не сдвигает нумерацию E: она сплошная."""
        raw = journal(
            {"type": "user/message", "seq": 5, "data": {"content": [{"type": "text", "text": "до"}]}},
            {"type": "user/message", "seq": 10, "data": {"content": [{"type": "text", "text": "внутри"}]}},
            {"type": "user/message", "seq": 20, "data": {"content": [{"type": "text", "text": "после"}]}},
        )
        stats = EV.Stats()
        stream = EV.convert(raw, 8, 15, stats)
        self.assertIn("[E1] USER (seq 10)", stream)
        self.assertNotIn("до", stream)
        self.assertNotIn("после", stream)
        self.assertEqual(stats.state_events, 1)

    def test_broken_json_lines_are_skipped(self) -> None:
        """Битая строка журнала пропускается, поток не падает."""
        raw = 'не json\n{"type":"user/message","seq":1,"data":{"content":[{"type":"text","text":"ок"}]}}'
        stats = EV.Stats()
        stream = EV.convert(raw, None, None, stats)
        self.assertIn("ок", stream)
        self.assertEqual(stats.state_events, 1)


class TestCli(unittest.TestCase):
    """Коды возврата CLI."""

    def test_missing_file_is_code_2(self) -> None:
        """Нечитаемый журнал -> код 2 (не измерено), не 0."""
        result = subprocess.run(
            [sys.executable, str(EVENTS), "/нет/такого/журнала.jsonl.zstd"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)

    def test_no_state_events_is_code_1(self) -> None:
        """Журнал без событий работы -> код 1 (нарушение)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "j.jsonl"
            path.write_text(journal({"type": "step/start", "seq": 1, "data": {}}), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(EVENTS), str(path)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)

    def test_real_journal_produces_stream(self) -> None:
        """Журнал с событиями -> код 0 и записанный поток."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "j.jsonl"
            out = Path(tmp) / "events.txt"
            path.write_text(
                journal(
                    {"type": "system/message", "seq": 3, "data": {"message": {"content": [{"type": "text", "text": "роль"}]}}},
                    {"type": "user/message", "seq": 8, "data": {"content": [{"type": "text", "text": "задача"}]}},
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(EVENTS), str(path), "--out", str(out)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            text = out.read_text(encoding="utf-8")
            self.assertIn("роль", text)
            self.assertIn("задача", text)


def main() -> int:
    """Запустить тесты с кодом возврата по соглашению SeverPact."""
    if not EVENTS.exists():
        print(f"НЕ ИЗМЕРЕНО: нет инструмента {EVENTS}", file=sys.stderr)
        return 2
    verbosity = 2 if "-v" in sys.argv else 1
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
