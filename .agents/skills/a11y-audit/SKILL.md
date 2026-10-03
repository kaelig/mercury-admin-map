---
name: a11y-audit
description: Run, resume or refresh the WCAG 2.2 AA accessibility audit of demo.mercury.com's Admin view, and publish its verified findings into the Mercury Admin Map and its report. Use when asked to run or rerun the accessibility audit, continue an interrupted audit run, record or refresh replay recipes, verify a11y findings, or publish the audit snapshot.
---

# Accessibility audit

After any interruption, run `python3 tools/a11y/audit.py status` before anything else. It reads every stage's progress from files: sweep coverage, batches out and returned, the canary, pending attachment proposals, an incomplete publish, and whether the snapshot already holds this run. Resume at the first stage that is not done. Every stage resumes by running its command again.

You conduct. Recorders, record judges and verifiers are subagents on Sonnet: spawn them with the Agent tool and `model: "sonnet"`, and point each at its prompt in `references/`. You adjudicate: recipe reviews, second-opinion disagreements, proposals nobody decided, and every stop condition.

## Ground rules

- Every stage is `python3 tools/a11y/audit.py <command> [sub] [--run <id>] [options]`. Defaults: workspace `a11y/`, data `data/`, map `build/map.json`, base URL `https://demo.mercury.com`. `python3 tools/a11y/audit.py --help` lists the stages in the order below. `tools/a11y/schemas.md` defines every file.
- `--run` defaults to the newest run with a `states.json`. Once a second run exists (a determinism pair, a characterization run), pass `--run <id>` to every stage and every script.
- One writer per file. By hand you edit only `a11y/policy.json`, `tools/a11y/hand-found.json`, and a recipe's `status` and `review` during review. `verdicts.jsonl`, `judgments.jsonl` and `decisions.jsonl` change only through their `add` commands.
- `add` appends only the lines its ledger does not hold yet, so adding a file again is safe: repeats come back as `duplicates`. When an agent fixes rejected lines, it writes only the fixed lines to a new file, `batches/<batch>.fix1.jsonl`, and you add that file.
- A stage that needs a code fix gets it in its owning module, with a test (`npm run a11y:test`). Then resume. A change under `tools/a11y/checks/` or `tools/a11y/lib/*.mjs`, to `tools/a11y/sweep.mjs`, `tools/a11y/recipe.mjs`, `tools/a11y/fingerprint.mjs` or `tools/a11y/checks.json`, or a new axe-core or Playwright version, changes the harness hash, so the next sweep re-measures every state.
- Ask before anything reaches an audience. Deploying needs Kaelig's go. Research, reruns, rebuilds and drafts don't: do them, then say so.
- Each subagent gets its own agent-browser session, named after its batch. No two subagents share a batch, a session or a subtree.

## Stop conditions

Stop, report, and wait for Kaelig when one of these happens:

1. **First-run gate.** On the first run (no `a11y/published/meta.json`), stop before agents record recipes in bulk. Before the gate, only script stages and one pilot recorder batch of at most 15 non-mutating states run. Show the policy and the cost estimate (stage 2), then wait for go.
2. **Isolation fails.** A form submitted in one browser context shows up in a fresh one. The demo keeps submissions server-side, which breaks determinism for every stage.
3. **Access blocked.** demo.mercury.com answers 403 or 429, serves a challenge page, or fails root pages, and the sweep's circuit breaker trips again after one 10-minute pause and resume.
4. **Action outside the policy.** Reaching a state needs an action the policy forbids. Report the state ids and the action each one needs.
5. **Determinism failure.** Two sweeps on one build hash still differ after you fixed the harness waits. Report the differing fields.
6. **Deploy.** Never deploy. End with the prepared deploy command (stage 11) and leave it to Kaelig.

## 1. Preflight

```sh
python3 tools/a11y/audit.py preflight
```

It checks that `build/map.json` exists, that playwright and axe-core are installed at the versions `package.json` pins, and that `/dashboard` answers 200. It prints `build`, the demo's build hash. Write the hash down with the time: the sweep stage compares it.

- Map missing: `python3 build.py`. Packages wrong: `npm install`.
- Demo failing (403, 429, a timeout): wait 10 minutes and run preflight once more. A second failure is stop condition 3.

Resume: rerun. It writes nothing.

## 2. Plan and recipes

### Plan

```sh
python3 tools/a11y/audit.py plan
```

It creates run `YYYY-MM-DD-HHMM` with `run.json` and `states.json` and prints the counts: states, roots, states that need a recipe, states behind a mutating step, states with no path. Expect 915 states and about 120 roots. A `no-path` state stays unreachable; name it in the summary.

Record before you sweep a run. The sweep writes `unreachable(no-recipe)` for every state without an accepted recipe, and a resumed sweep skips every state that already has a raw file.

### Refresh queue

```sh
python3 tools/a11y/audit.py recipe queue
```

It lists each state with a reason: `no-recipe`, `stale` (an edge label on its path changed) or `unreachable` (its recipe broke in the previous published run). This queue is the work for this stage. On a later run with an empty queue, go to stage 3.

### Roots

`recipe draft` writes root recipes: it loads the root URL, records the fingerprint and compares the screenshot with the capture. Draft the roots first, then review the ones that land in `review`.

- The map's screenshots were captured in dark theme. The audit runs in light (R2). `recipe check` screenshots each replay in both themes and keeps the better score, so a low score is not a theme problem.
- A fresh session opens a "Try out Mercury" panel on `/dashboard`. The `home.index` root recipe closes it with one setup step and `"via": "home.try-panel-more"`. Any setup step in a root recipe needs `via`: a state with a recorded edge into the root, whose action the step follows.
- Some concrete page URLs reproduce nothing alone: a wizard step redirects, a draft exists only in the session that created it, a URL answers 404. Add each one to `policy.roots.exclude` with its reason, then run `python3 tools/a11y/audit.py plan --run <id> --force`. Those states then get a parent path and a recipe like any other state.

### Drafts

```sh
python3 tools/a11y/audit.py recipe draft [--run <id>] [--ready [--shard <i>/8]] [<state>...]
```

Draft turns edge labels ("Click X", "Choose X", "Type V in X") into steps, resolves them on the live parent, and submits them through `recipe check`. It can only draft a state whose parent chain is accepted. Every attempt appends a line to `a11y/runs/<id>/draft.jsonl`. A state's last line there gives its reason when the drafter gives up, and that reason is the recorders' starting point.

`--ready` drafts every queued state whose parent chain is accepted and that the drafter has not tried against that same parent recipe. It keeps going round by round, so newly accepted states make their children ready. Start it as 8 background processes, `--ready --shard <i>/8` for i = 1 to 8, wait for all of them, and repeat until `audit.py recipe draft --run <id> status` shows `"ready": 0`. A shard that exits 1 could not launch its browser and logged nothing: run it again. `recipe check` locks each area file while it writes, so parallel processes never drop each other's entries. Run `--ready` only between recorder waves, never while recorders hold states. A state that failed with `tool-error: <message>`, or on a typed value since added to `policy.fixtureValues`, is not retried by `--ready`: name its id to draft it again.

Resume: run the `--ready` passes again. Accepted recipes leave the queue.

### Reviews

`recipe check` sends a recipe to `review` when its similarity is under 0.90, when an agent recorded it and it mutates, and for a 10% sample of agent recipes. A `--recipe` candidate counts as the agent's whatever `recordedBy` it carries, and so does a root candidate with setup steps. A child cannot be checked until its whole chain is `accepted`, so review roots and parents first.

For each recipe in `review`, view three images side by side: the capture (the node's `image` in `build/map.json`), `a11y/runs/<id>/shots/<state>.recipe.png` (light) and `<state>.recipe.dark.png`. The same screen with drifted dates, amounts or chart data: accept. A different screen, row or dialog, or a missing layer: reject. Record the decision in the entry in `a11y/recipes/<area>.json`, while no draft or recipe check is running on that area:

```json
"status": "accepted",
"review": {"reason": "below-threshold", "by": "conductor", "decision": "accept", "date": "2026-10-02", "note": "same screen; dates and amounts drifted"}
```

A rejected recipe gets `"status": "rejected"` and `"decision": "reject"`, and goes back to a recorder.

### First run: characterize, then the gate

On the first run, before bulk recording, work in this run and publish nothing from it:

1. **Isolation.** If `policy.isolation.status` is `confirmed`, skip this step (confirmed on 2026-10-02: submissions stay in the session). Otherwise submit one policy-allowed form in one browser context and look for its result in a fresh context. If it shows up: stop condition 2. If not, record `{"status": "confirmed", "date": "<today>", "note": "<what you submitted, and where>"}` in `policy.isolation`.
2. **Drafts.** Draft the roots and everything draftable, and review.
3. **Timing.** Sweep this run (stage 3). It measures what has recipes at full concurrency. Seconds per state = sweep wall time ÷ states swept, from `run.json`'s `stages.sweep`.
4. **Pilot.** Run one recorder batch: at most 15 non-mutating states from one area's agent queue. Note its token use from the subagent's report.
5. **Counts.** Run `python3 tools/a11y/audit.py records prepare --run <id>` and `python3 tools/a11y/audit.py group --run <id>` on this run. Count the pilot area's findings and distinct records, and estimate the snapshot and export sizes from them.
6. **Gate.** Show Kaelig, in one message:
   - the policy: denylist names and notes, fixture values, mutating verbs, isolation status, root exclusions;
   - the recording estimate: agent-queue size, recorder batches of 10 to 15 states, waves of 8, projected tokens (pilot tokens per state × queue size);
   - the projected sweep time (seconds per state × 915);
   - findings and records in the pilot area, scaled to all areas, so verifier batches (findings ÷ 10) and judge batches (records ÷ 150);
   - the snapshot and export size estimate.

   Wait for go.
7. After go, record (below), then plan a fresh run for the audit. The characterization run is never published.

### Recorders

Build recorder batches from the agent queue: 10 to 15 states from one subtree, parents before children. Write each to `a11y/runs/<id>/batches/record-NNN.json` in the format `references/recorder.md` describes: `area`, `kind`, `parent`, `labels` and `mutates` from `states.json`; `title`, `note` and `image` from the node in `build/map.json`; `draft` from the state's last line in `draft.jsonl`; and `reach`, the workspace, data, map, base URL and run every command takes. Run up to 8 recorders at once, never two on one subtree. Spawn each with:

> Read `.agents/skills/a11y-audit/references/recorder.md` and follow it. Batch: `a11y/runs/<id>/batches/record-NNN.json`. agent-browser session: `record-NNN`.

Each recorder writes `batches/record-NNN.out.jsonl`, one line per state, with `result` set to `accepted`, `review`, `failed` or `policy`.

- `policy` lines are stop condition 4: collect them and report the state ids with the action each needs.
- Retry `failed` states once, in a later batch, with the recorder's `detail`. Still failing, they stay `no-recipe` and show as unreachable in the report.
- Review every `review` recipe (above).
- Then re-draft with the `--ready` passes (above). The wave's accepted recipes make their children draftable at no model cost, so the next wave holds only what the drafter could not resolve.

Before the sweep, `recipe queue` holds only states you know will stay unreachable.

## 3. Sweep

Start it as a background job:

```sh
python3 tools/a11y/audit.py sweep --run <id>
```

Eight workers. A state starts once its parent's raw file exists. Each state ends audited, or unreachable with a reason (`step-failed`, `fingerprint-mismatch`, `blocked-by-parent`, `no-recipe`, `no-path`, `tool-error`). Follow progress with `audit.py status`: `stages.sweep` counts audited, unreachable, stale and pending states.

When it exits:

- **Breaker.** If `run.json` shows `stages.sweep.interrupted: "environment"`, the circuit breaker tripped on 403, 429, a challenge page or failing roots. Wait 10 minutes (a background `sleep 600`), run preflight, and run the same sweep command once more. A second trip is stop condition 3. Recipe drift never trips the breaker: broken recipes are written as unreachable and refreshed on the next run.
- **Coverage.** `status` must show `stages.sweep.outcomes.coverage: "ok"`: every state audited or unreachable, none twice, none missing. `outcomes.json` exists only when that holds.
- **Build hash.** Run preflight again and compare its hash with the one from before the sweep. demo.mercury.com deploys often (three build hashes on 2026-10-02). List the hashes the raw files carry: `grep -h '"build"' a11y/runs/<id>/raw/*.json | sort | uniq -c`. The report names every hash; mixed hashes do not block publishing.

Resume: run the same command. Only states without a valid raw file run. To re-sweep a state after a recipe fix, delete its raw file and its descendants' `blocked-by-parent` files first. If the run was already grouped, run `audit.py group` again: findings.json records the raw files it was grouped from, and the later stages refuse it once one changes.

### Determinism pair

On the first run, and whenever the harness changed since the last pair, sweep a second run on the same build hash and compare:

1. Plan a second run (wait a minute for a new run id) and sweep it with its own `--run`.
2. Check that every raw file in both runs carries one and the same build hash. If the demo deployed in between, sweep the pair again.
3. Compare. Raw files hold no run id and no timestamps, so equal means equal bytes:

   ```sh
   diff -rq a11y/runs/<A>/raw a11y/runs/<B>/raw
   diff a11y/runs/<A>/raw/<state>.json a11y/runs/<B>/raw/<state>.json
   ```

4. A difference from timing (a late spinner, a list still loading, an animation) is a harness wait. Fix it in its module with a test. The harness hash changes, so both runs re-sweep every state. Then compare a full pair again.
5. A pair that still differs after you fixed the waits is stop condition 5: report the states and fields.

The second run of the pair is never published.

## 4. Lighthouse calibration

`audit.py status` shows `stages.calibration.due`. It is true on the first run and whenever axe-core or Chromium changed since the last calibration. Lighthouse runs the same axe engine, so a difference points at the harness, not at Mercury. When due, use Chrome DevTools MCP:

1. Pick three roots with no setup hop, and three overlays (modal, drawer, menu or popover) one click from a root. All six must be audited in this run.
2. For each one, open a fresh page (`new_page` with an `isolatedContext`), `resize_page` to 1440×900, and `emulate` with `colorScheme: "light"`. For an overlay, `take_snapshot`, `click` the control its recipe hop names, and wait for the layer to open.
3. Run `lighthouse_audit` with `mode: "snapshot"`, `device: "desktop"` and `outputDirPath: "a11y/runs/<id>/shots/lighthouse"`. From the report, list the accessibility audits that failed (score 0) whose id is an axe rule. Read Chrome's version from `environment.hostUserAgent` and Lighthouse's axe version from `environment.credits["axe-core"]`.
4. Write the comparison to `a11y/runs/<id>/batches/lighthouse-input.json`:

   ```json
   {"chrome": "151.0.7700.0", "lighthouseAxe": "4.11.0", "states": [{"state": "cards.index", "kind": "root", "lighthouseFailed": ["color-contrast", "heading-order"]}], "explanations": {"heading-order": "rule-selection"}}
   ```

   You may claim two explanations: `rule-selection` (Lighthouse runs a rule the sweep's WCAG tags leave out, or the reverse) and `version` (a different axe or Chrome). `scope`, a parent-page failure Lighthouse reports on an overlay, is computed for you.
5. Record it:

   ```sh
   python3 tools/a11y/audit.py calibration add --file a11y/runs/<id>/batches/lighthouse-input.json --run <id>
   ```

   Exit 1 means an unexplained difference: a harness bug. Fix it with a test before grouping. The harness hash changes, so re-sweep and calibrate again.

Resume: `calibration add` rewrites `lighthouse.json`. Rerun it with the same file.

## 5. Records and judging

```sh
python3 tools/a11y/audit.py records prepare --run <id>
```

It deduplicates the announced-as records into `records.jsonl` and writes judge batches, `batches/records-NNN.json`, about 150 records each. A record a published finding came from skips the judge.

Run up to 8 record judges at once, one batch each:

> Read `.agents/skills/a11y-audit/references/record-judge.md` and follow it. Batch: `a11y/runs/<id>/batches/records-NNN.json`. agent-browser session: `records-NNN`.

As each one returns:

```sh
python3 tools/a11y/audit.py judgment add --file a11y/runs/<id>/batches/records-NNN.out.jsonl --run <id>
```

Exit 1 lists the rejected lines with reasons. Send them back to the judge for a fix file. A judge that died gets an empty out file (`: > a11y/runs/<id>/batches/records-NNN.out.jsonl`), and its records go into the next prepare.

Resume: run `records prepare` again until `status` shows `stages.records.awaiting: 0`. It batches only records nobody judged and no batch holds.

## 6. Group

```sh
python3 tools/a11y/audit.py group --run <id>
```

It writes `findings.json`: one finding per rule per shared element, one per hand-found issue, and attachment proposals. It prints node and group counts per rule.

- A flag ("tags and attributes agree N% of the time", under 90%) may mean one key covers different elements. Open two of its occurrences. If they are different components, fix `signature.py` with a test and group again.
- `group` fails when an `a11y` issue in `data/` is missing from `tools/a11y/hand-found.json`. Add it with its family and criteria.
- Group again whenever the judgments change, and after any sweep of this run, including `sweep --state` or a resume after deleting a raw file. It rewrites the whole file, and the same inputs give the same bytes. `verify prepare`, `diff` and `publish` refuse a findings.json grouped before the latest sweep ("rerun `audit.py group`"), and a run swept with another harness than the current one ("rerun `audit.py sweep`, then `audit.py group`").

## 7. Verification

### Canary first

Waves wait until this run's canary passes: `python3 tools/a11y/audit.py verify status` shows `holdWaves: false`.

1. Run `python3 tools/a11y/audit.py canary prepare --run <id>`. It prints the canary's own workspace, its batch, the fixture server command and the score command.
2. Start the printed server command in the background: `node tools/a11y/fixtures/server.mjs --port <port> canary`.
3. Spawn one verifier on the canary batch, with the same message as a wave (below). The canary workspace has a root recipe for every canary page, so `reach` and `probe` work there.
4. Run the printed `audit.py canary score` command. It writes `canary.json` into this run. Stop the server.
5. A wrong verdict fails the canary. Read each wrong item's `expected` and `got`, fix `references/verifier.md`, bump its version id (`verifier@3` becomes `verifier@4`), and run the canary again. Never change the canary to fit the prompt.

### Waves

1. Prepare batches:

   ```sh
   python3 tools/a11y/audit.py verify prepare --run <id>
   ```

   It writes `batches/verify-NNN.json`, up to 10 findings that share an area and a primary state, for every finding with no verdict that no batch holds.
2. Run up to 8 verifiers at once, one batch each:

   > Read `.agents/skills/a11y-audit/references/verifier.md` and follow it. Batch: `a11y/runs/<id>/batches/verify-NNN.json`. agent-browser session: `verify-NNN`. Demo build hash: `<hash from preflight>`.

3. As each one returns, add its file with both primitives. Each skips the other's lines:

   ```sh
   python3 tools/a11y/audit.py verdict add --file a11y/runs/<id>/batches/verify-NNN.out.jsonl --run <id>
   python3 tools/a11y/audit.py decision add --file a11y/runs/<id>/batches/verify-NNN.out.jsonl --run <id>
   ```

   A verifier that died gets an empty out file, so the attempt counts.
4. Go back to 1 until `verify status` shows `findings.pending: 0`. A finding whose batch came back twice without a valid verdict gets an `unverified` line from `verify prepare`. A new unverified finding is not published. A published one stays in the map as not retested (R26).

Resume: `verify status`. Add every batch it lists in `batches.returnedNotAdded` with `verdict add` and `decision add`. Rerunning an add is safe: lines already added count as `duplicates`. Then run `verify prepare`, which refuses while any returned batch is not added. Batches still out (`inFlight`) are not batched again: give them to a verifier, or write their empty out file.

### Second opinions

`verify status` lists `secondOpinion`: every verdict that flips the previous published status on the same evidence hash, and every drop of a finding whose default severity is high. For each one, write `batches/second-NNN.json` in the verify batch format (`batch`, `run`, `area`, `primaries`, `reach`, `findings`), holding the finding's entry copied from its verify batch with `proposals` and `attachments` emptied. Spawn a fresh verifier on it with the wave message. It must not see the first verdict. It writes `"second": true` lines. Add them with `verdict add`.

When the two verdicts disagree, you decide. Compare both lines, their probes and their screenshots. Then write your own line in the same format, with `"model": "<your model>"`, `"prompt": "conductor"`, `"batch": "second-NNN"` and `"second": true`, to `batches/second-NNN.conductor.jsonl`, and add it with `verdict add`. The last line for a key is its verdict.

### Attachment decisions

A proposal says a scanner finding may be the same failure as a hand-found issue (AE5). Verifiers decide each proposal in the batch that holds its scanner finding: `attach` or `reject`. They may `release` an attachment the registry holds when the two no longer describe one failure. `audit.py status` lists `proposals.pending`. Decide every leftover yourself, with lines in the verifier's decision format and `"prompt": "conductor"`, through `decision add`. Publish refuses while a proposal is pending. The last decision line for a proposal is the one publish applies.

## 8. Diff

```sh
python3 tools/a11y/audit.py diff --run <id>
```

It writes `diff.json`: each finding's status against the published snapshot (`new`, `persisting`, `re-rated`, `not-retested`, `fixed`, `withdrawn`, `reappeared`) and each hand-found issue's (`first-audited`, `confirmed`, `re-rated`, `not-reproduced`, `not-retested`). On the first run every scanner finding is new. Check every `fixed`: it needs its family completed on every prior screen. Many `not-retested` usually means many unreachable states: read their root causes.

## 9. Publish

```sh
python3 tools/a11y/audit.py publish --run <id>
```

It checks the gates and writes `a11y/published/` (issues, report, registry, meta) through a three-step swap. The gates: the run swept with the current harness (else "rerun `audit.py sweep`, then `audit.py group`"), findings.json grouped from that sweep (else "rerun `audit.py group`"), coverage, a verdict or carry-forward for every finding, every proposal decided, the canary passed, the diff's baseline equal to the current snapshot's run, the export's rules on every generated issue, unique ids, and every report link resolving. A refusal names the gate and leaves the previous snapshot in place: fix the cause and rerun. "Already published" means this run is in.

Resume: if `status` shows "publish incomplete, rerun publish", rerun it. Publish rebuilds everything from the run's ledger.

## 10. Build

```sh
python3 build.py
```

It merges the snapshot into the map, writes the ticket export, and renders the report to `dist/a11y/index.html` (`python3 tools/a11y/audit.py report` renders the report alone). Check: no new `WARN` lines; the report opens; a report row's `?issue=` link opens the map with that issue's screens highlighted.

## 11. Summary and deploy command

Write Kaelig a summary:

- coverage: audited and unreachable states out of the planned states, `no-path` states, unreachable states grouped by root cause, every demo build hash the run saw;
- changes: counts per diff status, and each hand-found issue's status with its reason;
- published findings by severity, and the top of the priority list;
- verification: the canary result, prompt versions, second opinions and your decisions, unverified findings, the advisory record count;
- the Lighthouse calibration result, or the earlier one the snapshot carries;
- the determinism pair: identical, or what differed and what you fixed;
- states that need an action outside the policy;
- the seven acceptance cases below: where the demo had a case, and where it had none.
  - AE1, one component on many screens: a failure shared by many states (muted table text failing 1.4.3, say) publishes as one issue whose "Show all N screens" highlights every one.
  - AE2, broken recipe: a drawer whose recipe no longer reaches it appears in the report's unreachable list by state id, and the rest of the run completes.
  - AE3, verifier downgrade: an axe violation the live demo shows to be minor (an icon button with a visible text label inside the same control) publishes as low, with the verifier's reason.
  - AE4, drawer focus: a drawer that opens with Enter but leaves focus on the page raises a 2.4.3 finding with the focus trace; it is high if keyboard users then cannot reach its controls.
  - AE5, matching a hand-found issue: a scanner finding that is the same failure as a hand-found issue attaches to it instead of creating a second one.
  - AE6, rerun after a fix: a scanner finding published in the previous run that no longer reproduces is listed as fixed and leaves the map.
  - AE7, verifier raises severity: a hand-found issue rated medium that keyboard users cannot get past at all publishes as high, with that reason.

End with the deploy command. Do not run it. It runs only when Kaelig says go:

```sh
MAP_BASE_URL=https://mercury-admin-map.netlify.app python3 build.py
netlify deploy --prod --dir dist
```
