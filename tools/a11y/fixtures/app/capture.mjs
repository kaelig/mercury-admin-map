// Regenerates the fixture app's "captured" screenshots, the way the original capture took the demo's:
// plain Playwright clicks in the KTD4 context, independent of the recipe interpreter they test.
//
//   node tools/a11y/fixtures/app/capture.mjs
//
// Writes captures/<node id>.png at 1440×900, DPR 1, plus two re-renders test_similarity.py compares:
// captures/rerender/cards.edit-limit-modal.dpr2.png (2880×1800, like the demo's captures) and
// captures/rerender/transactions.index.drift.png (Blue Bottle Coffee paid 2 days ago, not yesterday).
import { cpSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { injectMotionOff, launch, newContext, parkPointer, settle, trackRequests } from '../../lib/browser.mjs';
import { startServer } from '../server.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const CAPTURES = join(HERE, 'captures');

const STEPS = {
  'home.index': ['/app/dashboard'],
  'home.move-money-menu': ['/app/dashboard', (p) => p.getByRole('button', { name: 'Move money' }).click()],
  'home.send-modal': ['/app/dashboard', (p) => p.getByRole('button', { name: 'Move money' }).click(),
    (p) => p.getByRole('menuitem', { name: 'Send money' }).click()],
  'home.send-toast': ['/app/dashboard', (p) => p.getByRole('button', { name: 'Move money' }).click(),
    (p) => p.getByRole('menuitem', { name: 'Send money' }).click(),
    (p) => p.getByLabel('Recipient email').fill('a11y-audit@example.invalid'),
    (p) => p.getByLabel('Amount').fill('1.00'),
    (p) => p.getByRole('button', { name: 'Send', exact: true }).click()],
  'home.chart-tooltip': ['/app/dashboard', (p) => p.locator('.chart').hover()],
  'cards.index': ['/app/cards'],
  'cards.edit-limit-modal': ['/app/cards', (p) => p.getByRole('button', { name: 'Edit limits' }).click()],
  'cards.freeze-modal': ['/app/cards', (p) => p.getByRole('button', { name: 'Freeze card' }).click()],
  'transactions.index': ['/app/transactions'],
  'transactions.filters-menu': ['/app/transactions', (p) => p.getByRole('button', { name: 'Filters' }).click()],
  'transactions.card-payments': ['/app/transactions', (p) => p.getByRole('button', { name: 'Filters' }).click(),
    (p) => p.getByRole('menuitem', { name: 'Card payments' }).click()],
  'transactions.drawer': ['/app/transactions', (p) => p.getByRole('button', { name: 'Filters' }).click(),
    (p) => p.getByRole('menuitem', { name: 'Card payments' }).click(),
    (p) => p.getByRole('button', { name: /^Blue Bottle Coffee/ }).click()],
  'reports.index': ['/app/reports'],
  'books.index': ['/app/dashboard', (p) => p.getByRole('button', { name: 'Try Acme Books' }).click()],
};

async function shoot(browser, origin, id, out, contextOpts = {}) {
  const [path, ...actions] = STEPS[id];
  const context = await newContext(browser, contextOpts);
  try {
    const page = await context.newPage();
    const tracker = trackRequests(page);
    const cdp = await context.newCDPSession(page);
    await page.goto(origin + path);
    await parkPointer(page);
    await injectMotionOff(page);
    await settle(page, cdp, { tracker });
    for (const act of actions) {
      await act(page);
      await settle(page, cdp, { tracker });
    }
    await page.screenshot({ path: out });
  } finally {
    await context.close();
  }
}

// Palette PNGs keep the committed files small; similarity.py works on downscaled grayscale anyway.
function shrink(paths) {
  const code = 'import sys\nfrom PIL import Image\nfor p in sys.argv[1:]:\n    Image.open(p).convert("RGB").quantize(colors=64).save(p, optimize=True)\n';
  const r = spawnSync('python3', ['-c', code, ...paths], { stdio: 'inherit' });
  if (r.status !== 0) throw new Error('could not shrink the screenshots');
}

const browser = await launch();
const server = await startServer({ dirs: ['app'] });
const tmp = mkdtempSync(join(tmpdir(), 'a11y-app-drift-'));
try {
  const written = [];
  for (const id of Object.keys(STEPS)) {
    const out = join(CAPTURES, `${id}.png`);
    await shoot(browser, server.origin, id, out);
    written.push(out);
  }
  const dpr2 = join(CAPTURES, 'rerender', 'cards.edit-limit-modal.dpr2.png');
  await shoot(browser, server.origin, 'cards.edit-limit-modal', dpr2, { deviceScaleFactor: 2 });
  written.push(dpr2);

  cpSync(HERE, join(tmp, 'app'), { recursive: true });
  writeFileSync(join(tmp, 'app', 'config.js'), "export default { paidWord: 'Paid', daysAgo: 2, editLimitsOpens: 'limits', reportsRedirect: false };\n");
  const drift = await startServer({ dirs: [join(tmp, 'app')] });
  const driftOut = join(CAPTURES, 'rerender', 'transactions.index.drift.png');
  await shoot(browser, drift.origin, 'transactions.index', driftOut);
  written.push(driftOut);
  await drift.close();

  shrink(written);
  console.log(JSON.stringify({ screens: written.length }));
} finally {
  await browser.close();
  await server.close();
  rmSync(tmp, { recursive: true, force: true });
}
