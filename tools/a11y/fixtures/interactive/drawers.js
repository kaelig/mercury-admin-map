// Non-modal drawers that open without moving focus: the opener blurs, so focus falls to the body.
// Esc closes a drawer and returns focus to its opener.
function openDrawer(opener, { label, body }) {
  const drawer = document.createElement('div');
  drawer.className = 'drawer';
  drawer.setAttribute('role', 'dialog');
  drawer.setAttribute('aria-label', label);
  drawer.innerHTML = `<h2>${label}</h2>${body}`;
  document.body.append(drawer);
  opener.blur();
  const onKey = (e) => {
    if (e.key !== 'Escape') return;
    drawer.remove();
    document.removeEventListener('keydown', onKey);
    opener.focus();
  };
  document.addEventListener('keydown', onKey);
}

const on = (id, opts) => document.getElementById(id).addEventListener('click', (e) => openDrawer(e.currentTarget, opts));
on('limits-open', { label: 'Card limits', body: '<label>Daily limit <input></label><button>Save limits</button>' });
on('address-open', { label: 'Billing address', body: '<div class="row">Street</div><div class="row">Save address</div>' });
on('payment-open', { label: 'Payment details', body: '<button>Close</button>' });
on('settings-open', { label: 'Card settings', body: '<button>Close</button>' });
