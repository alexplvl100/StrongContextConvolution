"""
SCC — валидация чанка по схеме
================================

Схема [`core/schemas/chunk.schema.json`](../core/schemas/chunk.schema.json) —
**контракт между ядром и памятью**. Этот модуль делает его исполняемым: схема не
лежит мёртвым описанием, а проверяет каждый чанк перед записью.

ПОЧЕМУ СВОЙ ВАЛИДАТОР, А НЕ `jsonschema`
-----------------------------------------
`jsonschema` доступен в окружении разработки, но он не входит в стандартную
библиотеку. Делать его обязательной зависимостью ради семи полей неоправданно:
схема простая, а лишняя зависимость — это лишний способ сломать установку.

Поддерживается нужное подмножество JSON Schema: `type`, `required`,
`properties`, `additionalProperties: false`, `enum`, `minimum`, `pattern`,
`items`, `format: date-time`. Полная спецификация не реализуется намеренно: если
схема усложнится настолько, что этого подмножества не хватит, это будет сигналом
подключить настоящий валидатор, а не расширять самодельный.

ЗАЧЕМ ПРОВЕРЯТЬ ПЕРЕД ЗАПИСЬЮ
-------------------------------
Чанк неизменяем (инвариант 4): записанный однажды, он не перезаписывается. Значит
ошибка в структуре останется в архиве навсегда — исправить её будет нечем. Поэтому
проверка идёт ДО записи, а не после.

    from memory.schema import validate_chunk, SchemaError
    validate_chunk(chunk)      # бросит SchemaError с указанием поля
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "core" / "schemas" / "chunk.schema.json"


class SchemaError(ValueError):
    """Чанк не соответствует схеме. Сообщение называет конкретное поле."""


def load_schema(path: Path | None = None) -> dict:
    """
    Прочитать схему с диска.

    Читается каждый раз, а не кэшируется: схема может быть изменена при
    разработке, и кэш дал бы расхождение между тем, что проверяется, и тем, что
    лежит в файле. Стоимость чтения мала — это происходит один раз на чанк.
    """
    schema_path = SCHEMA_PATH if path is None else path
    try:
        return json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SchemaError(f"схема чанка нечитаема ({schema_path}): {error}") from error


# ── Проверки типов ───────────────────────────────────────────────────────────
_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def _check_type(value: object, expected: str | list[str], where: str) -> None:
    """Проверить тип значения. Список типов допускает любой из них."""
    variants = expected if isinstance(expected, list) else [expected]
    for variant in variants:
        if variant == "integer" and isinstance(value, bool):
            # bool — подкласс int в Python, но в JSON Schema это разные типы.
            continue
        allowed = _TYPES.get(variant)
        if allowed is not None and isinstance(value, allowed):
            return
    raise SchemaError(
        f"{where}: ожидался тип {expected}, получено {type(value).__name__} ({value!r})"
    )


def _check_format(value: object, fmt: str, where: str) -> None:
    """Проверить формат. Поддерживается только date-time."""
    if fmt != "date-time" or not isinstance(value, str):
        return
    try:
        datetime.fromisoformat(value)
    except ValueError as error:
        raise SchemaError(f"{where}: не ISO 8601 ({value!r}): {error}") from error


def validate(value: object, schema: dict, where: str = "чанк") -> None:
    """
    Проверить значение по подсхеме рекурсивно.

    `where` — путь до проверяемого места, чтобы сообщение называло конкретное
    поле, а не «чанк неверен» целиком.
    """
    if "type" in schema:
        _check_type(value, schema["type"], where)

    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{where}: значение {value!r} не входит в {schema['enum']}")

    if "const" in schema and value != schema["const"]:
        raise SchemaError(f"{where}: ожидалось {schema['const']!r}, получено {value!r}")

    if "minimum" in schema and isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema["minimum"]:
            raise SchemaError(f"{where}: {value} меньше минимума {schema['minimum']}")

    if "pattern" in schema and isinstance(value, str):
        if not re.search(schema["pattern"], value):
            raise SchemaError(f"{where}: {value!r} не соответствует шаблону {schema['pattern']!r}")

    if "format" in schema:
        _check_format(value, schema["format"], where)

    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                raise SchemaError(f"{where}: отсутствует обязательное поле {name!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                raise SchemaError(f"{where}: неизвестные поля {sorted(extra)}")
        for name, subschema in properties.items():
            if name in value:
                validate(value[name], subschema, f"{where}.{name}")

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            validate(item, schema["items"], f"{where}[{index}]")


def validate_chunk(chunk: object, path: Path | None = None) -> None:
    """
    Проверить чанк по схеме. Бросает SchemaError при несоответствии.

    Вызывается перед записью чанка: из-за неизменяемости (инвариант 4) ошибка в
    структуре осталась бы в архиве навсегда.
    """
    validate(chunk, load_schema(path))


def is_valid_chunk(chunk: object, path: Path | None = None) -> bool:
    """Проверить чанк без исключения. True, если соответствует схеме."""
    try:
        validate_chunk(chunk, path)
        return True
    except SchemaError:
        return False
