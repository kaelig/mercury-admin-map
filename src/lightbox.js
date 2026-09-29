// Full-screen, zoomable view of one screenshot. A modal <dialog> gives top-layer stacking, inert
// background, focus trapping and focus return for free. The image is moved with a single CSS
// transform (translate + scale, origin 0 0); it grows out of the drawer thumbnail on open and
// shrinks back into it on close.
const MAX_ZOOM = 4; // 400% of capture size (1 CSS px per screenshot px at 2x = 100%)
const PAD = 24, BAR = 56;

export function createLightbox({ closeIcon, extIcon, fitIcon }) {
  const d = document.createElement("dialog");
  d.className = "lightbox";
  d.setAttribute("aria-label", "Screenshot");
  d.innerHTML = `<div class="lb-bg"></div><div class="lb-stage"><img alt="" draggable="false"></div>
    <div class="lb-bar"><span class="lb-title"></span><span class="lb-hint">Click, scroll or pinch to zoom · drag to pan · Esc to close</span>
      <div class="lb-tools">
        <button class="icon-btn" type="button" data-act="out" aria-label="Zoom out" title="Zoom out (−)">−</button>
        <span class="lb-z" aria-live="polite">100%</span>
        <button class="icon-btn" type="button" data-act="in" aria-label="Zoom in" title="Zoom in (+)">+</button>
        <button class="icon-btn" type="button" data-act="fit" aria-label="Fit to screen" title="Fit to screen (0)">${fitIcon}</button>
        <a class="icon-btn" target="_blank" rel="noopener" aria-label="Open image in a new tab" title="Open image in a new tab">${extIcon}</a>
        <button class="icon-btn" type="button" data-act="close" aria-label="Close" title="Close (Esc)" autofocus>${closeIcon}</button>
      </div></div>`;
  document.body.append(d);
  const stage = d.querySelector(".lb-stage"), img = d.querySelector("img"), zl = d.querySelector(".lb-z"), ext = d.querySelector("a");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  let W = 0, H = 0, s = 1, x = 0, y = 0, from = null, closing = false, raf = 0, fitted = true;

  const vw = () => d.clientWidth, vh = () => d.clientHeight;
  const fitScale = () => Math.min((vw() - 2 * PAD) / W, (vh() - BAR - 2 * PAD) / H);
  const minScale = () => Math.min(fitScale(), 1);
  const maxScale = () => Math.max(MAX_ZOOM, fitScale());
  // Keep the image centred on an axis where it fits, and never let it leave the screen where it doesn't.
  function clampPan() {
    const sw = W * s, sh = H * s, top = BAR + PAD, h = vh() - top - PAD;
    x = sw <= vw() - 2 * PAD ? (vw() - sw) / 2 : Math.min(PAD, Math.max(vw() - PAD - sw, x));
    y = sh <= h ? top + (h - sh) / 2 : Math.min(top, Math.max(top + h - sh, y));
  }
  function apply() {
    cancelAnimationFrame(raf); raf = 0;
    img.style.transform = `translate(${x}px, ${y}px) scale(${s})`;
    zl.textContent = Math.round(s * 100) + "%";
    stage.classList.toggle("pannable", s > fitScale() * 1.001);
  }
  const schedule = () => { if (!raf) raf = requestAnimationFrame(apply); };
  function set(ns, cx, cy, animate) {
    ns = Math.min(maxScale(), Math.max(minScale(), ns));
    x = cx - (cx - x) * (ns / s); y = cy - (cy - y) * (ns / s); s = ns; fitted = false;
    clampPan();
    img.classList.toggle("anim", !!animate);
    animate ? apply() : schedule();
  }
  const center = () => [vw() / 2, BAR + (vh() - BAR) / 2];
  function fit(animate) { s = fitScale(); fitted = true; clampPan(); img.classList.toggle("anim", !!animate); apply(); }
  // Transform that lays the image exactly over the drawer thumbnail.
  function toThumb() {
    const r = from?.isConnected && from.getBoundingClientRect();
    if (!r || !r.width) return false;
    s = r.width / W; x = r.left; y = r.top; return true;
  }

  function open({ src, full, w, h, title, fromEl }) {
    W = w; H = h; from = fromEl; closing = false;
    img.style.width = W + "px"; img.style.height = H + "px";
    img.alt = title + " screenshot"; img.src = src;
    d.querySelector(".lb-title").textContent = title;
    ext.href = full;
    if (src !== full) { const f = new Image(); f.src = full; f.decode().then(() => { if (d.open && ext.href === f.src) img.src = full; }, () => {}); }
    d.classList.remove("shown");
    img.classList.remove("anim");
    d.showModal();
    const animate = !reduced.matches && toThumb();
    if (animate) { apply(); void img.offsetWidth; } // commit the start position before transitioning
    d.classList.add("shown");
    fit(animate);
  }
  function close() {
    if (!d.open || closing) return;
    closing = true;
    d.classList.remove("shown");
    if (reduced.matches || !toThumb()) { d.close(); return; }
    img.classList.add("anim"); apply();
    const done = () => { img.removeEventListener("transitionend", done); clearTimeout(t); if (closing) d.close(); };
    const t = setTimeout(done, 400);
    img.addEventListener("transitionend", done);
  }
  d.addEventListener("close", () => { closing = false; img.removeAttribute("src"); });
  d.addEventListener("cancel", (e) => { e.preventDefault(); close(); }); // Esc animates out instead of vanishing

  d.querySelector(".lb-tools").addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "close") close();
    else if (act === "fit") fit(true);
    else if (act) set(s * (act === "in" ? 1.5 : 1 / 1.5), ...center(), true);
  });
  d.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key, step = 80;
    if (k === "+" || k === "=") set(s * 1.5, ...center(), true);
    else if (k === "-" || k === "_") set(s / 1.5, ...center(), true);
    else if (k === "0") fit(true);
    else if (k.startsWith("Arrow")) { x -= k === "ArrowRight" ? step : k === "ArrowLeft" ? -step : 0; y -= k === "ArrowDown" ? step : k === "ArrowUp" ? -step : 0; clampPan(); img.classList.add("anim"); apply(); }
    else return;
    e.preventDefault();
  });

  // Same wheel conventions as the map: pinch, Ctrl/Cmd+scroll, Shift+scroll and mouse wheels zoom; trackpad scroll pans.
  d.addEventListener("wheel", (e) => {
    e.preventDefault();
    if (closing) return;
    const mouseWheel = e.deltaMode === 1 || (e.deltaX === 0 && Math.abs(e.deltaY) >= 50 && Number.isInteger(e.deltaY));
    if (e.shiftKey) { const dd = Math.abs(e.deltaY) >= Math.abs(e.deltaX) ? e.deltaY : e.deltaX; set(s * Math.exp(-dd * (e.deltaMode === 1 ? 0.05 : 0.0035)), e.clientX, e.clientY); }
    else if (e.ctrlKey || e.metaKey || mouseWheel) set(s * Math.exp(-e.deltaY * (e.ctrlKey ? 0.012 : 0.0022)), e.clientX, e.clientY);
    else { x -= e.deltaX; y -= e.deltaY; clampPan(); img.classList.remove("anim"); schedule(); }
  }, { passive: false });

  // Drag to pan, two pointers to pinch. A click that didn't move closes when it lands on the backdrop;
  // one on the image toggles zoom, or opens it in a new tab with Cmd/Ctrl like the thumbnail link.
  const pts = new Map();
  let g = null;
  stage.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || closing) return;
    stage.setPointerCapture(e.pointerId);
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    img.classList.remove("anim");
    g = { moved: g?.moved || pts.size > 1, sx: e.clientX, sy: e.clientY, onImg: e.target === img, pinch: null };
    stage.classList.add("dragging");
  });
  stage.addEventListener("pointermove", (e) => {
    const p = pts.get(e.pointerId); if (!p || !g) return;
    const dx = e.clientX - p.x, dy = e.clientY - p.y;
    p.x = e.clientX; p.y = e.clientY;
    if (Math.hypot(e.clientX - g.sx, e.clientY - g.sy) > 4) g.moved = true;
    if (pts.size === 1) { x += dx; y += dy; clampPan(); schedule(); return; }
    const [a, b] = [...pts.values()], dist = Math.hypot(a.x - b.x, a.y - b.y), mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
    if (g.pinch) { x += mx - g.pinch.mx; y += my - g.pinch.my; set(s * dist / g.pinch.dist, mx, my); }
    g.pinch = { dist, mx, my };
  });
  const up = (e) => {
    if (!pts.delete(e.pointerId) || !g) return;
    if (pts.size) { g.pinch = null; return; }
    stage.classList.remove("dragging");
    const click = e.type === "pointerup" && !g.moved;
    if (click && g.onImg && (e.metaKey || e.ctrlKey)) open_(ext.href);
    else if (click && g.onImg) toggleZoom(e.clientX, e.clientY);
    else if (click && !g.onImg) close();
    g = null;
  };
  const open_ = (u) => window.open(u, "_blank", "noopener");
  stage.addEventListener("pointerup", up); stage.addEventListener("pointercancel", up);
  img.addEventListener("auxclick", (e) => { if (e.button === 1) open_(ext.href); });
  // Click toggles between fit and a close-up at the clicked point.
  function toggleZoom(cx, cy) { const f = fitScale(); if (s <= f * 1.05) set(Math.max(1, f * 2), cx, cy, true); else fit(true); }

  addEventListener("resize", () => { if (!d.open || closing) return; if (fitted) fit(false); else { s = Math.min(maxScale(), Math.max(minScale(), s)); clampPan(); apply(); } });
  addEventListener("popstate", () => { if (d.open) d.close(); });

  return { open, close, isOpen: () => d.open };
}
