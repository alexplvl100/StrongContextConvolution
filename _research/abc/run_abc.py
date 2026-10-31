#!/usr/bin/env python3
"""
ABC — прогон трёх условий и замер точности.

A — штатная компакция DSH: сводка вместо истории, доступа к оригиналу НЕТ
B — SCC: артефакт свёртки вместо истории, доступа к оригиналу НЕТ
C — штатная компакция + поиск по журналу (аналог session_search)

Метрика: сколько из 32 точных значений названо верно.

Запуск: python3 /tmp/abc_run.py [A|B|C|all]
"""

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

KEY = os.environ.get("ROUTERAI_API_KEY", "").strip()
URL = "https://routerai.ru/api/v1/chat/completions"
MODEL = "deepseek/deepseek-v4.1-flash"

ABC = Path("/tmp/abc")
JOURNAL = (ABC / "journal.txt").read_text(encoding="utf-8")
DATA = json.loads((ABC / "questions.json").read_text(encoding="utf-8"))
FACTS = DATA["facts"]

# ── Промт штатной компакции DSH (дословно из dsh-compaction-basic) ─────────
COMPACTION_INSTRUCTION = """You are now acting as a compaction engine for this AI coding assistant. Condense the conversation ABOVE into a structured checkpoint that preserves everything needed to continue the work.

Output EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write "(none)" for any section with nothing to report.

## Primary Request and Intent
- [the user's original and evolving goals; quote verbatim where the exact wording matters]

## Key Technical Concepts
- [technologies, frameworks, patterns, and conventions in play]

## Files and Code
- [exact path: why it matters, key changes or snippets]

## Errors and Fixes
- [error: how it was resolved, plus any related user feedback]

## Pending Jobs
- [explicitly requested work not yet completed]

## Current Work
- [precisely what was in progress at this checkpoint]

## Next Step
- [the single next action, directly in line with the most recent request, or "(none)"]

## Critical Context
- [decisions and their rationale, constraints, user preferences, open questions, data needed to continue]

Rules:
- Write concise English engineering prose. Preserve exact file paths, commands, error strings, identifiers, numeric values, function signatures.
- Capture user feedback and explicit instructions faithfully, especially corrections.
- Do NOT mention this summarization request or that the context was compacted.
- Output only the checkpoint text: do not call any tool or take any other action."""


def llm(messages, max_tokens=3000, temperature=0.0, retries=3):
    """Вызов модели с повторами."""
    body = {"model": MODEL, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                URL, data=json.dumps(body).encode(),
                headers={"Authorization": f"Bearer {KEY}",
                         "Content-Type": "application/json"})
            r = json.load(urllib.request.urlopen(req, timeout=300))
            return r["choices"][0]["message"]["content"]
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"    повтор {attempt+1}: {e}", file=sys.stderr)
            time.sleep(5)
    return ""


# ── Условие A: штатная компакция ──────────────────────────────────────────
def cond_a() -> str:
    print("  A: штатная компакция DSH...")
    content = llm([{"role": "user",
                    "content": JOURNAL + "\n\n" + COMPACTION_INSTRUCTION}],
                  max_tokens=16000)
    (ABC / "A_summary.md").write_text(content, encoding="utf-8")
    print(f"     сводка: {len(content)} символов")
    return content


# ── Условие B: SCC ────────────────────────────────────────────────────────
def cond_b() -> str:
    print("  B: SCC (три этапа)...")
    import subprocess
    core = Path("/tmp/scc_final/_engine/core/scc_run.py")
    env = {**os.environ, "ROUTERAI_API_KEY": KEY}

    def stage(name, inputs, out, *extra):
        cmd = ["python3", str(core), name, *[str(i) for i in inputs],
               "--out", str(out), "--max-tokens", "48000", *extra]
        r = subprocess.run(cmd, capture_output=True, text=True, env=env,
                           timeout=1800)
        if r.returncode != 0:
            print(f"     {name}: код {r.returncode}")
            print(r.stderr[-600:])
            return None
        return out.read_text(encoding="utf-8") if out.exists() else None

    wr = stage("stage1", [ABC / "journal.txt"], ABC / "B_wr.md")
    if wr is None:
        return ""
    print(f"     stage1 (WR): {len(wr)} символов")

    filled = stage("stage2", [ABC / "journal.txt", ABC / "B_wr.md"],
                   ABC / "B_filled.md")
    if filled is None:
        return ""
    print(f"     stage2 (WR_FILLED): {len(filled)} символов")

    merged = stage("stage3", [ABC / "B_wr.md", ABC / "B_filled.md"],
                   ABC / "B_scc.md")
    if merged is None:
        return ""
    print(f"     stage3 (SCC): {len(merged)} символов")
    return merged


# ── Условие C: компакция + поиск ──────────────────────────────────────────
def search_journal(query: str) -> str:
    """Аналог session_search: найти строки журнала по запросу."""
    terms = [t for t in re.split(r"[\s,]+", query.lower()) if len(t) > 2]
    hits = []
    for line in JOURNAL.splitlines():
        low = line.lower()
        if any(t in low for t in terms):
            hits.append(line)
    if not hits:
        return "Ничего не найдено."
    return "\n".join(hits[:150])


def cond_c(summary: str) -> str:
    """C: компакция + поиск по журналу (аналог session_search).

    Агент получает ТОЛЬКО сводку, но может искать по журналу. Это проверяет
    главную гипотезу: заменяет ли активный поиск пассивное удержание данных.
    """
    print("  C: компакция + поиск по журналу...")
    qs = "\n".join(f"{i+1}. {q}" for i, q in enumerate(DATA["questions"]))
    messages = [
        {"role": "system",
         "content": (
            "Ты агент. Тебе дана СВОДКА предыдущей работы (checkpoint). "
            "Полного журнала в контексте нет, но у тебя есть инструмент поиска "
            "по журналу.\n\n"
            "Чтобы вызвать поиск, напиши строку строго вида:\n"
            "SEARCH: <запрос>\n\n"
            "Результат поиска вернётся следующим сообщением. Можешь делать "
            "столько поисков, сколько нужно. Когда соберёшь все данные — выдай "
            "итоговый ответ по формату «N. значение».")},
        {"role": "user",
         "content": f"СВОДКА:\n{summary}\n\nВОПРОСЫ (на них нужны ТОЧНЫЕ значения):\n{qs}"},
    ]
    searches = 0
    collected = []
    for round_no in range(80):
        reply = llm(messages, max_tokens=16000)
        m = re.search(r"SEARCH:\s*(.+)", reply)
        if not m:
            print(f"     поисков сделано: {searches}")
            (ABC / "C_search_results.txt").write_text(
                "\n".join(collected), encoding="utf-8")
            return reply, "\n".join(collected)
        query = m.group(1).strip()
        result = search_journal(query)
        collected.append(f"--- поиск: {query}\n{result}")
        searches += 1
        if searches <= 4 or searches % 10 == 0:
            print(f"     поиск #{searches}: {query[:50]!r} -> {len(result)} симв")
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": f"РЕЗУЛЬТАТ ПОИСКА:\n{result}"})
        # Ближе к концу — попросить финальный ответ
        if round_no >= 74:
            messages.append({"role": "user",
                             "content": "Времени мало. Выдай ИТОГОВЫЙ ответ СЕЙЧАС, "
                                        "без новых поисков, по формату «N. значение»."})
    print(f"     поисков сделано: {searches} (лимит раундов)")
    (ABC / "C_search_results.txt").write_text("\n".join(collected), encoding="utf-8")
    return reply, "\n".join(collected)


# ── Замер ─────────────────────────────────────────────────────────────────
def score(answer: str) -> dict:
    """Сколько фактов названо верно."""
    found = {"path": 0, "sum": 0, "hash": 0, "seq": 0}
    total = {"path": 0, "sum": 0, "hash": 0, "seq": 0}
    for f in FACTS:
        total[f["kind"]] += 1
        if f["value"] in answer:
            found[f["kind"]] += 1
    all_found = sum(found.values())
    all_total = sum(total.values())
    return {"found": found, "total": total,
            "score": all_found, "max": all_total,
            "pct": round(100 * all_found / all_total, 1)}


def ask(context: str, label: str) -> str:
    """Задать вопросы, имея только контекст."""
    qs = "\n".join(f"{i+1}. {q}" for i, q in enumerate(DATA["questions"]))
    prompt = (
        "Ниже — рабочий контекст. Ответь на вопросы, используя ТОЛЬКО его.\n"
        "Если точного значения в контексте нет — напиши «не знаю», НЕ выдумывай.\n\n"
        f"КОНТЕКСТ:\n{context}\n\nВОПРОСЫ:\n{qs}\n\n"
        "Формат ответа — по строке на вопрос: «N. значение»."
    )
    ans = llm([{"role": "user", "content": prompt}], max_tokens=16000)
    (ABC / f"{label}_answer.md").write_text(ans, encoding="utf-8")
    return ans


def main() -> int:
    if not KEY:
        print("НЕ ИЗМЕРЕНО: нет ROUTERAI_API_KEY")
        return 2
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    results = {}

    if which in ("A", "all"):
        summary = cond_a()
        ans = ask(summary, "A")
        results["A"] = score(ans)
        print(f"     точность A: {results['A']['score']}/{results['A']['max']}")

    if which in ("B", "all"):
        scc = cond_b()
        if scc:
            ans = ask(scc, "B")
            results["B"] = score(ans)
            print(f"     точность B: {results['B']['score']}/{results['B']['max']}")

    if which in ("C", "all"):
        sp = ABC / "A_summary.md"
        if not sp.exists():
            print("  C: нет сводки A — сначала прогони A")
            return 2
        summary = sp.read_text(encoding="utf-8")
        ans, found = cond_c(summary)
        (ABC / "C_answer.md").write_text(ans, encoding="utf-8")
        # Финальный ответ — ОТДЕЛЬНЫМ вызовом: сводка + всё найденное поиском
        final_ctx = summary + "\n\n[НАЙДЕНО ПОИСКОМ]\n" + found
        ans2 = ask(final_ctx, "C_final")
        results["C"] = score(ans2)
        print(f"     точность C: {results['C']['score']}/{results['C']['max']}")

    (ABC / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== ИТОГ ===")
    for k in ("A", "B", "C"):
        if k in results:
            r = results[k]
            print(f"  {k}: {r['score']}/{r['max']} ({r['pct']}%)  "
                  f"пути {r['found']['path']}/{r['total']['path']}, "
                  f"суммы {r['found']['sum']}/{r['total']['sum']}, "
                  f"хеши {r['found']['hash']}/{r['total']['hash']}, "
                  f"seq {r['found']['seq']}/{r['total']['seq']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
