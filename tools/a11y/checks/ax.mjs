// Announced-as records (R12, KTD13): what a screen reader would announce for each interactive,
// landmark and heading node in scope, read from Chrome's accessibility tree and rendered in the
// template `role "name" [states] (description)` (schemas.md "Record").
import { nameTemplate, sortCanonical } from '../lib/canon.mjs';
import { describeBackendNodes } from '../lib/occurrence.mjs';

export const id = 'records';

// Chrome's role strings. The capitalized ones are native controls with no ARIA role name.
export const INTERACTIVE = new Set(['button', 'checkbox', 'combobox', 'link', 'listbox', 'menuitem', 'menuitemcheckbox',
  'menuitemradio', 'option', 'radio', 'searchbox', 'slider', 'spinbutton', 'switch', 'tab', 'textbox', 'treeitem',
  'DisclosureTriangle', 'Date', 'DateTime', 'InputTime', 'ColorWell']);
const LANDMARK = new Set(['banner', 'complementary', 'contentinfo', 'form', 'main', 'navigation', 'region', 'search']);
// Headings, plus the window and live-region roles that carry the `modal` and `live` states.
const STRUCTURE = new Set(['heading', 'dialog', 'alertdialog', 'status', 'alert', 'log', 'marquee', 'timer']);
const RECORDED = new Set([...INTERACTIVE, ...LANDMARK, ...STRUCTURE]);
const RANGE = new Set(['slider', 'spinbutton', 'scrollbar']);

// Subtrees that hold no page targets: a closed <select>'s options, the user-agent shadow parts of
// native date, time and color inputs. (Frame documents are never part of the main frame's tree.)
const SKIP_CHILDREN = new Set(['MenuListPopup', 'Date', 'DateTime', 'InputTime', 'ColorWell']);

export const STATE_ORDER = ['checked', 'pressed', 'selected', 'expanded', 'haspopup', 'disabled', 'readonly', 'required',
  'invalid', 'modal', 'multiselectable', 'level', 'valuenow', 'valuetext', 'live', 'errormessage'];

const clean = (s) => String(s ?? '').replace(/\s+/g, ' ').trim();

// An occurrence in the page's own DOM. Nodes in a user-agent shadow tree get an xpath that does
// not start at <html> and cannot be resolved again.
export const inDocument = (occ) => Boolean(occ && occ.xpath.startsWith('/html/'));

async function backendIdOf(cdp, xpath) {
  const group = 'a11y-ax-scope';
  try {
    const { result } = await cdp.send('Runtime.evaluate', {
      expression: `document.evaluate(${JSON.stringify(xpath)}, document, null, 9, null).singleNodeValue`, objectGroup: group,
    });
    if (result.subtype !== 'node') throw new Error(`layer not found: ${xpath}`);
    const { node } = await cdp.send('DOM.describeNode', { objectId: result.objectId });
    return node.backendNodeId;
  } finally {
    await cdp.send('Runtime.releaseObjectGroup', { objectGroup: group }).catch(() => {});
  }
}

// The accessibility tree in scope: the layer's subtree, or the whole document, in tree order.
// `nodes` holds the non-ignored nodes; `text(backendId)` reads the text inside any node of the tree.
export async function axInScope(cdp, layer) {
  const { nodes: all } = await cdp.send('Accessibility.getFullAXTree');
  const byId = new Map(all.map((n) => [n.nodeId, n]));
  let root = all.find((n) => !n.parentId && n.role?.value === 'RootWebArea') || all[0];
  if (layer?.xpath) {
    const backend = await backendIdOf(cdp, layer.xpath);
    root = all.find((n) => n.backendDOMNodeId === backend);
    if (!root) throw new Error(`layer is not in the accessibility tree: ${layer.xpath}`);
  }
  const nodes = [];
  const stack = root ? [root] : [];
  while (stack.length) {
    const n = stack.pop();
    if (!n.ignored) nodes.push(n);
    if (SKIP_CHILDREN.has(n.role?.value)) continue;
    const kids = (n.childIds || []).map((c) => byId.get(c)).filter(Boolean);
    for (let i = kids.length - 1; i >= 0; i--) stack.push(kids[i]);
  }
  const byBackend = new Map(all.filter((n) => n.backendDOMNodeId).map((n) => [n.backendDOMNodeId, n]));
  const textOf = (n) => (n.role?.value === 'StaticText' ? n.name?.value || ''
    : (n.childIds || []).map((c) => byId.get(c)).filter(Boolean).map(textOf).join(' '));
  const text = (backendId) => (byBackend.has(backendId) ? clean(textOf(byBackend.get(backendId))) : '');
  return { nodes, text };
}

const tristate = (v) => (v === 'true' ? true : v === 'false' ? false : v);

// States in schemas.md's fixed order. A false flag (disabled, readonly, required, modal,
// multiselectable), invalid=false, haspopup=false and live=off say nothing and are left out.
export function statesOf(node, text) {
  const p = Object.fromEntries((node.properties || []).map((x) => [x.name, x.value]));
  const role = node.role?.value;
  const s = {};
  if (p.checked) s.checked = tristate(p.checked.value);
  if (p.pressed) s.pressed = tristate(p.pressed.value);
  if (p.selected) s.selected = p.selected.value;
  if (p.expanded) s.expanded = p.expanded.value;
  if (p.hasPopup && p.hasPopup.value !== 'false') s.haspopup = p.hasPopup.value;
  for (const k of ['disabled', 'readonly', 'required']) if (p[k]?.value === true) s[k] = true;
  if (p.invalid && p.invalid.value !== 'false') s.invalid = tristate(p.invalid.value);
  for (const k of ['modal', 'multiselectable']) if (p[k]?.value === true) s[k] = true;
  if (p.level) s.level = p.level.value;
  if (RANGE.has(role) && typeof node.value?.value === 'number') s.valuenow = node.value.value;
  if (p.valuetext && p.valuetext.value !== '' && p.valuetext.value !== String(s.valuenow)) s.valuetext = p.valuetext.value;
  if (p.live && p.live.value !== 'off') s.live = p.live.value;
  if (p.errormessage?.relatedNodes?.length) s.errormessage = p.errormessage.relatedNodes.map((r) => text(r.backendDOMNodeId)).join(' ').trim();
  return s;
}

// `role "name" [k=v, ...] (description)`. Names, descriptions and error text are name templates.
export function templateOf(role, name, states, description) {
  let t = `${role} ${JSON.stringify(nameTemplate(name))}`;
  const kv = STATE_ORDER.filter((k) => k in states).map((k) => `${k}=${k === 'errormessage' ? nameTemplate(states[k]) : states[k]}`);
  if (kv.length) t += ` [${kv.join(', ')}]`;
  const d = nameTemplate(description);
  if (d) t += ` (${d})`;
  return t;
}

export async function run(ctx) {
  const { cdp, layer } = ctx;
  const { nodes, text } = await axInScope(cdp, layer);
  const kept = nodes.filter((n) => RECORDED.has(n.role?.value) && n.backendDOMNodeId);
  const occurrences = await describeBackendNodes(cdp, kept.map((n) => n.backendDOMNodeId), { layerXPath: layer?.xpath ?? null });
  const records = [];
  kept.forEach((n, i) => {
    if (!inDocument(occurrences[i])) return;
    const role = n.role.value;
    const name = clean(n.name?.value);
    const description = clean(n.description?.value);
    const states = statesOf(n, text);
    records.push({ role, name, states, description, template: templateOf(role, name, states, description), occurrence: occurrences[i] });
  });
  return { results: [], records: sortCanonical(records), detail: null, completed: true };
}
