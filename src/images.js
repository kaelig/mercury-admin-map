// Level-of-detail image store. Every frame the renderer asks for the image each visible card
// should show; missing ones are queued (closest to the viewport centre first), decoded off the
// main thread with img.decode(), and evicted least-recently-used per level so decoded-image
// memory stays bounded however far the user zooms.
const BUDGET = { mid: 360, thumb: 90, full: 8 };
const MAX_INFLIGHT = { mid: 6, thumb: 4, full: 2 };

export class ImageStore {
  constructor(onReady) {
    this.onReady = onReady;
    this.cache = new Map(); // url -> {img, level, ready, used}
    this.wants = [];
    this.inflight = { mid: 0, thumb: 0, full: 0 };
    this.frame = 0;
  }
  // Returns a decoded image or null; always marks the entry as used this frame.
  get(url) {
    const e = this.cache.get(url);
    if (!e) return null;
    e.used = this.frame;
    return e.ready ? e.bmp : null;
  }
  want(url, level, prio) {
    const e = this.cache.get(url);
    if (e) { e.used = this.frame; return; }
    this.wants.push({ url, level, prio });
  }
  // Called once at the end of each frame.
  pump() {
    if (this.wants.length) {
      this.wants.sort((a, b) => a.prio - b.prio);
      for (const w of this.wants) {
        if (this.inflight[w.level] >= MAX_INFLIGHT[w.level] || this.cache.has(w.url)) continue;
        this.load(w.url, w.level);
      }
      this.wants.length = 0;
    }
    this.evict();
    this.frame++;
  }
  load(url, level) {
    const img = new Image();
    img.decoding = "async";
    const e = { img, level, ready: false, used: this.frame };
    this.cache.set(url, e);
    this.inflight[level]++;
    img.src = url;
    img.decode().then(() => createImageBitmap(img)).then((bmp) => { e.bmp = bmp; e.ready = true; img.src = ""; }, () => { e.failed = true; }).finally(() => {
      this.inflight[level]--;
      if (this.cache.get(url) === e) this.onReady();
    });
  }
  evict() {
    const count = { mid: 0, thumb: 0, full: 0 };
    for (const e of this.cache.values()) count[e.level]++;
    for (const level of Object.keys(BUDGET)) {
      if (count[level] <= BUDGET[level]) continue;
      const old = [...this.cache].filter(([, e]) => e.level === level && e.used < this.frame && e.ready).sort((a, b) => a[1].used - b[1].used);
      for (const [url, e] of old.slice(0, count[level] - BUDGET[level])) { e.bmp?.close(); this.cache.delete(url); }
    }
  }
}
