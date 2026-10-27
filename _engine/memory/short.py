"""
SCC — короткая память
======================

Несжатая часть контекста в харнессе: самая актуальная и единственная
несжатая (METHODOLOGY_MEMORY.md §1).

ОСОБЕННОСТЬ: У КОРОТКОЙ ПАМЯТИ НЕТ ЧАНКА
------------------------------------------
Короткая память — это сам контекст, а не ссылка на него. Чанк появляется в
момент, когда короткая память СЖИМАЕТСЯ, и попадает уже в среднюю. Поэтому
класс не хранит чанков: он измеряет объём и сообщает, пора ли сжимать.

ГРАНИЦА ОТВЕТСТВЕННОСТИ
------------------------
Класс НЕ вызывает ядро SCC. Он отвечает на вопрос «пора?», а решение о запуске
свёртки принимает вызывающий: ядро — отдельный компонент, и связывать их внутри
модуля памяти значило бы смешать уровни.

    short = ShortMemory(config)
    short.append("текст сообщения")
    if short.needs_compress():
        ...  # вызвать ядро SCC с short.text()
"""

from __future__ import annotations

from memory import tokens
from memory.config import MemoryConfig


class ShortMemory:
    """
    Несжатый контекст текущего диалога.

    Хранит текст сообщений в порядке поступления. Порядок важен: контекст
    хронологичен, и перемешивание сломало бы всю схему.
    """

    def __init__(self, config: MemoryConfig) -> None:
        self.config = config
        self._parts: list[str] = []

    def append(self, text: str) -> None:
        """Добавить сообщение в конец контекста."""
        if text.strip():
            self._parts.append(text)

    def extend(self, texts: list[str]) -> None:
        """Добавить несколько сообщений по порядку."""
        for text in texts:
            self.append(text)

    def text(self) -> str:
        """Весь несжатый контекст как один текст."""
        return "\n\n".join(self._parts)

    def token_count(self) -> int:
        """Объём короткой памяти в токенах (эвристика, см. memory.tokens)."""
        return tokens.estimate(self.text())

    def needs_compress(self) -> bool:
        """
        Пора ли сжимать короткую память.

        Сравнение с `threshold_compress` — это единственное место, где порог
        применяется. Порог обязателен для короткой памяти: только она растёт от
        чата, и только её сжимает ядро (инвариант 1).
        """
        return self.token_count() > self.config.threshold_compress

    def take(self) -> str:
        """
        Забрать весь контекст и очистить короткую память.

        Вызывается ПОСЛЕ успешного сжатия: если свёртка провалилась, контекст
        должен остаться на месте, иначе он потерян безвозвратно.
        """
        text = self.text()
        self._parts.clear()
        return text

    def __len__(self) -> int:
        """Число сообщений в короткой памяти."""
        return len(self._parts)
