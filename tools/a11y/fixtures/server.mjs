// Static server for the fixture sites (KTD27): serves fixture directories on two origins and
// merges their map.json fragments into a map stages can take as --map.
//
//   node tools/a11y/fixtures/server.mjs [--port N] [--map-out path] dir...
//   prints {"origin": "http://127.0.0.1:<port>", "origin2": "http://localhost:<port2>"} and serves until killed.
//
// A dir is a folder under tools/a11y/fixtures/ or an absolute path, served at /<basename>/. The
// canary's definition files (expected.json, findings.json, raw/, data/) are never served.
// origin2 is a different site (localhost, not 127.0.0.1), so a frame from it is cross-origin and
// out-of-process like Plaid or Stripe. In .html files, {{origin}} and {{origin2}} are replaced
// with the two origins.
//
// /__delay/<ms> answers {"ms": <ms>} after <ms> milliseconds, on both origins, for pages that
// need a slow request.
//
// Hooks, set with startServer({hooks}) or setHook(name, value); null turns one off:
//   status429  a number N: 429 for the next N navigations (Sec-Fetch-Mode: navigate);
//              a function (path) => bool: 429 for every matching request.
//   crash      a function (path) => bool: a matching request's socket is destroyed before any
//              response byte, so its navigation fails with net::ERR_EMPTY_RESPONSE. (Destroying
//              it after headers or part of the body leaves Chromium's navigation hanging instead.)
import http from 'node:http';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { basename, dirname, extname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { readJson, writeJson } from '../lib/canon.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const TYPES = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.webp': 'image/webp',
  '.woff2': 'font/woff2', '.txt': 'text/plain; charset=utf-8',
};

function listen(server, port) {
  return new Promise((ok, fail) => {
    server.once('error', fail);
    server.listen(port, '127.0.0.1', () => ok(server.address().port));
  });
}

// Files in a mount that are fixture definitions, not part of its site: the canary's answer key and
// the sweep results it was built from must not be reachable by the verifier the canary tests.
const PRIVATE = { canary: new Set(['expected.json', 'findings.json', 'raw', 'data']) };

export async function startServer({ dirs, port = 0, hooks = {} }) {
  const mounts = new Map(dirs.map((d) => [basename(resolve(HERE, d)), resolve(HERE, d)]));
  const state = { status429: null, crash: null, ...hooks };
  const origins = {};

  function locate(path) {
    const [, mount, ...rest] = decodeURIComponent(path).split('/');
    const root = mounts.get(mount);
    if (!root) return null;
    if (PRIVATE[mount]?.has(rest[0])) return null;
    let file = join(root, ...rest);
    if (file !== root && !file.startsWith(root + sep)) return null;
    if (existsSync(file) && statSync(file).isDirectory()) file = join(file, 'index.html');
    return existsSync(file) && statSync(file).isFile() ? file : null;
  }

  function limited(req, path) {
    const hook = state.status429;
    if (typeof hook === 'function') return hook(path);
    if (!(hook > 0) || req.headers['sec-fetch-mode'] !== 'navigate') return false;
    state.status429 = hook - 1;
    return true;
  }

  function handle(req, res) {
    const path = new URL(req.url, 'http://fixture').pathname;
    if (state.crash?.(path)) {
      req.socket.destroy();
      return;
    }
    if (limited(req, path)) {
      res.writeHead(429, { 'content-type': TYPES['.txt'] });
      res.end('Too Many Requests');
      return;
    }
    const delay = /^\/__delay\/(\d+)$/.exec(path);
    if (delay) {
      const ms = Number(delay[1]);
      setTimeout(() => {
        res.writeHead(200, { 'content-type': TYPES['.json'], 'cache-control': 'no-store' });
        res.end(JSON.stringify({ ms }));
      }, ms).unref();
      return;
    }
    const file = locate(path);
    if (!file) {
      res.writeHead(404, { 'content-type': TYPES['.txt'] });
      res.end('Not Found');
      return;
    }
    let body = readFileSync(file);
    if (extname(file) === '.html') {
      body = body.toString('utf8').replaceAll('{{origin2}}', origins.origin2).replaceAll('{{origin}}', origins.origin);
    }
    res.writeHead(200, { 'content-type': TYPES[extname(file)] || 'application/octet-stream', 'cache-control': 'no-store' });
    res.end(body);
  }

  function mergedMap() {
    const areas = [];
    for (const root of mounts.values()) areas.push(...(readJson(join(root, 'map.json'))?.areas || []));
    const count = (key) => areas.reduce((n, a) => n + (a[key] || []).length, 0);
    return { areas, stats: { screens: count('nodes'), edges: count('edges'), journeys: count('journeys'), issues: count('issues') } };
  }

  const servers = [http.createServer(handle), http.createServer(handle)];
  origins.origin = `http://127.0.0.1:${await listen(servers[0], port)}`;
  origins.origin2 = `http://localhost:${await listen(servers[1], 0)}`;

  return {
    ...origins,
    mergedMap,
    writeMap(path) {
      writeJson(path, mergedMap());
      return path;
    },
    setHook(name, value) {
      state[name] = value;
    },
    close() {
      return Promise.all(servers.map((s) => new Promise((ok) => {
        s.close(() => ok());
        s.closeAllConnections();
      })));
    },
  };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const { values, positionals } = parseArgs({
    options: { port: { type: 'string' }, 'map-out': { type: 'string' } },
    allowPositionals: true,
  });
  const server = await startServer({ dirs: positionals, port: Number(values.port || 0) });
  if (values['map-out']) server.writeMap(values['map-out']);
  console.log(JSON.stringify({ origin: server.origin, origin2: server.origin2 }));
}
