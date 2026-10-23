// The expected-interactive set (KTD13): the accessibility tree's interactive roles in scope, plus
// the topmost `cursor: pointer` elements that are not focusable and have no focusable ancestor or
// descendant. It feeds the Tab walk and target size; it reports nothing by itself.
import { describeElements } from '../lib/occurrence.mjs';
import { sortCanonical } from '../lib/canon.mjs';
import { INTERACTIVE, inDocument, occurrencesOf, treeOf } from './ax.mjs';

export const id = 'interactive';

// Runs in the page. A pointer element is topmost when its parent's cursor is not a pointer (the
// cursor is inherited, so the children of a clickable card are pointers too). A label tied to a
// control is that control's target, not one of its own.
const POINTER = `(layerXPath) => {
  const root = layerXPath ? document.evaluate(layerXPath, document, null, 9, null).singleNodeValue : document.body;
  if (!root) return [];
  const FOCUSABLE = 'a[href], area[href], button, input:not([type=hidden]), select, textarea, iframe, summary,'
    + ' audio[controls], video[controls], [tabindex], [contenteditable]:not([contenteditable=false])';
  const seen = new Map();
  const pointer = (e) => {
    if (!seen.has(e)) seen.set(e, getComputedStyle(e).cursor === 'pointer');
    return seen.get(e);
  };
  const out = [];
  for (const e of [root, ...root.querySelectorAll('*')]) {
    if (!pointer(e) || (e.parentElement && pointer(e.parentElement))) continue;
    if (e.matches(FOCUSABLE) || e.parentElement?.closest(FOCUSABLE) || e.querySelector(FOCUSABLE)) continue;
    if (e.tagName === 'LABEL' && e.control) continue;
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height || !e.checkVisibility({ opacityProperty: true, visibilityProperty: true })) continue;
    out.push(e);
  }
  return out;
}`;

const propOf = (node, name) => (node.properties || []).some((p) => p.name === name && p.value.value === true);

export async function run(ctx) {
  const { cdp, layer } = ctx;
  const layerXPath = layer?.xpath ?? null;
  const { nodes } = await treeOf(ctx);
  const fromTree = nodes.filter((n) => INTERACTIVE.has(n.role?.value) && n.backendDOMNodeId);
  // The ax check, just before this one, has described these nodes already.
  const treeOccurrences = await occurrencesOf(ctx, fromTree);
  const pointerOccurrences = await describeElements(cdp, POINTER, layerXPath, { layerXPath });

  const seen = new Set();
  const detail = [];
  const add = (occ, focusable, disabled, source) => {
    if (!inDocument(occ) || seen.has(occ.xpath)) return;
    seen.add(occ.xpath);
    detail.push({ ...occ, focusable, disabled, source });
  };
  // Disabled controls stay in the set (target size measures them) but are flagged: Tab skips them by design.
  fromTree.forEach((n, i) => add(treeOccurrences[i], propOf(n, 'focusable'), propOf(n, 'disabled'), 'ax'));
  for (const occ of pointerOccurrences) add(occ, false, false, 'pointer');
  return { results: [], records: [], detail: sortCanonical(detail), completed: true };
}
