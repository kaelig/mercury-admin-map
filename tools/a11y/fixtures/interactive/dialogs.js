// Modal dialogs after the WAI-ARIA APG dialog example, with two variants that each break one rule.
const FOCUSABLE = 'button, input, a[href], [tabindex]:not([tabindex="-1"])';

function openDialog(opener, { title, fields, escape = true, returnFocus = true }) {
  const backdrop = document.createElement('div');
  backdrop.className = 'backdrop';
  const dialog = document.createElement('div');
  dialog.className = 'dialog';
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  dialog.setAttribute('aria-labelledby', 'dialog-title');
  dialog.innerHTML = `<h2 id="dialog-title">${title}</h2>`
    + fields.map((f) => `<label>${f} <input></label>`).join('')
    + '<div><button data-close>Cancel</button><button data-close>Save</button></div>';
  backdrop.append(dialog);
  document.body.append(backdrop);
  const close = () => {
    backdrop.remove();
    if (returnFocus) opener.focus();
  };
  dialog.addEventListener('keydown', (e) => {
    const items = [...dialog.querySelectorAll(FOCUSABLE)];
    const i = items.indexOf(document.activeElement);
    if (e.key === 'Tab' && e.shiftKey && i <= 0) {
      e.preventDefault();
      items.at(-1).focus();
    } else if (e.key === 'Tab' && !e.shiftKey && i === items.length - 1) {
      e.preventDefault();
      items[0].focus();
    } else if (e.key === 'Escape' && escape) {
      e.preventDefault();
      close();
    }
  });
  for (const b of dialog.querySelectorAll('[data-close]')) b.addEventListener('click', close);
  dialog.querySelector(FOCUSABLE).focus();
}

const on = (id, opts) => document.getElementById(id).addEventListener('click', (e) => openDialog(e.currentTarget, opts));
on('address-open', { title: 'Delivery address', fields: ['Street', 'City'] });
on('nickname-open', { title: 'Card nickname', fields: ['Nickname'], escape: false });
on('pin-open', { title: 'Change PIN', fields: ['New PIN'], returnFocus: false });
