#!/usr/bin/env python3
"""
SCC — проверка ПРИГОДНОСТИ ЯДРА: можно ли продолжить работу по SCC
====================================================================

ЧТО ИЗМЕРЯЕТСЯ
--------------
Полнота ядра (891 из 892 событий) и его пригодность — РАЗНЫЕ вещи. Полнота
говорит «ничего не потеряно», пригодность — «по этому можно работать дальше».
Этот тест отвечает на второй вопрос, и от ответа зависит, стоит ли развивать
схему.

МЕТОД
-----
Агенту даётся ТОЛЬКО артефакт (SCC, WR или сырой поток — что проверяем) и
задаются вопросы о фактах, которые заведомо есть в исходном журнале. Ответы
сверяются с эталоном, извлечённым из СЫРОГО журнала напрямую — то есть
проверяется не память, а извлекаемость факта из артефакта.

ПОЧЕМУ ЭТО ЧЕСТНЫЙ ТЕСТ
-----------------------
1. Эталон берётся из журнала, а не из головы проверяющего.
2. Агент не имеет доступа к оригиналу: только артефакт в промте.
3. Вопросы двух типов:
   * ФАКТ — точное значение (путь, число, идентификатор);
   * СВЯЗЬ — что за чем и почему (требует понимания, а не поиска строки).
4. Считаются ОТКАЗЫ отдельно от ОШИБОК: «не знаю» — это честный ответ и
   полезный сигнал, а выдумка — вредный.

ЧТО ЗНАЧИТ РЕЗУЛЬТАТ
--------------------
  высокий факт + высокий связь -> ядро пригодно, схему развивать
  высокий факт + низкий связь  -> ядро это справочник, а не рабочая память
  низкий факт                  -> ядро не годится, даже как справочник

Использование:
    python3 tools/scc_usability.py <артефакт> --label "SCC" [--json]
Коды возврата: 0 — измерено, 1 — нарушение (нет артефакта/ответов), 2 — НЕ ИЗМЕРЕНО.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_BASE_URL = "https://routerai.ru/api/v1"
API_KEY_ENV = "ROUTERAI_API_KEY"

# ── Эталон: факты, извлечённые из СЫРОГО журнала сессии ──────────────────────
# Каждый вопрос: (id, тип, вопрос, эталонные фрагменты ответа).
# Эталон задан набором допустимых подстрок: агент может ответить по-разному,
# важно чтобы ключевое значение присутствовало дословно.
QUESTIONS: tuple[dict[str, object], ...] = (
    # --- ФАКТ: точные значения ---
    {"id": "F1", "type": "факт",
     "q": "Какое значение contextWindow (размер окна) используется для модели в этой работе? Назови число.",
     "expect": ["1000000", "1 000 000", "1M", "1048576"]},
    {"id": "F2", "type": "факт",
     "q": "Какое значение headroomTokens задано по умолчанию в плагине компакции? Назови число.",
     "expect": ["65536", "65 536"]},
    {"id": "F3", "type": "факт",
     "q": "Как называется маркер, которым помечаются свёрнутые события? Приведи его дословно.",
     "expect": ["(_SSC_)"]},
    {"id": "F4", "type": "факт",
     "q": "В каком файле профиля DSH была сделана правка contextWindow? Приведи полный путь.",
     "expect": ["~/.dsh/profiles/web/cordis.patch.yml", "cordis.patch.yml"]},
    {"id": "F5", "type": "факт",
     "q": "Как называется репозиторий GitHub, куда пушились результаты? Укажи владельца и имя.",
     "expect": ["alexplvl100/MultiAgentDSH", "MultiAgentDSH"]},
    {"id": "F6", "type": "факт",
     "q": "Как называется папка в репозитории, куда сложены скрипты, тесты и методология?",
     "expect": ["STRONG_CONTEXT_CONVOLUTION"]},
    {"id": "F7", "type": "факт",
     "q": "Какая модель LLM использовалась для прогонов свёртки? Назови идентификатор.",
     "expect": ["deepseek/deepseek-v4.1-flash", "deepseek-v4.1-flash"]},
    {"id": "F8", "type": "факт",
     "q": "Какие два значения порога компакции назывались — старое (при окне 262144) и новое (после правки)? Назови числа.",
     "expect": ["196608", "196 608", "800000", "800 000"]},
    # --- СВЯЗЬ: понимание, а не поиск строки ---
    {"id": "L1", "type": "связь",
     "q": "ПОЧЕМУ правка contextWindow потребовалась? Что было причиной и что изменилось в поведении компакции?",
     "expect": ["260000", "262144", "фолбэк", "1000000", "порог", "196608", "800000", "дефолт", "каталог"]},
    {"id": "L2", "type": "связь",
     "q": "ПОЧЕМУ границы чанков режет скрипт, а не языковая модель? Какой замер это обосновал?",
     "expect": ["пересказ", "ядро", "хеш", "9182", "9 182", "ноль", "потер"]},
    {"id": "L3", "type": "связь",
     "q": "ЧТО происходит с несжимаемыми значениями (хешами) при свёртке и почему это считается правильным?",
     "expect": ["отсеива", "стохастик", "фильтр", "энтроп", "смысл", "шум"]},
    {"id": "L4", "type": "связь",
     "q": "ПОЧЕМУ свёртка делается в ТРИ этапа, а не одним проходом модели? Что теряется при одном проходе?",
     "expect": ["пересказ", "структур", "точные пути", "хронологи", "сводк", "секц"]},
    {"id": "L5", "type": "связь",
     "q": "ЗАЧЕМ нужен маркер (_SSC_) и что происходит с помеченными событиями при повторной свёртке?",
     "expect": ["изыма", "ядро", "дословн", "не трога", "неприкоснов", "сшива"]},
    {"id": "L6", "type": "связь",
     "q": "ПОЧЕМУ на T2 (159 событий) сжатие получилось сильнее, чем на живой сессии? Что определило разницу?",
     "expect": ["однотип", "диапазон", "группиров", "разнород", "159", "892", "чтени"]},
)


def call_llm(prompt: str, model: str, base_url: str, api_key: str, timeout: int) -> dict | None:
    """Один вызов LLM. Возвращает разобранный ответ или None."""
    payload = json.dumps(
        {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 8192}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except Exception as error:  # noqa: BLE001 — диагностика важнее типа
        print(f"ОШИБКА: {type(error).__name__}: {error}", file=sys.stderr)
        return None


def build_prompt(artifact: str, questions: list[dict]) -> str:
    """Собрать промт: артефакт плюс вопросы. Никакого доступа к оригиналу."""
    listing = "\n".join(f"{q['id']}: {q['q']}" for q in questions)
    return f"""Ниже — документ, описывающий ранее выполненную работу агента.
Кроме этого документа у тебя НЕТ доступа к первоисточнику: ни к журналу сессии,
ни к файлам проекта. Отвечай ТОЛЬКО по этому документу.

ДОКУМЕНТ:
{artifact}

ВОПРОСЫ:
{listing}

ФОРМАТ ОТВЕТА — строго одна строка на вопрос, без вступлений и пояснений:
<ID>: <ответ>

ПРАВИЛА:
- Если ответа в документе нет — напиши "<ID>: НЕ ЗНАЮ". Это нормальный и
  полезный ответ; выдумывать запрещено.
- На вопросы типа "почему/зачем/как" отвечай по существу, 1–3 предложения.
- Не пересказывай документ целиком и не добавляй выводов."""


def parse_answers(content: str, questions: list[dict]) -> dict[str, str]:
    """Вытащить ответы по ID, устойчиво к формату строки."""
    answers: dict[str, str] = {}
    for question in questions:
        qid = str(question["id"])
        pattern = rf"^{re.escape(qid)}\s*[:.\-]\s*(.*)$"
        match = re.search(pattern, content, re.M | re.I)
        if match:
            answers[qid] = match.group(1).strip()
            continue
        # запасной вариант: ID упомянут в строке
        match = re.search(rf"\b{re.escape(qid)}\b[^\n]*?[:.\-]\s*(.+)", content)
        answers[qid] = match.group(1).strip() if match else ""
    return answers


def verdict(answer: str, expect: list[str]) -> str:
    """
    Оценить ответ: ВЕРНО, ОТКАЗ или НЕВЕРНО.

    Отказ («НЕ ЗНАЮ» и синонимы) отделяется от ошибки намеренно: честное
    незнание — полезный сигнал о пробеле в ядре, а выдумка — вредный.

    Живой урок (первый прогон этого теста): отказ нельзя искать подстрокой по
    всему ответу. Модель написала «провайдер routerai ОТСУТСТВУЕТ в каталоге
    pi-ai (…), поэтому взялся фолбэк» — это развёрнутое объяснение причины, но
    детектор по подстроке «отсутств» объявил отказ. Ответ на 400 символов не
    может быть отказом.

    Поэтому отказ — это КОРОТКИЙ ответ, начинающийся с формулы незнания, либо
    явное «НЕ ЗНАЮ» в начале. Всё остальное оценивается по существу.
    """
    text = answer.strip()
    lowered = text.lower()
    if not text:
        return "ПУСТО"

    # Нормализация чисел: «9 182», «9,182» и «9182» — одно и то же значение.
    # Живой урок: без этого ответ «хеши: 9 182 → 0» не совпадал с эталоном
    # «9182» и оценивался как НЕВЕРНО.
    def normalize(value: str) -> str:
        return re.sub(r"(?<=\d)[\s,](?=\d)", "", value)

    flat = normalize(lowered)

    # Проверяем СНАЧАЛА соответствие — оно важнее формы.
    for fragment in expect:
        if normalize(fragment.lower()) in flat:
            return "ВЕРНО"

    # Признаки отказа: короткий ответ с формулой незнания в начале.
    refusal_head = re.match(
        r"^\s*(не знаю|неизвестн|нет в документ|не указан|не найден|"
        r"отсутствует в документ|затрудняюсь|не могу ответить|нет данных)",
        lowered,
    )
    if refusal_head and len(text) < 120:
        return "ОТКАЗ"
    # Явный отказ в чистом виде, даже если ответ длиннее
    if re.fullmatch(r"[\s\S]{0,40}не знаю[\s\S]{0,40}", lowered):
        return "ОТКАЗ"
    return "НЕВЕРНО"


def main(argv: list[str] | None = None) -> int:
    """Запустить проверку пригодности."""
    parser = argparse.ArgumentParser(description="SCC: пригодность ядра")
    parser.add_argument("artifact", help="файл артефакта (SCC, WR, поток)")
    parser.add_argument("--label", default="артефакт", help="имя для отчёта")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--retries", type=int, default=2, help="дополнительных попыток")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    args = parser.parse_args(argv)

    path = Path(args.artifact)
    if not path.exists():
        print(f"НЕ ИЗМЕРЕНО: нет файла {path}", file=sys.stderr)
        return 2
    artifact = path.read_text(encoding="utf-8")
    if not artifact.strip():
        print(f"НЕ ИЗМЕРЕНО: пустой файл {path}", file=sys.stderr)
        return 2

    api_key = os.environ.get(API_KEY_ENV, "")
    if not api_key:
        print(f"НЕ ИЗМЕРЕНО: нет ${API_KEY_ENV}", file=sys.stderr)
        return 2

    questions = list(QUESTIONS)
    prompt = build_prompt(artifact, questions)
    print(f"артефакт: {args.label} ({len(artifact)} символов)", file=sys.stderr)
    print(f"вопросов: {len(questions)} ({sum(1 for q in questions if q['type'] == 'факт')} факт, "
          f"{sum(1 for q in questions if q['type'] == 'связь')} связь)", file=sys.stderr)
    print("вызов...", file=sys.stderr)

    # Автоповтор: тот же урок, что и в scc_run.py. Замерено — модель тратит
    # весь бюджет вывода на reasoning и возвращает пустой content при
    # finish_reason=stop. Это стохастика, и вызов надо просто повторить.
    content = ""
    response: dict | None = None
    for attempt in range(1, args.retries + 2):
        response = call_llm(prompt, args.model, args.base_url, api_key, args.timeout)
        if response is None:
            print(f"  попытка {attempt}: сетевой отказ", file=sys.stderr)
            continue
        choices = response.get("choices") or []
        content = (choices[0].get("message") or {}).get("content") or "" if choices else ""
        answered = len(re.findall(r"^\s*[FL]\d+\s*[:.\-]", content, re.M))
        print(f"  попытка {attempt}: ответов {answered} из {len(questions)}", file=sys.stderr)
        if answered >= len(questions) // 2:
            break
        if attempt <= args.retries:
            print("  повтор...", file=sys.stderr)

    if response is None:
        print("НАРУШЕНИЕ: все попытки провалились (сеть)", file=sys.stderr)
        return 1
    answered = len(re.findall(r"^\s*[FL]\d+\s*[:.\-]", content, re.M))
    if answered < len(questions) // 2:
        # При --json отдаём валидный JSON даже на провале: вызывающий скрипт
        # должен получать разбираемый результат, а не пустой файл. Живой урок:
        # провальный прогон писал 0 байт, и json.load падал с «Expecting value».
        if args.json:
            print(json.dumps({
                "label": args.label,
                "artifact_chars": len(artifact),
                "error": f"получено {answered} ответов из {len(questions)}",
                "rows": [],
            }, ensure_ascii=False, indent=2))
        print(
            f"НАРУШЕНИЕ: получено {answered} ответов из {len(questions)} — "
            "артефакт или модель не дали результата",
            file=sys.stderr,
        )
        return 1

    answers = parse_answers(content, questions)
    rows = []
    for question in questions:
        qid = str(question["id"])
        answer = answers.get(qid, "")
        rows.append({
            "id": qid,
            "type": question["type"],
            "verdict": verdict(answer, list(question["expect"])),
            "answer": answer[:160],
        })

    result = {
        "label": args.label,
        "artifact_chars": len(artifact),
        "usage": response.get("usage") or {},
        "rows": rows,
    }
    if args.json:
        # При --json в stdout идёт ТОЛЬКО JSON: сводка ниже печатается в stderr,
        # иначе вывод нельзя разобрать машинно (живой урок: json.load падал с
        # «Extra data» на строках сводки).
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.json:
        print()
        print(f"{'ID':>4} {'тип':>7} {'вердикт':>9}  ответ")
        print("-" * 78)
        for row in rows:
            print(f"{row['id']:>4} {row['type']:>7} {row['verdict']:>9}  {row['answer'][:52]}")

    # Сводка по типам
    def rate(kind: str) -> tuple[int, int]:
        subset = [r for r in rows if r["type"] == kind]
        return sum(1 for r in subset if r["verdict"] == "ВЕРНО"), len(subset)

    facts_ok, facts_all = rate("факт")
    links_ok, links_all = rate("связь")
    refusals = sum(1 for r in rows if r["verdict"] == "ОТКАЗ")
    wrong = sum(1 for r in rows if r["verdict"] == "НЕВЕРНО")

    stream = sys.stderr if args.json else sys.stdout
    print(file=stream)
    print("=" * 78, file=stream)
    print(f"ФАКТ  (точные значения): {facts_ok}/{facts_all}", file=stream)
    print(f"СВЯЗЬ (понимание):       {links_ok}/{links_all}", file=stream)
    print(f"отказов: {refusals}   неверных: {wrong}", file=stream)
    print("=" * 78, file=stream)

    if facts_all and links_all:
        fr = facts_ok / facts_all
        lr = links_ok / links_all
        if fr >= 0.75 and lr >= 0.75:
            print("ВЕРДИКТ: ядро пригодно — факты и связи на месте, схему развивать", file=stream)
        elif fr >= 0.75:
            print("ВЕРДИКТ: ядро годится как СПРАВОЧНИК, но не как рабочая память", file=stream)
        else:
            print("ВЕРДИКТ: ядро НЕ ПРИГОДНО даже как справочник", file=stream)

    # Отказы при полном ядре — отдельный сигнал: данные есть, но не извлекаются
    if refusals > 0:
        print(f"ВНИМАНИЕ: {refusals} отказ(ов) — факт может быть в артефакте, "
              "но не находится вопросом", file=stream)
    return 0


if __name__ == "__main__":
    sys.exit(main())
