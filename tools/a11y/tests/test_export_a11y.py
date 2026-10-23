"""U9: the agent ticket export carries the accessibility audit's facts (R15, R17, R18; KTD23).

Run: cd tools/a11y && python3 -m unittest tests.test_export_a11y -v

The export runs three times, each with `--out <tmp>/<name>/site/agent`:
- `snap`: A11Y_SNAPSHOT is the test snapshot (tests/data/snapshot), as `python3 -m tests.ledger` wrote it.
- `big`: a temp copy of the test snapshot with two more generated issues, one on 214 real screens (AE1)
  and one on 20, plus two tombstones: one for an id that is live again, and one attached to a
  hand-found issue.
- `none`: A11Y_SNAPSHOT is a folder that does not exist.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.pathshim import A11Y, ROOT

import export_agent_data as export

SNAPSHOT = os.path.join(A11Y, "tests", "data", "snapshot")
CONTRAST = "transactions.i-a11y-contrast-text-in-recent-transactions"   # 30 screens in the test snapshot
RERATED = "accounts.i-tabs-not-tabs"
DROPPED = "cards.i-modal-heading-semantics"
UNAUDITED = "payments.i-edit-wipes-due-dates"
FIXED_TOMB = "home.i-a11y-target-size-button-dismiss"   # tombstoned in the test snapshot, not live
BIG = "accounts.i-a11y-contrast-muted-table-text"   # primary screen accounts.index
TWENTY = "cards.i-a11y-contrast-muted-card-text"
LIVE_AGAIN = "cards.i-a11y-name-button-more-actions"
ATTACHED = "settings.i-a11y-name-button-token-row-menu"
HAND_FOUND = "settings.i-unnamed-row-buttons"


def all_node_ids():
    ids = []
    for path in sorted(glob.glob(os.path.join(ROOT, "data", "*.json"))):
        with open(path, encoding="utf-8") as f:
            ids += [n["id"] for n in json.load(f)["nodes"]]
    return ids


def generated_like(base, iid, title, node_ids):
    """A generated issue cloned from base, on node_ids, with its own id and titles."""
    g = json.loads(json.dumps(base))
    g.update(id=iid, area=iid.split(".")[0], title=title, nodeIds=list(node_ids))
    g["copy"] = dict(g["copy"], title=f"Raise the contrast of {title.split(': ', 1)[1]}")
    return g


def run_export(out, snapshot):
    env = dict(os.environ, A11Y_SNAPSHOT=snapshot)
    env.pop("MAP_BASE_URL", None)
    return subprocess.run([sys.executable, os.path.join(ROOT, "tools", "export_agent_data.py"), "--out", out],
                          cwd=ROOT, env=env, capture_output=True, text=True)


class Out:
    """One export run's folder, read on demand."""

    def __init__(self, root, snapshot):
        self.agent = os.path.join(root, "site", "agent")
        self.proc = run_export(self.agent, snapshot)
        self._json = {}

    def json(self, name):
        if name not in self._json:
            with open(os.path.join(self.agent, name), encoding="utf-8") as f:
                self._json[name] = json.load(f)
        return self._json[name]

    def records(self):
        return {r["id"]: r for r in self.json("issues.json")["issues"]}

    def jsonl(self):
        with open(os.path.join(self.agent, "issues.jsonl"), encoding="utf-8") as f:
            return {r["id"]: r for r in (json.loads(x) for x in f if x.strip())}

    def text(self, name):
        with open(os.path.join(self.agent, name), encoding="utf-8") as f:
            return f.read()

    def ticket(self, iid):
        return self.text(f"tickets/{iid}.md")

    def has_ticket(self, iid):
        return os.path.exists(os.path.join(self.agent, "tickets", f"{iid}.md"))


def screen_rows(ticket):
    """The table rows under '## Affected screens', header and separator excluded."""
    section = ticket.split("## Affected screens\n", 1)[1].split("\n## ", 1)[0]
    rows = [x for x in section.splitlines() if x.startswith("| ")]
    return rows[1:]


class ExportA11y(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        with open(os.path.join(SNAPSHOT, "issues.json"), encoding="utf-8") as f:
            cls.snapshot = json.load(f)
        cls.generated = {g["id"]: g for g in cls.snapshot["generated"]}

        big_snap = os.path.join(cls.tmp.name, "big-snapshot")
        shutil.copytree(SNAPSHOT, big_snap)
        nodes = all_node_ids()
        cls.big_nodes = nodes[:214]
        cls.twenty_nodes = [n for n in nodes if n.startswith("cards.")][:20]
        base = cls.generated[CONTRAST]
        doc = json.loads(json.dumps(cls.snapshot))
        doc["generated"] += [generated_like(base, BIG, "Low contrast: muted table text", cls.big_nodes),
                             generated_like(base, TWENTY, "Low contrast: muted card text", cls.twenty_nodes)]
        with open(os.path.join(big_snap, "issues.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f)
        with open(os.path.join(big_snap, "registry.json"), encoding="utf-8") as f:
            reg = json.load(f)
        reg["tombstones"][LIVE_AGAIN] = {"reason": "fixed", "attachedTo": None, "run": "2026-10-01-0900",
                                         "date": "2026-10-01"}
        reg["tombstones"][ATTACHED] = {"reason": "attached", "attachedTo": HAND_FOUND, "run": "2026-10-02-1400",
                                       "date": "2026-10-02"}
        with open(os.path.join(big_snap, "registry.json"), "w", encoding="utf-8") as f:
            json.dump(reg, f)

        cls.snap = Out(os.path.join(cls.tmp.name, "snap"), SNAPSHOT)
        cls.big = Out(os.path.join(cls.tmp.name, "big"), big_snap)
        cls.none = Out(os.path.join(cls.tmp.name, "none"), os.path.join(cls.tmp.name, "no-snapshot"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_run_exits_cleanly(self):
        for out in (self.snap, self.big, self.none):
            self.assertEqual(out.proc.returncode, 0, out.proc.stderr)
            self.assertNotIn("error:", out.proc.stderr)

    # -- copy, labels, audit block ---------------------------------------------------------------

    def test_generated_copy_replaces_the_fallback_without_a_warning(self):
        records = self.snap.records()
        for iid, g in self.generated.items():
            self.assertNotIn(f"no curated copy for {iid}", self.snap.proc.stderr)
            r = records[iid]
            self.assertEqual(r["title"], g["copy"]["title"])
            self.assertFalse(r["title"].startswith("Resolve:"), iid)
            self.assertEqual(r["summary"], g["copy"]["summary"])
            self.assertEqual(r["fixHypothesis"], g["copy"]["fixHypothesis"])
            self.assertTrue(r["curated"], iid)
        self.assertNotIn("using generated fallback", self.snap.proc.stderr)

    def test_the_contrast_ticket_names_criterion_verdict_date_and_build(self):
        r = self.snap.records()[CONTRAST]
        self.assertEqual(r["dedupeKey"], f"mercury-admin-ux-audit:{CONTRAST}")
        self.assertIn("wcag:1.4.3", r["labels"])
        self.assertIn("source:a11y-audit", r["labels"])
        self.assertEqual(r["wcag"], ["1.4.3"])
        self.assertEqual(r["audit"], self.generated[CONTRAST]["audit"])
        t = self.snap.ticket(CONTRAST)
        self.assertIn("## Accessibility audit", t)
        self.assertIn("[1.4.3 Contrast (Minimum)](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum)", t)
        self.assertIn("Check: contrast", t)
        self.assertIn("Verdict: confirmed. Reproduced on every sampled state.", t)
        self.assertIn("2026-10-02", t)
        self.assertIn("`b1d2e3`", t)
        self.assertIn("`transactions.index`", t)
        self.assertIn("`transactions.attachment-menu`", t)
        self.assertIn(f"Dedupe key: `mercury-admin-ux-audit:{CONTRAST}`", t)

    def test_one_wcag_label_per_criterion(self):
        r = self.snap.records()[RERATED]
        self.assertEqual([x for x in r["labels"] if x.startswith("wcag:")], ["wcag:2.1.1", "wcag:4.1.2"])
        self.assertIn("source:a11y-audit", r["labels"])
        self.assertIn("source:ux-audit", r["labels"])

    def test_wcag_and_audit_appear_only_on_audited_records(self):
        self.assertNotIn("wcag", export.REQUIRED_ISSUE_FIELDS)
        self.assertNotIn("audit", export.REQUIRED_ISSUE_FIELDS)
        for out in (self.snap, self.none):
            for r in out.records().values():
                audited = bool(r.get("audit"))
                self.assertEqual("wcag" in r, audited, r["id"])
                self.assertEqual("source:a11y-audit" in r["labels"], audited, r["id"])
        self.assertNotIn("audit", self.snap.records()[UNAUDITED])
        self.assertFalse(any("audit" in r for r in self.none.records().values()))

    def test_an_audited_ticket_prints_the_full_live_evidence(self):
        for iid in (CONTRAST, RERATED, DROPPED):
            r = self.snap.records()[iid]
            t = self.snap.ticket(iid)
            evidence = t.split("## Evidence (verbatim from the audit)\n", 1)[1].split("\n\n", 1)[0]
            self.assertEqual(evidence, "> " + r["audit"]["evidence"].replace("\n", "\n> "), iid)
        # The generated issue's one-line map version is not the full evidence's first line.
        self.assertNotIn(self.snap.records()[CONTRAST]["evidence"], self.snap.ticket(CONTRAST))

    def test_verify_list_names_the_audit_date(self):
        records = self.snap.records()
        first = records[CONTRAST]["verifyBeforeFiling"][0]
        self.assertIn("2026-10-02", first)
        self.assertNotIn(export.CAPTURED, first)
        self.assertIn(export.CAPTURED, records[UNAUDITED]["verifyBeforeFiling"][0])

    def test_hand_found_tickets_print_the_rating_history(self):
        self.assertIn("rated high on 2026-10-02, previously medium", self.snap.ticket(RERATED))
        self.assertIn("not reproduced on 2026-10-02: The Edit card limits title is an h2 inside the dialog header",
                      self.snap.ticket(DROPPED))
        self.assertEqual(self.snap.records()[RERATED]["history"], "rated high on 2026-10-02, previously medium")
        self.assertNotIn("history", self.snap.records()[CONTRAST])

    def test_validate_issues_passes_for_generated_issues(self):
        records = list(self.snap.records().values())
        self.assertEqual(export.validate_issues(records), [])

    def test_every_generated_id_gets_a_ticket(self):
        records = self.snap.records()
        for iid in self.generated:
            self.assertIn(iid, records)
            self.assertTrue(self.snap.ticket(iid).startswith(f"# {self.generated[iid]['copy']['title']}\n"), iid)

    # -- size caps (AE1) -------------------------------------------------------------------------

    def test_ae1_a_214_screen_record_is_capped_on_write(self):
        for records in (self.big.records(), self.big.jsonl()):
            r = records[BIG]
            self.assertEqual(len(r["affectedScreens"]), 25)
            self.assertEqual([s["nodeId"] for s in r["affectedScreens"]], self.big_nodes[:25])
            self.assertEqual(r["affectedScreenCount"], 214)
            self.assertEqual(r["affectedScreenIds"], self.big_nodes)
        r = self.big.records()[BIG]
        self.assertIn("213 other screens, listed in affectedScreenIds", r["reproduce"]["steps"][-1])

    def test_ae1_a_214_screen_ticket_lists_15_rows_then_links_the_rest(self):
        t = self.big.ticket(BIG)
        rows = screen_rows(t)
        self.assertEqual(len(rows), 15)
        self.assertIn(f"[open](../../index.html#{self.big_nodes[14]})", rows[-1])
        self.assertIn("and 199 more", t)
        self.assertIn(f"[show all 214 screens](../../index.html?issue={BIG})", t)

    def test_ae1_full_lists_reach_screens_journeys_and_the_index(self):
        big = set(self.big_nodes)
        for s in self.big.json("screens.json")["screens"]:
            self.assertEqual(BIG in s["issueIds"], s["nodeId"] in big, s["nodeId"])
        journeys = self.big.json("journeys.json")["journeys"]
        through = [j for j in journeys if big & {s["nodeId"] for s in j["steps"]}]
        self.assertTrue(through)
        for j in journeys:
            self.assertEqual(BIG in j["issueIds"], j in through, j["id"])
        row = next(x for x in self.big.json("issues-index.json")["issues"] if x["id"] == BIG)
        self.assertEqual(row["screens"], 214)
        self.assertEqual(row["wcag"], ["1.4.3"])

    def test_a_20_screen_issue_keeps_its_full_list_and_table(self):
        r = self.big.records()[TWENTY]
        self.assertEqual(len(r["affectedScreens"]), 20)
        self.assertEqual(r["affectedScreenCount"], 20)
        self.assertEqual(r["affectedScreenIds"], self.twenty_nodes)
        t = self.big.ticket(TWENTY)
        self.assertEqual(len(screen_rows(t)), 20)
        self.assertNotIn("more:", t)
        self.assertNotIn("?issue=", t)

    def test_a_30_screen_issue_is_capped_too(self):
        r = self.snap.records()[CONTRAST]
        self.assertEqual((len(r["affectedScreens"]), r["affectedScreenCount"]), (25, 30))
        self.assertEqual(len(screen_rows(self.snap.ticket(CONTRAST))), 15)
        self.assertIn("and 15 more", self.snap.ticket(CONTRAST))

    def test_every_record_carries_the_full_count_and_ids(self):
        for out in (self.snap, self.none):
            for r in out.records().values():
                self.assertEqual(r["affectedScreenCount"], len(r["affectedScreenIds"]), r["id"])
                if r["affectedScreenCount"] <= 25:
                    self.assertEqual([s["nodeId"] for s in r["affectedScreens"]], r["affectedScreenIds"], r["id"])

    # -- tombstones ------------------------------------------------------------------------------

    def test_a_tombstoned_id_that_is_not_live_gets_a_stub(self):
        self.assertNotIn(FIXED_TOMB, self.snap.records())
        t = self.snap.ticket(FIXED_TOMB)
        self.assertIn("No longer open since run 2026-10-02-1400", t)
        self.assertIn("fixed", t)
        self.assertIn(f"`mercury-admin-ux-audit:{FIXED_TOMB}`", t)

    def test_an_attached_stub_names_its_hand_found_issue(self):
        t = self.big.ticket(ATTACHED)
        self.assertIn("No longer open since run 2026-10-02-1400", t)
        self.assertIn(f"`{HAND_FOUND}`", t)
        self.assertIn(f"]({HAND_FOUND}.md)", t)

    def test_a_tombstoned_id_that_is_live_again_gets_its_full_ticket(self):
        self.assertIn(LIVE_AGAIN, self.big.records())
        t = self.big.ticket(LIVE_AGAIN)
        self.assertNotIn("No longer open", t)
        self.assertIn("## Summary", t)
        self.assertIn("## Accessibility audit", t)

    def test_no_snapshot_writes_no_stub(self):
        self.assertFalse(self.none.has_ticket(FIXED_TOMB))
        names = sorted(os.listdir(os.path.join(self.none.agent, "tickets")))
        self.assertEqual(names, sorted(f"{r}.md" for r in self.none.records()))

    # -- schema and documents --------------------------------------------------------------------

    def test_the_schema_version_is_bumped(self):
        self.assertEqual(export.SCHEMA_VERSION, 2)
        self.assertEqual(self.snap.json("issues.json")["meta"]["schemaVersion"], 2)

    def test_the_readme_schema_documents_the_new_fields(self):
        for out in (self.snap, self.none):
            schema = out.text("README.md").split("## Issue record schema", 1)[1].split("\n## ", 1)[0]
            for field in ("wcag", "audit", "affectedScreenCount", "affectedScreenIds", "history"):
                self.assertRegex(schema, re.compile(rf"^\| `{field}` \|", re.M), field)
            self.assertIn("source:a11y-audit", schema)

    def test_llms_txt_points_at_the_report_only_with_a_snapshot(self):
        self.assertIn("(../a11y/index.html)", self.snap.text("llms.txt"))
        with open(os.path.join(os.path.dirname(self.snap.agent), "llms.txt"), encoding="utf-8") as f:
            self.assertIn("(a11y/index.html)", f.read())
        self.assertNotIn("a11y/index.html", self.none.text("llms.txt"))


if __name__ == "__main__":
    unittest.main()
