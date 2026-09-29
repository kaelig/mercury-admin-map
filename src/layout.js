// Pure layout code, shared by the build (tools/prerender.mjs) and nothing at runtime:
// the browser receives finished coordinates and never runs dagre.
export const CARD_W = 360, CARD_H = 290, SHOT_H = 225, GAP_X = 230, LANE_GAP = 120, LABEL_W = 360;

// Derive journeys/edges/issues the way the viewer uses them, including the implicit sidebar links.
export function prepare(MAP) {
  const nodes = new Map(), areaOf = new Map();
  for (const a of MAP.areas) for (const n of a.nodes) { nodes.set(n.id, n); areaOf.set(n.id, a.area); }
  const journeys = MAP.areas.flatMap((a) => (a.journeys || []).filter((j) => j.steps.length).map((j) => ({ ...j, area: a.area })));
  const edges = MAP.areas.flatMap((a) => a.edges.map((e) => ({ ...e, area: a.area })));
  for (const a of MAP.areas) if (a.area !== "home" && nodes.has("home.index") && nodes.has(a.area + ".index") && !edges.some((e) => e.from === "home.index" && e.to === a.area + ".index"))
    edges.push({ from: "home.index", to: a.area + ".index", label: "Sidebar: " + a.title.split(/[,&]/)[0].trim(), area: "home", nav: true });
  return { nodes, areaOf, journeys, edges };
}

export function layoutJourneys(MAP, P) {
  const blocks = [];
  for (const a of MAP.areas) {
    const b = { a, cards: [], labels: [], edges: [] }, used = new Set();
    let maxX = 40 + LABEL_W + 4 * (CARD_W + GAP_X), y = 60;
    for (const j of P.journeys.filter((j) => j.area === a.area)) {
      b.labels.push({ x: 40, y, lane: j.id });
      j.steps.forEach((id, i) => {
        const x = 40 + LABEL_W + i * (CARD_W + GAP_X), key = `${j.id}#${i}`;
        b.cards.push({ id, key, x, y, lane: j.id, step: i + 1 });
        used.add(id);
        if (i > 0) {
          const e = P.edges.find((e) => e.from === j.steps[i - 1] && e.to === id);
          b.edges.push({ from: `${j.id}#${i - 1}`, to: key, label: e ? e.label : "", lane: j.id, straight: true });
        }
        maxX = Math.max(maxX, x + CARD_W);
      });
      y += CARD_H + LANE_GAP;
    }
    const rest = a.nodes.filter((n) => !used.has(n.id));
    if (rest.length) {
      b.labels.push({ x: 40, y, others: rest.length });
      const per = Math.max(4, Math.floor((maxX - 40 - LABEL_W + 60) / (CARD_W + 60)));
      rest.forEach((n, i) => {
        const x = 40 + LABEL_W + (i % per) * (CARD_W + 60), yy = y + Math.floor(i / per) * (CARD_H + 70);
        b.cards.push({ id: n.id, key: n.id + "#o", x, y: yy });
        maxX = Math.max(maxX, x + CARD_W);
      });
      y += Math.ceil(rest.length / per) * (CARD_H + 70);
    }
    b.w = maxX + 60; b.h = y + 20;
    blocks.push(b);
  }
  return finish(pack(blocks));
}

export function layoutGraph(MAP, P, dagre) {
  const blocks = MAP.areas.map((a) => {
    const g = new dagre.graphlib.Graph({ multigraph: true });
    g.setGraph({ rankdir: "LR", nodesep: 70, ranksep: 170, marginx: 60, marginy: 70 });
    g.setDefaultEdgeLabel(() => ({}));
    for (const n of a.nodes) g.setNode(n.id, { width: CARD_W, height: CARD_H });
    P.edges.filter((e) => e.area === a.area && P.areaOf.get(e.to) === a.area && P.areaOf.get(e.from) === a.area)
      .forEach((e, i) => g.setEdge(e.from, e.to, { label: e.label, width: Math.min(220, 7 * (e.label || "").length), height: 20, labelpos: "c" }, "e" + i));
    dagre.layout(g);
    const b = { a, cards: [], labels: [], edges: [], w: g.graph().width, h: g.graph().height };
    for (const id of g.nodes()) { const n = g.node(id); b.cards.push({ id, key: id, x: Math.round(n.x - CARD_W / 2), y: Math.round(n.y - CARD_H / 2) }); }
    for (const eo of g.edges()) { const ed = g.edge(eo); b.edges.push({ from: eo.v, to: eo.w, label: ed.label, points: ed.points.map((p) => ({ x: Math.round(p.x), y: Math.round(p.y) })), lx: ed.x, ly: ed.y }); }
    return b;
  });
  const out = pack(blocks);
  for (const e of P.edges) if (P.areaOf.get(e.from) !== P.areaOf.get(e.to)) out.edges.push({ from: e.from, to: e.to, label: e.label, cross: true });
  return finish(out);
}

// Shelf-pack area blocks (local coords) into rows so the whole map reads as a landscape.
function pack(blocks) {
  const out = { cards: [], labels: [], regions: [], edges: [] };
  const total = blocks.reduce((s, b) => s + b.w * (b.h + 320), 0);
  const MAXW = Math.max(Math.max(...blocks.map((b) => b.w)), Math.sqrt(total * 1.7));
  let x = 0, y = 0, rowH = 0;
  for (const b of blocks) {
    if (x > 0 && x + b.w > MAXW) { x = 0; y += rowH + 900; rowH = 0; }
    const ox = x, oy = y; x += b.w + 500; rowH = Math.max(rowH, b.h);
    out.regions.push({ area: b.a.area, x: ox, y: oy, w: Math.round(b.w), h: Math.round(b.h) });
    for (const c of b.cards) out.cards.push({ ...c, x: c.x + ox, y: c.y + oy, area: b.a.area });
    for (const l of b.labels) out.labels.push({ ...l, x: l.x + ox, y: l.y + oy });
    for (const e of b.edges) out.edges.push(e.points ? { ...e, area: b.a.area, points: e.points.map((p) => ({ x: p.x + ox, y: p.y + oy })), lx: e.lx + ox, ly: e.ly + oy } : { ...e, area: b.a.area });
  }
  return out;
}

// Resolve every edge to concrete geometry: an SVG-style path string, its end tangent, label anchor and bbox.
function finish(L) {
  const byKey = new Map(), first = new Map();
  for (const c of L.cards) { byKey.set(c.key, c); if (!first.has(c.id)) first.set(c.id, c); }
  const box = (k) => byKey.get(k) || first.get(k);
  const edges = [];
  for (const e of L.edges) {
    const a = box(e.from), b = box(e.to); if (!a || !b) continue;
    let seg, lx, ly;
    if (e.points) {
      const p = e.points; seg = [["M", p[0].x, p[0].y]];
      for (let k = 1; k < p.length - 1; k++) {
        const last = k === p.length - 2;
        seg.push(["Q", p[k].x, p[k].y, last ? p[k + 1].x : (p[k].x + p[k + 1].x) / 2, last ? p[k + 1].y : (p[k].y + p[k + 1].y) / 2]);
      }
      if (p.length === 2) seg.push(["L", p[1].x, p[1].y]);
      lx = e.lx; ly = e.ly;
    } else {
      const x1 = a.x + CARD_W, y1 = a.y + 112, x2 = b.x, y2 = b.y + 112;
      if (e.straight) { seg = [["M", x1, y1], ["L", x2, y2]]; lx = (x1 + x2) / 2; ly = y1 - 14; }
      else { const dx = Math.max(200, Math.abs(x2 - x1) / 3); seg = [["M", x1, y1], ["C", x1 + dx, y1, x2 - dx, y2, x2, y2]]; lx = (x1 + x2) / 2; ly = (y1 + y2) / 2 - 10; }
    }
    // bbox over all control points (conservative, good enough for culling)
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const s of seg) for (let i = 1; i < s.length; i += 2) { x0 = Math.min(x0, s[i]); x1 = Math.max(x1, s[i]); y0 = Math.min(y0, s[i + 1]); y1 = Math.max(y1, s[i + 1]); }
    // end tangent for the arrowhead: last control point toward the end point
    const s = seg[seg.length - 1], ex = s[s.length - 2], ey = s[s.length - 1];
    const px = s.length > 3 ? s[s.length - 4] : seg[seg.length - 2][seg[seg.length - 2].length - 2];
    const py = s.length > 3 ? s[s.length - 3] : seg[seg.length - 2][seg[seg.length - 2].length - 1];
    const out = { from: e.from, to: e.to, fromId: a.id, toId: b.id, area: e.area, seg, ang: +Math.atan2(ey - py, ex - px).toFixed(4), bb: [x0, y0, x1, y1] };
    if (e.label && !e.cross) { out.label = e.label; out.lx = lx; out.ly = ly; }
    if (e.cross) out.cross = 1;
    if (e.lane) out.lane = e.lane;
    edges.push(out);
  }
  return { cards: L.cards, labels: L.labels, regions: L.regions, edges };
}
