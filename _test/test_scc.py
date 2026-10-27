#!/usr/bin/env python3
"""
SCC — тесты чанкования и сшивки
================================

ЧТО ПРОВЕРЯЕТСЯ
---------------
Ядро схемы — детерминированное изъятие свёрнутых блоков по маркеру `(_SSC_)`.
Если этот слой ошибается, ядро теряется молча, и вся идея SCC перестаёт
работать. Поэтому проверяются не «примеры», а инварианты:

1. round-trip: split -> stitch возвращает исходный текст без потерь;
2. маркер распознаётся в заголовке события, а не в теле;
3. диапазоны (E4–E13) не разворачиваются;
4. событие без маркера НЕ попадает в ядро (это и есть защита);
5. пустой/битый вход даёт код 2, а не 0 («не измерено» != «чисто»);
6. двойной маркер ловится как нарушение;
7. преамбула не теряется при изъятии.

Запуск:
    python3 tests/test_scc.py            # все тесты
    python3 tests/test_scc.py -v         # подробно
Код возврата: 0 — все прошли, 1 — есть падения, 2 — не удалось запустить.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = HERE.parent / "_engine" / "core"
CHUNK = CORE / "scc_chunk.py"

# ── Образец SCC-файла: преамбула, ядро, свежее, диапазон ─────────────────────
SAMPLE = """# SCC свёртка от 2026-10-23

E0: (_SSC_) SYSTEM (роль): "You are a sandboxed test subject."
    ТОЧНЫЕ ДАННЫЕ:
    - НЕТ В ИСТОЧНИКЕ

E1: (_SSC_) USER (seq 8): TASK set-A part 1: прочитать 20 файлов
    ТОЧНЫЕ ДАННЫЕ:
    - setA/chain_000_kalpha000_n23.txt

E4–E13: (_SSC_) TOOL_RESULT (seq 26–35): прочитаны файлы 000–009
    ТОЧНЫЕ ДАННЫЕ:
    - setA/chain_000_kalpha000_n23.txt

E14: ASSISTANT (seq 38): свежее событие без маркера
    ТОЧНЫЕ ДАННЫЕ:
    - args={"file_path":"/data/setB/chain_500.txt"}

E15: USER (seq 69): ещё одно свежее событие
"""


def run(*args: str) -> subprocess.CompletedProcess[str]:
    """Запустить инструмент и вернуть результат."""
    return subprocess.run(
        [sys.executable, str(CHUNK), *args],
        capture_output=True,
        text=True,
    )


class TestSplit(unittest.TestCase):
    """Изъятие ядра и свежей части."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "scc.md"
        self.src.write_text(SAMPLE, encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_split_separates_core_and_fresh(self) -> None:
        """Свёрнутое уходит в ядро, несвёрнутое — в свежую часть."""
        core = self.dir / "core.md"
        fresh = self.dir / "fresh.md"
        result = run("split", str(self.src), "--core", str(core), "--fresh", str(fresh))
        self.assertEqual(result.returncode, 0, result.stderr)

        core_text = core.read_text(encoding="utf-8")
        fresh_text = fresh.read_text(encoding="utf-8")

        # ядро: три свёрнутых события (E0, E1, диапазон E4–E13)
        self.assertIn("E0: (_SSC_)", core_text)
        self.assertIn("E1: (_SSC_)", core_text)
        self.assertIn("E4–E13: (_SSC_)", core_text)
        # свежее: E14 и E15, без маркера
        self.assertIn("E14:", fresh_text)
        self.assertIn("E15:", fresh_text)
        self.assertNotIn("(_SSC_)", fresh_text)

    def test_core_never_contains_unmarked(self) -> None:
        """ГЛАВНЫЙ инвариант: событие без маркера в ядро не попадает."""
        core = self.dir / "core.md"
        fresh = self.dir / "fresh.md"
        run("split", str(self.src), "--core", str(core), "--fresh", str(fresh))
        core_text = core.read_text(encoding="utf-8")
        self.assertNotIn("E14: ASSISTANT", core_text)
        self.assertNotIn("E15: USER", core_text)

    def test_preamble_preserved_in_core(self) -> None:
        """Преамбула не теряется: там шапка свёртки."""
        core = self.dir / "core.md"
        fresh = self.dir / "fresh.md"
        run("split", str(self.src), "--core", str(core), "--fresh", str(fresh))
        self.assertIn("# SCC свёртка от 2026-10-23", core.read_text(encoding="utf-8"))

    def test_missing_file_is_code_2(self) -> None:
        """Нет файла -> код 2 (не измерено), а не 0 (чисто)."""
        result = run(
            "split",
            str(self.dir / "нет-такого.md"),
            "--core", str(self.dir / "c.md"),
            "--fresh", str(self.dir / "f.md"),
        )
        self.assertEqual(result.returncode, 2)


class TestStitch(unittest.TestCase):
    """Сшивка ядра и нового чанка."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_core_first_then_chunk(self) -> None:
        """Ядро идёт первым: хронология должна сохраниться."""
        core = self.dir / "core.md"
        chunk = self.dir / "chunk2.md"
        out = self.dir / "merged.md"
        core.write_text("E0: (_SSC_) SYSTEM\n", encoding="utf-8")
        chunk.write_text("E20: (_SSC_) USER\n", encoding="utf-8")
        result = run("stitch", str(core), str(chunk), "--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        text = out.read_text(encoding="utf-8")
        self.assertLess(text.index("E0:"), text.index("E20:"))

    def test_empty_core_returns_chunk(self) -> None:
        """Пустое ядро не ломает сшивку."""
        core = self.dir / "core.md"
        chunk = self.dir / "chunk2.md"
        out = self.dir / "merged.md"
        core.write_text("", encoding="utf-8")
        chunk.write_text("E20: (_SSC_) USER\n", encoding="utf-8")
        result = run("stitch", str(core), str(chunk), "--out", str(out))
        self.assertEqual(result.returncode, 0)
        self.assertIn("E20:", out.read_text(encoding="utf-8"))


class TestRoundTrip(unittest.TestCase):
    """split -> stitch обязан вернуть исходный набор событий."""

    def test_no_event_lost(self) -> None:
        """Все события исходника присутствуют в сшитом результате."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            src = directory / "scc.md"
            src.write_text(SAMPLE, encoding="utf-8")
            core = directory / "core.md"
            fresh = directory / "fresh.md"
            merged = directory / "merged.md"

            run("split", str(src), "--core", str(core), "--fresh", str(fresh))
            run("stitch", str(core), str(fresh), "--out", str(merged))

            text = merged.read_text(encoding="utf-8")
            for label in ("E0:", "E1:", "E4–E13:", "E14:", "E15:"):
                self.assertIn(label, text, f"потеряно событие {label}")


class TestCheck(unittest.TestCase):
    """Гейт маркировки."""

    def test_all_marked_is_clean(self) -> None:
        """Все события с маркером -> код 0."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scc.md"
            path.write_text(
                "E0: (_SSC_) SYSTEM\n\nE1: (_SSC_) USER\n", encoding="utf-8"
            )
            self.assertEqual(run("check", str(path)).returncode, 0)

    def test_unmarked_is_violation(self) -> None:
        """Событие без маркера -> код 1: оно не попадёт в ядро."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scc.md"
            path.write_text(
                "E0: (_SSC_) SYSTEM\n\nE1: USER без маркера\n", encoding="utf-8"
            )
            self.assertEqual(run("check", str(path)).returncode, 1)

    def test_doubled_marker_is_violation(self) -> None:
        """Двойной маркер -> код 1: признак двойной сшивки."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scc.md"
            path.write_text("E0: (_SSC_) (_SSC_) SYSTEM\n", encoding="utf-8")
            self.assertEqual(run("check", str(path)).returncode, 1)

    def test_marker_quoted_in_body_is_clean(self) -> None:
        """
        Живой урок: маркер, процитированный в ТЕКСТЕ события, — не нарушение.

        Замер 2026-10-24: событие E681 цитировало постановку задачи —
        «постфикс `(_SSC_)`» — и наивная проверка по количеству вхождений в
        строке давала ложное нарушение на живом прогоне.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scc.md"
            path.write_text(
                "E681: (_SSC_) USER (seq 2408) — задание: постфикс `(_SSC_)`, "
                "алгоритм чанкования\n",
                encoding="utf-8",
            )
            result = run("check", str(path))
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_marker_after_label_only_once(self) -> None:
        """Маркер сразу после метки — норма; два подряд — нарушение."""
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.md"
            bad = Path(tmp) / "bad.md"
            good.write_text("E0: (_SSC_) SYSTEM\n", encoding="utf-8")
            bad.write_text("E0: (_SSC_)(_SSC_) SYSTEM\n", encoding="utf-8")
            self.assertEqual(run("check", str(good)).returncode, 0)
            self.assertEqual(run("check", str(bad)).returncode, 1)

    def test_no_events_is_code_2(self) -> None:
        """Нет событий -> код 2 (не измерено), а не 0."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scc.md"
            path.write_text("просто текст без событий\n", encoding="utf-8")
            self.assertEqual(run("check", str(path)).returncode, 2)


class TestStats(unittest.TestCase):
    """Статистика чанков."""

    def test_counts_events(self) -> None:
        """Ядро и свежее считаются раздельно."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scc.md"
            path.write_text(SAMPLE, encoding="utf-8")
            result = run("stats", str(path), "--json")
            self.assertEqual(result.returncode, 0)
            import json as jsonlib

            report = jsonlib.loads(result.stdout)
            self.assertEqual(report["events_total"], 5)
            self.assertEqual(report["core_events"], 3)
            self.assertEqual(report["fresh_events"], 2)


class TestMarkerPlacement(unittest.TestCase):
    """Маркер в теле не должен считаться маркером события."""

    def test_marker_in_body_not_marking(self) -> None:
        """Упоминание (_SSC_) внутри тела не делает событие свёрнутым."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scc.md"
            path.write_text(
                "E0: ASSISTANT: мы обсуждали маркер (_SSC_) в тексте\n", encoding="utf-8"
            )
            result = run("stats", str(path), "--json")
            import json as jsonlib

            report = jsonlib.loads(result.stdout)
            self.assertEqual(report["core_events"], 0, "маркер из тела попал в ядро")


class TestMarkerSingleSource(unittest.TestCase):
    """
    Маркер задан ОДИН раз, регулярка собрана из константы.

    Живой урок (найдено при переносе в отдельный проект): маркер был задан
    дважды — константой SSC_MARK и захардкоженной строкой внутри EVENT_RE.
    Правка одного места оставила бы второе в старом виде, и разбор молча
    перестал бы видеть события: «события не найдены» на полностью помеченном
    файле. Ошибка не проявляется до первой миграции маркера.
    """

    def _module(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("scc_chunk_marker", CHUNK)
        module = importlib.util.module_from_spec(spec)
        sys.modules["scc_chunk_marker"] = module
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_regex_is_built_from_constant(self) -> None:
        """Регулярка содержит именно текущее значение константы."""
        module = self._module()
        import re

        self.assertIn(
            re.escape(module.SSC_MARK),
            module.EVENT_RE.pattern,
            "регулярка не собрана из SSC_MARK — маркер задан дважды",
        )

    def test_approved_marker_value(self) -> None:
        """
        Маркер УТВЕРЖДЁН как `(_SSC_)` — это не временное состояние.

        Заказчик принял фактическое написание вместо постановочного `_SCC_`.
        Тест фиксирует решение: случайная смена константы при рефакторинге
        сломала бы совместимость со всеми ранее созданными артефактами.
        """
        module = self._module()
        self.assertEqual(
            module.SSC_MARK, "(_SSC_)",
            "маркер изменён; это решение заказчика, а не деталь реализации",
        )

    def test_old_marker_not_recognised(self) -> None:
        """Постановочное `_SCC_` НЕ распознаётся: артефакты используют `(_SSC_)`."""
        module = self._module()
        parsed = module.parse("E0: _SCC_ SYSTEM\nE1: USER\n")
        self.assertEqual(len(parsed.core), 0, "распознан маркер, которого нет в артефактах")

    def test_marker_change_is_picked_up(self) -> None:
        """
        Смена константы меняет разбор — значит источник действительно единый.

        Подменяем SSC_MARK, пересобираем регулярку тем же способом и проверяем,
        что старый маркер больше не распознаётся, а новый — распознаётся.
        """
        module = self._module()
        import re

        old_mark = module.SSC_MARK
        new_mark = "(_TEST_)"
        module.SSC_MARK = new_mark
        module.EVENT_RE = re.compile(
            module.EVENT_RE.pattern.replace(re.escape(old_mark), re.escape(new_mark)),
            re.MULTILINE,
        )
        parsed = module.parse(f"E0: {new_mark} SYSTEM\nE1: USER\n")
        self.assertEqual(len(parsed.core), 1, "новый маркер не распознан")
        parsed_old = module.parse(f"E0: {old_mark} SYSTEM\n")
        self.assertEqual(len(parsed_old.core), 0, "старый маркер всё ещё распознаётся")


class TestMarkdownHeadings(unittest.TestCase):
    """
    Живой урок: на большом контексте модель пишет заголовки в Markdown.

    Замер 2026-10-24: прогон SCC на реальной сессии (892 события) дал вывод
    вида `**E0: SYSTEM (seq 7)** — ...`, строгий разбор увидел 0 событий и
    вернул код 2 — то есть молча потерял всё ядро. Эти тесты закрывают регресс.
    """

    MD_SAMPLE = (
        "# Вертикальная последовательность событий\n\n"
        "**E0: SYSTEM (seq 7)** — шапка роли: агент-кодер\n\n"
        "**E1: USER (seq 8)** — «найди настройки автокомпакции»\n\n"
        "**E4–E13: TOOL_RESULT (seq 26–35)** — прочитаны файлы 000–009\n"
    )

    def test_markdown_headings_are_parsed(self) -> None:
        """События в Markdown-обёртке распознаются, а не теряются."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scc.md"
            path.write_text(self.MD_SAMPLE, encoding="utf-8")
            result = run("stats", str(path), "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            import json as jsonlib

            report = jsonlib.loads(result.stdout)
            self.assertEqual(report["events_total"], 3, "Markdown-заголовки не распознаны")

    def test_markdown_range_is_not_split(self) -> None:
        """Диапазон в Markdown-обёртке остаётся одним событием."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scc.md"
            path.write_text(self.MD_SAMPLE, encoding="utf-8")
            result = run("stats", str(path), "--json")
            import json as jsonlib

            report = jsonlib.loads(result.stdout)
            self.assertEqual(report["events_total"], 3)
            self.assertEqual(report["core_events"], 0)

    def test_markdown_with_marker_is_core(self) -> None:
        """Markdown-заголовок с маркером попадает в ядро."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scc.md"
            path.write_text(
                "**E0: (_SSC_) SYSTEM (seq 7)** — роль\n\n**E1: USER** — свежее\n",
                encoding="utf-8",
            )
            result = run("stats", str(path), "--json")
            import json as jsonlib

            report = jsonlib.loads(result.stdout)
            self.assertEqual(report["events_total"], 2)
            self.assertEqual(report["core_events"], 1, "маркер в Markdown не распознан")

    def test_mixed_formats_parsed_together(self) -> None:
        """Оба формата в одном файле разбираются одинаково."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scc.md"
            path.write_text(
                "E0: (_SSC_) SYSTEM\n\n**E1: USER** — свежее\n",
                encoding="utf-8",
            )
            result = run("stats", str(path), "--json")
            import json as jsonlib

            report = jsonlib.loads(result.stdout)
            self.assertEqual(report["events_total"], 2)
            self.assertEqual(report["core_events"], 1)

    def test_round_trip_preserves_markdown(self) -> None:
        """Сшивка не портит Markdown-заголовки."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            src = directory / "scc.md"
            src.write_text(self.MD_SAMPLE, encoding="utf-8")
            core = directory / "core.md"
            fresh = directory / "fresh.md"
            merged = directory / "merged.md"
            run("split", str(src), "--core", str(core), "--fresh", str(fresh))
            run("stitch", str(core), str(fresh), "--out", str(merged))
            text = merged.read_text(encoding="utf-8")
            for label in ("E0:", "E1:", "E4–E13:"):
                self.assertIn(label, text, f"потеряно событие {label}")


def main() -> int:
    """Запустить тесты с кодом возврата по соглашению SeverPact."""
    if not CHUNK.exists():
        print(f"НЕ ИЗМЕРЕНО: нет инструмента {CHUNK}", file=sys.stderr)
        return 2
    verbosity = 2 if "-v" in sys.argv else 1
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
