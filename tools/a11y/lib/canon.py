"""Canonical JSON, name templates and hashes, which mirror lib/canon.mjs byte for byte (schemas.md),
and the small helpers every Python stage shares (the last section)."""
import hashlib
import json
import math
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
A11Y = os.path.join(ROOT, "tools", "a11y")


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _round(x):
    # floor(x * 1e4 + 0.5) / 1e4 is what Math.round does in canon.mjs; Python's round() differs.
    return math.floor(x * 10000 + 0.5) / 10000


def normalize(v):
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise ValueError("non-finite number")
        r = _round(v)
        if r == 0:
            return 0
        return int(r) if r == int(r) else r
    if isinstance(v, dict):
        return {str(k): normalize(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [normalize(x) for x in v]
    raise TypeError(f"cannot serialize {type(v).__name__}")


def canonical(obj):
    return json.dumps(normalize(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def dump(obj):
    return json.dumps(normalize(obj), sort_keys=True, ensure_ascii=False, indent=1, separators=(",", ": ")) + "\n"


def sha(obj):
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


def sha1(obj):
    return hashlib.sha1(canonical(obj).encode()).hexdigest()


def write_text(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_json(path, obj):
    write_text(path, dump(obj))


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_jsonl(path, objs):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for o in objs:
            f.write(canonical(o) + "\n")


# ---------------------------------------------------------------------------
# Name templates (KTD15). Every pattern is ASCII-only so \b and \d behave as in JavaScript.
# ---------------------------------------------------------------------------

_F = re.ASCII
_WS = re.compile("[ \t\n\r\f\v  -   　]+")
_MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
          r"|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?")
_UNIT = r"(?:second|minute|hour|day|week|month|year)s?"
_STEPS = [
    (re.compile(r"\b(?:today|yesterday|tomorrow)\b", _F | re.I), "<date>"),
    (re.compile(r"\b(?:\d+|an?) " + _UNIT + r" ago\b", _F | re.I), "<date>"),
    (re.compile(r"\bin (?:\d+|an?) " + _UNIT + r"\b", _F | re.I), "<date>"),
    (re.compile(r"\b(?:last|next) (?:week|month|year)\b", _F | re.I), "<date>"),
    (re.compile(r"\b" + _MONTH + r" \d{1,2}(?:st|nd|rd|th)?(?:,? \d{4})?\b", _F), "<date>"),
    (re.compile(r"\b\d{1,2} " + _MONTH + r"(?: \d{4})?\b", _F), "<date>"),
    (re.compile(r"\b" + _MONTH + r" \d{4}\b", _F), "<date>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b", _F), "<date>"),
    (re.compile(r"\b\d{2}-\d{2}-\d{4}\b", _F), "<date>"),
    (re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", _F), "<date>"),
    (re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?: ?[AaPp][Mm])?\b", _F), "<time>"),
    (re.compile("[-−+]?[$€£] ?-?\\d[\\d,]*(?:\\.\\d+)?", _F), "<amount>"),
    (re.compile(r"\b\d{1,3}(?:,\d{3})*\.\d{2}\b", _F), "<amount>"),
    (re.compile(r"\d+(?:[.,]\d+)*", _F), "<n>"),
]
_COLLAPSE = re.compile("(<(?:n|amount|date|time)>)(?:[ .,/:\\-–]*\\1)+", _F)
_PLACEHOLDER = re.compile(r"<(?:n|amount|date|time)>")


def name_template(s):
    if s is None:
        return ""
    t = _WS.sub(" ", str(s)).strip()
    for pat, rep in _STEPS:
        t = pat.sub(rep, t)
    return _COLLAPSE.sub(r"\1", t)


def name_template_regex(template):
    parts = _PLACEHOLDER.split(template)
    return re.compile("^" + ".+?".join(re.escape(p) for p in parts) + "$")


# ---------------------------------------------------------------------------
# Hashes shared with Node
# ---------------------------------------------------------------------------

def harness_files(root=ROOT):
    base = os.path.join(root, "tools", "a11y")
    out = []
    for sub in ("checks", "lib"):
        d = os.path.join(base, sub)
        if os.path.isdir(d):
            out += [f"tools/a11y/{sub}/{f}" for f in os.listdir(d) if f.endswith(".mjs")]
    if os.path.exists(os.path.join(base, "sweep.mjs")):
        out.append("tools/a11y/sweep.mjs")
    return sorted(out)


def harness_hash(root=ROOT):
    text = ""
    for rel in harness_files(root):
        with open(os.path.join(root, rel), "rb") as f:
            text += rel + "\n" + hashlib.sha256(f.read()).hexdigest() + "\n"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


_BUILD = re.compile(r"cdn\.mercury\.com/builds/([A-Za-z0-9]+)/")


def build_hash(html):
    m = _BUILD.search(html or "")
    return m.group(1) if m else "unknown"


# ---------------------------------------------------------------------------
# Helpers the Python stages share
# ---------------------------------------------------------------------------

def rel(path):
    """path relative to the repository root, or as given when it lies outside it."""
    r = os.path.relpath(os.path.abspath(path), ROOT)
    return path if r.startswith("..") else r


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def latest_by(lines, field):
    """{value of field: the last line with that value}, for append-only ledgers."""
    return {line[field]: line for line in lines}


def load_checks():
    """tools/a11y/checks.json: check families, rule maps, templates and coverage tables."""
    return read_json(os.path.join(A11Y, "checks.json"))


def load_wcag():
    """{criterion number: criterion} from tools/a11y/wcag.json, the WCAG 2.2 A and AA criteria."""
    return {c["number"]: c for c in read_json(os.path.join(A11Y, "wcag.json"))["criteria"]}
