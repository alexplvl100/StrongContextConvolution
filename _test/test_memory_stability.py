#!/usr/bin/env python3
"""
SCC — тесты устойчивости порогов памяти
=========================================

ЧТО ПРОВЕРЯЕТСЯ
---------------
Опасение заказчика: при **фиксированном** условии сжатия (порог как доля окна)
возможны два вырождения:

* **короткая память душится** — стак занимает всю рабочую зону;
* **размер чанков вырождается** — последний чанк стремится к нулю.

Тесты фиксируют **вывод симуляции**:

| Режим | Условие | Результат |
|---|---|---|
| **A** | общий порог, стак **без** ограничения | **вырождается**: зона 618 %, стак растёт без предела |
| **B** | общий порог, стак K чанков | **переполняет зону** на один шаг |
| **C** | порог из зоны (−шаг), стак K чанков | **устойчив**: чанки одинаковы, зона не превышена |

**Итог: устойчив только C.** A душит короткую память (стак занимает всю зону),
B переполняет зону на шаг, потому что его условие не учитывает дискретность
прироста короткой памяти.

**Почему A вырождается.** Сжатие берёт `c = D/r`, после чего свободное место
`D` уменьшается: `D_new = D·(1−1/r)`. Место убывает **геометрически**, чанки
образуют геометрический ряд, сумма сходится к `D₀` — **весь запас зоны уходит в
стак**. Это и есть «задушит короткую память».

**Почему B и C устойчивы.** Стак ограничен `K` чанками, поэтому `D` не может
упасть ниже `Z − HEAD − K·c`. Равновесие `c* = (Z − HEAD)/(r + K)`
**положительно**, оба вырождения отсутствуют.

**Почему C лучше B.** В B есть переходный процесс (первый чанк крупнее), в C
порог выведен так, что равновесие достигается **сразу**: все чанки одинаковы.

**Почему `K + 1` в формуле.** Вынос **частичный**: `move_to_long` выносит
старейший чанк, когда стак превысил порог. Значит стак колеблется
`N → N+1 → N`, и в зону надо укладывать **максимум** `N+1`.

ЖИВОЙ УРОК ПРИ НАПИСАНИИ
--------------------------
Первая метрика считала **минимум короткой памяти** и объявляла вырождением
`short_min == 0`. Но короткая память **обнуляется при сжатии по устройству** —
это норма, а не вырождение. Настоящий признак в режиме A другой: контекст
выходит за рабочую зону, потому что стак растёт без предела.

Запуск:
    python3 _test/test_memory_stability.py -v
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TOOLS = ROOT / "_engine" / "tools"


def load_simulator():
    """Загрузить симулятор как модуль."""
    spec = importlib.util.spec_from_file_location("simulate_memory", TOOLS / "simulate_memory.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["simulate_memory"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


SIM = load_simulator()

# ── Параметры тестовых сценариев ─────────────────────────────────────────────
WINDOW = 1_000_000
HEAD = 2_000
STEP = 5_000
STEPS = 4_000
RATIO = 3.6
CHUNKS = 2
ZONE = 0.9


def run(regime: str, chunks: int = CHUNKS, ratio: float = RATIO,
        zone: float = ZONE, steps: int = STEPS) -> dict:
    """Прогнать режим с параметрами теста."""
    return SIM.simulate(regime, WINDOW, HEAD, STEP, steps, ratio, chunks, zone)


class TestRegimeADegenerates(unittest.TestCase):
    """Режим A: стак без ограничения — короткая память душится."""

    def test_context_exceeds_zone(self) -> None:
        """
        Контекст выходит за рабочую зону в разы.

        Это и есть вырождение: стак растёт, пока не займёт всю зону, а затем
        и за неё. Короткая память получает ноль места.
        """
        row = run("A")
        self.assertGreater(row["zone_used"], 100,
                           f"контекст не вышел за зону: {row['zone_used']}%")
        self.assertGreater(row["zone_used"], 300,
                           "вырождение выражено слабее ожидаемого")

    def test_chunks_form_decreasing_row(self) -> None:
        """
        Чанки убывают: вторая половина меньше первой.

        Геометрический ряд: `c_n = D₀·(1−1/r)^(n−1)/r`. Сумма сходится,
        отдельные чанки стремятся к нулю.
        """
        row = run("A")
        self.assertLess(row["avg_second_half"], row["avg_first_half"],
                        "чанки не убывают — ряд не сходится")
        self.assertLess(row["chunk_last"], row["chunk_first"] / 10,
                        "последний чанк не выродился относительно первого")

    def test_unlimited_stack_grows(self) -> None:
        """Стак растёт без предела — в этом причина вырождения."""
        row = run("A")
        self.assertGreater(row["stack_size"], 100,
                           f"стак не вырос: {row['stack_size']} чанков")


class TestRegimeBStable(unittest.TestCase):
    """Режим B: общий порог, но стак ограничен K чанками."""

    def test_overflows_zone_by_one_step(self) -> None:
        """
        Режим B переполняет зону на ОДИН ШАГ.

        Условие `контекст > Z` проверяется после прироста короткой памяти,
        поэтому она успевает перейти порог. Превышение равно шагу:
        при шаге 5 000 и зоне 900 000 это 0.56 %.

        Это аргумент против режима B: он **всегда** выходит за зону, тогда как
        режим C вычитает шаг и не выходит.
        """
        row = run("B")
        overflow = row["context_peak"] - row["zone"]
        self.assertGreater(overflow, 0, "режим B не переполнил зону")
        self.assertLessEqual(overflow, STEP + 1,
                             f"переполнение {overflow} больше шага {STEP}")

    def test_stack_size_constant(self) -> None:
        """Количество чанков в средней памяти постоянно и равно K."""
        row = run("B")
        self.assertEqual(row["stack_size"], CHUNKS,
                         f"стак {row['stack_size']} вместо {CHUNKS}")

    def test_short_memory_has_container(self) -> None:
        """У короткой памяти остаётся контейнер — она не вырождена."""
        row = run("B")
        self.assertGreater(row["short_max"], 0, "контейнер короткой памяти пуст")
        self.assertGreater(row["short_max"], row["chunk_last"],
                           "контейнер короткой памяти меньше чанка")

    def test_chunk_sizes_close(self) -> None:
        """Размеры чанков близки: вторая половина не меньше первой в разы."""
        row = run("B")
        drift = row["avg_second_half"] / max(row["avg_first_half"], 1)
        self.assertGreater(drift, 0.9, f"размер чанков падает: x{drift:.2f}")


class TestRegimeCStableAndEven(unittest.TestCase):
    """Режим C: порог выведен из зоны — равновесие сразу."""

    def test_threshold_formula(self) -> None:
        """
        Порог: `T = (Z − HEAD)·r/(r + K + 1) − step`.

        **Вычитание шага обязательно.** Короткая память растёт шагами, поэтому
        порог пересекается на значении от `T` до `T + step`. Без вычитания
        контекст выходит за рабочую зону на `step·(1 + K/r)`.
        """
        zone = int(WINDOW * ZONE)
        threshold = SIM.derive_threshold(zone, HEAD, CHUNKS, RATIO, STEP)
        # +1 слот: частичный вынос держит максимум K+1 чанков
        expected = int((zone - HEAD) * RATIO / (RATIO + CHUNKS + 1) - STEP)
        self.assertEqual(threshold, expected)
        self.assertEqual(threshold, 484_818, f"порог {threshold} вместо 484818")

    def test_threshold_without_step_overflows(self) -> None:
        """
        Без вычитания шага контекст выходит за зону.

        Проверяется сравнением: порог без вычитания даёт пик больше зоны.
        """
        zone = int(WINDOW * ZONE)
        without = SIM.derive_threshold(zone, HEAD, CHUNKS, RATIO, 0)
        with_step = SIM.derive_threshold(zone, HEAD, CHUNKS, RATIO, STEP)
        self.assertGreater(without, with_step, "порог без шага не больше")

        # пик в худшем случае: HEAD + K*((T+step)/r) + (T+step)
        def peak(threshold: int) -> float:
            # максимум стака — (K+1) чанков: вынос по одному
            s = threshold + STEP
            return HEAD + (CHUNKS + 1) * (s / RATIO) + s

        self.assertGreater(peak(without), zone,
                           "порог без вычитания шага не переполняет зону")
        self.assertLessEqual(peak(with_step), zone,
                             "порог с вычитанием шага переполняет зону")

    def test_all_chunks_equal(self) -> None:
        """
        Все чанки одинакового размера — переходного процесса нет.

        Это главное преимущество C перед B: в B первый чанк крупнее, и ряд
        сходится к равновесию постепенно.
        """
        row = run("C")
        self.assertEqual(row["chunk_first"], row["chunk_last"],
                         f"чанки различаются: {row['chunk_first']} и {row['chunk_last']}")
        self.assertEqual(row["avg_first_half"], row["avg_second_half"],
                         "половины различаются — ряд не установился")

    def test_context_fits_zone_exactly(self) -> None:
        """
        Контекст занимает зону почти целиком, но НЕ выходит за неё.

        После учёта шага пик равен 99.0 % — зона используется рационально и не
        переполняется.
        """
        row = run("C")
        self.assertLessEqual(row["zone_used"], 100, f"выход за зону: {row['zone_used']}%")
        self.assertGreater(row["zone_used"], 70, "зона используется нерационально")

    def test_container_and_chunk_arithmetic(self) -> None:
        """
        Чанк равен `(T + δ)/r`, где `δ` — перелёт от дискретного шага.

        Контейнер короткой памяти больше порога ровно на этот перелёт:
        короткая память растёт шагами и пересекает порог не точно.
        """
        row = run("C")
        threshold = row["threshold"]
        # перелёт: короткая пересекла порог на T + δ, где δ < STEP
        overshoot = row["short_max"] - threshold
        self.assertGreaterEqual(overshoot, 0, "контейнер меньше порога")
        self.assertLessEqual(overshoot, STEP, f"перелёт {overshoot} больше шага {STEP}")
        # чанк равен (T + δ)/r
        self.assertEqual(row["chunk_last"], round(row["short_max"] / RATIO),
                         "размер чанка не равен (T+δ)/r")


class TestStabilityAcrossParams(unittest.TestCase):
    """Устойчивость проверяется на РАЗБРОСЕ параметров, а не на одном наборе."""

    def test_regime_a_always_degenerates(self) -> None:
        """Режим A вырождается при любых K и r."""
        for chunks in (1, 2, 3, 5):
            for ratio in (2.0, 3.6, 10.0):
                with self.subTest(chunks=chunks, ratio=ratio):
                    row = run("A", chunks=chunks, ratio=ratio)
                    self.assertGreater(
                        row["zone_used"], 100,
                        f"A не выродился при K={chunks}, r={ratio}: {row['zone_used']}%",
                    )

    def test_both_regimes_keep_constant_stack(self) -> None:
        """B и C держат постоянное число чанков при любых K и r."""
        for regime in ("B", "C"):
            for chunks in (1, 2, 3, 5):
                for ratio in (2.0, 3.6, 10.0):
                    with self.subTest(regime=regime, chunks=chunks, ratio=ratio):
                        row = run(regime, chunks=chunks, ratio=ratio)
                        self.assertEqual(row["stack_size"], chunks,
                                         f"{regime}: стак {row['stack_size']} вместо {chunks}")

    def test_only_regime_c_never_overflows(self) -> None:
        """
        Только режим C НЕ переполняет зону — он вычитает шаг.

        Режим B переполняет на шаг при любых параметрах: его условие не знает
        о дискретности прироста короткой памяти.
        """
        for chunks in (1, 2, 3, 5):
            for ratio in (2.0, 3.6, 10.0):
                with self.subTest(chunks=chunks, ratio=ratio):
                    c_row = run("C", chunks=chunks, ratio=ratio)
                    self.assertLessEqual(
                        c_row["context_peak"], c_row["zone"],
                        f"C переполнил зону: {c_row['zone_used']}%",
                    )
                    b_row = run("B", chunks=chunks, ratio=ratio)
                    self.assertGreater(
                        b_row["context_peak"], b_row["zone"],
                        "B не переполнил зону — ожидалось переполнение на шаг",
                    )

    def test_larger_k_gives_smaller_container(self) -> None:
        """
        Чем больше чанков держим, тем меньше контейнер короткой памяти.

        Это следствие формулы: `T = (Z − HEAD)·r/(r + K)` убывает по K. Значит
        K — **регулятор** распределения места между стаком и короткой памятью.
        """
        containers = [run("C", chunks=k)["short_max"] for k in (1, 2, 3, 5)]
        self.assertEqual(containers, sorted(containers, reverse=True),
                         f"контейнер не убывает по K: {containers}")

    def test_zone_ratio_respected(self) -> None:
        """Рабочая зона — параметр: при 0.5 зоны контекст вдвое меньше."""
        full = run("C", zone=0.9)
        half = run("C", zone=0.5)
        self.assertLess(half["context_peak"], full["context_peak"] / 1.5,
                        "контекст не следует за рабочей зоной")


class TestVerdicts(unittest.TestCase):
    """Функция вердикта различает устойчивость и вырождение."""

    def test_a_is_unstable(self) -> None:
        """Режим A признаётся вырожденным."""
        row = run("A")
        ok, verdict = SIM.stability_verdict(row, row["zone"])
        self.assertFalse(ok, f"A признан устойчивым: {verdict}")

    def test_c_is_stable(self) -> None:
        """Режим C признаётся устойчивым."""
        row = run("C")
        ok, verdict = SIM.stability_verdict(row, row["zone"])
        self.assertTrue(ok, f"C признан вырожденным: {verdict}")

    def test_b_flagged_for_overflow(self) -> None:
        """
        Режим B признаётся неустойчивым — из-за переполнения на шаг.

        Его условие `контекст > Z` проверяется после прироста короткой памяти,
        поэтому она успевает перейти порог. Режим C вычитает шаг и не
        переполняет.
        """
        row = run("B")
        ok, verdict = SIM.stability_verdict(row, row["zone"])
        self.assertFalse(ok, f"B признан устойчивым, хотя переполняет: {verdict}")
        self.assertIn("переполнение", verdict.lower())

    def test_short_min_zero_is_not_degeneration(self) -> None:
        """
        Обнуление короткой памяти НЕ считается вырождением.

        Живой урок: первая версия метрики считала `short_min == 0` вырождением,
        но короткая память обнуляется при сжатии **по устройству**. Настоящий
        признак — выход за рабочую зону.
        """
        # имитация замеров устойчивого режима с нулевым минимумом
        row = {
            "created": 30, "zone_used": 99.0, "short_max": 500_000,
            "chunk_last": 160_000, "avg_first_half": 161_000,
            "avg_second_half": 160_000, "stack_size": 2,
        }
        ok, verdict = SIM.stability_verdict(row, 900_000)
        self.assertTrue(ok, f"устойчивый режим признан вырожденным: {verdict}")


def main() -> int:
    """Запустить тесты с кодом возврата по соглашению SeverPact."""
    if not (TOOLS / "simulate_memory.py").exists():
        print("НЕ ИЗМЕРЕНО: нет симулятора", file=sys.stderr)
        return 2
    verbosity = 2 if "-v" in sys.argv else 1
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
