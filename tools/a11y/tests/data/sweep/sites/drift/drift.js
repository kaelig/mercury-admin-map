// A settings dialog with five sections of two items each (fifteen states behind one trigger), and
// twelve unrelated panels (twelve sibling triggers). config.js names the triggers.
const { settingsWord, panelWord } = window.DRIFT;
const triggers = document.getElementById('triggers');

function button(text, onClick, parent = triggers) {
  const b = document.createElement('button');
  b.textContent = text;
  b.addEventListener('click', onClick);
  parent.append(b);
  return b;
}

function dialog(title, fill) {
  const backdrop = document.createElement('div');
  backdrop.className = 'backdrop';
  const d = document.createElement('div');
  d.className = 'dialog';
  d.setAttribute('role', 'dialog');
  d.setAttribute('aria-modal', 'true');
  d.setAttribute('aria-label', title);
  d.append(Object.assign(document.createElement('h2'), { textContent: title }));
  fill(d);
  button('Close', () => backdrop.remove(), d);
  backdrop.append(d);
  document.body.append(backdrop);
  d.querySelector('button').focus();
}

button(`${settingsWord} settings`, () => dialog('Settings', (d) => {
  for (let k = 1; k <= 5; k++) {
    const section = document.createElement('div');
    button(`Section ${k}`, () => {
      section.querySelector('[role=group]')?.remove();
      const group = document.createElement('div');
      group.setAttribute('role', 'group');
      group.setAttribute('aria-label', `Section ${k}`);
      for (const x of ['a', 'b']) button(`Item ${k}${x}`, () => { group.dataset.picked = x; }, group);
      section.append(group);
    }, section);
    d.append(section);
  }
}));

for (let n = 1; n <= 12; n++) {
  button(`${panelWord} panel ${n}`, () => dialog(`Panel ${n}`, (d) => {
    const p = document.createElement('p');
    p.textContent = `Details for panel ${n}.`;
    d.append(p);
  }));
}
