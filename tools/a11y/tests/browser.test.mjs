// Browser layer (KTD4) and fixture server (KTD27), checked in a real Chromium against local fixtures.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ROOT, readJson } from '../lib/canon.mjs';
import { cdpSession, envFor, injectMotionOff, launch, newContext, parkPointer, readBuildHash, settle, trackRequests } from '../lib/browser.mjs';
import { startServer } from '../fixtures/server.mjs';

const SERVER = join(ROOT, 'tools', 'a11y', 'fixtures', 'server.mjs');
let browser, server;
const contexts = [];

before(async () => {
  browser = await launch();
  server = await startServer({ dirs: ['browser'] });
});

after(async () => {
  await Promise.all(contexts.map((c) => c.close()));
  await browser?.close();
  await server?.close();
});

async function open(url) {
  const context = await newContext(browser);
  contexts.push(context);
  const page = await context.newPage();
  const tracker = trackRequests(page);
  const response = url ? await page.goto(url) : null;
  return { context, page, response, tracker };
}

function fragmentDirs() {
  const tmp = mkdtempSync(join(tmpdir(), 'a11y-fixtures-'));
  const area = (name, nodes, edges, journeys, issues) => ({
    area: name, title: name.toUpperCase(), blocked: [],
    nodes: nodes.map((id) => ({ id: `${name}.${id}`, title: id, url: `/${name}/${id}` })),
    edges: edges.map(([from, to]) => ({ from: `${name}.${from}`, to: `${name}.${to}`, label: `Open ${to}` })),
    journeys: journeys.map((steps, i) => ({ id: `${name}.j${i}`, title: `Journey ${i}`, steps: steps.map((s) => `${name}.${s}`) })),
    issues: issues.map((id) => ({ id: `${name}.i-${id}`, title: id, severity: 'low', nodeIds: [] })),
  });
  const write = (dir, doc) => {
    mkdirSync(join(tmp, dir));
    writeFileSync(join(tmp, dir, 'map.json'), JSON.stringify(doc));
    writeFileSync(join(tmp, dir, 'index.html'), `<!doctype html><title>${dir}</title><h1>${dir}</h1>`);
  };
  write('alpha', { areas: [area('alpha', ['index', 'detail'], [['index', 'detail']], [['index', 'detail']], ['one'])] });
  write('beta', { areas: [area('beta', ['index'], [], [], ['two', 'three']), area('gamma', ['index', 'a', 'b'], [['index', 'a'], ['a', 'b']], [], [])] });
  return tmp;
}

function firstLine(stream) {
  return new Promise((resolve, reject) => {
    let buf = '';
    stream.on('data', (chunk) => {
      buf += chunk;
      if (buf.includes('\n')) resolve(buf.split('\n')[0]);
    });
    stream.on('end', () => reject(new Error(`no line on stdout: ${buf}`)));
  });
}

test('the canary site serves its pages but not its answer key or definition files', async () => {
  const canary = await startServer({ dirs: ['canary'] });
  try {
    const get = (path) => fetch(`${canary.origin}${path}`);
    for (const path of ['/canary/expected.json', '/canary/findings.json', '/canary/raw/canary.hero.json', '/canary/data/canary.json']) {
      assert.equal((await get(path)).status, 404, path);
    }
    for (const page of ['activity', 'export', 'hero', 'tabs', 'toolbar']) {
      const res = await get(`/canary/${page}.html`);
      assert.equal(res.status, 200, page);
      const html = await res.text();
      assert.doesNotMatch(html, /<!--|expected verdict|trap/i, `${page}.html hints at its verdict`);
    }
  } finally {
    await canary.close();
  }
});

test('context: fixed viewport, media, locale and timezone, recorded by envFor', async () => {
  const { page } = await open(`${server.origin}/browser/plain.html`);
  const seen = await page.evaluate(() => ({
    width: innerWidth,
    height: innerHeight,
    dpr: devicePixelRatio,
    light: matchMedia('(prefers-color-scheme: light)').matches,
    dark: matchMedia('(prefers-color-scheme: dark)').matches,
    reduce: matchMedia('(prefers-reduced-motion: reduce)').matches,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    locale: navigator.language,
  }));
  const hints = await page.evaluate(() => navigator.userAgentData.getHighEntropyValues(['fullVersionList']));
  assert.equal(seen.width, 1440);
  assert.equal(seen.height, 900);
  assert.equal(seen.dpr, 1);
  assert.equal(seen.light, true);
  assert.equal(seen.dark, false);
  assert.equal(seen.reduce, true);
  assert.equal(seen.locale, 'en-US');
  assert.equal(seen.timezone, 'America/Los_Angeles');

  const env = envFor(browser);
  assert.deepEqual(env.viewport, [seen.width, seen.height]);
  assert.equal(env.dpr, seen.dpr);
  assert.equal(env.locale, seen.locale);
  assert.equal(env.timezone, seen.timezone);
  assert.equal(env.colorScheme, 'light');
  assert.equal(env.reducedMotion, 'reduce');
  assert.match(env.chromium, /^\d+\.\d+\.\d+\.\d+$/);
  assert.equal(env.chromium, hints.fullVersionList.find((b) => b.brand === 'Chromium').version);
  const pinned = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8')).devDependencies;
  assert.equal(env.playwright, pinned.playwright);
  assert.equal(env.axe, pinned['axe-core']);
});

test('parkPointer: moves the pointer off the element it hovered', async () => {
  const { page } = await open(`${server.origin}/browser/plain.html`);
  await page.hover('button');
  assert.equal(await page.evaluate(() => document.querySelector('button').matches(':hover')), true);
  await parkPointer(page);
  assert.equal(await page.evaluate(() => document.querySelector('button').matches(':hover')), false);
});

test('settle: returns only after a DOM change scheduled 600 ms after load', async () => {
  const { context, page, tracker } = await open(`${server.origin}/browser/late.html`);
  const cdp = await cdpSession(context, page);
  assert.equal(await page.locator('#late').count(), 0);
  const t0 = Date.now();
  const result = await settle(page, cdp, { tracker });
  const took = Date.now() - t0;
  assert.equal(await page.locator('#late').textContent(), 'Loaded late');
  assert.ok(took >= 550, `settle returned after ${took} ms`);
  assert.equal(result.stable, true);
  assert.ok(result.samples >= 2);
  assert.ok(result.ms >= 550 && result.ms <= took + 5, `ms ${result.ms}, took ${took}`);
});

test('settle: gives up at the cap on a page that never stops changing', async () => {
  const { context, page, tracker } = await open(`${server.origin}/browser/restless.html`);
  const cdp = await cdpSession(context, page);
  const t0 = Date.now();
  const result = await settle(page, cdp, { tracker, timeout: 1500 });
  const took = Date.now() - t0;
  assert.equal(result.stable, false);
  // It samples while a full 250 ms interval still fits under the cap, then gives up.
  assert.ok(took >= 1250 && took < 2000, `settle returned after ${took} ms`);
  assert.ok(result.samples >= 3, `${result.samples} samples`);
});

test('settle: after a click, waits for the fetch it started and the content that replaces the spinner', async () => {
  const { context, page, tracker } = await open(`${server.origin}/browser/spinner.html`);
  const cdp = await cdpSession(context, page);
  await settle(page, cdp, { tracker });
  await page.click('#load');
  assert.equal(await page.locator('#spinner').count(), 1);
  assert.equal(tracker.inflight(), 1);
  const t0 = Date.now();
  const result = await settle(page, cdp, { tracker });
  const took = Date.now() - t0;
  assert.equal(tracker.inflight(), 0);
  assert.equal(await page.locator('#content').count(), 1, `content missing after ${took} ms`);
  assert.equal(await page.locator('#content').textContent(), 'Details loaded');
  assert.equal(await page.locator('#spinner').count(), 0);
  assert.equal(result.stable, true);
  assert.equal(result.idle, true);
  assert.ok(took >= 750, `settle returned after ${took} ms`);
});

test('trackRequests: a slow fetch in a cross-origin frame does not hold the network busy', async () => {
  const { page, tracker } = await open();
  const frameFetch = page.waitForRequest((req) => req.url() === `${server.origin2}/__delay/3000`);
  await page.goto(`${server.origin}/browser/embed-busy.html`);
  await frameFetch;
  assert.equal(tracker.inflight(), 0);
  const t0 = Date.now();
  assert.equal(await tracker.idle(500, 2000), true);
  assert.ok(Date.now() - t0 < 1000, `idle took ${Date.now() - t0} ms`);
});

test('settle: refuses to run without an armed tracker', async () => {
  const { context, page } = await open(`${server.origin}/browser/plain.html`);
  const cdp = await cdpSession(context, page);
  await assert.rejects(settle(page, cdp), /trackRequests/);
});

test('readBuildHash: reads the cdn.mercury.com build path, else unknown', async () => {
  const { page } = await open(`${server.origin}/browser/build.html`);
  assert.equal(await readBuildHash(page), 'abc123');
  await page.goto(`${server.origin}/browser/plain.html`);
  assert.equal(await readBuildHash(page), 'unknown');
});

test('injectMotionOff: zeroes transitions and animations under a strict style-src', async () => {
  const { page } = await open(`${server.origin}/browser/motion.html`);
  const box = () => page.evaluate(() => {
    const s = getComputedStyle(document.getElementById('box'));
    return { transition: s.transitionDuration, animation: s.animationName, color: s.color };
  });
  assert.deepEqual(await box(), { transition: '2s', animation: 'spin', color: 'rgb(0, 0, 0)' });
  // The fixture's CSP must really block inline styles, or this test proves nothing.
  await page.evaluate(() => {
    const el = document.createElement('style');
    el.textContent = '#box { color: rgb(1, 2, 3) }';
    document.head.append(el);
  });
  assert.equal((await box()).color, 'rgb(0, 0, 0)');

  await injectMotionOff(page);
  assert.deepEqual(await box(), { transition: '0s', animation: 'none', color: 'rgb(0, 0, 0)' });
});

test('server: two origins, and a page embeds the second one as a cross-origin frame', async () => {
  assert.notEqual(new URL(server.origin).origin, new URL(server.origin2).origin);
  for (const origin of [server.origin, server.origin2]) {
    assert.equal((await fetch(`${origin}/browser/plain.html`)).status, 200);
  }
  const { page } = await open(`${server.origin}/browser/embed.html`);
  await page.frameLocator('iframe').locator('h1').waitFor();
  const frame = page.frames().find((f) => f !== page.mainFrame());
  assert.equal(await page.evaluate(() => location.origin), server.origin);
  assert.equal(await frame.evaluate(() => location.origin), server.origin2);
});

test('server hooks: 429 for the next N navigations or matching paths, crash fails the navigation', async () => {
  const hooked = await startServer({ dirs: ['browser'], hooks: { status429: 1 } });
  try {
    const { page } = await open();
    const plain = `${hooked.origin}/browser/plain.html`;
    assert.equal((await page.goto(plain)).status(), 429);
    assert.equal((await page.goto(plain)).status(), 200);

    hooked.setHook('status429', (path) => path === '/browser/build.html');
    assert.equal((await page.goto(`${hooked.origin}/browser/build.html`)).status(), 429);
    assert.equal((await page.goto(plain)).status(), 200);
    hooked.setHook('status429', null);

    hooked.setHook('crash', (path) => path === '/browser/late.html');
    await assert.rejects(page.goto(`${hooked.origin}/browser/late.html`, { timeout: 5000 }), /ERR_EMPTY_RESPONSE/);
    // Chromium then commits its own error page, which would interrupt a next goto on this page.
    const { page: next } = await open();
    assert.equal((await next.goto(plain)).status(), 200);
    hooked.setHook('crash', null);
    assert.equal((await next.goto(`${hooked.origin}/browser/late.html`)).status(), 200);
  } finally {
    await hooked.close();
  }
});

test('server: mergedMap concatenates the served fragments and counts like build.py', async () => {
  const tmp = fragmentDirs();
  const merged = await startServer({ dirs: [join(tmp, 'alpha'), join(tmp, 'beta')] });
  try {
    const map = merged.mergedMap();
    assert.deepEqual(map.areas.map((a) => a.area), ['alpha', 'beta', 'gamma']);
    assert.deepEqual(map.stats, { screens: 6, edges: 3, journeys: 1, issues: 3 });
    const out = join(tmp, 'out', 'map.json');
    merged.writeMap(out);
    assert.deepEqual(readJson(out), map);
    const res = await fetch(`${merged.origin}/beta/index.html`);
    assert.equal(res.status, 200);
    assert.match(await res.text(), /<h1>beta<\/h1>/);
    assert.equal((await fetch(`${merged.origin}/alpha/missing.html`)).status, 404);
  } finally {
    await merged.close();
    rmSync(tmp, { recursive: true, force: true });
  }
});

test('server CLI: prints both origins on one line, writes --map-out, serves until killed', async () => {
  const tmp = fragmentDirs();
  const out = join(tmp, 'cli-map.json');
  const child = spawn(process.execPath, [SERVER, '--map-out', out, join(tmp, 'alpha'), 'browser'], { stdio: ['ignore', 'pipe', 'inherit'] });
  try {
    const { origin, origin2 } = JSON.parse(await firstLine(child.stdout));
    assert.notEqual(origin, origin2);
    assert.equal((await fetch(`${origin}/browser/plain.html`)).status, 200);
    assert.equal((await fetch(`${origin2}/alpha/index.html`)).status, 200);
    const map = readJson(out);
    assert.deepEqual(map.areas.map((a) => a.area), ['alpha']);
    assert.deepEqual(map.stats, { screens: 2, edges: 1, journeys: 1, issues: 1 });
  } finally {
    child.kill();
    rmSync(tmp, { recursive: true, force: true });
  }
});

test('a hanging third-party request does not hold up the stability wait', async () => {
  const ctx = await newContext(browser);
  const page = await ctx.newPage();
  const tracker = trackRequests(page);
  const cdp = await cdpSession(ctx, page);
  await page.goto(`${server.origin}/browser/thirdparty.html`, { waitUntil: 'commit' });
  const r = await settle(page, cdp, { tracker });
  assert.equal(r.idle, true);
  assert.ok(r.ms < 2500, `settle took ${r.ms} ms waiting on an analytics-style request`);
  await ctx.close();
});
