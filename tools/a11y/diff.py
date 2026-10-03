"""The `diff` stage (R21, R26; KTD18): each finding's status against the snapshot in force.

    python3 tools/a11y/audit.py diff [--run <id>]

Reads the run's findings.json, verdicts.jsonl (the latest verdict per key), outcomes.json (each
state's outcome and the check modules that completed on it) and records.jsonl, the area data in
--data (raw, for the screens that still exist), and the current snapshot's issues.json, report.json,
registry.json and meta.json. Writes diff.json with the snapshot's run id as its baseline, or `none`
on a first run.

Scanner findings: new, persisting, re-rated, not-retested, fixed, withdrawn, reappeared.
- "Fixed" needs positive evidence: the check that produces the finding completed on every prior
  screen that still exists, and none reproduced the key. A judge's "ok" never fixes a finding: a
  published announced-as finding whose record is still present stays not-retested until verified.
- A prior screen that no longer exists in --data drops out; with none left the finding is fixed,
  "screen removed".
- A published finding with a prior screen unreachable or its check incomplete, or whose
  verification could not finish, is not-retested. Its nodeIds are this run's screens plus the prior
  screens not retested, so they never shrink on a gap.
Hand-found issues: first-audited (a confirm on the first run), confirmed, re-rated, not-reproduced
(reason from the drop code), not-retested (no verdict, or a drop where one of its screens was
unreachable or its checks incomplete: an unreachable screen never produces "not reproduced").

After the run is published, diff leaves diff.json as it is: publish built the snapshot from it.
File formats: schemas.md ("diff.json").
"""
import argparse
import sys

import mapdata
import signature
from lib.canon import dump, latest_by, load_checks, read_json, read_jsonl, write_json
from verdict import VERIFIED

STATUSES = ("new", "persisting", "re-rated", "reappeared", "not-retested", "withdrawn", "fixed")
HAND_STATUSES = ("first-audited", "confirmed", "re-rated", "not-reproduced", "not-retested")
DROP_REASONS = {"fixed-since-capture": "fixed since capture", "tool-false-positive": "tool false positive"}
NOT_AA = "not an AA failure"
SCREEN_REMOVED = "screen removed"
LIVE, ATTACHED, WITHDRAWN, FIXED = "live", "attached", "withdrawn", "fixed"
IN_MAP = (LIVE, ATTACHED)
UNFINISHED = {"not-reached": "verifier could not reach the state",
              "unverified": "unverified after two verification attempts",
              None: "no verdict"}


class DiffError(Exception):
    pass


# ---------------------------------------------------------------------------
# Check modules
# ---------------------------------------------------------------------------

def check_of(rule, checks):
    """The check module that produces a rule: axe, records (announced-as) or a custom module."""
    if rule == "announced-as":
        return "records"
    if rule in checks["axeRules"]:
        return "axe"
    return checks["customChecks"].get(rule)


def family_checks(family, checks):
    """Every check module that produces a rule of the family."""
    custom = checks["customChecks"]
    out = {custom[r] for r, fam in checks["customRules"].items() if fam == family and r in custom}
    if any(fam == family for fam in checks["axeRules"].values()):
        out.add("axe")
    if family in signature.RECORD_FAMILIES:
        out.add("records")
    return out


def rule_of(key):
    """The rule in a scanner key `<family>:<rule>:<hash>`."""
    parts = key.split(":")
    return parts[1] if len(parts) == 3 else None


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def latest_verdicts(ws, run_id):
    return latest_by(read_jsonl(ws.file("verdicts.jsonl", run_id)), "key")


def read_snapshot(ws):
    return {name: ws.published(f"{name}.json") for name in ("issues", "report", "registry", "meta")}


def map_nodes(ws):
    return {n["id"] for a in mapdata.load_areas(ws.data_dir, snapshot_dir=False) for n in a.get("nodes", [])}


class Prior:
    """A key's place in the baseline snapshot: live (a generated issue), attached (screens added to a
    hand-found issue), withdrawn or fixed (tombstoned)."""

    def __init__(self, kind, issue_id=None, screens=(), severity=None, row=None):
        self.kind, self.issue_id, self.screens, self.severity, self.row = kind, issue_id, list(screens), severity, row


def priors(snapshot):
    """{key: Prior} for every key the baseline snapshot knows."""
    registry = snapshot["registry"] or {}
    report = snapshot["report"] or {}
    issues = {i["id"]: i for i in (snapshot["issues"] or {}).get("generated", [])}
    keys = registry.get("keys") or {}
    tombs = registry.get("tombstones") or {}
    attached = {}
    for row in report.get("handFound") or []:
        for a in row.get("attached") or []:
            attached[a["key"]] = a
    withdrawn = {r["key"]: r for r in report.get("withdrawn") or [] if r.get("key")}
    out = {}
    for a in registry.get("attachments") or []:
        row = attached.get(a["finding"]) or {}
        out[a["finding"]] = Prior(ATTACHED, keys.get(a["finding"]), row.get("nodeIds") or [], row.get("severity"), row)
    for key, iid in keys.items():
        if key in out:
            continue
        t = tombs.get(iid)
        if t is None and iid in issues:
            out[key] = Prior(LIVE, iid, issues[iid].get("nodeIds") or [], issues[iid].get("severity"), issues[iid])
        elif t and t.get("reason") == WITHDRAWN:
            row = withdrawn.get(key) or {}
            out[key] = Prior(WITHDRAWN, iid, row.get("nodeIds") or [], row.get("severity"), row)
        elif t and t.get("reason") == FIXED:
            out[key] = Prior(FIXED, iid)
    return out


def load(ws, run_id):
    doc = read_json(ws.file("findings.json", run_id))
    if not doc:
        raise DiffError(f"run {run_id} has no findings.json: run `audit.py group` first")
    outcomes = read_json(ws.file("outcomes.json", run_id))
    if not outcomes:
        raise DiffError(f"run {run_id} has no outcomes.json: finish `audit.py sweep` first")
    snapshot = read_snapshot(ws)
    meta = snapshot["meta"] or {}
    return {
        "run": run_id,
        "baseline": meta.get("run") or "none",
        "findings": doc.get("findings", []),
        "verdicts": latest_verdicts(ws, run_id),
        "outcomes": outcomes.get("states") or {},
        "sigs": {line["sig"] for line in read_jsonl(ws.file("records.jsonl", run_id))},
        "record_keys": {},      # family: the announced-as keys of this run's records (record_present)
        "nodes": map_nodes(ws),
        "snapshot": snapshot,
        "priors": priors(snapshot),
        "checks": load_checks(),
    }


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

def retested(ctx, sid, check):
    """True when the state was audited and the check that produces the finding completed on it."""
    o = ctx["outcomes"].get(sid) or {}
    return o.get("outcome") == "audited" and check is not None and check in (o.get("completed") or [])


def gap_reason(ctx, gaps, check):
    unreachable = [s for s in gaps if (ctx["outcomes"].get(s) or {}).get("outcome") != "audited"]
    incomplete = [s for s in gaps if s not in unreachable]
    out = []
    if unreachable:
        out.append(f"unreachable: {', '.join(unreachable)}")
    if incomplete:
        out.append(f"check incomplete: {', '.join(incomplete)}" if check else
                   f"no check produces this rule: {', '.join(incomplete)}")
    return out


def primary_first(primary, screens):
    rest = sorted(set(screens) - {primary})
    return ([primary] if primary in screens else []) + rest


def record_present(key, ctx):
    """True when one of this run's records still has the signature an announced-as key came from."""
    family = key.split(":")[0]
    if family not in ctx["record_keys"]:
        ctx["record_keys"][family] = {signature.record_finding_key(family, sig) for sig in ctx["sigs"]}
    return key in ctx["record_keys"][family]


# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------

def entry(status, issue_id, before, after, node_ids, reason=None):
    return {"status": status, "issueId": issue_id, "severityBefore": before, "severityAfter": after,
            "nodeIds": node_ids, "reason": reason}


def present(f, v, prior, ctx):
    """Status of a scanner finding the sweep reported this run."""
    key = f["key"]
    check = check_of(f["rule"], ctx["checks"])
    here = primary_first(f["primary"], f["states"])
    kind = prior.kind if prior else None
    iid = ctx["registry_keys"].get(key)
    before = prior.severity if kind in (LIVE, ATTACHED, WITHDRAWN) else None
    outcome = v["outcome"] if v else None
    gaps, why = [], []
    if kind in IN_MAP:
        found = set(f["states"])
        gaps = [s for s in prior.screens if s in ctx["nodes"] and s not in found and not retested(ctx, s, check)]
        why = gap_reason(ctx, gaps, check)
    union = primary_first(f["primary"], f["states"] + gaps)
    if outcome == "drop":
        return entry("withdrawn", iid, before, v["severityAfter"], here, v.get("reasonCode"))
    if outcome in VERIFIED:
        after = v["severityAfter"]
        if kind in IN_MAP and gaps:
            return entry("not-retested", iid, before, after, union, "; ".join(why))
        if kind in IN_MAP:
            return entry("re-rated" if after != before else "persisting", iid, before, after, here)
        if kind == FIXED:
            return entry("reappeared", iid, before, after, here)
        if kind == WITHDRAWN:
            return entry("persisting", iid, before, after, here)
        return entry("new", iid, before, after, here)
    reason = "; ".join([UNFINISHED.get(outcome, UNFINISHED[None])] + why)
    if kind in IN_MAP:
        return entry("not-retested", iid, before, before or f["severityDefault"], union, reason)
    return entry("not-retested", iid, before, f["severityDefault"], here, reason)


def absent(key, prior, ctx):
    """Status of a baseline key the sweep did not report this run, or None when nothing changed."""
    if prior.kind not in (LIVE, ATTACHED, WITHDRAWN) or not prior.row:
        return None     # fixed earlier, or the baseline holds no screens to compare
    check = check_of(rule_of(key), ctx["checks"])
    screens = [s for s in prior.screens if s in ctx["nodes"]]
    before = prior.severity
    if not screens:
        return entry("fixed", prior.issue_id, before, before, list(prior.screens), SCREEN_REMOVED)
    if rule_of(key) == "announced-as" and record_present(key, ctx):
        if prior.kind not in IN_MAP:
            return None
        return entry("not-retested", prior.issue_id, before, before, screens,
                     "announced-as record still present, not verified")
    gaps = [s for s in screens if not retested(ctx, s, check)]
    if not gaps:
        return entry("fixed", prior.issue_id, before, before, list(prior.screens))
    if prior.kind not in IN_MAP:
        return None
    return entry("not-retested", prior.issue_id, before, before, gaps, "; ".join(gap_reason(ctx, gaps, check)))


def hand_entry(status, before, after, reason=None):
    return {"status": status, "reason": reason, "severityBefore": before, "severityAfter": after}


def hand_found(f, v, ctx):
    """Status of a hand-found issue."""
    before = f["severityDefault"]
    outcome = v["outcome"] if v else None
    if outcome not in VERIFIED + ("drop",):
        return hand_entry("not-retested", before, before)
    if outcome == "drop":
        checks = family_checks(f["family"], ctx["checks"])
        for s in f["states"]:
            if s not in ctx["nodes"]:
                continue
            o = ctx["outcomes"].get(s) or {}
            if o.get("outcome") != "audited" or checks & set(o.get("incomplete") or []):
                return hand_entry("not-retested", before, before)
        return hand_entry("not-reproduced", before, before, DROP_REASONS.get(v.get("reasonCode"), NOT_AA))
    if outcome == "rerate":
        return hand_entry("re-rated", before, v["severityAfter"])
    return hand_entry("first-audited" if ctx["baseline"] == "none" else "confirmed", before, before)


def compute(ws, run_id):
    """(diff.json document, context). The context holds the inputs publish reuses."""
    ctx = load(ws, run_id)
    ctx["registry_keys"] = (ctx["snapshot"]["registry"] or {}).get("keys") or {}
    findings, hand = {}, {}
    seen = set()
    for f in ctx["findings"]:
        v = ctx["verdicts"].get(f["key"])
        if f["source"] == "hand-found":
            hand[f["issueId"]] = hand_found(f, v, ctx)
            continue
        seen.add(f["key"])
        findings[f["key"]] = present(f, v, ctx["priors"].get(f["key"]), ctx)
    for key, prior in sorted(ctx["priors"].items()):
        if key in seen:
            continue
        e = absent(key, prior, ctx)
        if e:
            findings[key] = e
    return {"run": run_id, "baseline": ctx["baseline"], "findings": findings, "handFound": hand}, ctx


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

def counts(doc):
    f = {s: 0 for s in STATUSES}
    for e in doc.get("findings", {}).values():
        f[e["status"]] = f.get(e["status"], 0) + 1
    h = {s: 0 for s in HAND_STATUSES}
    for e in doc.get("handFound", {}).values():
        h[e["status"]] = h.get(e["status"], 0) + 1
    return f, h


def status(ws, run_id):
    doc = read_json(ws.file("diff.json", run_id)) if run_id else None
    if not doc:
        return {"pending": True}
    f, h = counts(doc)
    current = (ws.published_meta() or {}).get("run") or "none"
    return {"baseline": doc.get("baseline"), "current": doc.get("baseline") == current or current == run_id,
            "findings": f, "handFound": h}


def cli(ws, sub, argv):
    p = argparse.ArgumentParser(prog="audit.py diff", description=__doc__.split("\n\n")[0])
    p.parse_args(argv)
    run_id = ws.run_id()
    mapdata.restore_snapshot(ws.published_dir)
    if (ws.published_meta() or {}).get("run") == run_id:
        print(f"diff: run {run_id} is already published; diff.json is the one it was published with")
        return 0
    try:
        doc, _ = compute(ws, run_id)
    except DiffError as e:
        print(f"diff: {e}", file=sys.stderr)
        return 1
    write_json(ws.file("diff.json", run_id), doc)
    f, h = counts(doc)
    print(dump({"run": run_id, "baseline": doc["baseline"], "findings": f, "handFound": h}), end="")
    return 0
