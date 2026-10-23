"""U6 and U8: the shared map-data loader (KTD22): raw read, snapshot merge, viewer fields and order, and
build.py's test-data flag.

Run: cd tools/a11y && python3 -m unittest tests.test_mapdata -v
"""
import contextlib
import glob
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.pathshim import A11Y, ROOT

import mapdata

DATA = os.path.join(ROOT, "data")
SNAPSHOT = os.path.join(A11Y, "tests", "data", "snapshot")


def raw_areas(data_dir):
    out = []
    for path in sorted(glob.glob(os.path.join(data_dir, "*.json"))):
        with open(path, encoding="utf-8") as f:
            out.append(json.load(f))
    return out


def build_site(root):
    """A scratch root for build.py: the real data, patterns and screens, and a page of placeholders only."""
    os.makedirs(os.path.join(root, "tools"))
    for name in ("mapdata.py", "export_agent_data.py"):
        shutil.copy(os.path.join(ROOT, "tools", name), os.path.join(root, "tools", name))
    shutil.copytree(DATA, os.path.join(root, "data"))
    shutil.copy(os.path.join(ROOT, "patterns.json"), root)
    os.symlink(os.path.join(ROOT, "screens"), os.path.join(root, "screens"))
    os.makedirs(os.path.join(root, "src"))
    with open(os.path.join(root, "src", "index.html"), "w", encoding="utf-8") as f:
        f.write("<!--FONTS--><!--STYLE--><!--TEST_DATA--><!--MAP_DATA--><!--MAP_LAYOUT--><!--APP-->")
    open(os.path.join(root, "src", "app.css"), "w").close()
    shutil.copy(os.path.join(ROOT, "src", "test-data.css"), os.path.join(root, "src"))
    os.makedirs(os.path.join(root, "dist"))


def run_build(root, env):
    """build.py's main() on root with env set: the real loader, export and report run; screen encoding, the
    atlas, node and esbuild are stubbed. Returns (stderr, the export subprocess results)."""
    spec = importlib.util.spec_from_file_location("build_under_test", os.path.join(ROOT, "build.py"))
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    real_run, exports = subprocess.run, []

    def run(cmd, **kwargs):
        if cmd[0] == "node":
            with open(os.path.join(root, "build", "layout.json"), "w") as f:
                f.write("{}")
        elif cmd[0] == "npx":
            open(os.path.join(root, "build", "app.js"), "w").close()
        else:
            exports.append(real_run(cmd, capture_output=True, text=True, **kwargs))

    def convert(rel):
        return rel, (1440, 900), os.path.join(root, "dist", "img", os.path.splitext(os.path.relpath(rel, "screens"))[0])

    err = io.StringIO()
    paths = {"ROOT": root, "DIST": os.path.join(root, "dist"), "CACHE": os.path.join(root, "dist", "img"),
             "BUILD": os.path.join(root, "build")}
    with contextlib.ExitStack() as stack:
        for name, value in paths.items():
            stack.enter_context(mock.patch.object(build, name, value))
        stack.enter_context(mock.patch.object(mapdata, "ROOT", root))
        stack.enter_context(mock.patch.object(build.subprocess, "run", side_effect=run))
        stack.enter_context(mock.patch.object(build, "convert", side_effect=convert))
        stack.enter_context(mock.patch.object(build, "build_atlas", return_value={"src": "img/atlas.webp"}))
        stack.enter_context(mock.patch.dict(os.environ, env))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(err))
        os.environ.pop("MAP_SKIP_INVALID_DATA", None)
        build.main()
    return err.getvalue(), exports


class LoadAreas(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.missing = os.path.join(self.tmp.name, "no-snapshot")

    def tearDown(self):
        self.tmp.cleanup()

    def test_without_a_snapshot_the_result_is_the_raw_data(self):
        expected = raw_areas(DATA)
        self.assertEqual(len(expected), 9)
        self.assertEqual(mapdata.load_areas(DATA, snapshot_dir=False), expected)
        self.assertEqual(mapdata.load_areas(DATA, snapshot_dir=self.missing), expected)
        with mock.patch.dict(os.environ, {"A11Y_SNAPSHOT": self.missing}):
            self.assertEqual(mapdata.load_areas(), expected)

    def test_default_snapshot_is_the_committed_published_folder(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("A11Y_SNAPSHOT", None)
            self.assertEqual(mapdata.snapshot_path(), os.path.join(ROOT, "a11y", "published"))
            self.assertEqual(mapdata.snapshot_path(self.missing), self.missing)
        with mock.patch.dict(os.environ, {"A11Y_SNAPSHOT": self.missing}):
            self.assertEqual(mapdata.snapshot_path(), self.missing)
            self.assertEqual(mapdata.snapshot_path("/elsewhere"), "/elsewhere")

    def test_files_are_read_in_sorted_order_as_utf8(self):
        d = os.path.join(self.tmp.name, "data")
        os.makedirs(d)
        for name, title in (("zeta.json", "Zêta"), ("alpha.json", "Alpha — one")):
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                json.dump({"area": name[:-5], "title": title}, f, ensure_ascii=False)
        areas = mapdata.load_areas(d, snapshot_dir=False)
        self.assertEqual([a["title"] for a in areas], ["Alpha — one", "Zêta"])

    def test_an_empty_snapshot_folder_without_issues_json_reads_as_raw(self):
        snap = os.path.join(self.tmp.name, "published")
        os.makedirs(snap)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(mapdata.load_areas(DATA, snapshot_dir=snap), raw_areas(DATA))
        self.assertIn("WARN", err.getvalue())
        self.assertIn("no issues.json", err.getvalue())


class MalformedAreaFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = os.path.join(self.tmp.name, "data")
        os.makedirs(self.data)
        for name, text in (("a.json", '{"area": "a"}'), ("b.json", '{"area": "b",'), ("c.json", '{"area": "c"}')):
            with open(os.path.join(self.data, name), "w", encoding="utf-8") as f:
                f.write(text)

    def test_a_malformed_file_raises_without_a_problems_list(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAP_SKIP_INVALID_DATA", None)
            with self.assertRaises(json.JSONDecodeError):
                mapdata.load_areas(self.data, snapshot_dir=False)
            with self.assertRaises(json.JSONDecodeError):
                mapdata.read_areas(self.data)

    def test_map_skip_invalid_data_skips_the_file_without_a_problems_list(self):
        with mock.patch.dict(os.environ, {"MAP_SKIP_INVALID_DATA": "1"}):
            areas = mapdata.load_areas(self.data, snapshot_dir=False)
        self.assertEqual([a["area"] for a in areas], ["a", "c"])

    def test_with_a_problems_list_the_file_is_reported_and_its_area_skipped(self):
        problems = []
        areas = mapdata.load_areas(self.data, snapshot_dir=False, problems=problems)
        self.assertEqual([a["area"] for a in areas], ["a", "c"])
        self.assertEqual(len(problems), 1)
        self.assertRegex(problems[0], r"^b\.json: invalid JSON \(Expecting .+\)$")

    def test_a_clean_data_dir_adds_no_problems(self):
        problems = []
        mapdata.load_areas(DATA, snapshot_dir=False, problems=problems)
        self.assertEqual(problems, [])

    def test_build_warns_and_builds_the_map_and_export_without_that_area(self):
        # build.py on a scratch root holding the real data plus one file that does not parse.
        root = os.path.join(self.tmp.name, "site")
        build_site(root)
        with open(os.path.join(root, "data", "broken.json"), "w", encoding="utf-8") as f:
            f.write('{"area": "broken",')
        env = {"A11Y_SNAPSHOT": os.path.join(root, "no-snapshot")}
        err, exports = run_build(root, env)

        warns = [line for line in err.splitlines() if line.startswith("WARN")]
        self.assertEqual(len(warns), 1, err)
        self.assertRegex(warns[0], r"^WARN broken\.json: invalid JSON \(Expecting .+\)$")
        with open(os.path.join(root, "build", "map.json"), encoding="utf-8") as f:
            self.assertEqual(sorted(a["area"] for a in json.load(f)["areas"]), sorted(a["area"] for a in raw_areas(DATA)))
        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0].stderr, "")
        with open(os.path.join(root, "dist", "agent", "issues.json"), encoding="utf-8") as f:
            exported = {i["id"] for i in json.load(f)["issues"]}
        self.assertEqual(exported, {i["id"] for a in raw_areas(DATA) for i in a.get("issues", [])})
        # The export run by hand still stops on the bad file.
        alone = subprocess.run([sys.executable, "tools/export_agent_data.py", "--out", os.path.join(root, "out")],
                               cwd=root, capture_output=True, text=True,
                               env={**{k: v for k, v in os.environ.items() if k != "MAP_SKIP_INVALID_DATA"}, **env})
        self.assertEqual(alone.returncode, 1)
        self.assertIn("JSONDecodeError", alone.stderr)


TABS = "accounts.i-tabs-not-tabs"            # AE7: medium in data/accounts.json
ROWS = "settings.i-unnamed-row-buttons"      # medium, screens settings.tokens and settings.token-menu
HEADING = "cards.i-modal-heading-semantics"  # low, screens cards.edit-limit-modal and cards.edit-nickname


def generated(iid, area, nodes, severity="medium", **over):
    issue = {"id": iid, "area": area, "title": f"Title of {iid}", "severity": severity, "category": "a11y",
             "detail": "Detail.", "evidence": "Measured facts. Verifier: reason.", "nodeIds": list(nodes),
             "wcag": ["1.4.3"], "copy": {"title": "Copy title"}, "audit": {"family": "contrast", "verdict": "confirm"}}
    issue.update(over)
    return issue


def overlay(**over):
    o = {"severity": None, "evidenceAppend": None, "addNodeIds": [], "wcag": ["2.1.1"],
         "audit": {"family": "keyboard", "verdict": "confirm"}, "history": None}
    o.update(over)
    return o


def write_snapshot(path, generated_issues=(), overlays=None):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "issues.json"), "w", encoding="utf-8") as f:
        json.dump({"generated": list(generated_issues), "overlays": overlays or {}}, f)
    return path


def issue_in(areas, iid):
    for a in areas:
        for i in a.get("issues", []):
            if i["id"] == iid:
                return a["area"], i
    return None, None


class SnapshotMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.snap = os.path.join(self.tmp.name, "published")
        self.raw = raw_areas(DATA)

    def load(self, snap=None):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            areas = mapdata.load_areas(DATA, snapshot_dir=snap or self.snap)
        return areas, [ln for ln in err.getvalue().splitlines() if ln]

    def test_a_generated_issue_appears_in_its_area(self):
        write_snapshot(self.snap, [generated("cards.i-a11y-contrast-muted", "cards", ["cards.index", "cards.subscriptions"])])
        areas, warnings = self.load()
        self.assertEqual(warnings, [])
        area, issue = issue_in(areas, "cards.i-a11y-contrast-muted")
        self.assertEqual(area, "cards")
        self.assertEqual(issue["nodeIds"], ["cards.index", "cards.subscriptions"])
        self.assertNotIn("area", issue)
        self.assertEqual(issue["copy"], {"title": "Copy title"})
        self.assertEqual(issue["audit"]["verdict"], "confirm")
        cards = next(a for a in areas if a["area"] == "cards")
        raw_cards = next(a for a in self.raw if a["area"] == "cards")
        self.assertEqual(cards["issues"][:len(raw_cards["issues"])], raw_cards["issues"])
        self.assertEqual(len(cards["issues"]), len(raw_cards["issues"]) + 1)

    def test_the_merge_never_changes_the_raw_areas_it_was_given(self):
        write_snapshot(self.snap, [generated("cards.i-a11y-x", "cards", ["cards.index"])],
                       {TABS: overlay(severity="high", evidenceAppend="Verifier (2026-10-02): reason.")})
        areas = raw_areas(DATA)
        mapdata.merge_snapshot(areas, self.snap)
        self.assertEqual(areas, self.raw)

    def test_ae7_a_rerate_sets_the_severity_and_appends_the_reason_to_the_evidence(self):
        reason = "Verifier (2026-10-02): Tab never reaches the Treasury and Financing tabs, so keyboard users cannot switch views."
        write_snapshot(self.snap, overlays={TABS: overlay(severity="high", evidenceAppend=reason,
                                                          history="rated high on 2026-10-02, previously medium")})
        areas, warnings = self.load()
        self.assertEqual(warnings, [])
        _, raw = issue_in(self.raw, TABS)
        _, merged = issue_in(areas, TABS)
        self.assertEqual(raw["severity"], "medium")
        self.assertEqual(merged["severity"], "high")
        self.assertEqual(merged["evidence"], raw["evidence"] + " " + reason)
        self.assertEqual(merged["evidence"].count(reason), 1)
        self.assertEqual(merged["nodeIds"], raw["nodeIds"])
        self.assertEqual(merged["history"], "rated high on 2026-10-02, previously medium")
        self.assertEqual(merged["wcag"], ["2.1.1"])

    def test_a_drop_leaves_the_map_record_unchanged(self):
        write_snapshot(self.snap, overlays={HEADING: overlay(
            history="not reproduced on 2026-10-02: tool false positive",
            audit={"family": "structure", "verdict": "drop", "reason": "The span is inside an h2."})})
        areas, _ = self.load()
        _, raw = issue_in(self.raw, HEADING)
        _, merged = issue_in(areas, HEADING)
        self.assertEqual(mapdata.viewer_issue(merged), raw)
        self.assertEqual(merged["audit"]["verdict"], "drop")

    def test_an_attachment_adds_the_scanner_findings_screens(self):
        write_snapshot(self.snap, overlays={ROWS: overlay(addNodeIds=["settings.token-menu", "settings.index", "cards.index"])})
        areas, _ = self.load()
        _, merged = issue_in(areas, ROWS)
        self.assertEqual(merged["nodeIds"], ["settings.tokens", "settings.token-menu", "settings.index", "cards.index"])
        self.assertEqual(merged["severity"], "medium")

    def test_unknown_screens_are_pruned_with_a_warning_and_an_issue_left_without_screens_is_dropped(self):
        write_snapshot(self.snap, [
            generated("cards.i-a11y-partly-gone", "cards", ["cards.index", "cards.no-such-screen"]),
            generated("cards.i-a11y-all-gone", "cards", ["cards.deleted-one", "cards.deleted-two"]),
            generated("home.i-a11y-kept", "home", ["home.index"]),
        ], {ROWS: overlay(addNodeIds=["settings.gone"])})
        areas, warnings = self.load()
        _, partly = issue_in(areas, "cards.i-a11y-partly-gone")
        self.assertEqual(partly["nodeIds"], ["cards.index"])
        self.assertEqual(issue_in(areas, "cards.i-a11y-all-gone"), (None, None))
        self.assertIsNotNone(issue_in(areas, "home.i-a11y-kept")[1])
        self.assertEqual(issue_in(areas, ROWS)[1]["nodeIds"], ["settings.tokens", "settings.token-menu"])
        self.assertTrue(all(w.startswith("WARN ") for w in warnings), warnings)
        joined = "\n".join(warnings)
        for word in ("cards.no-such-screen", "cards.deleted-one", "cards.deleted-two", "settings.gone"):
            self.assertIn(word, joined)
        self.assertEqual(sum("cards.i-a11y-all-gone" in w and "no known screens" in w for w in warnings), 1)

    def test_a_generated_issue_for_an_area_not_in_data_is_dropped_with_a_warning(self):
        write_snapshot(self.snap, [generated("nowhere.i-a11y-x", "nowhere", ["cards.index"])])
        areas, warnings = self.load()
        self.assertEqual(issue_in(areas, "nowhere.i-a11y-x"), (None, None))
        self.assertEqual(len(warnings), 1)
        self.assertIn("nowhere", warnings[0])

    def test_within_an_area_generated_issues_follow_severity_then_screen_count(self):
        write_snapshot(self.snap, [
            generated("cards.i-a11y-low-big", "cards", ["cards.index", "cards.detail-physical", "cards.more-menu"], "low"),
            generated("cards.i-a11y-medium-one", "cards", ["cards.index"], "medium"),
            generated("cards.i-a11y-high-one", "cards", ["cards.detail-virtual"], "high"),
            generated("cards.i-a11y-medium-two", "cards", ["cards.subscriptions", "cards.index"], "medium"),
        ])
        areas, _ = self.load()
        cards = next(a for a in areas if a["area"] == "cards")
        ids = [i["id"] for i in cards["issues"] if ".i-a11y-" in i["id"]]
        self.assertEqual(ids, ["cards.i-a11y-high-one", "cards.i-a11y-medium-two", "cards.i-a11y-medium-one",
                               "cards.i-a11y-low-big"])
        self.assertEqual(issue_in(areas, "cards.i-a11y-medium-two")[1]["nodeIds"][0], "cards.subscriptions")

    def test_the_order_helper_ranks_severity_then_screen_count_then_id(self):
        issues = [{"id": "b", "severity": "low", "nodeIds": ["1", "2", "3"]},
                  {"id": "c", "severity": "medium", "nodeIds": ["1"]},
                  {"id": "a", "severity": "medium", "nodeIds": ["1"]},
                  {"id": "d", "severity": "medium", "nodeIds": ["1", "2"]},
                  {"id": "e", "severity": "high", "nodeIds": ["1"]}]
        self.assertEqual([i["id"] for i in mapdata.order_issues(issues)], ["e", "d", "a", "c", "b"])

    def test_the_viewer_filter_keeps_only_the_viewer_fields_in_their_order(self):
        issue = {"id": "cards.i-a11y-x", "title": "T", "detail": "D", "nodeIds": ["cards.index"], "category": "a11y",
                 "severity": "low", "evidence": "E", "wcag": ["1.4.3"], "copy": {"title": "C"}, "audit": {"x": 1},
                 "history": None, "area": "cards"}
        kept = mapdata.viewer_issue(issue)
        self.assertEqual(list(kept), ["id", "title", "detail", "nodeIds", "category", "severity", "evidence"])
        self.assertEqual(kept["nodeIds"], ["cards.index"])
        _, raw = issue_in(self.raw, TABS)
        self.assertEqual(mapdata.viewer_issue(raw), raw)

    def test_the_environment_variable_selects_the_snapshot(self):
        write_snapshot(self.snap, [generated("home.i-a11y-env", "home", ["home.index"])])
        with mock.patch.dict(os.environ, {"A11Y_SNAPSHOT": self.snap}):
            with contextlib.redirect_stderr(io.StringIO()):
                areas = mapdata.load_areas(DATA)
        self.assertIsNotNone(issue_in(areas, "home.i-a11y-env")[1])
        self.assertEqual(mapdata.load_areas(DATA, snapshot_dir=False), self.raw)

    def test_a_publish_stopped_between_its_renames_is_restored_by_the_loader(self):
        prev = write_snapshot(self.snap + ".prev", [generated("home.i-a11y-prev", "home", ["home.index"])])
        areas, _ = self.load()
        self.assertTrue(os.path.isdir(self.snap))
        self.assertFalse(os.path.exists(prev))
        self.assertIsNotNone(issue_in(areas, "home.i-a11y-prev")[1])


class TestDataBuild(unittest.TestCase):
    """build.py marks the map as test data for tests/ledger.py's snapshot (meta.json testData), and only then."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "site")
        build_site(self.root)

    def build(self, snapshot):
        """(map data, page, report page or "", build.py's NOTE and WARN lines) for a build from snapshot."""
        err, _ = run_build(self.root, {"A11Y_SNAPSHOT": snapshot})

        def read(*path):
            path = os.path.join(self.root, *path)
            if not os.path.exists(path):
                return ""
            with open(path, encoding="utf-8") as f:
                return f.read()
        notes = [ln for ln in err.splitlines() if ln.startswith(("NOTE", "WARN"))]
        return json.loads(read("build", "map.json")), read("dist", "index.html"), read("dist", "a11y", "index.html"), notes

    def test_the_test_snapshot_marks_the_map_data_shows_the_banner_and_prints_a_note(self):
        data, page, report, notes = self.build(SNAPSHOT)
        self.assertIs(data["testData"], True)
        self.assertIn(f'<section class="test-data" aria-label="Test data">{mapdata.TEST_DATA_NOTE}</section>', page)
        self.assertIn(".test-data {", page)
        self.assertIn(mapdata.TEST_DATA_NOTE, report)
        self.assertEqual(notes, [f"NOTE built from test data ({SNAPSHOT}): the a11y findings are invented"])

    def test_a_snapshot_without_test_data_or_no_snapshot_leaves_the_map_unmarked(self):
        real = os.path.join(self.tmp.name, "real")
        shutil.copytree(SNAPSHOT, real)
        with open(os.path.join(real, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertIs(meta.pop("testData"), True)
        with open(os.path.join(real, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f)
        for snapshot in (real, os.path.join(self.tmp.name, "no-snapshot")):
            data, page, report, notes = self.build(snapshot)
            self.assertNotIn("testData", data, snapshot)
            self.assertNotIn("test-data", page, snapshot)
            self.assertNotIn(mapdata.TEST_DATA_NOTE, page + report, snapshot)
            self.assertEqual(notes, [], snapshot)
            if snapshot == real:
                self.assertIn("Accessibility report", report)


if __name__ == "__main__":
    unittest.main()
