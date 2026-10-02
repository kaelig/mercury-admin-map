// The top layer of a state (KTD11): an open modal, alert dialog, drawer, menu, listbox popup or
// popover, or the toast on a toast state. Checks scan that layer instead of the page behind it.
//
// Roles alone are not enough on the demo: its transaction drawer is a plain <aside>, and onboarding
// spotlights use role=alert. So armLayer() records which elements are visible before a state's final
// hop, and findLayer() prefers a layer that appeared after it, chosen by the state's kind.

import { XPATH_FN } from './occurrence.mjs';

const BEFORE = "Symbol.for('a11y.layer.before')";
const BEFORE_TEXT = "Symbol.for('a11y.layer.liveText')";
const LIVE = '[role=status],[role=alert],[aria-live=polite],[aria-live=assertive]';

// Kinds whose top layer, when open, is what gets scanned (schemas.md "State kinds").
const OVERLAY_KINDS = new Set(['modal', 'drawer', 'menu', 'popover', 'toast']);
const OPTIONAL_KINDS = new Set(['form-state', 'empty-state', 'confirmation', 'validation-error']);

// Record the elements visible now, so findLayer() can tell which layer the next action opened or
// revealed (the demo keeps its transaction drawer in the DOM, hidden, until a row is clicked).
// Live regions are remembered with their text: a toast is often written into a status region that
// was already on the page, empty.
export function armLayer(page) {
  return page.evaluate(`window[${BEFORE}] = new WeakSet([...document.querySelectorAll('body *')]
    .filter((e) => e.checkVisibility({ opacityProperty: true, visibilityProperty: true })));
    window[${BEFORE_TEXT}] = new WeakMap([...document.querySelectorAll(${JSON.stringify(LIVE)})]
    .map((e) => [e, (e.innerText || '').trim()]))`);
}

const FIND = `(kind) => {
  const before = window[${BEFORE}];
  const visible = (e) => {
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && Number(s.opacity) > 0
      && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
  };
  const liveText = window[${BEFORE_TEXT}];
  const isNew = (e) => !before || !before.has(e) || (liveText?.has(e) && liveText.get(e) !== (e.innerText || '').trim());
  const role = (e) => e.getAttribute('role') || (e.tagName === 'DIALOG' ? 'dialog' : e.tagName === 'ASIDE' ? 'complementary' : '');
  const positioned = (e) => ['fixed', 'absolute', 'sticky'].includes(getComputedStyle(e).position);
  const modalish = (e) => ['dialog', 'alertdialog'].includes(role(e)) && (e.getAttribute('aria-modal') === 'true' || (e.tagName === 'DIALOG' && e.matches(':modal')) || role(e) === 'alertdialog');
  const popoverOpen = (e) => { try { return e.matches(':popover-open'); } catch { return false; } };
  const ROLE_SEL = '[role=dialog],[role=alertdialog],[role=menu],[role=listbox],[role=tooltip],dialog[open],[aria-modal=true],[popover]';
  const byRole = [...document.querySelectorAll(ROLE_SEL)].filter((e) => visible(e) && (e.tagName !== 'DIALOG' || e.open) && (!e.hasAttribute('popover') || popoverOpen(e)));
  // Layers the last action opened that carry no overlay role: a positioned container, or an aside.
  const fresh = before ? [...document.querySelectorAll('body *')].filter((e) => isNew(e) && visible(e)
    && (positioned(e) || e.tagName === 'ASIDE') && (e.innerText || '').trim()) : [];
  const status = [...document.querySelectorAll(${JSON.stringify(LIVE)})]
    .filter((e) => visible(e) && (e.innerText || '').trim());
  let pool;
  if (kind === 'menu') pool = byRole.filter((e) => ['menu', 'listbox'].includes(role(e)));
  else if (kind === 'modal') pool = byRole.filter(modalish).concat(byRole.filter((e) => role(e) === 'dialog'));
  else if (kind === 'drawer') pool = byRole.filter((e) => ['dialog', 'alertdialog'].includes(role(e))).concat(fresh.filter((e) => e.tagName === 'ASIDE' || role(e) === 'complementary'), fresh);
  else if (kind === 'popover') pool = byRole.concat(fresh);
  else if (kind === 'toast') pool = status.filter(isNew).concat(fresh, status);
  else pool = byRole.filter(modalish).concat(byRole.filter((e) => ['dialog', 'alertdialog'].includes(role(e))), fresh.filter((e) => e.tagName === 'ASIDE'));
  pool = pool.filter((e, i) => pool.indexOf(e) === i);
  if (!pool.length) return null;
  // Prefer what the last action opened, then the outermost candidate, then the last in document order.
  const newer = pool.filter(isNew);
  const set = newer.length ? newer : pool;
  const outer = set.filter((e) => !set.some((o) => o !== e && o.contains(e)));
  return (outer.length ? outer : set)[0];
}`;

const DOCUMENT = { scope: 'document', role: 'document', name: '', xpath: null };

// The layer a state's checks cover: { scope: 'top' | 'document', role, name, xpath }.
export async function findLayer(cdp, kind) {
  if (!OVERLAY_KINDS.has(kind) && !OPTIONAL_KINDS.has(kind)) return { ...DOCUMENT };
  const { result } = await cdp.send('Runtime.evaluate', { expression: `(${FIND})(${JSON.stringify(kind)})`, objectGroup: 'a11y-scope' });
  try {
    if (result.subtype !== 'node') return { ...DOCUMENT };
    const { result: x } = await cdp.send('Runtime.callFunctionOn', { objectId: result.objectId, functionDeclaration: XPATH_FN, returnByValue: true });
    const { nodes } = await cdp.send('Accessibility.getPartialAXTree', { objectId: result.objectId, fetchRelatives: false });
    const n = nodes.find((v) => !v.ignored) || {};
    return { scope: 'top', role: n.role?.value || 'generic', name: (n.name?.value || '').replace(/\s+/g, ' ').trim(), xpath: x.value };
  } finally {
    await cdp.send('Runtime.releaseObjectGroup', { objectGroup: 'a11y-scope' }).catch(() => {});
  }
}
