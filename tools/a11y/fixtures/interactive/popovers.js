const $ = (id) => document.getElementById(id);

$('fees-wrap').addEventListener('mouseenter', () => { $('fees-tip').hidden = false; });
$('fees-wrap').addEventListener('mouseleave', () => { $('fees-tip').hidden = true; });

const art = $('art-wrap');
const show = () => { $('art-pop').hidden = false; };
const hide = () => { $('art-pop').hidden = true; };
art.addEventListener('mouseenter', show);
art.addEventListener('mouseleave', () => { if (!art.contains(document.activeElement)) hide(); });
art.addEventListener('focusin', show);
art.addEventListener('focusout', (e) => { if (!art.contains(e.relatedTarget)) hide(); });

$('filters').addEventListener('click', (e) => {
  const panel = $('filters-panel');
  panel.hidden = !panel.hidden;
  e.currentTarget.setAttribute('aria-expanded', String(!panel.hidden));
});
