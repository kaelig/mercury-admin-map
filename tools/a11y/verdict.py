"""The `verdict add` primitive (R16, R17, R26; KTD19): the only writer of runs/<id>/verdicts.jsonl.

    python3 tools/a11y/audit.py verdict add --file runs/<id>/batches/verify-004.out.jsonl [--run <id>]

Reads a verifier's output file, checks each verdict line against findings.json and schemas.md,
prints the rejected lines with their reasons as JSON, and appends the valid ones. Decision lines in
the same file are skipped: `decision add` takes them, and so are lines verdicts.jsonl already
holds, so adding a file twice appends nothing. The verifier judges whether a criterion applies and
whether the state was reached; it cannot overrule a measured value, so a `not-reproduced-live` drop
on a measured family must cite a passing probe taken under the sweep's environment, of one of the
finding's own elements (an occurrence on the probe's state, read from the raw file). File formats:
schemas.md.

The plumbing every `add` primitive shares (read_out, check_lines, add_file, print_result and the
field checks) lives here too.
"""
import argparse
import collections
import datetime
import functools
import json
import os
import re
import sys

import mapdata
import signature
from lib.canon import append_jsonl, canonical, dump, read_json, read_jsonl, rel

OUTCOMES = ("confirm", "drop", "rerate", "not-reached", "unverified")
VERIFIED = ("confirm", "rerate")     # the outcomes that keep a finding in the map
SEVERITIES = tuple(mapdata.SEVERITY_RANK)
SOURCES = ("scanner", "hand-found")
DROP_CODES = ("exception:inline", "exception:equivalent-control", "exception:essential", "exception:ua-control",
              "decorative", "not-reproduced-live", "third-party-frame", "aria-optional-by-apg",
              "tool-false-positive", "not-aa-failure", "fixed-since-capture")
# Families whose evidence is a measurement (contrast ratio, size, focus pixels, trap trace), and the
# probe kind that can measure each one again.
MEASURED = {"contrast": "contrast", "target-size": "target", "focus-visible": "focus", "keyboard-trap": "focus"}
SWEEP_ENV = {"viewport": [1440, 900], "dpr": 1, "colorScheme": "light", "reducedMotion": "reduce"}
TARGET_MIN = 24
DEFAULT_RATIO = 4.5
REQUIRED = ("build", "model", "batch", "prompt")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def today():
    return datetime.date.today().isoformat()


def text(v):
    """True when v is a string with something other than whitespace in it."""
    return isinstance(v, str) and bool(v.strip())


def missing_fields(line, fields):
    """A "<field> is required" reason for each field that holds no text."""
    return [f"{field} is required" for field in fields if not text(line.get(field))]


def date_reasons(date):
    """The reason an optional date is invalid, as a list: empty when it is absent or YYYY-MM-DD."""
    if date is not None and not (isinstance(date, str) and DATE.match(date)):
        return ["date must be YYYY-MM-DD"]
    return []


def count_by(lines, field):
    """{value: lines with it}, sorted by value, for a stage's status."""
    return dict(sorted(collections.Counter(line.get(field) for line in lines).items()))


# ---------------------------------------------------------------------------
# Shared plumbing for the add primitives
# ---------------------------------------------------------------------------

def read_out(path):
    """[(line number, text, object or None)] for every non-blank line of an agent output file."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            raw = raw.rstrip("\r\n")
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                obj = None
            rows.append((n, raw, obj))
    return rows


def checked(path, validate, skip=None):
    """check_lines(), with each valid line as (the object read, the line to store)."""
    accepted, rejected, skipped = [], [], 0
    for n, raw, obj in read_out(path):
        if isinstance(obj, dict) and skip and skip(obj):
            skipped += 1
            continue
        if not isinstance(obj, dict):
            rejected.append({"line": n, "reasons": ["not a JSON object"], "text": raw})
            continue
        line, reasons = validate(obj)
        if reasons:
            rejected.append({"line": n, "reasons": reasons, "text": raw})
        else:
            accepted.append((obj, line))
    return accepted, rejected, skipped


def check_lines(path, validate, skip=None):
    """Validate every line of `path` without storing anything.

    validate(obj) -> (line to store, [reasons]); skip(obj) -> True for a line another primitive owns.
    Returns (valid lines, rejected entries, skipped count)."""
    accepted, rejected, skipped = checked(path, validate, skip)
    return [line for _, line in accepted], rejected, skipped


def undated(line):
    return {k: v for k, v in line.items() if k != "date"}


def ledger_key(obj, line):
    """The canonical text a line to store is matched against the ledger by. A date the validator
    filled in is left out, so the same line added again on another day still matches."""
    return canonical(undated(line) if obj.get("date") is None else line)


def ledger_keys(lines):
    """Every ledger_key the lines match: each line whole, and without its date."""
    return {canonical(form) for line in lines for form in (line, undated(line))}


def unadded(path, validate, held, skip=None):
    """How many valid lines of `path` are not among `held`, the ledger_keys of the ledger they go to."""
    accepted, _, _ = checked(path, validate, skip)
    return sum(1 for obj, line in accepted if ledger_key(obj, line) not in held)


def add_file(path, validate, out_path, skip=None):
    """check_lines(), then append the valid lines `out_path` does not hold yet, so a file added twice
    appends nothing the second time. Returns (appended lines, rejected entries, skipped count,
    duplicates: valid lines the ledger already held)."""
    accepted, rejected, skipped = checked(path, validate, skip)
    held = ledger_keys(read_jsonl(out_path))
    lines, duplicates = [], 0
    for obj, line in accepted:
        if ledger_key(obj, line) in held:
            duplicates += 1
            continue
        held |= ledger_keys([line])
        lines.append(line)
    if lines:
        append_jsonl(out_path, lines)
    return lines, rejected, skipped, duplicates


def print_result(path, out_path, accepted, rejected, skipped, duplicates):
    """Print the add summary as JSON; exit 1 when any line was rejected."""
    print(dump({"file": rel(path), "to": rel(out_path), "appended": len(accepted), "duplicates": duplicates,
                "skipped": skipped, "rejected": rejected}), end="")
    return 1 if rejected else 0


def load_findings(ws, run_id):
    """{key: finding} from the run's findings.json, or None when group has not run."""
    doc = read_json(ws.file("findings.json", run_id))
    return {f["key"]: f for f in doc.get("findings", [])} if doc else None


# ---------------------------------------------------------------------------
# Probes (KTD19): each records its environment; a measured family needs a passing one to drop
# ---------------------------------------------------------------------------

def number(v):
    """4.6 from 4.6, "4.6" or "4.6:1"; None otherwise."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(":1", "").strip())
    except (TypeError, ValueError):
        return None


def env_words(env):
    e = env or {}
    return (f"viewport {e.get('viewport')}, dpr {e.get('dpr')}, {e.get('colorScheme')}, "
            f"{e.get('reducedMotion')}")


def probe_reasons(p, finding):
    if not isinstance(p, dict):
        return ["each probe must be an object"]
    pid = p.get("id")
    label = f"probe {pid}" if text(pid) else "a probe"
    out = []
    if not text(pid):
        out.append("a probe needs an id")
    for field in ("state", "kind"):
        if not text(p.get(field)):
            out.append(f"{label} needs a {field}")
    if not isinstance(p.get("value"), dict):
        out.append(f"{label} needs a value object")
    env = p.get("env")
    if not isinstance(env, dict):
        out.append(f"{label} records no env")
    elif any(env.get(k) != v for k, v in SWEEP_ENV.items()):
        out.append(f"{label} was taken at {env_words(env)}; the sweep's environment is {env_words(SWEEP_ENV)}")
    if finding and text(p.get("state")) and p["state"] not in finding["states"]:
        out.append(f"{label} is on {p['state']}, which is not one of the finding's states")
    return out


def probe_passes(finding, p):
    """(passes, why) for a probe of the finding's measured family."""
    v, family = p.get("value") or {}, finding["family"]
    if family == "contrast":
        ratio = number(v.get("ratio"))
        required = number((finding.get("facts") or {}).get("required")) or number(v.get("required")) or DEFAULT_RATIO
        if ratio is None:
            return False, "it records no ratio"
        return ratio >= required, f"it measured {ratio:g}:1 where AA needs {required:g}:1"
    if family == "target-size":
        rect = v.get("rect") if isinstance(v.get("rect"), list) and len(v["rect"]) == 4 else [None] * 4
        w, h = number(v.get("width", rect[2])), number(v.get("height", rect[3]))
        offset = number(v.get("offset"))
        big = w is not None and h is not None and w >= TARGET_MIN and h >= TARGET_MIN
        spaced = offset is not None and offset >= TARGET_MIN
        return big or spaced, f"it measured {w}×{h} with a {offset}px clear circle where AA needs {TARGET_MIN}"
    if family == "focus-visible":
        return v.get("visible") is True, "it records visible: true" if v.get("visible") is True else \
            "it does not record visible: true"
    if family == "keyboard-trap":
        return v.get("trapped") is False, "it records trapped: false" if v.get("trapped") is False else \
            "it does not record trapped: false"
    return True, ""


def occurrence_items(f, state, raw):
    """The raw results (records, for announced-as) the finding's occurrences on `state` point at, from
    that state's raw file. An index past the end, or at an item of another rule or record signature
    (the raw file was swept again after `group`), is left out."""
    records = f.get("rule") == "announced-as"
    items = (raw or {}).get("records" if records else "results") or []
    out = []
    for o in f.get("occurrences") or []:
        if o["state"] != state or o["index"] >= len(items):
            continue
        item = items[o["index"]]
        if (signature.record_sig(item) == f.get("recordSig")) if records else item.get("rule") == f.get("rule"):
            out.append(item)
    return out


def measured_element_reasons(cite, p, finding, raw):
    """A scanner finding's measurement is of its own elements, so the probe must measure one of them on
    its state. A hand-found finding has no occurrences: the sweep measured no element of it. Without
    raw files (raw is None) there is nothing to match against, and the check is left out."""
    occurrences = finding.get("occurrences") or []
    if raw is None or not occurrences:
        return []
    state, xpath = p.get("state"), p.get("xpath")
    xpaths = set()
    if any(o["state"] == state for o in occurrences):
        xpaths = {(i.get("occurrence") or {}).get("xpath") for i in occurrence_items(finding, state, raw(state))}
    if text(xpath) and xpath in xpaths:
        return []
    return [f"probe {cite} measured {xpath!r} on {state}, which is not one of the finding's elements there: "
            f"probe the element the sweep measured (the xpath in the batch entry's evidence)"]


def cited_probe_reasons(line, finding, raw):
    family, kind = finding["family"], MEASURED[finding["family"]]
    cite = line.get("counterEvidence")
    probes = [p for p in line.get("probes") or [] if isinstance(p, dict)]
    p = next((p for p in probes if p.get("id") == cite), None)
    if p is None:
        return [f"a not-reproduced-live drop on {family} is a measured value: counterEvidence must name a probe "
                f"in probes by id (got {cite!r})"]
    if p.get("kind") != kind:
        return [f"probe {cite} is a {p.get('kind')} probe; {family} needs a {kind} probe"]
    ok, why = probe_passes(finding, p)
    if not ok:
        return [f"probe {cite} does not pass: {why}"]
    return measured_element_reasons(cite, p, finding, raw)


# ---------------------------------------------------------------------------
# The verdict validator
# ---------------------------------------------------------------------------

def validate(line, findings, allow_unverified=False, raw=None):
    """(line to store, [reasons]). An empty reason list means the line is valid.

    findings: {key: finding} from findings.json. `unverified` is written only by `verify prepare`,
    which passes allow_unverified=True. raw(state) returns the run's raw file for a state: with it, the
    probe a not-reproduced-live drop on a measured family cites must measure one of the finding's own
    elements. `verdict add` always passes it (run_check)."""
    reasons = []
    key, outcome = line.get("key"), line.get("outcome")
    f = findings.get(key) if isinstance(key, str) else None
    if not text(key):
        reasons.append("key is required")
    elif f is None:
        reasons.append(f"key {key} is not in findings.json")
    if outcome not in OUTCOMES:
        reasons.append(f"outcome must be one of {', '.join(OUTCOMES)} (got {outcome!r})")
    elif outcome == "unverified" and not allow_unverified:
        reasons.append("unverified is written by verify prepare after two failed attempts, not by a verifier")
    if line.get("source") not in SOURCES:
        reasons.append(f"source must be scanner or hand-found (got {line.get('source')!r})")
    reasons += missing_fields(line, REQUIRED)
    before, after = line.get("severityBefore"), line.get("severityAfter")
    for field, v in (("severityBefore", before), ("severityAfter", after)):
        if v not in SEVERITIES:
            reasons.append(f"{field} must be one of {', '.join(SEVERITIES)} (got {v!r})")
    states = line.get("states")
    if not isinstance(states, list) or not all(isinstance(s, str) for s in states):
        reasons.append("states must list the sampled states checked")
        states = []
    elif not states and outcome != "unverified":
        reasons.append("states must list the sampled states checked")
    probes = line.get("probes", [])
    if not isinstance(probes, list):
        reasons.append("probes must be a list")
        probes = []
    for p in probes:
        reasons += probe_reasons(p, f)

    code, cite = line.get("reasonCode"), line.get("counterEvidence")
    if outcome in ("drop", "rerate") and not text(line.get("reason")):
        reasons.append(f"a {outcome} needs a reason")
    if outcome == "rerate" and before == after:
        reasons.append("a rerate must change the severity")
    if outcome in ("confirm", "not-reached", "unverified") and before != after:
        reasons.append(f"a {outcome} keeps the severity: use rerate to change it")
    if outcome == "drop":
        if code is None:
            reasons.append(f"a drop needs a reasonCode, one of {', '.join(DROP_CODES)}")
        elif code not in DROP_CODES:
            reasons.append(f"reasonCode {code!r} is not one of {', '.join(DROP_CODES)}")
        if not text(cite):
            reasons.append("a drop needs a counterEvidence reference (a probe id or a screenshot)")
        if code == "not-aa-failure" and not text(line.get("criterionAnalysis")):
            reasons.append("not-aa-failure needs a criterionAnalysis: why no AA criterion fails")
    elif code is not None:
        reasons.append("reasonCode applies to drops only")

    if f:
        if line.get("source") in SOURCES and line["source"] != f["source"]:
            reasons.append(f"source must be {f['source']}, as in findings.json")
        if f.get("issueId") and line.get("issueId") != f["issueId"]:
            reasons.append(f"issueId must be {f['issueId']}: the registry holds one for this finding")
        if not f.get("issueId") and line.get("issueId") is not None:
            reasons.append("issueId must be null: the registry holds none for this finding")
        if before in SEVERITIES and before != f["severityDefault"]:
            reasons.append(f"severityBefore must be the finding's default severity, {f['severityDefault']}")
        outside = [s for s in states if s not in f["states"]]
        if outside:
            reasons.append(f"states {', '.join(outside)} are not among the finding's states")
        if outcome == "drop":
            unchecked = [s for s in f["sample"] if s not in states]
            if unchecked:
                reasons.append(f"a drop must check every sampled state: {', '.join(unchecked)} not checked")
            if code == "fixed-since-capture" and f["source"] != "hand-found":
                reasons.append("fixed-since-capture is for hand-found issues only")
            if code == "not-reproduced-live" and f["family"] in MEASURED:
                reasons += cited_probe_reasons(line, f, raw)
        if f["source"] == "hand-found" and outcome in VERIFIED + ("drop",) and not probes and not text(cite):
            reasons.append("a hand-found verdict must cite a probe (probes) or an evidence reference (counterEvidence)")

    attempt = line.get("attempt", 1)
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        reasons.append("attempt must be a positive integer")
    if not isinstance(line.get("second", False), bool):
        reasons.append("second must be true or false")
    reasons += date_reasons(line.get("date"))

    stored = dict(line)
    stored.setdefault("issueId", f.get("issueId") if f else None)
    for k, v in (("reasonCode", None), ("counterEvidence", None), ("probes", []), ("attempt", 1),
                 ("second", False)):
        stored.setdefault(k, v)
    if stored.get("date") is None:
        stored["date"] = today()
    return stored, reasons


def is_decision(obj):
    return "decision" in obj and "outcome" not in obj


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

def status(ws, run_id):
    lines = read_jsonl(ws.file("verdicts.jsonl", run_id)) if run_id else []
    if not lines:
        return {"pending": True}
    return {"lines": len(lines), "keys": len({v.get("key") for v in lines}), "outcomes": count_by(lines, "outcome")}


def raw_files(raw_dir):
    """raw(state) for validate(): the state's raw file in raw_dir, or None, each read once."""
    return functools.lru_cache(maxsize=None)(lambda sid: read_json(os.path.join(raw_dir, f"{sid}.json")))


def run_check(ws, run_id, findings):
    """The check `verdict add` applies to each line: validate() against the run's raw files, and no
    canary verdicts."""
    raw = raw_files(ws.raw_dir(run_id))

    def check(obj):
        line, reasons = validate(obj, findings, raw=raw)
        if str(obj.get("batch", "")).startswith("canary"):
            reasons = reasons + ["canary verdicts never enter the run: score them with `audit.py canary score`"]
        return line, reasons

    return check


def cli(ws, sub, argv):
    p = argparse.ArgumentParser(prog="audit.py verdict add", description=__doc__.split("\n\n")[0])
    p.add_argument("--file", required=True, help="a verifier output file, batches/<batch>.out.jsonl")
    args = p.parse_args(argv)
    run_id = ws.run_id()
    findings = load_findings(ws, run_id)
    if findings is None:
        print(f"verdict add: run {run_id} has no findings.json: run `audit.py group` first", file=sys.stderr)
        return 2
    out_path = ws.file("verdicts.jsonl", run_id)
    return print_result(args.file, out_path, *add_file(args.file, run_check(ws, run_id, findings), out_path,
                                                       skip=is_decision))
