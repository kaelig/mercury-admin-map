"""U4: screenshot similarity for `recipe check` (KTD8), on the fixture app's captured screens.

Run: cd tools/a11y && python3 -m unittest tests.test_similarity -v
"""
import contextlib
import io
import json
import os
import tempfile
import unittest

from PIL import Image

from tests.pathshim import ROOT

import similarity

SCREENS = os.path.join(ROOT, "tools", "a11y", "fixtures", "app", "captures")


def screen(name):
    return os.path.join(SCREENS, name)


class SimilarityTest(unittest.TestCase):
    def assertAbove(self, a, b):
        s = similarity.score(screen(a), screen(b))
        self.assertGreaterEqual(s, similarity.THRESHOLD, f"{a} vs {b} scored {s}")
        return s

    def assertBelow(self, a, b):
        s = similarity.score(screen(a), screen(b))
        self.assertLess(s, similarity.THRESHOLD, f"{a} vs {b} scored {s}")
        return s

    def test_threshold_is_a_fraction(self):
        self.assertGreater(similarity.THRESHOLD, 0.5)
        self.assertLess(similarity.THRESHOLD, 1)

    def test_a_screen_against_itself_scores_one(self):
        self.assertEqual(similarity.score(screen("cards.edit-limit-modal.png"), screen("cards.edit-limit-modal.png")), 1.0)

    def test_a_rerender_at_twice_the_pixel_ratio_scores_above_the_threshold(self):
        # The demo's captures are 2880×1800 (DPR 2); replays are 1440×900 (DPR 1).
        self.assertAbove("cards.edit-limit-modal.png", "rerender/cards.edit-limit-modal.dpr2.png")

    def test_a_rerender_with_a_drifted_date_scores_above_the_threshold(self):
        # Blue Bottle Coffee paid "Yesterday" in the capture and "2 days ago" in the re-render.
        self.assertAbove("transactions.index.png", "rerender/transactions.index.drift.png")

    def test_a_neighboring_dialog_scores_below_the_threshold(self):
        self.assertBelow("cards.edit-limit-modal.png", "cards.freeze-modal.png")

    def test_other_dialogs_and_pages_score_below_the_threshold(self):
        self.assertBelow("home.send-modal.png", "cards.edit-limit-modal.png")
        self.assertBelow("cards.index.png", "transactions.index.png")
        self.assertBelow("transactions.index.png", "transactions.card-payments.png")
        self.assertBelow("home.index.png", "books.index.png")

    def test_a_full_page_capture_is_cropped_to_the_viewport(self):
        with tempfile.TemporaryDirectory() as tmp, Image.open(screen("cards.index.png")) as im:
            tall = Image.new("RGB", (1440, 1253), "black")
            tall.paste(im.convert("RGB"), (0, 0))
            path = os.path.join(tmp, "tall.png")
            tall.save(path)
            self.assertEqual(similarity.score(screen("cards.index.png"), path), 1.0)

    def test_cli_prints_the_score_and_json(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = similarity.main([screen("cards.edit-limit-modal.png"), screen("cards.freeze-modal.png")])
        self.assertEqual(code, 0)
        value = float(out.getvalue())
        self.assertTrue(0 <= value < similarity.THRESHOLD)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            similarity.main([screen("cards.index.png"), screen("cards.index.png"), "--json"])
        self.assertEqual(json.loads(out.getvalue()), {"score": 1.0, "threshold": similarity.THRESHOLD, "pass": True})

    def test_cli_with_two_replays_scores_the_closer_one(self):
        captured = screen("cards.edit-limit-modal.png")
        far, near = screen("cards.freeze-modal.png"), screen("rerender/cards.edit-limit-modal.dpr2.png")
        best = max(similarity.score(captured, far), similarity.score(captured, near))
        self.assertEqual(best, similarity.score(captured, near))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = similarity.main([captured, far, near, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), {"score": best, "threshold": similarity.THRESHOLD, "pass": True})


if __name__ == "__main__":
    unittest.main()
