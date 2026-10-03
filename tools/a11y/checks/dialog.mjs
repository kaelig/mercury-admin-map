// The dialog check (R10, KTD13): from a fresh context at the parent state, Tab to the trigger, open
// it with Enter, then Space if nothing opened, and assert the pattern's focus rule:
//
//   modal or drawer: focus moves inside (dialog-focus-not-moved); for a modal layer, Tab stays inside
//     (dialog-focus-escaped); when Esc or the close control closes it, focus returns to the trigger
//     (dialog-focus-not-returned). A modal that ignores Esc but closes from its close control
//     produces nothing; one that neither closes produces nothing here either.
//   menu: focus lands on the first item (menu-focus-not-moved); Esc returns it to the button.
//   popover: focus moves inside, or the next Tab lands inside (popover-not-reachable), when it holds
//     anything focusable; if Esc closes it with focus inside, focus returns to the trigger.
//   a popover reached by hover must appear on keyboard focus, or open with Enter or Space
//     (hover-only); a click trigger that neither opens is dialog-trigger-not-operable, and one Tab
//     never reaches is dialog-trigger-not-focusable.
//
// Every failure carries the whole focus trace. `ctx.reopen()` gives the fresh context, which this
// check closes; `ctx.trigger(page)` gives the trigger's Locator; `ctx.finalHop` (the recipe's final
// step, e.g. {"hover": {...}}) says whether the state is reached by hover.
import { settle } from '../lib/browser.mjs';
import { BY_XPATH, describeElements, findExpr } from '../lib/occurrence.mjs';
import { customResult } from '../lib/results.mjs';
import { armLayer, findLayer } from '../lib/scope.mjs';
import { PAGE_HELPERS, ref, settleFocus } from './tabwalk.mjs';

export const id = 'dialog';

const KEY = 'a11y.dialog';
const STATE = `window[Symbol.for('${KEY}')]`;

// Where focus is: on the trigger, inside the opened layer, outside it, or on the body.
const WHERE = `(() => {
  ${PAGE_HELPERS}
  const D = ${STATE};
  const el = deepActive();
  D.el = el;
  if (!el) return { body: true, where: 'body' };
  if (!D.ids.has(el)) D.ids.set(el, D.next++);
  const t = D.trigger;
  const inLayer = D.layer && (D.layer === el || D.layer.contains(el));
  const where = el === t ? 'trigger' : inLayer ? 'inside' : t && (el.contains(t) || t.contains(el)) ? 'trigger' : 'outside';
  return { body: false, id: D.ids.get(el), where, frame: el.tagName === 'IFRAME' || el.tagName === 'FRAME',
    tag: el.tagName.toLowerCase(), label: labelOf(el), xpath: xpathOf(el) };
})()`;

const TABBABLE_IN_LAYER = `(() => { ${PAGE_HELPERS} return tabbableIn(${STATE}.layer); })()`;
const TABBABLE_IN_PAGE = `(() => { ${PAGE_HELPERS} return tabbableIn(document.body); })()`;
const CLOSED = `(() => { const l = ${STATE}.layer; return !l.isConnected || !l.checkVisibility({ opacityProperty: true, visibilityProperty: true }); })()`;
const MODAL = `(() => { const l = ${STATE}.layer; const role = l.getAttribute('role') || (l.tagName === 'DIALOG' ? 'dialog' : '');
  return ['dialog', 'alertdialog'].includes(role) && (l.getAttribute('aria-modal') === 'true' || l.matches(':modal')); })()`;
// Focus on the first menu item, directly or as the active descendant.
const ON_FIRST_ITEM = `(() => {
  ${PAGE_HELPERS}
  const items = [...${STATE}.layer.querySelectorAll('[role=menuitem],[role=menuitemcheckbox],[role=menuitemradio],[role=option]')]
    .filter((e) => e.checkVisibility({ visibilityProperty: true }));
  const a = deepActive();
  if (!items.length || !a) return false;
  return a === items[0] || (!!items[0].id && a.getAttribute('aria-activedescendant') === items[0].id);
})()`;
// Focus the layer's close control (a button named Close, Cancel, Dismiss or Done, or ×). Returns
// whether one exists.
const FOCUS_CLOSE = `(() => {
  const name = (e) => (e.getAttribute('aria-label') || e.textContent || '').replace(/\\s+/g, ' ').trim();
  const c = [...${STATE}.layer.querySelectorAll('button, [role=button], a[href]')]
    .find((e) => /^(close|cancel|dismiss|done)\\b|^[×✕x]$/i.test(name(e)) && e.checkVisibility({ visibilityProperty: true }));
  if (!c) return false;
  c.focus();
  return true;
})()`;
const SET_LAYER = (xpath) => `(${STATE}.layer = ${findExpr(xpath)}, true)`;

export async function run(ctx) {
  const verb = ctx.finalHop && 'hover' in ctx.finalHop ? 'hover' : 'click';
  const session = await ctx.reopen();
  try {
    return await check(ctx, session, verb);
  } finally {
    await session.context.close();
  }
}

async function check(ctx, { page, cdp, tracker }, verb) {
  const { kind } = ctx;
  const trace = [];
  const results = [];
  let layerXPath = null;

  const marked = await ctx.trigger(page).evaluate((el, key) => {
    window[Symbol.for(key)] = { trigger: el, layer: null, el: null, ids: new WeakMap(), next: 1 };
    return true;
  }, KEY, { timeout: 5000 }).catch(() => false);
  if (!marked) return { results, records: [], detail: { trigger: null, opened: null, trace }, completed: false };
  const [trigger] = await describeElements(cdp, `() => [${STATE}.trigger]`);

  const where = () => page.evaluate(WHERE);
  // Append a focus step; consecutive Tab presses inside one iframe are one step with a count.
  const note = (key, f) => {
    const last = trace.at(-1);
    if (key === 'Tab' && f.frame && last && last.key === 'Tab' && last.focus.xpath === f.xpath) {
      last.count = (last.count || 1) + 1;
      return f;
    }
    trace.push({ key, where: f.where, focus: ref(f) });
    return f;
  };
  const press = async (key) => {
    await page.keyboard.press(key);
    return note(key, await where());
  };
  // The focused element as an occurrence (call right after where()), or "body".
  const focusOf = async (f) => (f.body ? 'body'
    : (await describeElements(cdp, `() => [${STATE}.el]`, null, { layerXPath }))[0]);
  const fail = (rule, occurrence, evidence) => results.push(customResult(id, rule, 'failure', occurrence, { ...evidence, trace }));
  const finish = (opened) => {
    const stops = new Set(trace.filter((s) => s.key === 'Tab' && s.focus !== 'body').map((s) => s.focus.xpath)).size;
    for (const r of results) r.evidence.stops = stops;
    return { results, records: [], detail: { trigger, opened, trace }, completed: true };
  };

  // 1. Tab until the trigger has focus: give up at a second pass through the body, a revisit, or
  //    max(60, 2 × tabbable + 10) presses.
  const limit = Math.max(60, 2 * await page.evaluate(TABBABLE_IN_PAGE) + 10);
  let before = { scope: 'document', xpath: null };
  if (verb === 'hover') {
    await armLayer(page);
    before = await findLayer(cdp, kind);
  }
  let f = note('start', await where());
  const seen = new Set(f.body ? [] : [f.id]);
  let bodies = 0;
  let reached = false;
  let lastFrame = null;
  for (let i = 0; i < limit; i++) {
    f = await press('Tab');
    if (f.where === 'trigger') {
      reached = true;
      break;
    }
    if (f.body) {
      if (++bodies > 1) break;
      continue;
    }
    if (f.frame && f.id === lastFrame) continue;
    lastFrame = f.frame ? f.id : null;
    if (seen.has(f.id)) break;
    seen.add(f.id);
  }
  if (!reached) {
    fail('dialog-trigger-not-focusable', trigger, { focus: await focusOf(f) });
    return finish(null);
  }

  // 2. Open it: keyboard focus alone for a hover popover, then Enter, then Space.
  let layer = null;
  let opened = null;
  const openedLayer = async () => {
    const now = await findLayer(cdp, kind);
    return now.scope === 'top' && !(before.scope === 'top' && before.xpath === now.xpath) ? now : null;
  };
  if (verb === 'hover') {
    await settle(page, cdp, { tracker });
    layer = await openedLayer();
    if (layer) opened = 'focus';
  }
  for (const key of ['Enter', 'Space']) {
    if (layer) break;
    if ((await where()).where !== 'trigger') {
      await page.evaluate(`${STATE}.trigger.focus()`);
      note('focus', await where());
    }
    await armLayer(page);
    before = await findLayer(cdp, kind);
    await press(key);
    await settle(page, cdp, { tracker });
    layer = await openedLayer();
    if (layer) opened = key.toLowerCase();
  }
  if (!layer) {
    f = await where();
    fail(verb === 'hover' ? 'hover-only' : 'dialog-trigger-not-operable', trigger, { focus: await focusOf(f) });
    return finish(null);
  }
  layerXPath = layer.xpath;
  await page.evaluate(SET_LAYER(layerXPath));
  const [overlay] = await describeElements(cdp, BY_XPATH, [layerXPath], { layerXPath });
  await settleFocus(page);
  const atOpen = note('settle', await where());
  const focusAtOpen = await focusOf(atOpen);

  // Esc, then the close control; when it closes, focus must be back on the trigger.
  const closeAndReturn = async ({ control }) => {
    await press('Escape');
    await settleFocus(page);
    let closedBy = (await page.evaluate(CLOSED)) ? 'escape' : null;
    if (!closedBy && control && await page.evaluate(FOCUS_CLOSE)) {
      note('close', await where());
      await press('Enter');
      await settleFocus(page);
      if (await page.evaluate(CLOSED)) closedBy = 'close';
    }
    if (!closedBy) return;
    const back = note('settle', await where());
    if (back.where !== 'trigger') fail('dialog-focus-not-returned', trigger, { focus: await focusOf(back), closedBy });
  };

  if (kind === 'menu') {
    // Open the closed menu again the way it first opened, and make the layer it opens the one WHERE tests.
    const reopenMenu = async () => {
      if ((await where()).where !== 'trigger') await page.evaluate(`${STATE}.trigger.focus()`);
      if (opened !== 'focus') await press(opened === 'enter' ? 'Enter' : 'Space');
      await settleFocus(page);
      const again = await findLayer(cdp, kind);
      if (again.scope === 'top') await page.evaluate(SET_LAYER(again.xpath));
    };
    const onFirst = await page.evaluate(ON_FIRST_ITEM);
    await closeAndReturn({ control: false });
    if (!onFirst) {
      // Reopen it the same way, then press Tab once: does focus reach the menu at all?
      if (await page.evaluate(CLOSED)) await reopenMenu();
      const next = await press('Tab');
      fail('menu-focus-not-moved', overlay, { focus: focusAtOpen, nextTabInside: next.where === 'inside' });
    }
  } else if (kind === 'popover') {
    let inside = atOpen.where === 'inside';
    if (!inside && await page.evaluate(TABBABLE_IN_LAYER) > 0) {
      const next = await press('Tab');
      inside = next.where === 'inside';
      if (!inside) fail('popover-not-reachable', overlay, { focus: await focusOf(next) });
    }
    if (inside) await closeAndReturn({ control: false });
  } else {
    let reachedLayer = atOpen.where === 'inside';
    if (!reachedLayer) {
      // Record whether Tab reaches the layer's controls at all: the verifier rates the finding high
      // when it never does.
      const passed = new Set(atOpen.body ? [] : [atOpen.id]);
      for (let i = 0; i < limit; i++) {
        const next = await press('Tab');
        if (next.where === 'inside') {
          reachedLayer = true;
          break;
        }
        if (next.body || passed.has(next.id)) break;
        passed.add(next.id);
      }
      fail('dialog-focus-not-moved', overlay, { focus: focusAtOpen, reachedLayer });
    }
    if (reachedLayer && (kind === 'modal' || await page.evaluate(MODAL))) {
      const n = await page.evaluate(TABBABLE_IN_LAYER);
      for (let i = 0; i <= n; i++) {
        const next = await press('Tab');
        if (next.where === 'outside' || next.where === 'trigger') {
          fail('dialog-focus-escaped', overlay, { focus: await focusOf(next) });
          break;
        }
      }
    }
    await closeAndReturn({ control: true });
  }
  return finish(opened);
}
