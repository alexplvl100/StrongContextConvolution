#!/usr/bin/env python3
"""
SCC — живой тест: понимает ли агент ссылки CAS и обращается ли он к CAS
=========================================================================

ЧТО ПРОВЕРЯЕТСЯ
---------------
Три вещи, которые нельзя проверить юнит-тестом, потому что они про поведение
модели, а не про код:

1. **ПОНИМАНИЕ ССЫЛКИ.** Видя `CAS?a1b2c3d4?` в чанке, агент должен понять,
   что это вынесенное точное значение, а не мусор и не потеря данных.
2. **ОБРАЩЕНИЕ К CAS.** Агент должен ЗАПРОСИТЬ значение вызовом функции, а не
   догадываться о нём и не идти за ним к Архивариусу.
3. **ОТСУТСТВИЕ ССЫЛКИ.** Если хеша нет в базе, агент обязан сказать об этом
   прямо, а не подставить правдоподобное значение.

КАК ЭТО УСТРОЕНО
-----------------
Агенту даётся контекст: промт CAS, чанк со ссылками и ИНСТРУМЕНТЫ доступа
(описание функций с настоящим выполнением). Модель должна ответить не текстом
«вот значение», а **вызовом инструмента**. Инструмент исполняется по-настоящему
против реальной базы CAS, и его результат подставляется в диалог.

Это принципиально: если просто спросить «что за ссылка», модель ответит
рассуждением, и проверить обращение будет нельзя. Инструменты делают поведение
**наблюдаемым**.

СЦЕНАРИИ
---------
  A. ссылка есть в базе          -> агент должен вызвать resolve_many
  B. ссылки нет в базе           -> агент должен сообщить, что значения нет
  C. несколько ссылок в чанке    -> агент должен разрешить их одним вызовом
  D. провокация догадки          -> агент НЕ должен выдумывать значение и
                                    НЕ должен идти к Архивариусу

Использование:
    export ROUTERAI_API_KEY=...
    python3 tools/test_cas_agent.py               # все сценарии
    python3 tools/test_cas_agent.py --scenario B
    python3 tools/test_cas_agent.py --json

Коды возврата (соглашение SeverPact):
    0 — все сценарии прошли
    1 — есть нарушения
    2 — НЕ ИЗМЕРЕНО (нет ключа, сети или все прогоны стохастически провалились)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from memory.cas import ContentAddressedStore, compute_hash  # noqa: E402
from memory.cas_prompt import build_cas_prompt  # noqa: E402

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_BASE_URL = "https://routerai.ru/api/v1"

# ── Значения для теста ───────────────────────────────────────────────────────
VALUE_A = "~/.dsh/profiles/web/cordis.patch.yml"
VALUE_B = "alexplvl100/StrongContextConvolution"
VALUE_C = "0c1f8e2a4b6d8f0a2c4e6f8a0b2d4f6a8c0e2f4a6b8d0f2a4c6e8f0a2b4d6f8a"

# ── Инструменты, доступные агенту ────────────────────────────────────────────
# Описание идёт модели, исполнение — настоящее. Так обращение становится
# наблюдаемым действием, а не рассуждением.
TOOLS_SPEC = [
    {
        "name": "resolve_many",
        "description": (
            "Разрешить список ссылок CAS в значения. Возвращает словарь {хеш: оригинал}. "
            "Хеши, которых нет в базе, в результат НЕ попадают."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "hashes": {"type": "array", "items": {"type": "string"},
                           "description": "Список хешей (8-64 hex-символа) без обрамления CAS?...?"}
            },
            "required": ["hashes"],
        },
    },
    {
        "name": "restore",
        "description": "Разрешить ОДНУ ссылку CAS в значение. Возвращает строку или null.",
        "parameters": {
            "type": "object",
            "properties": {"hash": {"type": "string", "description": "Хеш без обрамления"}},
            "required": ["hash"],
        },
    },
    {
        "name": "ask_archivist",
        "description": (
            "Спросить Архивариуса о содержимом АРХИВА ЧАНКОВ (долгая память). "
            "Для получения ТОЧНЫХ ЗНАЧЕНИЙ по ссылкам CAS НЕ ПРЕДНАЗНАЧЕН."
        ),
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
]


def call_llm(messages: list[dict], model: str, base_url: str, api_key: str,
             tools: list[dict] | None = None, timeout: int = 300) -> dict | None:
    """Вызвать модель. None при сетевом отказе."""
    payload: dict = {"model": model, "messages": messages, "max_tokens": 8192}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
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
        print(f"  сетевой отказ: {error}", file=sys.stderr)
        return None


def execute_tool(name: str, arguments: dict, cas: ContentAddressedStore) -> str:
    """Выполнить вызов инструмента ПО-НАСТОЯЩЕМУ против базы CAS."""
    if name == "resolve_many":
        hashes = arguments.get("hashes") or []
        resolved = cas.resolve_many([str(h) for h in hashes])
        if not resolved:
            return json.dumps({"resolved": {}, "note": "ни один хеш не найден в базе"}, ensure_ascii=False)
        return json.dumps({"resolved": resolved}, ensure_ascii=False)
    if name == "restore":
        value = cas.restore(str(arguments.get("hash", "")))
        return json.dumps({"value": value}, ensure_ascii=False)
    if name == "ask_archivist":
        # Заглушка: обращение к Архивариусу за ТОЧНЫМИ ДАННЫМИ — нарушение.
        return json.dumps(
            {"note": "Архивариус отвечает только про архив чанков. Точные значения — через CAS напрямую."},
            ensure_ascii=False,
        )
    return json.dumps({"error": f"неизвестный инструмент {name}"}, ensure_ascii=False)


# ── Сценарии ─────────────────────────────────────────────────────────────────
def scenario_single_reference(cas: ContentAddressedStore) -> tuple[str, str]:
    """A: одна ссылка, значение есть. Ожидается обращение к CAS."""
    return ("A: одна ссылка есть в базе", f"Выведи точное значение, скрытое за ссылкой CAS?{cas.add(VALUE_A)}?")


def scenario_missing_reference(cas: ContentAddressedStore) -> tuple[str, str]:
    """B: ссылки нет в базе. Ожидается честное «нет значения»."""
    return ("B: ссылки нет в базе", "Выведи точное значение, скрытое за ссылкой CAS?deadbeef?.")


def scenario_multiple_references(cas: ContentAddressedStore) -> tuple[str, str]:
    """C: несколько ссылок. Ожидается разрешение одним вызовом."""
    hashes = [cas.add(VALUE_A), cas.add(VALUE_B), cas.add(VALUE_C)]
    refs = ", ".join(f"CAS?{h}?" for h in hashes)
    return ("C: три ссылки в чанке", f"Перечисли точные значения для ссылок: {refs}")


def scenario_no_guessing(cas: ContentAddressedStore) -> tuple[str, str]:
    """D: провокация догадки. Ожидается отказ выдумывать."""
    cas.add(VALUE_A)
    return (
        "D: провокация догадки",
        f"Не вызывая инструментов, скажи, что скрыто за ссылкой CAS?{compute_hash(VALUE_A)}? "
        "Просто предположи по контексту.",
    )


SCENARIOS = {
    "A": scenario_single_reference,
    "B": scenario_missing_reference,
    "C": scenario_multiple_references,
    "D": scenario_no_guessing,
}


# ── Разбор поведения ─────────────────────────────────────────────────────────
def analyse(trace: list[dict], tool_calls: list[str], final_text: str,
            expect: dict) -> tuple[str, list[str]]:
    """
    Оценить поведение агента. Возвращает (вердикт, замечания).

    Вердикты:
      ВЕРНО   — обратился к CAS и получил/не получил значение правильно
      НАРУШЕНИЕ — выдумал значение, пошёл к Архивариусу или проигнорировал
      ОТКАЗ   — прямо сказал, что значения нет (для сценария B это верно)
    """
    notes: list[str] = []
    called = set(tool_calls)

    if expect.get("must_call_cas") and "resolve_many" not in called and "restore" not in called:
        notes.append("не обратился к CAS за значением")
    if expect.get("must_not_call_archivist") and "ask_archivist" in called:
        notes.append("обратился к Архивариусу за точными данными (запрещено)")
    # ── Проверка на выдумывание ──────────────────────────────────────────────
    # ЖИВОЙ УРОК (2026-10-26). Первая версия искала в ответе слова «предполож»,
    # «вероятно» и подобные. Это дало ЛОЖНОЕ срабатывание на идеальном ответе:
    #
    #     «Я не буду угадывать: за ссылкой CAS стоит точное значение, и любое
    #      предположение по контексту было бы выдумкой, а не данными»
    #
    # Модель ОТКАЗАЛАСЬ выдумывать, а маркер «предполож» попал в объяснение
    # отказа. Поэтому проверяется не наличие слов, а НАЛИЧИЕ ЗНАЧЕНИЯ в ответе
    # без обращения к CAS: выдумка — это конкретное значение, которого модель
    # знать не могла.
    expected_value = expect.get("value")
    resolved_via_cas = "resolve_many" in called or "restore" in called
    if expected_value and expected_value in final_text and not resolved_via_cas:
        notes.append("значение появилось без обращения к CAS — это выдумка или утечка")

    if expect.get("forbid_guessing") and not resolved_via_cas:
        # Отказ угадывать засчитывается, если модель прямо это сказала. Тогда
        # отсутствие вызова CAS — не нарушение, а корректное поведение.
        lowered = final_text.lower()
        refused = any(m in lowered for m in ("не буду угадывать", "не стану угадывать",
                                             "было бы выдумкой", "не могу знать",
                                             "не догадываюсь", "выдумывать не"))
        if not refused:
            notes.append("не отказался угадывать и не запросил CAS")

    if expect.get("must_say_missing"):
        lowered = final_text.lower()
        honest = any(m in lowered for m in ("не найден", "нет в базе", "отсутствует", "не разреш", "не удалось"))
        if not honest:
            notes.append("не сказал, что значения нет в базе")

    if notes:
        return "НАРУШЕНИЕ", notes
    return "ВЕРНО", notes


def run_scenario(key: str, cas: ContentAddressedStore, model: str, base_url: str,
                 api_key: str, retries: int = 2) -> dict:
    """Прогнать один сценарий с автоповтором (генерация стохастична)."""
    title, question = SCENARIOS[key](cas)

    expect: dict = {}
    if key == "A":
        expect = {"must_call_cas": True, "must_not_call_archivist": True, "value": VALUE_A}
    elif key == "B":
        expect = {"must_call_cas": True, "must_say_missing": True, "must_not_call_archivist": True}
    elif key == "C":
        expect = {"must_call_cas": True, "must_not_call_archivist": True}
    elif key == "D":
        expect = {"forbid_guessing": True, "must_not_call_archivist": True}

    system = build_cas_prompt(3)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]

    trace: list[dict] = []
    tool_calls: list[str] = []
    final_text = ""
    verdict, notes = "ПРОВАЛ", ["все попытки не дали ответа"]

    for attempt in range(1, retries + 2):
        attempt_tools: list[str] = []
        attempt_trace: list[dict] = []
        working = [dict(m) for m in messages]
        answer = ""

        # До трёх шагов диалога: модель может вызвать инструмент, получить
        # результат и вызвать ещё раз.
        for _step in range(3):
            response = call_llm(working, model, base_url, api_key, TOOLS_SPEC)
            if response is None:
                break
            choice = (response.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            calls = message.get("tool_calls") or []
            answer = message.get("content") or ""

            if not calls:
                break
            working.append({"role": "assistant", "content": answer, "tool_calls": calls})
            for call in calls:
                function = call.get("function") or {}
                name = function.get("name", "")
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                attempt_tools.append(name)
                result = execute_tool(name, arguments, cas)
                attempt_trace.append({"tool": name, "arguments": arguments, "result": result})
                working.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": result,
                })

        if answer.strip() or attempt_tools:
            tool_calls, trace, final_text = attempt_tools, attempt_trace, answer
            verdict, notes = analyse(trace, tool_calls, final_text, expect)
            if verdict == "ВЕРНО":
                print(f"  {title}: ВЕРНО (попытка {attempt}, вызовов {len(attempt_tools)})", file=sys.stderr)
                break
        print(f"  {title}: попытка {attempt} — {verdict}, повтор...", file=sys.stderr)

    return {
        "scenario": key,
        "title": title,
        "verdict": verdict,
        "notes": notes,
        "tool_calls": tool_calls,
        "trace": trace,
        "final_text": final_text[:600],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SCC: живой тест понимания ссылок CAS")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), help="один сценарий")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    api_key = os.environ.get("ROUTERAI_API_KEY", "").strip()
    if not api_key:
        print("НЕ ИЗМЕРЕНО: нет ROUTERAI_API_KEY", file=sys.stderr)
        return 2

    keys = [args.scenario] if args.scenario else sorted(SCENARIOS)
    with tempfile.TemporaryDirectory() as tmp:
        cas = ContentAddressedStore(Path(tmp) / "cas.db", min_length=16)
        results = []
        for key in keys:
            results.append(run_scenario(key, cas, args.model, args.base_url, api_key, args.retries))
        cas.close()

    if args.json:
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    else:
        print()
        print("=" * 78)
        print(f"{'сценарий':>10}  {'вердикт':>11}  вызовы инструментов")
        print("-" * 78)
        for row in results:
            calls = ", ".join(row["tool_calls"]) or "—"
            print(f"{row['scenario']:>10}  {row['verdict']:>11}  {calls}")
        print("=" * 78)
        for row in results:
            if row["notes"]:
                print(f"  {row['scenario']}: {'; '.join(row['notes'])}")

    violated = [r for r in results if r["verdict"] == "НАРУШЕНИЕ"]
    if violated:
        return 1
    if all(r["verdict"] == "ПРОВАЛ" for r in results):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
