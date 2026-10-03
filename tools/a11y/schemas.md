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

`harness_hash()` / `harnessHash()`: list the files `tools/a11y/checks/*.mjs`, `tools/a11y/lib/*.mjs`,
`tools/a11y/sweep.mjs`, `tools/a11y/recipe.mjs`, `tools/a11y/fingerprint.mjs` and
`tools/a11y/checks.json` that exist, as repo-relative POSIX paths sorted by code point. For each,
append `path + "\n" + hex sha256(file bytes) + "\n"`. Then, for `axe-core` and then `playwright`, if
`node_modules/<name>/package.json` exists, append `"node_modules/" + name + "\n" + version + "\n"`,
where `version` is the `version` field of that file. The hash is the first 16 hex characters of the
SHA-256 of that text. The axe options live in `checks/axe.mjs`, so they are covered; the replay and
the fingerprint match live in `recipe.mjs` and `fingerprint.mjs`, the custom checks' rule table in
`checks.json`, and the engines in the two package versions. A raw file whose `harness` differs from
the current hash is invalid and is re-swept.

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
  "replay": {"onlyRecordedEdges": true},
  "roots": {"exclude": {"<state id>": "why its URL alone does not reproduce it"}}
}
```

`roots.exclude` lists page states whose concrete URL does not reproduce them (a wizard step that
redirects, a view rebuilt from in-memory state), found when `recipe check` compares a root with its
capture. `plan` gives their URL no root, so they get a recipe through their predecessors (R5).

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
- A root's `hop` holds setup steps (closing a first-visit panel) only with `via`: a state that has a
  recorded edge into the root, whose action the steps follow (KTD9). The demo opens its "Try out
  Mercury" panel on every fresh /dashboard visit; the capture closed it ("Click collapse chevron").
- `status`: `accepted` (replayed by the sweep), `review` (waiting for the conductor), `rejected`,
  `draft` (not yet checked). Only `accepted` recipes are replayed.
- `recordedBy` is set by whoever submits the recipe, never read from a `--recipe` candidate:
  `recipe draft` writes `draft`, `recipe check --recipe` writes `agent`, and a root checked with an
  empty `hop` writes `root`. A root candidate with setup steps is `agent`, so it gets the mutating and
  sample reviews. Re-checking an existing entry keeps its `recordedBy`. `recipe check` reports a
  state that throws, or whose browser crashes, as `error`; the other states still run and the exit
  code is 1.
- `checked` is the run id of the last `recipe check`; `queue` drops states re-checked since the
  published run. A pending review is `{reason, by: "conductor", decision: null, date: null}`.
- `recipe check` also requires the open layer the state's kind needs (modal, drawer, menu, popover,
  toast): a screenshot cannot tell a menu over its page from the page (`structure-missing`
  otherwise). Similarity threshold 0.90 (mean SSIM of 160×100 grayscale crops), to tune on the first
  real area. Agent-recorded recipes that pass but mutate, or fall in a 10% sample, go to `review`.
- Captured screenshots for the fixture app live in `fixtures/app/captures/`: `.git/info/exclude`
  ignores every `screens/` directory.

### Steps

Each step is a one-key object:

| step | value |
|---|---|
| `goto` | a path on the policy origin, e.g. `"/cards"` |
| `click`, `hover`, `check`, `scroll` | a locator (`scroll` also accepts `{"y": 600}`) |
| `press` | a key name, e.g. `"Enter"`, `"Escape"`. Refused (`step-failed`) when it would put text into the focused field (a text input, textarea, select, editable element or frame from another origin): a character key with no Control, Alt or Meta held, or a paste (V under Control or Meta, Shift+Insert, Control+Y). Type with `fill` instead. Enter or Space is refused on a focused element a click would be refused on, and on anything inside a frame from another origin |
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
    "tabwalk": {"stops": [], "end": "body" | "repeat" | "exit" | "limit", "presses": 31},
    "dialog": {"trigger": "<occurrence>", "opened": "enter" | "space" | "focus" | null, "trace": []},
    "status": {"message": "...", "region": {"role": "status", "live": "polite", "preexisting": true}, "focused": false,
               "present": true, "messages": [], "errors": []}
  }
}
```

- An unreachable file has `status`, `reason`, `reasonDetail`, `harness`, `recipeHash`, `build`, `env`
  and empty `results`, `records`, `checks`, with `families.completed` empty.
- The file holds no run id and no timestamps, so two sweeps can be compared byte for byte.
- Results and records are sorted by `canonical()` for stable output (`sortCanonical` in canon.mjs).
- `frames` is the axe check's `detail.frames` (url without query or hash, origin, `firstParty` by
  site), which the sweep moves to the top level; `checks.axe` keeps `rulesRan` and `inapplicable`.
  First-party frames are listed but not scanned.
- `checks.interactive` is the expected-interactive set: occurrences plus `focusable`, `disabled`
  and `source` (`ax` or `pointer`). Disabled controls stay in the set for target size; the Tab walk
  does not expect to reach them.
- There is no `checks.records`: the `records` check returns its output as the top-level `records`.
- `checks.tabwalk.stops[]`: `{n, occurrence, box, visible, hidden, focusVisible, style, diff, count,
  frame, composite}`; `box` is in page coordinates; `hidden` is null or `invisible`, `zero-size`,
  `clipped`, `off-screen`. `end: exit` means focus left the top layer the walk covers. Inside a top
  layer, coming back round to the first stop ends the walk as `repeat` and is not probed as a trap.
- `checks.dialog.trace[]`: `{key, where, focus, count?}`; `key` is `start`, `Tab`, `Enter`, `Space`,
  `Escape`, `focus`, `close` or `settle`; `where` is `trigger`, `inside`, `outside` or `body`.
- `checks.status.messages[]`: `{message, region, focused, present, xpath}`; `errors[]`: `{name,
  message, tied, describedBy, errorMessage, xpath}`.
- `target-size-own` evidence: `width`, `height`, `offset`, `own` (`fail` or `pass`), `passedBy`
  (`size`, `spacing`, `inline` or null), `axe` (`violation`, `incomplete`, `pass` or
  `inapplicable`), `agree`, `exception` (`ua-control` or null).

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
- Custom checks use these rule ids, which `checks.json` maps to families (`customRules`) and to the
  check module in parentheses (`customChecks`):
  `target-size-own` (target), `focus-not-visible`, `focus-hidden`, `keyboard-trap`, `not-reached`,
  `backward-jump` (tabwalk), `dialog-trigger-not-focusable`, `dialog-trigger-not-operable`,
  `dialog-focus-not-moved`, `dialog-focus-escaped`, `dialog-focus-not-returned`,
  `menu-focus-not-moved`, `popover-not-reachable`, `hover-only` (dialog), `status-not-announced`,
  `error-not-associated` (status).
- `basis: candidate` marks a result KTD13 sends to the verifier as unconfirmed.
- Evidence fields `group` reads to fill templates: `stop` (1-based tab stop where the failure
  shows), `stops` (distinct tab stops visited), `previous` (occurrence of the stop before),
  `focus` (occurrence where focus was at the assertion, or `"body"`), `message` (status or error
  text), `offset` (diameter in CSS px of the largest clear circle centered on a target).

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

Records cover interactive roles, landmarks, headings, and the dialog, alertdialog, status, alert,
log, marquee and timer roles (the ones that carry `modal` and `live`). False flags, `invalid=false`,
`haspopup=false`, `live=off`, and a `valuetext` equal to `valuenow` are left out.
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

`sweep` rewrites `harness` when it starts. `verify prepare`, `diff` and `publish` refuse when it
differs from the current harness hash: "rerun `audit.py sweep`, then `audit.py group`".

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

A `flag` with a criterion feeds `group`. A `flag` without one goes to the advisory list. A flag
always carries the `criterion` key (null means advisory). `records prepare` is a second appender of
`judgments.jsonl`, through the same validator: for a published record signature not yet judged it
carries the published flag forward (`batch: "published"`, `model: "script"`, `prompt: "carried"`),
so a published announced-as finding skips the judge (KTD18).

## runs/<id>/findings.json (U6 `group`)

```json
{
  "run": "…",
  "inputs": {"harness": "<harness hash when group ran>", "outcomes": null | "<sha of outcomes.json's states>", "raw": "<sha of {state id: hex sha256 of the raw file's bytes, or null}>"},
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

`occurrences[].index` points into the state's raw `results` (or `records` for record findings). An
index counts only while the item there has the finding's `rule` (for announced-as, its `recordSig`):
a raw file swept again after `group` can move it.

`inputs` records what `group` read. `raw` covers the raw files of the states outcomes.json lists as
audited, or of every state in states.json before outcomes.json exists; a missing file counts as null.
A state swept again (`sweep --state`, or a resume after its raw file was deleted) changes `raw` when
its raw file changes, even if its outcome stays the same. `verify prepare`, `diff` and `publish`
refuse when run.json's `harness` is not the current harness hash ("the run was swept with harness
<old>, and the harness is now <new>: rerun `audit.py sweep`, then `audit.py group`"). They also refuse
when `inputs` differ from the run's current ones, or are missing ("findings.json was grouped from a
different sweep: rerun `audit.py group`").

- Scanner keys read `<family>:<rule>:<12 hex of sha(key parts)>` (signature.py lists the parts).
- axe incomplete results (`basis: incomplete`) group product-wide by rule and messageKey (KTD12),
  whatever the element: key `<family>:<rule>:<12 hex of sha({family, rule, basis: "incomplete",
  messageKey})>`. Their `facts` are `rule`, `messageKey` ("none" when null), `why`, `elements`,
  `screen`, `criterion`, `criterionName`; title and copy come from `checks.json` `incomplete`;
  `flags` never lists them. Published, they get the id `<area>.i-a11y-<rule>-<messageKey>`.
- Announced-as findings: key `<family>:announced-as:<12 hex>`, `rule: "announced-as"`,
  `basis: "candidate"`, `recordSig: "rec:<12 hex>"`.
- Hand-found findings: key `hand:<issue id>`, `rule: "hand-found"`, `basis: "failure"`, `issueId`
  set to that id, `states` from the issue's `nodeIds`, `severityDefault` the issue's severity,
  `summary` its original evidence, `facts: {}`.
- `flags[].message` reads "tags and attributes agree N% of the time"; `advisory[].states` is a count.

## Batch files (U7)

`verify prepare` and `records prepare` write `runs/<id>/batches/<name>.json` (input) and agents
write `runs/<id>/batches/<name>.out.jsonl` (one verdict, judgment or decision per line). The `add`
primitives read an `.out.jsonl`, print `{file, to, appended, duplicates, skipped, rejected: [{line,
reasons, text}]}`, append the valid lines the ledger does not hold yet, and exit 1 if any line was
rejected. A valid line canonically equal to a ledger line, leaving out a `date` the validator filled
in, counts in `duplicates` and is not appended, so adding a file twice is safe. A verify out file may
mix verdict and decision lines; each `add` skips the other kind. A batch counts as returned once its
`.out.jsonl` exists (write an empty one for an agent that died); a finding's attempts are the
returned batches that held it without a valid verdict, and at two `verify prepare` writes an
`unverified` line (`model: "script"`, `prompt: "verify-prepare"`). `verify status` lists the
returned verify batches whose `.out.jsonl` holds a valid verdict or decision line that
verdicts.jsonl or decisions.jsonl does not (`batches.returnedNotAdded`), and `verify prepare` exits
1, naming them, until they are added.

- `records-NNN.json`: `{batch, run, records: [{sig, template, role, states, description, sample,
  screens, occurrence}]}`, about 150 records each.
- `verify-NNN.json`: `{batch, run, area, primaries, reach: {workspace, data, map, baseUrl, run},
  findings: [{key, issueId, source, family, rule, criteria, basis, severityDefault, measured, title,
  summary, facts, guidance, states, primary, screens, evidenceHash, evidence: [{state, occurrence,
  evidence}], proposals, attachments, attempt}]}`, at most 10 findings sharing an area and a primary
  state. `attachments` are registry attachments a verifier may release; their id is
  `sha([finding, handFound])`.
- `canary-001.json` has the verify format and lives in the canary's own temp workspace.

## runs/<id>/verdicts.jsonl (U7 `verdict add`, KTD19)

```json
{"key": "…", "issueId": null | "…", "source": "scanner" | "hand-found",
 "outcome": "confirm" | "drop" | "rerate" | "not-reached" | "unverified",
 "severityBefore": "medium", "severityAfter": "low",
 "reason": "…", "reasonCode": null | "exception:inline" | "…",
 "counterEvidence": null | "<probe or screenshot reference>",
 "probes": [{"id": "…", "state": "…", "kind": "contrast", "env": {"viewport": [1440, 900], "dpr": 1,
            "colorScheme": "light", "reducedMotion": "reduce"}, "value": {"...": "..."}, "xpath": "<the element measured>"}],
 "states": ["<sampled states checked>"], "build": "<hash>",
 "model": "sonnet", "batch": "verify-004", "prompt": "verifier@1", "attempt": 1,
 "second": false, "date": "YYYY-MM-DD", "criterionAnalysis": null | "…"}
```

- `severityBefore` equals the finding's `severityDefault`; `confirm` and `not-reached` keep it.
- `criterionAnalysis` is required for `not-aa-failure`. `counterEvidence` is also the evidence
  reference on hand-found confirm and rerate lines (which need it or a probe).
- Every probe carries the sweep environment. Probe `value` by kind, and when it passes:

  | kind | value | passes when |
  |---|---|---|
  | `contrast` | `ratio`, optional `required` | ratio ≥ the finding's `facts.required`, else `value.required`, else 4.5 |
  | `target` | `width`, `height` (or `rect`), `offset` | both sides ≥ 24, or offset ≥ 24 |
  | `focus` | `visible` (focus-visible) or `trapped` (keyboard-trap) | `visible: true` / `trapped: false` |
  | `ax` | `role`, `name`, `states`, `description`, `template` | not a measured kind; cited as evidence |

  `probe.mjs` prints the probe object as its last line, with `xpath`, `build` and an advisory `pass`
  (the validator recomputes from `value`). Ids are `probe-<12 hex of sha([state, kind, xpath, value])>`.
  A focus probe in `--session` mode presses keys in that session: run `reach` again afterwards.
- A `not-reproduced-live` drop on a measured family cites, in `counterEvidence`, a passing probe
  whose `xpath` is one of the finding's elements on the probe's `state`: the occurrence xpath of a
  raw item the finding's `occurrences` point at there. One such probe is enough. Hand-found findings
  have no occurrences, so their probes are not matched to an element.

Drop reason codes: `exception:inline`, `exception:equivalent-control`, `exception:essential`,
`exception:ua-control`, `decorative`, `not-reproduced-live`, `third-party-frame`,
`aria-optional-by-apg`, `tool-false-positive`, `not-aa-failure`, `fixed-since-capture`.

## runs/<id>/decisions.jsonl (U7 `decision add`)

```json
{"proposal": "<proposal id>", "finding": "<key>", "handFound": "<id>",
 "decision": "attach" | "reject" | "release", "reason": "…",
 "model": "sonnet", "batch": "…", "prompt": "verifier@1", "date": "YYYY-MM-DD"}
```

The last line for a proposal is its decision. `publish` applies only that line for each proposal, in
proposal id order, so a `reject` after an `attach` leaves no attachment.

## runs/<id>/canary.json (U7 `canary`)

The canary definition lives in `tools/a11y/fixtures/canary/` (pages, `map.json`, `data/`, `raw/`,
`findings.json`, `expected.json` as `{key: {expected, why}}`). `got` is null when no valid line
exists; `pass` also requires zero rejected lines; `prompt` joins the distinct prompts.

```json
{"prompt": "verifier@1", "batch": "canary-001", "expected": 6, "correct": 6, "pass": true,
 "items": [{"key": "…", "expected": "rerate:low", "got": "rerate:low", "ok": true}]}
```

## runs/<id>/lighthouse.json (U7 `calibration add`, KTD25)

Input to `calibration add --file`: `{date?, chrome, lighthouseAxe, states: [{state, kind: "root" |
"overlay", lighthouseFailed: [rule]}], explanations: {rule: "rule-selection" | "version"}}`. An
overlay's `sweepRules` are its own results; a rule Lighthouse fails that the sweep found only on the
parent page is a difference explained by `scope`.

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

Optional `testData: true` marks a snapshot whose findings are invented. Only `tests/ledger.py` writes it,
for `tests/data/snapshot/`; `publish` never does. With it, `build.py` shows a test-data banner above the
map and prints a `NOTE` line, and the report opens with the same message.

### report.json

The report model `publish` writes and `report.py` renders (KTD24):

- `run`, `date` (run.json `created`), `captured` ("2026-09-24"), `baseline` ("none" or a run id),
  `standard` ("WCAG 2.2 AA"); `counts` `{high, medium, low}` over `findings`.
- `coverage`: `states`, `audited`, `unreachable`; `tested[]` `{number, name, principle, level,
  coverage: "full" | "partial", checks: [rule ids], modes}` (a criterion counts as tested when an axe
  rule in the raw files' `checks.axe.rulesRan` covers it or a custom check completed somewhere);
  `untested[]` `{number, name, principle, level, named}` (`named` is true for 1.4.12, 1.4.13, 2.4.11,
  2.5.7); `dimensions` (zoom and reflow, real screen readers, mobile, dark theme); `builds[]`;
  `lighthouse` `{run, date, unexplained, line}`; `caveat` (the export's `CAVEAT`).
- `changes`: `{findings: {new, persisting, re-rated, reappeared, not-retested, withdrawn, fixed},
  handFound: {first-audited, confirmed, re-rated, not-reproduced, not-retested}}`.
- `findings[]`: `{id, key, source, title, criteria, principle, family, severity, severityBefore,
  screens, status, fix, evidence}` for new, persisting, re-rated and reappeared scanner findings and
  first-audited, confirmed and re-rated hand-found issues.
- `tables`: `contrast`, `keyboard`, `dialog`, `screenReader`; every row `{id, title, criterion,
  rule, severity, screens, status}` plus contrast `element, fg, bg, ratio, required`; keyboard
  `element, stop, stops, previous, arrowKeys: "not tested"`; dialog `element, overlay, focus`;
  screenReader `element, announced, message`.
- `advisory` (from findings.json); `priority` (ids by severity, then screen count, then id).
- `notRetested[]` `{id | null, key, title, criteria, family, severity, screens, nodeIds, reason,
  lastVerified: {run, date}}`; `withdrawn[]` `{id: null, formerId, key, title, criteria, family,
  severity, screens, nodeIds, reason, reasonCode, run}`; `fixed[]` `{id: null, formerId, key, title,
  criteria, family, severity, screens, reason, run}`.
- `handFound[]` (all 17) `{id, title, status, reason, verifierReason, severityBefore, severityAfter,
  criteria, family, screens, date, attached: [{key, formerId, title, criteria, family, severity,
  nodeIds, status, issue}]}`; `attached[].issue` carries the generated issue forward.
- `unreachable[]` `{rootCause, reason, firstUnreachable, states: [{state, reason, firstUnreachable}]}`.
- Links the publish gate checks: every `id` in findings, notRetested, handFound and table rows, and
  `priority`.

Other snapshot notes: `issues.json` `overlays` has an entry for every hand-found issue (`audit` null
when never verified); `meta.json` `lighthouse` may be null; generated issues' `audit.evidence` holds
the full live evidence over several lines, ending with the verifier's reason. In diff.json, a
withdrawn entry's `reason` is the drop `reasonCode`; a not-retested entry's reason joins its causes.

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
  - `axeCriteria: {ruleId: [criteria]}`: the WCAG criteria of each rule in `axeRules`, from axe-core
    4.13.0's `wcagNNN` tags (publish's coverage table drops a criterion outside `wcag.json`, such as
    2.1.3 on `scrollable-region-focusable`). `customChecks: {ruleId: check module}`: which check
    (`target`, `tabwalk`, `dialog` or `status`) produces a custom rule, so `diff` and `publish` can
    tell whether the check behind a rule completed.
  - `fullCoverage: {criterion: [ruleIds]}`: a tested criterion counts as fully covered only when
    every listed rule ran; any other tested criterion is partial. `failureModes: {criterion: text}`:
    the failure modes the checks cover for each criterion (R20), shown in the report's coverage table.
  - `factDocs: {placeholder: meaning}`: the shared fact vocabulary. `group` computes every fact a
    finding's family lists into `finding.facts`; templates are filled with `str.format(**facts)`.
  - The fact `criterion` is the finding's criterion that comes first in its family's `criteria`.
- `hand-found.json`: `{issueId: {family, criteria, why}}` for every `category: "a11y"` issue in
  `--data`. `group` fails when one is missing.
- Findings from judged announced-as records (KTD20) use rule id `announced-as`; a judgment may flag
  only the families `name`, `role-state` and `structure`.
- R20 names 1.4.12 as not tested, so `checks/axe.mjs` disables `avoid-inline-spacing`.
- `checks.json` `incomplete`: `facts`, `messageKeyText` (messageKey → words, at most 36
  characters), `noMessageKey`, and `templates` (title, detail, evidence, copy) for incomplete groups.

## Shared browser helpers (lib/)

- `lib/browser.mjs`: `launch()`, `newContext(browser)` (KTD4), `envFor(browser)`, `trackRequests(page)`
  (arm right after `newPage()`; counts first-party requests only), `settle(page, cdp, {tracker})`,
  `injectMotionOff(page)`, `parkPointer(page)`, `cdpSession(context, page)`, `readBuildHash(page)`.
- `lib/scope.mjs`: `armLayer(page)` before a state's final hop, then `findLayer(cdp, kind)` →
  `{scope, role, name, xpath}` (the raw file's `layer`). Without arming, only overlay roles count.
- `lib/occurrence.mjs`: `describeElements(cdp, fnSource, args, {layerXPath})` returns occurrences
  (DOM facts plus the accessibility tree's role and name) for the elements an in-page function
  returns; `describeAXNodes(cdp, nodes, {layerXPath})` does the same for non-ignored accessibility
  tree nodes, taking role and name from each node; `describeEvaluated(cdp, expression, objectGroup,
  {layerXPath})` describes the one element an expression evaluates to and keeps its handle in
  `objectGroup` until `releaseGroup(cdp, objectGroup)`. `describeElements` and `describeAXNodes` share
  one object group, so they never run at once on a CDP session. `BY_XPATH` resolves xpaths (SVG
  included); `XPATH_FN` computes an element's xpath; `findExpr(xpath)` is the Runtime.evaluate
  expression for the node at an xpath and `backendIdOf(cdp, xpath)` returns its backend id or null.
- `lib/results.mjs`: `customResult(check, rule, basis, occurrence, evidence)` builds a custom check's
  result, with `criteria` from `checks.json` `customCriteria` and `messageKey: null`.

## Check modules (U2, U3) and replay (U4), as the sweep (U5) calls them

Every module in `tools/a11y/checks/` exports `id` (its check-module id) and
`async run(ctx) -> {results, records, detail, completed}`:

- `results`: result objects (schemas.md "Result"); `records`: announced-as records (only `ax`);
  `detail`: what goes into the raw file's `checks.<id>`; `completed`: false when the check started
  and could not finish, or ran but did not cover its scope (the raw file lists it under
  `families.incomplete`).
- `ctx` holds `page`, `cdp`, `tracker`, `kind`, `state` (the state id), `layer` (from
  `findLayer`), `env`, and what earlier checks produced: `axe` (the axe module's detail and
  results), `interactive` (the expected-interactive occurrences). The sweep runs them in this
  order: `status` collect first on toast states, then `axe`, `ax`, `interactive`, `target`,
  `tabwalk`, and last `dialog`, which works in fresh contexts. `ax` and `interactive` share one
  accessibility-tree read and one description per node for each state (`treeOf(ctx)` and
  `occurrencesOf(ctx, nodes)` in `checks/ax.mjs`).
- `checks/dialog.mjs` also gets `ctx.reopen()`: opens a fresh context replayed to the parent state
  and returns `{context, page, cdp, tracker}` (the caller closes it), and `ctx.trigger(page)`:
  returns a Playwright Locator for the final hop's trigger.
- `checks/status.mjs` additionally exports `async arm(page)` (before the final hop) and its `run`
  collects what the observer saw. It also exports `present(page)`: on toast, confirmation and
  validation-error states the sweep calls it after each later check, and `false` lists that check
  as incomplete (the message vanished before it finished).
- `checks/dialog.mjs` reads `ctx.finalHop` (the recipe's final step; a `hover` step marks a hover
  popover); `checks/tabwalk.mjs` reads `ctx.shotsDir` (absolute `runs/<id>/shots`) and `ctx.state`,
  and writes `<state>.tabstops.png` there for backward jumps. The Tab walk changes the page (it
  presses Esc on a revisit), so it stays the last check on the shared context.
- `checks/tabwalk.mjs` reports `completed: false` when the walk did not cover its scope: it stopped
  at the press limit, found a keyboard trap, or came back round to a stop later than its first (the
  stops past that loop were never visited). `checks/dialog.mjs` reports `completed: false` when a
  focus rule the layer calls for never ran: Tab never reached the trigger or the trigger never opened
  the layer, focus never got inside a modal layer or inside a popover that holds anything focusable,
  or the layer never closed (Esc on a menu or popover; Esc, then the close control, on a modal or
  drawer). Either way the results found are kept, the state is still audited, and `diff` reports an
  absent published finding there as not retested, never fixed.

`recipe.mjs` exports the interpreter the sweep, `reach` and `probe` share:

- `loadRecipes(ws) -> {stateId: recipe}` (all areas merged) and `chain(recipes, stateId)` → the
  recipes from the root to the state.
- `async replay(page, ctx, stateId, {recipes, policy, beforeFinalHop, untilParent, layerKind})` →
  `{status: "reached" | "step-failed" | "fingerprint-mismatch", step, message, fingerprint}`.
  It navigates to the root, runs every hop with `settle` after each, arms the layer
  (`armLayer`) and calls `beforeFinalHop(page)` (the sweep arms the status observer there on status
  kinds), runs the final hop, settles,
  injects the motion-off CSS and compares the fingerprint. `untilParent: true` stops at the
  parent state (for the dialog check). `ctx` is `{cdp, tracker, baseUrl}`. It refuses a step the
  policy forbids before running it (`step-failed` with the reason). Before a press it also refuses a
  key that would type or paste into the focused field, and Enter or Space on a focused element it
  would refuse to click or inside a frame from another origin. With `layerKind` equal to the
  kind the fingerprint used, a reached result also carries `layer` (`findLayer`'s result), which the
  sweep uses instead of finding the layer again.
- `async resolveLocator(page, locator) -> Locator` (KTD6 order, name templates; async because the
  fallback order needs a count per strategy, so callers such as `ctx.trigger(page)` await it).
- `replay` also accepts `kind` and `edges` overrides and returns `mutates`. Step 0 is the root load;
  hop steps follow in chain order. Recipes must come from `loadRecipes(ws)`, which attaches the map's
  edges and kinds the replay needs to refuse hops on no recorded edge. Roots need recipe entries too
  (`recordedBy: "root"`, created by `recipe draft`), because `chain()` starts from the root's recipe.
- The allowed origins are `policy.allowedOrigins` plus the `--base-url` origin.
- `async captureFingerprint(page, cdp, kind, layer = null) -> fingerprint` (pass `layer` when the
  caller has just found that kind's layer) and `matchFingerprint(a, b) -> {ok, why}`
  live in `fingerprint.mjs`.

## Stage modules

`audit.py`'s `STAGES` table names every command, its module and its owning unit. Adding a stage
module never requires editing `audit.py` or `status.py`.

- A Python stage module exposes `cli(ws, sub, argv) -> int`. `ws` is a `lib.ws.Workspace`, `sub`
  is the subcommand or `None`, and `argv` holds the stage's own options, parsed by the module with
  `argparse`. It may also expose `status(ws, run_id) -> dict`, which `status.py` calls and prints
  under the module's name. A status function reads files only and never writes, and returns
  `{"pending": true, …}` when the run holds none of its inputs or outputs yet.
- A Node stage module runs as `node tools/a11y/<module>.mjs [sub] --workspace … --data … --map …
  --base-url … [--run …] [stage options]` and parses them with `lib/cli.mjs`'s `parse()`; `reach`,
  `probe`, `recipe` and `draft` run their `main` through `runMain(import.meta.url, label, main)`. It prints
  a JSON summary on stdout as its last line and exits non-zero on failure. Every Node module answers
  a `status` subcommand by printing one JSON object (it may be `{}`) and doing nothing else;
  `status.py` prints it under the module's name.
- Python modules import shared code as `from lib.canon import …` and `from lib.ws import …`;
  `audit.py` and `tests/pathshim.py` put `tools/a11y/` and `tools/` on `sys.path`.
