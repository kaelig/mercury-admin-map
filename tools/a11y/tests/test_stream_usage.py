"""Token usage of a headless recorder run, from its stream-json file (record-headless.sh).

Run: cd tools/a11y && python3 -m unittest tests.test_stream_usage -v
"""
import json
import os
import tempfile
import unittest

from tests.pathshim import A11Y  # noqa: F401  (puts tools/a11y on sys.path)

import stream_usage


def assistant(mid, inp, write, read, out):
    return {"type": "assistant", "message": {"id": mid, "model": "claude-sonnet-5-5", "usage": {
        "input_tokens": inp, "cache_creation_input_tokens": write, "cache_read_input_tokens": read, "output_tokens": out}}}


class StreamUsage(unittest.TestCase):
    def write(self, lines):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")
        self.addCleanup(os.remove, path)
        return path

    def test_dedupes_lines_of_one_message_and_takes_output_from_the_result_line(self):
        # Message m1 spans two lines (two content blocks) that repeat its usage with a partial output.
        path = self.write([
            {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5"},
            assistant("m1", 3, 3000, 0, 4), assistant("m1", 3, 3000, 0, 4),
            assistant("m2", 2, 500, 3000, 9),
            {"type": "result", "total_cost_usd": 0.0123, "num_turns": 2,
             "usage": {"input_tokens": 5, "cache_creation_input_tokens": 3500, "cache_read_input_tokens": 3000, "output_tokens": 700}},
        ])
        s = stream_usage.summarize(path)
        self.assertEqual((s["turns"], s["input"], s["cacheWrite"], s["cacheRead"]), (2, 5, 3500, 3000))
        self.assertEqual((s["output"], s["outputSource"]), (700, "result"))
        self.assertEqual(s["processed"], 5 + 3500 + 3000 + 700)
        self.assertEqual(s["firstTurnContext"], 3003)
        self.assertEqual(s["costUsd"], 0.0123)
        self.assertNotIn("resultUsageMismatch", s)

    def test_without_a_result_line_output_is_the_largest_count_per_message(self):
        path = self.write([assistant("m1", 1, 100, 0, 4), assistant("m1", 1, 100, 0, 40), assistant("m2", 1, 0, 100, 7)])
        s = stream_usage.summarize(path)
        self.assertEqual((s["output"], s["costUsd"]), (47, None))
        self.assertNotEqual(s["outputSource"], "result")

    def test_reports_a_result_line_that_disagrees_with_the_message_sums(self):
        path = self.write([assistant("m1", 1, 100, 0, 4),
                           {"type": "result", "usage": {"input_tokens": 1, "cache_creation_input_tokens": 999,
                                                        "cache_read_input_tokens": 0, "output_tokens": 4}}])
        self.assertEqual(stream_usage.summarize(path)["resultUsageMismatch"], {"cache_creation_input_tokens": 999})


if __name__ == "__main__":
    unittest.main()
