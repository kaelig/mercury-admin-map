// U5: the sweep runner, against the fixture sites (tools/a11y/fixtures/static, interactive, app) and
// the synthetic sites in tests/data/sweep/sites. Every test runs `sweep.mjs` as a child process in a
// temp workspace planned by `audit.py plan`, and asserts on what it writes.
//
// Expected outcomes come from the fixtures' design: fixtures/app/recipes has no recipe for
// home.chart-tooltip, and tests/data/sweep/recipes/interactive.json has none for
// interactive.toast-copied (its toast is gone before a replay can prove arrival), so both are
// no-recipe; every other fixture state has an accepted recipe and is audited.
import { describe, test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { cpSync, existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, unlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ROOT, harnessHash, readJson, writeJson } from '../lib/canon.mjs';
import { startServer } from '../fixtures/server.mjs';
import { Queue } from '../lib/queue.mjs';

const A11Y = join(ROOT, 'tools', 'a11y');
const FIXTURES = join(A11Y, 'fixtures');
const DATA = join(A11Y, 'tests', 'data', 'sweep');
const SITES = join(DATA, 'sites');
const RUN = '2026-10-02-1500';
const EXIT_ENVIRONMENT = 75;

const APP_RECIPES = Object.fromEntries(readdirSync(join(FIXTURES, 'app', 'recipes')).map((f) => [f.replace(/\.json$/, ''), readJson(join(FIXTURES, 'app', 'recipes', f))]));
const FIXTURE_RECIPES = { ...APP_RECIPES, interactive: readJson(join(DATA, 'recipes', 'interactive.json')) };
const NO_RECIPE = ['home.chart-tooltip', 'interactive.toast-copied'];

// The families each kind gets (schemas.md "State kinds and the checks they get"); dialog only when the
// final hop is a click or hover, which every fixture overlay's is.
const BASE = ['axe', 'records', 'interactive', 'target'];
const FAMILIES = {
  page: [...BASE, 'tabwalk'],
  modal: [...BASE, 'tabwalk', 'dialog'],
  drawer: [...BASE, 'tabwalk', 'dialog'],
  menu: [...BASE, 'dialog'],
  popover: [...BASE, 'dialog'],
  toast: [...BASE, 'status'],
  'validation-error': [...BASE, 'status'],
};
// Fixture states built to stop a check short: a dialog check whose trigger never takes focus, never
// opens the layer or cannot close it again (the details panel has no close control and ignores Esc),
// and Tab walks that hit the press limit or a trap. The
// state is still audited; the family is listed as incomplete.
const INCOMPLETE = {
  'home.send-modal': ['dialog'],
  'home.move-money-menu': ['dialog'],
  'transactions.filters-menu': ['dialog'],
  'interactive.drawer-payment': ['dialog'],
  'interactive.drawer-settings': ['dialog'],
  'interactive.panel-details': ['dialog'],
  'interactive.popover-fees': ['dialog'],
  'interactive.popover-card-art': ['dialog'],
  'interactive.popover-filters': ['dialog'],
  'interactive.iframe-trap': ['tabwalk'],
  'interactive.trap': ['tabwalk'],
};
const RAW_KEYS = ['build', 'checks', 'env', 'families', 'frames', 'harness', 'kind', 'layer', 'reason', 'reasonDetail',
  'recipeHash', 'records', 'results', 'schema', 'state', 'status', 'url'];

const temps = [];
const servers = [];
let fixtures;

before(async () => {
  fixtures = await startServer({ dirs: ['static', 'interactive', 'app'] });
});

after(async () => {
  await Promise.all([fixtures, ...servers].map((s) => s?.close()));
  for (const t of temps) rmSync(t, { recursive: true, force: true });
});

function tmp(prefix) {
  const d = mkdtempSync(join(tmpdir(), prefix));
  temps.push(d);
  return d;
}

async function serve(dirs, hooks = {}) {
  const s = await startServer({ dirs, hooks });
  servers.push(s);
  return s;
}

// A temp workspace: the map, `audit.py plan`, then recipes/<area>.json for each area given.
function workspace(map, recipes = {}) {
  const root = tmp('a11y-sweep-');
  const mapPath = join(root, 'map.json');
  writeJson(mapPath, map);
  const r = spawnSync('python3', [join(A11Y, 'audit.py'), 'plan', '--workspace', root, '--map', mapPath, '--run', RUN], { encoding: 'utf8' });
  assert.equal(r.status, 0, r.stderr);
  for (const [area, doc] of Object.entries(recipes)) writeJson(join(root, 'recipes', `${area}.json`), doc);
  const run = join(root, 'runs', RUN);
  return { root, mapPath, run, raw: join(run, 'raw'), states: readJson(join(run, 'states.json')).states };
}

const rawFiles = (w) => (existsSync(w.raw) ? readdirSync(w.raw).filter((f) => f.endsWith('.json')).sort() : []);
const raw = (w, id) => readJson(join(w.raw, `${id}.json`));
const rawBytes = (w) => Object.fromEntries(rawFiles(w).map((f) => [f, readFileSync(join(w.raw, f), 'utf8')]));
const inodes = (w) => Object.fromEntries(rawFiles(w).map((f) => [f, statSync(join(w.raw, f)).ino]));
const ids = (w) => Object.keys(w.states).sort();

// Run sweep.mjs as audit.py would, without blocking this process (it serves the fixture pages).
// killAfter: SIGTERM the sweep once that many raw files exist.
function sweep(w, origin, args = [], { killAfter = null } = {}) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const child = spawn('node', [join(A11Y, 'sweep.mjs'), ...args, '--workspace', w.root, '--map', w.mapPath,
      '--base-url', origin, '--run', RUN], { stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => { stdout += d; });
    child.stderr.on('data', (d) => { stderr += d; });
    const poll = killAfter ? setInterval(() => {
      if (rawFiles(w).length < killAfter) return;
      clearInterval(poll);
      child.kill('SIGTERM');
    }, 10) : null;
    const timer = setTimeout(() => child.kill('SIGKILL'), 900000);
    child.on('close', (status, signal) => {
      clearInterval(poll);
      clearTimeout(timer);
      let json = null;
      try {
        json = JSON.parse(stdout.trim().split('\n').pop());
      } catch { /* not JSON */ }
      resolve({ status, signal, stdout, stderr, json, ms: Date.now() - t0 });
    });
  });
}

const sorted = (xs) => [...xs].sort();

// The fixture site in tests/data/sweep/sites/drift with config.js rewritten, on its own server.
async function drift(config) {
  const dir = join(tmp('a11y-drift-'), 'drift');
  cpSync(join(SITES, 'drift'), dir, { recursive: true });
  writeFileSync(join(dir, 'config.js'), `window.DRIFT = ${JSON.stringify({ settingsWord: 'Open', panelWord: 'Open', ...config })};\n`);
  return serve([dir]);
}

const button = (name) => ({ role: 'button', name });
const accepted = (parent, hop) => ({ parent, hop, fingerprint: null, mutates: false, source: null, status: 'accepted',
  similarity: null, recordedBy: 'hand', review: null });

// ---------------------------------------------------------------------------
// The dispatch queue (lib/queue.mjs)
// ---------------------------------------------------------------------------

describe('queue', () => {
  const tree = { a: null, b: 'a', c: 'b', d: 'a', e: null };
  const parentOf = (id) => tree[id] ?? null;
  const pending = (p) => Promise.race([p, new Promise((r) => setTimeout(() => r('waiting'), 20))]);

  test('a state is released only once its parent has an outcome', async () => {
    const q = new Queue(['c', 'b', 'a', 'd', 'e'], parentOf);
    assert.deepEqual([await q.next(), await q.next()], [{ id: 'a', blocked: null }, { id: 'e', blocked: null }]);
    const third = q.next();
    assert.equal(await pending(third), 'waiting', 'b and d wait for a');
    q.finish('a', { unreachable: false, rootCause: null });
    assert.deepEqual(await third, { id: 'b', blocked: null });
    assert.deepEqual(await q.next(), { id: 'd', blocked: null });
    q.finish('e', { unreachable: false, rootCause: null });
    q.finish('d', { unreachable: false, rootCause: null });
    const fifth = q.next();
    assert.equal(await pending(fifth), 'waiting', 'c waits for b');
    q.finish('b', { unreachable: false, rootCause: null });
    assert.deepEqual(await fifth, { id: 'c', blocked: null });
    q.finish('c', { unreachable: false, rootCause: null });
    assert.equal(await q.next(), null);
  });

  test('children of an unreachable parent come back blocked, naming the root cause down the tree', async () => {
    const q = new Queue(['a', 'b', 'c', 'd'], parentOf);
    assert.equal((await q.next()).id, 'a');
    q.finish('a', { unreachable: true, rootCause: 'a' });
    assert.deepEqual([await q.next(), await q.next()], [{ id: 'b', blocked: 'a' }, { id: 'd', blocked: 'a' }]);
    q.finish('b', { unreachable: true, rootCause: 'a' });
    assert.deepEqual(await q.next(), { id: 'c', blocked: 'a' });
  });

  test('on resume, states with a valid raw file release their children', async () => {
    const q = new Queue(['b', 'c', 'e'], parentOf, { done: { a: { unreachable: true, rootCause: 'x' } } });
    assert.deepEqual([await q.next(), await q.next()], [{ id: 'b', blocked: 'x' }, { id: 'e', blocked: null }]);
  });

  test('stop() hands every waiting worker null', async () => {
    const q = new Queue(['a', 'b'], parentOf);
    await q.next();
    const waiting = q.next();
    q.stop();
    assert.equal(await waiting, null);
    assert.equal(await q.next(), null);
    assert.equal(q.pending, 1);
  });

  test('a held state is settled by onIdle once nothing else is in flight, then releases its children', async () => {
    const seen = [];
    const q = new Queue(['a', 'b', 'e'], parentOf, { onIdle: (held) => { seen.push(held); return held.map((id) => [id, { unreachable: true, rootCause: id }]); } });
    await q.next();
    await q.next();
    q.hold('a');
    const next = q.next();
    assert.equal(await pending(next), 'waiting', 'e is still in flight');
    q.finish('e', { unreachable: false, rootCause: null });
    assert.deepEqual(await next, { id: 'b', blocked: 'a' });
    assert.deepEqual(seen, [['a']]);
  });

  test('a parent chain that loops is released as if it had no parent', async () => {
    const loop = { x: 'y', y: 'x' };
    const q = new Queue(['x', 'y'], (id) => loop[id]);
    assert.deepEqual(sorted([(await q.next()).id, (await q.next()).id]), ['x', 'y']);
  });
});

// ---------------------------------------------------------------------------
// Full fixture sweeps: coverage, outcomes, determinism, resume, status
// ---------------------------------------------------------------------------

describe('fixture sweep', () => {
  let map, A;

  before(() => {
    map = fixtures.mergedMap();
  });

  test('3 workers write one raw file per fixture state, and the coverage invariant holds', async (t) => {
    A = workspace(map, FIXTURE_RECIPES);
    const r = await sweep(A, fixtures.origin, ['--workers', '3']);
    t.diagnostic(`full fixture sweep, ${ids(A).length} states, 3 workers: ${r.ms} ms`);
    assert.equal(r.status, 0, r.stderr.slice(-4000));
    const { ms, ...summary } = r.json;
    assert.ok(Number.isFinite(ms));
    assert.deepEqual(summary, { run: RUN, audited: ids(A).length - NO_RECIPE.length, unreachable: NO_RECIPE.length, pending: 0, interrupted: null });

    assert.deepEqual(rawFiles(A), ids(A).map((id) => `${id}.json`).sort());
    const audited = [];
    const unreachable = [];
    for (const id of ids(A)) {
      const doc = raw(A, id);
      assert.deepEqual(Object.keys(doc).sort(), RAW_KEYS, id);
      assert.equal(doc.state, id);
      assert.equal(doc.schema, 1);
      assert.equal(doc.kind, A.states[id].kind);
      assert.equal(doc.harness, harnessHash());
      assert.equal(doc.url, A.states[id].url);
      assert.deepEqual(doc.env.viewport, [1440, 900]);
      assert.match(doc.env.chromium, /^\d+\./);
      (doc.status === 'audited' ? audited : unreachable).push(id);
    }
    // Coverage: audited and unreachable are exactly the states, with no overlap.
    assert.deepEqual(sorted([...audited, ...unreachable]), ids(A));
    assert.deepEqual(sorted(unreachable), NO_RECIPE);

    for (const id of audited) {
      const doc = raw(A, id);
      assert.equal(doc.reason, null, id);
      const incomplete = INCOMPLETE[id] || [];
      assert.deepEqual(sorted(doc.families.completed), sorted(FAMILIES[doc.kind].filter((f) => !incomplete.includes(f))), `${id} (${doc.kind})`);
      assert.deepEqual(doc.families.incomplete, incomplete, id);
      assert.equal(doc.build, 'unknown', 'the fixtures load no cdn.mercury.com build');
      assert.match(doc.recipeHash, /^[0-9a-f]{64}$/);
      for (const fam of doc.families.completed.filter((f) => f !== 'records')) assert.ok(fam in doc.checks, `${id}: checks.${fam}`);
      assert.equal('records' in doc.checks, false);
      assert.deepEqual(Object.keys(doc.checks.axe).sort(), ['inapplicable', 'rulesRan']);
    }
    for (const id of NO_RECIPE) {
      const doc = raw(A, id);
      assert.equal(doc.reason, 'no-recipe');
      assert.deepEqual(doc.families, { completed: [], incomplete: [] });
      assert.deepEqual([doc.results, doc.records, doc.checks, doc.layer], [[], [], {}, null]);
    }

    // The checks ran on the right layer and saw what each fixture page was built to break.
    assert.deepEqual(raw(A, 'interactive.dialog-address').layer.scope, 'top');
    assert.deepEqual(raw(A, 'interactive.dialog-address').layer.role, 'dialog');
    assert.equal(raw(A, 'transactions.drawer').layer.scope, 'top');
    assert.equal(raw(A, 'static.contrast').layer.scope, 'document');
    const rules = (id) => raw(A, id).results.map((x) => x.rule);
    assert.ok(rules('static.contrast').includes('color-contrast'));
    assert.ok(rules('interactive.dialog-pin').includes('dialog-focus-not-returned'), 'Esc leaves focus on the body');
    assert.ok(rules('interactive.drawer-payment').includes('dialog-trigger-not-focusable'), 'a clickable row with no tab stop');
    assert.ok(rules('interactive.popover-fees').includes('hover-only'), 'a hover-only tooltip');
    assert.ok(rules('interactive.toast-sent').includes('status-not-announced'), 'a live region created with its message');
    assert.ok(!rules('interactive.toast-saved').includes('status-not-announced'), 'a message in the page-load status region');
    assert.ok(rules('interactive.form-errors').includes('error-not-associated'), 'the ZIP error is not tied to its field');
    assert.equal(raw(A, 'interactive.toast-saved').checks.status.message, 'Changes saved');
    assert.ok(raw(A, 'interactive.dialog-address').records.length > 0);
    // Frames: the axe detail's frame list moves to the top level, cross-origin marked.
    const frames = raw(A, 'static.frame').frames;
    assert.deepEqual(frames.map((f) => f.firstParty).sort(), [false, true]);
    assert.ok(frames.some((f) => f.origin === fixtures.origin2));
    assert.equal('frames' in raw(A, 'static.frame').checks.axe, false);
    // The Tab walk on a page; no Tab walk on a menu.
    assert.ok(raw(A, 'interactive.focus-rings').checks.tabwalk.stops.length > 0);
    assert.equal('tabwalk' in raw(A, 'interactive.menu-actions').checks, false);
  });

  test('outcomes.json lists every state with its outcome, reason, first-unreachable run and families, matching the raw files', () => {
    const o = readJson(join(A.run, 'outcomes.json'));
    assert.equal(o.run, RUN);
    assert.deepEqual(Object.keys(o.states).sort(), ids(A));
    assert.deepEqual(o.counts, { audited: ids(A).length - NO_RECIPE.length, unreachable: NO_RECIPE.length });
    assert.deepEqual(o.builds, ['unknown']);
    for (const id of ids(A)) {
      const doc = raw(A, id);
      assert.deepEqual(o.states[id], {
        outcome: doc.status, reason: doc.reason, detail: doc.reasonDetail,
        firstUnreachable: doc.status === 'unreachable' ? RUN : null,
        completed: doc.families.completed, incomplete: doc.families.incomplete, build: doc.build,
      }, id);
    }
    const run = readJson(join(A.run, 'run.json'));
    assert.equal(run.harness, harnessHash());
    assert.match(run.versions.chromium, /^\d+\./);
    assert.deepEqual(run.builds, ['unknown']);
    assert.equal(run.stages.sweep.interrupted, null);
    assert.ok(run.stages.sweep.started && run.stages.sweep.finished);
  });

  test('status reports every fixture state accounted for', async () => {
    const r = spawnSync('python3', [join(A11Y, 'audit.py'), 'status', '--workspace', A.root, '--map', A.mapPath], { encoding: 'utf8' });
    assert.equal(r.status, 0, r.stderr);
    const sweepStatus = JSON.parse(r.stdout).stages.sweep;
    assert.equal(sweepStatus.audited + sweepStatus.unreachable, ids(A).length);
    assert.deepEqual([sweepStatus.pending, sweepStatus.stale, sweepStatus.states], [0, 0, ids(A).length]);
    assert.deepEqual(sweepStatus.outcomes, { written: true, coverage: 'ok' });

    const own = await sweep(A, fixtures.origin, ['status']);
    assert.equal(own.status, 0, own.stderr);
    assert.deepEqual(own.json, { run: RUN, states: ids(A).length, audited: ids(A).length - NO_RECIPE.length,
      unreachable: NO_RECIPE.length, stale: 0, pending: 0, outcomes: true });

    const empty = tmp('a11y-empty-');
    for (const extra of [[], ['--run', RUN]]) {
      const p = spawnSync('node', [join(A11Y, 'sweep.mjs'), 'status', '--workspace', empty, ...extra], { encoding: 'utf8' });
      assert.equal(p.status, 0, p.stderr);
      assert.deepEqual(JSON.parse(p.stdout.trim().split('\n').pop()), { pending: true });
    }
  });

  test('two full sweeps give identical canonical output (8 workers against 3)', async (t) => {
    const B = workspace(map, FIXTURE_RECIPES);
    const r = await sweep(B, fixtures.origin, ['--workers', '8']);
    t.diagnostic(`full fixture sweep, ${ids(B).length} states, 8 workers: ${r.ms} ms`);
    assert.equal(r.status, 0, r.stderr.slice(-4000));
    const [a, b] = [rawBytes(A), rawBytes(B)];
    assert.deepEqual(Object.keys(b), Object.keys(a));
    const differ = Object.keys(a).filter((f) => a[f] !== b[f]);
    assert.deepEqual(differ, [], 'raw files that differ between the two sweeps');
    assert.equal(readFileSync(join(B.run, 'outcomes.json'), 'utf8'), readFileSync(join(A.run, 'outcomes.json'), 'utf8'));
  });

  test('a sweep aborted after 5 states and resumed runs only the rest, and ends where an uninterrupted sweep does', async () => {
    const C = workspace(map, FIXTURE_RECIPES);
    const first = await sweep(C, fixtures.origin, ['--workers', '3'], { killAfter: 5 });
    assert.equal(first.status, 143, 'stopped by SIGTERM');
    const kept = inodes(C);
    assert.ok(Object.keys(kept).length >= 5 && Object.keys(kept).length < ids(C).length, `${Object.keys(kept).length} written before the abort`);
    assert.equal(existsSync(join(C.run, 'outcomes.json')), false);

    const second = await sweep(C, fixtures.origin, ['--workers', '3']);
    assert.equal(second.status, 0, second.stderr.slice(-4000));
    const after = inodes(C);
    for (const [f, ino] of Object.entries(kept)) assert.equal(after[f], ino, `${f} was swept again`);
    const swept = second.stderr.split('\n').filter((l) => /^sweep: \[\d+\/\d+\]/.test(l)).length;
    assert.equal(swept, ids(C).length - Object.keys(kept).length, 'the resumed sweep handled only the states without a raw file');
    assert.deepEqual(rawBytes(C), rawBytes(A));
    assert.equal(readFileSync(join(C.run, 'outcomes.json'), 'utf8'), readFileSync(join(A.run, 'outcomes.json'), 'utf8'));
  });
});

// ---------------------------------------------------------------------------
// Resume across a harness change, and --state
// ---------------------------------------------------------------------------

const SMALL = ['interactive.dialogs', 'interactive.dialog-address', 'interactive.menus', 'interactive.menu-actions'];

function subset(map, keep) {
  const set = new Set(keep);
  return { areas: map.areas.map((a) => ({ ...a, nodes: (a.nodes || []).filter((n) => set.has(n.id)),
    edges: (a.edges || []).filter((e) => set.has(e.from) && set.has(e.to)), journeys: [] })).filter((a) => a.nodes.length) };
}

test('a check module changed between an aborted and a resumed sweep: every state is swept again', async () => {
  const w = workspace(subset(fixtures.mergedMap(), SMALL), FIXTURE_RECIPES);
  let r = await sweep(w, fixtures.origin, ['--workers', '2']);
  assert.equal(r.status, 0, r.stderr.slice(-4000));
  const before = inodes(w);
  // What an older harness left: every raw file under another harness hash, one cut short, one never written.
  for (const f of rawFiles(w)) {
    const doc = readJson(join(w.raw, f));
    writeJson(join(w.raw, f), { ...doc, harness: '0123456789abcdef' });
  }
  writeFileSync(join(w.raw, 'interactive.menus.json'), '{"schema": 1, "state": "interactive.me');
  unlinkSync(join(w.raw, 'interactive.menu-actions.json'));
  unlinkSync(join(w.run, 'outcomes.json'));

  r = await sweep(w, fixtures.origin, ['--workers', '2']);
  assert.equal(r.status, 0, r.stderr.slice(-4000));
  assert.deepEqual(r.json.audited, SMALL.length);
  const after = inodes(w);
  for (const f of Object.keys(before)) assert.notEqual(after[f], before[f], `${f} kept`);
  for (const id of SMALL) assert.equal(raw(w, id).harness, harnessHash(), id);
  assert.ok(existsSync(join(w.run, 'outcomes.json')));

  // --state sweeps the named state again even with a valid raw file, and nothing else.
  const mid = inodes(w);
  r = await sweep(w, fixtures.origin, ['--state', 'interactive.dialog-address']);
  assert.equal(r.status, 0, r.stderr.slice(-4000));
  const last = inodes(w);
  assert.deepEqual(Object.keys(last).filter((f) => last[f] !== mid[f]), ['interactive.dialog-address.json']);
  const bad = await sweep(w, fixtures.origin, ['--state', 'interactive.nope']);
  assert.equal(bad.status, 2);
});

// ---------------------------------------------------------------------------
// Recipe drift: never trips the breaker
// ---------------------------------------------------------------------------

test('a renamed trigger above a parent with 15 descendants: 1 step-failed, 15 blocked-by-parent naming it, no trip', async () => {
  const server = await drift({ settingsWord: 'Edit' });
  const nodes = [{ id: 'drift.index', kind: 'page', url: '/drift/index.html' }, { id: 'drift.settings', kind: 'modal', url: '/drift/index.html' }];
  const edges = [{ from: 'drift.index', to: 'drift.settings', label: 'Click Open settings' }];
  const recipes = { 'drift.settings': accepted('drift.index', [{ click: button('Open settings') }]) };
  const descendants = [];
  for (let k = 1; k <= 5; k++) {
    const section = `drift.section-${k}`;
    nodes.push({ id: section, kind: 'popover', url: '/drift/index.html' });
    edges.push({ from: 'drift.settings', to: section, label: `Click Section ${k}` });
    recipes[section] = accepted('drift.settings', [{ click: button(`Section ${k}`) }]);
    descendants.push(section);
    for (const x of ['a', 'b']) {
      const item = `drift.item-${k}${x}`;
      nodes.push({ id: item, kind: 'popover', url: '/drift/index.html' });
      edges.push({ from: section, to: item, label: `Click Item ${k}${x}` });
      recipes[item] = accepted(section, [{ click: button(`Item ${k}${x}`) }]);
      descendants.push(item);
    }
  }
  assert.equal(descendants.length, 15);
  const w = workspace({ areas: [{ area: 'drift', nodes, edges }] }, { drift: recipes });
  const r = await sweep(w, server.origin, ['--workers', '3']);
  assert.equal(r.status, 0, r.stderr.slice(-4000));
  assert.equal(r.json.interrupted, null);
  assert.equal(raw(w, 'drift.index').status, 'audited');
  const settings = raw(w, 'drift.settings');
  assert.equal(settings.reason, 'step-failed');
  assert.equal(settings.reasonDetail.step, 1, 'step 0 loads the root; step 1 clicks the trigger');
  assert.match(settings.reasonDetail.message, /Open settings/);
  for (const id of descendants) {
    const doc = raw(w, id);
    assert.equal(doc.reason, 'blocked-by-parent', id);
    assert.equal(doc.reasonDetail.rootCause, 'drift.settings', id);
  }
  assert.deepEqual(readJson(join(w.run, 'outcomes.json')).counts, { audited: 1, unreachable: 16 });
  assert.equal(readJson(join(w.run, 'run.json')).stages.sweep.interrupted, null);
});

test('12 unrelated sibling recipes break at once: 12 step-failed, no trip', async () => {
  const server = await drift({ panelWord: 'Show' });
  const nodes = [{ id: 'drift.index', kind: 'page', url: '/drift/index.html' }];
  const edges = [];
  const recipes = {};
  for (let n = 1; n <= 12; n++) {
    nodes.push({ id: `drift.panel-${n}`, kind: 'modal', url: '/drift/index.html' });
    edges.push({ from: 'drift.index', to: `drift.panel-${n}`, label: `Click Open panel ${n}` });
    recipes[`drift.panel-${n}`] = accepted('drift.index', [{ click: button(`Open panel ${n}`) }]);
  }
  const w = workspace({ areas: [{ area: 'drift', nodes, edges }] }, { drift: recipes });
  const r = await sweep(w, server.origin, ['--workers', '3']);
  assert.equal(r.status, 0, r.stderr.slice(-4000));
  assert.deepEqual([r.json.audited, r.json.unreachable, r.json.interrupted], [1, 12, null]);
  for (let n = 1; n <= 12; n++) {
    const doc = raw(w, `drift.panel-${n}`);
    assert.equal(doc.reason, 'step-failed');
    assert.match(doc.reasonDetail.message, new RegExp(`Open panel ${n}\\b`));
  }
});

// ---------------------------------------------------------------------------
// Environment signals: the circuit breaker
// ---------------------------------------------------------------------------

const STATIC_FIVE = ['static.contrast', 'static.field', 'static.same-a', 'static.same-b', 'static.inline-link'];

test('429 on 15 consecutive navigations: the sweep stops as interrupted: environment and writes no unreachable records', async () => {
  const server = await serve(['static'], { status429: 15 });
  const w = workspace(subset(server.mergedMap(), STATIC_FIVE));
  const r = await sweep(w, server.origin, ['--workers', '3']);
  assert.equal(r.status, EXIT_ENVIRONMENT, r.stderr.slice(-4000));
  assert.equal(r.json.interrupted, 'environment');
  assert.deepEqual(rawFiles(w).filter((f) => raw(w, f.replace(/\.json$/, '')).status === 'unreachable'), []);
  assert.equal(r.json.unreachable, 0);
  assert.equal(existsSync(join(w.run, 'outcomes.json')), false);
  assert.equal(readJson(join(w.run, 'run.json')).stages.sweep.interrupted, 'environment');

  // Once the demo answers again, resuming finishes the run.
  server.setHook('status429', null);
  const again = await sweep(w, server.origin, ['--workers', '3']);
  assert.equal(again.status, 0, again.stderr.slice(-4000));
  assert.deepEqual([again.json.audited, again.json.unreachable, again.json.interrupted], [5, 0, null]);
  assert.equal(readJson(join(w.run, 'run.json')).stages.sweep.interrupted, null);
});

test('a challenge page trips the breaker', async () => {
  const server = await serve([join(SITES, 'gate')]);
  const w = workspace({ areas: [{ area: 'gate', nodes: [{ id: 'gate.index', kind: 'page', url: '/gate/index.html' }] }] });
  const r = await sweep(w, server.origin);
  assert.equal(r.status, EXIT_ENVIRONMENT, r.stderr.slice(-4000));
  assert.equal(r.json.interrupted, 'environment');
  assert.deepEqual(rawFiles(w), []);
});

test('three consecutive root pages that fail to load trip the breaker, and none is written as unreachable', async () => {
  const server = await serve(['static'], { crash: () => true });
  const w = workspace(subset(server.mergedMap(), STATIC_FIVE));
  const r = await sweep(w, server.origin, ['--workers', '3']);
  assert.equal(r.status, EXIT_ENVIRONMENT, r.stderr.slice(-4000));
  assert.equal(r.json.interrupted, 'environment');
  assert.deepEqual(rawFiles(w), []);

  server.setHook('crash', null);
  const again = await sweep(w, server.origin, ['--workers', '3']);
  assert.equal(again.status, 0, again.stderr.slice(-4000));
  assert.equal(again.json.audited, 5);
});

test('the third root failure arriving while the other workers are idle trips the breaker before the held roots are written', async () => {
  // One worker per state: when the last root fails, nothing is ready or in flight and the others wait.
  const server = await serve(['static'], { crash: () => true });
  const w = workspace(subset(server.mergedMap(), STATIC_FIVE.slice(0, 3)));
  const r = await sweep(w, server.origin, ['--workers', '3']);
  assert.equal(r.status, EXIT_ENVIRONMENT, r.stderr.slice(-4000));
  assert.equal(r.json.interrupted, 'environment');
  assert.deepEqual(rawFiles(w), []);
});

// ---------------------------------------------------------------------------
// Errors, previous runs, vanishing toasts, hung browsers
// ---------------------------------------------------------------------------

test('an injected page crash is retried twice, then recorded as unreachable(tool-error); its children are blocked', async () => {
  let hits = 0;
  const server = await serve(['interactive'], { crash: (path) => path === '/interactive/menus.html' && ++hits > 0 });
  const keep = ['interactive.menus', 'interactive.menu-actions', 'interactive.menu-export', 'interactive.dialogs', 'interactive.dialog-address'];
  const w = workspace(subset(server.mergedMap(), keep), FIXTURE_RECIPES);
  const r = await sweep(w, server.origin, ['--workers', '2']);
  assert.equal(r.status, 0, r.stderr.slice(-4000));
  assert.equal(r.json.interrupted, null);
  // Chromium resends a navigation once after an empty response, so the hook counts requests, not attempts.
  assert.ok(hits >= 3, `${hits} requests`);
  const retries = r.stderr.split('\n').filter((l) => /^sweep: retry interactive\.menus \(attempt [23] of 3\)/.test(l));
  assert.equal(retries.length, 2, 'one attempt and two retries');
  const menus = raw(w, 'interactive.menus');
  assert.equal(menus.reason, 'tool-error');
  assert.equal(menus.reasonDetail.step, 0);
  assert.match(menus.reasonDetail.message, /ERR_EMPTY_RESPONSE/);
  for (const id of ['interactive.menu-actions', 'interactive.menu-export']) {
    assert.equal(raw(w, id).reason, 'blocked-by-parent');
    assert.equal(raw(w, id).reasonDetail.rootCause, 'interactive.menus');
  }
  assert.equal(raw(w, 'interactive.dialog-address').status, 'audited');
  assert.ok(existsSync(join(w.run, 'outcomes.json')));
});

test('a state unreachable in the previous published run records that run as the start of the gap', async () => {
  const PREV = '2026-10-01-0900';
  const app = readJson(join(FIXTURES, 'app', 'map.json'));
  const map = { areas: app.areas.filter((a) => ['home', 'cards'].includes(a.area)) };
  const home = { ...APP_RECIPES.home };
  const cards = { ...APP_RECIPES.cards };
  delete home['home.send-toast'];
  delete cards['cards.freeze-modal'];
  const w = workspace(map, { home, cards });
  writeJson(join(w.root, 'published', 'meta.json'), { run: PREV, date: '2026-10-01' });
  writeJson(join(w.root, 'runs', PREV, 'outcomes.json'), { run: PREV, counts: {}, builds: [], states: {
    'home.chart-tooltip': { outcome: 'unreachable', reason: 'no-recipe', detail: null, firstUnreachable: PREV, completed: [], incomplete: [], build: null },
    'cards.freeze-modal': { outcome: 'unreachable', reason: 'step-failed', detail: null, firstUnreachable: '2026-09-28-1000', completed: [], incomplete: [], build: 'abc' },
    'home.send-toast': { outcome: 'audited', reason: null, detail: null, firstUnreachable: null, completed: [], incomplete: [], build: 'abc' },
    'cards.edit-limit-modal': { outcome: 'unreachable', reason: 'step-failed', detail: null, firstUnreachable: PREV, completed: [], incomplete: [], build: 'abc' },
  } });
  const r = await sweep(w, fixtures.origin, ['--workers', '3']);
  assert.equal(r.status, 0, r.stderr.slice(-4000));
  const o = readJson(join(w.run, 'outcomes.json')).states;
  assert.equal(o['home.chart-tooltip'].firstUnreachable, PREV, 'unreachable since the previous run');
  assert.equal(o['cards.freeze-modal'].firstUnreachable, '2026-09-28-1000', 'carried from the previous run');
  assert.equal(o['home.send-toast'].firstUnreachable, RUN, 'audited before, unreachable from this run on');
  assert.equal(o['cards.edit-limit-modal'].outcome, 'audited');
  assert.equal(o['cards.edit-limit-modal'].firstUnreachable, null);
});

test('a toast that disappears mid-check is audited, with its incomplete families listed', async () => {
  const server = await serve([join(SITES, 'toast')]);
  const map = { areas: [{ area: 'toast', nodes: [
    { id: 'toast.index', kind: 'page', url: '/toast/index.html' },
    { id: 'toast.notify', kind: 'toast', url: '/toast/index.html' },
  ], edges: [{ from: 'toast.index', to: 'toast.notify', label: 'Click Notify' }] }] };
  const w = workspace(map, { toast: readJson(join(DATA, 'recipes', 'toast.json')) });
  const r = await sweep(w, server.origin);
  assert.equal(r.status, 0, r.stderr.slice(-4000));
  const doc = raw(w, 'toast.notify');
  assert.equal(doc.status, 'audited');
  // The status observer runs first and sees the toast; it leaves as the rule scan finishes.
  assert.deepEqual(doc.families.completed, ['status']);
  assert.deepEqual(sorted(doc.families.incomplete), ['axe', 'interactive', 'records', 'target']);
  assert.equal(doc.checks.status.message, 'Reminder set');
  assert.equal(doc.checks.status.region.preexisting, true);
  assert.deepEqual(readJson(join(w.run, 'outcomes.json')).states['toast.notify'].incomplete, doc.families.incomplete);
  assert.deepEqual(raw(w, 'toast.index').families.incomplete, []);
});

test('a hung browser is closed and restarted; the state ends as tool-error and the sweep goes on', async () => {
  const server = await serve([join(SITES, 'hang'), join(SITES, 'toast')]);
  const map = { areas: [{ area: 'hang', nodes: [{ id: 'hang.index', kind: 'page', url: '/hang/index.html' }] },
    { area: 'toast', nodes: [{ id: 'toast.index', kind: 'page', url: '/toast/index.html' }] }] };
  const w = workspace(map);
  const r = await sweep(w, server.origin, ['--workers', '1', '--timeout', '4000']);
  assert.equal(r.status, 0, r.stderr.slice(-4000));
  const hung = raw(w, 'hang.index');
  assert.equal(hung.reason, 'tool-error');
  assert.match(hung.reasonDetail.message, /4000 ms/);
  assert.equal(raw(w, 'toast.index').status, 'audited', 'the restarted browser sweeps the next state');
});
