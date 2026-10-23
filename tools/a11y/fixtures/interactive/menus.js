const actions = document.getElementById('actions');
const menu = document.getElementById('actions-menu');
const items = [...menu.querySelectorAll('[role=menuitem]')];
const setOpen = (open) => {
  menu.hidden = !open;
  actions.setAttribute('aria-expanded', String(open));
};
actions.addEventListener('click', () => {
  setOpen(menu.hidden);
  if (!menu.hidden) items[0].focus();
});
menu.addEventListener('keydown', (e) => {
  const i = items.indexOf(document.activeElement);
  if (e.key === 'ArrowDown') items[(i + 1) % items.length].focus();
  else if (e.key === 'ArrowUp') items[(i + items.length - 1) % items.length].focus();
  else if (e.key === 'Escape') {
    setOpen(false);
    actions.focus();
  } else if (e.key === 'Tab') setOpen(false);
  else return;
  if (e.key !== 'Tab') e.preventDefault();
});

const exportButton = document.getElementById('export');
const exportMenu = document.getElementById('export-menu');
exportButton.addEventListener('click', () => {
  exportMenu.hidden = !exportMenu.hidden;
  exportButton.setAttribute('aria-expanded', String(!exportMenu.hidden));
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !exportMenu.hidden) {
    exportMenu.hidden = true;
    exportButton.setAttribute('aria-expanded', 'false');
  }
});
