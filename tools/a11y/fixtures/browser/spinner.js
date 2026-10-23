document.getElementById('load').addEventListener('click', async () => {
  const out = document.getElementById('out');
  const spinner = document.createElement('p');
  spinner.id = 'spinner';
  spinner.setAttribute('role', 'status');
  spinner.textContent = 'Loading';
  out.replaceChildren(spinner);
  await (await fetch('/__delay/800')).json();
  const h = document.createElement('h2');
  h.id = 'content';
  h.textContent = 'Details loaded';
  out.replaceChildren(h);
});
