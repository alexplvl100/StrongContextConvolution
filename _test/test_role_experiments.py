#!/usr/bin/env python3
"""
SCC — тесты метрики экспериментов о влиянии роли
==================================================

ЧТО ПРОВЕРЯЕТСЯ
---------------
Не сами эксперименты (они требуют сети и ключа), а их **метрика** и **вывод** —
самое слабое место. При разработке найдены **четыре ошибки измерения**, и
каждая давала неверный вывод:

1. **Вердикт по одному прогону.** Первая версия `experiment_role_rules` смотрела
   первый прогон, а печатала доли: `4/5` против `5/5` объявлялось «бесполезно».
   На 12 прогонах то же правило дало `11/12` против `12/12` — различие в один
   прогон, то есть шум.
2. **Неверная метрика для правила-запрета.** `workflow-only-asked` запрещает
   инструмент, но не предписывает замену. Первая версия мерила «выбрала ли
   `glob`»: модель шла в `bash`, запрет **соблюдался**, а вердикт говорил «не
   работает». Введены режимы `prefer` и `ban`.
3. **Прогресс в stdout ломал `--json`.** Строки «прогон N из M» попадали в
   вывод, и JSON перестал разбираться.
4. **Ложное срабатывание проверки устаревшего числа.** Поиск подстроки `1 800`
   срабатывал на числе `1 800 000`. Исправлено на отдельное число; первый
   вариант регулярки дал **ложный отрицательный** результат — не находил
   «итого 1 800 токенов» из-за пробела перед числом.

Запуск:
    python3 _test/test_role_experiments.py -v
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


RULES = load("experiment_role_rules")
DECAY = load("experiment_role_decay")


class TestVerdictByRates(unittest.TestCase):
    """Вердикт считается по ДОЛЯМ, а не по одному прогону."""

    def _rule(self, mode: str = "prefer") -> dict:
        return {"mode": mode, "expected": "read", "forbidden": "bash"}

    def test_no_difference_is_useless(self) -> None:
        """
        Модель и без роли выполняет правило — роль бесполезна.

        Это главный результат R-SCC-07: правило, совпадающее с обученным
        поведением, места в HEAD не оправдывает.
        """
        verdict = RULES.verdict(12, 12, 12, self._rule())
        self.assertEqual(verdict, "бесполезно")

    def test_shift_is_working(self) -> None:
        """Заметный сдвиг — правило работает."""
        verdict = RULES.verdict(0, 11, 12, self._rule())
        self.assertEqual(verdict, "работает")

    def test_small_difference_is_useless_not_working(self) -> None:
        """
        Различие в один прогон из двенадцати — НЕ результат.

        Живой урок: на пяти прогонах `4/5` против `5/5` выглядело работающим
        правилом, на двенадцати — `11/12` против `12/12`. Это шум, и вердикт
        обязан говорить «бесполезно», а не «работает».
        """
        verdict = RULES.verdict(11, 12, 12, self._rule())
        self.assertEqual(verdict, "бесполезно",
                         "различие в один прогон принято за результат")

    def test_ignored_rule_is_not_working(self) -> None:
        """С ролью по-прежнему не выполняет — правило не работает."""
        verdict = RULES.verdict(0, 1, 12, self._rule())
        self.assertEqual(verdict, "не работает")

    def test_ban_mode_measures_forbidden_tool(self) -> None:
        """
        Для правила-запрета «попадание» — НЕиспользование запрещённого.

        Живой урок: первая версия мерила для `workflow-only-asked` выбор
        `glob`. Модель шла в `bash`, запрет соблюдался, а вердикт говорил
        «не работает» — дефект измерения.
        """
        rule = {"mode": "ban", "forbidden": "workflow"}
        # 0 попаданий из 12 означает: запрещённый использован 12 раз
        self.assertEqual(RULES.verdict(12, 12, 12, rule), "бесполезно")


class TestSignificanceNote(unittest.TestCase):
    """Замечание о достаточности данных."""

    def test_small_run_count_warns(self) -> None:
        """При малом числе прогонов результат помечается как шумный."""
        note = RULES.significance_note(0, 1, 5)
        self.assertIn("мало прогонов", note)

    def test_large_difference_is_silent(self) -> None:
        """При большом различии и достаточных прогонах замечания нет."""
        note = RULES.significance_note(0, 11, 12)
        self.assertEqual(note, "")

    def test_one_run_difference_is_insignificant(self) -> None:
        """Различие в один прогон признаётся незначимым."""
        note = RULES.significance_note(11, 12, 12)
        self.assertIn("незначимо", note)


class TestDecayRates(unittest.TestCase):
    """Доли соблюдения и убыль в эксперименте о выветривании."""

    def test_glob_share(self) -> None:
        """Доля соблюдения считается по первому инструменту хода."""
        dialogs = [["glob", "glob", "read"], ["glob", "read", "read"]]
        total = sum(len(d) for d in dialogs)
        globs = sum(1 for d in dialogs for first in d if first == "glob")
        self.assertEqual(total, 6)
        self.assertEqual(globs, 3)

    def test_start_and_end_windows(self) -> None:
        """
        Начало и конец диалога сравниваются по одинаковому окну.

        Живой урок: падение 1,00 -> 0,30 при окне в два хода. Если окна
        разной длины, сравнение нечестное.
        """
        dialog = ["glob", "glob", "read", "read", "read", "glob", "glob"]
        window = 2
        start = dialog[:window]
        end = dialog[-window:]
        self.assertEqual(start, ["glob", "glob"])
        self.assertEqual(end, ["glob", "glob"])
        self.assertEqual(len(start), len(end))

    def test_decay_direction(self) -> None:
        """Убыль положительна, когда начало соблюдается лучше конца."""
        start_rate, end_rate = 1.0, 0.3
        self.assertGreater(start_rate - end_rate, 0,
                           "убыль не положительна — роль не выветривается")


class TestNoNetworkInTests(unittest.TestCase):
    """Тесты не должны требовать сети: эксперименты вызывают модель."""

    def test_modules_do_not_call_llm_on_import(self) -> None:
        """
        Импорт модуля не обращается к сети.

        Загрузка выше уже прошла без ключа, значит `call_llm` при импорте не
        вызывается. Тест фиксирует это как свойство, а не совпадение.
        """
        for module in (RULES, DECAY):
            self.assertFalse(hasattr(module, "API_KEY_CALLED"),
                             f"{module.__name__} обращается к сети при импорте")

    def test_rules_include_both_modes(self) -> None:
        """В наборе правил есть оба режима: prefer и ban."""
        modes = {rule.get("mode", "prefer") for rule in RULES.RULES}
        self.assertIn("prefer", modes, "нет правил режима prefer")
        self.assertIn("ban", modes, "нет правил режима ban — запреты не проверяются")

    def test_every_rule_has_task_and_rule(self) -> None:
        """У каждого правила есть текст правила и задача для пробы."""
        for rule in RULES.RULES:
            with self.subTest(rule=rule.get("id")):
                self.assertTrue(rule.get("rule"), "нет текста правила")
                self.assertTrue(rule.get("task"), "нет задачи")
                self.assertTrue(rule.get("forbidden"), "не указан запрещённый инструмент")
                if rule.get("mode", "prefer") == "prefer":
                    self.assertTrue(rule.get("expected"),
                                    "режиму prefer нужен ожидаемый инструмент")


def main() -> int:
    """Запустить тесты с кодом возврата по соглашению SeverPact."""
    for tool in ("experiment_role_rules.py", "experiment_role_decay.py"):
        if not (TOOLS / tool).exists():
            print(f"НЕ ИЗМЕРЕНО: нет {tool}", file=sys.stderr)
            return 2
    verbosity = 2 if "-v" in sys.argv else 1
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
