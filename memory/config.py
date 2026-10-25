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
from dataclasses import dataclass, replace

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
DEFAULT_THRESHOLD_COMPRESS = 12_000
DEFAULT_THRESHOLD_ARCHIVIST = 18_000

# Минимальная длина строки, которую имеет смысл заменять ссылкой.
# Ссылка `LB?{8 hex}?` занимает 13 символов; заменять более короткое значение
# означает рост, а не экономию.
DEFAULT_GLOSSARY_MIN_LENGTH = 16

# Сколько байтов читать из файла чанка за раз (защита от гигантских файлов).
DEFAULT_MAX_CHUNK_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class MemoryConfig:
    """
    Настройки механизма памяти. Неизменяемый: правка создаёт новую копию.

    Неизменяемость здесь не украшение: конфиг читается несколькими компонентами
    (ядро, стак, CAS), и его случайная правка на лету дала бы разное
    поведение в разных местах одного прогона.
    """

    # ── Пороги ───────────────────────────────────────────────────────────────
    threshold_compress: int = DEFAULT_THRESHOLD_COMPRESS
    threshold_archivist: int = DEFAULT_THRESHOLD_ARCHIVIST

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
        if self.threshold_archivist <= self.threshold_compress:
            # Архив обязан начинаться ПОЗЖЕ первого сжатия, иначе он бессмыслен:
            # первый же чанк сразу уедет в архив, и средняя память не будет
            # использоваться вовсе.
            raise ValueError(
                "threshold_archivist должен быть больше threshold_compress "
                f"({self.threshold_archivist} <= {self.threshold_compress}): "
                "иначе первый же чанк сразу уезжает в архив и средняя память не используется"
            )
        if self.cas_min_length < 0:
            raise ValueError("cas_min_length не может быть отрицательным")

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
            SCC_THRESHOLD_COMPRESS   — порог сжатия короткой памяти
            SCC_THRESHOLD_ARCHIVIST  — порог выноса стака в архив
            SCC_CAS_MIN_LENGTH  — минимальная длина заменяемого значения
            SCC_CAS_ENABLED     — "0"/"false" отключает CAS
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

        enabled_raw = source.get("SCC_CAS_ENABLED", "1").strip().lower()
        return cls(
            threshold_compress=number("SCC_THRESHOLD_COMPRESS", DEFAULT_THRESHOLD_COMPRESS),
            threshold_archivist=number("SCC_THRESHOLD_ARCHIVIST", DEFAULT_THRESHOLD_ARCHIVIST),
            cas_min_length=number("SCC_CAS_MIN_LENGTH", DEFAULT_GLOSSARY_MIN_LENGTH),
            cas_enabled=enabled_raw not in ("0", "false", "no", "off"),
        )
