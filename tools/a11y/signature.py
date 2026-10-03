"""Shared-element signatures (KTD15). Finding keys and record signatures are computed only here.

Every value is normalized before it enters a key: a CSS-Module class `_<local>_<hash>_<line>`
becomes `<local>`, FontAwesome classes are dropped, React `:rN:` ids and `nth-child` positions are
removed, and names go through `lib.canon.name_template`. The module hash never enters a key; it
only finds the nearest ancestor from a different CSS Module.

Two key shapes (checks.json `families.<id>.key`):
- intrinsic: family, rule, sorted local classes, role, tag; contrast adds the rounded colors;
- content: the same, plus the name template and the call site (the nearest ancestor from a
  different CSS Module, or the landmark and `data-*` hooks for an element with no module classes).

A key reads `<family>:<rule>:<12 hex characters of sha(key parts)>`.

axe's incomplete results ("needs review") are keyed by rule and messageKey instead, product-wide
(KTD12): `incomplete_key` hashes family, rule, `basis: "incomplete"` and the messageKey, no element.
"""
import collections
import re

from lib.canon import name_template, sha

MODULE_CLASS = re.compile(r"^_([A-Za-z0-9-]+)_([A-Za-z0-9]{5})_([0-9]+)$")
FONTAWESOME = re.compile(r"^(?:fa|fa-.+|svg-inline--fa)$")
REACT_ID = re.compile(r":r[0-9a-z]+:|«r[0-9a-z]+»")
NTH = re.compile(r":nth-(?:last-)?(?:child|of-type)\([^)]*\)")
COLOR_STEP = 8  # channels are floored to a multiple of this, so rendering noise keeps one key
HEX = re.compile(r"^#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
RGB = re.compile(r"^rgba?\(\s*([0-9.]+)[\s,]+([0-9.]+)[\s,]+([0-9.]+)")
# The families a judged announced-as record can be flagged in (records.jsonl, judgments.jsonl).
RECORD_FAMILIES = ("name", "role-state", "structure")


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def strip_volatile(s):
    """Drop React ids and nth-child positions from a text value."""
    return NTH.sub("", REACT_ID.sub("", str(s or "")))


def template(s):
    """A name, heading or landmark name as it enters a key."""
    return name_template(strip_volatile(s))


def split_classes(classes):
    """(normalized class names, module hashes). FontAwesome is dropped; other classes stay as written."""
    names, hashes = set(), set()
    for c in classes or []:
        if FONTAWESOME.match(c):
            continue
        m = MODULE_CLASS.match(c)
        if m:
            names.add(m.group(1))
            hashes.add(m.group(2))
        else:
            names.add(c)
    return sorted(names), hashes


def local_classes(classes):
    return split_classes(classes)[0]


def module_hashes(classes):
    return split_classes(classes)[1]


def parse_color(c):
    """(r, g, b) from #rgb, #rrggbb (alpha ignored) or rgb()/rgba(); None when it is neither."""
    if not isinstance(c, str):
        return None
    c = c.strip()
    m = HEX.match(c)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(ch * 2 for ch in h[:3])
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    m = RGB.match(c)
    if m:
        return tuple(min(255, int(float(x) + 0.5)) for x in m.groups())
    return None


def hex_color(c):
    rgb = parse_color(c)
    return "#%02x%02x%02x" % rgb if rgb else None


def round_color(c):
    rgb = parse_color(c)
    return "#%02x%02x%02x" % tuple(v // COLOR_STEP * COLOR_STEP for v in rgb) if rgb else None


def contrast_colors(evidence):
    """(foreground, background) as axe measured them: fgColor/bgColor, or link-in-text-block's
    nodeColor (the link) and parentColor (the text around it)."""
    ev = evidence or {}
    return ev.get("fgColor") or ev.get("nodeColor"), ev.get("bgColor") or ev.get("parentColor")


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def call_site(occ):
    """Where a component is used: the nearest ancestor with a module hash the element does not have,
    by its local classes. An element with no module classes uses its landmark and data-* hooks."""
    o = occ or {}
    own = module_hashes(o.get("classes"))
    if own:
        for anc in o.get("ancestors") or []:
            if module_hashes(anc) - own:
                return {"ancestor": local_classes(anc)}
        return {"ancestor": []}
    lm = o.get("landmark") or {}
    return {"landmark": [lm.get("role") or "", template(lm.get("name"))],
            "data": sorted([k, strip_volatile(v)] for k, v in (o.get("data") or {}).items())}


def element_parts(occ):
    o = occ or {}
    return {"tag": (o.get("tag") or "").lower(), "role": o.get("role") or "",
            "classes": local_classes(o.get("classes")), "callSite": call_site(o)}


def key_parts(family, rule, shape, occ, evidence=None):
    """The values a finding key hashes. shape is checks.json's `key`: "intrinsic" or "content"."""
    el = element_parts(occ)
    parts = {"family": family, "rule": rule, "classes": el["classes"], "role": el["role"], "tag": el["tag"]}
    if family == "contrast":
        fg, bg = contrast_colors(evidence)
        parts["fg"], parts["bg"] = round_color(fg), round_color(bg)
    if shape == "content":
        parts["name"] = template((occ or {}).get("name"))
        parts["callSite"] = el["callSite"]
    return parts


def finding_key(family, rule, shape, occ, evidence=None):
    return f"{family}:{rule}:{sha(key_parts(family, rule, shape, occ, evidence))[:12]}"


def incomplete_key(family, rule, message_key):
    """The key of the one finding all of axe's incomplete results for a rule and messageKey share,
    whatever the element (KTD12). A null messageKey is a group of its own."""
    parts = {"family": family, "rule": rule, "basis": "incomplete", "messageKey": message_key}
    return f"{family}:{rule}:{sha(parts)[:12]}"


def record_sig(record):
    """An announced-as record's signature: the element (content shape, no family) and its template.
    `records prepare` deduplicates by it and judgments name it."""
    parts = {"element": element_parts(record.get("occurrence")), "template": strip_volatile(record.get("template"))}
    return "rec:" + sha(parts)[:12]


def record_finding_key(family, sig):
    """The key of the finding a flagged record signature becomes (rule `announced-as`)."""
    return f"{family}:announced-as:{sha({'family': family, 'rule': 'announced-as', 'sig': sig})[:12]}"


def hand_key(issue_id):
    return f"hand:{issue_id}"


def proposal_id(finding_key, hand_found_id):
    """The id of an attachment proposal, and of the registry attachment it becomes (KTD17)."""
    return sha([finding_key, hand_found_id])


# ---------------------------------------------------------------------------
# Group checks
# ---------------------------------------------------------------------------

def tag_attr_set(occ):
    """An element's tag-and-attribute set: tag, role, class attribute as written (FontAwesome
    dropped) and data-* attributes."""
    o = occ or {}
    classes = tuple(sorted({c for c in o.get("classes") or [] if not FONTAWESOME.match(c)}))
    data = tuple(sorted((k, strip_volatile(v)) for k, v in (o.get("data") or {}).items()))
    return ((o.get("tag") or "").lower(), o.get("role") or "", classes, data)


def agreement(occs):
    """The share of a group's members that have its most common tag-and-attribute set."""
    occs = list(occs)
    if not occs:
        return 1
    top = collections.Counter(tag_attr_set(o) for o in occs).most_common(1)[0][1]
    return round(top / len(occs), 4)


def class_set(occ):
    o = occ or {}
    out = set(local_classes(o.get("classes")))
    for anc in o.get("ancestors") or []:
        out.update(local_classes(anc))
    return out


def class_distance(a, b):
    """Jaccard distance between the local classes of two elements and their ancestors."""
    return set_distance(class_set(a), class_set(b))


def set_distance(x, y):
    """Jaccard distance between two class sets (class_set)."""
    if not x and not y:
        return 0
    return round(1 - len(x & y) / len(x | y), 4)
