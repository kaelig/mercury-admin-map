#!/usr/bin/env python3
"""Merge area manifests, encode screenshot levels of detail, prerender layouts, bundle the app, emit dist/.

Pipeline:
  data/*.json + screens/**/*.png
    -> dist/img/<area>/<name>.{mid,thumb,full}.webp   (400px q82, 800px q85, lossless 2880px)
    -> dist/img/atlas.webp                            (every screen as a 128x80 tile, for zoomed-out views)
    -> build/map.json -> node tools/prerender.mjs -> build/layout.json   (dagre runs here, not in the page)
    -> esbuild src/main.js -> build/app.js
    -> src/index.html + src/app.css + fonts -> dist/index.html
"""
import hashlib, json, os, shutil, subprocess, sys, struct
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import mapdata  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "tools", "a11y"))
import report  # noqa: E402
DIST = os.path.join(ROOT, "dist")
CACHE = os.path.join(DIST, "img")
ORDER = ["home", "accounts", "transactions", "cards", "payments", "billpay", "invoicing", "settings", "books", "extra"]
THUMB_W, THUMB_H = 800, 500
MID_W = 400
ATLAS_TW, ATLAS_TH, ATLAS_COLS = 128, 80, 32
BUILD = os.path.join(ROOT, "build")
FONTS_SRC = os.path.join(ROOT, "assets", "fonts")


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    w, h = struct.unpack(">II", head[16:24])
    return w, h


def webp(src, dst, args):
    if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    subprocess.run(["cwebp", "-quiet", *args, src, "-o", dst], check=True)


def convert(rel):
    src = os.path.join(ROOT, rel)
    w, h = png_size(src)
    base = os.path.join(CACHE, os.path.splitext(os.path.relpath(rel, "screens"))[0])
    crop_h = min(h, round(w * THUMB_H / THUMB_W))
    webp(src, base + ".thumb.webp", ["-q", "85", "-sharp_yuv", "-crop", "0", "0", str(w), str(crop_h), "-resize", str(THUMB_W), "0"])
    webp(src, base + ".mid.webp", ["-q", "82", "-sharp_yuv", "-crop", "0", "0", str(w), str(crop_h), "-resize", str(MID_W), "0"])
    webp(src, base + ".full.webp", ["-lossless", "-z", "9"])
    return rel, (w, h), base


def main():
    # data/*.json, merged with the published accessibility snapshot when there is one (KTD22):
    # A11Y_SNAPSHOT, else a11y/published/. The loader prints its own WARN lines.
    areas, problems = mapdata.load_areas(), []
    areas.sort(key=lambda a: ORDER.index(a["area"]) if a.get("area") in ORDER else 99)

    all_ids = {n["id"] for a in areas for n in a.get("nodes", [])}
    images = []
    for a in areas:
        kept = []
        for n in a.get("nodes", []):
            img = n.get("image")
            if not img or not os.path.exists(os.path.join(ROOT, img)):
                problems.append(f"{n['id']}: missing image {img}")
                continue
            kept.append(n)
            images.append(img)
        a["nodes"] = kept
    all_ids = {n["id"] for a in areas for n in a["nodes"]}
    for a in areas:
        edges = []
        for e in a.get("edges", []):
            if e.get("from") in all_ids and e.get("to") in all_ids and e["from"] != e["to"]:
                edges.append(e)
            else:
                problems.append(f"{a['area']}: dropped edge {e.get('from')} -> {e.get('to')}")
        a["edges"] = edges
        for j in a.get("journeys", []):
            before = len(j.get("steps", []))
            j["steps"] = [s for s in j.get("steps", []) if s in all_ids]
            if len(j["steps"]) != before:
                problems.append(f"{j['id']}: dropped {before - len(j['steps'])} unknown steps")
        for i in a.get("issues", []):
            i["nodeIds"] = [x for x in i.get("nodeIds", []) if x in all_ids]
        a.setdefault("blocked", [])

    with ThreadPoolExecutor(max_workers=8) as pool:
        converted = list(pool.map(convert, sorted(set(images))))
    meta = {rel: (size, base) for rel, size, base in converted}

    order = []
    for a in areas:
        for n in a["nodes"]:
            (w, h), base = meta[n["image"]]
            n["w"], n["h"] = w, h
            for lvl in ("mid", "thumb", "full"):
                n[lvl] = os.path.relpath(f"{base}.{lvl}.webp", DIST)
            if n["thumb"] not in order:
                order.append(n["thumb"])
            n["ai"] = order.index(n["thumb"])
    atlas = build_atlas(order)

    stats = {
        "screens": sum(len(a["nodes"]) for a in areas),
        "edges": sum(len(a["edges"]) for a in areas),
        "journeys": sum(len(a.get("journeys", [])) for a in areas),
        "issues": sum(len(a.get("issues", [])) for a in areas),
    }
    patterns = []
    if os.path.exists(os.path.join(ROOT, "patterns.json")):
        ids = {i["id"] for a in areas for i in a.get("issues", [])}
        for p in json.load(open(os.path.join(ROOT, "patterns.json"))):
            p["issueIds"] = [x for x in p["issueIds"] if x in ids]
            if p["issueIds"]:
                patterns.append(p)

    os.makedirs(BUILD, exist_ok=True)
    # The page gets each issue's viewer fields only; copy and audit stay in the snapshot for the export.
    shown = [dict(a, issues=[mapdata.viewer_issue(i) for i in a.get("issues", [])]) for a in areas]
    data = {"areas": shown, "stats": stats, "patterns": patterns, "atlas": atlas}
    with open(os.path.join(BUILD, "map.json"), "w") as f:
        json.dump(data, f)
    subprocess.run(["node", "tools/prerender.mjs", "build/map.json", "build/layout.json"], cwd=ROOT, check=True)
    subprocess.run(["npx", "--no-install", "esbuild", "src/main.js", "--bundle", "--minify", "--format=iife", "--target=chrome110,safari16,firefox115",
                    "--outfile=build/app.js", "--log-level=warning"], cwd=ROOT, check=True)

    fonts_css = copy_fonts()
    inline = lambda s: s.replace("</", "<\\/")
    page = open(os.path.join(ROOT, "src", "index.html")).read()
    parts = {
        "<!--FONTS-->": fonts_css,
        "<!--STYLE-->": open(os.path.join(ROOT, "src", "app.css")).read(),
        "<!--MAP_DATA-->": f'<script id="map-data" type="application/json">{inline(json.dumps(data))}</script>',
        "<!--MAP_LAYOUT-->": f'<script id="map-layout" type="application/json">{inline(open(os.path.join(BUILD, "layout.json")).read())}</script>',
        "<!--APP-->": inline(open(os.path.join(BUILD, "app.js")).read()),
    }
    for k, v in parts.items():
        page = page.replace(k, v, 1)
    with open(os.path.join(DIST, "index.html"), "w") as f:
        f.write(page)
    write_host_files()
    for stale in [f for f in os.listdir(DIST) if f.endswith(".json")]:
        os.remove(os.path.join(DIST, stale))
    used = {os.path.join(DIST, n[k]) for a in areas for n in a["nodes"] for k in ("mid", "thumb", "full")} | {os.path.join(DIST, atlas["src"])}
    for dp, _, fs in os.walk(CACHE):
        for f in fs:
            if os.path.join(dp, f) not in used:
                os.remove(os.path.join(dp, f))

    # Agent-readable export (dist/agent/*, dist/llms.txt). Set MAP_BASE_URL to the public site URL
    # so links in ticket bodies stay absolute once pasted into a tracker.
    export = [sys.executable, "tools/export_agent_data.py", "--out", os.path.join(DIST, "agent")]
    if os.environ.get("MAP_BASE_URL"):
        export += ["--base-url", os.environ["MAP_BASE_URL"]]
    subprocess.run(export, cwd=ROOT, check=True)
    for stray in [f for f in os.listdir(DIST) if f.endswith(".html") and f != "index.html"]:
        os.remove(os.path.join(DIST, stray))
    # The standalone accessibility report (KTD24): dist/a11y/index.html, or no dist/a11y/ without a snapshot.
    report.render(mapdata.snapshot_path(), DIST)

    total = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(DIST) for f in fs)
    print(json.dumps(stats), f"dist={total/1e6:.1f}MB")
    for p in problems:
        print("WARN", p, file=sys.stderr)


def write_host_files():
    """Static-host files: keep the site out of search engines, cache fonts and images."""
    with open(os.path.join(DIST, "robots.txt"), "w") as f:
        f.write("User-agent: *\nDisallow: /\n")
    with open(os.path.join(DIST, "_headers"), "w") as f:
        f.write("/*\n  X-Robots-Tag: noindex, nofollow, noarchive\n"
                "/img/*\n  Cache-Control: public, max-age=86400\n"
                "/*.jsonl\n  Content-Type: application/x-ndjson; charset=utf-8\n"
                "/*.md\n  Content-Type: text/markdown; charset=utf-8\n"
                "/llms.txt\n  Content-Type: text/plain; charset=utf-8\n")


def build_atlas(thumbs):
    """Pack every screen into one sprite sheet so a fully zoomed-out map is a single texture."""
    from PIL import Image
    key = hashlib.sha1(json.dumps([(t, os.path.getmtime(os.path.join(DIST, t))) for t in thumbs]).encode()).hexdigest()[:12]
    rel = f"img/atlas-{key}.webp"
    dst = os.path.join(DIST, rel)
    rows = (len(thumbs) + ATLAS_COLS - 1) // ATLAS_COLS
    if not os.path.exists(dst):
        sheet = Image.new("RGB", (ATLAS_COLS * ATLAS_TW, rows * ATLAS_TH))
        for i, t in enumerate(thumbs):
            with Image.open(os.path.join(DIST, t)) as im:
                sheet.paste(im.convert("RGB").resize((ATLAS_TW, ATLAS_TH), Image.LANCZOS), ((i % ATLAS_COLS) * ATLAS_TW, (i // ATLAS_COLS) * ATLAS_TH))
        sheet.save(dst, "WEBP", quality=88, method=6)
    return {"src": rel, "cols": ATLAS_COLS, "tw": ATLAS_TW, "th": ATLAS_TH}


def copy_fonts():
    """Inline the webfonts as data: URIs. file:// pages cannot load fonts at all (font requests
    are CORS-mode and a file:// page has a null origin), and inlining saves two requests online."""
    import base64
    theme = os.path.join(ROOT, "assets", "mercury-theme.json")
    if not os.path.exists(theme):
        return ""
    rules = []
    for f in json.load(open(theme)).get("fonts", []):
        src = os.path.join(ROOT, "assets", f["file"])
        if not os.path.exists(src):
            continue
        fmt = "woff2" if src.endswith(".woff2") else "woff"
        data = base64.b64encode(open(src, "rb").read()).decode()
        rules.append(f'@font-face {{ font-family: "{f["family"]}"; src: url(data:font/{fmt};base64,{data}) format("{fmt}"); font-weight: {f.get("weight", 400)}; font-style: {f.get("style", "normal")}; font-display: swap; }}')
    shutil.rmtree(os.path.join(DIST, "fonts"), ignore_errors=True)
    return "<style>\n" + "\n".join(rules) + "\n</style>"


if __name__ == "__main__":
    main()
