#!/usr/bin/env python3
"""
SCC — тесты прогона этапов (сборка промта и CLI)
=================================================

ЧТО ПРОВЕРЯЕТСЯ
---------------
`LLM` в тестах НЕ вызывается: проверяется то, что можно проверить без сети —
сборка промта, подстановка документов и коды возврата. Это ровно те места, где
при разработке были найдены два реальных бага:

1. `--list-prompts` был объявлен подкомандой вместо флага — CLI падал с
   «unrecognized arguments»;
2. имя этапа не сохранялось в `args` у подкоманд — `cmd_stage` падал с
   «'Namespace' object has no attribute 'stage'».

Оба бага ловятся именно здесь, без единого обращения к модели.

Что проверяется:
1. все три промта существуют и содержат нужное число мест подстановки;
2. число документов на входе этапа совпадает с числом мест в промте;
3. подстановка не оставляет плейсхолдеров `{{INPUT_n}}`;
4. чужой этап с неверным числом документов -> код 2;
5. отсутствие ключа -> код 2 (не измерено), а не падение;
6. `--list-prompts` работает как флаг -> код 0.

Запуск:
    python3 tests/test_scc_run.py -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CORE = ROOT / "core"
RUNNER = CORE / "scc_run.py"


def load_module():
    """Загрузить scc_run.py как модуль, зарегистрировав его в sys.modules."""
    spec = importlib.util.spec_from_file_location("scc_run", RUNNER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["scc_run"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


RUN = load_module()


class TestPromptsExist(unittest.TestCase):
    """Промты на месте и соответствуют описанию этапов."""

    def test_all_prompts_exist(self) -> None:
        """Каждый объявленный этап имеет файл промта."""
        for name, spec in RUN.STAGES.items():
            path = RUN.PROMPTS / str(spec["prompt"])
            self.assertTrue(path.exists(), f"{name}: нет промта {path}")

    def test_placeholders_match_input_count(self) -> None:
        """Число мест {{INPUT_n}} совпадает с числом документов этапа."""
        for name, spec in RUN.STAGES.items():
            text = (RUN.PROMPTS / str(spec["prompt"])).read_text(encoding="utf-8")
            count = int(spec["inputs"])
            for index in range(1, count + 1):
                self.assertIn(
                    f"{{{{INPUT_{index}}}}}", text,
                    f"{name}: в промте нет места INPUT_{index}",
                )


class TestPromptAssembly(unittest.TestCase):
    """Сборка промта: подстановка документов."""

    def test_documents_are_substituted(self) -> None:
        """Документы подставляются, плейсхолдеры не остаются."""
        for name, spec in RUN.STAGES.items():
            docs = [f"СОДЕРЖИМОЕ_{i}" for i in range(1, int(spec["inputs"]) + 1)]
            prompt = RUN.build_prompt(name, docs, True)
            self.assertIsNotNone(prompt, f"{name}: промт не собрался")
            self.assertNotIn("{{INPUT", prompt, f"{name}: остался плейсхолдер")
            for doc in docs:
                self.assertIn(doc, prompt, f"{name}: документ не подставлен")

    def test_stage3_has_marker_by_default(self) -> None:
        """По умолчанию этап III требует маркер (_SSC_)."""
        prompt = RUN.build_prompt("stage3", ["A", "B"], True)
        self.assertIsNotNone(prompt)
        self.assertIn("(_SSC_)", prompt)

    def test_stage3_marker_can_be_disabled(self) -> None:
        """Режим без маркера убирает и правило, и пример формата."""
        with_marker = RUN.build_prompt("stage3", ["A", "B"], True)
        without = RUN.build_prompt("stage3", ["A", "B"], False)
        self.assertIsNotNone(with_marker)
        self.assertIsNotNone(without)
        self.assertLess(without.count("(_SSC_)"), with_marker.count("(_SSC_)"))

    def test_missing_prompt_returns_none(self) -> None:
        """Отсутствующий промт -> None (это код 2 у вызывающего)."""
        original = RUN.PROMPTS
        try:
            RUN.PROMPTS = Path("/нет/такого/каталога")
            self.assertIsNone(RUN.build_prompt("stage1", ["A"], True))
        finally:
            RUN.PROMPTS = original


class TestCli(unittest.TestCase):
    """Коды возврата CLI — те самые два бага при разработке."""

    def test_list_prompts_is_flag(self) -> None:
        """--list-prompts работает как флаг (баг №1: был подкомандой)."""
        result = subprocess.run(
            [sys.executable, str(RUNNER), "--list-prompts"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stage1", result.stdout)
        self.assertIn("stage3", result.stdout)

    def test_stage_name_reaches_handler(self) -> None:
        """Имя этапа доходит до обработчика (баг №2: терялся в подкоманде).

        Проверяем без вызова модели: неверное число документов обязано дать
        код 2 из cmd_stage, а не падение AttributeError.
        """
        result = subprocess.run(
            [sys.executable, str(RUNNER), "stage1", "a.txt", "b.txt", "--out", "/tmp/x"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn("AttributeError", result.stderr)
        self.assertIn("принимает 1", result.stderr)

    def test_missing_input_file_is_code_2(self) -> None:
        """Нет входного документа -> код 2, без падения."""
        result = subprocess.run(
            [sys.executable, str(RUNNER), "stage1", "/нет/такого.txt", "--out", "/tmp/x"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_key_is_code_2(self) -> None:
        """Нет ключа API -> код 2 (не измерено), а не исключение."""
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "in.txt"
            doc.write_text("данные", encoding="utf-8")
            env = dict(os.environ)
            env.pop(RUN.API_KEY_ENV, None)
            result = subprocess.run(
                [sys.executable, str(RUNNER), "stage1", str(doc), "--out", str(Path(tmp) / "o.md")],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("нет ключа", result.stderr)

    def test_no_arguments_is_code_2(self) -> None:
        """Без аргументов -> код 2 и подсказка, а не молчаливый 0."""
        result = subprocess.run(
            [sys.executable, str(RUNNER)], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)


class TestTransitMode(unittest.TestCase):
    """
    ТРАНЗИТНЫЙ РЕЖИМ: принять контекст, преобразовать, отдать — НЕ храня.

    Режим транзита: хранение контекста запрещено, поэтому результат уходит в
    stdout и на диск не пишется. Файл в промежутке — это и есть хранение,
    поэтому он не создаётся вовсе, а не удаляется после.

    Проверки идут без вызова модели: режим отказа проверяется до обращения к
    API, а значит коды возврата видны и без сети.
    """

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RUNNER), *args],
            capture_output=True, text=True,
        )

    def test_refuses_without_any_destination(self) -> None:
        """Без --out и без --to-stdout запуск отвергается понятной ошибкой.

        Иначе было бы Path(None) с невнятным падением.
        """
        result = self._run("stage1", "/tmp/nonexistent_input.txt")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--to-stdout", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_refuses_both_destinations(self) -> None:
        """--to-stdout вместе с --out отвергается: транзит не пишет файл.

        Без этой проверки файл был бы записан в режиме, который обязан не
        хранить — то есть запрет хранения нарушался бы молча.
        """
        result = self._run(
            "stage1", "/tmp/nonexistent_input.txt",
            "--out", "/tmp/should_not_exist.md", "--to-stdout",
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("НАРУШЕНИЕ", result.stderr)
        self.assertFalse(
            Path("/tmp/should_not_exist.md").exists(),
            "в транзитном режиме создан файл",
        )

    def test_flag_is_documented(self) -> None:
        """--to-stdout есть в справке этапа."""
        result = self._run("stage1", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--to-stdout", result.stdout)
        self.assertIn("НЕ писать на диск", result.stdout)

    def test_out_is_no_longer_mandatory(self) -> None:
        """--out перестал быть обязательным: есть альтернатива."""
        result = self._run("stage1", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        # При обязательном --out argparse печатал бы "required"
        self.assertNotIn("required", result.stdout)

    def test_no_ssc_flag_supported(self) -> None:
        """--no-ssc доступен: в транзите повторной свёртки нет, маркер не нужен."""
        result = self._run("stage3", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--no-ssc", result.stdout)


class TestOutputValidation(unittest.TestCase):
    """
    Живой урок: `finish_reason=stop` не гарантирует, что этап выполнен.

    Замер 2026-10-24: при одном из прогонов с поднятым потолком модель ушла в
    роль агента — выдала bash-команды вместо извлечения данных (0 событий из
    892), и `finish_reason` был `stop`, то есть формально «успех». Валидация
    содержания обязана ловить такой вывод.
    """

    def _accepted(self, content: str) -> bool:
        """Повторить проверку из scc_run: есть ли в выводе события E<N>."""
        import re

        return bool(re.search(r"^(?:\*\*)?E\d+", content, re.M))

    def test_garbage_agent_output_rejected(self) -> None:
        """Вывод-«роль агента» без событий не принимается."""
        garbage = "Понял задачу: пойду выполнять\n```bash\nls -la\n```\n"
        self.assertFalse(self._accepted(garbage))

    def test_normal_wr_accepted(self) -> None:
        """Обычный WR принимается."""
        self.assertTrue(self._accepted("E0: SYSTEM\n\nE1: USER\n"))

    def test_markdown_wr_accepted(self) -> None:
        """Markdown-формат заголовков принимается (см. R-SCC-02, дефект 1)."""
        self.assertTrue(self._accepted("**E0: SYSTEM**\n\n**E1: USER**\n"))

    def test_range_heading_accepted(self) -> None:
        """Свёрнутый диапазон считается событием."""
        self.assertTrue(self._accepted("E4–E13: TOOL_RESULT\n"))

    def test_empty_rejected(self) -> None:
        """Пустой вывод не принимается."""
        self.assertFalse(self._accepted(""))

    def test_truncated_tail_rejected(self) -> None:
        """Обрыв на полуслове без заголовков не принимается."""
        self.assertFalse(self._accepted("ТОЧНЫЕ ДАННЫЕ (994 chars): состав из 13"))

    def test_default_max_tokens_is_reported(self) -> None:
        """Дефолт потолка вывода задан и выше прежнего лимита 131072."""
        self.assertGreater(RUN.DEFAULT_MAX_TOKENS, 131072)


class TestRetries(unittest.TestCase):
    """
    Автоповтор: сбой стохастичен, повтор обязателен.

    Замер 2026-10-24: прогон этапа II с идентичными параметрами вернул
    0 событий, а повтор на том же входе дал 892. Потеря артефакта означает
    потерю части контекста, поэтому цепочка не должна требовать человека там,
    где достаточно повторить. `call_llm` подменяется — сети нет.
    """

    def _args(self, **over):
        import argparse

        base = dict(
            model="m", base_url="u", timeout=1, max_tokens=1000, retries=2
        )
        base.update(over)
        return argparse.Namespace(**base)

    def _fake(self, sequence):
        """
        Подменить call_llm последовательностью ответов.

        Вывод этапа подавляется: функция печатает журнал попыток, и без этого
        он засоряет результаты тестов.
        """
        it = iter(sequence)
        original = RUN.call_llm
        RUN.call_llm = lambda *a, **k: next(it)
        self.addCleanup(lambda: setattr(RUN, "call_llm", original))
        import contextlib
        import io

        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(lambda: quiet.__exit__(None, None, None))

    @staticmethod
    def _reply(content: str, finish: str = "stop") -> dict:
        return {
            "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"completion_tokens": 1},
        }

    def test_garbage_then_success(self) -> None:
        """Мусор на первой попытке — повтор даёт норму."""
        self._fake([self._reply("Понял задачу, пойду bash"), self._reply("E0: SYSTEM\nE1: USER\n")])
        content, diag, log = RUN.run_with_retries("p", self._args(), "k")
        self.assertIsNone(diag)
        self.assertEqual(len(log), 2)
        self.assertGreater(RUN.count_events(content), 0)

    def test_truncation_escalates_limit(self) -> None:
        """Обрыв по потолку поднимает потолок на следующей попытке."""
        self._fake([
            self._reply("E0: SYSTEM", finish="length"),
            self._reply("E0: SYSTEM\nE1: USER\n"),
        ])
        _, diag, log = RUN.run_with_retries("p", self._args(), "k")
        self.assertIsNone(diag)
        self.assertEqual(log[0]["max_tokens"], 1000)
        self.assertGreater(log[1]["max_tokens"], 1000, "потолок не поднялся при обрыве")

    def test_all_garbage_fails_honestly(self) -> None:
        """Если все попытки мусорные — код нарушения и диагноз."""
        self._fake([self._reply("мусор")] * 3)
        _, diag, log = RUN.run_with_retries("p", self._args(), "k")
        self.assertIsNotNone(diag)
        self.assertEqual(len(log), 3)

    def test_best_attempt_is_kept(self) -> None:
        """Сохраняется ЛУЧШАЯ попытка, а не последняя."""
        big = "\n".join(f"E{i}: x" for i in range(50))
        self._fake([
            self._reply(big, finish="length"),
            self._reply("мусор"),
            self._reply("мусор2"),
        ])
        content, diag, _ = RUN.run_with_retries("p", self._args(), "k")
        self.assertIsNotNone(diag)
        self.assertEqual(RUN.count_events(content), 50, "удачная попытка затёрта мусором")

    def test_zero_retries_makes_one_attempt(self) -> None:
        """--retries 0 = ровно одна попытка, без повтора."""
        self._fake([self._reply("мусор")])
        _, diag, log = RUN.run_with_retries("p", self._args(retries=0), "k")
        self.assertIsNotNone(diag)
        self.assertEqual(len(log), 1)

    def test_network_failure_is_retried(self) -> None:
        """Сетевой отказ тоже повторяется, а не роняет цепочку."""
        original = RUN.call_llm
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return self._reply("E0: SYSTEM\n")

        RUN.call_llm = flaky
        self.addCleanup(lambda: setattr(RUN, "call_llm", original))
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            content, diag, log = RUN.run_with_retries("p", self._args(), "k")
        self.assertIsNone(diag)
        self.assertEqual(len(log), 2)

    def test_count_events_handles_markdown(self) -> None:
        """Счётчик событий понимает Markdown-заголовки."""
        self.assertEqual(RUN.count_events("**E0: x**\n**E1: y**\n"), 2)
        self.assertEqual(RUN.count_events("мусор"), 0)

    def test_self_limiting_output_is_retried(self) -> None:
        """
        Дефект 5: модель отдала 51 событие из 891 и предложила «батчи».

        Обе прежние проверки это пропускали: события есть, обрыва нет. Но это
        потеря 94 % контекста, поэтому такая попытка обязана считаться сбоем.
        """
        partial = "\n".join(f"E{i}: x" for i in range(51))
        full = "\n".join(f"E{i}: x" for i in range(891))
        self._fake([self._reply(partial), self._reply(full)])
        with contextlib.redirect_stdout(io.StringIO()):
            content, diag, log = RUN.run_with_retries(
                "p", self._args(), "k", source_events=891
            )
        self.assertIsNone(diag)
        self.assertEqual(len(log), 2, "самоограничение не вызвало повтор")
        self.assertEqual(RUN.count_events(content), 891)

    def test_full_coverage_passes_first_try(self) -> None:
        """Полное покрытие проходит с первой попытки, без лишних вызовов."""
        full = "\n".join(f"E{i}: x" for i in range(891))
        self._fake([self._reply(full)])
        with contextlib.redirect_stdout(io.StringIO()):
            _, diag, log = RUN.run_with_retries("p", self._args(), "k", source_events=891)
        self.assertIsNone(diag)
        self.assertEqual(len(log), 1, "лишний повтор при полном покрытии")

    def test_source_counter_understands_both_formats(self) -> None:
        """Счётчик входа понимает и [E1] потока, и E1: в WR."""
        self.assertEqual(RUN.count_source_events("[E1] USER\n[E2] TOOL\n"), 2)
        self.assertEqual(RUN.count_source_events("E1: USER\nE2: TOOL\n"), 2)
        self.assertEqual(RUN.count_source_events("**E1: USER**\n"), 1)

    def test_range_counts_all_covered_events(self) -> None:
        """
        Диапазон E4–E13 покрывает ДЕСЯТЬ событий, а не одно.

        Без этого проверка покрытия на T2 дала бы 43/159 = 27 % и объявила
        ложный сбой, хотя покрыто всё.
        """
        text = "E0: SYSTEM\nE4–E13: прочитаны файлы\n"
        self.assertEqual(RUN.count_events(text), 2, "заголовков два")
        self.assertEqual(RUN.count_covered_events(text), 11, "покрыто 1 + 10")

    def test_repeated_numbers_are_not_double_counted(self) -> None:
        """
        Повторные упоминания номера не удваивают покрытие.

        Замерено на потоке: суммирование диапазонов дало 998 совпадений вместо
        893 событий — диапазоны цитировались в тексте.
        """
        text = "E1: a\nE2: b\nE1: a снова\nE2: b снова\n"
        self.assertEqual(RUN.count_events(text), 4)
        self.assertEqual(RUN.count_covered_events(text), 2, "номера должны схлопнуться")

    def test_reverse_range_is_safe(self) -> None:
        """Обратный диапазон не даёт отрицательного счёта."""
        self.assertEqual(RUN.count_covered_events("E50-E10: опечатка\n"), 1)

    def test_huge_range_does_not_explode(self) -> None:
        """Цитата с огромным диапазоном не разворачивается в память."""
        self.assertLessEqual(RUN.count_covered_events("E1–E999999999: цитата\n"), 3)


def main() -> int:
    """Запустить тесты с кодом возврата по соглашению SeverPact."""
    if not RUNNER.exists():
        print(f"НЕ ИЗМЕРЕНО: нет инструмента {RUNNER}", file=sys.stderr)
        return 2
    verbosity = 2 if "-v" in sys.argv else 1
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
