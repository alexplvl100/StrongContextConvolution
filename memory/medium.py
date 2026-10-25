"""
SCC — средняя память
=====================

Стак чанков `chunk_1..chunk_k`, который ещё помещается в харнесс
(METHODOLOGY_MEMORY.md §1). В контексте идут: стак + короткая память.

БЕЗ LLM
--------
Модуль выполняет только механическую работу: изъятие чанков перед свёрткой,
добавление нового чанка, обновление стака. Никаких вызовов модели — это
инвариант 3, и он проверяется тестом (отсутствие импортов LLM-клиентов).

СВЯЗЬ СО СТАКОМ
----------------
Средняя память — надстройка над ChunkStack: стак отвечает за порядок и
хранение, средняя — за решение «пора в архив» и за сборку текста для контекста.

    medium = MediumMemory(stack, config)
    medium.append_chunk(chunk)             # после свёртки ядром
    if medium.needs_archive():
        medium.move_to_long(long_memory)   # вынос, без LLM
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from memory import tokens
from memory.config import MemoryConfig
from memory.stack import ChunkStack

if TYPE_CHECKING:
    # Только для подсказок типов: прямой импорт создал бы цикл medium <-> long.
    from memory.long import LongMemory


class MediumMemory:
    """
    Средняя память: стак чанков в пределах порога.

    Порог `threshold_archivist` — не то же, что `threshold_compress`: первый
    про объём стака, второй про объём короткой памяти. Разные причины, разные
    числа (METHODOLOGY_MEMORY.md §4).
    """

    def __init__(self, stack: ChunkStack, config: MemoryConfig) -> None:
        self.stack = stack
        self.config = config

    def append_chunk(self, chunk: dict) -> None:
        """
        Добавить чанк, созданный ядром.

        Чанк обязан иметь `level: medium` — средняя память не принимает чанки
        архива. Проверка здесь, а не в стеке: стек хранит любой уровень, а
        политика уровней — дело памяти.
        """
        if chunk.get("level") != "medium":
            raise ValueError(
                f"средняя память принимает только level=medium, получено {chunk.get('level')!r}"
            )
        self.stack.add(chunk)

    def chunks(self) -> list[dict]:
        """Записи чанков средней памяти по возрастанию номера."""
        return self.stack.medium()

    def content(self) -> str:
        """
        Текст средней памяти для контекста.

        Порядок — по числовому `n`, не по имени файла (см. memory/stack.py).
        """
        return self.stack.content_of_level("medium")

    def token_count(self) -> int:
        """Объём средней памяти в токенах."""
        return tokens.estimate(self.content())

    def needs_archive(self) -> bool:
        """
        Пора ли выносить стак в архив.

        Условие из METHODOLOGY_MEMORY.md: размер стака > `threshold_archivist`.
        Строго больше: на равенстве стак ещё помещается.
        """
        return self.token_count() > self.config.threshold_archivist

    def move_to_long(self, long_memory: LongMemory) -> int:
        """
        Вынести все чанки средней памяти в архив. Возвращает их число.

        Операция БЕЗ LLM: перенос — это смена уровня в chain.json
        (`medium` -> `long`). Файлы чанков не трогаются вовсе — они неизменяемы
        (инвариант 4), а уровень описывает, где чанк находится сейчас.

        Порядок сохранён: `long_memory.accept` получает номера по возрастанию.
        """
        numbers = [entry["n"] for entry in self.stack.medium()]
        for number in numbers:
            self.stack.set_level(number, "long")
        long_memory.after_accept(self.stack, numbers)
        return len(numbers)

    def __len__(self) -> int:
        """Число чанков в средней памяти."""
        return len(self.stack.medium())
