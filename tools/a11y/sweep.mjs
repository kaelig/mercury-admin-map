// The `sweep` stage (KTD10): replay every state, run the checks its kind gets, and write
// runs/<id>/raw/<state>.json; once every state in states.json has a valid raw file, write
// runs/<id>/outcomes.json. File formats: schemas.md.
//
//   node tools/a11y/sweep.mjs [--workers 8] [--state <id> ...] [--timeout <ms>]
//   node tools/a11y/sweep.mjs status
//
// Each worker owns a browser (recycled every 50 states) and gives every attempt a fresh context
// (KTD4). A state is dispatched once the state it replays through has its raw file (lib/queue.mjs).
// Resuming is rerunning: only states without a valid raw file run. Valid means it parses, names its
// state and carries the current harness hash, so a code change sweeps every state again. --state
// sweeps the named states even when valid, plus any ancestor they need that has no valid file.
//
// A state ends audited or unreachable: step-failed, fingerprint-mismatch, blocked-by-parent (the
// child of an unreachable state, never replayed), no-recipe, no-path, or tool-error (an exception, a
// crashed page or a check that hangs past --timeout, after two retries in a fresh context; a hang
// also restarts the worker's browser).
//
// The circuit breaker counts environment signals only: a 403 or 429 answer to a page navigation, a
// challenge page, or three consecutive root pages that fail to load. Recipe drift never counts. A
// trip stops the sweep with `interrupted: environment` and writes no unreachable result for any state
// it skipped; a state whose root page failed to load is held back until the sweep ends untripped.
//
// Exit codes: 0 every state accounted for (or the --state states swept); 1 failure; 2 usage;
// 75 the breaker tripped (resume once the demo answers again); 130, 143, 129 stopped by SIGINT,
// SIGTERM or SIGHUP (rerun to resume). The last stdout line is
// {run, audited, unreachable, pending, interrupted, ms}; progress goes to stderr.
import { existsSync, unlinkSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { harnessHash, readJson, sha, sortCanonical, writeJson } from './lib/canon.mjs';
import { cdpSession, envFor, launch, newContext, readBuildHash, trackRequests } from './lib/browser.mjs';
import { findLayer } from './lib/scope.mjs';
import { parse } from './lib/cli.mjs';
import { Queue } from './lib/queue.mjs';
import { chain, loadRecipes, replay, resolveLocator } from './recipe.mjs';
import * as axeCheck from './checks/axe.mjs';
import * as axCheck from './checks/ax.mjs';
import * as interactiveCheck from './checks/interactive.mjs';
import * as targetCheck from './checks/target.mjs';
import * as tabwalkCheck from './checks/tabwalk.mjs';
import * as dialogCheck from './checks/dialog.mjs';
import * as statusCheck from './checks/status.mjs';

const RETRIES = 2;
const RECYCLE_AFTER = 50;
const ROOT_FAILURES = 3;
const TIMEOUT = 180000;
const EXIT_ENVIRONMENT = 75;

const STATUS_KINDS = new Set(['toast', 'confirmation', 'validation-error']);
const TABWALK_KINDS = new Set(['page', 'modal', 'drawer']);
const DIALOG_KINDS = new Set(['modal', 'drawer', 'menu', 'popover']);
const CHALLENGE_TITLE = /^(just a moment|attention required)/i;
// What replay() reports when the root page answered with an environment signal or did not load.
const ENV_ANSWER = /answered HTTP (403|429)\b/;
const LOAD_FAILURE = /net::ERR_(?!ABORTED)|answered HTTP 5\d\d\b/;

// The checks a state gets, in the order they run (schemas.md "State kinds", "Check modules"): the
// status observer first on toasts and right after axe on the other status kinds, the Tab walk last
// on the shared page, the dialog check (fresh contexts) only after a click or hover final hop.
function checksFor(kind, finalHop) {
  const list = kind === 'toast' ? [statusCheck, axeCheck] : [axeCheck];
  if (STATUS_KINDS.has(kind) && kind !== 'toast') list.push(statusCheck);
  list.push(axCheck, interactiveCheck, targetCheck);
  if (TABWALK_KINDS.has(kind)) list.push(tabwalkCheck);
  if (DIALOG_KINDS.has(kind) && finalHop && ('click' in finalHop || 'hover' in finalHop)) list.push(dialogCheck);
  return list;
}

class Hang extends Error {}

function within(ms, promise, what) {
  let timer;
  const limit = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Hang(`${what} did not finish within ${ms} ms; the browser was restarted`)), ms);
  });
  return Promise.race([promise, limit]).finally(() => clearTimeout(timer));
}

const pathOf = (url) => {
  try {
    return new URL(url).pathname;
  } catch {
    return String(url);
  }
};

// Local time, as plan writes run.json's `created`.
function stamp(d = new Date()) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

const recipeHash = (list) => sha(list.map((r) => ({ id: r.id, parent: r.parent, hop: r.hop || [], fingerprint: r.fingerprint ?? null })));

const outcomeOf = (doc) => ({
  unreachable: doc.status === 'unreachable',
  rootCause: doc.reason === 'blocked-by-parent' ? doc.reasonDetail.rootCause : doc.state,
});

// A raw file's document, or null when it is missing or does not parse.
function readRaw(rawDir, id) {
  try {
    return readJson(join(rawDir, `${id}.json`));
  } catch {
    return null;
  }
}

function validDoc(rawDir, id, harness) {
  const doc = readRaw(rawDir, id);
  return doc && doc.state === id && doc.harness === harness && ['audited', 'unreachable'].includes(doc.status) ? doc : null;
}

// What outcomes.json and run.json read from a valid raw file (outcomeOf, outcomesDoc, the builds).
const summaryOf = (doc) => ({ state: doc.state, status: doc.status, reason: doc.reason, reasonDetail: doc.reasonDetail,
  families: doc.families, build: doc.build });

// What every page of one attempt saw: an environment signal, the root page's load (the main page's
// first navigation), and the first page navigation that failed.
function watch(page, seen, main) {
  let first = main;
  const nav = (failure) => {
    if (first) seen.root = failure ? { failed: failure } : { ok: true };
    first = false;
    if (failure) seen.failed ??= failure;
  };
  page.on('response', (res) => {
    const req = res.request();
    if (!req.isNavigationRequest() || req.frame() !== page.mainFrame()) return;
    const code = res.status();
    if (code >= 300 && code < 400) return;
    if (code === 403 || code === 429) seen.env ??= `HTTP ${code} on ${pathOf(res.url())}`;
    else if (res.headers()['cf-mitigated'] === 'challenge') seen.env ??= `a challenge page at ${pathOf(res.url())}`;
    nav(code >= 500 ? `HTTP ${code} loading ${pathOf(res.url())}` : null);
  });
  page.on('requestfailed', (req) => {
    if (!req.isNavigationRequest() || req.frame() !== page.mainFrame()) return;
    const why = req.failure()?.errorText || 'failed';
    if (why !== 'net::ERR_ABORTED') nav(`${why} loading ${pathOf(req.url())}`);
  });
  page.on('crash', () => { seen.crashed = true; });
}

class Worker {
  constructor(browser = null) {
    this.current = browser;
    this.used = 0;
  }

  // Once per state: recycle the browser after RECYCLE_AFTER states.
  async next() {
    if (this.current && this.used >= RECYCLE_AFTER) await this.close();
    this.used += 1;
  }

  async browser() {
    if (this.current && !this.current.isConnected()) await this.close();
    if (!this.current) {
      this.current = await launch();
      this.used = 1;
    }
    return this.current;
  }

  async close() {
    const b = this.current;
    this.current = null;
    if (b) await within(30000, b.close(), 'closing the browser').catch(() => {});
  }
}

class Sweep {
  constructor(ws, runId, { workers, timeout, only }) {
    this.ws = ws;
    this.runId = runId;
    this.workers = workers;
    this.timeout = timeout;
    this.states = ws.states(runId);
    this.ids = Object.keys(this.states);
    this.harness = harnessHash();
    this.rawDir = ws.rawDir(runId);
    this.shotsDir = join(ws.runDir(runId), 'shots');
    this.policy = ws.policy();
    if (!this.policy) throw new Error(`no policy at ${ws.policyPath}`);
    this.recipes = loadRecipes(ws);
    // A root is reproduced by its URL alone (needsRecipe: false): without an entry, replay that URL.
    for (const id of this.ids) {
      const s = this.states[id];
      if (s.root && !this.recipes[id]) this.recipes[id] = { parent: s.rootUrl || s.url, hop: [], fingerprint: null, status: 'accepted', recordedBy: 'root' };
    }
    // The summary of every state with a valid raw file; save() keeps it current.
    this.valid = {};
    for (const id of this.ids) {
      const doc = validDoc(this.rawDir, id, this.harness);
      if (doc) this.valid[id] = summaryOf(doc);
    }
    this.todo = this.select(only);
    this.env = null;
    this.tripped = null;
    this.aborted = false;
    this.rootFailures = 0;
    this.held = new Map();
    this.written = 0;
    this.pool = [];
  }

  // The state `id` replays through, when that is a state the sweep tracks; null for roots, states
  // that are written without a replay, and recipes whose parent is a URL.
  parentOf(id) {
    const s = this.states[id];
    const r = this.recipes[id];
    if (s.unreachable || !r || r.status !== 'accepted') return null;
    return typeof r.parent === 'string' && !r.parent.startsWith('/') && this.states[r.parent] ? r.parent : null;
  }

  select(only) {
    if (!only) return this.ids.filter((id) => !this.valid[id]);
    const out = new Set(only);
    for (const id of only) {
      const seen = new Set([id]);
      for (let p = this.parentOf(id); p && !this.valid[p] && !seen.has(p); p = this.parentOf(p)) {
        seen.add(p);
        out.add(p);
      }
    }
    return this.ids.filter((id) => out.has(id));
  }

  log(line) {
    process.stderr.write(`sweep: ${line}\n`);
  }

  unreachable(id, reason, detail, { hash = null, build = null } = {}) {
    const s = this.states[id];
    return {
      schema: 1, state: id, kind: s.kind, status: 'unreachable', reason,
      reasonDetail: { step: null, message: null, rootCause: null, ...detail },
      harness: this.harness, recipeHash: hash, build, env: this.env, url: s.url, layer: null, frames: [],
      families: { completed: [], incomplete: [] }, results: [], records: [], checks: {},
    };
  }

  save(doc, ms) {
    writeJson(join(this.rawDir, `${doc.state}.json`), doc);
    this.valid[doc.state] = summaryOf(doc);
    this.written += 1;
    const what = doc.status === 'audited'
      ? `audited ${doc.state} (${doc.kind}, ${ms} ms)${doc.families.incomplete.length ? `, incomplete: ${doc.families.incomplete.join(', ')}` : ''}`
      : `unreachable ${doc.state} (${doc.kind}, ${ms} ms): ${doc.reason}${doc.reasonDetail.message ? `: ${doc.reasonDetail.message}` : ''}`;
    this.log(`[${this.written}/${this.todo.length}] ${what}`);
  }

  get stopped() {
    return Boolean(this.tripped || this.aborted);
  }

  write(doc, t0) {
    if (this.aborted || (this.tripped && doc.status === 'unreachable')) return;
    this.save(doc, Date.now() - t0);
    this.queue.finish(doc.state, outcomeOf(doc));
  }

  trip(signal) {
    if (this.tripped) return;
    this.tripped = signal;
    this.log(`circuit breaker tripped: ${signal}. Nothing more is written; resume once the demo answers again.`);
    this.queue.stop();
    for (const w of this.pool) w.close();
  }

  // A signal: stop dispatching, write nothing more, close the browsers.
  async abort() {
    this.aborted = true;
    this.queue?.stop();
    await Promise.all(this.pool.map((w) => w.close()));
  }

  // Held root-load failures are written once nothing else is left, unless the breaker tripped.
  onIdle(ids) {
    if (this.stopped) return [];
    return ids.map((id) => {
      const { doc, t0 } = this.held.get(id);
      this.held.delete(id);
      this.save(doc, Date.now() - t0);
      return [id, outcomeOf(doc)];
    });
  }

  hashOf(id) {
    try {
      return recipeHash(chain(this.recipes, id));
    } catch {
      return null;
    }
  }

  async one(worker, item) {
    const { id } = item;
    const s = this.states[id];
    const t0 = Date.now();
    if (item.blocked) {
      return this.write(this.unreachable(id, 'blocked-by-parent', { message: `${item.blocked} is unreachable`, rootCause: item.blocked },
        { hash: this.hashOf(id) }), t0);
    }
    if (s.unreachable === 'no-path') return this.write(this.unreachable(id, 'no-path', { message: 'no path from a root in the map' }), t0);
    const r = this.recipes[id];
    if (!r || r.status !== 'accepted') {
      return this.write(this.unreachable(id, 'no-recipe', { message: r ? `its recipe is ${r.status || 'unchecked'}, not accepted` : 'no recipe' }), t0);
    }
    let list;
    try {
      list = chain(this.recipes, id);
    } catch (e) {
      return this.write(this.unreachable(id, 'no-recipe', { message: e.message }), t0);
    }
    await worker.next();
    let last = null;
    for (let n = 0; n <= RETRIES; n++) {
      if (this.stopped) return;
      const browser = await worker.browser();
      if (this.stopped) return;
      const out = await this.attempt(browser, id, list);
      if (this.stopped) return;
      if (out.env) return this.trip(out.env);
      if (out.rootLoaded) this.rootFailures = 0;
      if (!out.error) return this.write(out.doc, t0);
      last = out;
      if (out.hang) await worker.close();
      if (n < RETRIES) this.log(`retry ${id} (attempt ${n + 2} of ${RETRIES + 1}): ${out.error}`);
    }
    const doc = this.unreachable(id, 'tool-error', { step: last.step ?? null, message: last.error }, { hash: recipeHash(list) });
    if (!last.rootLoad) return this.write(doc, t0);
    // Trip before holding: hold() can settle the held states at once when nothing else is left.
    this.rootFailures += 1;
    if (this.rootFailures >= ROOT_FAILURES) return this.trip(`${ROOT_FAILURES} consecutive root pages failed to load (last: ${last.error})`);
    this.held.set(id, { doc, t0 });
    this.queue.hold(id);
  }

  // One replay of `id` and its checks in fresh contexts. Returns {env}, {error, step, rootLoad, hang}
  // (retried) or {doc} (written), with `rootLoaded` when the root page loaded.
  async attempt(browser, id, list) {
    const s = this.states[id];
    const seen = { env: null, root: null, failed: null, crashed: false };
    const contexts = [];
    const guard = (promise, what) => within(this.timeout, promise, what);
    const open = async (main) => {
      const context = await guard(newContext(browser), 'opening a context');
      contexts.push(context);
      const page = await guard(context.newPage(), 'opening a page');
      watch(page, seen, main);
      const tracker = trackRequests(page);
      const cdp = await guard(cdpSession(context, page), 'opening a CDP session');
      return { context, page, tracker, cdp };
    };
    const reach = async (session, opts) => {
      const r = await guard(replay(session.page, { cdp: session.cdp, tracker: session.tracker, baseUrl: this.ws.baseUrl }, id,
        { recipes: this.recipes, policy: this.policy, ...opts }), 'the replay');
      if (CHALLENGE_TITLE.test(await guard(session.page.title(), 'reading the title').catch(() => ''))) {
        seen.env ??= `a challenge page at ${pathOf(session.page.url())}`;
      }
      return r;
    };
    const statusKind = STATUS_KINDS.has(s.kind);
    const finalHop = (list.at(-1).hop || []).at(-1) || null;
    let step = null;
    let hung = false;
    try {
      const main = await open(true);
      const { page, cdp, tracker } = main;
      // replay() arms the layer before the final hop itself; a status kind also arms the observer.
      const r = await reach(main, { beforeFinalHop: statusKind ? (p) => statusCheck.arm(p) : null, layerKind: s.kind });
      step = r.step;
      if (seen.env || ENV_ANSWER.test(r.message || '')) return { env: seen.env || r.message };
      const rootLoaded = Boolean(seen.root?.ok);
      const failed = seen.failed || (seen.crashed && 'the page crashed')
        || (r.status === 'step-failed' && r.step === 0 && LOAD_FAILURE.test(r.message || '') && r.message);
      if (failed) return { error: failed, step: r.step, rootLoad: !rootLoaded, rootLoaded };
      if (r.status !== 'reached') {
        const build = await guard(readBuildHash(page), 'reading the build').catch(() => null);
        return { rootLoaded, doc: this.unreachable(id, r.status, { step: r.step, message: r.message }, { hash: recipeHash(list), build }) };
      }

      const build = await guard(readBuildHash(page), 'reading the build');
      // The layer the fingerprint was scoped to, when the replay looked for this kind's layer.
      const layer = r.layer || await guard(findLayer(cdp, s.kind), 'finding the layer');
      const triggers = new WeakMap();
      const ctx = {
        page, cdp, tracker, kind: s.kind, state: id, layer, env: this.env, finalHop, shotsDir: this.shotsDir,
        axe: null, interactive: null,
        // The dialog check's fresh context, replayed to the parent state, with the final hop's trigger resolved.
        reopen: async () => {
          const session = await open(false);
          const back = await reach(session, { untilParent: true });
          if (seen.env) throw new Error(seen.env);
          if (back.status !== 'reached') throw new Error(`the dialog check could not reach ${id}'s parent again: ${back.status}: ${back.message}`);
          const locator = finalHop.click || finalHop.hover;
          triggers.set(session.page, await resolveLocator(session.page, locator)
            .catch(() => session.page.locator('[data-a11y-sweep-unresolved-trigger]')));
          return session;
        },
        trigger: (p) => triggers.get(p),
      };
      const completed = [];
      const incomplete = [];
      const results = [];
      const records = [];
      const checks = {};
      let frames = [];
      for (const check of checksFor(s.kind, finalHop)) {
        const out = await guard(check.run(ctx), `the ${check.id} check`);
        let done = out.completed;
        if (statusKind && check !== statusCheck && !(await guard(statusCheck.present(page), 'the status presence probe'))) done = false;
        (done ? completed : incomplete).push(check.id);
        results.push(...out.results);
        records.push(...out.records);
        if (check === axeCheck) {
          const { frames: f, ...detail } = out.detail;
          frames = f;
          checks.axe = detail;
          ctx.axe = { detail: out.detail, results: out.results };
        } else if (check === interactiveCheck) {
          checks.interactive = out.detail;
          ctx.interactive = out.detail;
        } else if (check !== axCheck) {
          checks[check.id] = out.detail;
        }
      }
      if (seen.env) return { env: seen.env };
      if (seen.failed || seen.crashed) return { error: seen.failed || 'the page crashed', step: null, rootLoad: false, rootLoaded };
      return {
        rootLoaded,
        doc: {
          schema: 1, state: id, kind: s.kind, status: 'audited', reason: null, reasonDetail: null,
          harness: this.harness, recipeHash: recipeHash(list), build, env: this.env, url: s.url, layer, frames,
          families: { completed, incomplete }, results: sortCanonical(results), records: sortCanonical(records), checks,
        },
      };
    } catch (e) {
      if (seen.env) return { env: seen.env };
      hung = e instanceof Hang;
      const rootLoaded = Boolean(seen.root?.ok);
      return { error: (e.message || String(e)).split('\n')[0], step, rootLoad: Boolean(seen.root?.failed), rootLoaded, hang: hung };
    } finally {
      // A hung renderer may not close: the worker closes the whole browser instead.
      if (!hung) await Promise.all(contexts.map((c) => within(30000, c.close(), 'closing a context').catch(() => {})));
    }
  }

  async run() {
    this.queue = new Queue(this.todo, (id) => this.parentOf(id), {
      done: Object.fromEntries(this.ids.filter((id) => this.valid[id] && !this.todo.includes(id)).map((id) => [id, outcomeOf(this.valid[id])])),
      onIdle: (held) => this.onIdle(held),
    });
    const first = await launch();
    this.env = envFor(first);
    this.pool = Array.from({ length: Math.max(1, Math.min(this.workers, this.todo.length)) }, (_, i) => new Worker(i === 0 ? first : null));
    try {
      await Promise.all(this.pool.map(async (worker) => {
        for (let item; (item = await this.queue.next());) {
          try {
            await this.one(worker, item);
          } catch (e) {
            // A bug here, not in the page: stop dispatching rather than spin on it.
            this.log(`stopping at ${item.id}: ${e.stack || e.message}`);
            this.queue.stop();
            throw e;
          }
        }
      }));
    } finally {
      await Promise.all(this.pool.map((w) => w.close()));
    }
  }
}

// ---------------------------------------------------------------------------
// outcomes.json and run.json
// ---------------------------------------------------------------------------

function outcomesDoc(runId, ids, docs, prev) {
  const states = {};
  const counts = { audited: 0, unreachable: 0 };
  const builds = new Set();
  for (const id of [...ids].sort()) {
    const doc = docs[id];
    counts[doc.status] += 1;
    if (doc.build) builds.add(doc.build);
    let firstUnreachable = null;
    if (doc.status === 'unreachable') {
      const before = prev?.states[id];
      firstUnreachable = before?.outcome === 'unreachable' ? before.firstUnreachable || prev.run : runId;
    }
    states[id] = { outcome: doc.status, reason: doc.reason, detail: doc.reasonDetail, firstUnreachable,
      completed: doc.families.completed, incomplete: doc.families.incomplete, build: doc.build };
  }
  return { run: runId, counts, builds: [...builds].sort(), states };
}

function updateRun(ws, runId, fn) {
  const path = ws.file('run.json', runId);
  const doc = readJson(path) || { run: runId };
  fn(doc);
  writeJson(path, doc);
}

// `sweep status`: counts from the raw files, never writing.
function progress(ws) {
  const runId = ws.runId(false);
  if (!ws.hasStates(runId)) return { pending: true };
  const states = ws.states(runId);
  const harness = harnessHash();
  const out = { run: runId, states: Object.keys(states).length, audited: 0, unreachable: 0, stale: 0, pending: 0 };
  for (const sid of Object.keys(states)) {
    const doc = readRaw(ws.rawDir(runId), sid); // unreadable counts as pending
    if (!doc || doc.state !== sid) out.pending += 1;
    else if (doc.harness !== harness) out.stale += 1;
    else if (doc.status === 'audited' || doc.status === 'unreachable') out[doc.status] += 1;
    else out.pending += 1;
  }
  out.outcomes = existsSync(ws.file('outcomes.json', runId));
  return out;
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

const USAGE = 'usage: sweep.mjs [--workers N] [--state <id> ...] [--timeout <ms>] | status';

class Usage extends Error {}

function positiveInt(value, name, fallback) {
  if (value === undefined) return fallback;
  const n = Number(value);
  if (!Number.isInteger(n) || n < 1) throw new Usage(`--${name} must be a positive integer, not ${value}`);
  return n;
}

async function main(argv) {
  const t0 = Date.now();
  const { ws, values, positionals } = parse(argv, {
    workers: { type: 'string' }, state: { type: 'string', multiple: true }, timeout: { type: 'string' },
  });
  if (positionals[0] === 'status' && positionals.length === 1) {
    console.log(JSON.stringify(progress(ws)));
    return 0;
  }
  if (positionals.length) throw new Usage(USAGE);
  const workers = positiveInt(values.workers, 'workers', 8);
  const timeout = positiveInt(values.timeout, 'timeout', TIMEOUT);
  const runId = ws.runId();
  if (!ws.hasStates(runId)) throw new Error(`run ${runId} has no states.json: run \`audit.py plan\` first`);
  const states = ws.states(runId);
  const unknown = (values.state || []).filter((s) => !states[s]);
  if (unknown.length) throw new Usage(`not in run ${runId}'s states.json: ${unknown.join(', ')}`);

  const sweep = new Sweep(ws, runId, { workers, timeout, only: values.state || null });
  const prev = ws.previousOutcomes();
  const outcomesPath = ws.file('outcomes.json', runId);
  updateRun(ws, runId, (doc) => {
    doc.harness = sweep.harness;
    doc.stages = { ...(doc.stages || {}), sweep: { started: stamp(), finished: null, interrupted: null } };
  });
  if (sweep.todo.length) {
    // outcomes.json describes a finished sweep: it goes until the coverage invariant holds again.
    if (existsSync(outcomesPath)) unlinkSync(outcomesPath);
    sweep.log(`run ${runId}: ${sweep.todo.length} of ${sweep.ids.length} states to sweep, ${Math.min(workers, sweep.todo.length)} workers`);
    // Playwright closes its browsers on these signals, but after SIGTERM the sweep would carry on.
    for (const [signal, code] of [['SIGINT', 130], ['SIGTERM', 143], ['SIGHUP', 129]]) {
      process.once(signal, () => {
        sweep.log(`${signal}: stopping; rerun the stage to resume`);
        sweep.abort().finally(() => process.exit(code));
      });
    }
    await sweep.run();
  }

  const docs = sweep.valid;
  const pending = sweep.ids.filter((sid) => !docs[sid]);
  const outcomes = !pending.length ? outcomesDoc(runId, sweep.ids, docs, prev) : null;
  if (outcomes && !sweep.tripped) writeJson(outcomesPath, outcomes);
  const interrupted = sweep.tripped ? 'environment' : null;
  updateRun(ws, runId, (doc) => {
    if (sweep.env) doc.versions = { ...(doc.versions || {}), chromium: sweep.env.chromium };
    doc.builds = [...new Set(Object.values(docs).map((d) => d.build).filter(Boolean))].sort();
    doc.stages.sweep = { ...doc.stages.sweep, finished: stamp(), interrupted };
  });
  const counted = Object.values(docs);
  const summary = {
    run: runId,
    audited: counted.filter((d) => d.status === 'audited').length,
    unreachable: counted.filter((d) => d.status === 'unreachable').length,
    pending: pending.length,
    interrupted,
    ms: Date.now() - t0,
  };
  if (pending.length && !interrupted && !values.state) sweep.log(`${pending.length} states have no valid raw file: ${pending.join(', ')}`);
  console.log(JSON.stringify(summary));
  if (interrupted) return EXIT_ENVIRONMENT;
  return pending.length && !values.state ? 1 : 0;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  // exitCode, not exit(): on macOS a pipe write can still be queued, and exit() would drop it. A
  // handle left by an abandoned page (a hung replay) gets 30 s before the process exits anyway.
  const done = (code) => {
    process.exitCode = code;
    setTimeout(() => process.exit(code), 30000).unref();
  };
  main(process.argv.slice(2)).then(done, (e) => {
    console.error(`sweep: ${e instanceof Usage ? e.message : e.stack || e.message}`);
    console.log(JSON.stringify({ error: e.message }));
    done(e instanceof Usage || e.code === 'ERR_PARSE_ARGS_UNKNOWN_OPTION' ? 2 : 1);
  });
}
