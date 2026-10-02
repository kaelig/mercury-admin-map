#!/usr/bin/env python3
"""Export the Mercury Admin UX audit as ticket-ready data for AI agents.

Usage:
    python3 tools/export_agent_data.py --out dist/agent [--base-url https://example.netlify.app]

Reads data/*.json and patterns.json and writes, into --out:
    issues.json, issues.jsonl   one ticket-ready record per audit issue
    patterns.json               cross-area patterns framed as epics / parent tickets
    screens.json                compact index of every captured screen
    journeys.json               the user journeys with their steps and actions
    issues-index.json           compact one-line-per-issue index for triage
    tickets/<id>.md             ready-to-paste ticket body per issue
    README.md, AGENTS.md        instructions for agents (same content)
    llms.txt                    llms.txt index (a second copy goes to the parent of --out)

URLs to screenshots and the viewer are relative to the file that contains them
unless --base-url (the site root) is given, in which case they are absolute.

Stdlib only. Exits non-zero if any record is missing a required field.
"""

import argparse
import datetime
import glob
import json
import os
import re
import sys
import urllib.parse
from collections import Counter, defaultdict, deque

import mapdata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_ORIGIN = "https://demo.mercury.com"
AUDIT_SLUG = "mercury-admin-ux-audit"
AUDIT_LABEL = "audit:mercury-admin-2026-09"
CAPTURED = "2026-09-24"  # capture date of the screenshots (see project folder name)
CAPTURE_SETUP = "1440x900 viewport at 2x, headless Chrome"
SCHEMA_VERSION = 2
A11Y_SOURCE_LABEL = "source:a11y-audit"
# Write-time caps for issues on many screens (KTD23): affectedScreens entries in issues.json and
# issues.jsonl, and table rows in the ticket. Records at or under the cap keep full lists and tables.
SCREEN_CAP = 25
TICKET_SCREEN_ROWS = 15
VERDICT_PHRASE = {"confirm": "confirmed", "rerate": "re-rated", "drop": "not reproduced"}
STATUS_PHRASE = {"not-retested": "not retested", "not-reproduced": "not reproduced", "first-audited": "first audited"}
TOMBSTONE_REASON = {
    "fixed": "fixed. The accessibility audit no longer finds it on the public demo.",
    "withdrawn": "withdrawn. The verifier did not reproduce it on the live demo, or it is not an AA failure.",
    "attached": "attached. It matched a hand-found issue, which now carries it.",
}

CAVEAT = (
    "This audit was produced by black-box testing of the public demo at demo.mercury.com, "
    "with no access to Mercury's design system, component library, content style guide, "
    "analytics or roadmap. Some findings may be intentional decisions, artifacts of the demo's "
    "seeded data, or issues Mercury already knows about. Every finding is a hypothesis to verify, "
    "not a confirmed defect. Before filing, check each one against Mercury's internal conventions. "
    "Those are available only to Mercury employees, or to an agent driving a logged-in browser "
    "on their behalf (for example Claude in Chrome or computer use)."
)

SEVERITIES = ("high", "medium", "low")
IMPACTS = ("data-integrity", "broken-task", "accessibility", "consistency", "cosmetic")
CONFIDENCES = ("high", "medium", "low")

# Priority grid: impact x audited severity. Data problems outrank broken tasks and
# accessibility, which outrank consistency, which outranks cosmetic polish.
PRIORITY_GRID = {
    "data-integrity": {"high": 1, "medium": 2, "low": 2},
    "broken-task":    {"high": 1, "medium": 2, "low": 3},
    "accessibility":  {"high": 1, "medium": 2, "low": 3},
    "consistency":    {"high": 2, "medium": 3, "low": 4},
    "cosmetic":       {"high": 3, "medium": 4, "low": 4},
}
IMPACT_PHRASE = {
    "data-integrity": "the UI shows wrong, contradictory or lost data",
    "broken-task": "a flow blocks, misleads or loses work",
    "accessibility": "assistive-technology users get missing or wrong semantics",
    "consistency": "the same thing looks or reads differently across screens",
    "cosmetic": "visual polish with little effect on comprehension",
}
PRIORITY_MEANING = {
    1: "P1 (urgent)",
    2: "P2 (high)",
    3: "P3 (medium)",
    4: "P4 (low)",
}

CATEGORY_CHECKS = {
    "copy": "Find the content style guide rule that applies (capitalization, punctuation, terminology or voice) and quote it in the ticket. If the guide allows both variants, do not file.",
    "data-format": "Find the canonical date, time, number or currency format in the design-system or content docs, and the shared formatter or component that implements it. Name it in the ticket.",
    "component": "Find the canonical component and variant for this role in the component library. Check whether the off-pattern instance is a known legacy component with a migration already planned.",
    "interaction": "Ask the owning team or read the product spec to confirm the behavior is not intentional (for example a deliberate shortcut, or a step skipped for low-risk actions).",
    "layout": "Check the design system's spacing, overlay and layout guidance. Reproduce at the audit's 1440x900 viewport and at a common smaller laptop size.",
    "a11y": "Confirm in the accessibility tree (DevTools Accessibility pane) or with a screen reader, and cite the WCAG success criterion and any Mercury accessibility standard.",
    "color": "Check the color token definitions for the semantic meaning of each tint involved (status, warning, success, money in/out) in both themes.",
    "typography": "Check the type scale tokens for the size, weight and heading level this role should use.",
    "iconography": "Check the icon guidelines for placement relative to labels and which icon set applies.",
}

# For data and flow bugs the useful check is about the behavior, not the style guide.
IMPACT_CHECKS = {
    "data-integrity": "Tell a display bug from a data bug: reload, then compare with the same record on another screen or in the API response. Note which one is wrong in the ticket.",
    "broken-task": "Ask the owning team or read the product spec to confirm the behavior is not intentional, and note whether work or data is lost or only the flow is confusing.",
}

# Epic framing for each cross-area pattern. Keyed by pattern id in patterns.json.
EPICS = {
    "p-dates": {
        "epicTitle": "Adopt one date and time format standard across the Admin app",
        "goal": "Every date, time and date range in the Admin app is written the same way for the same purpose, with the year shown when it is not the current year.",
        "approach": "Agree the standard (short for tables, long for confirmations, rules for relative words and ranges) with content design, implement it once in a shared formatter, then migrate each area. File one child ticket per area or surface that still formats dates locally.",
        "doneWhen": "A spot check of tables, detail panels, confirmations and badges in every area shows only the formats the standard allows.",
    },
    "p-money": {
        "epicTitle": "Render all money through one amount component",
        "goal": "Cents, negatives, abbreviations (K/M) and foreign currencies look the same wherever an amount appears.",
        "approach": "Confirm which money component is canonical, document its variants (table, headline, compact, input), and move each surface onto it. Child tickets per surface; treat wrong currency symbols as bugs, not polish.",
        "doneWhen": "No surface renders amounts outside the shared component, and one minus character, one abbreviation case and one currency convention are used everywhere.",
    },
    "p-case": {
        "epicTitle": "Apply sentence case to every UI label",
        "goal": "Dialog titles, buttons, badges, menu items and table headers follow the product's capitalization rule.",
        "approach": "Confirm the rule and its exceptions (product names, proper nouns) in the content style guide, then sweep strings per area. A lint rule or string review in CI would stop it from coming back.",
        "doneWhen": "No Title Case label sits next to a sentence-case twin for the same concept.",
    },
    "p-validation": {
        "epicTitle": "Use one validation pattern for required and invalid fields",
        "goal": "An empty or invalid required field is reported the same way in every form, with error copy in one voice.",
        "approach": "Pick the canonical pattern (inline message under the field, submit enabled or not) with design systems, publish it, and migrate forms area by area. Child tickets per form family.",
        "doneWhen": "Submitting any audited form with empty required fields produces the same kind of message in the same place, with consistent wording and punctuation.",
    },
    "p-buttons": {
        "epicTitle": "Use one button component per role in multi-step flows",
        "goal": "Back, dismiss, acknowledge and secondary actions use the same component, size, emphasis and label across flows.",
        "approach": "Document the role-to-variant mapping (for example Back is always secondary, 40px, with a chevron) and migrate flows that deviate. One child ticket per flow.",
        "doneWhen": "Every step of each audited flow uses the documented variant for each role.",
    },
    "p-disabled": {
        "epicTitle": "Make a dimmed button mean disabled, everywhere",
        "goal": "A button that looks disabled cannot be activated, and a button that can be activated does not look disabled.",
        "approach": "Decide whether incomplete forms disable submit or validate on click, then remove look-alike dimmed styles that still submit. Child tickets per surface.",
        "doneWhen": "No button in the audited surfaces is dimmed while still clickable, and dimmed buttons expose a disabled state to assistive technology.",
    },
    "p-badges": {
        "epicTitle": "Give each status color one meaning",
        "goal": "Badge colors map to a single semantic (success, warning, error, neutral) and failures never use success colors.",
        "approach": "Audit status-to-token mapping against the color token docs, fix the mapping centrally, then fix one-off badges. Child tickets per status family.",
        "doneWhen": "Each status tint is used for one meaning across areas, and failed states use the error or warning tint.",
    },
    "p-empty": {
        "epicTitle": "Use one empty-state pattern for tables and panels",
        "goal": "Empty tables, panels and empty values share one layout and one placeholder glyph.",
        "approach": "Confirm the canonical empty-state component and the empty-value glyph, then migrate surfaces. Empty states must never render over loading skeletons.",
        "doneWhen": "Every audited empty table and panel uses the shared pattern and one placeholder for missing values.",
    },
    "p-destructive": {
        "epicTitle": "Confirm destructive and risky actions consistently",
        "goal": "Declines, cancels, deletes and permission escalations follow one confirmation rule and one dialog template.",
        "approach": "Write the rule (which actions need confirmation, and the dialog template) with design and risk owners, then apply it. Missing confirmations on money or permission changes go first.",
        "doneWhen": "Every destructive or permission-widening action in the audited areas follows the rule, and delete dialogs share one template.",
    },
    "p-feedback": {
        "epicTitle": "Give every completed action the same kind of success feedback",
        "goal": "Comparable actions end with the same feedback (toast, inline update or success screen) and toast copy follows one template.",
        "approach": "Define when to use a toast versus a success screen, and a toast copy template, then fix silent saves first and wording second.",
        "doneWhen": "No audited action completes silently, and toasts for comparable outcomes are worded the same way.",
    },
    "p-a11y": {
        "epicTitle": "Give every control and dialog title correct semantics",
        "goal": "Icon buttons have accessible names, tabs are real tabs, and dialog titles are headings at the right level.",
        "approach": "Fix shared components first (icon button, kebab menu, tabs, modal header) so most instances are fixed at once, then fix one-offs. Add an automated accessibility check to CI if one does not exist.",
        "doneWhen": "An accessibility-tree pass over the audited screens finds no unnamed buttons, no clickable divs acting as tabs, and a heading for every dialog title.",
    },
    "p-utc": {
        "epicTitle": "Stamp user-facing dates in the user's local time zone",
        "goal": "Records created in the evening (US time) are dated today, not tomorrow.",
        "approach": "Find where created and default dates are computed in UTC and switch to the account's or user's time zone. It changes the dates users see and may change stored dates, so it ranks above formatting work.",
        "doneWhen": "Creating a token, journal entry or bill late in the day in a US time zone shows today's date everywhere it appears.",
    },
    "p-stale": {
        "epicTitle": "Refresh lists and counts after every action",
        "goal": "After paying, approving, canceling or editing, the list, its counts and its totals reflect the change without a reload.",
        "approach": "Check each action's cache invalidation or optimistic update. Fix per surface; the shared cause may be one data-fetching layer.",
        "doneWhen": "Each audited action updates its list, tab counts and totals immediately.",
    },
    "p-masking": {
        "epicTitle": "Mask account and routing numbers with one rule",
        "goal": "Account numbers are masked the same way (same dot count and visible digits) everywhere, including review screens.",
        "approach": "Confirm the masking rule with security and design, implement it in one shared formatter, and apply it to lists, cards and review steps. Unmasked numbers on review screens go first.",
        "doneWhen": "Every audited surface masks account numbers identically, and none shows a full number unless the user reveals it.",
    },
}

# ---------------------------------------------------------------------------
# Curated ticket copy, one entry per issue id. Written by reading each issue's
# detail, evidence and screens. Issues missing here fall back to generated copy
# (and the script warns). Fields: title, summary, userImpact, fixHypothesis,
# acceptance, verify, searchTerms, impact, confidence, confidenceReason,
# demoArtifactRisk.
# ---------------------------------------------------------------------------
# BEGIN CURATED
CURATED = {'accounts.i-account-masking': {'title': 'Mask account numbers the same way everywhere in Accounts',
                                'summary': "Checking shows '••••••1038', savings '•••••7658' and lists "
                                           "'••1038', while Review transfer prints the Treasury account "
                                           "number in full as 'Treasury 3713286'.",
                                'userImpact': 'The full number on Review transfer shows account details that '
                                              'every other screen hides. The varying dot counts are minor '
                                              'but suggest number lengths that may not exist.',
                                'fixHypothesis': 'Masking seems to be applied per screen, and Review '
                                                 'transfer seems to skip it. One masking helper with fixed '
                                                 'short and long forms, used on review screens too, would '
                                                 'likely resolve it.',
                                'acceptance': ['Review transfer shows the Treasury account number masked '
                                               'like other accounts.',
                                               'Checking and savings detail pages mask account numbers with '
                                               'the same number of dots.',
                                               'List and detail views each use one documented mask.'],
                                'verify': ['Check the design-system or security guidelines for the standard '
                                           'account mask and whether full numbers are allowed on review '
                                           'screens.',
                                           "Check whether 'Treasury 3713286' and the differing routing "
                                           'numbers (132456789 vs 123456789) are demo seed data.'],
                                'searchTerms': ['account number masking',
                                                'unmasked account number',
                                                'masked digits dots'],
                                'impact': 'consistency',
                                'confidence': 'high',
                                'confidenceReason': 'Masked strings and element styles captured with '
                                                    'DevTools on three screens.',
                                'demoArtifactRisk': True},
 'accounts.i-authorization-copy': {'title': 'Format authorization disclaimers the same way in every Accounts '
                                            'flow',
                                   'summary': "Consent lines quote the button as 'Create rule,' (straight "
                                              'quotes), “Transfer,” (curly quotes) or Confirm Payment '
                                              "(unquoted Title Case, though the button reads 'Confirm "
                                              "payment'), and sit above or below buttons.",
                                   'userImpact': 'The lines still authorize the right action, but legal copy '
                                                 "that names 'Confirm Payment' when the button reads "
                                                 "'Confirm payment' is a visible mismatch. Admins also find "
                                                 'the line in a different place in each flow.',
                                   'fixHypothesis': 'These lines seem to be written per flow. A shared '
                                                    'authorization pattern with fixed quoting, button-name '
                                                    'casing and position would likely resolve it, subject to '
                                                    'legal review.',
                                   'acceptance': ['Every authorization line quotes the button name the same '
                                                  "way and matches the button's casing exactly.",
                                                  "'By clicking Save' has the same punctuation in both "
                                                  'credit autopay modals.',
                                                  'Authorization lines sit in the same position relative to '
                                                  'the buttons in rule review, payment review, autopay and '
                                                  'Review transfer.'],
                                   'verify': ['Check whether legal or compliance owns these strings and '
                                              'whether any wording difference is required.',
                                              'Check the content style guide for how UI element names are '
                                              'quoted in running text.'],
                                   'searchTerms': ['authorization disclaimer copy',
                                                   'By clicking',
                                                   'consent text quotes'],
                                   'impact': 'consistency',
                                   'confidence': 'high',
                                   'confidenceReason': 'Disclaimer strings quoted verbatim, including the '
                                                       'quote characters.',
                                   'demoArtifactRisk': False},
 'accounts.i-autopay-rule-mismatch': {'title': 'List credit autopay in the Auto transfer rules tab',
                                      'summary': "The accounts list shows 'Autopay from Ops / Payroll, Every "
                                                 "22nd of the month or if <80% of limit left' in the Auto "
                                                 'transfer rules column, but that tab shows its empty state.',
                                      'userImpact': 'An admin checking which rules move money sees none in '
                                                    'the rules tab, and may create a duplicate rule or not '
                                                    'realize credit autopay is already running.',
                                      'fixHypothesis': 'Credit autopay seems to be stored apart from auto '
                                                       'transfer rules but shown in the rules column. '
                                                       'Listing it in the tab with a link to Autopay '
                                                       'settings, or moving it out of that column, would '
                                                       'likely remove the contradiction.',
                                      'acceptance': ['The Auto transfer rules column and the Auto transfer '
                                                     'rules tab agree on whether any rules exist.',
                                                     'If credit autopay appears in the rules column, the '
                                                     'rules tab lists it or links to Autopay settings.'],
                                      'verify': ['Check whether excluding credit autopay from auto transfer '
                                                 'rules is a deliberate product decision.',
                                                 'Check whether demo data seeds the autopay text without a '
                                                 'matching rule.'],
                                      'searchTerms': ['autopay auto transfer rules',
                                                      'rules tab empty',
                                                      'credit autopay column'],
                                      'impact': 'data-integrity',
                                      'confidence': 'medium',
                                      'confidenceReason': 'Both texts quoted from the page, but the split '
                                                          'may be intentional or seeded.',
                                      'demoArtifactRisk': True},
 'accounts.i-back-labels': {'title': 'Use one label for Back and Cancel actions in Accounts flows',
                            'summary': "The dismiss-or-previous action reads 'Cancel', 'Back', 'Go back', "
                                       "'Go Back' or '‹ Edit' depending on the flow, and one rule form "
                                       "switches between 'Back' and 'Cancel' by entry point.",
                            'userImpact': "Admins can't predict whether the button steps back or abandons "
                                          'the flow, which matters in the middle of a payment. The shared '
                                          "links dialog relabelling 'Close' to 'Cancel' once a row is ticked "
                                          'adds to the doubt.',
                            'fixHypothesis': 'Each flow seems to choose its own label. Setting one label for '
                                             'step-back and one for dismiss, and applying them to these '
                                             'modals, would likely resolve it.',
                            'acceptance': ['Multi-step flows (credit payment, venture debt draw, SAFE, rule '
                                           'forms) use the same step-back label and casing.',
                                           'Single-step modals in Accounts use one dismiss label.',
                                           'A rule form shows the same secondary label whichever entry point '
                                           'opened it.',
                                           'The credit payment review step uses the same back label as the '
                                           'step before it.'],
                            'verify': ["Check the content style guide for whether 'Back' and 'Go back' are "
                                       "both allowed, and when 'Cancel' is preferred.",
                                       'Check the design-system modal docs for the prescribed secondary '
                                       'action label in multi-step modals.'],
                            'searchTerms': ['Go back vs Back', 'Cancel button label', 'modal back action'],
                            'impact': 'consistency',
                            'confidence': 'high',
                            'confidenceReason': 'Accessible button names captured from accessibility '
                                                'snapshots in each flow.',
                            'demoArtifactRisk': False},
 'accounts.i-button-sizes': {'title': 'Use the standard dialog button size in Remove shared document links',
                             'summary': 'Dialog buttons are 40px tall with 8px 32px padding in Create '
                                        'account and limit increase, but 32px with 4px 16px in Remove shared '
                                        "document links; 'View loan details' has a 16px label.",
                             'userImpact': 'Cosmetic. The smaller buttons in the shared links dialog, '
                                           'including the destructive one, are smaller targets but still '
                                           'work.',
                             'fixHypothesis': 'The shared links dialog seems to use the small button size, '
                                              'and the venture debt success link a larger text style. Moving '
                                              'both to the size other dialogs use would likely resolve it.',
                             'acceptance': ['Buttons in Remove shared document links are 40px tall with the '
                                            'same padding as other dialogs.',
                                            "'View loan details' uses the same label size (13.33px) as other "
                                            'primary buttons.'],
                             'verify': ['Check the design-system button docs for the size prescribed in '
                                        'dialogs and on full-page success screens.'],
                             'searchTerms': ['button size dialog',
                                             'small button modal',
                                             'View loan details button'],
                             'impact': 'cosmetic',
                             'confidence': 'high',
                             'confidenceReason': 'Font size, padding and height measured with DevTools on '
                                                 'four screens.',
                             'demoArtifactRisk': False},
 'accounts.i-case-mixing': {'title': 'Use one capitalization style for Accounts labels and buttons',
                            'summary': "The same labels switch case between screens: 'Bank Letters' vs 'Bank "
                                       "letters', 'Net yield' vs 'Net Yield', 'Post-money' vs 'Post-Money', "
                                       "'Make Another Request' vs 'Make another transfer'.",
                            'userImpact': 'A polish issue. Nothing is blocked, but the same term capitalized '
                                          'two ways on adjacent screens looks unreviewed.',
                            'fixHypothesis': 'Labels seem to be written separately per surface. Checking '
                                             'Accounts, Treasury, Financing and SAFE strings against the '
                                             "style guide's case rule would likely be a copy-only fix.",
                            'acceptance': ['The Documents menu and Documents sub-nav use the same case for '
                                           "'Bank letters' and 'Wire details'.",
                                           "'Net yield', 'Ending balance', 'Valuation cap' and 'Post-money' "
                                           'each use one case everywhere they appear.',
                                           'Buttons in the limit increase modal and venture debt success '
                                           'page follow the same case as other Accounts buttons.',
                                           "Status badges such as 'Draw Request Pending' and 'Up to date' "
                                           'use one case.'],
                            'verify': ['Check the content style guide for the case rule for buttons, labels, '
                                       'toasts and badges.',
                                       "Check whether 'SAFE', 'IO' and product names like 'Venture Debt' are "
                                       'proper nouns that keep their capitals.'],
                            'searchTerms': ['Title Case sentence case',
                                            'capitalization inconsistent',
                                            'button label case'],
                            'impact': 'consistency',
                            'confidence': 'high',
                            'confidenceReason': 'Each pair quoted verbatim from rendered text on named '
                                                'screens.',
                            'demoArtifactRisk': False},
 'accounts.i-cents-format': {'title': 'Show cents the same way on every Accounts balance',
                             'summary': 'Balances show cents as superscript in the Accounts list, inline on '
                                        'detail pages, and not at all in SAFE totals, payment success copy '
                                        "and limits; '$21,249 available' sits above '$21,248.88'.",
                             'userImpact': "An admin comparing the credit header ('$21,249 available') with "
                                           "Show details ('$21,248.88') sees two numbers for one balance. "
                                           'The venture debt confirmation drops the cents that the review '
                                           "step showed ('$250,000.00'), so the two screens look like "
                                           'different amounts.',
                             'fixHypothesis': 'Rounding and superscript seem to be decided per screen rather '
                                              'than by one money formatter. Defining when cents are shown, '
                                              'and when they are raised, as named variants would likely '
                                              'remove the drift.',
                             'acceptance': ['The credit card balance renders cents the same way in the '
                                            'Accounts list and on its detail page.',
                                            "The credit header's available amount matches 'Available to "
                                            "spend' in Show details, or is labelled as rounded.",
                                            'The venture debt confirmation shows the amount in the same '
                                            'format as the review step.',
                                            'SAFE totals and SAFE table amounts follow one cents rule.'],
                             'verify': ['Check the content style guide for when amounts may drop cents or be '
                                        "abbreviated ('$5K').",
                                        'Check the design-system money display docs for whether superscript '
                                        'cents are reserved for specific contexts.',
                                        "Check whether appending 'USD' on confirmation screens is a "
                                        'documented pattern.'],
                             'searchTerms': ['superscript cents',
                                             'balance rounding',
                                             'currency format Accounts',
                                             '$21,249 available'],
                             'impact': 'consistency',
                             'confidence': 'high',
                             'confidenceReason': 'Class names, computed styles and verbatim amounts captured '
                                                 'with DevTools on several screens.',
                             'demoArtifactRisk': False},
 'accounts.i-date-formats': {'title': 'Use one date format across Accounts and Financing',
                             'summary': 'Accounts and Financing show dates in at least six formats, '
                                        "including 'Sep 24', 'October 1', 'Oct 01', 'Jan 1 2027' and "
                                        "'September 24-25, 2026'; Working Capital shows two side by side.",
                             'userImpact': 'Admins track loan due dates, autopay days and transfer windows '
                                           'here. Zero-padded days, missing commas and dropped years make '
                                           'dates slower to compare and easier to misread when a payment is '
                                           'due.',
                             'fixHypothesis': 'Each screen seems to format dates on its own. Sending '
                                              'Accounts and Financing dates through one shared formatter '
                                              'with a short and a long variant would likely resolve most '
                                              'cases.',
                             'acceptance': ['Working Capital Upcoming payments and Activity use the same '
                                            'date format and text style.',
                                            'No date in Accounts or Financing shows a zero-padded day such '
                                            "as 'Oct 01'.",
                                            'Dates that include a year use the punctuation the style guide '
                                            "requires, for example 'Jan 1, 2027'.",
                                            'The credit autopay pill and the autopay modal show dates in the '
                                            'same format.'],
                             'verify': ['Look up the date standard in the content style guide and note which '
                                        'of the observed formats it allows.',
                                        "Check whether full month names ('October 1') are allowed in "
                                        'upcoming-payment lists.'],
                             'searchTerms': ['date format Accounts',
                                             'zero-padded date',
                                             'Financing dates inconsistent'],
                             'impact': 'consistency',
                             'confidence': 'high',
                             'confidenceReason': 'Formats quoted verbatim from page text, with computed '
                                                 'styles for the Working Capital pair.',
                             'demoArtifactRisk': False},
 'accounts.i-destructive-confirm': {'title': 'Confirm before removing an auto transfer rule',
                                    'summary': 'Remove rule deletes an auto transfer rule immediately with '
                                               'only a toast, while Unlink account asks for confirmation; '
                                               'saving loan autopay closes the modal with no toast, unlike '
                                               'other saves.',
                                    'userImpact': 'A misclick on Remove rule deletes a rule that moves money '
                                                  'on a schedule, with no chance to back out. After saving '
                                                  "loan autopay, admins can't tell whether the change took.",
                                    'fixHypothesis': 'Remove rule seems to skip the confirmation step Unlink '
                                                     'uses. Adding the same confirm dialog, or an undo in '
                                                     'the toast, and a success toast for loan autopay would '
                                                     'likely resolve both.',
                                    'acceptance': ['Remove rule asks for confirmation, or its toast offers '
                                                   'Undo, before the rule is gone.',
                                                   'Saving loan autopay shows a success toast or inline '
                                                   'confirmation.',
                                                   'Destructive confirmations in Accounts use the same '
                                                   'dialog pattern as Unlink account.'],
                                    'verify': ['Check the design-system docs for when a destructive action '
                                               'needs a confirmation dialog versus an undo toast.',
                                               'Check whether immediate rule removal is a deliberate product '
                                               'decision.'],
                                    'searchTerms': ['remove rule no confirmation',
                                                    'delete auto transfer rule',
                                                    'loan autopay no toast'],
                                    'impact': 'broken-task',
                                    'confidence': 'high',
                                    'confidenceReason': 'Flow observed end to end, with the DOM checked for '
                                                        'dialogs and toast text afterwards.',
                                    'demoArtifactRisk': False},
 'accounts.i-dismiss-emphasis': {'title': 'Use one button style for acknowledge-only dialog actions',
                                 'summary': "Acknowledge-only dialogs use a primary blue button ('Got it', "
                                            "'Done', 'Close'), except the Treasury net return dialog, whose "
                                            "'Done' is a grey tonal button.",
                                 'userImpact': "Minor. The net return 'Done' looks like a secondary option, "
                                               'which can leave an admin looking for a primary action that '
                                               "isn't there.",
                                 'fixHypothesis': 'The net return dialog seems to use the secondary variant '
                                                  'for its only action. Switching it to the variant other '
                                                  'info dialogs use would likely resolve it.',
                                 'acceptance': ["The Treasury net return 'Done' button uses the same variant "
                                                "as 'Got it' in How your IO limit works.",
                                                'Every single-action info dialog in Accounts uses one button '
                                                'variant.'],
                                 'verify': ['Check the design-system dialog docs for whether a lone '
                                            'acknowledge action is primary or secondary.'],
                                 'searchTerms': ['Done button secondary',
                                                 'info dialog button',
                                                 'net return dialog'],
                                 'impact': 'cosmetic',
                                 'confidence': 'high',
                                 'confidenceReason': 'Background and text colors measured with DevTools on '
                                                     'both buttons.',
                                 'demoArtifactRisk': False},
 'accounts.i-documents-control': {'title': "Make Treasury's Documents control match checking and savings",
                                  'summary': 'Documents is a 13.33px chevron dropdown on checking and '
                                             'savings detail, but on Treasury it is a 16px link, icon on the '
                                             'other side, that goes straight to Statements.',
                                  'userImpact': 'Admins who expect the Statements / Bank Letters / Wire '
                                                'Details menu are sent straight to Statements on Treasury. '
                                                'The size and icon difference is minor on its own.',
                                  'fixHypothesis': 'Treasury seems to use a link button where the other '
                                                   'pages use a menu button. If Treasury has only '
                                                   'statements, keeping the link but matching size and icon '
                                                   'position would likely be enough.',
                                  'acceptance': ['Documents on Treasury has the same font size, padding and '
                                                 'icon position as on checking.',
                                                 'Documents is the same control type on every account detail '
                                                 'page, or the difference is labelled.'],
                                  'verify': ['Check whether Treasury accounts offer bank letters or wire '
                                             'details; if not, the link may be deliberate.',
                                             'Check the design-system button docs for how a '
                                             'single-destination action looks next to menu buttons.'],
                                  'searchTerms': ['Documents dropdown Treasury',
                                                  'account Documents button',
                                                  'Treasury statements link'],
                                  'impact': 'consistency',
                                  'confidence': 'high',
                                  'confidenceReason': 'Element types, font sizes and padding measured with '
                                                      'DevTools on both pages.',
                                  'demoArtifactRisk': False},
 'accounts.i-error-copy': {'title': 'Write Accounts form errors in one voice',
                           'summary': "Required-field errors read 'You must make a selection.', 'Select an "
                                      "account', 'Please choose an account' and 'Please complete this "
                                      "field'; limit errors read 'Must be <= $12,505.87' or 'Request amount "
                                      "may not exceed $1,000,000.00.'",
                           'userImpact': "Most messages are understandable, but '<=' is a raw operator that "
                                         'reads as code to a finance user. Mixed tone and punctuation make '
                                         'the forms feel unedited.',
                           'fixHypothesis': 'Each form seems to carry its own messages. Moving them to a '
                                            'small set of shared validation strings written to the style '
                                            'guide would likely resolve it.',
                           'acceptance': ["No error message shows a raw operator such as '<='.",
                                          'Required-field errors follow one pattern for tone, use of '
                                          "'Please' and end punctuation.",
                                          'Over-limit errors on credit payment and venture debt use the same '
                                          'phrasing.'],
                           'verify': ["Check the content style guide's error message rules for 'Please', "
                                      'imperatives and end punctuation.',
                                      'Check whether shared validation strings exist that these forms are '
                                      'meant to use.'],
                           'searchTerms': ['error message copy', 'Must be <=', 'validation message tone'],
                           'impact': 'consistency',
                           'confidence': 'high',
                           'confidenceReason': 'Error strings read verbatim from DOM text on seven forms.',
                           'demoArtifactRisk': False},
 'accounts.i-heading-levels': {'title': 'Give Accounts modal titles a consistent heading level',
                               'summary': 'Modal titles are h1 (Autopay settings, Edit autopay accounts), h2 '
                                          '(Create account, Distribute funds across accounts) or h3 '
                                          '(Maintain a target account balance), and the share-statement '
                                          'sheet has no heading.',
                               'userImpact': 'Screen reader users navigating by heading get a different '
                                             'outline in each modal, and two sibling rule forms sit at '
                                             "different levels. The share sheet can't be found by heading at "
                                             'all.',
                               'fixHypothesis': 'Modals seem to set their own heading level. Having the '
                                                'modal header render one fixed level, and giving the share '
                                                'sheet a title, would likely resolve it.',
                               'acceptance': ['All modal titles in Accounts are exposed at the same heading '
                                              'level.',
                                              "'Maintain a target account balance' and 'Distribute funds "
                                              "across accounts' have the same heading level.",
                                              'The share-statement sheet has a visible title exposed as a '
                                              'heading.'],
                               'verify': ['Check the design-system accessibility docs for the required '
                                          'heading level inside modals and full-screen sheets.'],
                               'searchTerms': ['modal heading level',
                                               'dialog h1 h2 h3',
                                               'share statement no heading'],
                               'impact': 'accessibility',
                               'confidence': 'high',
                               'confidenceReason': 'Heading levels read from accessibility snapshots and a '
                                                   'DOM query.',
                               'demoArtifactRisk': False},
 'accounts.i-io-wordmark': {'title': "Render 'IO' in the How your IO limit works dialog title",
                            'summary': "The dialog title wraps 'IO' in a span set to the 'IO' font; the font "
                                       "reports loaded but draws no glyphs, so the title reads 'How your   "
                                       "limit works'.",
                            'userImpact': 'Sighted admins see a title with a gap and may not know which '
                                          "limit it explains. Screen readers still read 'IO', so the problem "
                                          'is visual.',
                            'fixHypothesis': 'The IO font may not contain glyphs for these characters, or '
                                             'may be meant for a logo rather than running text. Rendering IO '
                                             'in Arcadia as elsewhere, or using the wordmark as an image '
                                             'with alt text, would likely fix it.',
                            'acceptance': ["'IO' is visible in the How your IO limit works title.",
                                           "The title's 'IO' matches how 'Mercury IO' renders elsewhere, or "
                                           'uses the approved wordmark asset.'],
                            'verify': ['Check the brand or design-system docs for when the IO wordmark font '
                                       'may be used inside headings.'],
                            'searchTerms': ['IO font missing',
                                            'How your IO limit works',
                                            'IO wordmark blank'],
                            'impact': 'consistency',
                            'confidence': 'high',
                            'confidenceReason': 'innerHTML, FontFace load status and span size captured, '
                                                'plus two screenshots.',
                            'demoArtifactRisk': False},
 'accounts.i-last-month-toggle': {'title': 'Label which month the credit activity table is showing',
                                  'summary': "On Sep 24 the credit activity table lists 'Sep 30' rows; after "
                                             "'Last Month' it lists 'Aug 30' rows while the heading stays "
                                             "'This month' and the control stays 'Last Month'.",
                                  'userImpact': "An admin reviewing card spend can't tell which month's rows "
                                                'they are reading, and the cashback and spend summary under '
                                                "'This month' no longer matches the table.",
                                  'fixHypothesis': 'The control seems to swap table data without updating '
                                                   'the heading, summary or its own label. Driving all three '
                                                   'from the selected period would likely resolve it; the '
                                                   'future-dated rows may come from seeded demo data.',
                                  'acceptance': ['After selecting last month, the heading names that month '
                                                 'and the summary matches the rows.',
                                                 'The control shows which period is selected and offers a '
                                                 'way back to the current month.'],
                                  'verify': ["Check whether the future-dated 'Sep 30' rows come from demo "
                                             "seed data or the demo's frozen clock.",
                                             'Check whether the summary is meant to stay on the current '
                                             'month by design.'],
                                  'searchTerms': ['Last Month toggle',
                                                  'credit activity This month',
                                                  'credit card period filter'],
                                  'impact': 'data-integrity',
                                  'confidence': 'medium',
                                  'confidenceReason': 'Page text counted before and after the click; the '
                                                      'dates may come from demo data.',
                                  'demoArtifactRisk': True},
 'accounts.i-modal-title-scale': {'title': 'Use the standard modal title size in Link bank account',
                                  'summary': 'Modal titles are 19px Arcadia Text h2 headings in Create '
                                             'account, How your IO limit works and Disbursement Agreement, '
                                             'but the Link bank account question is a 28px Arcadia Display '
                                             'span.',
                                  'userImpact': 'Visual polish. The Link bank title uses the size the '
                                                'product uses for page balances, so this modal looks louder '
                                                'than its siblings.',
                                  'fixHypothesis': 'The Link bank modal seems to style its title directly '
                                                   'instead of using the modal header. Moving it to the '
                                                   'standard modal title would likely also give it heading '
                                                   'semantics.',
                                  'acceptance': ['The Link bank account type question renders at the same '
                                                 'size and font as other modal titles.',
                                                 'The Link bank account title is exposed as a heading.'],
                                  'verify': ['Check the design-system modal docs for the prescribed title '
                                             'style and whether a Display size is allowed for question-style '
                                             'titles.'],
                                  'searchTerms': ['modal title size',
                                                  'Link bank account modal',
                                                  'Arcadia Display modal'],
                                  'impact': 'cosmetic',
                                  'confidence': 'high',
                                  'confidenceReason': 'Element types, font sizes and families measured with '
                                                      'DevTools.',
                                  'demoArtifactRisk': False},
 'accounts.i-plus-icon-position': {'title': "Place the plus icon before the label on 'Create new SAFE'",
                                   'summary': "'Create new SAFE +' puts the plus icon after its label, while "
                                              "'+ Create rule', '+ Deposit', '+ Order Checkbook' and '+ "
                                              "Create account' put it before.",
                                   'userImpact': "Cosmetic. It doesn't affect use; the SAFEs page is just "
                                                 'the only place the create action reads this way.',
                                   'fixHypothesis': 'The SAFEs button seems to pass the icon as a trailing '
                                                    'icon. Switching it to a leading icon would likely '
                                                    'resolve it.',
                                   'acceptance': ["The plus icon on 'Create new SAFE' renders before the "
                                                  'label.',
                                                  "'Create new SAFE' uses the same padding as other "
                                                  'leading-icon buttons (4px 16px 4px 12px).'],
                                   'verify': ['Check the design-system button docs for leading versus '
                                              'trailing icon rules on create actions.'],
                                   'searchTerms': ['plus icon position',
                                                   'Create new SAFE button',
                                                   'trailing icon'],
                                   'impact': 'cosmetic',
                                   'confidence': 'high',
                                   'confidenceReason': 'Child element order and padding inspected in the '
                                                       'DOM.',
                                   'demoArtifactRisk': False},
 'accounts.i-range-format': {'title': 'Write business-day ranges the same way in Accounts',
                             'summary': "Ranges appear as '1–2 business days' (en dash), '1 to 5 business "
                                        "days', '0-1 business days' (hyphen), 'September 24-25, 2026' and "
                                        "'August 1-31', sometimes two styles on one page.",
                             'userImpact': 'Minor. Each range is readable, but Add funds and Edit portfolio '
                                           'each mix two styles on one screen, which reads as unedited copy.',
                             'fixHypothesis': 'The strings seem to have been written separately per flow. '
                                              "Applying the style guide's range rule to them would likely be "
                                              'a copy-only change.',
                             'acceptance': ['Add funds uses one range style for every settlement time.',
                                            'Edit portfolio uses one range style for both timing notes.',
                                            'Date ranges on the transfer success screen and net return modal '
                                            'use the same separator as business-day ranges.'],
                             'verify': ['Check the content style guide for the range separator: en dash, '
                                        "hyphen or 'to'.",
                                        'Check whether date ranges and number ranges follow different '
                                        'rules.'],
                             'searchTerms': ['en dash range', 'business days copy', 'range separator'],
                             'impact': 'cosmetic',
                             'confidence': 'high',
                             'confidenceReason': 'Every string read from DOM text, so the dash characters '
                                                 'are exact.',
                             'demoArtifactRisk': False},
 'accounts.i-safe-form-labels': {'title': 'Give SAFE, share and draw form controls real accessible names',
                                 'summary': 'The Pro-Rata Rights Letter switch is announced as '
                                            "'proRataRightsLetter', valuation cap and discount inputs have "
                                            "no name, a listbox is 'mercury-utils-id-78-label', and "
                                            'linked-account kebab buttons are unnamed.',
                                 'userImpact': 'Screen reader users hear a code key, an id or nothing for '
                                               "these controls, so they can't tell which field they are "
                                               'filling or what the kebab button opens.',
                                 'fixHypothesis': 'The labels seem to be missing or wired to internal ids '
                                                  'instead of the visible label text. Associating each '
                                                  'control with its visible label, and giving icon-only '
                                                  'buttons a label, would likely resolve it.',
                                 'acceptance': ['The Pro-Rata Rights Letter switch is announced with its '
                                                'visible label.',
                                                'Valuation cap and discount inputs have accessible names '
                                                'that match their visible labels.',
                                                'The share recipient listbox and venture debt amount field '
                                                'have readable names, not ids or placeholders.',
                                                'Linked-account kebab buttons have an accessible name.'],
                                 'verify': ['Check the design-system form docs for how switches and '
                                            'comboboxes are expected to receive their labels.',
                                            'Check the accessibility guidelines for the naming convention '
                                            'for icon-only menu buttons.'],
                                 'searchTerms': ['missing accessible name',
                                                 'proRataRightsLetter',
                                                 'kebab button no label',
                                                 'SAFE form labels'],
                                 'impact': 'accessibility',
                                 'confidence': 'high',
                                 'confidenceReason': 'Accessible names read from accessibility snapshots on '
                                                     'four screens.',
                                 'demoArtifactRisk': False},
 'accounts.i-safe-sent-state': {'title': "Fix Send SAFE opening another investor's SAFE and stale totals",
                                'summary': "After Send SAFE the toast says 'SAFE sent to Arthur Rock', but "
                                           "the list still shows 3 SAFEs and '$10,011,000', and the drawer "
                                           "that opens is Logan Roy's $1,000 SAFE.",
                                'userImpact': "An admin who just sent a SAFE sees someone else's record and "
                                              "unchanged totals, so they can't confirm the new SAFE exists "
                                              "and may send it again. Company details also say 'Acme Inc.' "
                                              "where other flows say 'Mercury Demo, Incorporated'.",
                                'fixHypothesis': 'The drawer may open by list position (safeRequest=1) '
                                                 "rather than by the new SAFE's id, and the list may not "
                                                 'refetch after sending. The demo sandbox may also not '
                                                 'persist new SAFEs.',
                                'acceptance': ['After Send SAFE, the drawer that opens shows the SAFE just '
                                               'sent.',
                                               'The SAFE count and total include the new SAFE without a '
                                               'reload.',
                                               'SAFE company details show the same legal name as other '
                                               'flows.'],
                                'verify': ['Check whether the demo sandbox persists newly sent SAFEs; if '
                                           'not, test on a staging account.',
                                           "Check whether 'Acme Inc.' and state 'CA' are demo profile data "
                                           'used only by the SAFE flow.'],
                                'searchTerms': ['SAFE sent wrong drawer',
                                                'SAFE list not updated',
                                                'safeRequest'],
                                'impact': 'data-integrity',
                                'confidence': 'medium',
                                'confidenceReason': 'Toast, URL, totals and drawer header captured once; the '
                                                    'demo may not persist new SAFEs.',
                                'demoArtifactRisk': True},
 'accounts.i-same-concept-names': {'title': 'Call each account, rule type and setting by one name',
                                   'summary': "Ops / Payroll also appears as 'Mercury Checking ••1038', "
                                              "Treasury activity names a 'Primary Checking' account that "
                                              "isn't in the list, and rule types change names between picker "
                                              'and form.',
                                   'userImpact': "An admin reading Treasury activity can't match 'Primary "
                                                 "Checking' to any account they have. The rule name changes "
                                                 "('Distribute funds out' opens 'Distribute funds across "
                                                 "accounts') are smaller but make it less clear the right "
                                                 'form opened.',
                                   'fixHypothesis': 'The account names may come from activity records that '
                                                    'store an older or default name. The rule and setting '
                                                    'labels seem to be separate strings that drifted; '
                                                    'aligning each to one name would likely resolve that '
                                                    'part.',
                                   'acceptance': ['Financing and Treasury activity name accounts the way the '
                                                  'account list does.',
                                                  "Each rule type's picker label matches its form heading.",
                                                  'The backup autopay account is spelled the same way for '
                                                  'the credit card and the working capital loan.',
                                                  "The 'Manage shared documents' link and the dialog it "
                                                  'opens use the same wording.'],
                                   'verify': ["Check whether 'Primary Checking' and 'Mercury Checking "
                                              "••1038' come from demo seed data rather than product logic.",
                                              "Check the content style guide or glossary for 'auto transfer "
                                              "rule' vs 'auto-transfer rule' and 'backup' vs 'back up'."],
                                   'searchTerms': ['account name mismatch activity',
                                                   'Primary Checking',
                                                   'auto-transfer rule naming',
                                                   'backup autopay account'],
                                   'impact': 'consistency',
                                   'confidence': 'medium',
                                   'confidenceReason': 'Strings quoted verbatim, but the account-name '
                                                       'mismatches may come from seeded demo records.',
                                   'demoArtifactRisk': True},
 'accounts.i-secondary-button-style': {'title': 'Match the Create account Cancel button to other modal '
                                                'Cancels',
                                       'summary': 'Cancel in Create account is a transparent ghost button, '
                                                  'while Cancel in Unlink, limit increase and Disbursement '
                                                  'agreement is a tonal pill with an rgba(180,183,200,0.12) '
                                                  'fill; size and padding match.',
                                       'userImpact': 'Cosmetic. The button works, but the same action looks '
                                                     'different from one modal to the next.',
                                       'fixHypothesis': 'Create account seems to use a different button '
                                                        'variant for its secondary action. Switching it to '
                                                        'the variant the other modals use would likely '
                                                        'resolve it.',
                                       'acceptance': ['Cancel in Create account has the same background as '
                                                      'Cancel in Unlink and limit increase.',
                                                      'Secondary modal actions in Accounts share one button '
                                                      'variant.'],
                                       'verify': ['Check the design-system button docs for which variant is '
                                                  "prescribed for a modal's secondary action.",
                                                  'Check whether ghost secondary buttons are reserved for a '
                                                  'particular kind of modal.'],
                                       'searchTerms': ['ghost button modal',
                                                       'secondary button variant',
                                                       'Create account Cancel'],
                                       'impact': 'cosmetic',
                                       'confidence': 'high',
                                       'confidenceReason': 'Computed background, color, radius, padding and '
                                                           'height measured on four modals.',
                                       'demoArtifactRisk': False},
 'accounts.i-shared-link-toast': {'title': "Fix the 'Removed 1 statements' toast after removing a shared "
                                           'link',
                                  'summary': 'Removing one recipient link that covered 4 documents shows the '
                                             "toast 'Removed 1 statements', right after a button that read "
                                             "'Remove access to 1 document link'.",
                                  'userImpact': 'The toast uses the wrong plural and calls a link a '
                                                'statement, so an admin may not know whether one document or '
                                                'four lost access.',
                                  'fixHypothesis': 'The toast seems to combine the link count with a fixed '
                                                   "'statements' noun. Pluralizing by count and using the "
                                                   "button's noun would likely resolve it.",
                                  'acceptance': ['Removing one link shows a toast with a singular noun and '
                                                 'the correct count.',
                                                 'The toast uses the same noun as the button that triggered '
                                                 'it.',
                                                 'Removing several links shows a correctly pluralized '
                                                 'toast.'],
                                  'verify': ['Check the content style guide for count pluralization and '
                                             'whether toasts echo the button wording.'],
                                  'searchTerms': ['Removed 1 statements',
                                                  'shared document link toast',
                                                  'toast plural'],
                                  'impact': 'consistency',
                                  'confidence': 'high',
                                  'confidenceReason': 'Button label and toast text quoted verbatim from the '
                                                      'same action.',
                                  'demoArtifactRisk': False},
 'accounts.i-tabs-not-tabs': {'title': 'Make Accounts tab bars and inline Edit links keyboard reachable',
                              'summary': 'Accounts, Treasury and Financing tab bars are divs with click '
                                         'handlers and no tab roles, aria-selected or tabindex; Autopay '
                                         "'Edit' links and the credit 'Last Month' control are built the "
                                         'same way.',
                              'userImpact': "Keyboard and screen reader users can't switch between Mercury "
                                            'accounts, Linked accounts and Auto transfer rules, or edit an '
                                            'autopay rule. Those tasks are blocked without a mouse.',
                              'fixHypothesis': 'These controls seem to be built from plain divs rather than '
                                               'tab and button components. Moving them onto components with '
                                               'the right roles would likely fix focus and announcement '
                                               'together.',
                              'acceptance': ['Accounts, Treasury and Financing tab bars expose role=tablist, '
                                             'role=tab and aria-selected.',
                                             "Each tab, each Autopay 'Edit' control and 'Last Month' can be "
                                             'reached with Tab and activated from the keyboard.',
                                             'The selected tab is announced as selected.'],
                              'verify': ['Check the design-system tabs docs for whether the component behind '
                                         "'_dsTabViewTab' is meant to add tab roles itself.",
                                         'Check the accessibility guidelines for whether these bars are '
                                         'meant to be tabs or navigation links.'],
                              'searchTerms': ['tabs not keyboard accessible',
                                              'role=tab missing',
                                              'div click handler'],
                              'impact': 'accessibility',
                              'confidence': 'high',
                              'confidenceReason': 'Role, tabindex and class attributes inspected in the DOM '
                                                  'on three pages.',
                              'demoArtifactRisk': False},
 'accounts.i-treasury-docs-filter': {'title': 'Fix Treasury Documents link ignoring its account filter',
                                     'summary': "Treasury's Documents link opens Statements with "
                                                "?accountIds=party-treasury-id-0, but the filter shows '8 "
                                                "selected' and lists every account; the checking link "
                                                "filters correctly to 'Ops / Payroll'.",
                                     'userImpact': 'An admin looking for Treasury statements lands on every '
                                                   "account's statements and has to filter by hand, and may "
                                                   'download the wrong one.',
                                     'fixHypothesis': 'The Statements filter may not recognize the Treasury '
                                                      'id format and fall back to all accounts. Accepting '
                                                      'that id would likely fix it.',
                                     'acceptance': ['Opening Documents from Treasury shows Statements '
                                                    'filtered to Treasury only.',
                                                    "The Accounts filter reads 'Treasury' rather than '8 "
                                                    "selected'."],
                                     'verify': ['Check whether Treasury statements are meant to be listed on '
                                                'this Statements page at all.',
                                                "Check whether the demo's account ids differ in format from "
                                                'production ids.'],
                                     'searchTerms': ['Treasury statements filter',
                                                     'accountIds ignored',
                                                     'Documents 8 selected'],
                                     'impact': 'broken-task',
                                     'confidence': 'medium',
                                     'confidenceReason': 'Link href and resulting filter state captured, but '
                                                         'demo account ids may behave differently.',
                                     'demoArtifactRisk': True},
 'accounts.i-validation-display': {'title': 'Show the target balance error inline and the SAFE type error '
                                            'once',
                                   'summary': 'The target balance form shows inline pink errors under three '
                                              'fields but only a warning icon with a hover tooltip for the '
                                              'amount; the SAFE form shows its type error twice, with two '
                                              'icons.',
                                   'userImpact': 'An admin who submits the rule form sees no message for the '
                                                 'amount unless they hover the icon, and keyboard or touch '
                                                 'users may never see it. The duplicate SAFE error is noise.',
                                   'fixHypothesis': 'The amount input seems to use a different field '
                                                    'component that puts its error in a tooltip. Using the '
                                                    'same inline error as the other fields, and dropping the '
                                                    'extra SAFE warning, would likely resolve both.',
                                   'acceptance': ["'Enter a target balance' appears as inline text under the "
                                                  'amount field after an empty submit.',
                                                  'The SAFE type field shows one error message with one '
                                                  'icon.',
                                                  'Field error icons in both forms use the same glyph.'],
                                   'verify': ['Check the design-system form validation docs for whether '
                                              'tooltip-only errors are allowed.',
                                              'Check which warning icon, filled or outline, is prescribed '
                                              'for field errors.'],
                                   'searchTerms': ['tooltip validation error',
                                                   'inline error amount field',
                                                   'duplicate SAFE type error'],
                                   'impact': 'consistency',
                                   'confidence': 'high',
                                   'confidenceReason': 'Error text styles and icon path data captured with '
                                                       'DevTools after empty submits.',
                                   'demoArtifactRisk': False},
 'billpay.i-amount-sign': {'title': 'Use one sign convention for bill amounts across Bill Pay tabs',
                           'summary': 'Inbox rows and the bill panel show outstanding bills as positive '
                                      "('$220.00'); Needs Approval, Scheduled, Paid and the approval panel "
                                      "show them negative ('−$5,000.00'); the pay review is positive again.",
                           'userImpact': 'A minus sign can read as a credit or refund. An admin who follows '
                                         'a bill from Inbox to Scheduled sees its sign flip and may wonder '
                                         'whether it is the same payment.',
                           'fixHypothesis': 'Some tabs may reuse transaction-style formatting, where '
                                            'outgoing money is negative, while Inbox and the pay flow use a '
                                            'bill-style positive format. Picking one convention for bills '
                                            'would likely remove the flip.',
                           'acceptance': ['A given bill shows the same sign in Inbox, Needs Approval, '
                                          'Scheduled, Paid, its side panels and the pay review.',
                                          'If outgoing amounts stay negative, every Bill Pay tab applies '
                                          'that rule the same way.'],
                           'verify': ['Check the design-system or content guidance on signed amounts: '
                                      'whether outgoing payments are negative everywhere or only in '
                                      'transaction lists.',
                                      'Confirm with product whether the positive Inbox amount is deliberate '
                                      'because an unpaid bill is not yet money movement.'],
                           'searchTerms': ['negative bill amount',
                                           'amount sign Bill Pay',
                                           'minus sign scheduled payments'],
                           'impact': 'consistency',
                           'confidence': 'medium',
                           'confidenceReason': 'Row text quoted verbatim, but whether signed amounts are '
                                               'wrong depends on an amount convention we cannot see.',
                           'demoArtifactRisk': False},
 'billpay.i-ars-currency': {'title': 'Drop the dollar sign from non-USD amounts in Bill Pay Scheduled',
                            'summary': 'A scheduled Argentine-peso international wire to Jean Vallee reads '
                                       "'−$410.40 ARS', pairing the USD symbol with the ARS code.",
                            'userImpact': 'An admin can read this as $410.40 in US dollars. Pesos and '
                                          'dollars differ widely in value, so misreading the currency '
                                          'changes the size of the payment a lot.',
                            'fixHypothesis': "The Scheduled amount formatter seems to prefix '$' to every "
                                             'amount and append a code for non-USD currencies. Using the '
                                             "currency's own symbol, or the code alone, would likely fix it.",
                            'acceptance': ["Non-USD amounts in Scheduled show no '$' unless the currency "
                                           'uses that symbol.',
                                           'Scheduled and Paid format non-USD amounts the same way.'],
                            'verify': ['Check the design-system currency formatting docs for how non-USD '
                                       'amounts are shown: symbol, code, or both.',
                                       "Check whether the ARS wire's seeded demo data carries a malformed "
                                       'currency field.'],
                            'searchTerms': ['ARS dollar sign',
                                            'foreign currency amount format',
                                            'international wire currency symbol'],
                            'impact': 'data-integrity',
                            'confidence': 'high',
                            'confidenceReason': 'Row text quoted verbatim from the Scheduled table.',
                            'demoArtifactRisk': False},
 'billpay.i-badge-hover-color': {'title': 'Keep the Overdue badge color on hovered and selected Bill Pay '
                                          'rows',
                                 'summary': 'On a hovered or selected inbox row, the Overdue badge changes '
                                            'from orange text rgb(252,155,111) to neutral rgb(221,221,229) '
                                            'on a blue tint.',
                                 'userImpact': 'The warning color disappears on the row the admin is looking '
                                               'at, so an open overdue bill stops standing out. The '
                                               "'Overdue' text stays, so the status is still readable.",
                                 'fixHypothesis': 'The row hover and selected style seems to override the '
                                                  'colors of child elements, badges included. Scoping that '
                                                  'override so status badges keep their tone would likely '
                                                  'fix it.',
                                 'acceptance': ['The Overdue badge keeps its orange text and background when '
                                                'its row is hovered.',
                                                'The Overdue badge keeps its orange treatment when its row '
                                                'is selected and the bill panel is open.'],
                                 'verify': ['Check the design-system badge and table docs for whether badges '
                                            'are meant to turn neutral inside hovered or selected rows.'],
                                 'searchTerms': ['badge hover color',
                                                 'Overdue badge row hover',
                                                 'status badge selected row'],
                                 'impact': 'cosmetic',
                                 'confidence': 'high',
                                 'confidenceReason': 'Computed badge colors captured with DevTools for '
                                                     'hovered and resting rows.',
                                 'demoArtifactRisk': False},
 'billpay.i-casing': {'title': 'Use one casing rule for Bill Pay and Accounting labels',
                      'summary': "'Bill Pay settings' sits beside 'Accounting Settings', the 'Automation "
                                 "settings' tab opens an 'Automation Settings' heading, and 'Review sync "
                                 "errors' becomes 'Review Sync Errors' on Ready to Export.",
                      'userImpact': 'Polish issue. Nothing breaks, but one button reading two ways on '
                                    'adjacent tabs makes the product read as assembled from different parts.',
                      'fixHypothesis': 'Labels seem to have been written per screen without a shared casing '
                                       "rule. Applying the style guide's rule to headings, tabs, buttons and "
                                       'table cells would likely resolve it, with product names keeping '
                                       'their capitals.',
                      'acceptance': ['The Automation settings tab and its heading use the same casing.',
                                     "'Review sync errors' reads identically on Needs Review and Ready to "
                                     'Export.',
                                     'Accounting workbench tabs and Accounting Settings tabs follow the same '
                                     'casing rule.',
                                     "'Bank description' has the same casing in the rules table and the If "
                                     'picker.'],
                      'verify': ['Check the content style guide for sentence case or Title Case on headings, '
                                 'tabs and buttons.',
                                 "Check which terms count as product names ('Bill Pay', 'Accounting') and "
                                 'keep their capitals.'],
                      'searchTerms': ['Title Case sentence case',
                                      'Review Sync Errors casing',
                                      'Accounting Settings casing'],
                      'impact': 'consistency',
                      'confidence': 'high',
                      'confidenceReason': 'Every label quoted verbatim with its element type and screen.',
                      'demoArtifactRisk': False},
 'billpay.i-cents-style': {'title': 'Render cents the same way in Bill Pay tables, cards and review',
                           'summary': 'Tables and panels render cents as a raised 10.95px span beside 15px '
                                      'dollars, while summary cards, the pay review, the success card and '
                                      'the recurring schedule print cents flat at full size.',
                           'userImpact': 'Polish issue. Amounts still read correctly, but the same $220.00 '
                                         'looks different at each step of paying it.',
                           'fixHypothesis': 'Summary cards and the pay flow seem to print amounts as a '
                                            'single text span instead of the amount display used in tables. '
                                            'Moving them onto the same amount component would likely align '
                                            'them.',
                           'acceptance': ['The inbox table, summary cards, pay review and pay success card '
                                          'use the same cents treatment.',
                                          'Amounts in the recurring schedule preview use the same cents '
                                          'treatment as Bill Pay tables.'],
                           'verify': ['Look up the design-system amount component docs for when superscript '
                                      'cents apply and whether large hero amounts are exempt.'],
                           'searchTerms': ['superscript cents', 'cents styling', 'amount display component'],
                           'impact': 'cosmetic',
                           'confidence': 'high',
                           'confidenceReason': 'Markup and computed font sizes captured with DevTools on '
                                               'each surface.',
                           'demoArtifactRisk': False},
 'billpay.i-create-recipient-cta': {'title': "Use design-system styles for 'Create recipient' in Pay bill",
                                    'summary': "With a matched recipient, 'Create recipient' is a 20px "
                                               'underlined link at 13.3333px, the browser default; with no '
                                               'match it is a 50px full-width secondary button.',
                                    'userImpact': 'Minor. Admins who learned one version may not spot the '
                                                  "other, and the small link's browser-default size makes it "
                                                  'look unstyled.',
                                    'fixHypothesis': 'The matched state seems to use a bare button with link '
                                                     'styling that misses the 15px body size. Using a '
                                                     'documented link or tertiary button variant would '
                                                     'likely fix it; the change in emphasis between states '
                                                     'may be intended.',
                                    'acceptance': ["'Create recipient' in the matched state uses the 15px "
                                                   'body font size.',
                                                   'Both states use documented design-system button or link '
                                                   'variants.'],
                                    'verify': ['Check the design-system guidance on link-style buttons and '
                                               'whether a secondary action may change emphasis by state.'],
                                    'searchTerms': ['Create recipient button',
                                                    'recipient match link',
                                                    '13.3333px font size'],
                                    'impact': 'consistency',
                                    'confidence': 'high',
                                    'confidenceReason': 'Element sizes, font size and background measured '
                                                        'with DevTools in both states.',
                                    'demoArtifactRisk': False},
 'billpay.i-currency-input-align': {'title': 'Format and align currency inputs the same way in Bill Pay',
                                    'summary': "The same $220 amount shows as '220.00' in the bill panel "
                                               "edit field, '220' left-aligned on the Amount step, and '220' "
                                               'right-aligned in the line-item Unit price.',
                                    'userImpact': 'Minor. The value stays readable, but the input changes '
                                                  'shape between steps, and dropped cents make it less '
                                                  'obvious that the amount is exact.',
                                    'fixHypothesis': 'The three screens seem to configure currency inputs '
                                                     'differently. One currency input setup with fixed '
                                                     'alignment and two-decimal formatting would likely '
                                                     'align them.',
                                    'acceptance': ['Bill Pay currency inputs show the same value format for '
                                                   'the same amount.',
                                                   'Currency inputs in the bill panel, Amount step and line '
                                                   'items use one text alignment, or a documented exception '
                                                   'for tables.'],
                                    'verify': ['Check the design-system currency input docs for alignment '
                                               'and decimal formatting rules.',
                                               'Check whether right alignment is deliberate in line items so '
                                               'amounts stack by decimal.'],
                                    'searchTerms': ['currency input alignment',
                                                    'amount input decimals',
                                                    'unit price alignment'],
                                    'impact': 'consistency',
                                    'confidence': 'high',
                                    'confidenceReason': 'Input values, placeholders and text-align read from '
                                                        'the DOM on three screens.',
                                    'demoArtifactRisk': False},
 'billpay.i-date-input-label': {'title': 'Label the Invoice date input correctly in bill edit mode',
                                'summary': "In bill panel edit mode, both date inputs use 'Select due date' "
                                           'as placeholder and accessible name, including the Invoice date '
                                           'field.',
                                'userImpact': "A screen reader user hears 'Select due date' twice and cannot "
                                              'tell which field sets the invoice date, so they may change '
                                              'the wrong date.',
                                'fixHypothesis': "The Invoice date field seems to reuse the due date input's "
                                                 'props. Giving it its own label and placeholder would '
                                                 'likely fix it.',
                                'acceptance': ["The Invoice date input's accessible name refers to the "
                                               'invoice date.',
                                               'Each date input in bill edit mode has a label that is not '
                                               'only its placeholder.'],
                                'verify': ['Check the design-system form field docs for whether a '
                                           "placeholder may serve as an input's accessible name."],
                                'searchTerms': ['Select due date placeholder',
                                                'invoice date label',
                                                'date input accessible name'],
                                'impact': 'accessibility',
                                'confidence': 'high',
                                'confidenceReason': 'Input placeholders and values read from the DOM.',
                                'demoArtifactRisk': False},
 'billpay.i-date-picker-overflow': {'title': 'Keep the bill panel due date picker inside the viewport',
                                    'summary': 'In bill panel edit mode the due date calendar spans x=1122 '
                                               'to 1492 on a 1440px window, so the Saturday column and the '
                                               'next-month arrow are off screen.',
                                    'userImpact': 'At a common laptop width, admins cannot pick a Saturday '
                                                  'or move to the next month from the picker, so some due '
                                                  'dates need typing or a wider window.',
                                    'fixHypothesis': "The popover seems to anchor to the input's left edge "
                                                     'without collision handling. Flipping or shifting it '
                                                     'when it would overflow would likely keep it on screen.',
                                    'acceptance': ["At 1440px wide, the due date calendar's right edge sits "
                                                   'inside the viewport.',
                                                   'The Saturday column and next-month arrow are visible and '
                                                   'clickable in the bill panel picker.'],
                                    'verify': ['Check whether the design-system date picker documents flip '
                                               'or collision behavior, and whether this panel opts out of '
                                               'it.'],
                                    'searchTerms': ['date picker overflow',
                                                    'calendar popover clipped',
                                                    'due date picker off screen'],
                                    'impact': 'broken-task',
                                    'confidence': 'high',
                                    'confidenceReason': 'Popover bounding box and window width measured with '
                                                        'DevTools.',
                                    'demoArtifactRisk': False},
 'billpay.i-decline-no-confirm': {'title': 'Confirm before declining an approval request in Bill Pay',
                                  'summary': "Clicking 'Decline' in the approval panel removes the request "
                                             "at once with only a 'Request declined' toast, while Cancel "
                                             'payment, Delete and rule delete each open a confirmation '
                                             'dialog.',
                                  'userImpact': 'A mis-click on Decline rejects a $5,000 recurring payment '
                                                'request with no chance to back out and no visible undo. The '
                                                'requester may have to submit it again.',
                                  'fixHypothesis': 'Decline seems to call the action directly while other '
                                                   'destructive actions go through a confirmation dialog. '
                                                   'Adding the same dialog, or an Undo on the toast, would '
                                                   'likely close the gap.',
                                  'acceptance': ['Clicking Decline in the approval panel opens a '
                                                 'confirmation dialog or shows a toast with Undo.',
                                                 'The request stays in Needs Approval until the decline is '
                                                 'confirmed or the undo window passes.'],
                                  'verify': ['Check the design-system guidance on which destructive actions '
                                             'need confirmation and when undo is used instead.',
                                             'Confirm with product whether one-click decline is deliberate '
                                             'for approvers with long queues.'],
                                  'searchTerms': ['decline confirmation',
                                                  'approval decline no confirm',
                                                  'Request declined toast'],
                                  'impact': 'broken-task',
                                  'confidence': 'high',
                                  'confidenceReason': 'DevTools showed zero dialogs after Decline, compared '
                                                      'with dialogs for three other destructive actions.',
                                  'demoArtifactRisk': False},
 'billpay.i-due-date-format': {'title': 'Show the day in Bill Pay due dates from other years',
                               'summary': "The inbox Due date column shows past-year bills as 'Apr 2025' and "
                                          "'Dec 2025' but a current-year bill as 'Jan 17'; the same bill "
                                          "reads 'Apr 19, 2025' in its panel.",
                               'userImpact': 'Overdue bills are often the ones from past years, and for '
                                             'those the table hides the day. An admin triaging overdue bills '
                                             'has to open each panel to see when it was due.',
                               'fixHypothesis': 'The table formatter seems to switch to month and year when '
                                                'a date falls outside the current year. Showing month, day '
                                                'and year for those dates would likely match the panel and '
                                                'pay flow.',
                               'acceptance': ['Every Due date cell in Inbox and Scheduled shows a day, '
                                              'including dates outside the current year.',
                                              "A bill's due date reads the same in its table row and its "
                                              'detail panel.',
                                              'Scheduled rows show a day in Due date and Send on dates set '
                                              'in future years.'],
                               'verify': ["Check the content style guide's date rules for tables and whether "
                                          'month and year without a day is an allowed short form.',
                                          'Confirm the month-and-year form is not a deliberate density '
                                          'choice for the Due date column.'],
                               'searchTerms': ['due date month year',
                                               'Bill Pay due date format',
                                               'due date missing day'],
                               'impact': 'consistency',
                               'confidence': 'high',
                               'confidenceReason': 'DOM text of table cells, panel and pay-flow input quoted '
                                                   'verbatim.',
                               'demoArtifactRisk': False},
 'billpay.i-email-receipt-input': {'title': "Match the 'Email receipt to' field to the other Pay bill inputs",
                                   'summary': "On the Pay bill Details step, 'Email receipt to' is 42px tall "
                                              'with 4px left padding and white text, while Invoice and Memo '
                                              'inputs are 40px with 12px padding and rgb(221,221,229).',
                                   'userImpact': 'Polish issue. Its placeholder sits out of line with the '
                                                 'fields above, so the form looks uneven.',
                                   'fixHypothesis': 'The multi-select seems to use a different input shell '
                                                    'from the text inputs. Aligning its height, padding and '
                                                    'text color with the text inputs would likely fix it.',
                                   'acceptance': ["'Email receipt to' is the same height as the Invoice and "
                                                  'Memo inputs.',
                                                  'Its placeholder starts at the same left offset and uses '
                                                  'the same color as the inputs above.'],
                                   'verify': ['Check the design-system docs for the multi-select or token '
                                              'input and whether it is meant to share text input '
                                              'dimensions.'],
                                   'searchTerms': ['email receipt input',
                                                   'multi-select height',
                                                   'input padding mismatch'],
                                   'impact': 'cosmetic',
                                   'confidence': 'high',
                                   'confidenceReason': 'Heights, padding and colors measured with DevTools.',
                                   'demoArtifactRisk': False},
 'billpay.i-empty-states': {'title': 'Use one empty-state pattern across Bill Pay',
                            'summary': 'Needs Approval and filtered Inbox center a title over a blurred '
                                       'skeleton, Ready to Export adds two buttons, and an emptied Inbox '
                                       "shows 'All your bills are paid!' in the Silkscreen pixel font.",
                            'userImpact': "After deleting every bill, the inbox says 'All your bills are "
                                          "paid!', which is false and could suggest payments went out. The "
                                          'three styles also make empty screens look unrelated.',
                            'fixHypothesis': 'The inbox-zero card seems to be a one-off keyed on an empty '
                                             'inbox rather than on paid bills. Using the shared skeleton '
                                             'empty state, with copy that does not assume how the inbox '
                                             'emptied, would likely fix both.',
                            'acceptance': ['An inbox emptied by deleting bills does not say the bills are '
                                           'paid.',
                                           'Bill Pay empty states use the same title, body and layout '
                                           'treatment.',
                                           'Buttons inside empty states follow one documented pattern.'],
                            'verify': ['Check whether the pixel-font Zero Inbox card is a deliberate brand '
                                       'moment approved by design.',
                                       'Look up the design-system empty-state component and whether it '
                                       'allows action buttons.'],
                            'searchTerms': ['All your bills are paid',
                                            'inbox zero empty state',
                                            'Bill Pay empty state'],
                            'impact': 'consistency',
                            'confidence': 'high',
                            'confidenceReason': 'Headings, fonts and sizes captured with DevTools, and the '
                                                'paid message seen right after a bulk delete.',
                            'demoArtifactRisk': False},
 'billpay.i-feedback-badge': {'title': 'Give the Bill Pay Feedback control a button style',
                              'summary': 'The Feedback control beside the Bill Pay title is a 20px button '
                                         'wrapping the status badge (4px radius, 12px text), next to 32px '
                                         "pill buttons like 'Upload bill'.",
                              'userImpact': 'Admins may read Feedback as a status label and not realize it '
                                            'opens a feedback panel.',
                              'fixHypothesis': 'The control seems to place the badge component inside a '
                                               'button. Using a small button or link variant would likely '
                                               'make it read as clickable.',
                              'acceptance': ['The Feedback control uses a design-system button or link '
                                             'variant, not the status badge.',
                                             'It looks different from the Overdue status badge.'],
                              'verify': ['Check whether a Feedback chip beside page titles is a documented '
                                         'pattern elsewhere in Mercury.',
                                         'Check the design-system badge docs for whether badges may be '
                                         'interactive.'],
                              'searchTerms': ['Feedback badge button',
                                              'Bill Pay feedback chip',
                                              'interactive badge'],
                              'impact': 'consistency',
                              'confidence': 'high',
                              'confidenceReason': 'Class names, sizes and radii captured with DevTools for '
                                                  'the control and its neighbors.',
                              'demoArtifactRisk': False},
 'billpay.i-filter-all-time': {'title': "Clear the start date when Accounting filters are set to 'All time'",
                               'summary': "The Accounting Filters panel shows 'All time' selected while the "
                                          "Start date field reads 'Jun 17, 2026' and End date reads 'Today'.",
                               'userImpact': 'Admins cannot tell whether the list covers every transaction '
                                             'or only those since Jun 17, so a review or export may miss '
                                             'older items.',
                               'fixHypothesis': 'The preset select and the date inputs seem to hold separate '
                                                "state. Emptying the dates for 'All time', or switching the "
                                                'preset to a custom range when dates are set, would likely '
                                                'resolve it.',
                               'acceptance': ["With 'All time' selected, Start date is empty or shows the "
                                              'earliest date the list covers.',
                                              "Setting a start date changes the preset label away from 'All "
                                              "time'."],
                               'verify': ['Check which range the list applies when both are shown, to learn '
                                          'whether the label or the date is wrong.',
                                          "The demo's frozen clock may set the default start date; check how "
                                          'that default is computed.'],
                               'searchTerms': ['All time filter start date',
                                               'Accounting date filter',
                                               'date range preset mismatch'],
                               'impact': 'data-integrity',
                               'confidence': 'medium',
                               'confidenceReason': 'Select and input values read from the DOM, but which '
                                                   'range is applied was not checked.',
                               'demoArtifactRisk': True},
 'billpay.i-gl-column-truncation': {'title': 'Stop the sticky Actions column from covering GL Code in '
                                             'Accounting',
                                    'summary': 'In the Accounting table the sticky 110px Actions column '
                                               'overlaps the 240px GL Code column, leaving about 72px '
                                               "visible, so values read '215 - A' and 'Mercur' with no "
                                               'ellipsis.',
                                    'userImpact': 'Admins reviewing transactions cannot read the assigned GL '
                                                  'code without opening the picker, which slows review and '
                                                  'hides miscoded rows.',
                                    'fixHypothesis': 'The GL Code column seems to be laid out as if the '
                                                     'sticky column took no space. Reserving room for '
                                                     'Actions, or truncating with an ellipsis and tooltip, '
                                                     'would likely fix it.',
                                    'acceptance': ['At 1440px wide, each GL code shows in full or truncated '
                                                   'with an ellipsis and a tooltip.',
                                                   'No column renders underneath the sticky Actions column '
                                                   'when the table is not scrolled.'],
                                    'verify': ['Check the design-system table docs for sticky column '
                                               'behavior and truncation rules.',
                                               "Check whether the demo's GL code names are longer than "
                                               'typical customer ones.'],
                                    'searchTerms': ['GL Code column truncated',
                                                    'sticky actions column overlap',
                                                    'accounting table clipping'],
                                    'impact': 'broken-task',
                                    'confidence': 'high',
                                    'confidenceReason': 'Header positions, widths and sticky positioning '
                                                        'measured with DevTools.',
                                    'demoArtifactRisk': False},
 'billpay.i-manage-link': {'title': "Style the 'Manage' email alerts link as a link in Bill Pay settings",
                           'summary': "In Bill Pay settings the 'Manage' link is 14px rgb(195,195,204) with "
                                      "no underline, the paragraph style, while the sibling 'Edit' and "
                                      "'Connect' links are 15px blue rgb(141,164,245).",
                           'userImpact': 'Admins may not see that email alerts can be changed here. Users '
                                         "who rely on color or underline to find links cannot tell 'Manage' "
                                         'from body text.',
                           'fixHypothesis': "The 'Manage' link seems to inherit paragraph styles instead of "
                                            'the link style its siblings use. Applying the shared link style '
                                            'would likely fix it.',
                           'acceptance': ["'Manage' uses the same color and size as the 'Edit' and 'Connect' "
                                          'links.',
                                          "'Manage' is distinguishable from the surrounding text without "
                                          'hovering.'],
                           'verify': ['Check the design-system link docs for whether a muted inline link is '
                                      'an approved variant.'],
                           'searchTerms': ['Manage link style', 'email alerts link', 'link looks like text'],
                           'impact': 'accessibility',
                           'confidence': 'high',
                           'confidenceReason': 'Computed color, size and text-decoration captured with '
                                               'DevTools for all three links.',
                           'demoArtifactRisk': False},
 'billpay.i-method-names': {'title': 'Use the same payment method names on every Bill Pay tab',
                            'summary': "Scheduled lists 'ACH', 'Check' and 'Intl. Wire'; Paid lists 'Mercury "
                                       "ACH Payment', 'Check Payment', 'Domestic Wire' and 'Intl. Wire'; the "
                                       "payment method menu says 'International Wire'.",
                            'userImpact': 'Minor. Admins can map the names, but a method that changes name '
                                          'between tabs makes Paid look like it holds different payment '
                                          'types.',
                            'fixHypothesis': 'Paid seems to show transaction-type names while Scheduled and '
                                             "the menu use method names. Mapping both tables to the menu's "
                                             'method labels would likely align them.',
                            'acceptance': ['Scheduled and Paid Method cells use the same name for each '
                                           'payment method.',
                                           'Wire labels in tables match the payment method menu, abbreviated '
                                           'only where the style guide allows.'],
                            'verify': ['Check the content style guide for the canonical names of ACH, check '
                                       "and wire payments and whether 'Intl.' is an approved abbreviation.",
                                       "Check whether 'Mercury ACH Payment' is a transaction-type label "
                                       'deliberately shown on Paid.'],
                            'searchTerms': ['payment method names',
                                            'Intl. Wire International Wire',
                                            'Mercury ACH Payment label'],
                            'impact': 'consistency',
                            'confidence': 'high',
                            'confidenceReason': 'Method cell text and menu options quoted verbatim.',
                            'demoArtifactRisk': False},
 'billpay.i-pay-dialog-name': {'title': "Hide the invisible 'Upload an attachment' heading in the pay flow",
                               'summary': "On every pay-flow step, the drag-and-drop overlay's h2 'Upload an "
                                          "attachment' (opacity 0, no aria-hidden) comes before the visible "
                                          'h1 step title in the accessibility tree.',
                               'userImpact': "Screen reader users navigating by heading hear 'Upload an "
                                             "attachment' first on each step and may think the step is about "
                                             'uploading a file.',
                               'fixHypothesis': 'The drop overlay seems to stay mounted and hidden only '
                                                'visually. Adding aria-hidden, or rendering it only while a '
                                                'file is dragged, would likely fix it.',
                               'acceptance': ['When no file is being dragged, the first heading in the pay '
                                              "flow's accessibility tree is the step title.",
                                              'The overlay heading is exposed only while the drop overlay is '
                                              'visible.'],
                               'verify': ['Check the design-system file drop or overlay docs for how hidden '
                                          'states are removed from the accessibility tree.'],
                               'searchTerms': ['Upload an attachment heading',
                                               'hidden overlay aria-hidden',
                                               'pay flow heading order'],
                               'impact': 'accessibility',
                               'confidence': 'high',
                               'confidenceReason': 'Accessibility snapshot and overlay computed styles '
                                                   'captured with DevTools.',
                               'demoArtifactRisk': False},
 'billpay.i-relative-dates': {'title': 'Use absolute dates in the Bill Pay approval panel',
                              'summary': "The approval panel says 'Requested by Alice C. on Sep 23' but "
                                         "lists Invoice date and Added on as 'Yesterday'; the inbox bill "
                                         "panel uses absolute dates ('Added on Sep 14').",
                              'userImpact': "Minor. The admin has to work out whether 'Yesterday' and 'Sep "
                                            "23' are the same day, and relative words lose meaning while a "
                                            'request waits in the queue.',
                              'fixHypothesis': 'The approval panel seems to use a relative date formatter '
                                               'for some fields. Using the absolute format of the bill panel '
                                               'would likely remove the mix.',
                              'acceptance': ['Invoice date and Added on in the approval panel show absolute '
                                             'dates.',
                                             'The approval, scheduled and bill panels format the same fields '
                                             'the same way.'],
                              'verify': ['Check the content style guide for whether relative dates like '
                                         "'Yesterday' are approved, and for which fields.",
                                         "The demo's frozen clock decides what counts as 'Yesterday'; check "
                                         'whether the relative formatter is deliberate for recent items.'],
                              'searchTerms': ['Yesterday relative date',
                                              'approval panel dates',
                                              'Added on Yesterday'],
                              'impact': 'consistency',
                              'confidence': 'medium',
                              'confidenceReason': 'Panel text quoted verbatim, but relative dates may be an '
                                                  'approved convention.',
                              'demoArtifactRisk': True},
 'billpay.i-rule-operator-dup': {'title': 'Stop categorization rules from printing the operator twice',
                                 'summary': "In the categorization rules table, hovered amount rules read '> "
                                            "> $500.00' and '< < $2,000.00' because a hover-only connector "
                                            'chip repeats the operator already in the amount text.',
                                 'userImpact': 'On hover the condition looks garbled, and screen reader '
                                               'users always hear the operator twice because the hidden chip '
                                               'stays in the accessible text.',
                                 'fixHypothesis': 'The connector chip seems to show the operator for amount '
                                                  'rules while the amount formatter also prefixes it. '
                                                  'Dropping one of the two for amount conditions would '
                                                  'likely fix it.',
                                 'acceptance': ['Hovered amount rules show the comparison operator once.',
                                                "The condition cell's accessible text reads, for example, '> "
                                                "$500.00'.",
                                                "Text rules still read 'has AWS'."],
                                 'verify': ['Check whether the connector chip is meant to stay in the '
                                            'accessible name while it is at opacity 0.'],
                                 'searchTerms': ['categorization rule operator',
                                                 'duplicate operator amount rule',
                                                 'connector badge'],
                                 'impact': 'cosmetic',
                                 'confidence': 'high',
                                 'confidenceReason': 'Accessible text and connector opacity captured with '
                                                     'DevTools.',
                                 'demoArtifactRisk': False},
 'billpay.i-sort-icon': {'title': 'Use one sort indicator in Bill Pay and Accounting tables',
                         'summary': "Bill Pay's 'Last updated' header uses a chevron-down sort icon, while "
                                    "Accounting's 'Date (PDT)' header uses a filled caret-down.",
                         'userImpact': 'Cosmetic. Both read as sort arrows; the difference only shows when '
                                       'the tables are compared.',
                         'fixHypothesis': 'The two tables seem to set their sort icons separately. Using the '
                                          'icon from the shared table header would likely align them.',
                         'acceptance': ['Sorted column headers in Bill Pay and Accounting show the same '
                                        'icon.',
                                        'That icon matches the one in the design-system table header docs.'],
                         'verify': ['Look up which sort icon the design-system table component specifies.'],
                         'searchTerms': ['sort icon', 'chevron caret table header', 'sort indicator'],
                         'impact': 'cosmetic',
                         'confidence': 'high',
                         'confidenceReason': 'SVG data-icon attributes read from both headers with DevTools.',
                         'demoArtifactRisk': False},
 'billpay.i-stale-after-pay': {'title': 'Refresh Bill Pay lists and summary cards after paying or deleting',
                               'summary': 'After scheduling the $220 Debug LLC payment, the bill stays in '
                                          'Inbox as Overdue, Scheduled still has 3 rows, and cards stay at 7 '
                                          '/ $22,272.18, though Ops / Payroll dropped $220.',
                               'userImpact': 'Admins may pay the same bill twice because it still looks '
                                             'overdue. After deleting bills, the Overdue card still shows 3 '
                                             '/ $13,110.00 for bills that no longer exist.',
                               'fixHypothesis': "Bill Pay's lists and summary counts seem to be cached and "
                                                'not refetched after a payment or bulk delete. Refetching '
                                                'them after those actions would likely fix it, unless the '
                                                'demo sandbox simply does not persist bill state.',
                               'acceptance': ['After scheduling a bill payment, the bill leaves Inbox and '
                                              'appears in Scheduled without a reload.',
                                              'Summary cards update counts and totals after a payment, '
                                              'delete or cancel.',
                                              'The Overdue card shows 0 after every overdue bill is '
                                              'deleted.'],
                               'verify': ['Check whether the demo sandbox persists bill state after '
                                          'scheduling; the account balance did update.',
                                          'Check whether Bill Pay deliberately keeps a paid bill in Inbox '
                                          'until the payment settles.'],
                               'searchTerms': ['stale inbox after payment',
                                               'summary cards not updating',
                                               'bill still overdue after pay'],
                               'impact': 'broken-task',
                               'confidence': 'medium',
                               'confidenceReason': 'Before and after values quoted from the DOM, but the '
                                                   'state bug was seen in one demo session.',
                               'demoArtifactRisk': True},
 'billpay.i-time-format': {'title': 'Use one timestamp format in Accounting',
                           'summary': "The sync error drawer shows 'Sep 21 at 10:08PM' directly above 'Sync "
                                      "failed on Sep 22 at 10:08 PM.'; GL code settings show 'Uploaded at "
                                      "10:11 PM PDT, Sep 24, 2026'.",
                           'userImpact': 'Minor. Each time reads correctly, but the missing space before PM, '
                                         'the time-first order and a time zone in only one place make '
                                         'timestamps harder to compare.',
                           'fixHypothesis': 'Each surface seems to build its own time string. A shared '
                                            'timestamp formatter with one date-then-time order and one AM/PM '
                                            'style would likely fix it.',
                           'acceptance': ['Accounting timestamps use the same spacing before AM/PM '
                                          'everywhere.',
                                          'The sync error drawer and GL code settings put date and time in '
                                          'the same order.',
                                          'The time zone is either shown on all Accounting timestamps or on '
                                          'none.'],
                           'verify': ["Look up the content style guide's time format: AM/PM spacing and "
                                      'casing, and when to show a time zone.'],
                           'searchTerms': ['timestamp format', 'time AM PM spacing', 'Uploaded at PDT'],
                           'impact': 'consistency',
                           'confidence': 'high',
                           'confidenceReason': 'Leaf text of each timestamp quoted verbatim from the DOM.',
                           'demoArtifactRisk': False},
 'billpay.i-unlabeled-controls': {'title': 'Name the row delete button and give Bill Pay tabs a tab role',
                                  'summary': 'The inbox row trash button has no aria-label or title, Bill '
                                             'Pay and Accounting tabs are DIVs with no role, and the bill '
                                             "edit amount input is named only by its '0.00' placeholder.",
                                  'userImpact': 'A screen reader user hears an unnamed button on each bill '
                                                'and cannot tell it deletes the bill. Tabs are not announced '
                                                'as tabs, so moving between Inbox, Needs Approval, Scheduled '
                                                'and Paid is hard to discover.',
                                  'fixHypothesis': 'The trash button seems to render only an icon without a '
                                                   'label, and the tabs appear to be styled DIVs rather than '
                                                   'a tab component. Adding a name and tablist and tab roles '
                                                   'with selected state would likely fix it.',
                                  'acceptance': ['The row delete button has an accessible name such as '
                                                 "'Delete bill'.",
                                                 'Bill Pay and Accounting tabs expose role=tab inside a '
                                                 'role=tablist, with aria-selected on the active tab.',
                                                 'The bill edit amount input has a label that is not its '
                                                 'placeholder.'],
                                  'verify': ['Check the design-system tabs component docs for built-in '
                                             'roles, and whether these screens use that component.',
                                             'Check the icon button guidance for a required accessible '
                                             'label.'],
                                  'searchTerms': ['trash button aria-label',
                                                  'tabs missing role',
                                                  'Bill Pay tabs accessibility'],
                                  'impact': 'accessibility',
                                  'confidence': 'high',
                                  'confidenceReason': 'Button attributes, class names and accessibility '
                                                      'snapshot roles captured with DevTools.',
                                  'demoArtifactRisk': False},
 'billpay.i-unmasked-account': {'title': 'Mask recipient account and routing numbers on the pay review',
                                'summary': "The pay flow's recipient step masks bank details ('Account "
                                           "••6789 · Routing ••0021'), but the review step prints the full "
                                           'account number 158856789 and routing number 021000021.',
                                'userImpact': 'Anyone who sees the review screen, or a screenshot of it, '
                                              "gets the recipient's full account number. It also contradicts "
                                              'the masking the admin saw one step earlier.',
                                'fixHypothesis': 'The review step seems to render raw recipient fields '
                                                 'instead of the masked display used on the recipient step. '
                                                 'Using the same masked format, perhaps with a reveal '
                                                 'control, would likely fix it.',
                                'acceptance': ['The pay review shows recipient account and routing numbers '
                                               'masked, as on the recipient step.',
                                               'Full numbers, if available, appear only after an explicit '
                                               'reveal action.'],
                                'verify': ["Check Mercury's security or design-system guidance on when full "
                                           'account numbers may be displayed.',
                                           'Confirm whether the review step shows full numbers on purpose so '
                                           'admins can check them before sending.'],
                                'searchTerms': ['unmasked account number',
                                                'routing number review step',
                                                'mask bank details'],
                                'impact': 'data-integrity',
                                'confidence': 'medium',
                                'confidenceReason': 'Rendered text quoted verbatim on both steps, but '
                                                    'showing full numbers at review may be intentional.',
                                'demoArtifactRisk': False},
 'billpay.i-utc-dates': {'title': "Show Bill Pay dates in the user's time zone",
                         'summary': 'At 10:11 PM PDT on Sep 24, Bill Pay stamps Last updated, Paid on and '
                                    "the default Send on as Sep 25, while Accounting shows 'Uploaded at "
                                    "10:11 PM PDT, Sep 24, 2026'.",
                         'userImpact': 'In a US evening, Bill Pay shows a paid date a day after the payment '
                                       'and defaults Send on to tomorrow. Admins reconciling against '
                                       'Accounting see the same event a day apart.',
                         'fixHypothesis': 'Bill Pay seems to format dates in UTC, while Accounting uses the '
                                          "browser's time zone and labels it. Formatting Bill Pay dates in "
                                          "the user's or organization's time zone would likely align them.",
                         'acceptance': ['At 10 PM Pacific, Bill Pay Last updated and Paid on show the local '
                                        'calendar date.',
                                        'The default Send on date matches the local date or a stated '
                                        'business-day rule.',
                                        'Bill Pay and Accounting show the same date for the same event.'],
                         'verify': ['Check whether Bill Pay uses UTC or Eastern time on purpose for payment '
                                    'cut-offs, which could justify the Send on default.',
                                    "Check whether the demo's frozen clock is set in UTC, which would "
                                    'produce the shift only in the demo.'],
                         'searchTerms': ['UTC date off by one',
                                         'time zone Bill Pay dates',
                                         'Send on tomorrow'],
                         'impact': 'data-integrity',
                         'confidence': 'medium',
                         'confidenceReason': 'Browser Date() and rendered dates quoted verbatim, but the '
                                             'demo clock could cause the shift.',
                         'demoArtifactRisk': True},
 'billpay.i-validation-patterns': {'title': 'Use one validation error pattern across Bill Pay forms',
                                   'summary': 'Bill Pay shows errors four ways: a hover-only tooltip on the '
                                              "forwarding email, inline 12px text, inline 15px text ('Please "
                                              "complete all fields') and a toast for too-small uploads.",
                                   'userImpact': 'On the forwarding email field, Save is disabled and the '
                                                 'reason appears only when the admin hovers a small icon, so '
                                                 'they may not know what to fix. Elsewhere, varying styles '
                                                 'make errors harder to spot.',
                                   'fixHypothesis': 'Each form seems to handle errors locally. Moving them '
                                                    'onto one inline field-error pattern, with a documented '
                                                    'form-level style for cases like uploads, would likely '
                                                    'resolve most of it.',
                                   'acceptance': ['The forwarding email error text is visible without '
                                                  'hovering and is programmatically tied to the input.',
                                                  'Field-level errors in Bill Pay forms share one font size '
                                                  'and color.',
                                                  "The line-item 'Please complete all fields' message "
                                                  'matches the field-error style or a documented form-level '
                                                  'style.'],
                                   'verify': ['Look up the design-system form validation guidance for when '
                                              'inline errors, tooltips or toasts are used.',
                                              'Check the file upload component docs for whether '
                                              'rejected-file errors are meant to be toasts.'],
                                   'searchTerms': ['validation error tooltip',
                                                   'forwarding email error',
                                                   'inline error styles'],
                                   'impact': 'consistency',
                                   'confidence': 'high',
                                   'confidenceReason': 'Error elements, sizes, colors and the hover-only DOM '
                                                       'insertion captured with DevTools.',
                                   'demoArtifactRisk': False},
 'books.i-bill-due-date': {'title': 'Show the due date in the Books bill drawer and flag past-due bills',
                           'summary': 'BILL-001 shows due date 08/31/2026 in the Bills grid, but its drawer '
                                      "shows 'Due date -'; the bill is past due yet keeps an Open badge, "
                                      'while past-due invoices show Overdue.',
                           'userImpact': "An accountant opening the bill can't see when it is due, and the "
                                         'Open badge hides that it is already late, so it may be paid late.',
                           'fixHypothesis': 'The drawer may read the due date from a different field than '
                                            'the grid. Bills also seem to lack the overdue status that '
                                            'invoices compute; applying the same rule would likely fix the '
                                            'badge.',
                           'acceptance': ['The BILL-001 drawer shows the same due date as the Bills grid.',
                                          'A bill whose due date has passed gets the same overdue treatment '
                                          'as an invoice.',
                                          'Grid and drawer due dates match for every bill.'],
                           'verify': ['Check whether bills intentionally have no Overdue status in Books.',
                                      "Check whether BILL-001's empty drawer due date comes from incomplete "
                                      'demo seed data.'],
                           'searchTerms': ['bill due date missing', 'bill overdue status', 'BILL-001 drawer'],
                           'impact': 'data-integrity',
                           'confidence': 'medium',
                           'confidenceReason': 'Grid and drawer text quoted with the dash code point, but '
                                               'seeded demo bills may be incomplete.',
                           'demoArtifactRisk': True},
 'books.i-canceled-spelling': {'title': "Spell 'Canceled' the same way in Books and Banking",
                               'summary': "Books invoice and bill status badges read 'Cancelled', while "
                                          "Banking uses 'Canceled' in its toasts, filters and transaction "
                                          'states.',
                               'userImpact': 'A spelling difference only, but it sits in status badges '
                                             'admins see on both sides of the product.',
                               'fixHypothesis': 'Books strings seem to have been written apart from '
                                                "Banking's. Aligning Books badges to the style guide's "
                                                'spelling would likely be a copy-only fix.',
                               'acceptance': ['Books invoice and bill status badges use the same spelling as '
                                              'Banking.',
                                              'Books filters and toasts use that same spelling.'],
                               'verify': ["Check the content style guide for 'canceled' vs 'cancelled'.",
                                          'Check whether the Books status strings come from a third-party '
                                          'accounting integration.'],
                               'searchTerms': ['Cancelled vs Canceled',
                                               'status badge spelling',
                                               'Books cancelled'],
                               'impact': 'consistency',
                               'confidence': 'high',
                               'confidenceReason': 'Both spellings quoted verbatim from badges, a toast and '
                                                   'a URL parameter.',
                               'demoArtifactRisk': False},
 'books.i-cmd-s-conflict': {'title': 'Stop assigning ⌘S to both Save and Show keyboard shortcuts',
                            'summary': "The Books shortcut sheet lists ⌘S for 'Show keyboard shortcuts', "
                                       'while the journal entry, rule and tag drawers advertise ⌘S as Save '
                                       'in their tooltips.',
                            'userImpact': "An accountant pressing ⌘S to save a journal entry can't know "
                                          'which action will run, so they may open the shortcut sheet '
                                          'instead of saving and not notice.',
                            'fixHypothesis': 'One of the two listings is likely stale. Giving Show keyboard '
                                             'shortcuts a different key and keeping ⌘S for Save would likely '
                                             'resolve it.',
                            'acceptance': ['No two actions share ⌘S across the shortcut sheet and drawer '
                                           'tooltips.',
                                           'Pressing ⌘S in an open journal entry drawer saves the entry.',
                                           'The shortcut sheet lists the key that actually opens it.'],
                            'verify': ['Check what ⌘S does inside an open drawer and on the Books home page.',
                                       'Check the Books shortcut spec for the intended binding of Show '
                                       'keyboard shortcuts.'],
                            'searchTerms': ['cmd S shortcut conflict',
                                            'keyboard shortcuts sheet',
                                            'Save shortcut drawer'],
                            'impact': 'broken-task',
                            'confidence': 'medium',
                            'confidenceReason': 'Both labels quoted verbatim, but which action ⌘S fires was '
                                                'not tested.',
                            'demoArtifactRisk': False},
 'books.i-create-add-verbs': {'title': 'Use one verb pattern for Books primary create buttons',
                              'summary': "Books list pages name their primary action 'Create invoice', "
                                         "'Create rule', 'Add a customer', 'Add a vendor', 'Add ledger "
                                         "account' and 'Add account'.",
                              'userImpact': 'Polish only. Each button is clear on its own; the mix of '
                                            "'Create', 'Add' and 'Add a' shows only when moving between "
                                            'pages.',
                              'fixHypothesis': 'Each page seems to have named its button on its own. Picking '
                                               'one verb rule, and one rule for the article, would likely '
                                               'resolve it.',
                              'acceptance': ['Books primary create buttons follow one verb pattern.',
                                             "Primary create buttons either all use an article ('a') or none "
                                             'do.'],
                              'verify': ["Check the content style guide for 'Create' vs 'Add', and whether "
                                         "'Add' is reserved for linking existing items.",
                                         'Check which verbs the banking product uses for customers and '
                                         'vendors.'],
                              'searchTerms': ['Create vs Add button',
                                              'Add a customer',
                                              'primary button verb'],
                              'impact': 'consistency',
                              'confidence': 'high',
                              'confidenceReason': 'Button labels quoted verbatim from nine list pages.',
                              'demoArtifactRisk': False},
 'books.i-date-formats': {'title': 'Use one date format across Books grids, drawers and Rules',
                          'summary': "Books grids show '09/27/2026', drawers show 'Sep 27, 2026', and the "
                                     "Rules 'Last applied' column mixes 'Sep 25', '08/21/25' and 'Never' in "
                                     'one column.',
                          'userImpact': 'Accountants work by date, and a two-digit year next to '
                                        "month-and-day dates in one column makes 'Last applied' easy to "
                                        'misread. The grid-to-drawer switch costs less.',
                          'fixHypothesis': 'Grids and drawers seem to use different formatters, and Rules '
                                           'may format recent and older dates differently. One formatter '
                                           'with documented grid and detail variants would likely resolve '
                                           'it.',
                          'acceptance': ["The Rules 'Last applied' column uses one format for every dated "
                                         'cell.',
                                         'No Books surface shows a two-digit year.',
                                         'Grid and drawer dates follow the documented grid and detail '
                                         'variants.'],
                          'verify': ['Look up the date standard in the content style guide and whether '
                                     'MM/DD/YYYY is approved for dense accounting grids.',
                                     'Check whether Books is meant to follow the same date rules as the '
                                     'banking product.'],
                          'searchTerms': ['Books date format', 'Last applied date', 'MM/DD/YYYY grid'],
                          'impact': 'consistency',
                          'confidence': 'high',
                          'confidenceReason': 'Each format quoted verbatim from grids, drawers and the Rules '
                                              'table.',
                          'demoArtifactRisk': False},
 'books.i-drawer-truncation': {'title': 'Keep grid labels readable when a Books drawer is open',
                               'summary': 'With a detail drawer open, the Transactions grid truncates Ledger '
                                          "cells to 'Un…', 'C' and 'I.', and two dashboard Expenses rows "
                                          "both read 'Other…'.",
                               'userImpact': 'An accountant comparing a transaction in the drawer with its '
                                             "neighbors can't read their ledger names, and two different "
                                             "'Other…' rows look identical.",
                               'fixHypothesis': 'The grid seems to shrink every column to fit instead of '
                                                'hiding or scrolling. Letting the grid scroll horizontally, '
                                                'or dropping low-priority columns, while a drawer is open '
                                                'would likely resolve it.',
                               'acceptance': ['With a drawer open, Ledger account cells show a recognizable '
                                              'name or a tooltip with the full name.',
                                              'Dashboard By ledger account rows stay distinguishable beside '
                                              'an open drawer.'],
                               'verify': ['Check the design-system data grid docs for expected behavior when '
                                          'a side drawer reduces available width.',
                                          'Record the viewport width used, since truncation depends on it.'],
                               'searchTerms': ['drawer truncates grid',
                                               'column truncation',
                                               'Other truncated'],
                               'impact': 'broken-task',
                               'confidence': 'medium',
                               'confidenceReason': 'Truncated cell text quoted, but the result depends on '
                                                   'viewport width.',
                               'demoArtifactRisk': False},
 'books.i-fake-disabled-save': {'title': "Match the rule drawer Save button's look to its real state",
                                'summary': 'On an empty Create categorization rule drawer, Save renders at '
                                           'opacity 0.4 but is neither disabled nor aria-disabled; pressing '
                                           'it does nothing and shows no message.',
                                'userImpact': 'Screen reader and keyboard users reach an enabled Save that '
                                              'silently does nothing. Sighted users see what looks like a '
                                              'disabled button with no hint of what is missing.',
                                'fixHypothesis': 'The button seems to be styled as disabled without the '
                                                 'disabled or aria-disabled attribute. Either setting '
                                                 'aria-disabled with an explanation, or keeping it enabled '
                                                 'and showing validation on press, would likely resolve it.',
                                'acceptance': ['When Save looks disabled, it exposes disabled or '
                                               'aria-disabled=true.',
                                               'Pressing Save on an empty rule either does nothing because '
                                               'it is disabled, or shows which fields are required.'],
                                'verify': ['Check the design-system button docs for the approved disabled '
                                           'pattern: native disabled or aria-disabled with an explanation.'],
                                'searchTerms': ['fake disabled button',
                                                'rule Save opacity',
                                                'aria-disabled missing'],
                                'impact': 'accessibility',
                                'confidence': 'high',
                                'confidenceReason': 'Computed opacity and the disabled and aria-disabled '
                                                    'properties read with DevTools.',
                                'demoArtifactRisk': False},
 'books.i-frequency-aria': {'title': 'Mark the selected report frequency correctly in the listbox',
                            'summary': "On the balance sheet report, the frequency combobox reads 'Monthly', "
                                       "but the open listbox exposes 'Annual' as the selected option.",
                            'userImpact': 'A screen reader user opening the menu hears Annual as selected '
                                          'and may believe the report is annual, or pick the wrong period.',
                            'fixHypothesis': 'The listbox may mark the first option selected by default '
                                             'instead of the current value. Setting the selected state from '
                                             'the combobox value would likely fix it.',
                            'acceptance': ['With the combobox reading Monthly, only the Monthly option is '
                                           'exposed as selected.',
                                           'Changing the frequency moves the selected state to the new '
                                           'option.'],
                            'verify': ['Check the design-system select or combobox docs for whether selected '
                                       'state is set automatically from the value.'],
                            'searchTerms': ['aria-selected wrong option',
                                            'report frequency listbox',
                                            'combobox selected state'],
                            'impact': 'accessibility',
                            'confidence': 'high',
                            'confidenceReason': 'Combobox value and option selected states read from the '
                                                'accessibility tree.',
                            'demoArtifactRisk': False},
 'books.i-je-default-date': {'title': 'Default new journal entries to the local date, not UTC',
                             'summary': "At 21:45 GMT-0700 on Sep 24, a new journal entry's Date is "
                                        "pre-filled 'Sep 25, 2026', and the dashboard range ends in "
                                        "'Tomorrow'.",
                             'userImpact': 'An accountant booking an entry in the evening in the Americas '
                                           "gets tomorrow's date by default and may post it to the wrong "
                                           "day, or the wrong month on a month's last evening.",
                             'fixHypothesis': 'The default likely comes from the UTC date rather than the '
                                              "browser's or company's timezone. Using the local or company "
                                              'timezone for date-only defaults would likely fix it.',
                             'acceptance': ['A new journal entry created at 21:45 local time defaults to '
                                            "that day's local date.",
                                            'At the same time, the dashboard range label does not end in '
                                            "'Tomorrow'.",
                                            'On the last evening of a month, the default date stays in that '
                                            'month.'],
                             'verify': ["Check whether Books defines dates by the company's timezone or the "
                                        "user's browser.",
                                        "Check whether the demo's frozen clock or server timezone could "
                                        'produce the Sep 25 default.'],
                             'searchTerms': ['journal entry default date',
                                             'UTC timezone off by one',
                                             'date defaults to tomorrow'],
                             'impact': 'data-integrity',
                             'confidence': 'medium',
                             'confidenceReason': 'Browser clock and field value captured together, but the '
                                                 "demo's clock could cause it.",
                             'demoArtifactRisk': True},
 'books.i-journal-labels': {'title': 'Call journal entries by one name across Books',
                            'summary': "The sidebar says 'Journal Entries', the page title 'Manual Journal "
                                       "Entries', the Add menu 'Journal entry', the button 'Create journal "
                                       "entry' and the search chip 'Journal entries'.",
                            'userImpact': "'Manual' appears only in the page title, which may make an "
                                          'accountant wonder whether other journal entries live somewhere '
                                          'else. The case differences are polish.',
                            'fixHypothesis': 'The labels seem to have drifted per surface. Picking one name, '
                                             "with or without 'Manual', and one case would likely resolve "
                                             'it.',
                            'acceptance': ['Sidebar, page title and search chip use the same name and case.',
                                           "If 'Manual' is kept, it appears consistently or the page "
                                           'explains it.'],
                            'verify': ["Check whether 'Manual Journal Entries' deliberately separates user "
                                       'entries from system-generated ones.',
                                       'Check the content style guide for case in navigation items.'],
                            'searchTerms': ['Manual Journal Entries',
                                            'journal entries label',
                                            'sidebar case'],
                            'impact': 'consistency',
                            'confidence': 'high',
                            'confidenceReason': 'Each label quoted verbatim from five named surfaces.',
                            'demoArtifactRisk': False},
 'books.i-ledger-search-ranking': {'title': 'Rank exact ledger name matches first in Books pickers and '
                                            'search',
                                   'summary': "Typing 'Rent' in the journal entry ledger picker preselects "
                                              'Other Current Assets and lists Rent last; global search for '
                                              "'rent' shows three 'Current' ledgers and omits Rent.",
                                   'userImpact': "An accountant who types 'Rent' and presses Enter books the "
                                                 'line to Other Current Assets, because it is preselected. '
                                                 "In search they can't reach the Rent ledger at all.",
                                   'fixHypothesis': 'The picker seems to keep chart-of-accounts order after '
                                                    'substring filtering, and search may cap the Ledgers '
                                                    'group at three results. Ranking exact and prefix '
                                                    'matches first would likely fix both.',
                                   'acceptance': ["Typing 'Rent' in the ledger picker highlights the Rent "
                                                  'ledger first.',
                                                  "Global search for 'rent' includes the Rent ledger in the "
                                                  'Ledgers group.',
                                                  'Exact and prefix matches rank above substring matches in '
                                                  'both places.'],
                                   'verify': ['Check whether the search Ledgers group is capped at three '
                                              'results by design.',
                                              'Check whether picker order is meant to follow the chart of '
                                              'accounts.'],
                                   'searchTerms': ['ledger search ranking',
                                                   'Rent ledger picker',
                                                   'substring match search'],
                                   'impact': 'broken-task',
                                   'confidence': 'high',
                                   'confidenceReason': 'Option order and selected state captured from the '
                                                       'picker and the search results.',
                                   'demoArtifactRisk': False},
 'books.i-money-formats': {'title': 'Format money the same way across Books grids, drawers and dashboard',
                           'summary': "Books grids show '−14,934.67' with no symbol, drawers and search show "
                                      "'$14,934.67', the dashboard rounds to '$519,514', and Rules raise the "
                                      "cents in '≥ $100.00'.",
                           'userImpact': 'An accountant matching a grid row to its drawer or search result '
                                         'sees the amount written differently each time, and rounded '
                                         "dashboard figures can't be tied to the ledger to the cent.",
                           'fixHypothesis': 'Each surface seems to format amounts locally. A shared money '
                                            'formatter with named grid, detail and summary variants would '
                                            'likely resolve most of it; dropping the symbol in grids may be '
                                            'intentional.',
                           'acceptance': ['The Rules condition amount renders cents inline like other Books '
                                          'amounts.',
                                          'Grid, drawer and search show the same amount with the documented '
                                          'currency symbol rule.',
                                          'Dashboard rounding matches a documented summary variant.'],
                           'verify': ['Check the design-system money display docs for whether grids may omit '
                                      'the currency symbol.',
                                      'Check whether superscript cents are meant only for banking balances.'],
                           'searchTerms': ['Books currency format',
                                           'superscript cents Rules',
                                           'amount without dollar sign'],
                           'impact': 'consistency',
                           'confidence': 'high',
                           'confidenceReason': 'Amounts quoted verbatim for the same records across four '
                                               'surfaces.',
                           'demoArtifactRisk': False},
 'books.i-open-badge-color': {'title': 'Color the Open badge text to match its teal tint',
                              'summary': 'Paid, Overdue, Active and Auto-synced badges color their text in '
                                         'the badge hue, but Open pairs neutral rgb(221,221,229) text with a '
                                         'teal rgba(119,190,207,0.2) background.',
                              'userImpact': 'Cosmetic. Open reads slightly differently from the other '
                                            'statuses in the same column.',
                              'fixHypothesis': 'The Open status seems to map to a badge variant with neutral '
                                               'text. Using the teal text color would likely resolve it, '
                                               'unless neutral text is the intended style for that variant.',
                              'acceptance': ['The Open badge text uses the same hue as its background tint, '
                                             'like Paid and Overdue.',
                                             'All Books status badges pair text and tint the same way.'],
                              'verify': ['Check the design-system badge docs for the text and tint pairing '
                                         'of the variant Open uses.'],
                              'searchTerms': ['Open badge color', 'status badge text color', 'badge tint'],
                              'impact': 'cosmetic',
                              'confidence': 'medium',
                              'confidenceReason': 'Colors measured with DevTools, but the neutral text may '
                                                  'be a deliberate variant.',
                              'demoArtifactRisk': False},
 'books.i-payroll-empty-skeleton': {'title': 'Hide loading skeletons behind the Books Payroll empty state',
                                    'summary': "The Books Payroll page shows 'Payroll account will show up "
                                               "here when it's connected' and Connect payroll over grey "
                                               'skeleton rows in the grid behind it.',
                                    'userImpact': 'The page reads as both loading and empty, so an admin may '
                                                  'wait for data instead of connecting payroll.',
                                    'fixHypothesis': 'The grid seems to stay in its loading state when no '
                                                     'payroll account exists. Replacing the skeleton with '
                                                     'the empty state once loading resolves would likely fix '
                                                     'it.',
                                    'acceptance': ['With no payroll account connected, the Payroll grid '
                                                   'shows no skeleton rows behind the empty state.',
                                                   'Skeleton rows appear only while data is loading.'],
                                    'verify': ['Check the design-system empty-state docs for whether '
                                               'skeletons behind an empty state are an intended placeholder.',
                                               'Check whether the demo lacks a payroll integration that '
                                               'production would load.'],
                                    'searchTerms': ['skeleton empty state',
                                                    'Payroll empty Books',
                                                    'loading skeleton stuck'],
                                    'impact': 'cosmetic',
                                    'confidence': 'low',
                                    'confidenceReason': 'Seen in one screenshot only, and the demo has no '
                                                        'payroll connection.',
                                    'demoArtifactRisk': True},
 'books.i-range-separator': {'title': 'Use one separator for Books date ranges and ledger codes',
                             'summary': "The dashboard subtitle reads 'Jun 25, 2026 – Tomorrow' (en dash) "
                                        "while the timeline chip reads 'Jun 25 - Sep 25' (hyphen); ledger "
                                        "code separators switch between '—' and '-'.",
                             'userImpact': 'Cosmetic. The ranges stay readable; the difference only shows '
                                           'because both sit on the same dashboard.',
                             'fixHypothesis': 'The chip and subtitle seem to build ranges separately. Using '
                                              "the style guide's range separator in both, and one separator "
                                              'between ledger code and name, would likely resolve it.',
                             'acceptance': ['The dashboard subtitle and timeline chip use the same range '
                                            'separator.',
                                            'Ledger codes and names use one separator in drawer titles and '
                                            'rule outcomes.'],
                             'verify': ['Check the content style guide for range separators and for how '
                                        'codes and names are joined.'],
                             'searchTerms': ['date range separator',
                                             'en dash hyphen',
                                             'ledger code separator'],
                             'impact': 'cosmetic',
                             'confidence': 'high',
                             'confidenceReason': 'Separator code points captured from rendered text.',
                             'demoArtifactRisk': False},
 'books.i-same-concept-labels': {'title': 'Label the same Books fields the same way in grids and drawers',
                                 'summary': "The transaction drawer's two tabs call one table 'Line items' "
                                            "and 'Line entries', the chart of accounts says 'Debit / credit' "
                                            "and 'Debit or credit', and tabs say 'Credit card' and 'Credit'.",
                                 'userImpact': "Small, but in accounting software 'line items' and 'line "
                                               "entries' can mean different things, so an accountant may "
                                               'wonder whether the two tabs show different data.',
                                 'fixHypothesis': 'The labels seem to come from separate strings per view. '
                                                  'Choosing one term for each concept would likely be a '
                                                  'copy-only fix.',
                                 'acceptance': ['Both transaction drawer tabs name the table the same way, '
                                                'or the difference is explained.',
                                                'The chart of accounts grid and drawer use the same '
                                                'debit/credit label.',
                                                'Transactions and Accounts tabs use one name for credit card '
                                                'accounts.'],
                                 'verify': ["Check the Books glossary or content style guide for 'line "
                                            "items' vs 'line entries'.",
                                            "Check whether the Accounts 'Credit' tab deliberately covers "
                                            'more than credit cards.'],
                                 'searchTerms': ['Line items Line entries',
                                                 'Debit or credit label',
                                                 'Credit card tab'],
                                 'impact': 'consistency',
                                 'confidence': 'high',
                                 'confidenceReason': 'Labels quoted verbatim from adjacent views of the same '
                                                     'records.',
                                 'demoArtifactRisk': False},
 'books.i-setup-done-mismatch': {'title': 'Update the Finalize setup indicator when Books setup completes',
                                 'summary': "After Complete on step 3, the modal says 'You’re all set' while "
                                            "the header keeps a spinner on 'Finalize setup 3 of 3' and the "
                                            "chip's alt text says '2 of 3 setup steps complete'.",
                                 'userImpact': "An admin can't tell whether setup and the accountant invite "
                                               'actually finished. Screen reader users are told only two of '
                                               'three steps are done.',
                                 'fixHypothesis': 'The chip seems to keep its pre-completion state until a '
                                                  'later refresh. Updating it from the completion response '
                                                  'would likely resolve it; the demo may also not persist '
                                                  'the step.',
                                 'acceptance': ['After step 3 completes, the header shows no spinner and '
                                                'reads as complete.',
                                                "The chip's accessible label reports 3 of 3 steps complete.",
                                                'Chip, header and modal agree on setup status.'],
                                 'verify': ['Check whether the demo sandbox persists setup completion.',
                                            'Check whether the chip is meant to disappear once setup is '
                                            'done.'],
                                 'searchTerms': ['Finalize setup 2 of 3',
                                                 'setup chip not updating',
                                                 "You're all set"],
                                 'impact': 'data-integrity',
                                 'confidence': 'medium',
                                 'confidenceReason': 'Modal text, header and alt text captured once, in a '
                                                     'sandbox that may not persist the step.',
                                 'demoArtifactRisk': True},
 'books.i-tag-hash': {'title': 'Show tags with the same # prefix in the Transactions grid',
                      'summary': 'Tags show a # prefix on the Tags page and in the tag drawer '
                                 "('#Marketing'), but appear as plain 'Marketing' in the Transactions grid's "
                                 'Tags cells.',
                      'userImpact': 'Minor. Tags are still recognizable, but without the prefix they are '
                                    'harder to tell apart from other text in a dense grid.',
                      'fixHypothesis': 'The grid seems to render tag names as plain text rather than with '
                                       'the tag display used elsewhere. Using the same display in the grid '
                                       'would likely resolve it.',
                      'acceptance': ['Tags cells in the Transactions grid render tags the same way as the '
                                     'Tags page.',
                                     "The tag drawer, Tags page and grids agree on whether '#' is shown."],
                      'verify': ['Check the design-system tag docs for whether grids use a compact variant '
                                 "without '#'."],
                      'searchTerms': ['tag hash prefix', 'Tags column grid', 'tag display'],
                      'impact': 'consistency',
                      'confidence': 'medium',
                      'confidenceReason': 'Rendered text quoted verbatim, but a compact grid variant may be '
                                          'intended.',
                      'demoArtifactRisk': False},
 'books.i-toast-copy': {'title': 'Write Books success toasts in one pattern',
                        'summary': "Creating a journal entry shows the toast 'Successfully created the "
                                   "journal entry', while creating a tag shows 'Tag created'.",
                        'userImpact': 'Minor. Both toasts confirm the save; they just follow different '
                                      'patterns for the same event.',
                        'fixHypothesis': 'The toasts seem to be written separately. Using one pattern for '
                                         "both, likely the shorter '<Object> created' if the style guide "
                                         'prefers it, would resolve it.',
                        'acceptance': ['Journal entry and tag creation toasts follow the same pattern.',
                                       'Books create toasts use the wording pattern the content style guide '
                                       'specifies.'],
                        'verify': ['Check the content style guide for toast wording and whether '
                                   "'Successfully' is discouraged."],
                        'searchTerms': ['success toast copy', 'Successfully created', 'toast wording'],
                        'impact': 'consistency',
                        'confidence': 'high',
                        'confidenceReason': 'Both toast strings quoted verbatim after each create action.',
                        'demoArtifactRisk': False},
 'books.i-validation-patterns': {'title': 'Validate empty journal entry, tag and rule drawers the same way',
                                 'summary': "On empty save, the journal entry drawer shows 'Please complete "
                                            "all required fields.' and marks no field, the tag drawer shows "
                                            "inline 'Tag name cannot be empty', and the rule drawer shows "
                                            'nothing.',
                                 'userImpact': 'In the journal entry drawer an accountant has to hunt for '
                                               'the missing field, and in the rule drawer they get no '
                                               'feedback, so Save may look broken.',
                                 'fixHypothesis': 'Each drawer seems to handle validation separately. Using '
                                                  'the inline field error the tag drawer uses in all three '
                                                  'would likely resolve it.',
                                 'acceptance': ['Each Books create drawer marks every missing required field '
                                                'inline on empty save.',
                                                'The rule drawer shows a message when Save is pressed with '
                                                'empty fields.',
                                                'Error text in the three drawers follows one punctuation '
                                                'rule.'],
                                 'verify': ['Check the design-system form validation docs for whether a '
                                            'form-level summary is used with, or instead of, inline errors.',
                                            'Check the content style guide for required-field error '
                                            'wording.'],
                                 'searchTerms': ['drawer validation inconsistent',
                                                 'Please complete all required fields',
                                                 'empty save no error'],
                                 'impact': 'consistency',
                                 'confidence': 'high',
                                 'confidenceReason': 'Error text, styles and the absence of alert text '
                                                     'checked in the DOM.',
                                 'demoArtifactRisk': False},
 'cards.i-assign-button-relabel': {'title': 'Show a stable Next button on step 1 of Assign budget',
                                   'summary': "The Assign budget dialog opens with a disabled 'Assign' "
                                              "button that becomes 'Next' after a member is picked; step 2 "
                                              "then shows 'Assign' again.",
                                   'userImpact': "An admin may expect the first 'Assign' to finish the job "
                                                 'and be surprised by a second step, or hesitate when the '
                                                 'label changes under the cursor.',
                                   'fixHypothesis': 'The button label seems to depend on selection state '
                                                    "rather than on the step. Labeling step 1 'Next' from "
                                                    'the start would likely remove the shift.',
                                   'acceptance': ["Step 1 of Assign budget shows 'Next' whether or not a "
                                                  'member is selected.',
                                                  "Only the last step's primary button reads 'Assign'."],
                                   'verify': ["Check whether 'Assign' on an empty step 1 is intended, for "
                                              'example because some paths skip step 2.'],
                                   'searchTerms': ['Assign budget button label',
                                                   'Assign changes to Next',
                                                   'Assign Team Lunch dialog'],
                                   'impact': 'consistency',
                                   'confidence': 'high',
                                   'confidenceReason': 'Button labels read from the accessibility tree at '
                                                       'each step.',
                                   'demoArtifactRisk': False},
 'cards.i-blocking-case': {'title': 'Match Spend Policies rule wording in Reimbursement settings',
                           'summary': "Spend Policies labels reimbursement rules '(Not blocking)' and "
                                      "'(Blocking)'; Reimbursement settings shows them as '(not blocking)' "
                                      "and '(blocking)'. Card rules say 'if transaction amount exceeds', "
                                      "reimbursement rules 'for expenses that exceed'.",
                           'userImpact': 'Polish. The meaning is clear, but the same rule reads differently '
                                         'depending on where the admin sees it.',
                           'fixHypothesis': 'The page and the dialog seem to build rule summaries '
                                            'separately. Generating both from one summary would likely align '
                                            'the casing; the two threshold phrasings may need a content '
                                            'decision.',
                           'acceptance': ['The blocking qualifier uses one casing on Spend Policies and in '
                                          'Reimbursement settings.',
                                          'Card and reimbursement threshold rules use one phrasing pattern, '
                                          'or the difference is documented.'],
                           'verify': ['Check the content style guide for casing inside parentheses and for '
                                      "threshold phrasing ('exceeds' versus 'that exceed')."],
                           'searchTerms': ['Not blocking casing',
                                           'policy rule summary',
                                           'Reimbursement settings policy summary'],
                           'impact': 'consistency',
                           'confidence': 'high',
                           'confidenceReason': 'The same rule text quoted verbatim in both casings from two '
                                               'surfaces.',
                           'demoArtifactRisk': False},
 'cards.i-currency-formats': {'title': 'Use one money format across Cards and Team Spend',
                              'summary': 'Amounts appear with superscript cents in tables, flat cents in '
                                         "panels, abbreviated ('$3.5k available · $5k spend limit') and "
                                         "without cents ('$1,000'), sometimes mixed in one row ('$183.89 / "
                                         "$250').",
                              'userImpact': "Admins compare spend against limits. When '$183.89 / $250' "
                                            "mixes precision and '$3.5k' sits near '$3,500.00', it takes "
                                            'longer to see how much room is left.',
                              'fixHypothesis': 'Surfaces seem to format money locally. A shared formatter '
                                               'with named variants (table, summary, compact) and a rule for '
                                               'when cents appear would likely remove most of the drift.',
                              'acceptance': ['Paired values such as spent / limit use the same precision on '
                                             'both sides.',
                                             "The card panel's available and limit amounts use the same "
                                             'format as the limit breakdown, or the compact form is '
                                             'documented.',
                                             'Team members Available amounts all show cents or all omit '
                                             'them.'],
                              'verify': ["Look up Mercury's money formatting rules in the content style "
                                         "guide, including when cents and 'k' abbreviations are allowed.",
                                         'Check whether superscript cents are a table-only design-system '
                                         'pattern.'],
                              'searchTerms': ['currency format', 'superscript cents', '$5k abbreviation'],
                              'impact': 'consistency',
                              'confidence': 'high',
                              'confidenceReason': 'Amounts quoted verbatim and the superscript span measured '
                                                  'with DevTools.',
                              'demoArtifactRisk': False},
 'cards.i-date-formats': {'title': 'Use one date format in Cards and show the year in expense lists',
                          'summary': 'Expense lists drop the year, so Oct 1, 2024 rows look out of order '
                                     "below Sep 2026 rows; other screens use 'Sep 22, 2026', 'Sep 22nd at "
                                     "5:00pm' and 'September 24, 2026'.",
                          'userImpact': 'Without a year, an old expense reads like a recent one, so an admin '
                                        'reviewing reimbursements may misjudge which requests are stale or '
                                        'past the submission window.',
                          'fixHypothesis': 'List cells seem to use a month-day format regardless of year. '
                                           'Adding the year when it is not the current year, and routing '
                                           'other dates through one formatter, would likely fix both.',
                          'acceptance': ['Expense list dates outside the current year show the year.',
                                         'Expense panel, timeline and review screens use one long date '
                                         'format, without ordinals unless the style guide allows them.',
                                         'The budget preview and its start date field show the date in the '
                                         'same format.'],
                          'verify': ["Look up Mercury's date standard in the content style guide and note "
                                     'which observed formats it allows.',
                                     'Check whether the Oct 1, 2024 expenses are seeded demo data with '
                                     'unusual dates.'],
                          'searchTerms': ['date format reimbursements',
                                          'missing year in date list',
                                          'Sep 22nd ordinal'],
                          'impact': 'consistency',
                          'confidence': 'high',
                          'confidenceReason': 'Every date format quoted verbatim from rendered text on named '
                                              'screens.',
                          'demoArtifactRisk': False},
 'cards.i-dropdown-menu-density': {'title': 'Use one menu row size for More and Actions menus in Cards',
                                   'summary': 'The card panel More menu has 52px rows with 16px 28px padding '
                                              'and 14px labels; the budget Actions and expense More menus '
                                              'have 40px rows, 8px 12px padding and 15px labels.',
                                   'userImpact': 'Minor. Two overflow menus with the same job look '
                                                 'different, so the card panel feels built on another kit.',
                                   'fixHypothesis': 'The card panel menu seems to use a different menu '
                                                    'component or density. Moving it to the menu used on '
                                                    'budget rows would likely match them.',
                                   'acceptance': ['The card More menu, budget Actions menu and expense More '
                                                  'menu share row height, padding and label size.',
                                                  'All three menus use the same item corner radius.'],
                                   'verify': ['Check the design system for menu density variants and whether '
                                              'the card panel is meant to use a larger one.'],
                                   'searchTerms': ['More menu row height',
                                                   'dropdown menu density',
                                                   'overflow menu padding'],
                                   'impact': 'consistency',
                                   'confidence': 'high',
                                   'confidenceReason': 'Row height, padding and label size measured with '
                                                       'DevTools on two menus.',
                                   'demoArtifactRisk': False},
 'cards.i-error-copy': {'title': 'Use one wording and punctuation pattern for card form errors',
                        'summary': "Card flow validation errors use four patterns: 'Please complete this "
                                   "field', 'Please enter a valid US state.', 'Must be a dollar value.' and "
                                   "'Must be 4 digits', some with periods.",
                        'userImpact': 'Minor. Each error still explains the problem, but the shifting tone '
                                      'and punctuation make the forms feel unedited.',
                        'fixHypothesis': 'Each form likely defines its own validation strings. A shared set '
                                         'of messages that follow one punctuation rule would likely align '
                                         'them.',
                        'acceptance': ['Card flow validation messages all end with a period, or none do.',
                                       'Required-field errors and format errors each follow one phrasing '
                                       'pattern across Cards forms.'],
                        'verify': ['Check the content style guide for error message punctuation and whether '
                                   "errors start with 'Please'."],
                        'searchTerms': ['validation message punctuation',
                                        'Please complete this field',
                                        'error copy consistency'],
                        'impact': 'consistency',
                        'confidence': 'medium',
                        'confidenceReason': 'Error strings read from the accessibility tree; the punctuation '
                                            'rule is not known to us.',
                        'demoArtifactRisk': False},
 'cards.i-expense-status-colors': {'title': 'Give expense status badges colors that match their meaning',
                                   'summary': "'Payment Pending' uses the green success badge, and "
                                              "'Declined' uses the orange warning badge also used for a "
                                              'Suspended card, although a pink error badge exists for '
                                              "'Action Required'.",
                                   'userImpact': "Admins scanning reimbursements may read 'Payment Pending' "
                                                 "as paid and treat 'Declined' as a warning instead of a "
                                                 'final outcome.',
                                   'fixHypothesis': 'Each status seems to pick a badge tone without a shared '
                                                    'rule. Mapping pending states to a neutral tone and '
                                                    'declines to error would likely fix it, if the badge '
                                                    'guidance agrees.',
                                   'acceptance': ["'Payment Pending' does not use the success badge.",
                                                  "'Declined' uses a different badge tone from a Suspended "
                                                  'card, or the shared tone is documented.',
                                                  'Expense status labels follow the same casing rule as card '
                                                  'statuses.'],
                                   'verify': ['Check the design-system badge docs for the intended meaning '
                                              'of success, warning and error tones.',
                                              "Check whether green for 'Payment Pending' is meant to signal "
                                              "'approved' rather than 'paid'."],
                                   'searchTerms': ['Payment Pending green',
                                                   'expense status badge color',
                                                   'Declined badge warning'],
                                   'impact': 'consistency',
                                   'confidence': 'medium',
                                   'confidenceReason': 'Badge classes and colors captured with DevTools; the '
                                                       'tone mapping may be deliberate.',
                                   'demoArtifactRisk': False},
 'cards.i-filter-controls': {'title': 'Use one filter button style in Cards, Team Spend and Reimbursements',
                             'summary': "Cards uses a borderless filled pill 'Add filter' (radius 9999px); "
                                        'Reimbursements and Team Spend use bordered 8px-radius filter '
                                        'buttons, and Reimbursements adds bordered segmented buttons.',
                             'userImpact': 'Admins filter lists in all three places. Different shapes for '
                                           'the same control make filters slower to spot on each page.',
                             'fixHypothesis': 'The pages may use different generations of the filter bar. '
                                              'Moving all three to one filter bar component would likely '
                                              'align them.',
                             'acceptance': ["'Add filter' on Cards and Reimbursements renders with the same "
                                            'shape, border and background.',
                                            "Team Spend filter dropdowns match 'Add filter' elsewhere, or "
                                            'the difference is documented.'],
                             'verify': ['Check the design system for the current filter bar component and '
                                        'which of the two styles it uses.'],
                             'searchTerms': ['Add filter button style',
                                             'filter bar inconsistent',
                                             'filter pill vs bordered'],
                             'impact': 'consistency',
                             'confidence': 'high',
                             'confidenceReason': 'Radius, border and background captured with DevTools on '
                                                 'three pages.',
                             'demoArtifactRisk': False},
 'cards.i-finish-button-size': {'title': 'Use the standard primary button size for Finish in card '
                                         'replacement',
                                'summary': 'Next and Confirm in the replacement flow are 40px primary pills '
                                           "with 480-weight text; the final 'Finish' button is 32px tall "
                                           'with 400-weight text.',
                                'userImpact': "Minor. The last step's main action looks smaller than the "
                                              'ones before it, so it can read as a secondary option.',
                                'fixHypothesis': 'Finish likely uses the small button size. Switching it to '
                                                 'the size used by Next and Confirm would likely resolve it.',
                                'acceptance': ['Finish on the replacement success step is 40px tall with the '
                                               'same padding and weight as Next and Confirm.',
                                               'Finish keeps the primary background color.'],
                                'verify': ['Check the design-system button docs for whether a small primary '
                                           'button is allowed as the last action of a full-screen flow.'],
                                'searchTerms': ['Finish button size',
                                                'replacement flow primary button',
                                                'small primary button'],
                                'impact': 'cosmetic',
                                'confidence': 'high',
                                'confidenceReason': 'Computed height, padding and font weight captured with '
                                                    'DevTools.',
                                'demoArtifactRisk': False},
 'cards.i-merchant-name-case': {'title': 'Spell GitHub the same way in budgets and Spend Policies',
                                'summary': "The Software Subscriptions budget lists 'GitHub'; Spend Policies "
                                           "lists the same merchant as 'Github' twice. Merchant chips also "
                                           'differ in size between the two pages.',
                                'userImpact': 'Minor, but a misspelled well-known brand looks careless, and '
                                              'an admin may wonder whether the two spellings are two '
                                              'merchants.',
                                'fixHypothesis': 'The two pages may take merchant names from different '
                                                 'sources, one normalized and one typed. Using one merchant '
                                                 'display name would likely fix the spelling, if it is not '
                                                 'just seed data.',
                                'acceptance': ["Spend Policies shows 'GitHub' wherever the budget page does.",
                                               'Merchant chips on budget detail and Spend Policies share one '
                                               'size.'],
                                'verify': ['Check whether merchant names come from a shared merchant '
                                           'directory or were typed into demo seed data.',
                                           'Check the design system for the merchant chip component and its '
                                           'sizes.'],
                                'searchTerms': ['Github spelling',
                                                'merchant name casing',
                                                'merchant chip size'],
                                'impact': 'consistency',
                                'confidence': 'medium',
                                'confidenceReason': 'Chip text quoted verbatim, but the spelling may come '
                                                    'from demo seed data.',
                                'demoArtifactRisk': True},
 'cards.i-modal-dismiss-button': {'title': 'Use one dismiss button style and label in Cards dialogs',
                                  'summary': "Cards dialogs dismiss three ways: a borderless 'Cancel' in "
                                             "Edit card limits, a filled grey 'Go back' pill in Freeze, "
                                             'nickname, Transfer and shipping dialogs, and a filled grey '
                                             "'Cancel' in budget dialogs.",
                                  'userImpact': 'Admins learn where to click to back out of a dialog. When '
                                                "the exit reads 'Cancel' in one dialog and 'Go back' in the "
                                                'next, and changes weight, it takes a second look to find '
                                                'it.',
                                  'fixHypothesis': 'Edit card limits seems to use a ghost button variant '
                                                   'while the other dialogs use the filled secondary pill. '
                                                   'Picking one variant and one label for dialog dismissal '
                                                   'would likely remove the split.',
                                  'acceptance': ['Every Cards and Team Spend dialog dismisses with the same '
                                                 'button variant, padding and height.',
                                                 'The Edit card limits dismiss button has the same '
                                                 'background as the Freeze card dismiss button.',
                                                 'Dismiss buttons use one label, or follow a documented rule '
                                                 "for when 'Go back' and 'Cancel' apply."],
                                  'verify': ['Check the design-system dialog guidance for which button '
                                             'variant dismisses a dialog.',
                                             "Check the content style guide for whether 'Go back' and "
                                             "'Cancel' have distinct meanings, such as confirmations versus "
                                             'forms.'],
                                  'searchTerms': ['Go back vs Cancel',
                                                  'dialog dismiss button',
                                                  'modal cancel button variant'],
                                  'impact': 'consistency',
                                  'confidence': 'high',
                                  'confidenceReason': 'Computed background, padding and size captured with '
                                                      'DevTools on three dialogs.',
                                  'demoArtifactRisk': False},
 'cards.i-modal-heading-semantics': {'title': 'Mark the Edit card limits dialog title as a heading',
                                     'summary': 'The Edit card limits dialog title renders in a plain span, '
                                                'while the nickname, Freeze and Archive budget dialogs use '
                                                'an h2 with the same 19px/400 styling.',
                                     'userImpact': 'Screen reader users who move through a dialog by heading '
                                                   "can't find or identify the Edit card limits title the "
                                                   'way they can in the sibling dialogs.',
                                     'fixHypothesis': 'The limits dialog seems to render its title outside '
                                                      'the title slot the other dialogs use. Using the same '
                                                      'title element would likely give it heading semantics.',
                                     'acceptance': ['The Edit card limits dialog title is an h2, matching '
                                                    'the nickname and Freeze dialogs.',
                                                    "The dialog's accessible name matches its visible "
                                                    'title.'],
                                     'verify': ['Check the design-system dialog docs for how titles are '
                                                'passed, and whether the limits dialog uses that dialog '
                                                'component.'],
                                     'searchTerms': ['Edit card limits heading',
                                                     'dialog title h2',
                                                     'modal title span'],
                                     'impact': 'accessibility',
                                     'confidence': 'high',
                                     'confidenceReason': 'Element tags inspected with DevTools on four '
                                                         'dialogs.',
                                     'demoArtifactRisk': False},
 'cards.i-modal-title-case': {'title': 'Match card dialog titles to their More menu labels',
                              'summary': "The More menu items 'Edit nickname' and 'Edit limit' open dialogs "
                                         "titled 'Change Card Nickname' and 'Edit card limits'; their "
                                         "primary buttons read 'Save' and 'Save Limit'.",
                              'userImpact': "A polish issue. The verb switch from 'Edit' to 'Change' and the "
                                            'mixed casing make the menu and its dialogs read as if written '
                                            'separately.',
                              'fixHypothesis': "The nickname dialog title and the 'Save Limit' button may "
                                               'predate a sentence-case pass. Renaming them to match the '
                                               'menu verb and sentence case would likely align them.',
                              'acceptance': ['The nickname dialog title uses sentence case and the same verb '
                                             'as its More menu item.',
                                             'The Edit card limits primary button uses sentence case.',
                                             "Both dialogs' primary buttons follow one labeling pattern, "
                                             "either generic 'Save' or 'Save' plus the object."],
                              'verify': ['Check the content style guide for dialog title and button casing, '
                                         "and whether button labels repeat the object ('Save limit') or stay "
                                         "generic ('Save')."],
                              'searchTerms': ['Change Card Nickname',
                                              'Save Limit title case',
                                              'dialog title casing'],
                              'impact': 'consistency',
                              'confidence': 'medium',
                              'confidenceReason': 'Titles and button labels quoted verbatim; whether Title '
                                                  'Case is wrong depends on a style guide we have not seen.',
                              'demoArtifactRisk': False},
 'cards.i-more-menu-clipped': {'title': "Stop the card More menu from hiding 'Cancel card'",
                               'summary': 'With details expanded on a virtual card, the More menu renders '
                                          "inside the panel's scroll container, so 'Cancel card' (top 869px) "
                                          "sits below the panel's visible bottom (868px).",
                               'userImpact': "An admin who needs to cancel a virtual card doesn't see the "
                                             'option unless they think to scroll the panel while the menu is '
                                             'open.',
                               'fixHypothesis': 'The menu likely renders inline inside an ancestor with '
                                                "overflow 'hidden auto'. Rendering it in a portal, or "
                                                'letting it flip upward, would likely keep every item '
                                                'visible.',
                               'acceptance': ['With Show details expanded, every More menu item on a virtual '
                                              'card is fully visible without scrolling the panel.',
                                              'The menu repositions or flips when there is not enough room '
                                              'below its trigger.'],
                               'verify': ['Check the design-system menu or popover docs for whether overflow '
                                          'menus are meant to render in a portal.'],
                               'searchTerms': ['More menu clipped',
                                               'Cancel card hidden',
                                               'dropdown overflow panel'],
                               'impact': 'broken-task',
                               'confidence': 'high',
                               'confidenceReason': "Element rects and the ancestor's overflow value measured "
                                                   'with DevTools.',
                               'demoArtifactRisk': False},
 'cards.i-non-budget-hyphen': {'title': "Hyphenate 'Non-budget spend' in the Team Spend legend",
                               'summary': "The Team Spend display-options switch reads 'Include non-budget "
                                          "spend'; the legend entry it adds reads 'Non budget spend' with no "
                                          'hyphen.',
                               'userImpact': 'A minor copy slip; it does not affect reading the chart.',
                               'fixHypothesis': 'The legend label seems to be a separate string from the '
                                                "switch. Changing it to 'Non-budget spend' would likely "
                                                'resolve it.',
                               'acceptance': ["The Team Spend legend reads 'Non-budget spend'.",
                                              'The switch label and the legend use the same spelling.'],
                               'verify': ['Check the content style guide for the approved spelling of '
                                          "'non-budget'."],
                               'searchTerms': ['Non budget spend', 'non-budget hyphen', 'Team Spend legend'],
                               'impact': 'consistency',
                               'confidence': 'high',
                               'confidenceReason': 'Both spellings read from leaf text nodes on the same '
                                                   'screen.',
                               'demoArtifactRisk': False},
 'cards.i-pin-input-one-off': {'title': 'Match the Change PIN error to standard card form errors',
                               'summary': 'Change PIN uses a 64px borderless input with 38px digits, a '
                                          "lowercase 'save' key button and a 13px error with no icon; other "
                                          'card forms use 40px fields and 12px errors with a warning icon.',
                               'userImpact': 'The PIN error lacks the warning icon other errors carry, so it '
                                             "is easier to miss, and the lowercase 'save' key looks unlike "
                                             'any other submit control in Cards.',
                               'fixHypothesis': 'The PIN screen seems to use a custom input rather than the '
                                                'shared text field. Keeping the large digits but using the '
                                                'shared error message and button would likely close most of '
                                                'the gap.',
                               'acceptance': ["The 'Must be 4 digits' error uses the same size, color and "
                                              'warning icon as other card form errors.',
                                              'The PIN submit control uses a standard button label in the '
                                              "product's usual casing."],
                               'verify': ['Check the design system for a PIN or one-time-code input and '
                                          'whether this screen is meant to use it.',
                                          "Check whether the uppercase letter-spaced 'CHANGE PIN' eyebrow is "
                                          'an approved text style.'],
                               'searchTerms': ['Change PIN input', 'PIN error style', 'reset PIN screen'],
                               'impact': 'consistency',
                               'confidence': 'medium',
                               'confidenceReason': 'Computed styles captured with DevTools, but a large PIN '
                                                   'input may be a deliberate design.',
                               'demoArtifactRisk': False},
 'cards.i-policy-error-tooltip': {'title': 'Show the receipt threshold error below the field like other '
                                           'forms',
                                  'summary': "Clearing the receipt threshold shows 'Please complete this "
                                             "field' as a white 13px tooltip above the input; other Cards "
                                             'and Team Spend forms show a pink 12px message with a warning '
                                             'icon below the field.',
                                  'userImpact': 'The tooltip reads like help text rather than an error, so '
                                                'an admin may not see why Save is disabled.',
                                  'fixHypothesis': 'The inline policy input seems to use a tooltip-style '
                                                   'error instead of the shared field error. Switching to '
                                                   'the shared error message would likely align it.',
                                  'acceptance': ['An empty receipt threshold shows its error below the '
                                                 'field, in the standard error color, with a warning icon.',
                                                 'The receipt threshold error matches the Edit budget error '
                                                 'in size and color.'],
                                  'verify': ['Check the design-system form validation guidance for whether '
                                             'tooltip errors are allowed in inline editors.'],
                                  'searchTerms': ['policy validation tooltip',
                                                  'receipt threshold error',
                                                  'Please complete this field tooltip'],
                                  'impact': 'consistency',
                                  'confidence': 'high',
                                  'confidenceReason': 'Error color, size and placement captured with '
                                                      'DevTools on three forms.',
                                  'demoArtifactRisk': False},
 'cards.i-policy-inline-input': {'title': 'Use standard fields and buttons in Spend Policies inline edit',
                                 'summary': 'Receipt and Spend controls inline edits use a filled underline '
                                            'amount input (4px top radius, no side border) and 32px '
                                            'regular-weight Cancel/Save; other Cards forms use 40px bordered '
                                            'fields and buttons.',
                                 'userImpact': 'Minor. The policy editor looks like it belongs to a '
                                               'different product than the rest of Cards, though it works.',
                                 'fixHypothesis': 'The inline editor may use an older or compact input '
                                                  'style. Moving it to the field and button sizes used by '
                                                  'Edit budget would likely align it, unless a compact '
                                                  'inline variant is intended.',
                                 'acceptance': ['The receipt amount input uses the same border and radius as '
                                                'other Cards text fields.',
                                                'Inline Cancel and Save match other Cards form buttons, or '
                                                'use a documented compact variant.'],
                                 'verify': ['Check the design system for an approved compact or inline-edit '
                                            'variant of text fields and buttons.'],
                                 'searchTerms': ['policy inline edit input',
                                                 'underline input',
                                                 'small Cancel Save buttons'],
                                 'impact': 'consistency',
                                 'confidence': 'medium',
                                 'confidenceReason': 'Computed styles captured with DevTools, but a compact '
                                                     'inline variant may be deliberate.',
                                 'demoArtifactRisk': False},
 'cards.i-receipt-policy-contradiction': {'title': 'Keep the Cards receipt switch in sync with Spend '
                                                   'Policies',
                                          'summary': "The Cards banner and card-created screen show 'Require "
                                                     "receipts for card transactions over $75' switched off, "
                                                     'while Spend Policies shows the $75 receipt requirement '
                                                     'active with a Disable button.',
                                          'userImpact': "An admin can't tell whether receipts are required. "
                                                        'They may switch it on and create a duplicate rule, '
                                                        'or assume employees are not being asked for '
                                                        'receipts.',
                                          'fixHypothesis': 'The banner switch may read a separate '
                                                           'recommendation flag rather than the policy '
                                                           'itself. Reading one policy state in both places '
                                                           'would likely resolve it; seeded demo data could '
                                                           'also set the two independently.',
                                          'acceptance': ['When the receipt requirement is active in Spend '
                                                         'Policies, the Cards banner switch shows on, or the '
                                                         'banner is hidden.',
                                                         'Toggling the banner switch changes the Spend '
                                                         'Policies receipt row, and the reverse.'],
                                          'verify': ['Check whether the banner switch and the Spend Policies '
                                                     'receipt rule are meant to be the same setting.',
                                                     'Check whether the demo seed data sets the banner state '
                                                     'and the policy independently.'],
                                          'searchTerms': ['receipt policy toggle',
                                                          'Require receipts over $75',
                                                          'receipt requirement out of sync'],
                                          'impact': 'data-integrity',
                                          'confidence': 'medium',
                                          'confidenceReason': 'Switch state and policy row read from the '
                                                              'DOM, but seeded demo data could set them '
                                                              'apart.',
                                          'demoArtifactRisk': True},
 'cards.i-replace-flow-column-shift': {'title': 'Keep the card replacement flow on one content column',
                                       'summary': 'In the Order replacement flow at 1440px, step headings '
                                                  'start at x=510 (step 1), x=566 (step 2) and x=576 '
                                                  '(success), with the success banner back at x=510.',
                                       'userImpact': 'Cosmetic. The heading and form shift sideways on each '
                                                     'step, which makes the flow feel less steady but blocks '
                                                     'nothing.',
                                       'fixHypothesis': 'Each step seems to set its own content width or '
                                                        'padding inside the shared flow shell. Giving all '
                                                        'steps the same content container would likely line '
                                                        'them up.',
                                       'acceptance': ['At 1440px, the step 1, step 2 and success headings '
                                                      'share one left edge.',
                                                      'The success banner and success heading share one left '
                                                      'edge.'],
                                       'verify': ['Check the design-system full-screen flow layout docs for '
                                                  'the expected content column width.'],
                                       'searchTerms': ['replace card flow alignment',
                                                       'step layout shift',
                                                       'full-screen flow column'],
                                       'impact': 'cosmetic',
                                       'confidence': 'high',
                                       'confidenceReason': 'Left edges measured at a fixed 1440px viewport '
                                                           'on each step.',
                                       'demoArtifactRisk': False},
 'cards.i-secondary-button-colors': {'title': 'Use one label color for the grey secondary pill button',
                                     'summary': 'The grey secondary pill has grey text (rgb 221,221,229) as '
                                                "'Go back' and 'Submit another', but blue text (rgb "
                                                "141,164,245) as 'Create another' on the card-created "
                                                'screen.',
                                     'userImpact': "Minor. The blue 'Create another' reads more like a link, "
                                                   'and the card-created and expense-submitted screens look '
                                                   'unrelated.',
                                     'fixHypothesis': "'Create another' may carry a color override or be a "
                                                      'link styled as a button. Using the standard secondary '
                                                      "button would likely match it to 'Submit another'.",
                                     'acceptance': ["'Create another' and 'Submit another' render with the "
                                                    'same label color, background and size.',
                                                    'Every grey secondary pill in Cards uses one label '
                                                    'color.'],
                                     'verify': ['Check the design-system button docs for whether a secondary '
                                                'variant with link-colored text exists.'],
                                     'searchTerms': ['Create another button color',
                                                     'secondary button text color',
                                                     'Submit another'],
                                     'impact': 'cosmetic',
                                     'confidence': 'high',
                                     'confidenceReason': 'Label color, background and padding captured with '
                                                         'DevTools on three screens.',
                                     'demoArtifactRisk': False},
 'cards.i-self-approval-no-confirm': {'title': 'Confirm before enabling reimbursement self-approval',
                                      'summary': "Disabling the Notes requirement opens 'Disable rule?' with "
                                                 "'Go back' and 'Yes, disable', but enabling Self-approval "
                                                 'applies instantly with no dialog or toast; only the button '
                                                 "changes to 'Disable'.",
                                      'userImpact': 'Self-approval lets approvers sign off on their own '
                                                    'reimbursements. An admin can turn it on with one '
                                                    'misclick and get no signal that a spending control just '
                                                    'loosened.',
                                      'fixHypothesis': 'Enable seems to skip the confirmation pattern that '
                                                       'Disable uses. Adding a confirmation, or at least a '
                                                       'toast, would likely match the weight of the change, '
                                                       'unless enabling without confirmation is a deliberate '
                                                       'choice.',
                                      'acceptance': ['Clicking Enable on Self-approval opens a confirmation '
                                                     'that explains what the rule allows.',
                                                     'After enabling, a toast or status message confirms the '
                                                     'change.'],
                                      'verify': ['Check with the product owner whether enabling policy rules '
                                                 'without confirmation is deliberate.',
                                                 'Check the design-system guidance on which actions need a '
                                                 'confirmation dialog.'],
                                      'searchTerms': ['self-approval enable confirmation',
                                                      'enable rule no dialog',
                                                      'Spend Policies self-approval'],
                                      'impact': 'broken-task',
                                      'confidence': 'medium',
                                      'confidenceReason': 'Dialog and status regions checked in the DOM, but '
                                                          'the missing confirmation may be intended.',
                                      'demoArtifactRisk': False},
 'cards.i-silent-saves': {'title': 'Confirm card edits with a toast, as Transfer card does',
                          'summary': "Transferring a card shows a 'Card transferred' toast, but saving a "
                                     'nickname or limit, freezing, unfreezing, changing the PIN and saving '
                                     'Spend controls all close with no toast.',
                          'userImpact': 'After a limit or freeze change, admins have to scan the panel to '
                                        'confirm it took effect, and may repeat or doubt a change that did '
                                        'save.',
                          'fixHypothesis': 'Only some card actions seem wired to the toast system. Adding a '
                                           'short confirmation after each card edit would likely make '
                                           'feedback predictable.',
                          'acceptance': ['Saving a nickname, saving a limit, freezing and unfreezing a card '
                                         'each show a confirmation toast.',
                                         'Saving Spend controls shows a confirmation, as saving an Edit '
                                         "budget form shows 'Changes saved'.",
                                         'Each confirmation is exposed through role=status.'],
                          'verify': ['Check the design-system feedback guidance for when a toast is expected '
                                     'versus relying on the in-place change.',
                                     'Check whether silent freeze and unfreeze is a deliberate product '
                                     'choice.'],
                          'searchTerms': ['no toast after save',
                                          'card nickname saved feedback',
                                          'freeze card confirmation toast'],
                          'impact': 'consistency',
                          'confidence': 'medium',
                          'confidenceReason': 'DOM checked for role=status after each action, but silent '
                                              'saves may be intentional.',
                          'demoArtifactRisk': False},
 'cards.i-status-case-drift': {'title': "Write 'Details requested' the same way in the chip and badge",
                               'summary': "On My expenses the filter chip reads 'Details requested' and the "
                                          "badge below it 'Details Requested'; headings pair 'Drafts' with "
                                          "'Submitted Expenses', and a badge reads 'Draft - Missing info'.",
                               'userImpact': 'Polish. The same status in two casings on one screen is '
                                             "visible but won't lead to a mistake.",
                               'fixHypothesis': 'The chip and badge likely take their labels from different '
                                                'string sources. One status label map for filters and badges '
                                                'would likely align them.',
                               'acceptance': ["The 'Details requested' filter chip and status badge use "
                                              'identical text.',
                                              'Section headings on My expenses use one casing rule.',
                                              'The draft badge uses a separator the style guide allows.'],
                               'verify': ['Check the content style guide for status label casing and for '
                                          'hyphen versus en dash as a separator.'],
                               'searchTerms': ['Details Requested casing',
                                               'Submitted Expenses heading',
                                               'status badge casing'],
                               'impact': 'consistency',
                               'confidence': 'high',
                               'confidenceReason': 'Both casings of the same status quoted verbatim from one '
                                                   'screen.',
                               'demoArtifactRisk': False},
 'cards.i-subscription-actions-unlabeled': {'title': 'Label the Subscriptions row action icons',
                                            'summary': 'Each Subscriptions row ends with a 32px icon link '
                                                       'and a 32px icon button (open, block), plus a 32x70 '
                                                       'button, none with an aria-label, title or text.',
                                            'userImpact': "Screen reader users hear 'link' and 'button' with "
                                                          "no name, so they can't tell which control opens "
                                                          'the merchant and which one blocks it.',
                                            'fixHypothesis': 'The icon controls likely omit an accessible '
                                                             "label. Adding names such as 'Open Adyen N.V.' "
                                                             "and 'Block Adyen N.V.' would likely resolve "
                                                             'it.',
                                            'acceptance': ['Every icon-only control in a Subscriptions row '
                                                           'has an accessible name that includes the action '
                                                           'and the merchant.',
                                                           'No link or button in the Subscriptions table has '
                                                           'an empty accessible name.'],
                                            'verify': ['Check the design-system icon button docs for whether '
                                                       'an accessible label is a required prop.'],
                                            'searchTerms': ['Subscriptions icon button label',
                                                            'aria-label block merchant',
                                                            'unnamed icon button'],
                                            'impact': 'accessibility',
                                            'confidence': 'high',
                                            'confidenceReason': 'aria-label, title and text content '
                                                                'inspected with DevTools on a row.',
                                            'demoArtifactRisk': False},
 'cards.i-table-header-case': {'title': 'Use one header casing and name format in card tables',
                               'summary': "Manage uses sentence case headers and full names ('Jane Black'); "
                                          "Subscriptions uses Title Case ('Last 30 Day Spend', 'Payment "
                                          "Method') and 'Jane B. ••4928'; All expenses mixes 'Team Member' "
                                          "with 'Date'.",
                               'userImpact': 'Polish. Switching tabs on one page shows the same person '
                                             'written two ways and headers in two casings, a small but '
                                             'avoidable speed bump.',
                               'fixHypothesis': 'The Subscriptions and Reimbursements tables seem to define '
                                                'their headers separately from Manage. Aligning them on one '
                                                'casing and one name format would likely fix it.',
                               'acceptance': ['Column headers on Manage, Subscriptions and All expenses '
                                              'follow one casing rule.',
                                              'Cardholder names use one format on Manage and Subscriptions, '
                                              'or the short form appears only where space requires it.'],
                               'verify': ['Check the content style guide for table header casing and for '
                                          "when to abbreviate a person's last name."],
                               'searchTerms': ['table header casing',
                                               'Last 30 Day Spend',
                                               'abbreviated cardholder name'],
                               'impact': 'consistency',
                               'confidence': 'medium',
                               'confidenceReason': 'Header and name text quoted verbatim; the casing rule is '
                                                   'in a style guide we have not seen.',
                               'demoArtifactRisk': False},
 'cards.i-title-case-steps': {'title': 'Use sentence case and one address name in card replacement',
                              'summary': "The replacement flow uses Title Case ('Choose Reason', 'Confirm "
                                         "Shipping Address', 'Never Received') and calls one address 'Your "
                                         "personal mailing address' in the picker and 'Your personal "
                                         "address' on the step.",
                              'userImpact': 'The casing is polish. The two names for one address matter a '
                                            'little more: an admin may wonder whether they are choosing '
                                            'between two different addresses.',
                              'fixHypothesis': "The flow's strings seem to have been written apart from the "
                                               'sentence case used elsewhere in Cards. Rewriting them and '
                                               'using one address name would likely fix both.',
                              'acceptance': ['Step rail labels, headings and reason options in the '
                                             'replacement flow use sentence case.',
                                             'The personal address has the same name in the address picker '
                                             'and on the confirm step.'],
                              'verify': ['Check the content style guide for step-rail and radio-option '
                                         'casing.',
                                         "Check whether 'personal mailing address' and 'personal address' "
                                         'are meant to be different records.'],
                              'searchTerms': ['Confirm Shipping Address',
                                              'replacement flow title case',
                                              'personal mailing address'],
                              'impact': 'consistency',
                              'confidence': 'medium',
                              'confidenceReason': 'Strings quoted verbatim; the casing rule itself is in a '
                                                  'style guide we have not seen.',
                              'demoArtifactRisk': False},
 'cards.i-toast-copy': {'title': 'Use one wording pattern for Cards and Reimbursements toasts',
                        'summary': "Toasts mix punctuation ('Reimbursement submitted.' vs 'Card "
                                   "transferred'), phrase one approval two ways ('Reimbursement payment "
                                   "approved' vs '1 reimbursement payment approved') and show a green check "
                                   'for declines.',
                        'userImpact': "Mostly polish. The green check on 'Reimbursement request declined' "
                                      'matters more: it signals success for a rejection, which can make an '
                                      'admin think they approved it.',
                        'fixHypothesis': 'Each action seems to write its own toast string and use the '
                                         'default success icon. A shared toast copy pattern, plus a '
                                         'non-success icon for declines, would likely fix it.',
                        'acceptance': ['All Cards and Reimbursements toasts end with a period, or none do.',
                                       'Approving one request shows the same toast from the detail panel and '
                                       'from the bulk bar.',
                                       'The decline toast does not use the success check icon.',
                                       "The toast after 'Create budget' uses the same verb as the button."],
                        'verify': ['Check the content style guide for toast punctuation and for how counts '
                                   'appear in toasts.',
                                   'Check the design-system toast docs for icon variants for non-success '
                                   'outcomes.'],
                        'searchTerms': ['toast punctuation',
                                        'Reimbursement submitted toast',
                                        'decline toast green check'],
                        'impact': 'consistency',
                        'confidence': 'high',
                        'confidenceReason': 'Toast text read from role=status after each action and seen in '
                                            'captures.',
                        'demoArtifactRisk': False},
 'cards.i-view-new-card-wrong': {'title': 'Fix View card details opening an existing card after creation',
                                 'summary': 'After creating a virtual card, the success screen shows ••0330 '
                                            "and 'View card details' opens /cards/cardid-credit-1, the "
                                            'existing AWS billing card; no new row appears in the list.',
                                 'userImpact': 'An admin who just issued a card lands on a different card '
                                               'and may edit its limit or share its details thinking it is '
                                               'the new one.',
                                 'fixHypothesis': 'The success screen may link to a fixed or first-matching '
                                                  'card id rather than the one returned by creation. The '
                                                  'demo sandbox may also not persist new cards, which would '
                                                  'explain the missing row.',
                                 'acceptance': ["'View card details' opens the card created in that flow, "
                                                'showing its nickname and limit.',
                                                'The last four digits on the success screen match the card '
                                                'that opens.',
                                                'The new card appears in the Cards list after creation.'],
                                 'verify': ['Check whether the demo environment persists newly created '
                                            'cards; if not, reproduce outside the demo before filing.',
                                            'Check whether ••0330 is a fixed demo value on the card-created '
                                            'screen.'],
                                 'searchTerms': ['View card details wrong card',
                                                 'card created success screen',
                                                 'new virtual card missing from list'],
                                 'impact': 'data-integrity',
                                 'confidence': 'low',
                                 'confidenceReason': 'URL and panel text recorded once, and a demo sandbox '
                                                     'that does not persist new cards could explain it.',
                                 'demoArtifactRisk': True},
 'home.i-approval-actions': {'title': 'Use one order, verb and emphasis for approve and decline in Tasks',
                             'summary': "Recurring payment puts Decline left of 'Approve payments' at 40px; "
                                        'dual admin and reimbursement put Approve first at 32px; receipt '
                                        "exemption pairs a ghost 'Reject' with a grey 'Approve exception'.",
                             'userImpact': 'Admins working through Tasks click in different places for the '
                                           'same decision on each task type, which invites mis-clicks. The '
                                           'receipt exemption gives approve no primary color at all.',
                             'fixHypothesis': "Each task type seems to reuse its host page's actions. A "
                                              'shared approval action pattern with fixed order, size and '
                                              'verbs would likely fix it.',
                             'acceptance': ['Approve and decline appear in the same order on every task '
                                            'review.',
                                            'The approve action uses the primary variant on every task type.',
                                            'The negative action uses one verb, or the style guide documents '
                                            "when 'Reject' applies."],
                             'verify': ['Check the design-system guidance on button order in action pairs.',
                                        "Check the content style guide on 'Decline' versus 'Reject'."],
                             'searchTerms': ['approve decline button order',
                                             'Reject vs Decline',
                                             'approval actions'],
                             'impact': 'consistency',
                             'confidence': 'high',
                             'confidenceReason': 'Button positions, sizes, classes and colors captured with '
                                                 'DevTools on four task types.',
                             'demoArtifactRisk': False},
 'home.i-approved-task-lingers': {'title': 'Remove approved tasks from the Tasks list and update its count',
                                  'summary': 'After approving the $1,042.95 Jason Green payment and the '
                                             '$375.87 Jessica Awad reimbursement, both remain under '
                                             'Incomplete, the sidebar badge still reads 10, and Completed '
                                             "says 'No completed tasks'.",
                                  'userImpact': 'Admins cannot trust Tasks to show what is left to do, and '
                                                'may open and try to approve the same payment again.',
                                  'fixHypothesis': 'Tasks seem not to be refetched or marked complete after '
                                                   'an approval made on another screen. Refreshing the list '
                                                   'and count after approvals would likely fix it, unless '
                                                   'the demo sandbox does not persist approvals.',
                                  'acceptance': ['An approved task leaves Incomplete without a page reload.',
                                                 'The sidebar Tasks badge decreases after each approval.',
                                                 'Approved tasks appear under Completed with who completed '
                                                 'them and when.'],
                                  'verify': ['Check whether the demo sandbox writes approvals back to the '
                                             'tasks backend.',
                                             'Check whether tasks are meant to clear only after the payment '
                                             'settles.'],
                                  'searchTerms': ['approved task still incomplete',
                                                  'Tasks badge count stale',
                                                  'task not completed after approval'],
                                  'impact': 'broken-task',
                                  'confidence': 'medium',
                                  'confidenceReason': 'Success screens and Tasks text quoted verbatim, but '
                                                      'the state bug was seen in one demo session.',
                                  'demoArtifactRisk': True},
 'home.i-badge-styles': {'title': 'Use one badge treatment for role and status badges on Home',
                         'summary': "On the invite review, the 'Admin' badge is outlined while the 'Pending' "
                                    'badges beside it are filled at the same size and color; the dashboard '
                                    "'Failed' badge is a third, pink outlined style.",
                         'userImpact': 'Minor. Adjacent badges that differ only in fill suggest different '
                                       'meanings that may not exist.',
                         'fixHypothesis': 'The badges seem to use different variants without a stated rule. '
                                          'If outlined means role and filled means status, documenting that '
                                          'would settle it; otherwise one variant would likely fix it.',
                         'acceptance': ['Badges of the same kind on the invite review share fill and border.',
                                        'Each badge variant on Home maps to a documented meaning.'],
                         'verify': ['Check the design-system badge docs for outlined and filled variants and '
                                    'what each signals.'],
                         'searchTerms': ['badge variants',
                                         'outlined badge filled badge',
                                         'Pending badge Admin badge'],
                         'impact': 'consistency',
                         'confidence': 'medium',
                         'confidenceReason': 'Computed styles captured with DevTools, but the variants may '
                                             'be intentional.',
                         'demoArtifactRisk': False},
 'home.i-cents-format': {'title': 'Render cents one way across Home widgets',
                         'summary': 'Home mixes superscript cents (Accounts rows, hero), inline cents (the '
                                    "row tooltip, Disputes, 'Pending transfers') and abbreviations ('$6K', "
                                    "'$12.3K') beside a superscript '$950.00' in Invoicing.",
                         'userImpact': 'The same balance looks different in an account row and its own '
                                       "tooltip, and one Invoicing widget mixes '$950.00' with '$6K', so "
                                       'figures are harder to compare at a glance.',
                         'fixHypothesis': 'Widgets seem to format amounts locally, some with the shared '
                                          'superscript style and some without. Routing Home amounts through '
                                          'one amount component with a documented compact variant would '
                                          'likely fix most of it.',
                         'acceptance': ['Accounts rows and their hover tooltips render cents the same way.',
                                        "All balance table rows, including 'Pending transfers', share one "
                                        'cents treatment.',
                                        'A single widget does not mix full and abbreviated amounts for the '
                                        'same kind of value.'],
                         'verify': ['Look up the design-system amount component docs for when superscript '
                                    "cents and compact ('$6K') formats are allowed.",
                                    "Check whether the Invoicing widget's abbreviated totals are a "
                                    'deliberate density choice.'],
                         'searchTerms': ['superscript cents Home',
                                         'abbreviated amounts $6K',
                                         'amount formatting widgets'],
                         'impact': 'consistency',
                         'confidence': 'high',
                         'confidenceReason': 'Class names and computed sizes captured with DevTools for each '
                                             'amount.',
                         'demoArtifactRisk': False},
 'home.i-chips-as-menuitems': {'title': 'Give Home filter chips tab semantics and name the icon menu buttons',
                               'summary': 'Transactions widget and Tasks chips act as tabs but are '
                                          'role=menuitem buttons with no tablist or selected state; the '
                                          'avatar button and four widget kebab buttons have no accessible '
                                          'name.',
                               'userImpact': 'Screen reader users hear a set of menu items with no sign of '
                                             'which filter is active, and a nameless button for the profile '
                                             'menu and each widget menu.',
                               'fixHypothesis': 'The chips seem to reuse a menu component, and the icon '
                                                'buttons seem to lack a label. Tab or radio-group semantics '
                                                'with selected state, plus aria-labels, would likely fix it.',
                               'acceptance': ['Transactions widget and Tasks chips expose role=tab or '
                                              'role=radio, with the active chip marked selected.',
                                              'The avatar menu button has an accessible name.',
                                              'Each widget kebab button has a name that includes its '
                                              'widget.'],
                               'verify': ['Check the design-system docs for filter chips or segmented '
                                          'controls and their intended ARIA roles.'],
                               'searchTerms': ['chips role menuitem',
                                               'kebab aria-label',
                                               'avatar menu button name'],
                               'impact': 'accessibility',
                               'confidence': 'high',
                               'confidenceReason': 'Roles, aria attributes and element counts captured with '
                                                   'DevTools.',
                               'demoArtifactRisk': False},
 'home.i-command-card-mismatch': {'title': "Show the confirmed card in Command's 'Debit card issued' result",
                                  'summary': "Command's 'Issue debit card' proposal shows an unnamed virtual "
                                             'card for Jane Black at $500.00 per month; after Confirm, '
                                             "'Debit card issued' names 'Office Card' for Alice Chen with no "
                                             'spend limit.',
                                  'userImpact': 'The admin confirms a card for Jane Black and is told a card '
                                                'went to Alice Chen. They cannot tell which card exists, who '
                                                'holds it, or whether the $500 limit applied.',
                                  'fixHypothesis': 'The result card seems to render a different or fixed '
                                                   'record rather than the card created from the proposal, '
                                                   'which may be a canned demo response. Binding the result '
                                                   'to the confirmed card would likely fix it.',
                                  'acceptance': ["The 'Debit card issued' result shows the same cardholder "
                                                 'and card name as the proposal.',
                                                 'The result shows the spend limit that was confirmed.',
                                                 'A proposal with no card name shows a placeholder name '
                                                 'instead of repeating the cardholder.'],
                                  'verify': ['Check whether Command in the demo returns canned results, '
                                             "which would explain 'Office Card' and Alice Chen.",
                                             'Check the intended proposal layout when no card name is '
                                             'given.'],
                                  'searchTerms': ['Command debit card issued',
                                                  'wrong cardholder confirmation',
                                                  'Issue debit card proposal'],
                                  'impact': 'data-integrity',
                                  'confidence': 'medium',
                                  'confidenceReason': 'Proposal and result DOM quoted verbatim, but seen '
                                                      'once and possibly a canned demo response.',
                                  'demoArtifactRisk': True},
 'home.i-date-formats': {'title': 'Use one date and time format across Home and Tasks',
                         'summary': "The approval flow writes dates as 'Sep 23', 'Sep 23, 2026' and "
                                    "'September 24, 2026'; times appear as '5:12PM', 'Jul 4th at 5:00pm' and "
                                    "'5:31 PM'.",
                         'userImpact': 'Admins approving payments compare received, initiated and expense '
                                       'dates across screens. Changing formats, ordinals and AM/PM casing '
                                       'make that slower and easier to misread.',
                         'fixHypothesis': 'Screens seem to format dates and times locally. A shared '
                                          'formatter with short and long date variants and one time style '
                                          'would likely remove most of the drift.',
                         'acceptance': ['Tasks, the invite review and the payment review use one short and '
                                        'one long date format.',
                                        'Times use one AM/PM style, with the same spacing and casing '
                                        'everywhere.',
                                        'Dates carry no ordinal suffixes unless the style guide allows them.',
                                        'The balance graph hover header and its axis use the same month '
                                        'style.'],
                         'verify': ["Look up Mercury's date and time standards in the content style guide "
                                    'and note which observed formats it allows.',
                                    "Check whether the long 'September 24, 2026' form is reserved for "
                                    'confirmation or review screens.'],
                         'searchTerms': ['date format Tasks', 'time format AM PM', 'ordinal date Jul 4th'],
                         'impact': 'consistency',
                         'confidence': 'high',
                         'confidenceReason': 'Every format quoted verbatim from DOM text on named screens.',
                         'demoArtifactRisk': False},
 'home.i-disabled-lookalike': {'title': 'Match dimmed buttons on Home to their real disabled state',
                               'summary': "'Submit', 'Create account' and 'Share feedback' all render at 40% "
                                          "opacity before the form is valid; only 'Share feedback' is "
                                          'disabled, and the other two stay clickable with no aria-disabled.',
                               'userImpact': 'Sighted users may assume Submit and Create account cannot be '
                                             'pressed and never see the validation message. Screen reader '
                                             'users get no signal that the buttons are not ready.',
                               'fixHypothesis': "The buttons seem to use an 'appear disabled' style that is "
                                                'separate from the disabled attribute. Pairing that style '
                                                'with aria-disabled, or not dimming buttons that stay '
                                                'active, would likely fix it.',
                               'acceptance': ['Every button shown at 40% opacity is disabled or has '
                                              "aria-disabled='true'.",
                                              'Buttons that stay clickable to run validation are not '
                                              'dimmed.'],
                               'verify': ["Check the design-system button docs for the 'appear disabled' "
                                          'variant and its intended behavior for assistive tech.'],
                               'searchTerms': ['appearDisabled button',
                                               'aria-disabled',
                                               'disabled button clickable'],
                               'impact': 'accessibility',
                               'confidence': 'high',
                               'confidenceReason': 'Opacity, disabled and aria-disabled values and class '
                                                   'names captured with DevTools.',
                               'demoArtifactRisk': False},
 'home.i-empty-states': {'title': 'Use one empty-state pattern in Updates, Tasks and Search',
                         'summary': "Updates shows a lone 13px 'No updates' line, Tasks > Completed shows "
                                    'skeleton rows with a 17px title and 15px body, and Search with no '
                                    'matches shows no message at all.',
                         'userImpact': "With no 'no results' message, a searcher cannot tell whether search "
                                       'found nothing or is still working. The other two differ only in '
                                       'look.',
                         'fixHypothesis': 'Each surface seems to build its own empty state. Using the shared '
                                          'empty-state component where there is room, and one short text '
                                          'pattern for popovers and search, would likely align them.',
                         'acceptance': ['Search with no matches shows a message saying nothing matched.',
                                        'Updates and Tasks empty states follow the documented empty-state '
                                        'pattern for their container size.'],
                         'verify': ['Look up the design-system empty-state component and whether it has a '
                                    'compact variant for popovers.',
                                    "Check whether the 'Search all transactions' row is the intended "
                                    'no-results state for search.'],
                         'searchTerms': ['empty state', 'search no results', 'No updates popover'],
                         'impact': 'consistency',
                         'confidence': 'high',
                         'confidenceReason': 'Text, sizes and colors captured with DevTools on all three '
                                             'surfaces.',
                         'demoArtifactRisk': False},
 'home.i-empty-value-dash': {'title': 'Render empty values the same way on the invite review',
                             'summary': "On the invite review, 'Department -' has a white rgb(255,255,255) "
                                        "dash and 'Leading departments -' a grey rgb(157,157,168) dash; "
                                        "Home's Bill Pay 'Due soon -' also uses a bare hyphen.",
                             'userImpact': 'Cosmetic. The white dash reads like a value rather than an empty '
                                           'field; nothing else is affected.',
                             'fixHypothesis': 'Empty values seem to be rendered case by case. One muted '
                                              'empty-value placeholder would likely fix it.',
                             'acceptance': ['Empty values on the invite review use the same character and '
                                            'color.',
                                            'Home widgets use the same empty-value placeholder as the invite '
                                            'review.'],
                             'verify': ['Check the content style guide for the empty-value placeholder: '
                                        "hyphen, em dash, or a word such as 'None'."],
                             'searchTerms': ['empty value dash', 'placeholder dash color', 'None vs dash'],
                             'impact': 'cosmetic',
                             'confidence': 'high',
                             'confidenceReason': 'Leaf node colors captured with DevTools.',
                             'demoArtifactRisk': False},
 'home.i-legend-case': {'title': "Match 'Money in' casing in the Command chart legend",
                        'summary': "Home's Money movement cards say 'Money in' and 'Money out', while the "
                                   "Command chart legend says 'Money In' and 'Money Out'.",
                        'userImpact': 'Minor polish. The mismatch only shows when comparing a Command answer '
                                      'with Home.',
                        'fixHypothesis': 'The Command chart legend seems to use its own strings, or text '
                                         'generated with the answer. Using the same labels as Home would '
                                         'likely fix it.',
                        'acceptance': ["The Command chart legend uses the same casing for 'Money in' and "
                                       "'Money out' as Home.",
                                       'Other Command chart labels follow the same casing rule as Home '
                                       'headings.'],
                        'verify': ["Check the content style guide for whether 'Money in' and 'Money out' are "
                                   'product terms with fixed casing.',
                                   'Check whether Command chart legends are fixed strings or generated per '
                                   'answer.'],
                        'searchTerms': ['Money In casing', 'Command chart legend', 'Money in Money out'],
                        'impact': 'consistency',
                        'confidence': 'medium',
                        'confidenceReason': 'Text quoted verbatim, but the Command legend may be generated '
                                            'per answer.',
                        'demoArtifactRisk': False},
 'home.i-light-illustration': {'title': 'Add a dark-theme illustration to the bookmarks popover',
                               'summary': 'In dark mode the bookmarks education popover shows one light-mode '
                                          'PNG (406x228) as a bright block inside a popover with background '
                                          'rgb(23,23,33).',
                               'userImpact': 'Cosmetic. The bright image is jarring in dark mode but does '
                                             'not hide the explanation.',
                               'fixHypothesis': 'The popover seems to load a single raster with no dark '
                                                'variant. Adding a dark version and swapping it by theme '
                                                'would likely fix it.',
                               'acceptance': ['In dark mode the bookmarks popover shows an illustration that '
                                              'matches the dark theme.',
                                              'In light mode the current illustration still appears.'],
                               'verify': ['Check the design-system illustration guidance for how theme '
                                          'variants of product screenshots are supplied.'],
                               'searchTerms': ['bookmarks popover illustration',
                                               'dark mode image',
                                               'light screenshot dark theme'],
                               'impact': 'cosmetic',
                               'confidence': 'high',
                               'confidenceReason': 'Image source, size and popover background read with '
                                                   'DevTools.',
                               'demoArtifactRisk': False},
 'home.i-personalized-totals': {'title': 'Keep account rows in line with the personalized demo balance',
                                'summary': 'After Customize demo with a $250,000 balance, the Home hero '
                                           'reads $250,000.00 while account rows still list Ops / Payroll '
                                           '$2,023,267.12, AP $226,767.82 and Treasury $200,000.00.',
                                'userImpact': 'A prospect trying the personalized demo sees a total that '
                                              'does not match its accounts, which undercuts the demo. '
                                              'Customers on real accounts are not affected.',
                                'fixHypothesis': 'Personalization seems to override only the hero total and '
                                                 'greeting. Scaling or replacing account balances, bookmarks '
                                                 "and Command's sample names would likely make it "
                                                 'consistent.',
                                'acceptance': ['After personalization, the account rows sum to the hero '
                                               'balance.',
                                               'Bookmarks show the same balances as the account rows.',
                                               "Command proposals use the personalized user's name."],
                                'verify': ['Confirm with the demo owners whether personalization is meant to '
                                           'change account balances or only the headline.'],
                                'searchTerms': ['personalized demo balance',
                                                'Customize demo totals',
                                                'hero balance mismatch'],
                                'impact': 'consistency',
                                'confidence': 'medium',
                                'confidenceReason': 'Balances quoted verbatim, but the behavior exists only '
                                                    "in the demo's personalization feature.",
                                'demoArtifactRisk': True},
 'home.i-private-mode-leak': {'title': 'Mask amounts in Private Mode without revealing their size',
                              'summary': 'Private Mode keeps one dot per digit and keeps K and M suffixes, '
                                         "so '$•••••••••' for $5,216,471.18, '$••M' and '$•••K' still reveal "
                                         "each amount's order of magnitude.",
                              'userImpact': "Someone looking over an admin's shoulder can still tell whether "
                                            'a balance is in the thousands or millions, which is part of '
                                            'what Private Mode is meant to hide.',
                              'fixHypothesis': 'The mask seems to replace each digit instead of the whole '
                                               'value. A fixed-length mask with no suffix would likely '
                                               'remove the leak.',
                              'acceptance': ['Masked amounts show the same number of dots regardless of '
                                             'value.',
                                             'Masked amounts show no K or M suffix.'],
                              'verify': ['Check the Private Mode product spec for whether revealing '
                                         'magnitude is accepted or intended.'],
                              'searchTerms': ['Private Mode mask',
                                              'hide dollar amounts',
                                              'masked balance dots'],
                              'impact': 'broken-task',
                              'confidence': 'high',
                              'confidenceReason': 'Masked strings quoted verbatim alongside the unmasked '
                                                  'values.',
                              'demoArtifactRisk': False},
 'home.i-task-destination-mismatch': {'title': 'Link each task to the record and action it describes',
                                      'summary': "'Review category for your $300.03 Uber Eats transaction' "
                                                 'opens a $300.03 ACH payment to Domestic Ads; the daily '
                                                 'maximum task shows no pending request; the overspend task '
                                                 "offers only 'Cancel request'.",
                                      'userImpact': 'Admins click a task and land on a record that does not '
                                                    'match it, or on a page with no approve action. They '
                                                    'cannot finish the task from Tasks and may act on the '
                                                    'wrong transaction.',
                                      'fixHypothesis': 'Task links seem to point to generic pages or the '
                                                       'wrong record, which may come from seeded demo data. '
                                                       "Checking each task type's deep link and required "
                                                       'action would likely resolve it.',
                                      'acceptance': ['Each task opens a record matching the counterparty and '
                                                     'amount in its description.',
                                                     'Every approval task lands on a screen with an approve '
                                                     'action for that request.',
                                                     "Row hover links on 'Approve …' tasks use one label."],
                                      'verify': ['Check whether the demo seeds tasks with placeholder links, '
                                                 'which would make this demo-only.',
                                                 'Confirm the intended destinations for the overspend and '
                                                 'daily maximum tasks with the owning teams.'],
                                      'searchTerms': ['task deep link wrong',
                                                      'Tasks opens wrong transaction',
                                                      'overspend approve task'],
                                      'impact': 'data-integrity',
                                      'confidence': 'medium',
                                      'confidenceReason': 'Task text and destination DOM quoted verbatim, '
                                                          'but seeded demo tasks could explain the mismatch.',
                                      'demoArtifactRisk': True},
 'home.i-task-emphasis': {'title': 'Bold the same kind of entity in every Tasks description',
                          'summary': "Some Tasks rows bold the counterparty ('Jason Green', 'Monarch Books') "
                                     "while equivalent names stay regular ('Uber Eats', 'Alice C.', 'Landon "
                                     "Shepherd'), and only the Monarch Books row ends with a period.",
                          'userImpact': 'Minor. Bold names help scanning, but when only some rows use them '
                                        'the admin cannot rely on bold to find who a task is about.',
                          'fixHypothesis': 'Each task type seems to have its own description template. '
                                           'Applying one emphasis and punctuation rule to all templates '
                                           'would likely fix it.',
                          'acceptance': ['Every Tasks row bolds the same kind of entity, or no rows use '
                                         'bold.',
                                         'Task descriptions follow one ending-punctuation rule.'],
                          'verify': ['Check the content style guide for emphasis inside list rows and for '
                                     'punctuation in table cells.'],
                          'searchTerms': ['task description bold',
                                          'Tasks list emphasis',
                                          'task copy punctuation'],
                          'impact': 'consistency',
                          'confidence': 'high',
                          'confidenceReason': 'Font weights of each leaf span and row-ending text read from '
                                              'the DOM.',
                          'demoArtifactRisk': False},
 'home.i-title-case-ctas': {'title': 'Use one casing rule for task CTAs, badges and search suggestions',
                            'summary': "The payment success CTA reads 'Back to Needs Approval', "
                                       "Reimbursements badges read 'Payment Pending' and 'Action Required', "
                                       "and search lists both 'Failed Transaction' and 'Failed transaction'.",
                            'userImpact': 'Mostly polish, but search suggestions that differ only in casing '
                                          'look like two different things and take two slots in the list.',
                            'fixHypothesis': 'Strings seem to be written per team without one casing rule, '
                                             'and search suggestions may merge two sources without '
                                             "deduplication. Applying the style guide's casing and "
                                             'deduplicating suggestions case-insensitively would likely fix '
                                             'it.',
                            'acceptance': ['Reimbursements status badges use the same casing as the '
                                           "'Pending' badge on the invite review.",
                                           'Search suggestions contain no entries that differ only by '
                                           'casing.',
                                           "'Back to Needs Approval' follows the style guide's rule for "
                                           'naming tabs in buttons.'],
                            'verify': ['Check the content style guide for casing in buttons and badges, and '
                                       "whether tab names like 'Needs Approval' keep capitals when "
                                       'referenced.',
                                       'Check where search suggestions come from and whether near-duplicates '
                                       'are expected.'],
                            'searchTerms': ['Title Case badges',
                                            'duplicate search suggestions',
                                            'Back to Needs Approval'],
                            'impact': 'consistency',
                            'confidence': 'high',
                            'confidenceReason': 'Button, badge and search option text quoted verbatim from '
                                                'the DOM.',
                            'demoArtifactRisk': False},
 'home.i-two-button-systems': {'title': 'Move the Customize demo modal onto the standard button component',
                               'summary': "The 'Customize the Mercury demo' modal uses different button "
                                          'classes (28px radius) from other Home dialogs (9999px radius), '
                                          "and 'Cancel' is styled three ways across Home dialogs.",
                               'userImpact': 'Minor. The dialogs work, but Cancel appearing as a blue pill '
                                             'in one dialog and plain text in another makes the secondary '
                                             'action harder to recognize.',
                               'fixHypothesis': 'The demo lead-form modal seems to be built on a separate '
                                                'button implementation. Moving it to the button used '
                                                'elsewhere and picking one Cancel variant for dialogs would '
                                                'likely resolve it.',
                               'acceptance': ['All Home dialogs render buttons with the same corner radius '
                                              'and height scale.',
                                              'Cancel uses the same variant, color and height in Customize '
                                              'demo, Create account and Provide feedback.'],
                               'verify': ['Check whether the Customize demo modal is demo or marketing '
                                          'chrome maintained outside the product design system.',
                                          'Look up the design-system dialog guidance for which button '
                                          'variant Cancel uses.'],
                               'searchTerms': ['two button components',
                                               'Cancel button style',
                                               'Customize demo modal buttons'],
                               'impact': 'consistency',
                               'confidence': 'high',
                               'confidenceReason': 'Button class names and computed styles captured with '
                                                   'DevTools in three dialogs.',
                               'demoArtifactRisk': True},
 'home.i-validation-copy': {'title': 'Use one voice for validation messages in Home forms',
                            'summary': "The Customize demo form says 'Please complete this field' and "
                                       "'Please submit a valid email' without periods; Create account says "
                                       "'You must make a selection.' with a period.",
                            'userImpact': "Minor. Each message is clear, but 'You must' reads sterner than "
                                          "the rest. The Industry placeholder 'Startup' also looks like a "
                                          "chosen value, so its 'Please complete this field' error is "
                                          'confusing.',
                            'fixHypothesis': "Messages seem to be written per form. Using the style guide's "
                                             'error message pattern, and a placeholder that cannot pass for '
                                             'a value, would likely resolve it.',
                            'acceptance': ['Home validation messages share one tone and one '
                                           'ending-punctuation rule.',
                                           "The Industry field's placeholder does not look like a selected "
                                           'value.'],
                            'verify': ["Check the content style guide for error message voice ('Please', "
                                       "'You must') and ending punctuation.",
                                       'Check whether the Customize demo form follows the product content '
                                       'guidelines or separate marketing ones.'],
                            'searchTerms': ['validation message copy',
                                            'You must make a selection',
                                            'Please complete this field'],
                            'impact': 'consistency',
                            'confidence': 'medium',
                            'confidenceReason': 'Messages quoted verbatim, but whether they break a rule '
                                                'depends on a style guide we cannot see.',
                            'demoArtifactRisk': True},
 'invoicing.i-action-copy': {'title': 'Use consistent wording in Invoicing dialogs, toasts and buttons',
                             'summary': "Dialogs dismiss with 'Close' or 'Go back'. Catalog toasts say 'Item "
                                        "added' then 'Product deleted'. Canceling an invoice says 'Item "
                                        "canceled'. Buttons mix 'Create & send email' and 'Create series and "
                                        "send'.",
                             'userImpact': "Mostly polish. 'Item canceled' after canceling an invoice is the "
                                           "one that can confuse, since 'item' elsewhere means a catalog "
                                           'item.',
                             'fixHypothesis': 'Strings seem to be written per dialog without a shared '
                                              'vocabulary. One dismiss label, one noun per object and one '
                                              'conjunction style would likely resolve it.',
                             'acceptance': ['All Invoicing confirmation dialogs use the same dismiss label.',
                                            'Catalog toasts use the same noun, and the cancel toast names '
                                            'the invoice.',
                                            'Send buttons use the same conjunction style.'],
                             'verify': ['Check the content style guide for the approved dismiss label in '
                                        "confirmation dialogs and whether '&' is allowed in buttons.",
                                        'Check whether success headings are meant to end with an exclamation '
                                        'mark.'],
                             'searchTerms': ['Close vs Go back',
                                             'Product deleted toast',
                                             'Item canceled invoice'],
                             'impact': 'consistency',
                             'confidence': 'medium',
                             'confidenceReason': 'Strings are quoted verbatim; which variants are allowed '
                                                 'depends on the style guide.',
                             'demoArtifactRisk': False},
 'invoicing.i-active-past-due': {'title': 'Show Overdue on past-due invoices currently labeled Active',
                                 'summary': "INV-0025 and INV-0018, both due Aug 31 ('24 days ago'), show "
                                            "'Active', which the Status filter defines as 'Unpaid and not "
                                            "overdue'. The Overdue card counts only 3 invoices.",
                                 'userImpact': 'Admins chasing late payments rely on the Overdue badge and '
                                               'card. Two past-due invoices worth $9,500 are missing from '
                                               'that view, so they may not get followed up.',
                                 'fixHypothesis': 'Both are monthly series invoices, so status may be '
                                                  'computed differently for series invoices than for '
                                                  'one-time ones. Deriving Overdue from the due date for '
                                                  'every invoice type would likely fix the badge and the '
                                                  'card count together.',
                                 'acceptance': ['An unpaid invoice whose due date has passed shows the '
                                                "'Overdue' badge, whatever its type.",
                                                'The Overdue summary card counts every invoice that shows an '
                                                'Overdue badge.',
                                                "Filtering Status by 'Active' returns no invoice with a past "
                                                'due date.'],
                                 'verify': ['Check whether series invoices have a grace period or a separate '
                                            'status rule that keeps them Active after the due date.',
                                            "Check whether the demo's frozen clock (Sep 24, 2026) or seeded "
                                            'statuses explain the mismatch rather than live status logic.'],
                                 'searchTerms': ['overdue invoice shows Active',
                                                 'recurring invoice status overdue',
                                                 'Overdue card count'],
                                 'impact': 'data-integrity',
                                 'confidence': 'medium',
                                 'confidenceReason': 'Row text and the filter definition are quoted '
                                                     'verbatim, but the statuses belong to seeded demo '
                                                     'invoices.',
                                 'demoArtifactRisk': True},
 'invoicing.i-back-button-variants': {'title': 'Use one Back button style across invoice and payment link '
                                               'flows',
                                      'summary': 'Back is a filled secondary pill on Invoice set up, Invoice '
                                                 'details and Series review, but a transparent tertiary '
                                                 'button on the one-time Review and send step and the '
                                                 'payment link step.',
                                      'userImpact': 'The Back button changes weight from one step to the '
                                                    'next, so the footer shifts as admins move through a '
                                                    'flow. No risk of error, but it looks unfinished.',
                                      'fixHypothesis': 'Two steps seem to use the tertiary variant where the '
                                                       'others use secondary. Moving Review and send and '
                                                       'Payment link details onto the secondary Back would '
                                                       'likely resolve it.',
                                      'acceptance': ['Every Back button in the Request money and Create '
                                                     'series flows uses the same variant and background.',
                                                     'Back on Review and send matches Back on Invoice '
                                                     'details.'],
                                      'verify': ['Check the design-system guidance on which button variant a '
                                                 "multi-step flow's Back action uses.",
                                                 'Check whether a tertiary Back on the final step is a '
                                                 'deliberate way to emphasize the send action.'],
                                      'searchTerms': ['Back button style',
                                                      'secondary tertiary Back',
                                                      'invoice flow footer'],
                                      'impact': 'consistency',
                                      'confidence': 'high',
                                      'confidenceReason': 'Class names, backgrounds and padding were '
                                                          'captured with DevTools on four steps.',
                                      'demoArtifactRisk': False},
 'invoicing.i-cancel-not-reflected': {'title': 'Keep canceled invoices listed and update Total open after '
                                               'cancel',
                                      'summary': 'After canceling INV-0014 ($12,000.00) from the bulk bar, '
                                                 'its row disappears even with Show canceled on, and Total '
                                                 "open still reads '$86.3K', 12 invoices, though its tooltip "
                                                 'excludes canceled items.',
                                      'userImpact': "The admin can't confirm the cancel worked or find the "
                                                    'canceled invoice, and Total open overstates receivables '
                                                    'by $12,000.',
                                      'fixHypothesis': 'The list and summary cards may not refetch after a '
                                                       'bulk cancel, and Show canceled may not include newly '
                                                       'canceled rows. The demo sandbox may also fake the '
                                                       'cancel.',
                                      'acceptance': ['With Show canceled on, a just-canceled invoice stays '
                                                     'listed with a Canceled status.',
                                                     'Total open drops by the canceled amount and its '
                                                     'invoice count decreases by one.',
                                                     'The list and totals look the same after a page '
                                                     'reload.'],
                                      'verify': ['Check whether the demo sandbox persists cancels at all.',
                                                 'Check whether Show canceled is meant to include invoices '
                                                 'canceled in the current session.'],
                                      'searchTerms': ['canceled invoice disappears',
                                                      'Total open not updated',
                                                      'Show canceled filter'],
                                      'impact': 'data-integrity',
                                      'confidence': 'medium',
                                      'confidenceReason': 'Before and after card text and URL were captured, '
                                                          'but the state bug was seen once in the sandbox.',
                                      'demoArtifactRisk': True},
 'invoicing.i-catalog-last-updated': {'title': 'Show the creation date as Last updated on new catalog items',
                                      'summary': "A catalog item created on Sep 24, 2026 shows 'Last updated "
                                                 "Sep 19, 2026' in its panel, the same date as every seeded "
                                                 'item.',
                                      'userImpact': "Admins can't use the date to find the items they just "
                                                    'added or changed.',
                                      'fixHypothesis': 'This seems most likely a demo artifact where the '
                                                       'sandbox stamps a fixed date. If production shows it '
                                                       'too, the create path may not set the updated '
                                                       'timestamp.',
                                      'acceptance': ['A newly created catalog item shows its creation date '
                                                     'as Last updated.',
                                                     'Editing an item changes Last updated to the edit '
                                                     'date.'],
                                      'verify': ['Check whether the demo sandbox stamps every catalog record '
                                                 'with a fixed date.'],
                                      'searchTerms': ['catalog last updated date',
                                                      'new catalog item wrong date',
                                                      'catalog timestamp'],
                                      'impact': 'data-integrity',
                                      'confidence': 'low',
                                      'confidenceReason': 'Panel text is quoted verbatim, but the date '
                                                          "matches the demo's seeded records.",
                                      'demoArtifactRisk': True},
 'invoicing.i-cents-rendering': {'title': 'Render currency cents the same way across Invoicing',
                                 'summary': 'Tables, totals and detail panels show cents as a smaller raised '
                                            'fraction (10.95px on 15px), while success screens, invoice '
                                            'previews, the catalog panel and review summaries print '
                                            "'$7,500.00' at one size.",
                                 'userImpact': 'Minor, but it makes the product read as assembled from '
                                               'different parts: the same amount looks different on the list '
                                               'and on the confirmation. Amounts stay readable.',
                                 'fixHypothesis': 'The list and panels likely use a money component that '
                                                  'styles the fraction, while other screens print a '
                                                  'preformatted string. Choosing one treatment and rendering '
                                                  'every amount through the same component would likely '
                                                  'resolve it.',
                                 'acceptance': ['Amounts on the success screen, review step and catalog '
                                                'panel use the same cents treatment as the invoice list.',
                                                'Every Invoicing amount uses the same markup for its '
                                                'fractional part.'],
                                 'verify': ['Check the design-system docs for when the reduced-size cents '
                                            'style applies, for example only to large display amounts.',
                                            'Check whether the invoice preview deliberately mirrors the '
                                            'plain PDF style.'],
                                 'searchTerms': ['fractional cents styling',
                                                 'currency amount component',
                                                 'smaller cents'],
                                 'impact': 'consistency',
                                 'confidence': 'medium',
                                 'confidenceReason': 'Font sizes and innerHTML were measured with DevTools, '
                                                     'but the split may be intentional by context.',
                                 'demoArtifactRisk': False},
 'invoicing.i-date-field-height': {'title': 'Use the standard date field size in Mark as paid',
                                   'summary': 'In Mark as paid, the Paid on date field uses the inline input '
                                              '(32px, 4px corners, class _inline_) directly above a 40px, '
                                              '8px-corner Payment method dropdown of the same 720px width.',
                                   'userImpact': 'Cosmetic. The two stacked fields look like they belong to '
                                                 'different forms.',
                                   'fixHypothesis': 'The date picker seems to default to its inline variant. '
                                                    'Using the standard field size in stacked forms would '
                                                    'likely resolve it.',
                                   'acceptance': ['Paid on and Payment method share height and corner '
                                                  'radius.',
                                                  'Date fields in stacked forms use the standard input '
                                                  'variant.'],
                                   'verify': ['Check the design-system date picker docs for when the inline '
                                              'variant applies.'],
                                   'searchTerms': ['date field height',
                                                   'inline date input',
                                                   'Mark as paid form'],
                                   'impact': 'cosmetic',
                                   'confidence': 'high',
                                   'confidenceReason': 'Classes, heights, radii and padding were captured '
                                                       'with DevTools.',
                                   'demoArtifactRisk': False},
 'invoicing.i-date-formats': {'title': 'Use one date format across Invoicing',
                              'summary': "Invoicing writes dates as 'Sep 24', 'Sep 24, 2026', '09/24/2026' "
                                         "(invoice preview only) and uppercase 'SEP 24 - CREATED BY', and "
                                         'the customer panel and table format the same date differently.',
                              'userImpact': 'Admins compare invoice, due and paid dates across the list, '
                                            'panels and the document their customer receives. Switching '
                                            'formats slows scanning, and numeric MM/DD/YYYY on the '
                                            'customer-facing invoice can be misread by customers outside the '
                                            'US.',
                              'fixHypothesis': 'Each surface seems to format dates locally. A shared '
                                               'formatter with a few named variants (short for tables, long '
                                               'for forms and documents) would likely remove most of the '
                                               'drift.',
                              'acceptance': ['Invoicing tables use one short date format, with the year when '
                                             'it is not the current year.',
                                             'The invoice preview Terms block uses the same date format as '
                                             'the rest of Invoicing, or one the style guide names for '
                                             'documents.',
                                             'Activity timeline dates use the same letter case as other '
                                             'dates.'],
                              'verify': ['Look up the date standard in the content style guide and note '
                                         'which observed formats it allows.',
                                         'Check whether the invoice PDF deliberately uses numeric dates for '
                                         'customer or regional reasons.'],
                              'searchTerms': ['invoice date format',
                                              'MM/DD/YYYY invoice preview',
                                              'inconsistent dates Invoicing'],
                              'impact': 'consistency',
                              'confidence': 'high',
                              'confidenceReason': 'Every format is quoted verbatim from rendered text on '
                                                  'named screens.',
                              'demoArtifactRisk': False},
 'invoicing.i-detail-panel-actions': {'title': 'Match Edit and Delete styles in customer and catalog panels',
                                      'summary': 'The customer panel shows Edit and Request as neutral pills '
                                                 'with icons and Delete as a red-tinted pill. The catalog '
                                                 'panel has a 147px primary blue Edit and a neutral Delete, '
                                                 'without icons.',
                                      'userImpact': 'Delete is red in one panel and neutral in the other, so '
                                                    'the warning cue for a destructive action is unreliable. '
                                                    'Edit also changes weight between sibling panels.',
                                      'fixHypothesis': 'The two panels seem to be built separately. A shared '
                                                       'panel action row, with one rule for destructive '
                                                       'actions, would likely resolve it.',
                                      'acceptance': ['Delete in the customer and catalog panels uses the '
                                                     'same variant and color.',
                                                     'Edit in both panels uses the same variant, size and '
                                                     'icon treatment.'],
                                      'verify': ['Check the design-system docs for how destructive actions '
                                                 'are styled in side panels.',
                                                 'Check whether a primary Edit in the catalog panel is '
                                                 "intended because editing is that panel's main task."],
                                      'searchTerms': ['detail panel actions',
                                                      'Delete button red',
                                                      'catalog panel Edit button'],
                                      'impact': 'consistency',
                                      'confidence': 'high',
                                      'confidenceReason': 'Backgrounds, classes, widths and icon presence '
                                                          'were captured with DevTools in both panels.',
                                      'demoArtifactRisk': False},
 'invoicing.i-due-date-blank': {'title': 'Show the default due date in the Invoice details field',
                                'summary': 'On Invoice details the Due date combobox value is empty, but the '
                                           "live preview already prints 'Due date 10/24/2026', the Net 30 "
                                           'date.',
                                'userImpact': "The admin can't tell whether a due date is set. They may send "
                                              'assuming Net 30 without picking it, or pick another term '
                                              'believing none was chosen.',
                                'fixHypothesis': 'The preview seems to apply a Net 30 default that the field '
                                                 "doesn't reflect. Prefilling the field with that default, "
                                                 'or leaving the preview blank until a term is picked, would '
                                                 'likely resolve it.',
                                'acceptance': ['The Due date field and the preview always show the same due '
                                               'date.',
                                               'If a default term applies, the field shows it on load.'],
                                'verify': ['Check whether Net 30 is the intended default and what due date '
                                           'is saved if the user never opens the field.',
                                           'Check Invoice settings Defaults for a default-terms setting that '
                                           'explains it.'],
                                'searchTerms': ['due date field empty',
                                                'invoice preview due date',
                                                'Net 30 default'],
                                'impact': 'data-integrity',
                                'confidence': 'high',
                                'confidenceReason': 'The empty combobox value was read from the DOM '
                                                    'alongside the preview text.',
                                'demoArtifactRisk': False},
 'invoicing.i-duplicate-ids': {'title': 'Stop reusing invoice numbers and payment-link URLs',
                               'summary': 'In one session, INV-34 went to an Orbital one-time invoice and to '
                                          'queued invoices in two series, and a Vega payment link reused the '
                                          "Orbital invoice's /pay/ylk7fnl18bpkfcg9 URL.",
                               'userImpact': 'Duplicate invoice numbers make payments hard to reconcile and '
                                             'can confuse accounting sync. A reused payment link could send '
                                             "one customer to another customer's request and record money "
                                             'against the wrong invoice.',
                               'fixHypothesis': 'Numbers and link slugs may be allocated per flow or on the '
                                                'client rather than reserved centrally. The demo sandbox may '
                                                'also fake allocation, in which case production is '
                                                'unaffected.',
                               'acceptance': ['Every new invoice, in any flow, gets an invoice number no '
                                              'other invoice in the org uses.',
                                              'Every new invoice and payment link gets its own /pay/ URL.',
                                              'Opening the pay URLs of two different requests shows each '
                                              "request's own customer and amount."],
                               'verify': ['Confirm whether the demo sandbox mocks invoice number and '
                                          'pay-link slug allocation.',
                                          'Check whether queued series invoices get their final number only '
                                          "when sent, which would make a queued 'INV-34' a placeholder."],
                               'searchTerms': ['duplicate invoice number',
                                               'payment link URL reused',
                                               'invoice number collision'],
                               'impact': 'data-integrity',
                               'confidence': 'medium',
                               'confidenceReason': 'Exact numbers and URLs were captured, but the demo '
                                                   'sandbox may fake ID allocation.',
                               'demoArtifactRisk': True},
 'invoicing.i-email-preview-copy': {'title': 'Hide the empty due date in the invoice email preview',
                                    'summary': "Before a due date is set, the email preview reads 'Please "
                                               "review and pay your invoice by' and 'has sent you an invoice "
                                               "#INV-34 due on for $8,165.63.'",
                                    'userImpact': 'Admins previewing the email see broken sentences and may '
                                                  'worry the sent email will read the same way.',
                                    'fixHypothesis': 'The template seems to insert the due date without a '
                                                     'fallback. Omitting the clause or showing a placeholder '
                                                     'when no date exists would likely resolve it.',
                                    'acceptance': ["With no due date, the email preview has no dangling 'by' "
                                                   "or 'due on'.",
                                                   'Once a due date is set, the preview shows it in both the '
                                                   'preview text and the body.'],
                                    'verify': ['Check whether a sent invoice email can ever lack a due date, '
                                               'which would make this a bug in real mail.'],
                                    'searchTerms': ['email preview due date empty',
                                                    'invoice email template',
                                                    'due on for'],
                                    'impact': 'cosmetic',
                                    'confidence': 'high',
                                    'confidenceReason': 'Both broken strings are quoted verbatim from the '
                                                        'preview.',
                                    'demoArtifactRisk': False},
 'invoicing.i-empty-value-glyph': {'title': 'Use one placeholder for empty values in Invoicing',
                                   'summary': 'Empty values show an em dash (U+2014) in the invoice list, a '
                                              'hyphen (U+002D) in Customers and the Catalog panel, and the '
                                              "words 'No payment history' in the customer panel.",
                                   'userImpact': 'Cosmetic. A lone hyphen can look like a minus sign or a '
                                                 'typo, but admins still understand the value is missing.',
                                   'fixHypothesis': 'Each table seems to hard-code its own placeholder. One '
                                                    'shared empty-value character would likely resolve it.',
                                   'acceptance': ['Empty cells in the invoice list, Customers and Catalog '
                                                  'use the same character.',
                                                  "The Catalog panel 'Used' field uses the same placeholder "
                                                  'as the tables.'],
                                   'verify': ['Check the content style guide or design-system table docs for '
                                              'the approved empty-value placeholder.'],
                                   'searchTerms': ['empty cell placeholder',
                                                   'em dash hyphen empty value',
                                                   'Last paid you dash'],
                                   'impact': 'cosmetic',
                                   'confidence': 'high',
                                   'confidenceReason': 'Character codes were read from the DOM on three '
                                                       'screens.',
                                   'demoArtifactRisk': False},
 'invoicing.i-escape-discards-draft': {'title': 'Confirm before Escape discards an invoice or series draft',
                                       'summary': 'In the full-screen Request money and Create series flows, '
                                                  'one Escape exits to the list and discards all input '
                                                  'without confirmation, even when pressed to close an open '
                                                  'dropdown.',
                                       'userImpact': 'An admin who presses Escape to close the payment-terms '
                                                     'menu loses the customer, line items and terms they '
                                                     'entered and has to start over.',
                                       'fixHypothesis': 'The full-screen layout seems to bind Escape to exit '
                                                        'without checking for open menus or unsaved changes. '
                                                        'Letting open menus handle Escape first, and asking '
                                                        'before discarding a filled form, would likely '
                                                        'resolve it.',
                                       'acceptance': ['With a dropdown open, Escape closes only the dropdown '
                                                      'and the flow stays on the current step.',
                                                      'With unsaved input, Escape shows a discard '
                                                      'confirmation.',
                                                      'With no input entered, Escape exits the flow without '
                                                      'a prompt.'],
                                       'verify': ['Check the design-system full-screen modal docs for the '
                                                  'approved Escape behavior and unsaved-changes handling.',
                                                  'Check whether the series detail page closing on Escape is '
                                                  'intended, since it holds no draft.'],
                                       'searchTerms': ['Escape discards draft',
                                                       'full screen flow Escape',
                                                       'unsaved changes invoice'],
                                       'impact': 'broken-task',
                                       'confidence': 'high',
                                       'confidenceReason': 'Reproduced on three pages with the resulting '
                                                           'URLs recorded.',
                                       'demoArtifactRisk': False},
 'invoicing.i-fake-disabled-next': {'title': 'Stop dimming the enabled Next button in Request money',
                                    'summary': 'Next in Request money renders at opacity 0.4 before a '
                                               'customer is chosen but is not disabled, and clicking it runs '
                                               'validation. Add Item and Add customer use truly disabled '
                                               'buttons.',
                                    'userImpact': 'Sighted admins read the dim Next as unavailable, while '
                                                  'screen reader users hear an enabled button. The same look '
                                                  'means two different behaviors across Invoicing forms.',
                                    'fixHypothesis': 'The flow seems to style the button as disabled while '
                                                     'keeping it clickable to trigger validation. Picking '
                                                     'one rule for incomplete forms, either full-opacity '
                                                     'with validation on click or a real disabled state, '
                                                     'would likely resolve it.',
                                    'acceptance': ['Next in Request money renders at full opacity whenever '
                                                   'it is clickable.',
                                                   'Every primary button styled as disabled has the disabled '
                                                   "attribute or aria-disabled='true'.",
                                                   'Add Item, Add customer and Request money follow the same '
                                                   'rule for incomplete forms.'],
                                    'verify': ['Check the design-system button docs for the approved pattern '
                                               'for incomplete forms: disabled, or enabled with validation '
                                               'on click.',
                                               "Check whether the 40% opacity is a documented 'soft "
                                               "disabled' state."],
                                    'searchTerms': ['Next button opacity',
                                                    'fake disabled button',
                                                    'aria-disabled primary button'],
                                    'impact': 'accessibility',
                                    'confidence': 'high',
                                    'confidenceReason': 'Opacity, disabled attribute and cursor were read '
                                                        'with DevTools and compared with two other forms.',
                                    'demoArtifactRisk': False},
 'invoicing.i-filter-labels': {'title': 'Match Invoicing filter labels to table column names',
                               'summary': "Filter facets read 'Due Date', 'Invoice Number' and 'Invoice "
                                          "Date' while columns read 'Due date', 'Invoice no.' and 'Invoice "
                                          "date'. The Type menu offers 'Recurring invoice' where rows say "
                                          "'Monthly' or 'Quarterly'.",
                               'userImpact': 'Admins have to translate between a filter and the column it '
                                             'narrows. It is a small speed bump, most noticeable for Type, '
                                             'where one option covers rows labeled by frequency.',
                               'fixHypothesis': 'Filters and columns seem to hold separate label strings. '
                                                'Deriving filter labels from the column definitions would '
                                                'likely resolve it.',
                               'acceptance': ['Each filter facet name matches its column header in wording '
                                              'and casing.',
                                              'Type filter options match the type values shown in the table, '
                                              'or the table shows type alongside frequency.'],
                               'verify': ['Check the content style guide for whether abbreviations like '
                                          "'Invoice no.' are allowed in column headers.",
                                          'Check whether the Type column is meant to show frequency instead '
                                          'of type.'],
                               'searchTerms': ['filter label column mismatch',
                                               'Invoice Number filter',
                                               'One time invoice filter'],
                               'impact': 'consistency',
                               'confidence': 'high',
                               'confidenceReason': 'Facet names and column headers are quoted verbatim from '
                                                   'the same page.',
                               'demoArtifactRisk': False},
 'invoicing.i-filter-popover-height': {'title': 'Size the Type and Status filter popovers to their content',
                                       'summary': 'The Type filter popover is a fixed 400px tall for 144px '
                                                  'of content (three checkboxes), leaving about 250px of '
                                                  'empty panel over the table. The Status popover also has '
                                                  'trailing empty space.',
                                       'userImpact': 'Cosmetic, though the empty panel covers table rows the '
                                                     'admin may want to see while filtering.',
                                       'fixHypothesis': 'The menu seems to have a fixed height meant for '
                                                        'longer lists. Sizing it to content up to a maximum '
                                                        'height would likely resolve it.',
                                       'acceptance': ['The Type popover height equals its content plus '
                                                      'standard padding.',
                                                      'The Status popover has no empty area below its last '
                                                      'option.'],
                                       'verify': ['Check the design-system menu or popover docs for a fixed '
                                                  'or maximum height.'],
                                       'searchTerms': ['filter popover height',
                                                       'dropdown empty space',
                                                       'Type filter menu'],
                                       'impact': 'cosmetic',
                                       'confidence': 'high',
                                       'confidenceReason': 'Menu and content heights were measured with '
                                                           'DevTools.',
                                       'demoArtifactRisk': False},
 'invoicing.i-frequency-controls': {'title': 'Use standard form controls on the series Frequency step',
                                    'summary': 'The Frequency step uses inline dropdowns at 13.33px with a '
                                               "solid 'fa-caret-down' icon and a 32px date field with 4px "
                                               'corners. Other forms use 40px fields with 8px corners and '
                                               'outline chevrons.',
                                    'userImpact': 'The step looks unlike every other form in the flow, and '
                                                  'its controls are smaller targets. It slows admins down '
                                                  'more than it confuses them.',
                                    'fixHypothesis': 'The sentence-style step seems to use a custom or older '
                                                     'inline select. Swapping in the standard select and '
                                                     'date input, or a documented inline variant if one '
                                                     'exists, would likely resolve it.',
                                    'acceptance': ["The Frequency step's date field has the same height and "
                                                   'corner radius as other date fields in the flow.',
                                                   'Frequency and terms dropdowns use the same chevron icon '
                                                   'as other selects.'],
                                    'verify': ['Check the design-system docs for an approved inline or '
                                               'sentence-style select and its specs.',
                                               'Check whether FontAwesome icons are still an allowed icon '
                                               'source.'],
                                    'searchTerms': ['inline dropdown sentence form',
                                                    'fa-caret-down',
                                                    'series frequency form'],
                                    'impact': 'consistency',
                                    'confidence': 'high',
                                    'confidenceReason': "Heights, radii, font sizes and the icon's "
                                                        'data-prefix were captured with DevTools.',
                                    'demoArtifactRisk': False},
 'invoicing.i-invoice-not-found-toast': {'title': "Stop the 'Invoice not found' toast after creating an "
                                                  'invoice',
                                         'summary': 'Return to dashboard opens /invoicing?id=<new invoice> '
                                                    "with an 'Invoice not found' error toast over the "
                                                    "loading skeleton; 2.5s later the same invoice's panel "
                                                    'opens normally.',
                                         'userImpact': 'Right after sending an invoice, the admin is told it '
                                                       "doesn't exist and may send it again or contact "
                                                       'support.',
                                         'fixHypothesis': 'The panel seems to look up the id before the list '
                                                          'has loaded and report the miss. Waiting for the '
                                                          'list, or fetching the invoice by id, would likely '
                                                          'resolve it.',
                                         'acceptance': ['Returning from the success screen shows no error '
                                                        'toast.',
                                                        "The new invoice's panel opens once the list has "
                                                        'loaded.'],
                                         'verify': ['Check whether the delay comes from demo sandbox latency '
                                                    'or from read-after-write lag that production also has.'],
                                         'searchTerms': ['Invoice not found toast',
                                                         'Return to dashboard error',
                                                         'detail panel id loading'],
                                         'impact': 'broken-task',
                                         'confidence': 'medium',
                                         'confidenceReason': 'URL, toast text and timing were recorded, but '
                                                             'the race was seen once.',
                                         'demoArtifactRisk': True},
 'invoicing.i-invoice-number-format': {'title': 'Use one invoice number format across invoices and series',
                                       'summary': "Seeded invoices read 'INV-0033', new ones get 'INV-34', a "
                                                  "new series previews 'INV-1', and editing a series "
                                                  "switches the prefix to 'S-0003' (next invoice "
                                                  "'S-0003-4').",
                                       'userImpact': 'Admins and their customers use invoice numbers to '
                                                     'reconcile payments. Mixed padding breaks sort order '
                                                     "('INV-34' queued above 'INV-0025'), and a prefix that "
                                                     "changes on edit makes one series' invoices look "
                                                     'unrelated.',
                                       'fixHypothesis': 'Numbers seem to be generated in several places with '
                                                        'different padding and prefix rules. One shared '
                                                        'number format, and an edit form that keeps the '
                                                        'saved prefix, would likely resolve it.',
                                       'acceptance': ['New one-time and series invoices use the same '
                                                      'zero-padding as existing invoices.',
                                                      'Edit recurring series prefills the saved invoice '
                                                      'prefix, not the series ID.',
                                                      'Sorting the invoice list by number puts invoices in '
                                                      'numeric order.'],
                                       'verify': ['Check whether the zero-padded numbers exist only in '
                                                  'seeded demo data and production numbering is unpadded.',
                                                  "Check whether a per-series prefix such as 'S-0003' is an "
                                                  'intended default when editing a series.'],
                                       'searchTerms': ['invoice number padding',
                                                       'invoice prefix series ID',
                                                       'invoice number sort order'],
                                       'impact': 'consistency',
                                       'confidence': 'medium',
                                       'confidenceReason': 'Input values and table cells are quoted '
                                                           'verbatim, but the padded numbers come from '
                                                           'seeded demo invoices.',
                                       'demoArtifactRisk': True},
 'invoicing.i-payment-method-names': {'title': 'Use one name for each payment method in Invoicing',
                                      'summary': "Mercury's method appears as 'Pay with Mercury', 'Pay With "
                                                 "Mercury' and 'Pay by Mercury'; manual transfer as 'Manual "
                                                 "Transfer (ACH/Wire)', 'Manual Transfer' and 'Manual "
                                                 "transfer'; wires as 'International wires' and "
                                                 "'International Wire'.",
                                      'userImpact': 'Payers see these names on the pay page and the invoice '
                                                    'PDF. When the builder, pay page and PDF disagree, '
                                                    "admins can't be sure which methods they enabled, and "
                                                    'customers may think they are different options.',
                                      'fixHypothesis': 'Each surface seems to hold its own copy of method '
                                                       'names. One shared set of method labels for the '
                                                       'builder, payer page and invoice document would '
                                                       'likely resolve it.',
                                      'acceptance': ['Each payment method has one name, with the same '
                                                     'casing, on Invoice details, the payer page preview and '
                                                     'the invoice preview.',
                                                     "The invoice preview 'Pay via' line uses the same names "
                                                     'as the Methods summary.'],
                                      'verify': ['Check the content style guide or product glossary for the '
                                                 "official names of 'Pay with Mercury' and manual ACH/wire "
                                                 'transfer.',
                                                 'Check whether the invoice PDF deliberately uses shorter '
                                                 'names to save space.'],
                                      'searchTerms': ['Pay with Mercury naming',
                                                      'payment method labels invoice',
                                                      'Manual Transfer ACH/Wire'],
                                      'impact': 'consistency',
                                      'confidence': 'high',
                                      'confidenceReason': 'All variants are quoted verbatim from rendered '
                                                          'text, several on the same screen.',
                                      'demoArtifactRisk': False},
 'invoicing.i-sender-name': {'title': 'Show admins the sender name customers see on invoices',
                             'summary': "The sidebar and Add customer header say 'Mercury Demo', but "
                                        "invoices, emails and the payer page come from 'Nebula Consulting "
                                        "Group', a name close to the customer 'Nebula Strategy Group'.",
                             'userImpact': "Admins can't tell at a glance which name their customers will "
                                           "see. A sender name close to a customer's name also makes invoice "
                                           'review more error-prone.',
                             'fixHypothesis': 'The invoice header may deliberately use a separate company '
                                              'name set in Invoice settings. Showing that name where admins '
                                              'build invoices would likely help; the near-match with a '
                                              'customer name is probably seeded data.',
                             'acceptance': ['The invoice builder shows the sender name customers will see.',
                                            'The org name and the invoice header company name agree, or the '
                                            'UI says where the invoice name is set.'],
                             'verify': ['Check whether the invoice header company name is meant to be '
                                        'separate from the org name, for example as a DBA.',
                                        "Confirm whether 'Mercury Demo' and 'Nebula Consulting Group' are "
                                        'both seeded demo values.'],
                             'searchTerms': ['invoice sender name',
                                             'company name invoice header',
                                             'DBA on invoices'],
                             'impact': 'consistency',
                             'confidence': 'low',
                             'confidenceReason': 'Names are quoted verbatim, but both are likely demo '
                                                 'personalization.',
                             'demoArtifactRisk': True},
 'invoicing.i-series-data-mismatch': {'title': 'Show the same next date and amount on series list and detail',
                                      'summary': "Meridian's series shows 'Next on Oct 1' in the list and "
                                                 "'Next on Oct 24, 2026' in detail. Solstice lists "
                                                 "$1,200.00; its invoices are $2,000.00. Astral's success "
                                                 "screen claims an email its activity doesn't show.",
                                      'userImpact': 'Admins use the series list to forecast incoming cash. '
                                                    'When list and detail disagree on the next date and '
                                                    "amount, they can't tell which figure to trust.",
                                      'fixHypothesis': 'The list and detail may read different fields, such '
                                                       'as the template versus the next scheduled invoice, '
                                                       'or the seeded series may be inconsistent. Deriving '
                                                       'both views from the same next-invoice record would '
                                                       'likely fix the live part.',
                                      'acceptance': ['A series shows the same next date and amount on '
                                                     'Recurring Series and its detail page.',
                                                     "A series' listed amount matches its invoices, or the "
                                                     'page shows when the amount changed.',
                                                     "After 'We've emailed the first invoice', the series "
                                                     'activity shows a sent entry.'],
                                      'verify': ['Check whether Meridian and Solstice are seeded demo '
                                                 'records with inconsistent data.',
                                                 'Check whether an edited series amount applies only to '
                                                 'future invoices, which would explain Solstice.'],
                                      'searchTerms': ['series next date mismatch',
                                                      'recurring series amount',
                                                      'series activity email sent'],
                                      'impact': 'data-integrity',
                                      'confidence': 'medium',
                                      'confidenceReason': 'Values are quoted verbatim, but most of the '
                                                          'affected series are seeded demo records.',
                                      'demoArtifactRisk': True},
 'invoicing.i-series-stat-typography': {'title': 'Match the series Amount value size to its sibling stats',
                                        'summary': 'On the series detail header, the Amount value is 15px '
                                                   'with a 24px line height, while Series ID and Primary '
                                                   'email values are 17px with 28px.',
                                        'userImpact': 'Cosmetic. The amount looks smaller than the less '
                                                      'important values beside it.',
                                        'fixHypothesis': 'The Amount value may use a money component with '
                                                         'its own type size. Applying the stat value style '
                                                         'to it would likely resolve it.',
                                        'acceptance': ['Amount, Series ID and Primary email values share '
                                                       'font size and line height.',
                                                       'The three values align on the same baseline.'],
                                        'verify': ['Check the design-system type scale for the key-value or '
                                                   'stat pattern.'],
                                        'searchTerms': ['series detail header font size',
                                                        'stat row typography',
                                                        'Amount value size'],
                                        'impact': 'cosmetic',
                                        'confidence': 'high',
                                        'confidenceReason': 'Computed font sizes and line heights were '
                                                            'captured with DevTools.',
                                        'demoArtifactRisk': False},
 'invoicing.i-settings-close-overlap': {'title': 'Stop the Invoice settings close button overlapping the '
                                                 'preview',
                                        'summary': 'In Invoice settings, the close X (x 1360–1386, y '
                                                   "102–128) sits on the invoice preview card's top-right "
                                                   'corner (card x 774–1386, top 110).',
                                        'userImpact': 'Cosmetic. The X covers a corner of the preview and '
                                                      'looks misplaced.',
                                        'fixHypothesis': 'The preview column seems to ignore the space '
                                                         'reserved for the page close button. Offsetting the '
                                                         'preview column would likely resolve it.',
                                        'acceptance': ['The close button and the preview card do not overlap '
                                                       'on the General and Branding tabs.',
                                                       'The close button stays clear of the preview at '
                                                       'narrower viewports.'],
                                        'verify': ['Check the design-system full-screen page docs for the '
                                                   'area reserved around the close button.'],
                                        'searchTerms': ['close button overlap preview',
                                                        'Invoice settings close X',
                                                        'settings preview card'],
                                        'impact': 'cosmetic',
                                        'confidence': 'high',
                                        'confidenceReason': 'Element rects were measured with DevTools.',
                                        'demoArtifactRisk': False},
 'invoicing.i-status-badge-colors': {'title': 'Give Active and Processing invoice badges distinct colors',
                                     'summary': "Active and Processing share one 'pearl' badge with "
                                                'identical colors. Overdue and Paid use tinted text while '
                                                'Active, Processing and Scheduled use neutral text. Series '
                                                "say 'Completed', the list says 'Series complete'.",
                                     'userImpact': "Scanning the list, an admin can't tell an invoice "
                                                   'waiting for payment from one whose payment is in flight '
                                                   'without reading each label. The series label mismatch '
                                                   'makes the two pages seem to describe different states.',
                                     'fixHypothesis': 'The status-to-color mapping seems to assign one tone '
                                                      'to two states and mix two text treatments. One status '
                                                      'map shared by invoices and series, with a tone per '
                                                      'state, would likely resolve it.',
                                     'acceptance': ['Active and Processing badges have different background '
                                                    'colors.',
                                                    'All invoice status badges use the same text-color '
                                                    'treatment.',
                                                    'A finished series shows the same label on Recurring '
                                                    'Series and in the invoice list.'],
                                     'verify': ['Check the design-system status badge docs for the approved '
                                                'tone for in-progress states.',
                                                "Check whether 'Completed' (series status) and 'Series "
                                                "complete' (invoice sub-label) are meant to differ."],
                                     'searchTerms': ['invoice status badge colors',
                                                     'Active Processing same color',
                                                     'Series complete Completed'],
                                     'impact': 'consistency',
                                     'confidence': 'high',
                                     'confidenceReason': 'Background and text colors and class names were '
                                                         'read with DevTools for five statuses.',
                                     'demoArtifactRisk': False},
 'invoicing.i-table-overhang': {'title': 'Align Customers and Catalog tables with the page header',
                                'summary': 'The Customers and Catalog tables end at x=1332px, 18px past the '
                                           "right edge of the 'Add customer' and 'Add item' buttons "
                                           '(1314px). The invoice list table ends at 1314px.',
                                'userImpact': 'Cosmetic. The right edge of two Invoicing tabs looks slightly '
                                              'off.',
                                'fixHypothesis': 'These two tables may sit in a container with different '
                                                 'padding or margin from the invoice list. Using the invoice '
                                                 "list's container would likely resolve it.",
                                'acceptance': ["On Customers and Catalog, the table's right edge matches the "
                                               "header action button's right edge.",
                                               'The three Invoicing tables share one right edge at the same '
                                               'viewport width.'],
                                'verify': ['Check the design-system page layout docs for whether tables may '
                                           'extend past the content gutter.'],
                                'searchTerms': ['table overhang header',
                                                'Customers table alignment',
                                                'table right margin'],
                                'impact': 'cosmetic',
                                'confidence': 'high',
                                'confidenceReason': 'Bounding rects were measured with DevTools on three '
                                                    'pages.',
                                'demoArtifactRisk': False},
 'invoicing.i-title-case': {'title': 'Use sentence case for Invoicing headings and buttons',
                            'summary': "Most Invoicing labels are sentence case, but 'Edit Invoice', 'Add "
                                       "Item', 'Yes, Send Now', 'Cancel Invoice', 'Set Up', 'Manage Logo', "
                                       "'View All', 'Download Invoice' and 'Payment Method' use Title Case.",
                            'userImpact': 'A polish issue that blocks nothing. The most visible case is the '
                                          "'Add item' button opening a page titled 'Add Item'.",
                            'fixHypothesis': 'These strings seem to predate or bypass a sentence-case '
                                             'convention. Updating the listed strings would likely resolve '
                                             'it once the style guide confirms the rule.',
                            'acceptance': ['The listed headings, buttons and column headers render in '
                                           'sentence case.',
                                           "The 'Add item' button and the page it opens use the same casing.",
                                           "'Edit Invoice' matches the casing of 'Edit recurring series'."],
                            'verify': ['Confirm in the content style guide that sentence case applies to '
                                       'headings, buttons and column headers.',
                                       "Check whether payer-facing strings such as 'Download Invoice' follow "
                                       'separate rules for hosted pages.'],
                            'searchTerms': ['Title Case buttons Invoicing',
                                            'sentence case headings',
                                            'Add Item casing'],
                            'impact': 'consistency',
                            'confidence': 'medium',
                            'confidenceReason': 'Strings are quoted verbatim; whether each breaks a rule '
                                                'depends on the style guide.',
                            'demoArtifactRisk': False},
 'invoicing.i-toggle-placement': {'title': 'Place Invoice settings switches on one side of their labels',
                                  'summary': 'On the Invoice settings General tab, the Address and Phone '
                                             'number switches sit at x=560, right of their labels, while '
                                             'Configurations and Defaults switches sit at x=120, left of '
                                             'theirs.',
                                  'userImpact': 'Cosmetic. Admins scanning the page look in two places for '
                                                'the switch.',
                                  'fixHypothesis': 'The header fields seem to use a different row layout '
                                                   'from the toggle lists. Using the same switch row for '
                                                   'both would likely resolve it.',
                                  'acceptance': ['All switches on the General and Defaults tabs sit on the '
                                                 'same side of their labels.',
                                                 'Switches in one column share the same left position.'],
                                  'verify': ['Check the design-system switch docs for label position rules '
                                             'and whether field-row toggles are a separate pattern.'],
                                  'searchTerms': ['switch label position',
                                                  'Invoice settings toggles',
                                                  'toggle alignment'],
                                  'impact': 'cosmetic',
                                  'confidence': 'high',
                                  'confidenceReason': 'Switch positions were measured from role=switch '
                                                      'elements with DevTools.',
                                  'demoArtifactRisk': False},
 'invoicing.i-transaction-match-empty': {'title': 'Give Transaction match an empty state that clears the '
                                                  'buttons',
                                         'summary': 'The Transaction match dropdown in Mark as paid shows '
                                                    "column headers and a bare 'No results' row with no "
                                                    "guidance, and the list overlaps the 'Close' and 'Mark "
                                                    "as paid' buttons.",
                                         'userImpact': 'The admin gets no hint why nothing matches or what '
                                                       'to do instead, such as marking it paid externally, '
                                                       'and the list covers the buttons they need next.',
                                         'fixHypothesis': 'The dropdown seems to lack an empty-state message '
                                                          'and to extend past the dialog body. An empty '
                                                          "state that points to 'Externally', and a list "
                                                          'that fits inside the dialog or layers cleanly '
                                                          'above it, would likely resolve it.',
                                         'acceptance': ['With no matches, the dropdown says why and suggests '
                                                        'marking the invoice paid externally.',
                                                        'The open dropdown does not cover the Close or Mark '
                                                        'as paid buttons.'],
                                         'verify': ['Check the design-system empty-state guidance for '
                                                    'combobox and table dropdowns.',
                                                    'Check whether the empty list comes from demo data '
                                                    'having no unmatched deposits.'],
                                         'searchTerms': ['Transaction match No results',
                                                         'Mark as paid dropdown overlap',
                                                         'combobox empty state'],
                                         'impact': 'broken-task',
                                         'confidence': 'low',
                                         'confidenceReason': 'The overlap was seen only in a screenshot, and '
                                                             'the empty result may come from demo data.',
                                         'demoArtifactRisk': True},
 'invoicing.i-unit-price-format': {'title': 'Format unit prices the same way in invoice line items',
                                   'summary': "In the line-item table a catalog price shows '5,000.00' while "
                                              "a typed price in the next row shows '1,250'. Edit Invoice "
                                              "prefills catalog rows as '12,000' and '4,500'.",
                                   'userImpact': 'Small. Two rows of the same table disagree on precision, '
                                                 "and an admin may stop to check whether '1,250' was saved "
                                                 'correctly.',
                                   'fixHypothesis': 'The input may apply two-decimal formatting only when a '
                                                    'value comes from the catalog picker. Formatting every '
                                                    'unit price the same way on blur and on prefill would '
                                                    'likely resolve it.',
                                   'acceptance': ['Every unit price input in Invoice set up and Edit Invoice '
                                                  'shows the same number of decimals after blur.',
                                                  'Catalog-picked, typed and prefilled prices display '
                                                  'identically for the same amount.'],
                                   'verify': ['Check the design-system currency input guidance for whether '
                                              "inputs show trailing '.00'."],
                                   'searchTerms': ['unit price decimals',
                                                   'line item price format',
                                                   'currency input formatting'],
                                   'impact': 'consistency',
                                   'confidence': 'high',
                                   'confidenceReason': 'Input values were read from the DOM on two screens.',
                                   'demoArtifactRisk': False},
 'invoicing.i-unlabeled-controls': {'title': 'Label Customers icon buttons and Branding color swatches',
                                    'summary': 'Per-row edit and request buttons in Customers have no text, '
                                               'aria-label or title. Branding color radios are named '
                                               "'white-theme', 'green-theme' and so on, and 'white-theme' is "
                                               'dark grey.',
                                    'userImpact': 'Screen reader users hear unnamed buttons on every '
                                                  "customer row and can't tell edit from request. A swatch "
                                                  "named 'white-theme' that is dark grey misleads anyone "
                                                  'relying on the name.',
                                    'fixHypothesis': 'The icon buttons seem to lack an aria-label, and the '
                                                     'swatches seem to expose internal ids as names. Adding '
                                                     "names such as 'Edit Lumen Robotics' and readable color "
                                                     'names would likely resolve it.',
                                    'acceptance': ['Each Customers row button has an accessible name with '
                                                   'the action and the customer name.',
                                                   'Each Branding swatch has an accessible name that '
                                                   'describes its visible color.'],
                                    'verify': ['Check the design-system icon button docs for the required '
                                               'accessible-name pattern.',
                                               "Check whether 'white-theme' refers to the invoice's text or "
                                               'background color rather than the swatch fill.'],
                                    'searchTerms': ['icon button aria-label Customers',
                                                    'branding swatch accessible name',
                                                    'unlabeled buttons'],
                                    'impact': 'accessibility',
                                    'confidence': 'high',
                                    'confidenceReason': 'aria-label, title and text content were read from '
                                                        'the DOM, and radio names from the accessibility '
                                                        'tree.',
                                    'demoArtifactRisk': False},
 'payments.i-back-button-variants': {'title': 'Use one Back button across Payments flows',
                                     'summary': "The step-back action appears five ways: 'Go back' without "
                                                "icon, 'Back' with chevron, 'Back' without icon, transparent "
                                                "tertiary 'Back', and blue-text 'Back'.",
                                     'userImpact': 'Admins move between Send, Transfer, Deposit and Request '
                                                   'often. When the same action changes label, icon and fill '
                                                   'each time, it takes a second look to find it.',
                                     'fixHypothesis': 'Each flow seems to build its own footer. A shared '
                                                      'flow footer with one Back variant would likely remove '
                                                      'the drift.',
                                     'acceptance': ['All Back actions in Payments flows use the same label '
                                                    'and icon.',
                                                    'All Back actions in Payments flows use the same fill '
                                                    'and text color.',
                                                    "No Payments flow uses 'Go back' for step navigation "
                                                    "while another uses 'Back', unless the style guide "
                                                    'defines both.'],
                                     'verify': ["Check the content style guide for whether 'Go back' and "
                                                "'Back' are both allowed, and for which purpose.",
                                                'Check the design-system docs for the specified Back button '
                                                'variant in full-screen flows.'],
                                     'searchTerms': ['Back button variants',
                                                     'Go back vs Back',
                                                     'flow footer buttons'],
                                     'impact': 'consistency',
                                     'confidence': 'high',
                                     'confidenceReason': 'Padding, icon count, class names and colors '
                                                         'measured with DevTools on four flows.',
                                     'demoArtifactRisk': False},
 'payments.i-broken-back': {'title': "Fix Back on deposit Payment Details and SAFE 'Go back' target",
                            'summary': 'On /add-funds/wire the Back button does not navigate, and on Create '
                                       "a new SAFE, 'Go back' lands on /capital/safe instead of the deposit "
                                       'options.',
                            'userImpact': 'Admins reviewing wire instructions press Back and nothing '
                                          'happens, so they have to find the stepper link. From SAFE they '
                                          'end up in Capital instead of where they came from.',
                            'fixHypothesis': 'The Payment Details Back seems to have no navigation target, '
                                             'and the SAFE flow seems to hard-code its Capital entry point. '
                                             'Sending both back to /add-funds when entered from Deposit '
                                             'would likely resolve it.',
                            'acceptance': ['Back on /add-funds/wire returns to the deposit options.',
                                           "'Go back' on Create a new SAFE returns to /add-funds when the "
                                           'flow was opened from Deposit.',
                                           "Opened from Capital, 'Go back' on the SAFE flow still returns to "
                                           '/capital/safe.'],
                            'verify': ["Confirm with the SAFE flow owner whether 'Go back' is meant to "
                                       'always return to Capital.',
                                       'Check whether Back on /add-funds/wire works when the page is reached '
                                       'by URL rather than from Deposit.'],
                            'searchTerms': ['Back button does nothing',
                                            'add-funds wire Back',
                                            'SAFE go back Capital'],
                            'impact': 'broken-task',
                            'confidence': 'high',
                            'confidenceReason': 'Clicked twice with the resulting URLs recorded for both '
                                                'buttons.',
                            'demoArtifactRisk': False},
 'payments.i-cents-rendering': {'title': 'Render cents one way in Payments amounts',
                                'summary': 'Tables show superscript cents, but the recipient panel, '
                                           'Checkbooks table, review and success heroes and the approvals '
                                           "bulk bar print cents inline; Checkbooks shows '$1,250.75' inline "
                                           'and superscript in its panel.',
                                'userImpact': 'The same amount looks different from table to panel, so '
                                              'admins cross-checking a payment see two visual formats for '
                                              'one number. Readability is fine; trust in the numbers takes a '
                                              'small hit.',
                                'fixHypothesis': 'Some surfaces seem to print a formatted string instead of '
                                                 'the money component that adds the superscript class. Using '
                                                 'the money component everywhere amounts appear would likely '
                                                 'resolve it.',
                                'acceptance': ['A given amount renders with the same cents style in a table '
                                               'and in its detail panel.',
                                               'Recipient panel payments-by-year rows use the same cents '
                                               'style as the Recipients table.',
                                               'Review and success hero amounts follow one documented cents '
                                               'style.'],
                                'verify': ['Check the design-system money or currency component docs for '
                                           'when superscript cents are required.',
                                           'Check whether inline cents in hero amounts on review and success '
                                           'screens are a deliberate display variant.'],
                                'searchTerms': ['superscript cents', 'money component', 'amount formatting'],
                                'impact': 'consistency',
                                'confidence': 'high',
                                'confidenceReason': 'DOM markup with and without the superscript class '
                                                    'captured on five surfaces.',
                                'demoArtifactRisk': False},
 'payments.i-check-deposit-buttons': {'title': 'Keep one button style through every check deposit step',
                                      'summary': 'Check deposit steps 1–2 use 32px buttons with arrow icons '
                                                 "and a ghost 'Go back'; step 3 switches to 40px buttons "
                                                 "with chevrons and a blue 'Back'.",
                                      'userImpact': 'The footer buttons change size, icon and color partway '
                                                    'through a four-step deposit, so the flow feels like two '
                                                    'products stitched together. Nothing breaks, but admins '
                                                    'have to re-find the controls.',
                                      'fixHypothesis': 'Steps 1–2 and step 3 carry different class prefixes '
                                                       '(_r6b2u vs _1rzdg), which suggests two button '
                                                       'implementations. Moving every step onto the button '
                                                       'used by Send and Transfer would likely resolve it.',
                                      'acceptance': ['All four check deposit steps render Back and primary '
                                                     'buttons at the same height.',
                                                     'Back uses the same label, icon and fill on every check '
                                                     'deposit step.',
                                                     "Check deposit footer buttons match the Send flow's "
                                                     'Back and Next buttons.'],
                                      'verify': ['Check the design-system button docs for which size and '
                                                 'variant full-screen flow footers are meant to use.',
                                                 'Confirm whether the _1rzdg button on the check images step '
                                                 'is a deprecated component.'],
                                      'searchTerms': ['check deposit buttons',
                                                      'button size mismatch',
                                                      'Back button check deposit'],
                                      'impact': 'consistency',
                                      'confidence': 'high',
                                      'confidenceReason': 'Class names, heights, padding and colors captured '
                                                          'with DevTools on three steps.',
                                      'demoArtifactRisk': False},
 'payments.i-date-formats': {'title': 'Use one date format across Payments',
                             'summary': 'Payments renders dates in at least seven formats, sometimes two on '
                                        "one panel, and mixes relative words ('Yesterday', 'Today') with "
                                        'absolute dates for the same field.',
                             'userImpact': 'Admins compare due, send-on and approval dates across tables and '
                                           'panels. When the same kind of date is written differently, and '
                                           'some drop the year, lists are slower to scan and it is easier to '
                                           'misread when money moves.',
                             'fixHypothesis': 'Each surface seems to format dates locally. Routing Payments '
                                              'dates through one shared formatter with a few named variants '
                                              '(short for tables, long for confirmations) would likely '
                                              'remove most of the drift.',
                             'acceptance': ['Payments tables show dates in one format, with the year when it '
                                            'is not the current year.',
                                            'A detail panel never mixes relative words and absolute dates '
                                            'for the same kind of field.',
                                            'Confirmation screens use one long date format, without ordinal '
                                            'suffixes unless the style guide allows them.',
                                            "The Taxes 'IRS Deadline' badge uses the same format as other "
                                            'Payments dates.'],
                             'verify': ["Look up Mercury's date standard in the content style guide and note "
                                        'which of the seven observed formats it allows.',
                                        "Check whether relative dates ('Yesterday', 'Today') are an approved "
                                        'pattern for recent activity.'],
                             'searchTerms': ['date format Payments',
                                             'inconsistent dates',
                                             'relative dates Yesterday'],
                             'impact': 'consistency',
                             'confidence': 'high',
                             'confidenceReason': 'Every format is quoted verbatim from rendered text on '
                                                 'named screens.',
                             'demoArtifactRisk': False},
 'payments.i-datepicker-offset': {'title': "Position the Send on calendar so it doesn't cover its field",
                                  'summary': 'The Send on calendar opens at left 416px and top 460px, 24px '
                                             'left of the form column and about 48px above the field it '
                                             'edits.',
                                  'userImpact': "Admins picking a send date can't see the field's current "
                                                'value while the calendar is open. Small, but it breaks the '
                                                'link between field and picker.',
                                  'fixHypothesis': 'The popover seems to be positioned with a fixed offset '
                                                   'rather than anchored to the input. Anchoring it below '
                                                   'the field would likely resolve it.',
                                  'acceptance': ['The Send on calendar opens below or above the field '
                                                 'without covering it.',
                                                 "The calendar's left edge aligns with the field's left "
                                                 'edge.'],
                                  'verify': ['Check the design-system date picker docs for its default '
                                             'placement.',
                                             'Check whether other date pickers in Payments (scheduled edit, '
                                             'bill details) share the offset.'],
                                  'searchTerms': ['date picker position',
                                                  'calendar covers input',
                                                  'Send on date picker'],
                                  'impact': 'cosmetic',
                                  'confidence': 'medium',
                                  'confidenceReason': 'Positions measured with DevTools, but the placement '
                                                      'may be intentional.',
                                  'demoArtifactRisk': False},
 'payments.i-decline-no-confirm': {'title': 'Confirm before declining a payment request',
                                   'summary': 'Decline in the approval panel removes the request at once '
                                              '(count 6 to 5) with a green-check toast, while Cancel payment '
                                              'opens a confirmation modal first.',
                                   'userImpact': 'An approver who misclicks Decline rejects a payment with '
                                                 'no chance to stop it, and the green check suggests success '
                                                 'rather than a rejection.',
                                   'fixHypothesis': 'Decline seems to call the action directly, without the '
                                                    'confirmation used for Cancel payment. Adding a '
                                                    'confirmation, or an undo in the toast, and a neutral '
                                                    'toast icon would likely resolve it.',
                                   'acceptance': ['Clicking Decline in the approval panel asks for '
                                                  'confirmation or offers undo before the request leaves the '
                                                  'list.',
                                                  "The 'Request declined' toast does not use the success "
                                                  'check icon.',
                                                  'Decline uses the same button style in the approval panel '
                                                  'and the batch reviewer.'],
                                   'verify': ['Check whether a declined request can be restored; if so, undo '
                                              'may be enough.',
                                              'Check the design-system guidance on confirming destructive '
                                              'actions and on toast icons.'],
                                   'searchTerms': ['decline no confirmation',
                                                   'Request declined toast',
                                                   'destructive action confirm'],
                                   'impact': 'broken-task',
                                   'confidence': 'high',
                                   'confidenceReason': 'Reproduced with the list count change and button '
                                                       'styles captured with DevTools.',
                                   'demoArtifactRisk': False},
 'payments.i-disabled-next-patterns': {'title': 'Use one rule for when Next is disabled in Payments flows',
                                       'summary': 'Send and Transfer keep Next enabled, Check deposit '
                                                  'hard-disables it, Request shows it at 40% opacity but '
                                                  'clickable, and Wire drawdown toggles it off again after '
                                                  'an error.',
                                       'userImpact': "Admins can't predict whether a greyed Next means "
                                                     "'blocked' or 'click to see errors'. On Request, a Next "
                                                     'that looks disabled but submits may confuse screen '
                                                     'reader and sighted users alike.',
                                       'fixHypothesis': 'Each flow seems to choose its own validation '
                                                        'pattern. Picking one rule (likely enabled Next with '
                                                        'errors on click) and removing the appear-disabled '
                                                        'style would likely resolve it.',
                                       'acceptance': ['Next follows the same enable and disable rule in '
                                                      'Send, Transfer, Check deposit, Request and Wire '
                                                      'drawdown.',
                                                      'No Next button looks disabled while it still accepts '
                                                      'clicks.',
                                                      'Next does not become disabled after a validation '
                                                      'error is shown.'],
                                       'verify': ['Check the design-system form guidance for whether primary '
                                                  'actions are disabled until valid or validated on submit.',
                                                  'Confirm whether the _appearDisabled style on Request is a '
                                                  'documented pattern and when it is meant to be used.'],
                                       'searchTerms': ['disabled Next button',
                                                       'appearDisabled',
                                                       'form validation on submit'],
                                       'impact': 'consistency',
                                       'confidence': 'high',
                                       'confidenceReason': 'Disabled attributes, class names and opacity '
                                                           'captured with DevTools in each flow.',
                                       'demoArtifactRisk': False},
 'payments.i-drawdown-no-accounts': {'title': 'Show funding accounts in the wire drawdown Source account '
                                              'step',
                                     'summary': "The wire drawdown Source account select reads 'There are no "
                                                "accounts' and Next stays disabled, while the Send from "
                                                'picker in the same session lists four or more accounts.',
                                     'userImpact': 'An admin setting up a payroll drawdown hits a dead end '
                                                   'with no explanation of which accounts qualify or how to '
                                                   'get one.',
                                     'fixHypothesis': 'Drawdowns may only accept a specific account type '
                                                      'that the demo org lacks. Listing eligible accounts, '
                                                      'or explaining in the empty state why none qualify, '
                                                      'would likely resolve it.',
                                     'acceptance': ['The Source account select lists every account eligible '
                                                    'for wire drawdowns.',
                                                    'When no account qualifies, the step explains why and '
                                                    'what to do next.'],
                                     'verify': ['Check which account types are eligible for wire drawdowns '
                                                'and whether the demo org has any.',
                                                'Check the design-system empty-state docs for select fields '
                                                'with no options.'],
                                     'searchTerms': ['wire drawdown no accounts',
                                                     'There are no accounts',
                                                     'drawdown funding account'],
                                     'impact': 'broken-task',
                                     'confidence': 'low',
                                     'confidenceReason': 'Seen in the demo sandbox, where the flow is '
                                                         'expected not to complete.',
                                     'demoArtifactRisk': True},
 'payments.i-duration-ranges': {'title': 'Write business-day ranges one way across Payments',
                                'summary': "Delivery estimates use four styles: '0-1 business days', "
                                           "'Arrives in 1–2 business days.', 'Appears in 1 to 5 business "
                                           "days.' and '10 - 14 business days'.",
                                'userImpact': 'Minor. The estimates are readable, but four styles for the '
                                              'same kind of range across Send, Deposit and Checkbooks look '
                                              'unedited.',
                                'fixHypothesis': 'Range strings seem to be written by hand per screen. A '
                                                 'shared helper for business-day ranges would likely resolve '
                                                 'it.',
                                'acceptance': ['All business-day ranges in Payments use the same separator.',
                                               'No range uses spaces around a hyphen.',
                                               'Delivery estimates use one trailing-period rule.'],
                                'verify': ['Check the content style guide for number ranges (en dash, hyphen '
                                           "or 'to')."],
                                'searchTerms': ['business days range',
                                                'en dash ranges',
                                                'delivery estimate copy'],
                                'impact': 'consistency',
                                'confidence': 'high',
                                'confidenceReason': 'All four range strings quoted verbatim from rendered '
                                                    'text on named screens.',
                                'demoArtifactRisk': False},
 'payments.i-edit-wipes-due-dates': {'title': 'Fix scheduled-payment edit clearing every due date',
                                     'summary': 'After confirming an amount change on one recurring payment, '
                                                "every Scheduled row switched to 'No due date'; before, rows "
                                                'showed due dates like Nov 1, Dec 1 and Apr 2027.',
                                     'userImpact': 'Admins lose the due dates they use to plan cash, on '
                                                   "payments they did not touch, and can't tell which bills "
                                                   'are coming due.',
                                     'fixHypothesis': 'The update may refresh the table from a response that '
                                                      'omits due dates, or the edit may write to more '
                                                      'payments than the one edited. Checking the stored due '
                                                      'dates after a reload would tell the two apart.',
                                     'acceptance': ['Editing one scheduled payment leaves due dates on other '
                                                    'rows unchanged.',
                                                    'The edited payment keeps its due date unless the admin '
                                                    'changed it.',
                                                    'Reloading /payments/scheduled after an edit shows the '
                                                    'same due dates as before.'],
                                     'verify': ['Reproduce on a non-demo account and reload to see whether '
                                                'due dates are lost or only hidden.',
                                                'Check whether the demo sandbox resets or reseeds scheduled '
                                                'payments after a mutation.'],
                                     'searchTerms': ['No due date after edit',
                                                     'scheduled payment edit due date',
                                                     'due dates cleared'],
                                     'impact': 'data-integrity',
                                     'confidence': 'medium',
                                     'confidenceReason': 'Before and after table rows quoted verbatim, but '
                                                         'the state bug was seen once.',
                                     'demoArtifactRisk': True},
 'payments.i-escape-exits-flow': {'title': "Stop Escape on the 'Send from' picker from exiting Send",
                                  'summary': "On the Send Amount step, pressing Escape with 'Send from' open "
                                             'returns to /send-money/pay/start and discards the amount; '
                                             'Escape on the Payment method list only closes the list.',
                                  'userImpact': 'An admin who presses Escape to dismiss a dropdown loses the '
                                                'recipient, amount and date they entered and has to restart '
                                                'the payment.',
                                  'fixHypothesis': "The Escape key seems to reach the full-screen flow's "
                                                   'close handler because the account picker does not stop '
                                                   'it. Having the picker handle Escape itself, like the '
                                                   'Payment method list, would likely resolve it.',
                                  'acceptance': ["Pressing Escape with 'Send from' open closes only the "
                                                 'picker.',
                                                 'After that Escape, the URL stays on /send-money/pay/amount '
                                                 'and the entered amount remains.',
                                                 'Every dropdown in the Send flow handles Escape the same '
                                                 'way.'],
                                  'verify': ["Check the design-system docs for the account picker's "
                                             'documented keyboard behavior.',
                                             'Check whether other flows using the same account picker '
                                             '(Transfer, Request) also exit on Escape.'],
                                  'searchTerms': ['Escape closes flow',
                                                  'account picker Escape',
                                                  'Send from dropdown keyboard'],
                                  'impact': 'broken-task',
                                  'confidence': 'high',
                                  'confidenceReason': 'Reproduced twice with URLs recorded before and after '
                                                      'the key press.',
                                  'demoArtifactRisk': False},
 'payments.i-filter-copy': {'title': "Add the unit to the 'Due next 7' status filter",
                            'summary': "The Payments Status filter lists 'Overdue', 'Due next 7', 'Not due "
                                       "yet' and 'Missing due date'; 'Due next 7' has no unit.",
                            'userImpact': "Most admins will guess 'days', but on a filter for payment due "
                                          'dates, a guess is one more thing to get wrong.',
                            'fixHypothesis': "The label seems truncated or shortened to fit. Writing 'Due in "
                                             "next 7 days' would likely resolve it.",
                            'acceptance': ["The Status filter option names its unit, for example 'Due in "
                                           "next 7 days'.",
                                           'The label fits in the popover without truncation.'],
                            'verify': ["Check the source string to see whether 'days' is dropped by "
                                       'truncation or missing from the copy.'],
                            'searchTerms': ['Due next 7', 'status filter label', 'missing unit'],
                            'impact': 'cosmetic',
                            'confidence': 'medium',
                            'confidenceReason': 'Label quoted verbatim, but it may be deliberate shorthand.',
                            'demoArtifactRisk': False},
 'payments.i-flagged-row-shift': {'title': 'Align the approved row in the ACH Flagged table',
                                  'summary': 'In Flagged ACH pulls, the approved PG&E row swaps its checkbox '
                                             'for a check icon and its cells start at 398px instead of '
                                             '400px.',
                                  'userImpact': "Cosmetic. A 2px shift is barely visible and doesn't affect "
                                                'reading.',
                                  'fixHypothesis': 'The check icon seems to be narrower than the checkbox it '
                                                   'replaces. Giving it the same width would likely resolve '
                                                   'it.',
                                  'acceptance': ['Cells in the approved row start at the same x position as '
                                                 'other rows.',
                                                 'The check icon occupies the same width as the row '
                                                 'checkbox.'],
                                  'verify': ['Check the design-system table docs for the selection column '
                                             'width.'],
                                  'searchTerms': ['table row misaligned',
                                                  'Flagged ACH approved row',
                                                  'checkbox icon width'],
                                  'impact': 'cosmetic',
                                  'confidence': 'high',
                                  'confidenceReason': 'Cell left edges measured with DevTools across four '
                                                      'rows.',
                                  'demoArtifactRisk': False},
 'payments.i-floating-bar-overlap': {'title': 'Hide the empty bulk-action bar on Edit 1099 filing',
                                     'summary': "The Edit filing page shows a fixed '0 transactions "
                                                "selected' bar at top 756px to 840px, over the Last name "
                                                'input at top 830px, on a 1440x900 viewport.',
                                     'userImpact': "Admins correcting a payee's name on a 1099 find the Last "
                                                   'name field partly covered and have to scroll to reach '
                                                   'it.',
                                     'fixHypothesis': 'The bulk-action bar seems to render regardless of '
                                                      'selection count. Showing it only when at least one '
                                                      'transaction is selected would likely resolve it.',
                                     'acceptance': ['The bulk-action bar is hidden when zero transactions '
                                                    'are selected.',
                                                    'No form field on Edit filing is covered by a fixed bar '
                                                    'at 1440x900.',
                                                    'When the bar is shown, the page reserves space so the '
                                                    'last fields stay reachable.'],
                                     'verify': ["Check the design-system docs for the bulk-action bar's show "
                                                'and hide rules.'],
                                     'searchTerms': ['bulk action bar overlap',
                                                     '0 transactions selected',
                                                     '1099 edit filing'],
                                     'impact': 'broken-task',
                                     'confidence': 'high',
                                     'confidenceReason': 'Bar and input positions measured with DevTools at '
                                                         'a stated viewport.',
                                     'demoArtifactRisk': False},
 'payments.i-foreign-currency-format': {'title': 'Format foreign-currency amounts one way in Scheduled '
                                                 'payments',
                                        'summary': "Scheduled rows show '−$2,646.40 CAD' and '−$410.40 ARS', "
                                                   'while the cancel modal for the same CAD payment shows '
                                                   "'2,646.40 CAD' without the minus or the $.",
                                        'userImpact': 'An admin canceling a foreign-currency payment sees a '
                                                      "different amount format than in the table, and a '$' "
                                                      'next to a non-USD code can be read as US dollars.',
                                        'fixHypothesis': 'The table and cancel modal seem to format currency '
                                                         'differently. Using one currency formatter with a '
                                                         'defined rule for symbol and code would likely '
                                                         'resolve it.',
                                        'acceptance': ['A foreign-currency payment shows the same amount '
                                                       'format in the table, panel and cancel modal.',
                                                       'Foreign-currency amounts follow one documented rule '
                                                       'for symbol and ISO code.'],
                                        'verify': ['Check the content style guide for foreign-currency '
                                                   "amounts; CAD and ARS both use '$' locally, so '$410.40 "
                                                   "ARS' may be allowed.",
                                                   'Check whether the cancel modal drops the minus sign on '
                                                   'purpose.'],
                                        'searchTerms': ['foreign currency format',
                                                        'CAD amount dollar sign',
                                                        'ARS currency display'],
                                        'impact': 'consistency',
                                        'confidence': 'medium',
                                        'confidenceReason': "Amounts quoted verbatim; whether '$' with a "
                                                            "code is wrong depends on Mercury's currency "
                                                            'rules.',
                                        'demoArtifactRisk': False},
 'payments.i-form-widths': {'title': 'Use one column width for full-screen Payments forms',
                            'summary': 'Full-screen form columns measure 560px (Send), 584px (Create '
                                       'recipient), 522px (ACH), 496px (Invite recipient, Transfer) and '
                                       '424px (Check deposit).',
                            'userImpact': 'Minor. Switching between Create and Invite recipient, which link '
                                          'to each other, shifts the form edges by 88px, so the two read as '
                                          'unrelated screens.',
                            'fixHypothesis': 'Each form seems to set its own max width. A shared full-screen '
                                             'form container with one or two named widths would likely '
                                             'resolve it.',
                            'acceptance': ['Create recipient and Invite recipient render form columns of the '
                                           'same width.',
                                           'Full-screen Payments forms use no more than two column widths.',
                                           'Send, Transfer, ACH and Check deposit form columns align to the '
                                           'same widths as recipient forms.'],
                            'verify': ['Check the design-system layout docs for a specified full-screen form '
                                       'column width.',
                                       "Check whether Check deposit's narrower column is deliberate for its "
                                       'image upload step.'],
                            'searchTerms': ['form column width', 'full-screen form layout', 'max-width form'],
                            'impact': 'cosmetic',
                            'confidence': 'medium',
                            'confidenceReason': 'Two widths measured with DevTools; the other three are read '
                                                'from screenshot bounds.',
                            'demoArtifactRisk': False},
 'payments.i-icon-links-a11y': {'title': 'Label the send and transactions icon links in Recipients rows',
                                'summary': 'The send and transactions icons at the end of each Recipients '
                                           'row are anchors with no text, aria-label, title or SVG title.',
                                'userImpact': "Screen reader users hear 'link' for each icon and can't tell "
                                              'which one starts a payment and which one opens transactions.',
                                'fixHypothesis': 'The row actions seem to render an icon inside an anchor '
                                                 'without a label prop. Adding an accessible name such as '
                                                 "'Send payment to <recipient>' would likely resolve it.",
                                'acceptance': ['Each Recipients row icon link exposes an accessible name in '
                                               'the accessibility tree.',
                                               'The accessible name states the action and the recipient.',
                                               'Icon links show a visible tooltip or label on hover and '
                                               'focus.'],
                                'verify': ['Check the design-system icon button or icon link docs for the '
                                           'required label prop.'],
                                'searchTerms': ['icon link accessible name',
                                                'Recipients row actions aria-label',
                                                'unlabeled link'],
                                'impact': 'accessibility',
                                'confidence': 'high',
                                'confidenceReason': 'aria-label, aria-labelledby, title and textContent '
                                                    'checked in the DOM and accessibility snapshot.',
                                'demoArtifactRisk': False},
 'payments.i-kv-label-color': {'title': "Mute the 'Bank Name' label in Domestic Transfer Details",
                               'summary': "In Domestic Transfer Details, 'Bank Name' renders in "
                                          'rgb(221,221,229), the value color, while every other label is '
                                          'rgb(157,157,168).',
                               'userImpact': 'Cosmetic. One label looks like a value, which slightly slows '
                                             'scanning of the wire details admins share with payers.',
                               'fixHypothesis': "The 'Bank Name' row seems to use a different text style "
                                                'from its siblings. Applying the same label style would '
                                                'likely resolve it.',
                               'acceptance': ['Every label in Domestic Transfer Details renders in the same '
                                              'color.',
                                              "'Bank Name' matches the style of 'Beneficiary Name' and "
                                              "'Account Number'."],
                               'verify': ['Check the design-system key-value list docs for the label token.'],
                               'searchTerms': ['label color wire details',
                                               'Bank Name label',
                                               'key value list'],
                               'impact': 'cosmetic',
                               'confidence': 'high',
                               'confidenceReason': 'Computed colors measured with DevTools for three labels '
                                                   'in the same list.',
                               'demoArtifactRisk': False},
 'payments.i-link-styling': {'title': "Style the 'Manage' link in Payments settings as a link",
                             'summary': "In Payments settings, 'Manage' renders in the sentence's grey "
                                        "rgb(195,195,204) with no underline, while 'Edit' and the 1099 link "
                                        'elsewhere are blue.',
                             'userImpact': 'Admins looking for email alert settings may not see that '
                                           "'Manage' is clickable. Users who rely on color or underline to "
                                           'spot links get neither.',
                             'fixHypothesis': 'The link seems to inherit the paragraph color instead of '
                                              'using the link style. Applying the standard inline link style '
                                              'would likely resolve it.',
                             'acceptance': ["'Manage' renders in the same color as other inline links.",
                                            "'Manage' is distinguishable from surrounding text by more than "
                                            'color alone, or meets contrast rules for links.'],
                             'verify': ['Check the design-system docs for the inline link style and whether '
                                        'underlines are required in body text.'],
                             'searchTerms': ['inline link styling',
                                             'Manage link grey',
                                             'link looks like text'],
                             'impact': 'accessibility',
                             'confidence': 'high',
                             'confidenceReason': 'Computed color and text-decoration of the link captured '
                                                 'with DevTools.',
                             'demoArtifactRisk': False},
 'payments.i-masked-numbers': {'title': 'Mask account numbers the same way on Payments review screens',
                               'summary': "Transfer review shows 'Ops / Payroll Checking ••1038' but "
                                          "'Treasury 3713286' in full, and Send review shows 'Account no. "
                                          "99990101' after the Recipient step masked it as '••0101'.",
                               'userImpact': 'Admins see the same account number masked on one step and in '
                                             'full on the next, which makes it unclear what is safe to '
                                             'screenshot or share.',
                               'fixHypothesis': 'Review screens seem to format account numbers per field. '
                                                'Applying one masking rule, or a documented reason to show '
                                                'full numbers on review, would likely resolve it.',
                               'acceptance': ['Both accounts on Transfer review follow the same masking '
                                              'rule.',
                                              'Send recipient step and Send review follow the same masking '
                                              'rule for the recipient account.',
                                              'Routing and account numbers on review screens are masked or '
                                              'shown in full by one documented rule.'],
                               'verify': ['Check with security or the design-system docs whether review '
                                          'screens are meant to show full numbers so admins can verify them.',
                                          'Check whether the Treasury account is formatted by a different '
                                          'account type that skips masking.'],
                               'searchTerms': ['account number masking',
                                               'full account number review',
                                               'masked digits'],
                               'impact': 'consistency',
                               'confidence': 'medium',
                               'confidenceReason': 'Rendered text quoted verbatim, but showing full numbers '
                                                   'on review may be deliberate.',
                               'demoArtifactRisk': False},
 'payments.i-overlay-hides-banner': {'title': 'Stack all full-screen Payments overlays below the top banner',
                                     'summary': 'Edit recipient details and Order a Checkbook render above '
                                                "the 'Explore the Mercury Demo' banner, while Send, Transfer "
                                                'and Deposit sit below it.',
                                     'userImpact': 'In the demo, the banner that says who you are viewing as '
                                                   'disappears on two screens. In production the same '
                                                   'stacking may hide any global banner shown above flows.',
                                     'fixHypothesis': 'These two overlays seem to use a higher stacking '
                                                      'layer than other full-screen flows. Moving them to '
                                                      'the same layer would likely resolve it.',
                                     'acceptance': ['The top banner stays visible on Edit recipient details '
                                                    'and Order a Checkbook.',
                                                    'All full-screen Payments overlays start below the top '
                                                    'banner.'],
                                     'verify': ['Check whether production shows any global banner above '
                                                'full-screen flows; if not, this may be demo-only.',
                                                'Check the design-system z-index or layering docs for '
                                                'full-screen overlays.'],
                                     'searchTerms': ['overlay covers banner',
                                                     'z-index full-screen flow',
                                                     'demo banner hidden'],
                                     'impact': 'cosmetic',
                                     'confidence': 'medium',
                                     'confidenceReason': 'Measured with elementFromPoint, but the covered '
                                                         'banner exists only in the demo.',
                                     'demoArtifactRisk': True},
 'payments.i-quote-glyph': {'title': "Fix the closing quote after 'Mercury Demo, Incorporated'",
                            'summary': 'Check deposit instructions and the back-of-check modal close '
                                       '“Mercury Demo, Incorporated“ with U+201C, an opening quote, instead '
                                       'of U+201D.',
                            'userImpact': 'Cosmetic typo in the endorsement text admins copy onto the back '
                                          'of a check. The instruction is still clear.',
                            'fixHypothesis': 'The endorsement template seems to have the wrong glyph after '
                                             'the company name placeholder. Swapping it for a closing quote '
                                             'would likely resolve it.',
                            'acceptance': ['The character after the company name in check endorsement '
                                           'instructions is U+201D.',
                                           'The back-of-check modal uses the same corrected quotes.'],
                            'verify': ['Check whether the quote comes from the endorsement template or from '
                                       'the stored company name.'],
                            'searchTerms': ['check endorsement quote',
                                            'curly quote typo',
                                            'Mercury Demo Incorporated'],
                            'impact': 'cosmetic',
                            'confidence': 'high',
                            'confidenceReason': 'Code point U+201C confirmed in the rendered text.',
                            'demoArtifactRisk': False},
 'payments.i-recurring-counts': {'title': 'Match recurring payment progress to the cancel summary',
                                 'summary': "The weekly CAD payment shows 'Progress 5 of 6' while its cancel "
                                            "modal lists '1 payment total'; a Lighthouse payment's badge "
                                            "says 'Monthly' but its edit form's Repeat reads 'Custom'.",
                                 'userImpact': "Admins canceling or editing a recurring payment can't be "
                                               'sure how many payments remain or what rule is in effect.',
                                 'fixHypothesis': 'The panel and modal may count completed versus remaining '
                                                  'payments differently, and the edit form may map some '
                                                  "monthly rules to 'Custom'. Labeling the progress as "
                                                  'completed or remaining, and showing the same rule name in '
                                                  'both places, would likely resolve it.',
                                 'acceptance': ['The panel states whether its progress counts completed or '
                                                'scheduled payments.',
                                                "The panel progress and the cancel modal's remaining count "
                                                'agree for the same payment.',
                                                "The frequency badge and the edit form's Repeat field name "
                                                'the same rule.'],
                                 'verify': ["Check whether '5 of 6' means five completed; if so, one "
                                            'remaining agrees and only the label is unclear.',
                                            "Check whether 'Custom' is the expected Repeat value for monthly "
                                            'rules on a specific day.'],
                                 'searchTerms': ['recurring payment progress',
                                                 '5 of 6 payments',
                                                 'Repeat Custom Monthly'],
                                 'impact': 'data-integrity',
                                 'confidence': 'low',
                                 'confidenceReason': 'Strings quoted verbatim, but the counts may agree '
                                                     'under one reading and use seeded demo data.',
                                 'demoArtifactRisk': True},
 'payments.i-status-copy': {'title': 'Name payment statuses and methods the same way everywhere',
                            'summary': "Recipients show 'Pending details' where Scheduled shows 'Details "
                                       "pending', Scheduled abbreviates 'Intl. Wire', and one Request page "
                                       "shows both 'Pay with Mercury' and 'Pay With Mercury'.",
                            'userImpact': 'Admins filtering or searching by status or method may not match '
                                          "'Pending details' with 'Details pending'. The casing mismatch on "
                                          'one page is a polish issue.',
                            'fixHypothesis': 'Status and method labels seem to be defined separately per '
                                             'surface. Pulling them from one shared label set would likely '
                                             'resolve it.',
                            'acceptance': ['The pending-details status uses one wording on Recipients and '
                                           'Scheduled.',
                                           "The Request page spells 'Pay with Mercury' the same way in the "
                                           'preview and the methods list.',
                                           'International wire uses one label, or one documented '
                                           'abbreviation for tables.'],
                            'verify': ['Check the content style guide or glossary for the approved status '
                                       "name and whether 'Intl.' is an allowed abbreviation.",
                                       "Check whether 'Pay With Mercury' in the payer preview is a branded "
                                       'feature name.'],
                            'searchTerms': ['Pending details status',
                                            'Intl. Wire label',
                                            'Pay with Mercury casing'],
                            'impact': 'consistency',
                            'confidence': 'medium',
                            'confidenceReason': 'Strings quoted verbatim, but the abbreviation and preview '
                                                'casing may follow conventions we cannot see.',
                            'demoArtifactRisk': False},
 'payments.i-stepper-heading-mismatch': {'title': 'Match stepper labels to page headings in deposit and '
                                                  'drawdown flows',
                                         'summary': "Check deposit's stepper says 'Check images' above the "
                                                    "H1 'Upload pictures'; the wire drawdown stepper says "
                                                    "'Funding account' above the heading 'Source account'.",
                                         'userImpact': 'Minor. Admins read two names for the current step '
                                                       'and may wonder whether they are on the step they '
                                                       'expected.',
                                         'fixHypothesis': 'Stepper labels and headings seem to be written '
                                                          'separately. Using one term per step in both '
                                                          'places would likely resolve it.',
                                         'acceptance': ['The check deposit stepper label and page heading '
                                                        'for step 3 use the same term.',
                                                        'The wire drawdown stepper label and page heading '
                                                        'for the account step use the same term.'],
                                         'verify': ['Check the content style guide or glossary for the '
                                                    "preferred term ('pictures' vs 'images', 'funding' vs "
                                                    "'source' account)."],
                                         'searchTerms': ['stepper label mismatch',
                                                         'Upload pictures Check images',
                                                         'Funding account Source account'],
                                         'impact': 'consistency',
                                         'confidence': 'high',
                                         'confidenceReason': 'Stepper and heading text quoted verbatim from '
                                                             'the same screen in both flows.',
                                         'demoArtifactRisk': False},
 'payments.i-success-screens': {'title': 'Use one success screen layout across Move money flows',
                                'summary': 'Send, Transfer and Check deposit share one success layout; '
                                           'Request, Order checkbook and Bulk approval each use another, and '
                                           'adding an ACH authorization shows no confirmation.',
                                'userImpact': 'Admins finishing a money action get a different confirmation '
                                              'each time, and after adding an ACH authorization they get '
                                              'none, so they have to scan the list to know it worked.',
                                'fixHypothesis': 'Each flow seems to build its own success page. A shared '
                                                 'success layout with a standard dashboard link, plus a '
                                                 'toast for ACH authorizations, would likely resolve it.',
                                'acceptance': ['Send, Transfer, Check deposit, Request, Order checkbook and '
                                               'Bulk approval success screens share alignment, heading size '
                                               'and background.',
                                               'Success screens use one label for the dashboard link.',
                                               'Adding an ACH authorization shows a confirmation message.'],
                                'verify': ["Check the content style guide for 'Go to dashboard' vs 'Return "
                                           "to dashboard'.",
                                           'Check the design-system docs for a success or confirmation page '
                                           'pattern and whether centered variants are allowed.',
                                           'Confirm with the ACH authorizations owner whether the silent '
                                           'return to the list is intended.'],
                                'searchTerms': ['success screen',
                                                'confirmation page layout',
                                                'ACH authorization no toast'],
                                'impact': 'consistency',
                                'confidence': 'high',
                                'confidenceReason': 'Class names, backgrounds and heading sizes measured '
                                                    'with DevTools on each success screen.',
                                'demoArtifactRisk': False},
 'payments.i-tax-request-count': {'title': 'Explain why selected recipients drop out of tax requests',
                                  'summary': 'With two recipients selected, the Request tax documents modal '
                                             "says 'from 1 recipient' and lists only Debug LLC, without "
                                             'saying why Nutritionist was excluded.',
                                  'userImpact': 'An admin requesting W-9s may assume both recipients got the '
                                                'request. The reason only appears later in a tooltip.',
                                  'fixHypothesis': 'The modal seems to filter out ineligible recipients '
                                                   'without surfacing them. Listing excluded recipients with '
                                                   'the reason would likely resolve it.',
                                  'acceptance': ['The Request tax documents modal names each selected '
                                                 'recipient it excludes.',
                                                 'The modal states why each excluded recipient is not '
                                                 'eligible.',
                                                 'The count in the modal and the selection bar can be '
                                                 'reconciled from what the modal shows.'],
                                  'verify': ['Check the eligibility rules for tax document requests and why '
                                             'Nutritionist was excluded (existing W-9, missing email, '
                                             'non-US).',
                                             'Check whether the exclusion reason depends on seeded demo '
                                             'recipient data.'],
                                  'searchTerms': ['request tax documents count',
                                                  'recipients excluded tax docs',
                                                  'eligible for tax document requests'],
                                  'impact': 'broken-task',
                                  'confidence': 'medium',
                                  'confidenceReason': 'Bar, modal and tooltip text quoted verbatim, but '
                                                      'eligibility depends on data we cannot see.',
                                  'demoArtifactRisk': True},
 'payments.i-title-case': {'title': 'Use sentence case for Payments headings and buttons',
                           'summary': "Most Payments UI is sentence case, but 'Request Tax Documents', "
                                      "'Payment Details', 'Send Payment', 'Review Requests', 'Remove Filing' "
                                      'and more are Title Case, sometimes beside sentence-case siblings.',
                           'userImpact': "Polish issue. Pairs like 'Remove Filing' next to 'Edit filing', or "
                                         "4 'Edit Filing' buttons among 21 'Edit filing', make the product "
                                         'read as assembled from different parts.',
                           'fixHypothesis': 'These strings seem to predate or skip a sentence-case pass. '
                                            "Converting them to sentence case, and replacing the literal '+' "
                                            "in '+ Add Account' with an icon, would likely resolve it.",
                           'acceptance': ['Buttons shown side by side in Payments and Taxes use the same '
                                          'casing.',
                                          "Recipient panel section headings 'Payment details' and 'Tax "
                                          "documents' use the same casing.",
                                          "All 'Edit filing' buttons on /taxes/1099/filing/2026 use one "
                                          'casing.',
                                          "'Add account' uses an icon instead of a literal '+' character."],
                           'verify': ['Check the content style guide casing rule and whether product names '
                                      "like 'Checkbook' or 'SAFE' are exceptions.",
                                      "Check the design-system button docs for whether a leading '+' in "
                                      'labels is allowed.'],
                           'searchTerms': ['Title Case', 'sentence case buttons', 'Edit Filing casing'],
                           'impact': 'consistency',
                           'confidence': 'high',
                           'confidenceReason': 'Rendered strings quoted verbatim, with side-by-side pairs '
                                               'and button counts from DevTools.',
                           'demoArtifactRisk': False},
 'payments.i-toast-copy': {'title': 'Word approval and update toasts the same way',
                           'summary': "Approving from the panel toasts 'Request approved', from the batch "
                                      "reviewer 'Approved'; editing a scheduled payment toasts 'Payment(s) "
                                      "updated' with a parenthetical plural.",
                           'userImpact': 'Minor. The toasts are understandable, but the same outcome reads '
                                         'differently depending on where the admin approved it.',
                           'fixHypothesis': 'Toast strings seem to be written per surface. One set of toast '
                                            'messages, with real singular and plural forms, would likely '
                                            'resolve it.',
                           'acceptance': ['Approving a request shows the same toast text in the panel and '
                                          'the batch reviewer.',
                                          'The scheduled edit toast uses singular or plural based on the '
                                          "count, without '(s)'."],
                           'verify': ['Check the content style guide for toast message patterns and whether '
                                      "'(s)' plurals are allowed."],
                           'searchTerms': ['toast copy', 'Payment(s) updated', 'Request approved toast'],
                           'impact': 'consistency',
                           'confidence': 'medium',
                           'confidenceReason': 'Toast strings quoted verbatim; whether they must match '
                                               'depends on style rules we cannot see.',
                           'demoArtifactRisk': False},
 'payments.i-validation-punctuation': {'title': 'Match punctuation in Payments validation errors',
                                       'summary': "Errors in one form end with and without periods ('Must be "
                                                  "a dollar value.' above 'Please choose an account'), and "
                                                  "wire errors use '#' shorthand: 'Routing # should be a "
                                                  "9-digit number'.",
                                       'userImpact': 'Polish issue. Mixed punctuation and shorthand in '
                                                     'adjacent errors reads as unedited copy in the forms '
                                                     'where admins enter bank details.',
                                       'fixHypothesis': 'Error strings seem to be written per field rather '
                                                        'than from a shared set. Aligning them to one '
                                                        "punctuation rule and spelling out 'Routing number' "
                                                        "and 'Account number' would likely resolve it.",
                                       'acceptance': ['All inline validation errors in Payments follow one '
                                                      'trailing-period rule.',
                                                      "Wire bank-details errors spell out 'routing number' "
                                                      "and 'account number' instead of '#'.",
                                                      'Errors shown together in one form use matching '
                                                      'sentence structure.'],
                                       'verify': ['Check the content style guide for whether inline error '
                                                  'messages end with a period.',
                                                  "Check whether '#' as an abbreviation for 'number' is "
                                                  'allowed in UI copy.'],
                                       'searchTerms': ['validation message punctuation',
                                                       'error message period',
                                                       'Routing # error'],
                                       'impact': 'consistency',
                                       'confidence': 'high',
                                       'confidenceReason': 'Error strings quoted verbatim with styles '
                                                           'measured, mixed within the same form.',
                                       'demoArtifactRisk': False},
 'payments.i-w8ben-spelling': {'title': 'Spell W-8BEN and W-9 the same way everywhere',
                               'summary': "The tax form appears as 'W-9/W-8 BEN', 'W-8BEN' and 'W-9/W8-BEN' "
                                          "across Recipients and Taxes, and a button reads 'Request w-9s' "
                                          "under the H1 'Request W-9s'.",
                               'userImpact': 'Admins handling 1099s rely on exact form names. Three '
                                             'spellings of one IRS form make it look like three different '
                                             'documents and hurt search.',
                               'fixHypothesis': 'The form names seem to be typed by hand in each string. One '
                                                'shared set of tax form names, using the IRS spelling '
                                                "'W-8BEN', would likely resolve it.",
                               'acceptance': ["Every Payments and Taxes string spells the form 'W-8BEN'.",
                                              "Every Payments and Taxes string spells 'W-9' with a capital "
                                              'W.',
                                              'The Request W-9s button label matches the page H1 casing.'],
                               'verify': ['Check the content style guide or glossary for the approved '
                                          'spelling of tax form names.',
                                          "Check whether the Tax Documents tab label 'W-9/W8-BEN' comes from "
                                          'a shared constant used elsewhere.'],
                               'searchTerms': ['W-8BEN spelling', 'W-9 casing', 'tax form names'],
                               'impact': 'consistency',
                               'confidence': 'high',
                               'confidenceReason': 'Accessible names and button text quoted verbatim from '
                                                   'four surfaces.',
                               'demoArtifactRisk': False},
 'settings.i-capitalization': {'title': 'Use sentence case for Vault tags and the Save New Role button',
                               'summary': "Vault tags read 'Transfer in Progress' beside 'Up to $220M FDIC "
                                          "insured'. 'Create role' opens a page whose primary button is "
                                          "'Save New Role'. 'AMAZON Store' sits beside 'Shopify Store 1'.",
                               'userImpact': 'A polish issue with no effect on tasks. The Title Case tag and '
                                             'button stand out against their sentence-case neighbors.',
                               'fixHypothesis': 'These strings seem to predate a sentence-case convention. '
                                                "'AMAZON Store' is likely a seeded or integration-provided "
                                                'store name rather than product copy.',
                               'acceptance': ["'Transfer in Progress' and 'Save New Role' render in sentence "
                                              'case.',
                                              'Tags on neighboring Vault cards use the same casing.'],
                               'verify': ['Confirm in the content style guide that tags and buttons use '
                                          'sentence case.',
                                          "Check whether 'AMAZON Store' is seeded demo data or text returned "
                                          'by the Amazon integration.'],
                               'searchTerms': ['Title Case tag',
                                               'Save New Role',
                                               'Transfer in Progress casing'],
                               'impact': 'consistency',
                               'confidence': 'medium',
                               'confidenceReason': 'Strings are quoted verbatim; the store name may be demo '
                                                   'data.',
                               'demoArtifactRisk': True},
 'settings.i-channel-tags-as-status': {'title': 'Style Notifications channel labels apart from status tags',
                                       'summary': "The Notifications 'Email' and 'Push' labels use the same "
                                                  "green tag as 'Active', 'Delivered' and 'Paid' elsewhere, "
                                                  "and 'Off' uses the same neutral tag as 'Disabled' and "
                                                  "'Pending'.",
                                       'userImpact': "Admins may read 'Email' as a status rather than the "
                                                     'channel that is on. Context makes the meaning '
                                                     'recoverable, so the effect is small.',
                                       'fixHypothesis': 'The page seems to reuse the status tag for channel '
                                                        'labels. A neutral chip for channels, keeping green '
                                                        'for state, would likely resolve it.',
                                       'acceptance': ['Channel labels on Notifications use a tag style not '
                                                      'used for success statuses.',
                                                      'An on channel and an off channel are distinguishable '
                                                      'without relying on color.'],
                                       'verify': ['Check the design-system tag docs for whether success '
                                                  'tones are reserved for states.'],
                                       'searchTerms': ['notification channel tags',
                                                       'Email tag green',
                                                       'status tag reuse'],
                                       'impact': 'consistency',
                                       'confidence': 'medium',
                                       'confidenceReason': 'Colors were measured with DevTools, but reusing '
                                                           'the success tag may be intentional.',
                                       'demoArtifactRisk': False},
 'settings.i-connection-status-display': {'title': 'Show healthy store connections with a status tag',
                                          'summary': "On Third party connections, 'No connection' is a pink "
                                                     "12px tag but healthy stores show 'Connected' as plain "
                                                     '15px text. Team and Roles show healthy states as a '
                                                     "green 'Active' tag.",
                                          'userImpact': 'Admins compare two text styles to scan store '
                                                        'health, and the table differs from the rest of '
                                                        'Settings. The failed store is still easy to spot.',
                                          'fixHypothesis': 'The connections table seems to render a tag only '
                                                           'for the error state. Using a status tag for '
                                                           'every state, as Team and Roles do, would likely '
                                                           'resolve it.',
                                          'acceptance': ['Every row on Third party connections and '
                                                         'Integrations shows its status as a tag.',
                                                         'Healthy connections use the same tag tone as '
                                                         "'Active' on Team."],
                                          'verify': ['Check the design-system status guidance for whether '
                                                     'healthy states are tagged or shown as plain text.'],
                                          'searchTerms': ['Connected status tag',
                                                          'third party connections status',
                                                          'No connection badge'],
                                          'impact': 'consistency',
                                          'confidence': 'high',
                                          'confidenceReason': 'Computed styles were captured with '
                                                              'getComputedStyle on three tables.',
                                          'demoArtifactRisk': False},
 'settings.i-date-formats': {'title': 'Use one date format on Account Security and API Tokens',
                             'summary': "Account Security activity shows '09/24 9:11 PM' with no year, its "
                                        "card header says 'Last updated Sep 24', and API Tokens shows 'May "
                                        "16, 2019'.",
                             'userImpact': 'Admins reviewing security events and token use compare dates '
                                           'across these pages. A numeric month/day with no year is easy to '
                                           'misread, especially for older activity.',
                             'fixHypothesis': 'Each page seems to format dates locally. A shared formatter '
                                              'with named variants for dates and timestamps would likely '
                                              'resolve it.',
                             'acceptance': ['Account Security activity uses the same date format as API '
                                            'Tokens, followed by the time.',
                                            'Dates outside the current year show the year on both pages.'],
                             'verify': ["Look up the content style guide's date and timestamp standards, "
                                        'including whether numeric dates are allowed.'],
                             'searchTerms': ['activity history date format',
                                             'API tokens date format',
                                             'Account Security timestamp'],
                             'impact': 'consistency',
                             'confidence': 'high',
                             'confidenceReason': 'All three formats are quoted verbatim with computed '
                                                 'styles.',
                             'demoArtifactRisk': False},
 'settings.i-destructive-confirm-copy': {'title': 'Use one template for delete confirmations in API and '
                                                  'Security',
                                         'summary': "'Delete endpoint?' / 'Delete', 'Delete API Token' / "
                                                    "'Delete Token' and 'Remove passkey' / 'Remove' differ "
                                                    'in punctuation, casing and closing sentence. Deleting a '
                                                    'token shows no toast.',
                                         'userImpact': 'The dialogs work, but with no toast after deleting a '
                                                       'token the admin has to infer success from the row '
                                                       'disappearing.',
                                         'fixHypothesis': 'Each dialog seems written separately. One '
                                                          'confirmation template for title, button and '
                                                          'closing sentence, plus a toast on token deletion, '
                                                          'would likely resolve it.',
                                         'acceptance': ['The three dialogs share title punctuation, casing '
                                                        'and closing sentence.',
                                                        'Deleting an API token shows a success toast.'],
                                         'verify': ["Look up the content style guide's template for "
                                                    'destructive confirmations, including how to quote '
                                                    'object names.',
                                                    "Check whether 'Remove' versus 'Delete' is a deliberate "
                                                    'distinction for passkeys.'],
                                         'searchTerms': ['delete confirmation copy',
                                                         'Delete API Token dialog',
                                                         'cannot be undone'],
                                         'impact': 'consistency',
                                         'confidence': 'medium',
                                         'confidenceReason': 'Titles, buttons and bodies are quoted '
                                                             'verbatim; the right template depends on the '
                                                             'style guide.',
                                         'demoArtifactRisk': False},
 'settings.i-dialog-actions': {'title': 'Use consistent weight and alignment for Settings dialog actions',
                               'summary': "Notification dialogs end with a secondary 'Done', Download "
                                          "Statements with a primary 'Close'. Verify identity actions sit "
                                          "left, others right. In the token menu, Edit's icon is blue and "
                                          "Delete's grey.",
                               'userImpact': 'Admins look for the closing action in a different place and '
                                             'style in each dialog. Small friction, not an error risk.',
                               'fixHypothesis': 'Dialogs seem to set their own footer layout. A shared '
                                                'dialog footer, with one rule for single-action dialogs, '
                                                'would likely resolve it.',
                               'acceptance': ['Single-action dialogs use the same button variant for their '
                                              'only action.',
                                              'Dialog footers align actions on the same side.',
                                              'Token menu icons share one color, or follow a documented '
                                              'destructive style.'],
                               'verify': ['Check the design-system dialog docs for footer alignment and the '
                                          'variant for a lone dismiss action.'],
                               'searchTerms': ['dialog footer alignment',
                                               'Done secondary Close primary',
                                               'menu icon color'],
                               'impact': 'consistency',
                               'confidence': 'high',
                               'confidenceReason': 'Button backgrounds and icon fills were captured with '
                                                   'DevTools.',
                               'demoArtifactRisk': False},
 'settings.i-heading-nav-mismatch': {'title': 'Match Settings page headings to their sidebar labels',
                                     'summary': "Sidebar 'Tokens' opens 'API Tokens', 'Vault' opens 'Mercury "
                                                "Vault', 'Company Profile' opens 'Mercury Demo' and "
                                                "'Profile' opens 'Jane Black'. Only Account Security "
                                                'matches.',
                                     'userImpact': 'Minor. Admins still reach the right page, but the '
                                                   "heading doesn't confirm where they landed, most "
                                                   'noticeably when it shows the company name.',
                                     'fixHypothesis': 'Profile pages may deliberately title themselves with '
                                                      'the entity name. Aligning labels and headings for '
                                                      'utility pages such as Tokens and Vault would likely '
                                                      'resolve most of it.',
                                     'acceptance': ['Sidebar labels and h1 text match for Tokens and Vault.',
                                                    'Pages titled with an entity name also show the page '
                                                    'type near the heading.'],
                                     'verify': ['Check the design-system navigation guidance for whether the '
                                                'h1 must repeat the nav label.',
                                                'Check whether entity-name headings on profile pages are a '
                                                'deliberate pattern.'],
                                     'searchTerms': ['sidebar label heading mismatch',
                                                     'API Tokens page title',
                                                     'settings page heading'],
                                     'impact': 'consistency',
                                     'confidence': 'medium',
                                     'confidenceReason': 'Link and h1 text are quoted verbatim, but some '
                                                         'differences may be intentional.',
                                     'demoArtifactRisk': False},
 'settings.i-list-overflow': {'title': 'Keep the Scopes and Event types lists inside their modals',
                              'summary': 'The Create API token Scopes list (28 options) opens upward past '
                                         'the modal and under the demo banner, hiding the Accounts header. '
                                         'The webhook Event types list extends below the modal.',
                              'userImpact': "Admins can't see the first scopes, including the Accounts "
                                            'group, so they may miss permissions they need to grant.',
                              'fixHypothesis': 'The lists seem to be positioned without collision handling. '
                                               'Limiting their height to the viewport would likely resolve '
                                               'it; the demo banner may make the top overlap worse than in '
                                               'production.',
                              'acceptance': ['The full Scopes list, including the Accounts header, can be '
                                             'seen and reached.',
                                             'The Event types list fits within the viewport or scrolls '
                                             'inside its popover.'],
                              'verify': ['Check whether the fixed demo banner exists in production and '
                                         'whether the top overlap still occurs without it.',
                                         'Check the design-system combobox docs for maximum height and '
                                         'collision behavior.'],
                              'searchTerms': ['dropdown overflow modal',
                                              'scopes list clipped',
                                              'event types list overflow'],
                              'impact': 'broken-task',
                              'confidence': 'medium',
                              'confidenceReason': 'Seen in screenshots only, and the demo banner contributes '
                                                  'to one of the overlaps.',
                              'demoArtifactRisk': True},
 'settings.i-login-verb': {'title': "Write 'Login failure' in the Account Security activity log",
                           'summary': "The Account Security activity history lists the event 'Log in "
                                      "failure', using the verb where the noun is meant, next to 'Password "
                                      "changed' and 'Device forgotten'.",
                           'userImpact': 'A small copy error. Admins still understand the event.',
                           'fixHypothesis': 'The event label seems to use the verb form by mistake. Changing '
                                            'it to the noun form the style guide prefers would likely '
                                            'resolve it.',
                           'acceptance': ["The activity event reads 'Login failure', or the form the style "
                                          'guide specifies.',
                                          'Exported activity history uses the same label.'],
                           'verify': ["Check the content style guide for 'log in' (verb) versus 'login' "
                                      "(noun), and whether Mercury prefers 'sign in'."],
                           'searchTerms': ['Log in failure',
                                           'login noun verb',
                                           'activity history event labels'],
                           'impact': 'cosmetic',
                           'confidence': 'medium',
                           'confidenceReason': 'The label is quoted verbatim; the preferred term depends on '
                                               'the style guide.',
                           'demoArtifactRisk': False},
 'settings.i-one-time-secret-pattern': {'title': 'Use one save safeguard for one-time secrets in Settings',
                                        'summary': 'The webhook signing secret, API token and backup codes '
                                                   'are each shown once. Only backup codes offer Print, '
                                                   'Download and Copy and gate Done behind a checkbox; the '
                                                   'others close with an X.',
                                        'userImpact': 'An admin can close the token or signing-secret dialog '
                                                      'in one click before copying, then has to create a new '
                                                      'one. Screen reader users hear the secret itself as '
                                                      "the copy button's name.",
                                        'fixHypothesis': 'Three features seem to have built their own reveal '
                                                         'dialogs. One shared pattern with a labeled Copy '
                                                         'button and a confirm-before-close step would '
                                                         'likely resolve it.',
                                        'acceptance': ['The API token and signing secret dialogs have a '
                                                       'visible, labeled Copy button.',
                                                       'Closing a one-time secret dialog asks for '
                                                       'confirmation or requires a saved-it checkbox.',
                                                       "The copy control's accessible name describes the "
                                                       'action, not the secret.'],
                                        'verify': ['Check the design-system docs for an existing reveal-once '
                                                   'or one-time secret pattern.',
                                                   'Check security guidance on exposing a secret as an '
                                                   'accessible name.'],
                                        'searchTerms': ['one-time secret dialog',
                                                        'API token copy button',
                                                        'signing secret modal'],
                                        'impact': 'broken-task',
                                        'confidence': 'high',
                                        'confidenceReason': 'Controls and accessible names were read from '
                                                            'accessibility snapshots of all three dialogs.',
                                        'demoArtifactRisk': False},
 'settings.i-referral-money-and-math': {'title': "Fix Referrals totals that don't match the paid rows",
                                        'summary': "'Total earned $900' but paid rows sum to $750; '4 "
                                                   "approved referrals unlock Gold bonus' but the Gold tier "
                                                   "reads '10 referrals'; amounts mix '$250.00' with '$250' "
                                                   "and '$10K' with '$10,000'.",
                                        'userImpact': 'An admin checking referral payouts sees totals that '
                                                      "don't reconcile with the table and can't tell how far "
                                                      'they are from the next tier.',
                                        'fixHypothesis': 'Totals seem to come from a different source than '
                                                         'the rows, possibly seeded demo values. Money '
                                                         'formats seem written per string; a shared money '
                                                         'format would likely resolve that part.',
                                        'acceptance': ['Total earned equals the sum of Paid rows.',
                                                       'The Gold unlock copy and the tier tracker state the '
                                                       'same threshold.',
                                                       'Amounts on the page and in the email preview use one '
                                                       'money format.'],
                                        'verify': ['Check whether referral totals and tiers are seeded demo '
                                                   'values rather than live calculations.',
                                                   'Look up the style guide rules for cents and '
                                                   "abbreviations such as '$10K'."],
                                        'searchTerms': ['referral total earned',
                                                        'referral tier Gold',
                                                        'money format cents'],
                                        'impact': 'data-integrity',
                                        'confidence': 'medium',
                                        'confidenceReason': 'Figures are quoted verbatim, but referral data '
                                                            'is likely seeded for the demo.',
                                        'demoArtifactRisk': True},
 'settings.i-required-copy': {'title': 'Use one required-field message across Settings forms',
                              'summary': "Empty required fields say 'Please complete this field' in most "
                                         "forms and 'This field is required' on Profile; the webhook URL "
                                         'shows nothing, IP whitelist shows a format error, referrals use a '
                                         'tooltip.',
                              'userImpact': 'Admins get different signals for the same mistake. The IP '
                                            'whitelist case misleads most: it suggests a format problem when '
                                            'the field is empty.',
                              'fixHypothesis': 'Each form seems to handle validation locally. A shared '
                                               'required-field message with inline placement would likely '
                                               'resolve it.',
                              'acceptance': ['Empty required fields in the listed forms show the same '
                                             'message inline below the field.',
                                             'An empty required IP whitelist shows the required message, not '
                                             'the format message.',
                                             'The referral email error appears inline, not only in a '
                                             'tooltip.'],
                              'verify': ['Look up the required-field message in the content style guide.',
                                         'Check the design-system form docs for whether combobox errors may '
                                         'appear in a tooltip.'],
                              'searchTerms': ['required field message',
                                              'Please complete this field',
                                              'form validation inconsistent'],
                              'impact': 'consistency',
                              'confidence': 'high',
                              'confidenceReason': 'Error strings are quoted verbatim from five forms.',
                              'demoArtifactRisk': False},
 'settings.i-sign-out-verbs': {'title': 'Use one verb for signing in and out on personal Security',
                               'summary': "Personal Security shows 'Log out other sessions' above per-row "
                                          "'Sign out' and a 'Log out of all other devices' checkbox, and "
                                          "mixes 'Sign in faster…' with 'log in' in dialogs.",
                               'userImpact': 'Minor. Two verbs for one action can make admins wonder whether '
                                             "'Log out other sessions' and 'Sign out' do different things.",
                               'fixHypothesis': 'The strings seem to come from different features. Choosing '
                                                'the verb the style guide prefers and updating the listed '
                                                'strings would likely resolve it.',
                               'acceptance': ['Personal Security and its dialogs use one verb pair for '
                                              'signing in and out.',
                                              'The section button and the per-row button use the same verb.'],
                               'verify': ["Check the content style guide for 'sign in' versus 'log in'."],
                               'searchTerms': ['Log out Sign out',
                                               'sign in log in wording',
                                               'sessions sign out'],
                               'impact': 'consistency',
                               'confidence': 'high',
                               'confidenceReason': 'Both verbs are quoted verbatim from the same section.',
                               'demoArtifactRisk': False},
 'settings.i-stale-after-action': {'title': 'Refresh Webhooks, Security and Referrals after actions finish',
                                   'summary': 'A new webhook endpoint never appears in the list, regenerated '
                                              'backup codes keep their old dates, referral rows claim to be '
                                              'links but do nothing, and event deliveries are not in time '
                                              'order.',
                                   'userImpact': "After creating an endpoint the admin can't confirm it "
                                                 'exists and may create it again. Unsorted deliveries make '
                                                 'it harder to trace a failure.',
                                   'fixHypothesis': 'Lists may not refetch after changes, or the demo '
                                                    'sandbox may not persist new records. The delivery sort '
                                                    'and the referral link behavior seem to be separate '
                                                    'issues.',
                                   'acceptance': ['A newly created webhook endpoint appears in Endpoints '
                                                  'without a reload.',
                                                  'After regenerating backup codes, Added on shows the '
                                                  'regeneration date.',
                                                  'Event deliveries are sorted by timestamp, and referral '
                                                  'rows either open something or are not announced as '
                                                  'links.'],
                                   'verify': ['Check whether the demo sandbox persists new webhook endpoints '
                                              'and backup-code regeneration.',
                                              'Check whether referral rows are meant to open a detail view.'],
                                   'searchTerms': ['webhook not in list after create',
                                                   'stale list after action',
                                                   'event deliveries sort order'],
                                   'impact': 'broken-task',
                                   'confidence': 'medium',
                                   'confidenceReason': 'Row text before and after each action was captured, '
                                                       'but the sandbox may not persist records.',
                                   'demoArtifactRisk': True},
 'settings.i-submit-disable-rules': {'title': 'Apply one rule for disabling submit buttons in Settings '
                                              'modals',
                                     'summary': "'Create token' is enabled on an empty form and validates on "
                                                "click, while 'Create endpoint' stays disabled until the URL "
                                                "is verified and 'Confirm' until an email is added, with no "
                                                'explanation.',
                                     'userImpact': 'Admins facing a disabled button get no hint why. A valid '
                                                   "webhook URL still leaves 'Create endpoint' disabled "
                                                   'until they find the separate verify step.',
                                     'fixHypothesis': 'Each modal seems to choose its own rule. One pattern '
                                                      'for submit buttons, plus visible text explaining what '
                                                      'is missing when a button is disabled, would likely '
                                                      'resolve it.',
                                     'acceptance': ['Create token, Create endpoint and Confirm follow the '
                                                    'same enable and disable rule.',
                                                    'Every disabled submit button has visible text '
                                                    'explaining what is missing.'],
                                     'verify': ['Check the design-system button or form docs for the '
                                                'approved rule on disabled submit buttons.',
                                                'Check whether webhook verification is a required product '
                                                'step that justifies the disabled state.'],
                                     'searchTerms': ['disabled submit button',
                                                     'Create endpoint disabled',
                                                     'validate on click'],
                                     'impact': 'consistency',
                                     'confidence': 'high',
                                     'confidenceReason': 'Button states were read from accessibility '
                                                         'snapshots in four modals.',
                                     'demoArtifactRisk': False},
 'settings.i-table-type-scale': {'title': 'Use one table text size and empty-cell glyph across Settings',
                                 'summary': 'Table body text is 16px on API Tokens, 14px on Webhooks and '
                                            'Referrals and 13px on Security. Empty cells use U+2212 on '
                                            "Security and U+2014 on Referrals. Headers mix 'Created Date' "
                                            "and 'Created date'.",
                                 'userImpact': 'Tables look slightly different from page to page. Reading is '
                                               'not affected much.',
                                 'fixHypothesis': 'These tables may use different table components or local '
                                                  'overrides. Moving them onto one table style would likely '
                                                  'resolve it.',
                                 'acceptance': ['Body cells on Tokens, Webhooks, Referrals and Security '
                                                'share one font size.',
                                                'Empty cells use the same character on every Settings table.',
                                                'Column headers use the same casing.'],
                                 'verify': ['Check the design-system table docs for body text size and the '
                                            'empty-cell character.'],
                                 'searchTerms': ['table font size', 'empty cell dash', 'Created Date header'],
                                 'impact': 'consistency',
                                 'confidence': 'high',
                                 'confidenceReason': 'Font sizes and character codes were captured with '
                                                     'DevTools on four tables.',
                                 'demoArtifactRisk': False},
 'settings.i-timestamp-formats-personal': {'title': 'Use one timestamp format on Webhooks, Security and '
                                                    'Profile',
                                           'summary': "Webhooks print 'Feb 18, 2026 01:14:32' (24-hour, "
                                                      "seconds), Security shows 'Sep 14 at 12:00 AM' and "
                                                      "'Sep 24, 2025 at 12:00 AM' in one column, and Profile "
                                                      "shows '01/31/1990'.",
                                           'userImpact': 'The same kind of value reads differently on each '
                                                         "page. '12:00 AM' on every Security row also "
                                                         'implies a time that may never have been recorded, '
                                                         'which can mislead someone reviewing account '
                                                         'activity.',
                                           'fixHypothesis': 'Each page seems to format timestamps locally, '
                                                            'and Security may print date-only values as '
                                                            'midnight. A shared formatter that omits the '
                                                            'time when none is stored would likely resolve '
                                                            'it.',
                                           'acceptance': ['Webhooks, Security and Profile use the shared '
                                                          'date and timestamp formats.',
                                                          "Rows without a stored time show no '12:00 AM'.",
                                                          'One column uses one date format for recent and '
                                                          'older dates.'],
                                           'verify': ['Look up the style guide rules for 24-hour versus '
                                                      '12-hour time and seconds in developer-facing tables.',
                                                      "Check whether '12:00 AM' comes from seeded demo "
                                                      'timestamps with no time part.'],
                                           'searchTerms': ['timestamp format webhooks',
                                                           '12:00 AM passkey activity',
                                                           'date of birth format'],
                                           'impact': 'consistency',
                                           'confidence': 'high',
                                           'confidenceReason': 'All formats are quoted verbatim from '
                                                               'rendered cells on named screens.',
                                           'demoArtifactRisk': True},
 'settings.i-title-case-new-pages': {'title': 'Match Settings dialog title casing to the buttons that open '
                                              'them',
                                     'summary': "'Change password' opens 'Change Password' with 'Old "
                                                "Password' fields, 'Download statements' opens 'Download "
                                                "Statements', and 'Generate New Codes', 'Opt In', 'Important "
                                                "Updates' and 'Account Opened' use Title Case.",
                                     'userImpact': 'Polish. The mismatch is most visible when a '
                                                   'sentence-case button opens a Title Case dialog.',
                                     'fixHypothesis': 'These strings seem to predate or skip the '
                                                      'sentence-case convention. Updating the listed strings '
                                                      'would likely resolve it.',
                                     'acceptance': ['Each dialog title matches the casing of the button that '
                                                    'opens it.',
                                                    "The 'Account Opened' status and the 'Account opened' "
                                                    'tile use the same casing.',
                                                    'Notifications section headings use sentence case.'],
                                     'verify': ['Confirm in the content style guide that dialog titles, '
                                                'field labels and section headings use sentence case.'],
                                     'searchTerms': ['Change Password title case',
                                                     'Download Statements dialog',
                                                     'sentence case dialogs'],
                                     'impact': 'consistency',
                                     'confidence': 'medium',
                                     'confidenceReason': 'Strings are quoted verbatim; whether each breaks a '
                                                         'rule depends on the style guide.',
                                     'demoArtifactRisk': False},
 'settings.i-toast-punctuation': {'title': 'Use one sentence style for Settings success toasts',
                                  'summary': "Settings toasts mix full sentences with periods ('Your company "
                                             "name has been updated.') and fragments without ('Job title "
                                             "updated', 'Invite sent'). One adds 'successfully'; another "
                                             "uses a colon ('New role created: AP Clerk').",
                                  'userImpact': 'Polish only. Admins get the confirmation either way, but '
                                                'the toasts read as if written by different people.',
                                  'fixHypothesis': 'Toast strings seem to be written per feature. Applying '
                                                   'one toast pattern from the style guide to the listed '
                                                   'strings would likely resolve it.',
                                  'acceptance': ['All listed Settings toasts follow the same punctuation '
                                                 'rule.',
                                                 "No Settings toast uses the word 'successfully'.",
                                                 'Toasts that name an object use the same sentence '
                                                 'structure.'],
                                  'verify': ['Look up the content style guide rule for toast punctuation and '
                                             "whether 'successfully' is discouraged."],
                                  'searchTerms': ['toast punctuation',
                                                  'success toast copy',
                                                  'successfully toast'],
                                  'impact': 'consistency',
                                  'confidence': 'medium',
                                  'confidenceReason': 'Toast strings are quoted verbatim; the correct style '
                                                      'depends on the style guide.',
                                  'demoArtifactRisk': False},
 'settings.i-token-created-tomorrow': {'title': "Fix new API token rows showing 'Tomorrow' and a mock "
                                                'creator',
                                       'summary': "A token created at 21:48 PDT on Sep 24 shows 'Tomorrow' "
                                                  "for Last used and Created date, 'mocked@mercury.com' as "
                                                  "creator and 'Custom' for 'Custom Scopes'. Older rows show "
                                                  'absolute dates.',
                                       'userImpact': 'Admins auditing tokens see a creation date in the '
                                                     "future and a creator who isn't the signed-in user, so "
                                                     "the table can't serve as an audit record.",
                                       'fixHypothesis': 'The date seems computed in UTC and printed as a '
                                                        'relative word, and the creator may be a sandbox '
                                                        "placeholder. Formatting in the user's time zone as "
                                                        'an absolute date would likely fix the date part.',
                                       'acceptance': ['A token created in the evening, US Pacific time, '
                                                      "shows today's date in the user's time zone.",
                                                      'New rows use the same absolute date format as '
                                                      'existing rows.',
                                                      "Created by shows the signed-in user's email."],
                                       'verify': ["Check whether 'mocked@mercury.com' is a demo sandbox "
                                                  'placeholder that production replaces with the real '
                                                  'creator.',
                                                  'Check whether relative dates are allowed in audit-style '
                                                  'tables.'],
                                       'searchTerms': ['API token created Tomorrow',
                                                       'token date timezone UTC',
                                                       'mocked@mercury.com'],
                                       'impact': 'data-integrity',
                                       'confidence': 'medium',
                                       'confidenceReason': 'The browser clock and row text were captured, '
                                                           'but the mock creator shows the demo sandbox is '
                                                           'involved.',
                                       'demoArtifactRisk': True},
 'settings.i-tokens-truncation': {'title': "Size API Tokens columns so names and creators aren't cut off",
                                  'summary': 'All seven API Tokens columns are 138px, so nicknames and '
                                             "creator emails clip at 106px ('My read-write Mercury API "
                                             "token' needs 214px), while '2 IPs whitelisted' wraps to two "
                                             'lines.',
                                  'userImpact': 'Admins tell tokens apart by nickname and creator. With both '
                                                'cut off, they have to hover each row and may edit or delete '
                                                'the wrong token.',
                                  'fixHypothesis': 'The table seems to use equal fixed column widths. '
                                                   'Letting text columns size to content, with narrower '
                                                   'columns for short values, would likely resolve it.',
                                  'acceptance': ["At the audited viewport, 'My read-write Mercury API token' "
                                                 "and 'landon@demo.mercury.com' show without an ellipsis.",
                                                 "'2 IPs whitelisted' fits on one line.",
                                                 'All token rows share one height.'],
                                  'verify': ['Check the design-system table docs for column sizing and '
                                             'truncation rules.',
                                             'Check whether full values are meant to be available through a '
                                             'tooltip or detail view.'],
                                  'searchTerms': ['API tokens table truncation',
                                                  'token nickname ellipsis',
                                                  'table column width'],
                                  'impact': 'broken-task',
                                  'confidence': 'high',
                                  'confidenceReason': 'Column widths, clip widths and required text widths '
                                                      'were measured with DevTools.',
                                  'demoArtifactRisk': False},
 'settings.i-unnamed-row-buttons': {'title': 'Name the API token row menu buttons',
                                    'summary': 'The ••• button on each API Tokens row has no text or '
                                               'aria-label; the page has 11 buttons with empty text and no '
                                               'aria-label.',
                                    'userImpact': 'A screen reader user hears three identical unnamed '
                                                  "buttons and can't tell which token they would edit or "
                                                  'delete.',
                                    'fixHypothesis': 'The icon button seems to lack an aria-label. Adding '
                                                     "one that includes the token nickname, such as 'Actions "
                                                     "for Map audit token', would likely resolve it.",
                                    'acceptance': ['Each token row menu button has an accessible name that '
                                                   'includes the token nickname.',
                                                   'No button on API Tokens has an empty accessible name.'],
                                    'verify': ['Check the design-system icon button docs for the '
                                               'accessible-name pattern for row menus.'],
                                    'searchTerms': ['row menu aria-label',
                                                    'API tokens actions button',
                                                    'unnamed button'],
                                    'impact': 'accessibility',
                                    'confidence': 'high',
                                    'confidenceReason': 'The accessibility snapshot and a DOM query both '
                                                        'show buttons with no name.',
                                    'demoArtifactRisk': False},
 'settings.i-warning-hue-overloaded': {'title': 'Stop using one orange tag for paused, failed and neutral '
                                                'states',
                                       'summary': "'Paused', 'Failed', 'Personal Banking' and 'Sync "
                                                  "eligible' all use rgb(252,155,111) orange, while 'Needs "
                                                  "attention' uses the pink rgb(252,146,180) that form "
                                                  'errors use.',
                                       'userImpact': "Admins can't use color to tell a failed webhook "
                                                     'delivery, which needs action, from a paused endpoint '
                                                     'or a referral category.',
                                       'fixHypothesis': 'Tags seem to pick tones per feature rather than '
                                                        'from a shared meaning map. Reserving the warning '
                                                        'tone for states that need attention and using '
                                                        'neutral for categories would likely resolve it.',
                                       'acceptance': ["'Failed' and 'Paused' use different tones.",
                                                      "Category labels such as 'Personal Banking' use a "
                                                      'neutral tag.',
                                                      "'Sync eligible' uses the tone the design system "
                                                      'assigns to informational states.'],
                                       'verify': ['Check the design-system status color docs for which tones '
                                                  'map to error, warning, informational and neutral.',
                                                  "Check whether 'Needs attention' is meant to share the "
                                                  'form-error pink.'],
                                       'searchTerms': ['tag color meaning',
                                                       'Failed Paused same color',
                                                       'orange warning tag'],
                                       'impact': 'consistency',
                                       'confidence': 'high',
                                       'confidenceReason': 'Text and background colors were captured with '
                                                           'getComputedStyle on four pages.',
                                       'demoArtifactRisk': False},
 'settings.i-webhook-edit-drops-events': {'title': 'Fix webhook URL edit clearing saved event subscriptions',
                                          'summary': 'In Edit webhook endpoint, changing the URL resets '
                                                     "Event types from '3 selected' to the empty 'All "
                                                     "events' placeholder, and a 'Verify endpoint' button "
                                                     'appears.',
                                          'userImpact': 'An admin fixing a typo in the URL has to re-pick '
                                                        'every event or, if they miss the change, may save '
                                                        'an endpoint that receives every event type.',
                                          'fixHypothesis': 'Changing the URL seems to reset the event '
                                                           'selection along with the verification state. '
                                                           'Keeping the saved events when only the URL '
                                                           'changes would likely resolve it.',
                                          'acceptance': ['After the URL is edited, Event types still shows '
                                                         'the saved selection.',
                                                         'Saving after a URL-only edit keeps the same '
                                                         'subscriptions in the detail panel.'],
                                          'verify': ['Check whether a new URL is meant to require choosing '
                                                     'events again, for example as part of verification.',
                                                     "Check what is saved when the field shows the 'All "
                                                     "events' placeholder."],
                                          'searchTerms': ['webhook edit event types reset',
                                                          'webhook URL change subscriptions',
                                                          'Edit webhook endpoint'],
                                          'impact': 'data-integrity',
                                          'confidence': 'high',
                                          'confidenceReason': 'Accessibility snapshots before and after the '
                                                              'edit show the combobox value change.',
                                          'demoArtifactRisk': False},
 'transactions.i-abbrev-case': {'title': 'Use one abbreviation case and precision in Insights trend notes',
                                'summary': "Insights trend notes use both 'K' and 'k' in one sentence "
                                           "('$33.4K' vs '-$303k'), and headline figures switch between "
                                           "'$1.9M', '−$870,358' and '-$2,970.23'.",
                                'userImpact': 'Admins reading trend notes have to parse each figure on its '
                                              'own terms. It slows comparison, though the values themselves '
                                              'are correct.',
                                'fixHypothesis': 'The notes seem to be generated with ad hoc number '
                                                 'formatting. A shared compact-number formatter with fixed '
                                                 'casing and precision would likely fix it.',
                                'acceptance': ['Thousands abbreviations on Insights use one case.',
                                               'Figures within one trend note sentence use the same '
                                               'precision.',
                                               'Headline totals follow one documented precision rule.'],
                                'verify': ["Check the content style guide for compact number formatting ('k' "
                                           "versus 'K', number of decimals)."],
                                'searchTerms': ['K vs k abbreviation',
                                                'Insights number precision',
                                                'compact number format'],
                                'impact': 'consistency',
                                'confidence': 'high',
                                'confidenceReason': 'Strings read via innerText and confirmed with DevTools '
                                                    'in one sentence.',
                                'demoArtifactRisk': False},
 'transactions.i-bar-label-truncation': {'title': 'Show full bar and node labels in Transactions and '
                                                  'Insights charts',
                                         'summary': 'Transactions graph bar labels truncate to about six '
                                                    "characters ('Ventur…', 'Milgra…', 'Softwa…'), and the "
                                                    "Insights Sankey truncates a recipient to 'Milgram "
                                                    "Brokera…'.",
                                         'userImpact': "Admins can't identify bars without hovering each "
                                                       'one, which makes the chart slow to read.',
                                         'fixHypothesis': 'Labels seem to be cut at a fixed character count. '
                                                          'Wrapping, rotating, or moving names to a legend '
                                                          'would likely help.',
                                         'acceptance': ['Bar labels in the Transactions graph show enough '
                                                        'text to identify each category or counterparty.',
                                                        'Truncated labels expose the full name on hover and '
                                                        'on keyboard focus.',
                                                        'Sankey node labels do not truncate when space '
                                                        'allows.'],
                                         'verify': ['Check the design-system chart guidance for axis label '
                                                    'truncation rules.'],
                                         'searchTerms': ['chart label truncation',
                                                         'bar chart labels cut off',
                                                         'Sankey label ellipsis'],
                                         'impact': 'broken-task',
                                         'confidence': 'high',
                                         'confidenceReason': 'SVG text content read from the DOM on two '
                                                             'charts.',
                                         'demoArtifactRisk': False},
 'transactions.i-category-name-label': {'title': 'Label the name field in the Create category dialog',
                                        'summary': 'The Create category name input has no visible label, '
                                                   'placeholder, aria-label or aria-labelledby, and '
                                                   'input.labels is empty.',
                                        'userImpact': 'Screen reader users hear an unnamed text field and '
                                                      'have to guess it is the category name. Sighted users '
                                                      'only have the dialog title for context.',
                                        'fixHypothesis': 'The input likely renders without its label. Adding '
                                                         "a visible 'Category name' label tied to the input "
                                                         'would likely fix it.',
                                        'acceptance': ['The Create category name input has an accessible '
                                                       'name.',
                                                       'A visible label identifies the field.'],
                                        'verify': ['Check the design-system text field docs for whether a '
                                                   'visible label is required.'],
                                        'searchTerms': ['Create category input label',
                                                        'unlabeled text field',
                                                        'category name aria-label'],
                                        'impact': 'accessibility',
                                        'confidence': 'high',
                                        'confidenceReason': 'Input attributes and input.labels inspected '
                                                            'with DevTools.',
                                        'demoArtifactRisk': False},
 'transactions.i-counterparty-name': {'title': 'Use the counterparty nickname in the Insights breakdown and '
                                               'Sankey',
                                      'summary': "The Insights breakdown and Sankey show 'GUSTO', while the "
                                                 'drill-down table, filter chip and detail panel show the '
                                                 "nickname 'Gusto (Payroll)'.",
                                      'userImpact': "An admin clicks 'GUSTO' and lands on 'Gusto (Payroll)'. "
                                                    'It is minor, but it makes them check they reached the '
                                                    'right counterparty.',
                                      'fixHypothesis': 'Insights seems to group by the raw counterparty name '
                                                       'while Transactions shows the nickname. Showing the '
                                                       'nickname in Insights would likely align them.',
                                      'acceptance': ["The Insights breakdown and Sankey show 'Gusto "
                                                     "(Payroll)' where Transactions does.",
                                                     'After drilling in, the filter chip shows the same name '
                                                     'as the row clicked.'],
                                      'verify': ['Check whether Insights groups by raw counterparty on '
                                                 'purpose, so that nicknamed and unnicknamed payments '
                                                 'combine.'],
                                      'searchTerms': ['GUSTO vs Gusto (Payroll)',
                                                      'counterparty nickname Insights',
                                                      'raw merchant name'],
                                      'impact': 'consistency',
                                      'confidence': 'high',
                                      'confidenceReason': 'Names quoted verbatim from Insights, the chip and '
                                                          'the detail panel.',
                                      'demoArtifactRisk': False},
 'transactions.i-drilldown-date-chip': {'title': 'Show the carried-over date range in the drill-down date '
                                                 'chip',
                                        'summary': 'Drilling from Insights into Transactions carries a Jun '
                                                   '17 to Sep 24, 2026 range in the URL, but the date chip '
                                                   "reads 'All time' and the summary reads 'Money out all "
                                                   "time −$268,986.75'.",
                                        'userImpact': 'Admins see a three-month total labeled as lifetime '
                                                      'spend and may report it that way, for example as '
                                                      'total payroll paid to Gusto.',
                                        'fixHypothesis': "The chip seems to fall back to 'All time' when it "
                                                         'receives a custom range. Showing the custom range '
                                                         'in the chip and the summary label would likely fix '
                                                         'it.',
                                        'acceptance': ['After drilling in from Insights, the date chip shows '
                                                       'the carried-over range.',
                                                       'The summary label names the same range as the chip.',
                                                       'Opening the chip shows the same range as the chip '
                                                       'label.'],
                                        'verify': ['Check whether a custom range set directly in the '
                                                   "Transactions date picker also shows 'All time', to "
                                                   'separate a drill-down bug from a chip bug.'],
                                        'searchTerms': ['All time date chip',
                                                        'Insights drill-down date range',
                                                        'custom date range label'],
                                        'impact': 'data-integrity',
                                        'confidence': 'high',
                                        'confidenceReason': 'URL filter value, chip text and picker state '
                                                            'captured together.',
                                        'demoArtifactRisk': False},
 'transactions.i-dropdown-overlaps-footer': {'title': 'Keep the panel Category dropdown inside the '
                                                      'transaction panel',
                                             'summary': 'In the transaction side panel, the Category listbox '
                                                        "extends to x=1432, past the panel's right edge at "
                                                        '1412, and down over the comment composer, so '
                                                        "'Create category' covers 'Add a comment'.",
                                             'userImpact': 'An admin categorizing a transaction may click '
                                                           "the comment field when aiming for 'Create "
                                                           "category', or the reverse, and the dropdown "
                                                           'looks broken.',
                                             'fixHypothesis': 'The listbox seems to be positioned without '
                                                              'collision handling against the panel. '
                                                              'Constraining its width to the panel and '
                                                              'flipping or limiting its height would likely '
                                                              'fix it.',
                                             'acceptance': ['The open Category listbox stays within the side '
                                                            "panel's horizontal bounds.",
                                                            "'Create category' does not overlap the comment "
                                                            'composer.',
                                                            'The listbox scrolls or flips when there is not '
                                                            'enough room below the field.'],
                                             'verify': ['Check the design-system combobox docs for expected '
                                                        'collision handling inside side panels.'],
                                             'searchTerms': ['category dropdown overflow',
                                                             'listbox overlaps comment',
                                                             'side panel dropdown clipped'],
                                             'impact': 'broken-task',
                                             'confidence': 'high',
                                             'confidenceReason': 'Element rects and z-index measured with '
                                                                 'DevTools.',
                                             'demoArtifactRisk': False},
 'transactions.i-dup-keyword-suggestions': {'title': "Tell apart duplicate 'Google' rows in keyword "
                                                     'suggestions',
                                            'summary': "Typing 'Google' in the keyword filter lists two "
                                                       "checkbox rows both labeled 'Google', backed by "
                                                       'different counterparties, with nothing to tell them '
                                                       'apart.',
                                            'userImpact': "An admin filtering by counterparty can't tell "
                                                          "which 'Google' to pick and may filter on the "
                                                          'wrong one.',
                                            'fixHypothesis': 'Suggestions seem to show only the display '
                                                             'name. Adding a secondary detail, such as '
                                                             'account or transaction count, would likely '
                                                             'distinguish them. Demo data may contain two '
                                                             'near-identical counterparties.',
                                            'acceptance': ['Keyword suggestions with the same name show a '
                                                           'secondary detail that tells them apart.',
                                                           "Each suggestion's accessible name includes that "
                                                           'detail.'],
                                            'verify': ["Check whether the demo seed data has two 'Google' "
                                                       'counterparties that production would merge.',
                                                       'Check the design system for a menu item pattern with '
                                                       'secondary text.'],
                                            'searchTerms': ['duplicate keyword suggestions',
                                                            'two Google rows',
                                                            'counterparty suggestions'],
                                            'impact': 'broken-task',
                                            'confidence': 'medium',
                                            'confidenceReason': 'Popover text quoted verbatim, but the '
                                                                'duplicates may come from demo seed data.',
                                            'demoArtifactRisk': True},
 'transactions.i-empty-state-styles': {'title': 'Use one empty-state style for Transactions and Insights '
                                                'lists',
                                       'summary': "Three empty states use three styles: a 17px white 'No "
                                                  "transactions matching those filters', a 15px grey 'No "
                                                  "excluded transactions yet' with a subline, and a 14px "
                                                  "grey 'No matches in last 500 transactions.'",
                                       'userImpact': 'Minor. Each empty result still reads clearly, but the '
                                                     'three messages look unrelated.',
                                       'fixHypothesis': 'Each surface seems to write its own empty message. '
                                                        'A shared empty-state component with a small variant '
                                                        'for popovers would likely align them.',
                                       'acceptance': ['The Transactions table and Insights exclusions empty '
                                                      'states use the same text styles.',
                                                      'Popover empty messages use a documented small '
                                                      'empty-state variant.',
                                                      'Empty-state messages follow one punctuation rule.'],
                                       'verify': ['Check the design system for an empty-state component and '
                                                  'its variants.',
                                                  'Check the content style guide for empty-state tone and '
                                                  'punctuation.'],
                                       'searchTerms': ['empty state style',
                                                       'No transactions matching those filters',
                                                       'no results message'],
                                       'impact': 'consistency',
                                       'confidence': 'high',
                                       'confidenceReason': 'Class names, sizes and colors captured with '
                                                           'DevTools on three empty states.',
                                       'demoArtifactRisk': False},
 'transactions.i-export-button-variant': {'title': 'Use one button variant for Export on Transactions and '
                                                   'Insights',
                                          'summary': "Transactions shows 'Export all' as a transparent "
                                                     'tertiary button in the toolbar; Insights shows '
                                                     "'Export' as a filled secondary pill in the page "
                                                     'header. Size, font and padding match.',
                                          'userImpact': 'Minor. The same action looks more prominent on one '
                                                        'page than on the other.',
                                          'fixHypothesis': 'The two pages likely chose variants '
                                                           'independently. Picking one variant and one label '
                                                           'for export would likely resolve it; the '
                                                           'different placement may be deliberate.',
                                          'acceptance': ['Export on Transactions and Insights uses the same '
                                                         'button variant.',
                                                         'The export label follows one pattern on both '
                                                         "pages, or the 'all' qualifier is documented."],
                                          'verify': ['Check the design-system button hierarchy guidance for '
                                                     'toolbar actions versus page-header actions.'],
                                          'searchTerms': ['Export all button',
                                                          'Export button variant',
                                                          'tertiary vs secondary button'],
                                          'impact': 'consistency',
                                          'confidence': 'medium',
                                          'confidenceReason': 'Class names and backgrounds captured with '
                                                              'DevTools, but the variant choice may be '
                                                              'intentional.',
                                          'demoArtifactRisk': False},
 'transactions.i-export-empty-name': {'title': 'Validate the file name in Export transactions',
                                      'summary': 'Clearing File name in Export transactions and clicking '
                                                 'Download closes the dialog with no validation message and '
                                                 'no confirmation.',
                                      'userImpact': "An admin can't tell whether a download started or what "
                                                    'the file will be called, and may export again.',
                                      'fixHypothesis': 'The dialog seems not to validate the name field. '
                                                       'Requiring a name or falling back to the default, '
                                                       'plus confirming the download, would likely fix it. '
                                                       'The demo may block real downloads.',
                                      'acceptance': ['Download with an empty file name shows an inline '
                                                     'error, or uses the default name.',
                                                     'After Download, a confirmation appears or the browser '
                                                     'download starts.'],
                                      'verify': ['Check whether the demo sandbox suppresses file downloads, '
                                                 'which would explain the missing confirmation.',
                                                 'Check whether an empty name is meant to fall back to a '
                                                 'default name.'],
                                      'searchTerms': ['export empty file name',
                                                      'export transactions validation',
                                                      'download no confirmation'],
                                      'impact': 'broken-task',
                                      'confidence': 'medium',
                                      'confidenceReason': 'Input value and dialog removal checked in the '
                                                          'DOM, but the demo may suppress downloads.',
                                      'demoArtifactRisk': True},
 'transactions.i-failed-green': {'title': 'Stop showing failed incoming payments in money-in green',
                                 'summary': "'Payment from NASA — Failed $419.00' is struck through but "
                                            'keeps the money-in green (rgb 119,197,153) of completed '
                                            'deposits; failed outgoing payments use neutral white.',
                                 'userImpact': 'Admins scanning for incoming money can read a failed payment '
                                               'as received, especially if they miss the strikethrough, and '
                                               'expect funds that never arrived.',
                                 'fixHypothesis': 'The amount color seems to follow direction only, not '
                                                  'status. Using the neutral color for any failed amount '
                                                  'would likely match outgoing failures.',
                                 'acceptance': ['Failed incoming payments render in the same neutral color '
                                                'as failed outgoing payments.',
                                                'Completed incoming payments keep the money-in green.'],
                                 'verify': ['Check the design-system color tokens for money amounts for '
                                            'guidance on failed or reversed transactions.'],
                                 'searchTerms': ['failed payment green',
                                                 'money in color failed',
                                                 'struck-through amount color'],
                                 'impact': 'consistency',
                                 'confidence': 'high',
                                 'confidenceReason': 'Class names and colors captured with DevTools on '
                                                     'failed incoming and outgoing rows.',
                                 'demoArtifactRisk': False},
 'transactions.i-insights-export-silent': {'title': 'Confirm when an Insights export finishes',
                                           'summary': 'Clicking Export on Insights shows a three-dot loader, '
                                                      "then the button returns to 'Export' about 3 seconds "
                                                      'later with no toast, while other Insights actions '
                                                      "confirm with toasts such as 'View deleted'.",
                                           'userImpact': "An admin can't tell whether the export worked or "
                                                         'where the file went, and may click again.',
                                           'fixHypothesis': 'The export seems to finish without triggering '
                                                            'the toast other Insights actions use. Adding a '
                                                            'success or failure toast would likely resolve '
                                                            'it. The demo may not produce a file at all.',
                                           'acceptance': ['After Export finishes, a toast or status message '
                                                          'confirms success or failure.',
                                                          'The confirmation is exposed through role=status.'],
                                           'verify': ['Check whether the demo sandbox suppresses downloads, '
                                                      'which would explain the silent finish.',
                                                      "Check whether the browser's download bar is the "
                                                      'intended confirmation for exports.'],
                                           'searchTerms': ['Insights export no confirmation',
                                                           'export toast',
                                                           'silent export'],
                                           'impact': 'broken-task',
                                           'confidence': 'medium',
                                           'confidenceReason': 'Button text and status regions checked in '
                                                               'the DOM, but the demo may suppress '
                                                               'downloads.',
                                           'demoArtifactRisk': True},
 'transactions.i-keyword-plural': {'title': 'Use one name for the keyword filter in Transactions and '
                                            'Insights',
                                   'summary': "Transactions labels its filter tab and chip 'Keyword'; "
                                              "Insights labels it 'Keywords'. The two filter panels also "
                                              'offer different tab sets (15 tabs versus 11 plus Exclusions).',
                                   'userImpact': 'Minor. An admin who filters in one place and looks for the '
                                                 'same filter in the other may pause at the different name.',
                                   'fixHypothesis': 'The two filter panels seem to be configured separately. '
                                                    'Sharing the label list would likely align the name; the '
                                                    'different tab sets may be intentional because Insights '
                                                    'aggregates.',
                                   'acceptance': ['The keyword filter has the same label in both panels.',
                                                  'Filter tabs present in both panels appear in the same '
                                                  'order.'],
                                   'verify': ['Check whether the Insights filter panel is meant to offer a '
                                              'subset of Transactions filters, and which label the style '
                                              'guide prefers.'],
                                   'searchTerms': ['Keyword vs Keywords',
                                                   'Insights filter panel',
                                                   'filter tab labels'],
                                   'impact': 'consistency',
                                   'confidence': 'high',
                                   'confidenceReason': 'Tab labels read from [role=tab] in both panels.',
                                   'demoArtifactRisk': False},
 'transactions.i-keyword-popover-height': {'title': 'Size the Transactions keyword popover to its content',
                                           'summary': "With 'Amazon' typed, the keyword popover measures "
                                                      "376x400px while holding only the search field, 'No "
                                                      "matches in last 500 transactions.' and one link.",
                                           'userImpact': 'Cosmetic. The empty block covers table rows the '
                                                         'admin may still want to see.',
                                           'fixHypothesis': 'The popover likely has a fixed height. A '
                                                            'content-based height with a maximum would '
                                                            'likely fix it.',
                                           'acceptance': ['With no matches, the keyword popover is only as '
                                                          'tall as its content.',
                                                          'With many matches, the popover caps its height '
                                                          'and scrolls.'],
                                           'verify': ['Check the design-system popover docs for fixed versus '
                                                      'content-sized heights.'],
                                           'searchTerms': ['keyword popover height',
                                                           'empty popover space',
                                                           'fixed height menu'],
                                           'impact': 'cosmetic',
                                           'confidence': 'high',
                                           'confidenceReason': 'Popover size measured with DevTools.',
                                           'demoArtifactRisk': False},
 'transactions.i-menu-case': {'title': "Use sentence case for 'View recipient' in the ACH panel menu",
                              'summary': "The ACH transaction panel's overflow menu lists 'View Recipient' "
                                         "next to 'Nickname recipient'; the card panel menu reads 'Nickname "
                                         "sender'.",
                              'userImpact': 'Polish. Two casings in a two-item menu look like a slip.',
                              'fixHypothesis': "'View Recipient' likely has its own Title Case string. "
                                               "Changing it to 'View recipient' would likely resolve it.",
                              'acceptance': ["The ACH panel overflow menu reads 'View recipient' and "
                                             "'Nickname recipient'.",
                                             'All transaction panel overflow menu items use sentence case.'],
                              'verify': ['Check the content style guide for menu item casing.'],
                              'searchTerms': ['View Recipient casing',
                                              'ACH panel menu',
                                              'Nickname recipient'],
                              'impact': 'consistency',
                              'confidence': 'high',
                              'confidenceReason': 'Menu item text read from [role=menuitem] in one menu.',
                              'demoArtifactRisk': False},
 'transactions.i-minus-glyphs': {'title': 'Use the minus sign for negative amounts in Insights',
                                 'summary': 'Insights writes negative amounts with three characters on one '
                                            "screen: U+2212 ('−$870,358'), hyphen-minus ('-$2,970.23 YTD', "
                                            "'-$303k') and en dash ('–$45.2k/mo').",
                                 'userImpact': "Most sighted users won't notice. Screen readers may read a "
                                               'hyphen or dash differently from a minus, and copied values '
                                               'may paste inconsistently into spreadsheets.',
                                 'fixHypothesis': 'The generated trend notes seem to format negatives '
                                                  'separately from the headline and table components. Using '
                                                  'the same money formatter for the notes would likely fix '
                                                  'it.',
                                 'acceptance': ['Every negative amount on Insights pages starts with U+2212.',
                                                'Trend note amounts use the same negative format as the '
                                                'headline figures.'],
                                 'verify': ['Check the content style guide or number formatting rules for '
                                            'which minus character Mercury uses.'],
                                 'searchTerms': ['minus sign negative amount',
                                                 'hyphen minus Insights',
                                                 'trend notes formatting'],
                                 'impact': 'consistency',
                                 'confidence': 'high',
                                 'confidenceReason': 'Code points measured in rendered text and confirmed '
                                                     'with DevTools class names.',
                                 'demoArtifactRisk': False},
 'transactions.i-money-in-case': {'title': "Write 'Money in' and 'Money out' in one casing across Insights",
                                  'summary': "Insights uses 'Money In'/'Money Out' in the cashflow menu, "
                                             "filter chip, chart legend and Sankey, but 'Money in'/'Money "
                                             "out' in headings, tabs and the Transactions summary.",
                                  'userImpact': 'Polish only. The labels are understood either way, but two '
                                                'casings for one term on one page look unedited.',
                                  'fixHypothesis': 'The chart and filter strings seem to come from a '
                                                   'different source than the headings. Using one pair of '
                                                   'labels throughout would likely fix it.',
                                  'acceptance': ["'Money in' and 'Money out' use one casing in the cashflow "
                                                 'menu, chips, legends, Sankey and headings.',
                                                 "'All Cashflow' follows the same casing rule."],
                                  'verify': ["Check the content style guide for whether 'Money in' and "
                                             "'Money out' are product terms with fixed casing."],
                                  'searchTerms': ['Money In casing', 'Money in vs Money In', 'All Cashflow'],
                                  'impact': 'consistency',
                                  'confidence': 'high',
                                  'confidenceReason': 'Both casings read from rendered text on the same '
                                                      'screen.',
                                  'demoArtifactRisk': False},
 'transactions.i-receipt-rename': {'title': 'Keep the original file name when a PNG receipt is uploaded',
                                   'summary': 'Uploading receipt.png (image/png) to a card transaction lists '
                                              "the attachment as 'receipt.pdf'.",
                                   'userImpact': "Admins and accountants can't trust the attachment list to "
                                                 'show what was uploaded, and may think the wrong file was '
                                                 'attached.',
                                   'fixHypothesis': 'The upload may convert images to PDF and rename them, '
                                                    'or the demo may return a fixed attachment. Showing the '
                                                    'original name, or saying the file was converted, would '
                                                    'likely resolve it.',
                                   'acceptance': ['After uploading receipt.png, the Attachments row shows '
                                                  "'receipt.png' or states that it was converted to PDF.",
                                                  'Opening the attachment shows the uploaded image.'],
                                   'verify': ['Check whether Mercury converts image receipts to PDF by '
                                              'design.',
                                              "Check whether the demo sandbox returns a canned 'receipt.pdf' "
                                              'for any upload.'],
                                   'searchTerms': ['receipt renamed pdf',
                                                   'attachment file name',
                                                   'png receipt upload'],
                                   'impact': 'data-integrity',
                                   'confidence': 'low',
                                   'confidenceReason': 'Seen once, and a demo sandbox that stubs uploads '
                                                       'would explain it.',
                                   'demoArtifactRisk': True},
 'transactions.i-sankey-total': {'title': "Label the gap between Sankey 'Money In' and the Insights headline",
                                 'summary': "On one screen the headline reads 'Money in $1.9M' and the "
                                            "Sankey node reads 'Money In $2.7M', because the Sankey includes "
                                            '$774K of transfers; nothing explains the difference.',
                                 'userImpact': 'Admins reconciling cash flow see two totals under the same '
                                               'label and may report the wrong one, or stop trusting both.',
                                 'fixHypothesis': 'The Sankey seems to include internal transfers that the '
                                                  'headline excludes. Renaming the node, or noting that it '
                                                  'includes transfers, would likely remove the conflict.',
                                 'acceptance': ['The Sankey center node and the headline show the same Money '
                                                'in total, or use different labels.',
                                                'The Sankey states that transfers are included when they '
                                                'are.'],
                                 'verify': ['Check with the Insights owner whether including transfers in '
                                            'the Sankey total is intended.',
                                            'Check whether the linked-transfer toggle in Insights Settings '
                                            'changes either figure.'],
                                 'searchTerms': ['Sankey total mismatch',
                                                 'Money in $2.7M vs $1.9M',
                                                 'transfers in Sankey'],
                                 'impact': 'data-integrity',
                                 'confidence': 'high',
                                 'confidenceReason': 'Both totals and the transfer link quoted verbatim from '
                                                     'one screen.',
                                 'demoArtifactRisk': False},
 'transactions.i-settings-title-case': {'title': 'Use sentence case for Insights Settings and Rename View',
                                        'summary': "'Insights Settings', 'Rename View' and the 'Remove 2 "
                                                   "Exclusions' button use Title Case, while sibling dialogs "
                                                   "and buttons read 'Export transactions', 'Save view', "
                                                   "'Create category' and 'Exclude'.",
                                        'userImpact': 'Minor copy drift across dialogs that otherwise share '
                                                      'one heading style.',
                                        'fixHypothesis': 'These strings may have missed a sentence-case '
                                                         "pass. Rewriting them as 'Insights settings', "
                                                         "'Rename view' and 'Remove 2 exclusions' would "
                                                         'likely resolve it.',
                                        'acceptance': ['The Insights Settings and Rename View dialog titles '
                                                       'use sentence case.',
                                                       'The exclusions action button uses sentence case.'],
                                        'verify': ['Check the content style guide for dialog title and '
                                                   "button casing, and whether 'Insights' is capitalized as "
                                                   'a product name.'],
                                        'searchTerms': ['Insights Settings title case',
                                                        'Rename View',
                                                        'Remove Exclusions button'],
                                        'impact': 'consistency',
                                        'confidence': 'medium',
                                        'confidenceReason': 'Titles quoted verbatim; the casing rule is in a '
                                                            'style guide we have not seen.',
                                        'demoArtifactRisk': False},
 'transactions.i-two-money-components': {'title': 'Match cents treatment in Transactions totals and table '
                                                  'amounts',
                                         'summary': 'On /transactions, the Net change, Money in and Money '
                                                    'out totals show full-size cents while table amounts use '
                                                    'superscript cents; the Money in/out card also mixes '
                                                    "'$1.9M', '$1,909,616' and '$1,000,000.00'.",
                                         'userImpact': 'Minor. Totals and rows look like different kinds of '
                                                       'numbers, and three precisions on one card make it '
                                                       'harder to check that rows add up.',
                                         'fixHypothesis': 'The totals seem to use an animated digit '
                                                          'component without the superscript cents of the '
                                                          'table amount component. Adding it, or documenting '
                                                          'when each applies, would likely close the gap.',
                                         'acceptance': ['Summary totals and table amounts on /transactions '
                                                        'use the same cents treatment, or the difference is '
                                                        'documented.',
                                                        'The Money in/out card shows its totals at one '
                                                        'precision.'],
                                         'verify': ['Check the design system for the money display '
                                                    'components and whether superscript cents are meant for '
                                                    'tables only.'],
                                         'searchTerms': ['superscript cents totals',
                                                         'money display component',
                                                         'Money in total format'],
                                         'impact': 'consistency',
                                         'confidence': 'medium',
                                         'confidenceReason': 'Class names and sizes captured with DevTools, '
                                                             'but the split may be deliberate.',
                                         'demoArtifactRisk': False},
 'transactions.i-zero-no-cents': {'title': 'Show zero subtotals with cents in grouped Transactions',
                                  'summary': "Grouped by Category, zero money-in subtotals render as '$0' "
                                             'with no superscript cents, while sibling subtotals such as '
                                             "'$2,608,874.96' show superscript cents.",
                                  'userImpact': "Cosmetic. '$0' breaks the column's pattern slightly but "
                                                'does not change what the admin reads.',
                                  'fixHypothesis': 'The money component likely short-circuits zero values. '
                                                   "Formatting zero as '$0.00' would likely match its "
                                                   'siblings.',
                                  'acceptance': ["Zero subtotals in grouped views render as '$0.00' with "
                                                 'superscript cents.',
                                                 'Zero and non-zero subtotals in one column use the same '
                                                 'money component.'],
                                  'verify': ['Check the money formatting rules for whether zero is written '
                                             "'$0' or '$0.00'."],
                                  'searchTerms': ['$0 no cents',
                                                  'zero subtotal format',
                                                  'grouped by category totals'],
                                  'impact': 'cosmetic',
                                  'confidence': 'high',
                                  'confidenceReason': 'Class names and missing superscript span captured '
                                                      'with DevTools.',
                                  'demoArtifactRisk': False},
 'settings.i-link-trailing-period': {'title': 'Move sentence periods out of Settings link text',
                                     'summary': 'Two Settings links include the period that ends their '
                                                "sentence: 'accounting settings.' on Categories and 'Learn "
                                                "more.' on Approval Rules > Dual admin.",
                                     'userImpact': 'Polish. The underline runs past the words onto the '
                                                   'period, which reads as sloppy, and screen readers '
                                                   'announce the period as part of the link name.',
                                     'fixHypothesis': 'Move the period out of the link element so the '
                                                      'sentence punctuation follows the link. If the link '
                                                      'text comes from a translation string, split the '
                                                      'string so the link wraps only the words.',
                                     'acceptance': ['On Settings > Categories and Approval Rules > Dual '
                                                    'admin, the link text ends with a word, not a period.',
                                                    'The sentence still ends with a period, rendered outside '
                                                    'the link.',
                                                    'Hovering the link underlines only the words.'],
                                     'verify': ["Confirm in Mercury's content style guide that punctuation "
                                                'sits outside link text (the usual rule for web links).',
                                                'Search the codebase for other link strings ending in a '
                                                'period; the sweep covered routes, not every modal or '
                                                'tooltip.'],
                                     'searchTerms': ['accounting settings.',
                                                     'Learn more.',
                                                     'link trailing period'],
                                     'impact': 'consistency',
                                     'confidence': 'high',
                                     'confidenceReason': 'Link markup captured from the live DOM; the style '
                                                         "rule itself should be confirmed against Mercury's "
                                                         'guide.',
                                     'demoArtifactRisk': False},
 'invoicing.i-link-trailing-period': {'title': "Move the period out of the 'View API docs' link",
                                      'summary': 'Invoice settings > General: the Invoicing API link reads '
                                                 "'View API docs.' with the period inside the link.",
                                      'userImpact': 'Polish. The underline runs past the words onto the '
                                                    'period, which reads as sloppy, and screen readers '
                                                    'announce the period as part of the link name.',
                                      'fixHypothesis': 'Move the period out of the link element so the '
                                                       'sentence punctuation follows the link. If the link '
                                                       'text comes from a translation string, split the '
                                                       'string so the link wraps only the words.',
                                      'acceptance': ['On Invoice settings > General, the link text ends with '
                                                     'a word, not a period.',
                                                     'The sentence still ends with a period, rendered '
                                                     'outside the link.',
                                                     'Hovering the link underlines only the words.'],
                                      'verify': ["Confirm in Mercury's content style guide that punctuation "
                                                 'sits outside link text (the usual rule for web links).',
                                                 'Search the codebase for other link strings ending in a '
                                                 'period; the sweep covered routes, not every modal or '
                                                 'tooltip.'],
                                      'searchTerms': ['View API docs.',
                                                      'Invoicing API',
                                                      'accounts_receivable'],
                                      'impact': 'consistency',
                                      'confidence': 'high',
                                      'confidenceReason': 'Link markup captured from the live DOM; the style '
                                                          "rule itself should be confirmed against Mercury's "
                                                          'guide.',
                                      'demoArtifactRisk': False},
 'payments.i-link-trailing-period': {'title': "Move the period out of the check deposit 'Help Center' link",
                                     'summary': "Check deposit guidelines end with 'Check out our Help "
                                                "Center.' where the link text includes the period.",
                                     'userImpact': 'Polish. The underline runs past the words onto the '
                                                   'period, which reads as sloppy, and screen readers '
                                                   'announce the period as part of the link name.',
                                     'fixHypothesis': 'Move the period out of the link element so the '
                                                      'sentence punctuation follows the link. If the link '
                                                      'text comes from a translation string, split the '
                                                      'string so the link wraps only the words.',
                                     'acceptance': ['On the check deposit guidelines step, the link text '
                                                    'ends with a word, not a period.',
                                                    'The sentence still ends with a period, rendered outside '
                                                    'the link.',
                                                    'Hovering the link underlines only the words.'],
                                     'verify': ["Confirm in Mercury's content style guide that punctuation "
                                                'sits outside link text (the usual rule for web links).',
                                                'Search the codebase for other link strings ending in a '
                                                'period; the sweep covered routes, not every modal or '
                                                'tooltip.'],
                                     'searchTerms': ['our Help Center.',
                                                     'More Questions? Check out',
                                                     'check deposit guidelines'],
                                     'impact': 'consistency',
                                     'confidence': 'high',
                                     'confidenceReason': 'Link markup captured from the live DOM; the style '
                                                         "rule itself should be confirmed against Mercury's "
                                                         'guide.',
                                     'demoArtifactRisk': False},
 'accounts.i-rule-cards-hidden-period': {'title': 'Remove the hidden period from auto transfer rule card '
                                                  'names',
                                         'summary': 'Each rule template card on Auto transfer rules contains '
                                                    "an invisible '.' element, so screen readers announce "
                                                    "'Distribute funds across accounts .' and the like.",
                                         'userImpact': "Screen reader users hear a stray 'dot' after every "
                                                       'card name; sighted users see nothing wrong, so it is '
                                                       'unlikely to be caught by visual QA.',
                                         'fixHypothesis': "The '.' is probably a layout spacer or leftover "
                                                          "placeholder inside the card's second paragraph. "
                                                          'Remove it, or hide it with aria-hidden if it '
                                                          'holds layout, and replace the <div> inside <p> '
                                                          'with valid markup.',
                                         'acceptance': ["The four rule cards' accessible names end with "
                                                        "their words, with no trailing ' .'.",
                                                        'The card layout is unchanged.',
                                                        'No <div> is nested inside a <p> in the card.'],
                                         'verify': ["Check in production (not just the demo) whether the '.' "
                                                    'element exists.',
                                                    'Check whether the same card component is used elsewhere '
                                                    'and has the same spacer.'],
                                         'searchTerms': ['Distribute funds across accounts',
                                                         'auto transfer rule template',
                                                         'rule card'],
                                         'impact': 'accessibility',
                                         'confidence': 'high',
                                         'confidenceReason': 'Accessible names read from the accessibility '
                                                             "tree and the element's computed opacity "
                                                             'captured from the DOM.',
                                         'demoArtifactRisk': False}}
# END CURATED


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_inputs():
    # data/*.json through the shared loader (KTD22), merged with the published accessibility snapshot
    # when there is one: A11Y_SNAPSHOT, else a11y/published/.
    areas = mapdata.load_areas()
    with open(os.path.join(ROOT, "patterns.json"), encoding="utf-8") as f:
        patterns = json.load(f)
    inputs = glob.glob(os.path.join(ROOT, "data", "*.json")) + [os.path.join(ROOT, "patterns.json")]
    mtime = max(os.path.getmtime(p) for p in inputs)
    generated = datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc).strftime("%Y-%m-%d")
    return areas, patterns, generated


def load_tombstones():
    """Ids the accessibility audit closed, from the snapshot's registry.json (KTD16); {} without a snapshot."""
    path = os.path.join(mapdata.snapshot_path(), "registry.json")
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("tombstones") or {}


def has_report():
    """True when the snapshot holds the report model that the build renders to a11y/index.html."""
    return os.path.isfile(os.path.join(mapdata.snapshot_path(), "report.json"))


class Urls:
    """Builds site and product URLs relative to one output file's directory."""

    def __init__(self, site_root, file_dir, base_url, agent_prefix=""):
        self.site_root = site_root
        self.file_dir = file_dir
        self.base = base_url.rstrip("/") if base_url else None
        self.agent_prefix = agent_prefix  # path of the agent folder from the site root, e.g. "agent/"

    def site(self, path):
        path = path.lstrip("/")
        if self.base:
            return f"{self.base}/{path}"
        rel = os.path.relpath(os.path.join(self.site_root, path), self.file_dir)
        return rel.replace(os.sep, "/")

    def viewer(self, node_id):
        return self.site("index.html") + "#" + urllib.parse.quote(node_id, safe="._-")

    def issue(self, issue_id):
        """The viewer with every screen of one issue highlighted."""
        return self.site("index.html") + "?issue=" + urllib.parse.quote(issue_id, safe="._-")

    def screenshot(self, image, size="full"):
        # screens/<area>/<name>.png -> img/<area>/<name>.<size>.webp
        m = re.match(r"screens/(.+)\.png$", image)
        stem = m.group(1) if m else os.path.splitext(image)[0]
        return self.site(f"img/{stem}.{size}.webp")

    @staticmethod
    def product(path):
        return DEMO_ORIGIN + path


# ---------------------------------------------------------------------------
# Derived fields
# ---------------------------------------------------------------------------

DEVTOOLS_RE = re.compile(
    r"DevTools|\bclass\b|_[a-zA-Z]+_[a-z0-9]{5}_\d+|rgba?\(|\b\d+(\.\d+)?px\b|aria-|\brole\b|"
    r"U\+[0-9A-Fa-f]{4}|disabled|tabindex|accessible name|<(div|button|span|h\d)\b|\bopacity\b|"
    r"getComputedStyle|computed|innerText|textContent|\bDOM\b|listbox|combobox|\bsvg\b|"
    r"snapshot|accessibility tree|accessible (button )?names?|\beval\b|Date\(\)|window\.\w+|\bx=\d|"
    r"\[(level|checked|selected|expanded)|input\[|\bh[1-6]\b|leaf (text|span)|<[A-Za-z]+ [a-z]+=|"
    r"\brect\b|font-weight",
    re.I,
)
QUOTED_RE = re.compile(r"'[^']{2,}'|\"[^\"]{2,}\"|‘[^’]{2,}’|“[^”]{2,}”|Rendered text")


def evidence_type(evidence):
    if DEVTOOLS_RE.search(evidence):
        return "devtools"
    if QUOTED_RE.search(evidence):
        return "rendered-text"
    return "observed"


def fallback_copy(issue, etype):
    """Generated copy for issues without a curated entry. Plainer, flagged as generated."""
    conf = {"devtools": "high", "rendered-text": "medium", "observed": "low"}[etype]
    return {
        "title": f"Resolve: {issue['title']}",
        "summary": issue["title"] + ".",
        "userImpact": "Not assessed. Describe the effect on admins before filing.",
        "fixHypothesis": "Not assessed. Identify the canonical pattern and propose moving the affected screens onto it.",
        "acceptance": ["The affected screens follow the documented Mercury pattern for this case."],
        "verify": [],
        "searchTerms": [issue["title"]],
        "impact": {"a11y": "accessibility", "interaction": "broken-task", "iconography": "cosmetic"}.get(issue["category"], "consistency"),
        "confidence": conf,
        "confidenceReason": f"Generated from evidence type '{etype}'; not reviewed.",
        "demoArtifactRisk": False,
    }


def priority_for(impact, severity, confidence, demo_risk=False):
    level = PRIORITY_GRID[impact][severity]
    reason = f"{impact.replace('-', ' ').capitalize()}: {IMPACT_PHRASE[impact]}. Audited severity {severity}."
    if confidence == "low" and level < 4:
        level += 1
        reason += " Lowered one step because confidence is low; raise it back if verification confirms it."
    elif demo_risk and level <= 2:
        reason += " Confirm on non-demo data first; demo data may explain it."
    return {"level": f"P{level}", "rank": level, "rationale": reason}


def build_graph(areas):
    nodes, edges, journeys = {}, {}, []
    incoming = defaultdict(list)
    outgoing = defaultdict(list)
    for a in areas:
        for n in a["nodes"]:
            nodes[n["id"]] = dict(n, area=a["area"])
        for e in a["edges"]:
            edges.setdefault((e["from"], e["to"]), e.get("label") or "")
            incoming[e["to"]].append(e["from"])
            outgoing[e["from"]].append((e["to"], e.get("label") or ""))
        for j in a.get("journeys", []):
            journeys.append(dict(j, area=a["area"]))
    return nodes, edges, incoming, outgoing, journeys


ACTION_VERBS = set(
    "Click Open Pick Enter Choose Type Select Scroll Hover Hover/ArrowRight Close Toggle Tick Check Fill "
    "Clear Save Change Press Wait Switch Upload Fix Drop Stay Replace Complete Search Expand Edit Drag "
    "Approve Add Go Keep Turn Confirm Dismiss Set Correct Customize Focus Submit Remove Delete".split())
OUTCOME_RE = re.compile(r"\b(created|listed|shows|scheduled|loads?|dismisses|appears|opens)\b")


def as_action(label):
    """Turn a recorded edge label into an instruction that reads as a step."""
    label = label.strip().rstrip(".")
    first = label.split(" ")[0].rstrip(",:")
    if first in ACTION_VERBS:
        return label
    if label.startswith("Sidebar: "):
        return f"Click {label[len('Sidebar: '):]} in the sidebar"
    if OUTCOME_RE.search(label):
        return f"Wait: {label[0].lower()}{label[1:]}"
    return f"Click {label}"


def hop_label(edges, nodes, a, b):
    label = edges.get((a, b))
    if label:
        return as_action(label)
    return f"Go to {nodes[b]['title']}"


def open_step(node):
    return (f"Open {DEMO_ORIGIN}{node['url']} (public demo, no login; the top bar should read "
            f"'Viewing as Admin', switch to Admin there if it does not).")


def reproduction(issue, nodes, edges, incoming, journeys):
    targets = issue["nodeIds"]

    # 1. A journey that reaches one of the affected screens. Prefer the first
    #    (primary) screen, then the shortest route, then a journey in the same area.
    best = None
    for rank, target in enumerate(targets):
        for j in journeys:
            if target in j["steps"]:
                idx = j["steps"].index(target)
                key = (rank, idx, j["area"] != issue["area"])
                if best is None or key < best[0]:
                    best = (key, j, idx, target)
        if best is not None and best[0][0] == rank:
            break
    if best:
        _, j, idx, target = best
        route = j["steps"][: idx + 1]
        steps = [open_step(nodes[route[0]])]
        for a, b in zip(route, route[1:]):
            steps.append(f"{hop_label(edges, nodes, a, b)}. You reach '{nodes[b]['title']}'.")
        steps.append(observe_step(issue, target, nodes))
        return {"method": "journey", "journeyId": j["id"], "journeyName": j["name"],
                "targetNodeId": target, "steps": steps}

    # 2. Shortest edge path back to a page-level screen.
    target = targets[0]
    prev = {target: None}
    queue = deque([target])
    start = None
    while queue:
        cur = queue.popleft()
        if cur != target and nodes[cur]["kind"] == "page":
            start = cur
            break
        for p in incoming.get(cur, []):
            if p not in prev and p in nodes:
                prev[p] = cur
                queue.append(p)
    if start:
        route = [start]
        while route[-1] != target:
            route.append(prev[route[-1]])
        steps = [open_step(nodes[start])]
        for a, b in zip(route, route[1:]):
            steps.append(f"{hop_label(edges, nodes, a, b)}. You reach '{nodes[b]['title']}'.")
        steps.append(observe_step(issue, target, nodes))
        return {"method": "edge-path", "journeyId": None, "journeyName": None,
                "targetNodeId": target, "steps": steps}

    # 3. Product URL only.
    n = nodes[target]
    return {"method": "url", "journeyId": None, "journeyName": None, "targetNodeId": target,
            "steps": [open_step(n), f"Find '{n['title']}': {n['note']}", observe_step(issue, target, nodes)]}


def observe_step(issue, target, nodes):
    others = [nid for nid in issue["nodeIds"] if nid != target]
    s = f"Compare what you see with the audit screenshot of '{nodes[target]['title']}' and the evidence."
    if others:
        field = "affectedScreenIds" if len(issue["nodeIds"]) > SCREEN_CAP else "affectedScreens"
        s += f" The finding spans {len(others)} other screen{'s' if len(others) != 1 else ''}, listed in {field}."
    return s


def screen_ref(node, urls):
    return {
        "nodeId": node["id"],
        "title": node["title"],
        "kind": node["kind"],
        "urlPath": node["url"],
        "productUrl": urls.product(node["url"]),
        "screenshot": urls.screenshot(node["image"], "full"),
        "thumbnail": urls.screenshot(node["image"], "thumb"),
        "viewer": urls.viewer(node["id"]),
        "note": node["note"],
    }


def verify_list(issue, copy, pattern_refs):
    audit_date = (issue.get("audit") or {}).get("date")
    seen = (f"The accessibility audit checked the public demo on {audit_date}" if audit_date
            else f"The audit captured the public demo on {CAPTURED}")
    items = [
        f"Reproduce it on the current product in a logged-in browser. {seen}; the UI may have changed since.",
        f"Search the tracker for an existing ticket: first the dedupe key, then the searchTerms. If one exists, add this evidence as a comment instead of filing a new ticket.",
        IMPACT_CHECKS.get(copy["impact"]) or CATEGORY_CHECKS.get(issue["category"], CATEGORY_CHECKS["component"]),
    ]
    items += copy["verify"]
    if copy["demoArtifactRisk"] and not any("demo" in v.lower() for v in copy["verify"]):
        items.append("Rule out demo artifacts: check whether the demo's seeded data, fixed clock or sandbox behavior explains it, and confirm on a real (non-demo) account or staging.")
    if pattern_refs:
        names = ", ".join(f"{p['id']} ({p['title']})" for p in pattern_refs)
        items.append(f"Part of pattern {names}. Check whether an epic for it exists and file this as a child ticket under it.")
    return items


def md_escape(s):
    return s.replace("|", "\\|").replace("\n", " ")


def audit_section(r, urls):
    """The '## Accessibility audit' lines of an audited issue's ticket (KTD23)."""
    a = r["audit"]
    criteria = a.get("criteria") or []
    names = ", ".join(f"[{c['number']} {c['name']}]({c['url']})" if c.get("url") else f"{c['number']} {c['name']}"
                      for c in criteria)
    reason = a.get("reason")
    status = a.get("status")
    audited = f"- Audited: {a.get('date')}, demo build `{a.get('build')}`, run `{a.get('run')}`."
    if status:
        audited += f" Status: {STATUS_PHRASE.get(status, status)}."
    if status == "not-retested":
        audited += " The latest run could not retest it; these are its last verified results."
    checked = ", ".join(f"[`{s}`]({urls.viewer(s)})" for s in a.get("checkedStates") or [])
    lines = [
        "## Accessibility audit",
        f"- WCAG 2.2 success criteri{'on' if len(criteria) == 1 else 'a'}: {names}",
        f"- Check: {a.get('family')}",
        f"- Verdict: {VERDICT_PHRASE.get(a.get('verdict'), a.get('verdict'))}." + (f" {reason}" if reason else ""),
        audited,
        f"- Screens the verifier checked: {checked or 'none'}",
    ]
    if r.get("history"):
        lines.append(f"- Rating history: {r['history']}")
    return lines + [""]


def ticket_markdown(r, urls):
    """The ticket body for one record as the writers emit it: capped records list TICKET_SCREEN_ROWS screens."""
    evidence = (r.get("audit") or {}).get("evidence") or r["evidence"]
    lines = [
        f"# {r['title']}",
        "",
        f"> Found in a black-box UX audit of the public Mercury demo (audit id `{r['id']}`). Filed after the checks in Verification below.",
        "",
        "## Summary",
        r["summary"],
        "",
        "## Problem",
        r["auditDetail"],
        "",
        f"**Why it matters:** {r['userImpact']}",
        "",
        "## Evidence (verbatim from the audit)",
        "> " + evidence.replace("\n", "\n> "),
        "",
        f"Evidence type: {r['evidenceType']}. Confidence: {r['confidence']}. {r['confidenceReason']}",
        "",
    ]
    if r.get("audit"):
        lines += audit_section(r, urls)
    lines += [
        "## Affected screens",
        "| Screen | Product path | Screenshot | Audit viewer |",
        "|---|---|---|---|",
    ]
    total = r["affectedScreenCount"]
    shown = r["affectedScreens"][:TICKET_SCREEN_ROWS] if total > SCREEN_CAP else r["affectedScreens"]
    for s in shown:
        lines.append(f"| {md_escape(s['title'])} | `{md_escape(s['urlPath'])}` | [screenshot]({s['screenshot']}) | [open]({s['viewer']}) |")
    if total > SCREEN_CAP:
        lines += ["", f"{len(shown)} of {total} screens listed, and {total - len(shown)} more: "
                      f"[show all {total} screens]({urls.issue(r['id'])}) in the audit viewer."]
    lines += ["", "## Steps to reproduce"]
    lines += [f"{i}. {step}" for i, step in enumerate(r["reproduce"]["steps"], 1)]
    lines += ["", "## Suggested fix (hypothesis)", r["fixHypothesis"], "", "## Acceptance criteria"]
    lines += [f"- [ ] {a}" for a in r["acceptanceCriteria"]]
    lines += ["", "## Verification (done before filing)"]
    lines += [f"- [ ] {v}" for v in r["verifyBeforeFiling"]]
    lines += ["", "Verification notes: _name the internal guideline, spec or owner that confirmed this, or say why it is filed unconfirmed._"]
    lines += ["", "---",
              f"Severity (audited): {r['severity']} · Suggested priority: {r['priority']['level']}, {r['priority']['rationale']}",
              f"Area: {r['areaTitle']} · Category: {r['category']} · Impact: {r['impact']}"]
    if r["patterns"]:
        lines.append("Pattern: " + ", ".join(f"{p['id']} ({p['title']})" for p in r["patterns"]))
    lines.append(f"Dedupe key: `{r['dedupeKey']}`")
    return "\n".join(lines)


def stub_markdown(iid, tomb, urls):
    """The stub at tickets/<id>.md for an id the accessibility audit closed, so tracker links keep working."""
    since = f"No longer open since run {tomb.get('run')}" + (f" ({tomb['date']})" if tomb.get("date") else "") + "."
    reason, hid = tomb.get("reason"), tomb.get("attachedTo")
    if reason == "attached" and hid:
        why = (f"Reason: attached. It matched the hand-found issue `{hid}`, which now carries it. "
               f"Use [its ticket]({urls.site(f'{urls.agent_prefix}tickets/{hid}.md')}) instead.")
    else:
        why = f"Reason: {TOMBSTONE_REASON.get(reason, f'{reason}.')}"
    lines = [f"# No longer open: {iid}", "",
             f"> {since} The accessibility audit closed this issue (audit id `{iid}`). Do not file it.", "", why]
    if reason in ("fixed", "withdrawn"):
        lines += ["", "If a ticket was filed from it, check the current product, then close that ticket with this reason."]
    lines += ["", "---", f"Dedupe key: `{AUDIT_SLUG}:{iid}`"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

REQUIRED_ISSUE_FIELDS = [
    "id", "dedupeKey", "title", "auditTitle", "summary", "problem", "auditDetail", "userImpact",
    "evidence", "evidenceType", "severity", "priority", "impact", "category", "area", "areaTitle",
    "patterns", "confidence", "confidenceReason", "demoArtifactRisk", "affectedScreens",
    "affectedScreenCount", "affectedScreenIds", "reproduce",
    "fixHypothesis", "acceptanceCriteria", "labels", "searchTerms", "verifyBeforeFiling",
    "ticketUrl", "curated",
]
# Only on issues the accessibility audit verified (KTD23); `history` only on hand-found ones it re-rated or dropped.
AUDIT_FIELDS = ["wcag", "audit", "history"]
# Fields allowed to be empty lists or false.
MAY_BE_EMPTY = {"patterns", "demoArtifactRisk", "curated"}


def capped(r):
    """The record as issues.json, issues.jsonl and its ticket write it (KTD23): an issue on more than
    SCREEN_CAP screens keeps its first SCREEN_CAP affectedScreens; affectedScreenIds keeps them all."""
    if len(r["affectedScreens"]) <= SCREEN_CAP:
        return r
    return dict(r, affectedScreens=r["affectedScreens"][:SCREEN_CAP])


def build_issues(areas, patterns, nodes, edges, incoming, journeys, urls):
    issue_patterns = defaultdict(list)
    for p in patterns:
        for iid in p["issueIds"]:
            issue_patterns[iid].append({"id": p["id"], "title": p["title"]})
    area_titles = {a["area"]: a["title"] for a in areas}

    records, warnings = [], []
    for a in areas:
        for issue in a["issues"]:
            issue = dict(issue, area=a["area"])
            audit = issue.get("audit")
            # Generated accessibility issues come only from the sweep's measurements.
            etype = "devtools" if audit and issue.get("copy") else evidence_type(issue["evidence"])
            # Generated accessibility issues carry copy built from templates and their facts (KTD23).
            copy = issue.get("copy") or CURATED.get(issue["id"])
            curated = copy is not None
            if not curated:
                warnings.append(f"no curated copy for {issue['id']}; using generated fallback")
                copy = fallback_copy(issue, etype)
            wcag = (list(issue.get("wcag") or [c["number"] for c in audit.get("criteria") or []])
                    if audit else [])
            pats = issue_patterns.get(issue["id"], [])
            prio = priority_for(copy["impact"], issue["severity"], copy["confidence"], copy["demoArtifactRisk"])
            labels = (["source:ux-audit"] + ([A11Y_SOURCE_LABEL] if audit else []) +
                      [AUDIT_LABEL, f"area:{a['area']}", f"category:{issue['category']}", f"impact:{copy['impact']}"] +
                      [f"wcag:{n}" for n in wcag] +
                      ["needs-verification"] + [f"pattern:{p['id']}" for p in pats])
            r = {
                "id": issue["id"],
                "dedupeKey": f"{AUDIT_SLUG}:{issue['id']}",
                "title": copy["title"],
                "auditTitle": issue["title"],
                "summary": copy["summary"],
                "problem": f"{issue['detail']}\n\nWhy it matters: {copy['userImpact']}",
                "auditDetail": issue["detail"],
                "userImpact": copy["userImpact"],
                "evidence": issue["evidence"],
                "evidenceType": etype,
                "severity": issue["severity"],
                "priority": prio,
                "impact": copy["impact"],
                "category": issue["category"],
                "area": a["area"],
                "areaTitle": area_titles[a["area"]],
                "patterns": pats,
                "confidence": copy["confidence"],
                "confidenceReason": copy["confidenceReason"],
                "demoArtifactRisk": bool(copy["demoArtifactRisk"]),
                "affectedScreens": [screen_ref(nodes[n], urls) for n in issue["nodeIds"]],
                "affectedScreenCount": len(issue["nodeIds"]),
                "affectedScreenIds": list(issue["nodeIds"]),
                "reproduce": reproduction(issue, nodes, edges, incoming, journeys),
                "fixHypothesis": copy["fixHypothesis"],
                "acceptanceCriteria": list(copy["acceptance"]),
                "labels": labels,
                "searchTerms": list(copy["searchTerms"]),
                "verifyBeforeFiling": verify_list(issue, copy, pats),
                "curated": curated,
            }
            r["ticketUrl"] = urls.site(f"{urls.agent_prefix}tickets/{issue['id']}.md")
            if audit:
                r["wcag"] = wcag
                r["audit"] = audit
            if issue.get("history"):
                r["history"] = issue["history"]
            records.append({k: r[k] for k in REQUIRED_ISSUE_FIELDS + AUDIT_FIELDS if k in r})
    known = {r["id"] for r in records}
    for iid in CURATED:
        if iid not in known:
            warnings.append(f"curated copy for unknown issue {iid} (issue removed or renamed?)")
    records.sort(key=lambda r: (r["priority"]["rank"], IMPACTS.index(r["impact"]),
                                SEVERITIES.index(r["severity"]), r["area"], r["id"]))
    return records, warnings


def validate_issues(records):
    errors = []
    for r in records:
        for k in REQUIRED_ISSUE_FIELDS:
            v = r.get(k)
            if k in MAY_BE_EMPTY:
                continue
            if v is None or v == "" or v == []:
                errors.append(f"{r['id']}: empty {k}")
        if r["severity"] not in SEVERITIES:
            errors.append(f"{r['id']}: bad severity {r['severity']}")
        if r["impact"] not in IMPACTS:
            errors.append(f"{r['id']}: bad impact {r['impact']}")
        if r["confidence"] not in CONFIDENCES:
            errors.append(f"{r['id']}: bad confidence {r['confidence']}")
        if len(r["title"]) > 80:
            errors.append(f"{r['id']}: title longer than 80 characters")
    ids = [r["id"] for r in records]
    for iid, n in Counter(ids).items():
        if n > 1:
            errors.append(f"duplicate issue id {iid}")
    return errors


def build_patterns(patterns, records, areas):
    by_id = {r["id"]: r for r in records}
    out = []
    for p in patterns:
        members = [by_id[i] for i in p["issueIds"] if i in by_id]
        members.sort(key=lambda r: (r["priority"]["rank"], r["id"]))
        epic = EPICS.get(p["id"], {
            "epicTitle": p["title"],
            "goal": p["summary"],
            "approach": "Agree the canonical pattern, then file one child ticket per area or surface.",
            "doneWhen": "Every member issue is resolved or closed as intended behavior.",
        })
        top = min((r["priority"]["rank"] for r in members), default=4)
        out.append({
            "id": p["id"],
            "title": p["title"],
            "summary": p["summary"],
            "areas": p["areas"],
            "issueIds": [r["id"] for r in members],
            "issueCount": len(members),
            "severityCounts": dict(Counter(r["severity"] for r in members)),
            "priorityCounts": dict(sorted(Counter(r["priority"]["level"] for r in members).items())),
            "suggestedEpic": {
                "title": epic["epicTitle"],
                "suggestedPriority": f"P{top}",
                "priorityRationale": f"Highest suggested priority among its {len(members)} member issues.",
                "goal": epic["goal"],
                "approach": epic["approach"],
                "doneWhen": epic["doneWhen"],
                "labels": ["source:ux-audit", AUDIT_LABEL, f"pattern:{p['id']}", "epic", "needs-verification"],
                "dedupeKey": f"{AUDIT_SLUG}:{p['id']}",
                "childTickets": [{"id": r["id"], "title": r["title"], "area": r["area"],
                                  "priority": r["priority"]["level"]} for r in members],
            },
        })
    in_pattern = {i for p in patterns for i in p["issueIds"]}
    area_titles = {a["area"]: a["title"] for a in areas}
    ungrouped = defaultdict(list)
    for r in records:
        if r["id"] not in in_pattern:
            ungrouped[r["area"]].append(r)
    standalone = []
    for area in sorted(ungrouped):
        rs = sorted(ungrouped[area], key=lambda r: (r["priority"]["rank"], r["id"]))
        standalone.append({
            "area": area,
            "areaTitle": area_titles[area],
            "suggestedParent": f"{area_titles[area]}: UX audit follow-ups",
            "note": "Not part of a cross-area pattern. File individually, optionally under one parent per area.",
            "issueIds": [r["id"] for r in rs],
            "issueCount": len(rs),
        })
    return out, standalone


def build_screens(areas, nodes, outgoing, journeys, records, urls):
    issue_ids = defaultdict(list)
    for r in records:
        for nid in r["affectedScreenIds"]:
            issue_ids[nid].append(r["id"])
    journey_ids = defaultdict(list)
    for j in journeys:
        for s in dict.fromkeys(j["steps"]):
            journey_ids[s].append(j["id"])
    screens = []
    for a in areas:
        for n in a["nodes"]:
            ref = screen_ref(nodes[n["id"]], urls)
            ref["area"] = a["area"]
            ref["issueIds"] = sorted(issue_ids.get(n["id"], []))
            ref["journeyIds"] = journey_ids.get(n["id"], [])
            ref["leadsTo"] = [{"nodeId": t, "action": lbl} for t, lbl in outgoing.get(n["id"], [])]
            screens.append(ref)
    return screens


def build_journeys(journeys, nodes, edges, records, urls):
    touching = defaultdict(set)
    for r in records:
        for nid in r["affectedScreenIds"]:
            touching[nid].add(r["id"])
    out = []
    for j in journeys:
        steps = []
        prev = None
        for sid in j["steps"]:
            n = nodes[sid]
            steps.append({
                "nodeId": sid,
                "title": n["title"],
                "kind": n["kind"],
                "urlPath": n["url"],
                "action": None if prev is None else hop_label(edges, nodes, prev, sid),
                "screenshot": urls.screenshot(n["image"], "full"),
                "viewer": urls.viewer(sid),
            })
            prev = sid
        issues = sorted({i for s in j["steps"] for i in touching.get(s, ())})
        out.append({
            "id": j["id"],
            "area": j["area"],
            "name": j["name"],
            "description": j["description"],
            "startUrl": DEMO_ORIGIN + nodes[j["steps"][0]]["url"],
            "stepCount": len(steps),
            "steps": steps,
            "issueIds": issues,
        })
    return out


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def readme(meta, records, patterns_out, standalone, example):
    prio = Counter(r["priority"]["level"] for r in records)
    conf = Counter(r["confidence"] for r in records)
    sev = Counter(r["severity"] for r in records)
    in_patterns = sum(p["issueCount"] for p in patterns_out)
    unique_in_patterns = len({i for p in patterns_out for i in p["issueIds"]})
    grid_rows = "\n".join(
        f"| {imp} | {' | '.join('P' + str(PRIORITY_GRID[imp][s]) for s in SEVERITIES)} |" for imp in IMPACTS)
    pattern_rows = "\n".join(
        f"| `{p['id']}` | {p['suggestedEpic']['title']} | {p['issueCount']} | {p['suggestedEpic']['suggestedPriority']} |"
        for p in patterns_out)
    return f"""# Mercury Admin UX audit: data for agents

> **Read this first.** {CAVEAT}
>
> Treat every record as a lead. Nothing here should reach a tracker until someone with access to Mercury's internal docs, or an agent working in their logged-in browser, has checked it.

## What this is

A black-box UX and design-system audit of demo.mercury.com in the Admin view, captured on {CAPTURED} ({CAPTURE_SETUP}). It covers {meta['counts']['screens']} screens (pages, modals, drawers, menus, form states, validation errors, confirmations), {meta['counts']['journeys']} user journeys and {meta['counts']['issues']} issues. {unique_in_patterns} of the issues roll up into {meta['counts']['patterns']} cross-area patterns; the other {meta['counts']['issues'] - unique_in_patterns} are specific to one area.

Each issue is exported as a ticket-ready record with a stable id, evidence copied verbatim from the audit, affected screens with screenshots, reproduction steps, a suggested priority, a fix hypothesis, acceptance criteria, and a checklist of what to verify before filing. The interactive viewer (`../index.html`) shows the same data on a map; `../index.html#<nodeId>` opens one screen.

Counts: severity (as audited) high {sev['high']}, medium {sev['medium']}, low {sev['low']}. Suggested priority P1 {prio['P1']}, P2 {prio['P2']}, P3 {prio['P3']}, P4 {prio['P4']}. Confidence high {conf['high']}, medium {conf['medium']}, low {conf['low']}.

## Files

| File | What it holds |
|---|---|
| `llms.txt` | Short index of these files (a copy also sits at the site root) |
| `README.md`, `AGENTS.md` | This document (identical) |
| `patterns.json` | {meta['counts']['patterns']} cross-area patterns, each framed as an epic with its child issues, plus area-specific issues grouped by area |
| `issues-index.json` | One short row per issue (id, title, priority, severity, impact, confidence, area, patterns, WCAG criteria for audited accessibility issues, screen count, ticket link). Small enough to read whole; start triage here |
| `issues.json` | All {meta['counts']['issues']} full issue records under `issues`, with `meta` (caveat, counts, schema version). Over 1 MB; prefer the index plus single tickets if your context is limited |
| `issues.jsonl` | The same records, one JSON object per line, no meta. Use it with `grep` or `jq` |
| `tickets/<id>.md` | Ready-to-paste Markdown ticket body for one issue, following the template below. An id the accessibility audit closed keeps a short stub that says since which run and why, so tracker links keep working |
| `screens.json` | Every captured screen: path, screenshot, viewer link, issues on it, journeys through it, outgoing actions |
| `journeys.json` | The {meta['counts']['journeys']} journeys with ordered steps and the action that leads to each step |

URLs: `productUrl` and `startUrl` point at the live public demo. `screenshot`, `thumbnail` and `viewer` point at this site. {"They are absolute." if meta['baseUrl'] else "They are relative to the file that contains them (resolve them against that file's URL, as a browser would)."} Screenshots are lossless WebP at 2880x1800; thumbnails are 800px wide.

## Recommended workflow

1. **Read `patterns.json` first**, then `issues-index.json`. {unique_in_patterns} issues belong to a pattern. File one epic per pattern (use `suggestedEpic`) and the member issues as child tickets, instead of {meta['counts']['issues']} loose tickets. Area-specific issues are listed under `standalone`.
2. **Pick by priority.** `issues.json` and `issues-index.json` are sorted by suggested priority, then impact (data first), then severity. Start with P1 and P2. The rubric below explains the grid; override it when verification shows a different impact.
3. **Verify each issue before filing.** Work through `verifyBeforeFiling`. It always includes: reproduce on the current product in a logged-in browser, search for duplicates, and check the relevant internal guideline (content style guide, component library, tokens, accessibility standard). If the guideline allows the observed behavior, or the owning team confirms it is intentional, do not file; record that outcome where your team tracks audit triage.
4. **Dedupe.** Search the tracker for `dedupeKey` first (every ticket filed from this audit should contain it), then for each phrase in `searchTerms`. If a ticket exists, comment with the new evidence and screenshots instead of filing.
5. **File.** Use `title` as the ticket title and the Markdown at `ticketUrl` (`tickets/<id>.md`, minus its first heading line) as the body, or build the body from the template below.{"" if meta['baseUrl'] else " Links in this export are relative: turn them into absolute URLs before pasting, or trackers will show broken links."} Apply `labels`, map `priority.level` to your tracker's priority, and attach or link `affectedScreens[].screenshot`. Fill in the Verification notes line with what confirmed the issue. Keep `dedupeKey` in the body.
6. **Link.** Attach each child ticket to its pattern epic. Link the viewer URL so reviewers can see the screen in context.
7. **Leave a trail.** Replace the `needs-verification` label with `verified` (or your tracker's equivalent) once verification is done, so other agents can tell verified tickets from raw leads.

Linear mapping: `title` to title, the ticket Markdown to description, `priority.level` P1-P4 to Urgent, High, Medium, Low, `labels` to labels (create missing ones in a group such as `ux-audit`), epics as parent issues or a project, screenshots as attachments or links.

## Issue record schema (`issues.json` / `issues.jsonl`)

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Stable audit id, `<area>.i-<slug>`. Never changes between exports |
| `dedupeKey` | string | `{AUDIT_SLUG}:<id>`. Put it in every ticket body so later agents can find it |
| `title` | string | Imperative, ticket-style title |
| `auditTitle` | string | The auditor's original descriptive title |
| `summary` | string | One sentence stating what was observed |
| `problem` | string | `auditDetail` followed by why it matters |
| `auditDetail` | string | The auditor's full description, verbatim |
| `userImpact` | string | The concrete consequence for an admin |
| `evidence` | string | Verbatim evidence from the audit: rendered text, computed styles, DOM attributes |
| `evidenceType` | `devtools` \\| `rendered-text` \\| `observed` | Strongest kind of evidence, detected from the evidence text: `devtools` has measured values (computed styles, classes, DOM or accessibility-tree attributes, clock, geometry); `rendered-text` quotes on-screen text verbatim; `observed` describes behavior or visuals without either. Generated accessibility issues are always `devtools`: the sweep measured them |
| `severity` | `high` \\| `medium` \\| `low` | As audited |
| `priority` | object | `level` (P1-P4), `rank` (1-4), `rationale` (one line) |
| `impact` | enum | `data-integrity`, `broken-task`, `accessibility`, `consistency`, `cosmetic`. Drives priority |
| `category` | string | Audit category: copy, data-format, component, interaction, layout, a11y, color, typography, iconography |
| `area`, `areaTitle` | string | Product area |
| `patterns` | array | `{{id, title}}` of the cross-area patterns this issue belongs to (may be empty) |
| `confidence` | `high` \\| `medium` \\| `low` | How sure we are the finding is real and reproducible as described |
| `confidenceReason` | string | What the confidence rests on |
| `demoArtifactRisk` | boolean | True when demo data, the demo clock or the sandbox could explain it |
| `affectedScreens` | array | `{{nodeId, title, kind, urlPath, productUrl, screenshot, thumbnail, viewer, note}}`; the first is the primary screen. An issue on more than {SCREEN_CAP} screens lists its first {SCREEN_CAP} here |
| `affectedScreenCount` | number | How many screens the issue is on, always the full count |
| `affectedScreenIds` | string[] | The `nodeId` of every affected screen, primary first, always the full list. `screens.json` and `journeys.json` use it |
| `reproduce` | object | `method` (`journey`, `edge-path` or `url`), `journeyId`, `journeyName`, `targetNodeId`, `steps[]` |
| `fixHypothesis` | string | Suggested direction, phrased as a hypothesis |
| `acceptanceCriteria` | string[] | Observable conditions that are true when the issue is fixed |
| `labels` | string[] | Suggested labels, including `needs-verification`. Audited accessibility issues add `source:a11y-audit` and one `wcag:<number>` per success criterion |
| `searchTerms` | string[] | Phrases for duplicate search |
| `verifyBeforeFiling` | string[] | Checklist to complete before filing |
| `ticketUrl` | string | Link to `tickets/<id>.md`, the ready-to-paste ticket body built from the fields above |
| `curated` | boolean | True when the copy was written for this issue, by hand or by the accessibility audit from its templates and measured facts; false when generated as a fallback |
| `wcag` | string[] | Audited accessibility issues only. The WCAG 2.2 success criteria the issue is about, by number (`1.4.3`) |
| `audit` | object | Audited accessibility issues only: generated ones and re-verified hand-found ones. `criteria[]` {{`number`, `name`, `url` (the W3C Understanding page)}}, `family` (the check, such as `contrast` or `keyboard`), `verdict` (`confirm`, `rerate` or `drop`), `reason` (the verifier's), `date` (the audit date), `build` (the demo build hash), `evidence` (the full live evidence over several lines: measured values, accessibility-tree attributes or focus trace, then the verifier's reason), `checkedStates[]` (the screens the verifier checked), `run`, `status` (`new`, `persisting`, `re-rated`, `reappeared` or `not-retested` for generated issues; `first-audited`, `confirmed`, `re-rated`, `not-reproduced` or `not-retested` for hand-found ones). With `not-retested`, `date` and `evidence` are from the last run that verified it |
| `history` | string | Hand-found issues the accessibility audit re-rated or did not reproduce: `rated high on <date>, previously medium`, or `not reproduced on <date>: <reason>`. A hand-found issue it did not reproduce stays open with its original copy |

`reproduce.method`: `journey` means the steps follow a recorded journey that reaches the screen, with the recorded action between each step. `edge-path` means the shortest recorded route from a page to the screen. `url` means only the product path is known.

## Pattern schema (`patterns.json`)

`patterns[]`: `id`, `title`, `summary`, `areas`, `issueIds`, `issueCount`, `severityCounts`, `priorityCounts`, `suggestedEpic` {{`title`, `suggestedPriority`, `priorityRationale`, `goal`, `approach`, `doneWhen`, `labels`, `dedupeKey`, `childTickets[]` {{`id`, `title`, `area`, `priority`}}}}.

`standalone[]`: issues outside any pattern, per area: `area`, `areaTitle`, `suggestedParent`, `note`, `issueIds`, `issueCount`.

| Pattern | Suggested epic | Issues | Priority |
|---|---|---|---|
{pattern_rows}

## Screen and journey schemas

`screens.json` `screens[]`: `nodeId`, `title`, `kind` (page, modal, drawer, menu, popover, form-state, validation-error, toast, confirmation, empty-state), `urlPath`, `productUrl`, `screenshot`, `thumbnail`, `viewer`, `note`, `area`, `issueIds`, `journeyIds`, `leadsTo[]` {{`nodeId`, `action`}}. Several screens share one `urlPath` because modals and states do not change the URL; use `leadsTo` actions or a journey to reach them.

`journeys.json` `journeys[]`: `id`, `area`, `name`, `description`, `startUrl`, `stepCount`, `steps[]` {{`nodeId`, `title`, `kind`, `urlPath`, `action` (what to do on the previous step to get here), `screenshot`, `viewer`}}, `issueIds` (issues on any step).

## Ticket template

Use this when you build the body yourself. The files in `tickets/` already follow it. The Accessibility audit section appears only on issues with an `audit`, and its Rating history line only with a `history`.

```markdown
> Found in a black-box UX audit of the public Mercury demo (audit id `<id>`). Filed after the checks in Verification below.

## Summary
<summary>

## Problem
<auditDetail>

**Why it matters:** <userImpact>

## Evidence (verbatim from the audit)
> <evidence, or audit.evidence for audited accessibility issues>

Evidence type: <evidenceType>. Confidence: <confidence>. <confidenceReason>

## Accessibility audit
- WCAG 2.2 success criterion: [<audit.criteria[].number> <name>](<url>)
- Check: <audit.family>
- Verdict: <confirmed, re-rated or not reproduced>. <audit.reason>
- Audited: <audit.date>, demo build `<audit.build>`, run `<audit.run>`. Status: <audit.status>.
- Screens the verifier checked: <audit.checkedStates>
- Rating history: <history>

## Affected screens
| Screen | Product path | Screenshot | Audit viewer |
|---|---|---|---|
| <title> | `<urlPath>` | [screenshot](<screenshot>) | [open](<viewer>) |

<for an issue on more than {SCREEN_CAP} screens: the first {TICKET_SCREEN_ROWS} rows, then "{TICKET_SCREEN_ROWS} of <affectedScreenCount> screens listed, and <the rest> more", with a link to the viewer's ?issue=<id> view>

## Steps to reproduce
1. <reproduce.steps[0]>
2. ...

## Suggested fix (hypothesis)
<fixHypothesis>

## Acceptance criteria
- [ ] <acceptanceCriteria[0]>

## Verification (done before filing)
- [x] <verifyBeforeFiling items, ticked as completed>

Verification notes: <which internal guideline, spec or owner confirmed it>

---
Severity (audited): <severity> · Suggested priority: <priority.level>, <priority.rationale>
Area: <areaTitle> · Category: <category> · Impact: <impact>
Pattern: <patterns>
Dedupe key: `<dedupeKey>`
```

## Triage rubric

Suggested priority comes from impact and audited severity:

| Impact | high | medium | low |
|---|---|---|---|
{grid_rows}

- If confidence is `low`, the suggested priority is lowered one step. Raise it back once verification confirms the issue.
- If `demoArtifactRisk` is true on a P1 or P2, the rationale says so: confirm on non-demo data before treating it as urgent.
- Within one priority, records are ordered data-integrity, broken-task, accessibility, consistency, cosmetic.
- **data-integrity**: the UI shows wrong, contradictory or lost data, or changes data the user did not ask to change. These come first; in a banking product a wrong number or a silently cleared field costs more trust than any visual inconsistency.
- **broken-task**: a flow blocks, misleads or loses work (Back does nothing, Escape discards a draft, a destructive action has no confirmation, a list does not refresh).
- **accessibility**: missing names, roles or heading semantics, or fake-disabled states. Check Mercury's accessibility commitments; legal or contractual requirements can raise these.
- **consistency**: the same thing looks or reads differently across screens. Usually fixed once in a shared component or guideline, so file under the pattern epic.
- **cosmetic**: visual polish with little effect on comprehension. Batch these.

Adjust after verification:

- Close without filing when the internal guideline allows the behavior or the owner confirms it is intentional.
- Lower by one when it only reproduces with demo data, or only on a surface with very low traffic (check analytics if you have access).
- Raise by one when it affects money movement, permissions or compliance copy, or when many customers hit the surface daily.
- Merge into the pattern epic when the fix is clearly one shared component or formatter.

## Example record

```json
{json.dumps(example, indent=2, ensure_ascii=False)}
```

Generated {meta['generatedAt']} from the audit manifests, schema version {SCHEMA_VERSION}.
"""


def llms_txt(urls, meta, prefix, report=False):
    """report: the build writes the accessibility report (a11y/index.html) because a snapshot exists."""
    def link(name):
        return urls.site(f"{prefix}{name}") if prefix or urls.base else name

    viewer = urls.site("index.html")
    report_line = (f"- [Accessibility report]({urls.site('a11y/index.html')}): the latest WCAG 2.2 AA audit, with "
                   "coverage, changes since the previous run and a priority fix list; each finding links to its "
                   "screens on the map\n") if report else ""
    return f"""# Mercury Admin UX audit

> Black-box UX and design-system audit of the public demo at demo.mercury.com (Admin view): {meta['counts']['screens']} screens, {meta['counts']['journeys']} journeys and {meta['counts']['issues']} issues in {meta['counts']['patterns']} cross-area patterns. Findings are hypotheses to verify against Mercury's internal conventions, not confirmed defects.

The auditor had no access to Mercury's design system, component library, content style guide, analytics or roadmap. Some findings may be intentional, demo-data artifacts or already known. Verify each one, in a logged-in browser or with a Mercury employee, before filing a ticket. Start with the agent README: it has the schemas, the filing workflow, a ticket template and a triage rubric.

## Agent data

- [Agent README]({link('README.md')}): caveat, file schemas, workflow, ticket template, triage rubric
- [Patterns]({link('patterns.json')}): {meta['counts']['patterns']} cross-area patterns framed as epics, with member issue ids
- [Issue index]({link('issues-index.json')}): one short row per issue with priority, confidence, patterns and a link to its ticket body
- [Issues, JSON]({link('issues.json')}): {meta['counts']['issues']} full ticket-ready issue records, sorted by suggested priority
- [Issues, JSONL]({link('issues.jsonl')}): the same records, one per line
- [Screens]({link('screens.json')}): every captured screen with product path, screenshot and viewer link
- [Journeys]({link('journeys.json')}): {meta['counts']['journeys']} user journeys with ordered steps and actions
- Ticket bodies: `{link('tickets/x.md')[:-4]}<issue id>.md`, one ready-to-paste Markdown ticket per issue (ids are in the index)

## Optional

- [Interactive viewer]({viewer}): pan-and-zoom map of all screens; append #<nodeId> to open one screen
- [AGENTS.md]({link('AGENTS.md')}): same content as the agent README
{report_line}"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

EXAMPLE_ID = "payments.i-edit-wipes-due-dates"


def index_row(r):
    return {
        "id": r["id"],
        "title": r["title"],
        "priority": r["priority"]["level"],
        "severity": r["severity"],
        "impact": r["impact"],
        "confidence": r["confidence"],
        "area": r["area"],
        "category": r["category"],
        "patterns": [p["id"] for p in r["patterns"]],
        **({"wcag": r["wcag"]} if "wcag" in r else {}),
        "demoArtifactRisk": r["demoArtifactRisk"],
        "screens": r["affectedScreenCount"],
        "ticketUrl": r["ticketUrl"],
    }


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="output folder, e.g. dist/agent")
    ap.add_argument("--base-url", default=None, help="public site root; makes site URLs absolute")
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out)
    site_root = os.path.dirname(out_dir)
    prefix = os.path.relpath(out_dir, site_root).replace(os.sep, "/") + "/"
    os.makedirs(out_dir, exist_ok=True)

    areas, patterns, generated = load_inputs()
    nodes, edges, incoming, outgoing, journeys = build_graph(areas)
    urls = Urls(site_root, out_dir, args.base_url, prefix)
    tickets_dir = os.path.join(out_dir, "tickets")
    ticket_urls = Urls(site_root, tickets_dir, args.base_url, prefix)

    records, warnings = build_issues(areas, patterns, nodes, edges, incoming, journeys, urls)
    errors = validate_issues(records)
    # Same records with links relative to tickets/, for the Markdown bodies.
    ticket_records, _ = build_issues(areas, patterns, nodes, edges, incoming, journeys, ticket_urls)
    patterns_out, standalone = build_patterns(patterns, records, areas)
    screens = build_screens(areas, nodes, outgoing, journeys, records, urls)
    journeys_out = build_journeys(journeys, nodes, edges, records, urls)

    meta = {
        "name": "Mercury Admin UX audit",
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": generated,
        "capturedAt": CAPTURED,
        "capture": CAPTURE_SETUP,
        "product": DEMO_ORIGIN,
        "baseUrl": args.base_url.rstrip("/") if args.base_url else None,
        "urlsRelativeTo": None if args.base_url else "the URL of this file",
        "caveat": CAVEAT,
        "counts": {"screens": len(nodes), "journeys": len(journeys), "issues": len(records),
                   "patterns": len(patterns_out)},
    }

    write_json(os.path.join(out_dir, "issues.json"), {"meta": meta, "issues": [capped(r) for r in records]})
    with open(os.path.join(out_dir, "issues.jsonl"), "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(capped(r), ensure_ascii=False) + "\n")
    with open(os.path.join(out_dir, "issues-index.json"), "w", encoding="utf-8") as f:
        # One row per line keeps the index small and easy to scan.
        rows = ",\n    ".join(json.dumps(index_row(r), ensure_ascii=False) for r in records)
        f.write('{\n  "meta": ' + json.dumps(meta, ensure_ascii=False) + ',\n  "issues": [\n    ' + rows + "\n  ]\n}\n")
    write_json(os.path.join(out_dir, "patterns.json"),
               {"meta": meta, "patterns": patterns_out, "standalone": standalone})
    write_json(os.path.join(out_dir, "screens.json"), {"meta": meta, "screens": screens})
    write_json(os.path.join(out_dir, "journeys.json"), {"meta": meta, "journeys": journeys_out})

    os.makedirs(tickets_dir, exist_ok=True)
    for old in glob.glob(os.path.join(tickets_dir, "*.md")):
        os.remove(old)
    for r in ticket_records:
        with open(os.path.join(tickets_dir, f"{r['id']}.md"), "w", encoding="utf-8") as f:
            f.write(ticket_markdown(capped(r), ticket_urls) + "\n")
    # A closed accessibility id keeps a stub so tracker links keep working; one that is live again has its ticket.
    live = {r["id"] for r in records}
    for iid, tomb in sorted(load_tombstones().items()):
        if iid not in live:
            with open(os.path.join(tickets_dir, f"{iid}.md"), "w", encoding="utf-8") as f:
                f.write(stub_markdown(iid, tomb, ticket_urls) + "\n")

    example = capped(next((r for r in records if r["id"] == EXAMPLE_ID), records[0]))
    doc = readme(meta, records, patterns_out, standalone, example)
    for name in ("README.md", "AGENTS.md"):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.write(doc)

    report = has_report()
    with open(os.path.join(out_dir, "llms.txt"), "w", encoding="utf-8") as f:
        f.write(llms_txt(urls, meta, "", report))
    root_urls = Urls(site_root, site_root, args.base_url, prefix)
    with open(os.path.join(site_root, "llms.txt"), "w", encoding="utf-8") as f:
        f.write(llms_txt(root_urls, meta, prefix, report))

    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    print(f"wrote {len(records)} issues, {len(patterns_out)} patterns, {len(screens)} screens, "
          f"{len(journeys_out)} journeys to {out_dir} (+ {os.path.join(site_root, 'llms.txt')})")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
