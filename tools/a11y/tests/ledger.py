"""Test helper for `diff` and `publish`: a synthetic run ledger on real data/ node ids, written into a temp workspace.

`Ledger` writes what `diff` and `publish` read: run.json, states.json, outcomes.json, minimal raw
files (families and axe rules that ran), findings.json, verdicts.jsonl, decisions.jsonl, records.jsonl,
canary.json and lighthouse.json. run.json's harness and findings.json's `inputs` are the current
harness hash, outcomes and raw files, computed as it writes, as a sweep and `group` leave them. Findings get
their title and evidence summary from the checks.json templates, filled with plain facts, the way
`group` fills them. Every line is valid against schemas.md, so the tests exercise the stages, not
the fixtures.

`build_test_snapshot(dest)` publishes two runs with this ledger and copies the second snapshot to
`dest`: new, persisting, not-retested (one screen unreachable), withdrawn and fixed findings, a
30-screen contrast finding, a hand-found re-rate (the tabs issue), a hand-found drop, an attachment, and
unreachable states grouped under a root cause. tests/data/snapshot/ is that output; test_publish checks it is current. Regenerate it with:

    cd tools/a11y && python3 -m tests.ledger
"""
import contextlib
import io
import os
import shutil
import tempfile

from tests.pathshim import A11Y, ROOT

import group
import mapdata
import signature
from lib.canon import append_jsonl, harness_hash, read_json, sha, write_json
from lib.ws import Workspace

DATA = os.path.join(ROOT, "data")
SNAPSHOT = os.path.join(A11Y, "tests", "data", "snapshot")
CHECKS = read_json(os.path.join(A11Y, "checks.json"))
WCAG = {c["number"]: c for c in read_json(os.path.join(A11Y, "wcag.json"))["criteria"]}
HAND = read_json(os.path.join(A11Y, "hand-found.json"))
RUN_A = "2026-10-01-0900"
RUN_B = "2026-10-02-1400"
BUILD = "b1d2e3"
VERSIONS = {"playwright": "1.62.1", "axe": "4.13.0", "chromium": "151.0.7000.0", "node": "v24.1.0", "python": "3.14.0"}
# schemas.md "State kinds and the checks they get"
KIND_CHECKS = {
    "page": ["axe", "records", "interactive", "target", "tabwalk"],
    "modal": ["axe", "records", "interactive", "target", "tabwalk", "dialog"],
    "drawer": ["axe", "records", "interactive", "target", "tabwalk", "dialog"],
    "menu": ["axe", "records", "interactive", "target", "dialog"],
    "popover": ["axe", "records", "interactive", "target", "dialog"],
    "form-state": ["axe", "records", "interactive", "target"],
    "empty-state": ["axe", "records", "interactive", "target"],
    "toast": ["axe", "records", "interactive", "target", "status"],
    "confirmation": ["axe", "records", "interactive", "target", "status"],
    "validation-error": ["axe", "records", "interactive", "target", "status"],
}
RULES_RAN = ["button-name", "color-contrast", "document-title", "html-has-lang", "image-alt", "label",
             "link-in-text-block", "link-name", "list", "listitem", "nested-interactive", "target-size"]
FACTS = {
    "screens": None, "criterion": None, "criterionName": None, "rule": None, "screen": None,
    "element": 'button "More"', "announced": 'button ""', "axeSummary": "Element does not have an accessible name",
    "fg": "#9d9da8", "bg": "#ffffff", "ratio": "2.85", "required": "4.5", "size": "16×16", "offset": "18",
    "stop": "4", "stops": "12", "previous": 'link "Back"', "overlay": 'dialog "Edit card limits"',
    "focus": "page body", "message": '"Saved"',
}


def raw_issues(data_dir=DATA):
    """{issue id: issue} for every issue in data_dir, raw."""
    return {i["id"]: i for a in mapdata.load_areas(data_dir, snapshot_dir=False) for i in a.get("issues", [])}


def hand_found_ids(data_dir=DATA):
    return sorted(i for i, x in raw_issues(data_dir).items() if x.get("category") == "a11y")


class Ledger:
    def __init__(self, ws_dir, run, data_dir=DATA, date=None):
        self.ws_dir, self.run, self.data_dir = ws_dir, run, data_dir
        self.date = date or run[:10]
        self.states, self.outcomes, self.findings = {}, {}, []
        self.proposals, self.advisory, self.verdicts, self.decisions, self.records = [], [], [], [], []
        self.canary_pass, self.lighthouse = True, None
        self.builds = [BUILD]

    # States ------------------------------------------------------------------

    def state(self, sid, kind="page", outcome="audited", completed=None, incomplete=(), reason=None,
              root_cause=None, first=None):
        area = sid.split(".")[0]
        self.states[sid] = {"area": area, "kind": kind, "url": f"/{area}", "root": False, "rootUrl": None,
                            "needsRecipe": True, "parent": None, "path": [sid], "labels": [], "mutates": False,
                            "source": sha(sid), "unreachable": None}
        if outcome == "audited":
            done = [c for c in (completed if completed is not None else KIND_CHECKS[kind]) if c not in incomplete]
            self.outcomes[sid] = {"outcome": "audited", "reason": None, "detail": None, "firstUnreachable": None,
                                  "completed": done, "incomplete": list(incomplete), "build": BUILD}
        else:
            self.outcomes[sid] = {"outcome": "unreachable", "reason": reason or "step-failed",
                                  "detail": {"step": 1, "rootCause": root_cause or sid, "message": "no match"},
                                  "firstUnreachable": first or self.run, "completed": [], "incomplete": [],
                                  "build": BUILD}
        return sid

    def ensure(self, sids, kind="page"):
        for s in sids:
            if s not in self.states:
                self.state(s, kind)

    # Findings ----------------------------------------------------------------

    def scanner(self, family, rule, states, element='button "More"', facts=None, primary=None, criteria=None,
                severity=None, sig=None, key=None, kind="page"):
        fam = CHECKS["families"][family]
        states = sorted(set(states) | ({primary} if primary else set()))
        self.ensure(states, kind)
        primary = primary or states[0]
        criteria = criteria or (CHECKS["customCriteria"].get(rule) or fam["criteria"][:1])
        f = dict(FACTS, element=element, screens=len(states), criterion=criteria[0],
                 criterionName=WCAG[criteria[0]]["name"], rule=rule, screen=primary)
        f.update(facts or {})
        f = {n: f[n] for n in group.fact_names(fam)}
        t = group.templates_for(fam, rule)
        if rule == "announced-as":
            key = key or signature.record_finding_key(family, sig)
        finding = {
            "key": key or f"{family}:{rule}:{sha([family, rule, element, primary])[:12]}",
            "source": "scanner", "family": family, "rule": rule, "criteria": criteria,
            "basis": "candidate" if rule == "announced-as" else "violation",
            "severityDefault": severity or fam["defaultSeverity"],
            "title": t["title"].format(**f).strip(), "summary": t["evidence"].format(**f).strip(),
            "states": states, "occurrences": [], "primary": primary,
            "sample": [primary] + [s for s in states if s != primary][:1],
            "issueId": None, "recordSig": sig, "evidenceHash": sha([family, rule, element, states]), "facts": f,
        }
        self.findings.append(finding)
        return finding

    def hand_found(self, ids=None, kind="page"):
        issues = raw_issues(self.data_dir)
        out = {}
        for iid in ids or hand_found_ids(self.data_dir):
            issue, h = issues[iid], HAND[iid]
            states = sorted(set(issue["nodeIds"]))
            for s in states:
                if s not in self.states:
                    self.state(s, kind)
            primary = states[0]
            f = {"key": signature.hand_key(iid), "source": "hand-found", "family": h["family"], "rule": "hand-found",
                 "criteria": h["criteria"], "basis": "failure", "severityDefault": issue["severity"],
                 "title": issue["title"], "summary": issue["evidence"], "states": states, "occurrences": [],
                 "primary": primary, "sample": states[:2], "issueId": iid, "recordSig": None,
                 "evidenceHash": sha(iid), "facts": {}}
            self.findings.append(f)
            out[iid] = f
        return out

    def drop_finding(self, key):
        self.findings = [f for f in self.findings if f["key"] != key]

    def find(self, key):
        return next(f for f in self.findings if f["key"] == key)

    # Ledger lines --------------------------------------------------------------

    def verdict(self, f, outcome="confirm", after=None, code=None, reason=None, date=None, **over):
        before = f["severityDefault"]
        after = after or before
        line = {"key": f["key"], "issueId": f["issueId"], "source": f["source"], "outcome": outcome,
                "severityBefore": before, "severityAfter": after,
                "reason": reason or ("Reproduced on every sampled state." if outcome == "confirm" else "Checked live."),
                "reasonCode": code, "counterEvidence": "shot:" + f["primary"] if f["source"] == "hand-found" or outcome == "drop" else None,
                "probes": [], "states": list(f["sample"]), "build": BUILD, "model": "sonnet",
                "batch": "verify-001", "prompt": "verifier@1", "attempt": 1, "second": False,
                "date": date or self.date, "criterionAnalysis": "No AA criterion fails." if code == "not-aa-failure" else None}
        line.update(over)
        self.verdicts.append(line)
        return line

    def confirm_rest(self):
        done = {v["key"] for v in self.verdicts}
        for f in self.findings:
            if f["key"] not in done:
                self.verdict(f)

    def propose(self, f, hand_id, overlap=1.0):
        p = {"id": sha([f["key"], hand_id]), "finding": f["key"], "handFound": hand_id, "overlap": overlap,
             "family": f["family"]}
        self.proposals.append(p)
        return p

    def decide(self, proposal_id, finding_key, hand_id, decision="attach", date=None):
        self.decisions.append({"proposal": proposal_id, "finding": finding_key, "handFound": hand_id,
                               "decision": decision, "reason": "Same element on the same screens.", "model": "sonnet",
                               "batch": "verify-001", "prompt": "verifier@1", "date": date or self.date})

    def record(self, sig, states):
        self.records.append({"sig": sig, "template": 'button ""', "role": "button", "states": {}, "description": "",
                             "states_seen": sorted(states), "sample": sorted(states)[0], "occurrence": {},
                             "published": None})

    def calibrate(self, date=None, unexplained=0):
        self.lighthouse = {"date": date or self.date, "chrome": "151.0.7000.0", "lighthouseAxe": "4.13.0",
                           "sweepAxe": "4.13.0",
                           "states": [{"state": "cards.index", "kind": "root", "lighthouseFailed": ["color-contrast"],
                                       "sweepRules": ["color-contrast"], "differences": []}],
                           "unexplained": unexplained}

    # Files -------------------------------------------------------------------

    @property
    def run_dir(self):
        return os.path.join(self.ws_dir, "runs", self.run)

    def path(self, name):
        return os.path.join(self.run_dir, name)

    def workspace(self):
        return Workspace(workspace=self.ws_dir, data=self.data_dir, run=self.run)

    def write(self):
        rd = self.run_dir
        os.makedirs(os.path.join(rd, "raw"), exist_ok=True)
        write_json(self.path("run.json"), {"run": self.run, "created": f"{self.date}T09:00:00",
                                           "baseUrl": "https://demo.mercury.com", "workspace": "a11y",
                                           "versions": VERSIONS, "harness": harness_hash(),
                                           "builds": self.builds, "stages": {}})
        write_json(self.path("states.json"), {"run": self.run, "map": {"nodes": len(self.states), "sha": sha(sorted(self.states))},
                                              "roots": [], "states": self.states})
        audited = sorted(s for s, o in self.outcomes.items() if o["outcome"] == "audited")
        write_json(self.path("outcomes.json"), {"run": self.run, "counts": {"audited": len(audited),
                                                "unreachable": len(self.outcomes) - len(audited)},
                                                "builds": self.builds, "states": self.outcomes})
        for sid, o in self.outcomes.items():
            write_json(os.path.join(rd, "raw", f"{sid}.json"), {
                "schema": 1, "state": sid, "kind": self.states[sid]["kind"], "status": o["outcome"],
                "reason": o["reason"], "families": {"completed": o["completed"], "incomplete": o["incomplete"]},
                "results": [], "records": [],
                "checks": {"axe": {"rulesRan": RULES_RAN, "inapplicable": []}} if "axe" in o["completed"] else {}})
        write_json(self.path("findings.json"), {"run": self.run, "inputs": group.inputs(self.workspace(), self.run),
                                                "findings": sorted(self.findings, key=lambda f: f["key"]),
                                                "proposals": self.proposals, "advisory": self.advisory, "flags": []})
        for name, lines in (("verdicts.jsonl", self.verdicts), ("decisions.jsonl", self.decisions),
                            ("records.jsonl", self.records)):
            if os.path.exists(self.path(name)):
                os.remove(self.path(name))
            if lines:
                append_jsonl(self.path(name), lines)
        write_json(self.path("canary.json"), {"prompt": "verifier@1", "batch": "canary-001", "expected": 6,
                                              "correct": 6 if self.canary_pass else 5, "pass": self.canary_pass,
                                              "items": [{"key": "canary:1", "expected": "confirm",
                                                         "got": "confirm" if self.canary_pass else "drop",
                                                         "ok": self.canary_pass}]})
        if self.lighthouse:
            write_json(self.path("lighthouse.json"), self.lighthouse)
        return self.workspace()


# ---------------------------------------------------------------------------
# The committed test snapshot (tests/data/snapshot): two publishes on real node ids
# ---------------------------------------------------------------------------

CONTRAST_30 = ["transactions.index"] + sorted(n["id"] for a in mapdata.load_areas(DATA, snapshot_dir=False)
                                              if a["area"] == "transactions" for n in a["nodes"]
                                              if n["id"] != "transactions.index")[:29]
TABS = "accounts.i-tabs-not-tabs"
HEADING = "cards.i-modal-heading-semantics"
ROWS = "settings.i-unnamed-row-buttons"


def snapshot_runs(ws_dir):
    """Publish RUN_A then RUN_B into ws_dir. Returns the two ledgers."""
    import diff
    import publish

    a = Ledger(ws_dir, RUN_A)
    a.calibrate()
    a.hand_found()
    a.scanner("contrast", "color-contrast", CONTRAST_30, element='text in "Recent transactions"',
              primary="transactions.index", facts={"ratio": "3.12", "fg": "#9d9da8"})
    a.scanner("name", "button-name", ["cards.index", "cards.detail-physical"], element='button "More actions"',
              primary="cards.index", severity="high")
    a.scanner("target-size", "target-size-own", ["home.index", "home.tasks"], element='button "Dismiss"',
              primary="home.index")
    a.scanner("keyboard", "not-reached", ["accounts.treasury", "accounts.treasury-statements"],
              element='text "Statements"', primary="accounts.treasury")
    a.confirm_rest()
    a.write()
    for mod in (diff, publish):
        if mod.cli(a.workspace(), None, []) != 0:
            raise RuntimeError(f"{mod.__name__} failed for {RUN_A}")

    b = Ledger(ws_dir, RUN_B)
    b.state("accounts.treasury-statements", outcome="unreachable", reason="step-failed")
    b.state("cards.transfer-card", kind="modal", outcome="unreachable", reason="step-failed")
    b.state("cards.transfer-card-error", kind="validation-error", outcome="unreachable", reason="blocked-by-parent",
            root_cause="cards.transfer-card")
    hand = b.hand_found()
    b.scanner("contrast", "color-contrast", CONTRAST_30, element='text in "Recent transactions"',
              primary="transactions.index", facts={"ratio": "3.12", "fg": "#9d9da8"})
    b.scanner("name", "button-name", ["cards.index", "cards.detail-physical"], element='button "More actions"',
              primary="cards.index", severity="high")
    b.ensure(["home.index", "home.tasks"])            # the target-size finding is gone: fixed
    rows = b.scanner("name", "button-name", ["settings.tokens", "settings.token-menu", "settings.tokens-new-row"],
                     element='button in "API Tokens"', primary="settings.tokens")
    p = b.propose(rows, ROWS)
    b.decide(p["id"], rows["key"], ROWS)
    b.scanner("focus-visible", "focus-not-visible", ["settings.tokens", "settings.index"],
              element='link "Create token"', primary="settings.tokens")
    b.scanner("keyboard", "not-reached", ["accounts.treasury"], element='text "Statements"')   # one screen unreachable
    b.scanner("dialog-focus", "dialog-focus-not-moved", ["cards.freeze-confirm"], kind="modal",
              element='button "Freeze card"', facts={"overlay": 'dialog "Freeze card"'})
    tasks = b.scanner("structure", "list", ["home.tasks"], element='list in "Tasks"')
    b.verdict(tasks, "drop", code="tool-false-positive", reason="The list markup is valid; axe read a detached node.")
    b.verdict(hand[TABS], "rerate", after="high",
              reason="Tab never reaches the Treasury and Financing tabs, so keyboard users cannot switch views.")
    b.verdict(hand[HEADING], "drop", code="tool-false-positive",
              reason="The Edit card limits title is an h2 inside the dialog header; the capture read a stale DOM.")
    b.confirm_rest()
    b.write()
    for mod in (diff, publish):
        if mod.cli(b.workspace(), None, []) != 0:
            raise RuntimeError(f"{mod.__name__} failed for {RUN_B}")
    return a, b


def build_test_snapshot(dest):
    """Write the four snapshot files the two runs produce into dest."""
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
        ws = os.path.join(tmp, "ws")
        snapshot_runs(ws)
        os.makedirs(dest, exist_ok=True)
        for name in ("issues.json", "report.json", "registry.json", "meta.json"):
            shutil.copyfile(os.path.join(ws, "published", name), os.path.join(dest, name))


if __name__ == "__main__":
    build_test_snapshot(SNAPSHOT)
    print(f"Wrote {os.path.relpath(SNAPSHOT, ROOT)}")
