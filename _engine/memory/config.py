"""
SCC — конфигурация памяти
==========================

Пороги и параметры механизма памяти. Требование задания: пороги — **в конфиге,
не магические константы**. Здесь они в одном месте, с обоснованием каждого.

ДВА РАЗНЫХ ПОРОГА И ПОЧЕМУ ИХ ДВА
----------------------------------
`threshold_compress` — когда сжимать короткую память (растёт диалог).
`threshold_archivist` — когда выносить стак в архив (не помещаются чанки).

Если сделать один порог на оба события, архив будет всегда пуст или всегда
переполнен: вынос начнётся ровно там же, где первое сжатие. Это разные причины
с разными триггерами, поэтому и числа разные.

ЕДИНИЦЫ ИЗМЕРЕНИЯ
------------------
Пороги заданы в **токенах**, но считаются эвристикой «4 символа = 1 токен»
(`memory.tokens`). Эвристика занижает на треть — замерено в research/. Для
сравнения уровней между собой этого достаточно, для прогноза стоимости — нет.

    from memory.config import MemoryConfig
    config = MemoryConfig()
    MemoryConfig(threshold_compress=20000)     # своя копия с другим порогом
    config = MemoryConfig.from_env()           # значения из окружения
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

# ── Значения по умолчанию ────────────────────────────────────────────────────
# Обоснование чисел: взяты из замеров research/R-SCC-02 (живая сессия,
# 892 события, ~85 000 токенов в SCC-чанке). Порог сжатия выбран ниже размера
# одного чанка, чтобы короткая память не успевала стать больше самого ядра.
# Баланс порогов проверен замером (tools/measure_memory.py), и это НЕ произвол.
#
# Коэффициент сжатия ядра ×3,6 (замерено: SCC хранит точные данные, поэтому
# сжимает слабее, чем каркас WR). Значит чанк короткой памяти объёмом 12 000
# токенов даёт чанк ≈3 300 токенов — втрое меньше порога сжатия.
#
# Отсюда порог архива 18 000 = примерно 5,4 чанка: архив начинает работать
# около 100-го шага диалога. Порог обязан быть БОЛЬШЕ порога сжатия (это
# проверяет __post_init__), иначе первый же чанк сразу уехал бы в архив и
# средняя память не использовалась бы вовсе.
#
# Прежние значения (архив 60 000, затем 24 000) требовали 68 и 7 чанков —
# долгая память была практически недостижима, и два из трёх уровней памяти
# не проверялись бы ни одним замером.
# ── Рабочая зона и вывод порога ──────────────────────────────────────────────
# Порог сжатия ВЫВОДИТСЯ из рабочей зоны, а не задаётся произвольным числом.
# Обоснование — симуляция (_engine/tools/simulate_memory.py), проверено 21
# тестом (_test/test_memory_stability.py).
#
# ЗАЧЕМ ВЫВОДИТЬ. При фиксированном пороге (доля окна) возможны два вырождения:
#
#   * сжатие по ОБЩЕМУ контексту, стак без ограничения — короткая память
#     душится: чанки образуют геометрический ряд, сумма сходится ко всей зоне;
#   * сжатие по общему контексту, стак ограничен — контекст переполняет зону
#     на один шаг: условие не знает о дискретности прироста короткой памяти.
#
# Устойчиво только правило: **порог на короткую память**, выведенный из зоны
# с учётом шага, и **ограниченное число чанков** в средней памяти.
#
# ФОРМУЛА:
#
#     S = (Z − HEAD) · r / (r + K)      предел короткой памяти
#     T = S − step                       порог сжатия
#
# где Z — рабочая зона (доля окна), r — коэффициент сжатия ядра, K — чанков
# в средней памяти, step — прирост короткой памяти за шаг диалога.
#
# Вычитание шага обязательно: без него контекст выходит за зону на
# `step·(1 + K/r)` — при шаге 5 000 и K=2 это 7 778 токенов.
DEFAULT_ZONE_RATIO = 0.9          # рабочая зона: доля окна модели
DEFAULT_MEDIUM_CHUNKS = 2         # чанков в средней памяти (регулятор места)
DEFAULT_COMPRESS_RATIO = 3.6      # замеренный коэффициент сжатия SCC
DEFAULT_STEP_TOKENS = 5_000       # типовой прирост короткой памяти за шаг

# Пороги по умолчанию — вычисляются под окно 262 144 токена (безопасное
# значение, если окно модели неизвестно). При известном окне используйте
# `derive_thresholds(window)`.
DEFAULT_WINDOW = 262_144
DEFAULT_HEAD_TOKENS = 2_000       # роль и инструкции в HEAD

# Минимальная длина строки, которую имеет смысл заменять ссылкой.
# Ссылка `CAS?{8 hex}?` занимает 13 символов; заменять более короткое значение
# означает рост, а не экономию.
DEFAULT_GLOSSARY_MIN_LENGTH = 16

# Сколько байтов читать из файла чанка за раз (защита от гигантских файлов).
DEFAULT_MAX_CHUNK_BYTES = 64 * 1024 * 1024


def derive_thresholds(
    window: int,
    head_tokens: int = DEFAULT_HEAD_TOKENS,
    medium_chunks: int = DEFAULT_MEDIUM_CHUNKS,
    ratio: float = DEFAULT_COMPRESS_RATIO,
    step_tokens: int = DEFAULT_STEP_TOKENS,
    zone_ratio: float = DEFAULT_ZONE_RATIO,
) -> tuple[int, int]:
    """
    Вывести пороги из рабочей зоны. Возвращает (порог_сжатия, порог_архива).

    Устойчивое правило (обоснование — симуляция `simulate_memory.py`):

        Z = window · zone_ratio            рабочая зона
        S = (Z − HEAD) · r / (r + K)       предел короткой памяти
        T = S − step                       порог сжатия

    Порог архивации ставится на **середину** между `K` и `K+1` чанками:
    `T_arch = K·c + c/2`. Причина — строгое `>` в `needs_archive`: порог ровно
    `K·c` оказывается на границе, и вынос срабатывает уже на K-м чанке, из-за
    чего стак держит `K−1`.

    ПОЧЕМУ ИМЕННО ТАК. Проверено симуляцией на разбросе K и r: только это
    правило даёт одновременно контейнер короткой памяти, близкие размеры
    чанков и постоянное их количество в средней памяти. Альтернативы
    вырождаются (см. `_test/test_memory_stability.py`).

    Аргументы:
        window — окно модели в токенах (для `deepseek-v4.1-flash` = 1 000 000).
        head_tokens — размер HEAD: роль плюс инструкции.
        medium_chunks — сколько чанков держим в средней памяти (регулятор:
            больше чанков — меньше остаётся короткой памяти).
        ratio — коэффициент сжатия ядра (замерено ×3,6 для SCC).
        step_tokens — прирост короткой памяти за шаг диалога.
        zone_ratio — доля окна, отводимая под всю память.
    """
    if window <= 0:
        raise ValueError(f"окно модели должно быть положительным, получено {window}")
    if medium_chunks < 1:
        raise ValueError(f"чанков в средней памяти должно быть >= 1, получено {medium_chunks}")
    if ratio <= 1:
        raise ValueError(f"коэффициент сжатия должен быть > 1, получено {ratio}")
    if not 0 < zone_ratio <= 1:
        raise ValueError(f"рабочая зона — доля окна в пределах (0, 1], получено {zone_ratio}")

    zone = int(window * zone_ratio)
    if zone <= head_tokens:
        raise ValueError(
            f"рабочая зона {zone} не больше HEAD {head_tokens}: "
            "места под память не остаётся"
        )

    # ВЫНОС ПО ОДНОМУ: максимум на единицу больше равновесия.
    #
    # `evict_to_limit` выносит старейший чанк, когда стак превысил порог.
    # Значит стак колеблется: `N` -> `N+1` (сработал порог) -> `N` (вынесли
    # один). Максимум равен `N+1`, и именно его надо укладывать в зону:
    #
    #     HEAD + (N+1)·c + T <= Z,   c = T/r
    #
    # Живой урок 2026-10-27: сначала здесь стояло `r + K`, то есть условие для
    # максимума `K`. При выносе по одному это оставляло стак на единицу меньше
    # задуманного — `K` в конфиге означало `K−1` на практике.
    slots = medium_chunks + 1
    limit = (zone - head_tokens) * ratio / (ratio + slots)
    threshold_compress = max(1, int(limit - step_tokens))

    # ПОРОГ АРХИВАЦИИ: K чанков.
    #
    # `move_to_long` выносит **самый старый** чанк, а актуальные остаются.
    # Значит стак держится ровно `K`: на превышении порога вытесняется один
    # старый, и стак возвращается к `K`. Условие помещения в зону:
    #
    #     HEAD + K·c + T <= Z
    #
    # Отсюда `T_arch = K·c`.
    #
    # ЖИВОЙ УРОК. При ПОЛНОМ выносе (прежняя версия) максимальный стак равнялся
    # порогу ПЛЮС целый стак, и порог приходилось ставить на `(K−1)·c`. Это
    # оставляло неиспользованными 15,9 % рабочей зоны: при окне 1M пик был
    # 733 253 (73 % окна) против 892 221 (89 %) сейчас.
    chunk_size = threshold_compress / ratio
    # `needs_archive` использует СТРОГОЕ `>`, а размеры чанков близки, но не
    # равны: K·c оказывается ровно на границе, и вынос срабатывает уже на K-м
    # чанке — стак держит K−1 вместо K.
    #
    # Живой урок 2026-10-27: при пороге 269 343 и чанке 134 672 два чанка давали
    # ровно 269 343, условие `>` срабатывало, и в средней памяти оставался один
    # чанк вместо двух. Пик контекста был 62 % вместо расчётных 76 %.
    #
    # Порог ставится на середину между K и K+1 чанками: тогда K чанков НЕ
    # превышают порог, а K+1 — превышают.
    threshold_archivist = max(1, int(medium_chunks * chunk_size + chunk_size / 2))
    return threshold_compress, threshold_archivist


@dataclass(frozen=True)
class MemoryConfig:
    """
    Настройки механизма памяти. Неизменяемый: правка создаёт новую копию.

    Неизменяемость здесь не украшение: конфиг читается несколькими компонентами
    (ядро, стак, CAS), и его случайная правка на лету дала бы разное
    поведение в разных местах одного прогона.
    """

    # ── Пороги ───────────────────────────────────────────────────────────────
    # По умолчанию вычисляются под окно DEFAULT_WINDOW. При известном окне
    # модели используйте `MemoryConfig.for_window(window)`.
    threshold_compress: int = field(
        default_factory=lambda: derive_thresholds(DEFAULT_WINDOW)[0]
    )
    threshold_archivist: int = field(
        default_factory=lambda: derive_thresholds(DEFAULT_WINDOW)[1]
    )

    # ── Глоссарий ────────────────────────────────────────────────────────────
    cas_min_length: int = DEFAULT_GLOSSARY_MIN_LENGTH
    cas_enabled: bool = True

    # ── Прочее ───────────────────────────────────────────────────────────────
    max_chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES

    def __post_init__(self) -> None:
        """Проверить конфиг на очевидные ошибки сразу, а не при первом сжатии."""
        if self.threshold_compress <= 0:
            raise ValueError(
                f"threshold_compress должен быть положительным, получено {self.threshold_compress}"
            )
        if self.threshold_archivist <= 0:
            raise ValueError(
                f"threshold_archivist должен быть положительным, получено {self.threshold_archivist}"
            )
        # ПОРОГИ ИЗМЕРЯЮТ РАЗНОЕ — сравнивать их напрямую нельзя.
        #
        #   threshold_compress  — размер КОРОТКОЙ памяти (когда сжимать)
        #   threshold_archivist — размер СТАКА ЧАНКОВ (когда выносить)
        #
        # Живой урок 2026-10-27: здесь стояла проверка `archivist > compress`.
        # При окне 1M она давала «порог архива 397 420 должен быть больше
        # порога сжатия 572 285» — и поднимала архив до 572 286, то есть
        # ломала вывод порогов. Отношение между разными единицами смысла не имеет.
        #
        # Содержательное условие: **вся память помещается в рабочую зону**.
        # Оно проверяется отдельным методом `fits_zone(window)`, потому что
        # требует знания окна модели, которого в конфиге нет.
        if self.threshold_archivist < 1:
            raise ValueError(
                f"threshold_archivist должен быть положительным, "
                f"получено {self.threshold_archivist}"
            )
        if self.cas_min_length < 0:
            raise ValueError("cas_min_length не может быть отрицательным")

    def fits_zone(
        self,
        window: int,
        head_tokens: int = DEFAULT_HEAD_TOKENS,
        ratio: float = DEFAULT_COMPRESS_RATIO,
        zone_ratio: float = DEFAULT_ZONE_RATIO,
    ) -> bool:
        """
        Помещается ли память в рабочую зону окна.

        Это **содержательная** проверка вместо сравнения порогов между собой:
        они измеряют разное, и отношение между ними ничего не значит.

        Условие: `HEAD + стак + короткая <= Z`. Размер стака берётся как
        `threshold_archivist` (порог, при котором начинается вынос), размер
        короткой — как `threshold_compress`.
        """
        zone = int(window * zone_ratio)
        return head_tokens + self.threshold_archivist + self.threshold_compress <= zone

    @classmethod
    def for_window(cls, window: int, **kwargs: object) -> "MemoryConfig":
        """
        Собрать конфиг под конкретное окно модели.

        Пороги выводятся из рабочей зоны, а не задаются вручную — так исключены
        вырождения, найденные симуляцией (`_engine/tools/simulate_memory.py`).
        """
        compress, archivist = derive_thresholds(window, **kwargs)  # type: ignore[arg-type]
        return cls(threshold_compress=compress, threshold_archivist=archivist)

    def with_thresholds(self, compress: int | None = None, archivist: int | None = None) -> "MemoryConfig":
        """Вернуть копию с другими порогами (исходный конфиг не меняется)."""
        return replace(
            self,
            threshold_compress=compress if compress is not None else self.threshold_compress,
            threshold_archivist=archivist if archivist is not None else self.threshold_archivist,
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "MemoryConfig":
        """
        Собрать конфиг из окружения. Отсутствующие переменные — значения по умолчанию.

        Переменные:
            SCC_WINDOW               — окно модели, из него выводятся пороги
            SCC_THRESHOLD_COMPRESS   — порог сжатия (переопределяет вывод)
            SCC_THRESHOLD_ARCHIVIST  — порог архивации (переопределяет вывод)
            SCC_CAS_MIN_LENGTH       — минимальная длина заменяемого значения
            SCC_CAS_ENABLED          — "0"/"false" отключает CAS
        """
        source = os.environ if env is None else env

        def number(name: str, default: int) -> int:
            raw = source.get(name)
            if raw is None or raw.strip() == "":
                return default
            try:
                return int(raw)
            except ValueError as error:
                raise ValueError(f"{name}: ожидалось целое число, получено {raw!r}") from error

        # Пороги по умолчанию ВЫВОДЯТСЯ из окна модели; переменные окружения
        # могут переопределить их явно.
        window = number("SCC_WINDOW", DEFAULT_WINDOW)
        derived = derive_thresholds(window)

        enabled_raw = source.get("SCC_CAS_ENABLED", "1").strip().lower()
        return cls(
            threshold_compress=number("SCC_THRESHOLD_COMPRESS", derived[0]),
            threshold_archivist=number("SCC_THRESHOLD_ARCHIVIST", derived[1]),
            cas_min_length=number("SCC_CAS_MIN_LENGTH", DEFAULT_GLOSSARY_MIN_LENGTH),
            cas_enabled=enabled_raw not in ("0", "false", "no", "off"),
        )
