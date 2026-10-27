#!/usr/bin/env python3
"""
SCC — эксперимент: влияет ли роль агента на работу с инструментами
====================================================================

ПРОВЕРЯЕМАЯ ГИПОТЕЗА
--------------------
Из [`METHODOLOGY_HEAD.md`](../METHODOLOGY_HEAD.md) §8.1:

> Агент **без роли** перестаёт вызывать инструменты и теряет правила
> рассуждения.

Это **основание всей конструкции HEAD**: роль выносится из чанка именно потому,
что без неё агент перестаёт работать правильно. До этого эксперимента гипотеза
опиралась на рассуждение, а не на замер.

ПОЧЕМУ ЭКСПЕРИМЕНТ ВОЗМОЖЕН
-----------------------------
Роль DSH содержит **конкретные проверяемые инструкции** по выбору инструмента.
Извлечено из реальной сессии:

    «Use the read tool — not shell commands like cat — to inspect text files.»
    «Use the glob tool — not shell find — to discover files by path pattern.»
    «Use the grep tool — not shell grep or rg — to search file contents.»
    «Check the [exit code: N] marker on every bash result.»

То есть роль не «общие слова», а **правило выбора**: один и тот же результат
достигается двумя путями (`read` или `bash cat`), и роль предписывает один.

ДИЗАЙН
-------
Агенту даются **четыре инструмента** — `read`, `glob`, `grep` и `bash`. Первые
три объявлены описаниями; `bash` реально умеет `cat`, `find`, `grep`.

Условия (единственное различие — наличие роли):

    С РОЛЬЮ     системный промт = реальная роль из сессии DSH
    БЕЗ РОЛИ    системный промт отсутствует

Задача одна и та же, требует трёх действий: прочитать файл, найти файлы по
маске, найти текст в файлах. Каждое действие достижимо **обоими** путями.

ЧТО ИЗМЕРЯЕТСЯ
---------------
**Выбор инструмента**, а не качество ответа. Считается доля «правильных»
выборов (`read`/`glob`/`grep`) против «неправильных» (`bash` с `cat`/`find`/
`grep`).

Гипотеза подтверждается, если **без роли доля правильных выборов значимо ниже**.

ОГРАНИЧЕНИЯ
------------
* Одна модель (`deepseek/deepseek-v4.1-flash`).
* Прогонов немного (по умолчанию 5 на условие): эксперимент качественный,
  а не статистически строгий.
* Инструменты описаны упрощённо, а не схемой вызова DSH.

Запуск:
    export ROUTERAI_API_KEY=...
    python3 _engine/tools/experiment_head.py
    python3 _engine/tools/experiment_head.py --runs 8 --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "_engine"))

from memory.head import extract_head  # noqa: E402

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_BASE_URL = "https://routerai.ru/api/v1"

# ── Песочница ────────────────────────────────────────────────────────────────
SANDBOX = Path("/tmp/exp_head/sandbox")

# ── Инструменты ──────────────────────────────────────────────────────────────
# Четыре инструмента, из которых два пути ведут к одному результату.
# `read`/`glob`/`grep` — «правильные» по роли; `bash` с cat/find/grep —
# «неправильные», но результат тот же.
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

# ── Задача ───────────────────────────────────────────────────────────────────
# Три действия, каждое достижимо двумя путями. Формулировка НЕ подсказывает
# инструмент: называется цель, а не способ.
TASK = (
    "Работай в каталоге /tmp/exp_head/sandbox. Выполни три действия и отчитайся:\n"
    "1. Прочитай файл data.txt и приведи его содержимое.\n"
    "2. Найди все файлы с расширением .md в этом каталоге.\n"
    "3. Найди, в каких файлах встречается слово 'line'."
)

# ── Классификация вызовов ────────────────────────────────────────────────────
# «Правильный» выбор — инструмент, предписанный ролью.
# «Неправильный» — bash с командой, дублирующей инструмент.
SHELL_DUPLICATES = {
    "cat": "read",
    "head": "read",
    "tail": "read",
    "find": "glob",
    "ls": "glob",
    "grep": "grep",
    "rg": "grep",
}


# ── Действия задачи ──────────────────────────────────────────────────────────
# Три действия, каждое достижимо двумя путями. Оценивается ПЕРВЫЙ вызов,
# решающий действие, а не все вызовы: агент может сделать лишние шаги
# (проверить каталог, перечитать файл), и они не должны искажать метрику.
#
# ЖИВОЙ УРОК: первая версия считала долю ВСЕХ вызовов, и лишний `bash pwd && ls`
# в начале прогона портил оценку при том, что все три действия были выполнены
# правильно. Метрика измеряла активность, а не выбор инструмента.
ACTIONS: tuple[tuple[str, str], ...] = (
    ("прочитать data.txt", "read"),
    ("найти .md", "glob"),
    ("найти 'line'", "grep"),
)


def action_of(call: dict) -> str | None:
    """
    Определить, какое действие решает вызов. None — вызов не решает ни одного.

    Определяется по СОДЕРЖАНИЮ вызова, а не по имени инструмента: `bash` с
    `cat data.txt` решает то же действие, что `read` с тем же файлом.
    """
    name = call.get("name", "")
    args = call.get("arguments", {})
    if name == "read":
        if "data.txt" in str(args.get("file_path", "")):
            return "прочитать data.txt"
        return None
    if name == "glob":
        return "найти .md"
    if name == "grep":
        return "найти 'line'"
    if name == "bash":
        command = str(args.get("command", ""))
        # какое действие выполняет команда
        for token in re.split(r"[|;&]+", command):
            parts = token.strip().split()
            if not parts:
                continue
            head = parts[0]
            rest = " ".join(parts[1:])
            if head in ("cat", "head", "tail") and "data.txt" in rest:
                return "прочитать data.txt"
            if head == "find" and ".md" in rest:
                return "найти .md"
            # `ls` БЕЗ фильтра по .md — это осмотр каталога, а не поиск по маске.
            # Живой урок: первая версия считала `ls -la` решением действия, и
            # ориентировочный вызов портил метрику — агент осматривался, а
            # оценка падала как за неверный инструмент.
            if head == "ls" and ".md" in rest:
                return "найти .md"
            if head in ("grep", "rg") and "line" in rest:
                return "найти 'line'"
        return None
    return None


def verdict_for_action(call: dict, expected: str) -> str:
    """Оценить выбор для конкретного действия."""
    return "правильно" if call.get("name") == expected else "неправильно"


def evaluate(calls: list[dict]) -> dict[str, str]:
    """
    Оценить прогон по действиям: первое решение каждого действия.

    Возвращает {действие: вердикт}. Вердикт «пропущено», если действие не
    выполнено вовсе — это отдельный исход, и он важен: агент мог просто не
    справиться с задачей.
    """
    result: dict[str, str] = {}
    for title, expected in ACTIONS:
        chosen = next((c for c in calls if action_of(c) == title), None)
        result[title] = "пропущено" if chosen is None else verdict_for_action(chosen, expected)
    return result


def execute(call: dict) -> str:
    """Выполнить вызов инструмента по-настоящему в песочнице."""
    name = call.get("name", "")
    args = call.get("arguments", {})
    try:
        if name == "read":
            path = SANDBOX / Path(str(args.get("file_path", ""))).name
            return path.read_text(encoding="utf-8")[:2000]
        if name == "glob":
            pattern = str(args.get("pattern", "*"))
            found = sorted(p.name for p in SANDBOX.glob(pattern))
            return json.dumps(found, ensure_ascii=False)
        if name == "grep":
            pattern = str(args.get("pattern", ""))
            hits = []
            for path in sorted(SANDBOX.iterdir()):
                if path.is_file():
                    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                        if pattern in line:
                            hits.append(f"{path.name}:{number}:{line}")
            return "\n".join(hits) or "нет совпадений"
        if name == "bash":
            command = str(args.get("command", ""))
            result = subprocess.run(
                command, shell=True, cwd=SANDBOX,
                capture_output=True, text=True, timeout=20,
            )
            out = (result.stdout + result.stderr).strip()
            return f"{out[:1500]}\nexit code: {result.returncode}"
    except Exception as error:  # инструмент может отказать — это тоже данные
        return f"ошибка инструмента: {error}"
    return "неизвестный инструмент"


def call_llm(messages: list[dict], model: str, base_url: str, api_key: str,
             timeout: int = 300) -> dict | None:
    """Вызвать модель с инструментами."""
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOLS_SPEC,
        "tool_choice": "auto",
        "max_tokens": 8192,
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


def load_role() -> str:
    """
    Взять роль из реальной сессии DSH.

    Роль извлекается тем же кодом, что и в реализации: `extract_head`. Так
    эксперимент проверяет **тот же самый текст**, который пойдёт в HEAD.
    """
    sessions = sorted(
        Path("~/.dsh/sessions/--root-Projects-MultiAgentDSH--").glob("*/session.v4.jsonl.zstd"),
        key=lambda p: -p.stat().st_size,
    )
    for session in sessions[:5]:
        out = subprocess.run(
            [sys.executable, str(ROOT / "_engine" / "core" / "scc_events.py"),
             str(session), "--seq-to", "40", "--out", "/tmp/exp_head/stream.txt"],
            capture_output=True, text=True,
        )
        stream = Path("/tmp/exp_head/stream.txt")
        if not stream.exists():
            continue
        head, _ = extract_head(stream.read_text(encoding="utf-8"))
        if head.role:
            return head.role
    return ""


def run_once(role: str, model: str, base_url: str, api_key: str) -> dict:
    """Один прогон: агент получает задачу и выбирает инструменты."""
    messages: list[dict] = []
    if role:
        messages.append({"role": "system", "content": role})
    messages.append({"role": "user", "content": TASK})

    calls: list[dict] = []

    # До пяти шагов: три действия плюс возможные лишние проверки.
    for _step in range(5):
        response = call_llm(messages, model, base_url, api_key)
        if response is None:
            break
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") or []
        answer = message.get("content") or ""
        if not tool_calls:
            break
        messages.append({"role": "assistant", "content": answer, "tool_calls": tool_calls})
        for raw in tool_calls:
            function = raw.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            call = {"name": function.get("name", ""), "arguments": arguments}
            calls.append(call)
            messages.append({
                "role": "tool",
                "tool_call_id": raw.get("id", ""),
                "content": execute(call),
            })
    return {"calls": calls, "actions": evaluate(calls)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SCC: роль и выбор инструментов")
    parser.add_argument("--runs", type=int, default=5, help="прогонов на условие")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    api_key = os.environ.get("ROUTERAI_API_KEY", "").strip()
    if not api_key:
        print("НЕ ИЗМЕРЕНО: нет ROUTERAI_API_KEY", file=sys.stderr)
        return 2

    SANDBOX.mkdir(parents=True, exist_ok=True)
    role = load_role()
    if not role:
        print("НЕ ИЗМЕРЕНО: не удалось извлечь роль из сессии", file=sys.stderr)
        return 2

    print(f"роль: {len(role)} символов", file=sys.stderr)
    print(f"прогонов на условие: {args.runs}", file=sys.stderr)

    results: dict[str, list[dict]] = {"с ролью": [], "без роли": []}
    for condition, supplied in (("с ролью", role), ("без роли", "")):
        print(f"── условие: {condition}", file=sys.stderr)
        for run in range(1, args.runs + 1):
            outcome = run_once(supplied, args.model, args.base_url, api_key)
            results[condition].append(outcome)
            acts = outcome["actions"]
            good = sum(1 for v in acts.values() if v == "правильно")
            bad = sum(1 for v in acts.values() if v == "неправильно")
            skipped = sum(1 for v in acts.values() if v == "пропущено")
            print(f"   прогон {run}: вызовов {len(outcome['calls'])}, действий верно {good}/3, "
                  f"неверно {bad}, пропущено {skipped}", file=sys.stderr)

    # ── Сводка ───────────────────────────────────────────────────────────────
    # Считаются ДЕЙСТВИЯ, а не вызовы: три действия на прогон, всего 3*N.
    summary = {}
    for condition, outcomes in results.items():
        good = sum(v == "правильно" for o in outcomes for v in o["actions"].values())
        bad = sum(v == "неправильно" for o in outcomes for v in o["actions"].values())
        skipped = sum(v == "пропущено" for o in outcomes for v in o["actions"].values())
        total = good + bad
        summary[condition] = {
            "правильно": good,
            "неправильно": bad,
            "пропущено": skipped,
            "всего_действий": total,
            "доля_правильных": round(good / total, 3) if total else None,
            "прогонов": len(outcomes),
        }

    if args.json:
        print(json.dumps({"summary": summary, "runs": results}, ensure_ascii=False, indent=2))
        return 0

    print()
    print("=" * 82)
    print(f"{'условие':>12} {'верно':>7} {'неверно':>9} {'пропущено':>11} {'доля':>7}")
    print("-" * 82)
    for condition, row in summary.items():
        share = f"{row['доля_правильных']:.0%}" if row["доля_правильных"] is not None else "—"
        print(f"{condition:>12} {row['правильно']:>7} {row['неправильно']:>9} "
              f"{row['пропущено']:>11} {share:>7}")
    print("=" * 82)

    with_role = summary["с ролью"]
    without_role = summary["без роли"]
    print()
    if with_role["доля_правильных"] is None or without_role["доля_правильных"] is None:
        print("НЕ ИЗМЕРЕНО: не было вызовов инструментов — гипотеза не проверена")
        return 2
    if with_role["доля_правильных"] > without_role["доля_правильных"]:
        print("ГИПОТЕЗА ПОДТВЕРЖДЕНА: с ролью выбор инструментов правильнее")
        return 0
    if with_role["доля_правильных"] == without_role["доля_правильных"]:
        print("ГИПОТЕЗА НЕ ПОДТВЕРЖДЕНА: разницы нет")
        return 1
    print("ГИПОТЕЗА ОПРОВЕРГНУТА: без роли выбор оказался правильнее")
    return 1


if __name__ == "__main__":
    sys.exit(main())
