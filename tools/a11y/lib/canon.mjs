// Canonical JSON, name templates and hashes. Mirrors lib/canon.py byte for byte (schemas.md).
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, writeFileSync, appendFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');

// ---------------------------------------------------------------------------
// Serialization. Written by hand: JSON.stringify would put integer-like keys first.
// ---------------------------------------------------------------------------

function num(n) {
  if (!Number.isFinite(n)) throw new Error('non-finite number');
  if (Number.isInteger(n)) return Object.is(n, -0) ? '0' : String(n);
  const r = Math.floor(n * 10000 + 0.5) / 10000;
  return r === 0 ? '0' : String(r);
}

function ser(v, indent, level) {
  if (v === null || v === undefined) return 'null';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'number') return num(v);
  if (typeof v === 'string') return JSON.stringify(v);
  const pad = indent ? '\n' + ' '.repeat(level + 1) : '';
  const end = indent ? '\n' + ' '.repeat(level) : '';
  if (Array.isArray(v)) {
    if (!v.length) return '[]';
    return '[' + v.map((x) => pad + ser(x, indent, level + 1)).join(',') + end + ']';
  }
  if (typeof v === 'object') {
    const keys = Object.keys(v).filter((k) => v[k] !== undefined).sort();
    if (!keys.length) return '{}';
    const colon = indent ? ': ' : ':';
    return '{' + keys.map((k) => pad + JSON.stringify(k) + colon + ser(v[k], indent, level + 1)).join(',') + end + '}';
  }
  throw new Error(`cannot serialize ${typeof v}`);
}

export const canonical = (obj) => ser(obj, false, 0);
export const dump = (obj) => ser(obj, true, 0) + '\n';
export const sha = (obj) => createHash('sha256').update(canonical(obj)).digest('hex');
export const sha1 = (obj) => createHash('sha1').update(canonical(obj)).digest('hex');

export function writeText(path, text) {
  mkdirSync(dirname(resolve(path)), { recursive: true });
  const tmp = `${path}.tmp-${process.pid}`;
  writeFileSync(tmp, text);
  renameSync(tmp, path);
}

export const writeJson = (path, obj) => writeText(path, dump(obj));

export function readJson(path, fallback = null) {
  if (!existsSync(path)) return fallback;
  return JSON.parse(readFileSync(path, 'utf8'));
}

export function readJsonl(path) {
  if (!existsSync(path)) return [];
  return readFileSync(path, 'utf8').split('\n').filter((l) => l.trim()).map((l) => JSON.parse(l));
}

export function appendJsonl(path, objs) {
  mkdirSync(dirname(resolve(path)), { recursive: true });
  appendFileSync(path, objs.map((o) => canonical(o) + '\n').join(''));
}

// ---------------------------------------------------------------------------
// Name templates (KTD15)
// ---------------------------------------------------------------------------

const WS = /[ \t\n\r\f\v  -   　]+/g;
const MONTH = '(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?' +
  '|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\\.?';
const UNIT = '(?:second|minute|hour|day|week|month|year)s?';
const STEPS = [
  [new RegExp('\\b(?:today|yesterday|tomorrow)\\b', 'gi'), '<date>'],
  [new RegExp(`\\b(?:\\d+|an?) ${UNIT} ago\\b`, 'gi'), '<date>'],
  [new RegExp(`\\bin (?:\\d+|an?) ${UNIT}\\b`, 'gi'), '<date>'],
  [new RegExp('\\b(?:last|next) (?:week|month|year)\\b', 'gi'), '<date>'],
  [new RegExp(`\\b${MONTH} \\d{1,2}(?:st|nd|rd|th)?(?:,? \\d{4})?\\b`, 'g'), '<date>'],
  [new RegExp(`\\b\\d{1,2} ${MONTH}(?: \\d{4})?\\b`, 'g'), '<date>'],
  [new RegExp(`\\b${MONTH} \\d{4}\\b`, 'g'), '<date>'],
  [/\b\d{4}-\d{2}-\d{2}\b/g, '<date>'],
  [/\b\d{2}-\d{2}-\d{4}\b/g, '<date>'],
  [/\b\d{1,2}\/\d{1,2}(?:\/\d{2,4})?\b/g, '<date>'],
  [/\b\d{1,2}:\d{2}(?::\d{2})?(?: ?[AaPp][Mm])?\b/g, '<time>'],
  [/[-−+]?[$€£] ?-?\d[\d,]*(?:\.\d+)?/g, '<amount>'],
  [/\b\d{1,3}(?:,\d{3})*\.\d{2}\b/g, '<amount>'],
  [/\d+(?:[.,]\d+)*/g, '<n>'],
];
const COLLAPSE = /(<(?:n|amount|date|time)>)(?:[ .,/:\-–]*\1)+/g;
const PLACEHOLDER = /<(?:n|amount|date|time)>/;

export function nameTemplate(s) {
  if (s === null || s === undefined) return '';
  let t = String(s).replace(WS, ' ').trim();
  for (const [pat, rep] of STEPS) t = t.replace(pat, rep);
  return t.replace(COLLAPSE, '$1');
}

const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

export function nameTemplateRegex(template) {
  return new RegExp('^' + template.split(PLACEHOLDER).map(esc).join('.+?') + '$');
}

// ---------------------------------------------------------------------------
// Hashes shared with Python
// ---------------------------------------------------------------------------

export function harnessFiles(root = ROOT) {
  const base = join(root, 'tools', 'a11y');
  const out = [];
  for (const sub of ['checks', 'lib']) {
    const d = join(base, sub);
    if (existsSync(d)) out.push(...readdirSync(d).filter((f) => f.endsWith('.mjs')).map((f) => `tools/a11y/${sub}/${f}`));
  }
  if (existsSync(join(base, 'sweep.mjs'))) out.push('tools/a11y/sweep.mjs');
  return out.sort();
}

export function harnessHash(root = ROOT) {
  let text = '';
  for (const rel of harnessFiles(root)) {
    text += rel + '\n' + createHash('sha256').update(readFileSync(join(root, rel))).digest('hex') + '\n';
  }
  return createHash('sha256').update(text).digest('hex').slice(0, 16);
}

export function buildHash(html) {
  const m = /cdn\.mercury\.com\/builds\/([A-Za-z0-9]+)\//.exec(html || '');
  return m ? m[1] : 'unknown';
}
