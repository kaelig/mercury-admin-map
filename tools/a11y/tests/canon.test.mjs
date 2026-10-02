import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, mkdtempSync, readdirSync, readFileSync as read } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { ROOT, canonical, dump, nameTemplate, nameTemplateRegex, buildHash, harnessHash, writeJson } from '../lib/canon.mjs';

const V = JSON.parse(readFileSync(new URL('./data/canon-vectors.json', import.meta.url), 'utf8'));

test('serialization matches the shared vectors', () => {
  for (const v of V.serialize) {
    assert.equal(canonical(v.value), v.canonical);
    if (v.dump) assert.equal(dump(v.value), v.dump);
  }
});

test('integer-like keys sort as strings, as in Python', () => {
  assert.equal(canonical({ 9: 1, 10: 2, a: 3 }), '{"10":2,"9":1,"a":3}');
});

test('name templates match the shared vectors', () => {
  for (const [raw, want] of V.templates) assert.equal(nameTemplate(raw), want, raw);
});

test('template regexes match the shared vectors', () => {
  for (const v of V.regex) {
    const rx = nameTemplateRegex(v.template);
    for (const s of v.match) assert.ok(rx.test(s), `${v.template} should match ${s}`);
    for (const s of v.noMatch) assert.ok(!rx.test(s), `${v.template} should not match ${s}`);
  }
});

test('build hash matches the shared vectors', () => {
  for (const [html, want] of V.buildHash) assert.equal(buildHash(html), want);
});

test('harness hash equals the Python one', () => {
  const py = execFileSync('python3', ['-c', 'import sys; sys.path.insert(0, "tools/a11y"); from lib.canon import harness_hash; print(harness_hash())'], { cwd: ROOT, encoding: 'utf8' });
  assert.equal(harnessHash(), py.trim());
});

test('canonical bytes equal Python for the same object', () => {
  const obj = { b: [1.5, 2, -0.0001, 3.14159], a: { z: 'é', y: null } };
  const py = execFileSync('python3', ['-c', `import sys, json; sys.path.insert(0, "tools/a11y"); from lib.canon import canonical; print(canonical(json.loads(sys.argv[1])), end="")`, JSON.stringify(obj)], { cwd: ROOT, encoding: 'utf8' });
  assert.equal(canonical(obj), py);
});

test('writeJson writes dump() through a rename', () => {
  const d = mkdtempSync(join(tmpdir(), 'canon-'));
  const path = join(d, 'sub', 'x.json');
  writeJson(path, { b: 1.0, a: 2 });
  assert.equal(read(path, 'utf8'), '{\n "a": 2,\n "b": 1\n}\n');
  assert.deepEqual(readdirSync(join(d, 'sub')), ['x.json']);
});
