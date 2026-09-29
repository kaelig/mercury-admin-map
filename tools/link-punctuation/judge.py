# Classify link-punctuation candidates from sweep.sh with Jev (TypeSafe System One). Run: python3 judge.py
import json, glob, os, sys, urllib.request, concurrent.futures as cf
HERE = os.path.dirname(os.path.abspath(__file__))
KEY = next(l.split("=", 1)[1].strip().strip('"\'') for l in open(os.path.join(HERE, "../../.env")) if l.startswith("JEV_API_KEY="))

QUESTIONS = {
    "sentence_punct_in_link": {
        "type": "noul",
        "instructions": "`link.text` is the complete visible text of one clickable element (link or button) on a web page, and `link.context` is the text around it. The element's text ends with a punctuation mark. Is that final punctuation mark really the end of the surrounding sentence or clause (so it belongs outside the clickable element), rather than part of the element's own label?",
        "criteria": {
            "true": "The clickable text is a phrase inside a longer sentence, such as 'Check out our Help Center.' where the link is 'our Help Center.', or a short call to action such as 'Learn more.' or 'View API docs.' that follows a sentence. The period ends the sentence and should not be underlined or clickable.",
            "false": "The punctuation belongs to the label: an abbreviation ('Co.', 'N.A.', 'Inc.', 'no.'), a person's initial ('Alice C.'), or a clickable card or option whose whole label is itself a complete sentence or description.",
        },
    },
    "kind": {
        "type": "choice",
        "instructions": "What kind of clickable text is `link.text`, given `link.context`, `link.element` and `link.display`?",
        "criteria": {
            "inline_link_in_sentence": "A link embedded inside running sentence text, where the link covers the last words of the sentence.",
            "trailing_call_to_action": "A short standalone link or button after a sentence, like 'Learn more.' or 'View docs.'",
            "abbreviation_or_initial": "The label ends in an abbreviation, company suffix or a person's initial.",
            "sentence_as_label": "A card, option or menu item whose whole label or description is a complete sentence.",
            "other": "Anything else, such as a column header or a label ending with a colon.",
        },
    },
}

def ask(h):
    body = {"model": "jev-latest", "state": {"link": {"text": h["text"], "context": h["ctx"], "element": h["tag"] + (f"[role={h['role']}]" if h["role"] else ""), "display": h["display"]}}, "questions": QUESTIONS}
    req = urllib.request.Request("https://api.typesafe.ai/v1/systemone", data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r: a = json.load(r)["answers"]
            return {"p": a["sentence_punct_in_link"]["noul"], "kind": a["kind"]["choice"], "kind_conf": a["kind"]["confidence"]}
        except Exception as e:
            err = str(e)
    return {"error": err}

rows = []
for f in sorted(glob.glob(os.path.join(HERE, "out", "res*.jsonl"))):
    for line in open(f):
        d = json.loads(line)
        for h in d.get("hits", []): rows.append(dict(h, path=d["path"], at=d.get("at"), h1=d.get("h1")))
# de-duplicate identical (text, context) pairs; keep every path they appear on
uniq = {}
for r in rows: uniq.setdefault((r["text"], r["ctx"]), {**r, "paths": []})["paths"].append(r["path"])
cands = list(uniq.values())
with cf.ThreadPoolExecutor(12) as ex:
    for c, j in zip(cands, ex.map(ask, cands)): c.update(j)
json.dump(cands, open(os.path.join(HERE, "out", "judged.json"), "w"), indent=1)
for c in sorted(cands, key=lambda c: -c.get("p", -1)):
    print(f"{c.get('p', float('nan')):.2f} {c.get('kind','?'):24} {c['text']!r:48} {len(c['paths'])}× {c['paths'][0]}")
print(len(rows), "raw hits,", len(cands), "unique")
