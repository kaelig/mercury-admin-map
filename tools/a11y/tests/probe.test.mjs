// U7: `probe`, the verifier's measuring helper (KTD19, KTD20), on the static and interactive
// fixtures in a temp workspace. Expected values come from the check modules' own output on the same
// element (checks/axe.mjs, target.mjs, tabwalk.mjs, ax.mjs) or from the fixture's design, never
// from probe.mjs.
import { describe, test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ROOT, writeJson } from '../lib/canon.mjs';
import { cdpSession, injectMotionOff, launch, newContext, parkPointer, settle, trackRequests } from '../lib/browser.mjs';
import { findLayer } from '../lib/scope.mjs';
import { startServer } from '../fixtures/server.mjs';
import * as axe from '../checks/axe.mjs';
import * as ax from '../checks/ax.mjs';
import * as interactive from '../checks/interactive.mjs';
import * as target from '../checks/target.mjs';
import * as tabwalk from '../checks/tabwalk.mjs';

const A11Y = join(ROOT, 'tools', 'a11y');
const RUN = '2026-10-02-1200';
const SWEEP_ENV = { viewport: [1440, 900], dpr: 1, colorScheme: 'light', reducedMotion: 'reduce' };

// The fixture pages the probes visit, as root states, with fingerprints read off each page's design.
const PAGES = {
  'static.contrast': { url: '/static/contrast.html', headings: ['Contrast', 'Skipped level'], controls: [] },
  'static.target-text': { url: '/static/target-text.html', headings: ['Filters'], controls: ['Amount', 'Date', 'Name'] },
  'static.menu': { url: '/static/menu.html', headings: ['Payments'], controls: ['Move money'] },
  'interactive.focus-rings': { url: '/interactive/focus-rings.html', headings: ['Focus rings'],
    controls: ['Amount', 'Custom ring', 'Default ring', 'No ring'] },
  'interactive.trap': { url: '/interactive/trap.html', headings: ['Transactions'], controls: ['After', 'Amount', 'Before', 'Date', 'Status'] },
  'interactive.hidden': { url: '/interactive/hidden.html', headings: ['Hidden focus'],
    controls: ['Continue', 'Export CSV', 'Help center', 'Home', 'Skip to content', 'Undo'] },
};

let server, browser, w;
const temps = [];

before(async () => {
  server = await startServer({ dirs: ['static', 'interactive'] });
  browser = await launch();
  w = workspace();
});

after(async () => {
  await browser?.close();
  await server?.close();
  for (const t of temps) rmSync(t, { recursive: true, force: true });
});

function tmp(prefix) {
  const d = mkdtempSync(join(tmpdir(), prefix));
  temps.push(d);
  return d;
}

// A workspace planned from a two-area map of the pages above, with a root recipe for each.
function workspace({ plan = true } = {}) {
  const root = tmp('a11y-probe-ws-');
  const mapPath = join(root, 'map.json');
  const areas = {};
  const recipes = {};
  for (const [id, p] of Object.entries(PAGES)) {
    const area = id.split('.')[0];
    (areas[area] ||= []).push({ id, title: id, url: p.url, kind: 'page' });
    (recipes[area] ||= {})[id] = {
      parent: p.url, hop: [], mutates: false, source: null, status: 'accepted', similarity: null, recordedBy: 'root', review: null,
      fingerprint: { path: p.url, top: { role: 'document', name: '' }, headings: p.headings, controls: p.controls, structure: 'document' },
    };
  }
  writeJson(mapPath, { areas: Object.entries(areas).map(([area, nodes]) => ({ area, title: area, nodes, edges: [] })) });
  for (const [area, doc] of Object.entries(recipes)) writeJson(join(root, 'recipes', `${area}.json`), doc);
  if (plan) {
    const r = spawnSync('python3', [join(A11Y, 'audit.py'), 'plan', '--workspace', root, '--map', mapPath, '--run', RUN], { encoding: 'utf8' });
    assert.equal(r.status, 0, r.stderr);
  }
  return { root, mapPath };
}

// A child process, awaited without blocking: the fixture server lives in this process, so a
// spawnSync'd child that loads one of its pages would deadlock.
function run(cmd, args, { ignore = false, timeout = 120000 } = {}) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { stdio: ignore ? 'ignore' : ['ignore', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout?.on('data', (d) => { stdout += d; });
    child.stderr?.on('data', (d) => { stderr += d; });
    const timer = setTimeout(() => child.kill('SIGKILL'), timeout);
    child.on('close', (status) => {
      clearTimeout(timer);
      resolve({ status, stdout, stderr });
    });
  });
}

// Run a Node stage module the way a verifier does; returns {status, json (last stdout line), stdout, stderr}.
async function stage(module, args, ws = w) {
  const r = await run('node', [join(A11Y, module), ...args, '--workspace', ws.root, '--map', ws.mapPath, '--base-url', server.origin]);
  const lines = r.stdout.trim().split('\n');
  let json = null;
  try {
    json = JSON.parse(lines[lines.length - 1]);
  } catch { /* not JSON */ }
  return { ...r, json };
}

async function probe(state, kind, xpath, extra = []) {
  const r = await stage('probe.mjs', [state, '--kind', kind, '--xpath', xpath, ...extra]);
  assert.equal(r.status, 0, r.stderr + r.stdout);
  return r.json;
}

// A state as the sweep hands it to the checks: settled, motion off, pointer parked, its layer found.
async function open(state) {
  const context = await newContext(browser);
  const page = await context.newPage();
  const tracker = trackRequests(page);
  const cdp = await cdpSession(context, page);
  await page.goto(`${server.origin}${PAGES[state].url}`);
  await settle(page, cdp, { tracker });
  await injectMotionOff(page);
  await parkPointer(page);
  const layer = await findLayer(cdp, 'page');
  return { context, page, cdp, tracker, kind: 'page', state, layer, env: null };
}

// Run check modules in the sweep's order on a fresh copy of the state; returns {moduleId: output}.
async function sweep(state, modules) {
  const ctx = await open(state);
  try {
    const out = {};
    for (const mod of modules) {
      const r = await mod.run(ctx);
      out[mod.id] = r;
      if (mod.id === 'interactive') ctx.interactive = r.detail;
    }
    return out;
  } finally {
    await ctx.context.close();
  }
}

// WCAG relative luminance contrast, truncated to two decimals as axe reports it.
function contrastOf(fg, bg) {
  const lum = (hex) => {
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
      .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const [a, b] = [lum(fg), lum(bg)].sort((x, y) => y - x);
  return Math.floor(((a + 0.05) / (b + 0.05)) * 100) / 100;
}

// The verdict validator itself (verdict.py): a not-reproduced-live drop of a finding of `family`
// on the probe's state, citing the probe. Returns the reasons it rejects the line for ([] = valid).
function validate(p, family) {
  const key = `${family}:rule:000000000000`;
  const finding = { key, source: 'scanner', family, rule: 'rule', states: [p.state], sample: [p.state], severityDefault: 'medium',
    issueId: null, facts: {} };
  const line = { key, issueId: null, source: 'scanner', outcome: 'drop', severityBefore: 'medium', severityAfter: 'medium',
    reason: 'measured again on the live state', reasonCode: 'not-reproduced-live', counterEvidence: p.id, probes: [p],
    states: [p.state], build: p.build, model: 'sonnet', batch: 'verify-001', prompt: 'verifier@1' };
  // verdict.py imports mapdata from tools/, which audit.py puts on the path for the CLI.
  const code = 'import json, sys; sys.path.insert(0, ".."); import verdict; '
    + 'f, l = json.load(sys.stdin); print(json.dumps(verdict.validate(l, {f["key"]: f})[1]))';
  const r = spawnSync('python3', ['-c', code], { cwd: A11Y, input: JSON.stringify([finding, line]), encoding: 'utf8' });
  assert.equal(r.status, 0, r.stderr);
  return JSON.parse(r.stdout);
}

const ID = /^probe-[0-9a-f]{12}$/;

describe('probes in a fresh context', { concurrency: 4 }, () => {
  test('contrast: the 2.32:1 text gives the colors and ratio the sweep\'s axe check found, and fails', async () => {
    const [{ axe: a }, p] = await Promise.all([
      sweep('static.contrast', [axe]),
      probe('static.contrast', 'contrast', '/html/body/main[1]/p[1]'),
    ]);
    const v = a.results.find((r) => r.rule === 'color-contrast' && r.basis === 'violation');
    assert.equal(v.occurrence.xpath, '/html/body/main[1]/p[1]');
    assert.match(p.id, ID);
    assert.equal(p.state, 'static.contrast');
    assert.equal(p.kind, 'contrast');
    assert.equal(p.xpath, '/html/body/main[1]/p[1]');
    assert.equal(p.build, 'unknown');
    assert.deepEqual(p.env, SWEEP_ENV);
    assert.deepEqual(p.value, {
      fg: v.evidence.fgColor, bg: v.evidence.bgColor, ratio: v.evidence.contrastRatio, required: 4.5,
      fontSize: v.evidence.fontSize, fontWeight: v.evidence.fontWeight, incomplete: false, messageKey: null,
    });
    assert.equal(p.value.ratio, 2.32);
    assert.equal(p.pass, false);
    // The validator takes the probe and refuses it as counter-evidence: 2.32:1 does not pass.
    const reasons = validate(p, 'contrast');
    assert.equal(reasons.length, 1, reasons.join('; '));
    assert.match(reasons[0], /does not pass: it measured 2\.32:1 where AA needs 4\.5:1/);
  });

  test('contrast: a passing heading measures its design colors, passes, and the validator accepts the drop', async () => {
    const p = await probe('static.contrast', 'contrast', '/html/body/main[1]/h1[1]');
    // h1: #1a1a1a on #ffffff, 2em bold, so large text and 3:1 required.
    assert.deepEqual(p.env, SWEEP_ENV);
    assert.equal(p.value.fg, '#1a1a1a');
    assert.equal(p.value.bg, '#ffffff');
    assert.equal(p.value.ratio, contrastOf('#1a1a1a', '#ffffff'));
    assert.equal(p.value.required, 3);
    assert.equal(p.value.fontWeight, 'bold');
    assert.equal(p.value.incomplete, false);
    assert.equal(p.pass, true);
    assert.deepEqual(validate(p, 'contrast'), []);
    // The same probe taken at another viewport is refused.
    const moved = { ...p, env: { ...p.env, viewport: [1280, 800] } };
    assert.ok(validate(moved, 'contrast').some((r) => /was taken at viewport \[1280, 800\]/.test(r)));
  });

  test('contrast: text on a gradient is incomplete with the messageKey the sweep recorded, and does not pass', async () => {
    const [{ axe: a }, p] = await Promise.all([
      sweep('static.contrast', [axe]),
      probe('static.contrast', 'contrast', '/html/body/main[1]/p[2]'),
    ]);
    const inc = a.results.find((r) => r.rule === 'color-contrast' && r.basis === 'incomplete');
    assert.equal(inc.occurrence.xpath, '/html/body/main[1]/p[2]');
    assert.equal(p.value.incomplete, true);
    assert.equal(p.value.messageKey, inc.messageKey);
    assert.equal(p.value.ratio, null);
    assert.equal(p.pass, false);
  });

  test('target: the 16 px button gives the width, height and offset checks/target.mjs measured', async () => {
    const [out, p] = await Promise.all([
      sweep('static.target-text', [interactive, target]),
      probe('static.target-text', 'target', '/html/body/main[1]/p[1]/button[1]'),
    ]);
    const r = out.target.results.find((x) => x.occurrence.name === 'Date');
    assert.equal(r.occurrence.xpath, '/html/body/main[1]/p[1]/button[1]');
    assert.deepEqual(p.env, SWEEP_ENV);
    assert.deepEqual(p.value, { width: r.evidence.width, height: r.evidence.height, offset: r.evidence.offset });
    assert.deepEqual(p.value, { width: 16, height: 16, offset: 8 });
    assert.equal(p.pass, false);
    assert.equal(validate(p, 'target-size').length, 1);
  });

  test('target: a full-size button passes on size', async () => {
    // menu.html: 14 px text with 8 px vertical padding, so more than 24 px each way.
    const p = await probe('static.menu', 'target', '/html/body/main[1]/button[1]');
    assert.ok(p.value.width >= 24 && p.value.height >= 24, JSON.stringify(p.value));
    assert.equal(p.pass, true);
    assert.deepEqual(validate(p, 'target-size'), []);
  });

  test('focus: outline:none is not visible, the default ring is, with the Tab walk\'s pixel counts', async () => {
    const [{ tabwalk: t }, none, ring] = await Promise.all([
      sweep('interactive.focus-rings', [tabwalk]),
      probe('interactive.focus-rings', 'focus', '/html/body/main[1]/button[3]'),
      probe('interactive.focus-rings', 'focus', '/html/body/main[1]/button[1]'),
    ]);
    const stop = (name) => t.detail.stops.find((s) => s.occurrence.name === name);
    assert.equal(stop('No ring').occurrence.xpath, '/html/body/main[1]/button[3]');
    assert.equal(stop('Default ring').occurrence.xpath, '/html/body/main[1]/button[1]');
    for (const p of [none, ring]) {
      assert.deepEqual(p.env, SWEEP_ENV);
      assert.equal(p.value.via, 'tab');
      assert.equal(p.value.floor, tabwalk.FOCUS_DIFF_FLOOR);
      assert.equal(p.value.hidden, null);
      assert.equal(p.value.trapped, false);
      assert.equal(p.value.end, 'body');
    }
    assert.equal(none.value.visible, false);
    assert.equal(none.value.diff, stop('No ring').diff);
    assert.equal(none.pass, false);
    assert.equal(validate(none, 'focus-visible').length, 1);
    assert.equal(ring.value.visible, true);
    assert.equal(ring.value.diff, stop('Default ring').diff);
    assert.equal(ring.pass, true);
    assert.deepEqual(validate(ring, 'focus-visible'), []);
    assert.deepEqual(validate(ring, 'keyboard-trap'), []);
  });

  test('focus: a filter bar that holds Tab, Esc and Shift+Tab is trapped, as the Tab walk found', async () => {
    const [{ tabwalk: t }, p] = await Promise.all([
      sweep('interactive.trap', [tabwalk]),
      probe('interactive.trap', 'focus', '/html/body/main[1]/div[1]/button[1]'),
    ]);
    const trap = t.results.find((r) => r.rule === 'keyboard-trap');
    assert.equal(trap.occurrence.xpath, '/html/body/main[1]/div[1]/button[1]');
    assert.equal(p.value.trapped, true);
    assert.equal(p.value.end, 'repeat');
    assert.equal(p.value.loop, trap.occurrence.xpath);
    assert.deepEqual(p.value.probes.map((x) => [x.key, x.left]), trap.evidence.probes.map((x) => [x.key, x.left]));
    assert.equal(p.pass, false);
    assert.ok(validate(p, 'keyboard-trap').some((r) => /does not record trapped: false/.test(r)));
  });

  test('focus: a focused element that stays hidden is not visible, as the Tab walk judged it; a skip link is', async () => {
    const [{ tabwalk: t }, skip, clipped, ghost, main] = await Promise.all([
      sweep('interactive.hidden', [tabwalk]),
      probe('interactive.hidden', 'focus', '/html/body/a[1]'),
      probe('interactive.hidden', 'focus', '/html/body/main[1]/a[1]'),
      probe('interactive.hidden', 'focus', '/html/body/main[1]/button[2]'),
      probe('interactive.hidden', 'focus', '/html/body/main[1]'),
    ]);
    const stop = (xpath) => t.detail.stops.find((s) => s.occurrence.xpath === xpath);
    for (const p of [skip, clipped, ghost]) {
      const s = stop(p.xpath);
      assert.equal(p.value.via, 'tab', p.xpath);
      assert.equal(p.value.hidden, s.hidden, p.xpath);
      assert.equal(p.value.visible, s.hidden === null && s.diff >= tabwalk.FOCUS_DIFF_FLOOR, p.xpath);
    }
    // The fixture's design: the skip link appears on focus; sr-only text is clipped; opacity 0 is invisible.
    assert.deepEqual([skip.value.hidden, skip.value.visible], [null, true]);
    assert.deepEqual([clipped.value.hidden, clipped.value.visible], ['clipped', false]);
    assert.deepEqual([ghost.value.hidden, ghost.value.visible], ['invisible', false]);
    assert.equal(ghost.pass, false);
    // <main tabindex=-1> is outside the Tab order: focused directly, and the walk still runs to the body.
    assert.equal(main.value.via, 'direct');
    assert.equal(main.value.trapped, false);
    assert.equal(main.value.end, 'body');
  });

  test('ax: the menu button gives the role, name, states and template of checks/ax.mjs\'s record', async () => {
    const [{ records }, p] = await Promise.all([
      sweep('static.menu', [ax]),
      probe('static.menu', 'ax', '/html/body/main[1]/button[1]'),
    ]);
    const rec = records.records.find((r) => r.occurrence.xpath === '/html/body/main[1]/button[1]');
    assert.equal(rec.template, 'button "Move money" [expanded=false, haspopup=menu]');
    assert.deepEqual(p.env, SWEEP_ENV);
    assert.deepEqual(p.value, { role: rec.role, name: rec.name, states: rec.states, description: rec.description,
      template: rec.template, ignored: false });
    assert.equal(p.pass, null);
  });

  test('a missing element or a state with no recipe exits non-zero with a JSON error', async () => {
    const [missing, norecipe, badKind] = await Promise.all([
      stage('probe.mjs', ['static.contrast', '--kind', 'contrast', '--xpath', '/html/body/main[1]/p[9]']),
      stage('probe.mjs', ['static.nowhere', '--kind', 'contrast', '--xpath', '/html/body']),
      stage('probe.mjs', ['static.contrast', '--kind', 'colour', '--xpath', '/html/body']),
    ]);
    assert.notEqual(missing.status, 0);
    assert.match(missing.json.error, /no element at \/html\/body\/main\[1\]\/p\[9\] on static\.contrast/);
    assert.notEqual(norecipe.status, 0);
    assert.match(norecipe.json.error, /no recipe for static\.nowhere/);
    assert.notEqual(badKind.status, 0);
    assert.match(badKind.stderr, /--kind/);
  });

  test('status is pending on a run without states.json, then {}', async () => {
    const bare = workspace({ plan: false });
    assert.deepEqual((await stage('probe.mjs', ['status'], bare)).json, { pending: true });
    assert.deepEqual((await stage('probe.mjs', ['status'])).json, {});
  });
});

// ---------------------------------------------------------------------------
// --session: a state reach prepared in an agent-browser session
// ---------------------------------------------------------------------------

function agentBrowser() {
  return spawnSync('agent-browser', ['--version'], { encoding: 'utf8' }).status === 0;
}

test('--session probes the state reach left in an agent-browser session, then restores its viewport and media', async (t) => {
  if (!agentBrowser()) {
    t.skip('agent-browser is not on PATH: install agent-browser 0.26 to run the --session probe test');
    return;
  }
  const session = `a11y-u7-probe-${process.pid}`;
  const ab = async (...args) => {
    const r = await run('agent-browser', ['--session', session, ...args], { timeout: 60000 });
    assert.equal(r.status, 0, `agent-browser ${args.join(' ')}: ${r.stderr}${r.stdout}`);
    return r.stdout.trim();
  };
  try {
    // The first command starts the session's daemon, which would hold a piped stdout open: ignore it.
    assert.equal((await run('agent-browser', ['--session', session, 'open', 'about:blank'], { ignore: true, timeout: 60000 })).status, 0);
    const reached = await stage('reach.mjs', ['static.contrast', '--session', session]);
    assert.equal(reached.status, 0, reached.stderr + reached.stdout);
    assert.equal(reached.json.status, 'reached', reached.json.message);

    const [inSession, fresh] = await Promise.all([
      probe('static.contrast', 'contrast', '/html/body/main[1]/p[1]', ['--session', session]),
      probe('static.contrast', 'contrast', '/html/body/main[1]/p[1]'),
    ]);
    assert.deepEqual(inSession.env, SWEEP_ENV);
    assert.deepEqual(inSession.value, fresh.value);
    assert.equal(inSession.id, fresh.id, 'the same measurement gets the same id');
    assert.deepEqual(validate(inSession, 'contrast').filter((r) => /was taken at/.test(r)), []);

    // The session is still open, on the state, under agent-browser's own settings again.
    assert.equal(new URL(await ab('get', 'url')).pathname, PAGES['static.contrast'].url);
    const seen = JSON.parse(await ab('eval', `({ size: [innerWidth, innerHeight, devicePixelRatio],
      light: matchMedia('(prefers-color-scheme: light)').matches, reduce: matchMedia('(prefers-reduced-motion: reduce)').matches })`));
    assert.deepEqual(seen, { size: [1440, 900, 1], light: true, reduce: true });

    // A probe of a state the session is not on is refused.
    const wrong = await stage('probe.mjs', ['static.target-text', '--kind', 'target', '--xpath', '/html/body/main[1]/p[1]/button[1]', '--session', session]);
    assert.notEqual(wrong.status, 0);
    assert.match(wrong.json.error, /session .* is not on static\.target-text/);
  } finally {
    await run('agent-browser', ['--session', session, 'close'], { ignore: true, timeout: 30000 });
  }
});
