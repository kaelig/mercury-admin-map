// The one browser setup every stage uses (KTD4): launch, the fixed context, motion-off CSS,
// the stability wait, the build hash and a CDP session.
import { createRequire } from 'node:module';
import { setTimeout as sleep } from 'node:timers/promises';
import { chromium } from 'playwright';
import { buildHash, sha } from './canon.mjs';

const require = createRequire(import.meta.url);

// README "Capture notes": without these, fresh headless Chrome hangs on screenshots after the laptop sleeps.
export const GPU_FLAGS = ['--disable-gpu', '--disable-gpu-compositing', '--disable-gpu-vsync', '--disable-frame-rate-limit'];

// The raw file's `env` (schemas.md). `chromium` is only known once a browser runs: use envFor().
export const ENV = {
  chromium: null,
  playwright: require('playwright/package.json').version,
  axe: require('axe-core/package.json').version,
  viewport: [1440, 900],
  dpr: 1,
  colorScheme: 'light',
  reducedMotion: 'reduce',
  locale: 'en-US',
  timezone: 'America/Los_Angeles',
};

export const MOTION_OFF_CSS = '*, *::before, *::after { transition: none !important; animation: none !important;'
  + ' caret-color: transparent !important; scroll-behavior: auto !important; }';

export function launch(opts = {}) {
  return chromium.launch({ channel: 'chromium', headless: true, ...opts, args: [...GPU_FLAGS, ...(opts.args || [])] });
}

export function envFor(browser) {
  return { ...ENV, viewport: [...ENV.viewport], chromium: browser.version() };
}

export function newContext(browser, opts = {}) {
  return browser.newContext({
    viewport: { width: ENV.viewport[0], height: ENV.viewport[1] },
    deviceScaleFactor: ENV.dpr,
    colorScheme: ENV.colorScheme,
    reducedMotion: ENV.reducedMotion,
    locale: ENV.locale,
    timezoneId: ENV.timezone,
    serviceWorkers: 'block',
    ...opts,
  });
}

export const cdpSession = (context, page) => context.newCDPSession(page);

export const parkPointer = (page) => page.mouse.move(-1, -1);

// A constructed stylesheet rather than page.addStyleTag: a <style> tag throws under a style-src
// without 'unsafe-inline', while an adopted sheet applies under any CSP and adds no DOM node.
// It lasts until the next full navigation.
export function injectMotionOff(page) {
  return page.evaluate((css) => {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(css);
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
  }, MOTION_OFF_CSS);
}

// Long-lived types (EventSource, WebSocket, media) are left out: they never finish.
const COUNTED = new Set(['document', 'stylesheet', 'image', 'font', 'script', 'xhr', 'fetch']);

function originOf(url) {
  try {
    return new URL(url).origin;
  } catch {
    return 'null';
  }
}

// Counts the page's own requests in flight. Arm it right after newPage(), before the first goto:
// Playwright's 'networkidle' fires once per navigation, so it never waits for a fetch a click starts.
// Requests from cross-origin frames don't count.
// The registrable part of a URL's host: demo.mercury.com and cdn.mercury.com share mercury.com.
export function siteOf(url) {
  try {
    const host = new URL(url).hostname;
    return /^[\d.]+$|^\[|^localhost$/.test(host) ? host : host.split('.').slice(-2).join('.');
  } catch {
    return null;
  }
}

export function trackRequests(page) {
  const live = new Set();
  let changed = Date.now();
  const counts = (req) => {
    if (!COUNTED.has(req.resourceType())) return false;
    const frame = req.frame();
    if (frame === page.mainFrame() && req.isNavigationRequest()) return true;
    // Third-party analytics (Amplitude, LinkedIn) can stay pending for good: only first-party requests count.
    if (siteOf(req.url()) !== siteOf(page.mainFrame().url())) return false;
    if (frame === page.mainFrame()) return true;
    const origin = originOf(req.isNavigationRequest() ? req.url() : frame.url());
    return origin === 'null' || origin === originOf(page.mainFrame().url());
  };
  const done = (req) => {
    if (live.delete(req)) changed = Date.now();
  };
  page.on('request', (req) => {
    if (!counts(req)) return;
    live.add(req);
    changed = Date.now();
  });
  page.on('requestfinished', done);
  page.on('requestfailed', done);
  return {
    inflight: () => live.size,
    // True once nothing has been in flight for `ms`; false if that hasn't happened within `timeout`.
    async idle(ms = 500, timeout = 5000) {
      const t0 = Date.now();
      while (live.size || Date.now() - changed < ms) {
        if (Date.now() - t0 >= timeout) return false;
        await sleep(25);
      }
      return true;
    },
  };
}

async function axTreeHash(cdp) {
  const { nodes } = await cdp.send('Accessibility.getFullAXTree');
  return sha(nodes.filter((n) => !n.ignored).map((n) => [n.role?.value ?? '', n.name?.value ?? '']));
}

// Network idle (no tracked request for 500 ms), fonts, then the accessibility tree unchanged across
// two samples `interval` ms apart, all within about `timeout` ms. `ms` is wall time: never write it
// into a raw file.
export async function settle(page, cdp, { tracker, timeout = 10000, interval = 250, idleTimeout = 5000 } = {}) {
  if (!tracker) throw new Error('settle() needs { tracker: trackRequests(page) }, armed right after newPage()');
  const t0 = Date.now();
  const left = () => Math.max(1, timeout - (Date.now() - t0));
  const idled = await tracker.idle(500, Math.min(idleTimeout, left()));
  await Promise.race([
    page.evaluate(() => document.fonts.ready.then(() => null)),
    sleep(left(), null, { ref: false }),
  ]).catch(() => {});
  let prev = await axTreeHash(cdp);
  let samples = 1;
  while (Date.now() - t0 + interval <= timeout) {
    await sleep(interval);
    const next = await axTreeHash(cdp);
    samples += 1;
    if (next === prev) return { ms: Date.now() - t0, samples, stable: true, idle: idled };
    prev = next;
  }
  return { ms: Date.now() - t0, samples, stable: false, idle: idled };
}

export async function readBuildHash(page) {
  return buildHash(await page.content());
}
