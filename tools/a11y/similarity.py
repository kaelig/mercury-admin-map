#!/usr/bin/env python3
"""How alike two screenshots look, from 0 to 1 (KTD8): `recipe check` compares a replay with the PNG
the capture took of the same state.

    python3 tools/a11y/similarity.py captured.png replay.png [replay2.png] [--json]

Both images are cropped to the viewport's 1440:900 shape from the top (a capture may be a full-page
shot), converted to grayscale and shrunk to 160×100 by area averaging. At that size text is a blur,
so a 2× capture of the same screen, a relative date that moved from "Yesterday" to "2 days ago" or a
changed balance barely register, while a different dialog, panel or page does. The score is the
mean SSIM over 10×10 windows, clipped to [0, 1].

THRESHOLD comes from the fixture app's screens (tools/a11y/fixtures/app/captures):

    same screen re-rendered at DPR 2               0.9998
    same screen, date drift                        0.9988
    filtered list vs the unfiltered page           0.83   closest pair of different screens
    two different wide dialogs over a page         0.80
    two different pages                            0.77
    neighboring dialog (Freeze card vs limits)     0.70

0.90 sits between them, with room below 1.0 for the demo's own data drift. A state that differs from
its parent page only by an overlay that leaves most of the page in view (a menu 0.98, a tooltip 0.97,
a side drawer 0.91) cannot be told apart by a screenshot, so `recipe check` also requires the top
layer the state's kind needs (KTD7).
Open Question: tune THRESHOLD on the first area recorded.

With two replays (`recipe check` takes one in light and one in dark), the score is the closer one's:
the capture was taken in one of the two themes.
"""
import json
import sys

from PIL import Image

THRESHOLD = 0.90
SIZE = (160, 100)
WINDOW = 10
ASPECT = 1440 / 900
C1 = (0.01 * 255) ** 2
C2 = (0.03 * 255) ** 2


def prepare(path):
    """Grayscale, viewport-shaped, 160×100 pixels as a bytes object (row-major)."""
    with Image.open(path) as im:
        im = im.convert("L")
        w, h = im.size
        if w / h < ASPECT:
            im = im.crop((0, 0, w, round(w / ASPECT)))
        elif w / h > ASPECT:
            im = im.crop((0, 0, round(h * ASPECT), h))
        return im.resize(SIZE, Image.Resampling.BOX).tobytes()


def ssim_window(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs) / n
    vy = sum((y - my) ** 2 for y in ys) / n
    cxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
    return ((2 * mx * my + C1) * (2 * cxy + C2)) / ((mx * mx + my * my + C1) * (vx + vy + C2))


def score_pixels(a, b):
    w, h = SIZE
    values = []
    for top in range(0, h, WINDOW):
        for left in range(0, w, WINDOW):
            idx = [(top + j) * w + left + i for j in range(WINDOW) for i in range(WINDOW)]
            values.append(ssim_window([a[k] for k in idx], [b[k] for k in idx]))
    return min(1.0, max(0.0, sum(values) / len(values)))


def score(path_a, path_b):
    """Similarity of two PNG files in [0, 1], rounded to 4 places."""
    return round(score_pixels(prepare(path_a), prepare(path_b)), 4)


def best_score(captured, replays):
    """The highest score() of `captured` against each replay, decoding `captured` once."""
    ref = prepare(captured)
    return max(round(score_pixels(ref, prepare(path)), 4) for path in replays)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    paths = [a for a in argv if not a.startswith("--")]
    if len(paths) not in (2, 3):
        print("usage: similarity.py a.png b.png [c.png] [--json]", file=sys.stderr)
        return 2
    s = best_score(paths[0], paths[1:])
    if "--json" in argv:
        print(json.dumps({"score": s, "threshold": THRESHOLD, "pass": s >= THRESHOLD}))
    else:
        print(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
