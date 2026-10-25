#!/usr/bin/env python3
"""
SCC — замер расхода токенов по уровням памяти
==============================================

ЧТО ИЗМЕРЯЕТСЯ
---------------
Сценарий из N шагов диалога: короткая память растёт, сжимается в чанки, чанки
копятся в средней памяти, затем выносятся в архив. На каждом шаге считается
объём в контексте — то, что реально видит модель.

Сравниваются два режима:

* **с памятью** — короткая + стак средней + инструкция архива;
* **без памяти** — весь диалог целиком, как он рос бы без механизма.

ЧЕМ СЧИТАЕМ ТОКЕНЫ — ОБЯЗАТЕЛЬНО К УКАЗАНИЮ
---------------------------------------------
Эвристика «4 символа = 1 токен» (`memory/tokens.py`). Точность ограничена:
замерено на живой сессии, что **эвристика занижает на треть** (302 380 против
401 428 по счёту API). Причина — код, пути и JSON: там токен занимает 2–3
символа.

Числа годны для СРАВНЕНИЯ уровней и режимов между собой, но **не для прогноза
стоимости**: реальный счёт будет выше примерно на треть.

ЧТО МОДЕЛИРУЕТСЯ, А ЧТО НЕТ
-----------------------------
Моделируется: рост контекста, срабатывание порогов, переходы уровней, объём
инструкции архива. Сжатие имитируется коэффициентом (замеренный в research/),
а не вызовом ядра: замер должен быть детерминированным и не зависеть от LLM.

Не моделируется: содержание сжатия, поведение глоссария (он уменьшает объём,
и без него оценка консервативна).

Использование:
    python3 tools/measure_memory.py                     # таблица по умолчанию
    python3 tools/measure_memory.py --steps 60 --json
Коды возврата: 0 — измерено, 1 — нарушение, 2 — НЕ ИЗМЕРЕНО.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from memory import tokens  # noqa: E402
from memory.config import MemoryConfig  # noqa: E402
from memory.long import LongMemory  # noqa: E402
from memory.medium import MediumMemory  # noqa: E402
from memory.short import ShortMemory  # noqa: E402
from memory.stack import ChunkStack  # noqa: E402

# ── Параметры сценария ───────────────────────────────────────────────────────
# Средний шаг диалога: команда плюс её вывод. Взят из живой сессии (2 767 КБ на
# 892 события ≈ 3 100 символов на событие, но шаг содержит 2–3 события).
DEFAULT_STEP_CHARS = 2_400

# Коэффициент сжатия ядра.
#
# ВАЖНО, какой именно коэффициент брать. Замерено на живой сессии два разных
# числа: WR (каркас без данных) сжимает ×13,7, а SCC (каркас ПЛЮС точные данные)
# — только ×3,6. Чанк по схеме core/schemas/chunk.schema.json хранит данные,
# поэтому для замера объёма чанка верен именно ×3,6. Взять ×13,7 значило бы
# занизить объём чанков вчетверо и получить неверную картину уровней.
COMPRESS_RATIO = 3.6

# Объём инструкции архива (текст из memory/long.py плюс номер).
INSTRUCTION_CHARS = 320


def make_chunk(n: int, content_chars: int) -> dict:
    """Собрать чанк нужного объёма (содержимое — заполнитель)."""
    content = "x" * content_chars
    return {
        "n": n,
        "parent_chunk": n - 1 if n > 1 else None,
        "source_range": {"start": (n - 1) * 100 + 1, "end": n * 100},
        "tokens_before": content_chars * int(COMPRESS_RATIO),
        "tokens_after": tokens.estimate(content),
        "created_at": "2026-10-25T00:00:00+00:00",
        "content": content,
        "glossary_refs": [],
        "level": "medium",
    }


def run_scenario(steps: int, step_chars: int, config: MemoryConfig) -> list[dict]:
    """
    Прогнать сценарий и вернуть замеры по шагам.

    Механика повторяет реальную: короткая память растёт, при превышении порога
    сжимается в чанк, чанк уходит в среднюю; при превышении порога архивации
    стак вытесняется в архив и заменяется инструкцией.
    """
    with tempfile.TemporaryDirectory() as tmp:
        dialog = Path(tmp) / "measure"
        stack = ChunkStack(dialog, "measure")
        short = ShortMemory(config)
        medium = MediumMemory(stack, config)
        long_memory = LongMemory(stack, config)

        rows: list[dict] = []
        no_memory_tokens = 0
        chunk_number = 0

        for step in range(1, steps + 1):
            # Шаг диалога: растёт и короткая память, и «диалог без памяти»
            short.append("y" * step_chars)
            no_memory_tokens += tokens.estimate("y" * step_chars)

            # Порог сжатия: короткая -> чанк -> средняя
            if short.needs_compress():
                source_tokens = short.token_count()
                chunk_number += 1
                # Сжатие: объём чанка = исходный / коэффициент
                content_chars = max(1, int(len(short.text()) / COMPRESS_RATIO))
                stack.add(make_chunk(chunk_number, content_chars))
                short.take()

            # Порог архивации: стак -> архив
            if medium.needs_archive():
                medium.move_to_long(long_memory)

            # Объём в контексте: инструкция + стак + короткая
            instruction = long_memory.instruction()
            in_context = (
                tokens.estimate(instruction)
                + medium.token_count()
                + short.token_count()
            )
            rows.append({
                "step": step,
                "short_tokens": short.token_count(),
                "medium_tokens": medium.token_count(),
                "long_tokens": long_memory.token_count(),
                "medium_chunks": len(medium),
                "long_chunks": len(long_memory),
                "instruction_tokens": tokens.estimate(instruction),
                "in_context": in_context,
                "no_memory": no_memory_tokens,
            })
        return rows


def main(argv: list[str] | None = None) -> int:
    """Прогнать замер и напечатать таблицу."""
    parser = argparse.ArgumentParser(description="SCC: замер токенов по уровням памяти")
    parser.add_argument("--steps", type=int, default=50, help="шагов диалога")
    parser.add_argument("--step-chars", type=int, default=DEFAULT_STEP_CHARS, help="символов на шаг")
    # Дефолты берём ИЗ КОНФИГА, а не дублируем числами. Живой урок: здесь
    # стояли старые значения (архив 60 000), порог из config.py не применялся,
    # и архив не срабатывал ни разу — два из трёх уровней не проверялись.
    from memory.config import (  # импорт здесь: модуль уже в sys.path
        DEFAULT_THRESHOLD_ARCHIVIST,
        DEFAULT_THRESHOLD_COMPRESS,
    )

    parser.add_argument("--compress-threshold", type=int, default=DEFAULT_THRESHOLD_COMPRESS)
    parser.add_argument("--archivist-threshold", type=int, default=DEFAULT_THRESHOLD_ARCHIVIST)
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    args = parser.parse_args(argv)

    if args.steps <= 0 or args.step_chars <= 0:
        print("НАРУШЕНИЕ: steps и step-chars обязаны быть положительными", file=sys.stderr)
        return 1

    try:
        config = MemoryConfig(
            threshold_compress=args.compress_threshold,
            threshold_archivist=args.archivist_threshold,
        )
    except ValueError as error:
        print(f"НАРУШЕНИЕ: {error}", file=sys.stderr)
        return 1

    rows = run_scenario(args.steps, args.step_chars, config)
    if not rows:
        print("НЕ ИЗМЕРЕНО: сценарий не дал замеров", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "steps": args.steps,
            "step_chars": args.step_chars,
            "config": {
                "threshold_compress": config.threshold_compress,
                "threshold_archivist": config.threshold_archivist,
            },
            "compress_ratio": COMPRESS_RATIO,
            "tokenizer": "эвристика 4 символа = 1 токен (занижает ~на треть)",
            "rows": rows,
        }, ensure_ascii=False, indent=2))
        return 0

    # ── Таблица ──────────────────────────────────────────────────────────────
    print(f"сценарий: {args.steps} шагов по {args.step_chars} символов")
    print(f"пороги:   сжатие {config.threshold_compress}, архив {config.threshold_archivist} токенов")
    print(f"токены:   эвристика 4 символа = 1 токен (занижает примерно на треть)")
    print()
    header = f"{'шаг':>4} {'короткая':>9} {'средняя':>9} {'долгая':>8} {'инстр':>6} {'В КОНТЕКСТЕ':>12} {'без памяти':>11}"
    print(header)
    print("-" * len(header))

    # Печатаем не каждый шаг, а характерные точки: начало, срабатывания порогов,
    # конец. Полная портянка нечитаема и не добавляет понимания.
    show = {1, len(rows)}
    for index in range(1, len(rows)):
        previous, current = rows[index - 1], rows[index]
        if current["medium_chunks"] != previous["medium_chunks"]:
            show.add(current["step"])
        if current["long_chunks"] != previous["long_chunks"]:
            show.add(current["step"])
    for step in sorted(show):
        row = rows[step - 1]
        print(
            f"{row['step']:>4} {row['short_tokens']:>9} {row['medium_tokens']:>9} "
            f"{row['long_tokens']:>8} {row['instruction_tokens']:>6} "
            f"{row['in_context']:>12} {row['no_memory']:>11}"
        )

    last = rows[-1]
    print()
    print("=" * 72)
    print(f"итого в контексте:  {last['in_context']:>9} токенов")
    print(f"без памяти:         {last['no_memory']:>9} токенов")
    if last["in_context"]:
        saved = last["no_memory"] / last["in_context"]
        print(f"экономия:           ×{saved:.2f}")
    print(f"чанков: средняя {last['medium_chunks']}, архив {last['long_chunks']}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
