"""
SCC — HEAD: служебный раздел перед памятью
============================================

Реализует [`METHODOLOGY_HEAD.md`](../METHODOLOGY_HEAD.md). Состояние методологии:
**ПРИНЯТ** — правка документа без обращения к пользователю запрещена
(`METHODOLOGY_STATE.md`).

ЧТО ТАКОЕ HEAD
---------------
Отдельная единица: служебный текст в начале контекста, **не являющийся
памятью** и **не подверженный преобразованиям** (инвариант 8):

* не сжимается ядром SCC;
* не обрабатывается CAS — ссылки в роли сделали бы её нечитаемой без базы;
* не уходит в архив — иначе перестанет быть видимым;
* не пересказывается.

Три части, порядок фиксирован:

    1. РОЛЬ агента            — правила работы, инструкции по инструментам
    2. Инструкция Архивариуса — как читать архив чанков
    3. Инструкция CAS         — как получать точные данные по CAS?{хеш}?

ЗАЧЕМ ИЗЫМАТЬ HEAD
--------------------
Ядро SCC получает **поток событий** и сжимает его целиком. Если HEAD останется
в потоке, он попадёт внутрь чанка, и тогда роль окажется в архиве, CAS заменит
в ней пути ссылками, а при повторной свёртке HEAD будет сжат вместе с памятью.
**Изъятие — условие корректности, а не оптимизация.**

    from memory.head import extract_head, Head

    head, rest = extract_head(event_stream)   # изъять роль из потока
    context = head.render(last_chunk=3) + medium_text + short_text

ЦЕПОЧКА РАБОТЫ
----------------
    поток событий
        │ extract_head            <- отделяет роль
        ├──> Head                 <- в контекст, ПЕРЕД стаком
        └──> поток без HEAD       <- в ядро SCC

ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ МОДУЛЬ, А НЕ ЧАСТЬ MEMORY
------------------------------------------------
Память отвечает за чанки, HEAD — за то, что **перед** ними. Смешение дало бы
соблазн «сжать заодно и HEAD», от которого методология прямо защищает.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from memory.cas_prompt import build_cas_prompt

# ── Разбор потока событий ────────────────────────────────────────────────────
# Формат события (проверено на выходе core/scc_events.py):
#
#     [E<N>] ТИП (seq <M>):
#     <тело события>
#
# Заголовок начинается с `[E<N>]`, затем ТИП, затем `(seq <M>)`. Тело — всё до
# следующего заголовка.
EVENT_HEAD_RE = re.compile(
    r"^\[E(?P<number>\d+)\]\s+(?P<kind>[A-Z_]+)\s+\(seq\s+(?P<seq>[^)]+)\)\s*:\s*$",
    re.MULTILINE,
)

# Тип события, который считается кандидатом в HEAD.
ROLE_KIND = "SYSTEM"

# ── Сообщение при отсутствии роли ────────────────────────────────────────────
# Молча оставить HEAD без роли нельзя (METHODOLOGY_HEAD.md §3.5): агент будет
# работать без контракта и не поймёт, почему у него не получается. Явное
# сообщение превращает непонятную деградацию в видимую проблему.
NO_ROLE_NOTICE = (
    "[HEAD · РОЛЬ] Роль агента в этом контексте ОТСУТСТВУЕТ: она не найдена в "
    "журнале. Правила работы с инструментами неизвестны. Если агент перестал "
    "вызывать инструменты или нарушает порядок работы — причина в этом: "
    "уточните у пользователя, какой ролью следует руководствоваться."
)


@dataclass
class Head:
    """
    Служебный раздел контекста.

    Собирается из трёх частей; инструкции добавляются при рендере, потому что
    зависят от состояния памяти (номер последнего чанка, наличие ссылок CAS).
    """

    # Часть 1: роль агента. Пустая строка означает «роль не найдена».
    role: str = ""

    # Источник роли: номер события, откуда извлечена. Нужен для отладки и для
    # проверки, что изъятие сработало.
    role_source: int | None = None

    # Части 2 и 3 заполняются при рендере: они зависят от состояния памяти.
    archivist_instruction: str = ""
    cas_instruction: str = ""

    # События, изъятые из потока. Хранятся, чтобы вызывающий видел, что именно
    # ушло в HEAD, а не полагался на «ничего не потеряно».
    extracted: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        """Пуст ли HEAD: нет ни роли, ни инструкций."""
        return not (self.role or self.archivist_instruction or self.cas_instruction)

    def token_count(self) -> int:
        """
        Объём HEAD в токенах (эвристика «4 символа = 1 токен»).

        Считается по всем трём частям: HEAD занимает место в контексте, и это
        место должно быть видно в замерах, а не растворяться в «прочем».
        """
        text = self.render(last_chunk=0)
        return -(-len(text) // 4) if text else 0

    def render(
        self,
        last_chunk: int = 0,
        cas_instruction: str | None = None,
        archivist_instruction: str | None = None,
    ) -> str:
        """
        Собрать текст HEAD для контекста.

        Порядок частей фиксирован методологией (§5): роль, затем инструкция
        Архивариуса, затем инструкция CAS.

        `last_chunk` — номер последнего чанка; 0 означает «архива нет».
        Инструкция Архивариуса при нуле оформляется как отсутствие архива:
        упоминать несуществующий архив вредно — агент начнёт запрашивать то,
        чего нет.

        Пустые части пропускаются: пустой блок в контексте занимает место и
        ничего не сообщает.
        """
        parts: list[str] = []

        # Часть 1: роль. При отсутствии — явное сообщение, а не тишина.
        parts.append(self.role if self.role else NO_ROLE_NOTICE)

        # Часть 2: инструкция Архивариуса.
        archivist = archivist_instruction if archivist_instruction is not None else self.archivist_instruction
        if archivist:
            parts.append(archivist)

        # Часть 3: инструкция CAS.
        cas = cas_instruction if cas_instruction is not None else self.cas_instruction
        if cas:
            parts.append(cas)

        return "\n\n".join(part for part in parts if part)


def split_events(stream: str) -> list[tuple[re.Match[str], str]]:
    """
    Разбить поток на события: (заголовок-совпадение, тело).

    Возвращает список пар, чтобы вызывающий мог собрать поток обратно без
    потери текста. Пустой список означает «событий не найдено» — это НЕ то же
    самое, что «поток пуст», и вызывающий обязан различать.
    """
    matches = list(EVENT_HEAD_RE.finditer(stream))
    events: list[tuple[re.Match[str], str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(stream)
        events.append((match, stream[start:end]))
    return events


def extract_head(
    stream: str,
    cas_instruction: str | None = None,
) -> tuple[Head, str]:
    """
    Изъять HEAD из потока событий.

    Возвращает (Head, поток_без_HEAD). Из потока удаляются **только** события
    `SYSTEM`, стоящие **в начале** (до первого не-SYSTEM события) — см.
    METHODOLOGY_HEAD.md §3.3.

    ПОЧЕМУ ТОЛЬКО В НАЧАЛЕ. В DSH встречается повтор контекста роли в середине
    сессии (наблюдалось `[E1] SYSTEM (seq 10)` после смены каталога). Такой
    повтор — часть истории, и удалять его нельзя: он относится к конкретному
    моменту, а не является ролью.

    Если `SYSTEM` в начале нет — роль не найдена, и Head формируется с пустой
    ролью: при рендере появится явное сообщение (§3.5).

    Пустой поток даёт пустой Head и пустой поток — это не ошибка, но вызывающий
    должен проверить, что события вообще были (`split_events`).
    """
    events = split_events(stream)
    if not events:
        # Событий не найдено. Не выдаём это за «роль отсутствует»: возможно,
        # неверный формат входа, и молчаливое «роль не найдена» скрыло бы
        # настоящую причину.
        return Head(role="", role_source=None), stream

    # Собираем ведущие SYSTEM-события: пока не встретили не-SYSTEM.
    leading: list[tuple[re.Match[str], str]] = []
    for match, body in events:
        if match.group("kind") == ROLE_KIND:
            leading.append((match, body))
        else:
            break

    if not leading:
        return Head(role="", role_source=None), stream

    # Роль — тела ведущих SYSTEM-событий, в исходном порядке. Если их несколько,
    # склеиваем: методология требует сохранить все (§3.3).
    role_parts = [body.strip() for _, body in leading if body.strip()]
    role = "\n\n".join(role_parts)
    role_source = int(leading[0][0].group("number"))

    # Удаляем изъятые события из потока: остаётся всё, начиная с первого
    # не-SYSTEM события. Собираем заново из исходных срезов, а не вырезаем по
    # позициям: позиции смещаются, и вырезание по индексам — источник ошибок.
    first_kept = len(leading)
    rest = "".join(
        f"{match.group(0)}\n{body}"
        for match, body in events[first_kept:]
    )

    head = Head(
        role=role,
        role_source=role_source,
        cas_instruction=cas_instruction or "",
        extracted=[f"E{match.group('number')}" for match, _ in leading],
    )
    return head, rest


def attach_instructions(
    head: Head,
    archivist_instruction: str = "",
    cas_reference_count: int | None = None,
) -> Head:
    """
    Дополнить HEAD инструкциями доступа.

    Инструкция Архивариуса приходит из `LongMemory.instruction()`; она пуста,
    если архива нет. Инструкция CAS формируется здесь, потому что её наличие
    зависит от числа ссылок: при нуле ссылок она не нужна вовсе (вся задача CAS
    в экономии контекста).

    Возвращает тот же Head (мутация допустима: Head — рабочий объект сборки, а
    не неизменяемый артефакт).
    """
    head.archivist_instruction = archivist_instruction
    head.cas_instruction = build_cas_prompt(cas_reference_count)
    return head


def render_context(
    head: Head,
    medium_text: str,
    short_text: str,
    last_chunk: int = 0,
    cas_reference_count: int | None = None,
) -> str:
    """
    Собрать контекст целиком: `[HEAD][стак][короткая]`.

    Порядок — из METHODOLOGY_HEAD.md §5 и METHODOLOGY_MEMORY.md §3:

        HEAD      — первый: формирует поведение до первого шага
        средняя   — стак чанков
        короткая  — несжатый диалог, всегда последний как самый актуальный

    Пустые части пропускаются. Аргумент `cas_reference_count` определяет,
    ставить ли инструкцию CAS: `None` — поставить (число неизвестно), `0` — не
    ставить.
    """
    attach_instructions(
        head,
        archivist_instruction=head.archivist_instruction or "",
        cas_reference_count=cas_reference_count,
    )
    parts = [
        head.render(last_chunk=last_chunk),
        medium_text.strip(),
        short_text.strip(),
    ]
    return "\n\n".join(part for part in parts if part)
