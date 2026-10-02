// The fingerprint (KTD7): what proves a replay reached the screen its recipe was accepted on.
//
//   path        the URL path, each segment with a digit or over 20 characters replaced by <id>
//   top         the top layer's role and accessible name (lib/scope.mjs findLayer), or document
//   headings    heading names in that scope, in document order
//   controls    the sorted set of control names in that scope
//   structure   what the top layer is: dialog, menu, listbox, status, or document when none is open
//
// Names are KTD15 name templates, so a relative date or an amount that changes overnight still
// matches. Read from Chrome's accessibility tree: a hidden element (display: none, visibility:
// hidden, aria-hidden) is not in it, so "visible" means rendered and exposed. File format: schemas.md.
import { nameTemplate } from './lib/canon.mjs';
import { findLayer } from './lib/scope.mjs';

export const MIN_JACCARD = 0.6;

export const CONTROL_ROLES = new Set(['button', 'link', 'checkbox', 'radio', 'switch', 'tab', 'menuitem',
  'menuitemcheckbox', 'menuitemradio', 'option', 'textbox', 'searchbox', 'combobox', 'listbox', 'slider',
  'spinbutton', 'treeitem']);

const STATUS_ROLES = new Set(['status', 'alert', 'log']);

export function fingerprintPath(url) {
  let path;
  try {
    path = new URL(url).pathname;
  } catch {
    path = String(url || '').split(/[?#]/)[0];
  }
  if (path.length > 1) path = path.replace(/\/+$/, '');
  return path.split('/').map((seg) => (/\d/.test(seg) || seg.length > 20 ? '<id>' : seg)).join('/');
}

function structureOf(kind, top) {
  if (top.role === 'document') return 'document';
  if (top.role === 'menu' || top.role === 'listbox') return top.role;
  if (STATUS_ROLES.has(top.role) || kind === 'toast') return 'status';
  return 'dialog';
}

// The backend DOM node id of the element at `xpath`, or null.
async function backendIdOf(cdp, xpath) {
  const { result } = await cdp.send('Runtime.evaluate', {
    expression: `document.evaluate(${JSON.stringify(xpath)}, document, null, 9, null).singleNodeValue`,
    objectGroup: 'a11y-fingerprint',
  });
  try {
    if (result.subtype !== 'node') return null;
    const { node } = await cdp.send('DOM.describeNode', { objectId: result.objectId });
    return node.backendNodeId;
  } finally {
    await cdp.send('Runtime.releaseObjectGroup', { objectGroup: 'a11y-fingerprint' }).catch(() => {});
  }
}

// Accessibility nodes under `root` (inclusive), in tree order.
function subtree(nodes, root) {
  const byId = new Map(nodes.map((n) => [n.nodeId, n]));
  const out = [];
  const stack = [root];
  while (stack.length) {
    const n = stack.pop();
    if (!n) continue;
    out.push(n);
    for (const id of [...(n.childIds || [])].reverse()) stack.push(byId.get(id));
  }
  return out;
}

const nameOf = (n) => nameTemplate(n.name?.value ?? '');
const roleOf = (n) => n.role?.value ?? '';

// Capture the fingerprint of the state on screen. `kind` is the map node's kind (page, modal,
// drawer, menu, toast, ...). Arm the layer (lib/scope.mjs armLayer) before the final hop so a layer
// with no overlay role, like the demo's <aside> drawer, is found.
export async function captureFingerprint(page, cdp, kind) {
  const layer = await findLayer(cdp, kind);
  const { nodes } = await cdp.send('Accessibility.getFullAXTree');
  let scope = subtree(nodes, nodes.find((n) => !n.parentId) || nodes[0]);
  if (layer.scope === 'top' && layer.xpath) {
    const backend = await backendIdOf(cdp, layer.xpath);
    const root = nodes.find((n) => n.backendDOMNodeId === backend);
    if (root) scope = subtree(nodes, root);
  }
  const shown = scope.filter((n) => !n.ignored);
  const headings = [...new Set(shown.filter((n) => roleOf(n) === 'heading').map(nameOf).filter(Boolean))];
  const controls = [...new Set(shown.filter((n) => CONTROL_ROLES.has(roleOf(n))).map(nameOf).filter(Boolean))].sort();
  const top = layer.scope === 'top' ? { role: layer.role, name: nameTemplate(layer.name) } : { role: 'document', name: '' };
  return { path: fingerprintPath(page.url()), top, headings, controls, structure: structureOf(kind, top) };
}

function jaccard(a, b) {
  const A = new Set((a || []).map(nameTemplate));
  const B = new Set((b || []).map(nameTemplate));
  if (!A.size && !B.size) return 1;
  let both = 0;
  for (const x of A) if (B.has(x)) both += 1;
  return both / (A.size + B.size - both);
}

const showTop = (t) => `${t?.role || 'document'} "${t?.name || ''}"`;

// Does `actual` show the same state as `expected`? Path and top layer must be equal after
// normalization; the control sets must overlap by MIN_JACCARD or more. Returns {ok, why, jaccard}.
export function matchFingerprint(expected, actual) {
  const why = [];
  if (fingerprintPath(`http://x${expected.path}`) !== fingerprintPath(`http://x${actual.path}`)) {
    why.push(`path ${actual.path} is not ${expected.path}`);
  }
  const et = expected.top || { role: 'document', name: '' };
  const at = actual.top || { role: 'document', name: '' };
  if (et.role !== at.role || nameTemplate(et.name) !== nameTemplate(at.name)) {
    why.push(`top layer ${showTop(at)} is not ${showTop(et)}`);
  }
  const j = jaccard(expected.controls, actual.controls);
  if (j < MIN_JACCARD) why.push(`controls overlap ${j.toFixed(2)}, below ${MIN_JACCARD}`);
  return { ok: why.length === 0, why: why.length ? why.join('; ') : null, jaccard: Math.round(j * 10000) / 10000 };
}
