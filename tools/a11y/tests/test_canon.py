import json
import os
import subprocess
import tempfile
import unittest

from lib import canon

VECTORS = json.load(open(os.path.join(os.path.dirname(__file__), "data", "canon-vectors.json"), encoding="utf-8"))


def put(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def harness_fixture(root):
    """Every file and package the harness hash reads, each with distinct content."""
    for rel in ("checks/axe.mjs", "lib/canon.mjs", "sweep.mjs", "recipe.mjs", "fingerprint.mjs", "checks.json"):
        put(root, f"tools/a11y/{rel}", f"// {rel}\n")
    for name in ("axe-core", "playwright"):
        put(root, f"node_modules/{name}/package.json", json.dumps({"name": name, "version": "1.0.0"}))


class CanonTest(unittest.TestCase):
    def test_serialize(self):
        for v in VECTORS["serialize"]:
            self.assertEqual(canon.canonical(v["value"]), v["canonical"])
            if "dump" in v:
                self.assertEqual(canon.dump(v["value"]), v["dump"])

    def test_name_templates(self):
        for raw, want in VECTORS["templates"]:
            self.assertEqual(canon.name_template(raw), want, raw)

    def test_template_regex(self):
        for v in VECTORS["regex"]:
            rx = canon.name_template_regex(v["template"])
            for s in v["match"]:
                self.assertTrue(rx.match(s), f"{v['template']!r} should match {s!r}")
            for s in v["noMatch"]:
                self.assertFalse(rx.match(s), f"{v['template']!r} should not match {s!r}")

    def test_build_hash(self):
        for html, want in VECTORS["buildHash"]:
            self.assertEqual(canon.build_hash(html), want)

    def test_harness_hash_matches_node(self):
        node = subprocess.run(
            ["node", "--input-type=module", "-e",
             "import {harnessHash} from './tools/a11y/lib/canon.mjs'; console.log(harnessHash())"],
            cwd=canon.ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(node.stdout.strip(), canon.harness_hash())

    def test_harness_hash_matches_node_on_a_fixture_root(self):
        with tempfile.TemporaryDirectory() as root:
            harness_fixture(root)
            node = subprocess.run(
                ["node", "--input-type=module", "-e",
                 "import {harnessHash} from './tools/a11y/lib/canon.mjs'; console.log(harnessHash(process.argv[1]))", root],
                cwd=canon.ROOT, capture_output=True, text=True, check=True)
            self.assertEqual(node.stdout.strip(), canon.harness_hash(root))

    def test_harness_hash_changes_with_every_file_and_package_that_decides_a_raw_file(self):
        with tempfile.TemporaryDirectory() as root:
            harness_fixture(root)
            seen = {canon.harness_hash(root)}
            changes = [
                ("tools/a11y/sweep.mjs", "// changed\n"),
                ("tools/a11y/recipe.mjs", "// changed\n"),
                ("tools/a11y/fingerprint.mjs", "// changed\n"),
                ("tools/a11y/checks.json", "{}\n"),
                ("tools/a11y/checks/axe.mjs", "// changed\n"),
                ("tools/a11y/lib/canon.mjs", "// changed\n"),
                ("node_modules/axe-core/package.json", json.dumps({"version": "1.0.1"})),
                ("node_modules/playwright/package.json", json.dumps({"version": "1.0.1"})),
            ]
            for rel, text in changes:
                put(root, rel, text)
                h = canon.harness_hash(root)
                self.assertNotIn(h, seen, f"changing {rel} left the hash at {h}")
                seen.add(h)

    def test_harness_hash_ignores_files_the_sweep_does_not_read(self):
        with tempfile.TemporaryDirectory() as root:
            harness_fixture(root)
            before = canon.harness_hash(root)
            put(root, "tools/a11y/publish.py", "# changed\n")
            put(root, "tools/a11y/README.md", "changed\n")
            self.assertEqual(canon.harness_hash(root), before)

    def test_write_json_is_atomic_dump(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "x.json")
            canon.write_json(path, {"b": 1.0, "a": 2})
            with open(path) as f:
                self.assertEqual(f.read(), '{\n "a": 2,\n "b": 1\n}\n')
            self.assertEqual(os.listdir(os.path.dirname(path)), ["x.json"])


if __name__ == "__main__":
    unittest.main()
