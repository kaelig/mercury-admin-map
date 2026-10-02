"""U7: `decision add`, the only writer of decisions.jsonl (R17, KTD17, KTD19).

Run: cd tools/a11y && python3 -m unittest tests.test_decision -v

The fixture (tests/data/verify/ws) proposes attaching the API-tokens button-name finding to the
hand-found "API token row menus" issue, and its snapshot registry already holds one attachment of
an older webhooks finding to that issue.
"""
import json
import os
import re
import unittest

from tests import test_verdict as tv
from lib import canon

NAME = tv.NAME
HAND = "settings.i-unnamed-row-buttons"
PROPOSAL = canon.sha([NAME, HAND])
OLD = "name:button-name:9f8e7d6c5b4a"


def decision(**over):
    line = {"proposal": PROPOSAL, "finding": NAME, "handFound": HAND, "decision": "attach",
            "reason": "Both name the same row-menu buttons on API Tokens.", "model": "sonnet",
            "batch": "verify-002", "prompt": "verifier@1"}
    line.update(over)
    return line


class DecisionTest(unittest.TestCase):
    def setUp(self):
        self.ws = tv.copy_fixture(self)
        self.run_dir = os.path.join(self.ws, "runs", tv.RUN)

    def add(self, *lines):
        path = tv.write_lines(os.path.join(self.run_dir, "batches", "verify-002.out.jsonl"), lines)
        code, out, err = tv.run_cli("decision", "add", "--file", path, "--workspace", self.ws, "--run", tv.RUN)
        self.assertEqual(err, "")
        return code, json.loads(out)

    def stored(self):
        return canon.read_jsonl(os.path.join(self.run_dir, "decisions.jsonl"))

    def assert_rejected(self, line, pattern):
        code, out = self.add(line)
        self.assertEqual((code, out["appended"]), (1, 0), out)
        reasons = out["rejected"][0]["reasons"]
        self.assertTrue(any(re.search(pattern, r) for r in reasons), reasons)
        self.assertEqual(self.stored(), [])

    def test_an_attach_answering_a_proposal_is_accepted_and_dated(self):
        code, out = self.add(decision())
        self.assertEqual((code, out["appended"], out["rejected"]), (0, 1, []))
        (got,) = self.stored()
        self.assertRegex(got.pop("date"), r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(got, decision())

    def test_a_decision_without_a_reason_is_rejected(self):
        self.assert_rejected(decision(reason=None), "reason")
        self.assert_rejected(decision(reason=" "), "reason")

    def test_a_decision_without_the_proposal_it_answers_is_rejected(self):
        line = decision()
        del line["proposal"]
        self.assert_rejected(line, "proposal")

    def test_a_proposal_that_does_not_exist_is_rejected(self):
        self.assert_rejected(decision(proposal="0" * 64), "not a proposal")

    def test_finding_and_hand_found_must_match_the_proposal(self):
        self.assert_rejected(decision(handFound="accounts.i-tabs-not-tabs"), "accounts.i-tabs-not-tabs")

    def test_missing_finding_and_hand_found_are_taken_from_the_proposal(self):
        line = decision(decision="reject", reason="The row menu and the scanner's button differ.")
        del line["finding"], line["handFound"]
        code, out = self.add(line)
        self.assertEqual(code, 0, out)
        (got,) = self.stored()
        self.assertEqual((got["finding"], got["handFound"], got["decision"]), (NAME, HAND, "reject"))

    def test_an_unknown_decision_is_rejected(self):
        self.assert_rejected(decision(decision="maybe"), "decision")

    def test_model_batch_and_prompt_are_required(self):
        for field in ("model", "batch", "prompt"):
            with self.subTest(field=field):
                line = decision()
                del line[field]
                self.assert_rejected(line, field)

    def test_a_release_answers_an_attachment_in_the_snapshot_registry(self):
        release = decision(proposal=canon.sha([OLD, HAND]), finding=OLD, decision="release",
                           reason="The webhooks buttons now have names; the attachment no longer holds.")
        code, out = self.add(release)
        self.assertEqual((code, out["appended"]), (0, 1), out)

    def test_a_release_of_a_pair_the_registry_does_not_hold_is_rejected(self):
        self.assert_rejected(decision(decision="release"), "registry")

    def test_verdict_lines_in_the_same_file_are_left_for_verdict_add(self):
        code, out = self.add(decision(), tv.verdict())
        self.assertEqual((code, out["appended"], out["skipped"]), (0, 1, 1))


if __name__ == "__main__":
    unittest.main()
