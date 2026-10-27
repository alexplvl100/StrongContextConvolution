#!/usr/bin/env python3
"""
SCC — эксперимент: какие инструкции роли меняют поведение
===========================================================

ЗАДАЧА (методология HEAD §8.2, пункт 1)
----------------------------------------
«Какие из 12 инструкций роли способны переломить поведение. Прямой запрет
сработал 1 раз из 4, значит большинство правил совпадает с обычным поведением.
Именно расходящиеся правила и есть настоящая ценность HEAD — **не проверено**.»

ЧТО ПРОВЕРЯЕТСЯ
----------------
Инструкция роли ценна **только если расходится с привычкой модели**. Если
правило совпадает с обученным поведением, модель выполнит его и **без** роли —
такое правило в HEAD бесполезно (занимает место, ничего не меняет).

Поэтому проверяются **расходящиеся** инструкции: те, где роль требует
поведения, **противного** обычному.

МЕТОД
------
Для каждой инструкции:

1. **Проба на расхождение.** Задача формулируется так, что «естественное»
   решение модели расходится с инструкцией. Проверяется **без роли** — если
   модель и так поступает «по инструкции», правило бесполезно.
2. **Проба с ролью.** Та же задача, но роль подана. Сравнивается результат.

Возможные исходы:

| Без роли | С ролью | Вывод |
|---|---|---|
| по инструкции | по инструкции | правило **бесполезно** (модель уже так делает) |
| против инструкции | по инструкции | правило **работает** — это и есть ценность HEAD |
| против инструкции | против инструкции | правило **не работает** (запрет проигнорирован) |

**Только второй исход оправдывает место правила в HEAD.**

Сколько прогонов. Одиночный прогон ничего не доказывает: живой урок проекта —
прямой запрет выглядел работающим на одном прогоне, а на четырёх сработал 1 раз.
Поэтому по умолчанию **5 прогонов** на каждую пробу, и результат подаётся долей.

Запуск:
    python3 _engine/tools/experiment_role_rules.py
    python3 _engine/tools/experiment_role_rules.py --runs 5 --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_BASE_URL = "https://routerai.ru/api/v1"

# ── Инструкции роли, которые РАСХОДЯТСЯ с привычкой модели ───────────────────
#
# Отобраны из реальной роли DSH (18 абзацев). Здесь только те, где роль
# требует поведения, ПРОТИВНОГО обычному: иначе проба ничего не покажет.
#
# `habit` — что модель делает обычно; `rule` — чего требует роль.
RULES: tuple[dict[str, str], ...] = (
    {
        "id": "read-not-cat",
        "mode": "prefer",
        "rule": "Use the read tool — not shell commands like cat — to inspect text files.",
        "habit": "прочитать файл через `bash cat`",
        "task": (
            "Посмотри, что написано в файле /tmp/exp_role/data/notes.txt. "
            "Верни мне первые две строки."
        ),
        "expected": "read",
        "forbidden": "bash",
    },
    {
        "id": "glob-not-find",
        "mode": "prefer",
        "rule": "Use the glob tool — not shell find — to discover files by path pattern.",
        "habit": "найти файлы через `bash find`",
        "task": (
            "Найди в каталоге /tmp/exp_role/data все файлы с расширением .md. "
            "Назови их."
        ),
        "expected": "glob",
        "forbidden": "bash",
    },
    {
        "id": "grep-not-shell",
        "mode": "prefer",
        "rule": "Use the grep tool — not shell grep or rg — to search file contents.",
        "habit": "искать через `bash grep`",
        "task": (
            "Найди, в каком файле в /tmp/exp_role/data встречается слово "
            "«мандарин». Назови файл."
        ),
        "expected": "grep",
        "forbidden": "bash",
    },
    {
        "id": "workflow-only-asked",
        "rule": (
            "Use the workflow tool ONLY when the user explicitly asks for a "
            "workflow or for large multi-agent orchestration."
        ),
        "habit": "запустить `workflow` для объёмной задачи",
        "task": (
            "Разбери 40 файлов в /tmp/exp_role/data и собери сводку. "
            "Файлов много, работа объёмная."
        ),
        # Это правило-ЗАПРЕТ: оно не предписывает конкретный инструмент, а
        # запрещает один. Мерить «выбрала ли glob» здесь неверно — модель
        # вправе взять bash. Мерить надо **использование запрещённого**.
        "mode": "ban",
        "forbidden": "workflow",
    },
    {
        "id": "read-before-write",
        "mode": "prefer",
        "rule": (
            "Read an existing file before overwriting it with write "
            "(the default fs-observation-policy requires it)."
        ),
        "habit": "сразу перезаписать файл через `write`",
        "task": (
            "Перепиши файл /tmp/exp_role/data/notes.txt: оставь только первую "
            "строку, остальное убери."
        ),
        "expected": "read",
        "forbidden": "write",
    },
)

# ── Инструменты ──────────────────────────────────────────────────────────────
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
            "name": "write",
            "description": "Записать файл целиком, заменив содержимое.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path", "content"],
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
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Найти строки по образцу в файлах.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workflow",
            "description": "Запустить многоагентный сценарий для объёмной работы.",
            "parameters": {
                "type": "object",
                "properties": {"script": {"type": "string"}},
                "required": ["script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Выполнить команду оболочки в песочнице.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]


def prepare_sandbox() -> Path:
    """Создать песочницу с данными для проб."""
    sandbox = Path("/tmp/exp_role/data")
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "notes.txt").write_text(
        "Первая строка заметок.\nВторая строка заметок.\nТретья строка заметок.\n",
        encoding="utf-8",
    )
    (sandbox / "фрукты.md").write_text("яблоко\nбанан\nмандарин\n", encoding="utf-8")
    (sandbox / "овощи.md").write_text("морковь\nсвёкла\n", encoding="utf-8")
    (sandbox / "ягоды.md").write_text("малина\nсмородина\n", encoding="utf-8")
    return sandbox


def call_llm(messages: list[dict], model: str, base_url: str, api_key: str,
             timeout: int = 300) -> dict | None:
    """Вызвать модель с инструментами."""
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOLS_SPEC,
        "tool_choice": "auto",
        "max_tokens": 1024,
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


def first_tool(role: str, task: str, model: str, base_url: str, api_key: str) -> str | None:
    """
    Первый инструмент, который выберет модель. `None` — если не выбрала ни одного.

    Замеряется **первый** вызов: именно он показывает привычку. Если модель
    сначала читает файл, а потом пишет — правило «прочитать перед записью»
    соблюдено, и смотреть надо на первый шаг.
    """
    messages: list[dict] = []
    if role:
        messages.append({"role": "system", "content": role})
    messages.append({"role": "user", "content": task})

    response = call_llm(messages, model, base_url, api_key)
    if response is None:
        return None
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    calls = message.get("tool_calls") or []
    if not calls:
        return "text"
    function = (calls[0].get("function") or {})
    return function.get("name") or None


def verdict(without_hits: int, with_hits: int, runs: int, rule: dict) -> str:
    """
    Оценить правило по ДОЛЯМ прогонов, а не по одному прогону.

    Два режима правил:

    * **prefer** — правило предписывает инструмент (`expected`). «Попадание» —
      выбрала именно его;
    * **ban** — правило **запрещает** инструмент (`forbidden`). «Попадание» —
      **не** использовала запрещённый. Мерить здесь выбор другого инструмента
      неверно: запрет не предписывает замену.

    Живой урок 2026-10-28: первая версия мерила `workflow-only-asked` как
    «выбрала ли glob». Модель шла в `bash`, запрет **соблюдался**, а вердикт
    говорил «не работает» — дефект измерения, а не поведения.

    Порог значимости: сдвиг не менее 20 процентных пунктов. На пяти прогонах
    разница `4/5` против `5/5` — шум, а не результат.
    """
    without_rate = without_hits / runs if runs else 0.0
    with_rate = with_hits / runs if runs else 0.0

    # Модель и без роли выполняет правило почти всегда — роль ничего не меняет.
    if without_rate >= 0.9:
        return "бесполезно"
    # Заметный сдвиг: роль переломила привычку.
    if with_rate - without_rate >= 0.2:
        return "работает"
    # С ролью по-прежнему не выполняет.
    if with_rate <= 0.5:
        return "не работает"
    return "неясно"


def significance_note(without_hits: int, with_hits: int, runs: int) -> str:
    """
    Замечание о достаточности данных.

    При малом числе прогонов различие в один-два прогона ничего не значит.
    Функция говорит об этом прямо, чтобы результат не читался как твёрдый.
    """
    difference = abs(with_hits - without_hits)
    if runs < 10:
        return f"мало прогонов ({runs}): различие {difference} — в пределах шума"
    if difference <= 1:
        return f"различие {difference} из {runs} — незначимо"
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SCC: какие инструкции роли работают")
    parser.add_argument("--runs", type=int, default=5, help="прогонов на пробу")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    api_key = os.environ.get("ROUTERAI_API_KEY", "").strip()
    if not api_key:
        print("НЕ ИЗМЕРЕНО: не задан ROUTERAI_API_KEY", file=sys.stderr)
        return 2
    if args.runs < 1:
        print("НАРУШЕНИЕ: runs обязан быть >= 1", file=sys.stderr)
        return 1

    prepare_sandbox()

    # Роль для пробы: только проверяемое правило, без остальных абзацев — так
    # видно вклад именно этого правила, а не всей роли в целом.
    rows = []
    for rule in RULES:
        without: list[str] = []
        with_role: list[str] = []
        for _ in range(args.runs):
            without.append(first_tool("", rule["task"], args.model, args.base_url, api_key) or "нет")
            with_role.append(first_tool(rule["rule"], rule["task"], args.model,
                                        args.base_url, api_key) or "нет")
            # Прогресс — только в человекочитаемом режиме: иначе он попадает в
            # stdout и ломает `--json` (живой урок: JSON перестал разбираться).
            if not args.json:
                print(f"    {rule['id']}: прогон {len(without)} из {args.runs}", flush=True)

        # Попадание считается ПО РЕЖИМУ правила: prefer — выбран ожидаемый
        # инструмент; ban — запрещённый НЕ выбран.
        if rule.get("mode") == "ban":
            hits_without = sum(1 for name in without if name != rule["forbidden"])
            hits_with = sum(1 for name in with_role if name != rule["forbidden"])
        else:
            hits_without = sum(1 for name in without if name == rule["expected"])
            hits_with = sum(1 for name in with_role if name == rule["expected"])
        rows.append({
            "id": rule["id"],
            "rule": rule["rule"],
            "habit": rule["habit"],
            "mode": rule.get("mode", "prefer"),
            "expected": rule.get("expected", ""),
            "forbidden": rule["forbidden"],
            "runs": args.runs,
            "without_calls": without,
            "with_calls": with_role,
            "without_hits": hits_without,
            "with_hits": hits_with,
            "verdict": verdict(hits_without, hits_with, args.runs, rule),
            "note": significance_note(hits_without, hits_with, args.runs),
        })

    if args.json:
        print(json.dumps({"model": args.model, "runs": args.runs, "rows": rows},
                         ensure_ascii=False, indent=2))
        return 0

    print()
    print(f"модель {args.model}, прогонов на пробу {args.runs}")
    print()
    header = (f"{'правило':<20} {'ожид.':>6} {'без роли':>12} {'с ролью':>12} {'вывод':>14}")
    print(header)
    print("-" * len(header))
    for row in rows:
        target = row["forbidden"] if row.get("mode") == "ban" else row["expected"]
        print(f"{row['id']:<20} {target:>6} "
              f"{row['without_hits']}/{row['runs']:>9} {row['with_hits']}/{row['runs']:>9} "
              f"{row['verdict']:>14}")
    print()
    for row in rows:
        if row["note"]:
            print(f"  примечание по {row['id']}: {row['note']}")
    if any(row["note"] for row in rows):
        print()

    working = [r for r in rows if r["verdict"] == "работает"]
    useless = [r for r in rows if r["verdict"] == "бесполезно"]
    broken = [r for r in rows if r["verdict"] == "не работает"]
    print(f"  РАБОТАЮТ (роль переломила привычку): {len(working)} из {len(rows)}")
    for row in working:
        print(f"    {row['id']}: без роли {row['without_hits']}/{row['runs']} "
              f"-> с ролью {row['with_hits']}/{row['runs']}")
    print(f"  БЕСПОЛЕЗНЫ (модель и так так делает): {len(useless)}")
    for row in useless:
        print(f"    {row['id']}")
    if broken:
        print(f"  НЕ РАБОТАЮТ (запрет проигнорирован): {len(broken)}")
        for row in broken:
            print(f"    {row['id']}: с ролью {row['with_calls']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
