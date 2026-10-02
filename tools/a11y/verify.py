"""The `verify` stage (R16, R17, R26; KTD19, KTD20): batches for verifier subagents, and progress.

    python3 tools/a11y/audit.py verify prepare [--run <id>]
    python3 tools/a11y/audit.py verify status [--run <id>]

`prepare` puts every finding that has no verdict yet, and is not already out in a batch, into
batches/verify-NNN.json: at most 10 findings that share an area and a primary state, numbered after
the batches already written, so the same inputs give the same files. A batch is returned once its
batches/<name>.out.jsonl exists (an agent that failed gets an empty one). Each returned batch that
brought no valid verdict for a finding is one failed attempt; after two, the finding gets an
`unverified` verdict through `verdict add`'s validator. Run `verdict add` and `decision add` on every
returned batch before preparing again.

`status` reports findings verdicted, pending and unverified, proposals decided and pending, the
canary result (the waves hold until canary.json passes), and the second-opinion queue: every
verdict that flips the previous published status on the same evidence hash, and every drop of a
finding whose default severity is high, until a `second: true` verdict exists for it.
File formats: schemas.md ("Batch files", "verdicts.jsonl").
"""
import argparse
import collections
import os
import re
import sys

import verdict
from lib.canon import append_jsonl, dump, read_json, read_jsonl, sha, write_json

HERE = os.path.dirname(os.path.abspath(__file__))
BATCH_MAX = 10      # KTD20: about 10 findings per verifier subagent
ATTEMPTS = 2        # KTD20: then the finding is unverified
FAIL, PASS = "fail", "pass"
BATCH_FILE = re.compile(r"^(?P<prefix>[a-z]+)-(?P<n>\d{3,})\.json$")


class VerifyError(Exception):
    pass


# ---------------------------------------------------------------------------
# Batch files, shared with records.py and canary.py
# ---------------------------------------------------------------------------

def batch_name(prefix, n):
    return f"{prefix}-{n:03d}"


def batch_files(run_dir, prefix):
    """[(name, path, doc, returned)] for batches/<prefix>-NNN.json in number order. A batch is
    returned once its <name>.out.jsonl exists."""
    d = os.path.join(run_dir, "batches")
    if not os.path.isdir(d):
        return []
    found = []
    for f in os.listdir(d):
        m = BATCH_FILE.match(f)
        if m and m.group("prefix") == prefix:
            path = os.path.join(d, f)
            found.append((int(m.group("n")), f[:-5], path))
    return [(name, path, read_json(path), os.path.exists(path[:-5] + ".out.jsonl")) for _, name, path in sorted(found)]


def next_number(files):
    return max((int(name.rsplit("-", 1)[1]) for name, _, _, _ in files), default=0) + 1


def reach_of(ws, run_id):
    """How a verifier reaches the batch's states: the options `audit.py reach` takes."""
    return {"workspace": verdict.rel(ws.path), "data": verdict.rel(ws.data_dir), "map": verdict.rel(ws.map_path),
            "baseUrl": ws.base_url, "run": run_id}


# ---------------------------------------------------------------------------
# Batch entries
# ---------------------------------------------------------------------------

def area_of(f, states):
    p = f.get("primary")
    if p:
        return (states.get(p) or {}).get("area") or p.split(".")[0]
    return (f.get("issueId") or f["key"].split(":", 1)[-1]).split(".")[0]


def sampled_evidence(raw_dir, f, raws):
    """The raw result (or record) behind the finding on each sampled state, where the raw file has it."""
    out = []
    for s in f.get("sample") or []:
        occ = next((o for o in f.get("occurrences") or [] if o["state"] == s), None)
        if occ is None:
            continue
        if s not in raws:
            raws[s] = read_json(os.path.join(raw_dir, f"{s}.json"))
        raw = raws[s] or {}
        items = raw.get("records" if f.get("rule") == "announced-as" else "results") or []
        if occ["index"] >= len(items):
            continue
        item = items[occ["index"]]
        if f.get("rule") == "announced-as":
            ev = {k: v for k, v in item.items() if k != "occurrence"}
        else:
            ev = item.get("evidence") or {}
        out.append({"state": s, "occurrence": item.get("occurrence"), "evidence": ev})
    return out


def concerns(f, item):
    """True when a proposal or attachment is about this finding, from either side."""
    return item["finding"] == f["key"] or (f["source"] == "hand-found" and item["handFound"] == f.get("issueId"))


def entry(f, ctx, attempt):
    fam = ctx["checks"]["families"].get(f["family"]) or {}
    return {
        "key": f["key"], "issueId": f.get("issueId"), "source": f["source"], "family": f["family"],
        "rule": f.get("rule"), "criteria": f.get("criteria"), "basis": f.get("basis"),
        "severityDefault": f["severityDefault"], "measured": f["family"] in verdict.MEASURED,
        "title": f.get("title"), "summary": f.get("summary"), "facts": f.get("facts") or {},
        "guidance": fam.get("guidance"),
        "states": f.get("sample") or [], "primary": f.get("primary"), "screens": len(f.get("states") or []),
        "evidenceHash": f.get("evidenceHash"),
        "evidence": sampled_evidence(ctx["raw_dir"], f, ctx["raws"]),
        "proposals": [dict(p) for p in ctx["proposals"] if concerns(f, p)],
        "attachments": [{"id": sha([a["finding"], a["handFound"]]), "finding": a["finding"], "handFound": a["handFound"]}
                        for a in ctx["attachments"] if concerns(f, a)],
        "attempt": attempt,
    }


def batch_doc(name, run_id, area, reach, entries):
    return {"batch": name, "run": run_id, "area": area,
            "primaries": sorted({e["primary"] for e in entries if e["primary"]}),
            "reach": reach, "findings": entries}


def context(ws, run_id, doc):
    registry = read_json(os.path.join(ws.published_dir, "registry.json")) or {}
    return {"checks": read_json(os.path.join(HERE, "checks.json")), "raw_dir": ws.raw_dir(run_id), "raws": {},
            "proposals": doc.get("proposals", []), "attachments": registry.get("attachments", [])}


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

def build_of(ws, run_id):
    builds = (read_json(ws.file("run.json", run_id)) or {}).get("builds") or []
    return builds[-1] if builds else "unknown"


def unverified_line(f, tried, build):
    return {"key": f["key"], "issueId": f.get("issueId"), "source": f["source"], "outcome": "unverified",
            "severityBefore": f["severityDefault"], "severityAfter": f["severityDefault"],
            "reason": f"No valid verdict after {len(tried)} attempts: {', '.join(tried)}.",
            "reasonCode": None, "counterEvidence": None, "probes": [], "states": f.get("sample") or [],
            "build": build, "model": "script", "batch": tried[-1], "prompt": "verify-prepare",
            "attempt": len(tried), "second": False}


def prepare(ws, run_id):
    doc = read_json(ws.file("findings.json", run_id))
    if not doc:
        raise VerifyError(f"run {run_id} has no findings.json: run `audit.py group` first")
    findings = doc.get("findings", [])
    by_key = {f["key"]: f for f in findings}
    run_dir = ws.run_dir(run_id)
    has_verdict = {v["key"] for v in read_jsonl(ws.file("verdicts.jsonl", run_id))}
    files = batch_files(run_dir, "verify")
    out_now = [name for name, _, _, returned in files if not returned]
    in_flight = {e["key"] for _, _, b, returned in files if not returned for e in (b or {}).get("findings", [])}
    tried = collections.defaultdict(list)
    for name, _, b, returned in files:
        if returned:
            for e in (b or {}).get("findings", []):
                tried[e["key"]].append(name)
    pending = [f for f in findings if f["key"] not in has_verdict and f["key"] not in in_flight]

    give_up, lines = [f for f in pending if len(tried[f["key"]]) >= ATTEMPTS], []
    build = build_of(ws, run_id)
    for f in give_up:
        line, reasons = verdict.validate(unverified_line(f, tried[f["key"]], build), by_key, allow_unverified=True)
        if reasons:
            raise VerifyError(f"cannot mark {f['key']} unverified: {'; '.join(reasons)}")
        lines.append(line)
    if lines:
        append_jsonl(ws.file("verdicts.jsonl", run_id), lines)

    states = ws.states(run_id)
    groups = collections.defaultdict(list)
    for f in pending:
        if len(tried[f["key"]]) < ATTEMPTS:
            groups[(area_of(f, states), f.get("primary") or "")].append(f)
    ctx, reach, n, written = context(ws, run_id, doc), reach_of(ws, run_id), next_number(files), []
    for area, primary in sorted(groups):
        fs = sorted(groups[(area, primary)], key=lambda f: f["key"])
        for i in range(0, len(fs), BATCH_MAX):
            name = batch_name("verify", n)
            n += 1
            entries = [entry(f, ctx, len(tried[f["key"]]) + 1) for f in fs[i:i + BATCH_MAX]]
            b = batch_doc(name, run_id, area, reach, entries)
            write_json(os.path.join(run_dir, "batches", f"{name}.json"), b)
            written.append({"batch": name, "area": area, "primaries": b["primaries"], "findings": len(entries)})
    canary = canary_state(ws, run_id)
    return {"run": run_id, "batches": written, "inFlight": out_now, "unverified": [f["key"] for f in give_up],
            "canary": canary["status"], "holdWaves": canary["status"] != "passed"}


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def canary_state(ws, run_id):
    doc = read_json(ws.file("canary.json", run_id))
    if not doc:
        return {"status": "missing",
                "message": "No canary result: run `canary prepare`, verify its batch, then `canary score`, "
                           "before any wave."}
    wrong = [i["key"] for i in doc.get("items", []) if not i.get("ok")]
    out = {"prompt": doc.get("prompt"), "correct": doc.get("correct"), "expected": doc.get("expected")}
    if doc.get("pass") and not wrong:
        return dict(out, status="passed")
    return dict(out, status="failed", wrong=wrong,
                message=f"calibration failure: {len(wrong)} of {doc.get('expected')} canary verdicts wrong "
                        f"({', '.join(wrong)}). Fix the verifier prompt and rerun the canary before any wave.")


def prior_status(f, registry, overlays):
    """The finding's status in the published snapshot: fail (published), pass (withdrawn or dropped), or None."""
    if f["source"] == "hand-found":
        v = ((overlays.get(f.get("issueId")) or {}).get("audit") or {}).get("verdict")
        return FAIL if v in ("confirm", "rerate") else PASS if v == "drop" else None
    iid = (registry.get("keys") or {}).get(f["key"])
    if not iid:
        return None
    t = (registry.get("tombstones") or {}).get(iid)
    if t is None or t.get("reason") == "attached":
        return FAIL
    return PASS if t.get("reason") == "withdrawn" else None


def now_status(v):
    return FAIL if v["outcome"] in ("confirm", "rerate") else PASS if v["outcome"] == "drop" else None


def second_opinion_queue(ws, findings, verdicts):
    last, seconds = {}, set()
    for v in verdicts:
        last[v["key"]] = v
        if v.get("second"):
            seconds.add(v["key"])
    baseline = (ws.published_meta() or {}).get("run")
    prev = read_json(os.path.join(ws.runs_dir, baseline, "findings.json")) if baseline else None
    prev_hash = {f["key"]: f.get("evidenceHash") for f in (prev or {}).get("findings", [])}
    registry = read_json(os.path.join(ws.published_dir, "registry.json")) or {}
    overlays = (read_json(os.path.join(ws.published_dir, "issues.json")) or {}).get("overlays") or {}
    queue = []
    for f in findings:
        v = last.get(f["key"])
        if v is None or f["key"] in seconds:
            continue
        why = []
        before, after = prior_status(f, registry, overlays), now_status(v)
        if before and after and before != after and prev_hash.get(f["key"]) == f.get("evidenceHash"):
            why.append("flip")
        if v["outcome"] == "drop" and f["severityDefault"] == "high":
            why.append("high-drop")
        if why:
            queue.append({"key": f["key"], "why": why, "outcome": v["outcome"], "previous": before,
                          "severityDefault": f["severityDefault"], "batch": v.get("batch")})
    return queue


def status(ws, run_id):
    doc = read_json(ws.file("findings.json", run_id)) if run_id else None
    if not doc:
        return {"pending": True}
    findings = doc.get("findings", [])
    verdicts = read_jsonl(ws.file("verdicts.jsonl", run_id))
    last = {v["key"]: v for v in verdicts}
    files = batch_files(ws.run_dir(run_id), "verify")
    in_flight = {e["key"] for _, _, b, returned in files if not returned for e in (b or {}).get("findings", [])}
    counts = {"total": len(findings), "verdicted": 0, "notReached": 0, "unverified": 0, "pending": 0, "inFlight": 0}
    outcomes = collections.Counter()
    for f in findings:
        v = last.get(f["key"])
        if v is None:
            counts["pending"] += 1
            counts["inFlight"] += f["key"] in in_flight
            continue
        outcomes[v["outcome"]] += 1
        slot = {"not-reached": "notReached", "unverified": "unverified"}.get(v["outcome"], "verdicted")
        counts[slot] += 1
    decided = {d.get("proposal") for d in read_jsonl(ws.file("decisions.jsonl", run_id))}
    proposals = [p["id"] for p in doc.get("proposals", [])]
    canary = canary_state(ws, run_id)
    return {"findings": counts,
            "outcomes": dict(sorted(outcomes.items())),
            "proposals": {"total": len(proposals), "decided": sum(1 for p in proposals if p in decided),
                          "pending": [p for p in proposals if p not in decided]},
            "batches": {"written": len(files), "inFlight": [name for name, _, _, returned in files if not returned]},
            "canary": canary,
            "holdWaves": canary["status"] != "passed",
            "secondOpinion": second_opinion_queue(ws, findings, verdicts)}


def cli(ws, sub, argv):
    p = argparse.ArgumentParser(prog=f"audit.py verify {sub}", description=__doc__.split("\n\n")[0])
    p.parse_args(argv)
    run_id = ws.run_id()
    if sub == "status":
        print(dump(status(ws, run_id)), end="")
        return 0
    try:
        out = prepare(ws, run_id)
    except VerifyError as e:
        print(f"verify prepare: {e}", file=sys.stderr)
        return 1
    print(dump(out), end="")
    return 0
