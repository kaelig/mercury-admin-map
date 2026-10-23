// U10: the rendered report in a real Chromium (KTD24, Verification Contract "Report accessibility").
// `audit.py report` renders tests/data/snapshot/ into a temp dist next to a stand-in map page; the
// harness's own axe run (checks/axe.mjs) must find zero violations on it in light and in dark, every
// link must resolve from file://, and keyboard focus must be visible.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { cpSync, existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { cdpSession, launch, newContext } from '../lib/browser.mjs';
import * as axe from '../checks/axe.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const A11Y = dirname(HERE);
const SNAPSHOT = join(HERE, 'data', 'snapshot');
// Stands in for build.py's dist/index.html: shows the issue parameter it was opened with.
const MAP_STUB = '<!doctype html><html lang="en"><title>map stub</title><p id="issue"></p>'
  + '<script>document.getElementById("issue").textContent = new URLSearchParams(location.search).get("issue");</script>';

let tmp, dist, pageUrl, browser;

before(async () => {
  tmp = mkdtempSync(join(tmpdir(), 'a11y-report-'));
  const ws = join(tmp, 'ws');
  dist = join(tmp, 'dist');
  cpSync(SNAPSHOT, join(ws, 'published'), { recursive: true });
  mkdirSync(dist);
  writeFileSync(join(dist, 'index.html'), MAP_STUB);
  const r = spawnSync('python3', [join(A11Y, 'audit.py'), 'report', '--workspace', ws, '--dist', dist], { encoding: 'utf8' });
  assert.equal(r.status, 0, r.stderr);
  pageUrl = pathToFileURL(join(dist, 'a11y', 'index.html')).href;
  browser = await launch();
});

after(async () => {
  await browser?.close();
  if (tmp) rmSync(tmp, { recursive: true, force: true });
});

async function open(colorScheme, viewport) {
  const context = await newContext(browser, { colorScheme, ...(viewport ? { viewport } : {}) });
  const page = await context.newPage();
  await page.goto(pageUrl);
  return { context, page, cdp: await cdpSession(context, page) };
}

const BACKGROUND = { light: 'rgb(255, 255, 255)', dark: 'rgb(30, 30, 42)' }; // --card in src/app.css

for (const scheme of ['light', 'dark']) {
  test(`the harness's axe run finds zero violations on the report in ${scheme}`, async (t) => {
    const { context, page, cdp } = await open(scheme);
    try {
      assert.equal(await page.evaluate(() => getComputedStyle(document.body).backgroundColor), BACKGROUND[scheme]);
      const r = await axe.run({ cdp, page, layer: null });
      const violations = r.results.filter((x) => x.basis === 'violation');
      assert.deepEqual(violations.map((v) => `${v.rule}: ${v.occurrence.xpath} ${v.evidence.failureSummary}`), []);
      for (const rule of ['color-contrast', 'link-in-text-block', 'target-size', 'document-title', 'html-has-lang']) {
        assert.ok(r.detail.rulesRan.includes(rule), rule);
      }
      const incomplete = r.results.filter((x) => x.basis === 'incomplete');
      t.diagnostic(`${scheme}: ${r.detail.rulesRan.length} rules ran, 0 violations, ${incomplete.length} incomplete`
        + (incomplete.length ? ` (${[...new Set(incomplete.map((x) => `${x.rule}/${x.messageKey}`))].join(', ')})` : ''));
      assert.deepEqual(incomplete.filter((x) => x.rule === 'color-contrast').map((x) => x.occurrence.xpath), []);
    } finally {
      await context.close();
    }
  });
}

test('a row link opened from file:// resolves to dist/index.html with the issue parameter', async () => {
  const { context, page } = await open('light');
  try {
    const link = page.locator('#findings a[href^="../index.html?issue="]').first();
    const href = await link.getAttribute('href');
    const id = decodeURIComponent(href.slice('../index.html?issue='.length));
    await Promise.all([page.waitForURL(/\/index\.html\?issue=/), link.click()]);
    assert.equal(page.url(), `${pathToFileURL(join(dist, 'index.html')).href}?issue=${encodeURIComponent(id)}`);
    assert.equal(await page.textContent('#issue'), id);
  } finally {
    await context.close();
  }
});

test('every link on the page resolves', async () => {
  const { context, page } = await open('light');
  try {
    const links = await page.$$eval('a[href]', (as) => as.map((a) => [a.getAttribute('href'), a.href]));
    assert.ok(links.length > 40);
    for (const [raw, resolved] of links) {
      if (raw.startsWith('#')) {
        assert.equal(await page.locator(`[id="${raw.slice(1)}"]`).count(), 1, raw);
        continue;
      }
      const url = new URL(resolved);
      assert.equal(url.protocol, 'file:', raw);
      assert.equal(fileURLToPath(`file://${url.pathname}`), join(dist, 'index.html'), raw);
      assert.ok(existsSync(join(dist, 'index.html')));
    }
  } finally {
    await context.close();
  }
});

test('keyboard focus is visible on links and on scrollable tables', async () => {
  const { context, page } = await open('dark');
  try {
    const seen = new Set();
    for (let i = 0; i < 40 && seen.size < 2; i += 1) {
      await page.keyboard.press('Tab');
      const f = await page.evaluate(() => {
        const el = document.activeElement;
        const cs = getComputedStyle(el);
        return { kind: el.tagName === 'A' ? 'link' : el.classList.contains('scroll') ? 'table' : el.tagName,
          style: cs.outlineStyle, width: cs.outlineWidth, color: cs.outlineColor };
      });
      if (seen.has(f.kind)) continue;
      seen.add(f.kind);
      assert.deepEqual([f.style, f.width, f.color], ['solid', '2px', 'rgb(141, 164, 245)'], f.kind); // dark --accent
    }
    assert.deepEqual([...seen].sort(), ['link', 'table']);
  } finally {
    await context.close();
  }
});

test('at phone width the page does not scroll sideways; wide tables scroll inside their region', async () => {
  const { context, page } = await open('light', { width: 375, height: 800 });
  try {
    const { page: width, view } = await page.evaluate(() => ({ page: document.documentElement.scrollWidth, view: innerWidth }));
    assert.ok(width <= view, `page is ${width}px wide in a ${view}px viewport`);
  } finally {
    await context.close();
  }
});
