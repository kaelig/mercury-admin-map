// Replay recipes (KTD6–KTD9): the interpreter the sweep, `reach` and `probe` share, and the `recipe`
// stage that checks recipes and lists the refresh queue.
//
//   node tools/a11y/recipe.mjs check <state>... [--recipe '<json>']   replay twice, fingerprint, score
//   node tools/a11y/recipe.mjs queue                                   stale, unreachable, missing recipes
//   node tools/a11y/recipe.mjs status                                  counts by status and the queue size
//
// A replay loads the root's URL (step 0), then runs every hop in chain order (steps 1, 2, …). Before
// anything runs it refuses a step the policy forbids: a denylisted name, a goto off the allowed
// origins (policy.allowedOrigins plus the --base-url origin), a typed value that is not one of
// policy.fixtureValues, an unknown action, or a hop on a parent → state pair the map has no edge for.
// Before each action it refuses an element whose own name is denylisted or a link off the allowed
// origins. A refusal is `step-failed` naming the step. File formats: schemas.md.
import { spawnSync } from 'node:child_process';
import { closeSync, existsSync, mkdirSync, openSync, readdirSync, statSync, unlinkSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';
import { ROOT, nameTemplate, nameTemplateRegex, readJson, sha1, writeJson } from './lib/canon.mjs';
import { cdpSession, injectMotionOff, launch, newContext, parkPointer, settle, trackRequests } from './lib/browser.mjs';
import { armLayer } from './lib/scope.mjs';
import { parse } from './lib/cli.mjs';
import { captureFingerprint, matchFingerprint } from './fingerprint.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const SIMILARITY = join(HERE, 'similarity.py');

// What loadRecipes() learns from the map: edges ("from\nto" -> labels) and nodes (id -> {area, kind, image}).
// A symbol key: spreads keep it, JSON and canonical() ignore it.
export const META = Symbol.for('a11y.recipes.meta');

export const ACTIONS = new Set(['goto', 'click', 'hover', 'check', 'scroll', 'press', 'fill', 'select', 'wait']);
// Kinds whose state is an open layer: a replay that finds none did not reach the state (KTD7).
export const NEEDS_LAYER = new Set(['modal', 'drawer', 'menu', 'popover', 'toast']);
// Unreachable reasons that mean the recipe itself broke (KTD8 refresh).
const RECIPE_REASONS = new Set(['step-failed', 'fingerprint-mismatch']);

const RESOLVE_TIMEOUT = 3000;
const ACTION_TIMEOUT = 5000;
const NAV_TIMEOUT = 30000;

const edgeKey = (from, to) => `${from}\n${to}`;

// ---------------------------------------------------------------------------
// Recipes and chains
// ---------------------------------------------------------------------------

export function mapMeta(map) {
  const edges = new Map();
  const nodes = {};
  for (const a of map?.areas || []) {
    for (const n of a.nodes || []) nodes[n.id] = { area: a.area, kind: n.kind, url: n.url, image: n.image || null };
    for (const e of a.edges || []) {
      const k = edgeKey(e.from, e.to);
      if (!edges.has(k)) edges.set(k, []);
      edges.get(k).push(e.label || '');
    }
  }
  return { edges, nodes };
}

// Every recipe in <workspace>/recipes/*.json, merged, with the map's edges and node kinds under META.
export function loadRecipes(ws, { map } = {}) {
  const recipes = {};
  if (existsSync(ws.recipesDir)) {
    for (const f of readdirSync(ws.recipesDir).filter((f) => f.endsWith('.json')).sort()) {
      Object.assign(recipes, readJson(join(ws.recipesDir, f)));
    }
  }
  recipes[META] = mapMeta(map || (existsSync(ws.mapPath) ? ws.loadMap() : null));
  return recipes;
}

// The recipes from the root to `stateId`, root first, each with its `id`. Throws when one is missing.
export function chain(recipes, stateId) {
  const out = [];
  const seen = new Set();
  let id = stateId;
  for (;;) {
    const r = recipes[id];
    if (!r) throw new Error(`no recipe for ${id}${id === stateId ? '' : ` (on the chain to ${stateId})`}`);
    if (seen.has(id)) throw new Error(`the recipe chain to ${stateId} loops at ${id}`);
    seen.add(id);
    out.unshift({ id, ...r });
    if (typeof r.parent !== 'string' || !r.parent) throw new Error(`recipe ${id} has no parent`);
    if (r.parent.startsWith('/')) return out;
    id = r.parent;
  }
}

// ---------------------------------------------------------------------------
// Locators (KTD6)
// ---------------------------------------------------------------------------

const templateRx = (s) => nameTemplateRegex(nameTemplate(s));

function candidates(scope, l) {
  const out = [];
  if (l.role) out.push(l.name ? scope.getByRole(l.role, { name: templateRx(l.name) }) : scope.getByRole(l.role));
  if (l.label) out.push(scope.getByLabel(templateRx(l.label)).filter({ visible: true }));
  if (l.text) out.push(scope.getByText(templateRx(l.text)).filter({ visible: true }));
  if (l.css) out.push(scope.locator(l.css).filter({ visible: true }));
  return out;
}

export function describeLocator(l) {
  return JSON.stringify(Object.fromEntries(Object.entries(l || {}).filter(([, v]) => v !== null && v !== undefined)));
}

// A Playwright Locator for a recipe locator: role and name, then label, then text, then css, the
// first that matches more than `index` elements, inside `within` when given. Names are matched as
// name templates. Waits up to `timeout` ms for a match, then throws.
export async function resolveLocator(page, locator, { timeout = RESOLVE_TIMEOUT } = {}) {
  if (!locator || typeof locator !== 'object') throw new Error(`not a locator: ${JSON.stringify(locator)}`);
  const scope = locator.within ? await resolveLocator(page, locator.within, { timeout }) : page;
  const tries = candidates(scope, locator);
  if (!tries.length) throw new Error(`locator has no role, label, text or css: ${describeLocator(locator)}`);
  const index = locator.index ?? 0;
  const t0 = Date.now();
  for (;;) {
    for (const loc of tries) if ((await loc.count()) > index) return loc.nth(index);
    if (Date.now() - t0 >= timeout) break;
    await sleep(100);
  }
  throw new Error(`no element matches ${describeLocator(locator)}`);
}

// ---------------------------------------------------------------------------
// Policy (KTD9)
// ---------------------------------------------------------------------------

export function allowedOrigins(policy, baseUrl) {
  const out = new Set(policy.allowedOrigins || []);
  if (baseUrl) out.add(new URL(baseUrl).origin);
  return out;
}

const denied = (policy) => new Set((policy.denylist?.names || []).map((n) => nameTemplate(n).toLowerCase()));
const isDenied = (deny, s) => typeof s === 'string' && deny.has(nameTemplate(s).toLowerCase());
const fixtureValues = (policy) => new Set(Object.values(policy.fixtureValues || {}).map(String));

function locatorsIn(action, value) {
  const top = ['click', 'hover', 'check'].includes(action) ? value
    : ['fill', 'select'].includes(action) ? value?.locator
      : ['scroll', 'wait'].includes(action) && value && !('y' in value) && !('ms' in value) ? (value.locator || value)
        : null;
  const out = [];
  for (let l = top; l && typeof l === 'object'; l = l.within) out.push(l);
  return out;
}

// Why a step may not run, judged from the recipe alone, or null.
export function stepRefusal(step, policy, baseUrl) {
  const keys = step && typeof step === 'object' ? Object.keys(step) : [];
  if (keys.length !== 1 || !ACTIONS.has(keys[0])) return `unknown action ${JSON.stringify(step)}`;
  const [action] = keys;
  const value = step[action];
  const deny = denied(policy);
  for (const l of locatorsIn(action, value)) {
    for (const field of ['name', 'label', 'text']) {
      if (isDenied(deny, l[field])) return `"${l[field]}" is on the policy denylist`;
    }
  }
  if (action === 'goto') {
    if (typeof value !== 'string') return `malformed goto ${JSON.stringify(value)}`;
    const url = new URL(value, baseUrl);
    if (!allowedOrigins(policy, baseUrl).has(url.origin)) return `goto ${url.href} leaves the allowed origins`;
  }
  if (action === 'press' && typeof value !== 'string') return `malformed press ${JSON.stringify(value)}`;
  if (['click', 'hover', 'check'].includes(action) && (!value || typeof value !== 'object')) return `malformed ${action}`;
  if (action === 'fill') {
    if (!value?.locator || typeof value.value !== 'string') return 'malformed fill: it needs a locator and a value';
    if (!fixtureValues(policy).has(value.value)) return `typed value "${value.value}" is not one of policy.fixtureValues`;
  }
  if (action === 'select' && (!value?.locator || typeof value.option !== 'string')) return 'malformed select: it needs a locator and an option';
  if (action === 'wait' && !(value && (Number.isFinite(value.ms) || value.locator))) return 'malformed wait: it needs ms or a locator';
  if (action === 'scroll' && !(value && (Number.isFinite(value.y) || locatorsIn(action, value).length))) return 'malformed scroll';
  return null;
}

// Why the resolved element may not be acted on, or null: its own name is denylisted (a css locator
// can land on Logout), or it is a link that leaves the allowed origins.
async function elementRefusal(loc, policy, baseUrl) {
  const info = await loc.evaluate((el) => {
    const a = el.closest('a[href]');
    const name = el.getAttribute('aria-label') || el.innerText || el.value || el.title || '';
    return { name: String(name).replace(/\s+/g, ' ').trim(), href: a ? a.href : null };
  });
  if (isDenied(denied(policy), info.name)) return `the element's name "${info.name}" is on the policy denylist`;
  if (info.href) {
    const u = new URL(info.href);
    if (['http:', 'https:'].includes(u.protocol) && !allowedOrigins(policy, baseUrl).has(u.origin)) {
      return `the link leads to ${u.origin}, off the allowed origins`;
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Running steps
// ---------------------------------------------------------------------------

// Counts submit events in the page, so a hop that submits a form marks the recipe as mutating.
const SUBMITS = `(() => { const k = Symbol.for('a11y.submits');
  if (!(k in window)) { window[k] = 0; window.addEventListener('submit', () => { window[k] += 1; }, true); }
  return window[k]; })()`;

async function act(page, action, value, policy, baseUrl) {
  if (action === 'goto') {
    const response = await page.goto(new URL(value, baseUrl).href, { waitUntil: 'load', timeout: NAV_TIMEOUT });
    if (response && response.status() >= 400) throw new Error(`loading ${value} answered HTTP ${response.status()}`);
    return;
  }
  if (action === 'press') return page.keyboard.press(value);
  if (action === 'wait') {
    if (Number.isFinite(value.ms)) return sleep(Math.min(value.ms, 10000));
    return resolveLocator(page, value.locator, { timeout: ACTION_TIMEOUT });
  }
  if (action === 'scroll' && Number.isFinite(value.y)) return page.evaluate((y) => window.scrollBy(0, y), value.y);
  const locator = ['fill', 'select'].includes(action) ? value.locator : action === 'scroll' ? (value.locator || value) : value;
  const loc = await resolveLocator(page, locator);
  const why = await elementRefusal(loc, policy, baseUrl);
  if (why) {
    const e = new Error(why);
    e.refused = true;
    throw e;
  }
  if (action === 'click') return loc.click({ timeout: ACTION_TIMEOUT });
  if (action === 'hover') return loc.hover({ timeout: ACTION_TIMEOUT });
  if (action === 'check') return loc.check({ timeout: ACTION_TIMEOUT });
  if (action === 'fill') return loc.fill(value.value, { timeout: ACTION_TIMEOUT });
  if (action === 'select') return loc.selectOption(value.option, { timeout: ACTION_TIMEOUT });
  if (action === 'scroll') return loc.scrollIntoViewIfNeeded({ timeout: ACTION_TIMEOUT });
  throw new Error(`unknown action ${action}`);
}

function offOrigin(page, policy, baseUrl) {
  const url = page.url();
  if (!/^https?:/.test(url)) return null;
  const origin = new URL(url).origin;
  return allowedOrigins(policy, baseUrl).has(origin) ? null : `the page navigated to ${origin}, off the allowed origins`;
}

// Run one step on the live page: policy checks, the action, then settle. Returns
// {ok: true, submitted} or {ok: false, message}. `ctx` is {cdp, tracker, baseUrl}.
export async function runStep(page, ctx, step, policy) {
  const refusal = stepRefusal(step, policy, ctx.baseUrl);
  if (refusal) return { ok: false, message: refusal };
  const [action] = Object.keys(step);
  let before = null;
  if (action !== 'goto') before = await page.evaluate(SUBMITS).catch(() => null);
  try {
    await act(page, action, step[action], policy, ctx.baseUrl);
  } catch (e) {
    if (page.isClosed()) throw e;
    const what = typeof step[action] === 'string' ? step[action] : describeLocator(step[action]?.locator || step[action]);
    return { ok: false, message: e.refused ? e.message : `${action} ${what}: ${e.message.split('\n')[0]}` };
  }
  await settle(page, ctx.cdp, { tracker: ctx.tracker });
  const off = offOrigin(page, policy, ctx.baseUrl);
  if (off) return { ok: false, message: off };
  const after = await page.evaluate(SUBMITS).catch(() => null);
  return { ok: true, submitted: before !== null && after !== null && after > before };
}

// ---------------------------------------------------------------------------
// Replay
// ---------------------------------------------------------------------------

// Replay `stateId` into `page` (KTD6, KTD7). ctx: {cdp, tracker (armed right after newPage()), baseUrl}.
// The pointer is parked after the root load and again after a final hop that does not end in a hover.
// Options: recipes (from loadRecipes), policy, beforeFinalHop(page), untilParent, and `kind` and
// `edges` ([[from, to], ...]) to override what loadRecipes() read from the map.
// Returns {status: 'reached' | 'step-failed' | 'fingerprint-mismatch', step, message, fingerprint, mutates}:
// `step` is the failing step (0 is the root load, then hop steps in chain order), `fingerprint` what
// the replay captured, `mutates` whether a hop submitted a form.
export async function replay(page, ctx, stateId, { recipes, policy, beforeFinalHop = null, untilParent = false, kind = null, edges = null } = {}) {
  if (!policy) throw new Error('replay() needs the policy (ws.policy())');
  const meta = recipes[META] || { edges: new Map(), nodes: {} };
  const edgeSet = edges ? new Set(edges.map(([f, t]) => edgeKey(f, t))) : new Set(meta.edges.keys());
  const full = chain(recipes, stateId);
  const run = untilParent ? full.slice(0, -1) : full;
  if (!run.length) throw new Error(`${stateId} is a root: there is no parent state to stop at`);
  const target = run[run.length - 1];
  const targetKind = (untilParent ? null : kind) || meta.nodes[target.id]?.kind || 'page';

  const steps = [{ id: run[0].id, step: { goto: run[0].parent } }];
  for (const r of run) for (const step of r.hop || []) steps.push({ id: r.id, step });
  let mutates = false;
  const failed = (step, message) => ({ status: 'step-failed', step, message, fingerprint: null, mutates });

  // Refuse before anything runs.
  for (let i = 0; i < steps.length; i++) {
    const why = stepRefusal(steps[i].step, policy, ctx.baseUrl);
    if (why) return failed(i, `${steps[i].id}: ${why}`);
  }
  if (policy.replay?.onlyRecordedEdges !== false) {
    let i = 1;
    for (const r of run) {
      const root = r.parent.startsWith('/');
      // A root's setup steps (closing a first-visit panel) must follow a recorded edge into it: `via`
      // names that edge's source state.
      if (root && (r.hop || []).length && !(r.via && edgeSet.has(edgeKey(r.via, r.id)))) {
        return failed(i, `${r.id}: a hop from the root URL ${r.parent} must name in "via" a state with a recorded edge into ${r.id}`);
      }
      if (!root && !edgeSet.has(edgeKey(r.parent, r.id))) return failed(i, `${r.id}: ${r.parent} → ${r.id} is not a recorded edge`);
      i += (r.hop || []).length;
    }
  }

  const onRequest = (req) => {
    if (req.isNavigationRequest() && req.frame() === page.mainFrame() && req.method() !== 'GET') mutates = true;
  };
  page.on('request', onRequest);
  try {
    const rootStep = await runStep(page, ctx, steps[0].step, policy);
    if (!rootStep.ok) return failed(0, `${run[0].id}: ${rootStep.message}`);
    await parkPointer(page);
    await injectMotionOff(page);
    let i = 1;
    for (const r of run) {
      if (r === target) {
        await armLayer(page);
        if (!untilParent && beforeFinalHop) await beforeFinalHop(page);
      }
      for (const step of r.hop || []) {
        const res = await runStep(page, ctx, step, policy);
        if (!res.ok) return failed(i, `${r.id} hop ${(r.hop || []).indexOf(step)}: ${res.message}`);
        if (res.submitted) mutates = true;
        i += 1;
      }
    }
    // KTD4 parks the pointer outside the viewport: after a click it would rest on whatever is under
    // it, and hover styles or a hover tooltip would leak into the state. A hover hop keeps it.
    const last = (target.hop || []).at(-1);
    if (last && !('hover' in last)) {
      await parkPointer(page);
      await settle(page, ctx.cdp, { tracker: ctx.tracker });
    }
    await injectMotionOff(page);
    const fingerprint = await captureFingerprint(page, ctx.cdp, targetKind);
    if (!target.fingerprint) return { status: 'reached', step: null, message: `${target.id} has no fingerprint yet`, fingerprint, mutates };
    const m = matchFingerprint(target.fingerprint, fingerprint);
    if (!m.ok) return { status: 'fingerprint-mismatch', step: null, message: `${target.id}: ${m.why}`, fingerprint, mutates };
    return { status: 'reached', step: null, message: null, fingerprint, mutates };
  } finally {
    page.off('request', onRequest);
  }
}

// ---------------------------------------------------------------------------
// Source hashes and the refresh queue (KTD6, KTD8)
// ---------------------------------------------------------------------------

function verbPattern(verbs) {
  const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const alts = (verbs || []).filter((v) => v.trim()).sort((a, b) => b.length - a.length)
    .map((v) => v.trim().split(/\s+/).map(esc).join('\\s+'));
  return alts.length ? new RegExp(`(?<![A-Za-z0-9])(?:${alts.join('|')})(?![A-Za-z0-9])`, 'i') : null;
}

// The label plan.py keeps between two states: fewest mutating verbs, then the smallest label.
function bestLabel(labels, pattern) {
  const score = (l) => [pattern && pattern.test(l) ? 1 : 0, l];
  return labels.map(score).sort((a, b) => a[0] - b[0] || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0))[0][1];
}

// sha1 of [[from, to, label], ...] along the recipe's own chain, labels from the current map, as
// plan.py computes `source` for its paths. null when an edge on the chain is gone from the map.
export function sourceOf(recipes, stateId, policy) {
  const meta = recipes[META];
  const pattern = verbPattern(policy?.mutatingVerbs);
  const edges = [];
  for (const r of chain(recipes, stateId)) {
    if (r.parent.startsWith('/')) continue;
    const labels = meta.edges.get(edgeKey(r.parent, r.id));
    if (!labels) return null;
    edges.push([r.parent, r.id, bestLabel(labels, pattern)]);
  }
  return sha1(edges);
}

// States the previous published run found unreachable because their recipe broke.
function previousUnreachable(ws) {
  const meta = readJson(join(ws.publishedDir, 'meta.json'));
  if (!meta?.run) return {};
  const outcomes = readJson(ws.file('outcomes.json', meta.run));
  const out = {};
  for (const [id, o] of Object.entries(outcomes?.states || {})) {
    if (o.outcome === 'unreachable' && RECIPE_REASONS.has(o.reason)) out[id] = { run: meta.run, reason: o.reason };
  }
  return out;
}

// The refresh queue (KTD8): [{state, area, reason: 'no-recipe' | 'stale' | 'unreachable', detail}].
// Roots are listed too when they have no recipe: their recipe holds the root fingerprint.
export function refreshQueue(ws, runId, recipes = loadRecipes(ws)) {
  const states = ws.states(runId);
  const policy = ws.policy();
  const prev = previousUnreachable(ws);
  const out = [];
  for (const id of Object.keys(states).sort()) {
    const s = states[id];
    if (s.unreachable === 'no-path') continue;
    const r = recipes[id];
    const entry = (reason, detail = null) => out.push({ state: id, area: s.area, reason, detail });
    if (!r || !['accepted', 'review'].includes(r.status)) {
      entry('no-recipe', r ? `the recipe is ${r.status}` : null);
      continue;
    }
    let source;
    try {
      source = sourceOf(recipes, id, policy);
    } catch (e) {
      entry('stale', e.message);
      continue;
    }
    if (source !== r.source) {
      entry('stale', source === null ? 'an edge on its chain is gone from the map' : 'an edge label on its chain changed');
      continue;
    }
    const p = prev[id];
    if (p && !(r.checked && r.checked > p.run)) entry('unreachable', `${p.reason} in run ${p.run}`);
  }
  return out;
}

export function status(ws) {
  const runId = ws.runId(false);
  if (!runId || !existsSync(ws.file('states.json', runId))) return { pending: true };
  const recipes = loadRecipes(ws);
  const counts = { recipes: 0, accepted: 0, review: 0, rejected: 0, draft: 0 };
  for (const r of Object.values(recipes)) {
    counts.recipes += 1;
    counts[r.status] = (counts[r.status] || 0) + 1;
  }
  const queue = refreshQueue(ws, runId, recipes);
  const byReason = {};
  for (const q of queue) byReason[q.reason] = (byReason[q.reason] || 0) + 1;
  return { run: runId, ...counts, queue: queue.length, queueByReason: byReason };
}

// ---------------------------------------------------------------------------
// recipe check (KTD8 step 2)
// ---------------------------------------------------------------------------

// Read-modify-write of one area file under an exclusive lock, so recorders checking recipes in
// parallel never drop each other's entries.
function withLock(path, fn) {
  const lock = `${path}.lock`;
  mkdirSync(dirname(lock), { recursive: true });
  const t0 = Date.now();
  for (;;) {
    try {
      closeSync(openSync(lock, 'wx'));
      break;
    } catch (e) {
      if (e.code !== 'EEXIST') throw e;
      try {
        if (Date.now() - statSync(lock).mtimeMs > 30000) unlinkSync(lock); // left by a crashed process
      } catch { /* gone already */ }
      if (Date.now() - t0 > 60000) throw new Error(`${lock} is held: remove it if no recipe check is running`);
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 50);
    }
  }
  try {
    return fn();
  } finally {
    try {
      unlinkSync(lock);
    } catch { /* already removed */ }
  }
}

export function writeRecipe(ws, area, id, entry, { onlyIfMissing = false } = {}) {
  const path = join(ws.recipesDir, `${area}.json`);
  return withLock(path, () => {
    const doc = readJson(path, {});
    if (onlyIfMissing && doc[id]) return false;
    doc[id] = entry;
    writeJson(path, doc);
    return true;
  });
}

export function similarity(captured, shot) {
  const r = spawnSync('python3', [SIMILARITY, captured, shot, '--json'], { encoding: 'utf8' });
  if (r.status !== 0) throw new Error(`similarity.py failed: ${r.stderr}`);
  return JSON.parse(r.stdout);
}

// 10% of agent-recorded recipes go to the conductor even when they pass (KTD8 step 4).
const sampled = (id) => parseInt(sha1(id).slice(0, 8), 16) % 10 === 0;

// With `shots`, the reached state is screenshotted in light, then again in dark: the map's own
// captures are dark while the audit runs light (R2), and the demo follows prefers-color-scheme live.
// The replay itself, and its fingerprint, stay in light.
async function replayFresh(browser, baseUrl, recipes, id, policy, kind, shots = null) {
  const context = await newContext(browser);
  try {
    const page = await context.newPage();
    const tracker = trackRequests(page);
    const cdp = await cdpSession(context, page);
    const result = await replay(page, { cdp, tracker, baseUrl }, id, { recipes, policy, kind });
    if (shots && result.status === 'reached') {
      mkdirSync(dirname(shots.light), { recursive: true });
      await page.screenshot({ path: shots.light });
      await page.emulateMedia({ colorScheme: 'dark' });
      await settle(page, cdp, { tracker });
      await page.screenshot({ path: shots.dark });
    }
    return result;
  } finally {
    await context.close();
  }
}

const RECIPE_FIELDS = ['parent', 'via', 'hop', 'recordedBy'];

// Check one candidate recipe for `id` (the entry in recipes/<area>.json unless `candidate` is given):
// replay it twice in fresh contexts, capture the fingerprint, compare the second replay's screenshot
// with the map node's captured PNG, and write the entry back. A recipe that does not reach its state
// is written only when the state has no entry yet (as `draft`), so a working recipe is never lost.
async function checkOne(ws, runId, states, policy, browser, map, id, candidate) {
  const s = states[id];
  if (!s) return { state: id, status: 'error', message: `${id} is not in run ${runId}'s states.json` };
  const recipes = loadRecipes(ws, { map });
  const base = candidate || recipes[id];
  if (!base) return { state: id, status: 'no-recipe', message: `no recipe for ${id}: pass --recipe or run recipe draft` };
  const entry = Object.fromEntries(RECIPE_FIELDS.filter((k) => base[k] !== undefined).map((k) => [k, base[k]]));
  entry.recordedBy = entry.recordedBy || (s.root ? 'root' : 'agent');
  const trial = { ...recipes, [id]: { ...entry, fingerprint: null } };
  const keepDraft = () => writeRecipe(ws, s.area, id, { ...entry, fingerprint: null, mutates: false, source: null,
    status: 'draft', similarity: null, review: null, checked: runId }, { onlyIfMissing: true });

  let list;
  try {
    list = chain(trial, id);
  } catch (e) {
    keepDraft();
    return { state: id, status: 'blocked', message: e.message };
  }
  const notAccepted = list.slice(0, -1).find((r) => r.status !== 'accepted');
  if (notAccepted) {
    keepDraft();
    return { state: id, status: 'blocked', message: `${notAccepted.id} on its chain is ${notAccepted.status || 'unchecked'}, not accepted` };
  }

  const shot = join(ws.runDir(runId), 'shots', `${id}.recipe.png`);
  const one = await replayFresh(browser, ws.baseUrl, trial, id, policy, s.kind);
  if (one.status !== 'reached') {
    keepDraft();
    return { state: id, ...one };
  }
  if (NEEDS_LAYER.has(s.kind) && one.fingerprint.top.role === 'document') {
    keepDraft();
    return { state: id, status: 'structure-missing', step: null, message: `a ${s.kind} needs an open layer; the replay found none`, fingerprint: one.fingerprint };
  }
  const node = recipes[META].nodes[id];
  const captured = node?.image ? resolve(ROOT, node.image) : null;
  const shots = { light: shot, dark: shot.replace(/\.png$/, '.dark.png') };
  const second = { ...trial, [id]: { ...trial[id], fingerprint: one.fingerprint } };
  const two = await replayFresh(browser, ws.baseUrl, second, id, policy, s.kind, shots);
  if (two.status !== 'reached') {
    keepDraft();
    return { state: id, ...two, message: `second replay: ${two.message}` };
  }

  // The better of the two themes: whichever the capture was taken in.
  const sim = captured && existsSync(captured)
    ? [similarity(captured, shots.light), similarity(captured, shots.dark)].reduce((a, b) => (b.score > a.score ? b : a))
    : null;
  const mutates = Boolean(one.mutates || two.mutates || s.mutates || list.slice(0, -1).some((r) => r.mutates));
  let status = 'accepted';
  let review = null;
  const pending = (reason) => ({ reason, by: 'conductor', decision: null, date: null });
  if (!sim?.pass) [status, review] = ['review', pending('below-threshold')];
  else if (entry.recordedBy === 'agent' && mutates) [status, review] = ['review', pending('mutating')];
  else if (entry.recordedBy === 'agent' && sampled(id)) [status, review] = ['review', pending('sample')];
  const written = { ...entry, fingerprint: one.fingerprint, mutates, source: sourceOf(second, id, policy), status,
    similarity: sim ? sim.score : null, review, checked: runId };
  writeRecipe(ws, s.area, id, written);
  return { state: id, status, step: null, message: review ? `review: ${review.reason}` : null, similarity: written.similarity,
    threshold: sim?.threshold ?? null, mutates, screenshot: shot, captured, fingerprint: one.fingerprint };
}

// `recipe check` for each state; `candidates` maps a state id to a recipe to check in place of its
// entry. Pass `browser` to reuse one; otherwise a browser is launched and closed here.
export async function checkStates(ws, stateIds, { candidates = {}, browser = null } = {}) {
  const runId = ws.runId();
  const states = ws.states(runId);
  const policy = ws.policy();
  const map = ws.loadMap();
  const own = browser ? null : await launch();
  const results = [];
  try {
    for (const id of stateIds) results.push(await checkOne(ws, runId, states, policy, browser || own, map, id, candidates[id]));
  } finally {
    await own?.close();
  }
  return results;
}

export const checkPassed = (r) => r.status === 'accepted' || r.status === 'review';

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

const USAGE = 'usage: recipe.mjs check <state>... [--recipe <json>] | queue | status';

async function main(argv) {
  const { ws, values, positionals } = parse(argv, { recipe: { type: 'string' } });
  const [sub, ...ids] = positionals;
  if (sub === 'status') {
    console.log(JSON.stringify(status(ws)));
    return 0;
  }
  if (sub === 'queue') {
    const runId = ws.runId();
    const queue = refreshQueue(ws, runId);
    for (const q of queue) console.log(`${q.reason.padEnd(12)} ${q.state}${q.detail ? ` (${q.detail})` : ''}`);
    console.log(JSON.stringify({ run: runId, queue }));
    return 0;
  }
  if (sub === 'check') {
    if (!ids.length) throw new Error(`recipe check: name at least one state. ${USAGE}`);
    const candidates = {};
    if (values.recipe) {
      if (ids.length !== 1) throw new Error('recipe check: --recipe applies to exactly one state');
      candidates[ids[0]] = JSON.parse(values.recipe);
    }
    const results = await checkStates(ws, ids, { candidates });
    for (const r of results) {
      const sim = typeof r.similarity === 'number' ? ` similarity ${r.similarity}` : '';
      console.log(`${r.status.padEnd(20)} ${r.state}${sim}${r.message ? `: ${r.message}` : ''}`);
    }
    console.log(JSON.stringify({ checked: results }));
    return results.every(checkPassed) ? 0 : 1;
  }
  console.error(USAGE);
  return 2;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).then((code) => process.exit(code), (e) => {
    console.error(`recipe: ${e.message}`);
    console.log(JSON.stringify({ error: e.message }));
    process.exit(1);
  });
}
