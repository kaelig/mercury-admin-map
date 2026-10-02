// The status observer (R25, KTD13): where a toast, confirmation or validation message appears, and
// whether screen readers hear it.
//
// arm(page) runs before the state's final hop. It records the live regions present then
// (role=status|alert|log, aria-live other than off), the elements and text visible then, and installs
// a MutationObserver and a focusin listener. Both live in the current document only: a full
// navigation after arm() drops them, and run() then reports the family incomplete.
//
// run(ctx) collects. A message is an element that became visible after arm() with text that was not
// visible before, outermost first, that is not a dialog, menu or listbox and holds no form field;
// on a toast state with a top layer, only messages in that layer count.
//   status-not-announced (4.1.3): a message outside every live region that existed before it
//     appeared, and not focused (focus never moved into it, nor onto a field whose
//     aria-describedby or aria-errormessage points at it).
//   error-not-associated (3.3.1): a visible aria-invalid=true field whose aria-describedby and
//     aria-errormessage do not reach the message within three ancestors of it.
// Message wording goes into the evidence for the verifier. If a message is gone (removed or hidden)
// before run() describes it, run() returns completed: false. present(page) tells the sweep whether
// the message is still on screen, so it can list a later family that finished without it as
// incomplete.
import { join } from 'node:path';
import { ROOT, readJson } from '../lib/canon.mjs';
import { describeElements } from '../lib/occurrence.mjs';
import { PAGE_HELPERS } from './tabwalk.mjs';

export const id = 'status';

const CRITERIA = readJson(join(ROOT, 'tools/a11y/checks.json')).customCriteria;
const STATE = "window[Symbol.for('a11y.status')]";

const COMMON = `
  const REGION = '[role=status],[role=alert],[role=log],[aria-live]:not([aria-live=off])';
  const shown = (e) => e.isConnected && e.checkVisibility({ visibilityProperty: true }) && e.getClientRects().length > 0;
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
`;

const ARM = `(() => {
  ${PAGE_HELPERS}
  ${COMMON}
  const S = ${STATE} = { regions: new WeakSet(document.querySelectorAll(REGION)), shownBefore: new WeakSet(),
    textBefore: new Set(), found: [], focus: [], primary: null };
  for (const e of document.querySelectorAll('body *')) if (shown(e)) S.shownBefore.add(e);
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    if (n.parentElement && S.shownBefore.has(n.parentElement) && norm(n.nodeValue)) S.textBefore.add(norm(n.nodeValue));
  }
  // The live region a message sits in: the nearest one that existed before, else the nearest one.
  const regionOf = (el) => {
    let nearest = null;
    for (let r = el.closest(REGION); r; r = r.parentElement && r.parentElement.closest(REGION)) {
      const role = ['status', 'alert', 'log'].includes(r.getAttribute('role')) ? r.getAttribute('role') : null;
      const live = r.getAttribute('aria-live') || (role === 'alert' ? 'assertive' : 'polite');
      const info = { role, live, preexisting: S.regions.has(r) };
      if (info.preexisting) return info;
      nearest = nearest || info;
    }
    return nearest;
  };
  // Facts captured while the element is on screen, so a message that vanishes keeps them.
  const look = (m) => {
    if (!shown(m.el)) return;
    const texts = [];
    const w = document.createTreeWalker(m.el, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) if (norm(n.nodeValue)) texts.push(norm(n.nodeValue));
    Object.assign(m, { seen: true, text: norm(m.el.innerText).slice(0, 300), fresh: texts.some((t) => !S.textBefore.has(t)),
      region: regionOf(m.el), xpath: xpathOf(m.el) });
  };
  const note = (el, why, touched) => {
    if (!el || el.nodeType !== 1 || el === document.body || el === document.documentElement) return;
    if (why === 'attr' && S.shownBefore.has(el)) return;
    let m = S.found.find((x) => x.el === el);
    if (!m) S.found.push(m = { el, seen: false });
    touched.add(m);
  };
  S.look = look;
  S.regionOf = regionOf;
  new MutationObserver((list) => {
    const touched = new Set();
    for (const r of list) {
      if (r.type === 'childList') for (const n of r.addedNodes) note(n.nodeType === 1 ? n : n.parentElement, 'added', touched);
      else if (r.type === 'characterData') note(r.target.parentElement, 'text', touched);
      else note(r.target, 'attr', touched);
    }
    touched.forEach(look);
  }).observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true,
    attributeFilter: ['hidden', 'style', 'class', 'aria-hidden', 'open'] });
  document.addEventListener('focusin', (e) => S.focus.push(e.composedPath()[0]), true);
})()`;

// Decide each message and each invalid field. Stashes the elements to describe in S.out.
const COLLECT = `((layerXPath) => {
  ${PAGE_HELPERS}
  ${COMMON}
  const S = ${STATE};
  if (!S) return null;
  const within = (x, root) => x === root || x.startsWith(root + '/');
  S.found.forEach(S.look);
  const NOT_MESSAGE = '[role=dialog],[role=alertdialog],dialog,[role=menu],[role=listbox],input,select,textarea,[contenteditable]';
  let msgs = S.found.filter((m) => m.seen && m.fresh && m.text && !m.el.matches(NOT_MESSAGE) && !m.el.querySelector(NOT_MESSAGE));
  msgs = msgs.filter((m) => !msgs.some((o) => o !== m && o.el.contains(m.el)));
  if (layerXPath) msgs = msgs.filter((m) => within(m.xpath, layerXPath) || within(layerXPath, m.xpath));
  const order = (a, b) => (a.isConnected && b.isConnected ? (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1) : 0);
  msgs.sort((a, b) => order(a.el, b.el));
  const refs = (e, attr) => norm(e.getAttribute(attr)).split(' ').filter(Boolean).map((id) => document.getElementById(id)).filter(Boolean);
  const touches = (a, b) => a === b || a.contains(b) || b.contains(a);
  const focusedOn = (el) => S.focus.some((t) => t && el.contains(t))
    || S.focus.some((t) => t && t.nodeType === 1 && [...refs(t, 'aria-describedby'), ...refs(t, 'aria-errormessage')].some((r) => touches(r, el)));
  const messages = msgs.map((m) => ({ message: m.text, region: m.region, focused: focusedOn(m.el), present: shown(m.el), xpath: m.xpath }));
  const fields = [...document.querySelectorAll('[aria-invalid=true]')].filter(shown);
  const errors = fields.map((f) => {
    const described = refs(f, 'aria-describedby');
    const errorRefs = refs(f, 'aria-errormessage');
    let near = null;
    for (let p = f.parentElement, d = 1; p && d <= 3 && !near; p = p.parentElement, d++) {
      near = msgs.find((m) => m.el.isConnected && p.contains(m.el) && !m.el.contains(f)) || null;
    }
    const all = [...described, ...errorRefs];
    const tied = near ? all.some((r) => touches(r, near.el)) || all.map((r) => norm(r.textContent)).join(' ').includes(near.text) : null;
    return { message: near ? near.text : null, tied, describedBy: described.map((r) => norm(r.textContent)).join(' '),
      errorMessage: errorRefs.map((r) => norm(r.textContent)).join(' '), xpath: xpathOf(f) };
  });
  S.primary = msgs.length ? msgs[0].el : null;
  S.out = { messages: msgs.map((m) => (shown(m.el) ? m.el : null)), fields };
  return { messages, errors };
})`;

const PRESENT = `(() => {
  ${COMMON}
  const S = ${STATE};
  return !S || !S.primary || shown(S.primary);
})()`;

export function arm(page) {
  return page.evaluate(ARM);
}

export function present(page) {
  return page.evaluate(PRESENT);
}

const result = (rule, occurrence, evidence) => ({ check: id, rule, basis: 'failure', criteria: CRITERIA[rule], messageKey: null, occurrence, evidence });

export async function run(ctx) {
  const { page, cdp } = ctx;
  const layerXPath = ctx.kind === 'toast' && ctx.layer?.scope === 'top' ? ctx.layer.xpath : null;
  const seen = await page.evaluate(`${COLLECT}(${JSON.stringify(layerXPath)})`);
  if (!seen) {
    return { results: [], records: [], detail: { message: null, region: null, focused: false, present: false, messages: [], errors: [] }, completed: false };
  }
  const scope = ctx.layer?.scope === 'top' ? ctx.layer.xpath : null;
  const msgOccs = await describeElements(cdp, `() => ${STATE}.out.messages`, null, { layerXPath: scope });
  const fieldOccs = await describeElements(cdp, `() => ${STATE}.out.fields`, null, { layerXPath: scope });
  const [focus] = await describeElements(cdp, `() => { let a = document.activeElement;
    while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
    return [a === document.body || a === document.documentElement ? null : a]; }`, null, { layerXPath: scope });

  const results = [];
  seen.messages.forEach((m, i) => {
    if (!msgOccs[i] || (m.region && m.region.preexisting) || m.focused) return;
    results.push(result('status-not-announced', msgOccs[i], { message: m.message, region: m.region, focused: false, focus: focus || 'body' }));
  });
  seen.errors.forEach((e, i) => {
    if (e.tied !== false) return;
    results.push(result('error-not-associated', fieldOccs[i], { message: e.message, describedBy: e.describedBy, errorMessage: e.errorMessage }));
  });

  const first = seen.messages[0] || null;
  const detail = {
    message: first ? first.message : null,
    region: first ? first.region : null,
    focused: first ? first.focused : false,
    present: first ? first.present : false,
    messages: seen.messages,
    errors: seen.errors.map((e, i) => ({ ...e, name: fieldOccs[i]?.name ?? '' })),
  };
  return { results, records: [], detail, completed: seen.messages.every((m) => m.present) };
}
