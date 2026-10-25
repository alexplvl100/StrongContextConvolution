#!/usr/bin/env python3
"""
SCC — тесты оценки пригодности ядра
====================================

ЧТО ПРОВЕРЯЕТСЯ
---------------
Оценщик ответов (`verdict`) — слабое место теста пригодности: он решает, был
ответ верным, ошибочным или это честный отказ. Ошибка оценщика искажает вывод
о пригодности ядра, поэтому проверяются реальные случаи, найденные на прогонах:

1. развёрнутое объяснение не должно считаться отказом («провайдер отсутствует
   в каталоге… поэтому фолбэк» — это ОТВЕТ, а не «не знаю»);
2. короткое «НЕ ЗНАЮ» — отказ;
3. числа с разделителями равны числам без них («9 182» == «9182»);
4. пустой ответ — ПУСТО, а не отказ;
5. верное значение имеет приоритет над формой ответа.

Запуск:
    python3 tests/test_scc_usability.py -v
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = HERE.parent / "core"
USABILITY = CORE / "scc_usability.py"


def load_module():
    """Загрузить scc_usability.py как модуль, зарегистрировав в sys.modules."""
    spec = importlib.util.spec_from_file_location("scc_usability", USABILITY)
    module = importlib.util.module_from_spec(spec)
    sys.modules["scc_usability"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


U = load_module()


class TestVerdict(unittest.TestCase):
    """Оценка ответов агента."""

    def test_explicit_refusal(self) -> None:
        """Короткое «НЕ ЗНАЮ» — отказ."""
        self.assertEqual(U.verdict("НЕ ЗНАЮ", ["262144"]), "ОТКАЗ")

    def test_refusal_variants(self) -> None:
        """Синонимы отказа распознаются."""
        for text in ("Не указан в документе.", "неизвестно", "нет данных"):
            self.assertEqual(U.verdict(text, ["x"]), "ОТКАЗ", text)

    def test_long_explanation_is_not_refusal(self) -> None:
        """
        Развёрнутое объяснение с словом «отсутствует» — ОТВЕТ, а не отказ.

        Живой урок: первый прогон теста объявил отказом фразу «провайдер
        routerai отсутствует в каталоге pi-ai… поэтому взялся фолбэк».
        """
        answer = (
            "Провайдер провайдер отсутствует в каталоге pi-ai (4 файла "
            "проверены), поэтому взялся фолбэк 262144; порог стал 800000"
        )
        self.assertEqual(U.verdict(answer, ["262144"]), "ВЕРНО")

    def test_number_separators_are_normalized(self) -> None:
        """«9 182» и «9,182» равны «9182»."""
        self.assertEqual(U.verdict("Хеши: 9 182 → 0", ["9182"]), "ВЕРНО")
        self.assertEqual(U.verdict("Хеши: 9,182 → 0", ["9182"]), "ВЕРНО")

    def test_empty_is_empty_not_refusal(self) -> None:
        """Пустой ответ — ПУСТО, это отдельный класс."""
        self.assertEqual(U.verdict("", ["x"]), "ПУСТО")
        self.assertEqual(U.verdict("   ", ["x"]), "ПУСТО")

    def test_wrong_answer(self) -> None:
        """Ответ без ожидаемого значения и без формулы отказа — НЕВЕРНО."""
        self.assertEqual(U.verdict("Правка понадобилась из-за провайдера", ["262144"]), "НЕВЕРНО")

    def test_match_wins_over_form(self) -> None:
        """Верное значение важнее формы: ответ с числом не считается отказом."""
        self.assertEqual(U.verdict("1000000 (окно в 1 млн)", ["1000000"]), "ВЕРНО")

    def test_case_insensitive(self) -> None:
        """Регистр не важен."""
        self.assertEqual(U.verdict("DEEPSEEK/DEEPSEEK-V4.1-FLASH", ["deepseek/deepseek-v4.1-flash"]), "ВЕРНО")

    def test_markdown_backticks_do_not_break_match(self) -> None:
        """Обрамление кодом не мешает совпадению."""
        self.assertEqual(U.verdict("`alexplvl100/MultiAgentDSH`", ["alexplvl100/MultiAgentDSH"]), "ВЕРНО")


class TestQuestions(unittest.TestCase):
    """Набор вопросов корректен."""

    def test_questions_are_non_empty(self) -> None:
        """Есть вопросы обоих типов."""
        kinds = {q["type"] for q in U.QUESTIONS}
        self.assertEqual(kinds, {"факт", "связь"})

    def test_ids_are_unique(self) -> None:
        """ID не повторяются — иначе ответы перезапишут друг друга."""
        ids = [q["id"] for q in U.QUESTIONS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_question_has_expectations(self) -> None:
        """У каждого вопроса есть эталон для сверки."""
        for question in U.QUESTIONS:
            self.assertTrue(question["expect"], f"{question['id']}: пустой эталон")


class TestPrompt(unittest.TestCase):
    """Промт не даёт доступа к первоисточнику."""

    def test_prompt_forbids_invention(self) -> None:
        """Промт требует «НЕ ЗНАЮ» вместо выдумки."""
        prompt = U.build_prompt("ДОКУМЕНТ", list(U.QUESTIONS))
        self.assertIn("НЕ ЗНАЮ", prompt)
        self.assertIn("выдумывать запрещено", prompt)

    def test_prompt_contains_all_ids(self) -> None:
        """Все ID попадают в промт."""
        prompt = U.build_prompt("ДОКУМЕНТ", list(U.QUESTIONS))
        for question in U.QUESTIONS:
            self.assertIn(str(question["id"]), prompt)


def main() -> int:
    """Запустить тесты с кодом возврата по соглашению SeverPact."""
    if not USABILITY.exists():
        print(f"НЕ ИЗМЕРЕНО: нет инструмента {USABILITY}", file=sys.stderr)
        return 2
    verbosity = 2 if "-v" in sys.argv else 1
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
