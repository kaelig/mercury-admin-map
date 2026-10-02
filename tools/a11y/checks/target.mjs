// Target size (R11, WCAG 2.5.8): every expected-interactive element measured against 24x24 CSS px,
// with the spacing and inline exceptions, next to axe's own target-size verdict for the same element.
//
// A target under 24x24 passes when a 24 px circle centered on its bounding box intersects no other
// target and no other undersized target's circle, or when it is inline in a sentence. `offset` is
// the diameter of the largest clear circle, computed as axe computes `closestOffset`.
//
// basis `failure`: the own measurement fails and axe agrees. basis `candidate` (for the verifier):
// the two disagree, or the target is a native control whose size the user agent may set.
import { XPATH_FN } from '../lib/occurrence.mjs';
import { sortCanonical } from '../lib/canon.mjs';
import { injectAxe } from './axe.mjs';

export const id = 'target';

const MIN = 24;
const EPS = 0.01; // layout rounding: circles that touch do not intersect

// Runs in the page. Measures each target, then asks axe's target-size rule alone for a verdict on
// every node it considers (the main axe run keeps only violations and incomplete nodes).
const MEASURE = `async (xpaths, layerXPath) => {
  const xpathOf = (n) => (${XPATH_FN}).call(n);
  const find = (x) => document.evaluate(x, document, null, 9, null).singleNodeValue;
  const els = xpaths.map(find);
  const targets = new Set(els.filter(Boolean));
  const UA = 'input[type=checkbox], input[type=radio], input[type=range], input[type=color], input[type=file],'
    + ' input[type=date], input[type=time], input[type=datetime-local], input[type=month], input[type=week], select';
  const blockOf = (e) => {
    let p = e.parentElement;
    while (p && getComputedStyle(p).display.startsWith('inline')) p = p.parentElement;
    return p || document.body;
  };
  // Inline in a sentence: an inline box whose block also holds text outside every target.
  const inSentence = (e) => {
    const block = blockOf(e);
    const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
    for (let t = walker.nextNode(); t; t = walker.nextNode()) {
      if (!t.nodeValue.trim()) continue;
      let p = t.parentElement;
      while (p && p !== block && !targets.has(p)) p = p.parentElement;
      if (!p || p === block) return true;
    }
    return false;
  };
  const measured = els.map((e) => {
    if (!e) return null;
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    return { rect: [r.x, r.y, r.width, r.height], inline: s.display === 'inline' && inSentence(e), ua: e.matches(UA) && s.appearance !== 'none' };
  });
  const context = layerXPath ? find(layerXPath) : document;
  const r = await axe.run(context, { runOnly: { type: 'rule', values: ['target-size'] }, elementRef: true,
    selectors: false, ancestry: false, xpath: false, iframes: false });
  const verdict = {};
  for (const [type, name] of [['passes', 'pass'], ['incomplete', 'incomplete'], ['violations', 'violation']]) {
    for (const rule of r[type]) for (const n of rule.nodes) if (n.element) verdict[xpathOf(n.element)] = name;
  }
  return { measured, verdict };
}`;

const round2 = (v) => Math.round(v * 100) / 100;
const rendered = ([x, y, w, h]) => w * h > 1 && x + w > 0 && y + h > 0;
const related = (a, b) => a.startsWith(b + '/') || b.startsWith(a + '/');

function distanceToBox(px, py, b) {
  const dx = Math.max(b.x - px, 0, px - (b.x + b.w));
  const dy = Math.max(b.y - py, 0, py - (b.y + b.h));
  return Math.hypot(dx, dy);
}

// Half the clear diameter: the distance from a's center to the nearest other target's box, or to
// an undersized target's circle. Targets nested in one another (a listbox and its options) don't count.
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

export async function run(ctx) {
  const { cdp, layer } = ctx;
  const targets = ctx.interactive || [];
  await injectAxe(cdp);
  const { result, exceptionDetails } = await cdp.send('Runtime.evaluate', {
    expression: `(${MEASURE})(${JSON.stringify(targets.map((t) => t.xpath))}, ${JSON.stringify(layer?.xpath ?? null)})`,
    awaitPromise: true, returnByValue: true,
  });
  if (exceptionDetails) throw new Error(`target measurement failed: ${exceptionDetails.exception?.description || exceptionDetails.text}`);
  const { measured, verdict } = result.value;

  const boxes = [];
  targets.forEach((t, i) => {
    const m = measured[i];
    if (!m || !rendered(m.rect)) return;
    const [x, y, w, h] = m.rect;
    boxes.push({ t, m, xpath: t.xpath, x, y, w, h, cx: x + w / 2, cy: y + h / 2, small: w < MIN - EPS || h < MIN - EPS });
  });

  const results = [];
  for (const b of boxes) {
    const radius = clearRadius(b, boxes);
    const spaced = radius >= MIN / 2 - EPS / 2;
    const passedBy = !b.small ? 'size' : spaced ? 'spacing' : b.m.inline ? 'inline' : null;
    const fails = passedBy === null;
    const axe = verdict[b.xpath] || 'inapplicable';
    const agree = fails === (axe === 'violation');
    if (!fails && axe !== 'violation') continue;
    const { focusable, source, ...occurrence } = b.t;
    results.push({
      check: 'target', rule: 'target-size-own', basis: fails && agree && !b.m.ua ? 'failure' : 'candidate',
      criteria: ['2.5.8'], messageKey: null, occurrence,
      evidence: {
        width: round2(b.w), height: round2(b.h), offset: Number.isFinite(radius) ? round2(2 * radius) : null,
        own: fails ? 'fail' : 'pass', passedBy, axe, agree, exception: b.m.ua ? 'ua-control' : null,
      },
    });
  }
  return { results: sortCanonical(results), records: [], detail: { measured: boxes.length }, completed: true };
}
