"""U6: the shared map-data loader (KTD22), raw read only. U8 adds the snapshot merge.

Run: cd tools/a11y && python3 -m unittest tests.test_mapdata -v
"""
import glob
import json
import os
import tempfile
import unittest
from unittest import mock

from tests.pathshim import ROOT

import mapdata

DATA = os.path.join(ROOT, "data")


def raw_areas(data_dir):
    out = []
    for path in sorted(glob.glob(os.path.join(data_dir, "*.json"))):
        with open(path, encoding="utf-8") as f:
            out.append(json.load(f))
    return out


class LoadAreas(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.missing = os.path.join(self.tmp.name, "no-snapshot")

    def tearDown(self):
        self.tmp.cleanup()

    def test_without_a_snapshot_the_result_is_the_raw_data(self):
        expected = raw_areas(DATA)
        self.assertEqual(len(expected), 9)
        self.assertEqual(mapdata.load_areas(DATA, snapshot_dir=False), expected)
        self.assertEqual(mapdata.load_areas(DATA, snapshot_dir=self.missing), expected)
        with mock.patch.dict(os.environ, {"A11Y_SNAPSHOT": self.missing}):
            self.assertEqual(mapdata.load_areas(), expected)

    def test_default_snapshot_is_the_committed_published_folder(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("A11Y_SNAPSHOT", None)
            self.assertEqual(mapdata.snapshot_path(), os.path.join(ROOT, "a11y", "published"))
            self.assertEqual(mapdata.snapshot_path(self.missing), self.missing)
        with mock.patch.dict(os.environ, {"A11Y_SNAPSHOT": self.missing}):
            self.assertEqual(mapdata.snapshot_path(), self.missing)
            self.assertEqual(mapdata.snapshot_path("/elsewhere"), "/elsewhere")

    def test_files_are_read_in_sorted_order_as_utf8(self):
        d = os.path.join(self.tmp.name, "data")
        os.makedirs(d)
        for name, title in (("zeta.json", "Zêta"), ("alpha.json", "Alpha — one")):
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                json.dump({"area": name[:-5], "title": title}, f, ensure_ascii=False)
        areas = mapdata.load_areas(d, snapshot_dir=False)
        self.assertEqual([a["title"] for a in areas], ["Alpha — one", "Zêta"])

    def test_an_existing_snapshot_goes_through_the_merge(self):
        snap = os.path.join(self.tmp.name, "published")
        os.makedirs(snap)
        with self.assertRaisesRegex(NotImplementedError, "U8"):
            mapdata.load_areas(DATA, snapshot_dir=snap)
        with mock.patch.dict(os.environ, {"A11Y_SNAPSHOT": snap}):
            with self.assertRaisesRegex(NotImplementedError, "U8"):
                mapdata.load_areas(DATA)
        self.assertEqual(mapdata.load_areas(DATA, snapshot_dir=False), raw_areas(DATA))


if __name__ == "__main__":
    unittest.main()
