#!/usr/bin/env python3
"""
SCC — тест непротиворечивости методологии
==========================================

ЧТО ПРОВЕРЯЕТСЯ
---------------
Методология — пять документов плюс README. Они **ссылаются друг на друга**, и
при правке одного легко забыть другое: так в проекте уже накопилось шесть
расхождений (см. CHANGELOG 0.9.0). Этот тест ловит их машинно.

Проверяются **утверждения, которые обязаны совпадать** во всех документах:

1. число инвариантов (их восемь, инвариант 8 — про HEAD);
2. HEAD нигде не назван «уровнем памяти» (уровней три);
3. размер HEAD согласован (нет устаревшего «1 800»);
4. Архивариус **не работает с CAS** — это область CAS;
5. CAS: обход Архивариуса при доступе к точным данным обязателен;
6. HEAD описывается как изымаемый перед свёрткой;
7. порядок контекста: HEAD перед стаком;
8. устаревший термин LIBRARY не используется;
9. инструкция CAS — часть HEAD, а не отдельный блок;
10. указатель знает про четыре части и инвариант 8.

ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ ТЕСТ
---------------------------
Тесты памяти проверяют **код**, а этот — **документы**. Расхождение в
документах не роняет программу, но ломает работу человека: он читает одну
версию, а код делает другую.

ЖИВОЙ УРОК. Первые версии теста давали **ложное срабатывание**: регекс
`сем[иь] инвариант` находил подстроку «семь» внутри слова «восемь». Поэтому
проверки используют границы слов, а не подстроки.

Запуск:
    python3 tests/test_methodology.py -v
Коды возврата: 0 — согласовано, 1 — найдены расхождения, 2 — НЕ ИЗМЕРЕНО.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# Документы, которые обязаны быть согласованы между собой.
# docs/ входит намеренно: там тоже есть числа и утверждения о HEAD — живой урок
# 2026-10-26, когда размер HEAD разошёлся между методологией и замерами.
DOCUMENTS = (
    "METHODOLOGY.md",
    "METHODOLOGY_CORE.md",
    "METHODOLOGY_MEMORY.md",
    "METHODOLOGY_CAS.md",
    "METHODOLOGY_HEAD.md",
    "METHODOLOGY_STATE.md",
    "README.md",
    "docs/architecture.md",
    "docs/measurements.md",
)


def load() -> dict[str, str]:
    """Прочитать методологию и README. Отсутствующий файл — ошибка, не пропуск."""
    texts: dict[str, str] = {}
    for name in DOCUMENTS:
        path = ROOT / name
        if not path.exists():
            raise FileNotFoundError(f"нет документа методологии: {name}")
        texts[name] = path.read_text(encoding="utf-8")
    return texts


class TestMethodologyConsistency(unittest.TestCase):
    """Утверждения, которые обязаны совпадать во всех документах."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = load()

    def _offenders(self, pattern: str, flags: int = 0) -> list[str]:
        """Документы, в которых найден шаблон. Пустой список — расхождений нет."""
        return [
            name for name, text in self.docs.items()
            if re.search(pattern, text, flags)
        ]

    # ── Инварианты ───────────────────────────────────────────────────────────

    def test_invariant_count_is_eight(self) -> None:
        """В METHODOLOGY_MEMORY.md ровно восемь инвариантов."""
        found = len(re.findall(r"^\*\*[1-9]\. ", self.docs["METHODOLOGY_MEMORY.md"], re.M))
        self.assertEqual(found, 8, f"найдено инвариантов: {found}")

    def test_invariant_eight_is_about_head(self) -> None:
        """Инвариант 8 объявлен и говорит про HEAD."""
        text = self.docs["METHODOLOGY_MEMORY.md"]
        match = re.search(r"^\*\*8\. (.+?)\*\*", text, re.M)
        self.assertIsNotNone(match, "инвариант 8 не найден")
        self.assertIn("HEAD", match.group(1))

    def test_no_stale_seven_invariants(self) -> None:
        """
        Нигде не сказано «семь инвариантов».

        Границы слова обязательны: без них регекс находит «семь» внутри
        «восемь» — это было живое ложное срабатывание.
        """
        offenders = self._offenders(r"\bсем[иь] инвариант|инвариант\w* 1[–-]7")
        self.assertEqual(offenders, [], f"устаревшее число инвариантов: {offenders}")

    def test_pointer_lists_invariant_eight(self) -> None:
        """Указатель содержит инвариант 8 в таблице применения."""
        self.assertIn("8. HEAD", self.docs["METHODOLOGY.md"])

    # ── HEAD ─────────────────────────────────────────────────────────────────

    def test_head_is_not_a_memory_level(self) -> None:
        """HEAD нигде не назван уровнем памяти: уровней три (плюс HEAD отдельно)."""
        offenders = self._offenders(
            r"четвёртый уровень|четыре уровня|HEAD.{0,20}уровень памяти"
        )
        self.assertEqual(offenders, [], f"HEAD назван уровнем памяти: {offenders}")

    def test_head_size_is_consistent(self) -> None:
        """
        Размер HEAD согласован: устаревшего «1 800» нет ни в одном документе.

        Проверяются и docs/: там размер HEAD тоже указан, и он уже расходился
        с методологией.
        """
        offenders = [name for name, text in self.docs.items() if "1 800" in text]
        self.assertEqual(offenders, [], f"устаревший размер HEAD: {offenders}")

    def test_head_size_present_in_measurements(self) -> None:
        """Замеры знают про HEAD: без него экономия завышена."""
        self.assertIn("HEAD", self.docs["docs/measurements.md"])
        self.assertIn("1 976", self.docs["docs/measurements.md"])

    def test_head_described_as_removed(self) -> None:
        """HEAD описывается как изымаемый перед свёрткой (инвариант 8)."""
        memory = self.docs["METHODOLOGY_MEMORY.md"]
        head = self.docs["METHODOLOGY_HEAD.md"]
        self.assertIn("изымается", memory, "MEMORY не знает про изъятие HEAD")
        self.assertIn("изъятие", head, "HEAD не описывает изъятие")

    def test_head_is_first_in_context(self) -> None:
        """Порядок контекста: HEAD перед стаком."""
        offenders = self._offenders(r"стак.{0,30}HEAD(?!.{0,10}перед)")
        # Мягкая проверка: в MEMORY и HEAD сказано «сначала HEAD»
        combined = self.docs["METHODOLOGY_MEMORY.md"] + self.docs["METHODOLOGY_HEAD.md"]
        self.assertIn("сначала HEAD", combined, "не зафиксировано «сначала HEAD»")
        self.assertEqual(offenders, [], f"порядок нарушен: {offenders}")

    # ── Архивариус и CAS ─────────────────────────────────────────────────────

    def test_archivist_does_not_work_with_cas(self) -> None:
        """Архивариус отделён от CAS: это разные каналы доступа."""
        self.assertIn("не работает с CAS", self.docs["METHODOLOGY_HEAD.md"])

    def test_cas_requires_bypassing_archivist(self) -> None:
        """CAS: обход Архивариуса объявлен обязательным."""
        cas = self.docs["METHODOLOGY_CAS.md"]
        self.assertTrue(
            "ОБЯЗАТЕЛЕН" in cas or "обязателен" in cas,
            "не сказано, что обход Архивариуса обязателен",
        )

    def test_cas_instruction_is_part_of_head(self) -> None:
        """Инструкция CAS — часть HEAD, а не отдельный блок перед чанками."""
        self.assertIn("часть HEAD", self.docs["METHODOLOGY_CAS.md"])

    def test_no_stale_library_term(self) -> None:
        """Устаревший термин LIBRARY не используется как действующий."""
        offenders = [name for name, text in self.docs.items() if "LIBRARY" in text]
        self.assertEqual(offenders, [], f"устаревший термин: {offenders}")

    # ── Указатель ────────────────────────────────────────────────────────────

    def test_pointer_lists_four_parts(self) -> None:
        """Указатель знает про четыре части системы."""
        self.assertIn("четырёх частей", self.docs["METHODOLOGY.md"])

    def test_readme_lists_eight_invariants(self) -> None:
        """README указывает актуальное число инвариантов."""
        self.assertIn("инварианты 1-8", self.docs["README.md"])

    # ── Состояния методологии ────────────────────────────────────────────────

    def test_every_methodology_has_state_line(self) -> None:
        """
        У каждой МЕТОДОЛОГИИ есть строка `Состояние:` в шапке.

        Проверяются только документы `METHODOLOGY*.md`: README и `docs/` —
        описательные документы, состояния у них нет и быть не должно. Смешение
        двух списков дало бы ложное срабатывание — живой урок 2026-10-26.
        """
        methodologies = {n: t for n, t in self.docs.items()
                         if Path(n).name.startswith("METHODOLOGY")}
        self.assertTrue(methodologies, "не найдено ни одной методологии")
        offenders = [
            name for name, text in methodologies.items()
            if "**Состояние:**" not in "\n".join(text.split("\n")[:12])
        ]
        self.assertEqual(offenders, [], f"нет состояния в шапке: {offenders}")

    def test_descriptive_documents_have_no_state(self) -> None:
        """
        У описательных документов состояния НЕТ.

        README и `docs/` описывают систему, но не являются методологией:
        состояние на них не распространяется, и его там быть не должно —
        иначе список состояний размоется.
        """
        descriptive = {n for n in self.docs if not Path(n).name.startswith("METHODOLOGY")}
        offenders = [
            name for name in descriptive
            if "**Состояние:**" in "\n".join(self.docs[name].split("\n")[:12])
        ]
        self.assertEqual(offenders, [], f"состояние у описательного документа: {offenders}")

    def test_state_values_are_valid(self) -> None:
        """Состояние — только В_РАБОТЕ или ПРИНЯТ."""
        offenders: list[str] = []
        for name, text in self.docs.items():
            head = "\n".join(text.split("\n")[:12])
            match = re.search(r"^\*\*Состояние:\*\*\s*(\S+)", head, re.M)
            if match and match.group(1) not in ("В_РАБОТЕ", "ПРИНЯТ"):
                offenders.append(f"{name}: {match.group(1)}")
        self.assertEqual(offenders, [], f"недопустимое состояние: {offenders}")

    def test_state_and_implementation_are_separate_fields(self) -> None:
        """
        «Состояние» и «Реализация» — разные поля, не одно.

        Первое отвечает на вопрос «можно ли править», второе — «что сделано».
        Методология может быть ПРИНЯТ и при этом не реализована.
        """
        for name in ("METHODOLOGY_CORE.md", "METHODOLOGY_HEAD.md"):
            head = "\n".join(self.docs[name].split("\n")[:12])
            self.assertIn("**Состояние:**", head, f"{name}: нет поля Состояние")
            self.assertIn("**Реализация:**", head, f"{name}: нет поля Реализация")

    def test_state_document_defines_both_states(self) -> None:
        """Документ состояний описывает оба состояния и запрет реализации."""
        state = self.docs["METHODOLOGY_STATE.md"]
        self.assertIn("В_РАБОТЕ", state)
        self.assertIn("ПРИНЯТ", state)
        self.assertIn("только пользователь", state.lower())

    def test_pointer_mentions_states(self) -> None:
        """Указатель знает про состояния методологии."""
        pointer = self.docs["METHODOLOGY.md"]
        self.assertIn("METHODOLOGY_STATE.md", pointer)
        self.assertIn("В_РАБОТЕ", pointer)

    def test_readme_describes_state_gate(self) -> None:
        """README описывает запрет реализации при В_РАБОТЕ."""
        readme = self.docs["README.md"]
        self.assertIn("В_РАБОТЕ", readme)
        self.assertIn("check_state.py", readme)


class TestCodeMatchesMethodology(unittest.TestCase):
    """Код не должен противоречить методологии в терминах и порядке."""

    def test_cas_prompt_says_part_of_head(self) -> None:
        """Инструкция CAS в коде знает, что она часть HEAD."""
        text = (ROOT / "memory" / "cas_prompt.py").read_text(encoding="utf-8")
        self.assertIn("HEAD", text, "cas_prompt.py не упоминает HEAD")

    def test_no_stale_context_order_in_code(self) -> None:
        """В коде нет устаревшего порядка «после Архивариуса, перед чанками»."""
        offenders: list[str] = []
        for path in sorted((ROOT / "memory").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "после промта обращения" in text or "перед чанками" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"устаревший порядок контекста: {offenders}")

    def test_long_memory_has_read_only_interface(self) -> None:
        """У Архивариуса только чтение: нет методов возврата чанка в стак."""
        source = (ROOT / "memory" / "long.py").read_text(encoding="utf-8")
        for forbidden in ("move_to_medium", "restore_to_medium", "return_to_medium"):
            self.assertNotIn(forbidden, source, f"у Архивариуса появился путь возврата: {forbidden}")


def main() -> int:
    """Запустить тесты с кодом возврата по соглашению SeverPact."""
    try:
        load()
    except FileNotFoundError as error:
        print(f"НЕ ИЗМЕРЕНО: {error}", file=sys.stderr)
        return 2
    verbosity = 2 if "-v" in sys.argv else 1
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
