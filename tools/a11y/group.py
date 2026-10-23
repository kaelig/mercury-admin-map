"""The `group` stage (R12–R15, R17; KTD15–KTD17): raw results and judged records into findings.

    python3 tools/a11y/audit.py group [--run <id>]

Reads states.json, the audited raw files (the states outcomes.json lists as audited, or, before the
sweep writes it, every raw file marked audited), judgments.jsonl, the hand-found issues from --data
with the snapshot off, tools/a11y/hand-found.json and the current snapshot's registry.json. One
finding per rule per shared element (keys from signature.py), except axe's incomplete results: one
finding per rule and messageKey across the product (KTD12), written with checks.json's `incomplete`
templates, which name no single element. One finding per hand-found issue, and an
attachment proposal wherever a scanner finding covers at least half of a hand-found issue's screens
in the same family. Issues are never read from --map: the map gives screen titles only.

Writes findings.json, canonical and sorted, so two runs on the same inputs give the same bytes. Its
`inputs` record what it was grouped from: the harness hash, the sha of outcomes.json's states (null
before the sweep writes it) and the sha of the raw files it read, byte for byte. `verify prepare`,
`diff` and `publish` refuse a findings.json whose `inputs` differ from the run's current ones, or
that has none (rerun `group`), and a run whose run.json `harness`, written as the sweep starts, is
not the harness now (rerun `sweep`, then `group`).
File formats: schemas.md.
"""
import argparse
import collections
import hashlib
import math
import os
import string
import sys

import mapdata
import signature
import verdict
from lib.canon import (harness_hash, latest_by, load_checks, load_wcag, plural, read_json, read_jsonl, rel, sha,
                       write_json)

HERE = os.path.dirname(os.path.abspath(__file__))
AGREEMENT = 0.9     # KTD15: flag a group whose members share a tag-and-attribute set less often than this
OVERLAP = 0.5       # KTD17: share of a hand-found issue's screens a scanner finding must cover
BASIS = ("violation", "failure", "incomplete", "candidate")
GENERIC_ROLES = {"", "generic", "none", "presentation", "StaticText"}
NO_MESSAGE_KEY = "none"   # the messageKey fact of an incomplete group axe gave no messageKey
FAR = 10 ** 9
UNKNOWN = "unknown"
WHY_MAX = 36        # checks.json factDocs: `why` is at most 36 characters
STALE = "findings.json was grouped from a different sweep: rerun `audit.py group`"


class GroupError(Exception):
    pass


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def load_check_data():
    hand = read_json(os.path.join(HERE, "hand-found.json"))
    return load_checks(), load_wcag(), hand


def audited_ids(outcomes, states):
    """The states whose raw files group reads: those outcomes.json lists as audited, or, before the sweep
    writes it, every state."""
    if outcomes:
        return sorted(s for s, o in outcomes.get("states", {}).items() if o.get("outcome") == "audited")
    return sorted(states)


def audited_raws(ws, run_id, states):
    """({state id: raw file} for every audited state, states with no raw file yet)."""
    raw_dir = ws.raw_dir(run_id)
    outcomes = read_json(ws.file("outcomes.json", run_id))
    ids = audited_ids(outcomes, states)
    if outcomes:
        missing = [s for s in ids if not os.path.exists(os.path.join(raw_dir, f"{s}.json"))]
        if missing:
            raise GroupError(f"outcomes.json lists {len(missing)} audited state(s) with no raw file in "
                             f"{raw_dir}: {', '.join(missing)}")
    raws, absent = {}, []
    for sid in ids:
        doc = ws.raw(sid, run_id)
        if doc is None:
            absent.append(sid)
        elif doc.get("status") == "audited":
            raws[sid] = doc
    return raws, absent


def file_sha(path):
    """The sha256 of the file's bytes, or None when there is no file."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None


def inputs(ws, run_id):
    """What findings.json is grouped from now: the harness hash, the sha of outcomes.json's states (null
    before the sweep writes outcomes.json) and the sha of the raw files group reads. A state swept
    again changes its raw file even when its outcome stays the same."""
    outcomes = read_json(ws.file("outcomes.json", run_id))
    raw_dir = ws.raw_dir(run_id)
    files = {sid: file_sha(os.path.join(raw_dir, f"{sid}.json")) for sid in audited_ids(outcomes, ws.states(run_id))}
    return {"harness": harness_hash(), "outcomes": sha(outcomes.get("states") or {}) if outcomes else None,
            "raw": sha(files)}


def swept_with_another_harness(ws, run_id):
    """The refusal when run.json's harness, which every sweep writes as it starts, is not the harness now:
    the raw files hold another harness's results, and grouping them again cannot change that. Else None."""
    swept, now = ws.run_doc(run_id).get("harness"), harness_hash()
    if swept == now:
        return None
    return (f"the run was swept with harness {swept}, and the harness is now {now}: rerun `audit.py sweep`, "
            f"then `audit.py group`")


def stale(ws, run_id, doc):
    """Why findings.json (doc) cannot be used, else None: the run was swept with another harness, or
    findings.json was grouped from other inputs than the run's current ones (STALE). A findings.json
    without `inputs` was grouped before they were recorded: stale too."""
    return swept_with_another_harness(ws, run_id) or (None if doc.get("inputs") == inputs(ws, run_id) else STALE)


def latest_judgments(ws, run_id):
    """The last judgment for each record signature."""
    return latest_by(read_jsonl(ws.file("judgments.jsonl", run_id)), "sig")


def hand_found_issues(ws, hand):
    issues = [i for a in mapdata.load_areas(ws.data_dir, snapshot_dir=False) for i in a.get("issues", [])
              if i.get("category") == "a11y"]
    missing = sorted(i["id"] for i in issues if i["id"] not in hand)
    if missing:
        raise GroupError("hand-found a11y issue(s) missing from tools/a11y/hand-found.json: " + ", ".join(missing))
    return issues


def node_titles(map_doc):
    return {n["id"]: n.get("title") or n["id"] for a in map_doc.get("areas", []) for n in a.get("nodes", [])}


# ---------------------------------------------------------------------------
# Words: the facts templates are filled with (checks.json factDocs)
# ---------------------------------------------------------------------------

def cut(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def quoted(prefix, text, limit):
    return f'{prefix}"{cut(text, max(1, limit - len(prefix) - 2))}"'


def element_words(occ, limit=50):
    """button "Edit limits", or button in "API Tokens", or text in the main landmark."""
    o = occ or {}
    role = o.get("role") or ""
    role = "text" if role in GENERIC_ROLES else role
    name = signature.template(o.get("name"))
    if name:
        return quoted(f"{role} ", name, limit)
    heading = signature.template(o.get("heading"))
    if heading:
        return quoted(f"{role} in ", heading, limit)
    landmark = (o.get("landmark") or {}).get("role")
    if landmark:
        return cut(f"{role} in the {landmark} landmark", limit)
    return cut(role, limit)


def ratio_text(value, decimals=None):
    x = verdict.number(value)
    if x is None:
        return UNKNOWN
    return f"{x:.{decimals}f}" if decimals is not None else f"{x:g}"


def axe_summary(m):
    if m["check"] != "axe":
        return ""
    lines = [ln.strip() for ln in str(m["evidence"].get("failureSummary") or "").splitlines()]
    lines = [ln for ln in lines if ln and ln not in ("Fix any of the following:", "Fix all of the following:")]
    return cut("; ".join(lines), 160)


def announced(m):
    if m.get("record"):
        return cut(m["record"].get("template") or "", 80)
    o = m["occ"] or {}
    for rec in m["raw"].get("records") or []:
        if o.get("xpath") and (rec.get("occurrence") or {}).get("xpath") == o["xpath"]:
            return cut(rec.get("template") or "", 80)
    return cut(f'{o.get("role") or ""} "{signature.template(o.get("name"))}"', 80)


def overlay_words(raw):
    layer = raw.get("layer") or {}
    role = layer.get("role")
    if not role:
        return UNKNOWN
    name = signature.template(layer.get("name"))
    return quoted(f"{role} ", name, 40) if name else cut(f"unnamed {role}", 40)


def focus_words(ev):
    if "focus" not in ev:
        return UNKNOWN
    v = ev["focus"]
    if v is None or v == "body":
        return "page body"
    return element_words(v) if isinstance(v, dict) else cut(v, 50)


def stops_count(m):
    v = m["evidence"].get("stops")
    if v is None:
        checks = m["raw"].get("checks") or {}
        if m["check"] == "tabwalk":
            v = (checks.get("tabwalk") or {}).get("stops")
        elif m["check"] == "dialog":
            v = (checks.get("dialog") or {}).get("trace")
    if isinstance(v, list):
        return str(len(v))
    return str(v) if v is not None else UNKNOWN


def message_words(m):
    msg = m["evidence"].get("message") or ((m["raw"].get("checks") or {}).get("status") or {}).get("message")
    return quoted("", signature.template(msg), 50) if msg else UNKNOWN


def size_words(occ):
    rect = (occ or {}).get("rect") or []
    return f"{round(rect[2])}×{round(rect[3])}" if len(rect) == 4 else UNKNOWN


def number_words(v):
    return str(round(v)) if isinstance(v, (int, float)) else UNKNOWN


def fact_names(fam):
    """Every placeholder the family's templates use (templates and ruleTemplates), plus its `facts`."""
    names = set(fam.get("facts", []))

    def walk(v):
        if isinstance(v, str):
            names.update(f for _, f, _, _ in string.Formatter().parse(v) if f)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(fam.get("templates"))
    walk(fam.get("ruleTemplates"))
    return sorted(names)


def element_count(g, ctx):
    """An incomplete group's distinct elements, counted by the key a violation on each would get."""
    shape = ctx["checks"]["families"][g["family"]]["key"]
    n = len({signature.finding_key(g["family"], g["rule"], shape, m["occ"], m["evidence"]) for m in g["members"]})
    return plural(n, "element")


def why_words(message_key, ctx):
    inc = ctx["checks"]["incomplete"]
    if message_key is None:
        return inc["noMessageKey"]
    return inc["messageKeyText"].get(message_key) or cut(message_key, WHY_MAX)


def compute_facts(fam, rule, criteria, m, states, primary, ctx, g=None):
    occ, ev = m["occ"], m["evidence"]
    fg, bg = signature.contrast_colors(ev)
    calc = {
        "element": lambda: element_words(occ),
        "screen": lambda: cut(ctx["titles"].get(primary) or primary, 40),
        "screens": lambda: len(states),
        "criterion": lambda: criteria[0],
        "criterionName": lambda: ctx["wcag"][criteria[0]]["name"],
        "rule": lambda: rule,
        "announced": lambda: announced(m),
        "axeSummary": lambda: axe_summary(m),
        "fg": lambda: signature.hex_color(fg) or UNKNOWN,
        "bg": lambda: signature.hex_color(bg) or UNKNOWN,
        "ratio": lambda: ratio_text(ev.get("contrastRatio"), 2),
        "required": lambda: "3" if rule == "link-in-text-block" else ratio_text(ev.get("expectedContrastRatio")),
        "size": lambda: size_words(occ),
        "offset": lambda: number_words(ev["offset"] if ev.get("offset") is not None else ev.get("closestOffset")),
        "stop": lambda: str(ev["stop"]) if ev.get("stop") is not None else UNKNOWN,
        "stops": lambda: stops_count(m),
        "previous": lambda: (element_words(ev["previous"]) if isinstance(ev.get("previous"), dict)
                             else cut(ev["previous"], 50) if ev.get("previous") else UNKNOWN),
        "overlay": lambda: overlay_words(m["raw"]),
        "focus": lambda: focus_words(ev),
        "message": lambda: message_words(m),
        "messageKey": lambda: g.get("messageKey") or NO_MESSAGE_KEY,
        "why": lambda: why_words(g.get("messageKey"), ctx),
        "elements": lambda: element_count(g, ctx),
    }
    return {name: calc[name]() for name in fact_names(fam)}


def finding_templates(checks, family, rule, basis):
    """The templates a scanner finding is written with: checks.json's `incomplete` set for a group of
    axe's incomplete results, else its family's (templates_for)."""
    if basis == "incomplete":
        t = dict(checks["incomplete"]["templates"])
        t["copy"] = dict(t["copy"])
        return t
    return templates_for(checks["families"][family], rule)


def templates_for(fam, rule):
    """The family's templates with ruleTemplates[rule] merged over them key by key (and inside copy)."""
    t = dict(fam["templates"])
    t["copy"] = dict(fam["templates"].get("copy", {}))
    for k, v in (fam.get("ruleTemplates") or {}).get(rule, {}).items():
        if k == "copy":
            t["copy"].update(v)
        else:
            t[k] = v
    return t


# ---------------------------------------------------------------------------
# Primary and sampled states (KTD15)
# ---------------------------------------------------------------------------

def hops(info):
    path = (info or {}).get("path")
    return len(path) - 1 if path else FAR


def area_of(sid, states):
    return (states.get(sid) or {}).get("area") or sid.split(".")[0]


def pick_primary(ids, states):
    """A page state first, then the fewest recipe hops, then the id."""
    return min(ids, key=lambda s: (0 if (states.get(s) or {}).get("kind") == "page" else 1, hops(states.get(s)), s))


def pick_sample(primary, ids, states, occ_of):
    """The primary plus the state least like it: another area, then another kind, then the largest
    class distance; ties go to the fewest hops, then the id."""
    others = [s for s in ids if s != primary]
    if len(others) < 2:
        return [primary] + others
    p_area, p_kind = area_of(primary, states), (states.get(primary) or {}).get("kind")
    p_classes = signature.class_set(occ_of(primary))

    def rank(s):
        return (-(area_of(s, states) != p_area), -((states.get(s) or {}).get("kind") != p_kind),
                -signature.set_distance(p_classes, signature.class_set(occ_of(s))), hops(states.get(s)), s)

    return [primary, min(others, key=rank)]


def criterion_key(number):
    """(2, 4, 11) for "2.4.11": criteria in numeric order."""
    return tuple(int(p) for p in number.split("."))


def order_criteria(criteria, fam):
    order = fam["criteria"]
    return sorted(set(criteria), key=lambda c: (order.index(c) if c in order else len(order), criterion_key(c)))


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def collect(raws, judgments, checks, wcag):
    """(groups by key, advisory records by sig, skipped result counts)."""
    flagged, advisory_sigs = {}, {}
    for s, j in judgments.items():
        if j.get("verdict") != "flag":
            continue
        if j.get("criterion") in wcag and j.get("family") in checks["families"]:
            flagged[s] = j
        else:
            advisory_sigs[s] = j
    groups, advisory, skipped = {}, {}, collections.Counter()
    for sid in sorted(raws):
        raw = raws[sid]
        # (family, criterion, xpath) of every definite axe or custom result on this state: a flagged record on
        # the same element for the same criterion is the same failure, already a finding.
        covered = set()
        for i, r in enumerate(raw.get("results") or []):
            rule = r.get("rule")
            family = checks["axeRules"].get(rule) or checks["customRules"].get(rule)
            if not family:
                skipped[f"{rule}: no family in checks.json"] += 1
                continue
            criteria = [c for c in (r.get("criteria") or checks["customCriteria"].get(rule) or []) if c in wcag]
            if not criteria:
                skipped[f"{rule}: no WCAG 2.2 A or AA criterion"] += 1
                continue
            if r.get("basis") == "incomplete":
                mk = r.get("messageKey")
                key = signature.incomplete_key(family, rule, mk)
                g = groups.setdefault(key, {"family": family, "rule": rule, "sig": None, "basis": "incomplete",
                                            "messageKey": mk, "members": []})
            else:
                fam = checks["families"][family]
                key = signature.finding_key(family, rule, fam["key"], r.get("occurrence"), r.get("evidence"))
                g = groups.setdefault(key, {"family": family, "rule": rule, "sig": None, "members": []})
                xpath = (r.get("occurrence") or {}).get("xpath")
                if xpath:
                    covered |= {(family, c, xpath) for c in criteria}
            g["members"].append({"state": sid, "index": i, "occ": r.get("occurrence"), "evidence": r.get("evidence") or {},
                                 "check": r.get("check"), "criteria": criteria, "basis": r.get("basis"), "raw": raw})
        for i, rec in enumerate(raw.get("records") or []):
            s = signature.record_sig(rec)
            if s in flagged:
                j = flagged[s]
                if (j["family"], j["criterion"], (rec.get("occurrence") or {}).get("xpath")) in covered:
                    skipped["announced-as: same element and criterion as a scanner result"] += 1
                    continue
                key = signature.record_finding_key(j["family"], s)
                g = groups.setdefault(key, {"family": j["family"], "rule": "announced-as", "sig": s, "members": []})
                g["members"].append({"state": sid, "index": i, "occ": rec.get("occurrence"),
                                     "evidence": {k: v for k, v in rec.items() if k != "occurrence"},
                                     "check": "records", "criteria": [j["criterion"]], "basis": "candidate",
                                     "raw": raw, "record": rec})
            elif s in advisory_sigs:
                a = advisory.setdefault(s, {"sig": s, "template": rec.get("template"),
                                            "reason": advisory_sigs[s].get("reason"), "states": set()})
                a["states"].add(sid)
    advisory = [dict(a, states=len(a["states"])) for _, a in sorted(advisory.items())]
    return groups, advisory, skipped


def assemble(key, g, ctx):
    checks, states = ctx["checks"], ctx["states"]
    fam = checks["families"][g["family"]]
    members = sorted(g["members"], key=lambda m: (m["state"], m["index"]))
    first = {}
    for m in members:
        first.setdefault(m["state"], m)
    ids = sorted(first)
    primary = pick_primary(ids, states)
    sample = pick_sample(primary, ids, states, lambda s: first[s]["occ"])
    criteria = order_criteria([c for m in members for c in m["criteria"]], fam)
    kind = (states.get(primary) or {}).get("kind") or first[primary]["raw"].get("kind")
    rule = g["rule"]
    words = checks["incomplete"] if g.get("basis") == "incomplete" else fam
    facts = compute_facts(words, rule, criteria, first[primary], ids, primary, ctx, g)
    t = finding_templates(checks, g["family"], rule, g.get("basis"))
    bases = [m["basis"] for m in members]
    return {
        "key": key,
        "source": "scanner",
        "family": g["family"],
        "rule": rule,
        "criteria": criteria,
        "basis": next((b for b in BASIS if b in bases), bases[0]),
        "severityDefault": fam.get("defaultSeverityByKind", {}).get(kind, {}).get(rule, fam["defaultSeverity"]),
        "title": t["title"].format(**facts).strip(),
        "summary": t["evidence"].format(**facts).strip(),
        "states": ids,
        "occurrences": [{"state": m["state"], "index": m["index"]} for m in members],
        "primary": primary,
        "sample": sample,
        "issueId": ctx["registry"].get("keys", {}).get(key),
        "recordSig": g["sig"],
        "evidenceHash": sha([{"state": s, "evidence": first[s]["evidence"]} for s in sample]),
        "facts": facts,
    }


def hand_found_finding(issue, ctx):
    h = ctx["hand"][issue["id"]]
    fam = ctx["checks"]["families"][h["family"]]
    ids = sorted(set(issue.get("nodeIds") or []))
    primary = pick_primary(ids, ctx["states"]) if ids else None
    return {
        "key": signature.hand_key(issue["id"]),
        "source": "hand-found",
        "family": h["family"],
        "rule": "hand-found",
        "criteria": order_criteria([c for c in h["criteria"] if c in ctx["wcag"]], fam),
        "basis": "failure",
        "severityDefault": issue.get("severity"),
        "title": issue.get("title"),
        "summary": issue.get("evidence"),
        "states": ids,
        "occurrences": [],
        "primary": primary,
        "sample": pick_sample(primary, ids, ctx["states"], lambda s: None) if ids else [],
        "issueId": issue["id"],
        "recordSig": None,
        "evidenceHash": sha({k: issue.get(k) for k in ("title", "detail", "evidence", "nodeIds", "severity")}),
        "facts": {},
    }


def propose(scanner, hand_found, registry):
    """KTD17: same family, and at least half of the hand-found issue's screens among the finding's.
    Many-to-many. An attachment the registry already holds stands and is not proposed again."""
    attached = {(a["finding"], a["handFound"]) for a in registry.get("attachments", [])}
    by_family = collections.defaultdict(list)     # family: [(hand-found finding, its screens)]
    for h in hand_found:
        screens = set(h["states"])
        if screens:
            by_family[h["family"]].append((h, screens))
    out = []
    for f in scanner:
        mine = set(f["states"])
        for h, screens in by_family.get(f["family"], []):
            if (f["key"], h["issueId"]) in attached:
                continue
            overlap = len(screens & mine) / len(screens)
            if overlap >= OVERLAP:
                out.append({"id": signature.proposal_id(f["key"], h["issueId"]), "finding": f["key"],
                            "handFound": h["issueId"], "overlap": round(overlap, 4), "family": f["family"]})
    return sorted(out, key=lambda p: (p["finding"], p["handFound"]))


def flag_groups(groups):
    out = []
    for key, g in sorted(groups.items()):
        # An incomplete group spans different elements by design (KTD12): the shared-element check skips it.
        if len(g["members"]) < 2 or g.get("basis") == "incomplete":
            continue
        share = signature.agreement(m["occ"] for m in g["members"])
        if share < AGREEMENT:
            out.append({"key": key, "message": f"tags and attributes agree {math.floor(round(share * 100, 6))}% "
                                                 f"of the time"})
    return out


def build(ws, run_id):
    """(findings.json document, printable summary lines)."""
    checks, wcag, hand = load_check_data()
    states = ws.states(run_id)
    if not states:
        raise GroupError(f"run {run_id} has no states.json: run `audit.py plan` first")
    grouped_from = inputs(ws, run_id)
    raws, absent = audited_raws(ws, run_id, states)
    issues = hand_found_issues(ws, hand)
    registry = ws.published("registry.json") or {}
    ctx = {"checks": checks, "wcag": wcag, "hand": hand, "states": states, "registry": registry,
           "titles": node_titles(ws.load_map())}

    groups, advisory, skipped = collect(raws, latest_judgments(ws, run_id), checks, wcag)
    scanner = [assemble(k, g, ctx) for k, g in sorted(groups.items())]
    hand_found = [hand_found_finding(i, ctx) for i in sorted(issues, key=lambda i: i["id"])]
    doc = {"run": run_id,
           "inputs": grouped_from,
           "findings": sorted(scanner + hand_found, key=lambda f: f["key"]),
           "proposals": propose(scanner, hand_found, registry),
           "advisory": advisory,
           "flags": flag_groups(groups)}

    lines = []
    per_rule = collections.defaultdict(lambda: [0, 0])
    for g in groups.values():
        per_rule[g["rule"]][0] += len(g["members"])
        per_rule[g["rule"]][1] += 1
    for rule, (nodes, n) in sorted(per_rule.items()):
        lines.append(f"{rule}: {plural(nodes, 'node')}, {plural(n, 'group')}")
    lines.append(f"hand-found: {plural(len(hand_found), 'issue')}")
    for reason, n in sorted(skipped.items()):
        lines.append(f"skipped {plural(n, 'result')}, {reason}")
    for f in doc["flags"]:
        lines.append(f"flagged {f['key']}: {f['message']}")
    if absent:
        lines.append(f"{len(absent)} state{' has' if len(absent) == 1 else 's have'} no raw file yet")
    lines.append(f"{len(raws)} audited states, {len(scanner)} scanner findings, {len(hand_found)} hand-found, "
                 f"{len(doc['proposals'])} attachment proposals, {len(advisory)} advisory, "
                 f"{len(doc['flags'])} flagged groups.")
    return doc, lines


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

def status(ws, run_id):
    doc = read_json(ws.file("findings.json", run_id)) if run_id else None
    if not doc:
        return {"pending": True}
    fs = doc.get("findings", [])
    return {"findings": len(fs),
            "scanner": sum(1 for f in fs if f.get("source") == "scanner"),
            "handFound": sum(1 for f in fs if f.get("source") == "hand-found"),
            "proposals": len(doc.get("proposals", [])),
            "advisory": len(doc.get("advisory", [])),
            "flags": len(doc.get("flags", []))}


def cli(ws, sub, argv):
    p = argparse.ArgumentParser(prog="audit.py group", description=__doc__.split("\n\n")[0])
    p.parse_args(argv)
    run_id = ws.run_id()
    try:
        doc, lines = build(ws, run_id)
    except GroupError as e:
        print(f"group: {e}", file=sys.stderr)
        return 1
    path = ws.file("findings.json", run_id)
    write_json(path, doc)
    print("\n".join(lines))
    print(f"Wrote {rel(path)}")
    return 0
