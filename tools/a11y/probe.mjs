// `probe` (KTD19, KTD20): measure one element of a state again, under the sweep's environment and
// with the check modules' own logic, so a verifier can cite the measurement in a verdict.
//
//   node tools/a11y/probe.mjs <state> --kind contrast|target|focus|ax --xpath <xpath> [--session <name>]
//                            [--workspace … --map … --base-url … --run …]
//   node tools/a11y/probe.mjs status
//
// Without --session it opens a fresh context in KTD4's environment, replays the state's recipe and
// injects the motion-off CSS. With --session it attaches over CDP to the agent-browser session that
// `reach` prepared, checks the session still shows the state (the recipe's fingerprint), measures,
// and disconnects without closing it. Playwright's attach resets media emulation, so the probe
// emulates light and reduced motion on its own connection, and agent-browser's viewport and media
// settings are applied again after it disconnects.
//
// Kinds (schemas.md "verdicts.jsonl"):
//   contrast  axe's color-contrast rule on that element alone: fg, bg, ratio, required, fontSize,
//             fontWeight; or incomplete with axe's messageKey (ratio null).
//   target    width, height and offset (the clear circle's diameter) among the state's
//             expected-interactive set, as checks/target.mjs computes them for 2.5.8.
//   focus     the element focused by Tab from the start of its scope (or directly when the walk
//             never reaches it): the padded focused-vs-blurred pixel diff and the hidden test of
//             checks/tabwalk.mjs, so visible = not hidden and diff ≥ FOCUS_DIFF_FLOOR. The walk goes
//             on to its end; at a revisit, Esc and Shift+Tab probe the loop, and trapped says
//             whether focus got out.
//   ax        role, name, states, description and the record template, as checks/ax.mjs reads them.
//
// Prints the probe as the last stdout line: {id, state, kind, env, value, xpath, build, pass}. `env`
// is read from the page while it measures, so a probe taken elsewhere fails verdict.py's env check.
// `pass` applies verdict.py's rule for the kind (focus: visible and not trapped; ax: null, not a
// measured kind). Exits 1 with {error, …} when the state cannot be reached or the element is missing.
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { ENV, cdpSession, injectMotionOff, launch, newContext, readBuildHash, trackRequests } from './lib/browser.mjs';
import { canonical, sha } from './lib/canon.mjs';
import { parse } from './lib/cli.mjs';
import { XPATH_FN } from './lib/occurrence.mjs';
import { findLayer } from './lib/scope.mjs';
import { META, chain, loadRecipes, replay } from './recipe.mjs';
import { captureFingerprint, matchFingerprint } from './fingerprint.mjs';
import { injectAxe } from './checks/axe.mjs';
import { axInScope, statesOf, templateOf } from './checks/ax.mjs';
import * as interactive from './checks/interactive.mjs';
import { CROP_PAD, FOCUS_DIFF_FLOOR, PAGE_HELPERS, pixelDiff, settleFocus } from './checks/tabwalk.mjs';

const KINDS = ['contrast', 'target', 'focus', 'ax'];
// verdict.py's pass thresholds.
const TARGET_MIN = 24;
const DEFAULT_RATIO = 4.5;

class ProbeError extends Error {
  constructor(message, detail = {}) {
    super(message);
    this.detail = detail;
  }
}

const find = (xpath) => `document.evaluate(${JSON.stringify(xpath)}, document, null, 9, null).singleNodeValue`;

// What the page is rendered under right now: the fields verdict.py compares with the sweep's.
const ENV_NOW = `(() => ({ viewport: [innerWidth, innerHeight], dpr: devicePixelRatio,
  colorScheme: matchMedia('(prefers-color-scheme: light)').matches ? 'light' : matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'no-preference',
  reducedMotion: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'reduce' : 'no-preference' }))()`;

// ---------------------------------------------------------------------------
// contrast: checks/axe.mjs's injection and evidence fields, on one element
// ---------------------------------------------------------------------------

const CONTRAST = `async (el) => {
  const r = await axe.run(el, { runOnly: { type: 'rule', values: ['color-contrast'] }, elementRef: true,
    selectors: false, ancestry: false, xpath: false, iframes: false });
  for (const [type, basis] of [['violations', 'violation'], ['incomplete', 'incomplete'], ['passes', 'pass']]) {
    for (const rule of r[type]) for (const n of rule.nodes) {
      if (n.element !== el) continue;
      const data = {};
      for (const c of [...n.any, ...n.all, ...n.none]) if (c.data && typeof c.data === 'object') Object.assign(data, c.data);
      return { basis, data };
    }
  }
  return null;
}`;

async function contrast({ cdp, element }) {
  await injectAxe(cdp);
  const r = await callOn(cdp, element, CONTRAST);
  if (!r) throw new ProbeError('axe\'s color-contrast rule does not apply to the element: it holds no text axe measures');
  const d = r.data;
  const incomplete = r.basis === 'incomplete';
  return {
    fg: d.fgColor ?? null, bg: d.bgColor ?? null, ratio: incomplete ? null : d.contrastRatio ?? null,
    required: d.expectedContrastRatio ? Number.parseFloat(d.expectedContrastRatio) : null,
    fontSize: d.fontSize ?? null, fontWeight: d.fontWeight ?? null, incomplete, messageKey: incomplete ? d.messageKey ?? null : null,
  };
}

// ---------------------------------------------------------------------------
// target: checks/target.mjs's spacing computation (copied: it does not export it)
// ---------------------------------------------------------------------------

const MIN = 24;
const EPS = 0.01;
const round2 = (v) => Math.round(v * 100) / 100;
const rendered = ([x, y, w, h]) => w * h > 1 && x + w > 0 && y + h > 0;
const related = (a, b) => a.startsWith(b + '/') || b.startsWith(a + '/');

function distanceToBox(px, py, b) {
  const dx = Math.max(b.x - px, 0, px - (b.x + b.w));
  const dy = Math.max(b.y - py, 0, py - (b.y + b.h));
  return Math.hypot(dx, dy);
}

function clearRadius(a, boxes) {
  let r = Infinity;
  for (const b of boxes) {
    if (b === a || related(a.xpath, b.xpath)) continue;
    let d = distanceToBox(a.cx, a.cy, b);
    if (b.small) d = Math.min(d, Math.max(0, Math.hypot(a.cx - b.cx, a.cy - b.cy) - MIN / 2));
    r = Math.min(r, d);
  }
  return r;
}

const RECTS = `(xpaths) => xpaths.map((x) => {
  const e = document.evaluate(x, document, null, 9, null).singleNodeValue;
  if (!e) return null;
  const r = e.getBoundingClientRect();
  return [r.x, r.y, r.width, r.height];
})`;

async function targetSize({ page, cdp, kind, xpath }) {
  const layer = await findLayer(cdp, kind);
  const { detail: set } = await interactive.run({ page, cdp, kind, layer });
  const xpaths = set.map((t) => t.xpath);
  if (!xpaths.includes(xpath)) xpaths.push(xpath);
  const rects = await page.evaluate(`(${RECTS})(${JSON.stringify(xpaths)})`);
  const boxes = [];
  let own = null;
  xpaths.forEach((x, i) => {
    const rect = rects[i];
    if (!rect) return;
    const [bx, by, w, h] = rect;
    const box = { xpath: x, x: bx, y: by, w, h, cx: bx + w / 2, cy: by + h / 2, small: w < MIN - EPS || h < MIN - EPS };
    if (x === xpath) own = box;
    if (rendered(rect)) boxes.push(box);
  });
  const radius = clearRadius(own, boxes);
  return { width: round2(own.w), height: round2(own.h), offset: Number.isFinite(radius) ? round2(2 * radius) : null };
}

// ---------------------------------------------------------------------------
// focus: checks/tabwalk.mjs's walk, hidden test and padded pixel diff (copied in part: the in-page
// INIT, INFO, BLUR and REFOCUS expressions and probeTrap are not exported)
// ---------------------------------------------------------------------------

const STATE = "window[Symbol.for('a11y.probe')]";

// Reset focus to the start of the scope; returns the scope's tabbable count.
const INIT = `(layerXPath, targetXPath) => {
  ${PAGE_HELPERS}
  const find = (x) => document.evaluate(x, document, null, 9, null).singleNodeValue;
  const scope = layerXPath ? find(layerXPath) : null;
  const S = ${STATE} = { ids: new WeakMap(), next: 1, el: null, scope, restore: null, focused: null, target: find(targetXPath) };
  const a = deepActive();
  if (a) a.blur();
  if (scope) {
    if (!scope.hasAttribute('tabindex')) {
      scope.setAttribute('tabindex', '-1');
      S.restore = () => scope.removeAttribute('tabindex');
    }
    scope.focus({ preventScroll: true });
  } else {
    window.scrollTo(0, 0);
    const t = document.createElement('span');
    t.tabIndex = -1;
    document.body.prepend(t);
    t.focus({ preventScroll: true });
    t.remove();
  }
  return tabbableIn(scope || document.body);
}`;

const SIG = `
  const sigOf = (e, pseudo) => {
    const s = getComputedStyle(e, pseudo);
    return [
      s.outlineStyle + ' ' + s.outlineWidth + ' ' + s.outlineColor + ' ' + s.outlineOffset,
      s.boxShadow,
      [s.borderTopColor, s.borderRightColor, s.borderBottomColor, s.borderLeftColor].join(' '),
      [s.borderTopWidth, s.borderRightWidth, s.borderBottomWidth, s.borderLeftWidth].join(' '),
      s.backgroundColor + ' ' + s.backgroundImage,
      s.color,
      s.textDecorationLine + ' ' + s.textDecorationColor,
      s.opacity, s.transform, s.filter,
    ].concat(pseudo ? [s.content] : []);
  };
  const fullSig = (e, self) => self ? [sigOf(e, null), sigOf(e, '::before'), sigOf(e, '::after')] : [sigOf(e, null)];
`;

// Where focus is, and whether the focused element is hidden; keeps its style chain for BLUR.
const WHERE = `(() => {
  ${PAGE_HELPERS}
  ${SIG}
  const S = ${STATE};
  if (S.restore) { S.restore(); S.restore = null; }
  const el = deepActive();
  S.el = el;
  if (!el) return { body: true };
  const idOf = (e) => { if (!S.ids.has(e)) S.ids.set(e, S.next++); return S.ids.get(e); };
  const frame = el.tagName === 'IFRAME' || el.tagName === 'FRAME';
  const comp = el.closest('[role=grid],[role=listbox],[role=menu],[role=menubar],[role=radiogroup],[role=tablist],[role=tree],[role=treegrid],[role=toolbar]');
  const composite = comp && (!S.scope || (S.scope !== comp && S.scope.contains(comp))) ? comp : null;
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  let hidden = null;
  if (!el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) hidden = 'invisible';
  else if (r.width < 1 || r.height < 1) hidden = 'zero-size';
  else {
    let [x0, y0, x1, y1] = [r.left, r.top, r.right, r.bottom];
    if (cs.clip === 'rect(0px, 0px, 0px, 0px)' || /inset\\(50%\\)/.test(cs.clipPath)) x1 = x0;
    let pos = cs.position;
    for (let p = el.parentElement; p && p !== document.body && p !== document.documentElement && pos !== 'fixed'; p = p.parentElement) {
      const ps = getComputedStyle(p);
      if (pos === 'absolute' && ps.position === 'static' && ps.transform === 'none') continue;
      if (ps.overflowX !== 'visible' || ps.overflowY !== 'visible') {
        const b = p.getBoundingClientRect();
        x0 = Math.max(x0, b.left); y0 = Math.max(y0, b.top); x1 = Math.min(x1, b.right); y1 = Math.min(y1, b.bottom);
      }
      pos = ps.position;
    }
    if (Math.max(0, x1 - x0) * Math.max(0, y1 - y0) <= 1) hidden = 'clipped';
    else if (x1 <= 0 || y1 <= 0 || x0 >= innerWidth || y0 >= innerHeight) hidden = 'off-screen';
  }
  const chain = [el];
  for (let p = el.parentElement; p && p !== document.body && chain.length < 4; p = p.parentElement) chain.push(p);
  S.focused = chain.map((e, i) => ({ e, sig: fullSig(e, i === 0), r: e.getBoundingClientRect() }));
  return { body: false, unit: idOf(composite || el), frame, composite: Boolean(composite), hidden,
    inScope: !S.scope || S.scope === el || S.scope.contains(el), target: el === S.target, xpath: xpathOf(el) };
})()`;

// Blur the element; the crop covers it and every ancestor whose style changed, padded.
const BLUR = `((pad) => {
  ${SIG}
  const S = ${STATE};
  S.el.blur();
  let [x0, y0, x1, y1] = [Infinity, Infinity, -Infinity, -Infinity];
  const cover = (r) => { x0 = Math.min(x0, r.left - pad); y0 = Math.min(y0, r.top - pad); x1 = Math.max(x1, r.right + pad); y1 = Math.max(y1, r.bottom + pad); };
  S.focused.forEach(({ e, sig, r }, i) => {
    const now = fullSig(e, i === 0);
    const changed = sig.some((part, k) => part.some((v, j) => v !== now[k][j]));
    if (i === 0 || changed) cover(r);
  });
  x0 = Math.max(x0, 0); y0 = Math.max(y0, 0); x1 = Math.min(x1, innerWidth); y1 = Math.min(y1, innerHeight);
  return x1 - x0 >= 1 && y1 - y0 >= 1
    ? { x: Math.floor(x0) + scrollX, y: Math.floor(y0) + scrollY, width: Math.ceil(x1) - Math.floor(x0), height: Math.ceil(y1) - Math.floor(y0) }
    : null;
})`;

const REFOCUS = `(() => { const S = ${STATE}; S.el.focus({ preventScroll: true }); return document.activeElement === S.el || S.el.contains(document.activeElement); })()`;

// Focus the target without Tab, scrolled into view as Tab would.
const DIRECT = `(() => { const S = ${STATE}; S.target.focus(); return document.activeElement === S.target || S.target.contains(document.activeElement); })()`;

const capture = async (cdp, crop) => (await cdp.send('Page.captureScreenshot', {
  format: 'png', clip: { ...crop, scale: 1 }, fromSurface: true, captureBeyondViewport: false, optimizeForSpeed: true,
})).data;

// The focused element's padded pixel diff against itself blurred; null when nothing is on screen.
async function measureFocus(page, cdp) {
  const crop = await page.evaluate(`${BLUR}(${CROP_PAD})`);
  if (!crop) {
    await page.evaluate(REFOCUS);
    return null;
  }
  const blurred = await capture(cdp, crop);
  if (!await page.evaluate(REFOCUS)) return null;
  return pixelDiff(await capture(cdp, crop), blurred);
}

// Esc, then Shift+Tab up to once per stop in the loop: does focus get out of it?
async function probeTrap(page, loop) {
  const probes = [];
  const out = (f) => f.body || !loop.has(f.unit);
  await page.keyboard.press('Escape');
  await settleFocus(page);
  let f = await page.evaluate(WHERE);
  probes.push({ key: 'Escape', focus: f.body ? 'body' : f.xpath, left: out(f) });
  for (let i = 0; i < loop.size && !out(f); i++) {
    await page.keyboard.press('Shift+Tab');
    f = await page.evaluate(WHERE);
    probes.push({ key: 'Shift+Tab', focus: f.body ? 'body' : f.xpath, left: out(f) });
  }
  return { left: out(f), probes };
}

async function focus({ page, cdp, kind, xpath }) {
  const layer = await findLayer(cdp, kind);
  const top = layer.scope === 'top' ? layer.xpath : null;
  const tabbable = await page.evaluate(`(${INIT})(${JSON.stringify(top)}, ${JSON.stringify(xpath)})`);
  const limit = Math.max(60, 2 * tabbable + 10);

  let seen = null;
  const look = async (via, f) => {
    seen = { via, hidden: f.hidden, diff: f.frame || f.hidden ? null : await measureFocus(page, cdp) };
  };
  // Tab never reached the target: focus it directly and measure it there.
  const direct = async () => {
    if (await page.evaluate(DIRECT)) await look('direct', await page.evaluate(WHERE));
    else seen = { via: null, hidden: null, diff: null };
  };

  const stops = [];
  const firstVisit = new Map();
  let end = 'limit';
  let loop = null;
  let trap = null;
  for (let presses = 0; presses < limit; presses++) {
    await page.keyboard.press('Tab');
    const f = await page.evaluate(WHERE);
    if (f.body) {
      end = 'body';
      break;
    }
    if (!f.inScope) {
      end = 'exit';
      break;
    }
    const last = stops.at(-1);
    const same = last && last.unit === f.unit && (f.frame || f.composite);
    if (!same && firstVisit.has(f.unit)) {
      end = 'repeat';
      const from = firstVisit.get(f.unit);
      // Inside an open layer, coming back round to the first stop is containment, not a trap.
      if (top && from === 0) break;
      if (!seen) await direct(); // before Esc changes the page
      loop = stops[from].xpath;
      trap = await probeTrap(page, new Set(stops.slice(from).map((s) => s.unit)));
      break;
    }
    if (!same) {
      firstVisit.set(f.unit, stops.length);
      stops.push({ unit: f.unit, xpath: f.xpath });
    }
    if (f.target && !seen) await look('tab', f);
  }
  if (!seen) await direct();
  const visible = seen.hidden === null && seen.diff !== null && seen.diff >= FOCUS_DIFF_FLOOR;
  return {
    visible, diff: seen.diff, floor: FOCUS_DIFF_FLOOR, hidden: seen.hidden, via: seen.via,
    trapped: end === 'limit' ? null : Boolean(trap && !trap.left), end, loop, probes: trap ? trap.probes : [],
  };
}

// ---------------------------------------------------------------------------
// ax: checks/ax.mjs's accessibility tree reading and record template
// ---------------------------------------------------------------------------

async function axNode({ cdp, element }) {
  const { node } = await cdp.send('DOM.describeNode', { objectId: element });
  const { nodes, text } = await axInScope(cdp, null);
  const n = nodes.find((x) => x.backendDOMNodeId === node.backendNodeId);
  if (!n) return { role: '', name: '', states: {}, description: '', template: null, ignored: true };
  const clean = (s) => String(s ?? '').replace(/\s+/g, ' ').trim();
  const role = n.role?.value ?? '';
  const name = clean(n.name?.value);
  const description = clean(n.description?.value);
  const states = statesOf(n, text);
  return { role, name, states, description, template: templateOf(role, name, states, description), ignored: false };
}

// ---------------------------------------------------------------------------
// The probe
// ---------------------------------------------------------------------------

const MEASURE = { contrast, target: targetSize, focus, ax: axNode };

// verdict.py's pass rule for the kind.
function passes(kind, v) {
  if (kind === 'contrast') return v.ratio !== null && v.ratio >= (v.required ?? DEFAULT_RATIO);
  if (kind === 'target') return (v.width >= TARGET_MIN && v.height >= TARGET_MIN) || (v.offset !== null && v.offset >= TARGET_MIN);
  if (kind === 'focus') return v.visible === true && v.trapped === false;
  return null;
}

async function callOn(cdp, objectId, fn) {
  const { result, exceptionDetails } = await cdp.send('Runtime.callFunctionOn', {
    objectId, functionDeclaration: `function () { return (${fn})(this); }`, awaitPromise: true, returnByValue: true,
  });
  if (exceptionDetails) throw new Error(exceptionDetails.exception?.description || exceptionDetails.text);
  return result.value;
}

// Measure on a page that shows the state. The element's xpath is rewritten in the occurrence form.
async function measure(page, cdp, stateId, kind, xpath, kindOfState) {
  const group = 'a11y-probe';
  try {
    const { result } = await cdp.send('Runtime.evaluate', { expression: find(xpath), objectGroup: group });
    if (result.subtype !== 'node' || !result.objectId) throw new ProbeError(`no element at ${xpath} on ${stateId}`);
    const { result: x } = await cdp.send('Runtime.callFunctionOn', {
      objectId: result.objectId, functionDeclaration: `function () { return this.nodeType === 1 ? (${XPATH_FN}).call(this) : null; }`, returnByValue: true,
    });
    if (!x.value) throw new ProbeError(`no element at ${xpath} on ${stateId}: the xpath finds a ${result.description || 'non-element'} node`);
    const env = await page.evaluate(ENV_NOW);
    const value = await MEASURE[kind]({ page, cdp, kind: kindOfState, xpath: x.value, element: result.objectId });
    return { env, value, xpath: x.value, build: await readBuildHash(page) };
  } finally {
    await cdp.send('Runtime.releaseObjectGroup', { objectGroup: group }).catch(() => {});
  }
}

async function inFreshContext(ws, stateId, recipes, run) {
  const browser = await launch();
  try {
    const context = await newContext(browser);
    const page = await context.newPage();
    const tracker = trackRequests(page);
    const cdp = await cdpSession(context, page);
    const r = await replay(page, { cdp, tracker, baseUrl: ws.baseUrl }, stateId, { recipes, policy: ws.policy() });
    if (r.status !== 'reached') {
      throw new ProbeError(`${stateId} was not reached: ${r.status}${r.message ? `: ${r.message}` : ''}`, { status: r.status, step: r.step });
    }
    await injectMotionOff(page);
    return await run(page, cdp);
  } finally {
    await browser.close();
  }
}

// One agent-browser command in the session (as reach.mjs runs them). `quiet` detaches stdio: the
// command that starts a session's daemon would otherwise keep our stdout pipe open.
function ab(session, args, { quiet = false } = {}) {
  const r = spawnSync('agent-browser', ['--session', session, ...args],
    quiet ? { stdio: 'ignore', timeout: 60000 } : { encoding: 'utf8', timeout: 60000 });
  if (r.error?.code === 'ENOENT') throw new Error('agent-browser is not on PATH');
  if (r.error) throw new Error(`agent-browser ${args.join(' ')}: ${r.error.message}`);
  if (r.status !== 0) throw new Error(`agent-browser ${args.join(' ')} exited ${r.status}: ${(r.stderr || r.stdout || '').trim()}`);
  return quiet ? '' : r.stdout.trim();
}

function setEnvironment(session, { quiet = false } = {}) {
  ab(session, ['set', 'viewport', String(ENV.viewport[0]), String(ENV.viewport[1]), String(ENV.dpr)], { quiet });
  ab(session, ['set', 'media', ENV.colorScheme, 'reduced-motion']);
}

async function inSession(session, stateId, recipes, kindOfState, run) {
  setEnvironment(session, { quiet: true });
  const cdpUrl = ab(session, ['get', 'cdp-url']);
  const current = ab(session, ['get', 'url']);
  const browser = await chromium.connectOverCDP(cdpUrl);
  try {
    const context = browser.contexts()[0];
    const page = context?.pages().filter((p) => p.url() === current).at(-1) || context?.pages().at(-1);
    if (!page) throw new ProbeError(`session ${session} has no page: run reach first`);
    await page.emulateMedia({ colorScheme: ENV.colorScheme, reducedMotion: ENV.reducedMotion });
    const cdp = await cdpSession(context, page);
    const expected = chain(recipes, stateId).at(-1).fingerprint;
    if (expected) {
      const m = matchFingerprint(expected, await captureFingerprint(page, cdp, kindOfState));
      if (!m.ok) throw new ProbeError(`session ${session} is not on ${stateId} (${m.why}): run reach ${stateId} again`);
    }
    await injectMotionOff(page);
    return await run(page, cdp);
  } finally {
    await browser.close(); // a CDP connection: this disconnects and leaves the session's browser open
    setEnvironment(session);
  }
}

export async function probe(ws, stateId, kind, xpath, { session = null } = {}) {
  const recipes = loadRecipes(ws);
  chain(recipes, stateId); // throws "no recipe for …" before a browser starts
  const kindOfState = recipes[META].nodes[stateId]?.kind || 'page';
  const run = (page, cdp) => measure(page, cdp, stateId, kind, xpath, kindOfState);
  const m = session ? await inSession(session, stateId, recipes, kindOfState, run) : await inFreshContext(ws, stateId, recipes, run);
  return {
    id: `probe-${sha([stateId, kind, m.xpath, m.value]).slice(0, 12)}`,
    state: stateId, kind, env: m.env, value: m.value, xpath: m.xpath, build: m.build, pass: passes(kind, m.value),
  };
}

async function main(argv) {
  const { ws, values, positionals } = parse(argv, { kind: { type: 'string' }, xpath: { type: 'string' }, session: { type: 'string' } });
  if (positionals[0] === 'status') {
    // probe keeps no files: pending until the run has a states.json, then nothing to count.
    const runId = ws.runId(false);
    console.log(JSON.stringify(runId && existsSync(ws.file('states.json', runId)) ? {} : { pending: true }));
    return 0;
  }
  if (positionals.length !== 1 || !KINDS.includes(values.kind) || !values.xpath) {
    console.error(`usage: probe.mjs <state> --kind ${KINDS.join('|')} --xpath <xpath> [--session <agent-browser session name>]`);
    return 2;
  }
  const [stateId] = positionals;
  try {
    const out = await probe(ws, stateId, values.kind, values.xpath, { session: values.session || null });
    console.log(canonical(out));
    return 0;
  } catch (e) {
    console.error(`probe: ${e.message}`);
    console.log(canonical({ error: e.message, state: stateId, kind: values.kind, xpath: values.xpath, ...(e.detail || {}) }));
    return 1;
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).then((code) => process.exit(code), (e) => {
    console.error(`probe: ${e.message}`);
    console.log(JSON.stringify({ error: e.message }));
    process.exit(1);
  });
}
