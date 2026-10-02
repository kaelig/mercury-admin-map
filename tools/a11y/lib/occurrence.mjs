// Occurrences (schemas.md): the DOM facts a finding key needs, plus the element's computed role and
// name from Chrome's accessibility tree. Every check describes elements through this module.

const GROUP = 'a11y-occurrence';

// An element's xpath with same-tag sibling indices, e.g. /html/body/div[2]/main[1]/button[3].
export const XPATH_FN = `function () {
  const parts = [];
  for (let x = this; x && x.nodeType === 1; x = x.parentElement) {
    let i = 1;
    for (let s = x.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === x.tagName) i++;
    parts.unshift(x.tagName.toLowerCase() + '[' + i + ']');
  }
  return '/' + parts.join('/').replace(/^html\\[1\\]/, 'html').replace(/^html\\/body\\[1\\]/, 'html/body');
}`;

// Runs in the page with `this` bound to the element. `layerXPath` is the top layer's xpath or null.
const DESCRIBE = `function (layerXPath) {
  const el = this;
  const xpathOf = (n) => (${XPATH_FN}).call(n);
  const text = (n) => (n ? (n.innerText || n.textContent || '').replace(/\\s+/g, ' ').trim() : '');
  const labelOf = (n) => {
    const by = n.getAttribute('aria-labelledby');
    if (by) return by.split(/\\s+/).map((id) => text(document.getElementById(id))).filter(Boolean).join(' ');
    return n.getAttribute('aria-label') || '';
  };
  const LANDMARKS = { MAIN: 'main', NAV: 'navigation', ASIDE: 'complementary', HEADER: 'banner', FOOTER: 'contentinfo', FORM: 'form', SEARCH: 'search' };
  const ROLES = ['main', 'navigation', 'complementary', 'banner', 'contentinfo', 'form', 'search', 'region', 'dialog', 'alertdialog'];
  let landmark = null;
  for (let p = el.parentElement; p && p !== document.documentElement; p = p.parentElement) {
    const role = p.getAttribute('role') || LANDMARKS[p.tagName] || (p.tagName === 'DIALOG' ? 'dialog' : null);
    if (role && ROLES.includes(role)) { landmark = { role, name: labelOf(p) }; break; }
  }
  const layer = layerXPath ? document.evaluate(layerXPath, document, null, 9, null).singleNodeValue : null;
  const scope = layer || document;
  let heading = '';
  for (const h of scope.querySelectorAll('h1,h2,h3,h4,h5,h6,[role=heading]')) {
    if (h === el || h.contains(el)) continue;
    if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) heading = text(h).slice(0, 120);
  }
  const ancestors = [];
  for (let p = el.parentElement; p && p !== document.body && ancestors.length < 8; p = p.parentElement) ancestors.push([...p.classList]);
  const data = {};
  for (const a of el.attributes) if (a.name.startsWith('data-') && !/\\d/.test(a.value)) data[a.name] = a.value;
  const r = el.getBoundingClientRect();
  return {
    tag: el.tagName.toLowerCase(),
    classes: [...el.classList],
    ancestors,
    landmark,
    heading,
    data,
    xpath: xpathOf(el),
    rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
    layer: layer && layer.contains(el) ? 'top' : 'document',
  };
}`;

async function axOf(cdp, objectId) {
  const { nodes } = await cdp.send('Accessibility.getPartialAXTree', { objectId, fetchRelatives: false });
  const n = nodes.find((x) => !x.ignored) || nodes[0];
  if (!n || n.ignored) return { role: '', name: '' };
  return { role: n.role?.value ?? '', name: (n.name?.value ?? '').replace(/\s+/g, ' ').trim() };
}

async function describeObject(cdp, objectId, layerXPath) {
  const { result } = await cdp.send('Runtime.callFunctionOn', {
    objectId, functionDeclaration: DESCRIBE, arguments: [{ value: layerXPath ?? null }], returnByValue: true,
  });
  const ax = await axOf(cdp, objectId);
  const d = result.value;
  return { tag: d.tag, role: ax.role, name: ax.name, classes: d.classes, ancestors: d.ancestors, landmark: d.landmark,
    heading: d.heading, data: d.data, xpath: d.xpath, rect: d.rect, layer: d.layer };
}

// Object ids of the elements an in-page function returns. `fn` is a function source taking `args`
// and returning an array (or NodeList) of elements; null entries are kept as null.
async function elementIds(cdp, fn, args) {
  const { result, exceptionDetails } = await cdp.send('Runtime.evaluate', {
    expression: `Array.from((${fn})(${JSON.stringify(args ?? null)}) || [])`, objectGroup: GROUP, returnByValue: false,
  });
  if (exceptionDetails) throw new Error(`in-page function failed: ${exceptionDetails.exception?.description || exceptionDetails.text}`);
  const { result: props } = await cdp.send('Runtime.getProperties', { objectId: result.objectId, ownProperties: true });
  return props.filter((p) => /^\d+$/.test(p.name)).sort((a, b) => a.name - b.name)
    .map((p) => (p.value?.subtype === 'node' ? p.value.objectId : null));
}

// Describe the elements `fn(args)` returns in the page, in order (null where it returned no element).
export async function describeElements(cdp, fn, args, { layerXPath = null } = {}) {
  try {
    const ids = await elementIds(cdp, fn, args);
    const out = [];
    for (const id of ids) out.push(id ? await describeObject(cdp, id, layerXPath) : null);
    return out;
  } finally {
    await cdp.send('Runtime.releaseObjectGroup', { objectGroup: GROUP }).catch(() => {});
  }
}

// Describe DOM nodes by backend node id (from the accessibility tree or DOM domain).
export async function describeBackendNodes(cdp, backendNodeIds, { layerXPath = null } = {}) {
  try {
    const out = [];
    for (const backendNodeId of backendNodeIds) {
      const { object } = await cdp.send('DOM.resolveNode', { backendNodeId, objectGroup: GROUP }).catch(() => ({ object: null }));
      out.push(object?.objectId && object.subtype === 'node' ? await describeObject(cdp, object.objectId, layerXPath) : null);
    }
    return out;
  } finally {
    await cdp.send('Runtime.releaseObjectGroup', { objectGroup: GROUP }).catch(() => {});
  }
}

// In-page helper source: resolve xpaths to elements. Use as describeElements(cdp, BY_XPATH, xpaths).
export const BY_XPATH = '(xs) => xs.map((x) => document.evaluate(x, document, null, 9, null).singleNodeValue)';
