"""The `publish` stage (R16–R21, R26; KTD16, KTD21, KTD22, KTD24): one atomic snapshot for the map.

    python3 tools/a11y/audit.py publish [--run <id>]

Builds published/ from the run's ledger and the snapshot in force:
- issues.json: a generated `<area>.i-a11y-<slug>` issue for every verified scanner finding (ids
  minted through registry.py), with `copy` filled from the checks.json templates and the finding's
  facts, an `audit` block with the full live evidence, and a one-line `evidence` of at most 300
  characters; plus an overlay for every hand-found issue, computed only from its raw record in
  --data and its latest verdict (a re-rate sets the severity and appends the reason once, an
  attachment adds screens, a drop leaves the record unchanged).
- report.json: the model report.py renders (KTD24).
- registry.json: ids, tombstones and attachments (KTD16).
- meta.json: run id, build hashes, tool versions, coverage and the Lighthouse calibration (this
  run's lighthouse.json, else the previous snapshot's result with its run id and date).

Publish refuses, and leaves the snapshot in force untouched, unless every KTD21 gate passes: the
coverage invariant, a verdict (or `unverified` line) for every finding, a decision for every
proposal, a passing canary, diff.json current and based on the run in meta.json, every generated
issue valid for the ticket export, unique ids across the merged set, every report link resolving,
and a sound registry. It writes published.tmp/, checks what it wrote, renames published/ to
published.prev/ and published.tmp/ to published/, then deletes published.prev/. Rerunning it for a
run already published reports "already published" and changes nothing.
File formats: schemas.md ("published/").
"""
import argparse
import collections
import os
import shutil
import string
import sys

import diff
import export_agent_data as export
import group
import mapdata
import registry
import verify
from calibration import due
from lib.canon import dump, load_wcag, plural, read_json, read_jsonl, write_text
from status import coverage_problems, pending_proposals

STANDARD = "WCAG 2.2 AA"
FILES = ("issues.json", "report.json", "registry.json", "meta.json")
TITLE_MAX = 80
EVIDENCE_MAX = 300
FACTS_MIN = 80            # characters of measured facts kept when a long verifier reason must be cut
PUBLISHED = ("new", "persisting", "re-rated", "reappeared")
HAND_VERIFIED = ("first-audited", "confirmed", "re-rated", "not-reproduced")
COPY_KEYS = tuple(next(iter(export.CURATED.values())))   # the keys of a CURATED entry
COPY_MAY_BE_EMPTY = {"verify", "demoArtifactRisk"}
NEVER_TESTED = ("1.4.12", "1.4.13", "2.4.11", "2.5.7")   # R20 names them as not tested
DIMENSIONS = ["zoom and reflow", "real screen readers", "mobile", "dark theme"]
RECORD_CRITERIA = ("4.1.2", "1.3.1")
TABLES = {
    "contrast": ("contrast",),
    "keyboard": ("keyboard", "keyboard-trap", "focus-visible", "focus-order"),
    "dialog": ("dialog-trigger", "dialog-focus"),
    "screenReader": ("name", "role-state", "structure", "status-message", "error-association"),
}
TABLE_FACTS = {
    "contrast": ("element", "fg", "bg", "ratio", "required"),
    "keyboard": ("element", "stop", "stops", "previous"),
    "dialog": ("element", "overlay", "focus"),
    "screenReader": ("element", "announced", "message"),
}


class Refused(Exception):
    pass


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------

def one_line(text):
    return " ".join(str(text or "").split())


def cut(text, limit):
    return group.cut(text, limit) if limit > 0 else ""


def shorten(value, limit):
    """value cut to limit characters, keeping a quoted name's closing quote."""
    s = str(value)
    if len(s) <= limit:
        return s
    if s.endswith('"') and '"' in s[:-1]:
        head, inner = s[:-1].split('"', 1)
        room = limit - len(head) - 2
        if room >= 2:
            return f'{head}"{cut(inner, room)}"'
    return cut(s, limit)


def fill(value, facts):
    """A template string, or a list of them, filled with the finding's facts."""
    if isinstance(value, str):
        return value.format(**facts).strip()
    if isinstance(value, list):
        return [fill(v, facts) for v in value]
    return value


def fill_within(template, facts, limit):
    """fill(), shortening the longest fact it uses until the text fits in limit characters."""
    text = fill(template, facts)
    names = sorted({n for _, n, _, _ in string.Formatter().parse(template) if n})
    short = dict(facts)
    while len(text) > limit and names:
        name = max(names, key=lambda n: (len(str(short[n])), n))
        current = str(short[name])
        if len(current) <= 8:
            break
        short[name] = shorten(current, max(8, len(current) - (len(text) - limit)))
        text = fill(template, short)
    return text if len(text) <= limit else cut(text, limit)


def short_evidence(facts_text, reason, limit=EVIDENCE_MAX):
    """One line: the measured facts first, the verifier's reason last. The facts are shortened first."""
    facts_text, reason = one_line(facts_text), one_line(reason)
    if facts_text and facts_text[-1] not in ".!?…":
        facts_text += "."
    tail = f"Verifier: {reason}" if reason else ""
    if not tail or not facts_text:
        return cut(facts_text or tail, limit)
    whole = f"{facts_text} {tail}"
    if len(whole) <= limit:
        return whole
    keep = min(len(facts_text), FACTS_MIN)
    room = limit - len(tail) - 1
    if room >= keep:
        return f"{cut(facts_text, room)} {tail}"
    return f"{cut(facts_text, keep)} {cut(tail, limit - keep - 1)}"


def compact(value, limit=160):
    """A measured value as `key=value; …` on one line."""
    if isinstance(value, dict):
        parts = [f"{k}={compact(v, 200)}" for k, v in sorted(value.items())
                 if k != "occurrence" and v not in (None, "", [], {})]
        return cut("; ".join(parts), limit)
    if isinstance(value, (list, tuple)):
        return cut(", ".join(compact(v, 200) for v in value), limit)
    return cut(one_line(value), limit)


def run_date(ws, run_id):
    created = ws.run_doc(run_id).get("created") or ""
    return created[:10] if len(created) >= 10 else run_id[:10]


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def load(ws, run_id):
    d, ctx = diff.compute(ws, run_id)
    findings_doc = read_json(ws.file("findings.json", run_id)) or {}
    areas = mapdata.load_areas(ws.data_dir, snapshot_dir=False)
    ctx.update({
        "diff": d,
        "doc": findings_doc,
        "by_key": {f["key"]: f for f in findings_doc.get("findings", [])},
        "decisions": read_jsonl(ws.file("decisions.jsonl", run_id)),
        "states": ws.states(run_id),
        "run_doc": ws.run_doc(run_id),
        "outcomes_doc": read_json(ws.file("outcomes.json", run_id)) or {},
        "areas": {a["area"]: a for a in areas},
        "raw_issues": {i["id"]: i for a in areas for i in a.get("issues", [])},
        "wcag": load_wcag(),
        "date": run_date(ws, run_id),
        "raw_dir": ws.raw_dir(run_id),
        "raws": {},
    })
    return ctx


# ---------------------------------------------------------------------------
# Issues and overlays
# ---------------------------------------------------------------------------

def criteria_refs(criteria, ctx):
    return [{"number": c, "name": ctx["wcag"][c]["name"], "url": ctx["wcag"][c]["url"]}
            for c in criteria if c in ctx["wcag"]]


def full_evidence(f, v, ctx):
    """Measured facts, the sampled raw evidence, the verifier's probes and reason (KTD23)."""
    lines = [one_line(f.get("summary"))]
    for s in verify.sampled_evidence(ctx["raw_dir"], f, ctx["raws"]):
        lines.append(f"{s['state']}: {compact(s['evidence'], 800)}")
    for p in v.get("probes") or []:
        lines.append(f"Probe {p.get('id')} on {p.get('state')} ({p.get('kind')}): {compact(p.get('value') or {})}")
    if v.get("counterEvidence"):
        lines.append(f"Evidence reference: {v['counterEvidence']}")
    checked = ", ".join(v.get("states") or [])
    lines.append(f"Verifier ({v.get('date')}, checked {checked or 'no state'}): {one_line(v.get('reason'))}")
    return "\n".join(x for x in lines if x)


def audit_block(f, v, status, ctx):
    return {"criteria": criteria_refs(f["criteria"], ctx), "family": f["family"], "verdict": v["outcome"],
            "reason": one_line(v.get("reason")), "date": v.get("date"), "build": v.get("build"),
            "evidence": full_evidence(f, v, ctx), "checkedStates": list(v.get("states") or []),
            "run": ctx["run"], "status": status}


def fresh_issue(f, v, e, iid, ctx):
    """A generated issue from this run's finding and verdict."""
    t = group.finding_templates(ctx["checks"], f["family"], f["rule"], f.get("basis"))
    facts = f.get("facts") or {}
    copy = {k: fill(val, facts) for k, val in t["copy"].items() if k != "title"}
    copy["title"] = fill_within(t["copy"]["title"], facts, TITLE_MAX)
    copy["impact"] = "accessibility"
    copy["demoArtifactRisk"] = False
    return {"id": iid, "area": group.area_of(e["nodeIds"][0], ctx["states"]), "title": f["title"],
            "severity": e["severityAfter"], "category": "a11y", "detail": fill(t["detail"], facts),
            "evidence": short_evidence(f.get("summary"), v.get("reason")), "nodeIds": list(e["nodeIds"]),
            "wcag": list(f["criteria"]), "copy": copy, "audit": audit_block(f, v, e["status"], ctx)}


def carried_issue(prior, e, iid, ctx):
    """The issue the snapshot in force holds, with this run's nodeIds and status (R26)."""
    base = prior.row if prior.kind == diff.LIVE else (prior.row or {}).get("issue")
    if not base:
        return None
    issue = dict(base, id=iid, nodeIds=[n for n in e["nodeIds"] if n in ctx["nodes"]])
    issue["area"] = group.area_of(issue["nodeIds"][0], ctx["states"]) if issue["nodeIds"] else base.get("area")
    if issue.get("audit"):
        issue["audit"] = dict(issue["audit"], status=e["status"])
    return issue


def build_issue(key, e, iid, ctx, problems):
    f, v = ctx["by_key"].get(key), ctx["verdicts"].get(key)
    if f and v and v["outcome"] in diff.VERIFIED:
        try:
            return fresh_issue(f, v, e, iid, ctx)
        except (KeyError, IndexError, ValueError) as err:
            problems.append(f"{key}: its templates cannot be filled from its facts ({err!r})")
            return None
    issue = carried_issue(ctx["priors"].get(key), e, iid, ctx)
    if issue is None:
        problems.append(f"{key}: not retested, and the snapshot in force holds no issue to carry forward")
    return issue


def disambiguate(issues, ctx, field):
    """Give generated issues that share a title distinct ones: the screen, then a number (KTD24)."""
    def get(i):
        return i["copy"]["title"] if field == "copy" else i["title"]

    def put(i, text):
        if field == "copy":
            i["copy"] = dict(i["copy"], title=text)
        else:
            i["title"] = text

    groups = collections.defaultdict(list)
    for i in issues:
        groups[get(i).lower()].append(i)
    for same in groups.values():
        if len(same) < 2:
            continue
        same.sort(key=lambda i: ((i.get("audit") or {}).get("status") not in ("persisting", "re-rated", "not-retested"),
                                 i["id"]))
        used = {get(same[0]).lower()}
        for n, i in enumerate(same[1:], 2):
            f = ctx["by_key"].get(i.get("key")) or {}
            screen = (f.get("facts") or {}).get("screen") or ctx["titles"].get(i["nodeIds"][0]) or i["nodeIds"][0]
            for suffix in (f" on {screen}", f" ({n})"):
                text = cut(get(i), TITLE_MAX - len(suffix)) + suffix
                if text.lower() not in used:
                    break
            put(i, text)
            used.add(text.lower())


def hand_overlay(hf, v, e, add, ctx):
    """The overlay for one hand-found issue: a pure function of its raw record and latest verdict."""
    status = e["status"]
    if status in HAND_VERIFIED:
        rerate, drop = v["outcome"] == "rerate", v["outcome"] == "drop"
        history = None
        if rerate:
            history = f"rated {v['severityAfter']} on {v['date']}, previously {v['severityBefore']}"
        elif drop:
            history = f"not reproduced on {v['date']}: {one_line(v.get('reason'))}"
        return {"severity": v["severityAfter"] if rerate else None,
                "evidenceAppend": f"Verifier ({v['date']}): {one_line(v.get('reason'))}" if rerate else None,
                "addNodeIds": [] if drop else add, "wcag": list(hf["criteria"]),
                "audit": audit_block(hf, v, status, ctx), "history": history}
    prev = ((ctx["snapshot"]["issues"] or {}).get("overlays") or {}).get(hf["issueId"])
    ov = dict(prev) if prev else {"severity": None, "evidenceAppend": None, "addNodeIds": [],
                                  "wcag": list(hf["criteria"]), "audit": None, "history": None}
    ov["addNodeIds"] = add
    if ov.get("audit"):
        ov["audit"] = dict(ov["audit"], status=status)
    return ov


# ---------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------

def slug_parts(key, f):
    """(prefix, element) a new id's slug is made of: `<rule>-<messageKey>` for an incomplete group, which
    is one group per rule and messageKey, else the family and the element words."""
    facts = f.get("facts") or {}
    if f.get("basis") == "incomplete":
        return f["rule"], facts.get("messageKey")
    return (f.get("family") or key.split(":")[0],
            facts.get("element") or facts.get("message") or facts.get("overlay") or f.get("title"))


def apply_registry(ctx, problems):
    """(registry, {finding key: [hand-found ids]}, {key: issue id}) after this run's decisions and statuses.

    The snapshot's registry is copied once and changed in place."""
    d, run, date = ctx["diff"], ctx["run"], ctx["date"]
    reg = registry.copied(ctx["snapshot"]["registry"])
    held = len(reg["held"])
    for dec in ctx["decisions"]:
        if dec.get("decision") == "attach":
            registry.attach_in_place(reg, dec["finding"], dec["handFound"], run, dec.get("date") or date)
        elif dec.get("decision") == "release":
            registry.release_in_place(reg, dec["finding"], dec["handFound"])
    dropped = {hid for hid, e in d["handFound"].items() if e["status"] == "not-reproduced"}
    for a in list(reg["attachments"]):
        if a["handFound"] in dropped:
            registry.release_in_place(reg, a["finding"], a["handFound"])
    hand_ids = sorted(ctx["raw_issues"])
    ids = {}
    for key, e in sorted(d["findings"].items()):
        attached = [a["handFound"] for a in reg["attachments"] if a["finding"] == key]
        iid = reg["keys"].get(key)
        tomb = reg["tombstones"].get(iid) if iid else None
        if e["status"] == "fixed":
            for hid in attached:
                registry.release_in_place(reg, key, hid)
            if iid:
                registry.tombstone_in_place(reg, iid, "fixed", run, date)
        elif e["status"] == "withdrawn":
            if iid and not attached and tomb is None:
                registry.tombstone_in_place(reg, iid, "withdrawn", run, date)
        elif in_map(key, e, ctx) and not attached:
            if iid is None:
                prefix, element = slug_parts(key, ctx["by_key"].get(key) or {})
                area = group.area_of(e["nodeIds"][0], ctx["states"])
                iid = registry.mint_in_place(reg, key, area, prefix, element, hand_ids)
            elif tomb is not None:
                registry.untombstone_in_place(reg, iid)
            ids[key] = iid
    if len(reg["held"]) != held:        # ids were minted: sort held once, as registry.mint does after each
        reg["held"] = sorted(set(reg["held"]))
    attached_to = collections.defaultdict(list)
    for a in reg["attachments"]:
        attached_to[a["finding"]].append(a["handFound"])
    problems += [f"registry: {p}" for p in registry.violations(reg, hand_ids)]
    return reg, attached_to, ids


def in_map(key, e, ctx):
    """True when the finding is in the map after this run: verified, or not retested but published before."""
    if e["status"] in PUBLISHED:
        return True
    prior = ctx["priors"].get(key)
    return e["status"] == "not-retested" and prior is not None and prior.kind in diff.IN_MAP


def build_snapshot(ws, run_id):
    """({file name: document}, [problems], the context it was built from) for the run, without writing anything."""
    ctx = load(ws, run_id)
    ctx["titles"] = group.node_titles({"areas": list(ctx["areas"].values())})
    problems = []
    d = ctx["diff"]
    reg, attached_to, ids = apply_registry(ctx, problems)

    generated, attached = [], {}
    for key, e in sorted(d["findings"].items()):
        if not in_map(key, e, ctx):
            continue
        if key in attached_to:
            iid = reg["keys"].get(key)
            issue = build_issue(key, e, iid or attached_to[key][0], ctx, problems)
            if issue:
                attached[key] = dict(issue, id=iid)
        else:
            issue = build_issue(key, e, ids[key], ctx, problems)
            if issue:
                generated.append(dict(issue, key=key))
    disambiguate(generated, ctx, "title")
    disambiguate(generated, ctx, "copy")

    overlays = {}
    hand = {f["issueId"]: f for f in ctx["findings"] if f["source"] == "hand-found" and f["issueId"] in ctx["raw_issues"]}
    for hid, hf in sorted(hand.items()):
        raw_nodes = set(ctx["raw_issues"][hid]["nodeIds"])
        add = sorted({n for k, hs in attached_to.items() if hid in hs and k in attached
                      for n in attached[k]["nodeIds"]} - raw_nodes)
        overlays[hid] = hand_overlay(hf, ctx["verdicts"].get(hf["key"]), d["handFound"][hid], add, ctx)

    issues_doc = {"generated": sorted(({k: v for k, v in i.items() if k != "key"} for i in generated),
                                      key=lambda i: (i["area"],) + mapdata.issue_order(i)),
                  "overlays": overlays}
    lighthouse = lighthouse_of(ws, ctx)
    meta = build_meta(ctx, lighthouse)
    report = build_report(ctx, reg, attached_to, attached, generated, overlays, hand, lighthouse)
    docs = {"issues.json": issues_doc, "report.json": report, "registry.json": reg, "meta.json": meta}
    problems += export_problems(issues_doc, ctx) + id_problems(issues_doc, ctx) + link_problems(report, issues_doc, ctx)
    return docs, problems, ctx


# ---------------------------------------------------------------------------
# report.json (KTD24)
# ---------------------------------------------------------------------------

def principle(criteria, ctx):
    return ctx["wcag"][criteria[0]]["principle"] if criteria and criteria[0] in ctx["wcag"] else None


def criterion_key(c):
    return group.criterion_key(c) if c else (99,)


def curated_fix(iid):
    return (export.CURATED.get(iid) or {}).get("fixHypothesis")


def build_report(ctx, reg, attached_to, attached, generated, overlays, hand, lighthouse):
    d, run = ctx["diff"], ctx["run"]
    rows, tables = [], {name: [] for name in TABLES}
    for i in generated:
        e = d["findings"][i["key"]]
        if e["status"] not in PUBLISHED:
            continue
        f = ctx["by_key"].get(i["key"]) or {}
        rows.append({"id": i["id"], "key": i["key"], "source": "scanner", "title": i["title"], "criteria": i["wcag"],
                     "principle": principle(i["wcag"], ctx), "family": i["audit"]["family"],
                     "severity": i["severity"], "severityBefore": e["severityBefore"], "screens": len(i["nodeIds"]),
                     "status": e["status"], "fix": i["copy"].get("fixHypothesis"), "evidence": i["evidence"],
                     "facts": f.get("facts") or {}})

    hand_rows = []
    for hid, hf in sorted(hand.items()):
        e, v, raw, ov = d["handFound"][hid], ctx["verdicts"].get(hf["key"]), ctx["raw_issues"][hid], overlays[hid]
        screens = len(set(raw["nodeIds"]) | set(ov["addNodeIds"]))
        evidence = raw.get("evidence", "")
        if ov.get("evidenceAppend"):
            evidence = f"{evidence.rstrip()} {ov['evidenceAppend']}".strip()
        att = []
        for key in sorted(k for k, hs in attached_to.items() if hid in hs):
            ae = d["findings"].get(key) or {}
            issue = attached.get(key)
            f = ctx["by_key"].get(key) or {}
            prior = ctx["priors"].get(key)
            title = (issue or {}).get("title") or f.get("title") or ((prior.row or {}).get("title") if prior else None)
            att.append({"key": key, "formerId": reg["keys"].get(key), "title": title,
                        "criteria": (issue or {}).get("wcag") or f.get("criteria") or [],
                        "family": f.get("family") or ((issue or {}).get("audit") or {}).get("family"),
                        "severity": (issue or {}).get("severity") or ae.get("severityAfter"),
                        "nodeIds": (issue or {}).get("nodeIds") or ae.get("nodeIds") or [],
                        "status": ae.get("status"), "issue": issue})
        hand_rows.append({"id": hid, "title": raw["title"], "status": e["status"], "reason": e["reason"],
                          "verifierReason": one_line(v.get("reason")) if v else None,
                          "severityBefore": e["severityBefore"], "severityAfter": e["severityAfter"],
                          "criteria": list(hf["criteria"]), "family": hf["family"], "screens": screens,
                          "date": v.get("date") if v else None, "attached": att})
        if e["status"] in ("first-audited", "confirmed", "re-rated"):
            rows.append({"id": hid, "key": hf["key"], "source": "hand-found", "title": raw["title"],
                         "criteria": list(hf["criteria"]), "principle": principle(hf["criteria"], ctx),
                         "family": hf["family"], "severity": e["severityAfter"], "severityBefore": e["severityBefore"],
                         "screens": screens, "status": e["status"], "fix": curated_fix(hid), "evidence": evidence,
                         "facts": {}})

    rank = mapdata.SEVERITY_RANK
    rows.sort(key=lambda r: (criterion_key(r["criteria"][0] if r["criteria"] else None), rank[r["severity"]],
                             -r["screens"], r["id"]))
    for r in rows:
        for name, families in TABLES.items():
            if r["family"] in families:
                row = {"id": r["id"], "title": r["title"], "criterion": r["criteria"][0] if r["criteria"] else None,
                       "rule": (ctx["by_key"].get(r["key"]) or {}).get("rule"), "severity": r["severity"],
                       "screens": r["screens"], "status": r["status"]}
                row.update({k: r["facts"].get(k) for k in TABLE_FACTS[name]})
                if "elements" in r["facts"]:        # an incomplete group names no single element
                    row["element"] = r["facts"]["elements"]
                if name == "keyboard":
                    row["arrowKeys"] = "not tested"
                tables[name].append(row)
    for r in rows:
        del r["facts"]
    priority = [r["id"] for r in sorted(rows, key=lambda r: (rank[r["severity"]], -r["screens"], r["id"]))]

    published_ids = {i["id"] for i in generated}
    generated_by_key = {i["key"]: i for i in generated}
    not_retested, withdrawn, fixed = [], [], []
    for key, e in sorted(d["findings"].items()):
        f = ctx["by_key"].get(key) or {}
        prior = ctx["priors"].get(key)
        prow = (prior.row or {}) if prior else {}
        title = f.get("title") or prow.get("title")
        criteria = f.get("criteria") or prow.get("wcag") or prow.get("criteria") or []
        family = f.get("family") or (prow.get("audit") or {}).get("family") or prow.get("family") or key.split(":")[0]
        iid = reg["keys"].get(key)
        if e["status"] == "not-retested":
            if key in attached_to:
                continue        # listed with the hand-found issue it is attached to
            issue = generated_by_key.get(key)
            link = iid if iid in published_ids else None
            audit = (issue or {}).get("audit") or {}
            not_retested.append({"id": link, "key": key, "title": title, "criteria": criteria, "family": family,
                                 "severity": e["severityAfter"], "screens": len(e["nodeIds"]), "nodeIds": e["nodeIds"],
                                 "reason": e["reason"],
                                 "lastVerified": {"run": audit.get("run"), "date": audit.get("date")} if audit else None})
        elif e["status"] == "withdrawn":
            v = ctx["verdicts"].get(key) or {}
            withdrawn.append({"id": None, "formerId": iid, "key": key, "title": title, "criteria": criteria,
                              "family": family, "severity": e["severityAfter"], "screens": len(e["nodeIds"]),
                              "nodeIds": e["nodeIds"], "reason": one_line(v.get("reason")),
                              "reasonCode": e["reason"], "run": run})
        elif e["status"] == "fixed":
            fixed.append({"id": None, "formerId": iid, "key": key, "title": title, "criteria": criteria,
                          "family": family, "severity": e["severityBefore"], "screens": len(e["nodeIds"]),
                          "reason": e["reason"], "run": run})
    for row in (ctx["snapshot"]["report"] or {}).get("withdrawn") or []:
        key = row.get("key")
        iid = reg["keys"].get(key)
        if key and key not in d["findings"] and (reg["tombstones"].get(iid) or {}).get("reason") == "withdrawn":
            withdrawn.append(row)
    withdrawn.sort(key=lambda r: r["key"])

    f_counts, h_counts = diff.counts(d)
    for status in PUBLISHED:
        f_counts[status] = sum(1 for r in rows if r["source"] == "scanner" and r["status"] == status)
    f_counts.update({"not-retested": len(not_retested), "fixed": len(fixed),
                     "withdrawn": sum(1 for r in withdrawn if r.get("run") == run)})
    return {
        "run": run, "date": ctx["date"], "captured": export.CAPTURED, "baseline": d["baseline"], "standard": STANDARD,
        "counts": {s: sum(1 for r in rows if r["severity"] == s) for s in rank},
        "coverage": build_coverage(ctx, lighthouse),
        "changes": {"findings": f_counts, "handFound": h_counts},
        "findings": rows, "tables": tables,
        "advisory": ctx["doc"].get("advisory") or [],
        "priority": priority, "notRetested": not_retested, "withdrawn": withdrawn, "fixed": fixed,
        "handFound": hand_rows, "unreachable": unreachable_groups(ctx),
    }


def build_coverage(ctx, lighthouse):
    checks_data = ctx["checks"]
    outcomes = ctx["outcomes"]
    rules, ran = set(), set()
    for sid, o in sorted(outcomes.items()):
        if o.get("outcome") != "audited":
            continue
        ran.update(o.get("completed") or [])
        if "axe" in (o.get("completed") or []):
            raw = read_json(os.path.join(ctx["raw_dir"], f"{sid}.json")) or {}
            rules.update(((raw.get("checks") or {}).get("axe") or {}).get("rulesRan") or [])
    by_criterion = collections.defaultdict(set)
    for rule in rules:
        # axeCriteria holds axe-core's wcagNNN tags; a criterion outside wcag.json (2.1.3 is AAA) drops out below
        for c in checks_data["axeCriteria"].get(rule, []):
            by_criterion[c].add(rule)
    for rule, module in checks_data["customChecks"].items():
        if module in ran:
            for c in checks_data["customCriteria"].get(rule, []):
                by_criterion[c].add(rule)
    if "records" in ran:
        for c in RECORD_CRITERIA:
            by_criterion[c].add("announced-as")
    tested, untested = [], []
    for c, info in sorted(ctx["wcag"].items(), key=lambda x: criterion_key(x[0])):
        base = {"number": c, "name": info["name"], "principle": info["principle"], "level": info["level"]}
        checks = sorted(by_criterion.get(c, ()))
        if checks and c not in NEVER_TESTED:
            full_when = checks_data["fullCoverage"].get(c)
            full = full_when is not None and set(full_when) <= set(checks)
            tested.append(dict(base, coverage="full" if full else "partial", checks=checks,
                               modes=checks_data["failureModes"].get(c) or f"Checked by {', '.join(checks)}."))
        else:
            untested.append(dict(base, named=c in NEVER_TESTED))
    builds = doc_builds(ctx) | {o.get("build") for o in outcomes.values() if o.get("build")}
    return {**build_coverage_counts(ctx),
            "tested": tested, "untested": untested, "dimensions": list(DIMENSIONS), "builds": sorted(builds),
            "lighthouse": lighthouse_line(lighthouse), "caveat": export.CAVEAT}


def doc_builds(ctx):
    """The build hashes run.json and outcomes.json record."""
    return set(ctx["run_doc"].get("builds") or []) | set(ctx["outcomes_doc"].get("builds") or [])


def lighthouse_line(lh):
    if not lh:
        return {"run": None, "date": None, "unexplained": None, "line": "Lighthouse calibration has not run."}
    r = lh.get("result") or {}
    n = len(r.get("states") or [])
    line = (f"Lighthouse (axe {r.get('lighthouseAxe')}, Chrome {r.get('chrome')}) audited "
            f"{plural(n, 'state')} on {lh.get('date')} (run {lh.get('run')}): "
            f"{plural(r.get('unexplained', 0), 'unexplained difference')} from the sweep.")
    return {"run": lh.get("run"), "date": lh.get("date"), "unexplained": r.get("unexplained"), "line": line}


def unreachable_groups(ctx):
    groups = collections.defaultdict(list)
    for sid, o in sorted(ctx["outcomes"].items()):
        if o.get("outcome") != "unreachable":
            continue
        root = (o.get("detail") or {}).get("rootCause") or sid
        groups[root].append({"state": sid, "reason": o.get("reason"), "firstUnreachable": o.get("firstUnreachable")})
    return [{"rootCause": root, "reason": next((s["reason"] for s in states if s["state"] == root), states[0]["reason"]),
             "firstUnreachable": min((s["firstUnreachable"] for s in states if s["firstUnreachable"]), default=None),
             "states": states} for root, states in sorted(groups.items())]


# ---------------------------------------------------------------------------
# meta.json
# ---------------------------------------------------------------------------

def lighthouse_of(ws, ctx):
    """This run's Lighthouse calibration when it is due or the snapshot has none, else the snapshot's."""
    run_id = ctx["run"]
    mine = read_json(ws.file("lighthouse.json", run_id))
    previous = (ctx["snapshot"]["meta"] or {}).get("lighthouse")
    if mine and (due(ws, run_id) or not previous):
        return {"run": run_id, "date": mine.get("date"), "result": mine}
    return previous


def build_meta(ctx, lighthouse):
    return {"run": ctx["run"], "date": ctx["date"], "builds": sorted(doc_builds(ctx)),
            "versions": ctx["run_doc"].get("versions") or {}, "coverage": build_coverage_counts(ctx),
            "lighthouse": lighthouse}


def build_coverage_counts(ctx):
    audited = sum(1 for o in ctx["outcomes"].values() if o.get("outcome") == "audited")
    return {"states": len(ctx["outcomes"]), "audited": audited, "unreachable": len(ctx["outcomes"]) - audited}


# ---------------------------------------------------------------------------
# Gates (KTD21)
# ---------------------------------------------------------------------------

def export_problems(issues_doc, ctx):
    """The ticket export's rules, applied to every generated issue and overlay before they can break a build."""
    out = []
    for i in issues_doc["generated"]:
        iid = i.get("id")
        for k in mapdata.VIEWER_FIELDS + ("wcag",):
            if i.get(k) in (None, "", []):
                out.append(f"{iid}: empty {k}")
        if len(i.get("title") or "") > TITLE_MAX:
            out.append(f"{iid}: title longer than {TITLE_MAX} characters")
        if i.get("severity") not in export.SEVERITIES:
            out.append(f"{iid}: bad severity {i.get('severity')}")
        if len(i.get("evidence") or "") > EVIDENCE_MAX or "\n" in (i.get("evidence") or ""):
            out.append(f"{iid}: evidence is not one line of at most {EVIDENCE_MAX} characters")
        if i.get("area") not in ctx["areas"]:
            out.append(f"{iid}: area {i.get('area')} is not in the data")
        unknown = [n for n in i.get("nodeIds") or [] if n not in ctx["nodes"]]
        if unknown:
            out.append(f"{iid}: unknown nodeIds {', '.join(unknown)}")
        copy = i.get("copy") or {}
        for k in COPY_KEYS:
            if k not in COPY_MAY_BE_EMPTY and copy.get(k) in (None, "", []):
                out.append(f"{iid}: empty copy.{k}")
        if len(copy.get("title") or "") > TITLE_MAX:
            out.append(f"{iid}: copy title longer than {TITLE_MAX} characters")
        if copy.get("impact") not in export.IMPACTS:
            out.append(f"{iid}: bad impact {copy.get('impact')}")
        if copy.get("confidence") not in export.CONFIDENCES:
            out.append(f"{iid}: bad confidence {copy.get('confidence')}")
    for hid, ov in issues_doc["overlays"].items():
        if ov.get("severity") not in (None,) + tuple(export.SEVERITIES):
            out.append(f"{hid}: bad overlay severity {ov.get('severity')}")
        unknown = [n for n in ov.get("addNodeIds") or [] if n not in ctx["nodes"]]
        if unknown:
            out.append(f"{hid}: overlay adds unknown nodeIds {', '.join(unknown)}")
    return out


def id_problems(issues_doc, ctx):
    ids = list(ctx["raw_issues"]) + [i["id"] for i in issues_doc["generated"]]
    return [f"duplicate issue id {iid}" for iid, n in sorted(collections.Counter(ids).items()) if n > 1]


def report_links(report):
    links = list(report["priority"])
    for name in ("findings", "notRetested", "handFound"):
        links += [r["id"] for r in report[name] if r.get("id")]
    for rows in report["tables"].values():
        links += [r["id"] for r in rows if r.get("id")]
    return links


def link_problems(report, issues_doc, ctx):
    merged = set(ctx["raw_issues"]) | {i["id"] for i in issues_doc["generated"]}
    missing = sorted(set(report_links(report)) - merged)
    return [f"report links to {iid}, which is not in the merged issues" for iid in missing]


def ledger_problems(ws, run_id, ctx):
    """The gates that read the run's files: coverage, verdicts, proposals, canary, diff and its baseline."""
    out = []
    out += [f"coverage: {p}" for p in coverage_problems(ctx["states"], ctx["outcomes"])]
    missing = sorted(f["key"] for f in ctx["findings"] if f["key"] not in ctx["verdicts"])
    if missing:
        out.append(f"no verdict for {len(missing)} finding(s): {', '.join(missing)}")
    pending = pending_proposals(ws, run_id)
    if pending:
        out.append(f"undecided attachment proposal(s): {', '.join(pending)}")
    canary = verify.canary_state(ws, run_id)
    if canary["status"] != "passed":
        out.append(f"canary {canary['status']}: {canary.get('message') or 'every canary verdict must be correct'}")
    written = read_json(ws.file("diff.json", run_id))
    current = (ctx["snapshot"]["meta"] or {}).get("run") or "none"
    if not written:
        out.append("no diff.json: run `audit.py diff` first")
    else:
        if written.get("baseline") != current:
            out.append(f"diff.json compares with run {written.get('baseline')}, but the snapshot in force holds "
                       f"{current}: rerun `audit.py diff`")
        elif written != ctx["diff"]:
            out.append("diff.json is out of date with the ledger: rerun `audit.py diff`")
    return out


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def tidy(pub):
    """Restore a snapshot left between the two renames, and remove what a finished or failed publish left."""
    mapdata.restore_snapshot(pub)
    if os.path.isdir(pub) and os.path.exists(pub + ".prev"):
        shutil.rmtree(pub + ".prev")
    if os.path.exists(pub + ".tmp"):
        shutil.rmtree(pub + ".tmp")


def swap(pub, docs):
    """Write published.tmp/, check it, then rename published/ → .prev/, .tmp/ → published/, delete .prev/."""
    tmp, prev = pub + ".tmp", pub + ".prev"
    texts = {}
    for name in FILES:
        texts[name] = dump(docs[name])
        write_text(os.path.join(tmp, name), texts[name])
    for name in FILES:
        with open(os.path.join(tmp, name), encoding="utf-8") as f:
            if f.read() != texts[name]:
                shutil.rmtree(tmp)
                raise Refused(f"published.tmp/{name} does not read back as written")
    if os.path.exists(pub):
        os.rename(pub, prev)
    os.rename(tmp, pub)
    if os.path.exists(prev):
        shutil.rmtree(prev)


def publish(ws, run_id):
    """(summary, exit code). Never changes published/ unless every gate passes."""
    pub = ws.published_dir
    tidy(pub)
    meta = ws.published_meta() or {}
    if meta.get("run") == run_id:
        return {"run": run_id, "state": "already published"}, 0
    try:
        docs, problems, ctx = build_snapshot(ws, run_id)
    except diff.DiffError as e:
        return {"run": run_id, "state": "refused", "problems": [str(e)]}, 1
    problems = ledger_problems(ws, run_id, ctx) + problems
    if problems:
        return {"run": run_id, "state": "refused", "problems": problems}, 1
    try:
        swap(pub, docs)
    except Refused as e:
        return {"run": run_id, "state": "refused", "problems": [str(e)]}, 1
    issues = docs["issues.json"]
    return {"run": run_id, "state": "published", "baseline": docs["report.json"]["baseline"],
            "generated": len(issues["generated"]), "overlays": len(issues["overlays"]),
            "changes": docs["report.json"]["changes"]}, 0


def status(ws, run_id):
    meta = ws.published_meta() or {}
    written = read_json(ws.file("diff.json", run_id)) if run_id else None
    if run_id and meta.get("run") == run_id:
        return {"snapshot": meta.get("run"), "published": True}
    if not run_id or not written:
        return {"pending": True, "snapshot": meta.get("run")}
    return {"snapshot": meta.get("run"), "published": False,
            "baselineCurrent": written.get("baseline") == (meta.get("run") or "none")}


def cli(ws, sub, argv):
    p = argparse.ArgumentParser(prog="audit.py publish", description=__doc__.split("\n\n")[0])
    p.parse_args(argv)
    run_id = ws.run_id()
    summary, code = publish(ws, run_id)
    if code:
        print(f"publish: refused; published/ is unchanged ({len(summary['problems'])} problem(s))", file=sys.stderr)
        for line in summary["problems"]:
            print(f"  {line}", file=sys.stderr)
    print(dump(summary), end="")
    return code
