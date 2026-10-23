// `reach` (KTD20): replay a state into a named agent-browser session, so a verifier agent starts
// on the live state under the sweep's environment.
//
//   node tools/a11y/reach.mjs <state> --session <name> [--workspace … --map … --base-url …]
//   node tools/a11y/reach.mjs status
//
// A session that reach (or probe) starts is bound to the policy's origins: agent-browser refuses to
// `open` a domain outside policy.allowedOrigins and the --base-url origin. The restriction is set when
// the session's daemon starts and cannot change after, so a session opened by another command first
// has none. It covers agent-browser's own navigation command, not clicks, scripts or sub-resources.
//
// It clears the session's cookies (agent-browser) and its storage for the allowed origins (CDP), sets
// KTD4's environment through agent-browser's own settings (viewport 1440×900 at pixel ratio 1, light
// scheme, reduced motion), connects Playwright over CDP, replays the recipe, re-injects the
// motion-off CSS and disconnects without closing the session. Playwright's attach resets media
// emulation and its disconnect drops what it set, so the replay emulates light and reduced motion on
// its own connection and agent-browser's settings are applied again after it. Prints the fingerprint
// check: {state, session, status, step, message, url, fingerprint, expected}.
import { spawnSync } from 'node:child_process';
import { parseArgs } from 'node:util';
import { chromium } from 'playwright';
import { ENV, cdpSession, injectMotionOff, trackRequests } from './lib/browser.mjs';
import { Workspace, parse, runMain } from './lib/cli.mjs';
import { allowedOrigins, chain, loadRecipes, replay } from './recipe.mjs';

// The hostnames agent-browser may open: those of the origins recipe.mjs's replay allows. Its domain
// patterns match hosts, so the port of an origin is dropped.
export function allowedDomains(policy, baseUrl) {
  return [...new Set([...allowedOrigins(policy, baseUrl)].map((origin) => new URL(origin).hostname))].join(',');
}

// reach and probe both take --workspace and --base-url and both run their agent-browser commands
// through ab, so the stage's own arguments decide the domains.
let domains;
function sessionDomains() {
  if (domains === undefined) {
    const { values } = parseArgs({ args: process.argv.slice(2), options: { workspace: { type: 'string' }, 'base-url': { type: 'string' } }, strict: false });
    const ws = new Workspace({ workspace: values.workspace, baseUrl: values['base-url'] });
    domains = allowedDomains(ws.policy(), ws.baseUrl);
  }
  return domains;
}

// One agent-browser command in the session (probe.mjs runs its commands through this too). `quiet`
// detaches stdio: the command that starts the session's daemon would otherwise keep our stdout pipe
// open for the daemon's lifetime.
export function ab(session, args, { quiet = false } = {}) {
  const r = spawnSync('agent-browser', ['--session', session, '--allowed-domains', sessionDomains(), ...args],
    quiet ? { stdio: 'ignore', timeout: 60000 } : { encoding: 'utf8', timeout: 60000 });
  if (r.error?.code === 'ENOENT') throw new Error('agent-browser is not on PATH');
  if (r.error) throw new Error(`agent-browser ${args.join(' ')}: ${r.error.message}`);
  if (r.status !== 0) throw new Error(`agent-browser ${args.join(' ')} exited ${r.status}: ${(r.stderr || r.stdout || '').trim()}`);
  return quiet ? '' : r.stdout.trim();
}

// KTD4's viewport and media, through agent-browser's own settings. `quiet` applies to the first
// command, the one that may start the session's daemon.
export function setEnvironment(session, { quiet = false } = {}) {
  ab(session, ['set', 'viewport', String(ENV.viewport[0]), String(ENV.viewport[1]), String(ENV.dpr)], { quiet });
  ab(session, ['set', 'media', ENV.colorScheme, 'reduced-motion']);
}

async function clearStorage(cdp, origins) {
  await cdp.send('DOMStorage.enable').catch(() => {});
  for (const origin of origins) {
    await cdp.send('Storage.clearDataForOrigin', { origin, storageTypes: 'all' });
    for (const isLocalStorage of [true, false]) {
      await cdp.send('DOMStorage.clear', { storageId: { storageKey: `${origin}/`, isLocalStorage } }).catch(() => {});
    }
  }
}

async function reach(ws, stateId, session) {
  const policy = ws.policy();
  const recipes = loadRecipes(ws);
  const expected = chain(recipes, stateId).at(-1).fingerprint;
  ab(session, ['cookies', 'clear'], { quiet: true });
  setEnvironment(session);
  const cdpUrl = ab(session, ['get', 'cdp-url']);
  const current = ab(session, ['get', 'url']);
  const browser = await chromium.connectOverCDP(cdpUrl);
  let result;
  let url;
  try {
    const context = browser.contexts()[0];
    const pages = context.pages();
    const page = pages.filter((p) => p.url() === current).at(-1) || pages.at(-1) || await context.newPage();
    await page.emulateMedia({ colorScheme: ENV.colorScheme, reducedMotion: ENV.reducedMotion });
    const cdp = await cdpSession(context, page);
    await clearStorage(cdp, allowedOrigins(policy, ws.baseUrl));
    const tracker = trackRequests(page);
    result = await replay(page, { cdp, tracker, baseUrl: ws.baseUrl }, stateId, { recipes, policy });
    await injectMotionOff(page);
    url = page.url();
  } finally {
    await browser.close(); // a CDP connection: this disconnects and leaves the session's browser open
  }
  setEnvironment(session);
  return { state: stateId, session, status: result.status, step: result.step, message: result.message, url,
    fingerprint: result.fingerprint, expected };
}

async function main(argv) {
  const { ws, values, positionals } = parse(argv, { session: { type: 'string' } });
  if (positionals[0] === 'status') {
    // reach keeps no files: pending until the run has a states.json, then nothing to count.
    console.log(JSON.stringify(ws.hasStates(ws.runId(false)) ? {} : { pending: true }));
    return 0;
  }
  if (positionals.length !== 1 || !values.session) {
    console.error('usage: reach.mjs <state> --session <agent-browser session name>');
    return 2;
  }
  const out = await reach(ws, positionals[0], values.session);
  console.log(`${out.status} ${out.state} in session ${out.session} at ${out.url}${out.message ? `: ${out.message}` : ''}`);
  console.log(JSON.stringify(out));
  return out.status === 'reached' ? 0 : 1;
}

runMain(import.meta.url, 'reach', main);
