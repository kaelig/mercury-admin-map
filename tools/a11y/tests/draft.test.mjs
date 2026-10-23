// `recipe draft`'s attempt log and --ready selection, without a browser. The drafts themselves run
// in recipes.test.mjs, against the fixture app.
import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { appendFileSync, cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ROOT, canonical, readJson, readJsonl, sha, writeJson } from '../lib/canon.mjs';
import { Workspace } from '../lib/cli.mjs';
import { loadRecipes } from '../recipe.mjs';
import { acceptedChainHash, appendAttempt, attemptLine, inShard, parentRecipe, parseShard, readAttempts, readyStates } from '../draft.mjs';

const A11Y = join(ROOT, 'tools', 'a11y');
const APP = join(A11Y, 'fixtures', 'app');
const RUN = '2026-10-02-1200';

const temps = [];
after(() => {
  for (const t of temps) rmSync(t, { recursive: true, force: true });
});

function tmp(prefix) {
  const d = mkdtempSync(join(tmpdir(), prefix));
  temps.push(d);
  return d;
}

const fp = (path) => ({ path, top: { role: 'document', name: '' }, headings: [], controls: [], structure: 'document' });
const accepted = (parent, hop = []) => ({ parent, hop, fingerprint: fp(parent), status: 'accepted' });
const node = (parent, extra = {}) => ({ root: false, parent, labels: [`Click ${parent}`], path: [parent], unreachable: null, ...extra });

// Two roots and their subtrees. r.index's subtree has every case; s.index has no recipe yet.
function fixture() {
  const states = {
    'r.index': { root: true, parent: null, rootUrl: '/r', labels: null, path: ['r.index'], unreachable: null },
    's.index': { root: true, parent: null, rootUrl: '/s', labels: null, path: ['s.index'], unreachable: null },
    'r.new': node('r.index'),
    'r.tried': node('r.index'),
    'r.rejected': node('r.index'),
    'r.review': node('r.index'),
    'r.under-review': node('r.review'),
    'r.mid': node('r.index'),
    'r.deep': node('r.mid'),
    'r.orphan': node('r.missing'),
    'x.no-path': { root: false, parent: null, labels: null, path: null, unreachable: 'no-path' },
  };
  const recipes = {
    'r.index': accepted('/r'),
    'r.rejected': { ...accepted('r.index', [{ click: { name: 'X' } }]), status: 'rejected' },
    'r.review': { ...accepted('r.index', [{ click: { name: 'Review' } }]), status: 'review' },
    'r.mid': accepted('r.index', [{ click: { name: 'Mid' } }]),
  };
  return { states, recipes };
}

// What refreshQueue() would list: every state without an accepted or review recipe.
const queueOf = (states) => Object.keys(states).filter((id) => !['r.index', 'r.mid', 'r.review'].includes(id));

test('the parent recipe hash is the sweep\'s recipeHash of the parent\'s chain, and null until the whole chain is accepted', () => {
  const { states, recipes } = fixture();
  const want = sha([{ id: 'r.index', parent: '/r', hop: [], fingerprint: fp('/r') }]);
  assert.equal(acceptedChainHash(recipes, 'r.index'), want);
  assert.equal(parentRecipe(recipes, states['r.new']), want);
  assert.equal(parentRecipe(recipes, states['r.deep']), sha([
    { id: 'r.index', parent: '/r', hop: [], fingerprint: fp('/r') },
    { id: 'r.mid', parent: 'r.index', hop: [{ click: { name: 'Mid' } }], fingerprint: fp('r.index') },
  ]));
  assert.equal(parentRecipe(recipes, states['r.under-review']), null, 'the parent is in review');
  assert.equal(parentRecipe(recipes, states['r.orphan']), null, 'the parent has no recipe');
  assert.equal(parentRecipe(recipes, states['r.index']), null, 'a root replays no parent');
  assert.equal(parentRecipe(recipes, states['x.no-path']), null);
  // An accepted parent under a grandparent that is not: the chain is not accepted.
  const broken = { ...recipes, 'r.index': { ...recipes['r.index'], status: 'review' } };
  assert.equal(parentRecipe(broken, states['r.deep']), null);
});

test('--ready selects roots and states with an accepted parent, and skips the attempted, the rejected and the parentless', () => {
  const { states, recipes } = fixture();
  const attempts = [
    attemptLine({ state: 'r.tried', parent: 'r.index', parentRecipe: parentRecipe(recipes, states['r.tried']), label: 'Click r.index', result: 'queued', reason: 'cannot parse' }),
    // Attempted while its parent was in review: that hash is null, so it never blocks a later draft.
    attemptLine({ state: 'r.new', parent: 'r.index', parentRecipe: null, label: 'Click r.index', result: 'queued', reason: 'the parent r.index has no accepted recipe' }),
  ];
  const pick = readyStates(states, recipes, queueOf(states), attempts);
  assert.deepEqual(pick.selected, ['r.deep', 'r.new', 's.index']);
  assert.deepEqual(pick.skipped, { attempted: 1, noAcceptedParent: 3, rejected: 1 });
  assert.equal(pick.otherShards, 0);

  // A root drafted once is not drafted again: its attempts carry a null parent recipe.
  const rootTried = [...attempts, attemptLine({ state: 's.index', result: 'queued', reason: 'fingerprint-mismatch: …' })];
  assert.deepEqual(readyStates(states, recipes, queueOf(states), rootTried).selected, ['r.deep', 'r.new']);
});

test('a re-recorded parent makes its children ready again; a review decision on it does not', () => {
  const { states, recipes } = fixture();
  const hash = parentRecipe(recipes, states['r.deep']);
  const attempts = [attemptLine({ state: 'r.deep', parent: 'r.mid', parentRecipe: hash, label: 'Click r.mid', result: 'queued', reason: 'nothing matches' })];
  const ids = ['r.deep'];
  assert.deepEqual(readyStates(states, recipes, ids, attempts).selected, []);

  const noted = { ...recipes, 'r.mid': { ...recipes['r.mid'], similarity: 0.88, review: { reason: 'below-threshold', by: 'conductor', decision: 'accept', date: '2026-10-03' } } };
  assert.deepEqual(readyStates(states, noted, ids, attempts).selected, [], 'only hop, parent and fingerprint count');

  const rerecorded = { ...recipes, 'r.mid': { ...recipes['r.mid'], hop: [{ click: { role: 'button', name: 'Mid' } }] } };
  assert.deepEqual(readyStates(states, rerecorded, ids, attempts).selected, ['r.deep']);
  const refingerprinted = { ...recipes, 'r.index': { ...recipes['r.index'], fingerprint: fp('/r/again') } };
  assert.deepEqual(readyStates(states, refingerprinted, ids, attempts).selected, ['r.deep'], 'a re-recorded grandparent counts too');
});

test('--shard i/n splits the ready states into n disjoint parts whatever order they come in', () => {
  const { states, recipes } = fixture();
  const all = readyStates(states, recipes, queueOf(states), []).selected;
  const parts = [1, 2, 3].map((i) => readyStates(states, recipes, queueOf(states).reverse(), [], parseShard(`${i}/3`)));
  assert.deepEqual(parts.flatMap((p) => p.selected).sort(), all);
  for (const p of parts) assert.equal(p.otherShards + p.selected.length + Object.values(p.skipped).reduce((a, b) => a + b, 0), queueOf(states).length);
  for (const id of all) assert.equal([1, 2, 3].filter((i) => inShard(id, { index: i, count: 3 })).length, 1, id);
  assert.deepEqual(parseShard('8/8'), { index: 8, count: 8 });
  for (const bad of ['0/8', '9/8', '1/0', '1', 'a/b', '', undefined]) assert.throws(() => parseShard(bad), /--shard takes i\/n/, String(bad));
});

test('an attempt line holds the state, its parent, the parent recipe hash, the label, the result, the reason and the date', () => {
  const line = attemptLine({ state: 'r.new', parent: 'r.index', parentRecipe: 'abc', label: 'Click X', result: 'queued', reason: 'cannot parse "Click X"' });
  assert.deepEqual(Object.keys(line).sort(), ['date', 'label', 'parent', 'parentRecipe', 'reason', 'result', 'state']);
  assert.match(line.date, /^\d{4}-\d{2}-\d{2}$/);
  assert.equal(attemptLine({ state: 'r.new', result: 'accepted', reason: 'ignored' }).reason, null, 'only a queued attempt has a reason');
  const root = attemptLine({ state: 's.index', result: 'review' });
  assert.deepEqual([root.parent, root.parentRecipe, root.label, root.reason], [null, null, null, null]);

  const path = join(tmp('a11y-draftlog-'), 'runs', RUN, 'draft.jsonl');
  appendAttempt(path, line);
  appendAttempt(path, root);
  assert.equal(readFileSync(path, 'utf8'), `${canonical(line)}\n${canonical(root)}\n`);
});

test('reading draft.jsonl leaves out a last line another process is still writing', () => {
  const path = join(tmp('a11y-draftlog-'), 'draft.jsonl');
  assert.deepEqual(readAttempts(path), [], 'no file yet');
  const one = attemptLine({ state: 'r.new', result: 'accepted' });
  const two = attemptLine({ state: 'r.tried', result: 'review' });
  appendAttempt(path, one);
  appendFileSync(path, canonical(two).slice(0, 20));
  assert.deepEqual(readAttempts(path), [one], 'a torn last line');
  appendFileSync(path, canonical(two).slice(20));
  assert.deepEqual(readAttempts(path), [one, two], 'a whole last line without its newline yet');
  writeFileSync(path, `{"state":\n${canonical(one)}\n`);
  assert.throws(() => readAttempts(path), SyntaxError, 'a broken line before the last is an error');
});

// A child process, awaited without blocking this process.
function run(cmd, args, env = {}) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, ...env } });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => { stdout += d; });
    child.stderr.on('data', (d) => { stderr += d; });
    child.on('close', (status) => resolve({ status, stdout, stderr }));
  });
}

test('8 processes appending to one draft.jsonl at once lose and mangle no line', async () => {
  const path = join(tmp('a11y-draftlog-'), 'draft.jsonl');
  const writer = (n) => `import { appendAttempt, attemptLine } from ${JSON.stringify(join(A11Y, 'draft.mjs'))};
    for (let i = 0; i < 250; i++) appendAttempt(${JSON.stringify(path)}, attemptLine({ state: 'w${n}.' + i, parent: 'p', parentRecipe: '${'f'.repeat(64)}',
      label: 'Click a long enough label to make each line a few hundred bytes '.repeat(3), result: 'queued', reason: 'x'.repeat(200) }));`;
  const runs = await Promise.all([0, 1, 2, 3, 4, 5, 6, 7].map((n) => run('node', ['--input-type=module', '-e', writer(n)])));
  for (const r of runs) assert.equal(r.status, 0, r.stderr);
  const lines = readJsonl(path);
  assert.equal(lines.length, 2000);
  assert.equal(new Set(lines.map((l) => l.state)).size, 2000);
});

// ---------------------------------------------------------------------------
// The command line, on the fixture app's plan, with nothing to draft (so no browser starts)
// ---------------------------------------------------------------------------

function workspace() {
  const root = tmp('a11y-ws-');
  const mapPath = join(root, 'map.json');
  writeJson(mapPath, readJson(join(APP, 'map.json')));
  const r = spawnSync('python3', [join(A11Y, 'audit.py'), 'plan', '--workspace', root, '--map', mapPath, '--run', RUN], { encoding: 'utf8' });
  assert.equal(r.status, 0, r.stderr);
  cpSync(join(APP, 'recipes'), join(root, 'recipes'), { recursive: true });
  return { root, mapPath, ws: new Workspace({ workspace: root, map: mapPath }) };
}

async function draft(w, args, env = {}) {
  // An unused port: a draft that opened a page would fail.
  const r = await run('node', [join(A11Y, 'draft.mjs'), ...args, '--workspace', w.root, '--map', w.mapPath, '--base-url', 'http://127.0.0.1:9'], env);
  const lines = r.stdout.trim().split('\n');
  return { ...r, lines, json: JSON.parse(lines[lines.length - 1]) };
}

test('draft --ready skips a state already attempted against its parent recipe, and status counts attempts and ready states', async () => {
  const w = workspace();
  const states = w.ws.states(RUN);
  let s = await draft(w, ['status']);
  assert.equal(s.status, 0, s.stderr);
  assert.equal(s.json.attempts, 0);
  assert.equal(s.json.ready, 1, 'home.chart-tooltip, the one state without a recipe');

  const recipes = loadRecipes(w.ws);
  appendAttempt(w.ws.file('draft.jsonl', RUN), attemptLine({ state: 'home.chart-tooltip', parent: 'home.index',
    parentRecipe: parentRecipe(recipes, states['home.chart-tooltip']), label: 'Hover over the chart', result: 'queued', reason: 'cannot parse "Hover over the chart"' }));
  s = await draft(w, ['status']);
  assert.deepEqual([s.json.attempts, s.json.ready], [1, 0]);

  const r = await draft(w, ['--ready']);
  assert.equal(r.status, 0, r.stderr + r.stdout);
  assert.equal(r.lines[0], 'ready round 1: 0 selected; skipped 1 attempted against the same parent recipe, 0 with no accepted parent, 0 rejected');
  assert.deepEqual(r.json, { run: RUN, accepted: [], review: [], agentQueue: [],
    ready: { rounds: 1, selected: 0, skipped: { attempted: 1, noAcceptedParent: 0, rejected: 0 } } });
  assert.equal(readJsonl(w.ws.file('draft.jsonl', RUN)).length, 1, 'a pass that drafts nothing logs nothing');

  const sharded = await Promise.all([1, 2].map((i) => draft(w, ['--ready', '--shard', `${i}/2`])));
  for (const p of sharded) assert.equal(p.status, 0, p.stderr);
  assert.deepEqual(sharded.map((p) => p.json.ready.skipped.attempted + p.json.ready.otherShards).sort(), [1, 1]);
  assert.deepEqual(sharded.map((p) => p.json.ready.skipped.attempted).sort(), [0, 1]);

  // audit.py passes the options through.
  const via = spawnSync('python3', [join(A11Y, 'audit.py'), 'recipe', 'draft', '--ready', '--shard=1/1', '--workspace', w.root, '--map', w.mapPath,
    '--base-url', 'http://127.0.0.1:9'], { encoding: 'utf8' });
  assert.equal(via.status, 0, via.stderr);
  assert.equal(JSON.parse(via.stdout.trim().split('\n').pop()).ready.otherShards, 0);
});

test('a browser that fails to launch stops the pass with exit 1 and logs no attempt, so --ready tries the state again', async () => {
  const w = workspace();
  const area = join(w.root, 'recipes', 'transactions.json');
  const doc = readJson(area);
  delete doc['transactions.filters-menu'];
  writeJson(area, doc);
  const log = w.ws.file('draft.jsonl', RUN);
  const recipes = loadRecipes(w.ws);
  appendAttempt(log, attemptLine({ state: 'home.chart-tooltip', parent: 'home.index', parentRecipe: parentRecipe(recipes, w.ws.states(RUN)['home.chart-tooltip']),
    label: 'Hover over the chart', result: 'queued', reason: 'cannot parse "Hover over the chart"' }));
  const before = readFileSync(log, 'utf8');
  assert.equal((await draft(w, ['status'])).json.ready, 1, 'transactions.filters-menu');

  // Playwright finds no browser under a missing PLAYWRIGHT_BROWSERS_PATH, as after an upgrade.
  const broken = { PLAYWRIGHT_BROWSERS_PATH: join(tmp('a11y-nobrowser-'), 'missing') };
  for (const args of [['--ready'], ['transactions.filters-menu']]) {
    const r = await draft(w, args, broken);
    assert.equal(r.status, 1, `${args.join(' ')}: ${r.stdout}`);
    assert.match(r.json.error, /^could not launch the browser: /);
    assert.equal(readFileSync(log, 'utf8'), before, `${args.join(' ')} logs nothing`);
  }
  assert.equal((await draft(w, ['status'])).json.ready, 1, 'still ready');

  // An id that is not in states.json needs no browser, and is queued but not logged.
  const typo = await draft(w, ['transactions.filter-menu'], broken);
  assert.equal(typo.status, 0, typo.stderr);
  assert.deepEqual(typo.json.agentQueue, [{ state: 'transactions.filter-menu', label: null, reason: `not in run ${RUN}'s states.json` }]);
  assert.equal(readFileSync(log, 'utf8'), before);
});

test('draft refuses --shard without --ready, --ready with state ids, and a malformed shard', async () => {
  const w = workspace();
  for (const [args, message] of [
    [['--shard', '1/8'], /--shard splits a --ready pass/],
    [['--ready', 'home.chart-tooltip'], /--ready picks its states from the refresh queue/],
    [['--ready', '--shard', '9/8'], /--shard takes i\/n/],
  ]) {
    const r = await draft(w, args);
    assert.equal(r.status, 1, args.join(' '));
    assert.match(r.json.error, message);
  }
});
