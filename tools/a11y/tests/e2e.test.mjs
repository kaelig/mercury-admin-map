// U11: the fixture end to end (plan "Verification Contract": Fixture end to end). Drives
// `python3 tools/a11y/audit.py` through every stage the a11y-audit skill runs, on the fixture app
// (tools/a11y/fixtures/app, its recipes and captures) in temp workspaces:
//
//   A, run R1: preflight to report, uninterrupted.
//   B, run R1: the same in a second workspace, with the sweep stopped by SIGTERM after a few raw
//      files and rerun. It must end in the snapshot A published, byte for byte.
//   A, run R2: against R1's snapshot. Every finding is persisting, and issues.json changes only in
//      the fields that name the run and its status.
//
// Scripted lines stand in for the agents (record judges, the canary verifier, verifiers). They pass
// the real validators and carry the prompt version ids from the skill's references/. The fixture area
// data carries three hand-found issues, under ids tools/a11y/hand-found.json already lists, because
// group takes their family and criteria from that file: an AE7-shaped trap (clickable list items
// keyboard users cannot reach, rated medium: re-rated high), one a scanner finding attaches to (AE5),
// and one fixed since capture (dropped).
//
// Run ids are in the year 2000 so nothing this test could write is mistaken for a real run.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative } from 'node:path';
import { ROOT, dump, readJson, readJsonl, writeJson } from '../lib/canon.mjs';
import { startServer } from '../fixtures/server.mjs';

const A11Y = join(ROOT, 'tools', 'a11y');
const AUDIT = join(A11Y, 'audit.py');
const COMMITTED = join(ROOT, 'a11y');
const R1 = '2000-01-01-0101';
const R2 = '2000-01-01-0202';
const TODAY = new Date().toLocaleDateString('en-CA');
const promptId = (name) => readFileSync(join(ROOT, '.agents', 'skills', 'a11y-audit', 'references', `${name}.md`), 'utf8').split('\n')[0].trim();
const JUDGE = promptId('record-judge');
const VERIFIER = promptId('verifier');
const MODEL = 'e2e-script';

// What the fixture app is built to break (fixtures/app/app.js): its two modals and its drawer never
// move focus in or give it back, the modals never keep it in; its menus never move focus to their
// first item; the Send money item that opens the send modal is a list item Tab never reaches; the
// balance chart's aria-label sits on a div with no role. [rule, primary state].
const SEEDED = [
  ['aria-prohibited-attr', 'home.index'],
  ['dialog-focus-escaped', 'cards.edit-limit-modal'],
  ['dialog-focus-escaped', 'cards.freeze-modal'],
  ['dialog-focus-not-moved', 'cards.edit-limit-modal'],
  ['dialog-focus-not-moved', 'cards.freeze-modal'],
  ['dialog-focus-not-moved', 'transactions.drawer'],
  ['dialog-focus-not-returned', 'cards.edit-limit-modal'],
  ['dialog-focus-not-returned', 'cards.freeze-modal'],
  ['dialog-focus-not-returned', 'transactions.drawer'],
  ['dialog-trigger-not-focusable', 'home.send-modal'],
  ['menu-focus-not-moved', 'home.move-money-menu'],
  ['menu-focus-not-moved', 'transactions.filters-menu'],
];
const ROOTS = ['cards.index', 'home.index', 'reports.index', 'transactions.index'];
const NO_RECIPE = 'home.chart-tooltip';   // fixtures/app/recipes has no recipe for it

const AE7 = 'accounts.i-tabs-not-tabs';
const ATTACH = 'home.i-chips-as-menuitems';
const FIXED = 'cards.i-modal-heading-semantics';
const HAND_FOUND = {
  home: [
    { id: AE7, category: 'a11y', severity: 'medium', nodeIds: ['home.move-money-menu'],
      title: 'Move money menu items are clickable list items',
      detail: 'Send money, Transfer and Deposit check are list items with click handlers and tabindex="-1", so Tab never reaches them.',
      evidence: 'Dashboard: 3 <li role="menuitem" tabindex="-1"> items with onclick in the Move money menu; none takes focus.' },
    { id: ATTACH, category: 'a11y', severity: 'medium', nodeIds: ['home.index'],
      title: 'The balance chart is labelled on a div with no role',
      detail: 'The chart div carries aria-label="Balance over 7 days" but no role, so the label is not announced.',
      evidence: 'Dashboard: <div class="chart" aria-label="Balance over 7 days"> with no role.' },
  ],
  cards: [
    { id: FIXED, category: 'a11y', severity: 'medium', nodeIds: ['cards.edit-limit-modal'],
      title: 'The Edit card limits title is not a heading',
      detail: 'The Edit card limits dialog title was a span with no heading role when the map was captured.',
      evidence: 'Edit card limits: the title is a <span>, not an h2.' },
  ],
};
const AE7_REASON = 'Tab never reaches Send money, so keyboard users cannot open the Send money dialog at all.';

let server;
let origin;
let tmp;
let policy;
let map;
let committedBefore;
const W = {};            // workspace name -> {name, root, ws, data, map}
const S1 = {};           // the four published files A wrote for R1, as text
const groups = new Set();

before(async () => {
  committedBefore = treeHashes(COMMITTED);
  tmp = mkdtempSync(join(tmpdir(), 'a11y-e2e-'));
  // preflight asks the base URL for /dashboard; the fixture app lives under /app/, so a one-line page
  // answers there.
  mkdirSync(join(tmp, 'dashboard'));
  writeFileSync(join(tmp, 'dashboard', 'index.html'), '<!doctype html><title>Dashboard</title>\n');
  server = await startServer({ dirs: ['app', join(tmp, 'dashboard')] });
  origin = server.origin;
  map = server.mergedMap();
  // The committed policy, read once: another process may edit a11y/policy.json while this runs.
  policy = readJson(join(COMMITTED, 'policy.json'));
});

after(async () => {
  for (const pgid of groups) {
    try { process.kill(-pgid, 'SIGKILL'); } catch { /* gone */ }
  }
  await server?.close();
  if (tmp) rmSync(tmp, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// Workspaces and stages
// ---------------------------------------------------------------------------

function workspace(name) {
  const root = join(tmp, name);
  const w = { name, root, ws: join(root, 'ws'), data: join(root, 'data'), map: join(root, 'map.json') };
  mkdirSync(join(w.ws, 'recipes'), { recursive: true });
  mkdirSync(w.data, { recursive: true });
  for (const f of readdirSync(join(A11Y, 'fixtures', 'app', 'recipes'))) {
    writeFileSync(join(w.ws, 'recipes', f), readFileSync(join(A11Y, 'fixtures', 'app', 'recipes', f)));
  }
  writeJson(join(w.ws, 'policy.json'), policy);
  writeJson(w.map, map);
  // Area data from the merged fixture map, so publish knows every screen, plus the hand-found issues.
  for (const area of map.areas) {
    writeJson(join(w.data, `${area.area}.json`), { ...area, issues: [...(area.issues || []), ...(HAND_FOUND[area.area] || [])] });
  }
  W[name] = w;
  return w;
}

const runDir = (w, run) => join(w.ws, 'runs', run);
const rawDir = (w, run) => join(runDir(w, run), 'raw');
const rawNames = (w, run) => (existsSync(rawDir(w, run)) ? readdirSync(rawDir(w, run)).filter((f) => f.endsWith('.json')).sort() : []);
const rawBytes = (w, run) => Object.fromEntries(rawNames(w, run).map((f) => [f, readFileSync(join(rawDir(w, run), f), 'utf8')]));
const pubText = (w) => Object.fromEntries(['issues.json', 'report.json', 'registry.json', 'meta.json']
  .map((f) => [f, readFileSync(join(w.ws, 'published', f), 'utf8')]));
const pub = (w, f) => readJson(join(w.ws, 'published', f));

function lastJson(text) {
  const t = text.trim();
  try {
    return JSON.parse(t);
  } catch {
    try { return JSON.parse(t.split('\n').pop()); } catch { return null; }
  }
}

// `python3 audit.py <args> <common options>`, never blocking this process, which serves the fixture.
// Each stage runs in a process group of its own: audit.py does not pass a SIGTERM on to the Node stage
// it spawns, so stopping a sweep means signalling the group, as a terminal's Ctrl-C does.
// killWhen: polled every 20 ms; once true, the group gets SIGTERM.
function audit(w, run, args, { killWhen = null } = {}) {
  return new Promise((resolve) => {
    const argv = [AUDIT, ...args, '--workspace', w.ws, '--data', w.data, '--map', w.map, '--base-url', origin];
    if (run) argv.push('--run', run);
    const t0 = Date.now();
    const child = spawn('python3', argv, { cwd: ROOT, detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
    groups.add(child.pid);
    let stdout = '';
    let stderr = '';
    let killed = false;
    child.stdout.on('data', (d) => { stdout += d; });
    child.stderr.on('data', (d) => { stderr += d; });
    const poll = killWhen ? setInterval(() => {
      if (killed || !killWhen()) return;
      killed = true;
      process.kill(-child.pid, 'SIGTERM');
    }, 20) : null;
    // 'close' waits for every process holding the pipes, so for the Node stage too.
    child.on('close', (code, signal) => {
      clearInterval(poll);
      groups.delete(child.pid);
      resolve({ args: args.join(' '), code, signal, killed, stdout, stderr, json: lastJson(stdout), ms: Date.now() - t0 });
    });
  });
}

function ok(r) {
  assert.equal(r.code, 0, `audit.py ${r.args} exited ${r.code ?? r.signal}\n${r.stdout.slice(-3000)}\n${r.stderr.slice(-3000)}`);
  return r;
}

// ---------------------------------------------------------------------------
// The stand-in agents
// ---------------------------------------------------------------------------

// Record judge: every record is ok, except the drawer, which opens like a dialog but is announced as
// an unnamed complementary landmark (a finding), and the Help center link to another site (advisory).
function judge(record) {
  if (record.role === 'complementary') {
    return { verdict: 'flag', criterion: '4.1.2', family: 'role-state',
      reason: 'The transaction drawer opens like a dialog but is announced as an unnamed complementary landmark.' };
  }
  if (record.template === 'link "Help center"') {
    return { verdict: 'flag', criterion: null, family: null, reason: 'Opens another site without saying so; no AA criterion fails.' };
  }
  return { verdict: 'ok', criterion: null, family: null, reason: 'Role, name and states match what the control shows.' };
}

// Verifier: confirm at the default severity, except the hand-found traps and the backward jumps, which
// come from the fixed sidebar: the order still follows the page, so they are re-rated low.
function verdictFor(e) {
  if (e.key === `hand:${AE7}`) return { outcome: 'rerate', after: 'high', reason: AE7_REASON };
  if (e.key === `hand:${FIXED}`) {
    return { outcome: 'drop', reasonCode: 'fixed-since-capture', reason: 'The Edit card limits title is an h2 now.' };
  }
  if (e.rule === 'backward-jump') {
    return { outcome: 'rerate', after: 'low', reason: 'The jump goes from the fixed sidebar to the page toolbar, the order the page reads in.' };
  }
  return { outcome: 'confirm', reason: 'Reproduced on every sampled state.' };
}

// The balance chart's own result: its aria-label on a div with no role (AE5: it attaches to ATTACH).
const isChart = (f) => f.rule === 'aria-prohibited-attr' && f.basis === 'violation';

function verdictLine(e, batch, build) {
  const v = verdictFor(e);
  return {
    key: e.key, issueId: e.issueId, source: e.source, outcome: v.outcome, severityBefore: e.severityDefault,
    severityAfter: v.after || e.severityDefault, reason: v.reason, reasonCode: v.reasonCode || null,
    counterEvidence: e.source === 'hand-found' || v.outcome === 'drop' ? `shot:${e.primary}.e2e` : null,
    probes: [], states: e.states, build, model: MODEL, batch: batch.batch, prompt: VERIFIER, attempt: e.attempt,
    second: false, date: TODAY, criterionAnalysis: null,
  };
}

const writeLines = (path, lines) => writeFileSync(path, lines.map((l) => JSON.stringify(l) + '\n').join(''));
const batches = (w, run, prefix) => readdirSync(join(runDir(w, run), 'batches'))
  .filter((f) => new RegExp(`^${prefix}-\\d{3}\\.json$`).test(f)).sort().map((f) => join(runDir(w, run), 'batches', f));

// ---------------------------------------------------------------------------
// Every stage, in the skill's order
// ---------------------------------------------------------------------------

async function pipeline(w, run, { killSweep = false } = {}) {
  const out = {};
  out.preflight = ok(await audit(w, null, ['preflight'])).json;
  out.plan = ok(await audit(w, run, ['plan']));
  out.queue = ok(await audit(w, run, ['recipe', 'queue'])).json;
  out.check = ok(await audit(w, run, ['recipe', 'check', 'cards.edit-limit-modal'])).json;

  if (killSweep) {
    out.killed = await audit(w, run, ['sweep', '--workers', '8'], { killWhen: () => rawNames(w, run).length >= 3 });
    out.keptRaw = rawNames(w, run).length;
    out.outcomesAfterKill = existsSync(join(runDir(w, run), 'outcomes.json'));
  }
  out.sweep = ok(await audit(w, run, ['sweep', '--workers', '8']));
  out.status = ok(await audit(w, run, ['status'])).json;

  out.records = ok(await audit(w, run, ['records', 'prepare'])).json;
  for (const path of batches(w, run, 'records')) {
    const batch = readJson(path);
    writeLines(path.replace(/\.json$/, '.out.jsonl'), batch.records.map((r) => ({
      sig: r.sig, ...judge(r), batch: batch.batch, model: MODEL, prompt: JUDGE })));
    ok(await audit(w, run, ['judgment', 'add', '--file', path.replace(/\.json$/, '.out.jsonl')]));
  }
  out.group = ok(await audit(w, run, ['group']));

  // The canary first: its own workspace, the expected answers from fixtures/canary/expected.json.
  const canary = ok(await audit(w, run, ['canary', 'prepare', '--out', join(w.root, `canary-${run}`)])).json;
  out.canaryPrepare = canary;
  const expected = readJson(join(A11Y, 'fixtures', 'canary', 'expected.json'));
  const cbatch = readJson(canary.batch);
  writeLines(canary.batch.replace(/\.json$/, '.out.jsonl'), cbatch.findings.map((e) => {
    const [outcome, after] = expected[e.key].expected.split(':');
    return {
      key: e.key, issueId: e.issueId, source: e.source, outcome, severityBefore: e.severityDefault,
      severityAfter: after || e.severityDefault, reason: expected[e.key].why,
      reasonCode: outcome === 'drop' ? 'tool-false-positive' : null,
      counterEvidence: outcome === 'drop' || e.source === 'hand-found' ? `shot:${e.primary}.canary` : null,
      probes: [], states: e.states, build: 'unknown', model: MODEL, batch: cbatch.batch, prompt: VERIFIER, attempt: 1,
    };
  }));
  out.canary = ok(await audit(w, run, ['canary', 'score', '--file', canary.batch.replace(/\.json$/, '.out.jsonl')])).json;

  // Verification: one verdict per finding; one decision per proposal, in the batch of its scanner finding.
  out.verifyPrepare = ok(await audit(w, run, ['verify', 'prepare'])).json;
  const build = readJson(join(runDir(w, run), 'run.json')).builds.at(-1) || 'unknown';
  for (const path of batches(w, run, 'verify')) {
    const batch = readJson(path);
    const lines = batch.findings.map((e) => verdictLine(e, batch, build));
    for (const e of batch.findings.filter((x) => x.source === 'scanner')) {
      for (const p of e.proposals) {
        const same = isChart(e) && p.handFound === ATTACH;
        lines.push({ proposal: p.id, finding: p.finding, handFound: p.handFound, decision: same ? 'attach' : 'reject',
          reason: same ? 'The same element on the same screen.' : 'A different element.', model: MODEL, batch: batch.batch,
          prompt: VERIFIER, date: TODAY });
      }
    }
    const file = path.replace(/\.json$/, '.out.jsonl');
    writeLines(file, lines);
    ok(await audit(w, run, ['verdict', 'add', '--file', file]));
    ok(await audit(w, run, ['decision', 'add', '--file', file]));
  }
  out.verifyAgain = ok(await audit(w, run, ['verify', 'prepare'])).json;
  out.verifyStatus = ok(await audit(w, run, ['verify', 'status'])).json;

  out.diff = ok(await audit(w, run, ['diff'])).json;
  out.publish = ok(await audit(w, run, ['publish'])).json;
  out.statusAfter = ok(await audit(w, run, ['status'])).json;
  out.report = ok(await audit(w, run, ['report', '--dist', join(w.root, `dist-${run}`)])).json;
  return out;
}

const findingsOf = (w, run) => readJson(join(runDir(w, run), 'findings.json'));

// ---------------------------------------------------------------------------
// A, run R1
// ---------------------------------------------------------------------------

let A1;

test('run R1 goes through every stage, from preflight to the report', { timeout: 600000 }, async (t) => {
  const w = workspace('A');
  const t0 = Date.now();
  A1 = await pipeline(w, R1);
  t.diagnostic(`run R1, every stage: ${Date.now() - t0} ms (sweep ${A1.sweep.ms} ms)`);
  Object.assign(S1, pubText(w));

  // preflight checks the merged map, the two pinned packages and /dashboard on the base URL.
  assert.equal(A1.preflight.ok, true, JSON.stringify(A1.preflight));
  assert.equal(A1.preflight.baseUrl, origin);

  const states = readJson(join(runDir(w, R1), 'states.json'));
  const nodes = map.areas.flatMap((a) => a.nodes.map((n) => n.id)).sort();
  assert.deepEqual(Object.keys(states.states).sort(), nodes);
  assert.deepEqual([...states.roots].sort(), ROOTS, 'one root per page URL; books.* never roots');
  assert.equal(states.states['cards.edit-limit-modal'].parent, 'cards.index');

  assert.deepEqual(A1.queue.queue.map((q) => [q.state, q.reason]), [[NO_RECIPE, 'no-recipe']]);
  assert.equal(A1.check.checked[0].status, 'accepted');

  // The sweep: every state audited but the one without a recipe.
  assert.deepEqual({ ...A1.sweep.json, ms: 0 }, { run: R1, audited: nodes.length - 1, unreachable: 1, pending: 0, interrupted: null, ms: 0 });
  assert.deepEqual(A1.status.stages.sweep.outcomes, { written: true, coverage: 'ok' });

  // Records: the two scripted flags, one judgment per distinct record.
  const judged = readJsonl(join(runDir(w, R1), 'judgments.jsonl'));
  assert.equal(judged.length, A1.records.records);
  assert.equal(judged.filter((j) => j.verdict === 'flag').length, 2);

  // The canary's own workspace sits inside this one, so removing the workspace removes it.
  assert.ok(A1.canaryPrepare.root.startsWith(w.root), A1.canaryPrepare.root);
  assert.deepEqual({ pass: A1.canary.pass, correct: A1.canary.correct, expected: A1.canary.expected }, { pass: true, correct: 6, expected: 6 });

  // Verification finished in one wave: nothing left to batch, nothing pending.
  assert.equal(A1.verifyPrepare.holdWaves, false);
  assert.deepEqual(A1.verifyAgain.batches, []);
  const vs = A1.verifyStatus;
  assert.equal(vs.findings.pending, 0);
  assert.equal(vs.findings.verdicted, vs.findings.total);
  assert.deepEqual(vs.proposals.pending, []);
  assert.equal(vs.canary.status, 'passed');
  assert.deepEqual(vs.secondOpinion, []);

  assert.equal(A1.diff.baseline, 'none');
  assert.equal(A1.publish.state, 'published');
  assert.equal(A1.statusAfter.publish.state, 'already published');
  assert.ok(existsSync(A1.report.report), 'the report is rendered');
});

test('R1 findings: the fixture\'s seeded failures, the scripted flag and the hand-found issues', () => {
  const doc = findingsOf(W.A, R1);
  const scanner = doc.findings.filter((f) => f.source === 'scanner');
  // Violations and failures are keyed per element; axe's incomplete results are grouped product-wide
  // and are not pinned here.
  const measured = scanner.filter((f) => ['violation', 'failure'].includes(f.basis));
  assert.deepEqual(measured.map((f) => [f.rule, f.primary]).sort(), SEEDED);
  // The only candidates: Tab from the fixed sidebar up to a toolbar.
  assert.deepEqual([...new Set(scanner.filter((f) => f.basis === 'candidate' && f.rule !== 'announced-as').map((f) => f.rule))], ['backward-jump']);
  const announced = scanner.filter((f) => f.rule === 'announced-as');
  assert.deepEqual(announced.map((f) => [f.family, f.states]), [['role-state', ['transactions.drawer']]]);
  assert.deepEqual(doc.advisory.map((a) => a.template), ['link "Help center"']);

  const hand = doc.findings.filter((f) => f.source === 'hand-found');
  assert.deepEqual(hand.map((f) => f.key).sort(), [AE7, FIXED, ATTACH].map((id) => `hand:${id}`).sort());
  // AE5: the chart's role-state finding covers the hand-found issue's only screen, in its family.
  const chart = scanner.find(isChart);
  assert.ok(doc.proposals.some((p) => p.finding === chart.key && p.handFound === ATTACH), JSON.stringify(doc.proposals));
});

test('R1 snapshot: issues, overlays, registry, report and meta follow from the findings and the verdicts', () => {
  const doc = findingsOf(W.A, R1);
  const scanner = doc.findings.filter((f) => f.source === 'scanner');
  const chart = scanner.find(isChart);
  const issues = pub(W.A, 'issues.json');
  const registry = pub(W.A, 'registry.json');
  const report = pub(W.A, 'report.json');
  const meta = pub(W.A, 'meta.json');

  // One generated issue per verified scanner finding, except the one attached to a hand-found issue.
  const byKey = Object.fromEntries(Object.entries(registry.keys).map(([k, id]) => [id, k]));
  const generated = issues.generated;
  assert.equal(generated.length, scanner.length - 1);
  assert.deepEqual(generated.map((i) => byKey[i.id]).sort(), scanner.filter((f) => f !== chart).map((f) => f.key).sort());
  assert.deepEqual(Object.values(registry.keys).sort(), generated.map((i) => i.id).sort());
  assert.deepEqual(registry.attachments, [{ finding: chart.key, handFound: ATTACH, run: R1 }]);
  assert.deepEqual(registry.tombstones, {});
  for (const i of generated) {
    const f = scanner.find((x) => x.key === byKey[i.id]);
    assert.match(i.id, new RegExp(`^${f.primary.split('.')[0]}\\.i-a11y-[a-z0-9.-]+$`));
    assert.equal(i.area, f.primary.split('.')[0]);
    assert.equal(i.category, 'a11y');
    assert.equal(i.nodeIds[0], f.primary);
    assert.deepEqual([...i.nodeIds].sort(), f.states);
    assert.deepEqual(i.wcag, f.criteria);
    assert.equal(i.severity, f.rule === 'backward-jump' ? 'low' : f.severityDefault, i.id);
    assert.deepEqual([i.audit.run, i.audit.status, i.audit.verdict], [R1, 'new', f.rule === 'backward-jump' ? 'rerate' : 'confirm']);
    assert.ok(i.evidence.length <= 300 && !i.evidence.includes('\n'), i.id);
  }

  // Overlays: every hand-found issue. AE7 is raised to high with the verifier's reason; the fixed one is
  // left as it was; the attached one keeps its severity.
  assert.deepEqual(Object.keys(issues.overlays).sort(), [AE7, ATTACH, FIXED].sort());
  const ae7 = issues.overlays[AE7];
  assert.deepEqual([ae7.severity, ae7.evidenceAppend, ae7.history],
    ['high', `Verifier (${TODAY}): ${AE7_REASON}`, `rated high on ${TODAY}, previously medium`]);
  assert.deepEqual([issues.overlays[ATTACH].severity, issues.overlays[ATTACH].audit.status], [null, 'first-audited']);
  assert.deepEqual([issues.overlays[FIXED].severity, issues.overlays[FIXED].evidenceAppend, issues.overlays[FIXED].audit.verdict],
    [null, null, 'drop']);

  // report.json
  assert.deepEqual([report.run, report.baseline, report.standard], [R1, 'none', 'WCAG 2.2 AA']);
  const nodes = map.areas.flatMap((a) => a.nodes).length;
  assert.deepEqual([report.coverage.states, report.coverage.audited, report.coverage.unreachable], [nodes, nodes - 1, 1]);
  assert.deepEqual(report.unreachable.map((u) => [u.rootCause, u.reason, u.firstUnreachable]), [[NO_RECIPE, 'no-recipe', R1]]);
  assert.equal(report.changes.findings.new, generated.length);
  assert.deepEqual(report.changes.handFound, { 'first-audited': 1, confirmed: 0, 're-rated': 1, 'not-reproduced': 1, 'not-retested': 0 });
  const rows = report.findings;
  assert.deepEqual(rows.map((r) => r.id).sort(), [...generated.map((i) => i.id), AE7, ATTACH].sort());
  for (const sev of ['high', 'medium', 'low']) assert.equal(report.counts[sev], rows.filter((r) => r.severity === sev).length, sev);
  assert.equal(rows.find((r) => r.id === AE7).severity, 'high');
  assert.deepEqual([...report.priority].sort(), rows.map((r) => r.id).sort());
  assert.equal(rows.find((r) => r.id === report.priority[0]).severity, 'high');
  const hand = Object.fromEntries(report.handFound.map((h) => [h.id, h]));
  assert.deepEqual([hand[AE7].status, hand[ATTACH].status, hand[FIXED].status, hand[FIXED].reason],
    ['re-rated', 'first-audited', 'not-reproduced', 'fixed since capture']);
  assert.deepEqual(hand[ATTACH].attached.map((a) => a.key), [chart.key]);
  assert.deepEqual(report.advisory.map((a) => a.template), ['link "Help center"']);
  assert.deepEqual([report.notRetested, report.withdrawn, report.fixed], [[], [], []]);

  assert.deepEqual([meta.run, meta.coverage], [R1, { states: nodes, audited: nodes - 1, unreachable: 1 }]);

  // The rendered report links every row to the map.
  const html = readFileSync(A1.report.report, 'utf8');
  for (const id of report.priority) assert.ok(html.includes(`../index.html?issue=${encodeURIComponent(id)}`), id);
});

// ---------------------------------------------------------------------------
// B, run R1: the sweep stopped mid-run
// ---------------------------------------------------------------------------

test('a sweep stopped by SIGTERM and rerun ends in the same snapshot, byte for byte', { timeout: 600000 }, async (t) => {
  assert.ok(S1['issues.json'], 'needs run R1 in workspace A');
  const w = workspace('B');
  const B1 = await pipeline(w, R1, { killSweep: true });
  t.diagnostic(`stopped after ${B1.keptRaw} raw files; the rerun swept ${B1.sweep.stderr.split('\n').filter((l) => /^sweep: \[\d+\/\d+\]/.test(l)).length}`);
  assert.equal(B1.killed.killed, true);
  assert.match(B1.killed.stderr, /SIGTERM: stopping; rerun the stage to resume/);
  const total = map.areas.flatMap((a) => a.nodes).length;
  assert.ok(B1.keptRaw >= 3 && B1.keptRaw < total, `${B1.keptRaw} raw files when stopped`);
  assert.equal(B1.outcomesAfterKill, false, 'no outcomes.json for a stopped sweep');
  assert.deepEqual(rawBytes(w, R1), rawBytes(W.A, R1));
  assert.deepEqual(pubText(w), S1);
});

// ---------------------------------------------------------------------------
// A, run R2: against R1's snapshot
// ---------------------------------------------------------------------------

let A2;

test('run R2 against R1\'s snapshot: every finding persisting, the same issues', { timeout: 600000 }, async (t) => {
  assert.ok(S1['issues.json'], 'needs run R1 in workspace A');
  const w = W.A;
  const t0 = Date.now();
  A2 = await pipeline(w, R2);
  t.diagnostic(`run R2, every stage: ${Date.now() - t0} ms`);

  assert.deepEqual(rawBytes(w, R2), rawBytes(w, R1), 'two sweeps of the same fixture give the same raw files');
  const diff = readJson(join(runDir(w, R2), 'diff.json'));
  assert.equal(diff.baseline, R1);
  const scanner = findingsOf(w, R2).findings.filter((f) => f.source === 'scanner');
  assert.deepEqual(Object.keys(diff.findings).sort(), scanner.map((f) => f.key).sort());
  for (const [key, e] of Object.entries(diff.findings)) assert.equal(e.status, 'persisting', key);
  assert.deepEqual(Object.fromEntries(Object.entries(diff.handFound).map(([id, e]) => [id, e.status])),
    { [AE7]: 're-rated', [ATTACH]: 'confirmed', [FIXED]: 'not-reproduced' });
  // The published announced-as finding skipped the judge (KTD18).
  const carried = readJsonl(join(runDir(w, R2), 'judgments.jsonl')).filter((j) => j.prompt === 'carried');
  assert.equal(carried.length, 1);
  const chart = findingsOf(w, R2).findings.find(isChart);
  assert.ok(!findingsOf(w, R2).proposals.some((p) => p.finding === chart.key && p.handFound === ATTACH),
    'an attachment the registry holds is not proposed again');

  // issues.json: R1's bytes, with the audit blocks naming R2 and this run's statuses. The registry is
  // unchanged.
  const status = { new: 'persisting', 'first-audited': 'confirmed' };
  const r1 = JSON.parse(S1['issues.json']);
  for (const block of [...r1.generated.map((i) => i.audit), ...Object.values(r1.overlays).map((o) => o.audit)]) {
    if (!block) continue;
    block.run = R2;
    block.status = status[block.status] || block.status;
  }
  assert.equal(pubText(w)['issues.json'], dump(r1));
  assert.equal(pubText(w)['registry.json'], S1['registry.json']);
  const report = pub(w, 'report.json');
  assert.deepEqual([report.run, report.baseline, report.changes.findings.persisting], [R2, R1, r1.generated.length]);
});

test('a loader read merges the snapshot into the fixture data', async () => {
  assert.ok(A2, 'needs run R2');
  const w = W.A;
  const r = await new Promise((resolve) => {
    const code = 'import json, sys; sys.path.insert(0, "tools"); import mapdata; '
      + 'print(json.dumps(mapdata.load_areas(sys.argv[1], snapshot_dir=sys.argv[2])))';
    const child = spawn('python3', ['-c', code, w.data, join(w.ws, 'published')], { cwd: ROOT });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => { stdout += d; });
    child.stderr.on('data', (d) => { stderr += d; });
    child.on('close', (status) => resolve({ status, stdout, stderr }));
  });
  assert.equal(r.status, 0, r.stderr);
  assert.equal(r.stderr, '', 'no WARN lines: every screen is known');
  const areas = Object.fromEntries(JSON.parse(r.stdout).map((a) => [a.area, a]));
  const issues = pub(w, 'issues.json');
  const merged = Object.fromEntries(Object.values(areas).flatMap((a) => (a.issues || []).map((i) => [i.id, { ...i, area: a.area }])));
  for (const g of issues.generated) {
    assert.ok(merged[g.id], g.id);
    assert.deepEqual([merged[g.id].area, merged[g.id].severity, merged[g.id].nodeIds], [g.area, g.severity, g.nodeIds]);
  }
  const raw = Object.fromEntries(Object.values(HAND_FOUND).flat().map((i) => [i.id, i]));
  assert.equal(merged[AE7].severity, 'high');
  assert.ok(merged[AE7].evidence.startsWith(raw[AE7].evidence), 'the original evidence comes first');
  assert.ok(merged[AE7].evidence.endsWith(`Verifier (${TODAY}): ${AE7_REASON}`), merged[AE7].evidence);
  for (const id of [ATTACH, FIXED]) {
    assert.deepEqual([merged[id].severity, merged[id].evidence, merged[id].nodeIds], [raw[id].severity, raw[id].evidence, raw[id].nodeIds], id);
  }
});

test('each prompt file\'s version id appears in what the stand-in agents wrote', () => {
  assert.ok(A2, 'needs run R2');
  assert.match(JUDGE, /^record-judge@\d+$/);
  assert.match(VERIFIER, /^verifier@\d+$/);
  for (const [w, run] of [[W.A, R1], [W.B, R1], [W.A, R2]]) {
    const dir = runDir(w, run);
    const judged = readJsonl(join(dir, 'judgments.jsonl')).filter((j) => j.prompt !== 'carried');
    assert.ok(judged.length > 0 && judged.every((j) => j.prompt === JUDGE), `${w.name} ${run} judgments`);
    const verdicts = readJsonl(join(dir, 'verdicts.jsonl'));
    assert.ok(verdicts.length > 0 && verdicts.every((v) => v.prompt === VERIFIER), `${w.name} ${run} verdicts`);
    assert.ok(readJsonl(join(dir, 'decisions.jsonl')).every((d) => d.prompt === VERIFIER), `${w.name} ${run} decisions`);
    assert.equal(readJson(join(dir, 'canary.json')).prompt, VERIFIER);
  }
  assert.equal(readJsonl(join(runDir(W.A, R1), 'decisions.jsonl')).length, findingsOf(W.A, R1).proposals.length);
});

// ---------------------------------------------------------------------------
// The committed a11y/ directory
// ---------------------------------------------------------------------------

function treeHashes(dir) {
  const out = {};
  const walk = (d) => {
    let names;
    try { names = readdirSync(d); } catch { return; }
    for (const n of names) {
      const p = join(d, n);
      try {
        if (statSync(p).isDirectory()) walk(p);
        else out[relative(dir, p)] = createHash('sha256').update(readFileSync(p)).digest('hex');
      } catch { /* removed while walking */ }
    }
  };
  walk(dir);
  return out;
}

// Other work may write a11y/ while this runs (a real sweep, recipe recording). So: a file that
// changed is allowed only when neither its path nor its content names anything this test made (its
// run ids, the fixture origin, its temp dir). With no other writer, nothing changes at all.
test('the committed a11y/ directory is untouched', (t) => {
  const now = treeHashes(COMMITTED);
  const changed = [...new Set([...Object.keys(committedBefore), ...Object.keys(now)])]
    .filter((p) => committedBefore[p] !== now[p]).sort();
  const markers = [R1, R2, origin, tmp];
  const ours = changed.filter((p) => {
    if (markers.some((m) => p.includes(m))) return true;
    try {
      const text = readFileSync(join(COMMITTED, p), 'utf8');
      return markers.some((m) => text.includes(m));
    } catch { return false; }
  });
  assert.deepEqual(ours, [], 'files under a11y/ written by this test');
  if (changed.length) t.diagnostic(`${changed.length} files under a11y/ changed by another process`);
});
