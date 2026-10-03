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
import { chromium } from 'playwright';
import { ENV, cdpSession, injectMotionOff, launch, newContext, readBuildHash, trackRequests } from './lib/browser.mjs';
import { canonical, sha } from './lib/canon.mjs';
import { parse, runMain } from './lib/cli.mjs';
import { XPATH_FN, findExpr } from './lib/occurrence.mjs';
import { findLayer } from './lib/scope.mjs';
import { META, chain, loadRecipes, replay } from './recipe.mjs';
import { ab, setEnvironment } from './reach.mjs';
import { captureFingerprint, matchFingerprint } from './fingerprint.mjs';
import { injectAxe } from './checks/axe.mjs';
import { axInScope, readNode } from './checks/ax.mjs';
import * as interactive from './checks/interactive.mjs';
import { MIN, boxOf, clearRadius, offsetOf, rendered, round2 } from './checks/target.mjs';
import { FOCUS_DIFF_FLOOR, INFO, INIT, STATE, measure as measureFocus, probeTrap } from './checks/tabwalk.mjs';

const KINDS = ['contrast', 'target', 'focus', 'ax'];
// verdict.py's pass threshold for contrast when axe states no required ratio. The target-size one is
// checks/target.mjs's MIN.
const DEFAULT_RATIO = 4.5;

class ProbeError extends Error {
  constructor(message, detail = {}) {
    super(message);
    this.detail = detail;
  }
}

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
// target: checks/target.mjs's spacing computation
// ---------------------------------------------------------------------------

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
    const box = boxOf(x, rect);
    if (x === xpath) own = box;
    if (rendered(rect)) boxes.push(box);
  });
  const radius = clearRadius(own, boxes);
  return { width: round2(own.w), height: round2(own.h), offset: offsetOf(radius) };
}

// ---------------------------------------------------------------------------
// focus: checks/tabwalk.mjs's reset, hidden test, padded pixel diff and trap probes, with the probe's
// own walk to the element
// ---------------------------------------------------------------------------

// Focus the target without Tab, scrolled into view as Tab would.
const DIRECT = `(() => { const S = ${STATE}; S.target.focus(); return document.activeElement === S.target || S.target.contains(document.activeElement); })()`;

// A trap probe's focus: "body" or the focused element's xpath.
const focusXPath = (f) => (f.body ? 'body' : f.xpath);

async function focus({ page, cdp, kind, xpath }) {
  const layer = await findLayer(cdp, kind);
  const top = layer.scope === 'top' ? layer.xpath : null;
  const tabbable = await page.evaluate(`(${INIT})(${JSON.stringify(top)}, ${JSON.stringify(xpath)})`);
  const limit = Math.max(60, 2 * tabbable + 10);

  let seen = null;
  const look = async (via, f) => {
    seen = { via, hidden: f.hidden, diff: f.frame || f.hidden ? null : (await measureFocus(page, cdp)).diff };
  };
  // Tab never reached the target: focus it directly and measure it there.
  const direct = async () => {
    if (await page.evaluate(DIRECT)) await look('direct', await page.evaluate(INFO));
    else seen = { via: null, hidden: null, diff: null };
  };

  const stops = [];
  const firstVisit = new Map();
  let end = 'limit';
  let loop = null;
  let trap = null;
  for (let presses = 0; presses < limit; presses++) {
    await page.keyboard.press('Tab');
    const f = await page.evaluate(INFO);
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
      trap = await probeTrap(page, new Set(stops.slice(from).map((s) => s.unit)), focusXPath);
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
  return { ...readNode(n, text), ignored: false };
}

// ---------------------------------------------------------------------------
// The probe
// ---------------------------------------------------------------------------

const MEASURE = { contrast, target: targetSize, focus, ax: axNode };

// verdict.py's pass rule for the kind.
function passes(kind, v) {
  if (kind === 'contrast') return v.ratio !== null && v.ratio >= (v.required ?? DEFAULT_RATIO);
  if (kind === 'target') return (v.width >= MIN && v.height >= MIN) || (v.offset !== null && v.offset >= MIN);
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
    const { result } = await cdp.send('Runtime.evaluate', { expression: findExpr(xpath), objectGroup: group });
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

async function probe(ws, stateId, kind, xpath, { session = null } = {}) {
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
    console.log(JSON.stringify(ws.hasStates(ws.runId(false)) ? {} : { pending: true }));
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

runMain(import.meta.url, 'probe', main);
