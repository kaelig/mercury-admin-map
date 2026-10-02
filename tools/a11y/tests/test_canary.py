"""U7: the canary (KTD20 "Calibration first"; AE3, AE7).

Run: cd tools/a11y && python3 -m unittest tests.test_canary -v

The canary definition lives in tools/a11y/fixtures/canary/: five fixture pages, their raw files
(swept once with checks/axe.mjs), a committed findings.json, the hand-found issue in data/, and
expected.json. The run's workspace here is a copy of tests/data/verify/ws, whose base URL stays on
the demo while the canary runs against the fixture server's origin.
"""
import json
import os
import shutil
import tempfile
import unittest

from tests.pathshim import A11Y
from tests import test_verdict as tv

import canary
import signature
import verify
from lib import canon
from lib.ws import Workspace

RUN = tv.RUN
DEF = os.path.join(A11Y, "fixtures", "canary")
FINDINGS = canon.read_json(os.path.join(DEF, "findings.json"))["findings"]
EXPECTED = canon.read_json(os.path.join(DEF, "expected.json"))
CHECKS = canon.read_json(os.path.join(A11Y, "checks.json"))
PORT = "4321"


class Definition(unittest.TestCase):
    def test_every_canary_finding_has_an_expected_verdict(self):
        self.assertEqual(sorted(EXPECTED), sorted(f["key"] for f in FINDINGS))
        self.assertEqual(len(FINDINGS), 6)
        for key, e in EXPECTED.items():
            self.assertRegex(e["expected"], r"^(confirm|drop|rerate:(high|medium|low))$", key)
            self.assertTrue(e["why"], key)

    def test_ae3_an_icon_button_with_a_visible_label_in_the_same_control_rerates_to_low(self):
        (f,) = [f for f in FINDINGS if f["rule"] == "button-name" and f["primary"] == "canary.export"]
        self.assertEqual((f["severityDefault"], EXPECTED[f["key"]]["expected"]), ("medium", "rerate:low"))
        with open(os.path.join(DEF, "export.html"), encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn('<span class="_exportLabel_cnry2_14">Export CSV</span>', html)

    def test_ae7_clickable_div_tabs_rated_medium_rerate_to_high(self):
        (issue,) = canon.read_json(os.path.join(DEF, "data", "canary.json"))["issues"]
        self.assertEqual((issue["title"], issue["severity"], issue["category"]),
                         ("Tabs and inline actions are clickable divs", "medium", "a11y"))
        f = next(f for f in FINDINGS if f["key"] == signature.hand_key(issue["id"]))
        self.assertEqual((f["source"], f["family"], f["severityDefault"], f["states"]),
                         ("hand-found", "keyboard", "medium", issue["nodeIds"]))
        self.assertEqual(EXPECTED[f["key"]]["expected"], "rerate:high")

    def test_seeded_failures_and_the_drop_trap(self):
        got = sorted((f["primary"], f["rule"], EXPECTED[f["key"]]["expected"]) for f in FINDINGS if f["source"] == "scanner")
        self.assertEqual(got, [("canary.activity", "button-name", "confirm"),
                               ("canary.activity", "color-contrast", "confirm"),
                               ("canary.export", "button-name", "rerate:low"),
                               ("canary.hero", "color-contrast", "drop"),
                               ("canary.toolbar", "target-size", "confirm")])

    def test_findings_rest_on_the_committed_raw_results(self):
        for f in FINDINGS:
            if f["source"] != "scanner":
                continue
            for o in f["occurrences"]:
                raw = canon.read_json(os.path.join(DEF, "raw", f"{o['state']}.json"))
                r = raw["results"][o["index"]]
                self.assertEqual(r["rule"], f["rule"], o)
                shape = CHECKS["families"][f["family"]]["key"]
                self.assertEqual(signature.finding_key(f["family"], r["rule"], shape, r["occurrence"], r["evidence"]),
                                 f["key"])

    def test_the_map_names_every_page_and_raw_file(self):
        (area,) = canon.read_json(os.path.join(DEF, "map.json"))["areas"]
        ids = sorted(n["id"] for n in area["nodes"])
        self.assertEqual(ids, sorted(f[:-5] for f in os.listdir(os.path.join(DEF, "raw"))))
        for n in area["nodes"]:
            self.assertTrue(os.path.exists(os.path.join(DEF, n["url"].split("/")[-1])), n["url"])


class CanaryRun(unittest.TestCase):
    def setUp(self):
        self.ws = tv.copy_fixture(self)
        self.run_dir = os.path.join(self.ws, "runs", RUN)

    def prepare(self, *extra):
        code, out, err = tv.run_cli("canary", "prepare", "--workspace", self.ws, "--run", RUN, "--port", PORT, *extra)
        self.assertEqual(code, 0, err)
        out = json.loads(out)
        self.addCleanup(shutil.rmtree, out["root"], True)
        return out

    def answers(self, batch, wrong=()):
        """Verdict lines for the canary batch: the expected outcome for each finding, except `wrong`."""
        lines = []
        for e in batch["findings"]:
            outcome, _, after = EXPECTED[e["key"]]["expected"].partition(":")
            if e["key"] in wrong:
                outcome, after = "confirm", ""
            line = {"key": e["key"], "issueId": e["issueId"], "source": e["source"], "outcome": outcome,
                    "severityBefore": e["severityDefault"], "severityAfter": after or e["severityDefault"],
                    "reason": "Checked on the fixture page.", "reasonCode": None, "counterEvidence": None,
                    "probes": [], "states": e["states"], "build": "unknown", "model": "sonnet",
                    "batch": batch["batch"], "prompt": "verifier@1", "attempt": 1}
            if outcome == "drop":
                line.update(reasonCode="not-reproduced-live", counterEvidence="p-hero", probes=[
                    {"id": "p-hero", "state": "canary.hero", "kind": "contrast", "env": dict(tv.SWEEP_ENV),
                     "value": {"fg": "#ffffff", "bg": "#1d3f8f", "ratio": 9.72}}])
            if e["source"] == "hand-found":
                line["counterEvidence"] = "shot:canary.tabs-tabwalk"
            lines.append(line)
        return lines

    def score(self, out, lines):
        path = tv.write_lines(os.path.join(out["batch"][:-5] + ".out.jsonl"), lines)
        code, text, err = tv.run_cli("canary", "score", "--file", path, "--workspace", self.ws, "--run", RUN)
        return code, json.loads(text), path

    def test_prepare_builds_its_own_workspace_on_the_fixture_origin(self):
        out = self.prepare()
        cws = out["workspace"]
        self.assertTrue(os.path.realpath(cws).startswith(os.path.realpath(tempfile.gettempdir())), cws)
        self.assertFalse(os.path.realpath(cws).startswith(os.path.realpath(self.ws)))
        origin = f"http://127.0.0.1:{PORT}"
        self.assertEqual(out["baseUrl"], origin)
        self.assertEqual(out["server"], f"node tools/a11y/fixtures/server.mjs --port {PORT} canary")
        policy = canon.read_json(os.path.join(cws, "policy.json"))
        self.assertEqual((policy["origin"], policy["allowedOrigins"]), (origin, [origin]))
        states = canon.read_json(os.path.join(cws, "runs", RUN, "states.json"))
        self.assertEqual(sorted(states["roots"]),
                         ["canary.activity", "canary.export", "canary.hero", "canary.tabs", "canary.toolbar"])
        self.assertEqual(states["states"]["canary.tabs"]["rootUrl"], "/canary/tabs.html")
        self.assertEqual(sorted(os.listdir(os.path.join(cws, "runs", RUN, "raw"))),
                         sorted(os.listdir(os.path.join(DEF, "raw"))))
        # The run's own workspace still points at the demo and receives nothing.
        self.assertEqual(Workspace(workspace=self.ws).base_url, "https://demo.mercury.com")
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "canary.json")))

    def test_the_canary_batch_travels_as_an_ordinary_verify_batch(self):
        out = self.prepare()
        batch = canon.read_json(out["batch"])
        self.assertEqual(os.path.basename(out["batch"]), "canary-001.json")
        self.assertEqual((batch["batch"], batch["run"], batch["area"]), ("canary-001", RUN, "canary"))
        self.assertEqual(batch["reach"], {"workspace": out["workspace"], "data": "tools/a11y/fixtures/canary/data",
                                          "map": "tools/a11y/fixtures/canary/map.json",
                                          "baseUrl": f"http://127.0.0.1:{PORT}", "run": RUN})
        self.assertEqual(sorted(e["key"] for e in batch["findings"]), sorted(EXPECTED))
        ws = tv.copy_fixture(self)
        tv.run_cli("verify", "prepare", "--workspace", ws, "--run", RUN)
        ordinary = canon.read_json(os.path.join(ws, "runs", RUN, "batches", "verify-001.json"))
        self.assertEqual(set(batch), set(ordinary))
        for e in batch["findings"]:
            self.assertEqual(set(e), set(ordinary["findings"][0]), e["key"])
        hero = next(e for e in batch["findings"] if e["primary"] == "canary.hero")
        self.assertEqual((hero["basis"], hero["measured"], hero["evidence"][0]["evidence"]["messageKey"]),
                         ("incomplete", True, "bgGradient"))

    def test_correct_verdicts_pass_and_land_only_in_canary_json(self):
        out = self.prepare()
        code, text, _ = self.score(out, self.answers(canon.read_json(out["batch"])))
        self.assertEqual(code, 0, text)
        doc = canon.read_json(os.path.join(self.run_dir, "canary.json"))
        self.assertEqual({k: doc[k] for k in ("prompt", "batch", "expected", "correct", "pass")},
                         {"prompt": "verifier@1", "batch": "canary-001", "expected": 6, "correct": 6, "pass": True})
        self.assertEqual(doc["items"], [{"key": k, "expected": EXPECTED[k]["expected"],
                                         "got": EXPECTED[k]["expected"], "ok": True} for k in sorted(EXPECTED)])
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "verdicts.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(out["workspace"], "runs", RUN, "verdicts.jsonl")))
        st = verify.status(Workspace(workspace=self.ws, run=RUN), RUN)
        self.assertEqual((st["canary"]["status"], st["holdWaves"]), ("passed", False))

    def test_a_wrong_canary_verdict_fails_calibration_and_holds_the_waves(self):
        out = self.prepare()
        ae3 = next(k for k, e in EXPECTED.items() if e["expected"] == "rerate:low")
        code, text, _ = self.score(out, self.answers(canon.read_json(out["batch"]), wrong={ae3}))
        self.assertEqual(code, 1)
        self.assertEqual(text["wrong"], [ae3])
        doc = canon.read_json(os.path.join(self.run_dir, "canary.json"))
        self.assertEqual((doc["correct"], doc["pass"]), (5, False))
        item = next(i for i in doc["items"] if i["key"] == ae3)
        self.assertEqual(item, {"key": ae3, "expected": "rerate:low", "got": "confirm", "ok": False})
        st = verify.status(Workspace(workspace=self.ws, run=RUN), RUN)
        self.assertEqual(st["canary"]["status"], "failed")
        self.assertIn("calibration failure", st["canary"]["message"])
        self.assertTrue(st["holdWaves"])

    def test_a_missing_or_malformed_canary_verdict_fails(self):
        out = self.prepare()
        lines = self.answers(canon.read_json(out["batch"]))
        ae7 = "hand:canary.i-tabs-clickable-divs"
        tabs = next(x for x in lines if x["key"] == ae7)
        tabs["counterEvidence"] = None                                   # a hand-found verdict must cite evidence
        code, text, _ = self.score(out, lines)
        self.assertEqual(code, 1)
        self.assertEqual([r["line"] for r in text["rejected"]], [lines.index(tabs) + 1])
        doc = canon.read_json(os.path.join(self.run_dir, "canary.json"))
        self.assertEqual(next(i for i in doc["items"] if i["key"] == ae7)["got"], None)
        self.assertFalse(doc["pass"])

    def test_verdict_add_refuses_canary_verdicts_for_the_run(self):
        out = self.prepare()
        _, _, path = self.score(out, self.answers(canon.read_json(out["batch"])))
        code, text, err = tv.run_cli("verdict", "add", "--file", path, "--workspace", self.ws, "--run", RUN)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)["appended"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "verdicts.jsonl")))

    def test_an_explicit_out_dir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = self.prepare("--out", tmp.name)
        self.assertEqual(out["root"], tmp.name)
        self.assertTrue(os.path.exists(os.path.join(tmp.name, "ws", "runs", RUN, "batches", "canary-001.json")))

    def test_status(self):
        ws = Workspace(workspace=self.ws, run=RUN)
        self.assertEqual(canary.status(ws, RUN), {"pending": True})
        out = self.prepare()
        self.score(out, self.answers(canon.read_json(out["batch"])))
        self.assertEqual(canary.status(ws, RUN), {"pass": True, "correct": 6, "expected": 6, "prompt": "verifier@1"})


if __name__ == "__main__":
    unittest.main()
