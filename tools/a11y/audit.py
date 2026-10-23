#!/usr/bin/env python3
"""The single entry point for the accessibility audit (KTD2).

    python3 tools/a11y/audit.py <command> [sub] [--workspace a11y] [--data data] [--map build/map.json]
                                [--base-url https://demo.mercury.com] [--run <run id>] [stage options]

Data stages run in this process (stdlib Python). Browser stages are Node ESM modules spawned with
the same common options. Every command below is a stage a person can run alone; the a11y-audit
skill runs them in the order listed. File formats: tools/a11y/schemas.md.
"""
import argparse
import importlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE, os.path.dirname(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from lib.ws import DEFAULTS, Workspace  # noqa: E402

# (command, sub, language, module, owning unit, summary). Order follows the skill's stage order (KTD26).
STAGES = [
    ("status", None, "py", "status.py", "U1", "Report every stage's progress from files; run first after any interruption"),
    ("preflight", None, "py", "preflight.py", "U1", "Check build/map.json, node_modules and demo reachability; read the build hash"),
    ("plan", None, "py", "plan.py", "U1", "Classify every state into runs/<id>/states.json (roots, parents, mutating paths)"),
    ("recipe", "queue", "node", "recipe.mjs", "U4", "List the refresh queue: stale, previously unreachable and missing recipes"),
    ("recipe", "draft", "node", "draft.mjs", "U4", "Turn edge labels into steps and submit them through recipe check"),
    ("recipe", "check", "node", "recipe.mjs", "U4", "Replay a recipe twice, capture its fingerprint, compare with the captured PNG"),
    ("reach", None, "node", "reach.mjs", "U4", "Replay a state into a named agent-browser session under the sweep's environment"),
    ("sweep", None, "node", "sweep.mjs", "U5", "Replay and measure every state; write raw/<state>.json and outcomes.json"),
    ("calibration", "add", "py", "calibration.py", "U7", "Record the Lighthouse comparison in lighthouse.json"),
    ("records", "prepare", "py", "records.py", "U7", "Deduplicate announced-as records into records.jsonl and judge batches"),
    ("judgment", "add", "py", "records.py", "U7", "Validate and append record judgments to judgments.jsonl"),
    ("group", None, "py", "group.py", "U6", "Group results into findings with stable keys, ids and attachment proposals"),
    ("canary", "prepare", "py", "canary.py", "U7", "Build the canary batch in its own fixture workspace"),
    ("canary", "score", "py", "canary.py", "U7", "Score canary verdicts into canary.json"),
    ("verify", "prepare", "py", "verify.py", "U7", "Batch findings for verifier subagents"),
    ("verify", "status", "py", "verify.py", "U7", "Report verification progress, calibration and the second-opinion queue"),
    ("verdict", "add", "py", "verdict.py", "U7", "Validate and append verifier verdicts to verdicts.jsonl"),
    ("decision", "add", "py", "decision.py", "U7", "Validate and append attachment decisions to decisions.jsonl"),
    ("probe", None, "node", "probe.mjs", "U7", "Measure one element on a reached state: contrast, AX node, focus trace, target rect"),
    ("diff", None, "py", "diff.py", "U8", "Compute each finding's status against the current snapshot into diff.json"),
    ("publish", None, "py", "publish.py", "U8", "Check the gates and write published/ with a three-step swap"),
    ("report", None, "py", "report.py", "U10", "Render published/report.json to <dist>/a11y/index.html"),
]


def stage_help():
    rows = []
    for cmd, sub, lang, module, unit, summary in STAGES:
        name = f"{cmd} {sub}" if sub else cmd
        rows.append(f"  {name:<20} {summary} [{module}, {unit}]")
    return "commands:\n" + "\n".join(rows)


def find(cmd, sub):
    for entry in STAGES:
        if entry[0] == cmd and (entry[1] == sub or entry[1] is None):
            return entry
    return None


def subs_of(cmd):
    return [e[1] for e in STAGES if e[0] == cmd and e[1]]


def build_parser():
    p = argparse.ArgumentParser(prog="audit.py", description=__doc__.split("\n\n")[0],
                                epilog=stage_help(), formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", help="a command from the list below")
    p.add_argument("--workspace", default=DEFAULTS["workspace"], help="where stages read and write (default a11y/)")
    p.add_argument("--data", default=DEFAULTS["data"], help="area data for load_areas() (default data/)")
    p.add_argument("--map", default=DEFAULTS["map"], help="nodes, edges and journeys (default build/map.json)")
    p.add_argument("--base-url", default=DEFAULTS["base_url"], help="site the browser stages open")
    p.add_argument("--run", default=None, help="run id (default: the newest run with a states.json)")
    return p


def main(argv=None, exec_node=False):
    """Run one stage. With exec_node (the command line), a Node stage replaces this process, so a
    signal sent to the audit.py pid reaches the sweep instead of orphaning it."""
    argv = sys.argv[1:] if argv is None else argv
    args, rest = build_parser().parse_known_args(argv)
    sub = None
    if subs_of(args.command):
        if not rest or rest[0].startswith("-") or rest[0] not in subs_of(args.command):
            print(f"audit.py {args.command}: choose one of {', '.join(subs_of(args.command))}", file=sys.stderr)
            return 2
        sub, rest = rest[0], rest[1:]
    entry = find(args.command, sub)
    if entry is None:
        print(f"audit.py: unknown command {args.command!r}\n\n{stage_help()}", file=sys.stderr)
        return 2
    _, _, lang, module, unit, _ = entry
    path = os.path.join(HERE, module)
    if not os.path.exists(path):
        print(f"audit.py: {args.command} is not implemented yet ({module}, {unit})", file=sys.stderr)
        return 3
    if lang == "node":
        # A module named after its subcommand (draft.mjs for `recipe draft`) is that command.
        passes_sub = sub and module[:-4] != sub
        cmd = ["node", path] + ([sub] if passes_sub else []) + [
            "--workspace", args.workspace, "--data", args.data, "--map", args.map, "--base-url", args.base_url]
        if args.run:
            cmd += ["--run", args.run]
        if exec_node:
            os.execvp("node", cmd + rest)
        return subprocess.call(cmd + rest)
    mod = importlib.import_module(module[:-3])
    return mod.cli(Workspace.from_args(args), sub, rest) or 0


if __name__ == "__main__":
    sys.exit(main(exec_node=True))
