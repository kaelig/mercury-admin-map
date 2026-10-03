// The Tab walk (R9, KTD13): real Tab presses from a reset focus, recording every stop.
//
// Scripted failures: a stop whose focus changes fewer than FOCUS_DIFF_FLOOR pixels
// (focus-not-visible, 2.4.7); a stop on an invisible, zero-size, clipped or off-screen element
// (focus-hidden, 2.4.3 and 2.4.7; a skip link that appears on focus is visible while focused, so it
// passes); a revisit before the body that Esc and Shift+Tab cannot leave (keyboard-trap, 2.1.2).
// Candidates for the verifier: expected-interactive elements the walk never reached (not-reached,
// 2.1.1) and stops more than one row band above the previous visible stop (backward-jump), sent with
// a numbered tab-stop overlay.
//
// Focus visibility is a pixel count: the stop's crop, padded by CROP_PAD px, with focus against
// without it (blurred), animations off. The crop also covers each of the three nearest ancestors
// whose computed style changes with focus, so a wrapper's :focus-within ring is in view. That style
// diff explains a result; only pixels decide it. `:focus-visible` is recorded and decides nothing.
//
// The walk ends at the body (`body`), at a revisit (`repeat`), when focus leaves the top layer it
// covers (`exit`), or after max(60, 2 × tabbable + 10) presses (`limit`). Consecutive presses inside
// one iframe, or one composite widget, are one stop with a `count`. Iframe stops are not judged for
// visibility: their content belongs to another document, and cross-origin frames are excluded from
// findings (KTD4).
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { inflateSync } from 'node:zlib';
import { XPATH_FN, describeEvaluated, releaseGroup } from '../lib/occurrence.mjs';
import { customResult } from '../lib/results.mjs';

export const id = 'tabwalk';

// Changed pixels below which a focused stop counts as showing no focus change: a starting value, to
// be tuned on real states.
export const FOCUS_DIFF_FLOOR = 16;
// Padding around the focused element's box, in CSS px, for the focus crop.
const CROP_PAD = 8;

// The walk's in-page state. probe.mjs measures through INIT, INFO, measure() and probeTrap() too.
export const STATE = "window[Symbol.for('a11y.tabwalk')]";

// In-page helpers shared by the expressions below (and by checks/dialog.mjs and checks/status.mjs).
export const PAGE_HELPERS = `
  const deepActive = () => {
    let a = document.activeElement;
    while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
    return !a || a === document.body || a === document.documentElement ? null : a;
  };
  const xpathOf = (n) => (${XPATH_FN}).call(n);
  const labelOf = (e) => (e.getAttribute('aria-label') || e.innerText || e.value || e.title || e.getAttribute('alt') || '')
    .replace(/\\s+/g, ' ').trim().slice(0, 80);
  const tabbableIn = (root) => [...root.querySelectorAll('*')].filter((e) => e.tabIndex >= 0 && !e.disabled
    && !e.closest('[inert]') && e.checkVisibility({ visibilityProperty: true })).length;
`;

// Reset focus to the start of the scope: the top layer's element (focusable for one press), or the
// start of the document. Returns the scope's tabbable count. INFO reports `target` for focus on the
// element at `targetXPath` (the probe's element; the walk passes none).
export const INIT = `(layerXPath, targetXPath = null) => {
  ${PAGE_HELPERS}
  const find = (x) => document.evaluate(x, document, null, 9, null).singleNodeValue;
  const scope = layerXPath ? find(layerXPath) : null;
  const S = ${STATE} = { ids: new WeakMap(), next: 1, el: null, scope, restore: null, focused: null,
    target: targetXPath ? find(targetXPath) : null };
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

// Computed-style signature of an element and its ::before and ::after, for the focus explanation.
const SIG = `
  const SIG_NAMES = ['outline', 'box-shadow', 'border-color', 'border-width', 'background', 'color', 'text-decoration', 'opacity', 'transform', 'filter'];
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

// Where focus is after a press, and the facts a stop records. Stashes the element for describe().
export const INFO = `(() => {
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
    // The visible part: the box cut by its own clip and by every ancestor that clips it, following
    // containing blocks (an absolute box escapes static ancestors; a fixed box escapes all of them).
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
  return {
    body: false, id: idOf(el), unit: idOf(composite || el), frame, composite: composite ? xpathOf(composite) : null,
    inScope: !S.scope || S.scope === el || S.scope.contains(el), hidden, focusVisible: el.matches(':focus-visible'),
    box: [Math.round(r.left + scrollX), Math.round(r.top + scrollY), Math.round(r.width), Math.round(r.height)],
    tag: el.tagName.toLowerCase(), label: labelOf(el), xpath: xpathOf(el), target: el === S.target,
  };
})()`;

// Blur the stop and diff its computed styles; returns the crop (page coordinates, inside the
// viewport) covering the element and every ancestor whose style changed.
const BLUR = `((pad) => {
  ${SIG}
  const S = ${STATE};
  S.el.blur();
  const style = [];
  let [x0, y0, x1, y1] = [Infinity, Infinity, -Infinity, -Infinity];
  const cover = (r) => { x0 = Math.min(x0, r.left - pad); y0 = Math.min(y0, r.top - pad); x1 = Math.max(x1, r.right + pad); y1 = Math.max(y1, r.bottom + pad); };
  S.focused.forEach(({ e, sig, r }, i) => {
    const now = fullSig(e, i === 0);
    let changed = false;
    sig.forEach((part, k) => part.forEach((v, j) => {
      if (v === now[k][j]) return;
      changed = true;
      const name = j < SIG_NAMES.length ? SIG_NAMES[j] : 'content';
      style.push((i ? '^' + i + ' ' : '') + (k ? ['', '::before ', '::after '][k] : '') + name);
    }));
    if (i === 0 || changed) cover(r);
  });
  x0 = Math.max(x0, 0); y0 = Math.max(y0, 0); x1 = Math.min(x1, innerWidth); y1 = Math.min(y1, innerHeight);
  const crop = x1 - x0 >= 1 && y1 - y0 >= 1
    ? { x: Math.floor(x0) + scrollX, y: Math.floor(y0) + scrollY, width: Math.ceil(x1) - Math.floor(x0), height: Math.ceil(y1) - Math.floor(y0) }
    : null;
  return { style: [...new Set(style)].sort(), crop };
})`;

const REFOCUS = `(() => { const S = ${STATE}; S.el.focus({ preventScroll: true }); return document.activeElement === S.el || S.el.contains(document.activeElement); })()`;

// The stop INFO stashed, and the object group that holds its handle until the walk ends.
const CURRENT = `${STATE}.el`;
const WALK_GROUP = 'a11y-tabwalk';

// --- PNG crops ----------------------------------------------------------------------------------

// Decode an 8-bit, non-interlaced PNG (what Chrome's screenshots are) into raw pixel rows.
function decodePng(buf) {
  let pos = 8;
  let width = 0;
  let height = 0;
  let depth = 0;
  let type = 0;
  let interlace = 0;
  const idat = [];
  while (pos < buf.length) {
    const len = buf.readUInt32BE(pos);
    const kind = buf.toString('latin1', pos + 4, pos + 8);
    const data = buf.subarray(pos + 8, pos + 8 + len);
    if (kind === 'IHDR') {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      [depth, type] = [data[8], data[9]];
      interlace = data[12];
    } else if (kind === 'IDAT') idat.push(data);
    else if (kind === 'IEND') break;
    pos += 12 + len;
  }
  const bpp = { 0: 1, 2: 3, 4: 2, 6: 4 }[type];
  if (depth !== 8 || interlace !== 0 || !bpp) throw new Error(`unsupported PNG: depth ${depth}, color type ${type}, interlace ${interlace}`);
  const raw = inflateSync(Buffer.concat(idat));
  const stride = width * bpp;
  const out = Buffer.alloc(stride * height);
  for (let y = 0; y < height; y++) {
    const filter = raw[y * (stride + 1)];
    const src = y * (stride + 1) + 1;
    const row = y * stride;
    for (let x = 0; x < stride; x++) {
      const a = x >= bpp ? out[row + x - bpp] : 0;
      const b = y ? out[row - stride + x] : 0;
      const c = x >= bpp && y ? out[row - stride + x - bpp] : 0;
      let v = raw[src + x];
      if (filter === 1) v += a;
      else if (filter === 2) v += b;
      else if (filter === 3) v += (a + b) >> 1;
      else if (filter === 4) {
        const p = a + b - c;
        const pa = Math.abs(p - a);
        const pb = Math.abs(p - b);
        const pc = Math.abs(p - c);
        v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      }
      out[row + x] = v & 255;
    }
  }
  return { width, height, bpp, data: out };
}

// How many pixels differ between two same-size PNGs (base64, as CDP returns them).
function pixelDiff(a, b) {
  if (a === b) return 0;
  const x = decodePng(Buffer.from(a, 'base64'));
  const y = decodePng(Buffer.from(b, 'base64'));
  if (x.width !== y.width || x.height !== y.height || x.bpp !== y.bpp) return x.width * x.height;
  let n = 0;
  for (let i = 0; i < x.data.length; i += x.bpp) {
    for (let k = 0; k < x.bpp; k++) {
      if (x.data[i + k] !== y.data[i + k]) {
        n++;
        break;
      }
    }
  }
  return n;
}

const capture = async (cdp, crop) => (await cdp.send('Page.captureScreenshot', {
  format: 'png', clip: { ...crop, scale: 1 }, fromSurface: true, captureBeyondViewport: false, optimizeForSpeed: true,
})).data;

// --- Focus helpers shared with checks/dialog.mjs -------------------------------------------------

// Wait until the focused element stays the same for three samples 50 ms apart (at most `max` ms).
export async function settleFocus(page, max = 1000) {
  const probe = `(() => { const a = document.activeElement; const S = window[Symbol.for('a11y.focusid')] ||= { ids: new WeakMap(), next: 1 };
    if (!a) return 0; if (!S.ids.has(a)) S.ids.set(a, S.next++); return S.ids.get(a); })()`;
  const t0 = Date.now();
  let last = await page.evaluate(probe);
  let same = 1;
  while (same < 3 && Date.now() - t0 < max) {
    await sleep(50);
    const now = await page.evaluate(probe);
    same = now === last ? same + 1 : 1;
    last = now;
  }
}

// --- The walk -----------------------------------------------------------------------------------

// A focus position in a trace: "body", or the focused element's tag, label and xpath.
export const ref = (f) => (f.body ? 'body' : { tag: f.tag, label: f.label, xpath: f.xpath });
// An xpath at or below another.
const within = (x, root) => x === root || x.startsWith(root + '/');
const OCCURRENCE_KEYS = ['tag', 'role', 'name', 'classes', 'ancestors', 'landmark', 'heading', 'data', 'xpath', 'rect', 'layer'];
const bare = (o) => Object.fromEntries(OCCURRENCE_KEYS.map((k) => [k, o[k]]));

// The focused stop's padded pixel diff against itself blurred (null when its crop is off screen or
// focus does not come back), and the style properties that changed.
export async function measure(page, cdp) {
  const { style, crop } = await page.evaluate(`${BLUR}(${CROP_PAD})`);
  if (!crop) {
    await page.evaluate(REFOCUS);
    return { style, diff: null };
  }
  const blurred = await capture(cdp, crop);
  const refocused = await page.evaluate(REFOCUS);
  if (!refocused) return { style, diff: null };
  const focused = await capture(cdp, crop);
  return { style, diff: pixelDiff(focused, blurred) };
}

// Esc, then Shift+Tab up to once per stop in the loop: does focus get out of it? `describe` writes
// each probe's focus.
export async function probeTrap(page, loop, describe = ref) {
  const probes = [];
  const out = (f) => f.body || !loop.has(f.unit);
  await page.keyboard.press('Escape');
  await settleFocus(page);
  let f = await page.evaluate(INFO);
  probes.push({ key: 'Escape', focus: describe(f), left: out(f) });
  for (let i = 0; i < loop.size && !out(f); i++) {
    await page.keyboard.press('Shift+Tab');
    f = await page.evaluate(INFO);
    probes.push({ key: 'Shift+Tab', focus: describe(f), left: out(f) });
  }
  return { left: out(f), probes };
}

// Numbered boxes over every stop, saved as one screenshot for the verifier.
async function saveOverlay(page, stops, dir, name, fullPage) {
  await page.evaluate((boxes) => {
    const root = document.createElement('div');
    root.id = 'a11y-tabstops';
    root.style.cssText = 'position:absolute;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none';
    for (const [n, [x, y, w, h]] of boxes) {
      const box = document.createElement('div');
      box.style.cssText = `position:absolute;left:${x}px;top:${y}px;width:${w}px;height:${h}px;outline:2px solid #d00;outline-offset:1px`;
      const tag = document.createElement('div');
      tag.textContent = String(n);
      tag.style.cssText = 'position:absolute;left:-10px;top:-10px;min-width:16px;padding:0 3px;font:bold 12px/16px sans-serif;'
        + 'text-align:center;color:#fff;background:#d00;border-radius:8px';
      box.append(tag);
      root.append(box);
    }
    document.documentElement.append(root);
  }, stops.filter((s) => !s.hidden).map((s) => [s.n, s.box]));
  mkdirSync(dir, { recursive: true });
  await page.screenshot({ path: join(dir, name), fullPage });
  await page.evaluate(() => document.getElementById('a11y-tabstops')?.remove());
}

export async function run(ctx) {
  const { page, cdp } = ctx;
  const top = ctx.layer?.scope === 'top' ? ctx.layer.xpath : null;
  const tabbable = await page.evaluate(`(${INIT})(${JSON.stringify(top)})`);
  const limit = Math.max(60, 2 * tabbable + 10);

  const stops = [];
  const firstVisit = new Map();
  let end = 'limit';
  let presses = 0;
  let trap = null;
  // Set when the walk comes back round to a later stop than the first: the stops past that loop are
  // never visited.
  let partial = false;
  try {
    while (presses < limit) {
      await page.keyboard.press('Tab');
      presses++;
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
      if (last && last.unit === f.unit && (f.frame || f.composite)) {
        last.count++;
        continue;
      }
      if (firstVisit.has(f.unit)) {
        end = 'repeat';
        const from = firstVisit.get(f.unit);
        // Inside an open layer, coming back round to the first stop is containment, which a modal does
        // by design: its close control is the way out (2.1.2), and the dialog check tests that.
        if (top && from === 0) break;
        partial = from > 0;
        const loop = new Set(stops.slice(from).map((s) => s.unit));
        const probe = await probeTrap(page, loop);
        if (!probe.left) trap = { stop: stops[from], probes: probe.probes };
        break;
      }
      firstVisit.set(f.unit, stops.length);
      // Described now, not after the walk: `rect` is in viewport coordinates and the page scrolls.
      const occurrence = await describeEvaluated(cdp, CURRENT, WALK_GROUP, { layerXPath: top });
      const stop = {
        n: stops.length + 1, occurrence, box: f.box, hidden: f.hidden, focusVisible: f.focusVisible,
        style: [], diff: null, count: 1, frame: f.frame, composite: f.composite, unit: f.unit,
      };
      if (!f.frame && !f.hidden) Object.assign(stop, await measure(page, cdp));
      stops.push(stop);
    }
  } finally {
    await releaseGroup(cdp, WALK_GROUP);
  }

  const results = [];
  const count = stops.length;
  const visible = stops.filter((s) => !s.hidden);
  const previousOf = (s) => stops[s.n - 2]?.occurrence ?? null;
  for (const s of stops) {
    if (s.hidden) {
      results.push(customResult(id, 'focus-hidden', 'failure', s.occurrence,
        { stop: s.n, stops: count, previous: previousOf(s), focus: s.occurrence, hidden: s.hidden, box: s.box }));
    } else if (!s.frame && s.diff !== null && s.diff < FOCUS_DIFF_FLOOR) {
      results.push(customResult(id, 'focus-not-visible', 'failure', s.occurrence,
        { stop: s.n, stops: count, previous: previousOf(s), diff: s.diff, floor: FOCUS_DIFF_FLOOR, style: s.style, focusVisible: s.focusVisible }));
    }
  }

  if (trap) {
    // Every stop in order, then the revisit that closed the loop, then the Esc and Shift+Tab probes.
    const trace = [...stops, trap.stop].map((s) => ({ n: s.n, role: s.occurrence.role, name: s.occurrence.name, xpath: s.occurrence.xpath }));
    results.push(customResult(id, 'keyboard-trap', 'failure', trap.stop.occurrence, { stop: trap.stop.n, stops: count, trace, probes: trap.probes }));
  }

  // A stop more than one row band (the taller of the two boxes) above the previous visible stop.
  const jumps = [];
  for (let i = 1; i < visible.length; i++) {
    const [prev, s] = [visible[i - 1], visible[i]];
    const band = Math.max(prev.box[3], s.box[3], 1);
    const rise = prev.box[1] - s.box[1];
    if (rise > band) jumps.push({ s, prev, rise, band });
  }
  let overlay = null;
  if (jumps.length && ctx.shotsDir) {
    overlay = `${ctx.state || 'state'}.tabstops.png`;
    await saveOverlay(page, stops, ctx.shotsDir, overlay, !top);
  }
  for (const { s, prev, rise, band } of jumps) {
    results.push(customResult(id, 'backward-jump', 'candidate', s.occurrence, { stop: s.n, stops: count, previous: prev.occurrence, rise, band, overlay }));
  }

  // Not reached: only a walk that went all the way round (no trap, no press limit) can say so.
  // Disabled controls are out of the Tab order by design.
  if (!trap && end !== 'limit') {
    const reached = (o) => stops.some((s) => within(o.xpath, s.occurrence.xpath) || within(s.occurrence.xpath, o.xpath)
      || (s.composite && within(o.xpath, s.composite)));
    for (const o of ctx.interactive || []) {
      if (o.disabled || (top && o.layer !== 'top')) continue;
      if (reached(o)) continue;
      results.push(customResult(id, 'not-reached', 'candidate', bare(o), { stops: count, end, focusable: o.focusable ?? null, source: o.source ?? null }));
    }
  }

  const detail = {
    stops: stops.map((s) => ({
      n: s.n, occurrence: s.occurrence, box: s.box, visible: !s.hidden, hidden: s.hidden, focusVisible: s.focusVisible,
      style: s.style, diff: s.diff, count: s.count, frame: s.frame, composite: s.composite,
    })),
    end,
    presses,
  };
  // A walk cut off at the press limit, held by a trap, or sent round a loop that starts past its first
  // stop did not cover the scope: a finding it does not report is not shown to be fixed.
  return { results, records: [], detail, completed: !trap && !partial && end !== 'limit' };
}
