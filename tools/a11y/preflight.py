"""The `preflight` stage (KTD26, step 1): is everything the audit needs in place? Writes no files.

    python3 tools/a11y/audit.py preflight [--map build/map.json] [--base-url https://demo.mercury.com]

Checks that the map exists, that playwright and axe-core are installed at the versions package.json
pins, and that the demo answers 200 for /dashboard. Reads the demo's build hash from that page.
Prints a JSON summary and exits non-zero, naming each failing check.
"""
import json
import os
import sys
import urllib.error
import urllib.request

from lib.canon import ROOT, build_hash, read_json

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/151.0.0.0 Safari/537.36")
TIMEOUT = 10
PACKAGES = ("playwright", "axe-core")


def check_map(ws):
    return {"ok": os.path.exists(ws.map_path), "path": ws.map_path}


def check_package(name):
    pinned = ((read_json(os.path.join(ROOT, "package.json"), {}) or {}).get("devDependencies") or {}).get(name)
    pkg = read_json(os.path.join(ROOT, "node_modules", name, "package.json"))
    installed = pkg.get("version") if pkg else None
    return {"ok": installed is not None and installed == pinned, "installed": installed, "pinned": pinned}


def check_demo(base_url):
    """GET <base_url>/dashboard like a desktop Chrome. Returns (check, build hash or None)."""
    url = base_url + "/dashboard"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            status, html = r.status, r.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return {"ok": False, "url": url, "status": e.code}, None
    except (urllib.error.URLError, OSError) as e:
        return {"ok": False, "url": url, "error": str(getattr(e, "reason", e))}, None
    return {"ok": status == 200, "url": url, "status": status}, build_hash(html)


def cli(ws, sub, argv):
    if argv:
        print(f"preflight: unexpected arguments {argv}", file=sys.stderr)
        return 2
    checks = {"map": check_map(ws)}
    for name in PACKAGES:
        checks[name] = check_package(name)
    checks["demo"], build = check_demo(ws.base_url)
    failed = [name for name, c in checks.items() if not c["ok"]]
    print(json.dumps({"ok": not failed, "build": build, "baseUrl": ws.base_url, "checks": checks}, sort_keys=True))
    if failed:
        print(f"preflight failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0
