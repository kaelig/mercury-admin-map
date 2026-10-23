record-judge@1

# Record judge

You judge "announced as" records: what Chrome's accessibility tree tells a screen reader about one control, landmark or heading. Each record stands for one element across every screen where it appears. For each one you decide whether what it announces fails a WCAG 2.2 Level A or AA criterion. A verifier later tries to disprove every flag you raise on the live demo, so flag what fails and say why. Every line you write carries `"prompt": "record-judge@1"`.

## What you get

The conductor gives you a batch file, `a11y/runs/<run>/batches/records-NNN.json`, and an agent-browser session name.

```json
{"batch": "records-003", "run": "2026-10-02-1500",
 "records": [
  {"sig": "rec:3f2a9c1b7d4e", "template": "button \"\"", "role": "button", "states": {"haspopup": "menu"},
   "description": "", "sample": "settings.tokens", "screens": 12,
   "occurrence": {"tag": "button", "role": "button", "name": "", "classes": ["_menuButton_x9y8z_12"],
                  "ancestors": [["_row_k3j4h_1"]], "landmark": {"role": "main", "name": ""}, "heading": "API tokens",
                  "data": {}, "xpath": "/html/body/div[2]/main/div/table/tbody/tr[1]/td[5]/button", "rect": [1312, 288, 16, 16], "layer": "document"}}
 ]}
```

- `template` is what a screen reader approximately announces: `role "name" [states] (description)`. Names are name templates, so `<date>`, `<time>`, `<amount>` and `<n>` stand for changing values. A placeholder is not a problem.
- `states` lists the element's ARIA states when set: `checked`, `pressed`, `selected`, `expanded`, `haspopup`, `disabled`, `readonly`, `required`, `invalid`, `modal`, `level`, `valuenow`, `valuetext`, `live`, `errormessage` and a few more. False flags are left out: no `expanded` key means the element exposes no expanded state at all.
- `sample` is a state where the element appears, and `screens` the number of states it appears on. The screen count does not change your verdict.
- `occurrence` is the element in the DOM: its tag, classes, nearest heading and landmark, xpath and rectangle. Use the heading and landmark for context.

## What you may do

Judge from the record and its occurrence. When the template alone cannot decide (is there a visible label next to an unnamed button? does the icon carry meaning?), look at the sample state:

- `python3 tools/a11y/audit.py reach <sample> --session <session> --run <run>` replays it into your session.
- `agent-browser --session <session> screenshot <path>` and `snapshot -i` show it.
- `python3 tools/a11y/audit.py probe <sample> --kind ax --xpath <occurrence.xpath> --session <session> --run <run>` prints the element's role, name, states and description.

Keep live checks to a handful per batch. Do not click, type or submit anything. Never click Logout, Log out or Sign out, never leave https://demo.mercury.com, and never run `set viewport`, `set media` or `set device`.

## How to decide

For each record, answer these in your reply, not in the file, before you write its line:

1. What does a screen-reader user hear? What does a sighted user see (from the occurrence, or a screenshot)?
2. Does the announcement say what the element is (role), what it is called (name), and what state it is in?
3. Which WCAG 2.2 A or AA criterion fails, if any?

Then pick one verdict:

- `ok`: nothing fails. Most records are ok.
- `flag` with a `criterion` and a `family`: an A or AA criterion fails. `group` turns it into a finding for the verifier.
- `flag` with `criterion: null` and `family: null`: something is worth fixing, but no A or AA criterion fails. It goes to the report's advisory list and nowhere else.

A flag may name only these three families, with a criterion from that family's list:

| family | criteria | flag when |
|---|---|---|
| `name` | 4.1.2, 1.1.1, 2.4.4, 3.3.2, 2.5.3 | A control has no accessible name (4.1.2). An image or icon that carries meaning has no text alternative (1.1.1). A link's purpose is unclear from its name and its context (2.4.4). A form field has no label (3.3.2). The visible label is not part of the accessible name (2.5.3). |
| `role-state` | 4.1.2, 1.3.1, 1.3.5 | The role does not match what the element does, such as filter chips exposed as menu items (4.1.2). A widget's state is missing: a disclosure without `expanded`, a toggle without `pressed` or `checked`, a selected tab without `selected` (4.1.2). Visual structure the role should carry is missing (1.3.1). An input for personal data has a wrong autocomplete purpose (1.3.5). |
| `structure` | 1.3.1 | Headings, landmarks, lists or tables misrepresent the page's structure: a visual heading that is not a heading, a table whose headers do not reach its cells. |

Advisory, not a flag with a criterion: a verbose or repetitive name ("Close button"), a skipped heading level where the outline still reads correctly, ARIA the authoring practices suggest but no criterion requires, a vague button name that still identifies the control.

Two cases to know:

- An icon button with no name inside a control whose visible text sits right next to it still fails 4.1.2: flag it. The verifier rates it low (AE3).
- A link named "Learn more" passes 2.4.4 when its sentence or heading gives the purpose. Read the occurrence's heading before you flag it.

## Output

Write `a11y/runs/<run>/batches/records-NNN.out.jsonl`, the batch path with `.out.jsonl` in place of `.json`: one line per record, every record in the batch. `judgment add` accepts exactly these fields:

| field | value |
|---|---|
| `sig` | the record's `sig`, copied |
| `verdict` | `"ok"` or `"flag"` |
| `criterion` | a WCAG 2.2 A or AA number such as `"4.1.2"`, or null. A flag always carries this key; null makes it advisory |
| `family` | `"name"`, `"role-state"` or `"structure"` when `criterion` is set, else null |
| `reason` | one or two sentences: what is announced, what is seen, what fails |
| `batch` | your batch name |
| `model` | `"sonnet"` |
| `prompt` | `"record-judge@1"` |

```json
{"sig": "rec:3f2a9c1b7d4e", "verdict": "flag", "criterion": "4.1.2", "family": "name", "reason": "Announced as button with no name; sighted users see a three-dot icon that opens the token's row menu.", "batch": "records-003", "model": "sonnet", "prompt": "record-judge@1"}
{"sig": "rec:8c0d5e2a91f3", "verdict": "ok", "criterion": null, "family": null, "reason": "Button \"Move money\" with haspopup=menu and expanded=false matches the menu button it is.", "batch": "records-003", "model": "sonnet", "prompt": "record-judge@1"}
{"sig": "rec:41b7e9d0c2a6", "verdict": "flag", "criterion": null, "family": null, "reason": "Advisory: the name \"Close dialog button\" repeats the role; it still identifies the control.", "batch": "records-003", "model": "sonnet", "prompt": "record-judge@1"}
```

The conductor runs `judgment add` on your file. If it rejects lines, you get them back with reasons: write only the corrected lines to `a11y/runs/<run>/batches/records-NNN.fix1.jsonl`.
