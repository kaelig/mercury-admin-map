"""`verify prepare` and `verify status` (R16, R17, R26; KTD19, KTD20).

Run: cd tools/a11y && python3 -m unittest tests.test_verify -v

Three kinds of input: the committed verify fixture (tests/data/verify/ws), findings built in code
for batch sizes, and the group fixture (tests/data/group) run through `group` for a dry
verification with scripted verdicts and decisions.
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests.pathshim import A11Y
from tests import test_verdict as tv

import signature
import verify
from lib import canon
from lib.ws import Workspace

RUN = tv.RUN
HAND = "settings.i-unnamed-row-buttons"
PROPOSAL = canon.sha([tv.NAME, HAND])
OLD = "name:button-name:9f8e7d6c5b4a"
CHECKS = canon.read_json(os.path.join(A11Y, "checks.json"))
GROUP_FIXTURE = os.path.join(A11Y, "tests", "data", "group")


def prepare(ws_dir, run=RUN, base_url=None):
    args = ["verify", "prepare", "--workspace", ws_dir, "--run", run]
    if base_url:
        args += ["--base-url", base_url]
    code, out, err = tv.run_cli(*args)
    return code, json.loads(out) if out else None, err


def batches(ws_dir, run=RUN):
    d = os.path.join(ws_dir, "runs", run, "batches")
    if not os.path.isdir(d):
        return {}
    return {f[:-5]: canon.read_json(os.path.join(d, f)) for f in sorted(os.listdir(d))
            if f.startswith("verify-") and f.endswith(".json")}


def out_path(ws_dir, name, run=RUN):
    return os.path.join(ws_dir, "runs", run, "batches", f"{name}.out.jsonl")


def add_verdicts(ws_dir, name, lines, run=RUN):
    path = tv.write_lines(out_path(ws_dir, name, run), lines)
    code, out, err = tv.run_cli("verdict", "add", "--file", path, "--workspace", ws_dir, "--run", run)
    return code, json.loads(out)


def add_decisions(ws_dir, path, run=RUN):
    code, out, err = tv.run_cli("decision", "add", "--file", path, "--workspace", ws_dir, "--run", run)
    return code, json.loads(out)


def confirm(entry, batch, **over):
    """A verdict line a verifier would write for a batch entry."""
    line = {"key": entry["key"], "issueId": entry["issueId"], "source": entry["source"], "outcome": "confirm",
            "severityBefore": entry["severityDefault"], "severityAfter": entry["severityDefault"],
            "reason": "Reproduced on every sampled state.", "reasonCode": None,
            "counterEvidence": "shot:" + entry["primary"] if entry["source"] == "hand-found" else None,
            "probes": [], "states": entry["states"], "build": "b1d2e3", "model": "sonnet", "batch": batch,
            "prompt": "verifier@1", "attempt": entry["attempt"], "second": False}
    line.update(over)
    return line


def status_of(ws_dir, run=RUN):
    return verify.status(Workspace(workspace=ws_dir, run=run), run)


def entry_of(ws_dir, key):
    for b in batches(ws_dir).values():
        for e in b["findings"]:
            if e["key"] == key:
                return e
    raise KeyError(key)


def batch_of(ws_dir, key):
    return next(name for name, b in batches(ws_dir).items() if key in [e["key"] for e in b["findings"]])


def write_canary(ws_dir, ok=True, run=RUN):
    items = [{"key": "canary:a", "expected": "confirm", "got": "confirm", "ok": True},
             {"key": "hand:canary.i-tabs", "expected": "rerate:high", "got": "rerate:high" if ok else "confirm",
              "ok": ok}]
    canon.write_json(os.path.join(ws_dir, "runs", run, "canary.json"),
                     {"prompt": "verifier@1", "batch": "canary-001", "expected": 2, "correct": 2 if ok else 1,
                      "pass": ok, "items": items})


class Fixture(unittest.TestCase):
    def setUp(self):
        self.ws = tv.copy_fixture(self)


# ---------------------------------------------------------------------------
# Batches (KTD20)
# ---------------------------------------------------------------------------

class Batching(unittest.TestCase):
    def synthetic(self, spec):
        """spec: [(area, primary state, number of findings)]. Returns the workspace path."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws = os.path.join(tmp.name, "ws")
        states, findings = {}, []
        for area, primary, n in spec:
            states[primary] = {"area": area, "kind": "page", "path": [primary]}
            for i in range(n):
                findings.append({"key": f"contrast:color-contrast:{canon.sha([primary, i])[:12]}",
                                 "source": "scanner", "family": "contrast", "rule": "color-contrast",
                                 "criteria": ["1.4.3"], "basis": "violation", "severityDefault": "medium",
                                 "title": "t", "summary": "s", "states": [primary], "occurrences": [],
                                 "primary": primary, "sample": [primary], "issueId": None, "recordSig": None,
                                 "evidenceHash": canon.sha(i), "facts": {}})
        run_dir = os.path.join(ws, "runs", RUN)
        canon.write_json(os.path.join(run_dir, "states.json"), {"run": RUN, "states": states})
        canon.write_json(os.path.join(run_dir, "findings.json"),
                         {"run": RUN, "findings": sorted(findings, key=lambda f: f["key"]), "proposals": [],
                          "advisory": [], "flags": []})
        tv.swept(ws)
        tv.stamp(ws)
        return ws

    def test_batches_hold_at_most_10_findings_sharing_an_area_and_a_primary_state(self):
        ws = self.synthetic([("b", "b.p1", 8), ("a", "a.p2", 3), ("a", "a.p1", 12)])
        code, out, err = prepare(ws)
        self.assertEqual(code, 0, err)
        got = batches(ws)
        self.assertEqual([(name, b["area"], b["primaries"], len(b["findings"])) for name, b in got.items()],
                         [("verify-001", "a", ["a.p1"], 10), ("verify-002", "a", ["a.p1"], 2),
                          ("verify-003", "a", ["a.p2"], 3), ("verify-004", "b", ["b.p1"], 8)])
        for b in got.values():
            self.assertEqual({e["primary"] for e in b["findings"]}, set(b["primaries"]))
            self.assertEqual([e["key"] for e in b["findings"]], sorted(e["key"] for e in b["findings"]))
        keys = [e["key"] for b in got.values() for e in b["findings"]]
        self.assertEqual(len(keys), 23)
        self.assertEqual(len(set(keys)), 23)
        self.assertEqual([b["batch"] for b in out["batches"]], list(got))

    def test_the_same_input_gives_the_same_batches(self):
        ws = self.synthetic([("a", "a.p1", 12), ("b", "b.p1", 4)])
        prepare(ws)
        d = os.path.join(ws, "runs", RUN, "batches")
        first = {f: open(os.path.join(d, f), "rb").read() for f in sorted(os.listdir(d))}
        shutil.rmtree(d)
        prepare(ws)
        second = {f: open(os.path.join(d, f), "rb").read() for f in sorted(os.listdir(d))}
        self.assertEqual(first, second)

    def test_without_findings_json_prepare_fails(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.makedirs(os.path.join(tmp.name, "runs", RUN))
        code, out, err = tv.run_cli("verify", "prepare", "--workspace", tmp.name, "--run", RUN)
        self.assertNotEqual(code, 0)
        self.assertIn("findings.json", err)


class Entries(Fixture):
    def test_the_fixture_makes_one_batch_per_area_and_primary_state(self):
        code, out, err = prepare(self.ws)
        self.assertEqual(code, 0, err)
        got = {name: [e["key"] for e in b["findings"]] for name, b in batches(self.ws).items()}
        self.assertEqual(got, {"verify-001": [tv.TABS], "verify-002": [tv.TRAP],
                               "verify-003": [tv.CONTRAST, tv.TARGET], "verify-004": [tv.FOCUS],
                               "verify-005": [tv.ROWS, tv.NAME]})

    def test_a_scanner_entry_carries_what_the_verifier_needs(self):
        prepare(self.ws)
        b = batches(self.ws)["verify-003"]
        # Paths inside the repo are repo-relative (batch files are committed); the temp workspace is not.
        self.assertEqual(b["reach"], {"workspace": self.ws, "data": "data", "map": "build/map.json",
                                      "baseUrl": "https://demo.mercury.com", "run": RUN})
        e = b["findings"][0]
        raw = {s: canon.read_json(os.path.join(self.ws, "runs", RUN, "raw", f"{s}.json"))
               for s in ("cards.index", "settings.tokens")}
        self.assertEqual(e, {
            "key": tv.CONTRAST, "issueId": None, "source": "scanner", "family": "contrast",
            "rule": "color-contrast", "criteria": ["1.4.3"], "basis": "violation", "severityDefault": "medium",
            "measured": True, "title": 'Low contrast: text "Pending"',
            "summary": "axe color-contrast: #9d9da8 on #ffffff measures 2.85:1; AA needs 4.5:1.",
            "facts": {"element": 'text "Pending"', "fg": "#9d9da8", "bg": "#ffffff", "ratio": "2.85",
                      "required": "4.5", "criterion": "1.4.3", "criterionName": "Contrast (Minimum)",
                      "rule": "color-contrast", "screen": "Cards", "screens": 3},
            "guidance": CHECKS["families"]["contrast"]["guidance"],
            "states": ["cards.index", "settings.tokens"], "primary": "cards.index", "screens": 3,
            "evidenceHash": "a" * 64,
            "evidence": [{"state": s, "occurrence": raw[s]["results"][0]["occurrence"],
                          "evidence": raw[s]["results"][0]["evidence"]} for s in ("cards.index", "settings.tokens")],
            "proposals": [], "attachments": [], "attempt": 1})

    def test_entries_carry_the_proposals_and_attachments_that_concern_them(self):
        prepare(self.ws)
        name, rows = entry_of(self.ws, tv.NAME), entry_of(self.ws, tv.ROWS)
        proposal = {"id": PROPOSAL, "finding": tv.NAME, "handFound": HAND, "overlap": 0.5, "family": "name"}
        self.assertEqual(name["proposals"], [proposal])
        self.assertEqual(name["issueId"], "settings.i-a11y-name-button-in-api-tokens")
        self.assertEqual(name["measured"], False)
        self.assertEqual(name["evidence"][0]["occurrence"]["xpath"],
                         "/html/body/div/main/table/tbody/tr[1]/td[4]/button")
        self.assertEqual(rows["proposals"], [proposal])
        self.assertEqual(rows["attachments"], [{"id": canon.sha([OLD, HAND]), "finding": OLD, "handFound": HAND}])
        self.assertEqual((rows["source"], rows["evidence"], rows["states"]),
                         ("hand-found", [], ["settings.tokens", "settings.token-menu"]))

    def test_evidence_at_an_occurrence_index_now_holding_another_rule_is_left_out(self):
        raw_dir = os.path.join(self.ws, "runs", RUN, "raw")
        raw = canon.read_json(os.path.join(raw_dir, "cards.index.json"))
        raw["results"][0]["rule"] = "link-in-text-block"           # swept again after group
        canon.write_json(os.path.join(raw_dir, "cards.index.json"), raw)
        findings = canon.read_json(os.path.join(self.ws, "runs", RUN, "findings.json"))["findings"]
        contrast = next(f for f in findings if f["key"] == tv.CONTRAST)
        self.assertEqual([x["state"] for x in verify.sampled_evidence(raw_dir, contrast, {})], ["settings.tokens"])

    def test_the_base_url_travels_with_the_batch(self):
        prepare(self.ws, base_url="http://127.0.0.1:4100")
        self.assertEqual(batches(self.ws)["verify-001"]["reach"]["baseUrl"], "http://127.0.0.1:4100")


# ---------------------------------------------------------------------------
# Resume and retries (KTD20)
# ---------------------------------------------------------------------------

class Resume(Fixture):
    def test_rerunning_before_any_batch_returns_writes_nothing_new(self):
        prepare(self.ws)
        code, out, err = prepare(self.ws)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["batches"], [])
        self.assertEqual(out["inFlight"], ["verify-001", "verify-002", "verify-003", "verify-004", "verify-005"])
        self.assertEqual(len(batches(self.ws)), 5)

    def test_on_resume_only_findings_without_a_valid_verdict_are_batched(self):
        prepare(self.ws)
        b3 = batches(self.ws)["verify-003"]
        contrast, target = b3["findings"]
        bad = confirm(target, "verify-003", severityAfter="high")          # rejected: a confirm keeps severity
        code, _ = add_verdicts(self.ws, "verify-003", [confirm(contrast, "verify-003"), bad])
        self.assertEqual(code, 1)
        for name in ("verify-001", "verify-002", "verify-004", "verify-005"):
            b = batches(self.ws)[name]
            add_verdicts(self.ws, name, [confirm(e, name) for e in b["findings"]])
        code, out, err = prepare(self.ws)
        self.assertEqual(code, 0, err)
        new = {name: b for name, b in batches(self.ws).items() if name > "verify-005"}
        self.assertEqual(list(new), ["verify-006"])
        (e,) = new["verify-006"]["findings"]
        self.assertEqual((e["key"], e["attempt"]), (tv.TARGET, 2))

    def test_after_two_failed_attempts_a_finding_is_marked_unverified(self):
        prepare(self.ws)
        self.assertEqual(batch_of(self.ws, tv.TRAP), "verify-002")
        trap = entry_of(self.ws, tv.TRAP)
        add_verdicts(self.ws, "verify-002", [confirm(trap, "verify-002", outcome="drop")])   # no code: rejected
        prepare(self.ws)
        second = sorted(n for n, b in batches(self.ws).items() if tv.TRAP in [e["key"] for e in b["findings"]])
        self.assertEqual(second, ["verify-002", "verify-006"])
        tv.write_lines(out_path(self.ws, "verify-006"), [])                                  # the agent returned nothing
        code, out, err = prepare(self.ws)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["unverified"], [tv.TRAP])
        self.assertEqual(out["batches"], [])
        (line,) = [v for v in canon.read_jsonl(os.path.join(self.ws, "runs", RUN, "verdicts.jsonl"))
                   if v["key"] == tv.TRAP]
        self.assertEqual((line["outcome"], line["severityBefore"], line["severityAfter"], line["attempt"],
                          line["batch"], line["states"]),
                         ("unverified", "high", "high", 2, "verify-006", ["cards.edit-limit-modal"]))
        self.assertIn("verify-002", line["reason"])
        self.assertIn("verify-006", line["reason"])
        self.assertEqual(status_of(self.ws)["findings"]["unverified"], 1)
        _, again, _ = prepare(self.ws)
        self.assertEqual((again["unverified"], again["batches"]), ([], []))

    def test_prepare_refuses_while_a_returned_batch_is_not_added(self):
        prepare(self.ws)
        name = entry_of(self.ws, tv.NAME)
        lines = [confirm(name, "verify-005", issueId=name["issueId"]),
                 {"proposal": PROPOSAL, "decision": "attach", "reason": "Same row menus.", "model": "sonnet",
                  "batch": "verify-005", "prompt": "verifier@1"}]
        path = tv.write_lines(out_path(self.ws, "verify-005"), lines)
        tv.write_lines(out_path(self.ws, "verify-004"), [])                    # the agent died: nothing to add
        self.assertEqual(status_of(self.ws)["batches"]["returnedNotAdded"], ["verify-005"])
        code, out, err = prepare(self.ws)
        self.assertEqual((code, out), (1, None))
        self.assertIn("verify-005", err)
        self.assertNotIn("verify-004", err)
        self.assertEqual(sorted(batches(self.ws)), [f"verify-00{n}" for n in range(1, 6)])

        add_verdicts(self.ws, "verify-005", lines)                              # the verdict, not the decision
        self.assertEqual(status_of(self.ws)["batches"]["returnedNotAdded"], ["verify-005"])
        add_decisions(self.ws, path)
        self.assertEqual(status_of(self.ws)["batches"]["returnedNotAdded"], [])
        code, out, err = prepare(self.ws)
        self.assertEqual(code, 0, err)

    def test_a_returned_batch_with_only_rejected_lines_counts_as_added(self):
        prepare(self.ws)
        trap = entry_of(self.ws, tv.TRAP)
        add_verdicts(self.ws, "verify-002", [confirm(trap, "verify-002", outcome="drop")])   # no code: rejected
        self.assertEqual(status_of(self.ws)["batches"]["returnedNotAdded"], [])
        code, out, err = prepare(self.ws)
        self.assertEqual(code, 0, err)
        self.assertEqual([b["batch"] for b in out["batches"]], ["verify-006"])


# ---------------------------------------------------------------------------
# A findings.json grouped from another sweep or harness
# ---------------------------------------------------------------------------

class Stale(Fixture):
    def assert_refused(self, why="findings.json was grouped from a different sweep: rerun `audit.py group`"):
        code, out, err = prepare(self.ws)
        self.assertEqual((code, out), (1, None))
        self.assertIn(why, err)
        self.assertEqual(batches(self.ws), {})

    def test_a_sweep_that_finished_after_group_ran_is_refused(self):
        canon.write_json(os.path.join(self.ws, "runs", RUN, "outcomes.json"), {"run": RUN, "states": {
            "cards.index": {"outcome": "audited", "completed": ["axe"], "incomplete": []}}})
        self.assert_refused()

    def test_an_audited_state_swept_again_after_group_is_refused(self):
        audited = {"outcome": "audited", "completed": ["axe"], "incomplete": []}
        canon.write_json(os.path.join(self.ws, "runs", RUN, "outcomes.json"), {"run": RUN, "states": {
            "cards.index": audited, "settings.tokens": audited}})
        tv.stamp(self.ws)
        # `sweep --state cards.index`: the same outcome, and another color-contrast element at results[0].
        path = os.path.join(self.ws, "runs", RUN, "raw", "cards.index.json")
        raw = canon.read_json(path)
        first = raw["results"][0]
        other = dict(first, occurrence=dict(first["occurrence"], xpath="/html/body/div/main/h1"))
        canon.write_json(path, dict(raw, results=[other] + raw["results"]))
        self.assert_refused()

    def test_findings_grouped_under_another_harness_than_the_sweeps_are_refused(self):
        path = os.path.join(self.ws, "runs", RUN, "findings.json")
        doc = canon.read_json(path)
        canon.write_json(path, dict(doc, inputs=dict(doc["inputs"], harness="ffffffffffffffff")))
        self.assert_refused()

    def test_a_harness_change_after_the_sweep_asks_for_a_new_sweep_even_after_grouping_again(self):
        now = canon.harness_hash()
        why = (f"the run was swept with harness {now}, and the harness is now ffffffffffffffff: "
               f"rerun `audit.py sweep`, then `audit.py group`")
        with mock.patch("group.harness_hash", return_value="ffffffffffffffff"):
            self.assert_refused(why)
            tv.stamp(self.ws)                                   # `group` again, under the new harness
            self.assert_refused(why)

    def test_a_findings_json_without_inputs_is_refused(self):
        path = os.path.join(self.ws, "runs", RUN, "findings.json")
        doc = canon.read_json(path)
        del doc["inputs"]
        canon.write_json(path, doc)
        self.assert_refused()

    def test_grouping_again_clears_it(self):
        canon.write_json(os.path.join(self.ws, "runs", RUN, "outcomes.json"), {"run": RUN, "states": {}})
        tv.stamp(self.ws)
        code, out, err = prepare(self.ws)
        self.assertEqual(code, 0, err)


# ---------------------------------------------------------------------------
# Status: progress, proposals, the canary and second opinions (KTD20)
# ---------------------------------------------------------------------------

class Status(Fixture):
    def test_an_empty_run_is_pending(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.makedirs(os.path.join(tmp.name, "runs", RUN))
        self.assertEqual(verify.status(Workspace(workspace=tmp.name, run=RUN), RUN), {"pending": True})

    def test_counts_by_outcome_and_proposals(self):
        prepare(self.ws)
        contrast, target = batches(self.ws)["verify-003"]["findings"]
        focus = entry_of(self.ws, tv.FOCUS)
        add_verdicts(self.ws, "verify-003", [
            confirm(contrast, "verify-003"),
            confirm(target, "verify-003", outcome="drop", reasonCode="exception:inline",
                    counterEvidence="shot:cards.index-chip", reason="Inline chip in a sentence.")])
        add_verdicts(self.ws, "verify-004", [confirm(focus, "verify-004", outcome="not-reached",
                                                     reason="Home did not load.")])
        st = status_of(self.ws)
        self.assertEqual(st["findings"], {"total": 7, "verdicted": 2, "notReached": 1, "unverified": 0,
                                          "pending": 4, "inFlight": 4})
        self.assertEqual(st["outcomes"], {"confirm": 1, "drop": 1, "not-reached": 1})
        self.assertEqual(st["proposals"], {"total": 1, "decided": 0, "pending": [PROPOSAL]})
        path = tv.write_lines(out_path(self.ws, "verify-005"), [
            {"proposal": PROPOSAL, "decision": "attach", "reason": "Same row menus.", "model": "sonnet",
             "batch": "verify-005", "prompt": "verifier@1"}])
        add_decisions(self.ws, path)
        self.assertEqual(status_of(self.ws)["proposals"], {"total": 1, "decided": 1, "pending": []})

    def test_without_a_canary_the_waves_are_held(self):
        st = status_of(self.ws)
        self.assertEqual(st["canary"]["status"], "missing")
        self.assertTrue(st["holdWaves"])

    def test_a_wrong_canary_verdict_is_a_calibration_failure_that_holds_the_waves(self):
        write_canary(self.ws, ok=False)
        st = status_of(self.ws)
        self.assertEqual(st["canary"]["status"], "failed")
        self.assertEqual(st["canary"]["wrong"], ["hand:canary.i-tabs"])
        self.assertIn("calibration failure", st["canary"]["message"])
        self.assertTrue(st["holdWaves"])
        code, out, err = prepare(self.ws)
        self.assertTrue(out["holdWaves"])

    def test_a_passing_canary_releases_the_waves(self):
        write_canary(self.ws, ok=True)
        st = status_of(self.ws)
        self.assertEqual((st["canary"]["status"], st["holdWaves"]), ("passed", False))


class SecondOpinions(Fixture):
    BASELINE = "2026-09-20-1000"

    def setUp(self):
        super().setUp()
        pub = os.path.join(self.ws, "published")
        canon.write_json(os.path.join(pub, "meta.json"), {"run": self.BASELINE, "date": "2026-09-20"})
        canon.write_json(os.path.join(pub, "issues.json"), {"generated": [], "overlays": {
            "accounts.i-tabs-not-tabs": {"severity": None, "audit": {"verdict": "confirm"}}}})
        self.previous({tv.NAME: "b" * 64, tv.TABS: "f" * 64, tv.TRAP: "c" * 64})
        prepare(self.ws)

    def previous(self, hashes):
        canon.write_json(os.path.join(self.ws, "runs", self.BASELINE, "findings.json"),
                         {"run": self.BASELINE, "findings": [{"key": k, "evidenceHash": h} for k, h in hashes.items()]})

    def queue(self):
        return {q["key"]: q["why"] for q in status_of(self.ws)["secondOpinion"]}

    def drop_name(self, batch=None, **over):
        e = entry_of(self.ws, tv.NAME)
        return confirm(e, batch or batch_of(self.ws, tv.NAME), outcome="drop", reasonCode="tool-false-positive",
                       counterEvidence="shot:settings.tokens-menu", reason="The live button is named.", **over)

    def test_a_drop_of_a_published_finding_on_the_same_evidence_is_queued(self):
        add_verdicts(self.ws, "verify-005", [self.drop_name()])
        self.assertEqual(self.queue(), {tv.NAME: ["flip"]})

    def test_a_confirm_of_a_published_finding_is_not_queued(self):
        add_verdicts(self.ws, "verify-005", [confirm(entry_of(self.ws, tv.NAME), "verify-005")])
        self.assertEqual(self.queue(), {})

    def test_a_drop_on_new_evidence_is_not_a_flip(self):
        self.previous({tv.NAME: "0" * 64})
        add_verdicts(self.ws, "verify-005", [self.drop_name()])
        self.assertEqual(self.queue(), {})

    def test_a_drop_of_a_confirmed_hand_found_issue_is_queued(self):
        e = entry_of(self.ws, tv.TABS)
        add_verdicts(self.ws, "verify-001", [confirm(e, "verify-001", outcome="drop", reasonCode="fixed-since-capture",
                                                     reason="The tabs are buttons now.")])
        self.assertEqual(self.queue(), {tv.TABS: ["flip"]})

    def test_a_confirm_of_a_withdrawn_finding_is_queued(self):
        path = os.path.join(self.ws, "runs", RUN, "findings.json")
        doc = canon.read_json(path)
        for f in doc["findings"]:
            if f["key"] == tv.FOCUS:
                f["issueId"] = "home.i-a11y-focus-visible-link-help"
        canon.write_json(path, doc)
        reg_path = os.path.join(self.ws, "published", "registry.json")
        reg = canon.read_json(reg_path)
        reg["keys"][tv.FOCUS] = "home.i-a11y-focus-visible-link-help"
        reg["tombstones"]["home.i-a11y-focus-visible-link-help"] = {"reason": "withdrawn", "attachedTo": None,
                                                                    "run": self.BASELINE, "date": "2026-09-20"}
        canon.write_json(reg_path, reg)
        self.previous({tv.FOCUS: "e" * 64})
        e = entry_of(self.ws, tv.FOCUS)
        add_verdicts(self.ws, "verify-004", [confirm(e, "verify-004", issueId="home.i-a11y-focus-visible-link-help")])
        self.assertEqual(self.queue(), {tv.FOCUS: ["flip"]})

    def test_every_drop_of_a_high_default_finding_is_queued(self):
        e = entry_of(self.ws, tv.TRAP)
        add_verdicts(self.ws, "verify-002", [confirm(e, "verify-002", outcome="drop", reasonCode="exception:ua-control",
                                                     counterEvidence="shot:date-picker",
                                                     reason="The trap is the browser's own date picker.")])
        self.assertEqual(self.queue(), {tv.TRAP: ["high-drop"]})

    def test_a_second_opinion_clears_the_queue(self):
        add_verdicts(self.ws, "verify-005", [self.drop_name()])
        add_verdicts(self.ws, "second-001", [self.drop_name(second=True, batch="second-001", model="sonnet")])
        self.assertEqual(self.queue(), {})

    def test_the_queue_names_the_batch_to_reread(self):
        add_verdicts(self.ws, "verify-005", [self.drop_name()])
        (q,) = status_of(self.ws)["secondOpinion"]
        self.assertEqual((q["batch"], q["outcome"], q["previous"]), ("verify-005", "drop", "fail"))


# ---------------------------------------------------------------------------
# A dry verification over group's own output
# ---------------------------------------------------------------------------

class DryVerification(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = os.path.join(tmp.name, "ws")
        shutil.copytree(os.path.join(GROUP_FIXTURE, "ws"), self.ws)
        raw_dir = os.path.join(self.ws, "runs", RUN, "raw")
        path = os.path.join(self.ws, "runs", RUN, "judgments.jsonl")
        lines = canon.read_jsonl(path)
        for j in lines:   # the fixture names records "@<state>#<index>"; a judge writes their signatures
            sid, index = j["sig"][1:].split("#")
            j["sig"] = signature.record_sig(canon.read_json(os.path.join(raw_dir, f"{sid}.json"))["records"][int(index)])
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(canon.canonical(j) + "\n" for j in lines)
        tv.swept(self.ws)
        code, out, err = tv.run_cli("group", "--workspace", self.ws, "--run", RUN,
                                    "--map", os.path.join(GROUP_FIXTURE, "map.json"),
                                    "--data", os.path.join(GROUP_FIXTURE, "data"))
        self.assertEqual(code, 0, err)
        self.findings = canon.read_json(os.path.join(self.ws, "runs", RUN, "findings.json"))
        write_canary(self.ws, ok=True)

    def script(self, name, batch, fail):
        """Verdicts and decisions for one batch: confirm everything, re-rate contrast to low, decide
        every proposal; the keys in `fail` get a malformed line instead."""
        lines = []
        for e in batch["findings"]:
            if e["key"] in fail:
                lines.append(confirm(e, name, outcome="rerate"))         # unchanged severity: rejected
            elif e["family"] == "contrast":
                lines.append(confirm(e, name, outcome="rerate", severityAfter="low",
                                     reason="Placeholder text, not needed for the task."))
            else:
                lines.append(confirm(e, name))
            for p in e["proposals"]:
                lines.append({"proposal": p["id"], "decision": "attach" if p["family"] == "name" else "reject",
                              "reason": "Checked both on the live screen.", "model": "sonnet", "batch": name,
                              "prompt": "verifier@1"})
        return lines

    def run_wave(self, fail):
        code, out, err = prepare(self.ws)
        self.assertEqual(code, 0, err)
        self.assertFalse(out["holdWaves"])
        for b in out["batches"]:
            name = b["batch"]
            path = tv.write_lines(out_path(self.ws, name), self.script(name, batches(self.ws)[name], fail))
            tv.run_cli("verdict", "add", "--file", path, "--workspace", self.ws, "--run", RUN)
            add_decisions(self.ws, path)
        return out

    def test_every_finding_ends_verdicted_or_unverified_and_every_proposal_decided(self):
        keys = [f["key"] for f in self.findings["findings"]]
        self.assertEqual(len(keys), 7)
        self.assertEqual(len(self.findings["proposals"]), 2)
        focus = next(f["key"] for f in self.findings["findings"] if f["family"] == "dialog-focus")

        first = self.run_wave(fail={focus})
        self.assertEqual(sum(b["findings"] for b in first["batches"]), 7)
        second = self.run_wave(fail={focus})
        self.assertEqual([b["findings"] for b in second["batches"]], [1])
        third = self.run_wave(fail=set())
        self.assertEqual((third["batches"], third["unverified"]), ([], [focus]))

        st = status_of(self.ws)
        self.assertEqual(st["findings"], {"total": 7, "verdicted": 6, "notReached": 0, "unverified": 1,
                                          "pending": 0, "inFlight": 0})
        self.assertEqual(st["proposals"]["pending"], [])
        self.assertEqual(st["proposals"]["decided"], 2)
        final = {}
        for v in canon.read_jsonl(os.path.join(self.ws, "runs", RUN, "verdicts.jsonl")):
            final[v["key"]] = v["outcome"]
        self.assertEqual(sorted(final), sorted(keys))
        self.assertEqual(final[focus], "unverified")
        contrast = next(f["key"] for f in self.findings["findings"] if f["family"] == "contrast")
        self.assertEqual(final[contrast], "rerate")


if __name__ == "__main__":
    unittest.main()
