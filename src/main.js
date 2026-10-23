import { CARD_W, CARD_H, LABEL_W, prepare } from "./layout.js";
import { Renderer, buildScene, AREA_COLORS, FLAGS } from "./render.js";
import { ImageStore } from "./images.js";
import { createLightbox } from "./lightbox.js";

const $ = (s) => document.querySelector(s);
const EXT_ICON = `<svg viewBox="0 0 16 16" width="11" height="11" aria-hidden="true"><path d="M6.5 3.5H3.5v9h9v-3M9.5 3.5h3v3M12.5 3.5 7 9" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>`;
const DEMO_ORIGIN = "https://demo.mercury.com";
const X_ICON = `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" fill="none"/></svg>`;
const FIT_ICON = `<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M2.5 6V2.5H6M10 2.5h3.5V6M13.5 10v3.5H10M6 13.5H2.5V10" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>`;
const lightbox = createLightbox({ closeIcon: X_ICON, fitIcon: FIT_ICON, extIcon: EXT_ICON.replaceAll('"11"', '"14"') });
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---------- data ----------
const MAP = JSON.parse(document.getElementById("map-data").textContent);
const LAYOUTS = JSON.parse(document.getElementById("map-layout").textContent);
const P = prepare(MAP);
const nodes = P.nodes, areaByKey = new Map(MAP.areas.map((a) => [a.area, a]));
const areaOf = new Map([...P.areaOf].map(([id, k]) => [id, areaByKey.get(k)]));
for (const n of nodes.values()) n.kindU = (n.kind || "page").toUpperCase();
MAP.issues = MAP.areas.flatMap((a) => (a.issues || []).map((i) => ({ ...i, area: a.area })));
MAP.journeys = P.journeys;
MAP.edges = P.edges;
const issueById = new Map(MAP.issues.map((i) => [i.id, i]));
const journeyById = new Map(MAP.journeys.map((j) => [j.id, j]));
const patternById = new Map((MAP.patterns || []).map((p) => [p.id, p]));
const issuesByNode = new Map();
for (const i of MAP.issues) for (const id of i.nodeIds) (issuesByNode.get(id) || issuesByNode.set(id, []).get(id)).push(i);
const M = { nodes, areaByKey, issuesByNode, journeyById };

// ---------- state ----------
let view = "journeys"; // set from the URL at boot
let tab = "j", query = "", sevFilter = new Set(["high", "medium", "low"]);
const T = { x: 0, y: 0, k: 0.2 };
let focus = null, current = null, hover = null, drawerLane = null;
let scene, focusIdsCache = null, gSteps = new Map(), lastMove = 0;
const stage = $("#stage"), canvas = $("#world");

const atlas = { img: null, ...MAP.atlas };
const images = new ImageStore(() => invalidate());
const R = new Renderer(canvas, images, atlas);
{
  const img = new Image(); img.src = MAP.atlas.src;
  // An ImageBitmap stays decoded and GPU-resident; drawing sub-rects of an <img> re-validates it on every call.
  // Ship one file (one request, one decode), then slice it into one small ImageBitmap per screen:
  // Chrome's canvas draws a small standalone bitmap ~70x faster than a sub-rect of a large one.
  // Crop from a decoded ImageBitmap, never from the <img>: cropping an <img> re-decodes the whole
  // sheet on the main thread for every tile (915 × ~80ms froze Chrome for over a minute).
  img.decode().then(() => createImageBitmap(img)).then(async (sheet) => {
    const n = Math.max(...[...nodes.values()].map((n) => n.ai)) + 1, { cols, tw, th } = MAP.atlas;
    atlas.tiles = await Promise.all(Array.from({ length: n }, (_, i) => createImageBitmap(sheet, (i % cols) * tw, Math.floor(i / cols) * th, tw, th)));
    sheet.close();
    R.bump(); invalidate();
  }, () => {});
}

// ---------- frame loop: draw at most once per animation frame, only when something changed ----------
let raf = 0, idleTimer = 0;
function invalidate() { if (!raf) raf = requestAnimationFrame(frame); }
const stats = { frames: 0, drawMs: 0 };
let zTimer = 0;
function frame() {
  raf = 0;
  const t0 = performance.now(), moving = t0 - lastMove < 160;
  R.draw({ T, cardOn, laneOn, hover, sel: current ? new Set([current]) : EMPTY, gSteps, moving });
  drawMiniView();
  const dt = performance.now() - t0;
  stats.frames++; stats.drawMs += dt;
  // The zoom readout is DOM: writing it every frame costs a layout + paint, so settle it at 10 Hz.
  if (!zTimer) zTimer = setTimeout(() => { zTimer = 0; const z = Math.round(T.k * 100) + "%"; if (zlabel.textContent !== z) zlabel.textContent = z; }, 100);
}
const EMPTY = new Set(), zlabel = $("#zlabel");
let urlTimer = 0, viewTouched = false;
function moved() {
  lastMove = performance.now();
  invalidate();
  clearTimeout(idleTimer); idleTimer = setTimeout(invalidate, 180); // settle → full-resolution pass
  // Record the viewport in the URL once the canvas settles (replace, not push: panning is not navigation).
  clearTimeout(urlTimer); urlTimer = setTimeout(() => viewTouched && syncUrl("replace"), 400);
}

function cardOn(c) {
  const ids = focusIdsCache; if (!ids) return true;
  const j = focus.type === "journey" ? journeyById.get(focus.id) : null;
  return view === "journeys" && j ? c.lane === j.id : ids.has(c.id);
}
function laneOn(l) { return !focusIdsCache || (focus.type === "journey" && l.lane === focus.id); }

// ---------- sizing ----------
// Stage position cached so input handlers never force a synchronous layout.
let SR = { left: 0, top: 0 };
function resize() {
  const r = SR = stage.getBoundingClientRect();
  R.resize(r.width, r.height, window.devicePixelRatio || 1);
  canvas.style.width = r.width + "px"; canvas.style.height = r.height + "px";
  drawMini(); invalidate();
}
new ResizeObserver(resize).observe(stage);
matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`).addEventListener?.("change", resize);
const scheme = matchMedia("(prefers-color-scheme: dark)");
scheme.addEventListener("change", () => { R.setTheme(getComputedStyle(document.documentElement)); themeColors(); drawMini(); invalidate(); });

// ---------- layout / scene ----------
function render() {
  scene = buildScene(LAYOUTS[view], M);
  R.setScene(scene);
  applyFocus();
  drawMini();
}

// ---------- transform ----------
function zoomAt(f, cx, cy) {
  viewTouched = true;
  const k = Math.min(2.5, Math.max(0.02, T.k * f));
  T.x = cx - (cx - T.x) * (k / T.k); T.y = cy - (cy - T.y) * (k / T.k); T.k = k; moved();
}
function fitRect(r, pad = 60, maxK = 1) {
  const W = stage.clientWidth - (drawerOpen() ? Math.min(640, stage.clientWidth) : 0), H = stage.clientHeight;
  const k = Math.min(maxK, (W - pad * 2) / r.w, (H - pad * 2) / r.h);
  T.k = k; T.x = (W - r.w * k) / 2 - r.x * k; T.y = (H - r.h * k) / 2 - r.y * k; moved();
}
const fit = () => fitRect(scene.bounds, 30);
const drawerOpen = () => !$("#drawer").hidden;

function hit(clientX, clientY) {
  const r = SR;
  const x = (clientX - r.left - T.x) / T.k, y = (clientY - r.top - T.y) / T.k;
  const cs = scene.cards;
  for (let i = cs.length - 1; i >= 0; i--) { const c = cs[i]; if (x >= c.x && x <= c.x + CARD_W && y >= c.y && y <= c.y + CARD_H) return c; }
  return null;
}

let drag = null; const pointers = new Map();
canvas.addEventListener("pointerdown", (e) => {
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  canvas.setPointerCapture(e.pointerId);
  drag = { x: e.clientX, y: e.clientY, tx: T.x, ty: T.y, moved: false };
  if (pointers.size === 2) { const [p, q] = [...pointers.values()]; drag.pinch = { d: Math.hypot(p.x - q.x, p.y - q.y), k: T.k }; }
});
canvas.addEventListener("pointermove", (e) => {
  if (!drag) { // hover
    const c = e.pointerType === "mouse" ? hit(e.clientX, e.clientY) : null;
    if (c !== hover) { hover = c; canvas.style.cursor = c ? "pointer" : ""; invalidate(); }
    return;
  }
  if (!pointers.has(e.pointerId)) return;
  pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (drag.pinch && pointers.size === 2) {
    const [p, q] = [...pointers.values()], r = SR;
    zoomAt((drag.pinch.k * Math.hypot(p.x - q.x, p.y - q.y) / drag.pinch.d) / T.k, (p.x + q.x) / 2 - r.left, (p.y + q.y) / 2 - r.top); drag.moved = true; return;
  }
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
  if (!drag.moved && Math.abs(dx) + Math.abs(dy) > 4) { drag.moved = true; stage.classList.add("panning"); if (hover) { hover = null; canvas.style.cursor = ""; } }
  if (drag.moved) { viewTouched = true; T.x = drag.tx + dx; T.y = drag.ty + dy; moved(); }
});
const end = (e) => {
  pointers.delete(e.pointerId);
  if (!drag) return;
  stage.classList.remove("panning");
  if (!drag.moved && pointers.size === 0 && e.type === "pointerup") {
    const c = hit(e.clientX, e.clientY);
    if (c) openNode(c.id, false, c.lane);
  }
  if (pointers.size === 0) drag = null;
};
canvas.addEventListener("pointerup", end); canvas.addEventListener("pointercancel", end);
canvas.addEventListener("pointerleave", () => { if (hover && !drag) { hover = null; canvas.style.cursor = ""; invalidate(); } });
canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const r = SR, x = e.clientX - r.left, y = e.clientY - r.top;
  const mouseWheel = e.deltaMode === 1 || (e.deltaX === 0 && Math.abs(e.deltaY) >= 50 && Number.isInteger(e.deltaY));
  if (e.shiftKey) {
    // Shift+scroll zooms. macOS turns Shift+wheel into horizontal scrolling, so read whichever axis moved.
    const d = Math.abs(e.deltaY) >= Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
    zoomAt(Math.exp(-d * (e.deltaMode === 1 ? 0.05 : 0.0035)), x, y);
  } else if (e.ctrlKey || e.metaKey || mouseWheel) zoomAt(Math.exp(-e.deltaY * (e.ctrlKey ? 0.012 : 0.0022)), x, y);
  else { viewTouched = true; T.x -= e.deltaX; T.y -= e.deltaY; moved(); }
}, { passive: false });
$("#zin").onclick = () => zoomAt(1.4, stage.clientWidth / 2, stage.clientHeight / 2);
$("#zout").onclick = () => zoomAt(1 / 1.4, stage.clientWidth / 2, stage.clientHeight / 2);
$("#zfit").onclick = () => fit();
addEventListener("keydown", (e) => {
  if (lightbox.isOpen() || e.target.matches("input, textarea")) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "f" || e.key === "F") fit();
  if ((e.key === "c" || e.key === "C") && current) centerSelected();
  if (e.key === "Escape") { if (drawerOpen()) closeDrawer(); else clearFocus(); }
  if (e.key === "+" || e.key === "=") $("#zin").click();
  if (e.key === "-") $("#zout").click();
  if (drawerOpen() && (e.key === "ArrowRight" || e.key === "ArrowLeft")) stepJourney(e.key === "ArrowRight" ? 1 : -1);
});

// ---------- minimap: static base cached in an offscreen canvas, viewport box drawn on top ----------
const mini = $("#mini"), mctx = mini.getContext("2d");
const miniBase = document.createElement("canvas"); miniBase.width = mini.width; miniBase.height = mini.height;
let MC = {};
function themeColors() { const css = getComputedStyle(document.documentElement); MC = { faint: css.getPropertyValue("--faint").trim(), accent: css.getPropertyValue("--accent").trim() }; }
function miniScale() { const b = scene.bounds, s = Math.min(mini.width / b.w, mini.height / b.h); return { s, ox: (mini.width - b.w * s) / 2 - b.x * s, oy: (mini.height - b.h * s) / 2 - b.y * s }; }
function drawMini() {
  if (!scene) return;
  const { s, ox, oy } = miniScale(), b = miniBase.getContext("2d");
  b.clearRect(0, 0, mini.width, mini.height);
  for (const r of scene.regions) { b.fillStyle = (AREA_COLORS[r.area] || "#888888") + "22"; b.fillRect(ox + r.x * s, oy + r.y * s, r.w * s, r.h * s); }
  b.fillStyle = MC.faint;
  for (const c of scene.cards) b.fillRect(ox + c.x * s, oy + c.y * s, Math.max(1, CARD_W * s), Math.max(1, CARD_H * s * 0.8));
  drawMiniView();
}
let miniKey = "";
function drawMiniView() {
  if (!scene) return;
  const key = `${T.x}|${T.y}|${T.k}|${R.W}|${R.H}`;
  if (key === miniKey) return; miniKey = key;
  const { s, ox, oy } = miniScale();
  mctx.clearRect(0, 0, mini.width, mini.height);
  mctx.drawImage(miniBase, 0, 0);
  mctx.strokeStyle = MC.accent; mctx.lineWidth = 3;
  mctx.strokeRect(ox + (-T.x / T.k) * s, oy + (-T.y / T.k) * s, (R.W / T.k) * s, (R.H / T.k) * s);
}
mini.addEventListener("pointerdown", (e) => {
  e.stopPropagation();
  const move = (ev) => {
    const r = mini.getBoundingClientRect(), { s, ox, oy } = miniScale();
    const wx = ((ev.clientX - r.left) * (mini.width / r.width) - ox) / s, wy = ((ev.clientY - r.top) * (mini.height / r.height) - oy) / s;
    viewTouched = true; T.x = stage.clientWidth / 2 - wx * T.k; T.y = stage.clientHeight / 2 - wy * T.k; moved();
  };
  move(e); mini.setPointerCapture(e.pointerId);
  mini.onpointermove = move; mini.onpointerup = () => { mini.onpointermove = null; };
});

// ---------- focus (journey / issue / pattern / search highlight) ----------
function focusIds() {
  if (!focus) return null;
  if (focus.type === "journey") { const j = journeyById.get(focus.id); return j ? new Set(j.steps) : null; }
  if (focus.type === "issue") { const i = issueById.get(focus.id); return i ? new Set(i.nodeIds) : null; }
  if (focus.type === "pattern") { const p = patternById.get(focus.id); return p ? new Set(p.issueIds.flatMap((id) => issueById.get(id)?.nodeIds || [])) : null; }
  if (focus.type === "search") return focus.ids;
}
function applyFocus() {
  const ids = focusIdsCache = focusIds();
  const j = focus?.type === "journey" ? journeyById.get(focus.id) : null;
  const pairs = new Set(); if (j) j.steps.forEach((s, i) => i && pairs.add(j.steps[i - 1] + ">" + s));
  gSteps = new Map(); if (j && view === "graph") j.steps.forEach((s, i) => { if (!gSteps.has(s)) gSteps.set(s, i + 1); });
  R.setEdgeStyles((e) => {
    const hot = j ? (view === "journeys" ? e.lane === j.id : pairs.has(e.fromId + ">" + e.toId)) : false;
    if (hot) return 1;
    if (ids && !(focus.type !== "journey" && ids.has(e.fromId) && ids.has(e.toId))) return 2;
    return 0;
  });
  const b = $("#banner");
  if (focus) {
    b.hidden = false;
    $("#banner-t").textContent = focus.type === "journey" ? `Journey: ${j?.name}` : focus.type === "issue" ? `Issue: ${issueById.get(focus.id)?.title}` : focus.type === "pattern" ? `Pattern: ${patternById.get(focus.id)?.title} · ${ids.size} screens` : `${ids.size} screens match “${query}”`;
  } else b.hidden = true;
  renderList();
  invalidate();
}
function clearFocus() { focus = null; if ($("#q").value) { $("#q").value = ""; query = ""; syncClear(); } applyFocus(); syncUrl(); }
$("#banner-x").onclick = clearFocus;
function frameIds(ids, lane) {
  const cs = scene.cards.filter((c) => ids.has(c.id) && (!lane || view !== "journeys" || c.lane === lane));
  if (!cs.length) return;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const c of cs) { x0 = Math.min(x0, c.x); y0 = Math.min(y0, c.y); x1 = Math.max(x1, c.x + CARD_W); y1 = Math.max(y1, c.y + CARD_H); }
  const x = x0 - (lane && view === "journeys" ? LABEL_W : 40), y = y0 - 60;
  fitRect({ x, y, w: x1 - x + 40, h: y1 - y + 60 }, 40, 0.9);
}
function focusJourney(id) { focus = { type: "journey", id }; applyFocus(); frameIds(new Set(journeyById.get(id).steps), id); syncUrl(); }
function focusPattern(id) { focus = { type: "pattern", id }; applyFocus(); if (focusIdsCache?.size) frameIds(focusIdsCache); syncUrl(); }
function focusIssue(id) { focus = { type: "issue", id }; applyFocus(); const i = issueById.get(id); if (i.nodeIds.length) { frameIds(new Set(i.nodeIds)); openNode(i.nodeIds[0], false, null, id); } syncUrl(); }
// Center the selected card (the instance in its journey row, when it has one).
function centerSelected() { if (current) frameIds(new Set([current]), drawerLane); }

// ---------- drawer ----------
function openNode(id, frameIt, lane, highlightIssue) {
  const n = nodes.get(id); if (!n) return;
  current = id; drawerLane = lane || (focus?.type === "journey" ? focus.id : null);
  invalidate();
  const a = areaOf.get(id), outs = MAP.edges.filter((e) => e.from === id), ins = MAP.edges.filter((e) => e.to === id);
  const iss = issuesByNode.get(id) || [], js = MAP.journeys.filter((j) => j.steps.includes(id));
  const j = drawerLane && MAP.journeys.find((x) => x.id === drawerLane && x.steps.includes(id));
  const d = $("#drawer");
  d.innerHTML = `<header><div><h3>${esc(n.title)}</h3></div><div class="x"><button class="icon-btn" id="d-frame" aria-label="Center on canvas" title="Center on canvas (C)">◎</button><button class="icon-btn" id="d-close" aria-label="Close panel" title="Close (Esc)">${X_ICON}</button></div>
    <div class="u">${esc(a.title)} · ${esc(n.kind || "page")}${n.url ? ` · <a class="path" href="${esc(DEMO_ORIGIN + n.url)}" target="_blank" rel="noopener noreferrer" title="Open on demo.mercury.com (new tab)">${esc(n.url)}${EXT_ICON}<span class="sr">(opens in a new tab)</span></a>` : ""}</div></header>
    <div class="body">
      ${j ? `<div class="jnav"><button id="j-prev" ${j.steps.indexOf(id) === 0 ? "disabled" : ""}>← Prev</button><span>Step ${j.steps.indexOf(id) + 1} of ${j.steps.length} · ${esc(j.name)}</span><button id="j-next" ${j.steps.indexOf(id) === j.steps.length - 1 ? "disabled" : ""}>Next →</button></div>` : ""}
      <a class="full" href="${esc(n.full)}" target="_blank" rel="noopener" title="View full screen" style="aspect-ratio:${n.w}/${n.h}"><img alt="${esc(n.title)} screenshot" decoding="async" src="${esc(n.thumb)}" data-full="${esc(n.full)}" width="${n.w}" height="${n.h}"></a>
      ${n.note ? `<p class="note">${esc(n.note)}</p>` : ""}
      ${iss.length ? `<div><h4>Design issues on this screen</h4><div class="links">${iss.map((i) => issueHTML(i, i.id === highlightIssue)).join("")}</div></div>` : ""}
      ${outs.length ? `<div><h4>Leads to</h4><div class="links">${outs.map((e) => linkHTML(e.to, e.label)).join("")}</div></div>` : ""}
      ${ins.length ? `<div><h4>Reached from</h4><div class="links">${ins.map((e) => linkHTML(e.from, e.label)).join("")}</div></div>` : ""}
      ${js.length ? `<div><h4>Part of journeys</h4><div class="links">${js.map((j) => `<button data-journey="${esc(j.id)}"><span>${esc(j.name)}</span><small>step ${j.steps.indexOf(id) + 1} of ${j.steps.length}</small></button>`).join("")}</div></div>` : ""}
    </div>`;
  d.hidden = false;
  // Show the (already cached) thumbnail immediately, swap in the lossless full image once decoded.
  const img = d.querySelector(".full img"), full = new Image();
  full.src = n.full; full.decode().then(() => { if (current === id) img.src = n.full; }, () => {});
  // A plain click opens the zoomable overlay; Cmd/Ctrl/Shift/middle-click and "Open in new tab" keep the link's native behaviour.
  d.querySelector("a.full").addEventListener("click", (e) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    lightbox.open({ src: img.currentSrc || img.src, full: new URL(n.full, location.href).href, w: n.w / 2, h: n.h / 2, title: n.title, fromEl: img });
  });
  d.querySelector("#d-close").onclick = closeDrawer;
  d.querySelector("#d-frame").onclick = centerSelected;
  d.querySelector("#j-prev")?.addEventListener("click", () => stepJourney(-1));
  d.querySelector("#j-next")?.addEventListener("click", () => stepJourney(1));
  d.querySelectorAll("[data-node]").forEach((b) => b.onclick = () => openNode(b.dataset.node, true));
  d.querySelectorAll("[data-journey]").forEach((b) => b.onclick = () => focusJourney(b.dataset.journey));
  d.querySelectorAll("[data-issue]").forEach((b) => b.onclick = () => { focus = { type: "issue", id: b.dataset.issue }; applyFocus(); frameIds(new Set(issueById.get(b.dataset.issue).nodeIds)); syncUrl(); });
  d.querySelector(".body").scrollTop = 0;
  if (frameIt) frameIds(new Set([id]), drawerLane);
  syncListCurrent();
  syncUrl();
}
function linkHTML(id, label) { const n = nodes.get(id); if (!n) return ""; return `<button data-node="${esc(id)}"><span>${esc(n.title)}</span><small>${esc(label || "")} · ${esc(areaOf.get(id).title)}</small></button>`; }
function issueHTML(i, hi) {
  return `<div class="issue" ${hi ? 'style="border-color:var(--accent)"' : ""}><div class="meta"><span class="sev ${esc(i.severity)}">${esc(i.severity)}</span><span>${esc(i.category)}</span>${i.nodeIds.length > 1 ? `<button class="chip" data-issue="${esc(i.id)}">Show all ${i.nodeIds.length} screens</button>` : ""}</div><b>${esc(i.title)}</b><p>${esc(i.detail)}</p>${i.evidence ? `<code>${esc(i.evidence)}</code>` : ""}</div>`;
}
function stepJourney(dir) {
  const j = drawerLane && journeyById.get(drawerLane); if (!j) return;
  const next = j.steps[j.steps.indexOf(current) + dir]; if (next) openNode(next, true, j.id);
}
function closeDrawer() { $("#drawer").hidden = true; current = null; drawerLane = null; invalidate(); syncListCurrent(); syncUrl(); }

// ---------- sidebar ----------
function matches(...xs) { return !query || xs.some((x) => String(x || "").toLowerCase().includes(query)); }
function syncListCurrent() { if (tab === "s") $("#list").querySelectorAll("[data-n]").forEach((b) => b.setAttribute("aria-current", b.dataset.n === current)); }
function renderList() {
  const L = $("#list"); let h = "";
  if (tab === "j") {
    for (const a of MAP.areas) {
      const js = MAP.journeys.filter((j) => j.area === a.area && matches(j.name, j.description, ...j.steps.map((s) => nodes.get(s)?.title)));
      if (!js.length) continue;
      h += `<div class="group-h"><i style="background:${AREA_COLORS[a.area]}"></i>${esc(a.title)}</div>`;
      h += js.map((j) => `<button class="item" data-j="${esc(j.id)}" aria-current="${focus?.type === "journey" && focus.id === j.id}"><span class="t">${esc(j.name)}</span><span class="d">${esc(j.description || "")}</span><span class="m">${j.steps.length} steps</span></button>`).join("");
    }
  } else if (tab === "s") {
    for (const a of MAP.areas) {
      const ns = a.nodes.filter((n) => matches(n.title, n.url, n.note, n.kind));
      if (!ns.length) continue;
      h += `<div class="group-h"><i style="background:${AREA_COLORS[a.area]}"></i>${esc(a.title)} · ${ns.length}</div>`;
      h += ns.map((n) => `<button class="item" data-n="${esc(n.id)}" aria-current="${current === n.id}"><span class="t">${esc(n.title)}</span><span class="m">${esc(n.kind || "page")} · ${esc(n.url || "")}</span></button>`).join("");
    }
  } else {
    const counts = { high: 0, medium: 0, low: 0 }; MAP.issues.forEach((i) => counts[i.severity] = (counts[i.severity] || 0) + 1);
    h += `<div class="filters">${["high", "medium", "low"].map((s) => `<button class="chip" data-sev="${s}" aria-pressed="${sevFilter.has(s)}">${s} · ${counts[s] || 0}</button>`).join("")}</div>`;
    const rank = { high: 0, medium: 1, low: 2 };
    const pats = (MAP.patterns || []).filter((p) => matches(p.title, p.summary));
    if (pats.length) {
      h += `<div class="group-h">Patterns across the product · ${pats.length}</div>`;
      h += pats.map((p) => `<button class="item pattern" data-p="${esc(p.id)}" aria-current="${focus?.type === "pattern" && focus.id === p.id}"><span class="t">${esc(p.title)}</span><span class="d">${esc(p.summary)}</span><span class="m">${p.issueIds.length} issues · ${p.areas.length} of ${MAP.areas.length} areas</span></button>`
        + (focus?.type === "pattern" && focus.id === p.id ? `<div class="pat-issues">${p.issueIds.map((id) => issueById.get(id)).filter(Boolean).map((i) => `<button class="item" data-i="${esc(i.id)}"><span class="sev ${esc(i.severity)}">${esc(i.severity)}</span><span class="t">${esc(i.title)}</span><span class="m">${esc(areaOf.get(i.nodeIds[0])?.title || i.area)}</span></button>`).join("")}</div>` : "")).join("");
      h += `<div class="group-h">All issues by category</div>`;
    }
    const cats = {};
    MAP.issues.filter((i) => sevFilter.has(i.severity) && matches(i.title, i.detail, i.category, i.evidence)).forEach((i) => (cats[i.category] ||= []).push(i));
    for (const [c, is] of Object.entries(cats).sort((a, b) => b[1].length - a[1].length)) {
      h += `<div class="group-h">${esc(c)} · ${is.length}</div>`;
      h += is.sort((a, b) => rank[a.severity] - rank[b.severity]).map((i) => `<button class="item" data-i="${esc(i.id)}" aria-current="${focus?.type === "issue" && focus.id === i.id}"><span class="sev ${esc(i.severity)}">${esc(i.severity)}</span><span class="t">${esc(i.title)}</span><span class="m">${esc(areaOf.get(i.nodeIds[0])?.title || i.area)} · ${i.nodeIds.length} screen${i.nodeIds.length === 1 ? "" : "s"}</span></button>`).join("");
    }
  }
  L.innerHTML = h || `<p style="padding:12px;color:var(--muted)">${query ? `Nothing matches “${esc(query)}”.` : "Nothing here yet."}</p>`;
}
// One delegated listener instead of hundreds of per-item handlers re-bound on every render.
$("#list").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.j) focusJourney(b.dataset.j);
  else if (b.dataset.n) openNode(b.dataset.n, true);
  else if (b.dataset.i) focusIssue(b.dataset.i);
  else if (b.dataset.p) focusPattern(b.dataset.p);
  else if (b.dataset.sev) { sevFilter.has(b.dataset.sev) ? sevFilter.delete(b.dataset.sev) : sevFilter.add(b.dataset.sev); renderList(); }
});
function setTab(t) { tab = t; ["j", "s", "i"].forEach((x) => $("#t-" + x).setAttribute("aria-selected", x === t)); renderList(); }
$("#t-j").onclick = () => setTab("j"); $("#t-s").onclick = () => setTab("s"); $("#t-i").onclick = () => setTab("i");
function searchFocus() { return { type: "search", ids: new Set([...nodes.values()].filter((n) => matches(n.title, n.url, n.note)).map((n) => n.id)) }; }
function runSearch() {
  query = $("#q").value.trim().toLowerCase();
  if (query) focus = searchFocus();
  else if (focus?.type === "search") focus = null;
  applyFocus();
  syncUrl("replace"); // typing refines one history entry instead of adding one per keystroke
}
const qClear = $("#q-clear");
function syncClear() { qClear.hidden = !$("#q").value; }
let qt; $("#q").addEventListener("input", () => { syncClear(); clearTimeout(qt); qt = setTimeout(runSearch, 150); });
$("#q").addEventListener("keydown", (e) => { if (e.key === "Escape" && e.target.value) { e.stopPropagation(); clearSearch(); } });
function clearSearch() { $("#q").value = ""; syncClear(); clearTimeout(qt); runSearch(); $("#q").focus(); }
qClear.onclick = clearSearch;
function showView(v) {
  view = v;
  $("#v-journeys").setAttribute("aria-pressed", v === "journeys"); $("#v-graph").setAttribute("aria-pressed", v === "graph");
  render();
}
function setView(v) {
  if (v === view) return;
  showView(v);
  if (current) centerSelected(); else if (focus?.type === "journey") frameIds(new Set(journeyById.get(focus.id).steps), focus.id); else fit();
  syncUrl();
}
$("#v-journeys").onclick = () => setView("journeys"); $("#v-graph").onclick = () => setView("graph");

// ---------- URL state ----------
// ?view=graph  &q=search  &journey= | &issue= | &pattern=  &screen=<node>&lane=<journey row>  &at=x,y,zoom
// Discrete navigation (select, close, focus, switch view) pushes a history entry so Back/Forward work;
// typing and panning replace the current one. Old #<nodeId> links still open that screen.
let urlPending = null, urlQueued = false, urlSuppress = false;
function syncUrl(mode = "push") {
  if (urlSuppress) return;
  if (urlPending !== "push") urlPending = mode; // several changes in one task collapse into one entry
  if (!urlQueued) { urlQueued = true; queueMicrotask(flushUrl); }
}
function buildUrl() {
  const p = new URLSearchParams();
  if (view === "graph") p.set("view", "graph");
  const q = $("#q").value.trim(); if (q) p.set("q", q);
  if (focus && focus.type !== "search") p.set(focus.type, focus.id);
  if (current) {
    p.set("screen", current);
    if (drawerLane && !(focus?.type === "journey" && focus.id === drawerLane)) p.set("lane", drawerLane);
  }
  if (viewTouched) p.set("at", [Math.round((R.W / 2 - T.x) / T.k), Math.round((R.H / 2 - T.y) / T.k), +T.k.toFixed(4)].join(","));
  const qs = p.toString().replace(/%2C/g, ",");
  return location.pathname + (qs ? "?" + qs : "");
}
function flushUrl() {
  urlQueued = false;
  const mode = urlPending; urlPending = null;
  const url = buildUrl();
  if (url === location.pathname + location.search + location.hash) return;
  try { history[mode === "push" ? "pushState" : "replaceState"](null, "", url); } catch {}
}
function applyUrl() {
  const p = new URLSearchParams(location.search);
  const legacy = decodeURIComponent(location.hash.slice(1));
  urlSuppress = true;
  try {
    const v = p.get("view") === "graph" ? "graph" : "journeys";
    if (v !== view || !scene) showView(v);
    $("#q").value = p.get("q") || ""; syncClear();
    query = $("#q").value.trim().toLowerCase();
    const has = (k, m) => p.get(k) && m.has(p.get(k));
    focus = has("journey", journeyById) ? { type: "journey", id: p.get("journey") }
      : has("issue", issueById) ? { type: "issue", id: p.get("issue") }
      : has("pattern", patternById) ? { type: "pattern", id: p.get("pattern") }
      : query ? searchFocus() : null;
    applyFocus();
    const at = (p.get("at") || "").split(",").map(Number);
    const hasAt = at.length === 3 && at.every(Number.isFinite) && at[2] > 0;
    const screen = p.get("screen") || legacy;
    if (nodes.has(screen)) openNode(screen, false, journeyById.has(p.get("lane")) ? p.get("lane") : null);
    else if (drawerOpen()) closeDrawer();
    if (hasAt) {
      viewTouched = true;
      T.k = Math.min(2.5, Math.max(0.02, at[2])); T.x = R.W / 2 - at[0] * T.k; T.y = R.H / 2 - at[1] * T.k; moved();
    } else {
      viewTouched = false;
      if (current) centerSelected();
      else if (focus?.type === "journey") frameIds(new Set(journeyById.get(focus.id).steps), focus.id);
      else if (focus && focus.type !== "search" && focusIdsCache?.size) frameIds(focusIdsCache);
      else fit();
    }
  } finally { urlSuppress = false; }
  if (legacy) syncUrl("replace"); // rewrite old #node links into the canonical form
}
addEventListener("popstate", applyUrl);

// Test hook for the benchmark: screen rect of the first card showing a node.
window.__map = {
  FLAGS, T, stats,
  // Throughput probe: draw n frames panning back and forth at zoom k around world point (wx, wy).
  async perf(k, wx, wy, n = 90) {
    T.k = k; T.x = R.W / 2 - wx * k; T.y = R.H / 2 - wy * k; moved();
    for (let i = 0; i < 30; i++) await new Promise((r) => requestAnimationFrame(r)); // warm images
    await new Promise((r) => setTimeout(r, 400));
    const t0 = performance.now(); let js = 0;
    for (let i = 0; i < n; i++) {
      T.x += i % 40 < 20 ? 6 : -6; lastMove = performance.now();
      await new Promise((r) => requestAnimationFrame(() => { const a = performance.now(); frame(); js += performance.now() - a; r(); }));
    }
    return { frameMs: +((performance.now() - t0) / n).toFixed(2), jsMs: +(js / n).toFixed(2) };
  },
  rectOf(id) {
    const c = scene.cards.find((c) => c.id === id); if (!c) return null;
    const r = SR;
    return { x: r.left + T.x + c.x * T.k, y: r.top + T.y + c.y * T.k, width: CARD_W * T.k, height: CARD_H * T.k };
  },
};

// ---------- boot ----------
$("#c-j").textContent = MAP.journeys.length; $("#c-s").textContent = nodes.size; $("#c-i").textContent = MAP.issues.length;
R.setTheme(getComputedStyle(document.documentElement)); themeColors();
resize(); applyUrl();
zlabel.textContent = Math.round(T.k * 100) + "%";
// Canvas text never triggers a webfont load by itself: request the faces explicitly, then
// re-measure and repaint (setTheme clears the text caches and invalidates the cached layers).
Promise.all(['400 16px "Arcadia Text"', '500 16px "Arcadia Text"', '460 26px "Arcadia Display"'].map((f) => document.fonts.load(f)))
  .catch(() => {}).then(() => { R.setTheme(getComputedStyle(document.documentElement)); invalidate(); });
window.addEventListener("resize", () => R.bump()); // devicePixelRatio may have changed
