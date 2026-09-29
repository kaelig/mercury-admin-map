(() => {
  const ctxFor = (e, t) => { let b = e.parentElement; while (b && b !== document.body && (b.innerText || '').replace(/\s+/g,' ').trim().length < t.length + 25) b = b.parentElement; let c = (b?.innerText || '').replace(/\s+/g, ' ').trim(); const i = c.indexOf(t); if (c.length > 300) c = c.slice(Math.max(0, i - 200), i + t.length + 60); return c; };
  const hits = [];
  for (const e of document.querySelectorAll('a[href], [role=link], button, [role=button], [role=menuitem], [role=tab], [role=option]')) {
    if (!e.getClientRects().length || getComputedStyle(e).visibility === 'hidden') continue;
    const t = (e.innerText || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 200) continue;
    if (!/[^.]\.$/.test(t) && !/[,;:!?]$/.test(t)) continue;
    const r = e.getBoundingClientRect();
    hits.push({ tag: e.tagName.toLowerCase(), role: e.getAttribute('role'), href: e.getAttribute('href'), text: t, display: getComputedStyle(e).display, ctx: ctxFor(e, t), dialog: !!e.closest('[role=dialog], [aria-modal=true]'), box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)] });
  }
  return JSON.stringify({ at: location.pathname + location.search + location.hash, h1: (document.querySelector('h1,h2')?.innerText || '').slice(0, 80), len: document.body.innerText.length, hits });
})()
