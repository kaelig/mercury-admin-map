"""The standalone report (R3, R19–R22, R26; KTD24, KTD25).

Run: cd tools/a11y && python3 -m unittest tests.test_report -v

Renders tests/data/snapshot/ (and edited copies of it) into temp dist directories only, then reads the
page back through html.parser. The browser half (axe in light and dark, file:// links, focus) is
tests/report.test.mjs.
"""
import contextlib
import html.parser
import io
import json
import os
import re
import shutil
import tempfile
import unittest

from tests.pathshim import A11Y, ROOT

import audit
import report
from lib.ws import Workspace

SNAPSHOT = os.path.join(A11Y, "tests", "data", "snapshot")
WCAG = json.load(open(os.path.join(A11Y, "wcag.json"), encoding="utf-8"))["criteria"]
SECTIONS = ["summary", "coverage", "changes", "findings", "checks", "priority", "not-retested-withdrawn-fixed",
            "not-reproduced", "unreachable"]
FINDING_TARGETS = {"new": "findings", "persisting": "findings", "re-rated": "findings", "reappeared": "findings",
                   "not-retested": "not-retested", "withdrawn": "withdrawn", "fixed": "fixed"}
HAND_TARGETS = {"first-audited": "findings", "confirmed": "findings", "re-rated": "findings",
                "not-reproduced": "not-reproduced", "not-retested": "not-retested"}
LINK = "../index.html?issue="


def load(name, snapshot=SNAPSHOT):
    with open(os.path.join(snapshot, name), encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# A small DOM, enough to read the page back
# ---------------------------------------------------------------------------

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BLOCK = {"br", "caption", "dd", "div", "dl", "dt", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ol", "p", "section",
         "table", "tbody", "td", "th", "thead", "tr", "ul", "header", "nav", "main"}


class Node:
    def __init__(self, tag, attrs=(), parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    def iter(self):
        for c in self.children:
            if isinstance(c, Node):
                yield c
                yield from c.iter()

    def all(self, tag=None, **attrs):
        return [n for n in self.iter() if (tag is None or n.tag == tag)
                and all(n.attrs.get(k.replace("_", "-")) == v for k, v in attrs.items())]

    def by_id(self, ident):
        found = [n for n in self.iter() if n.attrs.get("id") == ident]
        if len(found) != 1:
            raise AssertionError(f"{len(found)} elements with id {ident!r}")
        return found[0]

    def kids(self, tag):
        return [c for c in self.children if isinstance(c, Node) and c.tag == tag]

    def raw_text(self):
        out = []
        for c in self.children:
            if isinstance(c, str):
                out.append(c)
            elif c.tag in BLOCK:
                out.append(f" {c.raw_text()} ")
            else:
                out.append(c.raw_text())
        return "".join(out)

    @property
    def text(self):
        return " ".join(self.raw_text().split())

    def links(self):
        return [a.attrs.get("href") for a in self.all("a")]


class Parser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = self.cur = Node("#root")

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs, self.cur)
        self.cur.children.append(n)
        if tag not in VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse(text):
    p = Parser()
    p.feed(text)
    p.close()
    return p.root


def rows(table):
    """Each body row of a table as {column header: cell node}."""
    heads = [th.text for th in table.kids("thead")[0].all("th")]
    out = []
    for tr in table.kids("tbody")[0].kids("tr"):
        cells = [c for c in tr.children if isinstance(c, Node) and c.tag in ("th", "td")]
        out.append(dict(zip(heads, cells)))
    return out


def table(doc, ident):
    return doc.by_id(ident).all("table")[0] if doc.by_id(ident).tag != "table" else doc.by_id(ident)


def norm(s):
    return " ".join(str(s).split())


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def variant(tmp, edit):
    """A copy of the test snapshot in tmp with report.json passed through edit(model)."""
    dest = os.path.join(tmp, "snapshot")
    shutil.copytree(SNAPSHOT, dest)
    model = load("report.json", dest)
    edit(model)
    with open(os.path.join(dest, "report.json"), "w", encoding="utf-8") as f:
        json.dump(model, f, indent=1, ensure_ascii=False)
    return dest


def render_to(tmp, snapshot=SNAPSHOT, name="dist"):
    dist = os.path.join(tmp, name)
    os.makedirs(dist, exist_ok=True)
    path = report.render(snapshot, dist)
    return dist, path


def page_of(dist):
    with open(os.path.join(dist, "a11y", "index.html"), encoding="utf-8") as f:
        return f.read()


class RenderedSnapshot(unittest.TestCase):
    """The test snapshot, rendered once."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.dist, cls.path = render_to(cls.tmp)
        cls.html = page_of(cls.dist)
        cls.doc = parse(cls.html)
        cls.model = load("report.json")
        cls.issues = load("issues.json")
        cls.main = cls.doc.all("main")[0]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    # Structure --------------------------------------------------------------

    def test_render_returns_the_page_path(self):
        self.assertEqual(self.path, os.path.join(self.dist, "a11y", "index.html"))

    def test_sections_follow_ktd24_order(self):
        self.assertEqual([s.attrs.get("id") for s in self.main.kids("section")], SECTIONS)
        self.assertEqual([s.attrs.get("id") for s in self.doc.by_id("checks").kids("section")],
                         ["contrast", "keyboard", "dialog", "screen-reader"])
        self.assertEqual([s.attrs.get("id") for s in self.doc.by_id("not-retested-withdrawn-fixed").kids("section")],
                         ["not-retested", "withdrawn", "fixed"])

    def test_every_section_opens_with_its_heading_and_headings_never_skip_a_level(self):
        for ident in SECTIONS:
            first = next(c for c in self.doc.by_id(ident).children if isinstance(c, Node))
            self.assertEqual(first.tag, "h2", ident)
        levels = [int(n.tag[1]) for n in self.doc.iter() if re.fullmatch(r"h[1-6]", n.tag)]
        self.assertEqual(levels[0], 1)
        self.assertEqual(levels.count(1), 1)
        for a, b in zip(levels, levels[1:]):
            self.assertLessEqual(b, a + 1, f"heading jumps from h{a} to h{b}")

    def test_landmarks_captions_and_header_scopes(self):
        for tag in ("header", "nav", "main"):
            self.assertEqual(len(self.doc.all(tag)), 1, tag)
        self.assertEqual(self.doc.all("nav")[0].attrs.get("aria-label"), "Sections")
        self.assertEqual(self.doc.all("html")[0].attrs.get("lang"), "en")
        tables = self.doc.all("table")
        self.assertGreater(len(tables), 10)
        for t in tables:
            caps = t.kids("caption")
            self.assertEqual(len(caps), 1)
            self.assertTrue(caps[0].text)
            for th in t.all("th"):
                self.assertIn(th.attrs.get("scope"), ("col", "row"), t.attrs)
            for tr in t.kids("tbody")[0].kids("tr"):
                self.assertEqual(len(tr.all("th")), 1, "every body row has one row header")

    def test_scroll_regions_are_named_by_their_caption_and_focusable(self):
        for div in self.doc.all("div", **{"class": "scroll"}):
            self.assertEqual(div.attrs.get("role"), "region")
            self.assertEqual(div.attrs.get("tabindex"), "0")
            cap = self.doc.by_id(div.attrs["aria-labelledby"])
            self.assertEqual(cap.tag, "caption")

    def test_navigation_lists_the_nine_sections_in_order(self):
        nav = self.doc.all("nav")[0]
        self.assertEqual(nav.links(), [f"#{s}" for s in SECTIONS])

    # Summary ----------------------------------------------------------------

    def test_summary_counts_match_the_data(self):
        r = {row["Severity"].text: row["Findings"].text for row in rows(table(self.doc, "t-summary"))}
        counts = self.model["counts"]
        self.assertEqual(r, {"High": str(counts["high"]), "Medium": str(counts["medium"]), "Low": str(counts["low"]),
                             "Total": str(sum(counts.values()))})
        by_rows = {s: sum(1 for f in self.model["findings"] if f["severity"] == s) for s in ("high", "medium", "low")}
        self.assertEqual(by_rows, counts)
        self.assertEqual((counts["high"], counts["medium"], counts["low"]), (2, 13, 5))

    # Links (R19) --------------------------------------------------------------

    def test_every_new_persisting_and_hand_found_row_in_findings_links_to_its_issue(self):
        linked = []
        for p in ("perceivable", "operable", "understandable", "robust"):
            sec = self.doc.by_id(f"principle-{p}")
            for t in sec.all("table"):
                for row in rows(t):
                    hrefs = row["Issue"].links()
                    self.assertEqual(len(hrefs), 1)
                    linked.append(hrefs[0])
        self.assertEqual(sorted(linked), sorted(LINK + f["id"] for f in self.model["findings"]))
        statuses = {f["status"] for f in self.model["findings"]}
        self.assertTrue({"new", "persisting", "confirmed", "re-rated"} <= statuses)

    def test_not_retested_rows_link_and_carry_their_last_verification(self):
        sec = self.doc.by_id("not-retested")
        body = rows(sec.all("table")[0])
        self.assertEqual(len(body), len(self.model["notRetested"]))
        for row, data in zip(body, self.model["notRetested"]):
            self.assertEqual(row["Finding"].links(), [LINK + data["id"]])
            self.assertIn(data["lastVerified"]["date"], row["Last verified"].text)
            self.assertIn(data["lastVerified"]["run"], row["Last verified"].text)
            self.assertEqual(row["Why not retested"].text, data["reason"])

    def test_hand_found_not_reproduced_rows_link(self):
        sec = self.doc.by_id("not-reproduced")
        dropped = [h for h in self.model["handFound"] if h["status"] == "not-reproduced"]
        self.assertEqual(len(dropped), 1)
        body = rows(sec.all("table")[0])
        self.assertEqual([r["Issue"].links() for r in body], [[LINK + h["id"]] for h in dropped])
        self.assertEqual(body[0]["Why"].text, "tool false positive")
        self.assertEqual(body[0]["Verifier's note"].text, dropped[0]["verifierReason"])

    def test_every_hand_found_issue_is_linked_somewhere(self):
        hrefs = set(self.main.links())
        for h in self.model["handFound"]:
            self.assertIn(LINK + h["id"], hrefs, h["id"])

    def test_fixed_withdrawn_and_unreachable_have_no_links(self):
        for ident in ("withdrawn", "fixed", "unreachable"):
            self.assertEqual(self.doc.by_id(ident).all("a"), [], ident)
        withdrawn = rows(self.doc.by_id("withdrawn").all("table")[0])
        self.assertEqual([r["Finding"].text for r in withdrawn], [w["title"] for w in self.model["withdrawn"]])
        self.assertEqual(withdrawn[0]["Verifier's reason"].text, self.model["withdrawn"][0]["reason"])
        fixed = rows(self.doc.by_id("fixed").all("table")[0])
        f = self.model["fixed"][0]
        self.assertEqual(fixed[0]["Finding"].text, f["title"])
        self.assertIn("2.5.8", fixed[0]["WCAG criterion"].text)
        self.assertEqual(fixed[0]["Screens before"].text, str(f["screens"]))
        self.assertEqual(fixed[0]["Fixed in run"].text, f["run"])

    def test_every_link_resolves(self):
        ids = {n.attrs["id"] for n in self.doc.iter() if "id" in n.attrs}
        known = {i["id"] for i in self.issues["generated"]} | set(self.issues["overlays"])
        for href in self.doc.links():
            if href.startswith("#"):
                self.assertIn(href[1:], ids, href)
            elif href.startswith(LINK):
                self.assertIn(href[len(LINK):], known, href)
            else:
                self.assertEqual(href, "../index.html")

    def test_priority_fixes_follow_the_model_order_and_link(self):
        items = self.doc.by_id("priority").all("ol")[0].kids("li")
        self.assertEqual([li.links()[0] for li in items], [LINK + i for i in self.model["priority"]])
        first = items[0].text
        self.assertIn("High", first)
        self.assertIn("6 screens", first)

    # Coverage (R20) -----------------------------------------------------------

    def facts(self):
        dl = self.doc.by_id("coverage").all("dl")[0]
        dts, dds = dl.kids("dt"), dl.kids("dd")
        self.assertEqual(len(dts), len(dds))
        return {dt.text: dd for dt, dd in zip(dts, dds)}

    def test_coverage_states_dates_standard_builds_and_lighthouse(self):
        f, cov = self.facts(), self.model["coverage"]
        self.assertEqual(f["Run date"].text, "2026-10-02 (run 2026-10-02-1400)")
        self.assertEqual(f["Screenshots captured"].text, "2026-09-24")
        self.assertEqual(f["Standard"].text, "WCAG 2.2 AA")
        self.assertEqual(f["States audited"].text, f"{cov['audited']} of {cov['states']}")
        self.assertTrue(f["States unreachable"].text.startswith(f"{cov['unreachable']},"))
        self.assertEqual(f["States unreachable"].links(), ["#unreachable"])
        self.assertEqual([c.text for c in f["Demo builds seen"].all("code")], cov["builds"])
        self.assertEqual((cov["audited"], cov["unreachable"], cov["states"]), (75, 3, 78))
        lh = f["Lighthouse calibration"].text
        self.assertTrue(lh.startswith("2026-10-01:"), lh)
        self.assertIn(cov["lighthouse"]["line"], lh)

    def test_coverage_lists_tested_criteria_with_coverage_and_modes(self):
        body = rows(table(self.doc, "t-tested"))
        tested = self.model["coverage"]["tested"]
        self.assertEqual([r["Criterion"].text for r in body], [f"{c['number']} {c['name']}" for c in tested])
        for r, c in zip(body, tested):
            self.assertEqual(r["Coverage"].text, c["coverage"])
            self.assertEqual(r["Failure modes covered"].text, c["modes"])
            self.assertEqual(r["Checks"].text, ", ".join(c["checks"]))
        self.assertIn("full", {r["Coverage"].text for r in body})
        self.assertIn("partial", {r["Coverage"].text for r in body})

    def test_tested_criteria_without_a_failure_say_no_failures_found_by_these_checks(self):
        result = {r["Criterion"].text.split()[0]: r["Result"].text for r in rows(table(self.doc, "t-tested"))}
        for c in ("1.1.1", "1.3.2", "2.1.2", "2.4.2", "2.5.8", "3.1.1"):
            self.assertEqual(result[c], "No failures found by these checks", c)
        self.assertEqual(result["1.4.3"], "1 finding")
        self.assertNotRegex(self.doc.all("body")[0].text, re.compile(r"\bpass(es|ed|ing)?\b", re.I))

    def test_every_other_criterion_is_marked_not_tested_and_the_four_are_named(self):
        tested = {r["Criterion"].text.split()[0] for r in rows(table(self.doc, "t-tested"))}
        body = rows(table(self.doc, "t-untested"))
        untested = {r["Criterion"].text.split()[0]: r for r in body}
        self.assertEqual(tested | set(untested), {c["number"] for c in WCAG})
        self.assertFalse(tested & set(untested))
        for r in body:
            self.assertTrue(r["Result"].text.startswith("Not tested"), r["Criterion"].text)
        named = self.doc.by_id("named-untested").text
        for number, name in (("1.4.12", "Text Spacing"), ("1.4.13", "Content on Hover or Focus"),
                             ("2.4.11", "Focus Not Obscured (Minimum)"), ("2.5.7", "Dragging Movements")):
            self.assertIn(f"{number} {name}", named)
            self.assertIn(number, untested)

    def test_untested_criteria_with_a_hand_found_issue_say_so(self):
        untested = {r["Criterion"].text.split()[0]: r["Result"].text for r in rows(table(self.doc, "t-untested"))}
        self.assertEqual(untested["2.5.3"], "Not tested by these checks; 1 hand-found issue")
        self.assertEqual(untested["1.2.1"], "Not tested")

    def test_coverage_names_the_untested_dimensions_and_the_caveat(self):
        items = [li.text for li in self.doc.by_id("untested-dimensions").kids("li")]
        self.assertEqual(items, ["zoom and reflow", "real screen readers", "mobile", "dark theme"])
        caveat = self.doc.by_id("caveat").text
        self.assertIn(norm(self.model["coverage"]["caveat"]), caveat)

    # Changes (R21) --------------------------------------------------------------

    def test_changes_show_counts_links_and_one_definition_per_status(self):
        sec = self.doc.by_id("changes")
        self.assertIn("Compared with run 2026-10-01-0900, the last published run.", sec.text)
        self.assertNotIn("First audited run", sec.text)
        for ident, counts, targets in (("t-changes-findings", self.model["changes"]["findings"], FINDING_TARGETS),
                                       ("t-changes-hand", self.model["changes"]["handFound"], HAND_TARGETS)):
            body = rows(table(self.doc, ident))
            self.assertEqual(len(body), len(targets))
            seen = {}
            for r in body:
                link = r["Status"].all("a")[0]
                seen[link.attrs["href"]] = r
                self.assertTrue(r["Definition"].text.endswith("."))
            defs = [r["Definition"].text for r in body]
            self.assertEqual(len(set(defs)), len(defs))
            for status, target in targets.items():
                r = next(r for r in body if r["Status"].attrs.get("data-status") == status)
                self.assertEqual(r["Status"].links(), [f"#{target}"], status)
                self.assertEqual(r["Count"].text, str(counts[status]), status)
        withdrawn = next(r for r in rows(table(self.doc, "t-changes-findings"))
                         if r["Status"].attrs.get("data-status") == "withdrawn")
        self.assertEqual(withdrawn["Definition"].text,
                         "Reported by the scanner, disproved by the verifier, no longer in the map.")

    # Findings by principle --------------------------------------------------------

    def test_findings_rows_carry_the_full_live_evidence(self):
        audit_evidence = {i["id"]: i["audit"]["evidence"] for i in self.issues["generated"]}
        audit_evidence.update({k: v["audit"]["evidence"] for k, v in self.issues["overlays"].items() if v.get("audit")})
        checked = 0
        for p in ("perceivable", "operable", "understandable", "robust"):
            for t in self.doc.by_id(f"principle-{p}").all("table"):
                for row in rows(t):
                    iid = row["Issue"].links()[0][len(LINK):]
                    cell = row["Evidence"]
                    lines = audit_evidence[iid].split("\n")
                    self.assertGreater(len(lines), 1, iid)
                    for line in lines:
                        self.assertIn(norm(line), cell.text, iid)
                    checked += 1
        self.assertEqual(checked, len(self.model["findings"]))
        # The short one-line evidence would have dropped the verifier's checked states.
        row = self.finding_row("cards.i-a11y-name-button-more-actions")
        self.assertIn("checked cards.index, cards.detail-physical", row["Evidence"].text)

    def finding_row(self, iid):
        for t in self.doc.by_id("findings").all("table"):
            for row in rows(t):
                if row["Issue"].links() == [LINK + iid]:
                    return row
        raise AssertionError(iid)

    def test_findings_rows_show_criterion_severity_screens_status_and_fix(self):
        row = self.finding_row("accounts.i-tabs-not-tabs")
        self.assertEqual(row["WCAG criterion"].text, "2.1.1 Keyboard 4.1.2 Name, Role, Value")
        self.assertEqual(row["Severity"].text, "High, was Medium")
        self.assertEqual(row["Screens"].text, "6")
        self.assertEqual(row["Status"].text, "Re-rated, hand-found")
        self.assertTrue(row["Recommended fix"].text.startswith("These controls seem to be built from plain divs"))
        new = self.finding_row("cards.i-a11y-dialog-focus-button-freeze-card")
        self.assertEqual(new["Severity"].text, "Medium")
        self.assertEqual(new["Status"].text, "New")

    def test_findings_are_grouped_by_principle_and_an_empty_principle_says_so(self):
        for f in self.model["findings"]:
            sec = self.doc.by_id(f"principle-{f['principle'].lower()}")
            self.assertIn(LINK + f["id"], sec.links())
        understandable = self.doc.by_id("principle-understandable")
        self.assertEqual(understandable.all("table"), [])
        self.assertIn("No failures found by these checks.", understandable.text)

    def test_an_attached_scanner_finding_shows_on_its_hand_found_row(self):
        row = self.finding_row("settings.i-unnamed-row-buttons")
        self.assertIn('Scanner finding attached: Missing or unclear name: button in "API Tokens" (new, 3 screens).',
                      row["Evidence"].text)

    # Tables -----------------------------------------------------------------------

    def test_contrast_table(self):
        body = rows(self.doc.by_id("contrast").all("table")[0])
        self.assertEqual(len(body), len(self.model["tables"]["contrast"]))
        r = next(r for r in body if r["Issue"].links() == [LINK + "transactions.i-a11y-contrast-text-in-recent-transactions"])
        self.assertEqual([r[k].text for k in ("Foreground", "Background", "Ratio", "Required")],
                         ["#9d9da8", "#ffffff", "3.12:1", "4.5:1"])
        hand = next(r for r in body if r["Issue"].links() == [LINK + "billpay.i-manage-link"])
        self.assertEqual(hand["Ratio"].text, "not measured")

    def test_keyboard_table_arrow_keys_read_not_tested(self):
        body = rows(self.doc.by_id("keyboard").all("table")[0])
        self.assertEqual(len(body), len(self.model["tables"]["keyboard"]))
        self.assertEqual({r["Arrow keys"].text for r in body}, {"not tested"})

    def test_dialog_table(self):
        body = rows(self.doc.by_id("dialog").all("table")[0])
        self.assertEqual([(r["Trigger"].text, r["Overlay"].text, r["Focus after opening"].text) for r in body],
                         [('button "Freeze card"', 'dialog "Freeze card"', "page body")])

    def test_screen_reader_table_has_its_footnote_and_the_advisory_list(self):
        sec = self.doc.by_id("screen-reader")
        body = rows(sec.all("table")[0])
        self.assertEqual(len(body), len(self.model["tables"]["screenReader"]))
        more = next(r for r in body if r["Issue"].links() == [LINK + "cards.i-a11y-name-button-more-actions"])
        self.assertEqual(more["Announced as"].text, 'button ""')
        self.assertIn("computed from Chrome's accessibility tree, not a screen reader", sec.text)
        advisory = self.doc.by_id("advisory")
        self.assertEqual(advisory.tag, "section")
        self.assertIn("No advisory announcements this run.", advisory.text)

    # Unreachable states -----------------------------------------------------------

    def test_unreachable_states_are_grouped_by_root_cause_with_their_first_unreachable_run(self):
        sec = self.doc.by_id("unreachable")
        groups = sec.kids("section")
        self.assertEqual(len(groups), len(self.model["unreachable"]))
        for g, data in zip(groups, self.model["unreachable"]):
            self.assertIn(data["rootCause"], g.all("h3")[0].text)
            body = rows(g.all("table")[0])
            self.assertEqual([(r["State"].text, r["Reason"].text, r["First unreachable in run"].text) for r in body],
                             [(s["state"], s["reason"], s["firstUnreachable"]) for s in data["states"]])
        cards = groups[1]
        self.assertEqual([r["State"].text for r in rows(cards.all("table")[0])],
                         ["cards.transfer-card", "cards.transfer-card-error"])

    # The page ---------------------------------------------------------------------

    def test_the_page_has_noindex_no_script_and_no_embedded_font(self):
        robots = [m for m in self.doc.all("meta") if m.attrs.get("name") == "robots"]
        self.assertEqual(len(robots), 1)
        self.assertIn("noindex", robots[0].attrs["content"])
        self.assertEqual(self.doc.all("script"), [])
        self.assertNotIn("@font-face", self.html)
        self.assertNotIn("url(", self.html)
        self.assertNotIn("Arcadia", self.html)
        self.assertRegex(self.html, r"font: [^;]*system-ui")

    def test_css_reuses_the_map_tokens_in_light_and_dark(self):
        light, dark = report.tokens(os.path.join(ROOT, "src", "app.css"))
        self.assertEqual(dict(light)["--accent"], "#5266eb")
        self.assertEqual(dict(dark)["--accent"], "#8da4f5")
        self.assertNotIn("--ui", dict(light))
        css = self.doc.all("style")[0].text
        m = re.search(r"@media \(prefers-color-scheme: dark\) \{ :root \{(.*?)\} \}", css, re.S)
        self.assertIsNotNone(m)
        dark_css, light_css = m.group(1), css[:m.start()]
        for name, value in light:
            self.assertIn(f"{name}: {value};", light_css)
        for name, value in dark:
            self.assertIn(f"{name}: {value};", dark_css)
        used = set(re.findall(r"var\((--[\w-]+)\)", css))
        self.assertTrue(used)
        self.assertLessEqual(used, set(dict(light)))
        # Dark redefines every color the page uses; the monospace stack cascades from the light block.
        self.assertLessEqual(used - {"--mono"}, set(dict(dark)))
        self.assertIn("a:focus-visible", css)


class Variants(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_first_run_reads_first_audited_run_above_the_counts(self):
        snap = variant(self.tmp, lambda m: m.update(baseline="none"))
        dist, _ = render_to(self.tmp, snap)
        doc = parse(page_of(dist))
        sec = doc.by_id("changes")
        first = sec.all("p")[0]
        self.assertEqual(first.text, "First audited run: no earlier run to compare.")
        self.assertNotIn("Compared with run", sec.text)
        self.assertEqual(len(sec.all("table")), 2)

    def test_advisory_list_lists_each_flagged_announcement(self):
        advisory = [{"sig": "rec:aaa", "template": 'link "Learn more"', "reason": "Repeated with no context.", "states": 12},
                    {"sig": "rec:bbb", "template": 'img "logo_final_v2"', "reason": "File-name text.", "states": 1}]
        snap = variant(self.tmp, lambda m: m.update(advisory=advisory))
        dist, _ = render_to(self.tmp, snap)
        items = parse(page_of(dist)).by_id("advisory").all("li")
        self.assertEqual([li.text for li in items],
                         ['link "Learn more": Repeated with no context. (12 states)',
                          'img "logo_final_v2": File-name text. (1 state)'])

    def test_every_string_from_the_data_is_escaped(self):
        evil = '<img src=x onerror=alert(1)>"&'

        def edit(m):
            m["findings"][0]["title"] = evil
            m["findings"][0]["fix"] = "<script>alert(1)</script>"
            m["withdrawn"][0]["reason"] = evil
            m["unreachable"][0]["states"][0]["state"] = evil
            m["coverage"]["builds"] = [evil]
            m["advisory"] = [{"sig": "x", "template": evil, "reason": evil, "states": 2}]
            m["tables"]["contrast"][1]["fg"] = evil
        snap = variant(self.tmp, edit)
        dist, _ = render_to(self.tmp, snap)
        text = page_of(dist)
        doc = parse(text)
        self.assertEqual(doc.all("img"), [])
        self.assertEqual(doc.all("script"), [])
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;&quot;&amp;", text)
        self.assertIn(evil, doc.by_id("withdrawn").text)

    def test_ids_are_url_encoded_and_escaped_in_links(self):
        def edit(m):
            m["findings"][0]["id"] = 'a b"&c'
            m["priority"] = ['a b"&c'] + m["priority"][1:]
        snap = variant(self.tmp, edit)
        dist, _ = render_to(self.tmp, snap)
        hrefs = parse(page_of(dist)).by_id("findings").links()
        self.assertIn(LINK + "a%20b%22%26c", hrefs)

    def test_rendering_twice_is_byte_identical(self):
        a, _ = render_to(self.tmp, name="a")
        b, _ = render_to(self.tmp, name="b")
        first = page_of(a)
        self.assertEqual(first, page_of(b))
        report.render(SNAPSHOT, a)
        self.assertEqual(first, page_of(a))

    def test_rendering_deletes_the_old_report_directory_first(self):
        dist = os.path.join(self.tmp, "dist")
        os.makedirs(os.path.join(dist, "a11y"))
        with open(os.path.join(dist, "a11y", "old.html"), "w") as f:
            f.write("stale")
        report.render(SNAPSHOT, dist)
        self.assertEqual(os.listdir(os.path.join(dist, "a11y")), ["index.html"])

    def test_no_snapshot_writes_nothing_and_removes_a_stale_report(self):
        for snapshot in (None, os.path.join(self.tmp, "missing"), self.tmp):
            dist = os.path.join(self.tmp, "dist")
            os.makedirs(os.path.join(dist, "a11y"), exist_ok=True)
            with open(os.path.join(dist, "a11y", "index.html"), "w") as f:
                f.write("stale")
            with open(os.path.join(dist, "index.html"), "w") as f:
                f.write("map")
            self.assertIsNone(report.render(snapshot, dist), snapshot)
            self.assertFalse(os.path.exists(os.path.join(dist, "a11y")), snapshot)
            self.assertTrue(os.path.exists(os.path.join(dist, "index.html")))
        empty = os.path.join(self.tmp, "fresh")
        os.makedirs(empty)
        self.assertIsNone(report.render(None, empty))
        self.assertEqual(os.listdir(empty), [])


class Stage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ws_dir = os.path.join(self.tmp, "ws")
        self.dist = os.path.join(self.tmp, "dist")
        os.makedirs(self.dist)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_stage(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = audit.main(["report", "--workspace", self.ws_dir, *argv])
        return code, out.getvalue(), err.getvalue()

    def test_report_stage_renders_the_workspace_snapshot_into_dist(self):
        shutil.copytree(SNAPSHOT, os.path.join(self.ws_dir, "published"))
        code, out, _ = self.run_stage("--dist", self.dist)
        self.assertEqual(code, 0)
        page = os.path.join(self.dist, "a11y", "index.html")
        self.assertEqual(json.loads(out), {"report": page})
        with open(page, encoding="utf-8") as f:
            self.assertEqual(f.read(), page_of(render_to(self.tmp, name="direct")[0]))

    def test_report_stage_without_a_snapshot_removes_the_stale_report(self):
        os.makedirs(os.path.join(self.dist, "a11y"))
        code, out, err = self.run_stage("--dist", self.dist)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"report": None})
        self.assertIn("no published snapshot", err)
        self.assertFalse(os.path.exists(os.path.join(self.dist, "a11y")))

    def test_default_dist_is_the_repo_dist(self):
        self.assertEqual(report.DEFAULT_DIST, os.path.join(ROOT, "dist"))
        self.assertEqual(Workspace(workspace=self.ws_dir).published_dir, os.path.join(self.ws_dir, "published"))


class BuildCall(unittest.TestCase):
    def test_build_renders_the_report_after_the_stray_html_cleanup(self):
        with open(os.path.join(ROOT, "build.py"), encoding="utf-8") as f:
            src = f.read()
        stray = src.index('for stray in [f for f in os.listdir(DIST) if f.endswith(".html") and f != "index.html"]')
        call = src.index("report.render(mapdata.snapshot_path(), DIST)")
        self.assertGreater(call, stray)
        self.assertEqual(src.count("report.render("), 1)


if __name__ == "__main__":
    unittest.main()
