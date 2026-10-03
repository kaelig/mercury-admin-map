# Accessibility audit tooling

A repeatable WCAG 2.2 AA audit of every captured state of demo.mercury.com's Admin view: 915 states, at 1440×900 in light theme. A scripted Playwright sweep replays a recorded recipe for each state and measures it: axe-core rules and contrast, a keyboard Tab walk, a dialog focus check, target size, status messages, and "announced as" records from Chrome's accessibility tree. Sonnet agents then try to disprove every finding on the live demo. Verified findings are published as one snapshot that `build.py` merges into the map, the ticket export and a standalone report at `dist/a11y/index.html`.

One command surface runs every stage:

```sh
python3 tools/a11y/audit.py --help          # every stage, in the order the a11y-audit skill runs them
python3 tools/a11y/audit.py status          # progress from files; run it first after any interruption
npm run a11y:test                           # unit and fixture tests
```

File formats live in [schemas.md](schemas.md). The plan behind all of this is `docs/plans/2026-10-02-1018-feat-a11y-audit-plan.md`.

## Setup

From the repo root:

```sh
npm install                        # playwright 1.62.1 and axe-core 4.13.0, pinned exactly
npx playwright install chromium    # only if Playwright's Chromium is not cached yet
python3 build.py                   # writes build/map.json, which every stage reads
python3 tools/a11y/audit.py preflight
```

`preflight` checks the map, the two pinned packages and the demo, and prints the demo's build hash. Python stages use the standard library only. `reach`, `probe` and the agents also need `agent-browser` on `PATH`. A session that `reach` or `probe` starts can only `open` the hostnames of `policy.allowedOrigins` and the `--base-url` origin: both start it with agent-browser's `--allowed-domains`. agent-browser sets that when the session's daemon starts, so run `reach` before any other command in a new session. It refuses `agent-browser open` on another domain and nothing else: a click, a script or a sub-resource can still leave the demo.

Every stage takes the same options:

| option | default | what it sets |
|---|---|---|
| `--workspace` | `a11y/` | where policy, recipes, runs and the snapshot live |
| `--data` | `data/` | the area data hand-found issues come from |
| `--map` | `build/map.json` | nodes, edges and journeys |
| `--base-url` | `https://demo.mercury.com` | the site browser stages open |
| `--run` | the newest run with a `states.json` | which run a stage works on |

## Stages

In the order the audit runs them. `audit.py` is short for `python3 tools/a11y/audit.py`.

| stage | command | writes |
|---|---|---|
| Progress | `audit.py status` | nothing |
| Preflight | `audit.py preflight` | nothing |
| Plan | `audit.py plan` | `runs/<id>/run.json`, `states.json` |
| Refresh queue | `audit.py recipe queue` | nothing |
| Draft recipes | `audit.py recipe draft [state...]`, or `--ready [--shard i/8]` | `recipes/<area>.json`, `draft.jsonl` |
| Check one recipe | `audit.py recipe check <state> [--recipe '<json>']` | `recipes/<area>.json`, `shots/<state>.recipe.png` |
| Reach a state | `audit.py reach <state> --session <name>` | nothing: replays into an agent-browser session |
| Sweep | `audit.py sweep` | `raw/<state>.json`, `outcomes.json` |
| Lighthouse calibration | `audit.py calibration add --file <comparison.json>` | `lighthouse.json` |
| Records | `audit.py records prepare`, then `audit.py judgment add --file <out>` | `records.jsonl`, `batches/records-NNN.json`, `judgments.jsonl` |
| Group | `audit.py group` | `findings.json` |
| Canary | `audit.py canary prepare`, then `audit.py canary score --file <out>` | the canary's own workspace, `canary.json` |
| Verify | `audit.py verify prepare`, `audit.py verify status` | `batches/verify-NNN.json` |
| Verdicts, decisions | `audit.py verdict add --file <out>`, `audit.py decision add --file <out>` | `verdicts.jsonl`, `decisions.jsonl` |
| Probe | `audit.py probe <state> --kind contrast\|target\|focus\|ax --xpath <xpath>` | nothing: prints one measurement |
| Diff | `audit.py diff` | `diff.json` |
| Publish | `audit.py publish` | `published/` |
| Report | `audit.py report` (`build.py` calls it too) | `dist/a11y/index.html` |

## Draft

```sh
python3 tools/a11y/audit.py recipe draft [--run <id>] [<state>...]
python3 tools/a11y/audit.py recipe draft [--run <id>] --ready [--shard <i>/<n>]
```

Draft turns edge labels into recipe steps and submits them through `recipe check`. With state ids it drafts those states; with neither ids nor `--ready` it drafts the whole refresh queue.

Every attempt appends one line to `runs/<id>/draft.jsonl`: the state, its parent, a hash of the parent's accepted recipe chain, the label, the result (`accepted`, `review` or `queued`), the agent-queue reason and the date. A state's last line is the `draft` reason its recorder batch needs. An id not in `states.json` gets no line. If the browser fails to launch, the pass stops with exit 1 and logs nothing for the state that needed it, so the next pass tries that state again.

`--ready` drafts only the queue states worth a try now: roots, and states whose parent's chain is accepted, with no line in `draft.jsonl` against the same parent recipe. It skips a state whose recipe the conductor rejected. When a recorder re-records a parent, its hash changes and its children are tried again. Each round prints how many states it selected and skipped; `--ready` repeats rounds until none is left, so the children of what it accepts get drafted in the same run. The last stdout line adds `ready: {rounds, selected, skipped}`.

`--ready` keys attempts on the state and its parent recipe only. So it does not try again a `tool-error` attempt (a page or browser that crashed mid-state), a root it tried once, or a state that failed on a typed value since added to `policy.fixtureValues`. Name those ids to draft them again. Run `--ready` between recorder waves, not during one: it cannot tell which queue states a recorder is still recording.

`--shard i/n` splits a `--ready` pass across n processes by a hash of the state id: start `--shard 1/8` through `--shard 8/8` at once and wait for all of them. A shard that finishes early does not wait for parents another shard accepts later, so `audit.py recipe draft status` may still show `ready` above 0: run the pass again until it shows 0, and again after each recorder wave. That status (also under `draft` in `audit.py status`) shows `attempts`, the lines in `draft.jsonl`, and `ready`, the states the next pass would draft. Format details: [schemas.md](schemas.md).

## Sweep

```sh
python3 tools/a11y/audit.py sweep [--run <id>] [--workers 8] [--timeout 180000] [--state <id> ...]
```

- `--workers` sets how many browsers run at once (default 8). Each state gets a fresh browser context, and starts once its parent's raw file exists.
- `--timeout` is how long one step of a state may take, in milliseconds: the replay, or one check (default 180000). A step that runs over is a hang: the worker's browser restarts, the state is tried twice more, then written unreachable with `tool-error`.
- `--state` sweeps the named states again, even with a valid raw file, plus any ancestor they need that has none. Repeat it for several states. Nothing else runs.
- To resume, run the same command again. Only states without a valid raw file run. Valid means it parses, names its state and carries the current harness hash.

Progress goes to stderr, one line per state. The last line on stdout is `{run, audited, unreachable, pending, interrupted, ms}`. Exit codes:

| code | meaning |
|---|---|
| 0 | every state has a raw file and `outcomes.json` is written (with `--state`: the named states were swept) |
| 1 | an error, or states left without a raw file |
| 2 | a usage error, such as a `--state` id that is not in `states.json` |
| 75 | the circuit breaker tripped: the demo answered 403 or 429, served a challenge page, or failed three root pages in a row |
| 130, 143 | stopped by Ctrl-C (SIGINT) or SIGTERM (129 for SIGHUP). Rerun to resume |

After a breaker trip, the sweep writes nothing more. The states it skipped get no unreachable result: they stay pending and run when you resume. A root page that failed to load is held back the same way, and written unreachable only if the sweep ends without a trip.

Stop a sweep with Ctrl-C or SIGTERM. `audit.py` hands its process to the sweep, so a signal to its pid reaches the sweep, which closes its browsers and writes nothing more. A sweep stopped with SIGKILL (`kill -9`) cannot close its browsers, and headless Chromium keeps running. `pgrep -fl ms-playwright/chromium` lists them; `kill -9` each one that no running sweep or test owns.

## Walkthrough: the fixture app from plan to publish

The fixture app in `tools/a11y/fixtures/app/` is a small multi-screen site built to test recipes: a dashboard with a menu, a send modal that submits to a toast, a hover tooltip, a cards page with two modals, a transactions page with a filter menu and a drawer, a reports page, and a Books page that needs a setup step. It has hand-written recipes and its own captured screenshots, and its dialogs and menus do not move focus when they open, so the sweep has real failures to find. This walkthrough runs its 14 states through every stage in a temporary workspace, so the committed `a11y/` is never touched. Follow one state, `cards.edit-limit-modal`, through the files as you go.

In a real run, agents judge records and verify findings. Here, three short scripts stand in for them, and each one says so. The commands work in bash and zsh, from the repo root.

**1. Serve the fixture app,** in its own terminal:

```sh
node tools/a11y/fixtures/server.mjs --port 4173 app
```

It prints `{"origin":"http://127.0.0.1:4173","origin2":"http://localhost:<port>"}` and serves until you stop it.

**2. Make a workspace,** in a second terminal:

```sh
W=$(mktemp -d)
mkdir -p "$W/ws/recipes" "$W/data"
cp tools/a11y/fixtures/app/recipes/*.json "$W/ws/recipes/"
python3 - "$W/data" <<'EOF'
import json, sys
for area in json.load(open("tools/a11y/fixtures/app/map.json"))["areas"]:
    with open(f"{sys.argv[1]}/{area['area']}.json", "w") as f:
        json.dump(area, f, indent=1)
EOF
A=(--workspace "$W/ws" --data "$W/data" --map tools/a11y/fixtures/app/map.json --base-url http://127.0.0.1:4173)
```

The area data comes from the fixture map, so publish knows every screen. The workspace has no `policy.json`, so stages use the committed `a11y/policy.json`. The base URL's origin is always allowed.

**3. Plan.**

```sh
python3 tools/a11y/audit.py plan "${A[@]}"
RUN=$(ls "$W/ws/runs")
```

It reports 14 states, 4 roots, 10 that need a recipe and 3 behind a mutating step. `$W/ws/runs/$RUN/states.json` gives each state its root, parent and path: `cards.edit-limit-modal` hangs off the root `cards.index`.

**4. Recipes.**

```sh
python3 tools/a11y/audit.py recipe queue "${A[@]}"
python3 tools/a11y/audit.py recipe check cards.edit-limit-modal "${A[@]}"
```

The queue holds one state, `home.chart-tooltip`, as `no-recipe`: the fixture has no recipe for its hover tooltip, so the sweep will write it unreachable. Every other state has an accepted recipe. `recipe check` replays one recipe twice in fresh browsers, records its fingerprint, compares the screenshot with `fixtures/app/captures/cards.edit-limit-modal.png`, and prints `accepted` with the similarity score. Its screenshots land in `$W/ws/runs/$RUN/shots/`.

**5. Sweep.**

```sh
python3 tools/a11y/audit.py sweep "${A[@]}"
python3 tools/a11y/audit.py status "${A[@]}"
```

The sweep writes one raw file per state, then `outcomes.json`. It takes about 20 seconds. `status` shows 13 states audited and 1 unreachable (`home.chart-tooltip`, `no-recipe`) under `stages.sweep`, and `outcomes.coverage: "ok"`. Open `$W/ws/runs/$RUN/raw/cards.edit-limit-modal.json`: the open dialog it scanned (`layer`), the checks that completed, every result with its measured evidence, and the announced-as records.

**6. Records.** `records prepare` deduplicates the announced-as records and writes judge batches. The stand-in judge marks every record ok:

```sh
python3 tools/a11y/audit.py records prepare "${A[@]}"
python3 - "$W/ws/runs/$RUN/batches" <<'EOF'
# Stand-in for the record judges: every record is ok.
import glob, json, sys
for path in sorted(glob.glob(f"{sys.argv[1]}/records-*.json")):
    batch = json.load(open(path))
    with open(path[:-5] + ".out.jsonl", "w") as out:
        for r in batch["records"]:
            out.write(json.dumps({"sig": r["sig"], "verdict": "ok", "criterion": None, "family": None,
                                  "reason": "walkthrough stand-in", "batch": batch["batch"],
                                  "model": "person", "prompt": "walkthrough"}) + "\n")
EOF
for f in $(find "$W/ws/runs/$RUN/batches" -name 'records-*.out.jsonl' | sort); do
  python3 tools/a11y/audit.py judgment add --file "$f" "${A[@]}"
done
```

**7. Group.**

```sh
python3 tools/a11y/audit.py group "${A[@]}"
```

`findings.json` holds one finding per rule per shared element (axe's incomplete results: one per rule and message), with every state it occurs on, its primary state, the states a verifier samples, and its default severity. Look for `cards.edit-limit-modal`: the modal leaves focus on the page when it opens, so expect a dialog focus finding there.

**8. Canary.** Verification starts with a canary batch whose answers are known. `canary prepare` builds it in its own workspace, here inside `$W` (`--out`) so the cleanup removes it too. The stand-in copies the expected answers, which makes this canary worthless as a check: a real run gives the batch to a verifier agent. Score it with the walkthrough's options, so `canary.json` lands in this workspace:

```sh
python3 tools/a11y/audit.py canary prepare --out "$W/canary" "${A[@]}" > "$W/canary.json"
OUT=$(python3 - "$W/canary.json" <<'EOF'
# Stand-in for the canary verifier: the expected answers.
import json, sys
batch_path = json.load(open(sys.argv[1]))["batch"]
batch = json.load(open(batch_path))
expected = json.load(open("tools/a11y/fixtures/canary/expected.json"))
out_path = batch_path[:-5] + ".out.jsonl"
with open(out_path, "w") as f:
    for e in batch["findings"]:
        outcome, _, after = expected[e["key"]]["expected"].partition(":")
        line = {"key": e["key"], "issueId": e["issueId"], "source": e["source"], "outcome": outcome,
                "severityBefore": e["severityDefault"], "severityAfter": after or e["severityDefault"],
                "reason": expected[e["key"]]["why"], "states": e["states"], "build": "unknown",
                "model": "person", "batch": batch["batch"], "prompt": "walkthrough", "attempt": 1}
        if outcome == "drop":
            line.update(reasonCode="tool-false-positive", counterEvidence="shot:walkthrough")
        if e["source"] == "hand-found":
            line["counterEvidence"] = "shot:walkthrough"
        f.write(json.dumps(line) + "\n")
print(out_path)
EOF
)
python3 tools/a11y/audit.py canary score --file "$OUT" "${A[@]}"
```

`canary score` prints `"pass": true` and writes `$W/ws/runs/$RUN/canary.json`.

**9. Verify.** `verify prepare` batches the findings, up to 10 per batch sharing an area and a primary state. The stand-in verifier confirms each one at its default severity. The fixture app has no hand-found issues, so there are no attachment proposals; `decision add` runs anyway, as it does in a real run.

```sh
python3 tools/a11y/audit.py verify prepare "${A[@]}"
python3 - "$W/ws/runs/$RUN/batches" <<'EOF'
# Stand-in for the verifiers: confirm every finding at its default severity.
import glob, json, os, sys
for path in sorted(glob.glob(f"{sys.argv[1]}/verify-*.json")):
    if os.path.exists(path[:-5] + ".out.jsonl"):
        continue
    batch = json.load(open(path))
    with open(path[:-5] + ".out.jsonl", "w") as out:
        for e in batch["findings"]:
            out.write(json.dumps({"key": e["key"], "issueId": e["issueId"], "source": e["source"],
                                  "outcome": "confirm", "severityBefore": e["severityDefault"],
                                  "severityAfter": e["severityDefault"], "reason": "walkthrough stand-in",
                                  "states": e["states"], "build": "unknown", "model": "person",
                                  "batch": batch["batch"], "prompt": "walkthrough",
                                  "attempt": e["attempt"]}) + "\n")
EOF
for f in $(find "$W/ws/runs/$RUN/batches" -name 'verify-*.out.jsonl' | sort); do
  python3 tools/a11y/audit.py verdict add --file "$f" "${A[@]}"
  python3 tools/a11y/audit.py decision add --file "$f" "${A[@]}"
done
python3 tools/a11y/audit.py verify status "${A[@]}"
```

`verify status` shows every finding verdicted, none pending, and the canary passed.

**10. Diff and publish.**

```sh
python3 tools/a11y/audit.py diff "${A[@]}"
python3 tools/a11y/audit.py publish "${A[@]}"
python3 tools/a11y/audit.py status "${A[@]}"
python3 tools/a11y/audit.py report --dist "$W/dist" "${A[@]}"
```

With no earlier snapshot, `diff.json` lists every finding as `new`. Publish checks its gates and writes `$W/ws/published/`: `issues.json` (the generated `a11y` issues, each with its WCAG criteria, ticket copy and audit block), `report.json` (the report model), `registry.json` (finding keys to issue ids) and `meta.json`. Find the three issues whose `nodeIds` include `cards.edit-limit-modal`: focus not moved into the dialog, Tab leaving it, and focus lost when it closes. `status` now reports "already published". `report` renders `$W/dist/a11y/index.html`; its links to the map do not resolve outside a built `dist/`.

**11. Clean up.** Stop the server, then `rm -rf "$W"`. Do not run `build.py` against this snapshot: `build.py` writes the real `dist/` and `build/`.

`tools/a11y/tests/e2e.test.mjs` runs these steps and more: hand-found issues, a scripted flag and attachment, a sweep stopped and resumed, and a second run against the first snapshot.

## The whole audit

Run the `a11y-audit` skill in Claude Code (`.agents/skills/a11y-audit/SKILL.md`, linked from `.claude/skills/`). It runs the stages above against demo.mercury.com in this order: preflight, plan and recipes, sweep, Lighthouse calibration, records and judging, group, verification with the canary first, diff, publish, `python3 build.py`, and a summary that ends with the deploy command it never runs. Recorders, record judges and verifiers run on Sonnet; their prompts are in the skill's `references/`. Recorders run headless through `tools/a11y/record-headless.sh <run> <batch>`, which prints the batch's token usage (`tools/a11y/stream_usage.py` reads it again from `batches/<batch>.stream.jsonl`).

On the first run the skill stops before agents record recipes in bulk, and shows the mutation policy and a cost estimate. It also stops when a form submitted in one browser shows up in a fresh one, when the demo blocks headless access twice, when a state needs an action outside the policy, and when two sweeps of the same demo build differ after the harness waits are fixed. Nothing is deployed without Kaelig's go.

You can run every stage by hand from `SKILL.md`. Only recording, judging and verification need an agent.

## Resume

Run `python3 tools/a11y/audit.py status` first. It reads every stage's progress from files and names what is pending. Then run the pending stage's command again:

- `recipe draft --ready` tries only states it has not tried against their parent's current recipe, so rerun it after any stop.
- `sweep` runs only states without a valid raw file. A raw file made by different harness code (any change under `checks/` or `lib/*.mjs`, to `sweep.mjs`, `recipe.mjs`, `fingerprint.mjs` or `checks.json`, or a new axe-core or Playwright version) is invalid, so a code fix re-sweeps every state.
- `records prepare` and `verify prepare` batch only what has no judgment or verdict and is not already out in a batch. A batch counts as returned once its `.out.jsonl` exists; write an empty one for an agent that died.
- The `add` commands append only lines their ledger does not hold yet, so adding a file again is safe (repeats count as `duplicates`). Corrected lines go in a new file. `verify status` lists returned batches not added yet (`batches.returnedNotAdded`), and `verify prepare` refuses until they are added.
- `group` records in `findings.json` the harness hash, the sweep outcomes and the raw files it read. After any sweep of the run, `verify prepare`, `diff` and `publish` refuse it until `group` runs again. They also refuse a run swept with other harness code than the current one until `sweep`, then `group`, run again.
- `publish` rebuilds everything from the run's ledger. If `status` says "publish incomplete, rerun publish", rerun it.

Once a run has a second run beside it, pass `--run <id>` to every stage: the default is the newest run.

## Where files live

| path | what | in git |
|---|---|---|
| `tools/a11y/` | the code, `checks.json` (check families, default severities, text templates, rule-to-criterion and coverage tables), `wcag.json` (the 55 A and AA criteria), `hand-found.json` (family and criteria of each hand-found issue), `schemas.md` | yes |
| `a11y/policy.json` | the mutation policy: denylist, fixture values, mutating verbs, isolation result, root exclusions | yes |
| `a11y/recipes/<area>.json` | one replay recipe per state, editable by hand | yes |
| `a11y/runs/<id>/` | a run's ledger: `run.json`, `states.json`, `draft.jsonl`, `outcomes.json`, `findings.json`, `records.jsonl`, `judgments.jsonl`, `verdicts.jsonl`, `decisions.jsonl`, `canary.json`, `lighthouse.json`, `diff.json`, `batches/` | yes |
| `a11y/runs/<id>/raw/` | one measurement file per state, tens of megabytes per run | no |
| `a11y/runs/<id>/shots/` | replay screenshots, tab-stop overlays, verifier screenshots | no |
| `a11y/published/` | the snapshot `build.py` merges: `issues.json`, `report.json`, `registry.json`, `meta.json` | yes |
| `dist/a11y/index.html` | the rendered report | no |
| `.agents/skills/a11y-audit/` | the skill and its agent prompts | yes |

`build.py` and `tools/export_agent_data.py` read the snapshot through `tools/mapdata.py`. Set `A11Y_SNAPSHOT` to read another snapshot directory. `tools/a11y/tests/data/snapshot/` is a test snapshot with invented findings on real screens: its `meta.json` has `"testData": true`, so a build from it shows a test-data banner on the map and the report, and prints a `NOTE` line. A `data/*.json` that does not parse stops `tools/export_agent_data.py` run on its own. `build.py` leaves that area out of the map and prints `WARN <file>: invalid JSON (<error>)`, and runs the export with `MAP_SKIP_INVALID_DATA=1`, so the export leaves out the same file.

## Reading the report

Open `dist/a11y/index.html` after `python3 build.py`. Its sections, in order:

1. **Summary.** Published findings by severity. High: a keyboard, screen-reader or low-vision user cannot complete the task. Medium: the task is possible but hard. Low: friction, with an AA criterion still failing. Screen count never raises severity; it only orders findings within one.
2. **Coverage.** The run date, the date the map's screenshots were captured, states audited and unreachable, every demo build hash the run saw, the Lighthouse calibration result, and the WCAG 2.2 AA criteria tested, fully or partly, and not tested. It also names what was never tested: zoom and reflow, real screen readers, mobile and dark theme.
3. **Changes since the last published run.** Counts of new, persisting, re-rated, reappeared, not retested, withdrawn and fixed findings, each linking to its rows.
4. **Findings by WCAG principle,** with criterion, severity, screen count, recommended fix and the full live evidence.
5. **Contrast, keyboard, dialog and screen-reader tables.** Arrow-key navigation inside menus and tabs reads "not tested". The screen-reader table is computed from Chrome's accessibility tree, not a screen reader, and lists advisory records that fail no AA criterion.
6. **Priority fixes,** by severity, then screen count.
7. **Not retested, withdrawn and fixed.** Not retested: a published finding this run could not check, because its screens were unreachable or its verification did not finish. It stays in the map and is never counted as fixed. Withdrawn: the scanner still reports it, and a verifier disproved it. Fixed: its check completed on every earlier screen and found nothing.
8. **Hand-found issues not reproduced,** with the verifier's reason. They stay in the map with their original text.
9. **Unreachable states,** grouped by root cause, with the run each one first became unreachable.

Every new or persisting row links to `../index.html?issue=<id>`, which opens the map with that issue's screens highlighted. Fixed and withdrawn findings and unreachable states have no link. "No failures found by these checks" is not a pass. Every finding is a hypothesis until it is checked against Mercury's internal conventions.

## Tests

```sh
npm run a11y:test                                              # everything
node --test tools/a11y/tests/sweep.test.mjs                    # one Node test file
node --test tools/a11y/tests/e2e.test.mjs                      # every stage on the fixture app, about 2 minutes
cd tools/a11y && python3 -m unittest tests.test_verdict -v     # one Python test module
```

Browser tests run against the fixture sites in `tools/a11y/fixtures/`, served by a small static server inside the tests, and every stage test uses a temporary workspace. Some tests skip with a message when a local tool is missing: the `plan` test needs `build/map.json`, and the `reach` test, the `probe --session` test and the allowed-domains tests need agent-browser on `PATH`. `tests/test_skill_parity.py` checks that the skill names only stages `audit.py` lists, that each agent prompt starts with its version id, and that the verifier's drop reason codes match `verdict.py`.
