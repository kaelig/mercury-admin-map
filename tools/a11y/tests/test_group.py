"""The `group` stage (R12–R15, R17, KTD15–KTD17).

Run: cd tools/a11y && python3 -m unittest tests.test_group -v

Two kinds of input: the committed workspace under tests/data/group/ (raw files, states.json,
judgments, a tiny map and a small data dir), and workspaces built in code for the large cases
(214 and 200 states). Hand-found ids and their screens come from the real data/*.json.
"""
import contextlib
import glob
import io
import json
import os
import re
import shutil
import tempfile
import unittest

from tests.pathshim import A11Y, ROOT

import audit
import group
import signature
from lib import canon
from lib.ws import Workspace

RUN = "2026-10-02-1400"
DATA = os.path.join(ROOT, "data")
FIXTURE = os.path.join(A11Y, "tests", "data", "group")
SHA = re.compile(r"^[0-9a-f]{64}$")


def real_issues(data_dir=DATA):
    """Every a11y issue in a data dir, read without mapdata: {id: issue}."""
    out = {}
    for path in sorted(glob.glob(os.path.join(data_dir, "*.json"))):
        with open(path, encoding="utf-8") as f:
            for issue in json.load(f).get("issues", []):
                if issue.get("category") == "a11y":
                    out[issue["id"]] = issue
    return out


HAND = real_issues()


def occ(tag="button", role="button", name="", classes=(), ancestors=(), landmark=None, heading="", data=None,
        xpath="/html/body/div[2]/main/button[1]", rect=(10, 10, 24, 24), layer="document"):
    return {"tag": tag, "role": role, "name": name, "classes": list(classes),
            "ancestors": [list(a) for a in ancestors], "landmark": landmark or {"role": "main", "name": ""},
            "heading": heading, "data": data or {}, "xpath": xpath, "rect": list(rect), "layer": layer}


def result(rule, o, check="axe", basis="violation", criteria=("1.4.3",), evidence=None):
    return {"check": check, "rule": rule, "basis": basis, "criteria": list(criteria), "messageKey": None,
            "occurrence": o, "evidence": evidence or {}}


def contrast(fg, bg, ratio, o):
    return result("color-contrast", o, evidence={
        "fgColor": fg, "bgColor": bg, "contrastRatio": ratio, "fontSize": "10.5pt (14px)", "fontWeight": "normal",
        "expectedContrastRatio": "4.5:1",
        "failureSummary": f"Fix any of the following:\n  Element has insufficient color contrast of {ratio}"})


def muted(i=0, ancestors=(("_cell_tb001_8",),)):
    return occ(tag="span", role="", name="Pending", classes=["_muted_ab12c_3"], ancestors=ancestors,
               xpath=f"/html/body/div/table/tr[{i + 1}]/td/span")


ICON = ["_iconButton_btn01_5", "fa", "fa-ellipsis", "svg-inline--fa"]
ROW_ACTIONS = [["_rowActions_tok22_14"], ["_tokensTable_tok22_2"], ["_settingsPage_set99_1"]]
HEADER = [["_header_web33_2"], ["_settingsPage_set99_1"]]


def button_name(ancestors, heading="API Tokens", i=0):
    return result("button-name", occ(classes=ICON, ancestors=ancestors, heading=heading,
                                     xpath=f"/html/body/div/main/button[{i + 1}]"),
                  criteria=["4.1.2"], evidence={"failureSummary": "Fix any of the following:\n  Element does not "
                                                                  "have inner text that is visible to screen readers"})


def raw_doc(sid, kind, results=(), records=(), layer=None, checks=None, status="audited"):
    return {"schema": 1, "state": sid, "kind": kind, "status": status,
            "reason": None if status == "audited" else "no-path", "reasonDetail": None,
            "harness": "0000000000000000", "recipeHash": "0" * 64, "build": "fixture",
            "env": {"viewport": [1440, 900], "dpr": 1}, "url": "/x",
            "layer": layer or {"scope": "document", "role": "document", "name": "", "xpath": "/html"},
            "frames": [], "families": {"completed": ["axe", "records"], "incomplete": []},
            "results": list(results), "records": list(records), "checks": checks or {}}


def run_group(ws_dir, map_path, data_dir):
    """Run `audit.py group`; return (exit code, stdout, stderr, findings doc or None)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = audit.main(["group", "--workspace", ws_dir, "--map", map_path, "--data", data_dir, "--run", RUN])
    doc = canon.read_json(os.path.join(ws_dir, "runs", RUN, "findings.json"))
    return code, out.getvalue(), err.getvalue(), doc


def scanner(doc):
    return [f for f in doc["findings"] if f["source"] == "scanner"]


def by_rule(doc, rule):
    return [f for f in doc["findings"] if f["rule"] == rule]


class Scratch:
    """A temp workspace built in code: states with paths of a given length, raw files and a map."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.join(self.tmp.name, "ws")
        self.map_path = os.path.join(self.tmp.name, "map.json")
        self.states, self.raws, self.titles = {}, {}, {}

    def state(self, sid, kind="page", hops=1, area=None, layer=None, title=None):
        area = area or sid.split(".")[0]
        path = [f"{area}.hop{i}" for i in range(hops)] + [sid]
        self.states[sid] = {"area": area, "kind": kind, "url": "/x", "root": hops == 0, "rootUrl": None,
                            "needsRecipe": hops > 0, "parent": path[-2] if hops else None, "path": path,
                            "labels": ["Click"] * hops, "mutates": False, "source": "x", "unreachable": None}
        self.raws[sid] = raw_doc(sid, kind, layer=layer)
        self.titles[sid] = title or sid
        return self

    def add(self, sid, *results):
        self.raws[sid]["results"].extend(results)
        return self

    def run(self, data=DATA):
        canon.write_json(os.path.join(self.ws, "runs", RUN, "states.json"),
                         {"run": RUN, "map": {"nodes": len(self.states), "sha": "x"}, "roots": [],
                          "states": self.states})
        for sid, doc in self.raws.items():
            canon.write_json(os.path.join(self.ws, "runs", RUN, "raw", f"{sid}.json"), doc)
        areas = {}
        for sid, s in self.states.items():
            areas.setdefault(s["area"], []).append({"id": sid, "title": self.titles[sid], "kind": s["kind"]})
        canon.write_json(self.map_path, {"areas": [{"area": a, "nodes": n} for a, n in sorted(areas.items())]})
        return run_group(self.ws, self.map_path, data)

    def close(self):
        self.tmp.cleanup()


class ScratchTest(unittest.TestCase):
    def scratch(self):
        s = Scratch()
        self.addCleanup(s.close)
        return s


# ---------------------------------------------------------------------------
# Grouping (R13, KTD15)
# ---------------------------------------------------------------------------

class Grouping(ScratchTest):
    def test_ae1_one_component_on_214_states_is_one_finding(self):
        s = self.scratch()
        ids = [f"t.s{i:03d}" for i in range(214)]
        for i, sid in enumerate(ids):
            s.state(sid, hops=1 + i % 3, title="Transactions")
            s.add(sid, contrast("#9d9da8", "#ffffff", 2.85, muted(i, ancestors=[[f"_cell{i % 5}_tb001_8"]])))
        s.add("t.s007", contrast("#6b6b75", "#ffffff", 4.2, muted(0)))
        code, out, err, doc = s.run()
        self.assertEqual(code, 0, err)
        found = [f for f in scanner(doc) if f["family"] == "contrast"]
        self.assertEqual(len(found), 2)
        big = max(found, key=lambda f: len(f["states"]))
        self.assertEqual(big["states"], ids)
        self.assertEqual(big["occurrences"], [{"state": sid, "index": 0} for sid in ids])
        self.assertRegex(big["key"], r"^contrast:color-contrast:[0-9a-f]{12}$")
        self.assertEqual(big["rule"], "color-contrast")
        self.assertEqual(big["criteria"], ["1.4.3"])
        self.assertEqual(big["basis"], "violation")
        self.assertEqual(big["severityDefault"], "medium")
        self.assertEqual(big["primary"], "t.s000")
        self.assertEqual(big["title"], 'Low contrast: text "Pending"')
        self.assertEqual(big["summary"], "axe color-contrast: #9d9da8 on #ffffff measures 2.85:1; AA needs 4.5:1.")
        self.assertEqual(big["facts"], {
            "element": 'text "Pending"', "screen": "Transactions", "screens": 214, "criterion": "1.4.3",
            "criterionName": "Contrast (Minimum)", "rule": "color-contrast", "fg": "#9d9da8", "bg": "#ffffff",
            "ratio": "2.85", "required": "4.5"})
        self.assertIsNone(big["issueId"])
        self.assertIsNone(big["recordSig"])
        self.assertRegex(big["evidenceHash"], SHA)
        small = min(found, key=lambda f: len(f["states"]))
        self.assertEqual(small["occurrences"], [{"state": "t.s007", "index": 1}])
        self.assertIn("color-contrast: 215 nodes, 2 groups", out)

    def test_two_unnamed_icon_buttons_at_two_call_sites_are_two_findings(self):
        s = self.scratch()
        s.state("a.one").state("a.two").state("b.three")
        s.add("a.one", button_name(ROW_ACTIONS))
        s.add("a.two", button_name(HEADER, heading="Webhooks"))
        s.add("b.three", button_name(ROW_ACTIONS))
        code, out, err, doc = s.run()
        self.assertEqual(code, 0, err)
        found = sorted((f["states"] for f in by_rule(doc, "button-name")))
        self.assertEqual(found, [["a.one", "b.three"], ["a.two"]])
        row = next(f for f in by_rule(doc, "button-name") if f["states"] == ["a.one", "b.three"])
        self.assertEqual(row["title"], 'Missing or unclear name: button in "API Tokens"')
        self.assertEqual(row["family"], "name")
        self.assertEqual(row["criteria"], ["4.1.2"])
        self.assertEqual(row["facts"]["axeSummary"],
                         "Element does not have inner text that is visible to screen readers")
        self.assertEqual(row["facts"]["announced"], 'button ""')
        self.assertIn("button-name: 3 nodes, 2 groups", out)

    def test_criteria_outside_wcag_22_aa_are_dropped(self):
        s = self.scratch()
        s.state("a.one").add("a.one", result("scrollable-region-focusable",
                                             occ(tag="div", role="", classes=["_scroller_scr11_1"]),
                                             criteria=["2.1.1", "2.1.3"]))
        code, _, err, doc = s.run()
        self.assertEqual(code, 0, err)
        (f,) = by_rule(doc, "scrollable-region-focusable")
        self.assertEqual((f["family"], f["criteria"]), ("keyboard", ["2.1.1"]))
        self.assertEqual(f["title"], "Can't scroll by keyboard: text in the main landmark")

    def test_default_severity_uses_the_primary_state_kind(self):
        s = self.scratch()
        layer = {"scope": "top", "role": "menu", "name": "", "xpath": "/html/body/div[3]"}
        s.state("a.menu", kind="menu", layer=layer)
        s.state("a.modal", kind="modal", layer={"scope": "top", "role": "dialog", "name": "Edit card limits",
                                                "xpath": "/html/body/div[4]"})
        not_returned = dict(check="dialog", basis="failure", criteria=["2.4.3"], evidence={"focus": None})
        s.add("a.menu", result("dialog-focus-not-returned", occ(name="More", classes=["_kebab_kbb11_1"]),
                               **not_returned))
        s.add("a.modal", result("dialog-focus-not-returned", occ(name="Edit limits", classes=["_button_btn01_5"]),
                                **not_returned))
        code, _, err, doc = s.run()
        self.assertEqual(code, 0, err)
        sev = {f["primary"]: f["severityDefault"] for f in by_rule(doc, "dialog-focus-not-returned")}
        self.assertEqual(sev, {"a.menu": "low", "a.modal": "medium"})
        titles = {f["primary"]: f["title"] for f in by_rule(doc, "dialog-focus-not-returned")}
        self.assertEqual(titles, {"a.menu": "Focus lost after closing unnamed menu",
                                  "a.modal": 'Focus lost after closing dialog "Edit card limits"'})
        modal = next(f for f in by_rule(doc, "dialog-focus-not-returned") if f["primary"] == "a.modal")
        self.assertEqual(modal["facts"]["focus"], "page body")

    def test_a_group_whose_members_disagree_is_flagged(self):
        s = self.scratch()
        for i in range(10):
            sid = f"a.s{i}"
            s.state(sid)
            chip = "_chip_bbbbb_1" if i < 2 else "_chip_aaaaa_1"      # 8 of 10 agree
            pill = "_pill_bbbbb_1" if i < 1 else "_pill_aaaaa_1"      # 9 of 10 agree
            s.add(sid, result("target-size", occ(classes=[chip], rect=(0, 0, 16, 16)), criteria=["2.5.8"]),
                  result("target-size", occ(classes=[pill], rect=(0, 0, 16, 16)), criteria=["2.5.8"]))
        code, out, err, doc = s.run()
        self.assertEqual(code, 0, err)
        groups = by_rule(doc, "target-size")
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(len(f["states"]) == 10 for f in groups))
        chip = next(f for f in groups if f["occurrences"][0]["index"] == 0)
        self.assertEqual(doc["flags"], [{"key": chip["key"], "message": "tags and attributes agree 80% of the time"}])
        self.assertIn(chip["key"], out)
        self.assertEqual(chip["facts"]["size"], "16×16")


class PrimaryAndSample(ScratchTest):
    def contrast_on(self, s, sid, ancestors):
        s.add(sid, contrast("#9d9da8", "#ffffff", 2.85, muted(ancestors=ancestors)))

    def test_a_page_beats_an_overlay_then_fewest_hops_and_the_sample_differs_most(self):
        s = self.scratch()
        same = [["_cell_tb001_8"]]
        for sid, kind, hops, area in (("a.page-far", "page", 3, "a"), ("a.page-near", "page", 2, "a"),
                                      ("a.page-twin", "page", 2, "a"), ("a.modal", "modal", 1, "a"),
                                      ("b.drawer", "drawer", 2, "b")):
            s.state(sid, kind=kind, hops=hops, area=area)
            self.contrast_on(s, sid, same)
        code, _, err, doc = s.run()
        self.assertEqual(code, 0, err)
        (f,) = scanner(doc)
        self.assertEqual(f["primary"], "a.page-near")
        self.assertEqual(f["sample"], ["a.page-near", "b.drawer"])

    def test_kind_outranks_class_distance(self):
        s = self.scratch()
        near, far = [["_cell_tb001_8"], ["_table_tb001_1"]], [["_widget_hom22_6"], ["_dashboard_hom22_1"]]
        for sid, kind, hops, anc in (("a.p1", "page", 1, near), ("a.p2", "page", 1, far),
                                     ("a.p3", "page", 2, near), ("a.m", "modal", 1, near)):
            s.state(sid, kind=kind, hops=hops)
            self.contrast_on(s, sid, anc)
        _, _, _, doc = s.run()
        (f,) = scanner(doc)
        self.assertEqual(f["sample"], ["a.p1", "a.m"])

    def test_class_distance_breaks_the_tie(self):
        s = self.scratch()
        near, far = [["_cell_tb001_8"], ["_table_tb001_1"]], [["_widget_hom22_6"], ["_dashboard_hom22_1"]]
        for sid, hops, anc in (("a.p1", 1, near), ("a.p2", 2, far), ("a.p3", 1, near)):
            s.state(sid, hops=hops)
            self.contrast_on(s, sid, anc)
        _, _, _, doc = s.run()
        (f,) = scanner(doc)
        self.assertEqual(f["sample"], ["a.p1", "a.p2"])

    def test_one_state_samples_only_itself(self):
        s = self.scratch()
        s.state("a.only")
        self.contrast_on(s, "a.only", [])
        _, _, _, doc = s.run()
        self.assertEqual(scanner(doc)[0]["sample"], ["a.only"])


# ---------------------------------------------------------------------------
# axe's incomplete results: one finding per rule and messageKey (R8, KTD12)
# ---------------------------------------------------------------------------

def incomplete(rule, o, message_key, criteria=("4.1.2",), evidence=None):
    r = result(rule, o, basis="incomplete", criteria=criteria, evidence=evidence or {
        "failureSummary": "Fix any of the following:\n  Unable to determine if aria-controls referenced ID exists"})
    r["messageKey"] = message_key
    return r


WORDS = ["Account", "Card", "Payee", "Invoice", "Transfer", "Statement", "Category", "Recipient"]


def popup_trigger(i):
    """48 different shared elements: their own module classes, names, call sites, tags and roles."""
    tag, role = (("button", "button"), ("a", "link"), ("div", "combobox"))[i % 3]
    return occ(tag=tag, role=role, name=f"{WORDS[i % 8]} menu {'x' * (i // 8)}",
               classes=[f"_trigger{i}_mn{i:03d}_1"], ancestors=[[f"_toolbar{i}_tb{i:03d}_2"]],
               heading=f"{WORDS[i % 8]}s", xpath=f"/html/body/div/main/div[{i + 1}]/button")


def named_anywhere(f, occs):
    """Element names and CSS local class names of occs that appear in a finding's title, summary or facts."""
    text = " ".join([f["title"], f["summary"]] + [str(v) for v in f["facts"].values()])
    out = []
    for o in occs:
        out += [n for n in [signature.template(o["name"])] + signature.local_classes(o["classes"]) if n in text]
    return out


class Incomplete(ScratchTest):
    def test_48_controls_within_popup_results_on_different_elements_and_states_are_one_finding(self):
        s = self.scratch()
        ids = [f"a.s{i:02d}" for i in range(20)] + [f"b.s{i:02d}" for i in range(10)]
        for sid in ids:
            kind = {"a.s00": "menu", "b.s03": "drawer"}.get(sid, "page")
            s.state(sid, kind=kind, hops={"a.s00": 0, "a.s05": 1}.get(sid, 2),
                    title="Accounts — Overview" if sid == "a.s05" else None)
        triggers = [popup_trigger(i) for i in range(48)]
        for i, o in enumerate(triggers):
            s.add(ids[i % 30], incomplete("aria-valid-attr-value", o, "controlsWithinPopup"))
        self.assertEqual(len({signature.finding_key("role-state", "aria-valid-attr-value", "content", o)
                              for o in triggers}), 48)
        code, out, err, doc = s.run()
        self.assertEqual(code, 0, err)
        (f,) = by_rule(doc, "aria-valid-attr-value")
        self.assertEqual(f["key"], signature.incomplete_key("role-state", "aria-valid-attr-value", "controlsWithinPopup"))
        self.assertEqual((f["family"], f["basis"], f["criteria"], f["severityDefault"]),
                         ("role-state", "incomplete", ["4.1.2"], "medium"))
        self.assertEqual(f["states"], sorted(ids))
        self.assertEqual(len(f["occurrences"]), 48)
        self.assertEqual(f["primary"], "a.s05", "a page first, then the fewest hops")
        self.assertEqual(f["sample"], ["a.s05", "b.s03"], "then the state least like it")
        self.assertEqual(f["title"], "Needs review: aria-valid-attr-value, aria-controls names a closed popup")
        self.assertLessEqual(len(f["title"]), 80)
        self.assertEqual(f["summary"], "axe aria-valid-attr-value could not decide 48 elements (controlsWithinPopup): "
                                       "aria-controls names a closed popup.")
        self.assertEqual(f["facts"], {
            "rule": "aria-valid-attr-value", "messageKey": "controlsWithinPopup",
            "why": "aria-controls names a closed popup", "elements": "48 elements", "screen": "Accounts — Overview",
            "criterion": "4.1.2", "criterionName": "Name, Role, Value"})
        self.assertEqual(named_anywhere(f, triggers), [], "no single element stands for the group")
        self.assertIn("aria-valid-attr-value: 48 nodes, 1 group", out)
        self.assertEqual(doc["flags"], [], "an incomplete group's members differ by design")

    def test_two_message_keys_of_one_rule_are_two_findings_and_no_key_is_a_third(self):
        s = self.scratch()
        s.state("a.one").state("a.two")
        texts = [occ(tag="span", role="", name="Balance", classes=[f"_label{i}_lb{i:03d}_3"],
                     xpath=f"/html/body/div/span[{i + 1}]") for i in range(6)]
        for o, (sid, mk) in zip(texts, [("a.one", "bgOverlap"), ("a.two", "bgOverlap"), ("a.two", "bgOverlap"),
                                        ("a.one", "bgImage"), ("a.two", "bgImage"), ("a.one", None)]):
            s.add(sid, incomplete("color-contrast", o, mk, criteria=["1.4.3"],
                                  evidence={"fgColor": "#ffffff", "bgColor": None, "contrastRatio": 0}))
        code, _, err, doc = s.run()
        self.assertEqual(code, 0, err)
        got = {f["key"]: (f["title"], f["facts"]["elements"], f["facts"]["messageKey"], len(f["occurrences"]))
               for f in by_rule(doc, "color-contrast")}
        key = lambda mk: signature.incomplete_key("contrast", "color-contrast", mk)  # noqa: E731
        self.assertEqual(got, {
            key("bgOverlap"): ("Needs review: color-contrast, an element overlaps its background", "3 elements",
                               "bgOverlap", 3),
            key("bgImage"): ("Needs review: color-contrast, it sits on a background image", "2 elements", "bgImage", 2),
            key(None): ("Needs review: color-contrast, axe gave no reason", "1 element", "none", 1)})

    def test_a_violation_of_the_same_rule_on_the_same_element_stays_its_own_finding(self):
        s = self.scratch()
        s.state("a.one").state("a.two").state("b.three")
        e, other = popup_trigger(0), popup_trigger(1)
        violation = dict(criteria=["4.1.2"], evidence={"failureSummary": "Fix all of the following:\n  Invalid ARIA "
                                                                          "attribute value: aria-controls=\"menu-1\""})
        s.add("a.one", incomplete("aria-valid-attr-value", e, "controlsWithinPopup"),
              result("aria-valid-attr-value", e, **violation))
        s.add("a.two", incomplete("aria-valid-attr-value", other, "controlsWithinPopup"),
              result("aria-valid-attr-value", other, **violation))
        s.add("b.three", result("aria-valid-attr-value", e, **violation))
        code, out, err, doc = s.run()
        self.assertEqual(code, 0, err)
        found = {f["key"]: f for f in by_rule(doc, "aria-valid-attr-value")}
        self.assertEqual(len(found), 3)
        pending = found[signature.incomplete_key("role-state", "aria-valid-attr-value", "controlsWithinPopup")]
        self.assertEqual((pending["states"], pending["facts"]["elements"]), (["a.one", "a.two"], "2 elements"))
        mine = found[signature.finding_key("role-state", "aria-valid-attr-value", "content", e)]
        self.assertEqual((mine["basis"], mine["states"]), ("violation", ["a.one", "b.three"]))
        self.assertEqual(mine["title"], 'Wrong role or state: button "Account menu"')
        theirs = found[signature.finding_key("role-state", "aria-valid-attr-value", "content", other)]
        self.assertEqual((theirs["basis"], theirs["states"]), ("violation", ["a.two"]))
        self.assertIn("aria-valid-attr-value: 5 nodes, 3 groups", out)

    def test_an_unknown_long_message_key_on_the_longest_rule_id_keeps_the_title_within_80(self):
        s = self.scratch()
        s.state("a.one")
        mk = "focusableDescendantInsideAShadowRootSlot"
        s.add("a.one", incomplete("scrollable-region-focusable", occ(tag="div", role="", classes=["_scroller_scr11_1"]),
                                  mk, criteria=["2.1.1", "2.1.3"]))
        code, _, err, doc = s.run()
        self.assertEqual(code, 0, err)
        (f,) = by_rule(doc, "scrollable-region-focusable")
        self.assertEqual(f["facts"]["why"], mk[:35] + "…", "an unknown key reads as itself, cut to fit")
        self.assertEqual(f["title"], f"Needs review: scrollable-region-focusable, {mk[:35]}…")
        self.assertLessEqual(len(f["title"]), 80)
        self.assertNotIn("scroller", f["title"])


# ---------------------------------------------------------------------------
# Hand-found issues and attachment proposals (R17, KTD17)
# ---------------------------------------------------------------------------

class Proposals(ScratchTest):
    def test_ae5_row_menu_buttons_attach_to_the_api_tokens_issue(self):
        hand = HAND["settings.i-unnamed-row-buttons"]
        self.assertEqual(len(hand["nodeIds"]), 2)
        tokens = hand["nodeIds"][0]
        s = self.scratch()
        s.state(tokens).state("settings.webhooks")
        for sid in (tokens, "settings.webhooks"):
            s.add(sid, button_name(ROW_ACTIONS))
        # Covers both screens, but a target-size finding is not the same family as the hand-found one.
        for sid in hand["nodeIds"]:
            if sid not in s.states:
                s.state(sid)
            s.add(sid, result("target-size", occ(classes=ICON, rect=(0, 0, 16, 16)), criteria=["2.5.8"]))
        code, _, err, doc = s.run()
        self.assertEqual(code, 0, err)
        (row,) = by_rule(doc, "button-name")
        mine = [p for p in doc["proposals"] if p["handFound"] == "settings.i-unnamed-row-buttons"]
        self.assertEqual(mine, [{"id": canon.sha([row["key"], "settings.i-unnamed-row-buttons"]),
                                 "finding": row["key"], "handFound": "settings.i-unnamed-row-buttons",
                                 "overlap": 0.5, "family": "name"}])

    def test_a_finding_on_200_page_states_covers_the_4_home_chip_screens(self):
        hand = HAND["home.i-chips-as-menuitems"]
        self.assertEqual(len(hand["nodeIds"]), 4)
        s = self.scratch()
        ids = list(hand["nodeIds"]) + [f"home.p{i:03d}" for i in range(196)]
        chip = occ(role="menuitem", name="All", classes=["_chip_chp11_2"], ancestors=[["_filters_hom22_9"]])
        for sid in ids:
            s.state(sid).add(sid, result("aria-required-parent", chip, criteria=["1.3.1"]))
        code, _, err, doc = s.run()
        self.assertEqual(code, 0, err)
        (f,) = by_rule(doc, "aria-required-parent")
        self.assertEqual(len(f["states"]), 200)
        self.assertEqual(f["family"], "role-state")
        self.assertEqual([(p["handFound"], p["overlap"]) for p in doc["proposals"]],
                         [("home.i-chips-as-menuitems", 1)])

    def test_one_finding_is_proposed_for_two_hand_found_issues(self):
        inv, bill = HAND["invoicing.i-unlabeled-controls"], HAND["billpay.i-unlabeled-controls"]
        states = [inv["nodeIds"][0], bill["nodeIds"][0], bill["nodeIds"][1]]
        s = self.scratch()
        trash = occ(classes=["_iconButton_btn01_5"], ancestors=[["_rowActions_tbl77_3"]])
        for sid in states:
            s.state(sid).add(sid, result("button-name", trash, criteria=["4.1.2"]))
        code, _, err, doc = s.run()
        self.assertEqual(code, 0, err)
        (f,) = by_rule(doc, "button-name")
        got = sorted((p["finding"], p["handFound"], p["overlap"]) for p in doc["proposals"])
        self.assertEqual(got, [(f["key"], "billpay.i-unlabeled-controls", 0.6667),
                               (f["key"], "invoicing.i-unlabeled-controls", 0.5)])

    def test_less_than_half_of_the_screens_is_not_proposed(self):
        bill = HAND["billpay.i-unlabeled-controls"]
        s = self.scratch()
        sid = bill["nodeIds"][0]
        s.state(sid).add(sid, result("button-name", occ(classes=["_iconButton_btn01_5"]), criteria=["4.1.2"]))
        _, _, _, doc = s.run()
        self.assertEqual(doc["proposals"], [])

    def test_hand_found_findings_carry_the_raw_issue(self):
        s = self.scratch()
        s.state("settings.tokens", hops=1).state("settings.token-menu", kind="menu", hops=2)
        code, out, err, doc = s.run()
        self.assertEqual(code, 0, err)
        hand = {f["issueId"]: f for f in doc["findings"] if f["source"] == "hand-found"}
        self.assertEqual(sorted(hand), sorted(HAND))
        f = hand["settings.i-unnamed-row-buttons"]
        raw = HAND["settings.i-unnamed-row-buttons"]
        self.assertEqual(f["key"], "hand:settings.i-unnamed-row-buttons")
        self.assertEqual(f["family"], "name")
        self.assertEqual(f["criteria"], ["4.1.2"])
        self.assertEqual(f["severityDefault"], raw["severity"])
        self.assertEqual(f["title"], raw["title"])
        self.assertEqual(f["summary"], raw["evidence"])
        self.assertEqual(f["states"], sorted(raw["nodeIds"]))
        self.assertEqual(f["occurrences"], [])
        self.assertEqual(f["primary"], "settings.tokens")
        self.assertEqual(f["sample"], ["settings.tokens", "settings.token-menu"])
        self.assertRegex(f["evidenceHash"], SHA)
        self.assertIn("hand-found: 17 issues", out)

    def test_a_hand_found_issue_missing_from_hand_found_json_fails_with_its_id(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data = os.path.join(tmp.name, "data")
        shutil.copytree(DATA, data)
        path = os.path.join(data, "home.json")
        area = canon.read_json(path)
        area["issues"].append({"id": "home.i-new-unmapped", "title": "New", "detail": "", "nodeIds": ["home.index"],
                               "category": "a11y", "severity": "low", "evidence": "x"})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(area, f)
        s = self.scratch()
        s.state("home.index")
        code, _, err, doc = s.run(data=data)
        self.assertNotEqual(code, 0)
        self.assertIn("home.i-new-unmapped", err)
        self.assertIsNone(doc)

    def test_outcomes_name_the_audited_states_and_a_missing_raw_file_fails(self):
        s = self.scratch()
        s.state("a.one").state("a.two")
        canon.write_json(os.path.join(s.ws, "runs", RUN, "outcomes.json"), {"run": RUN, "states": {
            "a.one": {"outcome": "audited"}, "a.two": {"outcome": "audited"}, "a.gone": {"outcome": "audited"}}})
        code, _, err, doc = s.run()
        self.assertNotEqual(code, 0)
        self.assertIn("a.gone", err)
        self.assertIsNone(doc)


# ---------------------------------------------------------------------------
# The committed fixture workspace
# ---------------------------------------------------------------------------

class Fixture(unittest.TestCase):
    """tests/data/group/: 11 states over cards, home and settings, judged records, a map that already
    holds published a11y issues and overlays, and a data dir with two hand-found issues."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.join(self.tmp.name, "ws")
        shutil.copytree(os.path.join(FIXTURE, "ws"), self.ws)
        self.map_path = os.path.join(FIXTURE, "map.json")
        self.data = os.path.join(FIXTURE, "data")
        self.raw_dir = os.path.join(self.ws, "runs", RUN, "raw")
        # judgments.jsonl names records as "@<state>#<index>"; a judge would have written their signatures.
        path = os.path.join(self.ws, "runs", RUN, "judgments.jsonl")
        lines = canon.read_jsonl(path)
        for j in lines:
            j["sig"] = self.sig_of(j["sig"])
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(canon.canonical(j) + "\n" for j in lines)

    def tearDown(self):
        self.tmp.cleanup()

    def raw(self, sid):
        return canon.read_json(os.path.join(self.raw_dir, f"{sid}.json"))

    def sig_of(self, ref):
        sid, index = ref[1:].split("#")
        return signature.record_sig(self.raw(sid)["records"][int(index)])

    def run_group(self, data=None):
        return run_group(self.ws, self.map_path, data or self.data)

    def test_findings(self):
        code, out, err, doc = self.run_group()
        self.assertEqual(code, 0, err)
        self.assertEqual(doc["run"], RUN)
        got = sorted((f["source"], f["family"], f["rule"], tuple(f["states"])) for f in doc["findings"])
        self.assertEqual(got, [
            ("hand-found", "name", "hand-found", ("settings.token-menu", "settings.tokens")),
            ("hand-found", "role-state", "hand-found",
             ("home.accounts-kebab", "home.avatar-menu", "home.index-transactions", "home.tasks")),
            ("scanner", "contrast", "color-contrast", ("cards.edit-limit-modal", "cards.index", "home.index",
                                                       "home.index-transactions", "settings.tokens")),
            ("scanner", "dialog-focus", "dialog-focus-not-moved", ("cards.edit-limit-modal",)),
            ("scanner", "name", "button-name", ("settings.tokens",)),
            ("scanner", "name", "button-name", ("settings.webhooks",)),
            ("scanner", "role-state", "announced-as", ("home.index-transactions", "home.tasks")),
        ])
        self.assertEqual([f["key"] for f in doc["findings"]], sorted(f["key"] for f in doc["findings"]))
        for f in scanner(doc):
            if f["rule"] != "announced-as":
                for o in f["occurrences"]:
                    self.assertEqual(self.raw(o["state"])["results"][o["index"]]["rule"], f["rule"], o)
        rows = next(f for f in by_rule(doc, "button-name") if f["states"] == ["settings.tokens"])
        self.assertEqual(len(rows["occurrences"]), 2)

    def test_primary_and_sample_in_the_fixture(self):
        _, _, _, doc = self.run_group()
        (muted_text,) = [f for f in scanner(doc) if f["family"] == "contrast"]
        self.assertEqual(muted_text["primary"], "cards.index")
        self.assertEqual(muted_text["sample"], ["cards.index", "settings.tokens"])
        self.assertEqual(muted_text["facts"]["screen"], "Cards")
        (focus,) = by_rule(doc, "dialog-focus-not-moved")
        self.assertEqual(focus["title"], 'Focus not moved into dialog "Edit card limits"')
        self.assertEqual(focus["facts"]["element"], 'button "Edit limits"')
        self.assertEqual(focus["facts"]["focus"], "page body")
        self.assertEqual(focus["facts"]["screen"], "Cards — Edit card limits")

    def test_judged_records_become_findings_or_advisory(self):
        _, _, _, doc = self.run_group()
        (f,) = by_rule(doc, "announced-as")
        chip_sig = self.sig_of("@home.tasks#0")
        self.assertEqual(f["recordSig"], chip_sig)
        self.assertEqual(f["criteria"], ["4.1.2"])
        self.assertEqual(f["basis"], "candidate")
        self.assertRegex(f["key"], r"^role-state:announced-as:[0-9a-f]{12}$")
        for o in f["occurrences"]:
            self.assertEqual(signature.record_sig(self.raw(o["state"])["records"][o["index"]]), chip_sig)
        self.assertEqual(f["facts"]["announced"], 'menuitem "All" [selected=false]')
        self.assertEqual(f["title"], 'Wrong role or state: menuitem "All"')
        self.assertEqual(doc["advisory"], [{"sig": self.sig_of("@home.index#0"),
                                            "template": 'heading "<amount>" [level=2]',
                                            "reason": "Announced as a bare amount with no label.", "states": 2}])
        ok_sig = self.sig_of("@home.index#1")
        self.assertNotIn(ok_sig, json.dumps(doc))

    def test_proposals_in_the_fixture(self):
        _, _, _, doc = self.run_group()
        rows = next(f for f in by_rule(doc, "button-name") if f["states"] == ["settings.tokens"])
        (chips,) = by_rule(doc, "announced-as")
        self.assertEqual(sorted((p["finding"], p["handFound"], p["overlap"], p["family"]) for p in doc["proposals"]),
                         sorted([(rows["key"], "settings.i-unnamed-row-buttons", 0.5, "name"),
                                 (chips["key"], "home.i-chips-as-menuitems", 0.5, "role-state")]))

    def test_issues_come_from_the_data_dir_never_from_the_map(self):
        with open(self.map_path, encoding="utf-8") as f:
            map_doc = json.load(f)
        in_map = {i["id"]: i for a in map_doc["areas"] for i in a.get("issues", [])}
        generated = "settings.i-a11y-name-button-in-api-tokens"
        self.assertEqual(in_map[generated]["category"], "a11y")
        self.assertNotEqual(in_map["settings.i-unnamed-row-buttons"]["evidence"],
                            HAND["settings.i-unnamed-row-buttons"]["evidence"])

        # First run: learn the row-button finding's key, then publish it under the generated id.
        _, _, _, doc = self.run_group(data=DATA)
        rows = next(f for f in by_rule(doc, "button-name") if f["states"] == ["settings.tokens"])
        canon.write_json(os.path.join(self.ws, "published", "registry.json"), {
            "keys": {rows["key"]: generated}, "held": [generated], "tombstones": {}, "attachments": []})

        code, _, err, doc = self.run_group(data=DATA)
        self.assertEqual(code, 0, err)
        hand = {f["issueId"]: f for f in doc["findings"] if f["source"] == "hand-found"}
        self.assertEqual(sorted(hand), sorted(HAND))
        self.assertEqual(len(hand), 17)
        for iid, f in hand.items():
            self.assertEqual(f["summary"], HAND[iid]["evidence"], iid)
            self.assertEqual(f["severityDefault"], HAND[iid]["severity"], iid)
            self.assertEqual(f["states"], sorted(HAND[iid]["nodeIds"]), iid)
        rows = next(f for f in by_rule(doc, "button-name") if f["states"] == ["settings.tokens"])
        self.assertEqual(rows["issueId"], generated)
        self.assertNotIn(generated, [p["handFound"] for p in doc["proposals"]])
        for p in doc["proposals"]:
            self.assertNotEqual(p["handFound"], next(f["issueId"] for f in doc["findings"] if f["key"] == p["finding"]))

    def test_a_confirmed_attachment_is_not_proposed_again(self):
        _, _, _, doc = self.run_group()
        rows = next(f for f in by_rule(doc, "button-name") if f["states"] == ["settings.tokens"])
        canon.write_json(os.path.join(self.ws, "published", "registry.json"), {
            "keys": {}, "held": [], "tombstones": {},
            "attachments": [{"finding": rows["key"], "handFound": "settings.i-unnamed-row-buttons", "run": "x"}]})
        _, _, _, doc = self.run_group()
        self.assertEqual([p["handFound"] for p in doc["proposals"]], ["home.i-chips-as-menuitems"])

    def test_running_twice_gives_byte_identical_findings(self):
        path = os.path.join(self.ws, "runs", RUN, "findings.json")
        self.run_group()
        with open(path, "rb") as f:
            first = f.read()
        self.run_group()
        with open(path, "rb") as f:
            second = f.read()
        self.assertEqual(first, second)
        self.assertEqual(first.decode(), canon.dump(json.loads(first)))

    def test_status(self):
        ws = Workspace(workspace=self.ws, data=self.data, map=self.map_path, run=RUN)
        self.assertEqual(group.status(ws, RUN), {"pending": True})
        self.run_group()
        self.assertEqual(group.status(ws, RUN), {"findings": 7, "scanner": 5, "handFound": 2, "proposals": 2,
                                                 "advisory": 1, "flags": 0})


if __name__ == "__main__":
    unittest.main()
