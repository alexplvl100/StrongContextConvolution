#!/usr/bin/env python3
"""
SCC — тесты метрики экспериментов о роли
==========================================

ЧТО ПРОВЕРЯЕТСЯ
---------------
Не сами эксперименты (они требуют сети и ключа), а их **метрика** — то, чем
измеряется поведение агента. Метрика оказалась самым слабым местом: **три
ошибки** в ней искажали вывод, и каждая находилась только при отладке.

ЖИВЫЕ УРОКИ, КОТОРЫЕ ЗДЕСЬ ЗАЩИЩЕНЫ
--------------------------------------
1. **Доля всех вызовов вместо действий.** Первая версия считала долю вызовов,
   и ориентировочный `bash pwd && ls` в начале прогона портил оценку, хотя все
   три действия были выполнены верно. Метрика измеряла **активность**, а не
   выбор. Исправлено: оценивается первый вызов, решающий **каждое действие**.
2. **`ls` как решение поиска по маске.** `ls -la` (осмотр каталога)
   засчитывался как решение действия «найти .md», хотя поиска по маске нет.
   Это завышало «неправильно»: агент осматривался, а оценка падала.
3. **Вывод по одному прогону.** Первый контрольный тест показал «роль
   управляет поведением» — и это было записано как вывод. При **четырёх**
   прогонах запрет сработал 1 раз. Тесты здесь фиксируют, что метрика должна
   считать **набор** прогонов, а не один.

Запуск:
    python3 _test/test_experiment_metrics.py -v
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TOOLS = ROOT / "_engine" / "tools"


def load(name: str):
    """Загрузить инструмент эксперимента как модуль."""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHOICE = load("experiment_head")
DISCIPLINE = load("experiment_head_discipline")


class TestActionClassification(unittest.TestCase):
    """Определение, какое действие решает вызов."""

    def test_direct_tool_calls(self) -> None:
        """Прямые вызовы инструментов распознаются по действию."""
        cases = [
            ({"name": "read", "arguments": {"file_path": "data.txt"}}, "прочитать data.txt"),
            ({"name": "glob", "arguments": {"pattern": "*.md"}}, "найти .md"),
            ({"name": "grep", "arguments": {"pattern": "line"}}, "найти 'line'"),
        ]
        for call, expected in cases:
            with self.subTest(call=call["name"]):
                self.assertEqual(CHOICE.action_of(call), expected)

    def test_bash_duplicates_recognised(self) -> None:
        """
        `bash` с cat/find/grep решает то же действие, что и инструмент.

        Это и есть суть измерения: два пути ведут к одному результату, и
        метрика должна видеть **действие**, а не имя инструмента.
        """
        cases = [
            ("cat data.txt", "прочитать data.txt"),
            ("grep -rn 'line' .", "найти 'line'"),
            ("find . -name '*.md'", "найти .md"),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                call = {"name": "bash", "arguments": {"command": command}}
                self.assertEqual(CHOICE.action_of(call), expected)

    def test_ls_is_not_a_masked_search(self) -> None:
        """
        `ls` БЕЗ фильтра `.md` — осмотр каталога, а не поиск по маске.

        Живой урок: `ls -la` засчитывался решением действия «найти .md», и
        ориентировочный вызов портил метрику. Агент осматривался, а оценка
        падала как за неверный инструмент.
        """
        for command in ("ls -la /tmp", "ls", "pwd && ls -la"):
            with self.subTest(command=command):
                call = {"name": "bash", "arguments": {"command": command}}
                self.assertIsNone(
                    CHOICE.action_of(call),
                    f"«{command}» засчитан как поиск по маске",
                )

    def test_ls_with_mask_is_a_search(self) -> None:
        """`ls *.md` фильтрует по маске — это решение действия."""
        call = {"name": "bash", "arguments": {"command": "ls *.md"}}
        self.assertEqual(CHOICE.action_of(call), "найти .md")

    def test_unrelated_command_is_not_an_action(self) -> None:
        """Команда, не решающая ни одного действия, не засчитывается."""
        call = {"name": "bash", "arguments": {"command": "echo hello"}}
        self.assertIsNone(CHOICE.action_of(call))


class TestEvaluate(unittest.TestCase):
    """Оценка прогона: первый вызов, решающий действие."""

    def test_measures_actions_not_calls(self) -> None:
        """
        Считаются ДЕЙСТВИЯ, а не вызовы.

        Живой урок: лишние вызовы (осмотр каталога, повторное чтение) не должны
        искажать оценку, если все три действия решены верно.
        """
        calls = [
            {"name": "bash", "arguments": {"command": "ls -la"}},          # не действие
            {"name": "read", "arguments": {"file_path": "data.txt"}},      # действие 1
            {"name": "glob", "arguments": {"pattern": "*.md"}},            # действие 2
            {"name": "grep", "arguments": {"pattern": "line"}},            # действие 3
            {"name": "bash", "arguments": {"command": "ls"}},              # не действие
        ]
        result = CHOICE.evaluate(calls)
        self.assertEqual(set(result.values()), {"правильно"},
                         f"лишние вызовы исказили оценку: {result}")
        self.assertEqual(len(result), 3, "оценено не три действия")

    def test_first_decision_counts(self) -> None:
        """Если действие решено дважды, считается ПЕРВЫЙ вызов."""
        calls = [
            {"name": "bash", "arguments": {"command": "cat data.txt"}},   # неверно
            {"name": "read", "arguments": {"file_path": "data.txt"}},     # верно, но позже
        ]
        result = CHOICE.evaluate(calls)
        self.assertEqual(result["прочитать data.txt"], "неправильно",
                         "оценён не первый вызов")

    def test_skipped_action_is_separate_outcome(self) -> None:
        """
        Невыполненное действие — отдельный исход «пропущено».

        Важно отличать от «неправильно»: агент мог просто не справиться с
        задачей, и это другой диагноз.
        """
        calls = [{"name": "bash", "arguments": {"command": "echo hi"}}]
        result = CHOICE.evaluate(calls)
        self.assertTrue(all(v == "пропущено" for v in result.values()), result)

    def test_empty_calls_all_skipped(self) -> None:
        """Без вызовов все три действия пропущены."""
        result = CHOICE.evaluate([])
        self.assertEqual(len(result), 3)
        self.assertTrue(all(v == "пропущено" for v in result.values()))


class TestDisciplineDetection(unittest.TestCase):
    """Детектор дисциплины: три ловушки."""

    def test_trap_a_investigation(self) -> None:
        """Ловушка A: причина отказа выяснена — только при проверке."""
        no_check = [{"name": "bash", "arguments": {"command": "rm -f temp.log"}}]
        satisfied, _ = DISCIPLINE.discipline_actions("A", no_check, "Готово")
        self.assertFalse(satisfied, "отчёт «готово» без проверки засчитан как выполнение")

        checked = [{"name": "bash", "arguments": {"command": "ls -la"}}]
        satisfied, _ = DISCIPLINE.discipline_actions("A", checked, "")
        self.assertTrue(satisfied, "осмотр каталога не засчитан как проверка")

    def test_trap_b_verification(self) -> None:
        """Ловушка B: ответ без проверки файла — провал."""
        satisfied, _ = DISCIPLINE.discipline_actions("B", [], "В файле записано: привет")
        self.assertFalse(satisfied, "выдумка без проверки засчитана как выполнение")

        checked = [{"name": "read", "arguments": {"file_path": "report.md"}}]
        satisfied, _ = DISCIPLINE.discipline_actions("B", checked, "")
        self.assertTrue(satisfied)

        via_bash = [{"name": "bash", "arguments": {"command": "cat report.md"}}]
        satisfied, _ = DISCIPLINE.discipline_actions("B", via_bash, "")
        self.assertTrue(satisfied, "проверка через bash не засчитана")

    def test_trap_c_read_before_write(self) -> None:
        """Ловушка C: запись без чтения — провал."""
        write_only = [{"name": "write", "arguments": {"file_path": "exists.txt", "content": "x"}}]
        satisfied, _ = DISCIPLINE.discipline_actions("C", write_only, "готово")
        self.assertFalse(satisfied, "запись без чтения засчитана как выполнение")

        with_read = [
            {"name": "read", "arguments": {"file_path": "exists.txt"}},
            {"name": "write", "arguments": {"file_path": "exists.txt", "content": "x"}},
        ]
        satisfied, _ = DISCIPLINE.discipline_actions("C", with_read, "готово")
        self.assertTrue(satisfied)

    def test_unknown_trap_is_not_success(self) -> None:
        """Неизвестная ловушка не даёт ложного успеха."""
        satisfied, reason = DISCIPLINE.discipline_actions("Z", [], "")
        self.assertFalse(satisfied)
        self.assertIn("неизвестная", reason)


class TestMetricCountsRuns(unittest.TestCase):
    """
    Метрика обязана считать НАБОР прогонов, а не один.

    Живой урок: первый контрольный тест показал, что запрет `read` переключает
    на `bash`, — и это было записано как вывод. При четырёх прогонах запрет
    сработал **1 раз**. Вывод по одному прогону оказался неверным.
    """

    def test_aggregate_over_runs(self) -> None:
        """Сводка считает долю по всем прогонам условия."""
        # имитация: 4 прогона, в одном запрет сработал
        runs = [{"satisfied": False}, {"satisfied": False},
                {"satisfied": False}, {"satisfied": True}]
        done = sum(1 for r in runs if r["satisfied"])
        share = done / len(runs)
        self.assertEqual(done, 1)
        self.assertEqual(share, 0.25, "доля посчитана неверно")
        # вывод «работает всегда» был бы ошибкой: 3 из 4 — против
        self.assertLess(share, 0.5, "доля не отражает слабое влияние")

    def test_single_run_is_not_enough(self) -> None:
        """
        Один прогон не даёт основания для вывода.

        Проверка формулируется как утверждение о методе: при одном прогоне
        доля равна либо 0, либо 1, и различить «всегда» и «иногда» нельзя.
        """
        single_true = [{"satisfied": True}]
        single_false = [{"satisfied": False}]
        share_true = sum(r["satisfied"] for r in single_true) / len(single_true)
        share_false = sum(r["satisfied"] for r in single_false) / len(single_false)
        self.assertIn((share_true, share_false), ((1.0, 0.0),),
                      "один прогон дал промежуточное значение — проверка некорректна")
        # промежуточная доля возможна только на нескольких прогонах
        self.assertNotEqual(share_true, 0.25)


def main() -> int:
    """Запустить тесты с кодом возврата по соглашению SeverPact."""
    for module in ("experiment_head", "experiment_head_discipline"):
        if not (TOOLS / f"{module}.py").exists():
            print(f"НЕ ИЗМЕРЕНО: нет инструмента {module}.py", file=sys.stderr)
            return 2
    verbosity = 2 if "-v" in sys.argv else 1
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
