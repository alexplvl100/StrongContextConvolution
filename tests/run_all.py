#!/usr/bin/env python3
"""
SCC — запуск всех тестов одной командой
========================================

Собирает три набора проверок и печатает сводку:

    test_scc.py         чанкование, сшивка, гейт маркировки  (13 тестов)
    test_scc_events.py  конвертер журнала в поток событий    (13 тестов)
    test_scc_run.py     сборка промтов и CLI этапов          (11 тестов)

LLM не вызывается ни в одном наборе: всё, что требует сети, проверяется
отдельно прогонами в RESEARCH.

Запуск:
    python3 tests/run_all.py            # сводка
    python3 tests/run_all.py -v         # подробно
Код возврата: 0 — все наборы прошли, 1 — есть падения, 2 — не удалось запустить.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ── Наборы проверок: файл и что он покрывает ─────────────────────────────────
SUITES: tuple[tuple[str, str], ...] = (
    ("test_scc.py", "чанкование, сшивка, гейт маркировки"),
    ("test_scc_events.py", "конвертер журнала в поток событий"),
    ("test_scc_run.py", "сборка промтов и CLI этапов"),
    ("test_scc_usability.py", "оценка пригодности ядра"),
    ("test_memory.py", "трёхуровневая память и глоссарий"),
)


def main() -> int:
    """Прогнать все наборы и вернуть сводный код."""
    verbose = "-v" in sys.argv
    results: list[tuple[str, int, str]] = []

    for filename, description in SUITES:
        path = HERE / filename
        if not path.exists():
            print(f"НЕ ИЗМЕРЕНО: нет набора {path}", file=sys.stderr)
            return 2
        argv = [sys.executable, str(path)]
        if verbose:
            argv.append("-v")
        result = subprocess.run(argv, capture_output=True, text=True)
        results.append((filename, result.returncode, description))
        if verbose:
            print(result.stdout, end="")
            print(result.stderr, end="", file=sys.stderr)

    print("═" * 66)
    print(f"{'набор':24} {'код':>5}  что покрывает")
    print("─" * 66)
    for filename, code, description in results:
        mark = "OK" if code == 0 else "ПАДЕНИЕ" if code == 1 else "НЕ ИЗМЕРЕНО"
        print(f"{filename:24} {mark:>5}  {description}")
    print("═" * 66)

    failed = [f for f, code, _ in results if code == 1]
    unmeasured = [f for f, code, _ in results if code == 2]

    if unmeasured:
        print(f"НЕ ИЗМЕРЕНО: {', '.join(unmeasured)}", file=sys.stderr)
        return 2
    if failed:
        print(f"ПАДЕНИЯ: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"ВСЕ НАБОРЫ ПРОШЛИ ({len(results)} из {len(results)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
