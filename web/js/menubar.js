// The Platinum menu bar: pull-down menus with the WAI-ARIA menubar keyboard model.
//
// menus: [{ id, title (string or node), label?, align?: 'right', items: () => [item] }]
// item: { label, icon?, onSelect?, href?, disabled?, checked?, key? } or 'separator'.
// Items are rebuilt each time a menu opens, so they always reflect current state.

import { h } from './util.js';
import { pixelIcon } from './icons.js';

export function menubar(menus, { right = [] } = {}) {
  const bar = h('nav', { class: 'menubar', role: 'menubar', 'aria-label': 'Menu bar' });
  const rightBox = h('div', { class: 'mb-right' });
  const entries = [];
  let open = null;

  function close({ focusTitle = false } = {}) {
    if (!open) return;
    const e = open;
    open = null;
    e.title.setAttribute('aria-expanded', 'false');
    e.panel.hidden = true;
    e.panel.replaceChildren();
    if (focusTitle) e.title.focus();
  }

  function itemsOf(e) {
    return [...e.panel.querySelectorAll('[role^="menuitem"]:not([aria-disabled="true"])')];
  }

  function openMenu(e, { focus = null } = {}) {
    if (open && open !== e) close();
    open = e;
    e.title.setAttribute('aria-expanded', 'true');
    e.panel.replaceChildren(...build(e));
    e.panel.hidden = false;
    const items = itemsOf(e);
    if (focus === 'first' && items[0]) items[0].focus();
    if (focus === 'last' && items.length) items[items.length - 1].focus();
  }

  function build(e) {
    return e.def.items().map((it) => {
      if (it === 'separator') return h('hr', { role: 'separator' });
      const role = it.checked != null ? 'menuitemradio' : 'menuitem';
      const attrs = {
        role,
        tabindex: '-1',
        'aria-disabled': it.disabled ? 'true' : null,
        'aria-checked': it.checked != null ? String(!!it.checked) : null,
        onclick: (ev) => {
          if (it.disabled) {
            ev.preventDefault();
            return;
          }
          close();
          if (it.onSelect) {
            ev.preventDefault();
            it.onSelect();
          }
        },
      };
      const content = [it.icon ? h('span', { class: 'mi-icon', html: pixelIcon(it.icon, 16) }) : null, h('span', null, it.label), it.key ? h('span', { class: 'menu-key' }, it.key) : null];
      return it.href && !it.disabled ? h('a', { ...attrs, href: it.href }, ...content) : h('button', { ...attrs, type: 'button' }, ...content);
    });
  }

  menus.forEach((def, index) => {
    const panelId = `menu-${def.id}`;
    const title = h(
      'button',
      {
        type: 'button',
        class: ['mb-title', def.className],
        role: 'menuitem',
        'aria-haspopup': 'menu',
        'aria-expanded': 'false',
        'aria-controls': panelId,
        'aria-label': def.label || null,
        tabindex: index === 0 ? '0' : '-1',
      },
      def.title,
    );
    const panel = h('div', { class: 'menu', id: panelId, role: 'menu', 'aria-label': def.label || (typeof def.title === 'string' ? def.title : null), hidden: true });
    const wrap = h('div', { class: 'mb-menu', role: 'none' }, title, panel);
    const e = { def, title, panel, wrap };
    entries.push(e);
    (def.align === 'right' ? rightBox : bar).append(wrap);
    if (def.align === 'right' && !rightBox.isConnected) bar.append(rightBox);

    title.addEventListener('mousedown', (ev) => {
      ev.preventDefault();
      if (open === e) close();
      else openMenu(e);
      title.focus();
    });
    title.addEventListener('click', (ev) => {
      // Keyboard activation (Enter/Space) fires click without mousedown.
      if (ev.detail === 0) {
        if (open === e) close();
        else openMenu(e, { focus: 'first' });
      }
    });
    title.addEventListener('mouseenter', () => {
      if (open && open !== e) openMenu(e);
    });
    title.addEventListener('keydown', (ev) => {
      const i = entries.indexOf(e);
      if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
        ev.preventDefault();
        const next = entries[(i + (ev.key === 'ArrowRight' ? 1 : entries.length - 1)) % entries.length];
        roving(next);
        if (open) openMenu(next);
      } else if (ev.key === 'ArrowDown') {
        ev.preventDefault();
        openMenu(e, { focus: 'first' });
      } else if (ev.key === 'ArrowUp') {
        ev.preventDefault();
        openMenu(e, { focus: 'last' });
      } else if (ev.key === 'Escape') {
        close();
      }
    });
    panel.addEventListener('keydown', (ev) => {
      const items = itemsOf(e);
      const k = items.indexOf(document.activeElement);
      if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
        ev.preventDefault();
        if (!items.length) return;
        const n = ev.key === 'ArrowDown' ? (k + 1) % items.length : (k - 1 + items.length) % items.length;
        items[n].focus();
      } else if (ev.key === 'Home' || ev.key === 'End') {
        ev.preventDefault();
        (ev.key === 'Home' ? items[0] : items[items.length - 1])?.focus();
      } else if (ev.key === 'Escape') {
        ev.preventDefault();
        close({ focusTitle: true });
      } else if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
        ev.preventDefault();
        const i = entries.indexOf(e);
        const next = entries[(i + (ev.key === 'ArrowRight' ? 1 : entries.length - 1)) % entries.length];
        roving(next);
        openMenu(next, { focus: 'first' });
      } else if (ev.key === 'Tab') {
        close();
      }
    });
    panel.addEventListener('mouseover', (ev) => {
      const item = ev.target.closest('[role^="menuitem"]');
      if (item && item.getAttribute('aria-disabled') !== 'true') item.focus({ preventScroll: true });
    });
  });

  function roving(e) {
    for (const x of entries) x.title.tabIndex = x === e ? 0 : -1;
    e.title.focus();
  }

  for (const node of right) rightBox.append(node);
  if (!rightBox.isConnected) bar.append(rightBox);

  const onDoc = (ev) => {
    if (open && !open.wrap.contains(ev.target)) close();
  };
  document.addEventListener('mousedown', onDoc);
  bar.destroy = () => document.removeEventListener('mousedown', onDoc);
  bar.close = close;
  return bar;
}
