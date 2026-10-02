"""Checks wcag.json, checks.json and hand-found.json against WCAG 2.2, the installed axe-core,
schemas.md and the hand-found issues in data/*.json (KTD13, KTD14, KTD15, KTD17)."""
import glob
import json
import os
import re
import string
import subprocess
import unittest

from . import pathshim

A11Y = pathshim.A11Y
ROOT = pathshim.ROOT


def load(name):
    with open(os.path.join(A11Y, name), encoding="utf-8") as f:
        return json.load(f)


def number_key(n):
    return tuple(int(p) for p in n.split("."))


# The tags the sweep runs axe under, plus the two rules it adds by id (R3, R11, KTD13).
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]
AXE_EXTRA = ["label-content-name-mismatch", "target-size"]
LEVEL_TAGS = {"wcag2a": "A", "wcag21a": "A", "wcag22a": "A", "wcag2aa": "AA", "wcag21aa": "AA", "wcag22aa": "AA"}

AXE_SCRIPT = r"""
const axe = require(process.argv[1]);
const tags = JSON.parse(process.argv[2]);
const extra = JSON.parse(process.argv[3]);
const rules = axe.getRules().filter(r => extra.includes(r.ruleId) || r.tags.some(t => tags.includes(t)));
process.stdout.write(JSON.stringify({version: axe.version, rules: rules.map(r => ({id: r.ruleId, tags: r.tags}))}));
"""


def axe_rules():
    """Every rule the installed axe-core would run under AXE_TAGS, plus AXE_EXTRA, with its tags."""
    pkg = os.path.join(ROOT, "node_modules", "axe-core")
    if not os.path.isdir(pkg):
        raise AssertionError("node_modules/axe-core is missing: run npm install")
    out = subprocess.run(["node", "-e", AXE_SCRIPT, pkg, json.dumps(AXE_TAGS), json.dumps(AXE_EXTRA)],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def criterion_tags(tags):
    """axe's wcagNNN tags as criterion numbers: wcag1412 -> 1.4.12."""
    return [f"{t[4]}.{t[5]}.{t[6:]}" for t in tags if re.fullmatch(r"wcag\d{3,}", t)]


def hand_found_ids():
    ids = set()
    for path in sorted(glob.glob(os.path.join(ROOT, "data", "*.json"))):
        with open(path, encoding="utf-8") as f:
            for issue in json.load(f).get("issues", []):
                if issue.get("category") == "a11y":
                    ids.add(issue["id"])
    return ids


def schema_custom_rules():
    """The custom rule ids schemas.md lists under Result."""
    with open(os.path.join(A11Y, "schemas.md"), encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"Custom checks use these rule ids[^\n]*\n((?:  [^\n]*\n)+)", text)
    if not m:
        raise AssertionError("schemas.md no longer lists the custom rule ids")
    return set(re.findall(r"`([a-z][a-z0-9-]+)`", m.group(1)))


# KTD14's table: family id -> default severity.
KTD14_SEVERITY = {
    "contrast": "medium", "target-size": "low", "focus-visible": "medium", "focus-order": "medium",
    "keyboard": "medium", "keyboard-trap": "high", "dialog-trigger": "high", "dialog-focus": "medium",
    "name": "medium", "role-state": "medium", "status-message": "medium", "error-association": "medium",
}
# KTD15: intrinsic families are fixed in the component; the other base families where it is used.
KTD15_INTRINSIC = {"contrast", "target-size", "focus-visible"}
# KTD13's table: the criteria each custom check fails.
KTD13_CRITERIA = {
    "target-size-own": ["2.5.8"],
    "focus-not-visible": ["2.4.7"],
    "focus-hidden": ["2.4.3", "2.4.7"],
    "keyboard-trap": ["2.1.2"],
    "not-reached": ["2.1.1"],
    "backward-jump": ["1.3.2", "2.4.3"],
    "dialog-trigger-not-focusable": ["2.1.1"],
    "dialog-trigger-not-operable": ["2.1.1"],
    "hover-only": ["2.1.1"],
    "dialog-focus-not-moved": ["2.4.3"],
    "dialog-focus-escaped": ["2.4.3"],
    "dialog-focus-not-returned": ["2.4.3"],
    "menu-focus-not-moved": ["2.4.3"],
    "popover-not-reachable": ["2.4.3"],
    "status-not-announced": ["4.1.3"],
    "error-not-associated": ["3.3.1"],
}
SEVERITIES = {"high", "medium", "low"}
COPY_KEYS = {"title", "summary", "userImpact", "fixHypothesis", "acceptance", "verify", "searchTerms",
             "confidence", "confidenceReason"}
LIST_KEYS = {"acceptance", "verify", "searchTerms"}

# Long but realistic values for facts whose factDocs entry gives no character cap.
UNCAPPED_SAMPLES = {
    "role": "menuitemcheckbox",
    "screens": "214",
    "fg": "#9d9da8",
    "bg": "#f4f4f6",
    "ratio": "2.85",
    "required": "4.5",
    "size": "23×16",
    "offset": "14",
    "stop": "57",
    "stops": "118",
    "elements": "312 elements",
    "messageKey": "elmPartiallyObscuring",
}
# The message keys the incomplete block must word (KTD12): the ones axe 4.13 returns most on the demo.
MESSAGE_KEYS = ["bgOverlap", "bgImage", "bgGradient", "pseudoContent", "elmPartiallyObscured", "elmPartiallyObscuring",
                "shortTextContent", "nonBmp", "equalRatio", "controlsWithinPopup", "noId", "visibleLabelSingular",
                "noRoleSingular", "nonTabbableNeighbor"]
# Each capped fact is filled to its cap from text like this.
CAPPED_SEED = 'button "Distribute funds across accounts and keep a target balance in Treasury '
CSS_MODULE_CLASS = re.compile(r"_[A-Za-z][A-Za-z0-9-]*_[A-Za-z0-9]{5}_\d+")


def placeholders(text):
    names = set()
    for _, field, spec, conv in string.Formatter().parse(text):
        if field is None:
            continue
        if spec or conv or not re.fullmatch(r"[A-Za-z]+", field):
            raise AssertionError(f"use plain {{name}} placeholders only: {text!r}")
        names.add(field)
    return names


def template_strings(t):
    """Every string in a template object, with its path."""
    out = []
    for k, v in t.items():
        if k == "copy":
            for ck, cv in v.items():
                for i, s in enumerate(cv if isinstance(cv, list) else [cv]):
                    out.append((f"copy.{ck}[{i}]" if isinstance(cv, list) else f"copy.{ck}", s))
        else:
            out.append((k, v))
    return out


def merged(base, override):
    t = dict(base)
    t["copy"] = dict(base["copy"])
    for k, v in override.items():
        if k == "copy":
            t["copy"].update(v)
        else:
            t[k] = v
    return t


class CheckDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wcag = load("wcag.json")
        cls.checks = load("checks.json")
        cls.hand = load("hand-found.json")
        cls.criteria = {c["number"]: c for c in cls.wcag["criteria"]}
        cls.axe = axe_rules()
        cls.families = cls.checks["families"]

    # -- wcag.json ---------------------------------------------------------

    def test_wcag_lists_the_55_a_and_aa_criteria(self):
        crit = self.wcag["criteria"]
        numbers = [c["number"] for c in crit]
        self.assertEqual(len(crit), 55)
        self.assertEqual(len(set(numbers)), 55, "criterion numbers repeat")
        self.assertNotIn("4.1.1", numbers, "4.1.1 Parsing is removed in WCAG 2.2")
        self.assertEqual(numbers, sorted(numbers, key=number_key), "sort by criterion number")
        for c in crit:
            self.assertIn(c["level"], {"A", "AA"}, c["number"])
            self.assertIn(c["principle"], {"Perceivable", "Operable", "Understandable", "Robust"})
            self.assertEqual(c["principle"], ["Perceivable", "Operable", "Understandable", "Robust"][int(c["number"][0]) - 1])
            self.assertTrue(c["guideline"].startswith(c["number"].rsplit(".", 1)[0] + " "), c["number"])
            self.assertTrue(c["name"].strip(), c["number"])
            self.assertTrue(c["url"].startswith("https://www.w3.org/WAI/WCAG22/Understanding/"), c["number"])
            self.assertRegex(c["url"], r"/Understanding/[a-z0-9-]+$")
        self.assertEqual(len({c["url"] for c in crit}), 55, "Understanding URLs repeat")

    def test_wcag_levels_agree_with_axe_tags(self):
        for r in self.axe["rules"]:
            levels = {LEVEL_TAGS[t] for t in r["tags"] if t in LEVEL_TAGS}
            for n in criterion_tags(r["tags"]):
                if n in self.criteria and levels:
                    self.assertIn(self.criteria[n]["level"], levels, f"{n} via axe rule {r['id']}")

    # -- checks.json: families and rule maps --------------------------------

    def test_ktd14_families_and_default_severities(self):
        for fid, sev in KTD14_SEVERITY.items():
            self.assertIn(fid, self.families)
            self.assertEqual(self.families[fid]["defaultSeverity"], sev, fid)
            self.assertEqual(self.families[fid]["key"], "intrinsic" if fid in KTD15_INTRINSIC else "content", fid)
        by_kind = self.families["dialog-focus"]["defaultSeverityByKind"]
        for kind in ("menu", "popover"):
            self.assertEqual(by_kind[kind]["dialog-focus-not-returned"], "low", kind)

    def test_families_are_well_formed(self):
        for fid, f in self.families.items():
            self.assertRegex(fid, r"^[a-z][a-z-]*$")
            self.assertTrue(f["label"].strip(), fid)
            self.assertTrue(f["guidance"].strip(), fid)
            self.assertIn(f["defaultSeverity"], SEVERITIES, fid)
            self.assertIn(f["key"], {"intrinsic", "content"}, fid)
            self.assertTrue(f["criteria"], fid)
            self.assertEqual(len(f["criteria"]), len(set(f["criteria"])), fid)
            for n in f["criteria"]:
                self.assertIn(n, self.criteria, f"{fid} cites {n}")
            for kind, rules in f.get("defaultSeverityByKind", {}).items():
                for rule, sev in rules.items():
                    self.assertIn(sev, SEVERITIES, f"{fid} {kind} {rule}")
                    self.assertEqual(self.rule_family(rule), fid, f"{fid} {kind} {rule}")
            t = f["templates"]
            self.assertEqual(set(t), {"title", "detail", "evidence", "copy"}, fid)
            self.assertEqual(set(t["copy"]), COPY_KEYS, fid)
            for k in LIST_KEYS:
                self.assertTrue(t["copy"][k] and all(isinstance(s, str) and s for s in t["copy"][k]), f"{fid} {k}")
            self.assertIn(t["copy"]["confidence"], SEVERITIES, fid)
            for rule, o in f.get("ruleTemplates", {}).items():
                self.assertEqual(self.rule_family(rule), fid, f"{fid} overrides {rule}, which is not in it")
                self.assertTrue(set(o) <= {"title", "detail", "evidence", "copy"}, f"{fid} {rule}")
                self.assertTrue(set(o.get("copy", {})) <= COPY_KEYS, f"{fid} {rule}")

    def rule_family(self, rule):
        return self.checks["axeRules"].get(rule) or self.checks["customRules"].get(rule)

    def test_rule_maps_point_at_families(self):
        for table in ("axeRules", "customRules"):
            for rule, fid in self.checks[table].items():
                self.assertIn(fid, self.families, f"{table}.{rule}")
        self.assertFalse(set(self.checks["axeRules"]) & set(self.checks["customRules"]), "a rule id is in both maps")
        used = set(self.checks["axeRules"].values()) | set(self.checks["customRules"].values())
        for fid in self.families:
            self.assertIn(fid, used, f"no rule feeds family {fid}")

    def test_every_axe_rule_that_runs_has_a_family(self):
        ids = {r["id"] for r in self.axe["rules"]}
        for extra in AXE_EXTRA:
            self.assertIn(extra, ids, f"axe-core {self.axe['version']} has no rule {extra}")
        missing = sorted(ids - set(self.checks["axeRules"]))
        self.assertEqual(missing, [], f"axe-core {self.axe['version']} rules with no family")
        stale = sorted(set(self.checks["axeRules"]) - ids)
        self.assertEqual(stale, [], f"rules not in axe-core {self.axe['version']} under the sweep's tags")

    def test_axe_rule_criteria_belong_to_their_family(self):
        for r in self.axe["rules"]:
            fid = self.checks["axeRules"].get(r["id"])
            for n in criterion_tags(r["tags"]):
                if n in self.criteria:
                    self.assertIn(n, self.families[fid]["criteria"], f"axe {r['id']} fails {n}; family {fid}")

    def test_custom_rules_match_schemas_and_ktd13(self):
        self.assertEqual(set(self.checks["customRules"]), schema_custom_rules())
        self.assertEqual(set(self.checks["customCriteria"]), set(self.checks["customRules"]))
        for rule, crit in self.checks["customCriteria"].items():
            self.assertEqual(sorted(crit, key=number_key), KTD13_CRITERIA[rule], rule)
            for n in crit:
                self.assertIn(n, self.families[self.checks["customRules"][rule]]["criteria"], rule)

    # -- checks.json: templates and facts ------------------------------------

    def caps(self):
        caps = {}
        for name, doc in self.checks["factDocs"].items():
            m = re.search(r"at most (\d+) characters", doc)
            if m:
                caps[name] = int(m.group(1))
        return caps

    def sample_facts(self, fid, rule):
        """Long but realistic values for every fact: capped facts at their cap, the family's longest
        criterion name, and the longest rule id."""
        caps = self.caps()
        facts = {}
        for name in self.checks["factDocs"]:
            if name in caps:
                facts[name] = (CAPPED_SEED * 4)[:caps[name] - 1] + "…"  # cut to fit, as the filler does
            elif name in UNCAPPED_SAMPLES:
                facts[name] = UNCAPPED_SAMPLES[name]
        crit = max(self.families[fid]["criteria"], key=lambda n: len(self.criteria[n]["name"]))
        facts["criterion"] = crit
        facts["criterionName"] = self.criteria[crit]["name"]
        facts["rule"] = rule
        return facts

    def variants(self, fid):
        """(rule, filled-in template) for the family's default and each rule override. The default
        is tried with the family's longest rule id."""
        f = self.families[fid]
        rules = [r for t in ("axeRules", "customRules") for r, x in self.checks[t].items() if x == fid]
        out = [(max(rules, key=len), f["templates"])]
        for rule, o in sorted(f.get("ruleTemplates", {}).items()):
            out.append((rule, merged(f["templates"], o)))
        return out

    def test_every_fact_is_documented_and_sampled(self):
        caps = self.caps()
        for name in self.checks["factDocs"]:
            if name not in {"criterion", "criterionName", "rule"}:
                self.assertTrue(name in caps or name in UNCAPPED_SAMPLES,
                                f"fact {name} has no cap in factDocs and no sample in this test")

    def test_facts_lists_match_the_templates(self):
        used_anywhere = set()
        for fid, f in self.families.items():
            used = set()
            for _, t in self.variants(fid):
                for _, s in template_strings(t):
                    used |= placeholders(s)
            self.assertEqual(sorted(f["facts"]), sorted(used), f"{fid}: facts list differs from its templates")
            for name in used:
                self.assertIn(name, self.checks["factDocs"], f"{fid} uses undocumented fact {name}")
            used_anywhere |= used
        used_anywhere |= self.incomplete_placeholders()
        self.assertEqual(sorted(set(self.checks["factDocs"]) - used_anywhere), [], "documented but unused facts")

    def test_templates_fill_and_fit(self):
        for fid in self.families:
            for rule, t in self.variants(fid):
                facts = self.sample_facts(fid, rule)
                where = f"{fid}/{rule}"
                for path, s in template_strings(t):
                    try:
                        filled = s.format(**facts)
                    except KeyError as e:
                        self.fail(f"{where} {path}: no fact {e}")
                    self.assertNotIn("class", s.lower(), f"{where} {path} mentions a CSS class")
                    self.assertIsNone(CSS_MODULE_CLASS.search(s), f"{where} {path} names a CSS-Module class")
                    self.assertEqual(filled, filled.strip(), f"{where} {path}")
                    if path == "title":
                        self.assertLessEqual(len(filled), 80, f"{where} title: {filled}")
                    if path == "detail":
                        self.assertTrue(filled.endswith(f"{facts['criterion']} {facts['criterionName']}."),
                                        f"{where} detail must end with the criterion: {filled}")

    # -- the incomplete block (KTD12: one finding per axe rule and messageKey) ---

    def incomplete_placeholders(self):
        used = set()
        for _, s in template_strings(self.checks["incomplete"]["templates"]):
            used |= placeholders(s)
        return used

    def test_incomplete_block_is_well_formed(self):
        inc = self.checks["incomplete"]
        t = inc["templates"]
        self.assertEqual(sorted(t), ["copy", "detail", "evidence", "title"])
        self.assertEqual(set(t["copy"]), COPY_KEYS)
        for k in LIST_KEYS:
            self.assertIsInstance(t["copy"][k], list, k)
        used = self.incomplete_placeholders()
        self.assertEqual(sorted(inc["facts"]), sorted(used), "facts list differs from the templates")
        for name in used:
            self.assertIn(name, self.checks["factDocs"], f"incomplete uses undocumented fact {name}")
        self.assertNotIn("element", used, "an incomplete group names no single element")
        self.assertEqual(sorted(set(MESSAGE_KEYS) - set(inc["messageKeyText"])), [])
        cap = self.caps()["why"]
        for key, text in list(inc["messageKeyText"].items()) + [(None, inc["noMessageKey"])]:
            self.assertLessEqual(len(text), cap, f"{key}: {text}")
            self.assertEqual(text, text.strip(), key)
            self.assertNotIn("class", text.lower(), key)

    def test_incomplete_templates_fill_and_fit_for_every_axe_rule(self):
        inc = self.checks["incomplete"]
        whys = list(inc["messageKeyText"].values()) + [inc["noMessageKey"]]
        for rule, fid in sorted(self.checks["axeRules"].items()):
            for why in [None] + whys:
                facts = self.sample_facts(fid, rule)
                if why is not None:
                    facts["why"] = why
                where = f"incomplete/{rule}/{why}"
                for path, s in template_strings(inc["templates"]):
                    try:
                        filled = s.format(**facts)
                    except KeyError as e:
                        self.fail(f"{where} {path}: no fact {e}")
                    self.assertNotIn("class", s.lower(), f"{where} {path} mentions a CSS class")
                    self.assertIsNone(CSS_MODULE_CLASS.search(s), f"{where} {path} names a CSS-Module class")
                    self.assertEqual(filled, filled.strip(), f"{where} {path}")
                    if path in ("title", "copy.title"):
                        self.assertLessEqual(len(filled), 80, f"{where} {path}: {filled}")
                    if path == "detail":
                        self.assertTrue(filled.endswith(f"{facts['criterion']} {facts['criterionName']}."),
                                        f"{where} detail must end with the criterion: {filled}")

    # -- hand-found.json -----------------------------------------------------

    def test_hand_found_covers_every_a11y_issue(self):
        ids = hand_found_ids()
        self.assertEqual(len(ids), 17, "the plan expects 17 hand-found a11y issues")
        self.assertEqual(sorted(set(self.hand)), sorted(ids))
        for iid, h in self.hand.items():
            self.assertIn(h["family"], self.families, iid)
            self.assertTrue(h["criteria"], iid)
            for n in h["criteria"]:
                self.assertIn(n, self.criteria, f"{iid} cites {n}")
            self.assertTrue(h["why"].strip(), iid)


if __name__ == "__main__":
    unittest.main()
