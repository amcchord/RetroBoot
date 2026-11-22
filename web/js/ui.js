// Platinum-styled building blocks: windows, buttons, badges, dialogs, toasts.

import { h, uid, copyText } from './util.js';
import { pixelIcon, lineIcon } from './icons.js';
import { friendlyMessage } from './api.js';

/**
 * A Platinum document window: frame, pinstriped title bar with close/zoom/collapse
 * boxes, and a content area.  title may be a string or a node (e.g. the page's h1).
 * boxes: any of 'close' | 'zoom' | 'collapse'; onBox(kind) handles clicks.
 */
export function docWindow({ title, icon, id, className, boxes = [], onBox, labelledBy, headingLevel = 2 }, ...body) {
  const headingId = uid('doc');
  const heading = title instanceof Node ? title : h(`h${headingLevel}`, { id: headingId }, title);
  if (title instanceof Node && !heading.id) heading.id = headingId;
  const titleText = h('div', { class: 'doc-title-text' }, icon ? h('span', { class: 'doc-title-icon', html: pixelIcon(icon, 16) }) : null, heading);
  const boxEls = boxes.map((kind) =>
    h('button', {
      type: 'button',
      class: `title-box ${kind}`,
      'aria-label': { close: 'Close', zoom: 'Zoom', collapse: 'Collapse' }[kind],
      title: { close: 'Close', zoom: 'Zoom', collapse: 'Collapse' }[kind],
      onclick: () => onBox && onBox(kind),
    }),
  );
  const bar = h('div', { class: 'doc-title' }, h('span', { class: 'doc-stripes', 'aria-hidden': 'true' }), titleText, ...boxEls);
  const bodyEl = h('div', { class: 'doc-body' }, ...body);
  const el = h(
    'section',
    { class: ['doc', boxes.includes('zoom') && 'has-zoom', boxes.includes('close') && 'has-close', className], id, 'aria-labelledby': labelledBy || heading.id },
    bar,
    bodyEl,
  );
  el.body = bodyEl;
  el.titleText = titleText;
  snapTitle(el);
  return el;
}

// Mac OS centres a window title with integer math: pen = left + floor((width - title) / 2),
// clearing the pinstripes from 3 px before to 2 px after it.  CSS centring can land on half
// pixels, so the title's offset is set in whole Mac pixels whenever the window resizes.
let titleObserver = null;
function snapTitle(doc) {
  if (!('ResizeObserver' in window)) return;
  if (!titleObserver) {
    titleObserver = new ResizeObserver((entries) => {
      for (const e of entries) {
        const d = e.target.closest('.doc');
        if (!d || !d.titleText) continue;
        const t = d.titleText;
        t.style.marginLeft = '0px';
        const W = d.offsetWidth - 1;
        const tw = t.offsetWidth - 5;
        if (W > 0 && tw > 0) t.style.marginLeft = `${Math.max(0, Math.floor((W - tw) / 2) - 3)}px`;
      }
    });
  }
  titleObserver.observe(doc);
  titleObserver.observe(doc.titleText);
}

/** A Platinum group box (titled, etched); collapsible ones get a disclosure triangle. */
export function win({ title, icon, id, className, actions, collapsible = false, collapsed = false, headingLevel = 2 }, ...body) {
  const headingId = uid('win');
  const bodyEl = h('div', { class: 'win-body', id: `${headingId}-body` }, ...body);
  const heading = h(`h${headingLevel}`, { class: 'win-title-text', id: headingId }, icon ? h('span', { class: 'win-title-icon', html: pixelIcon(icon, 16) }) : null, title);
  const bar = h('div', { class: 'win-title' }, heading);
  if (collapsible) {
    const btn = h('button', {
      type: 'button',
      class: 'shade-box',
      'aria-expanded': String(!collapsed),
      'aria-controls': bodyEl.id,
      'aria-label': `Show or hide ${title}`,
      onclick: () => {
        const open = btn.getAttribute('aria-expanded') !== 'true';
        btn.setAttribute('aria-expanded', String(open));
        bodyEl.hidden = !open;
        el.classList.toggle('is-collapsed', !open);
      },
    });
    bar.append(btn);
    bodyEl.hidden = collapsed;
  }
  const el = h('section', { class: ['win', collapsed && 'is-collapsed', className], id, 'aria-labelledby': headingId }, bar, bodyEl);
  if (actions) bodyEl.append(h('div', { class: 'win-actions' }, ...actions));
  el.body = bodyEl;
  return el;
}

/** Push button. variant: default (ring), danger, plain, quiet; icon: line icon name. */
export function button(label, opts = {}) {
  const { variant, icon, onClick, disabled, type = 'button', title, focusKey, busy, describedBy, ariaLabel, className } = opts;
  const b = h(
    'button',
    {
      type,
      class: ['btn', variant && `btn-${variant}`, busy && 'is-busy', className],
      disabled: disabled || busy,
      title,
      'aria-label': ariaLabel,
      'aria-describedby': describedBy,
      dataset: focusKey ? { focusKey } : undefined,
      onclick: onClick,
    },
    icon ? h('span', { class: 'btn-icon', html: lineIcon(icon, 18) }) : null,
    h('span', { class: 'btn-label' }, label),
  );
  if (busy) b.setAttribute('aria-busy', 'true');
  return b;
}

export function badge(text, kind = 'neutral', title) {
  return h('span', { class: `badge badge-${kind}`, title }, text);
}

export function led(tone) {
  return h('span', { class: `led led-${tone}`, 'aria-hidden': 'true' });
}

/** Determinate when value is a number 0..1, otherwise the classic barber pole. */
export function progressBar(value, label) {
  const determinate = typeof value === 'number' && isFinite(value);
  const pct = determinate ? Math.round(Math.max(0, Math.min(1, value)) * 100) : null;
  const fill = h('span', { class: 'progress-fill' });
  if (determinate) fill.style.setProperty('--pct', String(pct));
  return h(
    'div',
    {
      class: ['progress', !determinate && 'is-indeterminate'],
      role: 'progressbar',
      'aria-label': label || 'Progress',
      'aria-valuemin': determinate ? '0' : null,
      'aria-valuemax': determinate ? '100' : null,
      'aria-valuenow': determinate ? String(pct) : null,
      'aria-valuetext': determinate ? `${pct}%` : 'In progress',
    },
    fill,
  );
}

/** Definition list of [label, value] rows (value may be a node). */
export function kv(rows, className) {
  return h('dl', { class: ['kv', className] }, ...rows.filter(Boolean).map(([k, v]) => h('div', { class: 'kv-row' }, h('dt', null, k), h('dd', null, v == null || v === '' ? '—' : v))));
}

export function notice({ level = 'info', title, message, actions, icon }) {
  const iconName = icon || (level === 'error' ? 'stop' : level === 'warning' ? 'caution' : 'note');
  return h(
    'div',
    { class: `notice notice-${level}`, role: level === 'error' ? 'alert' : 'status' },
    h('span', { class: 'notice-icon', html: pixelIcon(iconName, 32) }),
    h('div', { class: 'notice-text' }, title ? h('strong', { class: 'notice-title' }, title) : null, message ? h('p', null, message) : null, actions && actions.length ? h('div', { class: 'notice-actions' }, ...actions) : null),
  );
}

export function emptyState(text, icon = 'note') {
  return h('div', { class: 'empty' }, h('span', { html: pixelIcon(icon, 32) }), h('p', null, text));
}

export function loading(text = 'Loading…') {
  return h('div', { class: 'loading', role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), h('span', null, text));
}

export function errorState(err, retry) {
  return notice({
    level: err && err.status === 0 ? 'warning' : 'error',
    title: err && err.status === 0 ? "Can't reach RetroBoot" : "Couldn't load this section",
    message: friendlyMessage(err),
    actions: retry ? [button('Try Again', { icon: 'refresh', onClick: retry })] : null,
  });
}

export function copyButton(text, label = 'Copy', opts = {}) {
  const b = button(label, {
    variant: 'quiet',
    icon: 'copy',
    ariaLabel: opts.ariaLabel || `${label} ${opts.what || ''}`.trim(),
    className: opts.compact ? 'btn-small btn-icon-only' : 'btn-small',
    title: opts.compact ? label : undefined,
    onClick: async () => {
      const ok = await copyText(typeof text === 'function' ? text() : text);
      toast(ok ? `Copied${opts.what ? ` ${opts.what}` : ''}.` : "Couldn't copy. Select the text and copy it manually.", { level: ok ? 'ok' : 'warning', timeout: 2500 });
    },
  });
  return b;
}

// ---- Toasts (in the bottom dock)

let toastHost = null;
export function mountToasts(el) {
  toastHost = el;
}

export function toast(message, { level = 'info', timeout = 5000, action } = {}) {
  if (!toastHost) return;
  const close = button('Dismiss', { variant: 'quiet', icon: 'close', className: 'btn-icon-only', ariaLabel: 'Dismiss message' });
  const el = h('div', { class: `toast toast-${level}`, role: level === 'error' ? 'alert' : 'status' }, h('span', { class: 'toast-icon', html: pixelIcon(level === 'error' ? 'stop' : level === 'warning' ? 'caution' : 'note', 24) }), h('p', null, message), action || null, close);
  const remove = () => {
    el.classList.add('is-leaving');
    setTimeout(() => el.remove(), 180);
  };
  close.addEventListener('click', remove);
  toastHost.append(el);
  if (timeout) setTimeout(remove, timeout);
  return remove;
}

// ---- Dialogs (native <dialog>, Platinum alert styling)

/**
 * Show a modal dialog and resolve with the clicked button's value ('' on Escape).
 * buttons: [{label, value, variant, autofocus, needsAck}]
 * acknowledge: text for a checkbox that must be ticked before needsAck buttons enable.
 */
export function dialog({ title, icon = 'note', message, content, buttons, acknowledge, wide = false, onOpen, validate }) {
  return new Promise((resolve) => {
    const titleId = uid('dlg');
    const descId = uid('dlgd');
    const form = h('form', { method: 'dialog', class: 'dlg-form', novalidate: true });
    const ackBox = acknowledge ? h('input', { type: 'checkbox', id: uid('ack') }) : null;
    const btnEls = (buttons || [{ label: 'OK', value: 'ok', variant: 'default' }]).map((b) => {
      const el = h('button', { type: 'submit', value: b.value, class: ['btn', b.variant && `btn-${b.variant}`], autofocus: b.autofocus || null, disabled: b.needsAck && ackBox ? true : null }, b.label);
      if (b.needsAck && ackBox) ackBox.addEventListener('change', () => (el.disabled = !ackBox.checked));
      return el;
    });
    form.append(
      h(
        'div',
        { class: 'dlg-main' },
        h('span', { class: 'dlg-icon', html: pixelIcon(icon, 48) }),
        h(
          'div',
          { class: 'dlg-text' },
          h('h2', { id: titleId, class: 'dlg-title' }, title),
          message ? h('div', { id: descId, class: 'dlg-message' }, ...(Array.isArray(message) ? message : [message]).map((m) => (typeof m === 'string' ? h('p', null, m) : m))) : null,
          content || null,
          ackBox ? h('label', { class: 'check ack', for: ackBox.id }, ackBox, h('span', null, acknowledge)) : null,
        ),
      ),
      h('div', { class: 'dlg-buttons' }, ...btnEls),
    );
    const d = h('dialog', { class: ['dlg', wide && 'dlg-wide'], 'aria-labelledby': titleId, 'aria-describedby': message ? descId : null }, form);
    // Return in a text field activates the primary (last) button rather than the first.
    const primary = btnEls[(buttons || []).findIndex((b) => b.primary)] || btnEls[btnEls.length - 1];
    form.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.target.matches('input:not([type=checkbox]):not([type=radio])')) {
        e.preventDefault();
        if (!primary.disabled) primary.click();
      }
    });
    form.addEventListener('submit', (e) => {
      const value = e.submitter ? e.submitter.value : '';
      if (validate && value !== 'cancel' && validate(value, d) === false) e.preventDefault();
    });
    // Mac OS deactivates the windows behind a modal dialog: plain frames, gray titles.
    const behind = [...document.querySelectorAll('.doc:not(.is-inactive)')];
    d.addEventListener('close', () => {
      if (!document.querySelector('dialog.dlg[open]')) for (const w of behind) w.classList.remove('is-inactive');
      resolve(d.returnValue || '');
      d.remove();
    });
    for (const w of behind) w.classList.add('is-inactive');
    document.body.append(d);
    d.showModal();
    const auto = d.querySelector('[autofocus]');
    if (auto) auto.focus();
    if (onOpen) onOpen(d);
  });
}

export async function confirm({ title, message, confirmLabel = 'OK', cancelLabel = 'Cancel', icon = 'caution', danger = false, acknowledge, content }) {
  const value = await dialog({
    title,
    icon,
    message,
    content,
    acknowledge,
    buttons: [
      { label: cancelLabel, value: 'cancel', variant: danger ? 'default' : null, autofocus: danger },
      { label: confirmLabel, value: 'ok', variant: danger ? 'danger' : 'default', autofocus: !danger, needsAck: !!acknowledge },
    ],
  });
  return value === 'ok';
}

/** Show an API error in a dialog. */
export function errorDialog(title, err) {
  return dialog({ title, icon: 'stop', message: friendlyMessage(err), buttons: [{ label: 'OK', value: 'ok', variant: 'default', autofocus: true }] });
}

/** Wording and tone for the guest state (Starting/Running/Shutting Down/Stopped/Needs attention). */
export function guestStateInfo(status) {
  const g = (status && status.guest) || {};
  const name = g.machine_name || g.system || 'Mac OS';
  switch (g.state) {
    case 'running':
      if (g.responsive === false) {
        return { word: 'Needs attention', tone: 'bad', detail: 'Mac OS has stopped responding.' };
      }
      return { word: 'Running', tone: 'ok', detail: `${name} is running.` };
    case 'starting':
      return { word: 'Starting', tone: 'busy', detail: g.content ? 'Mac OS is starting up.' : 'RetroBoot is launching Mac OS.' };
    case 'stopping':
      return g.detail === 'restart'
        ? { word: 'Restarting', tone: 'busy', detail: 'Mac OS is closing applications and will start again.' }
        : { word: 'Shutting Down', tone: 'busy', detail: 'Mac OS is closing applications and shutting down.' };
    case 'restarting':
      return { word: 'Restarting', tone: 'busy', detail: 'Mac OS will start again shortly.' };
    case 'stopped':
      return { word: 'Stopped', tone: 'off', detail: 'Mac OS is shut down. RetroBoot is still on.' };
    case 'failed':
      return { word: 'Needs attention', tone: 'bad', detail: g.detail || "Mac OS stopped unexpectedly and didn't start again." };
    case undefined:
    case null:
      return { word: 'Checking…', tone: 'off', detail: 'Waiting for RetroBoot.' };
    default:
      return { word: 'Needs attention', tone: 'bad', detail: `RetroBoot reported an unfamiliar state (“${g.state}”).` };
  }
}

export function pageHeader(icon, title, intro) {
  return h(
    'header',
    { class: 'page-head' },
    h('span', { class: 'page-icon', html: pixelIcon(icon, 40) }),
    h('div', null, h('h1', { tabindex: '-1', class: 'page-title' }, title), intro ? h('p', { class: 'page-intro' }, h('span', { class: 'page-intro-text' }, intro)) : null),
  );
}
