const nav = document.getElementById('nav');
for (const name of ['Home', 'Tasks', 'Transactions', 'Payments', 'Cards', 'Capital', 'Accounts', 'Bill Pay', 'Invoicing', 'Reimbursements', 'Accounting', 'Settings']) {
  const a = document.createElement('a');
  a.href = `#${name.toLowerCase().replace(' ', '-')}`;
  a.textContent = name;
  nav.appendChild(a);
}
const rows = [];
for (let i = 1; i <= 120; i++) {
  const status = i % 3 ? 'Completed' : 'Pending';
  rows.push(`<tr class="row-card"><td><input type="checkbox" aria-label="Select row ${i}"></td>`
    + `<td><a href="#t${i}">Merchant ${i}</a><div class="muted">Card ••${1000 + i}</div></td>`
    + `<td>$${(i * 37.5).toFixed(2)}</td><td class="muted">${status}</td>`
    + `<td><button type="button" class="icon" aria-label="Attach receipt"></button> `
    + `<button type="button" class="more" aria-haspopup="menu" aria-expanded="false">More</button></td></tr>`);
}
document.getElementById('rows').innerHTML = rows.join('');
