"""`records prepare` and `judgment add` (R12; KTD18, KTD20).

Run: cd tools/a11y && python3 -m unittest tests.test_records -v

Workspaces are built in code: a few states with announced-as records in their raw files, and, for
the published-record cases, a snapshot registry and issues.json.
"""
import json
import os
import re
import tempfile
import unittest

from tests import test_verdict as tv

import group
import records
import signature
from lib import canon
from lib.ws import Workspace

RUN = tv.RUN


def occ(name="Move money", classes=("_button_btn01_5",), ancestors=(("_actions_hom22_2",),),
        xpath="/html/body/div/main/button[1]", role="button", tag="button"):
    return {"tag": tag, "role": role, "name": name, "classes": list(classes),
            "ancestors": [list(a) for a in ancestors], "landmark": {"role": "main", "name": ""}, "heading": "",
            "data": {}, "xpath": xpath, "rect": [10, 10, 90, 32], "layer": "document"}


def record(template, name="Move money", states=None, role="button", **o):
    return {"role": role, "name": name, "states": states or {}, "description": "", "template": template,
            "occurrence": occ(name=name, role=role, **o)}


MOVE = record('button "Move money" [expanded=false, haspopup=menu]', states={"expanded": False, "haspopup": "menu"})
MOVE_OPEN = record('button "Move money" [expanded=true, haspopup=menu]', states={"expanded": True, "haspopup": "menu"})
CHIP = record('menuitem "All" [selected=false]', name="All", role="menuitem", states={"selected": False},
              classes=("_chip_chp11_2",), ancestors=(("_filters_hom22_9",),))


class Scratch:
    def __init__(self, test):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.ws = os.path.join(tmp.name, "ws")
        self.run_dir = os.path.join(self.ws, "runs", RUN)
        self.states, self.raws = {}, {}

    def state(self, sid, kind="page", hops=0, *recs):
        area = sid.split(".")[0]
        path = [f"{area}.hop{i}" for i in range(hops)] + [sid]
        self.states[sid] = {"area": area, "kind": kind, "path": path}
        self.raws[sid] = {"schema": 1, "state": sid, "kind": kind, "status": "audited", "results": [],
                          "records": list(recs)}
        return self

    def write(self):
        canon.write_json(os.path.join(self.run_dir, "states.json"), {"run": RUN, "states": self.states})
        for sid, doc in self.raws.items():
            canon.write_json(os.path.join(self.run_dir, "raw", f"{sid}.json"), doc)
        return self

    def prepare(self):
        self.write()
        code, out, err = tv.run_cli("records", "prepare", "--workspace", self.ws, "--run", RUN)
        return code, json.loads(out) if out else None, err

    def lines(self):
        return canon.read_jsonl(os.path.join(self.run_dir, "records.jsonl"))

    def batches(self):
        d = os.path.join(self.run_dir, "batches")
        return {f[:-5]: canon.read_json(os.path.join(d, f)) for f in sorted(os.listdir(d))
                if f.startswith("records-") and f.endswith(".json")} if os.path.isdir(d) else {}

    def judge(self, lines, name="records-001"):
        path = tv.write_lines(os.path.join(self.run_dir, "batches", f"{name}.out.jsonl"), lines)
        code, out, err = tv.run_cli("judgment", "add", "--file", path, "--workspace", self.ws, "--run", RUN)
        return code, json.loads(out)

    def judgments(self):
        return canon.read_jsonl(os.path.join(self.run_dir, "judgments.jsonl"))

    def publish(self, sig, family, issue_id, wcag=None, tombstone=None):
        pub = os.path.join(self.ws, "published")
        key = signature.record_finding_key(family, sig)
        canon.write_json(os.path.join(pub, "registry.json"), {
            "keys": {key: issue_id}, "held": [issue_id],
            "tombstones": {issue_id: {"reason": tombstone, "attachedTo": None, "run": "r", "date": "d"}} if tombstone else {},
            "attachments": []})
        if wcag:
            canon.write_json(os.path.join(pub, "issues.json"), {"generated": [{"id": issue_id, "wcag": wcag}],
                                                                "overlays": {}})


def judgment(sig, **over):
    line = {"sig": sig, "verdict": "ok", "criterion": None, "family": None, "reason": "Name, role and state match.",
            "batch": "records-001", "model": "sonnet", "prompt": "record-judge@1"}
    line.update(over)
    return line


class Prepare(unittest.TestCase):
    def test_three_states_sharing_one_signature_and_template_make_one_record(self):
        s = Scratch(self)
        s.state("home.tasks", "page", 2, MOVE).state("home.index", "page", 1, MOVE, MOVE_OPEN)
        s.state("cards.menu", "menu", 1, MOVE)
        code, out, err = s.prepare()
        self.assertEqual(code, 0, err)
        lines = s.lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual([x["sig"] for x in lines], sorted(x["sig"] for x in lines))
        merged = next(x for x in lines if x["template"] == MOVE["template"])
        self.assertEqual(merged, {
            "sig": signature.record_sig(MOVE), "template": MOVE["template"], "role": "button",
            "states": {"expanded": False, "haspopup": "menu"}, "description": "",
            "states_seen": ["cards.menu", "home.index", "home.tasks"], "sample": "home.index",
            "occurrence": MOVE["occurrence"], "published": None})
        opened = next(x for x in lines if x["template"] == MOVE_OPEN["template"])
        self.assertEqual(opened["states_seen"], ["home.index"])
        self.assertEqual(out["records"], 2)

    def test_the_first_occurrence_comes_from_the_first_state(self):
        s = Scratch(self)
        later = record(MOVE["template"], xpath="/html/body/div/main/button[7]")
        s.state("b.two", "page", 0, later).state("a.one", "page", 3, MOVE)
        s.prepare()
        (line,) = s.lines()
        self.assertEqual((line["occurrence"]["xpath"], line["sample"]), ("/html/body/div/main/button[1]", "b.two"))

    def test_judge_batches_hold_about_150_records(self):
        s = Scratch(self)
        recs = [record(f'button "Action {chr(65 + i // 26)}{chr(65 + i % 26)}"', name=f"Action {i}")
                for i in range(320)]
        s.state("a.one", "page", 0, *recs)
        code, out, err = s.prepare()
        self.assertEqual(code, 0, err)
        got = s.batches()
        self.assertEqual({k: len(b["records"]) for k, b in got.items()},
                         {"records-001": 107, "records-002": 107, "records-003": 106})
        sigs = [r["sig"] for b in got.values() for r in b["records"]]
        self.assertEqual(sorted(sigs), sorted(x["sig"] for x in s.lines()))
        first = got["records-001"]["records"][0]
        self.assertEqual(set(first), {"sig", "template", "role", "states", "description", "sample", "screens",
                                      "occurrence"})
        self.assertEqual((got["records-001"]["batch"], got["records-001"]["run"]), ("records-001", RUN))
        self.assertEqual(out["batches"], ["records-001", "records-002", "records-003"])

    def test_the_same_input_gives_the_same_files(self):
        s = Scratch(self)
        s.state("a.one", "page", 0, MOVE, CHIP).state("a.two", "page", 0, CHIP)
        s.prepare()
        paths = [os.path.join(s.run_dir, "records.jsonl"), os.path.join(s.run_dir, "batches", "records-001.json")]
        first = [open(p, "rb").read() for p in paths]
        os.remove(paths[1])
        s.prepare()
        self.assertEqual([open(p, "rb").read() for p in paths], first)

    def test_on_resume_only_unjudged_records_are_batched(self):
        s = Scratch(self)
        s.state("a.one", "page", 0, MOVE, CHIP)
        s.prepare()
        _, again, _ = s.prepare()
        self.assertEqual((again["batches"], again["inFlight"]), ([], ["records-001"]))
        s.judge([judgment(signature.record_sig(MOVE))])
        _, third, _ = s.prepare()
        self.assertEqual(third["batches"], ["records-002"])
        self.assertEqual([r["sig"] for r in s.batches()["records-002"]["records"]], [signature.record_sig(CHIP)])

    def test_unreachable_states_are_left_out(self):
        s = Scratch(self)
        s.state("a.one", "page", 0, MOVE).state("a.gone", "page", 0, CHIP)
        s.raws["a.gone"]["status"] = "unreachable"
        s.prepare()
        self.assertEqual([x["template"] for x in s.lines()], [MOVE["template"]])


class Published(unittest.TestCase):
    """KTD18: a published finding that came from announced-as records skips the judge while its
    record signature is still present, and goes straight to verification."""

    def test_a_published_record_skips_the_judge_and_reaches_group_as_a_flag(self):
        s = Scratch(self)
        s.state("home.tasks", "page", 0, CHIP, MOVE)
        sig = signature.record_sig(CHIP)
        s.publish(sig, "role-state", "home.i-a11y-role-state-menuitem-all", wcag=["4.1.2", "1.3.1"])
        code, out, err = s.prepare()
        self.assertEqual(code, 0, err)
        line = next(x for x in s.lines() if x["sig"] == sig)
        self.assertEqual(line["published"], "home.i-a11y-role-state-menuitem-all")
        batched = [r["sig"] for b in s.batches().values() for r in b["records"]]
        self.assertEqual(batched, [signature.record_sig(MOVE)])
        (carried,) = s.judgments()
        self.assertEqual({k: carried[k] for k in ("sig", "verdict", "criterion", "family", "batch")},
                         {"sig": sig, "verdict": "flag", "criterion": "4.1.2", "family": "role-state",
                          "batch": "published"})
        self.assertIn("home.i-a11y-role-state-menuitem-all", carried["reason"])
        checks, wcag, _ = group.load_check_data()
        ws = Workspace(workspace=s.ws, run=RUN)
        groups, advisory, _ = group.collect(s.raws, group.latest_judgments(ws, RUN), checks, wcag)
        self.assertEqual(list(groups), [signature.record_finding_key("role-state", sig)])
        s.prepare()
        self.assertEqual(len(s.judgments()), 1)

    def test_without_issues_json_the_family_primary_criterion_is_used(self):
        s = Scratch(self)
        s.state("home.tasks", "page", 0, CHIP)
        sig = signature.record_sig(CHIP)
        s.publish(sig, "structure", "home.i-a11y-structure-chip")
        s.prepare()
        (carried,) = s.judgments()
        self.assertEqual((carried["family"], carried["criterion"]), ("structure", "1.3.1"))

    def test_a_withdrawn_record_finding_is_judged_again(self):
        s = Scratch(self)
        s.state("home.tasks", "page", 0, CHIP)
        sig = signature.record_sig(CHIP)
        s.publish(sig, "role-state", "home.i-a11y-role-state-menuitem-all", wcag=["4.1.2"], tombstone="withdrawn")
        s.prepare()
        self.assertEqual(s.lines()[0]["published"], None)
        self.assertEqual(s.judgments(), [])
        self.assertEqual([r["sig"] for r in s.batches()["records-001"]["records"]], [sig])


class JudgmentAdd(unittest.TestCase):
    def setUp(self):
        self.s = Scratch(self)
        self.s.state("home.tasks", "page", 0, CHIP, MOVE)
        self.s.prepare()
        self.chip = signature.record_sig(CHIP)

    def assert_rejected(self, line, pattern):
        code, out = self.s.judge([line])
        self.assertEqual((code, out["appended"]), (1, 0), out)
        reasons = out["rejected"][0]["reasons"]
        self.assertTrue(any(re.search(pattern, r) for r in reasons), reasons)
        self.assertEqual(self.s.judgments(), [])

    def test_an_ok_is_logged(self):
        code, out = self.s.judge([judgment(self.chip)])
        self.assertEqual((code, out["appended"]), (0, 1))
        self.assertEqual(self.s.judgments(), [judgment(self.chip)])

    def test_a_flag_with_a_criterion_and_family_feeds_group(self):
        flag = judgment(self.chip, verdict="flag", criterion="4.1.2", family="role-state",
                        reason="Filter chips are menuitems outside a menu.")
        code, out = self.s.judge([flag])
        self.assertEqual(code, 0, out)
        checks, wcag, _ = group.load_check_data()
        groups, advisory, _ = group.collect(self.s.raws, group.latest_judgments(Workspace(workspace=self.s.ws), RUN),
                                            checks, wcag)
        self.assertEqual(list(groups), [signature.record_finding_key("role-state", self.chip)])
        self.assertEqual(advisory, [])

    def test_a_flag_without_a_criterion_goes_to_the_advisory_list(self):
        flag = judgment(self.chip, verdict="flag", reason="Announced as a bare amount.")
        code, out = self.s.judge([flag])
        self.assertEqual(code, 0, out)
        checks, wcag, _ = group.load_check_data()
        groups, advisory, _ = group.collect(self.s.raws, group.latest_judgments(Workspace(workspace=self.s.ws), RUN),
                                            checks, wcag)
        self.assertEqual(groups, {})
        self.assertEqual(advisory, [{"sig": self.chip, "template": CHIP["template"],
                                     "reason": "Announced as a bare amount.", "states": 1}])

    def test_a_flag_must_carry_the_criterion_field(self):
        line = judgment(self.chip, verdict="flag")
        del line["criterion"]
        self.assert_rejected(line, "criterion")

    def test_a_criterion_outside_wcag_22_aa_is_rejected(self):
        self.assert_rejected(judgment(self.chip, verdict="flag", criterion="2.1.3", family="role-state"), "2.1.3")

    def test_a_flag_with_a_criterion_names_a_record_family(self):
        self.assert_rejected(judgment(self.chip, verdict="flag", criterion="4.1.2", family="contrast"), "family")
        self.assert_rejected(judgment(self.chip, verdict="flag", criterion="4.1.2"), "family")

    def test_verdict_must_be_ok_or_flag(self):
        self.assert_rejected(judgment(self.chip, verdict="maybe"), "verdict")

    def test_the_signature_must_be_known(self):
        self.assert_rejected(judgment("rec:000000000000"), "rec:000000000000")

    def test_reason_batch_model_and_prompt_are_required(self):
        for field in ("reason", "batch", "model", "prompt"):
            with self.subTest(field=field):
                line = judgment(self.chip)
                del line[field]
                self.assert_rejected(line, field)

    def test_a_malformed_line_is_returned_and_the_rest_appended(self):
        code, out = self.s.judge([judgment(self.chip), "not json", judgment(signature.record_sig(MOVE))])
        self.assertEqual((code, out["appended"], [r["line"] for r in out["rejected"]]), (1, 2, [2]))

    def test_adding_the_same_file_twice_appends_nothing_the_second_time(self):
        lines = [judgment(self.chip), judgment(signature.record_sig(MOVE))]
        self.s.judge(lines)
        code, out = self.s.judge(lines)
        self.assertEqual((code, out["appended"], out["duplicates"]), (0, 0, 2))
        self.assertEqual(len(self.s.judgments()), 2)


class Status(unittest.TestCase):
    def test_status_counts_records_judgments_and_batches(self):
        s = Scratch(self)
        ws = Workspace(workspace=s.ws, run=RUN)
        os.makedirs(s.run_dir)
        self.assertEqual(records.status(ws, RUN), {"pending": True})
        s.state("home.tasks", "page", 0, CHIP, MOVE)
        s.prepare()
        s.judge([judgment(signature.record_sig(CHIP), verdict="flag", reason="No tab role.")])
        self.assertEqual(records.status(ws, RUN), {"records": 2, "published": 0, "judged": 1, "flagged": 1,
                                                   "advisory": 1, "awaiting": 1, "batches": 1, "inFlight": []})


if __name__ == "__main__":
    unittest.main()
