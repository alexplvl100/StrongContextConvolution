#!/usr/bin/env python3
"""
SCC — конвертер журнала DSH в вертикальный поток событий
=========================================================

ЧТО ЭТО
-------
Первый шаг свёртки: превратить сырой журнал сессии DSH в плоский список
событий, пригодный для подачи языковой модели.

Зачем отдельный шаг, а не «скормить журнал как есть»: журнал это JSONL, там
служебные записи (`step/start`, `request/context`, `session/title`), вложенные
блоки контента, reasoning-циклы. Всё это занимает место, но не несёт событий.
Конвертер оставляет только то, что изменяет состояние работы: сообщения
пользователя, ответы агента (с пометкой о рассуждениях) и результаты
инструментов.

ЧТО ОСТАВЛЯЕТСЯ, ЧТО ВЫБРАСЫВАЕТСЯ
----------------------------------
    user/message       -> событие целиком
    assistant/message  -> событие: текст, счётчик reasoning, вызовы инструментов
    tool/result        -> событие целиком (это данные)
    system/message     -> событие E0 (шапка роли)

    reasoning          -> НЕ выбрасывается, а помечается: сохраняется счётчик
                          блоков и объём. Смысл теряется, факт «цикл был» — нет.
    step/turn/header   -> выбрасываются: это разметка, не события

КОДЫ ВОЗВРАТА (соглашение SeverPact)
------------------------------------
    0 — поток собран
    1 — нарушение: сессия не дала ни одного события
    2 — НЕ ИЗМЕРЕНО: журнала нет, он не читается, zstd недоступен

Использование:
    python3 scc_events.py <журнал.jsonl.zstd> --out events.txt
    python3 scc_events.py <журнал.jsonl.zstd> --out events.txt --seq-from 8 --seq-to 381
    python3 scc_events.py <журнал.jsonl.zstd> --stats
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── Типы событий, которые несут состояние работы ─────────────────────────────
# Остальные записи журнала (step/start, step/end, turn/start, turn/end,
# request/header, request/context, session/title, agent/inbox/spliced и прочая
# разметка) — служебные: они описывают механику цикла, а не работу.
STATE_EVENTS = ("user/message", "assistant/message", "tool/result", "system/message")


@dataclass
class Stats:
    """Счётчики разбора: что вошло, что вышло."""

    raw_events: int = 0
    state_events: int = 0
    reasoning_blocks: int = 0
    reasoning_chars: int = 0
    tool_calls: int = 0
    chars_in: int = 0
    chars_out: int = 0
    kinds: dict[str, int] = field(default_factory=dict)


def decompress(path: Path) -> str | None:
    """
    Прочитать журнал: zstd или обычный JSONL.

    Определяем по расширению: `.zstd` распаковываем через бинарь `zstd`.
    Если бинаря нет — это код 2, а не «пусто»: молча вернуть пустоту значило бы
    выдать «событий нет» там, где мы их просто не прочли.
    """
    try:
        if path.suffix == ".zstd":
            if shutil.which("zstd") is None:
                print("НЕ ИЗМЕРЕНО: нет бинаря zstd для распаковки", file=sys.stderr)
                return None
            result = subprocess.run(
                ["zstd", "-dc", str(path)],
                capture_output=True,
                timeout=600,
            )
            if result.returncode != 0:
                print(f"НЕ ИЗМЕРЕНО: zstd вернул {result.returncode}", file=sys.stderr)
                return None
            return result.stdout.decode("utf-8", "replace")
        return path.read_text(encoding="utf-8")
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as error:
        print(f"НЕ ИЗМЕРЕНО: {type(error).__name__}: {error}", file=sys.stderr)
        return None


def text_of(content: object) -> str:
    """Собрать текстовые блоки контента, отбросив прочие типы."""
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in ("text", "reasoning"):
            parts.append(str(block.get("text", "")))
    return "\n".join(parts)


def render_event(event: dict, number: int, stats: Stats) -> str | None:
    """
    Превратить запись журнала в текстовое событие потока.

    Возвращает None для служебных записей — они не события, а разметка.
    """
    kind = event.get("type", "")
    if kind not in STATE_EVENTS:
        return None

    data = event.get("data") or {}
    seq = event.get("seq", "?")
    stats.kinds[kind] = stats.kinds.get(kind, 0) + 1

    if kind == "user/message":
        body = text_of(data.get("content"))
        return f"[E{number}] USER (seq {seq}):\n{body}"

    if kind == "system/message":
        message = data.get("message") or {}
        body = text_of(message.get("content")) or text_of(data.get("content"))
        return f"[E{number}] SYSTEM (seq {seq}):\n{body}"

    if kind == "assistant/message":
        message = data.get("message") or {}
        content = message.get("content") or []
        if not isinstance(content, list):
            content = []
        reasoning = [b for b in content if isinstance(b, dict) and b.get("type") == "reasoning"]
        calls = [b for b in content if isinstance(b, dict) and b.get("type") == "tool-call"]
        texts = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]

        reasoning_chars = sum(len(str(b.get("text", ""))) for b in reasoning)
        stats.reasoning_blocks += len(reasoning)
        stats.reasoning_chars += reasoning_chars
        stats.tool_calls += len(calls)

        lines = [
            f"[E{number}] ASSISTANT (seq {seq}): "
            f"reasoning={len(reasoning)} blocks/{reasoning_chars} chars"
        ]
        for block in texts:
            text = str(block.get("text", "")).strip()
            if text:
                lines.append(f"      text: {text}")
        for call in calls:
            arguments = str(call.get("arguments", ""))
            lines.append(f"      tool_call {call.get('name')} args={arguments}")
        return "\n".join(lines)

    # tool/result — это данные, их сохраняем целиком
    message = data.get("message") or {}
    content = message.get("content")
    body = text_of(content)
    if not body:
        body = json.dumps(content, ensure_ascii=False)
    return f"[E{number}] TOOL_RESULT (seq {seq}), {len(body)} chars:\n{body}"


def convert(
    raw: str,
    seq_from: int | None,
    seq_to: int | None,
    stats: Stats,
) -> str:
    """Собрать поток событий из распакованного журнала."""
    lines: list[str] = []
    number = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        stats.raw_events += 1
        stats.chars_in += len(line)

        seq = event.get("seq")
        if isinstance(seq, int):
            if seq_from is not None and seq < seq_from:
                continue
            if seq_to is not None and seq > seq_to:
                continue

        # Нумеруем только те события, что реально попадут в поток: иначе в
        # диапазоне окажутся дыры в нумерации E, и скрипт чанкования решит,
        # что события потеряны.
        rendered = render_event(event, number + 1, stats)
        if rendered is None:
            continue
        number += 1
        stats.state_events += 1
        lines.append(rendered)

    return "\n".join(lines) + ("\n" if lines else "")


def cmd_convert(args: argparse.Namespace) -> int:
    """Выполнить конвертацию и записать поток."""
    path = Path(args.journal)
    raw = decompress(path)
    if raw is None:
        return 2

    stats = Stats()
    stream = convert(raw, args.seq_from, args.seq_to, stats)
    stats.chars_out = len(stream)

    if stats.state_events == 0:
        print("НАРУШЕНИЕ: в журнале нет событий работы", file=sys.stderr)
        return 1

    if args.out:
        Path(args.out).write_text(stream, encoding="utf-8")

    if args.stats or not args.out:
        print(f"журнал:      {path}")
        print(f"записей:     {stats.raw_events}")
        print(f"событий:     {stats.state_events}")
        print(f"reasoning:   {stats.reasoning_blocks} блоков, {stats.reasoning_chars} символов")
        print(f"tool_call:   {stats.tool_calls}")
        print(f"символов:    вход {stats.chars_in}, поток {stats.chars_out}")
        print(f"состав:      {json.dumps(stats.kinds, ensure_ascii=False)}")
    if args.out:
        print(f"записано:    {args.out} ({stats.chars_out} символов, ~{stats.chars_out // 4} токенов)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Собрать CLI."""
    parser = argparse.ArgumentParser(
        description="SCC: журнал DSH -> вертикальный поток событий",
    )
    parser.add_argument("journal", help="журнал сессии (jsonl или jsonl.zstd)")
    parser.add_argument("--out", help="куда писать поток событий")
    parser.add_argument("--seq-from", type=int, help="начальный seq журнала")
    parser.add_argument("--seq-to", type=int, help="конечный seq журнала")
    parser.add_argument("--stats", action="store_true", help="напечатать статистику")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI."""
    args = build_parser().parse_args(argv)
    return cmd_convert(args)


if __name__ == "__main__":
    sys.exit(main())
