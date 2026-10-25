#!/usr/bin/env python3
"""
SCC — прогон трёх этапов свёртки через LLM
===========================================

ЧТО ЭТО
-------
Свёртка сильного контекста (strong-context convolution, SCC) — это три
последовательных прохода языковой модели по журналу работы агента:

    этап I   — журнал событий -> вертикальный WR (каркас, без точных значений)
    этап II  — WR + журнал    -> WR_FILLED (сырые ТОЧНЫЕ ДАННЫЕ)
    этап III — WR + WR_FILLED -> SCC-файл (мерж: описания + данные, маркер _SSC_)

Этот скрипт запускает любой из трёх этапов по имени. Промты лежат в
`prompts/` отдельными файлами: их правят люди, а не код, и каждая правка
промта — это правка методологии, а не рефакторинг.

ЗАЧЕМ ТРИ ЭТАПА, А НЕ ОДИН
--------------------------
Замерено на реальном журнале (T2, 159 событий, 760 172 символа):

    один проход  ->  сводка 3 568 символов, но НОЛЬ значений из 9 182
    три прохода  ->  43 события со структурой И 87 216 символов данных

Причина: модель не может одновременно «сжать смысл» и «скопировать значения».
Это разные режимы работы. Разделение на проходы даёт каждому свой режим.

ЧЕГО SCC НЕ МОЖЕТ (проверено, не предположение)
-----------------------------------------------
Несжимаемые значения (64-символьные хеши, дампы, ключи) через LLM не проходят:
модель либо искажает их посимвольно, либо отказывается копировать. Замерено:
9 182 хеша на входе -> 0 на выходе, при точности 100% на именах файлов и путях.

Вывод методологии: значения такого рода обязаны жить ВНЕ контекста (SQLite,
файл), а в SCC остаётся ССЫЛКА на них. Это следующая итерация доработки.

КОДЫ ВОЗВРАТА (соглашение SeverPact)
------------------------------------
    0 — этап выполнен, артефакт записан
    1 — нарушение: этап не дал результата или результат пуст
    2 — НЕ ИЗМЕРЕНО: нет промта, нет входа, нет ключа, сеть недоступна

Использование:
    python3 scc_run.py stage1 <журнал.txt> --out WR.md
    python3 scc_run.py stage2 <журнал.txt> <WR.md> --out WR_FILLED.md
    python3 scc_run.py stage3 <WR.md> <WR_FILLED.md> --out SCC.md
    python3 scc_run.py stage3 <WR.md> <WR_FILLED.md> --out SCC.md --no-ssc
    python3 scc_run.py --list-prompts
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ── Расположение промтов и настройки по умолчанию ────────────────────────────
HERE = Path(__file__).resolve().parent
# Промты лежат РЯДОМ с ядром: core/prompts (путь относительно core/, не корня).
PROMPTS = HERE / "prompts"

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_BASE_URL = "https://routerai.ru/api/v1"
DEFAULT_TIMEOUT = 1800
API_KEY_ENV = "ROUTERAI_API_KEY"

# Потолок вывода одного этапа.
#
# Живой урок: без явного значения RouterAI брал 131 072 (2^17), и этапы II/III
# обрывались на 93% и 82%. Значение 262 144 проверено и принимается.
DEFAULT_MAX_TOKENS = 262144

# ── Автоповтор ───────────────────────────────────────────────────────────────
# Генерация стохастична: тот же вход даёт то норму, то мусор. Замерено — прогон
# этапа II с идентичными параметрами вернул 0 событий, а повтор на том же входе
# дал 892. Без повтора цепочка требует человека там, где достаточно повторить,
# а потеря артефакта означает потерю части контекста.
DEFAULT_RETRIES = 2

# Во сколько раз поднимать потолок при обрыве по `finish_reason=length`.
# Обрыв лечится именно потолком, поэтому он растёт только на этой причине.
ESCALATE = 1.5

# ── Порог покрытия ───────────────────────────────────────────────────────────
# Минимальная доля событий входа, которая обязана попасть в артефакт.
# Живой урок (дефект 5): модель обработала 51 событие из 891 и остановилась,
# предложив «продолжать батчами». Обе прежние проверки это пропускали.
# Порог мягкий: часть событий модель вправе свернуть в диапазоны, поэтому
# допускается просадка. 0.9 отсекает самоограничение, не мешая свёртке.
COVERAGE_MIN = 0.9

# Предел разворачивания диапазона при подсчёте покрытия (защита от цитат).
MAX_RANGE = 100000

# Каждый этап: файл промта и сколько документов он принимает.
STAGES: dict[str, dict[str, object]] = {
    "stage1": {
        "prompt": "stage1_wr.md",
        "inputs": 1,
        "input_names": ("ЖУРНАЛ",),
        "ssc": False,
    },
    "stage2": {
        "prompt": "stage2_filled.md",
        "inputs": 2,
        "input_names": ("ДОКУМЕНТ 1 — ЖУРНАЛ", "ДОКУМЕНТ 2 — WR"),
        "ssc": False,
    },
    "stage3": {
        "prompt": "stage3_merge.md",
        "inputs": 2,
        "input_names": ("ДОКУМЕНТ A — WR (каркас с описаниями)", "ДОКУМЕНТ B — WR_FILLED (точные данные)"),
        "ssc": True,
    },
}


def read_text(path: Path) -> str | None:
    """Прочитать файл; None — если нечитаем."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def call_llm(
    prompt: str,
    model: str,
    base_url: str,
    api_key: str,
    timeout: int,
    max_tokens: int | None = None,
) -> dict[str, object] | None:
    """
    Вызвать OpenAI-совместимый endpoint одним сообщением пользователя.

    Возвращает разобранный ответ либо None при сетевой ошибке. Системного
    сообщения нет намеренно: роль задаётся телом промта, чтобы этап можно было
    воспроизвести в любом клиенте без скрытого состояния.

    `max_tokens` передаётся ЯВНО, и это не мелочь. Живой урок (замер
    2026-10-24): без этого параметра RouterAI брал дефолт 131 072 (2¹⁷), и
    этапы II и III обрывались на 93 % и 82 % — итоговый SCC терял сплошной
    хвост событий. Потолок оказался не свойством модели: значение 262 144 и
    384 000 принимаются. Проверять обрыв обязательно по `finish_reason`:
    значение `length` означает усечение, и такой результат нельзя считать
    полным.
    """
    body: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")[:600]
        print(f"ОШИБКА HTTP {error.code}: {body}", file=sys.stderr)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        print(f"ОШИБКА СЕТИ: {type(error).__name__}: {error}", file=sys.stderr)
        return None
    except http.client.HTTPException as error:
        # Живой урок (замер 2026-10-24): большой ответ приходит chunked, и
        # соединение может оборваться на середине — `IncompleteRead`. Он
        # наследуется от `HTTPException`, а НЕ от `OSError`, поэтому падал мимо
        # обработчика и ронял весь прогон вместо повтора. Ровно тот случай, ради
        # которого автоповтор и сделан: вызов надо просто повторить.
        print(
            f"ОШИБКА СЕТИ (обрыв потока): {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return None


def build_prompt(stage: str, docs: list[str], ssc: bool) -> str | None:
    """
    Собрать полный промт этапа: шаблон из prompts/ плюс подставленные документы.

    Маркер `{{INPUT_n}}` в шаблоне — место подстановки. Шаблон обязан содержать
    ровно столько мест, сколько документов принимает этап, иначе это код 2:
    молча подставить не туда значит испортить артефакт.
    """
    spec = STAGES[stage]
    template = read_text(PROMPTS / str(spec["prompt"]))
    if template is None:
        print(f"НЕ ИЗМЕРЕНО: нет промта {PROMPTS / str(spec['prompt'])}", file=sys.stderr)
        return None

    # Для этапа III маркер _SSC_ можно отключить — нужен для сравнения схем.
    if stage == "stage3" and not ssc:
        template = template.replace(
            "E<N>: (_SSC_) <описание события из ДОКУМЕНТА A, дословно>",
            "E<N>: <описание события из ДОКУМЕНТА A, дословно>",
        )
        template = template.replace(
            '1. КАЖДОЕ событие ОБЯЗАНО начинаться с постфикса-маркера (_SSC_) сразу после\n'
            "   номера и двоеточия. Ни одно событие не должно остаться без него.\n"
            '   Пример: "E0: (_SSC_) SYSTEM (роль): ..."\n',
            "",
        )

    result = template
    for index, doc in enumerate(docs, start=1):
        placeholder = f"{{{{INPUT_{index}}}}}"
        if placeholder not in result:
            print(f"НЕ ИЗМЕРЕНО: в промте нет места {placeholder}", file=sys.stderr)
            return None
        result = result.replace(placeholder, doc)
    return result


def count_events(content: str) -> int:
    """
    Сосчитать ЗАГОЛОВКИ событий `E<N>` в артефакте этапа.

    Это признак того, что артефакт вообще является SCC-файлом.
    `finish_reason == "stop"` его не заменяет: замерено, что модель может
    вернуть `stop` и при этом ноль событий, уйдя в другую роль.

    Для проверки ПОКРЫТИЯ эта функция не годится — там нужен
    {@link count_covered_events}: заголовок `E4–E13` один, а событий покрывает
    десять.
    """
    return len(re.findall(r"^(?:\*\*)?E\d+", content, re.M))


def count_covered_events(content: str) -> int:
    """
    Сосчитать, сколько УНИКАЛЬНЫХ событий покрыто документом.

    Зачем не «число заголовков». Заголовок `E4–E13` — это ОДНА строка, но она
    покрывает десять событий: на T2 заголовков 43, а покрыто 160. Обратная
    ошибка тоже возможна: если диапазон ЦИТИРУЕТСЯ в тексте, а рядом стоят
    отдельные события того же промежутка, суммирование даст двойной счёт —
    замерено на потоке, 998 совпадений вместо 893 событий.

    Поэтому считаются уникальные номера: диапазон разворачивается в набор,
    повторные упоминания одного номера схлопываются.

    Понимает оба формата: `[E4]` потока и `E4:` артефакта, включая
    Markdown-обёртку `**` и диапазоны `E4–E13`, `E4-E13`, `E4 - E13`.
    """
    numbers: set[int] = set()
    for match in re.finditer(
        r"^(?:\*\*)?\[?(?:\*\*)?E(\d+)(?:\s*[–\-]\s*E(\d+))?",
        content,
        re.M,
    ):
        first = int(match.group(1))
        second = int(match.group(2)) if match.group(2) else first
        if second < first:
            numbers.add(first)
            continue
        # Санитарный предел: цитата вида `E1–E999999` не должна разворачиваться
        # в огромное множество и съедать память.
        if second - first > MAX_RANGE:
            numbers.add(first)
            numbers.add(second)
            continue
        numbers.update(range(first, second + 1))
    return len(numbers)


def count_source_events(content: str) -> int:
    """
    Сосчитать события во ВХОДНОМ документе — потоке или WR.

    Вход бывает двух форматов: поток событий нумерует их как `[E1]`, а WR — как
    `E1:`. Счётчик обязан понимать оба, иначе проверка покрытия сравнивала бы
    несравнимое. Диапазоны учитываются: они покрывают все свои события.
    """
    return count_covered_events(content)


def coverage_ratio(source_events: int, output_events: int) -> float:
    """Доля событий входа, попавшая в вывод. 1.0 — покрытие полное."""
    if source_events <= 0:
        return 1.0
    return output_events / source_events


def attempt_once(
    prompt: str,
    args: argparse.Namespace,
    api_key: str,
    max_tokens: int,
    source_events: int = 0,
) -> tuple[str, str | None, dict[str, object]]:
    """
    Выполнить одну попытку вызова и вернуть (содержимое, диагноз, usage).

    Диагноз — None, если попытка пригодна, иначе текст нарушения. Разбор
    нарушения здесь, а не у вызывающего: решение о повторе зависит от вида
    сбоя (обрыв лечится поднятием потолка, мусор — простым повтором).

    `source_events` включает проверку ПОКРЫТИЯ: сколько событий входа дошло до
    вывода. Без неё проходит дефект, найденный замером 2026-10-24: модель
    выполнила работу лишь для первых 51 события из 891 и предложила «продолжать
    батчами по твоей команде» — формально непустой артефакт, фактически
    потеря 94 % контекста.
    """
    response = call_llm(
        prompt, args.model, args.base_url, api_key, args.timeout, max_tokens
    )
    if response is None:
        return "", "сетевой отказ или отказ API", {}

    choices = response.get("choices") or []
    if not choices:
        return "", "пустой ответ без choices", {}

    content = (choices[0].get("message") or {}).get("content") or ""
    usage = response.get("usage") or {}
    finish = choices[0].get("finish_reason")

    if not content.strip():
        return content, "ответ пуст", usage

    # Обрыв по потолку вывода — артефакт НЕПОЛОН.
    # Живой урок: молчаливо усечённый этап терял сплошной хвост событий
    # (замерено: 93 % и 82 %). Лечится поднятием потолка на следующей попытке.
    if finish == "length":
        return content, "обрыв по потолку вывода (finish_reason=length)", usage

    # Содержательная проверка: артефакт обязан быть SCC-файлом с событиями.
    # Живой урок (2026-10-24): модель ушла в роль агента и выдала bash-команды
    # вместо извлечения — 0 событий из 892 при `finish_reason=stop`.
    events = count_events(content)
    if events == 0:
        return content, "в выводе нет событий E<N> (уход в другую роль)", usage

    # Проверка покрытия: артефакт обязан покрывать почти весь вход.
    # Живой урок (2026-10-24, дефект 5): модель обработала 51 событие из 891 и
    # остановилась со словами «продолжение — по твоей команде батчами». Обе
    # прежние проверки это пропускали: события есть, обрыва нет. Между тем это
    # потеря 94 % контекста — ровно то, от чего SCC и защищает.
    #
    # Порог мягкий (не 100 %): модель вправе свернуть часть событий в диапазоны,
    # поэтому допускается просадка до COVERAGE_MIN.
    if source_events > 0:
        covered = count_covered_events(content)
        ratio = coverage_ratio(source_events, covered)
        if ratio < COVERAGE_MIN:
            return (
                content,
                f"неполное покрытие: покрыто {covered} событий из {source_events} "
                f"({ratio * 100:.0f} % < {COVERAGE_MIN * 100:.0f} %); "
                f"заголовков в выводе {events}",
                usage,
            )

    return content, None, usage


def run_with_retries(
    prompt: str,
    args: argparse.Namespace,
    api_key: str,
    source_events: int = 0,
) -> tuple[str, str | None, list[dict[str, object]]]:
    """
    Выполнить этап с автоповтором и вернуть (лучшее содержимое, диагноз, журнал).

    ЗАЧЕМ ПОВТОР. Генерация стохастична: один и тот же вход даёт то норму, то
    мусор. Замерено — прогон этапа II с идентичными параметрами вернул 0 событий,
    а повтор на том же входе дал 892. Без повтора цепочка требует человека там,
    где достаточно просто попробовать снова, — а потеря артефакта означает потерю
    части контекста, что дороже лишнего вызова.

    СТРАТЕГИЯ:
      * мусор (нет событий) -> повтор с тем же потолком;
      * обрыв по потолку    -> повтор с потолком, умноженным на ESCALATE;
      * сохраняется ЛУЧШАЯ попытка (по числу событий), а не последняя — иначе
        удачный прогон затрётся неудачным;
      * каждая попытка логируется с числом событий и потолком.

    Возвращает диагноз последней значимой неудачи либо None при успехе.
    """
    retries = max(0, int(args.retries))
    max_tokens = int(args.max_tokens)
    best_content = ""
    best_events = -1
    best_diag: str | None = "ни одной попытки не выполнено"
    log: list[dict[str, object]] = []

    for attempt in range(1, retries + 2):
        content, diag, usage = attempt_once(
            prompt, args, api_key, max_tokens, source_events
        )
        events = count_events(content)
        out_tokens = (usage or {}).get("completion_tokens")
        log.append(
            {
                "attempt": attempt,
                "max_tokens": max_tokens,
                "events": events,
                "chars": len(content),
                "completion_tokens": out_tokens,
                "diagnosis": diag,
            }
        )

        marker = "ок" if diag is None else "СБОЙ"
        # В транзитном режиме прогресс уходит в stderr: stdout занят артефактом.
        # Иначе строка «попытка 1/3» попадала в начало выдаваемого документа.
        noise = sys.stderr if getattr(args, "to_stdout", False) else sys.stdout
        print(
            f"  попытка {attempt}/{retries + 1}: {marker} — событий {events}, "
            f"{len(content)} символов, потолок {max_tokens}"
            + (f", выход {out_tokens} ток" if out_tokens else ""),
            file=noise,
        )
        if diag:
            print(f"    причина: {diag}", file=noise)

        # Лучшей считаем попытку с наибольшим числом событий. При равенстве
        # приоритет у более полной по объёму: события могли быть те же, но
        # данные — обрезаны.
        if events > best_events or (events == best_events and len(content) > len(best_content)):
            best_content, best_events, best_diag = content, events, diag

        if diag is None:
            return best_content, None, log

        # Обрыв лечится потолком; мусор — повтором. Поднимаем только на обрыве,
        # иначе потолок рос бы без причины.
        if "обрыв по потолку" in diag:
            max_tokens = int(max_tokens * ESCALATE)
        if attempt <= retries:
            print("  повтор...")

    return best_content, best_diag, log


def cmd_stage(args: argparse.Namespace) -> int:
    """Выполнить один этап свёртки."""
    spec = STAGES[args.stage]

    # Куда девать результат: ровно одно из двух. Без проверки запуск без --out
    # упал бы на Path(None) с невнятной ошибкой, а запуск с обоими флагами
    # записал бы файл в транзитном режиме — то есть нарушил бы запрет хранения.
    if not args.to_stdout and not args.out:
        print(
            "НЕ ИЗМЕРЕНО: укажите --out <файл> или --to-stdout (транзитный режим без записи)",
            file=sys.stderr,
        )
        return 2
    if args.to_stdout and args.out:
        print(
            "НАРУШЕНИЕ: --to-stdout и --out вместе. В транзитном режиме файл не создаётся: "
            "либо вывод получателю, либо запись на диск",
            file=sys.stderr,
        )
        return 1

    expected = int(spec["inputs"])
    if len(args.inputs) != expected:
        names = ", ".join(str(n) for n in spec["input_names"])
        print(
            f"НЕ ИЗМЕРЕНО: этап {args.stage} принимает {expected} документ(а): {names}",
            file=sys.stderr,
        )
        return 2

    docs = []
    for path_str in args.inputs:
        text = read_text(Path(path_str))
        if text is None:
            print(f"НЕ ИЗМЕРЕНО: не читается {path_str}", file=sys.stderr)
            return 2
        docs.append(text)

    api_key = os.environ.get(API_KEY_ENV) or args.api_key or ""
    if not api_key:
        print(
            f"НЕ ИЗМЕРЕНО: нет ключа. Задайте ${API_KEY_ENV} или --api-key",
            file=sys.stderr,
        )
        return 2

    ssc = bool(spec["ssc"]) and not args.no_ssc
    prompt = build_prompt(args.stage, docs, ssc)
    if prompt is None:
        return 2

    # Диагностика. В транзитном режиме уходит в stderr: stdout отдан
    # получателю под артефакт, и служебные строки в нём недопустимы. Живой
    # урок 2026-10-26 — без этого артефакт начинался с «этап: / промт: / вход:»,
    # и получатель не мог отделить данные от служебных сообщений.
    noise = sys.stderr if args.to_stdout else sys.stdout
    print(f"этап:   {args.stage}", file=noise)
    print(f"промт:  {PROMPTS / str(spec['prompt'])}", file=noise)
    print(f"вход:   {', '.join(f'{p} ({len(d)} симв)' for p, d in zip(args.inputs, docs))}", file=noise)
    print(f"промт целиком: {len(prompt)} символов", file=noise)
    print(f"модель: {args.model}", file=noise)
    print(f"потолок: {args.max_tokens}, попыток: {int(args.retries) + 1}", file=noise)
    print("вызов...", file=noise)

    # События входа нужны для проверки покрытия: артефакт обязан покрывать
    # почти весь вход, иначе это самоограничение, а не свёртка.
    source_events = max(count_source_events(d) for d in docs)
    content, diagnosis, log = run_with_retries(prompt, args, api_key, source_events)

    # Пишем ЛУЧШУЮ попытку даже при провале. Причина: частичный артефакт
    # (например, события 1..827) полезнее пустоты — его можно допрогнать или
    # сшить с ядром, а пустой файл не даёт ничего. Но код возврата обязан
    # сообщить о неполноте.
    #
    # ТРАНЗИТНЫЙ РЕЖИМ (--to-stdout): результат уходит в stdout и НЕ пишется на
    # диск. Нужен там, где хранить контекст запрещено: приняли,
    # преобразовали, отдали. Файл в промежутке — это и есть хранение, поэтому
    # он не создаётся вовсе, а не удаляется после.
    #
    # Диагностика при этом уходит в stderr: иначе она смешалась бы с артефактом
    # в stdout, и получатель не смог бы отделить данные от служебных сообщений.
    if args.to_stdout:
        sys.stdout.write(content)
        sys.stdout.flush()
    else:
        out_path = Path(args.out)
        out_path.write_text(content, encoding="utf-8")

    if diagnosis is not None:
        print(
            f"НАРУШЕНИЕ: этап не выполнен за {len(log)} попыт(ок). "
            f"Записана лучшая: {count_events(content)} событий, {len(content)} символов. "
            f"Причина: {diagnosis}",
            file=sys.stderr,
        )
        print("  Артефакт НЕПОЛОН — не использовать без проверки.", file=sys.stderr)
        return 1

    # Отчёт о выходе: в транзитном режиме файла нет, и называть его нельзя —
    # прежде здесь стояло обращение к out_path, из-за чего прогон с
    # --to-stdout падал с UnboundLocalError УЖЕ ПОСЛЕ выдачи артефакта.
    # Получатель данные бы получил, а код возврата сообщил бы о сбое.
    destination = "stdout (транзит, на диск не записано)" if args.to_stdout else str(out_path)
    print(f"выход:  {destination}  ({len(content)} символов, ~{len(content) // 4} токенов)",
          file=sys.stderr if args.to_stdout else sys.stdout)
    print(f"событий: {count_events(content)}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """Показать этапы и наличие их промтов."""
    for name, spec in STAGES.items():
        prompt_path = PROMPTS / str(spec["prompt"])
        exists = "есть" if prompt_path.exists() else "НЕТ"
        print(f"{name}: {prompt_path.name} [{exists}], документов на входе: {spec['inputs']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Собрать CLI."""
    parser = argparse.ArgumentParser(description="SCC: прогон этапов свёртки контекста")
    parser.add_argument(
        "--list-prompts",
        action="store_true",
        help="показать этапы и наличие их промтов",
    )
    sub = parser.add_subparsers(dest="command")

    for stage in STAGES:
        stage_parser = sub.add_parser(stage, help=f"выполнить {stage}")
        stage_parser.add_argument("inputs", nargs="+", help="входные документы")
        stage_parser.add_argument(
            "--out",
            help="куда писать артефакт (не нужен при --to-stdout)",
        )
        stage_parser.add_argument(
            "--to-stdout",
            action="store_true",
            help="вывести артефакт в stdout и НЕ писать на диск (транзитный режим)",
        )
        stage_parser.add_argument("--model", default=DEFAULT_MODEL, help="модель")
        stage_parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="endpoint")
        stage_parser.add_argument("--api-key", default="", help=f"ключ (или ${API_KEY_ENV})")
        stage_parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="таймаут, с")
        stage_parser.add_argument(
            "--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
            help="потолок вывода этапа (обрыв ловится по finish_reason)",
        )
        stage_parser.add_argument(
            "--retries", type=int, default=DEFAULT_RETRIES,
            help="дополнительных попыток при мусорном выводе (0 — без повтора)",
        )
        stage_parser.add_argument("--no-ssc", action="store_true", help="не ставить маркер (_SSC_)")
        # Имя этапа храним явно: у подкоманд своё пространство имён, и без этого
        # cmd_stage не знает, какой этап выполнять.
        stage_parser.set_defaults(func=cmd_stage, stage=stage)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "list_prompts", False):
        return cmd_list(args)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
