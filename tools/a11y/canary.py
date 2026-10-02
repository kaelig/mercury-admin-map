"""The canary (KTD20 "Calibration first"): one verify batch with known answers, run before any wave.

    python3 tools/a11y/audit.py canary prepare [--run <id>] [--port N] [--out DIR]
    python3 tools/a11y/audit.py canary score --file <canary workspace>/runs/<id>/batches/canary-001.out.jsonl [--run <id>]

The canary is defined in tools/a11y/fixtures/canary/: five fixture pages with seeded true failures
and traps shaped like AE3 (an unnamed icon button whose visible label sits inside the same control:
re-rate to low) and AE7 (clickable-div tabs keyboard users cannot activate, hand-found as medium:
re-rate to high), their raw files, a committed findings.json, the hand-found issue in data/, and
expected.json. So no sweep runs for it.

`prepare` builds the canary's own workspace, under the system temp dir unless --out names one, on
the fixture server's origin (http://127.0.0.1:<port>, a free port unless --port names one), writes
batches/canary-001.json in the same format as a verify batch, and prints the server command to
start and the workspace path. `score` checks the verifier's lines with `verdict add`'s validator,
compares each finding's verdict with expected.json, and writes the run's canary.json. Canary
verdicts never enter the run's verdicts.jsonl. File formats: schemas.md ("canary.json").
"""
import argparse
import os
import shutil
import socket
import sys
import tempfile

import plan
import verdict
import verify
from lib.canon import dump, read_json, write_json
from lib.ws import Workspace

HERE = os.path.dirname(os.path.abspath(__file__))
DEFINITION = os.path.join(HERE, "fixtures", "canary")
BATCH = "canary-001"


class CanaryError(Exception):
    pass


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def prepare(ws, run_id, port=None, out_dir=None):
    port = port or free_port()
    origin = f"http://127.0.0.1:{port}"
    root = out_dir or tempfile.mkdtemp(prefix=f"a11y-canary-{run_id}-")
    cws = Workspace(workspace=os.path.join(root, "ws"), data=os.path.join(DEFINITION, "data"),
                    map=os.path.join(DEFINITION, "map.json"), base_url=origin, run=run_id)
    policy = dict(ws.policy() or {}, origin=origin, allowedOrigins=[origin])
    write_json(os.path.join(cws.path, "policy.json"), policy)
    write_json(cws.file("states.json"), plan.build_doc(read_json(cws.map_path), policy, run_id))
    states = read_json(cws.file("states.json"))["states"]
    # Every canary state is a page: an accepted root recipe lets reach and probe open it.
    write_json(os.path.join(cws.recipes_dir, "canary.json"), {
        sid: {"parent": s["rootUrl"], "hop": [], "fingerprint": None, "mutates": False, "source": s["source"],
              "status": "accepted", "similarity": None, "recordedBy": "root", "review": None}
        for sid, s in states.items() if s["root"]})
    findings = dict(read_json(os.path.join(DEFINITION, "findings.json")), run=run_id)
    write_json(cws.file("findings.json"), findings)
    os.makedirs(cws.raw_dir(), exist_ok=True)
    for f in sorted(os.listdir(os.path.join(DEFINITION, "raw"))):
        shutil.copyfile(os.path.join(DEFINITION, "raw", f), os.path.join(cws.raw_dir(), f))

    ctx = verify.context(cws, run_id, findings)
    entries = [verify.entry(f, ctx, 1) for f in sorted(findings["findings"], key=lambda f: f["key"])]
    batch_path = os.path.join(cws.run_dir(), "batches", f"{BATCH}.json")
    write_json(batch_path, verify.batch_doc(BATCH, run_id, "canary", verify.reach_of(cws, run_id), entries))
    return {"root": root, "workspace": cws.path, "run": run_id, "batch": batch_path, "findings": len(entries),
            "baseUrl": origin, "server": f"node tools/a11y/fixtures/server.mjs --port {port} canary",
            "score": f"python3 tools/a11y/audit.py canary score --workspace {ws.path} --run {run_id} "
                     f"--file {batch_path[:-5]}.out.jsonl"}


def got_word(v):
    """A verdict as expected.json spells it: confirm, drop, rerate:<severity>, not-reached, unverified."""
    if v is None:
        return None
    return f"rerate:{v['severityAfter']}" if v["outcome"] == "rerate" else v["outcome"]


def score(ws, run_id, path):
    run_dir = os.path.dirname(os.path.dirname(os.path.abspath(path)))
    doc = read_json(os.path.join(run_dir, "findings.json"))
    if not doc:
        raise CanaryError(f"{path} is not in a canary workspace: no findings.json in {run_dir}")
    findings = {f["key"]: f for f in doc.get("findings", [])}
    expected = read_json(os.path.join(DEFINITION, "expected.json"))
    if sorted(expected) != sorted(findings):
        raise CanaryError("the canary workspace's findings do not match fixtures/canary/expected.json: "
                          "run `canary prepare` again")
    valid, rejected = {}, []
    for n, raw, obj in verdict.read_out(path):
        if isinstance(obj, dict) and verdict.is_decision(obj):
            continue
        if not isinstance(obj, dict):
            rejected.append({"line": n, "reasons": ["not a JSON object"], "text": raw})
            continue
        line, reasons = verdict.validate(obj, findings)
        if reasons:
            rejected.append({"line": n, "reasons": reasons, "text": raw})
        else:
            valid[line["key"]] = line
    items = []
    for key in sorted(expected):
        got = got_word(valid.get(key))
        items.append({"key": key, "expected": expected[key]["expected"], "got": got,
                      "ok": got == expected[key]["expected"]})
    prompts = sorted({v["prompt"] for v in valid.values()})
    correct = sum(1 for i in items if i["ok"])
    out = {"prompt": ",".join(prompts) or None, "batch": os.path.basename(path).split(".")[0],
           "expected": len(items), "correct": correct, "pass": correct == len(items) and not rejected,
           "items": items}
    write_json(ws.file("canary.json", run_id), out)
    return out, rejected


def status(ws, run_id):
    doc = read_json(ws.file("canary.json", run_id)) if run_id else None
    if not doc:
        return {"pending": True}
    return {"pass": doc.get("pass"), "correct": doc.get("correct"), "expected": doc.get("expected"),
            "prompt": doc.get("prompt")}


def cli(ws, sub, argv):
    p = argparse.ArgumentParser(prog=f"audit.py canary {sub}", description=__doc__.split("\n\n")[0])
    if sub == "prepare":
        p.add_argument("--port", type=int, default=None, help="the fixture server's port (default: a free one)")
        p.add_argument("--out", default=None, help="where to build the canary workspace (default: a new temp dir)")
    else:
        p.add_argument("--file", required=True, help="the verifier's output for the canary batch")
    args = p.parse_args(argv)
    run_id = ws.run_id()
    if sub == "prepare":
        print(dump(prepare(ws, run_id, port=args.port, out_dir=args.out)), end="")
        return 0
    try:
        doc, rejected = score(ws, run_id, args.file)
    except CanaryError as e:
        print(f"canary score: {e}", file=sys.stderr)
        return 2
    print(dump({"file": verdict.rel(ws.file("canary.json", run_id)), "pass": doc["pass"], "correct": doc["correct"],
                "expected": doc["expected"], "wrong": [i["key"] for i in doc["items"] if not i["ok"]],
                "rejected": rejected}), end="")
    return 0 if doc["pass"] else 1
