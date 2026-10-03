"""`diff`, each finding's status against the snapshot in force (R21, R26; KTD18).

Run: cd tools/a11y && python3 -m unittest tests.test_diff -v

Each test writes a synthetic ledger (tests/ledger.py) on real data/ node ids into a temp workspace,
plus, for later runs, a baseline snapshot in the format `publish` writes (schemas.md "published/").
"""
import contextlib
import io
import json
import os
import tempfile
import unittest

import tests.pathshim  # noqa: F401  (puts tools/a11y and tools on sys.path)
from tests.ledger import DATA, Ledger, RUN_A, RUN_B

import diff
from lib.canon import read_json, write_json
from lib.ws import Workspace

TABS = "accounts.i-tabs-not-tabs"
HEADING = "cards.i-modal-heading-semantics"
ROWS = "settings.i-unnamed-row-buttons"
CHIPS = "home.i-chips-as-menuitems"


def snapshot(ws_dir, run=RUN_A, generated=(), keys=None, tombstones=None, attachments=(), attached_rows=None,
             withdrawn_rows=()):
    """A baseline published/ in the format publish writes: what diff reads from it."""
    pub = os.path.join(ws_dir, "published")
    issues = []
    for key, iid, nodes, severity in generated:
        issues.append({"id": iid, "area": iid.split(".")[0], "title": f"Title {iid}", "severity": severity,
                       "category": "a11y", "detail": "Detail.", "evidence": "Evidence.", "nodeIds": list(nodes),
                       "wcag": ["1.4.3"], "copy": {}, "audit": {"family": key.split(":")[0], "run": run}})
    reg_keys = {key: iid for key, iid, _, _ in generated}
    reg_keys.update(keys or {})
    write_json(os.path.join(pub, "issues.json"), {"generated": issues, "overlays": {}})
    write_json(os.path.join(pub, "registry.json"), {
        "keys": reg_keys, "held": sorted(set(reg_keys.values())), "tombstones": tombstones or {},
        "attachments": [{"finding": k, "handFound": h, "run": run} for k, h in attachments]})
    hand_rows = [{"id": h, "status": "confirmed", "attached": [
        {"key": k, "formerId": reg_keys.get(k), "nodeIds": nodes, "severity": sev, "status": "persisting"}
        for k, nodes, sev in rows]} for h, rows in (attached_rows or {}).items()]
    write_json(os.path.join(pub, "report.json"), {
        "run": run, "baseline": "none", "handFound": hand_rows,
        "withdrawn": [{"id": None, "formerId": reg_keys.get(k), "key": k, "nodeIds": nodes, "severity": sev,
                       "screens": len(nodes)} for k, nodes, sev in withdrawn_rows],
        "fixed": [], "notRetested": [], "findings": []})
    write_json(os.path.join(pub, "meta.json"), {"run": run, "date": run[:10]})


def run_diff(ws, run):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = diff.cli(ws, None, [])
    return code, out.getvalue(), err.getvalue()


class DiffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws_dir = os.path.join(self.tmp.name, "ws")

    def ledger(self, run=RUN_B, data_dir=DATA):
        return Ledger(self.ws_dir, run, data_dir=data_dir)

    def compute(self, led):
        ws = led.write()
        doc = diff.compute(ws, led.run)[0]
        return doc

    # First run ------------------------------------------------------------------

    def test_first_run_every_scanner_finding_is_new_and_hand_found_issues_are_first_audited(self):
        led = self.ledger(RUN_A)
        hand = led.hand_found()
        a = led.scanner("contrast", "color-contrast", ["cards.index", "cards.detail-physical"], primary="cards.index")
        b = led.scanner("name", "button-name", ["settings.tokens"])
        led.confirm_rest()
        doc = self.compute(led)
        self.assertEqual(doc["run"], RUN_A)
        self.assertEqual(doc["baseline"], "none")
        self.assertEqual({k: e["status"] for k, e in doc["findings"].items()}, {a["key"]: "new", b["key"]: "new"})
        self.assertEqual(doc["findings"][a["key"]]["nodeIds"], ["cards.index", "cards.detail-physical"])
        self.assertIsNone(doc["findings"][a["key"]]["issueId"])
        self.assertIsNone(doc["findings"][a["key"]]["severityBefore"])
        self.assertEqual(doc["findings"][a["key"]]["severityAfter"], "medium")
        self.assertEqual(len(doc["handFound"]), 17)
        self.assertEqual({e["status"] for e in doc["handFound"].values()}, {"first-audited"})
        self.assertEqual(doc["handFound"][TABS], {"status": "first-audited", "reason": None,
                                                  "severityBefore": "medium", "severityAfter": "medium"})
        self.assertEqual(set(doc["handFound"]), set(hand))

    def test_hand_found_statuses_follow_the_verdict_and_the_drop_code(self):
        led = self.ledger(RUN_A)
        hand = led.hand_found()
        led.verdict(hand[TABS], "rerate", after="high", reason="Keyboard users cannot switch tabs.")
        led.verdict(hand[HEADING], "drop", code="tool-false-positive")
        led.verdict(hand[ROWS], "drop", code="fixed-since-capture")
        led.verdict(hand[CHIPS], "drop", code="exception:inline")
        led.verdict(hand["billpay.i-manage-link"], "drop", code="not-aa-failure")
        led.confirm_rest()
        got = self.compute(led)["handFound"]
        self.assertEqual(got[TABS], {"status": "re-rated", "reason": None, "severityBefore": "medium",
                                     "severityAfter": "high"})
        self.assertEqual(got[HEADING], {"status": "not-reproduced", "reason": "tool false positive",
                                        "severityBefore": "low", "severityAfter": "low"})
        self.assertEqual(got[ROWS]["reason"], "fixed since capture")
        self.assertEqual(got[CHIPS]["reason"], "not an AA failure")
        self.assertEqual(got["billpay.i-manage-link"]["reason"], "not an AA failure")

    def test_a_hand_found_issue_whose_screens_are_unreachable_is_not_retested_never_not_reproduced(self):
        led = self.ledger(RUN_A)
        led.state("settings.tokens", outcome="unreachable", reason="step-failed")
        led.state("settings.token-menu", outcome="unreachable", reason="blocked-by-parent", root_cause="settings.tokens")
        led.state("cards.edit-limit-modal", kind="modal", outcome="unreachable")
        hand = led.hand_found()
        led.verdict(hand[ROWS], "not-reached", reason="The API Tokens page did not load.")
        led.verdict(hand[HEADING], "drop", code="not-reproduced-live")
        led.confirm_rest()
        got = self.compute(led)["handFound"]
        self.assertEqual(got[ROWS]["status"], "not-retested")
        self.assertEqual(got[HEADING]["status"], "not-retested")
        self.assertIsNone(got[HEADING]["reason"])
        self.assertEqual(got[TABS]["status"], "first-audited")

    def test_a_hand_found_drop_where_its_family_was_incomplete_is_not_retested(self):
        led = self.ledger(RUN_A)
        led.state("accounts.index", incomplete=["tabwalk"])
        hand = led.hand_found()
        led.verdict(hand[TABS], "drop", code="not-reproduced-live")
        led.confirm_rest()
        self.assertEqual(self.compute(led)["handFound"][TABS]["status"], "not-retested")

    # Later runs ----------------------------------------------------------------

    def test_ae6_a_key_absent_this_run_with_its_family_completed_on_every_prior_screen_is_fixed(self):
        led = self.ledger()
        led.hand_found()
        led.ensure(["home.index", "home.tasks"])
        led.confirm_rest()
        key = "target-size:target-size-own:aaaaaaaaaaaa"
        snapshot(self.ws_dir, generated=[(key, "home.i-a11y-target-size-button-dismiss", ["home.index", "home.tasks"], "low")])
        e = self.compute(led)["findings"][key]
        self.assertEqual(e["status"], "fixed")
        self.assertEqual(e["issueId"], "home.i-a11y-target-size-button-dismiss")
        self.assertEqual(e["nodeIds"], ["home.index", "home.tasks"])
        self.assertEqual(e["severityBefore"], "low")
        self.assertIsNone(e["reason"])

    def test_a_prior_screen_unreachable_this_run_keeps_a_confirmed_finding_not_retested_with_the_union(self):
        led = self.ledger()
        led.hand_found()
        led.state("cards.detail-physical", outcome="unreachable")
        f = led.scanner("contrast", "color-contrast", ["cards.index", "cards.subscriptions"], primary="cards.index")
        led.confirm_rest()
        snapshot(self.ws_dir, generated=[(f["key"], "cards.i-a11y-contrast-text", ["cards.index", "cards.detail-physical"], "medium")])
        e = self.compute(led)["findings"][f["key"]]
        self.assertEqual(e["status"], "not-retested")
        self.assertEqual(e["nodeIds"], ["cards.index", "cards.detail-physical", "cards.subscriptions"])
        self.assertEqual(e["issueId"], "cards.i-a11y-contrast-text")
        self.assertIn("unreachable: cards.detail-physical", e["reason"])

    def test_a_prior_screen_retested_clean_leaves_the_list_and_the_finding_persists(self):
        led = self.ledger()
        led.hand_found()
        led.ensure(["cards.detail-physical"])
        f = led.scanner("contrast", "color-contrast", ["cards.index"])
        led.confirm_rest()
        snapshot(self.ws_dir, generated=[(f["key"], "cards.i-a11y-contrast-text", ["cards.index", "cards.detail-physical"], "medium")])
        e = self.compute(led)["findings"][f["key"]]
        self.assertEqual(e["status"], "persisting")
        self.assertEqual(e["nodeIds"], ["cards.index"])
        self.assertEqual((e["severityBefore"], e["severityAfter"]), ("medium", "medium"))

    def test_a_toast_whose_status_check_was_incomplete_leaves_its_4_1_3_finding_not_retested(self):
        led = self.ledger()
        led.hand_found()
        led.state("settings.company-name-updated", kind="toast", incomplete=["status"])
        led.confirm_rest()
        key = "status-message:status-not-announced:bbbbbbbbbbbb"
        snapshot(self.ws_dir, generated=[(key, "settings.i-a11y-status-message-saved", ["settings.company-name-updated"], "medium")])
        e = self.compute(led)["findings"][key]
        self.assertEqual(e["status"], "not-retested")
        self.assertEqual(e["nodeIds"], ["settings.company-name-updated"])
        self.assertEqual(e["severityAfter"], "medium")
        self.assertIn("check incomplete: settings.company-name-updated", e["reason"])

    def test_the_same_toast_with_the_status_check_completed_reads_as_fixed(self):
        led = self.ledger()
        led.hand_found()
        led.state("settings.company-name-updated", kind="toast")
        led.confirm_rest()
        key = "status-message:status-not-announced:bbbbbbbbbbbb"
        snapshot(self.ws_dir, generated=[(key, "settings.i-a11y-status-message-saved", ["settings.company-name-updated"], "medium")])
        self.assertEqual(self.compute(led)["findings"][key]["status"], "fixed")

    def test_an_unreachable_prior_screen_never_reads_as_fixed(self):
        led = self.ledger()
        led.hand_found()
        led.state("home.index")
        led.state("home.tasks", outcome="unreachable")
        led.confirm_rest()
        key = "target-size:target-size-own:aaaaaaaaaaaa"
        snapshot(self.ws_dir, generated=[(key, "home.i-a11y-target", ["home.index", "home.tasks"], "low")])
        e = self.compute(led)["findings"][key]
        self.assertEqual(e["status"], "not-retested")
        self.assertEqual(e["nodeIds"], ["home.tasks"])

    def test_a_published_announced_as_finding_whose_record_is_still_present_is_not_fixed_by_a_judge_ok(self):
        import signature
        sig = "rec:0123456789ab"
        key = signature.record_finding_key("name", sig)
        led = self.ledger()
        led.hand_found()
        led.ensure(["settings.tokens"])
        led.record(sig, ["settings.tokens"])      # still on the page; the judge said ok, so group made no finding
        led.confirm_rest()
        snapshot(self.ws_dir, generated=[(key, "settings.i-a11y-name-button", ["settings.tokens"], "medium")])
        e = self.compute(led)["findings"][key]
        self.assertEqual(e["status"], "not-retested")
        self.assertIn("record still present", e["reason"])

    def test_the_carried_record_finding_goes_to_verification_and_persists(self):
        import signature
        sig = "rec:0123456789ab"
        led = self.ledger()
        led.hand_found()
        f = led.scanner("name", "announced-as", ["settings.tokens"], sig=sig)
        led.record(sig, ["settings.tokens"])
        led.confirm_rest()
        snapshot(self.ws_dir, generated=[(f["key"], "settings.i-a11y-name-button", ["settings.tokens"], "medium")])
        self.assertEqual(f["key"], signature.record_finding_key("name", sig))
        self.assertEqual(self.compute(led)["findings"][f["key"]]["status"], "persisting")

    def test_a_verifier_drop_of_a_published_finding_is_withdrawn(self):
        led = self.ledger()
        led.hand_found()
        f = led.scanner("name", "button-name", ["cards.index"])
        led.verdict(f, "drop", code="exception:equivalent-control")
        led.confirm_rest()
        snapshot(self.ws_dir, generated=[(f["key"], "cards.i-a11y-name-button-more", ["cards.index"], "medium")])
        e = self.compute(led)["findings"][f["key"]]
        self.assertEqual(e["status"], "withdrawn")
        self.assertEqual(e["issueId"], "cards.i-a11y-name-button-more")
        self.assertEqual(e["reason"], "exception:equivalent-control")

    def test_a_withdrawn_finding_confirmed_again_is_persisting(self):
        led = self.ledger()
        led.hand_found()
        f = led.scanner("name", "button-name", ["cards.index"])
        led.confirm_rest()
        iid = "cards.i-a11y-name-button-more"
        snapshot(self.ws_dir, keys={f["key"]: iid},
                 tombstones={iid: {"reason": "withdrawn", "attachedTo": None, "run": RUN_A, "date": "2026-10-01"}},
                 withdrawn_rows=[(f["key"], ["cards.index"], "medium")])
        e = self.compute(led)["findings"][f["key"]]
        self.assertEqual(e["status"], "persisting")
        self.assertEqual(e["issueId"], iid)

    def test_a_withdrawn_finding_absent_with_its_family_completed_is_fixed(self):
        led = self.ledger()
        led.hand_found()
        led.ensure(["cards.index"])
        led.confirm_rest()
        key, iid = "name:button-name:cccccccccccc", "cards.i-a11y-name-button-more"
        snapshot(self.ws_dir, keys={key: iid},
                 tombstones={iid: {"reason": "withdrawn", "attachedTo": None, "run": RUN_A, "date": "2026-10-01"}},
                 withdrawn_rows=[(key, ["cards.index"], "medium")])
        self.assertEqual(self.compute(led)["findings"][key]["status"], "fixed")

    def test_a_withdrawn_finding_the_baseline_report_holds_no_screens_for_stays_withdrawn(self):
        led = self.ledger()
        led.hand_found()
        led.confirm_rest()
        key, iid = "name:button-name:eeeeeeeeeeee", "cards.i-a11y-name-button-gone"
        snapshot(self.ws_dir, keys={key: iid},
                 tombstones={iid: {"reason": "withdrawn", "attachedTo": None, "run": RUN_A, "date": "2026-10-01"}})
        self.assertNotIn(key, self.compute(led)["findings"])

    def test_a_tombstoned_fixed_key_confirmed_again_is_reappeared_with_the_same_id(self):
        led = self.ledger()
        led.hand_found()
        f = led.scanner("target-size", "target-size-own", ["home.index"])
        led.confirm_rest()
        iid = "home.i-a11y-target-size-button-more"
        snapshot(self.ws_dir, keys={f["key"]: iid},
                 tombstones={iid: {"reason": "fixed", "attachedTo": None, "run": RUN_A, "date": "2026-10-01"}})
        e = self.compute(led)["findings"][f["key"]]
        self.assertEqual(e["status"], "reappeared")
        self.assertEqual(e["issueId"], iid)

    def test_a_severity_change_is_re_rated_with_both_values(self):
        led = self.ledger()
        led.hand_found()
        f = led.scanner("contrast", "color-contrast", ["cards.index"])
        led.verdict(f, "rerate", after="high", reason="The text is the card balance; low-vision users cannot read it.")
        led.confirm_rest()
        snapshot(self.ws_dir, generated=[(f["key"], "cards.i-a11y-contrast", ["cards.index"], "medium")])
        e = self.compute(led)["findings"][f["key"]]
        self.assertEqual((e["status"], e["severityBefore"], e["severityAfter"]), ("re-rated", "medium", "high"))

    def test_a_published_finding_the_verifier_could_not_reach_is_not_retested_at_its_published_severity(self):
        led = self.ledger()
        led.hand_found()
        f = led.scanner("contrast", "color-contrast", ["cards.index"])
        led.verdict(f, "not-reached", reason="The cards page did not load.")
        led.confirm_rest()
        snapshot(self.ws_dir, generated=[(f["key"], "cards.i-a11y-contrast", ["cards.index"], "high")])
        e = self.compute(led)["findings"][f["key"]]
        self.assertEqual((e["status"], e["severityAfter"]), ("not-retested", "high"))
        self.assertIn("could not reach", e["reason"])

    def test_a_new_finding_left_unverified_is_not_retested_without_an_id(self):
        led = self.ledger(RUN_A)
        led.hand_found()
        f = led.scanner("contrast", "color-contrast", ["cards.index"])
        led.verdict(f, "unverified", reason="No valid verdict after 2 attempts.")
        led.confirm_rest()
        e = self.compute(led)["findings"][f["key"]]
        self.assertEqual((e["status"], e["issueId"]), ("not-retested", None))

    def test_an_attached_finding_keeps_its_prior_screens_from_the_report(self):
        led = self.ledger()
        led.hand_found()
        led.state("settings.token-menu", kind="menu", outcome="unreachable")
        f = led.scanner("name", "button-name", ["settings.tokens"])
        led.confirm_rest()
        snapshot(self.ws_dir, attachments=[(f["key"], ROWS)],
                 attached_rows={ROWS: [(f["key"], ["settings.tokens", "settings.token-menu"], "medium")]})
        e = self.compute(led)["findings"][f["key"]]
        self.assertEqual(e["status"], "not-retested")
        self.assertEqual(e["nodeIds"], ["settings.tokens", "settings.token-menu"])

    # Deleted screens -----------------------------------------------------------

    def data_without(self, *node_ids):
        d = os.path.join(self.tmp.name, "data")
        os.makedirs(d)
        for name in sorted(os.listdir(DATA)):
            if not name.endswith(".json"):
                continue
            area = read_json(os.path.join(DATA, name))
            area["nodes"] = [n for n in area["nodes"] if n["id"] not in node_ids]
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                json.dump(area, f, ensure_ascii=False)
        return d

    def test_a_prior_screen_deleted_from_data_drops_out(self):
        data = self.data_without("cards.spend-details")
        led = self.ledger(data_dir=data)
        led.hand_found()
        f = led.scanner("contrast", "color-contrast", ["cards.index"])
        led.confirm_rest()
        snapshot(self.ws_dir, generated=[(f["key"], "cards.i-a11y-contrast", ["cards.index", "cards.spend-details"], "medium")])
        e = self.compute(led)["findings"][f["key"]]
        self.assertEqual((e["status"], e["nodeIds"]), ("persisting", ["cards.index"]))

    def test_a_finding_whose_prior_screens_are_all_deleted_is_fixed_with_the_reason_screen_removed(self):
        data = self.data_without("cards.spend-details", "cards.card-revealed")
        led = self.ledger(data_dir=data)
        led.hand_found()
        led.confirm_rest()
        key = "contrast:color-contrast:dddddddddddd"
        snapshot(self.ws_dir, generated=[(key, "cards.i-a11y-contrast", ["cards.spend-details", "cards.card-revealed"], "medium")])
        e = self.compute(led)["findings"][key]
        self.assertEqual((e["status"], e["reason"]), ("fixed", "screen removed"))

    # Stage ---------------------------------------------------------------------

    def test_the_stage_writes_diff_json_with_the_baseline_run(self):
        led = self.ledger()
        led.hand_found()
        f = led.scanner("contrast", "color-contrast", ["cards.index"])
        led.confirm_rest()
        snapshot(self.ws_dir, generated=[(f["key"], "cards.i-a11y-contrast", ["cards.index"], "medium")])
        ws = led.write()
        code, out, err = run_diff(ws, RUN_B)
        self.assertEqual(code, 0, err)
        doc = read_json(led.path("diff.json"))
        self.assertEqual(doc["baseline"], RUN_A)
        self.assertEqual(doc["findings"][f["key"]]["status"], "persisting")
        self.assertEqual(diff.status(ws, RUN_B)["baseline"], RUN_A)
        self.assertEqual(diff.status(ws, RUN_B)["findings"]["persisting"], 1)

    def test_after_the_run_is_published_diff_keeps_the_diff_json_it_published_with(self):
        led = self.ledger()
        led.hand_found()
        led.confirm_rest()
        ws = led.write()
        write_json(led.path("diff.json"), {"run": RUN_B, "baseline": RUN_A, "findings": {}, "handFound": {}})
        snapshot(self.ws_dir, run=RUN_B)
        code, out, err = run_diff(ws, RUN_B)
        self.assertEqual(code, 0)
        self.assertIn("already published", out + err)
        self.assertEqual(read_json(led.path("diff.json"))["baseline"], RUN_A)

    def test_without_findings_json_diff_fails(self):
        os.makedirs(os.path.join(self.ws_dir, "runs", RUN_A))
        write_json(os.path.join(self.ws_dir, "runs", RUN_A, "states.json"), {"run": RUN_A, "states": {}})
        ws = Workspace(workspace=self.ws_dir, run=RUN_A)
        code, out, err = run_diff(ws, RUN_A)
        self.assertEqual(code, 1)
        self.assertIn("findings.json", err)
        self.assertEqual(diff.status(ws, RUN_A), {"pending": True})


if __name__ == "__main__":
    unittest.main()
