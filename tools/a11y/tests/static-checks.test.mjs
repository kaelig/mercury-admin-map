// Static checks (U2): axe, announced-as records, the expected-interactive set and target size, run
// in a real Chromium against tools/a11y/fixtures/static/. Expected values come from each fixture's
// design (the colors, sizes and gaps it sets), not from the checks' output.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { canonical } from '../lib/canon.mjs';
import { cdpSession, envFor, injectMotionOff, launch, newContext, settle, trackRequests } from '../lib/browser.mjs';
import { findLayer } from '../lib/scope.mjs';
import { BY_XPATH, describeElements } from '../lib/occurrence.mjs';
import { startServer } from '../fixtures/server.mjs';
import * as axe from '../checks/axe.mjs';
import * as ax from '../checks/ax.mjs';
import * as interactive from '../checks/interactive.mjs';
import * as target from '../checks/target.mjs';

let server, browser;
before(async () => { server = await startServer({ dirs: ['static'] }); browser = await launch(); });
after(async () => { await browser?.close(); await server?.close(); });

// A state as the sweep hands it to the checks: settled, motion off, its layer found.
async function open(name, kind = 'page') {
  const context = await newContext(browser);
  const page = await context.newPage();
  const tracker = trackRequests(page);
  const cdp = await cdpSession(context, page);
  await page.goto(`${server.origin}/static/${name}.html`);
  await settle(page, cdp, { tracker });
  await injectMotionOff(page);
  const layer = await findLayer(cdp, kind);
  return { context, page, cdp, tracker, kind, state: `static.${name}`, layer, env: envFor(browser) };
}

// Run the four checks in the sweep's order, each seeing what the earlier ones produced.
async function checks(name, kind = 'page', { keep = false, times = null } = {}) {
  const ctx = await open(name, kind);
  try {
    const out = { layer: ctx.layer };
    for (const mod of [axe, ax, interactive, target]) {
      const t0 = performance.now();
      const r = await mod.run(ctx);
      if (times) times[mod.id] = Math.round(performance.now() - t0);
      out[mod.id] = r;
      if (mod.id === 'axe') ctx.axe = { detail: r.detail, results: r.results };
      if (mod.id === 'interactive') ctx.interactive = r.detail;
    }
    out.results = [...out.axe.results, ...out.target.results];
    if (keep) out.ctx = ctx;
    return out;
  } finally {
    if (!keep) await ctx.context.close();
  }
}

const templates = (records) => records.map((r) => r.template).sort();
const strip = ({ xpath, rect, ...rest }) => rest;

test('module ids are the check-module ids the raw file lists', () => {
  assert.deepEqual([axe.id, ax.id, interactive.id, target.id], ['axe', 'records', 'interactive', 'target']);
});

test('axe: grey text at 2.32:1 is a color-contrast violation with its colors and ratio', async () => {
  const { axe: a } = await checks('contrast');
  const contrast = a.results.filter((r) => r.rule === 'color-contrast');
  const violations = contrast.filter((r) => r.basis === 'violation');
  assert.equal(violations.length, 1);
  const [v] = violations;
  assert.equal(v.check, 'axe');
  assert.deepEqual(v.criteria, ['1.4.3']);
  assert.equal(v.messageKey, null);
  assert.equal(v.occurrence.xpath, '/html/body/main[1]/p[1]');
  assert.equal(v.occurrence.layer, 'document');
  assert.equal(v.evidence.fgColor, '#aaaaaa');
  assert.equal(v.evidence.bgColor, '#ffffff');
  assert.equal(v.evidence.contrastRatio, 2.32);
  assert.equal(v.evidence.fontSize, '12.0pt (16px)');
  assert.equal(v.evidence.fontWeight, 'normal');
  assert.equal(v.evidence.expectedContrastRatio, '4.5:1');
  assert.match(v.evidence.failureSummary, /contrast/);
});

test('axe: text on a gradient is kept as an incomplete result with messageKey bgGradient', async () => {
  const { axe: a } = await checks('contrast');
  const incomplete = a.results.filter((r) => r.rule === 'color-contrast' && r.basis === 'incomplete');
  assert.equal(incomplete.length, 1);
  assert.equal(incomplete[0].messageKey, 'bgGradient');
  assert.equal(incomplete[0].occurrence.xpath, '/html/body/main[1]/p[2]');
  assert.deepEqual(incomplete[0].criteria, ['1.4.3']);
});

test('axe: target-size and label-content-name-mismatch ran, best-practice rules did not', async () => {
  const { axe: a } = await checks('contrast');
  const { rulesRan, inapplicable } = a.detail;
  assert.ok(rulesRan.includes('target-size'));
  assert.ok(rulesRan.includes('label-content-name-mismatch'));
  assert.deepEqual(rulesRan, [...rulesRan].sort());
  assert.deepEqual(inapplicable, [...inapplicable].sort());
  assert.ok(inapplicable.every((r) => rulesRan.includes(r)));
  for (const rule of ['region', 'heading-order', 'avoid-inline-spacing']) {
    assert.ok(!rulesRan.includes(rule), `${rule} ran`);
    assert.ok(!a.results.some((r) => r.rule === rule), `${rule} in results`);
  }
  assert.ok(a.results.every((r) => r.criteria.length > 0));
  assert.equal(a.completed, true);
});

test('axe: dropping wcag22aa from runOnly fails the rule assertion', async () => {
  const saved = axe.AXE_OPTIONS.runOnly.values;
  axe.AXE_OPTIONS.runOnly.values = saved.filter((t) => t !== 'wcag22aa');
  const ctx = await open('contrast');
  try {
    await assert.rejects(axe.run(ctx), /target-size/);
  } finally {
    axe.AXE_OPTIONS.runOnly.values = saved;
    await ctx.context.close();
  }
});

test('axe: injection works under a CSP that blocks inline scripts, and is idempotent', async () => {
  const ctx = await open('contrast');
  try {
    await axe.injectAxe(ctx.cdp);
    await axe.injectAxe(ctx.cdp);
    assert.equal(await ctx.page.evaluate(() => window.axe.version), '4.13.0');
  } finally {
    await ctx.context.close();
  }
});

test('records: a menu button renders as button "Move money" [expanded=false, haspopup=menu]', async () => {
  const { records } = await checks('menu');
  assert.deepEqual(templates(records.records), [
    'button "Move money" [expanded=false, haspopup=menu]',
    'heading "Payments" [level=1]',
    'main ""',
  ]);
  const button = records.records.find((r) => r.role === 'button');
  assert.equal(button.name, 'Move money');
  assert.deepEqual(button.states, { expanded: false, haspopup: 'menu' });
  assert.equal(button.description, '');
  assert.equal(button.occurrence.tag, 'button');
  assert.equal(button.occurrence.xpath, '/html/body/main[1]/button[1]');
  assert.deepEqual(records.results, []);
});

test('records: an invalid field shows invalid=true, its error message and its description', async () => {
  const { records } = await checks('field');
  const field = records.records.find((r) => r.role === 'textbox');
  assert.equal(field.template, 'textbox "Email" [required=true, invalid=true, errormessage=Enter a valid email address] (Use your work email)');
  assert.equal(field.states.invalid, true);
  assert.equal(field.description, 'Use your work email');
  assert.ok(templates(records.records).includes('alert "" [live=assertive]'));
  assert.ok(templates(records.records).includes('form "Invite"'));
});

test('axe: a menu button whose aria-controls id is missing gives one incomplete aria-valid-attr-value', async () => {
  const { axe: a } = await checks('menu');
  assert.equal(a.results.length, 1);
  const [r] = a.results;
  assert.equal(r.rule, 'aria-valid-attr-value');
  assert.equal(r.basis, 'incomplete');
  assert.equal(r.messageKey, 'controlsWithinPopup');
  assert.deepEqual(r.criteria, ['4.1.2']);
  assert.equal(r.occurrence.name, 'Move money');
});

test('occurrences: the same button on two pages differs only in xpath and rect', async () => {
  const pick = (out) => ({
    result: out.axe.results.find((r) => r.rule === 'color-contrast' && r.occurrence.name === 'Send payment').occurrence,
    record: out.records.records.find((r) => r.name === 'Send payment').occurrence,
  });
  const a = pick(await checks('same-a'));
  const b = pick(await checks('same-b'));
  for (const k of ['result', 'record']) {
    assert.equal(canonical(strip(a[k])), canonical(strip(b[k])), k);
    assert.equal(a[k].xpath, '/html/body/main[1]/div[1]/button[1]');
    assert.equal(b[k].xpath, '/html/body/main[1]/div[1]/button[3]');
    assert.notDeepEqual(a[k].rect, b[k].rect);
    for (const id of [':r18:', ':r16:', 'row-18', 'row-16']) assert.ok(!canonical(a[k]).includes(id) && !canonical(b[k]).includes(id), id);
  }
  assert.deepEqual(strip(a.result), {
    tag: 'button', role: 'button', name: 'Send payment', classes: ['_button_k2j4h_7', '_primary_k2j4h_9'],
    ancestors: [['_actions_p9q8r_3'], []], landmark: { role: 'main', name: '' }, heading: 'Recipient',
    data: { 'data-testid': 'send-payment' }, layer: 'document',
  });
});

test('interactive: a pointer div without tabindex is in the set, its pointer children are not', async () => {
  const { interactive: i } = await checks('pointer');
  const seen = i.detail.map((o) => [o.source, o.tag, o.xpath, o.focusable]).sort();
  assert.deepEqual(seen, [
    ['ax', 'a', '/html/body/main[1]/p[1]/a[1]', true],
    ['ax', 'button', '/html/body/main[1]/button[1]', false],
    ['ax', 'button', '/html/body/main[1]/div[2]/button[1]', true],
    ['ax', 'div', '/html/body/main[1]/div[3]', false],
    ['ax', 'input', '/html/body/main[1]/p[2]/input[1]', true],
    ['pointer', 'div', '/html/body/main[1]/div[1]', false],
  ]);
  assert.deepEqual(i.results, []);
});

test('target: three adjacent 16 px buttons beside text fail the own test while axe skips them', async () => {
  const out = await checks('target-text');
  const own = out.target.results;
  assert.equal(own.length, 3);
  assert.deepEqual(own.map((r) => r.occurrence.name).sort(), ['Amount', 'Date', 'Name']);
  for (const r of own) {
    assert.equal(r.check, 'target');
    assert.equal(r.rule, 'target-size-own');
    assert.deepEqual(r.criteria, ['2.5.8']);
    assert.equal(r.basis, 'candidate');
    assert.equal(r.evidence.width, 16);
    assert.equal(r.evidence.height, 16);
    // Each center is 16 px from the next: 2 x min(8 px to its box, 16 - 12 px to its circle).
    assert.equal(r.evidence.offset, 8);
    assert.equal(r.evidence.own, 'fail');
    assert.equal(r.evidence.axe, 'inapplicable');
    assert.equal(r.evidence.agree, false);
  }
  assert.ok(!out.axe.results.some((r) => r.rule === 'target-size'));
  assert.deepEqual(out.target.detail, { measured: 3 });
});

test('target: six 20 px icon buttons pass with 4 px gaps and fail with none', async () => {
  const out = await checks('target-grid');
  const own = out.target.results;
  assert.deepEqual(own.map((r) => r.occurrence.name).sort(), ['Clear', 'Copy', 'Cut', 'Paste', 'Redo', 'Undo']);
  for (const r of own) {
    assert.equal(r.basis, 'failure');
    assert.equal(r.evidence.width, 20);
    assert.equal(r.evidence.height, 20);
    // Centers 20 px apart: 2 x min(10 px to the next box, 20 - 12 px to its circle).
    assert.equal(r.evidence.offset, 16);
    assert.equal(r.evidence.own, 'fail');
    assert.equal(r.evidence.axe, 'violation');
    assert.equal(r.evidence.agree, true);
  }
  assert.deepEqual(out.target.detail, { measured: 12 });
});

test('target: undersized native checkboxes are candidates under the user-agent control exception', async () => {
  const out = await checks('target-native');
  const own = out.target.results;
  assert.equal(own.length, 2);
  for (const r of own) {
    assert.equal(r.basis, 'candidate');
    assert.equal(r.evidence.exception, 'ua-control');
    assert.equal(r.evidence.own, 'fail');
    assert.ok(r.evidence.offset < 24);
  }
});

test('target: links inside a sentence are not flagged (inline exception)', async () => {
  const ctx = await open('inline-link');
  try {
    // The fixture's premise: the links are close enough that the spacing test alone would fail.
    const centers = await ctx.page.evaluate(() => [...document.querySelectorAll('main a')].map((a) => {
      const r = a.getBoundingClientRect();
      return [r.x + r.width / 2, r.y + r.height / 2, r.height];
    }));
    assert.equal(centers.length, 3);
    assert.ok(centers.every(([, , h]) => h < 24));
    assert.ok(Math.hypot(centers[1][0] - centers[0][0], centers[1][1] - centers[0][1]) < 24);
  } finally {
    await ctx.context.close();
  }
  const out = await checks('inline-link');
  assert.deepEqual(out.target.results, []);
  assert.deepEqual(out.target.detail, { measured: 3 });
  assert.ok(!out.axe.results.some((r) => r.rule === 'target-size'));
});

test('scope: with a role=dialog open, every result comes from inside it and carries layer top', async () => {
  const out = await checks('dialog', 'modal');
  assert.equal(out.layer.scope, 'top');
  assert.equal(out.layer.role, 'dialog');
  assert.equal(out.layer.name, 'Edit card limits');
  const inside = (o) => o.layer === 'top' && o.xpath.startsWith(out.layer.xpath);
  const occurrences = [...out.results.map((r) => r.occurrence), ...out.records.records.map((r) => r.occurrence), ...out.interactive.detail];
  assert.ok(occurrences.length > 0);
  for (const o of occurrences) assert.ok(inside(o), `${o.xpath} outside ${out.layer.xpath}`);
  const has = (rule, name) => out.results.some((r) => r.rule === rule && r.occurrence.name === name);
  assert.ok(out.axe.results.some((r) => r.rule === 'color-contrast' && r.basis === 'violation'));
  assert.ok(has('target-size-own', 'Decrease') && has('target-size-own', 'Increase'));
  assert.ok(has('aria-valid-attr-value', 'Move money'));
  assert.ok(!has('target-size-own', 'Previous') && !has('aria-valid-attr-value', 'More'));
  assert.ok(templates(out.records.records).includes('dialog "Edit card limits" [modal=true]'));
  assert.ok(out.interactive.detail.some((o) => o.source === 'pointer' && o.xpath.endsWith('/div[2]')));
  assert.ok(!out.interactive.detail.some((o) => o.name === 'More'));
});

test('frames: both frames are inventoried and none of their nodes appear', async () => {
  const out = await checks('frame');
  assert.deepEqual(out.axe.detail.frames, [
    { url: `${server.origin}/static/frame-content.html`, origin: server.origin, firstParty: true },
    { url: `${server.origin2}/static/frame-content.html`, origin: server.origin2, firstParty: false },
  ]);
  const text = canonical([out.results, out.records.records, out.interactive.detail]);
  for (const s of ['Inside the frame', 'Frame close', 'Frame help', 'Frame card']) assert.ok(!text.includes(s), s);
  assert.ok(!out.axe.results.some((r) => r.rule === 'color-contrast'));
  assert.deepEqual(out.target.detail, { measured: 0 });
});

test('determinism: two runs on the largest fixture give byte-identical output', async (t) => {
  const times = [{}, {}];
  const runs = [];
  for (const i of [0, 1]) runs.push(await checks('large', 'page', { times: times[i] }));
  for (const id of ['axe', 'records', 'interactive', 'target']) {
    const [a, b] = runs.map((r) => canonical({ results: r[id].results, records: r[id].records, detail: r[id].detail }));
    assert.equal(a, b, id);
  }
  assert.ok(runs[0].records.records.length > 400);
  assert.ok(runs[0].interactive.detail.length > 400);
  t.diagnostic(`ms per check on large.html: ${JSON.stringify(times)}`);
  t.diagnostic(`large.html: ${runs[0].records.records.length} records, ${runs[0].interactive.detail.length} interactive, ${runs[0].results.length} results`);
});

test('interactive: disabled controls are flagged so the Tab walk does not expect them', async () => {
  const { interactive: i } = await checks('pointer');
  const off = i.detail.find((o) => o.name === 'Archive');
  assert.equal(off.disabled, true);
  assert.equal(i.detail.find((o) => o.name === 'Transfer').disabled, false);
  assert.ok(i.detail.filter((o) => o.source === 'pointer').every((o) => o.disabled === false));
});

test('axe: a failing node inside an SVG is described, not lost to an xpath lookup', async () => {
  const out = await checks('svg-text');
  const r = out.axe.results.find((x) => x.rule === 'color-contrast');
  assert.ok(r, 'the grey chart label fails 1.4.3');
  assert.equal(r.occurrence.tag, 'text');
  assert.match(r.occurrence.xpath, /\/svg\[1\]\/g\[1\]\/g\[1\]\/text\[1\]$/);
});

test('BY_XPATH resolves an SVG node by its occurrence xpath', async () => {
  const ctx = await open('svg-text', 'page');
  try {
    const [occ] = await describeElements(ctx.cdp, BY_XPATH, ['/html/body/main[1]/svg[1]/g[1]/g[1]/text[1]']);
    assert.equal(occ?.tag, 'text');
  } finally {
    await ctx.context.close();
  }
});
