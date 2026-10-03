import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, mkdtempSync, mkdirSync, readdirSync, writeFileSync, readFileSync as read } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
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

// A repository root holding every file and package the harness hash reads, each with distinct content.
function harnessFixture() {
  const root = mkdtempSync(join(tmpdir(), 'harness-'));
  const put = (rel, text) => {
    mkdirSync(dirname(join(root, rel)), { recursive: true });
    writeFileSync(join(root, rel), text);
  };
  for (const rel of ['checks/axe.mjs', 'lib/canon.mjs', 'sweep.mjs', 'recipe.mjs', 'fingerprint.mjs', 'checks.json']) {
    put(`tools/a11y/${rel}`, `// ${rel}\n`);
  }
  for (const name of ['axe-core', 'playwright']) put(`node_modules/${name}/package.json`, JSON.stringify({ name, version: '1.0.0' }));
  return { root, put };
}

test('harness hash equals the Python one on a fixture root', () => {
  const { root } = harnessFixture();
  const py = execFileSync('python3', ['-c', 'import sys; sys.path.insert(0, "tools/a11y"); from lib.canon import harness_hash; print(harness_hash(sys.argv[1]))', root], { cwd: ROOT, encoding: 'utf8' });
  assert.equal(harnessHash(root), py.trim());
});

test('harness hash changes with every file and package that decides a raw file', () => {
  const { root, put } = harnessFixture();
  const seen = new Set([harnessHash(root)]);
  const changes = [
    ['tools/a11y/sweep.mjs', '// changed\n'],
    ['tools/a11y/recipe.mjs', '// changed\n'],
    ['tools/a11y/fingerprint.mjs', '// changed\n'],
    ['tools/a11y/checks.json', '{}\n'],
    ['tools/a11y/checks/axe.mjs', '// changed\n'],
    ['tools/a11y/lib/canon.mjs', '// changed\n'],
    ['node_modules/axe-core/package.json', JSON.stringify({ version: '1.0.1' })],
    ['node_modules/playwright/package.json', JSON.stringify({ version: '1.0.1' })],
  ];
  for (const [rel, text] of changes) {
    put(rel, text);
    const h = harnessHash(root);
    assert.ok(!seen.has(h), `changing ${rel} left the hash at ${h}`);
    seen.add(h);
  }
});

test('harness hash ignores files the sweep does not read', () => {
  const { root, put } = harnessFixture();
  const before = harnessHash(root);
  put('tools/a11y/publish.py', '# changed\n');
  put('tools/a11y/README.md', 'changed\n');
  assert.equal(harnessHash(root), before);
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
