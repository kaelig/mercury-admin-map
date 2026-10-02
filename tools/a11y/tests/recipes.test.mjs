// U4: the recipe interpreter, fingerprints, the mutation policy, `recipe check`/`queue`/`status`,
// `recipe draft` and `reach`, on the fixture app (tools/a11y/fixtures/app). Expected fingerprints are
// the hand-written ones in fixtures/app/recipes/*.json, read off the app's design.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { cpSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ROOT, readJson, writeJson } from '../lib/canon.mjs';
import { Workspace } from '../lib/cli.mjs';
import { cdpSession, launch, newContext, trackRequests } from '../lib/browser.mjs';
import { startServer } from '../fixtures/server.mjs';
import { chain, loadRecipes, replay, resolveLocator } from '../recipe.mjs';
import { captureFingerprint, matchFingerprint } from '../fingerprint.mjs';
import { parseLabel } from '../draft.mjs';

const A11Y = join(ROOT, 'tools', 'a11y');
const APP = join(A11Y, 'fixtures', 'app');
const RUN = '2026-10-02-1200';
const POLICY = readJson(join(ROOT, 'a11y', 'policy.json'));
const THRESHOLD = Number(spawnSync('python3', ['-c', 'import similarity; print(similarity.THRESHOLD)'], { cwd: A11Y, encoding: 'utf8' }).stdout);
const DESIGN = Object.assign({}, ...['home', 'cards', 'transactions', 'reports', 'books'].map((a) => readJson(join(APP, 'recipes', `${a}.json`))));

let browser, server;
const temps = [];
const servers = [];
const contexts = [];

before(async () => {
  browser = await launch();
  server = await startServer({ dirs: ['app'] });
});

after(async () => {
  await Promise.all(contexts.map((c) => c.close().catch(() => {})));
  await browser?.close();
  await Promise.all([server, ...servers].map((s) => s?.close()));
  for (const t of temps) rmSync(t, { recursive: true, force: true });
});

function tmp(prefix) {
  const d = mkdtempSync(join(tmpdir(), prefix));
  temps.push(d);
  return d;
}

// The fixture app with config.js overridden, on its own origin and the same paths.
async function variant(overrides) {
  const dir = join(tmp('a11y-app-variant-'), 'app');
  cpSync(APP, dir, { recursive: true });
  const config = { paidWord: 'Paid', daysAgo: 1, editLimitsOpens: 'limits', reportsRedirect: false, ...overrides };
  writeFileSync(join(dir, 'config.js'), `export default ${JSON.stringify(config)};\n`);
  const s = await startServer({ dirs: [dir] });
  servers.push(s);
  return s.origin;
}

// A temp workspace planned from the fixture map, with the fixture's recipes unless told otherwise.
function workspace({ recipes = true, map = null } = {}) {
  const root = tmp('a11y-ws-');
  const mapPath = join(root, 'map.json');
  writeJson(mapPath, map || readJson(join(APP, 'map.json')));
  const r = spawnSync('python3', [join(A11Y, 'audit.py'), 'plan', '--workspace', root, '--map', mapPath, '--run', RUN], { encoding: 'utf8' });
  assert.equal(r.status, 0, r.stderr);
  if (recipes) cpSync(join(APP, 'recipes'), join(root, 'recipes'), { recursive: true });
  return { root, mapPath, ws: new Workspace({ workspace: root, map: mapPath, baseUrl: server.origin }) };
}

function replan(w, runId, edit) {
  const map = readJson(w.mapPath);
  edit(map);
  writeJson(w.mapPath, map);
  const r = spawnSync('python3', [join(A11Y, 'audit.py'), 'plan', '--workspace', w.root, '--map', w.mapPath, '--run', runId], { encoding: 'utf8' });
  assert.equal(r.status, 0, r.stderr);
}

async function open() {
  const context = await newContext(browser);
  contexts.push(context);
  const page = await context.newPage();
  const tracker = trackRequests(page);
  const cdp = await cdpSession(context, page);
  return { context, page, tracker, cdp };
}

async function replayOn(origin, recipes, state, opts = {}) {
  const { page, tracker, cdp } = await open();
  const result = await replay(page, { cdp, tracker, baseUrl: origin }, state, { recipes, policy: POLICY, ...opts });
  return { result, page, cdp };
}

// A child process, awaited without blocking this process: the fixture servers live here, so a
// spawnSync'd child that loads one of their pages would deadlock.
function run(cmd, args, { ignore = false, timeout = 180000 } = {}) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { stdio: ignore ? 'ignore' : ['ignore', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout?.on('data', (d) => { stdout += d; });
    child.stderr?.on('data', (d) => { stderr += d; });
    const timer = setTimeout(() => child.kill('SIGKILL'), timeout);
    child.on('close', (status) => {
      clearTimeout(timer);
      resolve({ status, stdout, stderr });
    });
  });
}

// Run a Node stage module the way audit.py does; returns {status, json (last stdout line), stdout, stderr}.
async function stage(module, args, w, extra = []) {
  const r = await run('node', [join(A11Y, module), ...args, '--workspace', w.root, '--map', w.mapPath,
    '--base-url', w.ws.baseUrl, ...extra]);
  const lines = r.stdout.trim().split('\n');
  let json = null;
  try {
    json = JSON.parse(lines[lines.length - 1]);
  } catch { /* not JSON */ }
  return { ...r, json };
}

const recipesFile = (w, area) => readJson(join(w.root, 'recipes', `${area}.json`));

function setRecipe(w, area, id, entry) {
  const path = join(w.root, 'recipes', `${area}.json`);
  const doc = readJson(path, {});
  if (entry === null) delete doc[id];
  else doc[id] = entry;
  writeJson(path, doc);
}

// ---------------------------------------------------------------------------
// Chains, locators and fingerprints
// ---------------------------------------------------------------------------

test('chain lists the recipes from the root to the state', () => {
  const { ws } = workspace();
  const recipes = loadRecipes(ws);
  assert.deepEqual(chain(recipes, 'transactions.drawer').map((r) => r.id),
    ['transactions.index', 'transactions.filters-menu', 'transactions.card-payments', 'transactions.drawer']);
  assert.equal(chain(recipes, 'transactions.drawer')[0].parent, '/app/transactions');
  assert.throws(() => chain(recipes, 'home.chart-tooltip'), /no recipe for home\.chart-tooltip/);
});

test('a three-hop recipe reaches the drawer and its fingerprint matches the design', async () => {
  const { ws } = workspace();
  const recipes = loadRecipes(ws);
  const { result } = await replayOn(server.origin, recipes, 'transactions.drawer');
  assert.equal(result.status, 'reached', result.message);
  assert.deepEqual(result.fingerprint, DESIGN['transactions.drawer'].fingerprint);
  assert.equal(result.mutates, false);
});

test('fingerprints of a page, a menu, a modal and a toast match the design', async () => {
  const { ws } = workspace();
  const recipes = loadRecipes(ws);
  for (const id of ['home.index', 'home.move-money-menu', 'cards.edit-limit-modal', 'home.send-toast', 'transactions.card-payments']) {
    const { result } = await replayOn(server.origin, recipes, id);
    assert.equal(result.status, 'reached', `${id}: ${result.message}`);
    assert.deepEqual(result.fingerprint, DESIGN[id].fingerprint, id);
  }
});

test('captureFingerprint reads the open layer by kind; matchFingerprint applies KTD7', async () => {
  const { page, cdp } = await open();
  await page.goto(`${server.origin}/app/cards`);
  await page.getByRole('button', { name: 'Freeze card' }).click();
  const fp = await captureFingerprint(page, cdp, 'modal');
  assert.deepEqual(fp, DESIGN['cards.freeze-modal'].fingerprint);

  const base = DESIGN['cards.edit-limit-modal'].fingerprint; // controls: ATM limit, Cancel, Daily limit, Monthly limit, Save
  assert.equal(matchFingerprint(base, base).ok, true);
  assert.equal(matchFingerprint(base, { ...base, controls: ['ATM limit', 'Cancel', 'Daily limit'] }).ok, true, 'Jaccard 3/5 = 0.6 matches');
  const two = matchFingerprint(base, { ...base, controls: ['ATM limit', 'Cancel'] });
  assert.equal(two.ok, false, 'Jaccard 2/5 = 0.4 does not');
  assert.match(two.why, /controls/);
  assert.match(matchFingerprint(base, { ...base, path: '/app/dashboard' }).why, /path/);
  assert.match(matchFingerprint(base, { ...base, top: { role: 'dialog', name: 'Freeze card' } }).why, /top/);
  const dated = { ...base, top: { role: 'dialog', name: 'Paid Yesterday' } };
  assert.equal(matchFingerprint(dated, { ...dated, top: { role: 'dialog', name: 'Paid 2 days ago' } }).ok, true);
});

test('locators resolve in KTD6 order: role and name, label, text, then css with an index, within a scope', async () => {
  const { page } = await open();
  await page.goto(`${server.origin}/app/transactions`);
  const text = await resolveLocator(page, { role: 'button', name: 'No such button', text: 'Filters' });
  assert.equal(await text.textContent(), 'Filters');
  const css = await resolveLocator(page, { css: '[class*="_row_"]', index: 1 });
  assert.match(await css.textContent(), /^Delta Air Lines/);
  await css.click();
  const close = await resolveLocator(page, { role: 'button', name: 'Close', within: { css: 'aside' } });
  assert.equal(await close.textContent(), 'Close');
  await assert.rejects(resolveLocator(page, { role: 'button', name: 'Nothing like this' }), /no element matches/);
});

// ---------------------------------------------------------------------------
// Drift: AE2, relative dates, a different dialog, redirects, setup lost on reload
// ---------------------------------------------------------------------------

test('AE2: when the drawer trigger is renamed, the replay fails at the step that clicks it', async () => {
  const { ws } = workspace();
  const origin = await variant({ paidWord: 'Settled' });
  const { result } = await replayOn(origin, loadRecipes(ws), 'transactions.drawer');
  assert.equal(result.status, 'step-failed');
  assert.equal(result.step, 3, 'step 0 loads the root; 1 Filters, 2 Card payments, 3 the row');
  assert.match(result.message, /transactions\.drawer/);
});

test('a row recorded as "Paid Yesterday" is still found when it reads "Paid 2 days ago"', async () => {
  const { ws } = workspace();
  const origin = await variant({ daysAgo: 2 });
  const { result, page } = await replayOn(origin, loadRecipes(ws), 'transactions.drawer');
  assert.equal(result.status, 'reached', result.message);
  const raw = await resolveLocator(page, { role: 'button', name: 'Delta Air Lines Paid 3 days ago −$412.20' });
  assert.match(await raw.textContent(), /^Delta Air Lines/);
});

test('the same click opening a different dialog is a fingerprint mismatch', async () => {
  const { ws } = workspace();
  const origin = await variant({ editLimitsOpens: 'freeze' });
  const { result } = await replayOn(origin, loadRecipes(ws), 'cards.edit-limit-modal');
  assert.equal(result.status, 'fingerprint-mismatch');
  assert.match(result.message, /top/);
  assert.deepEqual(result.fingerprint.top, { role: 'dialog', name: 'Freeze card' });
});

test('a root page that now redirects to the dashboard is a fingerprint mismatch', async () => {
  const { ws } = workspace();
  const origin = await variant({ reportsRedirect: true });
  const { result } = await replayOn(origin, loadRecipes(ws), 'reports.index');
  assert.equal(result.status, 'fingerprint-mismatch');
  assert.equal(result.fingerprint.path, '/app/dashboard');
});

test('R5: a page behind a setup step lost on reload is reached through its parent; a direct load fails', async () => {
  const { ws } = workspace();
  const recipes = loadRecipes(ws);
  const chained = await replayOn(server.origin, recipes, 'books.index');
  assert.equal(chained.result.status, 'reached', chained.result.message);
  const direct = { ...recipes, 'books.index': { ...recipes['books.index'], parent: '/app/books', hop: [] } };
  const { result } = await replayOn(server.origin, direct, 'books.index');
  assert.equal(result.status, 'fingerprint-mismatch');
  assert.equal(result.fingerprint.path, '/app/dashboard');
});

test('untilParent stops at the parent state and leaves the final hop to the caller', async () => {
  const { ws } = workspace();
  let armed = false;
  const { result, page } = await replayOn(server.origin, loadRecipes(ws), 'cards.edit-limit-modal',
    { untilParent: true, beforeFinalHop: () => { armed = true; } });
  assert.equal(result.status, 'reached', result.message);
  assert.deepEqual(result.fingerprint, DESIGN['cards.index'].fingerprint);
  assert.equal(await page.getByRole('dialog').count(), 0);
  assert.equal(armed, false);
});

test('beforeFinalHop runs on the parent state, before the final hop', async () => {
  const { ws } = workspace();
  let dialogsBefore = null;
  const { result } = await replayOn(server.origin, loadRecipes(ws), 'cards.edit-limit-modal',
    { beforeFinalHop: async (p) => { dialogsBefore = await p.getByRole('dialog').count(); } });
  assert.equal(result.status, 'reached', result.message);
  assert.equal(dialogsBefore, 0);
});

// ---------------------------------------------------------------------------
// Mutation policy (KTD9)
// ---------------------------------------------------------------------------

function tampered(recipes, id, patch) {
  return { ...recipes, [id]: { ...recipes[id], ...patch } };
}

test('each name on the policy denylist is refused before anything runs', async () => {
  const { ws } = workspace();
  const recipes = loadRecipes(ws);
  assert.ok(POLICY.denylist.names.length >= 3);
  for (const name of POLICY.denylist.names) {
    for (const locator of [{ role: 'button', name }, { text: name.toUpperCase() }, { label: name }]) {
      const { result, page } = await replayOn(server.origin, tampered(recipes, 'home.move-money-menu', { hop: [{ click: locator }] }), 'home.move-money-menu');
      assert.equal(result.status, 'step-failed', `${JSON.stringify(locator)} was not refused`);
      assert.equal(result.step, 1);
      assert.match(result.message, /denylist/);
      assert.equal(page.url(), 'about:blank', 'nothing ran');
    }
  }
});

test('a css locator that lands on Logout is refused before the click', async () => {
  const { ws } = workspace();
  const recipes = tampered(loadRecipes(ws), 'home.move-money-menu', { hop: [{ click: { css: '#logout' } }] });
  const { result, page } = await replayOn(server.origin, recipes, 'home.move-money-menu');
  assert.equal(result.status, 'step-failed');
  assert.match(result.message, /denylist/);
  assert.equal(new URL(page.url()).pathname, '/app/dashboard');
  assert.equal(await page.evaluate(() => localStorage.getItem('loggedOut')), null);
});

test('an off-domain link is refused before it runs', async () => {
  const { ws } = workspace();
  const recipes = tampered(loadRecipes(ws), 'home.move-money-menu', { hop: [{ click: { role: 'link', name: 'Help center' } }] });
  const { page, tracker, cdp } = await open();
  const offsite = [];
  page.on('request', (r) => { if (r.url().startsWith(server.origin2)) offsite.push(r.url()); });
  const result = await replay(page, { cdp, tracker, baseUrl: server.origin }, 'home.move-money-menu', { recipes, policy: POLICY });
  assert.equal(result.status, 'step-failed');
  assert.equal(result.step, 1);
  assert.match(result.message, /allowed origins/);
  assert.deepEqual(offsite, []);
  assert.equal(new URL(page.url()).pathname, '/app/dashboard');
});

test('a goto off the allowed origins, an unknown action and an unlisted typed value are refused', async () => {
  const { ws } = workspace();
  const recipes = loadRecipes(ws);
  const cases = [
    ['home.move-money-menu', { hop: [{ goto: 'https://example.com/' }] }, /allowed origins/],
    ['home.move-money-menu', { hop: [{ drag: { css: '.chart' } }] }, /unknown action/],
    ['home.send-toast', { hop: [{ fill: { locator: { label: 'Recipient email' }, value: 'someone@gmail.com' } }] }, /fixtureValues/],
  ];
  for (const [id, patch, why] of cases) {
    const { result, page } = await replayOn(server.origin, tampered(recipes, id, patch), id);
    assert.equal(result.status, 'step-failed', JSON.stringify(patch));
    assert.match(result.message, why);
    assert.equal(page.url(), 'about:blank');
  }
});

test('an action on no recorded edge is refused before it runs', async () => {
  const { ws } = workspace();
  const recipes = tampered(loadRecipes(ws), 'cards.freeze-modal', { parent: 'home.index' });
  const { result, page } = await replayOn(server.origin, recipes, 'cards.freeze-modal');
  assert.equal(result.status, 'step-failed');
  assert.equal(result.step, 1);
  assert.match(result.message, /recorded edge/);
  assert.equal(page.url(), 'about:blank');
  const rootHop = tampered(loadRecipes(ws), 'cards.index', { hop: [{ click: { role: 'button', name: 'Edit limits' } }] });
  assert.match((await replayOn(server.origin, rootHop, 'cards.index')).result.message, /recorded edge/);
});

test('a path that submits a form is observed as mutating; one that only opens a dialog is not', async () => {
  const { ws } = workspace();
  const recipes = loadRecipes(ws);
  assert.equal((await replayOn(server.origin, recipes, 'home.send-toast')).result.mutates, true);
  assert.equal((await replayOn(server.origin, recipes, 'home.send-modal')).result.mutates, false);
});

// ---------------------------------------------------------------------------
// recipe check, queue and status
// ---------------------------------------------------------------------------

test('recipe check replays twice, scores the screenshot and accepts the drawer recipe', async () => {
  const w = workspace();
  const r = await stage('recipe.mjs', ['check', 'transactions.drawer'], w);
  assert.equal(r.status, 0, r.stderr + r.stdout);
  const entry = recipesFile(w, 'transactions')['transactions.drawer'];
  assert.equal(entry.status, 'accepted');
  assert.ok(entry.similarity >= THRESHOLD, `similarity ${entry.similarity}`);
  assert.deepEqual(entry.fingerprint, DESIGN['transactions.drawer'].fingerprint);
  assert.equal(entry.mutates, false);
  assert.equal(entry.source, DESIGN['transactions.drawer'].source);
  assert.equal(entry.checked, RUN);
  assert.equal(entry.recordedBy, 'hand');
  const [checked] = r.json.checked;
  assert.equal(checked.status, 'accepted');
  assert.ok(existsSync(checked.screenshot));
});

test('a recipe whose path submits a form is written with mutates: true', async () => {
  const w = workspace();
  setRecipe(w, 'home', 'home.send-toast', { ...DESIGN['home.send-toast'], mutates: false, fingerprint: null, status: 'draft' });
  const r = await stage('recipe.mjs', ['check', 'home.send-toast'], w);
  assert.equal(r.status, 0, r.stderr + r.stdout);
  const entry = recipesFile(w, 'home')['home.send-toast'];
  assert.equal(entry.mutates, true);
  assert.equal(entry.status, 'accepted');
});

test('a click that lands on the neighboring dialog scores below the threshold and goes to review', async () => {
  const w = workspace();
  const agent = { parent: 'cards.index', hop: [{ click: { role: 'button', name: 'Freeze card' } }], recordedBy: 'agent' };
  const r = await stage('recipe.mjs', ['check', 'cards.edit-limit-modal', '--recipe', JSON.stringify(agent)], w);
  assert.equal(r.status, 0, r.stderr + r.stdout);
  const entry = recipesFile(w, 'cards')['cards.edit-limit-modal'];
  assert.equal(entry.status, 'review');
  assert.ok(entry.similarity < THRESHOLD, `similarity ${entry.similarity}`);
  assert.equal(entry.review.reason, 'below-threshold');
  assert.deepEqual(entry.hop, agent.hop);
  assert.equal(entry.recordedBy, 'agent');
});

test('a recipe that does not reach its state is reported and leaves the existing entry alone', async () => {
  const w = workspace();
  const before = recipesFile(w, 'cards')['cards.edit-limit-modal'];
  const broken = { parent: 'cards.index', hop: [{ click: { role: 'button', name: 'Edit limit' } }] };
  const r = await stage('recipe.mjs', ['check', 'cards.edit-limit-modal', '--recipe', JSON.stringify(broken)], w);
  assert.equal(r.status, 1);
  assert.equal(r.json.checked[0].status, 'step-failed');
  assert.deepEqual(recipesFile(w, 'cards')['cards.edit-limit-modal'], before);
});

test('a menu recipe that never opens the menu is not accepted', async () => {
  const w = workspace();
  const noop = { parent: 'transactions.index', hop: [{ click: { role: 'heading', name: 'Transactions' } }] };
  const r = await stage('recipe.mjs', ['check', 'transactions.filters-menu', '--recipe', JSON.stringify(noop)], w);
  assert.equal(r.status, 1);
  assert.equal(r.json.checked[0].status, 'structure-missing');
  assert.equal(recipesFile(w, 'transactions')['transactions.filters-menu'].status, 'accepted', 'the existing recipe stays');
});

test('the refresh queue: no-recipe, stale after a label change, and unreachable in the published run', async () => {
  const w = workspace();
  let r = await stage('recipe.mjs', ['queue'], w);
  assert.equal(r.status, 0, r.stderr);
  assert.deepEqual(r.json.queue.map((q) => [q.state, q.reason]), [['home.chart-tooltip', 'no-recipe']]);

  writeJson(join(w.root, 'published', 'meta.json'), { run: '2026-10-01-0900' });
  writeJson(join(w.root, 'runs', '2026-10-01-0900', 'outcomes.json'), { run: '2026-10-01-0900', states: {
    'cards.freeze-modal': { outcome: 'unreachable', reason: 'step-failed', detail: { step: 1 } },
    'cards.index': { outcome: 'audited', reason: null },
  } });
  replan(w, '2026-10-02-1300', (map) => {
    map.areas.find((a) => a.area === 'transactions').edges.find((e) => e.label === 'Click Filters').label = 'Click Filter options';
  });
  r = await stage('recipe.mjs', ['queue'], w);
  const queue = Object.fromEntries(r.json.queue.map((q) => [q.state, q.reason]));
  assert.deepEqual(queue, {
    'cards.freeze-modal': 'unreachable',
    'home.chart-tooltip': 'no-recipe',
    'transactions.card-payments': 'stale',
    'transactions.drawer': 'stale',
    'transactions.filters-menu': 'stale',
  });

  r = await stage('recipe.mjs', ['check', 'cards.freeze-modal'], w);
  assert.equal(r.status, 0, r.stderr + r.stdout);
  r = await stage('recipe.mjs', ['queue'], w);
  assert.ok(!r.json.queue.some((q) => q.state === 'cards.freeze-modal'), 'checked in this run, so no longer queued');

  r = await stage('recipe.mjs', ['status'], w);
  assert.equal(r.status, 0);
  assert.equal(r.json.queue, 4);
  assert.equal(r.json.accepted, 13);
});

test('parallel recipe writers to one area file never drop each other\'s entries', async () => {
  const w = workspace({ recipes: false });
  const writer = (n) => `import { Workspace } from ${JSON.stringify(join(A11Y, 'lib', 'cli.mjs'))};
    import { writeRecipe } from ${JSON.stringify(join(A11Y, 'recipe.mjs'))};
    const ws = new Workspace({ workspace: ${JSON.stringify(w.root)} });
    for (let i = 0; i < 25; i++) writeRecipe(ws, 'cards', 'cards.w${n}-' + i, { parent: '/app/cards', hop: [], status: 'draft' });`;
  const runs = await Promise.all([0, 1, 2, 3, 4, 5].map((n) => run('node', ['--input-type=module', '-e', writer(n)])));
  for (const r of runs) assert.equal(r.status, 0, r.stderr);
  assert.equal(Object.keys(recipesFile(w, 'cards')).length, 150);
  assert.equal(existsSync(join(w.root, 'recipes', 'cards.json.lock')), false);
});

test('recipe, draft and reach status are pending on a run without states.json', () => {
  const root = tmp('a11y-empty-');
  for (const extra of [[], ['--run', RUN]]) {
    for (const module of ['recipe.mjs', 'draft.mjs', 'reach.mjs']) {
      const r = spawnSync('node', [join(A11Y, module), 'status', '--workspace', root, '--map', join(APP, 'map.json'), ...extra], { encoding: 'utf8' });
      assert.equal(r.status, 0, r.stderr);
      assert.deepEqual(JSON.parse(r.stdout.trim().split('\n').pop()), { pending: true }, `${module} ${extra.join(' ')}`);
    }
  }
});

// ---------------------------------------------------------------------------
// recipe draft
// ---------------------------------------------------------------------------

test('labels parse into steps; hover and prose labels do not', () => {
  assert.deepEqual(parseLabel('Click Edit limits', POLICY), { steps: [{ click: { name: 'Edit limits' } }] });
  assert.deepEqual(parseLabel('Open the Move money menu', POLICY), { steps: [{ click: { name: 'Move money', role: 'button' } }] });
  assert.deepEqual(parseLabel('Choose Card payments', POLICY), { steps: [{ click: { name: 'Card payments' } }] });
  assert.deepEqual(parseLabel("Tick 'Remember me'", POLICY), { steps: [{ check: { name: 'Remember me' } }] });
  assert.deepEqual(parseLabel('Toggle Books', POLICY), { steps: [{ click: { name: 'Books' } }] });
  assert.deepEqual(parseLabel('Press Enter', POLICY), { steps: [{ press: 'Enter' }] });
  assert.deepEqual(parseLabel('Type a11y-audit@example.invalid in Recipient email, type 1.00 in Amount, click Send', POLICY), { steps: [
    { fill: { locator: { name: 'Recipient email' }, value: 'a11y-audit@example.invalid' } },
    { fill: { locator: { name: 'Amount' }, value: '1.00' } },
    { click: { name: 'Send' } },
  ] });
  assert.match(parseLabel('Hover over the chart', POLICY).error, /cannot parse/);
  assert.match(parseLabel('Type jane@realcompany.com in Email', POLICY).error, /fixtureValues/);
});

test('draft turns "Click Edit limits" into a role-and-name click, accepts it, and queues "Hover over the chart"', async () => {
  const w = workspace();
  setRecipe(w, 'cards', 'cards.edit-limit-modal', null);
  const r = await stage('draft.mjs', ['cards.edit-limit-modal', 'home.chart-tooltip'], w);
  assert.equal(r.status, 0, r.stderr + r.stdout);
  const entry = recipesFile(w, 'cards')['cards.edit-limit-modal'];
  assert.deepEqual(entry.hop, [{ click: { role: 'button', name: 'Edit limits' } }]);
  assert.equal(entry.parent, 'cards.index');
  assert.equal(entry.status, 'accepted');
  assert.equal(entry.recordedBy, 'draft');
  assert.ok(entry.similarity >= THRESHOLD);
  assert.deepEqual(r.json.accepted, ['cards.edit-limit-modal']);
  assert.deepEqual(r.json.agentQueue.map((q) => [q.state, q.label]), [['home.chart-tooltip', 'Hover over the chart']]);
  assert.equal(recipesFile(w, 'home')['home.chart-tooltip'], undefined);
  const s = await stage('draft.mjs', ['status'], w);
  assert.equal(s.status, 0);
  assert.equal(s.json.accepted, 1);
});

test('draft resolves a multi-step label and a row named by its merchant', async () => {
  const w = workspace();
  setRecipe(w, 'home', 'home.send-toast', null);
  setRecipe(w, 'transactions', 'transactions.drawer', null);
  const r = await stage('draft.mjs', [], w);
  assert.equal(r.status, 0, r.stderr + r.stdout);
  assert.deepEqual(recipesFile(w, 'home')['home.send-toast'].hop, DESIGN['home.send-toast'].hop);
  assert.deepEqual(recipesFile(w, 'transactions')['transactions.drawer'].hop, DESIGN['transactions.drawer'].hop);
  assert.deepEqual([...r.json.accepted].sort(), ['home.send-toast', 'transactions.drawer']);
  assert.deepEqual(r.json.agentQueue.map((q) => q.state), ['home.chart-tooltip']);
});

// ---------------------------------------------------------------------------
// reach
// ---------------------------------------------------------------------------

function agentBrowser() {
  return spawnSync('agent-browser', ['--version'], { encoding: 'utf8' }).status === 0;
}

test('reach hands an agent-browser session the drawer under the sweep environment, from cleared storage', async (t) => {
  if (!agentBrowser()) {
    t.skip('agent-browser is not on PATH: install agent-browser 0.26 to run the reach test');
    return;
  }
  const session = `a11y-u4-test-${process.pid}`;
  const ab = async (...args) => {
    const r = await run('agent-browser', ['--session', session, ...args], { timeout: 60000 });
    assert.equal(r.status, 0, `agent-browser ${args.join(' ')}: ${r.stderr}${r.stdout}`);
    return r.stdout.trim();
  };
  try {
    const w = workspace();
    // The first command starts the session's daemon, which would hold a piped stdout open: ignore it.
    assert.equal((await run('agent-browser', ['--session', session, 'open', `${server.origin}/app/cards`], { ignore: true, timeout: 60000 })).status, 0);
    await ab('storage', 'local', 'set', 'leftover', 'yes');
    await ab('storage', 'session', 'set', 'leftoverSession', 'yes');
    await ab('set', 'media', 'dark');
    const r = await stage('reach.mjs', ['transactions.drawer', '--session', session], w);
    assert.equal(r.status, 0, r.stderr + r.stdout);
    assert.equal(r.json.status, 'reached', r.json.message);
    const fp = DESIGN['transactions.drawer'].fingerprint;
    assert.equal(new URL(await ab('get', 'url')).pathname, fp.path);
    const seen = JSON.parse(await ab('eval', `({
      top: (() => { const a = [...document.querySelectorAll('aside')].find((e) => e.checkVisibility()); return a ? a.querySelector('h2').textContent : null; })(),
      size: [innerWidth, innerHeight, devicePixelRatio],
      light: matchMedia('(prefers-color-scheme: light)').matches,
      reduce: matchMedia('(prefers-reduced-motion: reduce)').matches,
      local: localStorage.getItem('leftover'), session: sessionStorage.getItem('leftoverSession') })`));
    assert.deepEqual(seen, { top: fp.headings[0], size: [1440, 900, 1], light: true, reduce: true, local: null, session: null });
    const s = await stage('reach.mjs', ['status'], w);
    assert.deepEqual(s.json, {});
  } finally {
    await run('agent-browser', ['--session', session, 'close'], { ignore: true, timeout: 30000 });
  }
});
