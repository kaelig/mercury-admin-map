"""Report every stage's progress from files (KTD26). Run it first after any interruption.

Sweep coverage, attachment proposals and the publish state are computed here. Every other stage
module reports through its own `status(ws, run_id)` (Python) or `status` subcommand (Node), so
adding a stage never means editing this file.
"""
import collections
import importlib
import json
import os
import subprocess

import mapdata
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
    dupes = sorted({s for s, n in collections.Counter(a + u).items() if n > 1} - set(both))
    if dupes:
        problems.append(f"listed twice: {', '.join(dupes)}")
    missing = sorted(states - set(a) - set(u))
    if missing:
        problems.append(f"missing: {', '.join(missing)}")
    extra = sorted((set(a) | set(u)) - states)
    if extra:
        problems.append(f"not in states.json: {', '.join(extra)}")
    return problems


def coverage_problems(state_ids, outcomes):
    """check_coverage() for outcomes.json's states: {state id: {outcome, ...}}."""
    return check_coverage(state_ids, [s for s, o in outcomes.items() if o.get("outcome") == "audited"],
                          [s for s, o in outcomes.items() if o.get("outcome") == "unreachable"])


def sweep_progress(ws, run_id):
    states = ws.states(run_id)
    if not states:
        return {"pending": True}
    current = harness_hash()
    counts = {"audited": 0, "unreachable": 0, "stale": 0, "pending": 0}
    audited, unreachable = [], []
    for sid in states:
        try:
            doc = ws.raw(sid, run_id)
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
        problems = coverage_problems(states, outcomes.get("states", {}))
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


def publish_state(ws, run_id):
    pub = ws.published_dir
    restored = mapdata.restore_snapshot(pub)    # undo a publish that stopped between its two renames (KTD21)
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


def python_status(ws, run_id, module):
    mod = importlib.import_module(module[:-3])
    fn = getattr(mod, "status", None)
    return fn(ws, run_id) if fn else {"pending": True, "note": "no status"}


def start_node_status(ws, run_id, module):
    """Start `node <module> status`; node_status() collects it."""
    cmd = ["node", os.path.join(HERE, module), "status", "--workspace", ws.path, "--data", ws.data_dir,
           "--map", ws.map_path, "--base-url", ws.base_url] + (["--run", run_id] if run_id else [])
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def node_status(proc):
    """The JSON a Node module's `status` printed last, given at most 120 s, collected as subprocess.run would."""
    try:
        with proc:
            try:
                stdout, _ = proc.communicate(timeout=120)
            except BaseException:
                proc.kill()     # leaving the `with` block then waits for it
                raise
        lines = stdout.strip().splitlines()
        return json.loads(lines[-1]) if proc.returncode == 0 and lines else {"pending": True, "note": "no status"}
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
    modules = [m for m in dict.fromkeys(e[3] for e in STAGES) if m not in OWN]
    implemented = {m for m in modules if os.path.exists(os.path.join(HERE, m))}
    # The Node modules answer in parallel, each in its own process; their results are read in table order.
    started = {m: start_node_status(ws, run_id, m) for m in modules
               if run_id and m in implemented and m.endswith(".mjs")}
    try:
        for module in modules:
            name = module.rsplit(".", 1)[0]
            if not run_id:
                stages[name] = {"pending": True}
            elif module not in implemented:
                stages[name] = {"pending": True, "note": "not implemented"}
            elif module in started:
                stages[name] = node_status(started[module])
            else:
                stages[name] = python_status(ws, run_id, module)
    finally:
        for proc in started.values():
            if proc.returncode is None:     # not collected: a Python status raised first
                with proc:
                    proc.kill()
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
