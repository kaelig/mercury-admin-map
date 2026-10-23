// `recipe draft` (KTD8 step 1): turn edge labels into recipe steps mechanically, then submit them
// through `recipe check`. What it cannot resolve is the agent queue.
//
//   node tools/a11y/draft.mjs [state...]               default: every state in the refresh queue
//   node tools/a11y/draft.mjs --ready [--shard i/n]    only the queue states ready to draft
//   node tools/a11y/draft.mjs status
//
// A label is split into clauses on ", then", "then", "and <verb>" and commas. Each clause must read
// as one of: Click|Open|Choose|Pick|Select|Toggle X; Tick|Check X; Type|Enter V in|into X; Press K.
// X is found on the live parent state: a role and accessible name (case-insensitive, then a unique
// partial match such as "the Blue Bottle Coffee row"), then a label, then visible text. The step
// stores the element's own name as a name template. Typed values must be policy.fixtureValues.
// Anything else ("Hover over the chart", "Click a card row") goes to the agent queue.
//
// Every attempt appends one line to runs/<id>/draft.jsonl, with the hash of the parent recipe it
// drafted against. --ready drafts only queue states whose parent's chain is accepted and that have
// no attempt against that same parent recipe, then repeats until none is left. --shard i/n keeps
// the states whose id hashes to shard i, so n processes split one pass. A browser that fails to
// launch stops the pass with exit 1 and logs no attempt for the state that needed it. File
// formats: schemas.md.
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, writeSync } from 'node:fs';
import { dirname } from 'node:path';
import { canonical, clean, cmp, escapeRegExp, nameTemplate, nameTemplateRegex, sha, sha1 } from './lib/canon.mjs';
import { cdpSession, launch, newContext, trackRequests } from './lib/browser.mjs';
import { parse, runMain } from './lib/cli.mjs';
import { chain, checkPassed, checkStates, fixtureValues, loadRecipes, refreshQueue, replay, runStep, whileConnected } from './recipe.mjs';

const SPLIT = /\s*,\s*then\s+|\s+then\s+|\s*,\s*and\s+|\s*,\s+|\s+and\s+(?=(?:click|open|choose|pick|select|toggle|tick|check|type|enter|press)\b)/i;
const CLICK = /^(?:click|open|choose|pick|select|toggle)\s+(.+)$/i;
const CHECK = /^(?:tick|check)\s+(.+)$/i;
const FILL = /^(?:type|enter)\s+(.+?)\s+(?:in|into)\s+(.+)$/i;
const PRESS = /^press\s+(.+)$/i;
const KEYS = { enter: 'Enter', return: 'Enter', escape: 'Escape', esc: 'Escape', tab: 'Tab', space: 'Space',
  backspace: 'Backspace', 'arrow down': 'ArrowDown', 'arrow up': 'ArrowUp', 'arrow left': 'ArrowLeft', 'arrow right': 'ArrowRight' };
const HINTS = { button: 'button', link: 'link', tab: 'tab', 'menu item': 'menuitem', menuitem: 'menuitem', checkbox: 'checkbox',
  switch: 'switch', toggle: 'switch', option: 'option', row: null, menu: 'button', dropdown: 'button' };
const CLICKABLE = ['button', 'link', 'menuitem', 'tab', 'option', 'checkbox', 'radio', 'switch', 'menuitemcheckbox',
  'menuitemradio', 'combobox', 'treeitem', 'row', 'gridcell'];
const CHECKABLE = ['checkbox', 'switch', 'radio', 'menuitemcheckbox', 'menuitemradio'];
const TEXTBOX = ['textbox', 'searchbox', 'combobox', 'spinbutton'];

const unquote = (s) => s.trim().replace(/^(['"‘“])(.*)(['"’”])$/, '$2').trim();

function target(raw) {
  let name = raw.trim().replace(/\s*\([^)]*\)$/, '').replace(/^the\s+/i, '');
  let role;
  const hint = /\s+(button|link|tab|menu item|menuitem|checkbox|switch|toggle|option|row|menu|dropdown)$/i.exec(name);
  if (hint) {
    name = name.slice(0, hint.index);
    role = HINTS[hint[1].toLowerCase()];
  }
  name = unquote(name);
  if (!name) return null;
  return role ? { name, role } : { name };
}

// An edge label -> {steps} with unresolved targets ({name, role?}), or {error}.
export function parseLabel(label, policy) {
  const values = fixtureValues(policy);
  const steps = [];
  for (const clause of String(label || '').trim().split(SPLIT).map((c) => c.trim()).filter(Boolean)) {
    let m;
    if ((m = FILL.exec(clause))) {
      const value = unquote(m[1]);
      const t = target(m[2]);
      if (!t) return { error: `cannot parse "${clause}"` };
      if (!values.has(value)) return { error: `typed value "${value}" is not one of policy.fixtureValues` };
      steps.push({ fill: { locator: { name: t.name }, value } });
    } else if ((m = CHECK.exec(clause))) {
      const t = target(m[1]);
      if (!t) return { error: `cannot parse "${clause}"` };
      steps.push({ check: t });
    } else if ((m = CLICK.exec(clause))) {
      const t = target(m[1]);
      if (!t) return { error: `cannot parse "${clause}"` };
      steps.push({ click: t });
    } else if ((m = PRESS.exec(clause))) {
      const raw = unquote(m[1]);
      const key = KEYS[raw.toLowerCase()] || (/^[A-Za-z0-9]$/.test(raw) ? raw : null);
      if (!key) return { error: `cannot parse "${clause}"` };
      steps.push({ press: key });
    } else {
      return { error: `cannot parse "${clause}"` };
    }
  }
  return steps.length ? { steps } : { error: 'empty label' };
}

const loose = (name) => new RegExp(nameTemplateRegex(nameTemplate(name)).source, 'i');

// The accessible name Playwright computes for the first element of `loc`.
async function nameOf(loc) {
  const snap = await loc.ariaSnapshot().catch(() => '');
  const m = /^- [\w-]+ "((?:[^"\\]|\\.)*)"/.exec(snap);
  if (m) return JSON.parse(`"${m[1]}"`);
  return loc.evaluate((el) => (el.getAttribute('aria-label') || el.innerText || '').replace(/\s+/g, ' ').trim());
}

// A recipe locator for the element a label names, found on the live page, or null.
async function locate(page, { name, role }, roles) {
  const order = role ? [role, ...roles.filter((r) => r !== role)] : roles;
  for (const r of order) {
    const loc = page.getByRole(r, { name: loose(name) });
    if (await loc.count()) return { role: r, name: nameTemplate(await nameOf(loc.first())) };
  }
  const labelled = page.getByLabel(loose(name)).filter({ visible: true });
  if (await labelled.count()) return { label: nameTemplate(await nameOf(labelled.first())) };
  for (const r of order) {
    const loc = page.getByRole(r, { name: new RegExp(escapeRegExp(name), 'i') });
    if ((await loc.count()) === 1) return { role: r, name: nameTemplate(await nameOf(loc)) };
  }
  const text = page.getByText(loose(name)).filter({ visible: true });
  if (await text.count()) return { text: nameTemplate(clean(await text.first().innerText())) };
  return null;
}

async function concrete(page, step) {
  if ('press' in step) return step;
  if ('click' in step) {
    const l = await locate(page, step.click, CLICKABLE);
    return l ? { click: l } : null;
  }
  if ('check' in step) {
    const l = await locate(page, step.check, [...CHECKABLE, ...CLICKABLE.filter((r) => !CHECKABLE.includes(r))]);
    return l ? { check: l } : null;
  }
  const t = step.fill.locator;
  const labelled = page.getByLabel(loose(t.name)).filter({ visible: true });
  if (await labelled.count()) return { fill: { locator: { label: nameTemplate(await nameOf(labelled.first())) }, value: step.fill.value } };
  for (const r of TEXTBOX) {
    const loc = page.getByRole(r, { name: loose(t.name) });
    if (await loc.count()) return { fill: { locator: { role: r, name: nameTemplate(await nameOf(loc.first())) }, value: step.fill.value } };
  }
  return null;
}

const describe = (step) => JSON.stringify(step);

// Replay the parent, then resolve each parsed step on the live page, running all but the last so
// the next one is found where it will be. Returns {steps} or {error}.
async function resolveSteps(browser, ws, recipes, policy, s, parsed) {
  const context = await newContext(browser);
  try {
    const page = await context.newPage();
    const tracker = trackRequests(page);
    const cdp = await cdpSession(context, page);
    const ctx = { cdp, tracker, baseUrl: ws.baseUrl };
    const parent = await replay(page, ctx, s.parent, { recipes, policy });
    if (parent.status !== 'reached') return { error: `replaying the parent ${s.parent}: ${parent.status}: ${parent.message}` };
    const steps = [];
    for (const [i, step] of parsed.entries()) {
      const c = await concrete(page, step);
      if (!c) return { error: `nothing on ${s.parent} matches ${describe(step)}` };
      steps.push(c);
      if (i < parsed.length - 1) {
        const res = await runStep(page, ctx, c, policy);
        if (!res.ok) return { error: res.message };
      }
    }
    return { steps };
  } finally {
    await context.close();
  }
}

const depth = (s) => (s?.path ? s.path.length : 0);

// ---------------------------------------------------------------------------
// The attempt log (runs/<id>/draft.jsonl) and --ready
// ---------------------------------------------------------------------------

// The hash of the recipe chain from the root to `stateId` (the sweep's `recipeHash`), when every
// recipe on it is accepted; null when one is missing or not accepted.
export function acceptedChainHash(recipes, stateId) {
  let list;
  try {
    list = chain(recipes, stateId);
  } catch {
    return null;
  }
  if (list.some((r) => r.status !== 'accepted')) return null;
  return sha(list.map((r) => ({ id: r.id, parent: r.parent, hop: r.hop || [], fingerprint: r.fingerprint ?? null })));
}

// What a draft of state `s` replays: its parent's accepted chain hash. Null for a root (it replays
// no recipe) and for a parent whose chain is not accepted yet.
export const parentRecipe = (recipes, s) => (s && !s.root && s.parent ? acceptedChainHash(recipes, s.parent) : null);

function today(d = new Date()) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// One draft.jsonl line. `result` is accepted, review or queued; `reason` is the agent-queue reason
// of a queued attempt, else null.
export function attemptLine({ state, parent = null, parentRecipe: hash = null, label = null, result, reason = null, date = today() }) {
  return { state, parent, parentRecipe: hash, label, result, reason: result === 'queued' ? reason : null, date };
}

// Append one line with a single write() on a file opened O_APPEND. The kernel places each such
// write whole at the end of the file, so the 8 draft processes of a pass log at once without a lock.
export function appendAttempt(path, line) {
  mkdirSync(dirname(path), { recursive: true });
  const buf = Buffer.from(canonical(line) + '\n');
  const fd = openSync(path, 'a');
  try {
    const n = writeSync(fd, buf);
    if (n !== buf.length) throw new Error(`short write to ${path}: ${n} of ${buf.length} bytes`);
  } finally {
    closeSync(fd);
  }
}

// draft.jsonl's lines. A last line with no newline that does not parse is one another draft process
// is still writing: it is left out, not an error.
export function readAttempts(path) {
  if (!existsSync(path)) return [];
  const lines = readFileSync(path, 'utf8').split('\n');
  const last = lines.pop();
  const out = lines.filter((l) => l.trim()).map((l) => JSON.parse(l));
  if (last.trim()) {
    try {
      out.push(JSON.parse(last));
    } catch {
      // still being written
    }
  }
  return out;
}

// `--shard i/n` as {index: i, count: n}, 1 ≤ i ≤ n.
export function parseShard(text) {
  const m = /^(\d+)\/(\d+)$/.exec(String(text ?? ''));
  const [index, count] = m ? [Number(m[1]), Number(m[2])] : [0, 0];
  if (!m || count < 1 || index < 1 || index > count) throw new Error(`--shard takes i/n with 1 ≤ i ≤ n, such as 3/8 (got ${JSON.stringify(text)})`);
  return { index, count };
}

// Whether `id` is in `shard`. Decided by a hash of the id, not its place in a list, so processes
// that read the files a moment apart still split the states the same way.
export const inShard = (id, shard) => !shard || parseInt(sha1(id).slice(0, 8), 16) % shard.count === shard.index - 1;

// The queue states `--ready` drafts: a root, or a state whose parent's chain is accepted; not
// rejected by the conductor (a rejected recipe goes to a recorder); and with no attempt in
// `attempts` (draft.jsonl lines) against the same parent recipe. A re-recorded parent changes the
// hash, so its children are ready again. Returns {selected, skipped, otherShards}.
export function readyStates(states, recipes, queueIds, attempts, shard = null) {
  const key = (id, hash) => `${id}\n${hash ?? ''}`;
  const tried = new Set(attempts.map((a) => key(a.state, a.parentRecipe)));
  const out = { selected: [], skipped: { attempted: 0, noAcceptedParent: 0, rejected: 0 }, otherShards: 0 };
  for (const id of [...new Set(queueIds)].sort(cmp)) {
    if (!inShard(id, shard)) {
      out.otherShards += 1;
      continue;
    }
    const s = states[id];
    const hash = parentRecipe(recipes, s);
    if (!s || s.unreachable || (!s.root && hash === null)) out.skipped.noAcceptedParent += 1;
    else if (recipes[id]?.status === 'rejected') out.skipped.rejected += 1;
    else if (tried.has(key(id, hash))) out.skipped.attempted += 1;
    else out.selected.push(id);
  }
  return out;
}

// ---------------------------------------------------------------------------
// recipe draft
// ---------------------------------------------------------------------------

async function draft(ws, ids = [], { ready = false, shard = null } = {}) {
  const runId = ws.runId();
  const states = ws.states(runId);
  const policy = ws.policy();
  const map = ws.loadMap();
  const log = ws.file('draft.jsonl', runId);
  const byDepth = (list) => [...list].sort((a, b) => depth(states[a]) - depth(states[b]) || cmp(a, b));
  const out = { run: runId, accepted: [], review: [], agentQueue: [] };
  let browser = null;
  // A browser on first use, and a new one when the last one died. A launch that fails is no state's
  // fault: it stops the pass (exit 1) with no attempt logged for the state that needed it.
  const live = async () => {
    if (!browser?.isConnected()) {
      try {
        browser = await launch();
      } catch (e) {
        throw Object.assign(new Error(`could not launch the browser: ${e.message.split('\n')[0]}`), { launch: true });
      }
    }
    return browser;
  };

  // Draft one state: [result, reason], result accepted, review or queued. Sets `at.hash` to the
  // parent recipe it drafts against.
  async function work(id, s, label, at) {
    if (!s) return ['queued', `not in run ${runId}'s states.json`];
    // One state's exception (a page that crashed or navigated mid-replay, or the browser dying)
    // queues that state alone; the next state gets a new browser if this one is gone.
    try {
      let candidate;
      if (s.root) {
        candidate = { parent: s.rootUrl, hop: [] };
      } else if (s.unreachable) {
        return ['queued', `unreachable: ${s.unreachable}`];
      } else {
        const recipes = loadRecipes(ws, { map });
        at.hash = parentRecipe(recipes, s);
        const parsed = parseLabel(label, policy);
        if (parsed.error) return ['queued', parsed.error];
        if (recipes[s.parent]?.status !== 'accepted') return ['queued', `the parent ${s.parent} has no accepted recipe`];
        const b = await live();
        const resolved = await whileConnected(b, resolveSteps(b, ws, recipes, policy, s, parsed.steps));
        if (resolved.error) return ['queued', resolved.error];
        candidate = { parent: s.parent, hop: resolved.steps };
      }
      const [res] = await checkStates(ws, [id], { candidates: { [id]: candidate }, browser: await live(), recordedBy: 'draft' });
      console.log(`${res.status.padEnd(20)} ${id}${res.message ? `: ${res.message}` : ''}`);
      return checkPassed(res) ? [res.status, null] : ['queued', `${res.status}: ${res.message}`];
    } catch (e) {
      if (e.launch) throw e;
      const message = e.message.split('\n')[0];
      console.log(`${'tool-error'.padEnd(20)} ${id}: ${message}`);
      return ['queued', `tool-error: ${message}`];
    }
  }

  // Draft each state in turn, logging each attempt in draft.jsonl as it ends. An id that is not in
  // states.json (a typo) is queued but not logged: there was nothing to attempt.
  async function attemptAll(order) {
    for (const id of order) {
      const s = states[id];
      const label = s?.labels ? s.labels[s.labels.length - 1] : null;
      const at = { hash: null };
      const [result, reason] = await work(id, s, label, at);
      if (result === 'queued') out.agentQueue.push({ state: id, label, reason });
      else out[result].push(id);
      if (s) appendAttempt(log, attemptLine({ state: id, parent: s.parent ?? null, parentRecipe: at.hash, label, result, reason }));
    }
  }

  try {
    if (!ready) {
      await attemptAll(byDepth(ids.length ? ids : new Set(refreshQueue(ws, runId).map((q) => q.state))));
    } else {
      // Each round drafts what is ready now; what it accepts makes their children ready for the next.
      out.ready = { rounds: 0, selected: 0 };
      for (;;) {
        const recipes = loadRecipes(ws, { map });
        const pick = readyStates(states, recipes, refreshQueue(ws, runId, recipes).map((q) => q.state), readAttempts(log), shard);
        out.ready = { rounds: out.ready.rounds + 1, selected: out.ready.selected + pick.selected.length, skipped: pick.skipped,
          ...(shard ? { otherShards: pick.otherShards } : {}) };
        const k = pick.skipped;
        console.log(`ready round ${out.ready.rounds}: ${pick.selected.length} selected; skipped ${k.attempted} attempted against the same parent recipe, `
          + `${k.noAcceptedParent} with no accepted parent, ${k.rejected} rejected${shard ? `; ${pick.otherShards} in other shards` : ''}`);
        if (!pick.selected.length) break;
        await attemptAll(byDepth(pick.selected));
      }
    }
  } finally {
    await browser?.close();
  }
  for (const q of out.agentQueue) console.log(`${'agent queue'.padEnd(20)} ${q.state}: ${q.reason}`);
  return out;
}

export function status(ws) {
  const runId = ws.runId(false);
  if (!ws.hasStates(runId)) return { pending: true };
  const counts = { drafted: 0, accepted: 0, review: 0, rejected: 0, draft: 0 };
  const recipes = loadRecipes(ws);
  for (const r of Object.values(recipes)) {
    if (r.recordedBy !== 'draft') continue;
    counts.drafted += 1;
    counts[r.status] = (counts[r.status] || 0) + 1;
  }
  const attempts = readAttempts(ws.file('draft.jsonl', runId));
  counts.attempts = attempts.length;
  // Without the map, every recipe would look stale.
  counts.ready = !existsSync(ws.mapPath) ? null : readyStates(ws.states(runId), recipes, refreshQueue(ws, runId, recipes).map((q) => q.state), attempts).selected.length;
  return counts;
}

async function main(argv) {
  const { ws, values, positionals } = parse(argv, { ready: { type: 'boolean' }, shard: { type: 'string' } });
  if (positionals[0] === 'status') {
    console.log(JSON.stringify(status(ws)));
    return 0;
  }
  if (values.shard !== undefined && !values.ready) throw new Error('--shard splits a --ready pass: pass --ready too');
  if (values.ready && positionals.length) throw new Error('--ready picks its states from the refresh queue: name none');
  const shard = values.shard === undefined ? null : parseShard(values.shard);
  console.log(JSON.stringify(await draft(ws, positionals, { ready: Boolean(values.ready), shard })));
  return 0;
}

runMain(import.meta.url, 'recipe draft', main);
