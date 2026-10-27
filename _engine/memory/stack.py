"""
SCC — стак чанков
==================

Детерминированный стак `chunk_1..chunk_n` плюс `chain.json` как единственный
источник правды о порядке (инвариант 6 в METHODOLOGY_MEMORY.md).

ПОЧЕМУ НЕ ЛЕКСИКОГРАФИЧЕСКАЯ СОРТИРОВКА
-----------------------------------------
Имена файлов `chunk_1.json ... chunk_10.json` при обычной строковой сортировке
дают порядок `chunk_1, chunk_10, chunk_2, ...` — хронология разрушается. Это
прямо запрещено заданием и нарушает инвариант 5: стак обязан быть
детерминированной функцией от номеров. Поэтому сортировка идёт **по числовому
полю `n`** из chain.json, а имена файлов — только способ хранения.

ИММУТАБЕЛЬНОСТЬ
----------------
Чанк, единожды записанный, не перезаписывается (инвариант 4). Метод `add`
отказывается писать поверх существующего номера: новое сжатие обязано получить
новый `n`. Это защита от самой опасной ошибки — молчаливой потери старого чанка.

    stack = ChunkStack(dialog_dir)
    stack.add(chunk)                 # откажет, если n уже занят
    stack.medium()                   # чанки уровня medium
    stack.ordered()                  # все чанки по числовому n
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from memory.schema import SchemaError, validate_chunk

CHAIN_FILENAME = "chain.json"
CHUNKS_DIRNAME = "chunks"


class ChunkError(RuntimeError):
    """Нарушение инварианта работы с чанками."""


# SchemaError поднимается как есть: она называет конкретное поле и уже
# наследуется от ValueError. Оборачивать её в ChunkError значило бы потерять
# точность сообщения — а именно оно помогает исправить чанк.


class ChunkStack:
    """
    Стак чанков одного диалога.

    Каталог диалога создаётся при первом обращении: `chunks/` и `chain.json`.
    chain.json — источник правды о порядке и уровнях; содержимое чанков лежит
    отдельными файлами в `chunks/`.
    """

    def __init__(self, dialog_dir: Path, dialog_id: str | None = None) -> None:
        self.dialog_dir = Path(dialog_dir)
        self.dialog_id = dialog_id or self.dialog_dir.name
        self.chunks_dir = self.dialog_dir / CHUNKS_DIRNAME
        self.chain_path = self.dialog_dir / CHAIN_FILENAME
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        if not self.chain_path.exists():
            self._write_chain({"dialog_id": self.dialog_id, "chunks": [], "last_chunk": 0})

    # ── chain.json ───────────────────────────────────────────────────────────

    def _read_chain(self) -> dict:
        """Прочитать chain.json. Битый файл — исключение, а не молчаливый ноль."""
        try:
            return json.loads(self.chain_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ChunkError(
                f"chain.json нечитаем ({self.chain_path}): {error}. "
                "Молча начать заново нельзя: это потеря порядка чанков (инвариант 6)"
            ) from error

    def _write_chain(self, chain: dict) -> None:
        """Записать chain.json атомарно (через временный файл)."""
        chain["updated_at"] = datetime.now(timezone.utc).isoformat()
        temporary = self.chain_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(chain, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.chain_path)

    def chunk_path(self, n: int) -> Path:
        """Путь файла чанка. Имя — только способ хранения, порядок задаёт поле n."""
        return self.chunks_dir / f"chunk_{n:04d}.json"

    # ── Запись ───────────────────────────────────────────────────────────────

    def add(self, chunk: dict) -> None:
        """
        Добавить чанк. Требует поля `n`; отказывает при повторном номере.

        Проверка дубликата — защита инварианта 4: чанк неизменяем после записи.
        Без неё повторная свёртка молча затёрла бы старый чанк, и потеря
        контекста осталась бы незамеченной.
        """
        # СХЕМА ПРОВЕРЯЕТСЯ ДО ЗАПИСИ, и это принципиально. Чанк неизменяем
        # (инвариант 4): записанный однажды, он не перезаписывается. Значит
        # ошибка в структуре осталась бы в архиве навсегда — исправить её будет
        # нечем. Поэтому контракт из core/schemas/chunk.schema.json исполняется
        # здесь, а не описывается только в документации.
        validate_chunk(chunk)

        n = chunk.get("n")
        if not isinstance(n, int) or n < 1:
            raise ChunkError(f"чанк обязан иметь целое поле n >= 1, получено {n!r}")

        chain = self._read_chain()
        if any(entry["n"] == n for entry in chain["chunks"]):
            raise ChunkError(
                f"чанк с n={n} уже существует — перезапись запрещена (инвариант 4). "
                "Новое сжатие обязано получить новый номер"
            )

        path = self.chunk_path(n)
        if path.exists():
            raise ChunkError(
                f"файл {path.name} уже существует, но отсутствует в chain.json — "
                "состояние расходится, требуется ручная проверка"
            )

        path.write_text(json.dumps(chunk, ensure_ascii=False, indent=2), encoding="utf-8")
        chain["chunks"].append(
            {"n": n, "level": chunk.get("level", "medium"), "file": f"{CHUNKS_DIRNAME}/{path.name}"}
        )
        chain["last_chunk"] = max(chain.get("last_chunk", 0), n)
        self._write_chain(chain)

    def set_level(self, n: int, level: str) -> None:
        """
        Сменить уровень чанка: medium -> long при выносе в архив.

        Меняется только запись в chain.json; файл чанка НЕ трогается — он
        неизменяем (инвариант 4). Уровень описывает, где чанк находится сейчас.
        """
        if level not in ("medium", "long"):
            raise ChunkError(f"уровень обязан быть medium или long, получено {level!r}")
        chain = self._read_chain()
        for entry in chain["chunks"]:
            if entry["n"] == n:
                entry["level"] = level
                self._write_chain(chain)
                return
        raise ChunkError(f"чанк n={n} не найден в chain.json")

    # ── Чтение ───────────────────────────────────────────────────────────────

    def ordered(self) -> list[dict]:
        """
        Все чанки по ВОЗРАСТАНИЮ номера.

        Сортировка числовая, не строковая: см. заголовок модуля.
        """
        chain = self._read_chain()
        return sorted(chain["chunks"], key=lambda entry: entry["n"])

    def by_level(self, level: str) -> list[dict]:
        """Чанки одного уровня, по возрастанию номера."""
        if level not in ("medium", "long"):
            raise ChunkError(f"уровень обязан быть medium или long, получено {level!r}")
        return [entry for entry in self.ordered() if entry.get("level") == level]

    def medium(self) -> list[dict]:
        """Чанки средней памяти (в стаке, помещаются в харнесс)."""
        return self.by_level("medium")

    def long(self) -> list[dict]:
        """Чанки долгой памяти (в архиве)."""
        return self.by_level("long")

    def load(self, n: int) -> dict:
        """Прочитать содержимое чанка по номеру."""
        chain = self._read_chain()
        entry = next((e for e in chain["chunks"] if e["n"] == n), None)
        if entry is None:
            raise ChunkError(f"чанк n={n} не найден в chain.json")
        path = self.dialog_dir / entry["file"]
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ChunkError(f"чанк n={n} нечитаем ({path}): {error}") from error

    def last_chunk_number(self) -> int:
        """Номер последнего чанка. 0, если чанков нет."""
        return int(self._read_chain().get("last_chunk", 0))

    def content_of_level(self, level: str) -> str:
        """
        Содержимое всех чанков уровня, склеенное по порядку.

        Именно этот текст попадает в контекст для средней памяти. Для долгой он
        НЕ вызывается: архив в контекст не попадает (инвариант METHODOLOGY_MEMORY
        §1), к нему обращаются через Архивариуса.
        """
        parts = [self.load(entry["n"]).get("content", "") for entry in self.by_level(level)]
        return "\n\n".join(part for part in parts if part)
