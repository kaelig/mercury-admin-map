// Occurrences (schemas.md): the DOM facts a finding key needs, plus the element's computed role and
// name from Chrome's accessibility tree. Every check describes elements through this module.
import { clean } from './canon.mjs';

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

// Runs in the page with the elements as the arguments after `layerXPath` (the top layer's xpath or
// null): each element's DOM facts, or null for a non-element (the facts throw on a text node). The
// layer and the heading list are read once for all of them.
const DESCRIBE = `function (layerXPath, ...els) {
  const xpathOf = (n) => (${XPATH_FN}).call(n);
  const text = (n) => (n ? (n.innerText || n.textContent || '').replace(/\\s+/g, ' ').trim() : '');
  const labelOf = (n) => {
    const by = n.getAttribute('aria-labelledby');
    if (by) return by.split(/\\s+/).map((id) => text(document.getElementById(id))).filter(Boolean).join(' ');
    return n.getAttribute('aria-label') || '';
  };
  const LANDMARKS = { MAIN: 'main', NAV: 'navigation', ASIDE: 'complementary', HEADER: 'banner', FOOTER: 'contentinfo', FORM: 'form', SEARCH: 'search' };
  const ROLES = ['main', 'navigation', 'complementary', 'banner', 'contentinfo', 'form', 'search', 'region', 'dialog', 'alertdialog'];
  const layer = layerXPath ? document.evaluate(layerXPath, document, null, 9, null).singleNodeValue : null;
  const headings = [...(layer || document).querySelectorAll('h1,h2,h3,h4,h5,h6,[role=heading]')];
  const headingText = new Map();
  const facts = (el) => {
    let landmark = null;
    for (let p = el.parentElement; p && p !== document.documentElement; p = p.parentElement) {
      const role = p.getAttribute('role') || LANDMARKS[p.tagName] || (p.tagName === 'DIALOG' ? 'dialog' : null);
      if (role && ROLES.includes(role)) { landmark = { role, name: labelOf(p) }; break; }
    }
    // The last heading before the element in document order, other than the element or one around it.
    let before = null;
    for (const h of headings) {
      if (h === el || h.contains(el)) continue;
      if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) before = h;
    }
    if (before && !headingText.has(before)) headingText.set(before, text(before).slice(0, 120));
    const heading = before ? headingText.get(before) : '';
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
  };
  return els.map((el) => {
    try {
      return facts(el);
    } catch {
      return null;
    }
  });
}`;

// Elements per DESCRIBE call.
const CHUNK = 200;

// The DOM facts of each element handle, in order (null for a non-element or a handle that has gone).
// One call per chunk, with the chunk's first element as the call's target.
async function domFacts(cdp, objectIds, layerXPath) {
  const call = (ids) => cdp.send('Runtime.callFunctionOn', {
    objectId: ids[0], functionDeclaration: DESCRIBE, returnByValue: true,
    arguments: [{ value: layerXPath ?? null }, ...ids.map((objectId) => ({ objectId }))],
  }).then(({ result, exceptionDetails }) => (exceptionDetails ? null : result.value), () => null);
  const describeChunk = async (ids) => {
    const facts = await call(ids);
    if (facts) return facts;
    // The call fails as a whole when one handle has gone: describe each element on its own.
    return Promise.all(ids.map(async (id) => (await call([id]))?.[0] ?? null));
  };
  const chunks = [];
  for (let i = 0; i < objectIds.length; i += CHUNK) chunks.push(objectIds.slice(i, i + CHUNK));
  return (await Promise.all(chunks.map(describeChunk))).flat();
}

async function axOf(cdp, objectId) {
  const { nodes } = await cdp.send('Accessibility.getPartialAXTree', { objectId, fetchRelatives: false });
  const n = nodes.find((x) => !x.ignored) || nodes[0];
  if (!n || n.ignored) return { role: '', name: '' };
  return { role: n.role?.value ?? '', name: clean(n.name?.value) };
}

// The partial accessibility tree's role and name for each element handle, in order.
const axFacts = (cdp, objectIds) => Promise.all(objectIds.map((id) => axOf(cdp, id).catch(() => ({ role: '', name: '' }))));

// An occurrence from an element's DOM facts and its role and name; null without DOM facts.
function occurrenceOf(d, ax) {
  if (!d || typeof d !== 'object') return null;
  return { tag: d.tag, role: ax.role, name: ax.name, classes: d.classes, ancestors: d.ancestors, landmark: d.landmark,
    heading: d.heading, data: d.data, xpath: d.xpath, rect: d.rect, layer: d.layer };
}

// `values` laid out over `ids`: the next value for each id, null where the id is null.
function alongside(ids, values) {
  let k = 0;
  return ids.map((id) => (id ? values[k++] : null));
}

// Occurrences of element handles (null entries stay null): the DOM facts and the accessibility node,
// fetched together.
async function describeObjects(cdp, objectIds, layerXPath) {
  const live = objectIds.filter(Boolean);
  const [facts, axes] = await Promise.all([domFacts(cdp, live, layerXPath), axFacts(cdp, live)]);
  return alongside(objectIds, live.map((_, i) => occurrenceOf(facts[i], axes[i])));
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

export const releaseGroup = (cdp, objectGroup) => cdp.send('Runtime.releaseObjectGroup', { objectGroup }).catch(() => {});

// Describe the elements `fn(args)` returns in the page, in order (null where it returned no element).
// It and describeAXNodes share one object group and release it when done: never run them at once.
export async function describeElements(cdp, fn, args, { layerXPath = null } = {}) {
  try {
    return await describeObjects(cdp, await elementIds(cdp, fn, args), layerXPath);
  } finally {
    await releaseGroup(cdp, GROUP);
  }
}

// Describe the element an expression evaluates to (null when it is not one), keeping its handle in
// `objectGroup` for the caller to release: a loop that describes one element per step (the Tab
// walk) releases its group once, at the end.
export async function describeEvaluated(cdp, expression, objectGroup, { layerXPath = null } = {}) {
  const { result, exceptionDetails } = await cdp.send('Runtime.evaluate', { expression, objectGroup, returnByValue: false });
  if (exceptionDetails) throw new Error(`in-page function failed: ${exceptionDetails.exception?.description || exceptionDetails.text}`);
  if (result.subtype !== 'node') return null;
  const [occurrence] = await describeObjects(cdp, [result.objectId], layerXPath);
  return occurrence;
}

// Describe non-ignored accessibility-tree nodes that have a backend DOM node id: the DOM facts from
// the page, the role and name from the node itself (what the partial tree gives for its element).
export async function describeAXNodes(cdp, nodes, { layerXPath = null } = {}) {
  try {
    const ids = await Promise.all(nodes.map(async (n) => {
      const { object } = await cdp.send('DOM.resolveNode', { backendNodeId: n.backendDOMNodeId, objectGroup: GROUP }).catch(() => ({ object: null }));
      return object?.objectId && object.subtype === 'node' ? object.objectId : null;
    }));
    const facts = alongside(ids, await domFacts(cdp, ids.filter(Boolean), layerXPath));
    return nodes.map((n, i) => occurrenceOf(facts[i], { role: n.role?.value ?? '', name: clean(n.name?.value) }));
  } finally {
    await releaseGroup(cdp, GROUP);
  }
}

// An expression for Runtime.evaluate: the node at `xpath`, or null. A plain document.evaluate, so an
// SVG step finds nothing (BY_XPATH below handles those).
export const findExpr = (xpath) => `document.evaluate(${JSON.stringify(xpath)}, document, null, 9, null).singleNodeValue`;

// The backend DOM node id of the node at `xpath`, or null when there is none.
export async function backendIdOf(cdp, xpath) {
  const group = 'a11y-backend-id';
  try {
    const { result } = await cdp.send('Runtime.evaluate', { expression: findExpr(xpath), objectGroup: group });
    if (result.subtype !== 'node') return null;
    const { node } = await cdp.send('DOM.describeNode', { objectId: result.objectId });
    return node.backendNodeId;
  } finally {
    await cdp.send('Runtime.releaseObjectGroup', { objectGroup: group }).catch(() => {});
  }
}

// In-page helper source: resolve xpaths to elements. Use as describeElements(cdp, BY_XPATH, xpaths).
// SVG and MathML steps need local-name(): document.evaluate does not match them by tag name.
export const BY_XPATH = `(xs) => xs.map((x) => {
  const find = (p) => document.evaluate(p, document, null, 9, null).singleNodeValue;
  return find(x) || find(x.replace(/\\/([a-z][\\w-]*)(\\[\\d+\\])?/gi, (_, n, i) => "/*[local-name()='" + n + "']" + (i || '')));
})`;
