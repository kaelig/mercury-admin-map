import json
import os
import subprocess
import unittest

from lib import canon

VECTORS = json.load(open(os.path.join(os.path.dirname(__file__), "data", "canon-vectors.json"), encoding="utf-8"))


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

    def test_write_json_is_atomic_dump(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "x.json")
            canon.write_json(path, {"b": 1.0, "a": 2})
            with open(path) as f:
                self.assertEqual(f.read(), '{\n "a": 2,\n "b": 1\n}\n')
            self.assertEqual(os.listdir(os.path.dirname(path)), ["x.json"])


if __name__ == "__main__":
    unittest.main()
