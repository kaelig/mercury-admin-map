"""Report every stage's progress from files (KTD26). Run it first after any interruption.

Sweep coverage, attachment proposals and the publish state are computed here. Every other stage
module reports through its own `status(ws, run_id)` (Python) or `status` subcommand (Node), so
adding a stage never means editing this file.
"""
import importlib
import json
import os
import shutil
import subprocess

from lib.canon import dump, harness_hash, read_json, read_jsonl

HERE = os.path.dirname(os.path.abspath(__file__))
OWN = {"status.py", "preflight.py", "sweep.mjs"}


def check_coverage(state_ids, audited, unreachable):
    """The coverage invariant (KTD10): audited and unreachable are exactly the states, disjoint."""
    states, a, u = set(state_ids), list(audited), list(unreachable)
    problems = []
    both = sorted(set(a) & set(u))
    if both:
        problems.append(f"audited and unreachable: {', '.join(both)}")
    dupes = sorted({s for s in a + u if (a + u).count(s) > 1} - set(both))
    if dupes:
        problems.append(f"listed twice: {', '.join(dupes)}")
    missing = sorted(states - set(a) - set(u))
    if missing:
        problems.append(f"missing: {', '.join(missing)}")
    extra = sorted((set(a) | set(u)) - states)
    if extra:
        problems.append(f"not in states.json: {', '.join(extra)}")
    return problems


def sweep_progress(ws, run_id):
    states = ws.states(run_id)
    if not states:
        return {"pending": True}
    current = harness_hash()
    counts = {"audited": 0, "unreachable": 0, "stale": 0, "pending": 0}
    audited, unreachable = [], []
    for sid in states:
        path = os.path.join(ws.raw_dir(run_id), f"{sid}.json")
        try:
            doc = read_json(path)
        except (ValueError, OSError):
            doc = None
        if not doc or doc.get("state") != sid:
            counts["pending"] += 1
        elif doc.get("harness") != current:
            counts["stale"] += 1
        elif doc.get("status") == "audited":
            counts["audited"] += 1
            audited.append(sid)
        elif doc.get("status") == "unreachable":
            counts["unreachable"] += 1
            unreachable.append(sid)
        else:
            counts["pending"] += 1
    out = {**counts, "states": len(states), "harness": current}
    outcomes = read_json(ws.file("outcomes.json", run_id))
    if outcomes:
        recorded = outcomes.get("states", {})
        problems = check_coverage(states, [s for s, o in recorded.items() if o.get("outcome") == "audited"],
                                  [s for s, o in recorded.items() if o.get("outcome") == "unreachable"])
        out["outcomes"] = {"written": True, "coverage": problems or "ok"}
    else:
        out["outcomes"] = {"written": False}
    if counts["pending"] == len(states):
        out["pending"] = True
    return out


def pending_proposals(ws, run_id):
    findings = read_json(ws.file("findings.json", run_id))
    if not findings:
        return []
    decided = {d.get("proposal") for d in read_jsonl(ws.file("decisions.jsonl", run_id))}
    return [p["id"] for p in findings.get("proposals", []) if p["id"] not in decided]


def restore_published(pub):
    """Undo a publish that stopped between its two renames (KTD21). Returns True when it restored."""
    prev = pub + ".prev"
    if not os.path.exists(pub) and os.path.isdir(prev):
        os.rename(prev, pub)
        return True
    return False


def publish_state(ws, run_id):
    pub = ws.published_dir
    restored = restore_published(pub)
    if os.path.exists(pub + ".tmp") or os.path.exists(pub + ".prev"):
        state = "publish incomplete, rerun publish"
    else:
        meta = read_json(os.path.join(pub, "meta.json"))
        if not meta:
            state = "not published"
        elif run_id and meta.get("run") == run_id:
            state = "already published"
        else:
            state = f"snapshot holds run {meta.get('run')}"
    return {"state": state, "restored": restored}


def module_status(ws, run_id, module):
    path = os.path.join(HERE, module)
    if not os.path.exists(path):
        return {"pending": True, "note": "not implemented"}
    if module.endswith(".py"):
        mod = importlib.import_module(module[:-3])
        fn = getattr(mod, "status", None)
        return fn(ws, run_id) if fn else {"pending": True, "note": "no status"}
    cmd = ["node", path, "status", "--workspace", ws.path, "--data", ws.data_dir, "--map", ws.map_path,
           "--base-url", ws.base_url] + (["--run", run_id] if run_id else [])
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        lines = out.stdout.strip().splitlines()
        return json.loads(lines[-1]) if out.returncode == 0 and lines else {"pending": True, "note": "no status"}
    except (subprocess.TimeoutExpired, ValueError):
        return {"pending": True, "note": "status failed"}


def report(ws):
    from audit import STAGES
    run_id = ws.run_id(required=False)
    stages = {}
    if run_id and os.path.exists(ws.file("states.json", run_id)):
        stages["sweep"] = sweep_progress(ws, run_id)
    else:
        stages["sweep"] = {"pending": True}
    for module in dict.fromkeys(e[3] for e in STAGES):
        if module in OWN:
            continue
        name = module.rsplit(".", 1)[0]
        stages[name] = module_status(ws, run_id, module) if run_id else {"pending": True}
    return {
        "run": run_id,
        "workspace": ws.path,
        "stages": stages,
        "proposals": {"pending": pending_proposals(ws, run_id) if run_id else []},
        "publish": publish_state(ws, run_id),
    }


def cli(ws, sub, argv):
    print(dump(report(ws)), end="")
    return 0
