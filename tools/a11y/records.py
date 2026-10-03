"""The `records prepare` and `judgment add` stages (R12; KTD18, KTD20): announced-as records for the judge.

    python3 tools/a11y/audit.py records prepare [--run <id>]
    python3 tools/a11y/audit.py judgment add --file runs/<id>/batches/records-003.out.jsonl [--run <id>]

`records prepare` reads the audited raw files' `records`, deduplicates them by
`signature.record_sig` (element and template), and writes records.jsonl, one line per signature,
sorted. Records nobody has judged are split into judge batches of about 150,
batches/records-NNN.json, numbered after the batches already written; a batch counts as returned
once its .out.jsonl exists. A record whose signature a published finding came from skips the judge
(KTD18): prepare marks it `published` and logs its judgment as carried, a flag with the published
issue's family and criterion, so `group` folds it into a finding that goes straight to verification.

`judgment add` checks each line of a judge's output, prints the rejected lines with their reasons
as JSON, and appends the valid ones to judgments.jsonl. A flag carries `criterion`: a WCAG 2.2 A or
AA criterion with a family (name, role-state or structure), which `group` turns into a finding, or
null, which sends the record to the advisory list. File formats: schemas.md.
"""
import argparse
import math
import sys

import group
import signature
from lib.canon import append_jsonl, canonical, dump, load_checks, load_wcag, read_jsonl, write_json, write_text
from signature import RECORD_FAMILIES
from verdict import add_file, missing_fields, print_result, text
from verify import batch_files, batch_name, batch_path, in_flight, next_number, out_names

BATCH_SIZE = 150    # KTD20: about 150 records per judge
VERDICTS = ("ok", "flag")
REQUIRED = ("reason", "batch", "model", "prompt")


# ---------------------------------------------------------------------------
# records.jsonl
# ---------------------------------------------------------------------------

def collect(raws, states):
    """{sig: records.jsonl line} over every audited state, without `published`."""
    seen = {}
    for sid in sorted(raws):
        for rec in raws[sid].get("records") or []:
            sig = signature.record_sig(rec)
            line = seen.get(sig)
            if line is None:
                seen[sig] = {"sig": sig, "template": rec.get("template"), "role": rec.get("role"),
                             "states": rec.get("states") or {}, "description": rec.get("description") or "",
                             "states_seen": [sid], "occurrence": rec.get("occurrence")}
            elif line["states_seen"][-1] != sid:
                line["states_seen"].append(sid)
    for line in seen.values():
        line["sample"] = group.pick_primary(line["states_seen"], states)
    return seen


def published_sigs(ws, sigs, checks, wcag):
    """{sig: (issue id, family, criterion)} for the signatures a live published finding came from.

    A finding key is record_finding_key(family, sig); the registry maps it to an issue id. The id is
    live unless tombstoned as fixed or withdrawn (an attached id is still published, under the
    hand-found issue). The criterion is the published issue's first wcag entry, else the family's
    primary criterion."""
    registry = ws.published("registry.json") or {}
    keys, tombstones = registry.get("keys") or {}, registry.get("tombstones") or {}
    issues = ws.published("issues.json") or {}
    wcag_of = {i["id"]: i.get("wcag") or [] for i in issues.get("generated", [])}
    if not any(":announced-as:" in k for k in keys):
        return {}       # the registry holds no announced-as key: nothing to look up
    out = {}
    for sig in sigs:
        for family in RECORD_FAMILIES:
            iid = keys.get(signature.record_finding_key(family, sig))
            if not iid or (tombstones.get(iid) or {}).get("reason") in ("fixed", "withdrawn"):
                continue
            criteria = [c for c in wcag_of.get(iid, []) if c in wcag] or checks["families"][family]["criteria"][:1]
            out[sig] = (iid, family, criteria[0])
            break
    return out


def carried(sig, iid, family, criterion):
    return {"sig": sig, "verdict": "flag", "criterion": criterion, "family": family,
            "reason": f"Published as {iid}; its signature is still present, so it skips the judge and goes "
                      f"straight to verification (KTD18).",
            "batch": "published", "model": "script", "prompt": "carried"}


# ---------------------------------------------------------------------------
# judgments.jsonl
# ---------------------------------------------------------------------------

def validate(line, sigs, wcag):
    """(line to store, [reasons]) for one judgment."""
    reasons = []
    sig, verdict = line.get("sig"), line.get("verdict")
    if not text(sig):
        reasons.append("sig is required")
    elif sig not in sigs:
        reasons.append(f"sig {sig} is not in records.jsonl")
    if verdict not in VERDICTS:
        reasons.append(f"verdict must be ok or flag (got {verdict!r})")
    if verdict == "flag":
        if "criterion" not in line:
            reasons.append("a flag must carry criterion: a WCAG 2.2 A or AA criterion, or null for the advisory list")
        elif line["criterion"] is not None:
            if line["criterion"] not in wcag:
                reasons.append(f"criterion {line['criterion']!r} is not a WCAG 2.2 A or AA criterion in wcag.json")
            if line.get("family") not in RECORD_FAMILIES:
                reasons.append(f"a flag with a criterion names a family, one of {', '.join(RECORD_FAMILIES)} "
                               f"(got {line.get('family')!r})")
    reasons += missing_fields(line, REQUIRED)
    stored = dict(line)
    stored.setdefault("criterion", None)
    stored.setdefault("family", None)
    return stored, reasons


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

def chunks(items, size):
    """Split into ceil(n / size) batches of near-equal length, in order."""
    if not items:
        return []
    n = math.ceil(len(items) / size)
    step = math.ceil(len(items) / n)
    return [items[i:i + step] for i in range(0, len(items), step)]


def prepare(ws, run_id):
    checks, wcag = load_checks(), set(load_wcag())
    states = ws.states(run_id)
    raws, _ = group.audited_raws(ws, run_id, states)
    lines = collect(raws, states)
    pub = published_sigs(ws, lines, checks, wcag)
    for sig, line in lines.items():
        line["published"] = pub[sig][0] if sig in pub else None
    ordered = [lines[s] for s in sorted(lines)]
    write_text(ws.file("records.jsonl", run_id), "".join(canonical(x) + "\n" for x in ordered))

    judged = group.latest_judgments(ws, run_id)
    carry = []
    for sig in sorted(pub):
        if sig not in judged:
            line, reasons = validate(carried(sig, *pub[sig]), lines, wcag)
            if reasons:
                raise group.GroupError(f"cannot carry {sig}: {'; '.join(reasons)}")
            carry.append(line)
    if carry:
        append_jsonl(ws.file("judgments.jsonl", run_id), carry)

    run_dir = ws.run_dir(run_id)
    files = batch_files(run_dir, "records")
    out_sigs = in_flight(files, "records", "sig")
    todo = [x for x in ordered if x["sig"] not in judged and x["sig"] not in pub and x["sig"] not in out_sigs]
    todo.sort(key=lambda x: (x["sample"], x["template"] or "", x["sig"]))
    n, written = next_number(files), []
    for chunk in chunks(todo, BATCH_SIZE):
        name = batch_name("records", n)
        n += 1
        entries = [{"sig": x["sig"], "template": x["template"], "role": x["role"], "states": x["states"],
                    "description": x["description"], "sample": x["sample"], "screens": len(x["states_seen"]),
                    "occurrence": x["occurrence"]} for x in chunk]
        write_json(batch_path(run_dir, name), {"batch": name, "run": run_id, "records": entries})
        written.append(name)
    return {"run": run_id, "records": len(ordered), "published": len(pub), "carried": [x["sig"] for x in carry],
            "batches": written, "inFlight": out_names(files),
            "awaiting": sum(1 for x in ordered if x["sig"] not in judged and x["sig"] not in pub)}


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

def status(ws, run_id):
    lines = read_jsonl(ws.file("records.jsonl", run_id)) if run_id else []
    if not lines:
        return {"pending": True}
    judged = group.latest_judgments(ws, run_id)
    sigs = {x["sig"] for x in lines}
    flags = [j for s, j in judged.items() if s in sigs and j.get("verdict") == "flag"]
    files = batch_files(ws.run_dir(run_id), "records")
    return {"records": len(lines),
            "published": sum(1 for x in lines if x.get("published")),
            "judged": sum(1 for s in sigs if s in judged),
            "flagged": len(flags),
            "advisory": sum(1 for j in flags if j.get("criterion") is None),
            "awaiting": sum(1 for x in lines if x["sig"] not in judged and not x.get("published")),
            "batches": len(files),
            "inFlight": out_names(files)}


def cli(ws, sub, argv):
    if sub == "add":
        p = argparse.ArgumentParser(prog="audit.py judgment add", description="Validate and append record judgments.")
        p.add_argument("--file", required=True, help="a judge's output file, batches/records-NNN.out.jsonl")
        args = p.parse_args(argv)
        run_id = ws.run_id()
        lines = read_jsonl(ws.file("records.jsonl", run_id))
        if not lines:
            print(f"judgment add: run {run_id} has no records.jsonl: run `audit.py records prepare` first",
                  file=sys.stderr)
            return 2
        sigs = {x["sig"] for x in lines}
        wcag = set(load_wcag())
        out_path = ws.file("judgments.jsonl", run_id)
        accepted, rejected, skipped = add_file(args.file, lambda obj: validate(obj, sigs, wcag), out_path)
        return print_result(args.file, out_path, accepted, rejected, skipped)

    p = argparse.ArgumentParser(prog="audit.py records prepare", description=__doc__.split("\n\n")[0])
    p.parse_args(argv)
    run_id = ws.run_id()
    try:
        out = prepare(ws, run_id)
    except group.GroupError as e:
        print(f"records prepare: {e}", file=sys.stderr)
        return 1
    print(dump(out), end="")
    return 0
