// A small multi-screen app for the recipe interpreter's tests (U4): pages, a menu, a modal, a drawer
// kept in the DOM like the demo's plain <aside>, a form that submits to a toast, a Books-like page
// whose setup lives in memory and is lost on reload, and URLs that redirect.
import config from '/app/config.js';

const ORIGIN2 = document.documentElement.dataset.origin2;
const setup = { books: false }; // in memory only: a reload of /app/books lands on /app/dashboard

const el = (html) => {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
};

function relDate(days) {
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  return `${days} days ago`;
}

const TRANSACTIONS = [
  { merchant: 'Blue Bottle Coffee', days: () => config.daysAgo, amount: '−$4.50', type: 'card', method: 'Card •• 4242' },
  { merchant: 'Delta Air Lines', days: () => 3, amount: '−$412.20', type: 'card', method: 'Card •• 4242' },
  { merchant: 'Acme Payroll', days: () => 4, amount: '−$12,400.00', type: 'transfer', method: 'ACH transfer' },
  { merchant: 'Stripe payout', days: () => 5, amount: '$3,210.75', type: 'deposit', method: 'Deposit' },
];

function frame(active, content) {
  document.body.replaceChildren();
  document.body.append(el(`<header><strong>Acme</strong><button type="button" id="logout">Logout</button></header>`));
  const links = [['Dashboard', '/app/dashboard'], ['Cards', '/app/cards'], ['Transactions', '/app/transactions'], ['Reports', '/app/reports']];
  document.body.append(el(`<nav aria-label="Primary">${links.map(([t, h]) => `<a href="${h}"${t === active ? ' aria-current="page"' : ''}>${t}</a>`).join('')}</nav>`));
  const main = el('<main></main>');
  main.append(...content);
  document.body.append(main, el('<div class="toast" role="status"></div>'));
  document.getElementById('logout').onclick = () => {
    localStorage.setItem('loggedOut', '1');
    location.assign('/app/logged-out');
  };
}

function modal(dialogHtml) {
  const back = el(`<div class="backdrop">${dialogHtml}</div>`);
  document.body.append(back);
  back.querySelectorAll('[data-close]').forEach((b) => { b.onclick = () => back.remove(); });
  return back;
}

function menu(button, label, items, onChoose) {
  const list = el(`<ul role="menu" aria-label="${label}">${items.map((i) => `<li role="menuitem" tabindex="-1">${i}</li>`).join('')}</ul>`);
  button.setAttribute('aria-expanded', 'true');
  button.after(list);
  list.querySelectorAll('[role=menuitem]').forEach((item) => {
    item.onclick = () => {
      list.remove();
      button.setAttribute('aria-expanded', 'false');
      onChoose(item.textContent);
    };
  });
}

function dashboard() {
  const bars = [40, 55, 48, 70, 66, 82, 90].map((h) => `<span style="height:${h}%"></span>`).join('');
  frame('Dashboard', [
    el('<h1>Dashboard</h1>'),
    el(`<div class="toolbar">
      <button type="button" id="move" aria-haspopup="menu" aria-expanded="false">Move money</button>
      <button type="button" class="ghost" id="books">Try Acme Books</button>
      <a href="${ORIGIN2}/help">Help center</a>
    </div>`),
    el(`<section class="panel"><h2>Balance</h2><div class="balance">$250,000.00</div>
      <div class="chart" aria-label="Balance over 7 days">${bars}<div class="tip" role="tooltip">Balance today: $250,000.00</div></div></section>`),
    el('<section class="panel"><h2>Recent activity</h2><p>4 transactions this week.</p></section>'),
  ]);
  const move = document.getElementById('move');
  move.onclick = () => menu(move, 'Move money', ['Send money', 'Transfer', 'Deposit check'], (choice) => {
    if (choice === 'Send money') sendModal();
  });
  document.getElementById('books').onclick = () => {
    setup.books = true;
    history.pushState({}, '', '/app/books');
    render();
  };
}

function sendModal() {
  const back = modal(`<div role="dialog" aria-modal="true" aria-labelledby="send-title" class="wide">
    <h2 id="send-title">Send money</h2>
    <form id="send-form">
      <label for="recipient">Recipient email</label><input id="recipient" type="email">
      <label for="amount">Amount</label><input id="amount" inputmode="decimal">
      <div class="actions"><button type="button" class="ghost" data-close>Cancel</button><button type="submit">Send</button></div>
    </form></div>`);
  back.querySelector('form').onsubmit = (e) => {
    e.preventDefault();
    back.remove();
    const toast = document.querySelector('.toast');
    toast.replaceChildren(el('<span>Payment sent</span>'), el('<button type="button">Dismiss</button>'));
  };
}

function cards() {
  frame('Cards', [
    el('<h1>Cards</h1>'),
    el(`<section class="panel"><h2>Ada Lovelace •• 4242</h2><p>Debit card · Monthly spend $1,204.00 of $5,000.00</p>
      <div class="toolbar"><button type="button" id="edit-limits">Edit limits</button>
      <button type="button" class="ghost" id="freeze">Freeze card</button></div></section>`),
    el('<section class="panel"><h2>Grace Hopper •• 1881</h2><p>Credit card · Monthly spend $310.00 of $2,000.00</p></section>'),
  ]);
  document.getElementById('edit-limits').onclick = () => (config.editLimitsOpens === 'freeze' ? freezeModal() : limitsModal());
  document.getElementById('freeze').onclick = freezeModal;
}

function limitsModal() {
  modal(`<div role="dialog" aria-modal="true" aria-labelledby="limits-title" class="wide">
    <h2 id="limits-title">Edit card limits</h2>
    <p>Limits apply to Ada Lovelace •• 4242.</p>
    <label for="daily">Daily limit</label><input id="daily" value="1,000.00">
    <label for="monthly">Monthly limit</label><input id="monthly" value="5,000.00">
    <label for="atm">ATM limit</label><input id="atm" value="500.00">
    <div class="actions"><button type="button" class="ghost" data-close>Cancel</button><button type="button" data-close>Save</button></div>
  </div>`);
}

function freezeModal() {
  modal(`<div role="dialog" aria-modal="true" aria-labelledby="freeze-title" class="small">
    <h2 id="freeze-title">Freeze card</h2>
    <p>Freezing stops new purchases.</p>
    <div class="actions"><button type="button" class="ghost" data-close>Cancel</button><button type="button" class="danger" data-close>Freeze card</button></div>
  </div>`);
}

function transactions() {
  let filter = null;
  const drawer = el('<aside class="drawer" hidden></aside>');
  const rows = el('<div class="rows"></div>');
  const chip = el('<h2 class="chip" hidden></h2>');
  const filters = el('<button type="button" class="ghost" aria-haspopup="menu" aria-expanded="false">Filters</button>');
  const draw = () => {
    chip.hidden = !filter;
    chip.textContent = filter || '';
    rows.replaceChildren(...TRANSACTIONS.filter((t) => !filter || t.type === 'card').map((t) => {
      const row = el(`<button type="button" class="_row_k3j4h_7"><span>${t.merchant}</span> <span>${config.paidWord} ${relDate(t.days())}</span> <span>${t.amount}</span></button>`);
      row.onclick = () => {
        drawer.replaceChildren(el(`<div><h2>${t.merchant}</h2><dl><dt>Amount</dt><dd>${t.amount}</dd><dt>Method</dt><dd>${t.method}</dd>
          <dt>Status</dt><dd>${config.paidWord}</dd></dl>
          <div class="actions"><button type="button" class="ghost">Download receipt</button><button type="button" data-close>Close</button></div></div>`));
        drawer.querySelector('[data-close]').onclick = () => { drawer.hidden = true; };
        drawer.hidden = false;
      };
      return row;
    }));
  };
  const bar = el('<div class="toolbar"></div>');
  bar.append(filters);
  filters.onclick = () => menu(filters, 'Filters', ['Card payments', 'Transfers', 'Deposits'], (choice) => {
    if (choice === 'Card payments') {
      filter = 'Card payments';
      draw();
    }
  });
  frame('Transactions', [el('<h1>Transactions</h1>'), bar, chip, rows]);
  document.body.append(drawer);
  draw();
}

function reports() {
  frame('Reports', [
    el('<h1>Reports</h1>'),
    el(`<section class="panel"><h2>Statements</h2><div class="toolbar"><button type="button">Profit and loss</button>
      <button type="button" class="ghost">Cash flow</button><button type="button" class="ghost">Export CSV</button></div></section>`),
  ]);
}

function books() {
  frame('Dashboard', [
    el('<h1>Acme Books</h1>'),
    el(`<section class="panel"><h2>Get started with Books</h2><p>Connect your accounts to start bookkeeping.</p>
      <div class="toolbar"><button type="button">Connect accounts</button><button type="button" class="ghost">Import CSV</button></div></section>`),
  ]);
}

function render() {
  const path = location.pathname.replace(/\/+$/, '') || '/';
  const go = (to) => location.replace(to);
  if (path === '/app') return go('/app/dashboard');
  if (path === '/app/dashboard') return dashboard();
  if (path === '/app/cards') return cards();
  if (path === '/app/transactions') return transactions();
  if (path === '/app/reports') return config.reportsRedirect ? go('/app/dashboard') : reports();
  if (path === '/app/books') return setup.books ? books() : go('/app/dashboard');
  document.body.replaceChildren(el('<main><h1>Not found</h1></main>'));
}

window.addEventListener('popstate', render);
render();
