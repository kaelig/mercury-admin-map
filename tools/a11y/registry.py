"""Issue ids for findings (KTD16): pure functions `publish` uses on published/registry.json.

Nothing here reads or writes a file. Every function takes a registry (schemas.md, registry.json)
and returns a new one; the input is never changed.

- An id is minted once, as `<area of the primary state>.i-a11y-<slug>`, in the [a-z0-9.-] alphabet
  that ticket URLs use. It never matches an id the registry has ever held, live or tombstoned, nor a
  hand-found id: a clash gets `-2`, `-3`, ...
- A key keeps its id for good, through tombstones and reappearances.
- Attaching a finding to a hand-found issue tombstones the finding's published id as attached;
  releasing it removes that tombstone.
"""
import collections
import copy
import re
import unicodedata

ID = re.compile(r"^[a-z0-9-]+\.i-a11y-[a-z0-9.-]+$")
REASONS = ("fixed", "withdrawn", "attached")
SLUG_MAX = 48


def empty():
    return {"keys": {}, "held": [], "tombstones": {}, "attachments": []}


def _copy(reg):
    out = empty()
    out.update(copy.deepcopy(reg or {}))
    return out


def _words(text):
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")


def slug(family, element):
    """`<family>-<element words>`, at most 48 characters, cut at a word boundary."""
    s = _words(f"{family} {element or ''}")
    if len(s) > SLUG_MAX:
        cut = s[:SLUG_MAX + 1]
        s = cut.rsplit("-", 1)[0] if "-" in cut else s[:SLUG_MAX]
    return s or "finding"


def mint(reg, key, area, family, element, hand_found_ids=()):
    """(registry, id): the key's existing id, or a new one that clashes with nothing ever held."""
    reg = _copy(reg)
    if key in reg["keys"]:
        return reg, reg["keys"][key]
    base = f"{_words(area) or 'map'}.i-a11y-{slug(family, element)}"
    taken = set(reg["held"]) | set(reg["tombstones"]) | set(reg["keys"].values()) | set(hand_found_ids)
    iid, n = base, 1
    while iid in taken:
        n += 1
        iid = f"{base}-{n}"
    reg["keys"][key] = iid
    reg["held"] = sorted(set(reg["held"]) | {iid})
    return reg, iid


def tombstone(reg, issue_id, reason, run, date, attached_to=None):
    """Mark an id fixed, withdrawn or attached. The key keeps the id."""
    reg = _copy(reg)
    reg["tombstones"][issue_id] = {"reason": reason, "attachedTo": attached_to, "run": run, "date": date}
    return reg


def untombstone(reg, issue_id):
    """Reappeared, or Withdrawn back to Persisting: the id is live again."""
    reg = _copy(reg)
    reg["tombstones"].pop(issue_id, None)
    return reg


def attach(reg, key, hand_id, run, date):
    """Record a confirmed attachment. A live published id for the key is tombstoned as attached."""
    reg = _copy(reg)
    if not any(a["finding"] == key and a["handFound"] == hand_id for a in reg["attachments"]):
        reg["attachments"].append({"finding": key, "handFound": hand_id, "run": run})
        reg["attachments"].sort(key=lambda a: (a["finding"], a["handFound"]))
    iid = reg["keys"].get(key)
    if iid and iid not in reg["tombstones"]:
        reg["tombstones"][iid] = {"reason": "attached", "attachedTo": hand_id, "run": run, "date": date}
    return reg


def release(reg, key, hand_id):
    """Undo an attachment. The attached tombstone goes too, unless another attachment still holds the id."""
    reg = _copy(reg)
    reg["attachments"] = [a for a in reg["attachments"] if not (a["finding"] == key and a["handFound"] == hand_id)]
    iid = reg["keys"].get(key)
    t = reg["tombstones"].get(iid)
    if t and t["reason"] == "attached" and t["attachedTo"] == hand_id:
        rest = [a["handFound"] for a in reg["attachments"] if a["finding"] == key]
        if rest:
            t["attachedTo"] = rest[0]
        else:
            del reg["tombstones"][iid]
    return reg


def violations(reg, hand_found_ids=()):
    """Every broken KTD16 invariant, as one line each. An empty list means the registry is sound."""
    reg = _copy(reg)
    out = []
    held = reg["held"]
    if held != sorted(set(held)):
        out.append("held is not sorted and unique")
    used = set(reg["keys"].values()) | set(reg["tombstones"])
    for iid in sorted(used | set(held)):
        if not ID.match(iid):
            out.append(f"{iid} is not an <area>.i-a11y-<slug> id in [a-z0-9.-]")
    for iid in sorted(used - set(held)):
        out.append(f"{iid} is not in held")
    live = collections.Counter(i for i in reg["keys"].values() if i not in reg["tombstones"])
    for iid, n in sorted(live.items()):
        if n > 1:
            out.append(f"{iid} has {n} live keys")
    for iid in sorted((used | set(held)) & set(hand_found_ids)):
        out.append(f"{iid} is a hand-found id")
    for iid, t in sorted(reg["tombstones"].items()):
        if t.get("reason") not in REASONS:
            out.append(f"tombstone {iid} has reason {t.get('reason')!r}")
        elif t["reason"] == "attached":
            keys = {k for k, v in reg["keys"].items() if v == iid}
            if not any(a["finding"] in keys and a["handFound"] == t.get("attachedTo") for a in reg["attachments"]):
                out.append(f"tombstone {iid} is attached to {t.get('attachedTo')} with no matching attachment")
        elif t.get("attachedTo"):
            out.append(f"tombstone {iid} is {t['reason']} but names attachedTo")
    return out
