import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { launch, newContext, cdpSession, trackRequests, settle } from '../lib/browser.mjs';
import { describeElements, BY_XPATH } from '../lib/occurrence.mjs';
import { armLayer, findLayer } from '../lib/scope.mjs';
import { startServer } from '../fixtures/server.mjs';

let server, browser;
before(async () => { server = await startServer({ dirs: ['browser'] }); browser = await launch(); });
after(async () => { await browser.close(); await server.close(); });

async function open() {
  const ctx = await newContext(browser);
  const page = await ctx.newPage();
  const tracker = trackRequests(page);
  const cdp = await cdpSession(ctx, page);
  await page.goto(`${server.origin}/browser/layers.html`);
  await settle(page, cdp, { tracker });
  return { ctx, page, cdp, tracker };
}

test('an occurrence joins DOM facts with the accessibility tree role and name', async () => {
  const { ctx, cdp } = await open();
  const [occ] = await describeElements(cdp, '() => [document.getElementById("edit")]');
  assert.equal(occ.tag, 'button');
  assert.equal(occ.role, 'button');
  assert.equal(occ.name, 'Edit limits');
  assert.deepEqual(occ.classes, ['_root_ab12c_3', '_ghost_ab12c_9']);
  assert.deepEqual(occ.ancestors[0], ['_footer_x9y8z_12']);
  assert.deepEqual(occ.landmark, { role: 'main', name: '' });
  assert.equal(occ.heading, 'Card limits');
  assert.deepEqual(occ.data, { 'data-testid': 'edit-limits' });
  assert.equal(occ.layer, 'document');
  assert.equal(occ.rect.length, 4);
  const [again] = await describeElements(cdp, BY_XPATH, [occ.xpath]);
  assert.deepEqual(again, occ);
  await ctx.close();
});

test('no top layer on a plain page, even with a role=alert spotlight', async () => {
  const { ctx, cdp } = await open();
  assert.deepEqual(await findLayer(cdp, 'drawer'), { scope: 'document', role: 'document', name: '', xpath: null });
  assert.equal((await findLayer(cdp, 'page')).scope, 'document');
  await ctx.close();
});

for (const [kind, button, role, name] of [
  ['modal', 'open-modal', 'dialog', 'Edit card limits'],
  ['drawer', 'open-drawer', 'complementary', ''],
  ['drawer', 'show-aside', 'complementary', ''],
  ['menu', 'open-menu', 'menu', 'Move money'],
  ['toast', 'toast', 'generic', ''],
]) {
  test(`the ${kind} that #${button} opened is the top layer`, async () => {
    const { ctx, page, cdp, tracker } = await open();
    await armLayer(page);
    await page.click(`#${button}`);
    await settle(page, cdp, { tracker });
    const layer = await findLayer(cdp, kind);
    assert.equal(layer.scope, 'top');
    assert.equal(layer.role, role);
    assert.equal(layer.name, name);
    if (button === 'show-aside') assert.match(layer.xpath, /aside\[\d\]$/);
    const [inside] = await describeElements(cdp, `() => [document.evaluate(${JSON.stringify(layer.xpath)}, document, null, 9, null).singleNodeValue.querySelector('button, [role=menuitem], div')]`, null, { layerXPath: layer.xpath });
    if (kind !== 'toast') assert.equal(inside.layer, 'top');
    await ctx.close();
  });
}

test('a listbox inside a modal does not replace the modal as the top layer', async () => {
  const { ctx, page, cdp, tracker } = await open();
  await armLayer(page);
  await page.click('#open-modal');
  await settle(page, cdp, { tracker });
  const layer = await findLayer(cdp, 'form-state');
  assert.equal(layer.role, 'dialog');
  await ctx.close();
});
