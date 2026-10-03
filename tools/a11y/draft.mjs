// `recipe draft` (KTD8 step 1): turn edge labels into recipe steps mechanically, then submit them
// through `recipe check`. What it cannot resolve is the agent queue.
//
//   node tools/a11y/draft.mjs [state...]     default: every state in the refresh queue
//   node tools/a11y/draft.mjs status
//
// A label is split into clauses on ", then", "then", "and <verb>" and commas. Each clause must read
// as one of: Click|Open|Choose|Pick|Select|Toggle X; Tick|Check X; Type|Enter V in|into X; Press K.
// X is found on the live parent state: a role and accessible name (case-insensitive, then a unique
// partial match such as "the Blue Bottle Coffee row"), then a label, then visible text. The step
// stores the element's own name as a name template. Typed values must be policy.fixtureValues.
// Anything else ("Hover over the chart", "Click a card row") goes to the agent queue.
import { clean, cmp, escapeRegExp, nameTemplate, nameTemplateRegex } from './lib/canon.mjs';
import { cdpSession, launch, newContext, trackRequests } from './lib/browser.mjs';
import { parse, runMain } from './lib/cli.mjs';
import { checkPassed, checkStates, fixtureValues, loadRecipes, refreshQueue, replay, runStep, whileConnected } from './recipe.mjs';

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

async function draft(ws, ids = []) {
  const runId = ws.runId();
  const states = ws.states(runId);
  const policy = ws.policy();
  const map = ws.loadMap();
  const wanted = ids.length ? ids : [...new Set(refreshQueue(ws, runId).map((q) => q.state))];
  const order = [...wanted].sort((a, b) => depth(states[a]) - depth(states[b]) || cmp(a, b));
  const out = { run: runId, accepted: [], review: [], agentQueue: [] };
  const queue = (state, label, reason) => out.agentQueue.push({ state, label, reason });
  let browser = await launch();
  try {
    for (const id of order) {
      if (!browser.isConnected()) browser = await launch();
      const s = states[id];
      if (!s) {
        queue(id, null, `not in run ${runId}'s states.json`);
        continue;
      }
      const label = s.labels ? s.labels[s.labels.length - 1] : null;
      // One state's exception (a page that crashed or navigated mid-replay, or the browser dying)
      // queues that state alone; the next state gets a new browser if this one is gone.
      try {
        let candidate;
        if (s.root) {
          candidate = { parent: s.rootUrl, hop: [] };
        } else if (s.unreachable) {
          queue(id, label, `unreachable: ${s.unreachable}`);
          continue;
        } else {
          const parsed = parseLabel(label, policy);
          if (parsed.error) {
            queue(id, label, parsed.error);
            continue;
          }
          const recipes = loadRecipes(ws, { map });
          if (recipes[s.parent]?.status !== 'accepted') {
            queue(id, label, `the parent ${s.parent} has no accepted recipe`);
            continue;
          }
          const resolved = await whileConnected(browser, resolveSteps(browser, ws, recipes, policy, s, parsed.steps));
          if (resolved.error) {
            queue(id, label, resolved.error);
            continue;
          }
          candidate = { parent: s.parent, hop: resolved.steps };
        }
        const [res] = await checkStates(ws, [id], { candidates: { [id]: candidate }, browser, recordedBy: 'draft' });
        if (res.status === 'accepted') out.accepted.push(id);
        else if (res.status === 'review') out.review.push(id);
        if (!checkPassed(res)) queue(id, label, `${res.status}: ${res.message}`);
        console.log(`${res.status.padEnd(20)} ${id}${res.message ? `: ${res.message}` : ''}`);
      } catch (e) {
        const message = e.message.split('\n')[0];
        queue(id, label, `tool-error: ${message}`);
        console.log(`${'tool-error'.padEnd(20)} ${id}: ${message}`);
      }
    }
  } finally {
    await browser.close();
  }
  for (const q of out.agentQueue) console.log(`${'agent queue'.padEnd(20)} ${q.state}: ${q.reason}`);
  return out;
}

export function status(ws) {
  if (!ws.hasStates(ws.runId(false))) return { pending: true };
  const counts = { drafted: 0, accepted: 0, review: 0, rejected: 0, draft: 0 };
  for (const r of Object.values(loadRecipes(ws, { map: { areas: [] } }))) {
    if (r.recordedBy !== 'draft') continue;
    counts.drafted += 1;
    counts[r.status] = (counts[r.status] || 0) + 1;
  }
  return counts;
}

async function main(argv) {
  const { ws, positionals } = parse(argv);
  if (positionals[0] === 'status') {
    console.log(JSON.stringify(status(ws)));
    return 0;
  }
  console.log(JSON.stringify(await draft(ws, positionals)));
  return 0;
}

runMain(import.meta.url, 'recipe draft', main);
