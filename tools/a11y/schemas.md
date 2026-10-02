# Accessibility audit: file formats

Every stage reads and writes the files below. Units build against this document, not against each
other's code. Plan references (KTD, R, U) point at `docs/plans/2026-10-02-1018-feat-a11y-audit-plan.md`.

## Conventions

### Serialization

Two serializations, identical in Python (`lib/canon.py`) and Node (`lib/canon.mjs`):

- `canonical(obj)`: compact JSON for hashing, JSONL lines and determinism checks. Keys sorted,
  separators `,` and `:`, no ASCII escaping, every float rounded to 4 decimal places, an integral
  float written as an integer (`2.0` becomes `2`), `-0` written as `0`.
- `dump(obj)`: the same normalization, indented by one space, `": "` after keys, and a trailing
  newline. Every `.json` file in a workspace is written with `dump`; every `.jsonl` line is
  `canonical(obj)` plus `\n`.

Both write through a temp file in the same directory and a rename (`write_json` / `writeJson`).
Neither ever writes a timestamp into a file that a determinism check compares (raw state files,
`findings.json`, `published/issues.json`). Dates appear only in `run.json`, `meta.json`,
`report.json`, verdict lines and policy fields.

`sha(obj)` is the hex SHA-256 of `canonical(obj)`. `sha1(obj)` is the hex SHA-1 of
`canonical(obj)` (used only for recipe source hashes).

### Name templates (KTD15)

`name_template(s)` / `nameTemplate(s)` turns visible text or an accessible name into a template
that survives data and date drift. Both languages apply the same steps in this order:

1. Replace every run of spaces, tabs, line breaks, NBSP and the Unicode spaces U+2000–U+200A,
   U+202F, U+205F and U+3000 with one space, and trim. Every later pattern is ASCII-only, so `\b`
   and `\d` behave the same in both languages.
2. Relative dates become `<date>`: `Today`, `Yesterday`, `Tomorrow` (any case, whole word);
   `N (second|minute|hour|day|week|month|year)s? ago`; `in N (…)s?`; `last|next (week|month|year)`.
3. Absolute dates become `<date>`: a month name or three-letter abbreviation (optionally with a
   period) followed by a day and an optional `, YYYY` or ` YYYY`; `D Month YYYY`;
   `M/D`, `M/D/YY`, `M/D/YYYY`, `MM-DD-YYYY`; ISO `YYYY-MM-DD`; a bare month name followed by a
   four-digit year.
4. Times become `<time>`: `H:MM` or `HH:MM`, optional `:SS`, optional ` ?AM|PM` (any case).
5. Amounts become `<amount>`: an optional sign (`-`, `−`, `+`), a currency symbol (`$`, `€`, `£`)
   and a number with optional thousands commas and decimals, in either order around the symbol
   (`-$1,203.00`, `$-5`); also a bare number with exactly two decimals (`1,203.00`).
6. Any other digit run, with internal `.` or `,` groups, becomes `<n>`.
7. Collapse a run of the same placeholder separated only by spaces or `.,/:-–` into one
   (`<n>-<n>` becomes `<n>`; `<date> <time>` stays two placeholders).

`name_template_regex(t)` / `nameTemplateRegex(t)` turns a template back into an anchored,
case-sensitive regular expression: literal parts escaped, each placeholder matching `.+?`. A
locator name `"Paid <date>"` matches `"Paid Yesterday"` and `"Paid 2 days ago"`.

Shared test vectors live in `tools/a11y/tests/data/canon-vectors.json`; both languages' tests
read the same file.

### CSS-Module classes (KTD15)

A demo class looks like `_<local>_<hash>_<line>`: a leading underscore, a local name (letters,
digits, `-`), a 5-character hash of letters and digits, and a line number. Normalization keeps
`<local>`. The `<hash>` identifies the module file and is used only to find "the nearest ancestor
from a different CSS Module" within one state; it never enters a key. FontAwesome classes (`fa`,
`fa-*`, `svg-inline--fa`) are dropped. Raw files store classes exactly as the DOM had them; only
`signature.py` normalizes.

### Harness hash (KTD4, KTD10)

`harness_hash()` / `harnessHash()`: list the files `tools/a11y/checks/*.mjs`, `tools/a11y/lib/*.mjs`
and `tools/a11y/sweep.mjs` that exist, as repo-relative POSIX paths sorted by code point. For each,
append `path + "\n" + hex sha256(file bytes) + "\n"`. The hash is the first 16 hex characters of
the SHA-256 of that text. The axe options live in `checks/axe.mjs`, so they are covered. A raw file
whose `harness` differs from the current hash is invalid and is re-swept.

### Build hash

`build_hash(html)` / `buildHash(html)`: the first match of `cdn\.mercury\.com/builds/([A-Za-z0-9]+)/`
in the demo's HTML, else `"unknown"`.

### Run ids

`YYYY-MM-DD-HHMM` in local time. `plan` creates the run directory. Every other stage takes
`--run <id>`, defaulting to the newest directory under `<workspace>/runs/` that has a
`states.json`.

### State kinds and the checks they get

| kind | axe scope | records | interactive, target | tabwalk | dialog | status |
|---|---|---|---|---|---|---|
| `page` | document | yes | yes | yes | no | no |
| `modal`, `drawer` | top layer | yes | yes | yes | yes | no |
| `menu`, `popover` | top layer | yes | yes | no | yes | no |
| `form-state`, `empty-state` | top layer if open, else document | yes | yes | no | no | no |
| `toast` | the toast | yes | yes | no | no | yes |
| `confirmation`, `validation-error` | top layer if open, else document | yes | yes | no | no | yes |

`dialog` runs only when the final hop is a `click` or `hover`. `status` is armed before the final hop.

Check-module ids (the families a raw file lists as completed or incomplete): `axe`, `records`,
`interactive`, `target`, `tabwalk`, `dialog`, `status`.

## Workspace layout (KTD3)

```text
<workspace>/                     default a11y/
  policy.json                    falls back to the committed a11y/policy.json when absent
  recipes/<area>.json
  runs/<run-id>/
    run.json states.json outcomes.json findings.json records.jsonl judgments.jsonl
    verdicts.jsonl decisions.jsonl canary.json lighthouse.json diff.json
    batches/                     prepared batch inputs and agent outputs (committed)
    raw/<state-id>.json          one per state, gitignored
    shots/                       replay screenshots and tab-stop overlays, gitignored
  published/  issues.json report.json registry.json meta.json
```

One writer per file (KTD3): sweep workers write `raw/*`; `sweep` writes `outcomes.json`; `group`
writes `findings.json`; `records prepare` writes `records.jsonl`; `judgment add`, `verdict add`,
`decision add` and `calibration add` are the only appenders of their files; `canary` writes
`canary.json`; `diff` writes `diff.json`; `publish` writes `published/`.

## policy.json (KTD9)

```json
{
  "version": 1,
  "isolation": {"status": "unchecked" | "confirmed" | "failed", "date": "YYYY-MM-DD" | null, "note": "..."},
  "origin": "https://demo.mercury.com",
  "allowedOrigins": ["https://demo.mercury.com"],
  "denylist": {
    "names": ["Logout", "Log out", "Sign out"],
    "notes": ["Do NOT click Logout (recapture/scopes.json)", "..."]
  },
  "mutatingVerbs": ["submit", "create", "send", "delete", "invite", "confirm", "pay", "..."],
  "fixtureValues": {"email": "a11y-audit@example.invalid", "name": "Test Person", "...": "..."},
  "replay": {"onlyRecordedEdges": true}
}
```

`denylist.names` are matched against a step locator's name template, case-insensitively, as whole
strings. A `goto` or a navigation that leaves `allowedOrigins` is refused. `mutatingVerbs` entries are
words or multi-word phrases, matched as whole words against an edge label, case-insensitively; only
`plan` reads them.

## runs/<id>/states.json (U1 `plan`, KTD5)

```json
{
  "run": "2026-10-02-1400",
  "map": {"nodes": 915, "sha": "<sha of the sorted node id list>"},
  "roots": ["home.index", "..."],
  "states": {
    "cards.edit-limit-modal": {
      "area": "cards",
      "kind": "modal",
      "url": "/cards/…",
      "root": false,
      "rootUrl": null,
      "needsRecipe": true,
      "parent": "cards.detail-physical",
      "path": ["cards.index", "cards.detail-physical", "cards.edit-limit-modal"],
      "labels": ["Click a card row", "Click Edit limits"],
      "mutates": false,
      "source": "<sha1 of [[from, to, label], ...] along path>",
      "unreachable": null
    }
  }
}
```

- `root` is true for the first page node (by area order, then node order) of each concrete page URL,
  excluding `books.*` nodes. `rootUrl` is that URL. A root has `parent: null` and `path: [self]`.
- A non-root's `parent` is its predecessor on `path`, and `path[0]` is always a root.
- `needsRecipe` is false only for roots.
- `unreachable` is `"no-path"` when no path from a root exists; then `parent`, `path`, `labels` and
  `source` are `null`, `mutates` is false and `needsRecipe` is true.
- A URL is concrete unless it contains any of `…`, `...`, `<`, `>`, `{`, `}`, `[`, `]`. The
  `{savedTransactionViewId:…}` URLs are not real query strings: the demo ignores them.

## recipes/<area>.json (U4, KTD6–KTD8)

An object keyed by state id, sorted. Editable by hand.

```json
{
  "cards.index": {
    "parent": "/cards",
    "hop": [],
    "fingerprint": { "...": "see below" },
    "mutates": false,
    "source": "<sha1>",
    "status": "accepted",
    "similarity": 0.97,
    "recordedBy": "draft" | "agent" | "hand" | "root",
    "review": null | {"reason": "below-threshold" | "mutating" | "sample", "by": "conductor", "decision": "accept" | "reject", "date": "YYYY-MM-DD"}
  },
  "cards.edit-limit-modal": {
    "parent": "cards.detail-physical",
    "hop": [{"click": {"role": "button", "name": "Edit limits"}}],
    "...": "..."
  }
}
```

- `parent` is a state id, or a URL path starting with `/` for roots.
- `status`: `accepted` (replayed by the sweep), `review` (waiting for the conductor), `rejected`,
  `draft` (not yet checked). Only `accepted` recipes are replayed.

### Steps

Each step is a one-key object:

| step | value |
|---|---|
| `goto` | a path on the policy origin, e.g. `"/cards"` |
| `click`, `hover`, `check`, `scroll` | a locator (`scroll` also accepts `{"y": 600}`) |
| `press` | a key name, e.g. `"Enter"`, `"Escape"` |
| `fill` | `{"locator": {...}, "value": "..."}`; the value comes from `policy.fixtureValues` or is obviously fake |
| `select` | `{"locator": {...}, "option": "..."}` |
| `wait` | `{"ms": 500}` or `{"locator": {...}}` |

A locator is tried in this order: `role` plus `name`, `label`, `text`, then `css` (a hash-stripped
CSS-Module selector such as `[class*="_menuButton_"]`). Optional `index` picks among several matches
(default 0) and `within` scopes the search to another locator. `name`, `label` and `text` are name
templates (matched with `nameTemplateRegex`). Agent-browser `@e` refs are never stored.

```json
{"role": "button", "name": "Edit limits", "label": null, "text": null, "css": null, "index": 0, "within": null}
```

### Fingerprint (KTD7)

```json
{
  "path": "/cards/<id>",
  "top": {"role": "dialog", "name": "Edit card limits"},
  "headings": ["Edit card limits"],
  "controls": ["Cancel", "Daily limit", "Monthly limit", "Save"],
  "structure": "dialog" | "menu" | "listbox" | "status" | "document"
}
```

`path` is the URL path (no query) with each segment containing a digit or longer than 20 characters
replaced by `<id>`. `top` is `{"role": "document", "name": ""}` when there is no top layer. Names and
headings are name templates; `controls` is the sorted, de-duplicated set of visible control name
templates in scope. A replay matches when `path` and `top` are equal and the Jaccard overlap of
`controls` is at least 0.6.

## runs/<id>/raw/<state-id>.json (U5 writes; U2, U3 define `checks.*`)

```json
{
  "schema": 1,
  "state": "cards.edit-limit-modal",
  "kind": "modal",
  "status": "audited" | "unreachable",
  "reason": null | "step-failed" | "fingerprint-mismatch" | "blocked-by-parent" | "no-recipe" | "no-path" | "tool-error",
  "reasonDetail": null | {"step": 2, "message": "...", "rootCause": "cards.detail-physical"},
  "harness": "<harness hash>",
  "recipeHash": "<sha of the recipe chain from the root to this state>",
  "build": "<build hash>",
  "env": {"chromium": "151.0.…", "playwright": "1.62.1", "axe": "4.13.0", "viewport": [1440, 900],
          "dpr": 1, "colorScheme": "light", "reducedMotion": "reduce", "locale": "en-US",
          "timezone": "America/Los_Angeles"},
  "url": "/cards/…",
  "layer": {"scope": "top" | "document", "role": "dialog", "name": "Edit card limits", "xpath": "/html/body/div[4]"},
  "frames": [{"url": "https://cdn.plaid.com/…", "origin": "https://cdn.plaid.com", "firstParty": false}],
  "families": {"completed": ["axe", "records", "interactive", "target", "tabwalk", "dialog"], "incomplete": []},
  "results": [ "<result>", "..." ],
  "records": [ "<record>", "..." ],
  "checks": {
    "axe": {"rulesRan": ["color-contrast", "target-size", "label-content-name-mismatch", "..."], "inapplicable": ["..."]},
    "interactive": [ "<occurrence>", "..." ],
    "target": {"measured": 42},
    "tabwalk": {"stops": [], "end": "body" | "repeat" | "limit", "presses": 31},
    "dialog": {"trigger": "<occurrence>", "opened": "enter" | "space" | null, "trace": []},
    "status": {"message": "...", "region": {"role": "status", "preexisting": true}, "focused": false}
  }
}
```

- An unreachable file has `status`, `reason`, `reasonDetail`, `harness`, `recipeHash`, `build`, `env`
  and empty `results`, `records`, `checks`, with `families.completed` empty.
- The file holds no run id and no timestamps, so two sweeps can be compared byte for byte.
- Results and records are sorted by `canonical()` for stable output.

### Occurrence: the element an observation is about

Everything `signature.py` needs to build a key (KTD15), read live from the DOM:

```json
{
  "tag": "button",
  "role": "button",
  "name": "Edit limits",
  "classes": ["_root_ab12c_3", "_ghost_ab12c_9"],
  "ancestors": [["_footer_x9y8z_12"], ["_drawer_k3j4h_1", "_open_k3j4h_4"], []],
  "landmark": {"role": "main", "name": ""},
  "heading": "Card limits",
  "data": {"data-testid": "edit-limits"},
  "xpath": "/html/body/div[2]/main/div/button[1]",
  "rect": [1184, 212, 97, 40],
  "layer": "top" | "document"
}
```

`ancestors` lists the class lists of up to 8 element ancestors, nearest first, stopping at `body`.
`data` keeps `data-*` attributes whose values contain no digits. `rect` is `[x, y, width, height]`
in CSS pixels, rounded.

### Result: one observation that may become a finding

```json
{
  "check": "axe" | "target" | "tabwalk" | "dialog" | "status",
  "rule": "color-contrast",
  "basis": "violation" | "incomplete" | "failure" | "candidate",
  "criteria": ["1.4.3"],
  "messageKey": null,
  "occurrence": { "...": "occurrence" },
  "evidence": { "...": "measured facts, check-specific" }
}
```

- `axe` results: `basis` is `violation` or `incomplete`; `criteria` come from the rule's `wcagNNN`
  tags; `messageKey` is set for incomplete results; `evidence` holds axe's `data` (for
  `color-contrast`: `fgColor`, `bgColor`, `contrastRatio`, `fontSize`, `fontWeight`,
  `expectedContrastRatio`) plus `failureSummary`.
- Custom checks use these rule ids, which `checks.json` maps to families:
  `target-size-own` (target), `focus-not-visible`, `focus-hidden`, `keyboard-trap`, `not-reached`,
  `backward-jump` (tabwalk), `dialog-trigger-not-focusable`, `dialog-trigger-not-operable`,
  `dialog-focus-not-moved`, `dialog-focus-escaped`, `dialog-focus-not-returned`,
  `menu-focus-not-moved`, `popover-not-reachable`, `hover-only` (dialog), `status-not-announced`,
  `error-not-associated` (status).
- `basis: candidate` marks a result KTD13 sends to the verifier as unconfirmed.

### Record: one announced-as line (R12)

```json
{
  "role": "button",
  "name": "Move money",
  "states": {"expanded": false, "haspopup": "menu"},
  "description": "",
  "template": "button \"Move money\" [expanded=false, haspopup=menu]",
  "occurrence": { "...": "occurrence" }
}
```

`states` keys are written in this fixed order when present: `checked`, `pressed`, `selected`,
`expanded`, `haspopup`, `disabled`, `readonly`, `required`, `invalid`, `modal`, `multiselectable`,
`level`, `valuenow`, `valuetext`, `live`, `errormessage`. The template is
`role "name" [k=v, ...] (description)`, with the brackets or parentheses left out when empty. Names
and descriptions inside the template are name templates.

## runs/<id>/run.json (U1 `plan`, extended by stages)

```json
{
  "run": "2026-10-02-1400",
  "created": "2026-10-02T14:00:00",
  "baseUrl": "https://demo.mercury.com",
  "workspace": "a11y",
  "versions": {"playwright": "1.62.1", "axe": "4.13.0", "chromium": null | "151.0.…", "node": "v24…", "python": "3.14…"},
  "harness": null | "<harness hash at sweep start>",
  "builds": ["<build hash>", "..."],
  "stages": {"sweep": {"started": "…", "finished": "…", "interrupted": null | "environment"}}
}
```

## runs/<id>/outcomes.json (U5 `sweep`)

Written only when the coverage invariant holds: audited and unreachable together are exactly the
states in `states.json`, with no overlap.

```json
{
  "run": "2026-10-02-1400",
  "counts": {"audited": 870, "unreachable": 45},
  "builds": ["<hash>"],
  "states": {
    "cards.edit-limit-modal": {
      "outcome": "audited" | "unreachable",
      "reason": null | "<reason>",
      "detail": null | {"step": 2, "rootCause": "…", "message": "…"},
      "firstUnreachable": null | "<run id>",
      "completed": ["axe", "records", "..."],
      "incomplete": [],
      "build": "<hash>"
    }
  }
}
```

`firstUnreachable` is this run's id, or the value from the previous published run's outcomes when
the state was already unreachable there.

## runs/<id>/records.jsonl and judgments.jsonl (U7, KTD20)

`records prepare` writes one line per distinct record, keyed by `sig`:

```json
{"sig": "<record signature from signature.py>", "template": "button \"<n> more\"", "role": "button",
 "states": {}, "description": "", "states_seen": ["home.index", "..."], "sample": "home.index",
 "occurrence": {"...": "first occurrence"}, "published": null | "<issue id>"}
```

`judgment add` appends:

```json
{"sig": "…", "verdict": "ok" | "flag", "criterion": null | "4.1.2", "family": null | "name",
 "reason": "…", "batch": "records-003", "model": "sonnet", "prompt": "record-judge@1"}
```

A `flag` with a criterion feeds `group`. A `flag` without one goes to the advisory list.

## runs/<id>/findings.json (U6 `group`)

```json
{
  "run": "…",
  "findings": [
    {
      "key": "<finding key>",
      "source": "scanner" | "hand-found",
      "family": "contrast",
      "rule": "color-contrast",
      "criteria": ["1.4.3"],
      "basis": "violation",
      "severityDefault": "medium",
      "title": "≤ 80 chars from checks.json",
      "summary": "one-line evidence summary",
      "states": ["…sorted"],
      "occurrences": [{"state": "…", "index": 3}],
      "primary": "<state id>",
      "sample": ["<primary>", "<least-like>"],
      "issueId": null | "<id from the registry or the hand-found id>",
      "recordSig": null | "<sig>",
      "evidenceHash": "<sha of the canonical evidence of the sampled occurrences>",
      "facts": {"...": "values the templates need: role, name, colors, ratio, size, …"}
    }
  ],
  "proposals": [
    {"id": "<sha of [finding key, hand-found id]>", "finding": "<key>", "handFound": "<id>",
     "overlap": 0.75, "family": "name"}
  ],
  "advisory": [{"sig": "…", "template": "…", "reason": "…", "states": 12}],
  "flags": [{"key": "…", "message": "tags agree 82% of the time"}]
}
```

`occurrences[].index` points into the state's raw `results` (or `records` for record findings).
Hand-found findings have key `hand:<issue id>`, `issueId` set to that id, and `states` from the
issue's `nodeIds`.

## Batch files (U7)

`verify prepare` and `records prepare` write `runs/<id>/batches/<name>.json` (input) and agents
write `runs/<id>/batches/<name>.out.jsonl` (one verdict, judgment or decision per line). The `add`
primitives read an `.out.jsonl`, print the rejected lines with reasons as JSON, and append the rest.

## runs/<id>/verdicts.jsonl (U7 `verdict add`, KTD19)

```json
{"key": "…", "issueId": null | "…", "source": "scanner" | "hand-found",
 "outcome": "confirm" | "drop" | "rerate" | "not-reached" | "unverified",
 "severityBefore": "medium", "severityAfter": "low",
 "reason": "…", "reasonCode": null | "exception:inline" | "…",
 "counterEvidence": null | "<probe or screenshot reference>",
 "probes": [{"id": "…", "state": "…", "kind": "contrast", "env": {"viewport": [1440, 900], "dpr": 1,
            "colorScheme": "light", "reducedMotion": "reduce"}, "value": {"...": "..."}}],
 "states": ["<sampled states checked>"], "build": "<hash>",
 "model": "sonnet", "batch": "verify-004", "prompt": "verifier@1", "attempt": 1,
 "second": false, "date": "YYYY-MM-DD"}
```

Drop reason codes: `exception:inline`, `exception:equivalent-control`, `exception:essential`,
`exception:ua-control`, `decorative`, `not-reproduced-live`, `third-party-frame`,
`aria-optional-by-apg`, `tool-false-positive`, `not-aa-failure`, `fixed-since-capture`.

## runs/<id>/decisions.jsonl (U7 `decision add`)

```json
{"proposal": "<proposal id>", "finding": "<key>", "handFound": "<id>",
 "decision": "attach" | "reject" | "release", "reason": "…",
 "model": "sonnet", "batch": "…", "prompt": "verifier@1", "date": "YYYY-MM-DD"}
```

## runs/<id>/canary.json (U7 `canary`)

```json
{"prompt": "verifier@1", "batch": "canary-001", "expected": 6, "correct": 6, "pass": true,
 "items": [{"key": "…", "expected": "rerate:low", "got": "rerate:low", "ok": true}]}
```

## runs/<id>/lighthouse.json (U7 `calibration add`, KTD25)

```json
{"date": "YYYY-MM-DD", "chrome": "…", "lighthouseAxe": "4.13.0", "sweepAxe": "4.13.0",
 "states": [{"state": "…", "kind": "root" | "overlay", "lighthouseFailed": ["…"],
             "sweepRules": ["…"], "differences": [{"rule": "…", "side": "lighthouse" | "sweep",
             "explained": true, "explanation": "scope" | "rule-selection" | "version" | null}]}],
 "unexplained": 0}
```

## runs/<id>/diff.json (U8 `diff`, KTD18)

```json
{
  "run": "…",
  "baseline": "none" | "<run id in published/meta.json>",
  "findings": {
    "<key>": {"status": "new" | "persisting" | "re-rated" | "not-retested" | "fixed" | "withdrawn" | "reappeared",
              "issueId": null | "…", "severityBefore": null | "…", "severityAfter": "…",
              "nodeIds": ["…"], "reason": null | "screen removed" | "…"}
  },
  "handFound": {
    "<id>": {"status": "first-audited" | "confirmed" | "re-rated" | "not-reproduced" | "not-retested",
             "reason": null | "fixed since capture" | "tool false positive" | "not an AA failure",
             "severityBefore": "…", "severityAfter": "…"}
  }
}
```

## published/ (U8 `publish`, KTD21)

### issues.json

```json
{
  "generated": [
    {"id": "cards.i-a11y-contrast-muted-text", "area": "cards", "title": "…", "severity": "medium",
     "category": "a11y", "detail": "…", "evidence": "≤ 300 chars", "nodeIds": ["<primary first>", "…"],
     "wcag": ["1.4.3"],
     "copy": {"title": "…", "summary": "…", "userImpact": "…", "fixHypothesis": "…", "acceptance": ["…"],
              "verify": ["…"], "searchTerms": ["…"], "impact": "accessibility", "confidence": "high",
              "confidenceReason": "…", "demoArtifactRisk": false},
     "audit": {"criteria": [{"number": "1.4.3", "name": "Contrast (Minimum)", "url": "https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum"}],
               "family": "contrast", "verdict": "confirm", "reason": "…", "date": "YYYY-MM-DD",
               "build": "<hash>", "evidence": "full live evidence", "checkedStates": ["…"],
               "run": "<run id>", "status": "new"}}
  ],
  "overlays": {
    "<hand-found id>": {"severity": null | "high", "evidenceAppend": null | "Verifier (YYYY-MM-DD): …",
                        "addNodeIds": ["…"], "wcag": ["…"], "audit": {"...": "as above"},
                        "history": "rated high on YYYY-MM-DD, previously medium" | "not reproduced on YYYY-MM-DD: …" | null}
  }
}
```

The `copy` object has the same keys as an entry of `CURATED` in `tools/export_agent_data.py`.
Generated issues are written grouped by area, then by severity (high first), then by screen count
(descending), then by id.

### registry.json (KTD16)

```json
{
  "keys": {"<finding key>": "<issue id>"},
  "held": ["<every id ever minted, sorted>"],
  "tombstones": {"<issue id>": {"reason": "fixed" | "withdrawn" | "attached", "attachedTo": null | "<hand-found id>",
                                "run": "<run id>", "date": "YYYY-MM-DD"}},
  "attachments": [{"finding": "<key>", "handFound": "<id>", "run": "<run id>"}]
}
```

### meta.json

```json
{"run": "…", "date": "YYYY-MM-DD", "builds": ["…"], "versions": {"...": "..."},
 "coverage": {"states": 915, "audited": 870, "unreachable": 45},
 "lighthouse": {"run": "…", "date": "YYYY-MM-DD", "result": {"...": "lighthouse.json"}}}
```

### report.json

The report model (U10 renders it): `run`, `date`, `captured` (`2026-09-24`), `baseline`,
`counts` (by severity), `coverage` (tested criteria with `full` or `partial` and the failure modes
covered, untested criteria, untested dimensions, builds, Lighthouse line, caveat), `changes`
(count per status), `findings` (rows with id or null, title, criteria, severity, screens, status,
fix, evidence, family), `tables` (`contrast`, `keyboard`, `dialog`, `screenReader`, each a list of
row objects), `advisory`, `priority` (ids in order), `notRetested`, `withdrawn`, `fixed`,
`handFound` (rows with status and reason), `unreachable` (grouped by root cause, with
`firstUnreachable`).

## Check data: wcag.json, checks.json, hand-found.json (U1, KTD14)

- `wcag.json`: the 55 A and AA criteria of WCAG 2.2 (`number`, `name`, `level`, `principle`,
  `guideline`, `url`), sorted numerically. A criterion that is not in this file (an AAA tag such as
  axe's 2.1.3 on `scrollable-region-focusable`) is dropped from a result's criteria by `group`.
- `checks.json`:
  - `families.<id>`: `label`, `criteria` (primary first), `defaultSeverity`, optional
    `defaultSeverityByKind: {kind: {rule: severity}}`, `guidance` (for the verifier), `key`
    (`intrinsic` or `content`, KTD15), `facts` (placeholders its templates use), `templates`
    (`title`, `detail`, `evidence`, and `copy` with the keys of a `CURATED` entry minus `impact` and
    `demoArtifactRisk`, which publish adds as `accessibility` and `false`), and optional
    `ruleTemplates: {rule: partial templates}`, merged over `templates` key by key (and inside
    `copy` key by key).
  - Severity lookup: `fam.get("defaultSeverityByKind", {}).get(kind, {}).get(rule, fam["defaultSeverity"])`,
    where `kind` is the primary state's kind.
  - `axeRules: {ruleId: family}`, `customRules: {ruleId: family}`, `customCriteria: {ruleId: [criteria]}`.
  - `factDocs: {placeholder: meaning}`: the shared fact vocabulary. `group` computes every fact a
    finding's family lists into `finding.facts`; templates are filled with `str.format(**facts)`.
  - The fact `criterion` is the finding's criterion that comes first in its family's `criteria`.
- `hand-found.json`: `{issueId: {family, criteria, why}}` for every `category: "a11y"` issue in
  `--data`. `group` fails when one is missing.
- Findings from judged announced-as records (KTD20) use rule id `announced-as`; a judgment may flag
  only the families `name`, `role-state` and `structure`.
- R20 names 1.4.12 as not tested, so `checks/axe.mjs` disables `avoid-inline-spacing`.

## Stage modules

`audit.py`'s `STAGES` table names every command, its module and its owning unit. Adding a stage
module never requires editing `audit.py` or `status.py`.

- A Python stage module exposes `cli(ws, sub, argv) -> int`. `ws` is a `lib.ws.Workspace`, `sub`
  is the subcommand or `None`, and `argv` holds the stage's own options, parsed by the module with
  `argparse`. It may also expose `status(ws, run_id) -> dict`, which `status.py` calls and prints
  under the module's name. A status function reads files only and never writes, and returns
  `{"pending": true, …}` when the run holds none of its inputs or outputs yet.
- A Node stage module runs as `node tools/a11y/<module>.mjs [sub] --workspace … --data … --map …
  --base-url … [--run …] [stage options]` and parses them with `lib/cli.mjs`'s `parse()`. It prints
  a JSON summary on stdout as its last line and exits non-zero on failure. Every Node module answers
  a `status` subcommand by printing one JSON object (it may be `{}`) and doing nothing else;
  `status.py` prints it under the module's name.
- Python modules import shared code as `from lib.canon import …` and `from lib.ws import …`;
  `audit.py` and `tests/pathshim.py` put `tools/a11y/` and `tools/` on `sys.path`.
