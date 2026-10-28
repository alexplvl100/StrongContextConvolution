#!/usr/bin/env python3
"""
SCC — контроль размера AGENTS.md и подстановка фактических чисел
===================================================================

ЗАЧЕМ
------
`AGENTS.md` подаётся агенту автоматически при каждом запросе и **не сжимается
компакцией**. Но у механизма есть бюджет: `maxBytes` = 65 536 байт. При
превышении он **молча усекает** файл — сначала отбрасывает файлы целиком,
потом режет последний. Агент получит **неполный** текст, считая его полным.

Поэтому в самом файле записано условие «держать ниже 65 536» и указан текущий
размер. Числа в файле — плейсхолдеры `{{SIZE}}`, `{{PCT}}`, `{{FREE}}`, которые
проставляет этот инструмент. Иначе они устареют молча: живой урок проекта —
документ с числами устаревает вместе с инструментом, и обнаруживается это
только запуском.

ЧТО ДЕЛАЕТ
------------
1. Считает фактический размер `AGENTS.md`.
2. Подставляет числа в плейсхолдеры (если они есть).
3. Проверяет, что размер ниже бюджета.
4. Проверяет, что ссылки на методологии ведут к существующим файлам.

КОДЫ ВОЗВРАТА (соглашение SeverPact)
    0 — размер в норме
    1 — НАРУШЕНИЕ: превышен бюджет или битая ссылка
    2 — НЕ ИЗМЕРЕНО: файла нет

Запуск:
    python3 _engine/tools/check_agents_size.py
    python3 _engine/tools/check_agents_size.py --fix    # подставить числа
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

AGENTS = ROOT / "AGENTS.md"

# Бюджет из конфигурации DSH: `dsh-agent-instructions` -> maxBytes.
# Взят из dsh-base/cordis.patch.yml и dsh-web-app/presets/standard.patch.yml.
BUDGET_BYTES = 65_536

# Методологии, на которые обязан ссылаться указатель.
REQUIRED = (
    "METHODOLOGY.md",
    "METHODOLOGY_CORE.md",
    "METHODOLOGY_MEMORY.md",
    "METHODOLOGY_CAS.md",
    "METHODOLOGY_HEAD.md",
    "METHODOLOGY_STATE.md",
)

PLACEHOLDERS = ("{{SIZE}}", "{{PCT}}", "{{FREE}}")


def measure(text: str) -> int:
    """Размер в байтах, как его считает механизм (UTF-8)."""
    return len(text.encode("utf-8"))


def check_links(text: str) -> list[str]:
    """
    Битой считается ссылка на файл, которого нет.

    Проверяются только относительные ссылки: абсолютные и якоря не наша забота.
    """
    missing: list[str] = []
    for target in re.findall(r"\]\(([^)]+)\)", text):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        path = (AGENTS.parent / target).resolve()
        if not path.exists():
            missing.append(target)
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SCC: размер AGENTS.md и бюджет")
    parser.add_argument("--fix", action="store_true",
                        help="подставить фактические числа в плейсхолдеры")
    args = parser.parse_args(argv)

    if not AGENTS.exists():
        print(f"НЕ ИЗМЕРЕНО: нет {AGENTS}", file=sys.stderr)
        return 2

    text = AGENTS.read_text(encoding="utf-8")
    size = measure(text)
    percent = 100 * size / BUDGET_BYTES
    free = BUDGET_BYTES - size

    # ── Подстановка чисел ────────────────────────────────────────────────────
    fixed = text
    if "{{SIZE}}" in fixed:
        fixed = fixed.replace("{{SIZE}}", f"{size:,}".replace(",", " "))
    if "{{PCT}}" in fixed:
        fixed = fixed.replace("{{PCT}}", f"{percent:.1f}")
    if "{{FREE}}" in fixed:
        fixed = fixed.replace("{{FREE}}", f"{free:,}".replace(",", " "))

    if args.fix and fixed != text:
        # Пересчёт: подстановка меняет длину, поэтому числа ставятся ПОСЛЕ
        # вычисления и повторно уточняются один раз.
        size2 = measure(fixed)
        if size2 != size:
            fixed = fixed.replace(f"{size:,}".replace(",", " "),
                                  f"{size2:,}".replace(",", " "))
            size = measure(fixed)
            percent = 100 * size / BUDGET_BYTES
            free = BUDGET_BYTES - size
        AGENTS.write_text(fixed, encoding="utf-8")
        text = fixed
        print(f"числа подставлены: {size:,} байт")

    # ── Проверки ─────────────────────────────────────────────────────────────
    problems: list[str] = []
    if size > BUDGET_BYTES:
        problems.append(
            f"размер {size:,} байт превышает бюджет {BUDGET_BYTES:,} — "
            f"механизм усечёт файл молча"
        )
    missing = check_links(text)
    if missing:
        problems.append(f"битые ссылки: {', '.join(missing)}")
    if any(p in text for p in PLACEHOLDERS):
        problems.append(
            "в файле остались плейсхолдеры "
            f"({', '.join(p for p in PLACEHOLDERS if p in text)}) — "
            "запустите с --fix"
        )
    for name in REQUIRED:
        if name not in text:
            problems.append(f"нет ссылки на методологию {name}")

    print(f"── AGENTS.md ──")
    print(f"    размер:  {size:,} байт")
    print(f"    бюджет:  {BUDGET_BYTES:,} байт")
    print(f"    занято:  {percent:.1f}%")
    print(f"    запас:   {free:,} байт")

    if problems:
        print()
        for problem in problems:
            print(f"  НАРУШЕНИЕ: {problem}")
        return 1

    print()
    print("  размер в норме, ссылки целы")
    return 0


if __name__ == "__main__":
    sys.exit(main())
