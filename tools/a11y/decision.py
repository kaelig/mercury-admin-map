"""The `decision add` primitive (R17; KTD17, KTD19): the only writer of runs/<id>/decisions.jsonl.

    python3 tools/a11y/audit.py decision add --file runs/<id>/batches/verify-004.out.jsonl [--run <id>]

Each line answers one attachment proposal: `attach` or `reject` a proposal from findings.json, or
`release` an attachment the snapshot registry holds (its proposal id is sha([finding key,
hand-found id]), from signature.proposal_id like `group`'s proposals). Every decision needs a
reason. Prints the rejected lines with their reasons as JSON and appends the valid ones; verdict
lines in the same file are skipped, since `verdict add` takes them. File formats: schemas.md.
"""
import argparse
import sys

from lib.canon import read_json, read_jsonl
from signature import proposal_id
from verdict import add_file, count_by, date_reasons, missing_fields, print_result, text, today

DECISIONS = ("attach", "reject", "release")
REQUIRED = ("model", "batch", "prompt")


def answerable(ws, run_id):
    """({proposal id: proposal} from findings.json, {id: attachment} from the snapshot registry), or None."""
    doc = read_json(ws.file("findings.json", run_id))
    if not doc:
        return None
    proposals = {p["id"]: p for p in doc.get("proposals", [])}
    registry = ws.published("registry.json") or {}
    attachments = {proposal_id(a["finding"], a["handFound"]): a for a in registry.get("attachments", [])}
    return proposals, attachments


def validate(line, proposals, attachments):
    """(line to store, [reasons])."""
    reasons = []
    pid, kind = line.get("proposal"), line.get("decision")
    if kind not in DECISIONS:
        reasons.append(f"decision must be one of {', '.join(DECISIONS)} (got {kind!r})")
    if not text(line.get("reason")):
        reasons.append("a decision needs a reason")
    reasons += missing_fields(line, REQUIRED)
    reasons += date_reasons(line.get("date"))
    target = None
    if not text(pid):
        reasons.append("proposal is required: the id of the proposal this decision answers")
    elif kind == "release":
        target = attachments.get(pid)
        if target is None:
            reasons.append(f"proposal {pid} names no attachment in the snapshot registry: a release answers "
                           f"sha([finding, handFound]) of a registry attachment")
    elif kind in DECISIONS:
        target = proposals.get(pid)
        if target is None:
            reasons.append(f"{pid} is not a proposal in findings.json")
    stored = dict(line)
    if target:
        for field in ("finding", "handFound"):
            if stored.get(field) is None:
                stored[field] = target[field]
            elif stored[field] != target[field]:
                reasons.append(f"{field} {stored[field]} does not match the proposal's {target[field]}")
    if stored.get("date") is None:
        stored["date"] = today()
    return stored, reasons


def is_verdict(obj):
    return "outcome" in obj and "decision" not in obj


def status(ws, run_id):
    lines = read_jsonl(ws.file("decisions.jsonl", run_id)) if run_id else []
    if not lines:
        return {"pending": True}
    return {"lines": len(lines), "proposals": len({d.get("proposal") for d in lines}),
            "decisions": count_by(lines, "decision")}


def cli(ws, sub, argv):
    p = argparse.ArgumentParser(prog="audit.py decision add", description=__doc__.split("\n\n")[0])
    p.add_argument("--file", required=True, help="a verifier output file, batches/<batch>.out.jsonl")
    args = p.parse_args(argv)
    run_id = ws.run_id()
    known = answerable(ws, run_id)
    if known is None:
        print(f"decision add: run {run_id} has no findings.json: run `audit.py group` first", file=sys.stderr)
        return 2
    out_path = ws.file("decisions.jsonl", run_id)
    accepted, rejected, skipped = add_file(args.file, lambda obj: validate(obj, *known), out_path, skip=is_verdict)
    return print_result(args.file, out_path, accepted, rejected, skipped)
