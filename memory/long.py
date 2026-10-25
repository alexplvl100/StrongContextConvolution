"""
SCC — долгая память
====================

Архив чанков, вынесенных из контекста (METHODOLOGY_MEMORY.md §1). В контекст
попадает **только инструкция** с номером последнего чанка; содержимое — через
Архивариуса.

ЧЕГО ЗДЕСЬ НЕТ
---------------
Модуль **не возвращает чанки в среднюю память**. Переход «долгая -> средняя» не
существует: повторная свёртка чанков запрещена (инвариант 2), а возврат в стак
означал бы, что чанк снова может попасть под свёртку. Архивариус только читает.

Модуль **не сжимает** и **не обрабатывает LLM**. Вынос — смена уровня в
chain.json, больше ничего.

    long_memory = LongMemory(stack)
    long_memory.after_accept(stack, [1, 2, 3])   # вызвал move_to_long
    long_memory.instruction()                    # текст для контекста
    long_memory.read(2)                          # чанк из архива (для Архивариуса)
"""

from __future__ import annotations

from memory import tokens
from memory.config import MemoryConfig
from memory.stack import ChunkStack

# ── Шаблон инструкции ────────────────────────────────────────────────────────
# Формулировка должна сообщать агенту три вещи: что архив есть, где граница
# (номер последнего чанка) и как получить содержимое. Без номера агент не знает,
# какие чанки существуют, и не может сформулировать запрос.
INSTRUCTION_TEMPLATE = (
    "[АРХИВ ПАМЯТИ] Чанки 1..{last} вынесены из контекста и недоступны напрямую. "
    "Они не потеряны: содержимое хранится в архиве и доступно по запросу через "
    "Архивариуса. Запроси конкретный чанк по номеру, если его содержимое нужно "
    "для текущей задачи. Повторная свёртка архивных чанков не выполняется."
)


class LongMemory:
    """
    Долгая память: архив чанков.

    Сам архив — это чанки с `level: long` в общем стеке. Класс отвечает за
    инструкцию для контекста и за чтение по запросу (для Архивариуса).
    """

    def __init__(self, stack: ChunkStack, config: MemoryConfig | None = None) -> None:
        self.stack = stack
        self.config = config

    def after_accept(self, stack: ChunkStack, numbers: list[int]) -> None:
        """
        Завершить вынос: записать факт архивации.

        Отдельный метод, а не побочный эффект `move_to_long`: вызывающий должен
        явно видеть, что переход завершён. Здесь же — место для будущей
        синхронизации с базой CAS (сейчас её нет).
        """
        self._archived = sorted(set(numbers)) if numbers else []

    def chunks(self) -> list[dict]:
        """Записи архивных чанков по возрастанию номера."""
        return self.stack.long()

    def read(self, n: int) -> dict:
        """
        Прочитать чанк из архива по номеру.

        Это точка входа для Архивариуса: он читает по запросу агента. Метод
        только читает — переписать или сжать чанк нельзя (инвариант 7).
        """
        entry = next((e for e in self.stack.long() if e["n"] == n), None)
        if entry is None:
            raise KeyError(f"чанк n={n} не найден в архиве")
        return self.stack.load(n)

    def content(self) -> str:
        """
        Содержимое ВСЕГО архива.

        ВНИМАНИЕ: этот метод существует для замеров и отладки. В контекст
        агента архивное содержимое НЕ попадает — только инструкция. Вызов в
        рабочем пути означал бы нарушение схемы уровней.
        """
        return self.stack.content_of_level("long")

    def token_count(self) -> int:
        """Объём архива в токенах (для замеров, не для контекста)."""
        return tokens.estimate(self.content())

    def instruction(self) -> str:
        """
        Текст инструкции для контекста.

        Пустая строка, если архив пуст: упоминать несуществующий архив вредно —
        агент начнёт запрашивать то, чего нет.
        """
        last = self.last_number()
        if last == 0:
            return ""
        return INSTRUCTION_TEMPLATE.format(last=last)

    def last_number(self) -> int:
        """Номер последнего архивного чанка. 0, если архив пуст."""
        archive = self.stack.long()
        return max((entry["n"] for entry in archive), default=0)

    def is_empty(self) -> bool:
        """Пуст ли архив."""
        return not self.stack.long()

    def __len__(self) -> int:
        """Число чанков в архиве."""
        return len(self.stack.long())
