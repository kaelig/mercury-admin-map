addEventListener('load', () => setTimeout(() => {
  const h = document.createElement('h2');
  h.id = 'late';
  h.textContent = 'Loaded late';
  document.querySelector('main').append(h);
}, 600));
