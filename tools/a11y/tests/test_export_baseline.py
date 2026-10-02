"""U8: the agent export is unchanged when there is no snapshot (Verification Contract, "Export regression").

Run: cd tools/a11y && python3 -m unittest tests.test_export_baseline -v

tests/data/export-baseline.json was recorded before any export change: the sha256 of every
tickets/<id>.md written with `--out <dir>/agent` and no --base-url, and the sha256 of canonical() of
every issues.json record restricted to the baseline's `fields`. This test runs the export with
A11Y_SNAPSHOT pointing at a folder that does not exist and compares both, so it holds before and
after `load_inputs` moved to the shared loader, and after fields are added.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

from tests.pathshim import A11Y, ROOT

from lib.canon import canonical, read_json

BASELINE = os.path.join(A11Y, "tests", "data", "export-baseline.json")


class ExportBaseline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = os.path.join(cls.tmp.name, "site", "agent")
        env = dict(os.environ, A11Y_SNAPSHOT=os.path.join(cls.tmp.name, "no-snapshot"))
        env.pop("MAP_BASE_URL", None)
        cls.proc = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "export_agent_data.py"), "--out", cls.out],
                                  cwd=ROOT, env=env, capture_output=True, text=True)
        cls.baseline = read_json(BASELINE)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_export_runs_without_errors(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        self.assertNotIn("error:", self.proc.stderr)

    def test_every_ticket_is_byte_identical(self):
        tickets = os.path.join(self.out, "tickets")
        got = {}
        for name in sorted(os.listdir(tickets)):
            with open(os.path.join(tickets, name), "rb") as f:
                got[name[:-3]] = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(len(self.baseline["tickets"]), 236)
        self.assertEqual(sorted(got), sorted(self.baseline["tickets"]))
        changed = sorted(i for i, h in got.items() if self.baseline["tickets"][i] != h)
        self.assertEqual(changed, [])

    def test_every_issue_record_matches_on_the_baseline_fields(self):
        doc = read_json(os.path.join(self.out, "issues.json"))
        fields = self.baseline["fields"]
        got = {r["id"]: hashlib.sha256(canonical({k: r.get(k) for k in fields}).encode()).hexdigest()
               for r in doc["issues"]}
        self.assertEqual(sorted(got), sorted(self.baseline["issues"]))
        changed = sorted(i for i, h in got.items() if self.baseline["issues"][i] != h)
        self.assertEqual(changed, [])
        for r in doc["issues"]:
            self.assertTrue(set(fields) <= set(r), r["id"])

    def test_issues_jsonl_carries_the_same_records(self):
        with open(os.path.join(self.out, "issues.jsonl"), encoding="utf-8") as f:
            lines = [json.loads(x) for x in f if x.strip()]
        fields = self.baseline["fields"]
        got = {r["id"]: hashlib.sha256(canonical({k: r.get(k) for k in fields}).encode()).hexdigest() for r in lines}
        self.assertEqual(got, self.baseline["issues"])


if __name__ == "__main__":
    unittest.main()
