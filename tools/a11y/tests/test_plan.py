"""U1: the `plan` and `preflight` stages and the stage table (KTD5, KTD26, KTD27).

Run: cd tools/a11y && python3 -m unittest tests.test_plan -v
"""
import contextlib
import io
import json
import os
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.pathshim import ROOT

import audit
import plan
import preflight
from lib import canon

MAP_PATH = os.path.join(ROOT, "build", "map.json")
COMMITTED_A11Y = os.path.join(ROOT, "a11y")
VERBS = {"mutatingVerbs": ["save", "submit", "delete"]}


def graph(nodes, edges, journeys=()):
    """A one-area map document. nodes: (id, kind, url); edges: (from, to, label); journeys: step lists."""
    return {"areas": [{
        "area": "t",
        "nodes": [{"id": i, "kind": k, "url": u} for i, k, u in nodes],
        "edges": [{"from": f, "to": t, "label": l} for f, t, l in edges],
        "journeys": [{"id": f"t.j{n}", "steps": list(s)} for n, s in enumerate(journeys)],
    }]}


def run_plan(map_doc, policy=None, extra=()):
    """Run `audit.py plan` in a temp workspace; return (exit code, workspace path, stdout)."""
    tmp = tempfile.TemporaryDirectory()
    root = tmp.name
    map_file = os.path.join(root, "map.json")
    canon.write_json(map_file, map_doc)
    ws_dir = os.path.join(root, "ws")
    if policy is not None:
        canon.write_json(os.path.join(ws_dir, "policy.json"), policy)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = audit.main(["plan", "--workspace", ws_dir, "--map", map_file, *extra])
    return tmp, code, ws_dir, out.getvalue()


def states_of(ws_dir):
    runs = sorted(os.listdir(os.path.join(ws_dir, "runs")))
    return runs[-1], canon.read_json(os.path.join(ws_dir, "runs", runs[-1], "states.json"))


class UrlRule(unittest.TestCase):
    def test_concrete_urls(self):
        for url in ("/dashboard", "/accounts/credit?action=payCredit", "/insights/overview?chartMode=line",
                    "/books/books-3?endDate=08-31-2026&startDate=08-01-2026"):
            self.assertTrue(plan.is_concrete(url), url)

    def test_placeholder_urls(self):
        for url in ("/accounting?q=…", "/payments/recipients?recipient=...", "/invoicing/series/<id>",
                    "/checkbooks/hub/{id}", "/transactions?q={...}", "/x/[id]",
                    "/transactions?q={savedTransactionViewId:monthlyMoneyOut}", "/a>b", "", None):
            self.assertFalse(plan.is_concrete(url), url)


class Roots(unittest.TestCase):
    def test_roots_are_first_concrete_page_per_url_without_books(self):
        doc = graph([
            ("t.a", "page", "/a"),
            ("t.a2", "page", "/a"),                      # shares /a with t.a
            ("t.modal", "modal", "/m"),                  # not a page
            ("t.ph", "page", "/b?id=…"),            # placeholder
            ("t.brace", "page", "/c?q={saved:one}"),     # brace placeholder
            ("books.home", "page", "/books/home"),       # Books: never a root
            ("t.d", "page", "/d"),
        ], [])
        states = plan.classify(doc, VERBS)
        self.assertEqual([i for i, s in states.items() if s["root"]], ["t.a", "t.d"])
        self.assertEqual(states["t.a"]["rootUrl"], "/a")
        self.assertIsNone(states["t.a"]["parent"])
        self.assertEqual(states["t.a"]["path"], ["t.a"])
        for i in ("t.a2", "t.modal", "t.ph", "t.brace", "books.home"):
            self.assertFalse(states[i]["root"], i)
            self.assertTrue(states[i]["needsRecipe"], i)
        self.assertFalse(states["t.a"]["needsRecipe"])


class ParentChoice(unittest.TestCase):
    def test_journey_edge_beats_shorter_edge(self):
        nodes = [("t.r", "page", "/r"), ("t.a", "modal", "/r"), ("t.v", "modal", "/r")]
        edges = [("t.r", "t.v", "Click V"), ("t.r", "t.a", "Click A"), ("t.a", "t.v", "Click through A")]
        with_journey = plan.classify(graph(nodes, edges, [["t.r", "t.a", "t.v"]]), VERBS)
        self.assertEqual(with_journey["t.v"]["parent"], "t.a")
        self.assertEqual(with_journey["t.v"]["path"], ["t.r", "t.a", "t.v"])
        self.assertEqual(with_journey["t.v"]["labels"], ["Click A", "Click through A"])
        without = plan.classify(graph(nodes, edges), VERBS)
        self.assertEqual(without["t.v"]["parent"], "t.r")
        self.assertEqual(without["t.v"]["path"], ["t.r", "t.v"])

    def test_journey_edge_that_would_close_a_cycle_is_skipped(self):
        # B's path is [R, A, B]; the journey edge B -> A would put A on its own path.
        nodes = [("t.r", "page", "/r"), ("t.a", "modal", "/r"), ("t.b", "modal", "/r")]
        edges = [("t.r", "t.a", "Click A"), ("t.a", "t.b", "Click B"), ("t.b", "t.a", "Back to A")]
        states = plan.classify(graph(nodes, edges, [["t.b", "t.a"]]), VERBS)
        self.assertEqual(states["t.a"]["parent"], "t.r")
        self.assertEqual(states["t.b"]["path"], ["t.r", "t.a", "t.b"])

    def test_every_chain_ends_at_a_root_without_repeats(self):
        # A ring of journey edges: every node's journey source is another ring member.
        ids = ["t.n1", "t.n2", "t.n3", "t.n4"]
        nodes = [("t.r", "page", "/r")] + [(i, "modal", "/r") for i in ids]
        edges = [("t.r", "t.n1", "Click 1"), ("t.r", "t.n2", "Click 2"), ("t.r", "t.n3", "Click 3"),
                 ("t.r", "t.n4", "Click 4"),
                 ("t.n1", "t.n2", "a"), ("t.n2", "t.n3", "b"), ("t.n3", "t.n4", "c"), ("t.n4", "t.n1", "d")]
        states = plan.classify(graph(nodes, edges, [["t.n1", "t.n2", "t.n3", "t.n4", "t.n1"]]), VERBS)
        for i in ids:
            path = states[i]["path"]
            self.assertEqual(path[0], "t.r")
            self.assertEqual(path[-1], i)
            self.assertEqual(len(path), len(set(path)), path)
            self.assertEqual(path[-2], states[i]["parent"])

    def test_non_mutating_longer_path_beats_mutating_shortest(self):
        nodes = [("t.r", "page", "/r"), ("t.m", "menu", "/r"), ("t.v", "modal", "/r")]
        edges = [("t.r", "t.v", "Click Save"), ("t.r", "t.m", "Click menu"), ("t.m", "t.v", "Click details")]
        states = plan.classify(graph(nodes, edges), VERBS)
        self.assertEqual(states["t.v"]["path"], ["t.r", "t.m", "t.v"])
        self.assertFalse(states["t.v"]["mutates"])

    def test_mutating_only_path_is_marked_and_hashed(self):
        nodes = [("t.r", "page", "/r"), ("t.w", "toast", "/r")]
        edges = [("t.r", "t.w", "Type a name and click SAVE")]
        states = plan.classify(graph(nodes, edges), VERBS)
        self.assertTrue(states["t.w"]["mutates"])
        self.assertEqual(states["t.w"]["source"], canon.sha1([["t.r", "t.w", "Type a name and click SAVE"]]))

    def test_verbs_match_whole_words_only(self):
        nodes = [("t.r", "page", "/r"), ("t.a", "modal", "/r"), ("t.b", "modal", "/r")]
        edges = [("t.r", "t.a", "Click Saved views"), ("t.a", "t.b", "Click Save, then close")]
        states = plan.classify(graph(nodes, edges), VERBS)
        self.assertFalse(states["t.a"]["mutates"])
        self.assertTrue(states["t.b"]["mutates"])

    def test_prefers_fewer_hops_among_equally_mutating_paths(self):
        nodes = [("t.r", "page", "/r"), ("t.a", "menu", "/r"), ("t.b", "menu", "/r"), ("t.v", "modal", "/r")]
        edges = [("t.r", "t.a", "x"), ("t.a", "t.b", "y"), ("t.b", "t.v", "z"), ("t.r", "t.v", "direct")]
        states = plan.classify(graph(nodes, edges), VERBS)
        self.assertEqual(states["t.v"]["path"], ["t.r", "t.v"])

    def test_ties_break_by_node_id(self):
        nodes = [("t.r", "page", "/r"), ("t.b", "menu", "/r"), ("t.a", "menu", "/r"), ("t.v", "modal", "/r")]
        edges = [("t.r", "t.b", "x"), ("t.r", "t.a", "x"), ("t.b", "t.v", "y"), ("t.a", "t.v", "y")]
        states = plan.classify(graph(nodes, edges), VERBS)
        self.assertEqual(states["t.v"]["parent"], "t.a")


class Unreachable(unittest.TestCase):
    def test_node_with_no_path_from_a_root(self):
        nodes = [("t.r", "page", "/r"), ("t.x", "modal", "/r"), ("t.y", "toast", "/r"), ("t.ok", "modal", "/r")]
        edges = [("t.r", "t.ok", "Click ok"), ("t.x", "t.y", "Click Save")]
        states = plan.classify(graph(nodes, edges), VERBS)
        for i in ("t.x", "t.y"):
            self.assertEqual(states[i]["unreachable"], "no-path", i)
            self.assertIsNone(states[i]["parent"], i)
            self.assertIsNone(states[i]["path"], i)
            self.assertTrue(states[i]["needsRecipe"], i)
        self.assertIsNone(states["t.ok"]["unreachable"])


class PlanStage(unittest.TestCase):
    SMALL = graph(
        [("t.r", "page", "/r"), ("t.a", "modal", "/r"), ("t.b", "toast", "/r"), ("t.x", "modal", "/r")],
        [("t.r", "t.a", "Click Open"), ("t.a", "t.b", "Click Save")])

    def test_writes_states_and_run_json_in_the_workspace(self):
        tmp, code, ws_dir, out = run_plan(self.SMALL, VERBS)
        with tmp:
            self.assertEqual(code, 0)
            run_id, doc = states_of(ws_dir)
            self.assertRegex(run_id, r"^\d{4}-\d{2}-\d{2}-\d{4}$")
            self.assertEqual(doc["run"], run_id)
            self.assertEqual(doc["map"]["nodes"], 4)
            self.assertEqual(doc["map"]["sha"], canon.sha(sorted(["t.r", "t.a", "t.b", "t.x"])))
            self.assertEqual(doc["roots"], ["t.r"])
            self.assertEqual(sorted(doc["states"]), ["t.a", "t.b", "t.r", "t.x"])
            self.assertEqual(doc["states"]["t.b"]["area"], "t")
            self.assertEqual(doc["states"]["t.b"]["kind"], "toast")
            self.assertEqual(doc["states"]["t.b"]["url"], "/r")
            run = canon.read_json(os.path.join(ws_dir, "runs", run_id, "run.json"))
            self.assertEqual(run["run"], run_id)
            self.assertEqual(run["workspace"], ws_dir)
            self.assertEqual(run["baseUrl"], "https://demo.mercury.com")
            self.assertEqual(run["versions"]["playwright"], package_version("playwright"))
            self.assertEqual(run["versions"]["axe"], package_version("axe-core"))
            self.assertTrue(run["versions"]["python"])
            self.assertTrue(run["versions"]["node"].startswith("v"))
            self.assertEqual(run["builds"], [])
            self.assertEqual(run["stages"], {})
            self.assertIsNone(run["harness"])
            # the summary names the counts
            self.assertIn("4 states", out)
            self.assertIn("1 root", out)

    def test_status_counts(self):
        tmp, code, ws_dir, _ = run_plan(self.SMALL, VERBS)
        with tmp:
            run_id, _ = states_of(ws_dir)
            ws = plan.Workspace(workspace=ws_dir)
            self.assertEqual(plan.status(ws, run_id),
                             {"states": 4, "roots": 1, "needsRecipe": 3, "mutating": 1, "noPath": 1})
            self.assertEqual(plan.status(ws, "2000-01-01-0000"), {"pending": True})

    def test_explicit_run_is_not_overwritten_without_force(self):
        tmp, code, ws_dir, _ = run_plan(self.SMALL, VERBS, ["--run", "2026-01-02-0304"])
        with tmp:
            self.assertEqual(code, 0)
            self.assertEqual(states_of(ws_dir)[0], "2026-01-02-0304")
            map_file = os.path.join(os.path.dirname(ws_dir), "map.json")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                again = audit.main(["plan", "--workspace", ws_dir, "--map", map_file, "--run", "2026-01-02-0304"])
                forced = audit.main(["plan", "--workspace", ws_dir, "--map", map_file, "--run", "2026-01-02-0304",
                                     "--force"])
            self.assertNotEqual(again, 0)
            self.assertEqual(forced, 0)

    def test_workspace_without_policy_reads_the_committed_one(self):
        doc = graph([("t.r", "page", "/r"), ("t.w", "toast", "/r")], [("t.r", "t.w", "Click Save")])
        before = tree_state(os.path.join(COMMITTED_A11Y, "runs"))
        policy_bytes = read_bytes(os.path.join(COMMITTED_A11Y, "policy.json"))
        tmp, code, ws_dir, _ = run_plan(doc)  # no workspace policy.json
        with tmp:
            self.assertEqual(code, 0)
            self.assertFalse(os.path.exists(os.path.join(ws_dir, "policy.json")))
            run_id, states = states_of(ws_dir)
            self.assertTrue(states["states"]["t.w"]["mutates"])  # "save" comes from the committed verb list
            self.assertTrue(os.path.exists(os.path.join(ws_dir, "runs", run_id, "run.json")))
        self.assertEqual(tree_state(os.path.join(COMMITTED_A11Y, "runs")), before)
        self.assertEqual(read_bytes(os.path.join(COMMITTED_A11Y, "policy.json")), policy_bytes)

    def test_workspace_policy_wins_over_the_committed_one(self):
        doc = graph([("t.r", "page", "/r"), ("t.w", "toast", "/r")], [("t.r", "t.w", "Click Save")])
        tmp, code, ws_dir, _ = run_plan(doc, {"version": 1, "mutatingVerbs": ["zzz"]})
        with tmp:
            self.assertEqual(code, 0)
            self.assertFalse(states_of(ws_dir)[1]["states"]["t.w"]["mutates"])


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def package_version(name):
    with open(os.path.join(ROOT, "node_modules", name, "package.json")) as f:
        return json.load(f)["version"]


def tree_state(path):
    out = {}
    for base, _, files in os.walk(path):
        for f in files:
            p = os.path.join(base, f)
            out[p] = os.stat(p).st_mtime_ns
    return out


@unittest.skipUnless(os.path.exists(MAP_PATH), "build/map.json is missing: run `python3 build.py` first")
class CurrentData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(MAP_PATH) as f:
            cls.map = json.load(f)
        cls.nodes = [n for a in cls.map["areas"] for n in a["nodes"]]
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = audit.main(["plan", "--workspace", tmp.name, "--map", MAP_PATH])
        assert code == 0, out.getvalue()
        cls.run_id, cls.doc = states_of(tmp.name)
        cls.states = cls.doc["states"]

    def test_states_match_the_map_nodes(self):
        self.assertEqual(set(self.states), {n["id"] for n in self.nodes})
        self.assertEqual(len(self.states), len(self.nodes))
        self.assertEqual(self.doc["map"]["nodes"], len(self.nodes))

    def test_no_root_is_a_books_node(self):
        self.assertTrue(self.doc["roots"])
        for r in self.doc["roots"]:
            self.assertFalse(r.startswith("books."), r)
            self.assertTrue(self.states[r]["root"])

    def test_roots_are_the_first_concrete_non_books_page_of_each_url(self):
        seen, expected = set(), []
        for n in self.nodes:
            u = n["url"]
            if n["kind"] != "page" or n["id"].startswith("books.") or re.search(r"[…<>{}\[\]]|\.\.\.", u):
                continue
            if u not in seen:
                seen.add(u)
                expected.append(n["id"])
        self.assertEqual(self.doc["roots"], expected)

    def test_every_non_root_has_a_chain_ending_at_a_root(self):
        for i, s in self.states.items():
            if s["root"] or s["unreachable"]:
                continue
            self.assertEqual(s["path"][-1], i, i)
            self.assertTrue(self.states[s["path"][0]]["root"], i)
            self.assertEqual(len(s["path"]), len(set(s["path"])), i)
            self.assertEqual(s["path"][-2], s["parent"], i)
            self.assertEqual(len(s["labels"]), len(s["path"]) - 1, i)
            # the parent's own path is the prefix: chains are consistent
            self.assertEqual(self.states[s["parent"]]["path"], s["path"][:-1], i)

    def test_placeholder_and_shared_url_pages_need_recipes(self):
        first = {}
        for n in self.nodes:
            if n["kind"] == "page" and not n["id"].startswith("books."):
                first.setdefault(n["url"], n["id"])
        checked = 0
        for n in self.nodes:
            s = self.states[n["id"]]
            placeholder = bool(re.search(r"[…<>{}\[\]]|\.\.\.", n["url"]))
            shared = n["kind"] == "page" and first.get(n["url"]) not in (None, n["id"])
            if placeholder or shared or n["kind"] != "page":
                self.assertTrue(s["needsRecipe"], n["id"])
                self.assertFalse(s["root"], n["id"])
                checked += 1
        self.assertGreater(checked, 700)
        self.assertEqual(sum(1 for s in self.states.values() if not s["needsRecipe"]), len(self.doc["roots"]))

    def test_books_states_are_reached_through_the_books_toggle(self):
        books = [s for i, s in self.states.items() if i.startswith("books.") and not s["unreachable"]]
        self.assertTrue(books)
        for s in books:
            self.assertFalse(self.states[s["path"][0]]["area"] == "books")


class Preflight(unittest.TestCase):
    def serve(self, status, body):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status if self.path == "/dashboard" else 404)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def run_preflight(self, base_url, map_path):
        ws = plan.Workspace(map=map_path, base_url=base_url)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = preflight.cli(ws, None, [])
        return code, out.getvalue(), err.getvalue()

    def test_all_checks_pass_and_the_build_hash_is_read(self):
        base = self.serve(200, '<script src="https://cdn.mercury.com/builds/abc123/app.js"></script>')
        with tempfile.TemporaryDirectory() as tmp:
            map_file = os.path.join(tmp, "map.json")
            canon.write_json(map_file, {"areas": []})
            code, out, err = self.run_preflight(base, map_file)
            self.assertEqual(os.listdir(tmp), ["map.json"])  # writes nothing
        self.assertEqual(code, 0, err)
        summary = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["build"], "abc123")

    def test_failures_are_named(self):
        base = self.serve(503, "down")
        code, out, err = self.run_preflight(base, os.path.join(ROOT, "build", "no-such-map.json"))
        self.assertNotEqual(code, 0)
        summary = json.loads(out.strip().splitlines()[-1])
        self.assertFalse(summary["ok"])
        self.assertFalse(summary["checks"]["map"]["ok"])
        self.assertFalse(summary["checks"]["demo"]["ok"])
        self.assertTrue(summary["checks"]["playwright"]["ok"])
        self.assertIn("map", err)
        self.assertIn("demo", err)

    def test_unreachable_demo_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            map_file = os.path.join(tmp, "map.json")
            canon.write_json(map_file, {"areas": []})
            code, out, err = self.run_preflight("http://127.0.0.1:9", map_file)
        self.assertNotEqual(code, 0)
        self.assertIn("demo", err)


class Policy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = canon.read_json(os.path.join(COMMITTED_A11Y, "policy.json"))

    def test_shape(self):
        p = self.policy
        self.assertEqual(p["version"], 1)
        self.assertEqual(p["origin"], "https://demo.mercury.com")
        self.assertEqual(p["allowedOrigins"], ["https://demo.mercury.com"])
        self.assertEqual(p["isolation"]["status"], "unchecked")
        self.assertIsNone(p["isolation"]["date"])
        self.assertEqual(p["replay"], {"onlyRecordedEdges": True})

    def test_denylist_holds_the_capture_prohibitions(self):
        names = [n.lower() for n in self.policy["denylist"]["names"]]
        for n in ("logout", "log out", "sign out"):
            self.assertIn(n, names)
        notes = " ".join(self.policy["denylist"]["notes"])
        self.assertIn("Do NOT click Logout", notes)
        self.assertIn("Do NOT cover Bill Pay", notes)
        with open(os.path.join(ROOT, "recapture", "scopes.json")) as f:
            scopes = json.load(f)
        for area in scopes.values():
            for m in re.finditer(r"Do NOT [^.]*\.", area["scope"]):
                self.assertIn(m.group(0).rstrip("."), notes)

    def test_fixture_values_are_obviously_fake(self):
        v = self.policy["fixtureValues"]
        self.assertTrue(v["email"].endswith("@example.invalid"))
        self.assertRegex(v["phone"], r"555-01\d\d$")
        self.assertRegex(v["amount"], r"^\d+\.\d{2}$")
        for key in ("name", "firstName", "lastName", "businessName", "memo", "nickname", "addressLine1", "city",
                    "state", "zip"):
            self.assertTrue(v[key], key)
        for value in v.values():
            self.assertIsInstance(value, str)

    def test_mutating_verbs_are_unique_and_lowercase(self):
        verbs = self.policy["mutatingVerbs"]
        self.assertEqual(len(verbs), len(set(verbs)))
        for v in verbs:
            self.assertEqual(v, v.lower())
        for v in ("submit", "create", "send", "delete", "invite", "confirm", "save"):
            self.assertIn(v, verbs)

    def test_verbs_flag_submits_and_leave_navigation_alone(self):
        pattern = plan.verb_pattern(self.policy["mutatingVerbs"])
        for label in ("Click Create account", "Click Yes, delete", "Enter name, click Create recipient",
                      "Click Confirm payment", "Enter amount, click Request Increase", "Pick Alice Chen, click Transfer",
                      "Click Send invite", "Enter a backup code and click Submit", "Click Deposit check"):
            self.assertTrue(pattern.search(label), label)
        for label in ("Click Add filter", "Click Saved views", "Click Cancel", "Click Next", "Open Transfer from",
                      "Click Bill Pay widget kebab", "Click row", "Sidebar: Reports", "Click Edit limit", "Hover the info icon"):
            self.assertFalse(pattern.search(label), label)


class StageTable(unittest.TestCase):
    def test_every_stage_has_an_owning_module_and_unit(self):
        self.assertTrue(audit.STAGES)
        for cmd, sub, lang, module, unit, summary in audit.STAGES:
            name = f"{cmd} {sub}" if sub else cmd
            self.assertRegex(module, r"^[a-z][a-z0-9_-]*\.(py|mjs)$", name)
            self.assertEqual(module.endswith(".py"), lang == "py", name)
            self.assertEqual(module.endswith(".mjs"), lang == "node", name)
            self.assertRegex(unit, r"^U\d+$", name)
            self.assertTrue(summary, name)

    def test_u1_stage_modules_exist(self):
        for cmd, sub, lang, module, unit, summary in audit.STAGES:
            if cmd in ("plan", "preflight"):
                self.assertTrue(os.path.exists(os.path.join(audit.HERE, module)), module)


if __name__ == "__main__":
    unittest.main()
