// Schema-driven settings: fields, local draft, desired/applied display,
// the changes bar (Apply / Apply and Restart), conflict and validation
// handling, and the timed-confirmation banner.

import { patch, post, asJob, friendlyMessage } from './api.js';
import { h, append, uid, sameValue, plural } from './util.js';
import { store, subscribe, refreshSettings, refreshStatus, pollFast, guestRunning, guestHung } from './store.js';
import { formatValue, validateValue, groupOf, groupLabel, machineFamily, PAGE_TITLES, APPLY_TEXT } from './schema.js';
import { win, button, badge, toast, dialog, confirm, notice, loading, errorState } from './ui.js';
import { pixelIcon, lineIcon } from './icons.js';
import { track, jobOutcome, doneMessage } from './jobs.js';
import { offerForce } from './actions.js';
import { hw } from './words.js';

// ---- Draft state ---------------------------------------------------------

const draft = new Map(); // key -> value (unsaved local edits)
const clientErrors = new Map(); // key -> message
const serverErrors = new Map(); // key -> message (422)
let serverMessage = null;
let base = null; // {revision, desired} snapshot when the draft started
let busy = false;
let busyLabel = '';
let pendingFocus = null;

function emitDraft() {
  for (const fn of draftListeners) fn();
}
const draftListeners = new Set();
export function onDraftChange(fn) {
  draftListeners.add(fn);
  return () => draftListeners.delete(fn);
}

export const draftSize = () => draft.size;
export const hasStaleBase = () => !!(base && store.settings && store.settings.revision !== base.revision);

export function currentValue(key) {
  const m = store.settings;
  if (draft.has(key)) return draft.get(key);
  return m ? m.desired[key] : undefined;
}

export function setDraft(key, value) {
  const m = store.settings;
  if (!m) return;
  if (!base) base = { revision: m.revision, desired: { ...m.desired } };
  if (sameValue(value, m.desired[key])) draft.delete(key);
  else draft.set(key, value);
  serverErrors.delete(key);
  // The server's summary ("Some settings are not valid.") goes once nothing it flagged is left.
  if (!serverErrors.size) serverMessage = null;
  const err = validateValue(m.defs.get(key), value);
  if (err) clientErrors.set(key, err);
  else clientErrors.delete(key);
  if (!draft.size) {
    base = null;
    clientErrors.clear();
  }
  emitDraft();
}

export function discardDraft() {
  draft.clear();
  clientErrors.clear();
  serverErrors.clear();
  serverMessage = null;
  base = null;
  emitDraft();
}

/** Navigate to a setting's page and focus its control. */
export function goToSetting(key) {
  const page = groupOf(key).page;
  pendingFocus = key;
  if (location.hash !== `#/${page}`) location.hash = `#/${page}`;
  else focusPending();
}

export function focusPending() {
  if (!pendingFocus) return;
  const el = document.getElementById(controlId(pendingFocus));
  if (el) {
    pendingFocus = null;
    const row = el.closest('.setting') || el;
    row.scrollIntoView({ block: 'start' });
    el.focus({ preventScroll: true });
  }
}

const controlId = (key) => `ctl-${key.replace(/[^\w-]/g, '-')}`;

subscribe('settings', (m) => {
  // Drop draft entries that now match the saved value (e.g. someone else made the same change).
  for (const [k, v] of draft) if (sameValue(v, m.desired[k]) || !m.defs.has(k)) draft.delete(k);
  if (!draft.size) base = null;
  emitDraft();
});

// ---- Field rendering -----------------------------------------------------

function fieldTags(def, key, m) {
  const tags = [];
  if (!def.available) tags.push(badge('Unavailable', 'off'));
  if (draft.has(key)) tags.push(badge('Edited', 'edited'));
  else if (m.pending.has(key)) tags.push(badge('Pending', 'pending', 'Saved but not in use yet'));
  const confirming = !!(m.pendingConfirm && m.pendingConfirm.keys.includes(key));
  if (confirming) tags.push(badge('Keep or revert', 'warn'));
  if (def.apply === 'guest_restart') tags.push(badge('Mac OS restart', 'quiet', APPLY_TEXT.guest_restart));
  if (def.apply === 'host_reboot') tags.push(badge('RetroBoot restart', 'quiet', APPLY_TEXT.host_reboot));
  if (def.confirm && !confirming) tags.push(badge(`Confirm within ${def.confirm} s`, 'quiet', 'You will be asked to keep this change; otherwise it reverts automatically.'));
  return tags;
}

function stateText(def, key, m) {
  if (!def.available) return null;
  const applied = m.applied[key];
  const appliedLabel = formatValue(def, applied);
  if (draft.has(key)) {
    return `Not saved yet. In use now: ${appliedLabel}.`;
  }
  if (m.pendingConfirm && m.pendingConfirm.keys.includes(key)) {
    return 'In use for now. Keep it or it reverts automatically.';
  }
  if (m.pending.has(key)) {
    const when = def.apply === 'guest_restart' ? 'Takes effect when Mac OS restarts.' : def.apply === 'host_reboot' ? 'Takes effect when RetroBoot restarts.' : 'Waiting to be applied.';
    return `In use now: ${appliedLabel}. ${when}`;
  }
  return null;
}

function warningFor(def, value) {
  if (!def.warningValues) return null;
  const key = typeof value === 'string' ? value : JSON.stringify(value);
  if (def.warningValues[key]) return def.warningValues[key];
  if (def.warningValues['*'] && draft.has(def.key)) return def.warningValues['*'];
  return null;
}

/** Render one setting; returns {el, update}. */
function renderField(key) {
  const ids = { help: uid('help'), state: uid('state'), err: uid('err'), warn: uid('warn') };
  const el = h('div', { class: 'setting', id: `setting-${key.replace(/[^\w-]/g, '-')}` });
  let lastSig = null;
  let control = null; // {root, set(value), setDisabled(bool), focusEl}

  const build = (def, m) => {
    const value = currentValue(key);
    const describedBy = [def.help ? ids.help : null, ids.state, ids.err, ids.warn].filter(Boolean).join(' ');
    const disabled = !def.available || busy;
    const onValue = (v) => {
      setDraft(key, v);
    };
    let labelEl;
    let body;
    if (def.type === 'bool') {
      const input = h('input', { type: 'checkbox', id: controlId(key), checked: !!value, disabled, 'aria-describedby': describedBy, onchange: (e) => onValue(e.target.checked) });
      labelEl = h('label', { class: 'check setting-check', for: input.id }, input, h('span', { class: 'setting-label' }, def.label));
      body = null;
      control = { set: (v) => (input.checked = !!v), setDisabled: (d) => (input.disabled = d), focusEl: input };
    } else if (def.type === 'enum' && def.choices && def.choices.length <= 4 && def.choices.every((c) => c.label.length <= 34)) {
      const name = uid('radio');
      const radios = def.choices.map((c, i) => {
        const input = h('input', { type: 'radio', name, id: i === 0 ? controlId(key) : uid('r'), value: String(i), checked: sameValue(c.value, value), disabled: disabled || c.disabled, onchange: () => onValue(c.value) });
        return { c, input, label: h('label', { class: 'radio', for: input.id }, input, h('span', null, c.label)) };
      });
      const labelId = uid('lbl');
      labelEl = h('span', { class: 'setting-label', id: labelId }, def.label);
      body = h('div', { class: 'radio-row', role: 'radiogroup', 'aria-labelledby': labelId, 'aria-describedby': describedBy }, ...radios.map((r) => r.label));
      control = {
        set: (v) => radios.forEach((r) => (r.input.checked = sameValue(r.c.value, v))),
        setDisabled: (d) => radios.forEach((r) => (r.input.disabled = d || r.c.disabled)),
        focusEl: radios[0] && radios[0].input,
      };
    } else if (def.type === 'enum') {
      const choices = def.choices || [];
      const select = h(
        'select',
        { id: controlId(key), disabled, 'aria-describedby': describedBy, onchange: (e) => onValue(choices[Number(e.target.value)].value) },
        ...choices.map((c, i) => h('option', { value: String(i), selected: sameValue(c.value, value), disabled: c.disabled }, c.label)),
      );
      if (!choices.some((c) => sameValue(c.value, value))) select.prepend(h('option', { value: '', selected: true, disabled: true }, formatValue(def, value)));
      labelEl = h('label', { class: 'setting-label', for: select.id }, def.label);
      body = h('div', { class: 'select-wrap' }, select);
      control = {
        set: (v) => {
          const i = choices.findIndex((c) => sameValue(c.value, v));
          if (i >= 0) select.value = String(i);
        },
        setDisabled: (d) => (select.disabled = d),
        focusEl: select,
      };
    } else if ((def.type === 'int' || def.type === 'number') && def.range) {
      const out = h('output', { class: 'range-value', for: controlId(key), 'aria-hidden': 'true' }, formatValue(def, value));
      const input = h('input', {
        type: 'range',
        id: controlId(key),
        min: String(def.range.min),
        max: String(def.range.max),
        step: String(def.range.step || 'any'),
        value: String(value == null ? def.range.min : value),
        disabled,
        'aria-describedby': describedBy,
        'aria-valuetext': formatValue(def, value),
        oninput: (e) => {
          const v = def.type === 'int' ? Math.round(Number(e.target.value)) : Number(e.target.value);
          out.textContent = formatValue(def, v);
          e.target.setAttribute('aria-valuetext', formatValue(def, v));
          onValue(v);
        },
      });
      labelEl = h('label', { class: 'setting-label', for: input.id }, def.label);
      body = h('div', { class: 'range-wrap' }, h('span', { class: 'range-end', 'aria-hidden': 'true' }, formatValue(def, def.range.min)), input, h('span', { class: 'range-end', 'aria-hidden': 'true' }, formatValue(def, def.range.max)), out);
      control = {
        set: (v) => {
          input.value = String(v);
          out.textContent = formatValue(def, v);
          input.setAttribute('aria-valuetext', formatValue(def, v));
        },
        setDisabled: (d) => (input.disabled = d),
        focusEl: input,
      };
    } else if (key === 'catalog.urls') {
      const asText = (v) => (Array.isArray(v) ? v.join('\n') : '');
      const input = h('textarea', {
        id: controlId(key),
        rows: 4,
        disabled,
        spellcheck: 'false',
        placeholder: 'https://example.com/catalog.json',
        'aria-describedby': describedBy,
        oninput: (e) => onValue(e.target.value.split('\n').map((line) => line.trim()).filter(Boolean)),
      });
      input.value = asText(value);
      labelEl = h('label', { class: 'setting-label', for: input.id }, def.label);
      body = h('div', { class: 'text-wrap' }, input);
      control = {
        set: (v) => {
          if (document.activeElement !== input) input.value = asText(v);
        },
        setDisabled: (d) => (input.disabled = d),
        focusEl: input,
      };
    } else {
      const input = h('input', {
        type: def.type === 'int' || def.type === 'number' ? 'number' : 'text',
        id: controlId(key),
        value: value == null ? '' : String(value),
        maxlength: def.maxLength != null ? String(def.maxLength) : null,
        disabled,
        autocomplete: 'off',
        autocapitalize: 'off',
        spellcheck: 'false',
        'aria-describedby': describedBy,
        oninput: (e) => onValue(def.type === 'int' || def.type === 'number' ? (e.target.value === '' ? '' : Number(e.target.value)) : e.target.value),
      });
      labelEl = h('label', { class: 'setting-label', for: input.id }, def.label);
      body = h('div', { class: 'text-wrap' }, input);
      control = {
        set: (v) => {
          if (document.activeElement !== input) input.value = v == null ? '' : String(v);
        },
        setDisabled: (d) => (input.disabled = d),
        focusEl: input,
      };
    }
    const head = h('div', { class: 'setting-head' }, labelEl, h('span', { class: 'setting-tags' }));
    el.replaceChildren();
    append(el, [
      head,
      body,
      def.help ? h('p', { class: 'setting-help', id: ids.help }, def.help) : null,
      h('p', { class: 'setting-unavailable', hidden: def.available }, h('span', { class: 'mini-icon', html: pixelIcon('note', 16) }), `Unavailable: ${def.reason || ''}`),
      h('p', { class: 'setting-state', id: ids.state, hidden: true }),
      h('p', { class: 'setting-warning', id: ids.warn, hidden: true }),
      h('p', { class: 'setting-error', id: ids.err, hidden: true }),
    ]);
  };

  const update = () => {
    const m = store.settings;
    if (!m) return;
    const def = m.defs.get(key);
    if (!def) {
      el.hidden = true;
      return;
    }
    el.hidden = false;
    // Rebuild only when the definition changes (choices, availability), otherwise patch in place.
    const sig = JSON.stringify([def.type, def.label, def.help, def.choices, def.range, def.available, def.reason]);
    if (sig !== lastSig) {
      build(def, m);
      lastSig = sig;
    } else {
      control.set(currentValue(key));
      control.setDisabled(!def.available || busy);
    }
    const value = currentValue(key);
    el.querySelector('.setting-tags').replaceChildren(...fieldTags(def, key, m));
    const st = stateText(def, key, m);
    const stEl = el.querySelector('.setting-state');
    stEl.hidden = !st;
    stEl.textContent = st || '';
    const warn = warningFor(def, value);
    const wEl = el.querySelector('.setting-warning');
    wEl.hidden = !warn || !draft.has(key);
    wEl.textContent = warn || '';
    const err = serverErrors.get(key) || clientErrors.get(key);
    const eEl = el.querySelector('.setting-error');
    eEl.hidden = !err;
    eEl.textContent = err || '';
    if (control.focusEl) control.focusEl.setAttribute('aria-invalid', err ? 'true' : 'false');
    el.classList.toggle('is-edited', draft.has(key));
    el.classList.toggle('is-pending', !draft.has(key) && m.pending.has(key));
    el.classList.toggle('is-invalid', !!err);
    el.classList.toggle('is-unavailable', !def.available);
  };
  update();
  return { el, update };
}

/**
 * Settings panels for one page: one window per group in schema order.
 * Returns {el, destroy}.
 */
export function settingsPanels(page, { groups: onlyGroups, intro, extras } = {}) {
  const container = h('div', { class: 'settings-panels' }, win({ title: 'Settings', icon: iconForGroup(page) }, loading('Loading settings…')));
  let fields = new Map();
  let lastKeys = null;
  let extraParts = [];
  const render = () => {
    const m = store.settings;
    if (!m) return;
    // List-valued settings have custom editors, supplied per window through `extras`.
    const keys = m.order.filter((k) => groupOf(k).page === page && (!onlyGroups || onlyGroups.includes(groupOf(k).label)) && (m.defs.get(k).type !== 'list' || k === 'catalog.urls') && shownHere(m.defs.get(k)));
    const sig = keys.join('|');
    if (sig !== lastKeys) {
      lastKeys = sig;
      fields = new Map();
      for (const x of extraParts) if (x.destroy) x.destroy();
      extraParts = [];
      const byGroup = new Map();
      for (const k of keys) {
        const g = groupLabel(k, m.defs.get(k));
        if (!byGroup.has(g)) byGroup.set(g, []);
        byGroup.get(g).push(k);
      }
      const wins = [];
      for (const [g, ks] of byGroup) {
        const list = h('div', { class: 'settings-list' });
        for (const k of ks) {
          const f = renderField(k);
          fields.set(k, f);
          list.append(f.el);
        }
        const extra = extras && extras[g] ? extras[g]() : null;
        if (extra) extraParts.push(extra);
        wins.push(win({ title: g, icon: iconForGroup(page) }, intro && wins.length === 0 ? h('p', { class: 'win-intro' }, intro) : null, extra && extra.top ? extra.top : null, list, extra ? extra.el : null));
      }
      container.replaceChildren(...(wins.length ? wins : [notice({ level: 'info', title: 'No settings here yet', message: 'This RetroBoot does not offer settings in this section.' })]));
      focusPending();
    } else {
      for (const f of fields.values()) f.update();
      for (const x of extraParts) if (x.update) x.update();
    }
  };
  render();
  const onError = (err) => {
    if (!store.settings) container.replaceChildren(errorState(err, () => refreshSettings().catch(() => {})));
  };
  const offs = [subscribe('settings', render), subscribe('settings-error', onError), onDraftChange(render)];
  if (!store.settings && store.settingsError) onError(store.settingsError);
  // The router attaches the page synchronously after render; focus a requested field then.
  queueMicrotask(focusPending);
  return {
    el: container,
    destroy: () => {
      offs.forEach((f) => f());
      for (const x of extraParts) if (x.destroy) x.destroy();
    },
  };
}

// Settings for hardware this computer may not have at all (a touchscreen, a tilt
// sensor) are left out rather than shown as unavailable.
const HIDDEN_WITHOUT = new Set(['touch', 'accelerometer']);
function shownHere(def) {
  if (def && def.families && !def.families.includes(startupFamily())) return false;
  return !(def && def.available === false && HIDDEN_WITHOUT.has(def.capability));
}

/** The kind of Mac that starts next (the startup machine as edited here): settings for
 * the other kind (a Power Mac's processor, a 68k Mac's memory) are left out. */
export function startupFamily() {
  const m = store.settings;
  return m ? machineFamily(m.defs, currentValue('machine.active')) : 'powermac';
}

function iconForGroup(page) {
  return { machine: 'machine', display: 'display', sound: 'sound', storage: 'storage', network: 'network' }[page] || 'mac';
}

// ---- Applying --------------------------------------------------------------

function summary() {
  const m = store.settings;
  if (!m) return null;
  const def = (k) => m.defs.get(k);
  const draftKeys = [...draft.keys()].filter((k) => def(k));
  const pendingKeys = [...m.pending].filter((k) => !draft.has(k) && def(k));
  const all = [...draftKeys, ...pendingKeys];
  return {
    m,
    draftKeys,
    pendingKeys,
    restartKeys: all.filter((k) => def(k).apply === 'guest_restart'),
    rebootKeys: all.filter((k) => def(k).apply === 'host_reboot'),
    liveKeys: all.filter((k) => def(k).apply === 'live'),
    pendingLive: pendingKeys.filter((k) => def(k).apply === 'live'),
    invalid: [...new Set([...clientErrors.keys(), ...serverErrors.keys()])],
  };
}

function setBusy(v, label = '') {
  busy = v;
  busyLabel = label;
  emitDraft();
}

async function warnBeforeApply(keys) {
  const m = store.settings;
  const warnings = keys.map((k) => [k, warningFor(m.defs.get(k), draft.get(k))]).filter(([, w]) => w);
  if (!warnings.length) return true;
  return confirm({
    title: 'Before You Apply',
    icon: 'caution',
    message: warnings.map(([k, w]) => `${m.defs.get(k).label}: ${w}`),
    confirmLabel: 'Apply Anyway',
  });
}

/** Save the draft (PATCH) and apply (POST /settings/apply). */
export async function applyChanges({ restart = false } = {}) {
  const s = summary();
  if (!s || busy) return;
  if (s.invalid.some((k) => clientErrors.has(k))) {
    goToSetting(s.invalid[0]);
    toast('Fix the highlighted setting first.', { level: 'warning' });
    return;
  }
  if (!(await warnBeforeApply(s.draftKeys))) return;
  setBusy(true, restart ? 'Applying and restarting…' : 'Applying…');
  try {
    let revision = s.m.revision;
    if (draft.size) {
      try {
        const res = await patch('/api/v1/settings', { revision: base ? base.revision : s.m.revision, changes: Object.fromEntries(draft) });
        revision = res && res.revision != null ? res.revision : revision;
      } catch (err) {
        if (err.status === 409) {
          setBusy(false);
          await resolveConflict();
          return;
        }
        if (err.status === 422) {
          serverErrors.clear();
          serverMessage = err.message;
          for (const [k, msg] of Object.entries(err.fields || {})) serverErrors.set(k, String(msg));
          if (!serverErrors.size) serverMessage = friendlyMessage(err);
          emitDraft();
          const first = [...serverErrors.keys()][0];
          if (first && store.settings.defs.has(first)) goToSetting(first);
          return;
        }
        throw err;
      }
      draft.clear();
      clientErrors.clear();
      serverErrors.clear();
      serverMessage = null;
      base = null;
    }
    let job;
    try {
      job = asJob(await post('/api/v1/settings/apply', { revision, restart }));
    } catch (err) {
      await refreshSettings().catch(() => {});
      if (err.status === 409 && err.code === 'stale-revision') {
        toast('Settings changed elsewhere just now. Your changes were saved; review them and apply again.', { level: 'warning', timeout: 8000 });
        return;
      }
      throw err;
    }
    if (restart) pollFast(30000);
    await refreshSettings().catch(() => {});
    if (job) {
      const final = await track(job, { label: restart ? 'Applying settings and restarting Mac OS' : 'Applying settings' });
      await Promise.all([refreshSettings().catch(() => {}), refreshStatus().catch(() => {})]);
      if (final.state === 'failed' && final.error && final.error.code === 'superseded') {
        // A force action took over the restart; it reports for itself.
      } else if (final.state === 'failed') {
        if (final.result && final.result.force_available) await offerForce('restart', jobOutcome(final));
        else await dialog({ title: "Settings Weren't Fully Applied", icon: 'stop', message: jobOutcome(final), buttons: [{ label: 'OK', value: 'ok', variant: 'default', autofocus: true }] });
      } else if (!(store.settings && store.settings.pendingConfirm)) {
        toast(doneMessage(final, restart && final.result && final.result.restarting ? 'Settings applied and Mac OS restarted.' : 'Settings applied.'), { level: 'ok' });
      }
    }
  } catch (err) {
    toast(friendlyMessage(err), { level: 'error', timeout: 9000 });
  } finally {
    setBusy(false);
  }
}

/** 409 handling: show what changed elsewhere, then keep (rebase) or discard the draft. */
export async function resolveConflict() {
  const before = base ? base.desired : {};
  try {
    await refreshSettings();
  } catch (err) {
    toast(friendlyMessage(err), { level: 'error' });
    return;
  }
  const m = store.settings;
  const changed = Object.keys(m.desired).filter((k) => m.defs.has(k) && k in before && !sameValue(before[k], m.desired[k]));
  const overlap = changed.filter((k) => draft.has(k));
  const list = changed.length
    ? h('ul', { class: 'change-list' }, ...changed.map((k) => h('li', null, h('strong', null, m.defs.get(k).label), `: ${formatValue(m.defs.get(k), before[k])} → ${formatValue(m.defs.get(k), m.desired[k])}`, overlap.includes(k) ? h('span', { class: 'change-mine' }, ` (you changed this to ${formatValue(m.defs.get(k), draft.get(k))})`) : null)))
    : h('p', null, 'The other change may have been undone already.');
  const choice = await dialog({
    title: 'Settings Changed Elsewhere',
    icon: 'caution',
    message: [
      `While you were editing, settings on this RetroBoot were changed from another browser or ${hw().the}.`,
      overlap.length ? `You both changed ${plural(overlap.length, 'setting')}. If you keep your changes, your values replace the other ones.` : 'Your changes affect different settings, so both can be kept.',
    ],
    content: h('div', { class: 'dlg-section' }, h('h3', null, 'Changed elsewhere'), list),
    buttons: [
      { label: 'Discard My Changes', value: 'discard' },
      { label: 'Keep My Changes', value: 'keep', variant: 'default', autofocus: true, primary: true },
    ],
  });
  if (choice === 'discard') {
    discardDraft();
    toast('Your changes were discarded. Showing the latest settings.', { level: 'info' });
  } else if (choice === 'keep') {
    base = { revision: m.revision, desired: { ...m.desired } };
    for (const [k, v] of draft) if (sameValue(v, m.desired[k])) draft.delete(k);
    if (!draft.size) base = null;
    emitDraft();
    toast('Your changes are kept on top of the latest settings. Review them, then apply.', { level: 'info', timeout: 7000 });
  }
}

async function discardPending() {
  const s = summary();
  if (!s || !s.pendingKeys.length) return;
  const ok = await confirm({
    title: 'Discard Pending Changes?',
    icon: 'caution',
    message: `${plural(s.pendingKeys.length, 'saved change')} will be undone. The settings go back to what Mac OS is using now.`,
    confirmLabel: 'Discard Changes',
  });
  if (!ok) return;
  setBusy(true, 'Discarding…');
  try {
    const changes = Object.fromEntries(s.pendingKeys.map((k) => [k, s.m.applied[k]]));
    await patch('/api/v1/settings', { revision: s.m.revision, changes });
    await refreshSettings();
    toast('Pending changes discarded.', { level: 'ok' });
  } catch (err) {
    if (err.status === 409) {
      await refreshSettings().catch(() => {});
      toast('Settings changed elsewhere. Review the pending changes and try again.', { level: 'warning' });
    } else toast(friendlyMessage(err), { level: 'error' });
  } finally {
    setBusy(false);
  }
}

/** Restore the known-good configuration (PATCH back + Apply and Restart). */
export async function restoreKnownGood() {
  const m = store.settings;
  if (!m || !m.knownGood || !m.knownGood.values) return;
  const values = m.knownGood.values;
  const changes = Object.fromEntries(Object.keys(values).filter((k) => m.defs.has(k) && !sameValue(values[k], m.desired[k])).map((k) => [k, values[k]]));
  const keys = Object.keys(changes);
  if (!keys.length) {
    toast('Settings already match the last known working configuration.', { level: 'info' });
    return;
  }
  // A restart is needed only when a restart-class value differs from what Mac OS uses now.
  const needsRestart = keys.some((k) => m.defs.get(k).apply !== 'live' && !sameValue(values[k], m.applied[k])) && guestRunning();
  const ok = await confirm({
    title: 'Restore Known Working Settings?',
    icon: 'caution',
    message: [`${plural(keys.length, 'setting')} will return to the last configuration that started Mac OS successfully (revision ${m.knownGood.revision}).`, needsRestart ? 'Mac OS will be asked to restart cleanly to finish.' : ''].filter(Boolean),
    content: h('ul', { class: 'change-list' }, ...keys.map((k) => h('li', null, h('strong', null, m.defs.get(k).label), `: ${formatValue(m.defs.get(k), m.desired[k])} → ${formatValue(m.defs.get(k), values[k])}`))),
    confirmLabel: needsRestart ? 'Restore and Restart' : 'Restore',
  });
  if (!ok) return;
  discardDraft();
  for (const [k, v] of Object.entries(changes)) draft.set(k, v);
  base = { revision: m.revision, desired: { ...m.desired } };
  emitDraft();
  await applyChanges({ restart: needsRestart });
}

// ---- Changes bar -----------------------------------------------------------

function keyChips(keys, m) {
  return h(
    'ul',
    { class: 'chip-list' },
    ...keys.map((k) => {
      const def = m.defs.get(k);
      const page = groupOf(k).page;
      return h('li', null, h('a', { href: `#/${page}`, class: 'chip', onclick: (e) => (e.preventDefault(), goToSetting(k)), title: `${PAGE_TITLES[page] || ''}` }, def.label));
    }),
  );
}

export function mountChangesBar(el) {
  let expanded = null; // null: automatic (collapsed on phones when only saved changes wait)
  let lastShape = null;
  const render = () => {
    const s = summary();
    const confirming = !!(s && s.m.pendingConfirm);
    if (!s || (!s.draftKeys.length && !s.pendingKeys.length && !serverMessage)) {
      el.hidden = true;
      el.replaceChildren();
      return;
    }
    el.hidden = false;
    const running = guestRunning();
    const restartNeeded = s.restartKeys.length > 0 && running;
    const stale = hasStaleBase();
    const lines = [];
    let title;
    const buttons = [];
    if (s.draftKeys.length) {
      title = `${plural(s.draftKeys.length, 'unsaved change')}`;
      lines.push(keyChips(s.draftKeys, s.m));
      if (restartNeeded) {
        lines.push(h('p', { class: 'bar-note' }, `${s.restartKeys.length === s.draftKeys.length + s.pendingKeys.length ? 'These take' : `${plural(s.restartKeys.length, 'change')} ${s.restartKeys.length === 1 ? 'takes' : 'take'}`} effect only after Mac OS restarts. Apply and Restart asks Mac OS to restart cleanly, as if you chose Restart from the Special menu, so save open documents first.`));
      } else if (s.restartKeys.length && !running) {
        lines.push(h('p', { class: 'bar-note' }, 'Mac OS is shut down, so every change takes effect the next time it starts.'));
      }
      buttons.push(button('Discard', { onClick: () => discardDraft(), disabled: busy, focusKey: 'bar-discard' }));
      if (restartNeeded) {
        buttons.push(button('Apply Without Restarting', { onClick: () => applyChanges({ restart: false }), disabled: busy || confirming, focusKey: 'bar-apply-later' }));
        buttons.push(button(busy ? busyLabel : 'Apply and Restart Mac OS', { variant: 'default', icon: 'restart', onClick: () => applyChanges({ restart: true }), disabled: busy || confirming, busy, focusKey: 'bar-apply-restart' }));
      } else {
        buttons.push(button(busy ? busyLabel : 'Apply', { variant: 'default', onClick: () => applyChanges({ restart: false }), disabled: busy || confirming, busy, focusKey: 'bar-apply' }));
      }
    } else if (s.pendingKeys.length) {
      const waitingRestart = s.pendingKeys.filter((k) => s.m.defs.get(k).apply === 'guest_restart');
      title = waitingRestart.length === s.pendingKeys.length ? `${plural(s.pendingKeys.length, 'change')} waiting for Mac OS to restart` : `${plural(s.pendingKeys.length, 'saved change')} not applied yet`;
      lines.push(keyChips(s.pendingKeys, s.m));
      if (restartNeeded) lines.push(h('p', { class: 'bar-note' }, `Restart Mac OS to use ${s.pendingKeys.length === 1 ? 'it' : 'them'}. Apply and Restart asks Mac OS to restart cleanly; save open documents first.`));
      if (s.rebootKeys.length) lines.push(h('p', { class: 'bar-note' }, `${plural(s.rebootKeys.length, 'change')} take${s.rebootKeys.length === 1 ? 's' : ''} effect the next time RetroBoot itself restarts.`));
      buttons.push(button('Discard Pending', { onClick: discardPending, disabled: busy, focusKey: 'bar-discard-pending' }));
      if (s.pendingLive.length || (!running && s.restartKeys.length)) buttons.push(button(busy ? busyLabel : 'Apply', { variant: restartNeeded ? null : 'default', onClick: () => applyChanges({ restart: false }), disabled: busy || confirming, focusKey: 'bar-apply' }));
      if (restartNeeded) buttons.push(button(busy ? busyLabel : 'Apply and Restart Mac OS', { variant: 'default', icon: 'restart', onClick: () => applyChanges({ restart: true }), disabled: busy || confirming, busy, focusKey: 'bar-apply-restart' }));
    } else {
      title = 'Settings not saved';
    }
    if (serverMessage || s.invalid.length) {
      const m = s.m;
      lines.push(
        h(
          'div',
          { class: 'bar-errors', role: 'alert' },
          h('strong', null, serverMessage || 'Some values need attention.'),
          s.invalid.length ? h('ul', null, ...s.invalid.map((k) => h('li', null, h('a', { href: `#/${groupOf(k).page}`, onclick: (e) => (e.preventDefault(), goToSetting(k)) }, (m.defs.get(k) || { label: k }).label), `: ${serverErrors.get(k) || clientErrors.get(k)}`))) : null,
        ),
      );
    }
    if (confirming) lines.push(h('p', { class: 'bar-note' }, 'Keep or revert the change waiting for confirmation before applying more.'));
    if (stale && s.draftKeys.length) {
      lines.push(h('p', { class: 'bar-note bar-stale' }, 'Settings were also changed elsewhere while you were editing. ', h('a', { href: '#', onclick: (e) => (e.preventDefault(), resolveConflict()) }, 'Review')));
    }
    if (guestHung() && restartNeeded) lines.push(h('p', { class: 'bar-note' }, "Mac OS isn't responding, so a clean restart may not work. You can still save without restarting."));
    const shape = `${s.draftKeys.length > 0}|${s.invalid.length > 0}`;
    if (shape !== lastShape) {
      expanded = null;
      lastShape = shape;
    }
    const collapsed = expanded === null ? !s.draftKeys.length && !s.invalid.length && !serverMessage : !expanded;
    const toggle = h(
      'button',
      { type: 'button', class: 'bar-toggle', 'aria-expanded': String(!collapsed), 'data-focus-key': 'bar-toggle', onclick: () => ((expanded = collapsed), renderKeepingFocus()) },
      h('span', { class: 'visually-hidden' }, collapsed ? 'Show details for ' : 'Hide details for '),
      h('span', { 'aria-hidden': 'true', html: lineIcon(collapsed ? 'up' : 'down', 18) }),
    );
    el.replaceChildren(
      h(
        'div',
        { class: ['bar', collapsed && 'is-collapsed'] },
        h('span', { class: 'bar-icon', html: pixelIcon(restartNeeded ? 'caution' : 'note', 32) }),
        h('div', { class: 'bar-text' }, h('div', { class: 'bar-head' }, h('h2', { class: 'bar-title' }, title), toggle), ...lines),
        h('div', { class: 'bar-buttons' }, ...buttons),
      ),
    );
  };
  // Preserve focus across re-renders of the bar.
  const renderKeepingFocus = () => {
    const key = el.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
    render();
    if (key) {
      const again = el.querySelector(`[data-focus-key="${key}"]`);
      if (again && !again.disabled) again.focus();
    }
  };
  renderKeepingFocus();
  const offs = [
    subscribe('settings', renderKeepingFocus),
    subscribe('status', () => {
      const s = summary();
      if (s && (s.draftKeys.length || s.pendingKeys.length)) renderKeepingFocus();
    }),
    onDraftChange(renderKeepingFocus),
  ];
  return () => offs.forEach((f) => f());
}

// ---- Timed confirmation banner --------------------------------------------

export function mountConfirmBanner(el) {
  let shown = null; // the pendingConfirm we are showing
  let acted = null; // 'keep' | 'revert' when this page pressed a button
  let tick = null;
  let lastAnnounced = null;
  const live = h('p', { class: 'visually-hidden', 'aria-live': 'polite' });

  const stop = () => {
    if (tick) clearInterval(tick);
    tick = null;
  };

  const respond = async (keep) => {
    acted = keep ? 'keep' : 'revert';
    el.querySelectorAll('button').forEach((b) => (b.disabled = true));
    try {
      await post('/api/v1/settings/confirm', { keep });
    } catch (err) {
      if (err.code !== 'no-pending-confirmation') toast(friendlyMessage(err), { level: 'error' });
    }
    await refreshSettings().catch(() => {});
    refreshStatus().catch(() => {});
  };

  const render = () => {
    const m = store.settings;
    const pc = m && m.pendingConfirm;
    if (!pc) {
      if (shown) {
        const lc = m && m.lastConfirm;
        const names = shown.keys.map((k) => (m && m.defs.get(k) ? m.defs.get(k).label : k)).join(', ');
        if (acted === 'keep') toast(`Kept: ${names}.`, { level: 'ok' });
        else if (acted === 'revert') toast(`Reverted: ${names}.`, { level: 'info' });
        else if (lc && lc.outcome === 'kept') toast(`${names} was kept (confirmed on another device or ${hw().the}).`, { level: 'ok' });
        else toast(`${names} wasn't confirmed in time, so RetroBoot went back to the previous setting.`, { level: 'warning', timeout: 10000 });
      }
      shown = null;
      acted = null;
      stop();
      el.hidden = true;
      el.replaceChildren();
      return;
    }
    const same = shown && JSON.stringify(shown.keys) === JSON.stringify(pc.keys) && shown.revision === pc.revision;
    shown = pc;
    if (same && el.childElementCount) return;
    acted = null;
    lastAnnounced = null;
    live.textContent = '';
    const rows = pc.keys.map((k) => {
      const def = m.defs.get(k);
      const now = pc.values[k] !== undefined ? pc.values[k] : m.applied[k];
      const was = pc.previous[k];
      return h('li', null, h('strong', null, def ? def.label : k), `: ${formatValue(def, now)}`, was !== undefined ? h('span', { class: 'was' }, ` — was ${formatValue(def, was)}`) : null);
    });
    const count = h('span', { class: 'countdown-num' }, '');
    const isDisplay = pc.keys.some((k) => k.startsWith('display.'));
    el.hidden = false;
    el.replaceChildren(
      h(
        'div',
        { class: 'confirm-banner', role: 'alertdialog', 'aria-labelledby': 'confirm-title', 'aria-describedby': 'confirm-desc' },
        h('span', { class: 'bar-icon', html: pixelIcon('caution', 32) }),
        h(
          'div',
          { class: 'bar-text' },
          h('h2', { class: 'bar-title', id: 'confirm-title' }, isDisplay ? 'Keep the new display setting?' : 'Keep this change?'),
          h('ul', { class: 'confirm-list' }, ...rows),
          h('p', { id: 'confirm-desc', class: 'bar-note' }, isDisplay ? `Check ${hw().its} ${hw().display === 'display' ? 'display' : 'screen'}. If it looks wrong or stays blank, do nothing: ` : 'If you lose access, do nothing: ', 'RetroBoot reverts in ', count, '.'),
        ),
        h('div', { class: 'bar-buttons' }, button('Revert Now', { onClick: () => respond(false), focusKey: 'confirm-revert' }), button('Keep Change', { variant: 'default', icon: 'check', onClick: () => respond(true), focusKey: 'confirm-keep' })),
        live,
      ),
    );
    const update = () => {
      const left = pc.localDeadline ? Math.max(0, Math.ceil((pc.localDeadline - Date.now()) / 1000)) : null;
      count.textContent = left == null ? 'a few seconds' : left === 0 ? 'a moment' : `${left} second${left === 1 ? '' : 's'}`;
      if (left != null && (left === 10 || left === 5) && lastAnnounced !== left) {
        lastAnnounced = left;
        live.textContent = `${left} seconds left to keep the change.`;
      }
      if (left === 0) {
        stop();
        setTimeout(() => refreshSettings().catch(() => {}), 600);
        setTimeout(() => refreshSettings().catch(() => {}), 2500);
      }
    };
    stop();
    update();
    tick = setInterval(update, 250);
  };
  const off = subscribe('settings', render);
  render();
  return () => {
    off();
    stop();
  };
}

// Warn before leaving the page with unsaved edits (they live only in this tab).
window.addEventListener('beforeunload', (e) => {
  if (draft.size) {
    e.preventDefault();
    e.returnValue = '';
  }
});
