"""Shared loader for the map's area data (KTD22).

`build.py`, `tools/export_agent_data.py` and the accessibility audit read `data/*.json` through
`load_areas()`. With a published accessibility snapshot, the snapshot's generated issues and overlays
are merged in; audit stages pass `snapshot_dir=False` to read the raw data. Stdlib only.
"""
import glob
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_areas(data_dir):
    """Every data/*.json area, in file-name order, exactly as stored."""
    areas = []
    for path in sorted(glob.glob(os.path.join(data_dir, "*.json"))):
        with open(path, encoding="utf-8") as f:
            areas.append(json.load(f))
    return areas


def snapshot_path(snapshot_dir=None):
    """The snapshot directory: the argument, else $A11Y_SNAPSHOT, else <repo>/a11y/published/."""
    return snapshot_dir or os.environ.get("A11Y_SNAPSHOT") or os.path.join(ROOT, "a11y", "published")


def load_areas(data_dir=None, snapshot_dir=None):
    """The area dicts from data_dir (default <repo>/data), merged with the snapshot when one exists.

    snapshot_dir=False reads the raw data. A snapshot directory that does not exist also gives the raw data.
    """
    areas = read_areas(data_dir or os.path.join(ROOT, "data"))
    if snapshot_dir is False:
        return areas
    snap = snapshot_path(snapshot_dir)
    if not os.path.isdir(snap):
        return areas
    return merge_snapshot(areas, snap)


def merge_snapshot(areas, snapshot_dir):
    """Append generated issues, apply overlays, prune unknown nodeIds (KTD22)."""
    raise NotImplementedError("snapshot merge lands in U8")
