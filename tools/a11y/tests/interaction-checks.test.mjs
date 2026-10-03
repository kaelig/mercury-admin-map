// U3: the Tab walk, the dialog check and the status observer against tools/a11y/fixtures/interactive.
// Each expected result comes from the fixture's design (the comments in each fixture page say what
// it breaks). Every scenario runs twice in fresh contexts and must give byte-identical output.
import { describe, test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { launch, newContext, cdpSession, trackRequests, settle, injectMotionOff, parkPointer } from '../lib/browser.mjs';
import { describeElements } from '../lib/occurrence.mjs';
import { armLayer, findLayer } from '../lib/scope.mjs';
import { canonical } from '../lib/canon.mjs';
import { startServer } from '../fixtures/server.mjs';
import * as tabwalk from '../checks/tabwalk.mjs';
import * as dialog from '../checks/dialog.mjs';
import * as status from '../checks/status.mjs';

let server, browser, shots;
before(async () => {
  server = await startServer({ dirs: ['interactive'] });
  browser = await launch();
  shots = mkdtempSync(join(tmpdir(), 'a11y-shots-'));
});
after(async () => {
  await browser.close();
  await server.close();
  rmSync(shots, { recursive: true, force: true });
});

const FLOOR = tabwalk.FOCUS_DIFF_FLOOR;

// A fresh context on a fixture page, prepared the way the sweep prepares a root state (KTD4).
async function open(file) {
  const context = await newContext(browser);
  const page = await context.newPage();
  const tracker = trackRequests(page);
  const cdp = await cdpSession(context, page);
  await page.goto(`${server.origin}/interactive/${file}`);
  await settle(page, cdp, { tracker });
  await injectMotionOff(page);
  await parkPointer(page);
  return { context, page, cdp, tracker };
}

// The final hop's trigger, by its exact visible text (a role-less clickable div included).
const triggerOf = (page, text) => page.getByText(text, { exact: true });

// A state: the page itself, or the layer that `hop` (['click' | 'hover', text]) opens on it.
// `arm` runs before the final hop, as the sweep's beforeFinalHop does.
async function reach(file, { kind = 'page', hop = null, arm = null, wait = true } = {}) {
  const s = await open(file);
  if (hop) {
    await armLayer(s.page);
    if (arm) await arm(s.page);
    const t = triggerOf(s.page, hop[1]);
    await (hop[0] === 'hover' ? t.hover() : t.click());
    if (wait) {
      await settle(s.page, s.cdp, { tracker: s.tracker });
      await injectMotionOff(s.page);
    }
    if (hop[0] === 'click') await parkPointer(s.page);
  }
  return { ...s, kind, layer: await findLayer(s.cdp, kind), env: null };
}

// The expected-interactive set U2 produces, built by hand: every element matching `css` in scope.
async function expected(s, css, focusable, source, disabled = false) {
  const occs = await describeElements(s.cdp, `(q) => (${s.layer.xpath ? `document.evaluate(${JSON.stringify(s.layer.xpath)}, document, null, 9, null).singleNodeValue` : 'document'}).querySelectorAll(q)`, css, { layerXPath: s.layer.xpath });
  return occs.map((o) => ({ ...o, focusable, source, disabled }));
}

const who = (o) => (o === 'body' ? 'body' : o.name || o.xpath);
const brief = (results) => results.map((r) => [r.rule, r.basis, r.criteria.join(' '), who(r.occurrence)]).sort();
const names = (detail) => detail.stops.map((s) => who(s.occurrence));

// Runs a scenario twice from scratch, in two contexts at once; the canonical {results, detail}
// must match byte for byte.
async function twice(fn) {
  const [a, b] = await Promise.all([fn(), fn()]);
  assert.equal(canonical({ results: b.results, detail: b.detail }), canonical({ results: a.results, detail: a.detail }));
  return a;
}

async function walk(file, { state, interactive = [], shotsDir = null, ...opts } = {}) {
  const s = await reach(file, opts);
  try {
    const set = [];
    for (const [css, focusable, source, disabled] of interactive) set.push(...await expected(s, css, focusable, source, disabled));
    const t0 = performance.now();
    const out = await tabwalk.run({ ...s, state: state || `interactive.${file.replace('.html', '')}`, interactive: set, shotsDir });
    return { ...out, ms: performance.now() - t0 };
  } finally {
    await s.context.close();
  }
}

// --- Tab walk ---------------------------------------------------------------------------------

// Alone, before the concurrent groups, so the time is the walk's own.
test('tabwalk: the largest fixture, 60 stops, and its time', async (t) => {
  const out = await walk('table.html');
  assert.equal(out.detail.stops.length, 60);
  assert.equal(out.detail.end, 'body');
  assert.deepEqual(out.results, []);
  t.diagnostic(`tab walk over 60 stops: ${Math.round(out.ms)} ms`);
});

describe('tab walk', { concurrency: 4 }, () => {
  test('tabwalk: only the button with outline:none is flagged 2.4.7, with repeatable pixel counts', async () => {
    const out = await twice(() => walk('focus-rings.html'));
    assert.equal(tabwalk.id, 'tabwalk');
    assert.equal(out.completed, true);
    assert.deepEqual(names(out.detail), ['Default ring', 'Custom ring', 'No ring', 'Amount']);
    assert.equal(out.detail.end, 'body');
    assert.deepEqual(brief(out.results), [['focus-not-visible', 'failure', '2.4.7', 'No ring']]);
    const [ring, shadow, none, wrapped] = out.detail.stops.map((s) => s.diff);
    assert.ok(ring >= FLOOR && shadow >= FLOOR, `default ${ring}, box-shadow ${shadow}`);
    assert.ok(none < FLOOR, `outline:none ${none}`);
    // The input's ring is its wrapper's :focus-within border, 16 px out: seen, and explained by the style diff.
    assert.ok(wrapped >= FLOOR, `focus-within ${wrapped}`);
    assert.ok(out.detail.stops[3].style.some((p) => p.startsWith('^1 ')), out.detail.stops[3].style.join(', '));
    const ev = out.results[0].evidence;
    assert.equal(ev.stop, 3);
    assert.equal(ev.stops, 4);
    assert.equal(who(ev.previous), 'Custom ring');
    assert.equal(ev.diff, none);
  });

  test('tabwalk: a region that holds Tab, Esc and Shift+Tab is a 2.1.2 trap, with the trace', async () => {
    const out = await twice(() => walk('trap.html', { interactive: [['main button', true, 'ax']] }));
    assert.deepEqual(names(out.detail), ['Before', 'Status', 'Amount', 'Date']);
    assert.equal(out.detail.end, 'repeat');
    // A walk held by a trap did not cover the page: a finding it does not report is not a fixed one.
    assert.equal(out.completed, false);
    // After is never reached, but the trap explains that: no not-reached candidates behind a trap.
    assert.deepEqual(brief(out.results), [['keyboard-trap', 'failure', '2.1.2', 'Status']]);
    const ev = out.results[0].evidence;
    assert.equal(ev.stop, 2);
    assert.equal(ev.stops, 4);
    assert.deepEqual(ev.trace.map((t) => t.name), ['Before', 'Status', 'Amount', 'Date', 'Status']);
    assert.deepEqual(ev.probes.map((p) => [p.key, p.left]), [['Escape', false], ['Shift+Tab', false], ['Shift+Tab', false], ['Shift+Tab', false]]);
  });

  test('tabwalk: a modal that keeps focus inside and closes on Esc passes', async () => {
    const out = await twice(() => walk('dialogs.html', {
      kind: 'modal', hop: ['click', 'Add delivery address'], state: 'interactive.dialog-address',
      interactive: [['input, button', true, 'ax']],
    }));
    assert.equal(out.detail.stops[0].occurrence.layer, 'top');
    assert.deepEqual(names(out.detail), ['Street', 'City', 'Cancel', 'Save']);
    assert.equal(out.detail.end, 'repeat');
    assert.equal(out.completed, true);
    assert.deepEqual(out.results, []);
  });

  test('tabwalk: a modal that ignores Esc but cycles focus inside is no trap (its Cancel button closes it)', async () => {
    const out = await twice(() => walk('dialogs.html', {
      kind: 'modal', hop: ['click', 'Edit nickname'], state: 'interactive.dialog-nickname',
      interactive: [['input, button', true, 'ax']],
    }));
    assert.deepEqual(names(out.detail), ['Nickname', 'Cancel', 'Save']);
    assert.equal(out.detail.end, 'repeat');
    assert.equal(out.completed, true);
    assert.deepEqual(out.results, []);
  });

  test('tabwalk: a bar that sends Tab back to its first button but lets Shift+Tab out is no trap, and leaves the walk incomplete', async () => {
    const out = await twice(() => walk('wrap.html'));
    assert.deepEqual(names(out.detail), ['Before', 'Status', 'Amount', 'Date']);
    assert.equal(out.detail.end, 'repeat');
    assert.deepEqual(out.results, []);
    // After is never visited: a finding the walk would report there is not shown to be fixed.
    assert.equal(out.completed, false);
  });

  test('tabwalk: an iframe that takes three Tab presses is one stop with a count', async () => {
    const out = await twice(() => walk('iframe.html'));
    assert.deepEqual(names(out.detail), ['Before frame', 'Card form', 'After frame']);
    assert.deepEqual(out.detail.stops.map((s) => [s.frame, s.count]), [[false, 1], [true, 3], [false, 1]]);
    assert.equal(out.detail.end, 'body');
    assert.deepEqual(out.results, []);
  });

  test('tabwalk: a composite widget is one stop, and reaching it reaches its items', async () => {
    const out = await twice(() => walk('composite.html', { interactive: [['button', true, 'ax']] }));
    assert.deepEqual(names(out.detail), ['Overview', 'Bold', 'Done']);
    assert.deepEqual(out.detail.stops.map((s) => s.count), [1, 3, 1]);
    assert.deepEqual(out.detail.stops.map((s) => s.composite), ['/html/body/main[1]/div[1]', '/html/body/main[1]/div[2]', null]);
    assert.equal(out.detail.end, 'body');
    // Activity and Statements (arrow keys) and Italic and Underline (collapsed) count as reached.
    assert.deepEqual(out.results, []);
  });

  test('tabwalk: on a non-modal top layer the walk ends when focus leaves it', async () => {
    const out = await twice(() => walk('panel.html', {
      kind: 'drawer', hop: ['click', 'Show details'], state: 'interactive.panel-details',
      interactive: [['a, button', true, 'ax']],
    }));
    assert.match(out.detail.stops[0].occurrence.xpath, /aside/);
    assert.deepEqual(names(out.detail), ['View receipt', 'Download']);
    assert.equal(out.detail.end, 'exit');
    assert.equal(out.completed, true);
    assert.deepEqual(out.results, []);
  });

  test('tabwalk: an iframe that swallows every Tab is one stop, not a trap', async () => {
    const out = await twice(() => walk('iframe-trap.html', { interactive: [['main button', true, 'ax']] }));
    assert.deepEqual(names(out.detail), ['Before frame', 'Note editor']);
    assert.equal(out.detail.end, 'limit');
    assert.equal(out.detail.presses, 60);
    assert.equal(out.detail.stops[1].count, 59);
    // A walk cut off at the limit proves nothing about reach: no not-reached for After frame, and the
    // family is incomplete, so a finding it does not report is not a fixed one.
    assert.deepEqual(out.results, []);
    assert.equal(out.completed, false);
  });

  test('tabwalk: a clickable div is a 2.1.1 candidate, a disabled button is not; a stop a row above its predecessor is a 2.4.3 candidate with an overlay', async () => {
    const shotsDir = join(shots, 'order');
    const out = await twice(() => walk('order.html', {
      shotsDir,
      interactive: [['button:not([disabled])', true, 'ax'], ['button[disabled]', false, 'ax', true], ['.card', false, 'pointer']],
    }));
    assert.deepEqual(names(out.detail), ['Download', 'Share', 'Print', 'Done']);
    assert.equal(out.detail.end, 'body');
    assert.deepEqual(brief(out.results), [
      ['backward-jump', 'candidate', '1.3.2 2.4.3', 'Print'],
      ['not-reached', 'candidate', '2.1.1', '/html/body/main[1]/div[2]'],
    ]);
    const jump = out.results.find((r) => r.rule === 'backward-jump');
    assert.equal(jump.evidence.stop, 3);
    assert.equal(who(jump.evidence.previous), 'Share');
    assert.equal(jump.evidence.overlay, 'interactive.order.tabstops.png');
    assert.ok(existsSync(join(shotsDir, jump.evidence.overlay)));
    const missed = out.results.find((r) => r.rule === 'not-reached');
    assert.deepEqual([missed.evidence.focusable, missed.evidence.source, missed.evidence.stops], [false, 'pointer', 4]);
    assert.equal('focusable' in missed.occurrence, false);
  });

  test('tabwalk: focus on a hidden element fails 2.4.3 and 2.4.7; a skip link that appears on focus passes', async () => {
    const out = await twice(() => walk('hidden.html'));
    assert.deepEqual(names(out.detail), ['Skip to content', 'Home', 'Help center', 'Export CSV', 'Undo', 'Continue']);
    assert.deepEqual(out.detail.stops.map((s) => s.hidden), [null, null, 'clipped', 'off-screen', 'invisible', null]);
    assert.ok(out.detail.stops[0].diff >= FLOOR);
    assert.deepEqual(brief(out.results), [
      ['focus-hidden', 'failure', '2.4.3 2.4.7', 'Export CSV'],
      ['focus-hidden', 'failure', '2.4.3 2.4.7', 'Help center'],
      ['focus-hidden', 'failure', '2.4.3 2.4.7', 'Undo'],
    ]);
    const help = out.results.find((r) => r.occurrence.name === 'Help center');
    assert.equal(help.evidence.stop, 3);
    assert.equal(who(help.evidence.focus), 'Help center');
    assert.equal(who(help.evidence.previous), 'Home');
  });
});

// --- Dialog check -----------------------------------------------------------------------------

async function check(file, kind, verb, text) {
  return dialog.run({
    kind,
    state: `interactive.${file}`,
    env: null,
    finalHop: { [verb]: { text } },
    reopen: () => open(file),
    trigger: (page) => triggerOf(page, text),
  });
}

describe('dialog check', { concurrency: 5 }, () => {
  test('dialog: AE4, Enter opens the drawer but focus stays on the body: 2.4.3 with the trace', async () => {
    const reached = await twice(() => check('drawers.html', 'drawer', 'click', 'Edit limits'));
    assert.equal(dialog.id, 'dialog');
    assert.equal(reached.completed, true);
    assert.equal(reached.detail.opened, 'enter');
    assert.equal(who(reached.detail.trigger), 'Edit limits');
    assert.deepEqual(brief(reached.results), [['dialog-focus-not-moved', 'failure', '2.4.3', 'Card limits']]);
    const ev = reached.results[0].evidence;
    assert.equal(ev.focus, 'body');
    assert.equal(ev.reachedLayer, true);
    assert.deepEqual(ev.trace, reached.detail.trace);
    assert.ok(ev.trace.some((s) => s.key === 'Tab' && s.where === 'inside'));

    // The drawer's controls are never reached: the trace shows Tab running out at the body.
    const never = await twice(() => check('drawers.html', 'drawer', 'click', 'Edit address'));
    assert.deepEqual(brief(never.results), [['dialog-focus-not-moved', 'failure', '2.4.3', 'Billing address']]);
    assert.equal(never.results[0].evidence.reachedLayer, false);
    assert.equal(never.results[0].evidence.trace.some((s) => s.where === 'inside'), false);
    // A non-modal drawer has no containment rule to miss, and Esc closes it.
    assert.equal(never.completed, true);
  });

  test('dialog: a trigger with no tab stop, and one that only a click opens, fail 2.1.1', async () => {
    const row = await twice(() => check('drawers.html', 'drawer', 'click', 'Payment details'));
    assert.deepEqual(brief(row.results), [['dialog-trigger-not-focusable', 'failure', '2.1.1', '/html/body/main[1]/div[1]']]);
    assert.equal(row.detail.opened, null);
    // No focus rule ran, so the family is incomplete: a published focus finding on this state is not fixed.
    assert.equal(row.completed, false);
    assert.equal(row.results[0].evidence.stops, 3);

    const div = await twice(() => check('drawers.html', 'drawer', 'click', 'Card settings'));
    assert.deepEqual(brief(div.results), [['dialog-trigger-not-operable', 'failure', '2.1.1', 'Card settings']]);
    assert.equal(div.detail.opened, null);
    assert.equal(div.completed, false);
    assert.deepEqual(div.detail.trace.filter((s) => !['start', 'Tab'].includes(s.key)).map((s) => s.key), ['Enter', 'Space']);
  });

  test('dialog: a correct modal passes; ignoring Esc gives nothing; dropping focus on close fails 2.4.3', async () => {
    const ok = await twice(() => check('dialogs.html', 'modal', 'click', 'Add delivery address'));
    assert.equal(ok.detail.opened, 'enter');
    assert.equal(ok.completed, true);
    assert.deepEqual(ok.results, []);

    const noEsc = await twice(() => check('dialogs.html', 'modal', 'click', 'Edit nickname'));
    assert.equal(noEsc.detail.opened, 'enter');
    assert.deepEqual(noEsc.results, []);
    assert.ok(noEsc.detail.trace.some((s) => s.key === 'close'));
    assert.equal(noEsc.completed, true);

    const noReturn = await twice(() => check('dialogs.html', 'modal', 'click', 'Change PIN'));
    assert.deepEqual(brief(noReturn.results), [['dialog-focus-not-returned', 'failure', '2.4.3', 'Change PIN']]);
    assert.equal(noReturn.results[0].evidence.focus, 'body');
    assert.equal(noReturn.completed, true);
  });

  test('dialog: a layer the check cannot test every focus rule on leaves the family incomplete, with the results it has', async () => {
    // Focus moves inside and stays there, but neither Esc nor a close control closes it: the return rule never ran.
    const noExit = await twice(() => check('layers.html', 'modal', 'click', 'Review transfer'));
    assert.equal(noExit.detail.opened, 'enter');
    assert.deepEqual(noExit.results, []);
    assert.equal(noExit.completed, false);

    // Tab never reaches the modal, so containment was never tested.
    const outside = await twice(() => check('layers.html', 'modal', 'click', 'View terms'));
    assert.deepEqual(brief(outside.results), [['dialog-focus-not-moved', 'failure', '2.4.3', 'Terms']]);
    assert.equal(outside.results[0].evidence.reachedLayer, false);
    assert.equal(outside.completed, false);

    // A menu that ignores Esc.
    const menu = await twice(() => check('layers.html', 'menu', 'click', 'Sort'));
    assert.equal(menu.detail.opened, 'enter');
    assert.deepEqual(menu.results, []);
    assert.equal(menu.completed, false);

    // A popover whose link Tab never reaches, so its return rule never ran.
    const pop = await twice(() => check('layers.html', 'popover', 'click', 'Limits help'));
    assert.equal(pop.detail.opened, 'enter');
    assert.deepEqual(pop.results.map((r) => r.rule), ['popover-not-reachable']);
    assert.equal(pop.completed, false);
  });

  test('dialog: an APG menu button passes; a menu that leaves focus on its button fails 2.4.3', async () => {
    const ok = await twice(() => check('menus.html', 'menu', 'click', 'Actions'));
    assert.equal(ok.detail.opened, 'enter');
    assert.equal(ok.completed, true);
    assert.deepEqual(ok.results, []);

    const bad = await twice(() => check('menus.html', 'menu', 'click', 'Export'));
    assert.equal(bad.completed, true);
    assert.deepEqual(brief(bad.results), [['menu-focus-not-moved', 'failure', '2.4.3', 'Export']]);
    assert.equal(who(bad.results[0].evidence.focus), 'Export');
    assert.equal(bad.results[0].evidence.nextTabInside, false);
  });

  test('dialog: a hover-only popover fails 2.1.1; one shown on focus and an APG disclosure pass', async () => {
    const hover = await twice(() => check('popovers.html', 'popover', 'hover', 'Fees info'));
    assert.deepEqual(brief(hover.results), [['hover-only', 'failure', '2.1.1', 'Fees info']]);
    assert.equal(hover.detail.opened, null);
    assert.equal(hover.completed, false);

    // Neither closes on Esc, so the return rule never ran: no failure, and the family is incomplete.
    const focus = await twice(() => check('popovers.html', 'popover', 'hover', 'Card art'));
    assert.equal(focus.detail.opened, 'focus');
    assert.deepEqual(focus.results, []);
    assert.equal(focus.completed, false);

    const disclosure = await twice(() => check('popovers.html', 'popover', 'click', 'Filters'));
    assert.equal(disclosure.detail.opened, 'enter');
    assert.deepEqual(disclosure.results, []);
    assert.equal(disclosure.completed, false);
  });
});

// --- Status observer --------------------------------------------------------------------------

async function observe(file, kind, text, { wait = true, delay = 0 } = {}) {
  const s = await reach(file, { kind, hop: ['click', text], arm: status.arm, wait });
  try {
    if (delay) await sleep(delay);
    const out = await status.run({ ...s, state: `interactive.${file}` });
    return { ...out, present: await status.present(s.page) };
  } finally {
    await s.context.close();
  }
}

describe('status observer', { concurrency: 3 }, () => {
  test('status: a toast in a pre-existing status region passes; one with its own new live container fails 4.1.3; one that takes focus is focused', async () => {
    const saved = await twice(() => observe('status.html', 'toast', 'Save'));
    assert.equal(status.id, 'status');
    assert.equal(saved.completed, true);
    assert.deepEqual(saved.results, []);
    assert.equal(saved.detail.message, 'Changes saved');
    assert.deepEqual(saved.detail.region, { role: 'status', live: 'polite', preexisting: true });
    assert.equal(saved.detail.focused, false);

    const sent = await twice(() => observe('status.html', 'toast', 'Send'));
    assert.deepEqual(brief(sent.results), [['status-not-announced', 'failure', '4.1.3', '/html/body/div[2]']]);
    assert.equal(sent.results[0].evidence.message, 'Payment sent');
    assert.deepEqual(sent.results[0].evidence.region, { role: null, live: 'polite', preexisting: false });
    assert.equal(who(sent.results[0].evidence.focus), 'Send');
    assert.deepEqual(sent.detail.region, { role: null, live: 'polite', preexisting: false });

    const archived = await twice(() => observe('status.html', 'toast', 'Archive'));
    assert.deepEqual(archived.results, []);
    assert.equal(archived.detail.message, 'Card archived Undo');
    assert.equal(archived.detail.region, null);
    assert.equal(archived.detail.focused, true);
  });

  test('status: aria-errormessage ties an error to its field; error text only next to its field fails 3.3.1', async () => {
    const out = await twice(() => observe('form.html', 'validation-error', 'Save'));
    assert.deepEqual(brief(out.results), [
      ['error-not-associated', 'failure', '3.3.1', 'ZIP code'],
      ['status-not-announced', 'failure', '4.1.3', '/html/body/main[1]/form[1]/div[2]/p[1]'],
    ]);
    for (const r of out.results) assert.equal(r.evidence.message, 'Enter a 5-digit ZIP code');
    assert.deepEqual(out.detail.errors.map((e) => [e.name, e.message, e.tied]), [
      ['Nickname', 'Enter a nickname', true],
      ['ZIP code', 'Enter a 5-digit ZIP code', false],
    ]);
  });

  test('status: a toast gone after 1 s leaves status complete when it ran first, and the later families incomplete', async () => {
    // Status collects straight after the hop, as the sweep runs it first on toast states.
    const first = await observe('status.html', 'toast', 'Copy account number', { wait: false });
    assert.equal(first.completed, true);
    assert.deepEqual(first.results, []);
    assert.equal(first.detail.message, 'Account number copied');
    assert.equal(first.present, true);

    // A family that finishes after the toast is gone: present() tells the sweep to list it as incomplete.
    const s = await reach('status.html', { kind: 'toast', hop: ['click', 'Copy account number'], arm: status.arm, wait: false });
    try {
      const out = await status.run({ ...s, state: 'interactive.status' });
      assert.equal(out.completed, true);
      assert.equal(await status.present(s.page), true);
      await sleep(1300);
      assert.equal(await status.present(s.page), false);
    } finally {
      await s.context.close();
    }

    // Status itself, collecting after the toast is gone, cannot finish its checks.
    const late = await observe('status.html', 'toast', 'Copy account number', { wait: false, delay: 1300 });
    assert.equal(late.completed, false);
    assert.equal(late.detail.present, false);
  });
});
