#!/usr/bin/env python3
"""
SCC — эксперимент: держится ли роль в ДЛИННОЙ сессии
======================================================

ЗАДАЧА (методология HEAD §8.2, пункт 2)
-----------------------------------------
«Влияние роли в длинной сессии. Эксперимент измерял короткую задачу. Роль
может оказаться нужной, когда контекст накоплен, — **не проверено**.»

ЧТО ПРОВЕРЯЕТСЯ
----------------
Первые пробы (одна задача, один ход) показали: роль не меняет поведение,
потому что модель и без неё поступает по правилам. Но в **длинной** сессии
правило может «выветриваться»: чем больше контекста, тем слабее держится
инструкция из начала.

ГИПОТЕЗА: доля соблюдения правила падает к концу длинного диалога.

ПРАВИЛО ДЛЯ ПРОБЫ — ЗАВЕДОМО РАСХОДЯЩЕЕСЯ
--------------------------------------------
Проверять на правиле, которое модель выполняет и так, бессмысленно: нечего
измерять. Поэтому берётся правило, **противоречащее привычке**:

    «Прежде чем прочитать любой файл, сначала вызови glob с маской этого файла.»

Обычное поведение модели — сразу `read`. Правило требует предварительный
`glob`, то есть лишний шаг. Если роль работает, доля соблюдения будет выше.

МЕТРИКА
--------
Диалог из `steps` ходов. На каждом ходу — просьба прочитать файл. Считается
**первый** вызванный инструмент:

* `glob` — правило соблюдено;
* `read` — привычка победила.

Результат подаётся двумя долями: **начало** (первые 2 хода) и **конец**
(последние 2 хода). Если роль выветривается, конец будет ниже начала.

Запуск:
    python3 _engine/tools/experiment_role_decay.py --steps 8 --runs 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_BASE_URL = "https://routerai.ru/api/v1"
SANDBOX = Path("/tmp/exp_decay/data")

# Роль для пробы: ТОЛЬКО проверяемое правило. Остальные абзацы не подаются —
# иначе нельзя отделить вклад правила от влияния всей роли.
ROLE = (
    "Before reading ANY file, first call the glob tool with the file's mask "
    "to confirm the file exists. Only after that call read."
)

# Файлы для ходов: их и просит пользователь.
FILES = (
    "notes.txt", "фрукты.md", "овощи.md", "ягоды.md",
    "письмо.txt", "список.md", "отчёт.txt", "план.md",
)

TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Прочитать текстовый файл целиком.",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Найти файлы по маске пути (например '*.md').",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
]


def prepare_sandbox() -> Path:
    """Создать файлы для ходов."""
    SANDBOX.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(FILES):
        (SANDBOX / name).write_text(
            f"Файл {name}, строка 1.\nСтрока 2 файла {name}.\n", encoding="utf-8"
        )
    return SANDBOX


def call_llm(messages: list[dict], model: str, base_url: str, api_key: str,
             timeout: int = 300) -> dict | None:
    """Вызвать модель с инструментами."""
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOLS_SPEC,
        "tool_choice": "auto",
        "max_tokens": 512,
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
        print(f"    сетевой отказ: {error}", file=sys.stderr)
        return None


def run_dialog(role: str, steps: int, model: str, base_url: str,
               api_key: str) -> list[str]:
    """
    Провести диалог из `steps` ходов. Возвращает первый инструмент каждого хода.

    Контекст НАКАПЛИВАЕТСЯ: каждый ход добавляется в историю с ответом ассистента
    и результатом инструмента. Иначе диалог не был бы длинным, и гипотеза о
    выветривании не проверялась бы.
    """
    messages: list[dict] = []
    if role:
        messages.append({"role": "system", "content": role})

    firsts: list[str] = []
    for step in range(steps):
        name = FILES[step % len(FILES)]
        messages.append({
            "role": "user",
            "content": f"Прочитай файл {SANDBOX / name} и назови первую строку.",
        })

        response = call_llm(messages, model, base_url, api_key)
        if response is None:
            firsts.append("нет")
            break
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = message.get("tool_calls") or []
        if not calls:
            firsts.append("text")
            messages.append({"role": "assistant", "content": message.get("content") or ""})
            continue

        function = calls[0].get("function") or {}
        first = function.get("name") or "нет"
        firsts.append(first)

        # Ответ ассистента с вызовами и результат инструмента — чтобы контекст
        # рос, как в настоящей сессии.
        messages.append({
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": calls,
        })
        for raw in calls:
            fn = raw.get("function") or {}
            tool_name = fn.get("name") or ""
            result = (
                f"Файл {name}, строка 1." if tool_name == "read"
                else f"Найдено: {name}"
            )
            messages.append({
                "role": "tool",
                "tool_call_id": raw.get("id") or "",
                "content": result,
            })
    return firsts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SCC: выветривается ли роль")
    parser.add_argument("--steps", type=int, default=8, help="ходов в диалоге")
    parser.add_argument("--runs", type=int, default=3, help="диалогов на условие")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    api_key = os.environ.get("ROUTERAI_API_KEY", "").strip()
    if not api_key:
        print("НЕ ИЗМЕРЕНО: не задан ROUTERAI_API_KEY", file=sys.stderr)
        return 2
    if args.steps < 4:
        print("НАРУШЕНИЕ: нужно минимум 4 хода, иначе нечего сравнивать",
              file=sys.stderr)
        return 1

    prepare_sandbox()

    conditions = {"без роли": "", "с ролью": ROLE}
    results: dict[str, list[list[str]]] = {}
    for label, role in conditions.items():
        dialogs = []
        for run in range(args.runs):
            print(f"    {label}: диалог {run + 1} из {args.runs}", flush=True)
            dialogs.append(run_dialog(role, args.steps, args.model, args.base_url, api_key))
        results[label] = dialogs

    # ── Доли соблюдения: начало против конца ─────────────────────────────────
    window = 2
    summary = []
    for label, dialogs in results.items():
        total = sum(len(d) for d in dialogs)
        globs = sum(1 for d in dialogs for first in d if first == "glob")
        start = [d[:window] for d in dialogs if len(d) >= window]
        end = [d[-window:] for d in dialogs if len(d) >= window]
        start_rate = (sum(1 for d in start for f in d if f == "glob")
                      / max(1, sum(len(d) for d in start)))
        end_rate = (sum(1 for d in end for f in d if f == "glob")
                    / max(1, sum(len(d) for d in end)))
        summary.append({
            "condition": label,
            "steps": args.steps,
            "runs": args.runs,
            "calls": total,
            "glob_total": globs,
            "rate_total": round(globs / total, 3) if total else 0.0,
            "rate_start": round(start_rate, 3),
            "rate_end": round(end_rate, 3),
            "decay": round(start_rate - end_rate, 3),
            "dialogs": dialogs,
        })

    if args.json:
        print(json.dumps({"model": args.model, "summary": summary},
                         ensure_ascii=False, indent=2))
        return 0

    print()
    print(f"модель {args.model}, ходов {args.steps}, диалогов на условие {args.runs}")
    print(f"правило: сначала glob, потом read (привычка модели — сразу read)")
    print()
    header = f"{'условие':<12} {'glob всего':>12} {'начало':>9} {'конец':>9} {'убыль':>9}"
    print(header)
    print("-" * len(header))
    for row in summary:
        print(f"{row['condition']:<12} {row['glob_total']}/{row['calls']:>8} "
              f"{row['rate_start']:>9.2f} {row['rate_end']:>9.2f} {row['decay']:>+9.2f}")
    print()

    without, with_role = summary[0], summary[1]
    print(f"  СОБЛЮДЕНИЕ ПРАВИЛА (доля ходов с предварительным glob)")
    print(f"    без роли: {without['rate_total']:.2f}")
    print(f"    с ролью:  {with_role['rate_total']:.2f}")
    if with_role["rate_total"] - without["rate_total"] >= 0.2:
        print(f"  РОЛЬ РАБОТАЕТ: соблюдение выше на "
              f"{with_role['rate_total'] - without['rate_total']:.2f}")
    else:
        print(f"  РОЛЬ НЕ РАБОТАЕТ: различие "
              f"{with_role['rate_total'] - without['rate_total']:+.2f} — в пределах шума")
    print()
    print(f"  ВЫВЕТРИВАНИЕ (начало минус конец)")
    for row in summary:
        verdict = ("выветривается" if row["decay"] > 0.3
                   else "держится" if row["decay"] < 0.1
                   else "слабое падение")
        print(f"    {row['condition']}: {row['decay']:+.2f} — {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
