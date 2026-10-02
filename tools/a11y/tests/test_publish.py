"""U8: `publish`, the atomic snapshot the map, the export and the report read (R16–R21, R26; KTD16, KTD21, KTD22).

Run: cd tools/a11y && python3 -m unittest tests.test_publish -v

Ledgers come from tests/ledger.py (synthetic, on real data/ node ids) or from the group fixture
(tests/data/group) run through `group` with the real data/, for findings with real facts.
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.pathshim import A11Y, ROOT
from tests.ledger import (CHECKS, DATA, HEADING, RUN_A, RUN_B, ROWS, SNAPSHOT, TABS, Ledger, build_test_snapshot,
                          hand_found_ids, raw_issues)

import audit
import diff
import mapdata
import publish
import signature
import status
from lib.canon import read_json, write_json
from lib.ws import Workspace

GROUP_FIXTURE = os.path.join(A11Y, "tests", "data", "group")
GROUP_RUN = "2026-10-02-1400"
ID = re.compile(r"^[a-z0-9-]+\.i-a11y-[a-z0-9.-]+$")


def quiet(fn, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(*args)
    return code, out.getvalue(), err.getvalue()


def stage(ws, name):
    mod = {"diff": diff, "publish": publish}[name]
    return quiet(mod.cli, ws, None, [])


def snapshot_bytes(ws_dir):
    pub = os.path.join(ws_dir, "published")
    if not os.path.isdir(pub):
        return None
    out = {}
    for name in sorted(os.listdir(pub)):
        with open(os.path.join(pub, name), "rb") as f:
            out[name] = f.read()
    return out


def published(ws_dir, name):
    return read_json(os.path.join(ws_dir, "published", name))


def by_id(issues_doc):
    return {i["id"]: i for i in issues_doc["generated"]}


def scanner_of(led, family):
    return next(f for f in led.findings if f["family"] == family and f["source"] == "scanner")


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws_dir = os.path.join(self.tmp.name, "ws")

    def ledger(self, run=RUN_A, data_dir=DATA):
        return Ledger(self.ws_dir, run, data_dir=data_dir)

    def run_stages(self, led, expect=0):
        ws = led.write()
        code, out, err = stage(ws, "diff")
        self.assertEqual(code, 0, err)
        code, out, err = stage(ws, "publish")
        self.assertEqual(code, expect, out + err)
        return ws, out, err

    def first_run(self, **kw):
        """Run A: three scanner findings and the 17 hand-found issues, every one confirmed."""
        led = self.ledger(RUN_A, **kw)
        led.calibrate()
        led.hand_found()
        led.scanner("contrast", "color-contrast", ["cards.index", "cards.detail-physical", "cards.subscriptions"],
                    element='text in "Spend this month"', primary="cards.index")
        led.scanner("name", "button-name", ["settings.tokens"], element='button in "API Tokens"', severity="high")
        led.scanner("target-size", "target-size-own", ["home.index", "home.tasks"], element='button "Dismiss"',
                    primary="home.index")
        led.confirm_rest()
        self.run_stages(led)
        return led

    # First run ------------------------------------------------------------------

    def test_a_first_run_publishes_every_confirmed_finding_with_copy_audit_and_wcag(self):
        led = self.first_run()
        issues = published(self.ws_dir, "issues.json")
        gen = by_id(issues)
        self.assertEqual(sorted(gen), ["cards.i-a11y-contrast-text-in-spend-this-month",
                                       "home.i-a11y-target-size-button-dismiss",
                                       "settings.i-a11y-name-button-in-api-tokens"])
        c = gen["cards.i-a11y-contrast-text-in-spend-this-month"]
        self.assertEqual(c["area"], "cards")
        self.assertEqual(c["category"], "a11y")
        self.assertEqual(c["severity"], "medium")
        self.assertEqual(c["nodeIds"], ["cards.index", "cards.detail-physical", "cards.subscriptions"])
        self.assertEqual(c["wcag"], ["1.4.3"])
        self.assertEqual(c["title"], 'Low contrast: text in "Spend this month"')
        self.assertEqual(c["detail"], 'The text in "Spend this month" uses #9d9da8 on #ffffff, a contrast of 2.85:1 '
                                      'where AA needs 4.5:1, so people with low vision may not be able to read it. '
                                      'WCAG 1.4.3 Contrast (Minimum).')
        self.assertEqual(c["evidence"], "axe color-contrast: #9d9da8 on #ffffff measures 2.85:1; AA needs 4.5:1. "
                                        "Verifier: Reproduced on every sampled state.")
        self.assertEqual(c["copy"]["title"], 'Raise the contrast of the text in "Spend this month"')
        self.assertEqual(c["copy"]["impact"], "accessibility")
        self.assertIs(c["copy"]["demoArtifactRisk"], False)
        self.assertEqual(c["copy"]["acceptance"][0], 'The text in "Spend this month" reaches at least 4.5:1 against its background.')
        self.assertEqual(sorted(c["copy"]), sorted(["title", "summary", "userImpact", "fixHypothesis", "acceptance",
                                                    "verify", "searchTerms", "impact", "confidence",
                                                    "confidenceReason", "demoArtifactRisk"]))
        a = c["audit"]
        self.assertEqual(a["criteria"], [{"number": "1.4.3", "name": "Contrast (Minimum)",
                                          "url": "https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum"}])
        self.assertEqual((a["family"], a["verdict"], a["date"], a["build"], a["run"], a["status"]),
                         ("contrast", "confirm", "2026-10-01", "b1d2e3", RUN_A, "new"))
        self.assertEqual(a["checkedStates"], ["cards.index", "cards.detail-physical"])
        self.assertIn("Reproduced on every sampled state.", a["evidence"])
        self.assertEqual(gen["settings.i-a11y-name-button-in-api-tokens"]["severity"], "high")
        self.assertEqual(set(issues["overlays"]), set(hand_found_ids()))
        reg = published(self.ws_dir, "registry.json")
        self.assertEqual(sorted(reg["keys"].values()), sorted(gen))
        self.assertEqual(reg["held"], sorted(gen))
        self.assertEqual((reg["tombstones"], reg["attachments"]), ({}, []))
        meta = published(self.ws_dir, "meta.json")
        self.assertEqual((meta["run"], meta["date"], meta["builds"]), (RUN_A, "2026-10-01", ["b1d2e3"]))
        self.assertEqual(meta["lighthouse"]["run"], RUN_A)
        self.assertEqual(meta["lighthouse"]["date"], "2026-10-01")
        self.assertEqual(meta["lighthouse"]["result"]["lighthouseAxe"], "4.13.0")
        self.assertEqual(meta["coverage"], {"states": len(led.states), "audited": len(led.states), "unreachable": 0})

    def test_ids_use_the_ticket_alphabet_and_never_clash_with_hand_found_ids(self):
        self.first_run()
        ids = list(by_id(published(self.ws_dir, "issues.json")))
        self.assertTrue(all(ID.match(i) for i in ids), ids)
        self.assertFalse(set(ids) & set(raw_issues()))

    def test_within_an_area_generated_issues_follow_severity_then_screen_count_with_the_primary_first(self):
        led = self.ledger(RUN_A)
        led.hand_found()
        led.scanner("target-size", "target-size-own", ["cards.index", "cards.more-menu", "cards.subscriptions"],
                    element='button "Low big"', primary="cards.subscriptions")
        led.scanner("contrast", "color-contrast", ["cards.index"], element='text "Medium one"')
        led.scanner("contrast", "color-contrast", ["cards.detail-physical", "cards.index"], element='text "Medium two"',
                    primary="cards.index")
        led.scanner("keyboard-trap", "keyboard-trap", ["cards.detail-virtual"], element='button "High one"')
        led.confirm_rest()
        self.run_stages(led)
        gen = published(self.ws_dir, "issues.json")["generated"]
        cards = [i for i in gen if i["area"] == "cards"]
        order = [next(w for w in ("High one", "Medium two", "Medium one", "Low big") if w in i["title"]) for i in cards]
        self.assertEqual(order, ["High one", "Medium two", "Medium one", "Low big"])
        self.assertEqual([i["severity"] for i in cards], ["high", "medium", "medium", "low"])
        low = cards[-1]
        self.assertEqual(low["nodeIds"], ["cards.subscriptions", "cards.index", "cards.more-menu"])
        self.assertEqual([i["area"] for i in gen], sorted(i["area"] for i in gen))

    def test_evidence_is_one_line_of_at_most_300_characters_with_the_verifier_reason_kept_whole(self):
        led = self.ledger(RUN_A)
        led.hand_found()
        f = led.scanner("name", "button-name", ["cards.index"], element='button "More"',
                        facts={"axeSummary": "Element does not have inner text that is visible to screen readers; "
                                             "aria-label attribute does not exist or is empty; aria-labelledby "
                                             "attribute does not exist; Element has no title attribute; Element "
                                             "does not have an implicit (wrapped) <label>; Element's default "
                                             "semantics were not overridden with role=\"none\""})
        reason = ("The icon-only button opens the card actions menu and has no visible or programmatic name, so a "
                  "screen-reader user hears only \"button\" and cannot tell which card it acts on.")
        led.verdict(f, reason=reason)
        led.confirm_rest()
        self.run_stages(led)
        ev = published(self.ws_dir, "issues.json")["generated"][0]["evidence"]
        self.assertLessEqual(len(ev), 300)
        self.assertNotIn("\n", ev)
        self.assertTrue(ev.startswith("button-name: announced as"), ev)
        self.assertTrue(ev.endswith("Verifier: " + reason), ev)
        self.assertIn("…", ev)

    def test_two_issues_of_one_family_from_different_components_get_different_titles(self):
        led = self.ledger(RUN_A)
        led.hand_found()
        led.scanner("name", "button-name", ["cards.index"], element='button "More"', key="name:button-name:111111111111")
        led.scanner("name", "button-name", ["home.index"], element='button "More"', key="name:button-name:222222222222")
        led.confirm_rest()
        self.run_stages(led)
        gen = published(self.ws_dir, "issues.json")["generated"]
        titles = [i["title"] for i in gen]
        self.assertEqual(len(set(t.lower() for t in titles)), 2, titles)
        self.assertEqual(len({i["copy"]["title"].lower() for i in gen}), 2)
        self.assertTrue(all(len(t) <= 80 for t in titles))

    def incomplete(self, led, family, rule, message_key, states, primary, elements="48 elements"):
        """An incomplete group as `group` writes it: one finding per rule and messageKey, element-free facts."""
        inc = CHECKS["incomplete"]
        f = led.scanner(family, rule, states, primary=primary)
        facts = {"rule": rule, "messageKey": message_key, "why": inc["messageKeyText"][message_key],
                 "elements": elements, "screen": primary, "criterion": f["criteria"][0],
                 "criterionName": f["facts"]["criterionName"]}
        f.update(key=signature.incomplete_key(family, rule, message_key), basis="incomplete", facts=facts,
                 title=inc["templates"]["title"].format(**facts), summary=inc["templates"]["evidence"].format(**facts))
        return f

    def test_a_published_incomplete_group_gets_the_incomplete_copy_and_an_element_free_id(self):
        led = self.ledger(RUN_A)
        led.hand_found()
        f = self.incomplete(led, "role-state", "aria-valid-attr-value", "controlsWithinPopup",
                            ["cards.index", "cards.detail-physical", "home.index"], primary="cards.index")
        led.scanner("role-state", "aria-valid-attr-value", ["cards.index"], element='button "Card menu"')
        led.confirm_rest()
        self.run_stages(led)
        gen = by_id(published(self.ws_dir, "issues.json"))
        iid = "cards.i-a11y-aria-valid-attr-value-controlswithinpopup"
        self.assertEqual(sorted(gen), [iid, "cards.i-a11y-role-state-button-card-menu"])
        self.assertEqual(published(self.ws_dir, "registry.json")["keys"][f["key"]], iid)
        i, t = gen[iid], CHECKS["incomplete"]["templates"]
        self.assertEqual(i["title"], "Needs review: aria-valid-attr-value, aria-controls names a closed popup")
        self.assertEqual(i["detail"], t["detail"].format(**f["facts"]))
        self.assertTrue(i["detail"].startswith("axe could not decide aria-valid-attr-value for 48 elements: "
                                               "aria-controls names a closed popup."), i["detail"])
        self.assertEqual(i["evidence"], "axe aria-valid-attr-value could not decide 48 elements (controlsWithinPopup): "
                                        "aria-controls names a closed popup. Verifier: Reproduced on every sampled state.")
        expected = {k: ([s.format(**f["facts"]) for s in v] if isinstance(v, list) else v.format(**f["facts"]))
                    for k, v in t["copy"].items()}
        self.assertEqual(i["copy"], dict(expected, impact="accessibility", demoArtifactRisk=False))
        self.assertEqual(i["copy"]["title"], "Review and fix aria-valid-attr-value: aria-controls names a closed popup")
        self.assertEqual(i["copy"]["confidence"], "medium")
        self.assertEqual((i["wcag"], i["nodeIds"][0], i["audit"]["family"]), (["4.1.2"], "cards.index", "role-state"))
        row = next(r for r in published(self.ws_dir, "report.json")["tables"]["screenReader"] if r["id"] == iid)
        self.assertEqual((row["element"], row["rule"]), ("48 elements", "aria-valid-attr-value"))

    # Later runs ----------------------------------------------------------------

    def second_run(self):
        """Run B after first_run: the target-size finding is gone, the rest confirmed again."""
        self.first_run()
        led = self.ledger(RUN_B)
        led.hand_found()
        led.scanner("contrast", "color-contrast", ["cards.index", "cards.detail-physical", "cards.subscriptions"],
                    element='text in "Spend this month"', primary="cards.index")
        led.scanner("name", "button-name", ["settings.tokens"], element='button in "API Tokens"', severity="high")
        led.ensure(["home.index", "home.tasks"])
        return led

    def test_ae6_a_published_finding_that_no_longer_reproduces_is_fixed_out_of_the_map_and_tombstoned(self):
        led = self.second_run()
        led.confirm_rest()
        ws, out, _ = self.run_stages(led)
        self.assertEqual(json.loads(out)["state"], "published")
        iid = "home.i-a11y-target-size-button-dismiss"
        self.assertNotIn(iid, by_id(published(self.ws_dir, "issues.json")))
        reg = published(self.ws_dir, "registry.json")
        self.assertEqual(reg["tombstones"][iid], {"reason": "fixed", "attachedTo": None, "run": RUN_B, "date": "2026-10-02"})
        self.assertIn(iid, reg["held"])
        report = published(self.ws_dir, "report.json")
        self.assertEqual([(r["formerId"], r["id"], r["screens"]) for r in report["fixed"]], [(iid, None, 2)])
        self.assertEqual(report["baseline"], RUN_A)
        self.assertEqual(report["changes"]["findings"]["fixed"], 1)
        self.assertEqual(report["changes"]["findings"]["persisting"], 2)
        self.assertEqual(diff.status(ws, RUN_B)["findings"]["fixed"], 1)

    def test_a_run_without_calibration_publishes_the_previous_calibration_with_its_run_and_date(self):
        led = self.second_run()
        led.confirm_rest()
        self.run_stages(led)
        meta = published(self.ws_dir, "meta.json")
        self.assertEqual((meta["run"], meta["lighthouse"]["run"], meta["lighthouse"]["date"]), (RUN_B, RUN_A, "2026-10-01"))
        line = published(self.ws_dir, "report.json")["coverage"]["lighthouse"]
        self.assertEqual((line["run"], line["date"]), (RUN_A, "2026-10-01"))
        self.assertIn("2026-10-01", line["line"])

    def test_withdrawn_tombstones_the_id_and_a_later_confirmation_brings_it_back_as_persisting(self):
        led = self.second_run()
        name = scanner_of(led, "name")
        led.verdict(name, "drop", code="exception:equivalent-control", reason="A visible label names it.")
        led.confirm_rest()
        self.run_stages(led)
        iid = "settings.i-a11y-name-button-in-api-tokens"
        self.assertNotIn(iid, by_id(published(self.ws_dir, "issues.json")))
        self.assertEqual(published(self.ws_dir, "registry.json")["tombstones"][iid]["reason"], "withdrawn")
        row = published(self.ws_dir, "report.json")["withdrawn"][0]
        self.assertEqual((row["id"], row["formerId"], row["reasonCode"]), (None, iid, "exception:equivalent-control"))

        c = Ledger(self.ws_dir, "2026-10-03-1000")
        c.hand_found()
        c.scanner("contrast", "color-contrast", ["cards.index", "cards.detail-physical", "cards.subscriptions"],
                  element='text in "Spend this month"', primary="cards.index")
        c.scanner("name", "button-name", ["settings.tokens"], element='button in "API Tokens"', severity="high")
        c.ensure(["home.index", "home.tasks"])
        c.confirm_rest()
        self.run_stages(c)
        self.assertEqual(read_json(c.path("diff.json"))["findings"][name["key"]]["status"], "persisting")
        self.assertIn(iid, by_id(published(self.ws_dir, "issues.json")))
        self.assertNotIn(iid, published(self.ws_dir, "registry.json")["tombstones"])

    def test_a_fixed_key_confirmed_again_reappears_under_the_same_id(self):
        led = self.second_run()
        led.confirm_rest()
        self.run_stages(led)
        c = Ledger(self.ws_dir, "2026-10-03-1000")
        c.hand_found()
        f = c.scanner("target-size", "target-size-own", ["home.index", "home.tasks"], element='button "Dismiss"',
                      primary="home.index")
        c.confirm_rest()
        self.run_stages(c)
        iid = "home.i-a11y-target-size-button-dismiss"
        self.assertEqual(read_json(c.path("diff.json"))["findings"][f["key"]]["status"], "reappeared")
        self.assertIn(iid, by_id(published(self.ws_dir, "issues.json")))
        reg = published(self.ws_dir, "registry.json")
        self.assertNotIn(iid, reg["tombstones"])
        self.assertEqual(reg["keys"][f["key"]], iid)

    def test_a_published_finding_not_retested_stays_in_the_map_with_the_union_of_screens(self):
        led = self.second_run()
        led.state("cards.subscriptions", outcome="unreachable")
        contrast = scanner_of(led, "contrast")
        led.drop_finding(contrast["key"])
        led.scanner("contrast", "color-contrast", ["cards.index", "cards.detail-physical"],
                    element='text in "Spend this month"', primary="cards.index", key=contrast["key"])
        led.confirm_rest()
        self.run_stages(led)
        iid = "cards.i-a11y-contrast-text-in-spend-this-month"
        issue = by_id(published(self.ws_dir, "issues.json"))[iid]
        self.assertEqual(issue["nodeIds"], ["cards.index", "cards.detail-physical", "cards.subscriptions"])
        self.assertEqual(issue["audit"]["status"], "not-retested")
        report = published(self.ws_dir, "report.json")
        self.assertEqual([r["id"] for r in report["notRetested"]], [iid])
        self.assertNotIn(iid, [r["id"] for r in report["findings"]])

    def test_a_published_finding_the_sweep_could_not_reach_is_carried_forward_unchanged(self):
        led = self.second_run()
        contrast = scanner_of(led, "contrast")
        led.drop_finding(contrast["key"])
        for s in ("cards.index", "cards.detail-physical", "cards.subscriptions"):
            led.state(s, outcome="unreachable")
        led.confirm_rest()
        before = by_id(published(self.ws_dir, "issues.json"))["cards.i-a11y-contrast-text-in-spend-this-month"]
        self.run_stages(led)
        after = by_id(published(self.ws_dir, "issues.json"))["cards.i-a11y-contrast-text-in-spend-this-month"]
        self.assertEqual(after["audit"]["status"], "not-retested")
        self.assertEqual(after["audit"]["run"], RUN_A)
        self.assertEqual(dict(after, audit=None), dict(before, audit=None))

    # Hand-found overlays ---------------------------------------------------------

    def hand_run(self):
        """Run A with AE7's re-rate, a drop, and an attachment of a scanner finding to ROWS."""
        led = self.ledger(RUN_A)
        hand = led.hand_found()
        rows = led.scanner("name", "button-name", ["settings.tokens", "settings.index"], element='button in "API Tokens"',
                           primary="settings.tokens")
        p = led.propose(rows, ROWS)
        led.decide(p["id"], rows["key"], ROWS)
        self.reason = "Tab never reaches the Treasury and Financing tabs, so keyboard users cannot switch views."
        led.verdict(hand[TABS], "rerate", after="high", reason=self.reason)
        led.verdict(hand[HEADING], "drop", code="tool-false-positive", reason="The title is an h2 in the live demo.")
        led.confirm_rest()
        self.run_stages(led)
        return led, rows

    def test_ae7_a_hand_found_rerate_raises_the_severity_and_appends_the_reason_once(self):
        self.hand_run()
        ov = published(self.ws_dir, "issues.json")["overlays"][TABS]
        self.assertEqual(ov["severity"], "high")
        self.assertEqual(ov["evidenceAppend"], f"Verifier (2026-10-01): {self.reason}")
        self.assertEqual(ov["history"], "rated high on 2026-10-01, previously medium")
        self.assertEqual(ov["wcag"], ["2.1.1", "4.1.2"])
        self.assertEqual((ov["audit"]["verdict"], ov["audit"]["status"]), ("rerate", "re-rated"))
        merged = self.merged_issue(TABS)
        raw = raw_issues()[TABS]
        self.assertEqual(merged["severity"], "high")
        self.assertEqual(merged["evidence"], f"{raw['evidence']} Verifier (2026-10-01): {self.reason}")

    def test_a_hand_found_drop_leaves_the_map_record_unchanged(self):
        self.hand_run()
        ov = published(self.ws_dir, "issues.json")["overlays"][HEADING]
        self.assertEqual((ov["severity"], ov["evidenceAppend"], ov["addNodeIds"]), (None, None, []))
        self.assertEqual(ov["history"], "not reproduced on 2026-10-01: The title is an h2 in the live demo.")
        self.assertEqual(mapdata.viewer_issue(self.merged_issue(HEADING)), raw_issues()[HEADING])
        row = next(r for r in published(self.ws_dir, "report.json")["handFound"] if r["id"] == HEADING)
        self.assertEqual((row["status"], row["reason"]), ("not-reproduced", "tool false positive"))

    def test_an_attachment_adds_the_scanner_findings_screens_instead_of_a_second_issue(self):
        led, rows = self.hand_run()
        issues = published(self.ws_dir, "issues.json")
        self.assertEqual(issues["generated"], [])
        self.assertEqual(issues["overlays"][ROWS]["addNodeIds"], ["settings.index"])
        self.assertEqual(self.merged_issue(ROWS)["nodeIds"], ["settings.tokens", "settings.token-menu", "settings.index"])
        reg = published(self.ws_dir, "registry.json")
        self.assertEqual(reg["attachments"], [{"finding": rows["key"], "handFound": ROWS, "run": RUN_A}])
        report = published(self.ws_dir, "report.json")
        row = next(r for r in report["handFound"] if r["id"] == ROWS)
        self.assertEqual([(a["key"], a["status"], a["nodeIds"]) for a in row["attached"]],
                         [(rows["key"], "new", ["settings.tokens", "settings.index"])])
        self.assertEqual(row["screens"], 3)
        self.assertEqual(report["changes"]["findings"]["new"], 0)
        self.assertNotIn(rows["key"], [r["key"] for r in report["findings"]])

    def test_dropping_a_hand_found_issue_releases_its_attached_findings_under_their_own_ids(self):
        led, rows = self.hand_run()
        b = Ledger(self.ws_dir, RUN_B)
        hand = b.hand_found()
        b.scanner("name", "button-name", ["settings.tokens", "settings.index"], element='button in "API Tokens"',
                  primary="settings.tokens")
        b.verdict(hand[ROWS], "drop", code="fixed-since-capture", reason="Every row menu now has a name.")
        b.confirm_rest()
        self.run_stages(b)
        issues = published(self.ws_dir, "issues.json")
        self.assertEqual([i["nodeIds"] for i in issues["generated"]], [["settings.tokens", "settings.index"]])
        self.assertEqual(issues["overlays"][ROWS]["addNodeIds"], [])
        self.assertEqual(published(self.ws_dir, "registry.json")["attachments"], [])

    def merged_issue(self, iid):
        with contextlib.redirect_stderr(io.StringIO()):
            areas = mapdata.load_areas(DATA, snapshot_dir=os.path.join(self.ws_dir, "published"))
        return next(i for a in areas for i in a["issues"] if i["id"] == iid)

    # Report ---------------------------------------------------------------------

    def test_the_report_model_states_its_coverage_and_every_section(self):
        led = self.ledger(RUN_A)
        led.calibrate()
        led.state("cards.edit-limit-modal", kind="modal", outcome="unreachable", reason="step-failed")
        led.state("cards.limit-saved", kind="toast", outcome="unreachable", reason="blocked-by-parent",
                  root_cause="cards.edit-limit-modal")
        led.hand_found()
        led.scanner("contrast", "color-contrast", ["cards.index"], element='text "Pending"')
        led.scanner("keyboard", "not-reached", ["cards.detail-physical"], element='text "Edit"')
        led.confirm_rest()
        self.run_stages(led)
        r = published(self.ws_dir, "report.json")
        self.assertEqual((r["run"], r["date"], r["captured"], r["baseline"], r["standard"]),
                         (RUN_A, "2026-10-01", "2026-09-24", "none", "WCAG 2.2 AA"))
        cov = r["coverage"]
        tested = {c["number"]: c for c in cov["tested"]}
        untested = {c["number"]: c for c in cov["untested"]}
        self.assertEqual(len(tested) + len(untested), 55)
        self.assertEqual(tested["1.4.3"]["coverage"], "partial")
        self.assertEqual(tested["1.4.3"]["checks"], ["color-contrast"])
        self.assertEqual(tested["2.5.8"]["coverage"], "full")
        self.assertIn("2.1.1", tested)
        self.assertIn("4.1.3", untested)          # no toast state was audited, so the status check never ran
        for c in ("1.4.12", "1.4.13", "2.4.11", "2.5.7"):
            self.assertTrue(untested[c]["named"], c)
        self.assertEqual(cov["dimensions"], ["zoom and reflow", "real screen readers", "mobile", "dark theme"])
        self.assertEqual(cov["builds"], ["b1d2e3"])
        self.assertIn("hypothesis", cov["caveat"])
        self.assertIn("0 unexplained differences", cov["lighthouse"]["line"])
        self.assertEqual((cov["states"], cov["unreachable"]), (len(led.states), 2))
        self.assertEqual(r["unreachable"], [{"rootCause": "cards.edit-limit-modal", "reason": "step-failed",
                                             "firstUnreachable": RUN_A, "states": [
            {"state": "cards.edit-limit-modal", "reason": "step-failed", "firstUnreachable": RUN_A},
            {"state": "cards.limit-saved", "reason": "blocked-by-parent", "firstUnreachable": RUN_A}]}])
        hand = [raw_issues()[h]["severity"] for h in hand_found_ids()]
        self.assertEqual(r["counts"], {"high": hand.count("high"), "medium": hand.count("medium") + 2,
                                       "low": hand.count("low")})
        self.assertEqual(len(r["findings"]), 19)
        self.assertEqual(r["changes"]["findings"]["new"], 2)
        self.assertEqual(r["changes"]["handFound"]["first-audited"], 17)
        contrast = next(x for x in r["findings"] if x["family"] == "contrast" and x["source"] == "scanner")
        self.assertEqual((contrast["principle"], contrast["screens"], contrast["status"]), ("Perceivable", 1, "new"))
        self.assertTrue(contrast["fix"])
        scanned = next(row for row in r["tables"]["contrast"] if row["id"] == contrast["id"])
        self.assertEqual((scanned["fg"], scanned["bg"], scanned["ratio"], scanned["required"]),
                         ("#9d9da8", "#ffffff", "2.85", "4.5"))
        self.assertIn("billpay.i-manage-link", [row["id"] for row in r["tables"]["contrast"]])
        self.assertTrue(all(row["arrowKeys"] == "not tested" for row in r["tables"]["keyboard"]))
        rows = {x["id"]: x for x in r["findings"]}
        ranks = [({"high": 0, "medium": 1, "low": 2}[rows[i]["severity"]], -rows[i]["screens"]) for i in r["priority"]]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(sorted(r["priority"]), sorted(rows))
        self.assertEqual(len(r["handFound"]), 17)

    # Republish ---------------------------------------------------------------------

    def group_workspace(self):
        ws_dir = os.path.join(self.tmp.name, "gws")
        shutil.copytree(os.path.join(GROUP_FIXTURE, "ws"), ws_dir)
        ws = Workspace(workspace=ws_dir, data=DATA, map=os.path.join(GROUP_FIXTURE, "map.json"), run=GROUP_RUN)
        return ws_dir, ws

    def group(self, ws_dir):
        args = ["group", "--workspace", ws_dir, "--data", DATA, "--map", os.path.join(GROUP_FIXTURE, "map.json"),
                "--run", GROUP_RUN]
        code, out, err = quiet(audit.main, args)
        self.assertEqual(code, 0, err)
        return read_json(os.path.join(ws_dir, "runs", GROUP_RUN, "findings.json"))

    def complete_ledger(self, ws, doc):
        """outcomes.json from the raw files, run.json, a passing canary, verdicts and decisions."""
        run_dir = ws.run_dir(GROUP_RUN)
        states = {}
        for name in sorted(os.listdir(os.path.join(run_dir, "raw"))):
            raw = read_json(os.path.join(run_dir, "raw", name))
            states[raw["state"]] = {"outcome": raw["status"], "reason": raw.get("reason"),
                                    "detail": raw.get("reasonDetail"), "firstUnreachable": GROUP_RUN if raw["status"] == "unreachable" else None,
                                    "completed": raw["families"]["completed"], "incomplete": raw["families"]["incomplete"],
                                    "build": raw.get("build")}
        write_json(os.path.join(run_dir, "outcomes.json"), {"run": GROUP_RUN, "counts": {}, "builds": ["b1d2e3"], "states": states})
        write_json(os.path.join(run_dir, "run.json"), {"run": GROUP_RUN, "created": "2026-10-02T14:00:00",
                                                       "versions": {"axe": "4.13.0"}, "builds": ["b1d2e3"]})
        led = Ledger(ws.path, GROUP_RUN)
        led.findings = doc["findings"]
        led.calibrate()
        write_json(os.path.join(run_dir, "lighthouse.json"), led.lighthouse)
        write_json(os.path.join(run_dir, "canary.json"), {"prompt": "verifier@1", "batch": "canary-001", "expected": 1,
                                                          "correct": 1, "pass": True, "items": []})
        for f in doc["findings"]:
            if f["key"] == "hand:" + TABS:
                led.verdict(f, "rerate", after="high", reason="Keyboard users cannot reach the tabs.", date="2026-10-02")
            else:
                led.verdict(f, date="2026-10-02")
        lines = led.verdicts
        with open(os.path.join(run_dir, "verdicts.jsonl"), "w", encoding="utf-8") as fh:
            for v in lines:
                fh.write(json.dumps(v) + "\n")
        with open(os.path.join(run_dir, "decisions.jsonl"), "w", encoding="utf-8") as fh:
            for p in doc["proposals"]:
                fh.write(json.dumps({"proposal": p["id"], "finding": p["finding"], "handFound": p["handFound"],
                                     "decision": "reject", "reason": "Different element.", "model": "sonnet",
                                     "batch": "verify-001", "prompt": "verifier@1", "date": "2026-10-02"}) + "\n")

    def test_publish_rebuild_group_and_publish_again_keeps_the_hand_found_set_and_the_bytes(self):
        ws_dir, ws = self.group_workspace()
        doc = self.group(ws_dir)
        self.complete_ledger(ws, doc)
        pristine = os.path.join(self.tmp.name, "pristine")
        shutil.copytree(ws_dir, pristine)
        self.assertEqual(stage(ws, "diff")[0], 0)
        code, out, err = stage(ws, "publish")
        self.assertEqual(code, 0, out + err)
        first = snapshot_bytes(ws_dir)
        pub = os.path.join(ws_dir, "published")

        # Rebuild: the loader merges the snapshot; the raw read is untouched.
        with contextlib.redirect_stderr(io.StringIO()) as err:
            merged = mapdata.load_areas(DATA, snapshot_dir=pub)
        self.assertEqual(err.getvalue(), "")
        tabs = next(i for a in merged for i in a["issues"] if i["id"] == TABS)
        self.assertEqual(tabs["evidence"].count("Keyboard users cannot reach the tabs."), 1)
        self.assertEqual(sum(1 for a in merged for i in a["issues"] if i["category"] == "a11y"),
                         17 + len(published(ws_dir, "issues.json")["generated"]))

        # group again with the snapshot in force: still the 17 raw issues at their raw severities.
        with mock.patch.dict(os.environ, {"A11Y_SNAPSHOT": pub}):
            again = self.group(ws_dir)
        raw = raw_issues()
        hand = [f for f in again["findings"] if f["source"] == "hand-found"]
        self.assertEqual(sorted(f["issueId"] for f in hand), hand_found_ids())
        for f in hand:
            self.assertEqual((f["severityDefault"], f["summary"]), (raw[f["issueId"]]["severity"], raw[f["issueId"]]["evidence"]))

        # Publish again from the same ledger and the same snapshot in force: the same bytes.
        shutil.copyfile(os.path.join(ws_dir, "runs", GROUP_RUN, "findings.json"),
                        os.path.join(pristine, "runs", GROUP_RUN, "findings.json"))
        ws2 = Workspace(workspace=pristine, data=DATA, map=ws.map_path, run=GROUP_RUN)
        self.assertEqual(stage(ws2, "diff")[0], 0)
        code, out, err = stage(ws2, "publish")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(snapshot_bytes(pristine)["issues.json"], first["issues.json"])
        self.assertEqual(snapshot_bytes(pristine), first)

        # The same publish rerun in place changes nothing.
        code, out, err = stage(ws, "publish")
        self.assertEqual((code, json.loads(out)["state"]), (0, "already published"))
        self.assertEqual(snapshot_bytes(ws_dir), first)

    def test_titles_from_real_facts_never_carry_a_css_class_name(self):
        ws_dir, ws = self.group_workspace()
        doc = self.group(ws_dir)
        self.complete_ledger(ws, doc)
        stage(ws, "diff")
        self.assertEqual(stage(ws, "publish")[0], 0)
        classes = set()
        for name in os.listdir(os.path.join(ws_dir, "runs", GROUP_RUN, "raw")):
            raw = read_json(os.path.join(ws_dir, "runs", GROUP_RUN, "raw", name))
            for r in raw.get("results") or []:
                occ = r.get("occurrence") or {}
                classes.update((occ.get("classes") or []) + [c for anc in occ.get("ancestors") or [] for c in anc])
        module = re.compile(r"_[A-Za-z0-9-]+_[A-Za-z0-9]{5}_[0-9]+")
        # Local names that read as code (camelCase, hyphens): plain words such as "limits" may be in a name.
        local = {m.group(1) for c in classes for m in [re.match(r"^_([A-Za-z0-9-]+)_", c)] if m
                 if re.search(r"[a-z][A-Z]|-", m.group(1))}
        gen = published(ws_dir, "issues.json")["generated"]
        self.assertGreaterEqual(len(gen), 3)
        self.assertTrue(classes)
        for i in gen:
            for title in (i["title"], i["copy"]["title"]):
                self.assertIsNone(module.search(title), title)
                self.assertFalse([c for c in classes | local if c in title], title)
        names = [i for i in gen if i["audit"]["family"] == "name"]
        self.assertEqual(len({i["title"] for i in names}), len(names))

    # Gates -------------------------------------------------------------------------

    def refuses(self, led, why, setup=None):
        """Publish refuses with `why` among its problems and leaves the previous snapshot byte-identical."""
        ws = led.write()
        stage(ws, "diff")
        if setup:
            setup(ws)
        before = snapshot_bytes(self.ws_dir)
        code, out, err = stage(ws, "publish")
        self.assertEqual(code, 1, out)
        self.assertIn(why, err)
        self.assertEqual(json.loads(out)["state"], "refused")
        self.assertEqual(snapshot_bytes(self.ws_dir), before)
        for leftover in (".tmp", ".prev"):
            self.assertFalse(os.path.exists(os.path.join(self.ws_dir, "published" + leftover)))
        return err

    def test_refuses_on_a_coverage_gap(self):
        led = self.second_run()
        led.confirm_rest()
        led.outcomes.pop("home.tasks")
        self.refuses(led, "coverage: missing: home.tasks")

    def test_refuses_on_a_finding_without_a_verdict(self):
        led = self.second_run()
        led.confirm_rest()
        led.verdicts = [v for v in led.verdicts if v["source"] != "scanner" or "contrast" not in v["key"]]
        self.refuses(led, "no verdict for 1 finding(s): contrast:")

    def test_refuses_on_an_undecided_proposal(self):
        led = self.second_run()
        rows = led.scanner("name", "button-name", ["settings.token-menu"], element='button "Token menu"')
        p = led.propose(rows, ROWS)
        led.confirm_rest()
        self.refuses(led, f"undecided attachment proposal(s): {p['id']}")

    def test_refuses_on_a_failed_canary(self):
        led = self.second_run()
        led.confirm_rest()
        led.canary_pass = False
        self.refuses(led, "canary failed")

    def test_refuses_on_a_stale_baseline(self):
        led = self.second_run()
        led.confirm_rest()

        def stale(ws):
            path = led.path("diff.json")
            doc = read_json(path)
            write_json(path, dict(doc, baseline="none"))

        self.refuses(led, f"diff.json compares with run none, but the snapshot in force holds {RUN_A}", stale)

    def test_refuses_when_diff_json_is_older_than_the_verdicts(self):
        led = self.second_run()
        led.confirm_rest()

        def late_verdict(ws):
            contrast = scanner_of(led, "contrast")
            v = dict(led.verdict(contrast, "rerate", after="high", reason="Balance text."))
            with open(led.path("verdicts.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(v) + "\n")

        self.refuses(led, "diff.json is out of date", late_verdict)

    def test_refuses_on_a_title_over_80_characters(self):
        led = self.second_run()
        long = led.scanner("name", "button-name", ["payments.index"], element='button "' + "x" * 60 + '"')
        long["title"] = "Missing or unclear name: " + "x" * 60
        led.confirm_rest()
        self.refuses(led, "title longer than 80 characters")

    def test_refuses_on_a_duplicate_id(self):
        led = self.second_run()
        led.confirm_rest()

        def collide(ws):
            reg_path = os.path.join(self.ws_dir, "published", "registry.json")
            reg = read_json(reg_path)
            contrast = scanner_of(led, "contrast")
            name = scanner_of(led, "name")
            reg["keys"][name["key"]] = reg["keys"][contrast["key"]]
            write_json(reg_path, reg)
            quiet(diff.cli, ws, None, [])

        self.refuses(led, "duplicate issue id cards.i-a11y-contrast-text-in-spend-this-month", collide)

    def test_refuses_on_a_report_link_to_a_missing_id(self):
        led = self.second_run()
        led.confirm_rest()
        real = publish.build_report

        def broken(*args):
            report = real(*args)
            report["priority"].append("cards.i-a11y-not-in-the-map")
            return report

        with mock.patch.object(publish, "build_report", broken):
            self.refuses(led, "report links to cards.i-a11y-not-in-the-map")

    # Swap, crash, idempotence -----------------------------------------------------------

    def test_rerunning_publish_after_it_completed_reports_already_published_and_changes_nothing(self):
        led = self.first_run()
        before = snapshot_bytes(self.ws_dir)
        mtimes = {n: os.path.getmtime(os.path.join(self.ws_dir, "published", n)) for n in before}
        code, out, err = stage(led.workspace(), "publish")
        self.assertEqual((code, json.loads(out)["state"]), (0, "already published"))
        self.assertEqual(snapshot_bytes(self.ws_dir), before)
        self.assertEqual({n: os.path.getmtime(os.path.join(self.ws_dir, "published", n)) for n in before}, mtimes)
        code, out, err = stage(led.workspace(), "diff")
        self.assertIn("already published", out)
        self.assertEqual(publish.status(led.workspace(), RUN_A), {"snapshot": RUN_A, "published": True})
        b = Ledger(self.ws_dir, RUN_B)
        b.hand_found()
        b.confirm_rest()
        ws = b.write()
        self.assertEqual(publish.status(ws, RUN_B), {"pending": True, "snapshot": RUN_A})
        stage(ws, "diff")
        self.assertEqual(publish.status(ws, RUN_B), {"snapshot": RUN_A, "published": False, "baselineCurrent": True})

    def test_a_crash_between_the_two_renames_is_restored_by_status_and_the_loader(self):
        led = self.second_run()
        led.confirm_rest()
        ws = led.write()
        stage(ws, "diff")
        before = snapshot_bytes(self.ws_dir)
        real = os.rename

        def crash(src, dst):
            if src.endswith("published.tmp"):
                raise OSError("power cut")
            return real(src, dst)

        with mock.patch.object(publish.os, "rename", crash):
            with self.assertRaises(OSError):
                quiet(publish.cli, ws, None, [])
        pub = os.path.join(self.ws_dir, "published")
        self.assertFalse(os.path.exists(pub))
        self.assertTrue(os.path.isdir(pub + ".prev"))
        state = status.publish_state(ws, RUN_B)
        self.assertEqual(state, {"state": "publish incomplete, rerun publish", "restored": True})
        self.assertEqual(snapshot_bytes(self.ws_dir), before)

        os.rename(pub, pub + ".prev")
        with contextlib.redirect_stderr(io.StringIO()):
            areas = mapdata.load_areas(DATA, snapshot_dir=pub)
        self.assertTrue(os.path.isdir(pub))
        self.assertIn("cards.i-a11y-contrast-text-in-spend-this-month", {i["id"] for a in areas for i in a["issues"]})

        code, out, err = stage(ws, "publish")
        self.assertEqual((code, json.loads(out)["state"]), (0, "published"), err)
        self.assertEqual(status.publish_state(ws, RUN_B), {"state": "already published", "restored": False})

    # The committed test snapshot -------------------------------------------------------

    def test_the_committed_test_snapshot_is_what_publish_writes_today(self):
        dest = os.path.join(self.tmp.name, "snapshot")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            build_test_snapshot(dest)
        for name in ("issues.json", "report.json", "registry.json", "meta.json"):
            with open(os.path.join(dest, name), "rb") as a, open(os.path.join(SNAPSHOT, name), "rb") as b:
                self.assertEqual(a.read(), b.read(), f"{name} is stale: run `cd tools/a11y && python3 -m tests.ledger`")

    def test_the_committed_test_snapshot_merges_on_real_data_without_warnings(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            areas = mapdata.load_areas(DATA, snapshot_dir=SNAPSHOT)
        self.assertEqual(err.getvalue(), "")
        issues = read_json(os.path.join(SNAPSHOT, "issues.json"))
        merged = {i["id"]: (a["area"], i) for a in areas for i in a["issues"]}
        self.assertTrue(issues["generated"])
        for g in issues["generated"]:
            self.assertEqual(merged[g["id"]][0], g["area"])
            self.assertLessEqual(len(g["title"]), 71, "the export adds 'Resolve: ' until it reads copy")
        self.assertTrue(any(len(g["nodeIds"]) == 30 for g in issues["generated"]))
        reg = read_json(os.path.join(SNAPSHOT, "registry.json"))
        self.assertIn("fixed", {t["reason"] for t in reg["tombstones"].values()})
        self.assertTrue(reg["attachments"])
        self.assertEqual(merged[TABS][1]["severity"], "high")
        self.assertEqual(mapdata.viewer_issue(merged[HEADING][1]), raw_issues()[HEADING])
        self.assertEqual(sum(len(a["issues"]) for a in areas), 236 + len(issues["generated"]))
        viewer = ["id", "title", "severity", "category", "detail", "evidence", "nodeIds"]
        for g in issues["generated"]:
            self.assertEqual(sorted(mapdata.viewer_issue(merged[g["id"]][1])), sorted(viewer))
            self.assertIn("copy", merged[g["id"]][1])

    def test_the_axe_criteria_table_matches_the_installed_axe_core(self):
        script = ("const axe=require('axe-core');const out={};for(const r of axe.getRules())"
                  "out[r.ruleId]=r.tags.filter(t=>/^wcag\\d{3,4}$/.test(t)).map(t=>{const d=t.slice(4);"
                  "return d.length===3?d.split('').join('.'):d[0]+'.'+d[1]+'.'+d.slice(2)});"
                  "console.log(JSON.stringify({version:axe.version,rules:out}))")
        try:
            proc = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            self.skipTest("node is not available")
        if proc.returncode != 0:
            self.skipTest("axe-core is not installed")
        got = json.loads(proc.stdout)
        self.assertEqual(got["version"], "4.13.0")
        checks = read_json(os.path.join(A11Y, "checks.json"))
        self.assertEqual(set(publish.AXE_CRITERIA), set(checks["axeRules"]))
        for rule, criteria in publish.AXE_CRITERIA.items():
            self.assertEqual(sorted(criteria), sorted(got["rules"][rule]), rule)


if __name__ == "__main__":
    unittest.main()
