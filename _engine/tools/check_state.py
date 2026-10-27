#!/usr/bin/env python3
"""
SCC — проверка состояний методологии
=====================================

Читает строку `Состояние:` в шапке каждого документа методологии и сообщает,
разрешена ли реализация. Правила — в [`METHODOLOGY_STATE.md`](../METHODOLOGY_STATE.md).

ЗАЧЕМ МАШИННАЯ ПРОВЕРКА
------------------------
Состояние существует **против потери контекста агентом**: после потери он не
помнит, что было решено, и начинает править связную методологию. Если правило
держится на «помни и не трогай», оно обходится тем самым сбоем, от которого
поставлено. Скрипт делает правило **проверяемым**: состояние видно в файле, а
запрет — в коде возврата.

ДВА РАЗНЫХ ПОЛЯ, НЕ ПУТАТЬ
----------------------------
    Состояние:    В_РАБОТЕ | ПРИНЯТ      <- можно ли ПРАВИТЬ (меняет пользователь)
    Реализация:   свободный текст        <- что СДЕЛАНО в коде (меняет исполнитель)

Методология может быть ПРИНЯТ и при этом не реализована — это нормально.

КОДЫ ВОЗВРАТА (соглашение SeverPact)
--------------------------------------
    0 — все ПРИНЯТ: реализация разрешена
    1 — есть В_РАБОТЕ: реализация ЗАПРЕЩЕНА
    2 — НЕ ИЗМЕРЕНО: состояние не указано или документ нечитаем

Отсутствие состояния трактуется как **запрет**, а не как «разрешено»:
«не измерено» отличается от «чисто».

Использование:
    python3 tools/check_state.py           # таблица состояний
    python3 tools/check_state.py --gate    # только код возврата, для CI/скриптов
    python3 tools/check_state.py --json    # машинный вывод
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Скрипт лежит в _engine/tools, методологии — в корне репозитория.
# Здесь было HERE.parent, но при переносе в _engine/tools это стало _engine,
# и методологии «пропали» (НЕТ ФАЙЛА вместо ПРИНЯТ) — живой урок 2026-10-27.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

# ── Список методологий ───────────────────────────────────────────────────────
# Список ЗАКРЫТЫЙ: новый документ методологии обязан быть добавлен сюда вместе
# с состоянием. Иначе он не проверяется, и контроль целостности его не видит.
# METHODOLOGY_STATE.md входит намеренно: он сам себе не исключение.
METHODOLOGIES: tuple[str, ...] = (
    "METHODOLOGY.md",
    "METHODOLOGY_CORE.md",
    "METHODOLOGY_MEMORY.md",
    "METHODOLOGY_CAS.md",
    "METHODOLOGY_HEAD.md",
    "METHODOLOGY_STATE.md",
)

# ── Допустимые состояния ─────────────────────────────────────────────────────
STATE_WORK = "В_РАБОТЕ"
STATE_ACCEPTED = "ПРИНЯТ"
VALID_STATES = (STATE_WORK, STATE_ACCEPTED)

# Строка состояния в ШАПКЕ: «**Состояние:** ПРИНЯТ».
#
# Читается только НАЧАЛО документа (первые HEAD_LINES строк), а не весь текст:
# в METHODOLOGY_STATE.md состояние упоминается ещё и внутри примера (§3.1), и
# поиск по всему файлу нашёл бы пример вместо шапки. Живой урок 2026-10-26.
STATE_RE = re.compile(r"^\*\*Состояние:\*\*\s*(\S+)", re.M)
HEAD_LINES = 12

# Код возврата: 0 разрешено, 1 запрещено, 2 не измерено.
CODE_OK = 0
CODE_BLOCKED = 1
CODE_UNMEASURED = 2


def read_state(path: Path) -> str | None:
    """
    Прочитать состояние из шапки документа.

    None означает «не указано» — это НЕ разрешение. Нечитаемый файл тоже даёт
    None: «не измерено» отличается от «чисто».
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    head = "\n".join(text.split("\n")[:HEAD_LINES])
    match = STATE_RE.search(head)
    return match.group(1).strip() if match else None


def collect() -> tuple[list[dict], int]:
    """
    Собрать состояния всех методологий.

    Возвращает (строки отчёта, код возврата). Код — по худшему из найденных:
    не измерено важнее запрета, запрет важнее разрешения.
    """
    rows: list[dict] = []
    code = CODE_OK
    for name in METHODOLOGIES:
        path = ROOT / name
        exists = path.exists()
        state = read_state(path) if exists else None
        if not exists:
            verdict = "НЕТ ФАЙЛА"
            code = max(code, CODE_UNMEASURED)
        elif state is None:
            verdict = "НЕ УКАЗАНО"
            code = max(code, CODE_UNMEASURED)
        elif state not in VALID_STATES:
            verdict = f"НЕИЗВЕСТНО ({state})"
            code = max(code, CODE_UNMEASURED)
        else:
            verdict = state
            if state == STATE_WORK:
                code = max(code, CODE_BLOCKED)
        rows.append({"document": name, "state": state, "verdict": verdict})
    return rows, code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SCC: состояния методологии")
    parser.add_argument("--gate", action="store_true", help="только код возврата")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    args = parser.parse_args(argv)

    rows, code = collect()

    if args.json:
        print(json.dumps(
            {
                "methodologies": rows,
                "implementation_allowed": code == CODE_OK,
                "code": code,
                "states": {"work": STATE_WORK, "accepted": STATE_ACCEPTED},
            },
            ensure_ascii=False, indent=2,
        ))
        return code

    if args.gate:
        # Для скриптов: только код возврата, без вывода.
        return code

    print("── состояния методологии ──")
    width = max(len(r["document"]) for r in rows)
    for row in rows:
        marker = " " if row["verdict"] == STATE_ACCEPTED else "!"
        print(f"  {marker} {row['document']:<{width}}  {row['verdict']}")

    print()
    if code == CODE_OK:
        print("РЕАЛИЗАЦИЯ РАЗРЕШЕНА: все методологии ПРИНЯТ")
    elif code == CODE_BLOCKED:
        print(
            "РЕАЛИЗАЦИЯ ЗАПРЕЩЕНА: есть методология в состоянии "
            f"{STATE_WORK}.\nВсе работы по реализации методологий остановлены "
            "(METHODOLOGY_STATE.md §3).",
            file=sys.stderr,
        )
    else:
        print(
            "НЕ ИЗМЕРЕНО: состояние не указано или документ нечитаем.\n"
            "Отсутствие состояния — не разрешение, а неизвестность (§3.2).",
            file=sys.stderr,
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
