// Common options and workspace paths for Node stages (KTD2, KTD3). Mirrors lib/ws.py.
import { existsSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { ROOT, readJson } from './canon.mjs';

export const DEFAULTS = {
  workspace: join(ROOT, 'a11y'),
  data: join(ROOT, 'data'),
  map: join(ROOT, 'build', 'map.json'),
  'base-url': 'https://demo.mercury.com',
};
const COMMITTED_POLICY = join(ROOT, 'a11y', 'policy.json');

const COMMON = {
  workspace: { type: 'string' },
  data: { type: 'string' },
  map: { type: 'string' },
  'base-url': { type: 'string' },
  run: { type: 'string' },
};

// Parse argv with the common options plus a stage's own. Returns { ws, values, positionals }.
export function parse(argv, options = {}) {
  const { values, positionals } = parseArgs({ args: argv, options: { ...COMMON, ...options }, allowPositionals: true, strict: true });
  const ws = new Workspace({ workspace: values.workspace, data: values.data, map: values.map, baseUrl: values['base-url'], run: values.run });
  return { ws, values, positionals };
}

export class Workspace {
  constructor({ workspace, data, map, baseUrl, run } = {}) {
    this.path = resolve(workspace || DEFAULTS.workspace);
    this.dataDir = resolve(data || DEFAULTS.data);
    this.mapPath = resolve(map || DEFAULTS.map);
    this.baseUrl = (baseUrl || DEFAULTS['base-url']).replace(/\/+$/, '');
    this.run = run || null;
  }

  get runsDir() { return join(this.path, 'runs'); }
  get recipesDir() { return join(this.path, 'recipes'); }
  get publishedDir() { return join(this.path, 'published'); }

  get policyPath() {
    const own = join(this.path, 'policy.json');
    return existsSync(own) ? own : COMMITTED_POLICY;
  }

  policy() { return readJson(this.policyPath); }

  runs() {
    if (!existsSync(this.runsDir)) return [];
    return readdirSync(this.runsDir).filter((r) => existsSync(join(this.runsDir, r, 'states.json'))).sort();
  }

  runId(required = true) {
    if (this.run) return this.run;
    const runs = this.runs();
    if (runs.length) return runs[runs.length - 1];
    if (required) throw new Error('no run found: run `audit.py plan` first, or pass --run');
    return null;
  }

  runDir(runId) { return join(this.runsDir, runId || this.runId()); }
  rawDir(runId) { return join(this.runDir(runId), 'raw'); }
  file(name, runId) { return join(this.runDir(runId), name); }

  states(runId) {
    const doc = readJson(this.file('states.json', runId));
    return doc ? doc.states : {};
  }

  loadMap() {
    const m = readJson(this.mapPath);
    if (!m) throw new Error(`${this.mapPath} not found: run \`python3 build.py\` first`);
    return m;
  }
}
