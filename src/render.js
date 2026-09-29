// Canvas renderer for the map. One viewport-sized, opaque canvas redrawn on demand: GPU memory is
// bounded by the viewport, not by the size of the world, so nothing can drop out when zooming.
import { CARD_W, CARD_H, SHOT_H, LABEL_W } from "./layout.js";

const TAU = Math.PI * 2;
export const AREA_COLORS = { home: "#5266EB", accounts: "#1F8A5B", transactions: "#B9502E", cards: "#8C4FD1", payments: "#0C7FA8", billpay: "#B07A12", invoicing: "#C23D7A", settings: "#56627A", books: "#2F7D8C", extra: "#6D7B2F" };
const alpha = (hex, a) => hex + Math.round(a * 255).toString(16).padStart(2, "0");

export const FLAGS = { edges: true, labels: true, images: true, text: true, shadow: true, regions: true, lanes: true, cache: true };
// Zoomed far out, the whole map is static between focus changes: paint it once per scale into a
// bitmap and blit that, instead of re-issuing ~900 image draws per frame. Scales are device px per
// world unit; a layer is only ever scaled down, never up, and never exceeds 4096px on a side.
const LAYER_SCALES = [0.05, 0.1, 0.18], LAYER_MAX = 4096;
export class Renderer {
  constructor(canvas, images, atlas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d", { alpha: false });
    this.images = images;
    this.atlas = atlas; // {img, cols, tw, th}
    this.W = 1; this.H = 1; this.dpr = 1;
    this.fitCache = new Map();
    this.wrapCache = new Map();
    this.version = 0; this.layers = new Map();
  }
  bump() { this.version++; }
  resize(w, h, dpr) {
    this.W = w; this.H = h; this.dpr = dpr;
    this.canvas.width = Math.max(1, Math.round(w * dpr));
    this.canvas.height = Math.max(1, Math.round(h * dpr));
  }
  setTheme(css) {
    const v = (n) => css.getPropertyValue(n).trim();
    this.C = { ground: v("--ground"), card: v("--card"), line: v("--line"), edge: v("--edge"), ink: v("--ink"), muted: v("--muted"), faint: v("--faint"),
      accent: v("--accent"), accentInk: v("--accent-ink"), primary: v("--primary"), primaryInk: v("--primary-ink"), high: v("--high"), highInk: v("--high-ink"), cardBorder: v("--card-border"), text: v("--text"), panel2: v("--panel-2"), tag: v("--tag-bg"), tagInk: v("--tag-ink"), shadow: v("--card-shadow") };
    this.F = { ui: v("--ui"), display: v("--display"), mono: v("--mono") };
    this.fitCache.clear(); this.wrapCache.clear(); this.version++;
  }
  setScene(scene) { this.scene = scene; this.fitCache.clear(); this.wrapCache.clear(); this.version++; }

  // style(e) -> 0 normal, 1 hot, 2 dim. Rebuilt only when focus or layout changes.
  setEdgeStyles(style) {
    const groups = new Map();
    for (const e of this.scene.edges) {
      const s = style(e), key = (e.cross ? "x" : "") + e.area + ":" + s;
      let g = groups.get(key);
      if (!g) groups.set(key, g = { s, cross: !!e.cross, stroke: new Path2D(), arrows: new Path2D(), bb: [Infinity, Infinity, -Infinity, -Infinity], labels: [], edges: [] });
      g.stroke.addPath(e.path);
      const L = s === 1 ? 20 : 15, W = s === 1 ? 8 : 6, [x, y] = e.tip, a = e.ang;
      const arrow = new Path2D();
      arrow.moveTo(x, y);
      arrow.lineTo(x - L * Math.cos(a) + W * Math.sin(a), y - L * Math.sin(a) - W * Math.cos(a));
      arrow.lineTo(x - L * Math.cos(a) - W * Math.sin(a), y - L * Math.sin(a) + W * Math.cos(a));
      arrow.closePath();
      e.arrow = arrow; g.arrows.addPath(arrow); g.edges.push(e);
      g.bb[0] = Math.min(g.bb[0], e.bb[0]); g.bb[1] = Math.min(g.bb[1], e.bb[1]); g.bb[2] = Math.max(g.bb[2], e.bb[2]); g.bb[3] = Math.max(g.bb[3], e.bb[3]);
      if (e.label) g.labels.push(e);
    }
    this.version++;
    // draw order: dim, normal, hot
    this.edgeGroups = [...groups.values()].sort((a, b) => (a.s === 2 ? -1 : a.s) - (b.s === 2 ? -1 : b.s));
  }

  // S: {T, cardOn(c), laneOn(l), hover, sel:Set(ids), gSteps:Map(id->n), moving}
  draw(S) {
    const { ctx, C, dpr } = this, T = S.T, k = T.k, sc = this.scene;
    const vx0 = -T.x / k, vy0 = -T.y / k, vx1 = (this.W - T.x) / k, vy1 = (this.H - T.y) / k;
    const inView = (x0, y0, x1, y1) => x1 > vx0 && x0 < vx1 && y1 > vy0 && y0 < vy1;
    const px = 1 / k;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalAlpha = 1;
    ctx.fillStyle = C.ground;
    ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
    ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * T.x, dpr * T.y);
    const layer = FLAGS.cache && this.atlas.tiles ? this.layerFor(k * dpr, S) : null;
    let visible;
    if (layer) {
      ctx.drawImage(layer.bmp, layer.x, layer.y, layer.w, layer.h);
      visible = sc.cards.filter((c) => S.sel.has(c.id) || S.hover === c);
      this.outlines(visible, S, px);
    } else visible = this.paint(S, k, [vx0, vy0, vx1, vy1], true);

    // Region titles last so they sit above everything. They are drawn in screen space at a
    // size rounded to 0.5px: a world-space font scaled by the zoom is a new glyph size every
    // frame, which misses Skia's glyph cache and re-rasterises the text on each zoom step.
    ctx.globalAlpha = 1; ctx.textBaseline = "alphabetic";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    for (const r of sc.regions) {
      const fs = Math.round(Math.max(64 * k, Math.min(24, r.fit * k)) * 2) / 2; // CSS px
      const x = r.x * k + T.x + 28 * k, y = r.y * k + T.y;
      if (x > this.W || x + r.w * k < 0 || y < 0 || y - fs * 1.9 > this.H) continue;
      ctx.fillStyle = C.ink; ctx.font = `460 ${fs}px ${this.F.display}`; ctx.letterSpacing = `${Math.round(-0.01 * fs * 2) / 2}px`;
      ctx.fillText(r.title, x, y - fs * 0.78);
      ctx.letterSpacing = "0px";
      ctx.fillStyle = C.muted; ctx.font = `400 ${Math.round(fs * 0.8) / 2}px ${this.F.ui}`;
      ctx.fillText(r.sub, x, y - fs * 0.26);
    }
    this.images.pump();
    return { layer: !!layer };
  }

  layerFor(kd, S) {
    const b = this.scene.bounds, cap = LAYER_MAX / Math.max(b.w, b.h);
    const scale = LAYER_SCALES.map((s) => Math.min(s, cap)).find((s) => kd <= s);
    if (!scale) return null;
    let L = this.layers.get(scale);
    if (L && L.version === this.version) return L;
    L?.bmp.close();
    const w = Math.ceil(b.w * scale), h = Math.ceil(b.h * scale), cv = new OffscreenCanvas(w, h), lc = cv.getContext("2d", { alpha: false });
    lc.fillStyle = this.C.ground; lc.fillRect(0, 0, w, h);
    lc.setTransform(scale, 0, 0, scale, -b.x * scale, -b.y * scale);
    const main = this.ctx; this.ctx = lc;
    try { this.paint({ ...S, hover: null, sel: new Set() }, scale / this.dpr, [b.x, b.y, b.x + b.w, b.y + b.h], false); } finally { this.ctx = main; }
    L = { bmp: cv.transferToImageBitmap(), x: b.x, y: b.y, w: w / scale, h: h / scale, version: this.version };
    this.layers.set(scale, L);
    return L;
  }

  outlines(list, S, px) {
    const { ctx, C } = this;
    ctx.globalAlpha = 1;
    for (const c of list) {
      const sel = S.sel.has(c.id), hov = S.hover === c;
      if (!sel && !hov) continue;
      const w = sel ? Math.max(4, 2 * px) : Math.max(3, 1.5 * px);
      ctx.lineWidth = w; ctx.strokeStyle = sel ? C.accent : alpha6(C.accent);
      ctx.beginPath(); ctx.roundRect(c.x - w / 2, c.y - w / 2, CARD_W + w, CARD_H + w, 12 + w / 2); ctx.stroke();
    }
  }

  // Everything below the region titles, for the viewport rect v (world units) at zoom k.
  paint(S, k, v, live) {
    const { ctx, C, dpr } = this, sc = this.scene;
    const [vx0, vy0, vx1, vy1] = v;
    const inView = (x0, y0, x1, y1) => x1 > vx0 && x0 < vx1 && y1 > vy0 && y0 < vy1;
    const px = 1 / k; // one CSS pixel in world units

    // regions
    if (FLAGS.regions) for (const r of sc.regions) {
      if (!inView(r.x, r.y - 2000, r.x + r.w, r.y + r.h)) continue;
      const rc = AREA_COLORS[r.area] || "#888888";
      ctx.beginPath(); ctx.roundRect(r.x, r.y, r.w, r.h, 18);
      ctx.fillStyle = alpha(rc, 0.06); ctx.fill();
      ctx.lineWidth = Math.max(1.5, px); ctx.strokeStyle = alpha(rc, 0.4); ctx.stroke();
    }

    // edges (batched per area and style; a batch is skipped when its bbox is off screen)
    const showArrows = k > 0.06, dimA = 0.14;
    ctx.lineCap = "round";
    // Zoomed out: one stroke per batch. Zoomed in: most of each batch is off screen, so cull edge by edge.
    // Cross-area links are solid and faint: Skia dashes the whole path, off-screen length included.
    const perEdge = k >= 0.2;
    if (FLAGS.edges) for (const g of this.edgeGroups) {
      if (!inView(...g.bb)) continue;
      ctx.globalAlpha = g.s === 2 ? dimA * 0.75 : g.s === 1 ? 1 : g.cross ? 0.28 : 0.75;
      ctx.strokeStyle = ctx.fillStyle = g.s === 1 ? C.accent : C.edge;
      ctx.lineWidth = Math.max(g.s === 1 ? 4 : g.cross ? 1.5 : 2, px * (g.s === 1 ? 2 : 1));
      if (!perEdge) { ctx.stroke(g.stroke); if (showArrows) ctx.fill(g.arrows); continue; }
      for (const e of g.edges) {
        if (!inView(e.bb[0] - 20, e.bb[1] - 20, e.bb[2] + 20, e.bb[3] + 20)) continue;
        ctx.stroke(e.path); ctx.fill(e.arrow);
      }
    }

    // edge labels
    if (k >= 0.28 && FLAGS.labels) {
      ctx.font = `400 13px ${this.F.ui}`; ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
      ctx.lineJoin = "round"; ctx.lineWidth = 5; ctx.strokeStyle = C.ground;
      for (const g of this.edgeGroups) {
        if (!g.labels.length || !inView(...g.bb)) continue;
        ctx.globalAlpha = g.s === 2 ? dimA : 1;
        ctx.fillStyle = g.s === 1 ? C.accent : C.muted;
        if (g.s === 1) ctx.font = `500 13px ${this.F.ui}`;
        for (const e of g.labels) {
          const lines = e.lines, y0 = e.ly - (lines.length - 1) * 16;
          if (!inView(e.lx - 110, y0 - 14, e.lx + 110, y0 + lines.length * 16)) continue;
          for (let i = 0; i < lines.length; i++) { ctx.strokeText(lines[i], e.lx, y0 + i * 16); ctx.fillText(lines[i], e.lx, y0 + i * 16); }
        }
        if (g.s === 1) ctx.font = `400 13px ${this.F.ui}`;
      }
      ctx.textAlign = "left";
    }

    // lane labels
    ctx.globalAlpha = 1;
    if (k * 16 >= 3 && FLAGS.lanes) {
      for (const l of sc.labels) {
        if (!inView(l.x, l.y, l.x + LABEL_W, l.y + CARD_H)) continue;
        ctx.globalAlpha = S.laneOn(l) ? 1 : dimA;
        this.laneLabel(l, k);
      }
      ctx.globalAlpha = 1;
    }

    // cards: collect visible ones, then draw in passes so fonts/styles switch a handful of times per frame
    const on = [], dim = [];
    for (const c of sc.cards) {
      if (c.x + CARD_W < vx0 || c.x > vx1 || c.y + CARD_H < vy0 || c.y > vy1) continue;
      (S.cardOn(c) ? on : dim).push(c);
    }
    const devW = CARD_W * k * dpr;
    const level = devW <= 190 ? "atlas" : devW <= 520 ? "mid" : devW <= 1150 || S.moving ? "thumb" : "full";
    const cx = (vx0 + vx1) / 2, cy = (vy0 + vy1) / 2;
    if (dim.length) this.cards(dim, dimA, k, level, cx, cy, S);
    if (on.length) this.cards(on, 1, k, level, cx, cy, S);
    // Selection rings go under the step and issue badges so the numbers stay readable.
    if (live && (S.sel.size || S.hover)) this.outlines(on.concat(dim), S, px);
    if (dim.length) this.badges(dim, dimA, k, S);
    if (on.length) this.badges(on, 1, k, S);
    ctx.globalAlpha = 1;
    return on.concat(dim);
  }

  cards(list, a, k, level, cx, cy, S) {
    const { ctx, C, dpr } = this, px = 1 / k, screenW = CARD_W * k;
    ctx.globalAlpha = a;
    // 1. card bodies
    const body = new Path2D();
    const round = screenW > 40;
    for (const c of list) round ? body.roundRect(c.x, c.y, CARD_W, CARD_H, 12) : body.rect(c.x, c.y, CARD_W, CARD_H);
    if (screenW > 60 && a === 1 && FLAGS.shadow) { ctx.save(); ctx.translate(0, 3); ctx.fillStyle = C.shadow; ctx.fill(body); ctx.restore(); }
    ctx.fillStyle = C.card; ctx.fill(body);
    if (screenW > 30) { ctx.lineWidth = Math.max(1, px); ctx.strokeStyle = C.cardBorder; ctx.stroke(body); }

    // 2. screenshots, best available level of detail; fall back to the atlas tile while loading
    const A = this.atlas, clip = screenW > 150;
    if (FLAGS.images) for (const c of list) {
      let img = null;
      if (level !== "atlas") {
        const want = c.n[level];
        img = this.images.get(want);
        if (!img) {
          const prio = Math.hypot(c.x + CARD_W / 2 - cx, c.y + CARD_H / 2 - cy);
          this.images.want(want, level, prio);
          img = level === "full" ? this.images.get(c.n.thumb) || this.images.get(c.n.mid) : level === "thumb" ? this.images.get(c.n.mid) : null;
        }
      }
      if (clip) { ctx.save(); ctx.beginPath(); ctx.roundRect(c.x + 1, c.y + 1, CARD_W - 2, SHOT_H, [11, 11, 0, 0]); ctx.clip(); }
      if (img) ctx.drawImage(img, c.x + 1, c.y + 1, CARD_W - 2, SHOT_H);
      else if (A.tiles) ctx.drawImage(A.tiles[c.n.ai], c.x + 1, c.y + 1, CARD_W - 2, SHOT_H);
      else { ctx.fillStyle = C.panel2; ctx.fillRect(c.x + 1, c.y + 1, CARD_W - 2, SHOT_H); }
      if (clip) ctx.restore();
    }

    // 3. captions: real text when legible, placeholder bars otherwise
    if (!FLAGS.text) {} else if (k * 15 >= 4.5) {
      ctx.textBaseline = "alphabetic"; ctx.fillStyle = C.ink; ctx.font = `500 15px ${this.F.ui}`;
      for (const c of list) ctx.fillText(this.fit(c.n.title, 336, ctx.font), c.x + 12, c.y + SHOT_H + 26);
      ctx.fillStyle = C.faint; ctx.font = `11.5px ${this.F.mono}`;
      for (const c of list) ctx.fillText(this.fit(c.n.url || "", 336, ctx.font), c.x + 12, c.y + SHOT_H + 46);
    } else if (screenW > 24) {
      const bars = new Path2D();
      for (const c of list) { bars.rect(c.x + 12, c.y + SHOT_H + 14, Math.min(300, 40 + c.n.title.length * 7), 14); }
      ctx.fillStyle = C.cardBorder; ctx.fill(bars);
    }

    // 4. kind tags
    if (screenW > 110) {
      ctx.font = `500 11px ${this.F.ui}`; ctx.letterSpacing = "0.66px";
      const tags = new Path2D();
      for (const c of list) { c._kw = this.measure(c.n.kindU, ctx.font) + 14; tags.roundRect(c.x + 8, c.y + 8, c._kw, 21, 6); }
      ctx.fillStyle = C.tag; ctx.fill(tags);
      ctx.fillStyle = C.tagInk;
      for (const c of list) ctx.fillText(c.n.kindU, c.x + 15, c.y + 23);
      ctx.letterSpacing = "0px";
    }

  }

  // Issue badges and journey step numbers: dots at low zoom, numbers once legible.
  badges(list, a, k, S) {
    const { ctx, C } = this, px = 1 / k, screenW = CARD_W * k;
    ctx.globalAlpha = a;
    if (screenW > 40) { // below that the dots are larger than the cards and read as noise
      const ring = Math.max(3, px), numbers = screenW > 70;
      const badges = [], steps = [];
      for (const c of list) {
        if (c.iss) badges.push(c);
        const st = c.step || S.gSteps.get(c.id);
        if (st) steps.push([c, st]);
      }
      const dot = (x, y, r, fill) => { ctx.beginPath(); ctx.arc(x, y, r + ring, 0, TAU); ctx.fillStyle = C.ground; ctx.fill(); ctx.beginPath(); ctx.arc(x, y, r, 0, TAU); ctx.fillStyle = fill; ctx.fill(); };
      const rB = Math.max(13, 3.5 * px), rS = Math.max(15, 4 * px);
      for (const c of badges) dot(c.x + CARD_W - 3, c.y + 3, rB, C.high);
      for (const [c] of steps) dot(c.x + 3, c.y + 3, rS, C.primary);
      if (numbers) {
        ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.font = `500 13px ${this.F.ui}`; ctx.fillStyle = C.highInk;
        for (const c of badges) ctx.fillText(String(c.iss), c.x + CARD_W - 3, c.y + 4);
        ctx.font = `500 14px ${this.F.ui}`; ctx.fillStyle = C.primaryInk;
        for (const [c, st] of steps) ctx.fillText(String(st), c.x + 3, c.y + 4);
        ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
      }
    }
  }

  laneLabel(l, k) {
    const { ctx, C } = this;
    const legible = k * 26 >= 5;
    let y = l.y + 10;
    const title = l.others ? "Other screens" : l.j.name;
    const desc = l.others ? `${l.others} states reached outside the journeys above` : l.j.description || "";
    ctx.textBaseline = "top";
    ctx.font = `460 26px ${this.F.display}`;
    const tl = this.wrap(title, 300, ctx.font);
    if (!legible) {
      const bars = new Path2D();
      tl.forEach((_, i) => bars.rect(l.x, y + i * 30 + 3, 280, 22)); y += tl.length * 30 + 6;
      ctx.fillStyle = C.cardBorder; ctx.fill(bars); return;
    }
    ctx.fillStyle = l.others ? C.muted : C.ink;
    for (const s of tl) { ctx.fillText(s, l.x, y); y += 30; }
    y += 6;
    ctx.font = `400 16px ${this.F.ui}`; ctx.fillStyle = C.muted;
    for (const s of this.wrap(desc, 300, ctx.font)) { ctx.fillText(s, l.x, y); y += 23; }
    if (!l.others) { y += 6; ctx.font = `400 13px ${this.F.mono}`; ctx.fillStyle = C.faint; ctx.fillText(`${l.j.steps.length} steps`, l.x, y); }
    ctx.textBaseline = "alphabetic";
  }

  measure(s, font) {
    const key = font + "\u0000" + s;
    let w = this.fitCache.get(key);
    if (w === undefined) { this.ctx.font = font; w = this.ctx.measureText(s).width; this.fitCache.set(key, w); }
    return w;
  }
  fit(s, max, font) {
    const key = "fit" + max + font + "\u0000" + s;
    let r = this.fitCache.get(key);
    if (r !== undefined) return r;
    r = s;
    if (this.ctx.measureText(s).width > max) {
      let lo = 0, hi = s.length;
      while (lo < hi) { const m = (lo + hi + 1) >> 1; if (this.ctx.measureText(s.slice(0, m) + "…").width <= max) lo = m; else hi = m - 1; }
      r = s.slice(0, lo).trimEnd() + "…";
    }
    this.fitCache.set(key, r);
    return r;
  }
  wrap(s, max, font) {
    const key = max + font + "\u0000" + s;
    let out = this.wrapCache.get(key);
    if (out) return out;
    out = []; let line = "";
    for (const w of s.split(/\s+/)) {
      const t = line ? line + " " + w : w;
      if (line && this.ctx.measureText(t).width > max) { out.push(line); line = w; } else line = t;
    }
    if (line) out.push(line);
    this.wrapCache.set(key, out);
    return out;
  }
}
function alpha6(c) { return c.startsWith("#") && c.length === 7 ? c + "99" : c; }

// Turn a prerendered layout into draw-ready objects.
export function buildScene(L, M) {
  const regions = L.regions.map((r) => {
    const a = M.areaByKey.get(r.area);
    return { ...r, title: a.title, sub: `${a.nodes.length} screens · ${(a.journeys || []).length} journeys`, fit: (r.w * 0.95) / (a.title.length * 0.56) };
  });
  const cards = [];
  for (const c of L.cards) {
    const n = M.nodes.get(c.id); if (!n) continue;
    cards.push({ ...c, n, iss: M.issuesByNode.get(c.id)?.length || 0 });
  }
  const labels = L.labels.map((l) => ({ ...l, j: l.lane ? M.journeyById.get(l.lane) : null }));
  const edges = L.edges.map((e) => {
    const p = new Path2D();
    for (const s of e.seg) {
      if (s[0] === "M") p.moveTo(s[1], s[2]);
      else if (s[0] === "L") p.lineTo(s[1], s[2]);
      else if (s[0] === "Q") p.quadraticCurveTo(s[1], s[2], s[3], s[4]);
      else p.bezierCurveTo(s[1], s[2], s[3], s[4], s[5], s[6]);
    }
    const last = e.seg[e.seg.length - 1];
    return { ...e, path: p, tip: [last[last.length - 2], last[last.length - 1]], lines: e.label ? wrapChars(e.label, 24) : null };
  });
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const r of regions) { x0 = Math.min(x0, r.x); y0 = Math.min(y0, r.y - 600); x1 = Math.max(x1, r.x + r.w); y1 = Math.max(y1, r.y + r.h); }
  for (const c of cards) { x0 = Math.min(x0, c.x); y0 = Math.min(y0, c.y); x1 = Math.max(x1, c.x + CARD_W); y1 = Math.max(y1, c.y + CARD_H); }
  const bounds = { x: x0 - 200, y: y0 - 200, w: x1 - x0 + 400, h: y1 - y0 + 400 };
  return { regions, cards, labels, edges, bounds };
}
function wrapChars(s, n) { const out = []; let line = ""; for (const w of s.split(" ")) { if ((line + " " + w).trim().length > n && line) { out.push(line); line = w; } else line = (line + " " + w).trim(); } if (line) out.push(line); return out.slice(0, 3); }
