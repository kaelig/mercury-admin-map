// Rule scan with axe-core (R8, KTD12): the top layer or the document, WCAG 2.2 A and AA rules only.
// Every violation and incomplete node becomes a result; incomplete ones are kept for the verifier.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { describeElements } from '../lib/occurrence.mjs';
import { cmp, sortCanonical } from '../lib/canon.mjs';
import { siteOf } from '../lib/browser.mjs';

export const id = 'axe';

const require = createRequire(import.meta.url);
let SOURCE = null;

// KTD12. Part of the harness hash, since it lives in a check module. Tests alter it in place.
export const AXE_OPTIONS = {
  runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'] },
  rules: {
    'label-content-name-mismatch': { enabled: true }, // 2.5.3, experimental
    'avoid-inline-spacing': { enabled: false }, // 1.4.12 is not tested (R20)
  },
  resultTypes: ['violations', 'incomplete'],
  ancestry: true,
  xpath: true,
  iframes: false,
};

// Rules whose absence means the configuration is wrong, not that the state is clean.
const REQUIRED = ['target-size', 'label-content-name-mismatch'];

// Inject axe through CDP: Runtime.evaluate is not subject to the page's script-src CSP.
export async function injectAxe(cdp) {
  const { result } = await cdp.send('Runtime.evaluate', { expression: 'typeof window.axe === "object" && window.axe.version', returnByValue: true });
  if (result.value) return;
  SOURCE ??= readFileSync(require.resolve('axe-core/axe.min.js'), 'utf8');
  const { exceptionDetails } = await cdp.send('Runtime.evaluate', { expression: SOURCE });
  if (exceptionDetails) throw new Error(`axe injection failed: ${exceptionDetails.exception?.description || exceptionDetails.text}`);
}

// Runs in the page. Returns plain data: one entry per violation or incomplete node, and rule ids.
// The failing elements stay in the page (NODES) for describeElements: an xpath round trip loses SVG
// nodes, which document.evaluate cannot find by tag name.
const NODES = "window[Symbol.for('a11y.axe.nodes')]";
const RUN = `async (xpath, options) => {
  const context = xpath ? document.evaluate(xpath, document, null, 9, null).singleNodeValue : document;
  if (!context) throw new Error('layer not found: ' + xpath);
  const r = await axe.run(context, { ...options, elementRef: true });
  const elements = [];
  const nodes = (type) => r[type].flatMap((rule) => rule.nodes.map((n) => {
    elements.push(n.element);
    return {
      rule: rule.id, tags: rule.tags, xpath: n.xpath[n.xpath.length - 1], failureSummary: n.failureSummary || '',
      checks: [...n.any, ...n.all, ...n.none].map((c) => ({ id: c.id, data: c.data ?? null })),
    };
  }));
  const violation = nodes('violations');
  const incomplete = nodes('incomplete');
  ${NODES} = elements;
  return {
    violation, incomplete,
    ran: [...r.violations, ...r.incomplete, ...r.passes, ...r.inapplicable].map((x) => x.id),
    inapplicable: r.inapplicable.map((x) => x.id),
  };
}`;

// wcag143 -> 1.4.3, wcag1410 -> 1.4.10, wcag2411 -> 2.4.11. Level tags (wcag2aa) don't match.
function criteriaOf(tags) {
  const out = [];
  for (const t of tags) {
    const m = /^wcag(\d)(\d)(\d+)$/.exec(t);
    if (m) out.push(`${m[1]}.${m[2]}.${m[3]}`);
  }
  const key = (c) => c.split('.').map(Number);
  return out.sort((a, b) => { const [x, y] = [key(a), key(b)]; return x[0] - y[0] || x[1] - y[1] || x[2] - y[2]; });
}

function evidenceOf(node) {
  const evidence = {};
  for (const c of node.checks) {
    if (c.data && typeof c.data === 'object' && !Array.isArray(c.data)) Object.assign(evidence, c.data);
    else if (c.data !== null) evidence[c.id] = c.data;
  }
  evidence.failureSummary = node.failureSummary;
  return evidence;
}

const messageKeyOf = (node) => node.checks.find((c) => c.data && typeof c.data === 'object' && c.data.messageKey)?.data.messageKey ?? null;

// Every frame below the main one. Query and hash are dropped: third-party frames put session tokens
// there, which would make two sweeps of the same build differ.
function framesOf(page) {
  const main = page.mainFrame();
  return page.frames().filter((f) => f !== main).map((f) => {
    const raw = f.url();
    if (!/^https?:/.test(raw)) return { url: raw, origin: 'null', firstParty: true }; // about:blank, srcdoc
    const u = new URL(raw);
    return { url: u.origin + u.pathname, origin: u.origin, firstParty: siteOf(raw) === siteOf(main.url()) };
  }).sort((a, b) => cmp(a.url, b.url));
}

export async function run(ctx) {
  const { cdp, page, layer } = ctx;
  await injectAxe(cdp);
  const { result, exceptionDetails } = await cdp.send('Runtime.evaluate', {
    expression: `(${RUN})(${JSON.stringify(layer?.xpath ?? null)}, ${JSON.stringify(AXE_OPTIONS)})`,
    awaitPromise: true, returnByValue: true,
  });
  if (exceptionDetails) throw new Error(`axe.run failed: ${exceptionDetails.exception?.description || exceptionDetails.text}`);
  const raw = result.value;
  const rulesRan = [...new Set(raw.ran)].sort();
  const missing = REQUIRED.filter((r) => !rulesRan.includes(r));
  if (missing.length) throw new Error(`axe did not run ${missing.join(', ')}: check AXE_OPTIONS (target-size needs the wcag22aa tag)`);

  const nodes = [...raw.violation.map((n) => ['violation', n]), ...raw.incomplete.map((n) => ['incomplete', n])];
  const occurrences = await describeElements(cdp, `() => ${NODES}`, null, { layerXPath: layer?.xpath ?? null });
  const results = nodes.map(([basis, n], i) => {
    if (!occurrences[i]) throw new Error(`axe node not found again: ${n.rule} at ${n.xpath}`);
    return {
      check: 'axe', rule: n.rule, basis, criteria: criteriaOf(n.tags),
      messageKey: basis === 'incomplete' ? messageKeyOf(n) : null,
      occurrence: occurrences[i], evidence: evidenceOf(n),
    };
  });
  return {
    results: sortCanonical(results), records: [], completed: true,
    detail: { rulesRan, inapplicable: [...new Set(raw.inapplicable)].sort(), frames: framesOf(page) },
  };
}
