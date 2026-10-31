#!/usr/bin/env python3
"""ABC: журнал с точными фактами. Факты НЕПРЕДСКАЗУЕМЫ и не повторяются."""
import hashlib, json, random
from pathlib import Path

random.seed(7)
OUT = Path("/tmp/abc"); OUT.mkdir(exist_ok=True)
WORDS = ["quartz","nimbus","vertex","lumen","onyx","delta","prism",
         "cobalt","helix","zephyr","atlas","ember","basalt","cinder"]

N_FACTS = 1200
PER = N_FACTS // 4          # 75 каждого вида
facts = []

for i in range(1, PER + 1):
    facts.append({"kind":"path","turn":i,
        "value":f"/opt/sv/{WORDS[i%14]}{i}_{random.randint(1000,9999)}/k{i}.conf",
        "q":f"путь {i}-го конфига"})
for i in range(1, PER + 1):
    facts.append({"kind":"sum","turn":PER+i,
        "value":f"{random.randint(1000,9999)}.{random.randint(10,99)}",
        "q":f"сумма в сделке №{i}"})
for i in range(1, PER + 1):
    facts.append({"kind":"hash","turn":2*PER+i,
        "value":hashlib.sha256(f"art-{i}".encode()).hexdigest()[:8],
        "q":f"хеш артефакта {i}"})
for i in range(1, PER + 1):
    facts.append({"kind":"seq","turn":3*PER+i,
        "value":str(100000+i*137+random.randint(0,50)),
        "q":f"seq {i}-го события"})

by_turn = {}
for f in facts: by_turn.setdefault(f["turn"], []).append(f)

lines, seq = [], 0
def add(typ, text):
    global seq; seq += 1
    lines.append(f"[E{len(lines)}] {typ} (seq {seq}): {text}")

add("SYSTEM", "Ты агент-кодер. read вместо cat, glob вместо find, grep вместо shell.")

for t in range(1, 4*PER + 1):
    add("USER", f"Шаг {t}: обработай узел {t}.")
    for f in by_turn.get(t, []):
        k = f["kind"]
        if k == "path":
            add("TOOL_RESULT", f"grep: единственное совпадение в {f['value']}")
        elif k == "sum":
            add("TOOL_RESULT", f"quoteOffer(deal={t-PER}) -> {f['value']} USDT")
        elif k == "hash":
            add("TOOL_RESULT", f"artifact_store: хеш {f['value']}")
        else:
            add("TOOL_RESULT", f"journal: событие seq={f['value']} удалено")

# Шум: НЕ содержит фактов
for t in range(4*PER + 1, 4*PER + 400):
    add("USER", f"Шаг {t}: проверь сборку.")
    add("TOOL_RESULT", f"npm run typecheck -> exit 0; файлов {t*3}; сборка {t%60} с")

journal = "\n".join(lines)
(OUT/"journal.txt").write_text(journal, encoding="utf-8")
(OUT/"questions.json").write_text(
    json.dumps({"questions":[f["q"] for f in facts],"facts":facts},
               ensure_ascii=False, indent=2), encoding="utf-8")
print(f"событий: {len(lines)} | символов: {len(journal)} (≈{len(journal)//4} ток)")
print(f"фактов: {len(facts)} " + " ".join(
    f"{k}={sum(1 for f in facts if f['kind']==k)}" for k in ("path","sum","hash","seq")))
