"""
SCC — CAS: хранилище точных данных с адресацией по содержимому
================================================================

CAS (content-addressed store) — хранилище, где **адрес записи вычисляется из её
содержимого**: `sha256(value)[:8]`. Повторяющиеся точные данные в чанке
заменяются ссылками вида `CAS?{хеш}?`, оригиналы лежат в SQLite. Механизм описан
в METHODOLOGY_CAS.md, раздел 7 — прямой доступ к значениям.

ЧТО ЭТО ФАКТИЧЕСКИ
-------------------
Не «глоссарий» и не «библиотека». Глоссарий даёт **определения терминов**, а
здесь толкования нет: значение возвращается дословно, тем же самым. Библиотека
предполагает **каталог и классификацию** объектов, имеющих самостоятельную
ценность, а здесь плоская таблица «адрес → строка», и строка ценна лишь тем,
что на неё ссылается чанк.

Точное имя — **content-addressed store**: адрес есть функция данных,
восстановление побайтовое, смысл значения для хранилища безразличен.

    from memory.cas import ContentAddressedStore
    cas = ContentAddressedStore(Path("runtime/dialogs/d1/cas.db"))
    text, hashes = cas.replace_in_text("путь /home/user/x.yml и снова /home/user/x.yml")
    cas.restore(hashes[0])        # -> "/home/user/x.yml"

БЕЗ LLM
--------
Весь модуль — детерминированная обработка текста: нормализация, sha256, замена,
SQLite. Языковая модель здесь не участвует и не должна (инвариант 3 в
METHODOLOGY_MEMORY.md). Импортов LLM-клиентов в файле нет — это проверяется
тестом.

ДОСТУП НАПРЯМУЮ, МИНУЯ АРХИВАРИУС
-----------------------------------
Основной агент получает оригинал по ссылке **прямым вызовом функции**
(`restore` / `expand`), а не через Архивариуса. Причина: Архивариус — агент с
LLM, и его пересказ внёс бы искажение в точное значение. Точные данные обязаны
возвращаться побайтово, поэтому доступ к CAS идёт в обход любой модели.
См. METHODOLOGY_CAS.md §7.

ПОЧЕМУ ХЕШ, А НЕ ИНДЕКС
------------------------
Индекс (`CAS?1?`, `CAS?2?`) короче, но привязан к порядку добавления: скопировали
чанк в другой диалог — ссылки указывают не туда. Хеш от содержимого переносим:
одна и та же строка в любом диалоге даёт одну и ту же ссылку.

СТАБИЛЬНОСТЬ
-------------
Одна и та же строка обязана давать один и тот же хеш — иначе восстановление
невозможно. Поэтому нормализация (снятие кавычек, пробелов) выполняется ДО
хеширования и зафиксирована тестом.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ── Формат ссылки ────────────────────────────────────────────────────────────
# `CAS?{8 hex}?` — 13 символов. Префикс CAS — по утверждённому термину проекта.
# $ — конец строки-якорь: без него `re.search` нашёл бы `CAS?` внутри слова.
REFERENCE_PREFIX = "CAS?"
REFERENCE_SUFFIX = "?"
# Длина хеша в ссылке — от 8 до 64 символов. Восемь базовая; больше при
# коллизии, когда адрес расширяется (см. COLLISION_HASH_LENGTHS). Регулярка
# обязана принимать обе, иначе расширенный адрес станет НЕВИДИМ для системы:
# ссылка в чанке не распознается, значение не разрешится. Живой урок
# 2026-10-26 — на этом сначала спотыкались.
REFERENCE_RE = re.compile(r"CAS\?([0-9a-f]{8,64})\?")

# ── Хеш ──────────────────────────────────────────────────────────────────────
HASH_HEX_LENGTH = 8
HASH_SPACE = 16**HASH_HEX_LENGTH

# ── Длины хеша при коллизии ──────────────────────────────────────────────────
# Базовая длина 8 (32 бита, ~4 млрд значений). При совпадении адреса берётся
# БОЛЬШЕ символов того же sha256 — адрес остаётся функцией содержимого.
# Сдвиг адреса (hash+1) запрещён: он привязал бы адрес к порядку добавления,
# и ссылки в уже записанных чанках перестали бы разрешаться.
# Шаг 2 hex-символа: +8 бит на попытку, полный sha256 достигается к 64.
COLLISION_HASH_LENGTHS: tuple[int, ...] = tuple(range(HASH_HEX_LENGTH + 2, 65, 2))

# ── Что считаем точными данными ──────────────────────────────────────────────
# Список ОТКРЫТЫЙ (METHODOLOGY_CAS.md §2). Регулярки ищут кандидатов; длину
# отсекает cas_min_length из конфига.
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
class CasEntry:
    """Запись CAS: хеш, оригинал, когда добавлен, сколько раз встретился."""

    hash: str
    value: str
    first_seen: str
    uses: int


def normalize(value: str) -> str:
    """
    Привести значение к канонической форме ПЕРЕД хешированием.

    Правила (METHODOLOGY_CAS.md §5): снять обрамляющие пробелы, кавычки и
    бэктики, схлопнуть внутренние пробелы. Регистр НЕ трогаем: в путях и
    идентификаторах он значим.
    """
    result = value.strip(_STRIP_CHARS)
    return re.sub(r"\s+", " ", result)


def compute_hash(value: str, length: int = HASH_HEX_LENGTH) -> str:
    """
    Хеш нормализованной строки: первые `length` hex-символов sha256.

    Стабильность обязательна (одна строка -> один хеш). Это ЧИСТАЯ функция:
    результат зависит только от входа и длины, но НЕ от состояния базы и не от
    порядка добавления. На этом свойстве держится весь CAS.

    `length` больше базовой используется только при коллизии: берётся больше
    символов того же хеша, а не другое значение.
    """
    if length < HASH_HEX_LENGTH or length > 64:
        raise ValueError(
            f"длина хеша обязана быть в диапазоне {HASH_HEX_LENGTH}..64, получено {length}"
        )
    digest = hashlib.sha256(normalize(value).encode("utf-8")).hexdigest()
    return digest[:length]


def make_reference(hash_value: str) -> str:
    """Собрать ссылку из хеша: `CAS?a1b2c3d4?`."""
    return f"{REFERENCE_PREFIX}{hash_value}{REFERENCE_SUFFIX}"


class ContentAddressedStore:
    """
    База точных данных одного диалога.

    Файл БД лежит в runtime/dialogs/<dialog_id>/cas.db. Каталог создаётся
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
        """Создать таблицу, если её нет. Схема — из METHODOLOGY_CAS.md §6."""
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS cas (
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

        КОЛЛИЗИИ. Если базовая длина хеша занята ДРУГОЙ строкой, хеш РАСШИРЯЕТСЯ
        (берётся больше символов того же sha256). Сдвиг адреса запрещён — он
        привязал бы адрес к порядку добавления.

        ЖИВОЙ УРОК (2026-10-26). Прежняя проверка «значение уже есть» смотрела
        ТОЛЬКО базовую длину. Если значение ранее получило расширенный адрес
        из-за коллизии, повторный `add` не находил его и падал на вставке с
        `UNIQUE constraint failed: cas.hash`. Поэтому теперь ищется по всем
        длинам: сначала базовая, затем расширенные.
        """
        canonical = normalize(value)
        if len(canonical) < self.min_length:
            return None

        existing_hash = self._find_existing(canonical)
        if existing_hash is not None:
            # Значение уже есть: увеличиваем счётчик, адрес не меняем.
            self._connection.execute(
                "UPDATE cas SET uses = uses + 1 WHERE hash = ?", (existing_hash,)
            )
            self._connection.commit()
            return existing_hash

        hash_value = compute_hash(canonical)
        row = self._connection.execute(
            "SELECT value FROM cas WHERE hash = ?", (hash_value,)
        ).fetchone()
        if row is not None:
            # Базовая длина занята другим значением — расширяем адрес.
            hash_value = self._resolve_collision(canonical, hash_value)

        self._connection.execute(
            "INSERT INTO cas (hash, value, first_seen, uses) VALUES (?, ?, ?, 1)",
            (hash_value, canonical, datetime.now(timezone.utc).isoformat()),
        )
        self._connection.commit()
        return hash_value

    def _find_existing(self, canonical: str) -> str | None:
        """
        Найти запись для значения по ВСЕМ возможным длинам хеша.

        Возвращает адрес, под которым значение уже лежит, или None. Нужен,
        чтобы повторное добавление не создавало дубликат и не падало на
        ограничении уникальности: значение, получившее расширенный адрес при
        коллизии, обязано находиться и при базовой длине.
        """
        for length in (HASH_HEX_LENGTH, *COLLISION_HASH_LENGTHS):
            candidate = compute_hash(canonical, length)
            row = self._connection.execute(
                "SELECT value FROM cas WHERE hash = ?", (candidate,)
            ).fetchone()
            if row is not None and row["value"] == canonical:
                return candidate
        return None

    def _resolve_collision(self, canonical: str, start_hash: str) -> str:
        """
        Разрешить коллизию РАСШИРЕНИЕМ хеша, а не сдвигом.

        ЖИВОЙ УРОК (найдено при тестировании коллизий 2026-10-26).
        Прежняя реализация брала следующий свободный адрес (`hash+1`). Это
        ломало ГЛАВНЫЙ инвариант CAS: адрес перестаёт быть функцией от
        содержимого и начинает зависеть от ПОРЯДКА добавления. Проверено:

            база A: сначала v2, потом v1  ->  адрес v1 = fe6e2882
            база B: сначала v1, потом v2  ->  адрес v1 = fe6e2881

        Следствие тяжёлое: ссылка `CAS?fe6e2882?` в уже записанном чанке при
        пересборке базы (или в другом диалоге) перестала бы разрешаться —
        там v1 получил бы fe6e2881. Чанк неизменяем (инвариант 4), значит
        исправить ссылку в нём нечем: значение потеряно навсегда.

        ПРАВИЛЬНО: взять БОЛЬШЕ символов того же sha256. Адрес остаётся чистой
        функцией содержимого, а вероятность повторной коллизии падает с каждым
        шагом (8 hex = 32 бита, 10 = 40, 12 = 48, ...).

        Почему это безопасно для записанных ссылок: базовая длина 8 не
        меняется, расширяются только конфликтующие записи. Ссылки вида
        `CAS?xxxxxxxx?` разрешаются как прежде.
        """
        import sys

        print(
            f"CAS: КОЛЛИЗИЯ для {canonical[:60]!r}: {start_hash} занят другим значением. "
            "Расширяю хеш — сдвиг адреса запрещён, он сломал бы привязку к содержимому",
            file=sys.stderr,
        )
        for length in COLLISION_HASH_LENGTHS:
            candidate_hash = compute_hash(canonical, length)
            row = self._connection.execute(
                "SELECT value FROM cas WHERE hash = ?", (candidate_hash,)
            ).fetchone()
            if row is None:
                return candidate_hash
            if row["value"] == canonical:
                # Значение уже есть под этим адресом — расширение не нужно.
                return candidate_hash
        # Практически недостижимо: полный sha256 в hex — 64 символа.
        raise RuntimeError(
            "CAS: коллизия не разрешилась даже на полной длине sha256 — "
            "это означает повреждение базы, а не совпадение хешей"
        )

    # ── Чтение ───────────────────────────────────────────────────────────────

    def restore(self, hash_value: str) -> str | None:
        """Вернуть оригинал по хешу. None, если хеша нет."""
        row = self._connection.execute(
            "SELECT value FROM cas WHERE hash = ?", (hash_value,)
        ).fetchone()
        return row["value"] if row is not None else None

    def get(self, hash_value: str) -> CasEntry | None:
        """Вернуть запись целиком (хеш, оригинал, дата, счётчик)."""
        row = self._connection.execute(
            "SELECT hash, value, first_seen, uses FROM cas WHERE hash = ?",
            (hash_value,),
        ).fetchone()
        if row is None:
            return None
        return CasEntry(row["hash"], row["value"], row["first_seen"], row["uses"])

    def size(self) -> int:
        """Число записей в CAS."""
        return int(self._connection.execute("SELECT COUNT(*) AS c FROM cas").fetchone()["c"])

    def count_references(self, text: str) -> int:
        """Сколько ссылок `CAS?...?` в тексте."""
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

    def resolve_many(self, hashes: list[str]) -> dict[str, str]:
        """
        ПРЯМОЙ ДОСТУП: разрешить список ссылок одним запросом.

        Это тот самый механизм, которым основной агент получает точные данные
        **напрямую из CAS, минуя Архивариуса**. Причина обязательного обхода:
        Архивариус — агент с LLM, и его пересказ внёс бы искажение в значение.
        Точные данные обязаны возвращаться побайтово, поэтому путь к ним не
        проходит ни через одну модель.

        Возвращает словарь «хеш → оригинал». Чего нет в базе — в словаре
        отсутствует: подставить пустое или выдуманное значило бы потерять
        значение незаметно, а так вызывающий видит пробел.

        Один SQL-запрос вместо N: список ссылок в чанке обычно длиннее одной, и
        построчный обход был бы лишними обращениями к диску.
        """
        if not hashes:
            return {}
        unique = list(dict.fromkeys(hashes))
        placeholders = ",".join("?" * len(unique))
        rows = self._connection.execute(
            f"SELECT hash, value FROM cas WHERE hash IN ({placeholders})", unique
        ).fetchall()
        return {row["hash"]: row["value"] for row in rows}

    def references_in(self, text: str) -> list[str]:
        """
        Список ссылок в тексте, в порядке появления, без повторов.

        Нужен агенту, чтобы понять, какие значения доступны: он видит в чанке
        `CAS?{хеш}?`, собирает их этим методом и запрашивает у CAS напрямую.
        """
        return list(dict.fromkeys(REFERENCE_RE.findall(text)))

    # ── Замена в тексте ──────────────────────────────────────────────────────

    def replace_in_text(self, text: str, min_uses: int = 2) -> tuple[str, list[str]]:
        """
        Заменить повторяющиеся точные данные ссылками.

        Возвращает (новый текст, список хешей). Список идёт в поле
        `cas_refs` чанка по схеме core/schemas/chunk.schema.json.

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
            # внутри цикла по вариантам давало дубликат в cas_refs —
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

    def __enter__(self) -> "ContentAddressedStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
