"""The `plan` stage (KTD5): classify every state in the map into runs/<id>/states.json.

    python3 tools/a11y/audit.py plan [--run <id> [--force]]

A root is the first page node of each concrete page URL, Books pages excluded. Every other state
gets a parent and a path that starts at a root: the edge a journey uses when there is one, else the
cheapest path, where cost is (mutating steps, hops) compared in that order. File format: schemas.md.
"""
import argparse
import collections
import datetime
import heapq
import os
import platform
import re
import subprocess
import sys

from lib.canon import ROOT, read_json, sha, sha1, write_json
from lib.ws import Workspace

# A URL is a placeholder when it carries an ellipsis, angle brackets, braces or square brackets. The
# capture wrote `q={savedTransactionViewId:monthlyMoneyOut}` as shorthand for an encoded query: opening
# it on the demo shows the default Transactions toolbar, not the saved view. So any brace counts, as in
# tools/link-punctuation/sweep.sh.
PLACEHOLDER = re.compile(r"[…<>{}\[\]]|\.\.\.")
BOOKS = "books."
ZERO = (0, 0)


def is_concrete(url):
    return bool(url) and not PLACEHOLDER.search(url)


def verb_pattern(verbs):
    """One regex for the policy's mutating verbs: whole words, case-insensitive, phrases allowed."""
    alts = [r"\s+".join(re.escape(w) for w in v.split()) for v in sorted(verbs or [], key=len, reverse=True) if v.strip()]
    if not alts:
        return None
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(alts) + r")(?![A-Za-z0-9])", re.I)


# ---------------------------------------------------------------------------
# Graph logic
# ---------------------------------------------------------------------------

def map_nodes(map_doc):
    """Nodes in map order: area order, then node order."""
    return [(a.get("area"), n) for a in map_doc["areas"] for n in a.get("nodes", [])]


def find_roots(nodes, exclude=()):
    """Ids of the first page node of each concrete URL, Books nodes excluded. nodes: [(area, node)].

    `exclude` lists states whose URL alone does not reproduce them (a wizard step that redirects, a
    view the demo rebuilds from in-memory state), found by `recipe check`. Their URL yields no root.
    """
    seen, roots = set(), []
    for _, n in nodes:
        if n.get("kind") != "page" or n["id"].startswith(BOOKS) or not is_concrete(n.get("url")):
            continue
        if n["id"] in exclude:
            seen.add(n["url"])
            continue
        if n["url"] not in seen:
            seen.add(n["url"])
            roots.append(n["id"])
    return roots


def cheapest_paths(roots, out_edges):
    """Multi-source Dijkstra. Edge cost is (1 if mutating else 0, 1); costs compare lexicographically.

    out_edges: id -> sorted [(to, label, mutating)]. Returns (dist, parent) where parent[v] is
    (from, label) on a cheapest path; ties go to the lower parent id, then the lower label.
    """
    dist = {r: ZERO for r in roots}
    parent = {r: None for r in roots}
    heap = [(ZERO, r) for r in sorted(roots)]
    heapq.heapify(heap)
    done = set()
    while heap:
        d, u = heapq.heappop(heap)
        if u in done:
            continue
        done.add(u)
        for v, label, mut in out_edges.get(u, ()):
            if v in parent and parent[v] is None:  # a root costs nothing: never reach it through an edge
                continue
            nd = (d[0] + mut, d[1] + 1)
            if v not in dist or nd < dist[v] or (nd == dist[v] and (u, label) < parent[v]):
                dist[v], parent[v] = nd, (u, label)
                heapq.heappush(heap, (nd, v))
    return dist, parent


def prefer_journey_edges(order, dist, parent, journey_sources, best_label):
    """Switch a state's parent to an edge a journey uses, when that cannot close a cycle.

    order: reachable non-root ids by (cost, id). A switch is allowed when the state is not on the
    journey source's own current chain, so parent pointers always form a forest rooted at the roots.
    """
    for v in order:
        for u in sorted((u for u in journey_sources.get(v, ()) if u in dist and u != v and (u, v) in best_label),
                        key=lambda u: (dist[u], u)):
            chain, x = set(), u
            while x is not None:
                chain.add(x)
                x = parent[x][0] if parent[x] else None
            if v not in chain:
                parent[v] = (u, best_label[(u, v)])
                break


def classify(map_doc, policy):
    """Every node in the map -> its state record (schemas.md, runs/<id>/states.json)."""
    nodes = map_nodes(map_doc)
    ids = {n["id"] for _, n in nodes}
    pattern = verb_pattern((policy or {}).get("mutatingVerbs"))

    def mutating(label):
        return 1 if pattern and pattern.search(label or "") else 0

    best = {}  # (from, to) -> (mutating, label) of the best edge between the two
    for a in map_doc["areas"]:
        for e in a.get("edges", []):
            f, t, label = e["from"], e["to"], e.get("label", "")
            if f in ids and t in ids and f != t:
                best[(f, t)] = min(best.get((f, t), (2, "")), (mutating(label), label))
    out_edges = collections.defaultdict(list)
    for (f, t), (mut, label) in sorted(best.items()):
        out_edges[f].append((t, label, mut))
    best_label = {k: v[1] for k, v in best.items()}

    journey_sources = collections.defaultdict(set)
    for a in map_doc["areas"]:
        for j in a.get("journeys", []):
            for u, v in zip(j.get("steps", []), j.get("steps", [])[1:]):
                journey_sources[v].add(u)

    roots = find_roots(nodes, ((policy or {}).get("roots") or {}).get("exclude", {}))
    dist, parent = cheapest_paths(roots, out_edges)
    order = sorted((v for v in dist if parent[v] is not None), key=lambda v: (dist[v], v))
    prefer_journey_edges(order, dist, parent, journey_sources, best_label)

    chains = {}

    def chain(v):
        """(path, edges) from a root to v, following parent pointers."""
        stack, x = [], v
        while x not in chains and parent[x] is not None:
            stack.append(x)
            x = parent[x][0]
        if x not in chains:  # x is a root
            chains[x] = ([x], [])
        for y in reversed(stack):
            u, label = parent[y]
            path, edges = chains[u]
            chains[y] = (path + [y], edges + [[u, y, label]])
        return chains[v]

    root_set = set(roots)
    states = {}
    for area, n in nodes:
        i, url = n["id"], n.get("url")
        s = {"area": area, "kind": n.get("kind"), "url": url, "root": i in root_set,
             "rootUrl": url if i in root_set else None, "needsRecipe": i not in root_set,
             "parent": None, "path": None, "labels": None, "mutates": False, "source": None, "unreachable": None}
        if i not in dist:
            s["unreachable"] = "no-path"
        else:
            path, edges = chain(i)
            s["path"] = path
            s["parent"] = path[-2] if len(path) > 1 else None
            s["labels"] = [e[2] for e in edges]
            s["mutates"] = any(mutating(e[2]) for e in edges)
            s["source"] = sha1(edges)
        states[i] = s
    return states


def build_doc(map_doc, policy, run_id):
    nodes = map_nodes(map_doc)
    states = classify(map_doc, policy)
    return {"run": run_id,
            "map": {"nodes": len(nodes), "sha": sha(sorted(n["id"] for _, n in nodes))},
            "roots": [i for i, s in states.items() if s["root"]],
            "states": states}


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

def counts(doc):
    states = doc["states"].values()
    return {"states": len(doc["states"]),
            "roots": sum(1 for s in states if s["root"]),
            "needsRecipe": sum(1 for s in states if s["needsRecipe"]),
            "mutating": sum(1 for s in states if s["mutates"]),
            "noPath": sum(1 for s in states if s["unreachable"] == "no-path")}


def status(ws, run_id):
    doc = read_json(ws.file("states.json", run_id)) if run_id else None
    return counts(doc) if doc else {"pending": True}


def summary(doc):
    c = counts(doc)
    kinds = collections.Counter(s["kind"] for s in doc["states"].values())
    by_kind = ", ".join(f"{k} {n}" for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))
    return (f"Run {doc['run']}: {c['states']} states, {c['roots']} root{'' if c['roots'] == 1 else 's'}, "
            f"{c['needsRecipe']} need a recipe, {c['mutating']} sit behind a mutating step, "
            f"{c['noPath']} have no path from a root. By kind: {by_kind}.")


def package_version(name):
    pkg = read_json(os.path.join(ROOT, "node_modules", name, "package.json"))
    return pkg.get("version") if pkg else None


def node_version():
    try:
        out = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=10, check=True)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def run_doc(ws, run_id, now):
    inside = not os.path.relpath(ws.path, ROOT).startswith("..")
    return {"run": run_id,
            "created": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "baseUrl": ws.base_url,
            "workspace": os.path.relpath(ws.path, ROOT) if inside else ws.path,
            "versions": {"playwright": package_version("playwright"), "axe": package_version("axe-core"),
                         "chromium": None, "node": node_version(), "python": platform.python_version()},
            "harness": None,
            "builds": [],
            "stages": {}}


def cli(ws, sub, argv):
    p = argparse.ArgumentParser(prog="audit.py plan", description=__doc__.split("\n\n")[0])
    p.add_argument("--force", action="store_true", help="overwrite an existing states.json for the run")
    args = p.parse_args(argv)

    policy = ws.policy()
    if policy is None:
        print(f"plan: no policy at {ws.policy_path}", file=sys.stderr)
        return 2
    map_doc = ws.load_map()
    now = datetime.datetime.now()
    run_id = ws.run or Workspace.new_run_id(now)
    states_path = ws.file("states.json", run_id)
    if os.path.exists(states_path) and not args.force:
        print(f"plan: run {run_id} already has a states.json. Wait a minute for a new run id, "
              f"or pass --run {run_id} --force to replace it.", file=sys.stderr)
        return 1

    doc = build_doc(map_doc, policy, run_id)
    write_json(ws.file("run.json", run_id), run_doc(ws, run_id, now))
    write_json(states_path, doc)  # last: a run exists once it has a states.json
    print(summary(doc))
    rel = os.path.relpath(states_path, ROOT)
    print(f"Wrote {states_path if rel.startswith('..') else rel}")
    return 0
