const $ = (id) => document.getElementById(id);
const toast = (text) => {
  const t = document.createElement('div');
  t.className = 'toast';
  t.textContent = text;
  return t;
};

$('save').addEventListener('click', () => $('status').append(toast('Changes saved')));
$('send').addEventListener('click', () => {
  const live = document.createElement('div');
  live.className = 'toasts';
  live.style.top = '64px';
  live.setAttribute('aria-live', 'polite');
  live.append(toast('Payment sent'));
  document.body.append(live);
});
$('archive').addEventListener('click', () => {
  const t = toast('Card archived ');
  t.classList.add('alone');
  t.tabIndex = -1;
  const undo = document.createElement('button');
  undo.textContent = 'Undo';
  t.append(undo);
  document.body.append(t);
  t.focus();
});
$('copy').addEventListener('click', () => {
  const t = toast('Account number copied');
  $('status').append(t);
  setTimeout(() => t.remove(), 1000);
});
