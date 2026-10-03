"""`verdict add`, the only writer of verdicts.jsonl (R16, KTD19).

Run: cd tools/a11y && python3 -m unittest tests.test_verdict -v

Every test copies tests/data/verify/ws (seven findings: contrast, button-name with a registry id,
keyboard trap, target size, focus visibility, and two hand-found issues) into a temp workspace and
feeds `audit.py verdict add` an agent output file.
"""
import contextlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest

from tests.pathshim import A11Y

import audit
from lib import canon

RUN = "2026-10-02-1400"
FIXTURE = os.path.join(A11Y, "tests", "data", "verify", "ws")
CONTRAST = "contrast:color-contrast:0a1b2c3d4e5f"
NAME = "name:button-name:1a2b3c4d5e6f"
TRAP = "keyboard-trap:keyboard-trap:2b3c4d5e6f7a"
TARGET = "target-size:target-size:3c4d5e6f7a8b"
FOCUS = "focus-visible:focus-not-visible:4d5e6f7a8b9c"
TABS = "hand:accounts.i-tabs-not-tabs"
ROWS = "hand:settings.i-unnamed-row-buttons"
SWEEP_ENV = {"viewport": [1440, 900], "dpr": 1, "colorScheme": "light", "reducedMotion": "reduce"}


def copy_fixture(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    ws = os.path.join(tmp.name, "ws")
    shutil.copytree(FIXTURE, ws)
    return ws


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = audit.main(list(args))
    return code, out.getvalue(), err.getvalue()


def write_lines(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for x in lines:
            f.write((x if isinstance(x, str) else json.dumps(x)) + "\n")
    return path


def verdict(**over):
    """A valid confirm of the contrast finding on both sampled states."""
    line = {"key": CONTRAST, "issueId": None, "source": "scanner", "outcome": "confirm",
            "severityBefore": "medium", "severityAfter": "medium",
            "reason": "Measured 2.85:1 on both screens; the text is essential.", "reasonCode": None,
            "counterEvidence": None, "probes": [], "states": ["cards.index", "settings.tokens"],
            "build": "b1d2e3", "model": "sonnet", "batch": "verify-001", "prompt": "verifier@1", "attempt": 1,
            "second": False}
    line.update(over)
    return line


def probe(pid="p1", state="cards.index", kind="contrast", env=None, **value):
    return {"id": pid, "state": state, "kind": kind, "env": dict(SWEEP_ENV, **(env or {})), "value": value}


class VerdictTest(unittest.TestCase):
    def setUp(self):
        self.ws = copy_fixture(self)
        self.run_dir = os.path.join(self.ws, "runs", RUN)

    def add(self, *lines, name="verify-001.out.jsonl"):
        path = write_lines(os.path.join(self.run_dir, "batches", name), lines)
        code, out, err = run_cli("verdict", "add", "--file", path, "--workspace", self.ws, "--run", RUN)
        self.assertEqual(err, "")
        return code, json.loads(out)

    def stored(self):
        return canon.read_jsonl(os.path.join(self.run_dir, "verdicts.jsonl"))

    def assert_rejected(self, line, pattern):
        before = self.stored()
        code, out = self.add(line)
        self.assertEqual(code, 1, out)
        self.assertEqual(out["appended"], 0)
        (bad,) = out["rejected"]
        self.assertEqual(bad["line"], 1)
        self.assertTrue(any(re.search(pattern, r) for r in bad["reasons"]), bad["reasons"])
        self.assertEqual(self.stored(), before)

    def assert_accepted(self, line):
        before = self.stored()
        code, out = self.add(line)
        self.assertEqual((code, out["rejected"], out["appended"]), (0, [], 1), out)
        self.assertEqual(self.stored()[:-1], before)
        return self.stored()[-1]


class Confirm(VerdictTest):
    def test_a_plain_confirm_is_accepted_and_dated(self):
        got = self.assert_accepted(verdict())
        self.assertRegex(got.pop("date"), r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(got, verdict())

    def test_defaults_fill_the_optional_fields(self):
        line = verdict()
        for k in ("issueId", "reasonCode", "counterEvidence", "probes", "attempt", "second"):
            del line[k]
        got = self.assert_accepted(line)
        self.assertEqual((got["issueId"], got["reasonCode"], got["counterEvidence"], got["probes"], got["attempt"],
                          got["second"]), (None, None, None, [], 1, False))

    def test_each_required_field_is_required(self):
        for field in ("key", "source", "outcome", "severityBefore", "severityAfter", "states", "build", "model",
                      "batch", "prompt"):
            with self.subTest(field=field):
                line = verdict()
                del line[field]
                self.assert_rejected(line, field)

    def test_an_unknown_key_is_rejected(self):
        self.assert_rejected(verdict(key="contrast:color-contrast:ffffffffffff"), "not in findings.json")

    def test_the_issue_id_the_registry_holds_is_required(self):
        line = verdict(key=NAME, states=["settings.tokens"], reason="Unnamed icon button.")
        self.assert_rejected(line, "issueId")
        got = self.assert_accepted(dict(line, issueId="settings.i-a11y-name-button-in-api-tokens"))
        self.assertEqual(got["issueId"], "settings.i-a11y-name-button-in-api-tokens")

    def test_source_must_match_the_finding(self):
        self.assert_rejected(verdict(source="hand-found"), "source")

    def test_severity_before_is_the_finding_default(self):
        self.assert_rejected(verdict(severityBefore="high", severityAfter="high"), "severityBefore")

    def test_a_confirm_keeps_the_severity(self):
        self.assert_rejected(verdict(severityAfter="high"), "rerate")

    def test_states_must_belong_to_the_finding(self):
        self.assert_rejected(verdict(states=["cards.index", "home.index"]), "home.index")
        self.assert_rejected(verdict(states=[]), "states")


class Drop(VerdictTest):
    def drop(self, **over):
        line = verdict(outcome="drop", reasonCode="exception:inline", counterEvidence="shot:cards.index-row-1",
                       reason="The muted text is a disabled row label.")
        line.update(over)
        return line

    def test_a_drop_with_code_and_counter_evidence_is_accepted(self):
        got = self.assert_accepted(self.drop())
        self.assertEqual((got["outcome"], got["reasonCode"], got["counterEvidence"]),
                         ("drop", "exception:inline", "shot:cards.index-row-1"))

    def test_a_drop_without_a_reason_code_is_rejected(self):
        self.assert_rejected(self.drop(reasonCode=None), "reasonCode")

    def test_a_code_outside_the_list_is_rejected(self):
        self.assert_rejected(self.drop(reasonCode="looks-fine"), "looks-fine")

    def test_a_drop_without_counter_evidence_is_rejected(self):
        self.assert_rejected(self.drop(counterEvidence=None), "counterEvidence")
        self.assert_rejected(self.drop(counterEvidence=""), "counterEvidence")

    def test_a_drop_without_a_reason_is_rejected(self):
        self.assert_rejected(self.drop(reason=""), "reason")

    def test_a_drop_checks_every_sampled_state(self):
        self.assert_rejected(self.drop(states=["cards.index"]), "settings.tokens")

    def test_not_aa_failure_needs_a_criterion_analysis(self):
        line = self.drop(reasonCode="not-aa-failure")
        self.assert_rejected(line, "criterionAnalysis")
        got = self.assert_accepted(dict(line, criterionAnalysis="1.4.3 exempts inactive components; the row is "
                                                                 "disabled."))
        self.assertEqual(got["reasonCode"], "not-aa-failure")

    def test_fixed_since_capture_is_for_hand_found_issues_only(self):
        self.assert_rejected(self.drop(reasonCode="fixed-since-capture"), "hand-found")
        line = verdict(key=TABS, issueId="accounts.i-tabs-not-tabs", source="hand-found", outcome="drop",
                       reasonCode="fixed-since-capture", counterEvidence="shot:accounts.index-tabs",
                       reason="The tabs are now buttons in a tablist.", states=["accounts.index", "accounts.treasury"])
        got = self.assert_accepted(line)
        self.assertEqual(got["reasonCode"], "fixed-since-capture")

    def test_a_reason_code_on_a_confirm_is_rejected(self):
        self.assert_rejected(verdict(reasonCode="exception:inline"), "drops only")


class Rerate(VerdictTest):
    def test_a_rerate_with_no_reason_is_rejected(self):
        self.assert_rejected(verdict(outcome="rerate", severityAfter="high", reason=None), "reason")
        self.assert_rejected(verdict(outcome="rerate", severityAfter="high", reason="  "), "reason")

    def test_a_rerate_with_an_unchanged_severity_is_rejected(self):
        self.assert_rejected(verdict(outcome="rerate", severityAfter="medium"), "change")

    def test_a_rerate_with_a_reason_and_a_new_severity_is_accepted(self):
        got = self.assert_accepted(verdict(outcome="rerate", severityAfter="high",
                                           reason="Measured 1.9:1 on the balance, essential text."))
        self.assertEqual((got["severityBefore"], got["severityAfter"]), ("medium", "high"))


class MeasuredValues(VerdictTest):
    """KTD19: a not-reproduced-live drop on a measured family cites a passing probe taken under the
    sweep's environment (1440×900, pixel ratio 1, light, reduced motion)."""

    def live(self, probes, cite="p1", **over):
        line = verdict(outcome="drop", reasonCode="not-reproduced-live", counterEvidence=cite, probes=probes,
                       reason="The live text measures above 4.5:1.")
        line.update(over)
        return line

    def test_without_a_probe_reference_it_is_rejected(self):
        self.assert_rejected(self.live([], cite="looked fine on screen"), "probe")

    def test_a_probe_that_measured_4_6_under_the_sweep_environment_is_accepted(self):
        p = probe(fg="#6b6b75", bg="#ffffff", ratio=4.6)
        got = self.assert_accepted(self.live([p]))
        self.assertEqual(got["probes"], [p])

    def test_a_probe_taken_at_another_viewport_is_rejected(self):
        self.assert_rejected(self.live([probe(env={"viewport": [1280, 800]}, ratio=4.6)]), "1280")

    def test_a_probe_in_dark_mode_or_with_motion_is_rejected(self):
        self.assert_rejected(self.live([probe(env={"colorScheme": "dark"}, ratio=4.6)]), "dark")
        self.assert_rejected(self.live([probe(env={"reducedMotion": "no-preference"}, ratio=4.6)]), "no-preference")
        self.assert_rejected(self.live([probe(env={"dpr": 2}, ratio=4.6)]), "dpr")

    def test_a_probe_that_still_fails_cannot_overrule_the_measurement(self):
        self.assert_rejected(self.live([probe(ratio=4.4)]), "does not pass")

    def test_the_cited_probe_must_be_in_probes(self):
        self.assert_rejected(self.live([probe(ratio=4.6)], cite="p2"), "p2")

    def test_target_size_needs_a_passing_target_probe(self):
        base = dict(key=TARGET, severityBefore="low", severityAfter="low", states=["cards.index", "home.index"])
        self.assert_rejected(self.live([probe(kind="target", width=18, height=18, offset=18)], **base), "does not pass")
        self.assert_rejected(self.live([probe(ratio=7)], **base), "target")
        got = self.assert_accepted(self.live([probe(kind="target", width=18, height=18, offset=26)], **base))
        self.assertEqual(got["probes"][0]["value"]["offset"], 26)

    def test_focus_visibility_and_traps_need_passing_focus_probes(self):
        focus = dict(key=FOCUS, states=["home.index"])
        self.assert_rejected(self.live([probe(state="home.index", kind="focus", visible=False)], **focus), "does not pass")
        self.assert_accepted(self.live([probe(state="home.index", kind="focus", visible=True)], **focus))
        trap = dict(key=TRAP, severityBefore="high", severityAfter="high", states=["cards.edit-limit-modal"])
        self.assert_rejected(self.live([probe(state="cards.edit-limit-modal", kind="focus", trapped=True)], **trap),
                             "does not pass")

    def test_a_judgment_family_needs_no_probe(self):
        line = verdict(key=NAME, issueId="settings.i-a11y-name-button-in-api-tokens", outcome="drop",
                       reasonCode="not-reproduced-live", counterEvidence="shot:settings.tokens-row-menu",
                       reason="The live button is named Token actions.", states=["settings.tokens"])
        self.assert_accepted(line)

    def test_any_probe_off_the_sweep_environment_is_rejected(self):
        self.assert_rejected(verdict(probes=[probe(env={"viewport": [390, 844]}, ratio=2.85)]), "390")


class HandFound(VerdictTest):
    def tabs(self, **over):
        line = verdict(key=TABS, issueId="accounts.i-tabs-not-tabs", source="hand-found",
                       states=["accounts.index", "accounts.treasury"], reason="Tab never reaches the tab divs.")
        line.update(over)
        return line

    def test_a_hand_found_confirm_must_cite_a_probe_or_evidence(self):
        self.assert_rejected(self.tabs(), "cite")
        got = self.assert_accepted(self.tabs(probes=[probe(state="accounts.index", kind="focus", stops=12,
                                                           reached=False)]))
        self.assertEqual(got["outcome"], "confirm")

    def test_an_evidence_reference_is_enough(self):
        self.assert_accepted(self.tabs(counterEvidence="shot:accounts.index-tabwalk"))

    def test_ae7_rerate_to_high(self):
        got = self.assert_accepted(self.tabs(outcome="rerate", severityAfter="high", counterEvidence="probe:tabs-1",
                                             reason="Keyboard users cannot activate the tabs at all."))
        self.assertEqual((got["severityBefore"], got["severityAfter"]), ("medium", "high"))


class NotReached(VerdictTest):
    def test_a_state_not_reached_is_stored_as_not_retested_never_as_a_drop(self):
        got = self.assert_accepted(verdict(outcome="not-reached", reason="Edit limits button not found.",
                                           states=["cards.index"]))
        self.assertEqual((got["outcome"], got["reasonCode"], got["severityAfter"]), ("not-reached", None, "medium"))

    def test_not_reached_cannot_carry_a_drop_code(self):
        self.assert_rejected(verdict(outcome="not-reached", reasonCode="not-reproduced-live", counterEvidence="x"),
                             "drops only")

    def test_unverified_is_not_an_agent_outcome(self):
        self.assert_rejected(verdict(outcome="unverified"), "verify prepare")


class BatchFiles(VerdictTest):
    def test_one_malformed_line_is_returned_and_the_valid_lines_are_appended(self):
        good1 = verdict()
        good2 = verdict(key=TARGET, severityBefore="low", severityAfter="low", states=["cards.index", "home.index"],
                        reason="18×18 with no spacing.")
        code, out = self.add(good1, '{"key": "contrast:color-contrast:0a1b2c3d4e5f", "outcome": ', good2)
        self.assertEqual(code, 1)
        self.assertEqual(out["appended"], 2)
        (bad,) = out["rejected"]
        self.assertEqual(bad["line"], 2)
        self.assertEqual(bad["text"], '{"key": "contrast:color-contrast:0a1b2c3d4e5f", "outcome": ')
        self.assertEqual([v["key"] for v in self.stored()], [CONTRAST, TARGET])

    def test_decision_lines_are_left_for_decision_add(self):
        decision = {"proposal": "x", "finding": NAME, "handFound": "settings.i-unnamed-row-buttons",
                    "decision": "attach", "reason": "Same buttons.", "model": "sonnet", "batch": "verify-001",
                    "prompt": "verifier@1"}
        code, out = self.add(verdict(), decision)
        self.assertEqual((code, out["appended"], out["skipped"], out["rejected"]), (0, 1, 1, []))

    def test_canary_verdicts_never_enter_the_run(self):
        code, out = self.add(verdict(batch="canary-001"), name="canary-001.out.jsonl")
        self.assertEqual(code, 1)
        self.assertIn("canary", " ".join(out["rejected"][0]["reasons"]))
        self.assertEqual(self.stored(), [])

    def test_lines_are_appended_canonically(self):
        self.add(verdict())
        self.add(verdict(key=FOCUS, states=["home.index"], reason="No ring at stop 4."))
        with open(os.path.join(self.run_dir, "verdicts.jsonl"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 2)
        for text in lines:
            self.assertEqual(text, canon.canonical(json.loads(text)))


if __name__ == "__main__":
    unittest.main()
