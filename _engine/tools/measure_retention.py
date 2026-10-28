#!/usr/bin/env python3
"""
SCC — замер богатства остатка после компакции
===============================================

ЗАДАЧА
-------
Сравнить две доли удерживаемого дословно контекста (`retainRatio` 16 % и 50 %)
и ответить, **как выглядит прирост точности**:

* если функция **вогнутая** — прирост быстро насыщается, и 50 % избыточны;
* если **выпуклая** — прирост ускоряется, и 50 % может не хватить.

ЧТО ИЗМЕРЯЕТСЯ
----------------
Для каждой компакции:

1. **Дословный остаток** — события, НЕ попавшие в затенённый диапазон. Это то,
   что модель видит неприкосновенным.
2. **Пересказ** — текст сводки, заменяющий затенённый диапазон.
3. **Богатство остатка** — сколько точных значений (путей, чисел, хешей)
   сохранилось дословно относительно того, что было в затенённой части.

Почему это важно: пересказ ТЕРЯЕТ точные значения (замерено в R-SCC-09: 8 %
путей после первой компакции). Дословный остаток не теряет ничего. Значит
«богатство» = доля точных значений, дошедших до модели без искажения.

МЕТРИКИ
--------
    total_facts      — точных значений во всей сессии до компакции
    kept_verbatim    — сколько из них в дословном остатке
    summarized       — сколько попало в затенённый диапазон
    survived_summary — сколько из затенённых выжило в пересказе
    richness         — (kept_verbatim + survived_summary) / total_facts

Запуск:
    python3 _engine/tools/measure_retention.py <журнал> [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Точные значения: пути, числа от 3 цифр, хеши от 8 hex.
PATH_RE = re.compile(r"/[\w./-]{6,}")
NUM_RE = re.compile(r"\b\d{3,}\b")
HEX_RE = re.compile(r"\b[0-9a-f]{8,64}\b")


def read_events(journal: Path) -> list[dict]:
    """Распаковать журнал zstd и разобрать события."""
    raw = subprocess.run(
        ["zstd", "-dc", str(journal)], capture_output=True
    ).stdout.decode("utf-8", "replace")
    events = []
    for line in raw.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def text_of(event: dict) -> str:
    """Текст события: у surface-типов он в message.content или content."""
    data = event.get("data") or {}
    message = data.get("message") or {}
    content = message.get("content")
    if content is None:
        content = data.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "") for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def facts(text: str) -> set[str]:
    """Точные значения в тексте: пути, числа, хеши."""
    return set(PATH_RE.findall(text)) | set(NUM_RE.findall(text)) | set(HEX_RE.findall(text))


SURFACE = {"system/message", "developer/message", "user/message",
           "assistant/message", "tool/result"}


def measure(journal: Path) -> dict:
    """Замерить богатство остатка по журналу одной сессии."""
    events = read_events(journal)
    by_seq = {e.get("seq"): e for e in events if isinstance(e.get("seq"), int)}

    # Все затенённые seq по всем компакциям
    shadowed: set[int] = set()
    summaries: list[dict] = []
    for event in events:
        if event.get("type") != "compaction/summary":
            continue
        data = event.get("data") or {}
        seqs = set(data.get("shadowedSeqs") or [])
        shadowed |= seqs
        summary_text = "".join(
            b.get("text", "") for b in (data.get("summary") or [])
            if isinstance(b, dict)
        )
        summaries.append({
            "seq": event.get("seq"),
            "range": data.get("shadowedRange"),
            "shadowed_count": len(seqs),
            "shadowed_tokens": data.get("shadowedTokenCount"),
            "summary_chars": len(summary_text),
            "summary_text": summary_text,
        })

    # Точные значения: вся сессия, дословный остаток, затенённое, пересказ
    all_facts: set[str] = set()
    verbatim_facts: set[str] = set()
    shadowed_facts: set[str] = set()
    for event in events:
        if event.get("type") not in SURFACE:
            continue
        text = text_of(event)
        if not text:
            continue
        found = facts(text)
        all_facts |= found
        if event.get("seq") in shadowed:
            shadowed_facts |= found
        else:
            verbatim_facts |= found

    summary_facts: set[str] = set()
    for item in summaries:
        summary_facts |= facts(item["summary_text"])

    survived = shadowed_facts & summary_facts
    lost = shadowed_facts - summary_facts

    return {
        "journal": str(journal),
        "events": len(events),
        "compactions": len(summaries),
        "facts_total": len(all_facts),
        "facts_verbatim": len(verbatim_facts),
        "facts_shadowed": len(shadowed_facts),
        "facts_in_summary": len(summary_facts),
        "facts_survived": len(survived),
        "facts_lost": len(lost),
        "richness": round((len(verbatim_facts) + len(survived)) / max(1, len(all_facts)), 4),
        "survival_rate": round(len(survived) / max(1, len(shadowed_facts)), 4),
        "summaries": summaries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SCC: богатство остатка после компакции")
    parser.add_argument("journal", help="путь к session.v4.jsonl.zstd")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    journal = Path(args.journal)
    if not journal.exists():
        print(f"НЕ ИЗМЕРЕНО: нет журнала {journal}", file=sys.stderr)
        return 2

    result = measure(journal)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"── {journal.name} ──")
    print(f"    событий:    {result['events']:,}")
    print(f"    компакций:  {result['compactions']}")
    print()
    print(f"    точных значений всего:   {result['facts_total']:,}")
    print(f"    в дословном остатке:     {result['facts_verbatim']:,}")
    print(f"    в затенённом диапазоне:  {result['facts_shadowed']:,}")
    print(f"    выжило в пересказе:      {result['facts_survived']:,}")
    print(f"    ПОТЕРЯНО:                {result['facts_lost']:,}")
    print()
    print(f"    богатство остатка:       {result['richness']:.2%}")
    print(f"    выживаемость пересказа:  {result['survival_rate']:.2%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
