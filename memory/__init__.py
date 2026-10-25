"""
SCC — трёхуровневый механизм памяти
=====================================

Пакет реализует уровни памяти из METHODOLOGY_MEMORY.md: короткая (несжатый
контекст), средняя (стак чанков в харнессе), долгая (архив вне контекста).

ГЛАВНОЕ ПРАВИЛО ПАКЕТА: здесь НЕТ и не должно быть вызовов языковой модели.
Оборот средней и долгой памяти — строго скриптами (инвариант 3). Проверяется
тестом, который ищет импорты LLM-клиентов в исходниках пакета.

Быстрый старт:

    from memory.config import MemoryConfig
    from memory.glossary import Glossary
    from memory.stack import ChunkStack

    config = MemoryConfig()
    stack = ChunkStack(dialog_dir)
    glossary = Glossary(dialog_dir / "glossary.db")
"""

from __future__ import annotations

__all__ = [
    "config",
    "tokens",
    "glossary",
    "stack",
    "short",
    "medium",
    "long",
]
