"""
SCC — глоссарий точных данных
==============================

Заменяет повторяющиеся точные данные ссылками вида `LB?{хеш}?`, храня оригиналы
в SQLite. Описан в METHODOLOGY_LIBRARY.md.

БЕЗ LLM
--------
Весь модуль — детерминированная обработка текста: нормализация, sha256, замена,
SQLite. Языковая модель здесь не участвует и не должна (инвариант 3 в
METHODOLOGY_MEMORY.md). Импортов LLM-клиентов в файле нет — это проверяется
тестом.

ПОЧЕМУ ХЕШ, А НЕ ИНДЕКС
------------------------
Индекс (`LB?1?`, `LB?2?`) короче, но привязан к порядку добавления: скопировали
чанк в другой диалог — ссылки указывают не туда. Хеш от содержимого переносим:
одна и та же строка в любом диалоге даёт одну и ту же ссылку.

СТАБИЛЬНОСТЬ
-------------
Одна и та же строка обязана давать один и тот же хеш — иначе восстановление
невозможно. Поэтому нормализация (снятие кавычек, пробелов) выполняется ДО
хеширования и зафиксирована тестом.

    from memory.glossary import Glossary
    g = Glossary(Path("runtime/dialogs/d1/glossary.db"))
    text, hashes = g.replace_in_text("путь /home/user/x.yml и снова /home/user/x.yml")
    g.restore(hashes[0])        # -> "/home/user/x.yml"
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ── Формат ссылки ────────────────────────────────────────────────────────────
# `LB?{8 hex}?` — 13 символов. LB от «library»: имя файла методологии, по
#становлению. Термин в тексте документа — «глоссарий».
REFERENCE_PREFIX = "LB?"
REFERENCE_SUFFIX = "?"
REFERENCE_RE = re.compile(r"LB\?([0-9a-f]{8})\?")

# ── Хеш ──────────────────────────────────────────────────────────────────────
HASH_HEX_LENGTH = 8
HASH_SPACE = 16**HASH_HEX_LENGTH

# ── Что считаем точными данными ──────────────────────────────────────────────
# Список ОТКРЫТЫЙ (METHODOLOGY_LIBRARY.md §2). Регулярки ищут кандидатов; длину
# отсекает glossary_min_length из конфига.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Пути: абсолютные и относительные, с расширением или без
    ("path", re.compile(r"(?:/[\w.\-@]+){2,}/?")),
    # Хеши: 16+ hex-символов (полные 64-символьные и длинные фрагменты)
    ("hash", re.compile(r"\b[0-9a-f]{16,}\b")),
    # Идентификаторы вида владелец/репозиторий, версии моделей
    ("identifier", re.compile(r"\b[\w.\-]+/[\w.\-]+(?:/[\w.\-]+)*\b")),
    # Версии и числовые идентификаторы с точками: 4.1, 0.2.1-rc.2
    ("version", re.compile(r"\b\d+\.\d+(?:\.\d+)*(?:-[\w.\-]+)?\b")),
    # URL
    ("url", re.compile(r"https?://[^\s\)\]\},;\"']+")),
    # Даты и время ISO 8601
    ("datetime", re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b")),
    # Длинные числа от 5 разрядов: 262144, 65536 (короткие не выгодны)
    ("number", re.compile(r"\b\d{5,}\b")),
    # Имена файлов с расширением
    ("filename", re.compile(r"\b[\w.\-]+\.(?:py|md|json|yml|yaml|txt|db|js|ts|sh|toml|cfg|ini|log)\b")),
)

# Обрамление, которое снимается перед хешированием: кавычки, бэктики, скобки.
# Без этого `"путь"` и `путь` дали бы разные хеши при одном смысле.
_STRIP_CHARS = " \t\n\r\"'`«»()[]{}<>,;:"


@dataclass(frozen=True)
class GlossaryEntry:
    """Запись глоссария: хеш, оригинал, когда добавлен, сколько раз встретился."""

    hash: str
    value: str
    first_seen: str
    uses: int


def normalize(value: str) -> str:
    """
    Привести значение к канонической форме ПЕРЕД хешированием.

    Правила (METHODOLOGY_LIBRARY.md §5): снять обрамляющие пробелы, кавычки и
    бэктики, схлопнуть внутренние пробелы. Регистр НЕ трогаем: в путях и
    идентификаторах он значим.
    """
    result = value.strip(_STRIP_CHARS)
    return re.sub(r"\s+", " ", result)


def compute_hash(value: str) -> str:
    """
    Хеш нормализованной строки: sha256, первые 8 hex-символов.

    Стабильность обязательна (одна строка -> один хеш). Это чистая функция:
    результат зависит только от входа.
    """
    digest = hashlib.sha256(normalize(value).encode("utf-8")).hexdigest()
    return digest[:HASH_HEX_LENGTH]


def make_reference(hash_value: str) -> str:
    """Собрать ссылку из хеша: `LB?a1b2c3d4?`."""
    return f"{REFERENCE_PREFIX}{hash_value}{REFERENCE_SUFFIX}"


class Glossary:
    """
    База точных данных одного диалога.

    Файл БД лежит в runtime/dialogs/<dialog_id>/glossary.db. Каталог создаётся
    автоматически: вызывающему не нужно помнить о структуре.
    """

    def __init__(self, db_path: Path, min_length: int = 16) -> None:
        self.db_path = Path(db_path)
        self.min_length = min_length
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.db_path))
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        """Создать таблицу, если её нет. Схема — из METHODOLOGY_LIBRARY.md §6."""
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS glossary (
                hash       TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                uses       INTEGER DEFAULT 1
            )
            """
        )
        self._connection.commit()

    # ── Запись ───────────────────────────────────────────────────────────────

    def add(self, value: str) -> str | None:
        """
        Добавить значение и вернуть его хеш.

        Возвращает None, если значение короче порога: замена невыгодна (ссылка
        длиннее оригинала). Значения короче порога в базу не попадают — иначе
        база росла бы мусором.

        КОЛЛИЗИИ. Если хеш занят ДРУГОЙ строкой, берётся следующий свободный
        (hash+1). Конфликт логируется: он означает рост базы выше расчётного
        (2^32 значений).
        """
        canonical = normalize(value)
        if len(canonical) < self.min_length:
            return None

        hash_value = compute_hash(canonical)
        existing = self._connection.execute(
            "SELECT value FROM glossary WHERE hash = ?", (hash_value,)
        ).fetchone()

        if existing is not None:
            if existing["value"] == canonical:
                # Повтор: увеличиваем счётчик использования, хеш не меняется.
                self._connection.execute(
                    "UPDATE glossary SET uses = uses + 1 WHERE hash = ?", (hash_value,)
                )
                self._connection.commit()
                return hash_value
            # Коллизия: ищем свободный хеш последовательным сдвигом.
            hash_value = self._resolve_collision(canonical, hash_value)

        self._connection.execute(
            "INSERT INTO glossary (hash, value, first_seen, uses) VALUES (?, ?, ?, 1)",
            (hash_value, canonical, datetime.now(timezone.utc).isoformat()),
        )
        self._connection.commit()
        return hash_value

    def _resolve_collision(self, canonical: str, start_hash: str) -> str:
        """
        Найти свободный хеш при коллизии.

        Сдвиг на единицу по модулю пространства хешей. Логируем в stderr: это
        редкое событие, и о нём стоит знать — оно говорит, что база подошла к
        пределу 8-символьного хеша.
        """
        import sys

        candidate = (int(start_hash, 16) + 1) % HASH_SPACE
        for _ in range(HASH_SPACE):
            candidate_hash = f"{candidate:0{HASH_HEX_LENGTH}x}"
            row = self._connection.execute(
                "SELECT value FROM glossary WHERE hash = ?", (candidate_hash,)
            ).fetchone()
            if row is None:
                print(
                    f"глоссарий: КОЛЛИЗИЯ для {canonical[:60]!r}: "
                    f"{start_hash} занят, взят {candidate_hash}",
                    file=sys.stderr,
                )
                return candidate_hash
            if row["value"] == canonical:
                return candidate_hash
            candidate = (candidate + 1) % HASH_SPACE
        raise RuntimeError("глоссарий: пространство хешей исчерпано — нужна большая длина хеша")

    # ── Чтение ───────────────────────────────────────────────────────────────

    def restore(self, hash_value: str) -> str | None:
        """Вернуть оригинал по хешу. None, если хеша нет."""
        row = self._connection.execute(
            "SELECT value FROM glossary WHERE hash = ?", (hash_value,)
        ).fetchone()
        return row["value"] if row is not None else None

    def get(self, hash_value: str) -> GlossaryEntry | None:
        """Вернуть запись целиком (хеш, оригинал, дата, счётчик)."""
        row = self._connection.execute(
            "SELECT hash, value, first_seen, uses FROM glossary WHERE hash = ?",
            (hash_value,),
        ).fetchone()
        if row is None:
            return None
        return GlossaryEntry(row["hash"], row["value"], row["first_seen"], row["uses"])

    def size(self) -> int:
        """Число записей в глоссарии."""
        return int(self._connection.execute("SELECT COUNT(*) AS c FROM glossary").fetchone()["c"])

    def count_references(self, text: str) -> int:
        """Сколько ссылок `LB?...?` в тексте."""
        return len(REFERENCE_RE.findall(text))

    def expand(self, text: str) -> str:
        """
        Заменить все ссылки в тексте на оригиналы.

        Неизвестная ссылка остаётся как есть: это честнее, чем подставить пустое
        место или выдумку — видно, что значение потеряно.
        """
        def substitute(match: re.Match[str]) -> str:
            original = self.restore(match.group(1))
            return original if original is not None else match.group(0)

        return REFERENCE_RE.sub(substitute, text)

    # ── Замена в тексте ──────────────────────────────────────────────────────

    def replace_in_text(self, text: str, min_uses: int = 2) -> tuple[str, list[str]]:
        """
        Заменить повторяющиеся точные данные ссылками.

        Возвращает (новый текст, список хешей). Список идёт в поле
        `glossary_refs` чанка по схеме core/schemas/chunk.schema.json.

        ПОВТОРЯЮЩИЕСЯ, А НЕ ВСЕ ПОДРЯД. Требование методологии: заменяются
        ПОВТОРЯЮЩИЕСЯ точные данные. Значение, встреченное один раз, выгоднее
        оставить как есть: ссылка не сократит, а читаемость ухудшит.

        Порядок замены — от длинных к коротким: иначе короткий кандидат съест
        часть длинного, и хеш будет не от того, что заменили.
        """
        candidates: dict[str, tuple[str, int]] = {}
        for _kind, pattern in PATTERNS:
            for match in pattern.finditer(text):
                raw = match.group(0)
                canonical = normalize(raw)
                if len(canonical) < self.min_length:
                    continue
                count = candidates.get(canonical, (raw, 0))[1]
                candidates[canonical] = (raw, count + 1)

        repeated = {k: v for k, v in candidates.items() if v[1] >= min_uses}
        if not repeated:
            return text, []

        result = text
        hashes: list[str] = []
        # Сначала длинные: предотвращает частичное перекрытие короткими.
        for canonical in sorted(repeated, key=len, reverse=True):
            original_raw = repeated[canonical][0]
            hash_value = self.add(canonical)
            if hash_value is None:
                continue
            reference = make_reference(hash_value)
            # Меняем вхождения как в исходном обрамлении, так и в каноническом
            # виде: regex нашёл одно, а в тексте встречается и другое.
            #
            # Хеш добавляется в список РОВНО ОДИН РАЗ. Живой урок: добавление
            # внутри цикла по вариантам давало дубликат в glossary_refs —
            # `['64aa0bc9', '8ee75bee']` для одной строки, и схема чанка
            # заполнялась ссылками, которых в тексте нет.
            replaced = False
            for variant in {original_raw, canonical}:
                if variant in result:
                    result = result.replace(variant, reference)
                    replaced = True
            if replaced:
                hashes.append(hash_value)
        return result, hashes

    def close(self) -> None:
        """Закрыть соединение с базой."""
        self._connection.close()

    def __enter__(self) -> "Glossary":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
