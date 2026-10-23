"""The `report` stage (R3, R19–R22, R26; KTD24, KTD25): the standalone accessibility report.

    python3 tools/a11y/audit.py report [--dist dist]

render(snapshot_dir, dist_dir) writes <dist>/a11y/index.html from the snapshot's report.json, taking
each finding's full live evidence from its issues.json. It deletes <dist>/a11y/ first and writes
nothing when there is no snapshot. build.py calls it after its stray-HTML cleanup with
mapdata.snapshot_path(); this stage renders the workspace's published/ snapshot.

The page has no script and no embedded font. Its colors are the map's own tokens, read from the
:root blocks of src/app.css at render time (light, and dark under prefers-color-scheme). Every string
from the snapshot is escaped. Rows link to ../index.html?issue=<id>, which works on Netlify and from
file://. Wording: "no failures found by these checks", never "passes". A test snapshot (meta.json
testData) opens with mapdata.TEST_DATA_NOTE; any other snapshot renders without it.
File formats: schemas.md ("published/").
"""
import argparse
import html
import json
import os
import re
import shutil
import string
import sys
from urllib.parse import quote

import mapdata
from lib.canon import ROOT, plural, read_json

APP_CSS = os.path.join(ROOT, "src", "app.css")
DEFAULT_DIST = os.path.join(ROOT, "dist")
FONT_TOKENS = ("--ui", "--display")       # the map's webfont stacks: the report uses the system stack
SEVERITIES = tuple(mapdata.SEVERITY_RANK)
SEVERITY = {"high": "High", "medium": "Medium", "low": "Low"}
PRINCIPLES = ("Perceivable", "Operable", "Understandable", "Robust")
NO_FAILURES = "No failures found by these checks"
STATUS = {"new": "New", "persisting": "Persisting", "re-rated": "Re-rated", "reappeared": "Reappeared",
          "not-retested": "Not retested", "withdrawn": "Withdrawn", "fixed": "Fixed",
          "first-audited": "First audited", "confirmed": "Confirmed", "not-reproduced": "Not reproduced"}
# (status, the section that holds its rows, one-line definition), in diff.py's order (R21, KTD18).
FINDING_CHANGES = [
    ("new", "findings", "Found and verified in this run, and in no earlier published run."),
    ("persisting", "findings", "Verified in this run and in an earlier published run, at the same severity."),
    ("re-rated", "findings", "Verified in this run and in the last published run, at a different severity."),
    ("reappeared", "findings", "Recorded as fixed by an earlier run, then found and verified again."),
    ("not-retested", "not-retested", "Published before, but this run could not check all its screens; it stays in "
                                     "the map with its last verified evidence."),
    ("withdrawn", "withdrawn", "Reported by the scanner, disproved by the verifier, no longer in the map."),
    ("fixed", "fixed", "Published before; this run's check completed on every screen it was on and found nothing."),
]
HAND_CHANGES = [
    ("first-audited", "findings", "Reproduced by the verifier on the first audited run."),
    ("confirmed", "findings", "Reproduced by the verifier again, at the severity it was found at."),
    ("re-rated", "findings", "Reproduced by the verifier at a different severity."),
    ("not-reproduced", "not-reproduced", "Not reproduced on the screens this run reached; it stays in the map with "
                                         "the verifier's note."),
    ("not-retested", "not-retested", "Not checked in this run; it stays in the map unchanged."),
]
SECTIONS = [("summary", "Summary"), ("coverage", "Coverage"), ("changes", "Changes since the last published run"),
            ("findings", "Findings by WCAG principle"),
            ("checks", "Contrast, keyboard, dialog and screen-reader checks"), ("priority", "Priority fixes"),
            ("not-retested-withdrawn-fixed", "Not retested, withdrawn and fixed"),
            ("not-reproduced", "Hand-found issues not reproduced"), ("unreachable", "Unreachable states")]

PAGE = string.Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow, noarchive">
<title>Accessibility report: Mercury Admin Map</title>
<style>
$css
</style>
</head>
<body>
$test_data<header>
<p class="crumb"><a href="../index.html">Mercury Admin Map</a></p>
<h1>Accessibility report</h1>
<p class="lede">$lede</p>
</header>
<nav aria-label="Sections">
<ol>
$nav
</ol>
</nav>
<main>
$sections
</main>
</body>
</html>
""")

CSS = string.Template("""\
:root { color-scheme: light; $light }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; $dark } }
*, *::before, *::after { box-sizing: border-box; }
html { background: var(--card); }
body { margin: 0 auto; max-width: 1280px; padding: 32px 16px 80px; background: var(--card); color: var(--text);
  font: 400 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
h1, h2, h3, h4 { color: var(--ink); line-height: 1.25; font-weight: 650; text-wrap: balance; }
h1 { font-size: 30px; margin: 4px 0 8px; }
h2 { font-size: 22px; margin: 56px 0 12px; padding-top: 20px; border-top: 1px solid var(--card-border); }
h3 { font-size: 17px; margin: 32px 0 8px; }
h4 { font-size: 15px; margin: 24px 0 6px; }
p, ul, ol, dl { max-width: 80ch; }
p { margin: 8px 0; }
a { color: var(--accent); text-decoration: underline; text-underline-offset: 2px; }
a:focus-visible, .scroll:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 2px; }
code { font-family: var(--mono); font-size: .92em; overflow-wrap: anywhere; }
.crumb { margin: 0; font-size: 14px; }
.lede { color: var(--muted); font-size: 16px; }
nav ol { margin: 16px 0 0; padding-left: 22px; columns: 2 260px; column-gap: 32px; }
nav li { break-inside: avoid; padding: 2px 0; }
dl.facts { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 6px 20px; margin: 12px 0; }
dl.facts dt { font-weight: 600; color: var(--ink); }
dl.facts dd { margin: 0; }
.scroll { overflow-x: auto; margin: 8px 0 20px; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
caption { text-align: left; font-weight: 600; color: var(--ink); padding: 0 0 8px; }
th, td { text-align: left; vertical-align: top; padding: 8px 10px; border-bottom: 1px solid var(--card-border); }
thead th { background: var(--panel-2); color: var(--ink); font-weight: 600; white-space: nowrap; }
tbody th { font-weight: 500; color: var(--ink); min-width: 18ch; }
#t-summary { width: auto; min-width: 18rem; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.nowrap { white-space: nowrap; }
.evidence { min-width: 36ch; font-size: 13px; overflow-wrap: anywhere; }
.fix { min-width: 28ch; }
td p { margin: 6px 0 0; }
.sev { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 13px; font-weight: 600;
  color: var(--high-ink); white-space: nowrap; }
.sev-high { background: var(--high); }
.sev-medium { background: var(--med); }
.sev-low { background: var(--low); }
.muted, .note { color: var(--muted); }
.note { font-size: 14px; }
.caveat { margin: 16px 0; padding: 10px 14px; border-left: 3px solid var(--accent); background: var(--accent-soft); }
@media (max-width: 600px) {
  h1 { font-size: 26px; }
  dl.facts { grid-template-columns: minmax(0, 1fr); gap: 2px; }
  dl.facts dd { margin-bottom: 8px; }
}""")
TEST_DATA_CSS = ("\n.test-data { margin: 0 0 16px; padding: 10px 14px; border-radius: 6px; background: var(--med); "
                 "color: var(--high-ink); font-weight: 600; }")


# ---------------------------------------------------------------------------
# Words and cells
# ---------------------------------------------------------------------------

def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def join_and(items):
    items = list(items)
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def issue_link(iid, title):
    return f'<a href="{esc("../index.html?issue=" + quote(str(iid), safe=""))}">{esc(title)}</a>'


def maybe_link(iid, title):
    return issue_link(iid, title) if iid else esc(title)


def criteria_cell(numbers, names):
    return "<br>".join(esc(f"{n} {names.get(n, '')}".strip()) for n in numbers or []) or not_measured("none")


def severity(s, before=None):
    badge = f'<span class="sev sev-{esc(s)}">{esc(SEVERITY.get(s, s))}</span>'
    if before and before != s:
        badge += f", was {esc(SEVERITY.get(before, before))}"
    return badge


def status_cell(s, hand=False):
    return esc(STATUS.get(s, s)) + (", hand-found" if hand else "")


def not_measured(text="not measured"):
    return f'<span class="muted">{text}</span>'


def value(v, suffix="", blank="not measured"):
    return not_measured(blank) if v in (None, "") else esc(f"{v}{suffix}")


def lines(text):
    return "<br>".join(esc(line) for line in str(text or "").split("\n"))


COLUMN_CLASSES = {"#": "num", "Count": "num", "Findings": "num", "Screens": "num", "Screens before": "num",
                  "Severity": "nowrap", "Date": "nowrap", "Last verified": "nowrap", "Withdrawn in run": "nowrap",
                  "Fixed in run": "nowrap", "First unreachable in run": "nowrap"}


def table(tid, caption, headers, rows, header_col=0, classes=None, row_attrs=None):
    """A captioned data table in a named, focusable scroll region. Cells are HTML; headers are text.
    row_attrs[i], when given, is extra attribute text for row i's header cell."""
    classes = dict(COLUMN_CLASSES, **(classes or {}))
    cls_of = {h: f' class="{classes[h]}"' if h in classes else "" for h in headers}
    head = "".join(f'<th scope="col"{cls_of[h]}>{esc(h)}</th>' for h in headers)
    body = []
    for r, cells in enumerate(rows):
        tds = []
        for i, (h, cell) in enumerate(zip(headers, cells)):
            cls = cls_of[h]
            if i == header_col:
                extra = f" {row_attrs[r]}" if row_attrs else ""
                tds.append(f'<th scope="row"{cls}{extra}>{cell}</th>')
            else:
                tds.append(f"<td{cls}>{cell}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    return (f'<div class="scroll" role="region" aria-labelledby="{tid}-caption" tabindex="0">\n'
            f'<table id="{tid}">\n<caption id="{tid}-caption">{esc(caption)}</caption>\n'
            f"<thead><tr>{head}</tr></thead>\n<tbody>\n" + "\n".join(body) + "\n</tbody>\n</table>\n</div>")


def section(sid, heading, body, level=2):
    return f'<section id="{sid}">\n<h{level}>{heading}</h{level}>\n{body}\n</section>'


def attachment_notes(hf):
    notes = []
    for a in (hf or {}).get("attached") or []:
        state = STATUS.get(a.get("status"), a.get("status") or "unknown").lower()
        notes.append(f'<p class="note">Scanner finding attached: {esc(a.get("title"))} '
                     f'({esc(state)}, {plural(len(a.get("nodeIds") or []), "screen")}).</p>')
    return "".join(notes)


# ---------------------------------------------------------------------------
# Sections, in KTD24's order
# ---------------------------------------------------------------------------

def summary(m):
    counts = m.get("counts") or {}
    total = sum(counts.get(s, 0) for s in SEVERITIES)
    scanner = sum(1 for f in m["findings"] if f.get("source") != "hand-found")
    rows = [[severity(s), str(counts.get(s, 0))] for s in SEVERITIES] + [["Total", str(total)]]
    return (f"<p>This run verified {plural(total, 'failure')}: {scanner} found by the scripted checks and "
            f"{plural(total - scanner, 'hand-found issue')} the verifier reproduced. "
            "Severity is rated by what the failure blocks: High, Medium and Low stand for Critical, Major and Minor "
            'in the accessibility-review format. Findings this run could not retest stay in the map and are listed '
            'under <a href="#not-retested">Not retested</a>, outside these counts.</p>\n'
            + table("t-summary", "Verified failures by severity", ["Severity", "Findings"], rows))


def failures_by_criterion(m):
    """{criterion: {"scanner": n, "hand": n}} over every failure the map holds after this run."""
    out = {}

    def add(criteria, kind):
        for c in criteria or []:
            out.setdefault(c, {"scanner": 0, "hand": 0})[kind] += 1
    for f in m["findings"]:
        add(f.get("criteria"), "hand" if f.get("source") == "hand-found" else "scanner")
    for f in m.get("notRetested") or []:
        add(f.get("criteria"), "scanner")
    for h in m.get("handFound") or []:
        if h.get("status") == "not-retested":
            add(h.get("criteria"), "hand")
    return out


def coverage(m):
    cov = m["coverage"]
    fails = failures_by_criterion(m)
    lh = cov.get("lighthouse") or {}
    lh_text = f"{lh['date']}: {lh.get('line')}" if lh.get("date") else (lh.get("line") or "Lighthouse calibration has not run.")
    builds = ", ".join(f"<code>{esc(b)}</code>" for b in cov.get("builds") or []) or not_measured("none recorded")
    facts = [("Run date", f"{esc(m.get('date'))} (run {esc(m.get('run'))})"),
             ("Screenshots captured", esc(m.get("captured"))),
             ("Standard", esc(m.get("standard"))),
             ("States audited", f"{esc(cov.get('audited'))} of {esc(cov.get('states'))}"),
             ("States unreachable", f'{esc(cov.get("unreachable"))}, listed under '
                                    '<a href="#unreachable">Unreachable states</a>'),
             ("Demo builds seen", builds),
             ("Lighthouse calibration", esc(lh_text))]
    dl = '<dl class="facts">\n' + "\n".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts) + "\n</dl>"

    tested_rows = []
    for c in cov.get("tested") or []:
        f = fails.get(c["number"])
        n = (f["scanner"] + f["hand"]) if f else 0
        tested_rows.append([esc(f"{c['number']} {c['name']}"), esc(c.get("level")), esc(c.get("principle")),
                            esc(c.get("coverage")), esc(", ".join(c.get("checks") or [])), esc(c.get("modes")),
                            esc(plural(n, "finding")) if n else NO_FAILURES])
    untested_rows = []
    for c in cov.get("untested") or []:
        f = fails.get(c["number"]) or {}
        found = ([plural(f["hand"], "hand-found issue")] if f.get("hand") else []) + \
                ([plural(f["scanner"], "scanner finding")] if f.get("scanner") else [])
        result = f"Not tested by these checks; {join_and(found)}" if found else "Not tested"
        untested_rows.append([esc(f"{c['number']} {c['name']}"), esc(c.get("level")), esc(c.get("principle")),
                              esc(result)])
    named = [esc(f"{c['number']} {c['name']}") for c in cov.get("untested") or [] if c.get("named")]
    dims = "\n".join(f"<li>{esc(d)}</li>" for d in cov.get("dimensions") or [])
    return "\n".join([
        dl,
        "<h3>Criteria tested</h3>",
        "<p>Coverage says whether the checks cover a criterion fully or only the failure modes listed next to it. "
        "A tested criterion with no failure reads “no failures found by these checks”: other failures of it can "
        "still exist.</p>",
        table("t-tested", f"WCAG 2.2 A and AA criteria these checks tested ({len(tested_rows)})",
              ["Criterion", "Level", "Principle", "Coverage", "Checks", "Failure modes covered", "Result"],
              tested_rows),
        "<h3>Criteria not tested</h3>",
        f'<p id="named-untested">These checks did not test any criterion below, including {join_and(named)}.</p>'
        if named else '<p id="named-untested">These checks did not test any criterion below.</p>',
        table("t-untested", f"WCAG 2.2 A and AA criteria not tested ({len(untested_rows)})",
              ["Criterion", "Level", "Principle", "Result"], untested_rows),
        "<h3>Other limits</h3>",
        "<p>The audit also did not test:</p>",
        f'<ul id="untested-dimensions">\n{dims}\n</ul>',
        f'<p id="caveat" class="caveat"><strong>Black-box audit.</strong> {esc(cov.get("caveat"))}</p>',
    ])


def changes(m):
    ch = m.get("changes") or {}
    baseline = m.get("baseline")
    intro = ("<p>First audited run: no earlier run to compare.</p>" if baseline in (None, "none")
             else f"<p>Compared with run {esc(baseline)}, the last published run.</p>")

    def counts_table(tid, caption, spec, counts):
        rows = [[f'<a href="#{target}">{esc(STATUS[s])}</a>', str((counts or {}).get(s, 0)), esc(definition)]
                for s, target, definition in spec]
        return table(tid, caption, ["Status", "Count", "Definition"], rows,
                     row_attrs=[f'data-status="{s}"' for s, _, _ in spec])
    return "\n".join([
        intro,
        counts_table("t-changes-findings", "Scanner findings by status", FINDING_CHANGES, ch.get("findings")),
        counts_table("t-changes-hand", "Hand-found issues by status", HAND_CHANGES, ch.get("handFound")),
    ])


def findings(m, evidence, hand, names):
    groups = {}
    for f in m["findings"]:
        groups.setdefault(f.get("principle") or "Other", []).append(f)
    order = list(PRINCIPLES) + sorted(p for p in groups if p not in PRINCIPLES)
    parts = ["<p>New, persisting, re-rated and reappeared findings from the checks, and the hand-found issues the "
             "verifier reproduced in this run. Each issue opens the map with its screens highlighted. The evidence "
             "is what the checks measured and what the verifier saw on the live demo.</p>"]
    for p in order:
        rows = []
        for n, f in enumerate(groups.get(p, []), 1):
            is_hand = f.get("source") == "hand-found"
            rows.append([str(n), issue_link(f["id"], f.get("title")), criteria_cell(f.get("criteria"), names),
                         severity(f.get("severity"), f.get("severityBefore")), esc(f.get("screens")),
                         status_cell(f.get("status"), is_hand), esc(f.get("fix")) if f.get("fix") else not_measured("none recorded"),
                         lines(evidence.get(f["id"]) or f.get("evidence")) + (attachment_notes(hand.get(f["id"])) if is_hand else "")])
        slug = re.sub(r"[^a-z0-9]+", "-", p.lower()).strip("-")
        body = (table(f"t-principle-{slug}", f"{p}: {plural(len(rows), 'finding')}",
                      ["#", "Issue", "WCAG criterion", "Severity", "Screens", "Status", "Recommended fix", "Evidence"],
                      rows, header_col=1, classes={"Recommended fix": "fix", "Evidence": "evidence"})
                if rows else f"<p>{NO_FAILURES}.</p>")
        parts.append(section(f"principle-{slug}", esc(p), body, level=3))
    return "\n".join(parts)


TABLE_SPECS = [
    ("contrast", "contrast", "Contrast",
     "Text contrast as axe measured it, and hand-found color issues. WCAG 2.2 AA asks 4.5:1 for normal text and 3:1 "
     "for large text.",
     ["Element", "Foreground", "Background", "Ratio", "Required"],
     lambda r, b: [value(r.get("element"), blank=b), value(r.get("fg"), blank=b), value(r.get("bg"), blank=b),
                   value(r.get("ratio"), ":1", b), value(r.get("required"), ":1", b)]),
    ("keyboard", "keyboard", "Keyboard",
     "What the Tab walk found: clickable elements Tab never reaches, focus that is not visible, traps and backward "
     "jumps. Arrow keys were not tested.",
     ["Element", "Tab stop", "Previous stop", "Arrow keys"],
     lambda r, b: [value(r.get("element"), blank=b),
                   value(f"{r['stop']} of {r['stops']}" if r.get("stop") and r.get("stops") else r.get("stop"), blank=b),
                   value(r.get("previous"), blank=b), esc(r.get("arrowKeys") or "not tested")]),
    ("dialog", "dialog", "Dialogs",
     "Where focus went when a trigger opened a dialog, menu or popover, and triggers the keyboard cannot open.",
     ["Trigger", "Overlay", "Focus after opening"],
     lambda r, b: [value(r.get("element"), blank=b), value(r.get("overlay"), blank=b), value(r.get("focus"), blank=b)]),
    ("screenReader", "screen-reader", "Screen reader",
     "Names, roles and states as Chrome's accessibility tree exposes them.",
     ["Element", "Announced as", "Message"],
     lambda r, b: [value(r.get("element"), blank=b), value(r.get("announced"), blank=b),
                   value(r.get("message"), blank=b)]),
]


def checks(m, names):
    parts = []
    tables = m.get("tables") or {}
    for key, sid, heading, intro, facts, cells in TABLE_SPECS:
        rows = []
        for r in tables.get(key) or []:
            hand = r.get("rule") == "hand-found"     # a hand-found row has no measured facts
            rows.append([issue_link(r["id"], r.get("title")) if r.get("id") else esc(r.get("title")),
                         criteria_cell([r["criterion"]] if r.get("criterion") else [], names)]
                        + cells(r, "not measured" if hand else "none")
                        + [severity(r.get("severity")), esc(r.get("screens")), status_cell(r.get("status"), hand)])
        headers = ["Issue", "WCAG criterion"] + facts + ["Severity", "Screens", "Status"]
        body = [f"<p>{esc(intro)}</p>",
                table(f"t-{sid}", f"{heading}: {plural(len(rows), 'finding')}", headers, rows) if rows else f"<p>{NO_FAILURES}.</p>"]
        if key == "screenReader":
            body.append('<p class="note">Announced as: computed from Chrome\'s accessibility tree, not a screen '
                        "reader.</p>")
            body.append(advisory(m.get("advisory") or []))
        parts.append(section(sid, esc(heading), "\n".join(body), level=3))
    return "\n".join(parts)


def advisory(items):
    intro = ("<p>Announcements a reviewer flagged that match no WCAG A or AA criterion. They are not failures, are "
             "not counted above and are not in the map.</p>")
    if not items:
        body = "<p>No advisory announcements this run.</p>"
    else:
        body = "<ul>\n" + "\n".join(
            f"<li><code>{esc(a.get('template'))}</code>: {esc(a.get('reason'))} ({plural(a.get('states') or 0, 'state')})</li>"
            for a in items) + "\n</ul>"
    return section("advisory", "Advisory announcements", intro + "\n" + body, level=4)


def priority(m, names):
    by_id = {f["id"]: f for f in m["findings"]}
    items = []
    for iid in m.get("priority") or []:
        f = by_id.get(iid) or {"id": iid, "title": iid}
        crit = join_and(esc(f"{n} {names.get(n, '')}".strip()) for n in f.get("criteria") or [])
        fix = f" {esc(f['fix'])}" if f.get("fix") else ""
        items.append(f"<li>{issue_link(iid, f.get('title'))}: {esc(SEVERITY.get(f.get('severity'), f.get('severity')))}, "
                     f"{plural(f.get('screens') or 0, 'screen')}, WCAG {crit}.{fix}</li>")
    if not items:
        return f"<p>{NO_FAILURES}.</p>"
    return ("<p>Fix in this order: by severity, then by the number of screens affected.</p>\n<ol>\n"
            + "\n".join(items) + "\n</ol>")


def carried(m, hand, names):
    nr_rows = []
    for f in m.get("notRetested") or []:
        lv = f.get("lastVerified") or {}
        last = f"{esc(lv.get('date'))} (run {esc(lv.get('run'))})" if lv.get("date") else not_measured("not recorded")
        nr_rows.append([maybe_link(f.get("id"), f.get("title")), criteria_cell(f.get("criteria"), names),
                        severity(f.get("severity")), esc(f.get("screens")), esc(f.get("reason")), last])
    for h in m.get("handFound") or []:
        if h.get("status") == "not-retested":
            nr_rows.append([maybe_link(h.get("id"), h.get("title")) + ", hand-found",
                            criteria_cell(h.get("criteria"), names), severity(h.get("severityAfter")),
                            esc(h.get("screens")), "This run could not check it." + attachment_notes(h),
                            not_measured("not recorded")])
    w_rows = [[esc(w.get("title")), criteria_cell(w.get("criteria"), names), severity(w.get("severity")),
               esc(w.get("screens")), esc(w.get("reason")), esc(w.get("run"))] for w in m.get("withdrawn") or []]
    f_rows = [[esc(f.get("title")), criteria_cell(f.get("criteria"), names), severity(f.get("severity")),
               esc(f.get("screens")), esc(f.get("run")), esc(f.get("reason") or "Not found on any of its screens.")]
              for f in m.get("fixed") or []]
    definition = {s: d for s, _, d in FINDING_CHANGES}
    none = "<p>None this run.</p>"
    return "\n".join([
        section("not-retested", "Not retested", f"<p>{esc(definition['not-retested'])}</p>\n" + (
            table("t-not-retested", f"Not retested: {plural(len(nr_rows), 'finding')}",
                  ["Finding", "WCAG criterion", "Severity", "Screens", "Why not retested", "Last verified"], nr_rows) if nr_rows else none), level=3),
        section("withdrawn", "Withdrawn", f"<p>{esc(definition['withdrawn'])}</p>\n" + (
            table("t-withdrawn", f"Withdrawn: {plural(len(w_rows), 'finding')}",
                  ["Finding", "WCAG criterion", "Severity", "Screens", "Verifier's reason", "Withdrawn in run"], w_rows) if w_rows else none), level=3),
        section("fixed", "Fixed", f"<p>{esc(definition['fixed'])}</p>\n" + (
            table("t-fixed", f"Fixed: {plural(len(f_rows), 'finding')}",
                  ["Finding", "WCAG criterion", "Severity", "Screens before", "Fixed in run", "Note"], f_rows) if f_rows else none), level=3),
    ])


def not_reproduced(m, names):
    rows = [[issue_link(h["id"], h.get("title")), criteria_cell(h.get("criteria"), names),
             severity(h.get("severityAfter")), esc(h.get("screens")), esc(h.get("reason")),
             esc(h.get("verifierReason")) + attachment_notes(h), esc(h.get("date"))]
            for h in m.get("handFound") or [] if h.get("status") == "not-reproduced"]
    intro = ("<p>Hand-found issues the verifier could not reproduce on the screens this run reached. They stay in "
             "the map with the verifier's note, so each still opens its screens.</p>")
    if not rows:
        return intro + "\n<p>None this run.</p>"
    return intro + "\n" + table("t-not-reproduced", f"Not reproduced: {plural(len(rows), 'issue')}",
                                ["Issue", "WCAG criterion", "Severity", "Screens", "Why", "Verifier's note", "Date"],
                                rows)


def unreachable(m):
    groups = m.get("unreachable") or []
    if not groups:
        return "<p>None: this run reached every state.</p>"
    n = sum(len(g.get("states") or []) for g in groups)
    parts = [f"<p>{plural(n, 'state')} could not be reached in this run, grouped by root cause: the state whose "
             "failure blocked them.</p>"]
    for i, g in enumerate(groups, 1):
        first = g.get("firstUnreachable")
        rows = [[esc(s.get("state")), esc(s.get("reason")), value(s.get("firstUnreachable"))]
                for s in g.get("states") or []]
        body = (f"<p>Reason: {esc(g.get('reason'))}. First unreachable in run "
                f"{esc(first) if first else not_measured('not recorded')}.</p>\n"
                + table(f"t-unreachable-{i}", f"States blocked by {g.get('rootCause')}",
                        ["State", "Reason", "First unreachable in run"], rows))
        parts.append(section(f"unreachable-{i}", f"Root cause: <code>{esc(g.get('rootCause'))}</code>", body, level=3))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def tokens(path=APP_CSS):
    """([(name, value)] for light, [(name, value)] for dark) from the map's :root blocks, fonts left out."""
    with open(path, encoding="utf-8") as f:
        css = re.sub(r"/\*.*?\*/", "", f.read(), flags=re.S)
    light = re.search(r"(?m)^:root\s*\{(.*?)\}", css, re.S)
    dark = re.search(r':root:not\(\[data-theme="light"\]\)\s*\{(.*?)\}', css, re.S)
    if not light or not dark:
        raise ValueError(f"{path}: no light :root block or no dark :root block under prefers-color-scheme")

    def decls(block):
        return [(n, " ".join(v.split())) for n, v in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block)
                if n not in FONT_TOKENS]
    return decls(light.group(1)), decls(dark.group(1))


def page(m, issues, test_data=False):
    names = {c["number"]: c["name"] for c in (m["coverage"].get("tested") or []) + (m["coverage"].get("untested") or [])}
    evidence = {i["id"]: i["audit"]["evidence"] for i in issues.get("generated") or []
                if i.get("id") and (i.get("audit") or {}).get("evidence")}
    evidence.update({hid: ov["audit"]["evidence"] for hid, ov in (issues.get("overlays") or {}).items()
                     if ((ov or {}).get("audit") or {}).get("evidence")})
    hand = {h["id"]: h for h in m.get("handFound") or []}
    bodies = {
        "summary": summary(m),
        "coverage": coverage(m),
        "changes": changes(m),
        "findings": findings(m, evidence, hand, names),
        "checks": checks(m, names),
        "priority": priority(m, names),
        "not-retested-withdrawn-fixed": carried(m, hand, names),
        "not-reproduced": not_reproduced(m, names),
        "unreachable": unreachable(m),
    }
    light, dark = tokens()
    css = CSS.substitute(light=" ".join(f"{n}: {v};" for n, v in light), dark=" ".join(f"{n}: {v};" for n, v in dark))
    return PAGE.substitute(
        css=css + (TEST_DATA_CSS if test_data else ""),
        test_data=(f'<section id="test-data" class="test-data" aria-label="Test data">{esc(mapdata.TEST_DATA_NOTE)}</section>\n'
                   if test_data else ""),
        lede=(f"{esc(m.get('standard'))} audit of the Admin view of demo.mercury.com, run on {esc(m.get('date'))}. "
              "Each finding links to the map, which highlights the screens where it occurs."),
        nav="\n".join(f'<li><a href="#{sid}">{esc(title)}</a></li>' for sid, title in SECTIONS),
        sections="\n".join(section(sid, esc(title), bodies[sid]) for sid, title in SECTIONS),
    )


def read(snapshot_dir, name):
    path = os.path.join(snapshot_dir, name)
    return read_json(path) if os.path.isfile(path) else None


def render(snapshot_dir, dist_dir):
    """Write <dist_dir>/a11y/index.html from the snapshot and return its path; with no snapshot, None."""
    out_dir = os.path.join(dist_dir, "a11y")
    shutil.rmtree(out_dir, ignore_errors=True)
    model = read(snapshot_dir, "report.json") if snapshot_dir and os.path.isdir(snapshot_dir) else None
    if model is None:
        return None
    text = page(model, read(snapshot_dir, "issues.json") or {}, mapdata.is_test_data(snapshot_dir))
    os.makedirs(out_dir)
    path = os.path.join(out_dir, "index.html")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def cli(ws, sub, argv):
    p = argparse.ArgumentParser(prog="audit.py report", description=__doc__.split("\n\n")[0])
    p.add_argument("--dist", default=DEFAULT_DIST, help="the built site to write a11y/index.html into (default dist/)")
    args = p.parse_args(argv)
    path = render(ws.published_dir, os.path.abspath(args.dist))
    if path is None:
        print(f"report: no published snapshot in {ws.published_dir}; wrote nothing", file=sys.stderr)
    print(json.dumps({"report": path}, indent=1))
    return 0
