const add = (html) => { const d = document.createElement('div'); d.innerHTML = html; document.body.appendChild(d); return d; };
document.getElementById('open-modal').onclick = () => add('<div role="dialog" aria-modal="true" aria-labelledby="mt"><h2 id="mt">Edit card limits</h2><div role="listbox" aria-label="Accounts"><div role="option">A</div></div><button>Save</button></div>');
document.getElementById('open-drawer').onclick = () => add('<aside class="drawer"><h2>Transaction details</h2><button>Close</button></aside>');
document.getElementById('open-menu').onclick = (e) => { e.target.setAttribute('aria-expanded', 'true'); add('<div role="menu" class="menu" aria-label="Move money"><div role="menuitem">Send</div><div role="menuitem">Transfer</div></div>'); };
document.getElementById('toast').onclick = () => { add('<div style="position:fixed;top:10px;right:10px">Card saved</div>'); };
document.getElementById('show-aside').onclick = () => { document.getElementById('details').hidden = false; };
