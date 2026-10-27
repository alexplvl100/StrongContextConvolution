#!/usr/bin/env python3
"""
SCC — симуляция устойчивости памяти
=====================================

ПРОВЕРЯЕМОЕ ОПАСЕНИЕ
----------------------
При фиксированном условии сжатия (порог как доля окна) возможны два плохих
исхода:

* **короткая память вырождается** — чанки занимают всю рабочую зону;
* **размер чанков вырождается** — последний чанк стремится к нулю.

Нужны правила, при которых одновременно:

1. у короткой памяти остаётся **ожидаемый контейнер**;
2. размеры чанков **близки**;
3. количество чанков в средней памяти **постоянно**.

ТРИ РЕЖИМА
-----------
**A. Порог на общий контекст, стак НЕ ограничен.**
Условие: `HEAD + весь_стак + короткая > Z`. Всё сжатое остаётся в средней памяти.

**B. Порог на общий контекст, стак ограничен K чанками** (архивация).
Условие то же, но старые чанки уходят в архив.

**C. Порог на короткую память, стак ограничен K** (предлагаемое правило).
Условие: `короткая > T`, где `T` **выводится** из рабочей зоны:

    T = (Z − HEAD) · r / (r + K)

МАТЕМАТИКА: ЧТО ПРОИСХОДИТ
----------------------------
Обозначим `D` — свободное место в зоне: `D = Z − HEAD − стак`.

**Режим A.** Сжатие берёт `c = D/r`, после чего `D` уменьшается:
`D_new = D − D/r = D·(1−1/r)`. То есть `D` убывает **геометрически**, а чанки
образуют геометрический ряд:

    c_n = D_0·(1−1/r)^(n−1)/r

Сумма сходится к `D_0` — весь запас зоны уходит в стак, **короткая → 0**.
Это и есть «задушит короткую память».

**Режим B.** Стак ограничен `K` чанками, поэтому `D` не может упасть ниже
`Z − HEAD − K·c`. Равновесие:

    c* = (Z − HEAD)/(r + K)

Ряд **сходится к положительному c***, короткая стабилизируется на
`Z − HEAD − K·c*`. Оба вырождения отсутствуют.

**Режим C.** Порог `T` выбран так, что равновесие достигается **сразу**:
каждый чанк равен `T/r`, стак держит ровно `K`, короткая имеет контейнер `T`.
Переходного процесса с крупными первыми чанками нет.

Запуск:
    python3 _engine/tools/simulate_memory.py
    python3 _engine/tools/simulate_memory.py --window 1000000 --steps 4000
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def derive_threshold(
    zone: int,
    head: int,
    chunk_count: int,
    ratio: float,
    step_tokens: int = 0,
) -> int:
    """
    Вывести порог сжатия из рабочей зоны (режим C).

    Условие устойчивости: в момент сжатия контекст обязан помещаться в зону.
    Короткая память растёт **шагами**, поэтому порог она пересекает не ровно,
    а на значении от `T` до `T + step`. Значит проверять надо по худшему случаю:

        HEAD + K·((T + step)/r) + (T + step) <= Z

    Обозначив `S = T + step`:

        S ≤ (Z − HEAD)·r / (r + K)
        T = S − step

    **Вычитание шага обязательно.** Без него контекст выходит за рабочую зону на
    `step·(1 + K/r)`: при шаге 5 000 и K=2 это 7 778 токенов, а при шаге 20 000 —
    31 111, то есть выход за зону на 3.5 %.

    Живой урок: первая версия вычитания не делала, и контекст в симуляции
    показывал 100.7 % зоны вместо 99.9 %.
    """
    # +1 слот: вынос по одному держит максимум на единицу больше равновесия.
    # Стак колеблется N -> N+1 -> N, и в зону надо укладывать максимум N+1.
    limit = (zone - head) * ratio / (ratio + chunk_count + 1)
    return int(limit - step_tokens)


def simulate(
    regime: str,
    window: int,
    head: int,
    step_tokens: int,
    steps: int,
    ratio: float,
    chunk_count: int,
    zone_ratio: float,
) -> dict:
    """
    Прогнать сценарий и вернуть замеры.

    Режимы: `A` (общий порог, без архивации), `B` (общий порог, K чанков),
    `C` (порог из зоны, K чанков).
    """
    zone = int(window * zone_ratio)
    stack: list[float] = []          # чанки в средней памяти
    created: list[float] = []        # ВСЕ созданные чанки, для анализа ряда
    short = 0.0
    # Контейнер короткой памяти: сколько она успевает накопить ПЕРЕД сжатием.
    # Именно эта величина показывает, есть ли у короткой памяти место.
    # Живой урок: сначала измерялся минимум короткой памяти, но он равен нулю
    # сразу после сжатия — это НОРМА, а не вырождение.
    short_max = 0.0
    peak = 0.0

    threshold = (
        zone if regime in ("A", "B")
        else derive_threshold(zone, head, chunk_count, ratio, step_tokens)
    )
    # в режиме A стак не ограничен; в B и C — K чанков
    limit = None if regime == "A" else chunk_count

    for _ in range(steps):
        short += step_tokens

        if regime in ("A", "B"):
            trigger = head + sum(stack) + short > zone
        else:
            trigger = short > threshold

        # ЗАМЕР КОНТЕЙНЕРА — ДО сжатия: именно это значение короткая память
        # успевает накопить. Живой урок: замер после сжатия давал 0, и метрика
        # показывала «контейнер меньше порога» на устойчивом режиме.
        short_max = max(short_max, short)
        peak = max(peak, head + sum(stack) + short)

        if trigger:
            chunk = short / ratio
            created.append(chunk)
            stack.append(chunk)
            short = 0.0
            if limit is not None:
                # ЧАСТИЧНЫЙ вынос: старейший уходит, актуальные остаются.
                # Вытесняем по одному за шаг — как делает evict_to_limit.
                while len(stack) > limit:
                    stack.pop(0)

    # ── Показатели устойчивости ──────────────────────────────────────────────
    # «Первая половина» против «второй»: так видно, сходится ряд или расходится.
    half = max(1, len(created) // 2)
    first_half = created[:half] or [0.0]
    second_half = created[half:] or [0.0]

    return {
        "regime": regime,
        "threshold": int(threshold),
        "created": len(created),
        "chunk_first": round(created[0]) if created else 0,
        "chunk_last": round(created[-1]) if created else 0,
        "avg_first_half": round(sum(first_half) / len(first_half)),
        "avg_second_half": round(sum(second_half) / len(second_half)),
        "stack_now": round(sum(stack)),
        "stack_size": len(stack),
        "short_max": round(short_max),
        "short_container": int(threshold) if regime == "C" else None,
        "context_peak": round(peak),
        "zone": zone,
        "zone_used": round(100 * peak / zone, 1) if zone else 0,
    }


def stability_verdict(row: dict, zone: int) -> tuple[bool, str]:
    """
    Оценить устойчивость режима.

    Три условия заказчика:

    1. **контейнер короткой памяти** не вырождается — `short_max` заметно
       больше нуля (у короткой памяти есть место между сжатиями);
    2. **размеры чанков близки** — вторая половина не меньше первой в разы;
    3. **количество чанков постоянно** — стак не растёт без предела.

    Живой урок: сначала вырождением считался `short_min == 0`, но короткая
    память **обнуляется при сжатии по устройству** — это норма. Настоящий
    признак вырождения в режиме A другой: контекст выходит за рабочую зону,
    потому что стак растёт неограниченно.
    """
    if row["created"] == 0:
        return False, "чанков не создано — сценарий не дошёл до сжатия"

    first, second = row["avg_first_half"], row["avg_second_half"]
    drift = second / max(first, 1)

    # Переполнение зоны. Различаем два случая:
    #  - стак растёт без предела (режим A) — вырождение;
    #  - переполнение на один шаг (режим B) — условие не учитывает дискретность.
    if row["zone_used"] > 100:
        overflow = row["context_peak"] - row["zone"]
        if row["stack_size"] > 10:
            return False, (f"контекст выходит за зону: {row['zone_used']}% — "
                           f"стак растёт без предела ({row['stack_size']} чанков)")
        if overflow > 0:
            return False, (f"переполнение зоны на {overflow} (один шаг): "
                           f"условие не учитывает дискретность прироста")

    # контейнер короткой памяти: сравним с размером чанка
    if row["short_max"] < row["chunk_last"] * 0.5:
        return False, (f"короткая память вырождается: контейнер {row['short_max']} "
                       f"против чанка {row['chunk_last']}")

    if drift < 0.7:
        return False, f"размер чанков падает: {first} -> {second} (x{drift:.2f})"

    if row["stack_size"] > 10:
        return False, f"стак растёт: {row['stack_size']} чанков в средней памяти"

    return True, (f"устойчив: чанки {first} -> {second} (x{drift:.2f}), "
                  f"контейнер короткой {row['short_max']}, "
                  f"стак {row['stack_size']}, зона {row['zone_used']}%")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SCC: устойчивость порогов памяти")
    parser.add_argument("--window", type=int, default=1_000_000)
    parser.add_argument("--head", type=int, default=2_000)
    parser.add_argument("--step-tokens", type=int, default=5_000)
    parser.add_argument("--steps", type=int, default=4_000)
    parser.add_argument("--ratio", type=float, default=3.6)
    parser.add_argument("--chunks", type=int, default=2)
    parser.add_argument("--zone", type=float, default=0.9)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.chunks < 1:
        print("НАРУШЕНИЕ: чанков в средней памяти должно быть >= 1", file=sys.stderr)
        return 1
    if not 0 < args.zone <= 1:
        print("НАРУШЕНИЕ: рабочая зона — доля окна в пределах (0, 1]", file=sys.stderr)
        return 1

    common = (args.window, args.head, args.step_tokens, args.steps,
              args.ratio, args.chunks, args.zone)
    rows = [simulate(regime, *common) for regime in ("A", "B", "C")]

    if args.json:
        print(json.dumps({"rows": rows, "params": vars(args)}, ensure_ascii=False, indent=2))
        return 0

    zone = int(args.window * args.zone)
    print(f"окно {args.window}, рабочая зона {args.zone:.0%} = {zone}, HEAD {args.head}")
    print(f"сжатие x{args.ratio}, чанков в средней {args.chunks}, "
          f"шагов {args.steps} по {args.step_tokens}")
    print()
    header = (f"{'режим':>6} {'порог':>9} {'создано':>8} {'1-й чанк':>10} "
              f"{'последний':>11} {'1-я пол':>9} {'2-я пол':>9} {'контейнер':>10} "
              f"{'контекст':>10} {'зона':>7}")
    print(header)
    print("-" * len(header))
    for row in rows:
        print(f"{row['regime']:>6} {row['threshold']:>9} {row['created']:>8} "
              f"{row['chunk_first']:>10} {row['chunk_last']:>11} "
              f"{row['avg_first_half']:>9} {row['avg_second_half']:>9} "
              f"{row['short_max']:>10} {row['context_peak']:>10} {row['zone_used']:>6}%")
    print()

    labels = {
        "A": "A. общий порог, стак БЕЗ ограничения",
        "B": "B. общий порог, стак K чанков",
        "C": "C. порог из зоны, стак K чанков",
    }
    unstable = 0
    for row in rows:
        ok, verdict = stability_verdict(row, zone)
        mark = "УСТОЙЧИВ " if ok else "ВЫРОЖДЕНИЕ"
        if not ok:
            unstable += 1
        print(f"  {mark} {labels[row['regime']]}: {verdict}")

    print()
    threshold_c = derive_threshold(zone, args.head, args.chunks, args.ratio,
                                   args.step_tokens)
    # +1 слот: вынос по одному даёт максимум на единицу больше равновесия.
    limit = (zone - args.head) * args.ratio / (args.ratio + args.chunks + 1)
    print(f"  формула режима C: T = (Z - HEAD) * r / (r + K + 1) - step")
    print(f"    предел S = {limit:.0f}, минус шаг {args.step_tokens} -> T = {threshold_c}")
    print(f"    (+1 слот: вынос по одному держит максимум K+1 чанков)")
    return 0 if unstable < 3 else 1


if __name__ == "__main__":
    sys.exit(main())
