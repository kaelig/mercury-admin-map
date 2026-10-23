"""Shared loader for the map's area data (KTD22).

`build.py`, `tools/export_agent_data.py` and the accessibility audit read `data/*.json` through
`load_areas()`. With a published accessibility snapshot, the snapshot's generated issues and overlays
are merged in; audit stages pass `snapshot_dir=False` to read the raw data. Stdlib only.
"""
import copy
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The fields the map viewer reads from an issue; build.py inlines nothing else (KTD22).
VIEWER_FIELDS = ("id", "title", "severity", "category", "detail", "evidence", "nodeIds")
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
OVERLAY_FIELDS = ("wcag", "audit", "history")
# What the map and the report say over a test snapshot (meta.json testData, written by tests/ledger.py only).
TEST_DATA_NOTE = "Test data: the accessibility findings on this page are invented for tests. They are not audit results."


def read_areas(data_dir, problems=None):
    """Every data/*.json area, in file-name order, exactly as stored.

    A file that does not parse raises, unless problems is a list: the file is then skipped and
    "<file>: invalid JSON (<error>)" is appended to it. With $MAP_SKIP_INVALID_DATA set, it is skipped
    without a word: build.py sets it for the export it runs, and reports the file itself.
    """
    areas = []
    for path in sorted(glob.glob(os.path.join(data_dir, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                areas.append(json.load(f))
        except Exception as e:
            if problems is not None:
                problems.append(f"{os.path.basename(path)}: invalid JSON ({e})")
            elif not os.environ.get("MAP_SKIP_INVALID_DATA"):
                raise
    return areas


def snapshot_path(snapshot_dir=None):
    """The snapshot directory: the argument, else $A11Y_SNAPSHOT, else <repo>/a11y/published/."""
    return snapshot_dir or os.environ.get("A11Y_SNAPSHOT") or os.path.join(ROOT, "a11y", "published")


def is_test_data(snapshot_dir=None):
    """True when the snapshot's meta.json has testData: tests/ledger.py wrote it and its findings are invented."""
    path = os.path.join(snapshot_path(snapshot_dir), "meta.json")
    if not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("testData") is True


def load_areas(data_dir=None, snapshot_dir=None, problems=None):
    """The area dicts from data_dir (default <repo>/data), merged with the snapshot when one exists.

    problems is passed to read_areas(): build.py collects unreadable files there instead of stopping.

    snapshot_dir=False reads the raw data. A snapshot directory that does not exist also gives the raw
    data. A publish that stopped between its two renames left only <snapshot>.prev/: it is restored first.
    """
    areas = read_areas(data_dir or os.path.join(ROOT, "data"), problems)
    if snapshot_dir is False:
        return areas
    snap = snapshot_path(snapshot_dir)
    restore_snapshot(snap)
    if not os.path.isdir(snap):
        return areas
    return merge_snapshot(areas, snap)


def restore_snapshot(snapshot_dir):
    """Rename <snapshot>.prev/ back to <snapshot>/ when only the former exists (KTD21). True when it did."""
    snap = os.path.normpath(snapshot_dir)
    prev = snap + ".prev"
    if not os.path.exists(snap) and os.path.isdir(prev):
        os.rename(prev, snap)
        return True
    return False


def warn(message):
    """A problem line in build.py's format."""
    print("WARN", message, file=sys.stderr)


def viewer_issue(issue):
    """The issue with the viewer's fields only, in the order the issue holds them."""
    return {k: v for k, v in issue.items() if k in VIEWER_FIELDS}


def issue_order(issue):
    """Sort key within an area: severity (high first), then screen count (most first), then id."""
    return (SEVERITY_RANK.get(issue.get("severity"), len(SEVERITY_RANK)), -len(issue.get("nodeIds") or []),
            issue.get("id") or "")


def order_issues(issues):
    return sorted(issues, key=issue_order)


def known_screens(issue_id, node_ids, known):
    """node_ids without the screens data/ does not have, each one reported."""
    kept = []
    for n in node_ids or []:
        if n in known:
            kept.append(n)
        else:
            warn(f"{issue_id}: dropped unknown screen {n} from the a11y snapshot")
    return kept


def merge_snapshot(areas, snapshot_dir):
    """A copy of areas with the snapshot merged in (KTD21, KTD22).

    Overlays apply to the hand-found issues they name: a severity, a sentence appended to the
    evidence, added screens, and the wcag, audit and history fields for the export. Generated issues
    are appended to their area after its own issues, by severity then screen count. Screens data/ does
    not have are pruned, and a generated issue left without screens is dropped, each with a WARN line.
    """
    out = copy.deepcopy(areas)
    path = os.path.join(snapshot_dir, "issues.json")
    if not os.path.exists(path):
        warn(f"{snapshot_dir} has no issues.json; the a11y snapshot is ignored")
        return out
    with open(path, encoding="utf-8") as f:
        snapshot = json.load(f)
    known = {n["id"] for a in out for n in a.get("nodes", [])}
    raw = {i["id"]: i for a in out for i in a.get("issues", [])}

    for iid, ov in sorted((snapshot.get("overlays") or {}).items()):
        issue = raw.get(iid)
        if issue is None:
            warn(f"{iid}: the a11y snapshot overlays an issue that is not in data/; ignored")
            continue
        if ov.get("severity"):
            issue["severity"] = ov["severity"]
        if ov.get("evidenceAppend"):
            issue["evidence"] = f"{issue.get('evidence', '').rstrip()} {ov['evidenceAppend']}".strip()
        added = known_screens(iid, ov.get("addNodeIds"), known)
        if added:
            on_issue = set(issue["nodeIds"])
            for n in added:
                if n not in on_issue:
                    issue["nodeIds"].append(n)
                    on_issue.add(n)
        for k in OVERLAY_FIELDS:
            if ov.get(k) is not None:
                issue[k] = ov[k]

    by_area = {a["area"]: a for a in out}
    added = {}
    for g in snapshot.get("generated") or []:
        area = by_area.get(g.get("area"))
        if area is None:
            warn(f"{g.get('id')}: the a11y snapshot puts it in area {g.get('area')}, which is not in data/; dropped")
            continue
        issue = {k: v for k, v in g.items() if k != "area"}
        issue["nodeIds"] = known_screens(issue["id"], g.get("nodeIds"), known)
        if not issue["nodeIds"]:
            warn(f"{issue['id']}: no known screens left in the a11y snapshot; dropped")
            continue
        added.setdefault(area["area"], []).append(issue)
    for name, issues in added.items():
        by_area[name].setdefault("issues", []).extend(order_issues(issues))
    return out
