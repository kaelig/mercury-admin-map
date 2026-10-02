"""The `calibration add` stage (R3, KTD25): Lighthouse checks the harness, not Mercury.

    python3 tools/a11y/audit.py calibration add --file comparison.json [--run <id>]

The conductor runs Lighthouse in snapshot mode on three roots and three one-hop overlays at
1440×900 and writes a comparison file:

    {"date": "YYYY-MM-DD", "chrome": "<the MCP's Chrome version>", "lighthouseAxe": "4.13.0",
     "states": [{"state": "<state id>", "kind": "root" | "overlay", "lighthouseFailed": ["<axe rule id>", ...]}],
     "explanations": {"<axe rule id>": "rule-selection" | "version"}}

For each state this stage reads the sweep's axe violations and incomplete results from the raw file
and records every rule only one side reports. Lighthouse audits the whole document while the sweep
scopes an overlay to its top layer (KTD11), so a rule Lighthouse fails on an overlay state that the
sweep found only on its parent page is explained by scope. A rule the conductor attributes to
Lighthouse's rule selection or a different axe or Chrome version is explained. Anything else is an
unexplained difference: a harness bug to fix before grouping. Writes lighthouse.json; exits 1 while
any difference is unexplained. `due(ws, run_id)` says whether this run needs a calibration.
File format: schemas.md ("lighthouse.json").
"""
import argparse
import os
import sys

from lib.canon import dump, read_json, write_json
from verdict import DATE, rel, text, today

KINDS = ("root", "overlay")
EXPLANATIONS = ("rule-selection", "version")   # "scope" is computed here, never claimed
BASES = ("violation", "incomplete")


def sweep_rules(raw):
    return sorted({r["rule"] for r in (raw or {}).get("results") or []
                   if r.get("check") == "axe" and r.get("basis") in BASES})


def read_raw(ws, run_id, sid):
    return read_json(os.path.join(ws.raw_dir(run_id), f"{sid}.json"))


def compare(lighthouse, own, parent, overlay, explanations):
    out = []
    for rule in sorted(set(lighthouse) ^ set(own)):
        side = "lighthouse" if rule in lighthouse else "sweep"
        if overlay and side == "lighthouse" and rule in parent:
            why = "scope"
        else:
            why = explanations.get(rule)
        out.append({"rule": rule, "side": side, "explained": why is not None, "explanation": why})
    return out


def build(ws, run_id, doc):
    """(lighthouse.json document, [reasons]). The document is None when the comparison is invalid."""
    reasons = []
    if not isinstance(doc, dict):
        return None, ["the comparison must be a JSON object"]
    for field in ("chrome", "lighthouseAxe"):
        if not text(doc.get(field)):
            reasons.append(f"{field} is required")
    date = doc.get("date")
    if date is not None and not (isinstance(date, str) and DATE.match(date)):
        reasons.append("date must be YYYY-MM-DD")
    explanations = doc.get("explanations") or {}
    if not isinstance(explanations, dict):
        reasons.append("explanations must map axe rule ids to rule-selection or version")
        explanations = {}
    for rule, why in sorted(explanations.items()):
        if why not in EXPLANATIONS:
            reasons.append(f"explanation {why!r} for {rule} must be rule-selection or version; scope is computed")
    entries = doc.get("states")
    if not isinstance(entries, list) or not entries:
        return None, reasons + ["states must list the states Lighthouse audited"]
    states = ws.states(run_id)
    out, axe = [], None
    for e in entries:
        e = e if isinstance(e, dict) else {}
        sid, kind, failed = e.get("state"), e.get("kind"), e.get("lighthouseFailed")
        if kind not in KINDS:
            reasons.append(f"{sid}: kind must be root or overlay (got {kind!r})")
            continue
        if not isinstance(failed, list) or not all(isinstance(r, str) for r in failed):
            reasons.append(f"{sid}: lighthouseFailed must list axe rule ids")
            continue
        raw = read_raw(ws, run_id, sid) if sid in states else None
        if not raw or raw.get("status") != "audited":
            reasons.append(f"{sid}: not an audited state of run {run_id}")
            continue
        axe = axe or (raw.get("env") or {}).get("axe")
        parent_rules = []
        if kind == "overlay":
            parent = (states.get(sid) or {}).get("parent")
            praw = read_raw(ws, run_id, parent) if parent else None
            if not praw or praw.get("status") != "audited":
                reasons.append(f"{sid}: an overlay needs its parent page audited (parent {parent!r})")
                continue
            parent_rules = sweep_rules(praw)
        own = sweep_rules(raw)
        lighthouse = sorted(set(failed))
        out.append({"state": sid, "kind": kind, "lighthouseFailed": lighthouse, "sweepRules": own,
                    "differences": compare(lighthouse, own, parent_rules, kind == "overlay", explanations)})
    if reasons:
        return None, reasons
    versions = (read_json(ws.file("run.json", run_id)) or {}).get("versions") or {}
    return {"date": date or today(), "chrome": doc["chrome"], "lighthouseAxe": doc["lighthouseAxe"],
            "sweepAxe": axe or versions.get("axe"), "states": out,
            "unexplained": sum(1 for s in out for d in s["differences"] if not d["explained"])}, []


# ---------------------------------------------------------------------------
# When it is due
# ---------------------------------------------------------------------------

def current_versions(ws, run_id):
    """(axe, chromium) of this run: run.json, else the first raw file's env."""
    v = (read_json(ws.file("run.json", run_id)) or {}).get("versions") or {}
    axe, chromium = v.get("axe"), v.get("chromium")
    raw_dir = ws.raw_dir(run_id)
    if (not axe or not chromium) and os.path.isdir(raw_dir):
        for f in sorted(os.listdir(raw_dir)):
            env = (read_json(os.path.join(raw_dir, f)) or {}).get("env") or {}
            if env.get("axe") or env.get("chromium"):
                axe, chromium = axe or env.get("axe"), chromium or env.get("chromium")
                break
    return axe, chromium


def due(ws, run_id):
    """True on the first run, and when the axe-core or Chromium version differs from the last calibration."""
    meta = ws.published_meta() or {}
    last = meta.get("lighthouse")
    if not last:
        return True
    then = (read_json(os.path.join(ws.runs_dir, str(last.get("run")), "run.json")) or {}).get("versions") \
        or meta.get("versions") or {}
    axe, chromium = current_versions(ws, run_id)
    return axe != then.get("axe") or chromium != then.get("chromium")


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

def status(ws, run_id):
    if not run_id:
        return {"pending": True}
    doc = read_json(ws.file("lighthouse.json", run_id))
    if not doc:
        return {"pending": True, "due": due(ws, run_id)}
    return {"due": due(ws, run_id), "done": True, "states": len(doc.get("states", [])),
            "unexplained": doc.get("unexplained", 0)}


def cli(ws, sub, argv):
    p = argparse.ArgumentParser(prog="audit.py calibration add", description=__doc__.split("\n\n")[0])
    p.add_argument("--file", required=True, help="the conductor's Lighthouse comparison (JSON)")
    args = p.parse_args(argv)
    run_id = ws.run_id()
    try:
        comparison = read_json(args.file)
    except ValueError as e:
        comparison = f"not JSON: {e}"
    doc, reasons = build(ws, run_id, comparison)
    if doc is None:
        print(dump({"file": rel(args.file), "rejected": reasons}), end="")
        return 2
    path = ws.file("lighthouse.json", run_id)
    write_json(path, doc)
    print(dump({"file": rel(path), "states": len(doc["states"]), "unexplained": doc["unexplained"],
                "differences": [dict(d, state=s["state"]) for s in doc["states"] for d in s["differences"]]}), end="")
    if doc["unexplained"]:
        print(f"calibration add: {doc['unexplained']} unexplained difference(s): a harness bug; fix it before "
              f"grouping", file=sys.stderr)
        return 1
    return 0
