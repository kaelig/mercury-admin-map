import os
import tempfile
import unittest

from lib.canon import harness_hash, write_json
from lib.ws import Workspace
import status

RUN = "2026-10-02-0900"
STATES = {f"x.s{i}": {"area": "x", "kind": "page"} for i in range(5)}


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(workspace=self.tmp.name, run=RUN)
        os.makedirs(os.path.join(self.tmp.name, "runs", RUN))

    def tearDown(self):
        self.tmp.cleanup()

    def write_states(self):
        write_json(self.ws.file("states.json"), {"run": RUN, "states": STATES})

    def write_raw(self, sid, outcome="audited", harness=None):
        write_json(os.path.join(self.ws.raw_dir(), f"{sid}.json"),
                   {"state": sid, "status": outcome, "harness": harness or harness_hash()})

    def test_empty_run_reports_every_stage_pending(self):
        r = status.report(self.ws)
        self.assertEqual(r["run"], RUN)
        for name, st in r["stages"].items():
            self.assertTrue(st.get("pending"), f"{name} should be pending: {st}")
        self.assertEqual(r["publish"]["state"], "not published")

    def test_three_of_five_raw_files(self):
        self.write_states()
        for sid in ("x.s0", "x.s1", "x.s2"):
            self.write_raw(sid)
        s = status.sweep_progress(self.ws, RUN)
        self.assertEqual((s["audited"], s["pending"], s["unreachable"]), (3, 2, 0))

    def test_raw_file_from_other_harness_is_stale(self):
        self.write_states()
        self.write_raw("x.s0", harness="0000000000000000")
        self.write_raw("x.s1", outcome="unreachable")
        s = status.sweep_progress(self.ws, RUN)
        self.assertEqual((s["stale"], s["unreachable"], s["pending"]), (1, 1, 3))

    def test_coverage_invariant(self):
        ids = list(STATES)
        self.assertEqual(status.check_coverage(ids, ids[:3], ids[3:]), [])
        both = status.check_coverage(ids, ids[:3], ids[2:])
        self.assertTrue(any("x.s2" in p and "audited and unreachable" in p for p in both), both)
        missing = status.check_coverage(ids, ids[:3], ids[3:4])
        self.assertEqual(missing, ["missing: x.s4"])

    def test_outcomes_coverage_reported(self):
        self.write_states()
        outcomes = {sid: {"outcome": "audited"} for sid in list(STATES)[:4]}
        write_json(self.ws.file("outcomes.json"), {"run": RUN, "states": outcomes})
        s = status.sweep_progress(self.ws, RUN)
        self.assertEqual(s["outcomes"]["coverage"], ["missing: x.s4"])

    def test_publish_incomplete_and_already_published(self):
        pub = self.ws.published_dir
        os.makedirs(pub + ".tmp")
        self.assertEqual(status.publish_state(self.ws, RUN)["state"], "publish incomplete, rerun publish")
        os.rmdir(pub + ".tmp")
        write_json(os.path.join(pub, "meta.json"), {"run": RUN})
        self.assertEqual(status.publish_state(self.ws, RUN)["state"], "already published")
        self.assertEqual(status.publish_state(self.ws, "2026-10-03-0900")["state"], f"snapshot holds run {RUN}")

    def test_restores_snapshot_left_between_renames(self):
        pub = self.ws.published_dir
        write_json(os.path.join(pub + ".prev", "meta.json"), {"run": "older"})
        st = status.publish_state(self.ws, RUN)
        self.assertTrue(st["restored"])
        self.assertTrue(os.path.exists(os.path.join(pub, "meta.json")))
        self.assertFalse(os.path.exists(pub + ".prev"))

    def test_pending_proposals(self):
        self.write_states()
        write_json(self.ws.file("findings.json"), {"proposals": [{"id": "p1"}, {"id": "p2"}]})
        with open(self.ws.file("decisions.jsonl"), "w") as f:
            f.write('{"proposal":"p1","decision":"attach"}\n')
        self.assertEqual(status.pending_proposals(self.ws, RUN), ["p2"])


if __name__ == "__main__":
    unittest.main()
