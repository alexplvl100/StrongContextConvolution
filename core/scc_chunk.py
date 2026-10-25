#!/usr/bin/env python3
"""
SCC — чанкование: изъятие свёрнутого ядра и сшивка с новым чанком
==================================================================

ЧТО ЭТО (идея заказчика)
------------------------
Свёртка сильного контекста (strong-context convolution, SCC) проходит в три
этапа и на each итерации помечает КАЖДОЕ событие маркером `(_SSC_)`. Маркер —
это не украшение, а признак: «это событие уже прошло свёртку, оно неприкосновенно».

Тогда повторная свёртка перестаёт переписывать ядро целиком. Вместо этого:

    итерация 2:
      1. из контекста АЛГОРИТМИЧЕСКИ (этим скриптом) изымаются все блоки
         с маркером `(_SSC_)`            -> SSC_chunk_1  (ядро, не трогаем)
      2. СВЕЖАЯ часть (без маркера) проходит SCC (LLM, этапы I–III)
                                        -> SSC_chunk_2  (новое ядро)
      3. chunk_1 и chunk_2 СШИВАЮТСЯ     -> новый контекст
      4. работа продолжается; на следующей итерации всё повторяется

Смысл: **ядро не теряется**. Обычная компакция DSH пересказывает старую сводку
заново, и факты вымываются поколениями (замерено: 129 фактов первой сводки -> 0
к 83-й). SCC сохраняет chunk_1 дословно, потому что к нему не прикасается.

ПОЧЕМУ ЭТО СКРИПТ, А НЕ LLM
---------------------------
Границы чанков обязан определять детерминированный код. Если поручить
«изыми свёрнутое» языковой модели, она перескажет и ядро — то есть сделает ровно
ту ошибку, от которой схема и защищает. Скрипт режет по маркеру и не ошибается.

КОДЫ ВОЗВРАТА (соглашение SeverPact)
------------------------------------
    0 — операция выполнена
    1 — нарушение целостности: найден блок без маркера, или маркер разорван
    2 — НЕ ИЗМЕРЕНО (файла нет / пуст / нечитаем). Это НЕ «чисто».

Использование:
    # изъять свёрнутое ядро в отдельный файл, свежее — в другой
    python3 scc_chunk.py split <scc.md> --core core.md --fresh fresh.md

    # сшить ядро и новый чанк в один контекст
    python3 scc_chunk.py stitch <core.md> <chunk2.md> --out merged.md

    # проверить целостность маркировки
    python3 scc_chunk.py check <scc.md>

    # сколько уже свёрнуто и сколько свежего
    python3 scc_chunk.py stats <scc.md>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── Маркер свёрнутого события ────────────────────────────────────────────────
# Формат строго `(_SSC_)` — так его ставит III этап. Регистр фиксирован: если
# разрешить варианты, разбор станет неоднозначным, а неоднозначный разбор в
# границе чанка означает потерю данных.
SSC_MARK = "(_SSC_)"

# ── Заголовок события ────────────────────────────────────────────────────────
# Событие начинается с номера E<N> (или диапазона E<N>–E<M>), двоеточия и, у
# свёрнутых, маркера. Диапазоны — норма: этап I сворачивает однотипные действия
# («E4–E13: прочитаны файлы»), и разворачивать их запрещено.
#
# ЖИВОЙ УРОК (замер 2026-10-24, прогон SCC на реальной сессии):
# на большом контексте (892 события) модель сама перешла на Markdown-разметку
# заголовков: `**E0: SYSTEM (seq 7)** — ...` вместо `E0: SYSTEM (seq 7): ...`.
# Строгий разбор видел 0 событий из 891 и возвращал код 2 — то есть молча терял
# всё ядро. Поэтому необязательная обёртка `**` допускается: она не меняет
# смысл заголовка.
#
# Регулярка задаёт только НАЧАЛО события: тело берётся срезом по позиции, чтобы
# разбор оставался дословным и не зависел от формата остатка строки.
EVENT_RE = re.compile(
    r"^(?P<bold>\*\*)?"                        # необязательная markdown-обёртка
    r"(?P<label>E\d+(?:\s*[–\-]\s*E\d+)?)"     # E4  |  E4–E13
    r"\s*:\s*"
    r"(?P<mark>\(_SSC_\))?",                   # маркер, если уже свёрнуто
    re.MULTILINE,
)


@dataclass
class Event:
    """Одно событие SCC: заголовок, тело, признак свёрнутости."""

    label: str
    body: str
    marked: bool
    start: int
    end: int

    @property
    def text(self) -> str:
        """Событие как оно лежит в файле, без завершающих пустых строк."""
        return self.body.rstrip("\n")


@dataclass
class Parsed:
    """Результат разбора SCC-файла."""

    preamble: str = ""
    events: list[Event] = field(default_factory=list)
    unmarked: list[Event] = field(default_factory=list)

    @property
    def core(self) -> list[Event]:
        """Свёрнутые события — неприкосновенное ядро."""
        return [e for e in self.events if e.marked]

    @property
    def fresh(self) -> list[Event]:
        """Несвёрнутые события — то, что ещё предстоит свернуть."""
        return [e for e in self.events if not e.marked]


def read_text(path: Path) -> str | None:
    """Прочитать файл; None — если нечитаем (это код 2, а не «чисто»)."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def parse(text: str) -> Parsed:
    """
    Разобрать SCC-файл на события, сохранив их точные границы.

    Тело события тянется до следующего заголовка `E<N>:` или до конца файла.
    Всё, что стоит до первого события, сохраняется как преамбула: её нельзя
    терять, там обычно шапка свёртки и пояснения процесса.
    """
    matches = list(EVENT_RE.finditer(text))
    parsed = Parsed()

    if not matches:
        parsed.preamble = text
        return parsed

    parsed.preamble = text[: matches[0].start()]
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        event = Event(
            label=match.group("label").strip(),
            body=text[match.start():end],
            marked=match.group("mark") is not None,
            start=match.start(),
            end=end,
        )
        parsed.events.append(event)
        if not event.marked:
            parsed.unmarked.append(event)
    return parsed


def render_core(parsed: Parsed) -> str:
    """Собрать файл ядра: преамбула плюс все свёрнутые события по порядку."""
    parts = []
    if parsed.preamble.strip():
        parts.append(parsed.preamble.rstrip("\n"))
    parts.extend(e.text for e in parsed.core)
    return "\n\n".join(parts) + "\n" if parts else ""


def render_fresh(parsed: Parsed) -> str:
    """Собрать файл свежей части: только несвёрнутые события."""
    parts = [e.text for e in parsed.fresh]
    return "\n\n".join(parts) + "\n" if parts else ""


def stitch(core_text: str, chunk_text: str) -> str:
    """
    Сшить ядро и новый чанк в один контекст.

    Порядок важен: ядро идёт ПЕРВЫМ, потому что оно описывает более ранние
    события — хронология должна сохраниться, иначе свёртка перестанет быть
    вертикальной последовательностью.
    """
    left = core_text.strip("\n")
    right = chunk_text.strip("\n")
    if not left:
        return right + "\n" if right else ""
    if not right:
        return left + "\n"
    return left + "\n\n" + right + "\n"


def cmd_split(args: argparse.Namespace) -> int:
    """Изъять ядро и свежую часть в отдельные файлы."""
    source = Path(args.source)
    text = read_text(source)
    if text is None:
        print(f"НЕ ИЗМЕРЕНО: не читается {source}", file=sys.stderr)
        return 2

    parsed = parse(text)
    core_path = Path(args.core)
    fresh_path = Path(args.fresh)
    core_path.write_text(render_core(parsed), encoding="utf-8")
    fresh_path.write_text(render_fresh(parsed), encoding="utf-8")

    print(f"источник:   {source}")
    print(f"событий:    {len(parsed.events)}  (свёрнуто {len(parsed.core)}, свежих {len(parsed.fresh)})")
    print(f"ядро:       {core_path}  ({len(render_core(parsed))} символов)")
    print(f"свежее:     {fresh_path}  ({len(render_fresh(parsed))} символов)")
    return 0 if parsed.events else 1


def cmd_stitch(args: argparse.Namespace) -> int:
    """Сшить два файла в один контекст."""
    core = read_text(Path(args.core))
    chunk = read_text(Path(args.chunk))
    if core is None or chunk is None:
        missing = args.core if core is None else args.chunk
        print(f"НЕ ИЗМЕРЕНО: не читается {missing}", file=sys.stderr)
        return 2

    merged = stitch(core, chunk)
    Path(args.out).write_text(merged, encoding="utf-8")
    print(f"ядро:    {len(core)} символов")
    print(f"чанк:    {len(chunk)} символов")
    print(f"сшито:   {args.out}  ({len(merged)} символов)")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """
    Проверить целостность маркировки.

    Гейт отвечает на один вопрос: **есть ли события без маркера**. Событие без
    маркера означает, что III этап его не пометил, и на следующей итерации оно
    не попадёт в ядро — то есть будет свёрнуто повторно и может потеряться.
    """
    source = Path(args.source)
    text = read_text(source)
    if text is None:
        print(f"НЕ ИЗМЕРЕНО: не читается {source}", file=sys.stderr)
        return 2

    parsed = parse(text)
    if not parsed.events:
        print("НЕ ИЗМЕРЕНО: событий E<N> не найдено", file=sys.stderr)
        return 2

    unmarked = parsed.unmarked
    if unmarked:
        print(f"НАРУШЕНИЕ: {len(unmarked)} событий без маркера {SSC_MARK}", file=sys.stderr)
        for event in unmarked[:10]:
            first = event.body.splitlines()[0] if event.body else ""
            print(f"  {first[:100]}", file=sys.stderr)
        if len(unmarked) > 10:
            print(f"  ... ещё {len(unmarked) - 10}", file=sys.stderr)
        return 1

    # Маркер должен стоять ровно один раз — НЕПОСРЕДСТВЕННО в заголовке.
    #
    # Живой урок (замер 2026-10-24, прогон на реальной сессии): событие E681
    # цитировало текст задачи — «постфикс `(_SSC_)`» — и наивная проверка
    # «сколько раз маркер встречается в первой строке» дала ложное нарушение.
    # Правильный признак двойной простановки: маркер идёт СРАЗУ за маркером
    # (`E0: (_SSC_) (_SSC_) SYSTEM`), а не упоминается где-то в описании.
    doubled = []
    for event in parsed.events:
        first_line = event.body.split("\n", 1)[0]
        after_label = first_line.split(":", 1)[1] if ":" in first_line else ""
        stripped = after_label.strip()
        # отрезаем один легитимный маркер и смотрим, не начинается ли следующий
        if stripped.startswith(SSC_MARK):
            stripped = stripped[len(SSC_MARK):].strip()
        if stripped.startswith(SSC_MARK):
            doubled.append(event)
    if doubled:
        print(f"НАРУШЕНИЕ: маркер продублирован в {len(doubled)} событиях", file=sys.stderr)
        for event in doubled[:5]:
            print(f"  {event.body.splitlines()[0][:100]}", file=sys.stderr)
        return 1

    print(f"ЧИСТО: {len(parsed.events)} событий, все помечены {SSC_MARK}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Показать, сколько уже свёрнуто и сколько свежего."""
    source = Path(args.source)
    text = read_text(source)
    if text is None:
        print(f"НЕ ИЗМЕРЕНО: не читается {source}", file=sys.stderr)
        return 2

    parsed = parse(text)
    core_len = len(render_core(parsed))
    fresh_len = len(render_fresh(parsed))
    report = {
        "file": str(source),
        "events_total": len(parsed.events),
        "core_events": len(parsed.core),
        "fresh_events": len(parsed.fresh),
        "core_chars": core_len,
        "fresh_chars": fresh_len,
        "marked_ratio": round(len(parsed.core) / len(parsed.events), 3) if parsed.events else 0.0,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"файл:    {source}")
        print(f"событий: {report['events_total']} (ядро {report['core_events']}, свежих {report['fresh_events']})")
        print(f"символов: ядро {core_len}, свежее {fresh_len}")
        print(f"свёрнуто: {report['marked_ratio'] * 100:.1f}%")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Собрать CLI с подкомандами split/stitch/check/stats."""
    parser = argparse.ArgumentParser(
        description="SCC: чанкование контекста по маркеру (_SSC_)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    split = sub.add_parser("split", help="изъять ядро и свежую часть")
    split.add_argument("source", help="SCC-файл")
    split.add_argument("--core", required=True, help="куда писать ядро (свёрнутое)")
    split.add_argument("--fresh", required=True, help="куда писать свежую часть")
    split.set_defaults(func=cmd_split)

    stitch_p = sub.add_parser("stitch", help="сшить ядро и новый чанк")
    stitch_p.add_argument("core", help="файл ядра")
    stitch_p.add_argument("chunk", help="файл нового чанка")
    stitch_p.add_argument("--out", required=True, help="куда писать результат")
    stitch_p.set_defaults(func=cmd_stitch)

    check = sub.add_parser("check", help="проверить маркировку")
    check.add_argument("source", help="SCC-файл")
    check.set_defaults(func=cmd_check)

    stats = sub.add_parser("stats", help="показать статистику чанков")
    stats.add_argument("source", help="SCC-файл")
    stats.add_argument("--json", action="store_true", help="машинный вывод")
    stats.set_defaults(func=cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI."""
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
