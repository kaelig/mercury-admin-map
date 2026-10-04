"""`calibration add` and `calibration.due` (R3, KTD25).

Run: cd tools/a11y && python3 -m unittest tests.test_calibration -v

A workspace built in code: a root page, a modal opened over it, and a second root. The conductor's
comparison file lists the axe rule ids Lighthouse failed on each state.
"""
import json
import os
import re
import tempfile
import unittest

from tests import test_verdict as tv

import calibration
from lib import canon
from lib.ws import Workspace

RUN = tv.RUN
PREVIOUS = "2026-09-20-1000"


def result(rule, basis="violation"):
    return {"check": "axe", "rule": rule, "basis": basis, "criteria": [], "messageKey": None,
            "occurrence": {}, "evidence": {}}


class CalibrationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = os.path.join(tmp.name, "ws")
        self.run_dir = os.path.join(self.ws, "runs", RUN)
        states = {"cards.index": {"area": "cards", "kind": "page", "root": True, "parent": None},
                  "cards.edit-limit-modal": {"area": "cards", "kind": "modal", "root": False,
                                             "parent": "cards.index"},
                  "home.index": {"area": "home", "kind": "page", "root": True, "parent": None}}
        canon.write_json(os.path.join(self.run_dir, "states.json"), {"run": RUN, "states": states})
        self.raw("cards.index", [result("color-contrast"), result("landmark-one-main"),
                                 result("target-size", "incomplete"), result("tabwalk-rule")])
        self.raw("cards.edit-limit-modal", [result("color-contrast"), result("aria-dialog-name")])
        self.raw("home.index", [result("button-name")])
        self.run_doc(RUN, axe="4.13.0", chromium="151.0.7000.0")

    def raw(self, sid, results):
        results = [dict(r, check="tabwalk") if r["rule"] == "tabwalk-rule" else r for r in results]
        canon.write_json(os.path.join(self.run_dir, "raw", f"{sid}.json"), {
            "schema": 1, "state": sid, "status": "audited", "results": results,
            "env": {"axe": "4.13.0", "chromium": "151.0.7000.0", "viewport": [1440, 900], "dpr": 1}})

    def run_doc(self, run, axe, chromium):
        canon.write_json(os.path.join(self.ws, "runs", run, "run.json"),
                         {"run": run, "versions": {"axe": axe, "chromium": chromium}})

    def add(self, comparison):
        path = os.path.join(self.ws, "comparison.json")
        canon.write_json(path, comparison)
        code, out, err = tv.run_cli("calibration", "add", "--file", path, "--workspace", self.ws, "--run", RUN)
        return code, json.loads(out) if out.strip() else None, err

    def written(self):
        return canon.read_json(os.path.join(self.run_dir, "lighthouse.json"))

    def comparison(self, **over):
        doc = {"date": "2026-10-02", "chrome": "142.0.7444.59", "lighthouseAxe": "4.11.0",
               "states": [{"state": "cards.index", "kind": "root",
                           "lighthouseFailed": ["color-contrast", "landmark-one-main", "target-size"]}],
               "explanations": {}}
        doc.update(over)
        return doc


class Add(CalibrationTest):
    def test_a_root_that_matches_has_no_differences(self):
        code, out, err = self.add(self.comparison())
        self.assertEqual(code, 0, err)
        self.assertEqual(self.written(), {
            "date": "2026-10-02", "chrome": "142.0.7444.59", "lighthouseAxe": "4.11.0", "sweepAxe": "4.13.0",
            "states": [{"state": "cards.index", "kind": "root",
                        "lighthouseFailed": ["color-contrast", "landmark-one-main", "target-size"],
                        "sweepRules": ["color-contrast", "landmark-one-main", "target-size"], "differences": []}],
            "unexplained": 0})
        self.assertEqual(out["unexplained"], 0)

    def test_a_parent_page_failure_on_an_overlay_is_explained_by_scope(self):
        code, out, err = self.add(self.comparison(states=[
            {"state": "cards.edit-limit-modal", "kind": "overlay",
             "lighthouseFailed": ["aria-dialog-name", "color-contrast", "landmark-one-main"]}]))
        self.assertEqual(code, 0, err)
        (st,) = self.written()["states"]
        self.assertEqual(st["sweepRules"], ["aria-dialog-name", "color-contrast"])
        self.assertEqual(st["differences"], [{"rule": "landmark-one-main", "side": "lighthouse", "explained": True,
                                              "explanation": "scope"}])
        self.assertEqual(self.written()["unexplained"], 0)

    def test_any_other_difference_is_unexplained_and_counted(self):
        code, out, err = self.add(self.comparison(states=[
            {"state": "cards.edit-limit-modal", "kind": "overlay",
             "lighthouseFailed": ["color-contrast", "heading-order"]},
            {"state": "home.index", "kind": "root", "lighthouseFailed": ["image-alt"]}]))
        self.assertEqual(code, 1)
        modal, home = self.written()["states"]
        self.assertEqual(modal["differences"], [
            {"rule": "aria-dialog-name", "side": "sweep", "explained": False, "explanation": None},
            {"rule": "heading-order", "side": "lighthouse", "explained": False, "explanation": None}])
        self.assertEqual(home["differences"], [
            {"rule": "button-name", "side": "sweep", "explained": False, "explanation": None},
            {"rule": "image-alt", "side": "lighthouse", "explained": False, "explanation": None}])
        self.assertEqual((self.written()["unexplained"], out["unexplained"]), (4, 4))
        self.assertIn("harness", err)

    def test_the_conductor_explains_rule_selection_and_version(self):
        code, out, err = self.add(self.comparison(
            states=[{"state": "cards.index", "kind": "root", "lighthouseFailed": ["color-contrast", "image-alt"]}],
            explanations={"landmark-one-main": "rule-selection", "target-size": "rule-selection",
                          "image-alt": "version"}))
        self.assertEqual(code, 0, err)
        (st,) = self.written()["states"]
        self.assertEqual(st["differences"], [
            {"rule": "image-alt", "side": "lighthouse", "explained": True, "explanation": "version"},
            {"rule": "landmark-one-main", "side": "sweep", "explained": True, "explanation": "rule-selection"},
            {"rule": "target-size", "side": "sweep", "explained": True, "explanation": "incomplete"}])

    def test_the_conductor_explains_a_reduced_motion_environment_difference(self):
        code, out, err = self.add(self.comparison(
            states=[{"state": "cards.index", "kind": "root", "lighthouseFailed": ["color-contrast"]}],
            explanations={"landmark-one-main": "environment", "target-size": "rule-selection"}))
        self.assertEqual(code, 0, err)
        (st,) = self.written()["states"]
        self.assertEqual(st["differences"], [
            {"rule": "landmark-one-main", "side": "sweep", "explained": True, "explanation": "environment"},
            {"rule": "target-size", "side": "sweep", "explained": True, "explanation": "incomplete"}])

    def test_a_rule_the_sweep_has_only_as_incomplete_is_explained_by_incomplete(self):
        import calibration
        raw = {"results": [{"check": "axe", "rule": "aria-valid-attr-value", "basis": "incomplete"},
                           {"check": "axe", "rule": "color-contrast", "basis": "incomplete"},
                           {"check": "axe", "rule": "color-contrast", "basis": "violation"},
                           {"check": "tabwalk", "rule": "backward-jump", "basis": "candidate"}]}
        self.assertEqual(calibration.incomplete_only(raw), {"aria-valid-attr-value"})
        diffs = calibration.compare([], calibration.sweep_rules(raw), [], False, {}, calibration.incomplete_only(raw))
        self.assertEqual(diffs, [
            {"rule": "aria-valid-attr-value", "side": "sweep", "explained": True, "explanation": "incomplete"},
            {"rule": "color-contrast", "side": "sweep", "explained": False, "explanation": None}])

    def test_the_sweep_side_counts_only_axe_violations_and_incomplete_results(self):
        self.add(self.comparison())
        self.assertNotIn("tabwalk-rule", self.written()["states"][0]["sweepRules"])

    def test_an_invalid_comparison_writes_nothing(self):
        for bad, pattern in (
                (self.comparison(chrome=""), "chrome"),
                (self.comparison(lighthouseAxe=None), "lighthouseAxe"),
                (self.comparison(states=[]), "states"),
                (self.comparison(states=[{"state": "cards.gone", "kind": "root", "lighthouseFailed": []}]),
                 "cards.gone"),
                (self.comparison(states=[{"state": "cards.index", "kind": "overlay", "lighthouseFailed": []}]),
                 "parent"),
                (self.comparison(states=[{"state": "cards.index", "kind": "page", "lighthouseFailed": []}]), "kind"),
                (self.comparison(explanations={"landmark-one-main": "scope"}), "scope")):
            with self.subTest(pattern=pattern):
                code, out, err = self.add(bad)
                self.assertEqual(code, 2)
                self.assertTrue(any(re.search(pattern, r) for r in out["rejected"]), out)
                self.assertIsNone(self.written())


class Due(CalibrationTest):
    def ws_obj(self):
        return Workspace(workspace=self.ws, run=RUN)

    def publish(self, axe="4.13.0", chromium="151.0.7000.0", lighthouse=True):
        meta = {"run": PREVIOUS, "date": "2026-09-20", "versions": {"axe": axe, "chromium": chromium}}
        if lighthouse:
            meta["lighthouse"] = {"run": PREVIOUS, "date": "2026-09-20", "result": {}}
        canon.write_json(os.path.join(self.ws, "published", "meta.json"), meta)
        self.run_doc(PREVIOUS, axe=axe, chromium=chromium)

    def test_the_first_run_is_due(self):
        self.assertTrue(calibration.due(self.ws_obj(), RUN))

    def test_a_snapshot_without_a_calibration_is_due(self):
        self.publish(lighthouse=False)
        self.assertTrue(calibration.due(self.ws_obj(), RUN))

    def test_the_same_versions_are_not_due(self):
        self.publish()
        self.assertFalse(calibration.due(self.ws_obj(), RUN))

    def test_a_new_axe_or_chromium_is_due(self):
        self.publish(axe="4.12.0")
        self.assertTrue(calibration.due(self.ws_obj(), RUN))
        self.publish(chromium="150.0.1")
        self.assertTrue(calibration.due(self.ws_obj(), RUN))

    def test_the_chromium_version_falls_back_to_the_raw_files(self):
        self.publish()
        self.run_doc(RUN, axe="4.13.0", chromium=None)
        self.assertFalse(calibration.due(self.ws_obj(), RUN))

    def test_status(self):
        self.assertEqual(calibration.status(self.ws_obj(), RUN), {"pending": True, "due": True})
        self.add(self.comparison())
        self.assertEqual(calibration.status(self.ws_obj(), RUN), {"due": True, "done": True, "states": 1,
                                                                  "unexplained": 0})


if __name__ == "__main__":
    unittest.main()
