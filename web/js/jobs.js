// Long-running job tracking (GET /api/v1/jobs/{id}) and the Activity tray.

import { get, friendlyMessage, ApiError } from './api.js';
import { h } from './util.js';
import { button, progressBar } from './ui.js';
import { pixelIcon } from './icons.js';
import { emit, subscribe } from './store.js';

export const KIND_LABELS = {
  'guest.start': 'Starting Mac OS',
  'guest.restart': 'Restarting Mac OS',
  'guest.shutdown': 'Shutting down Mac OS',
  'guest.force-stop': 'Force shutting down Mac OS',
  'guest.force-restart': 'Force restarting Mac OS',
  'host.poweroff': 'Turning off RetroBoot',
  'host.reboot': 'Restarting RetroBoot',
  'settings.apply': 'Applying settings',
  'machines.select': 'Choosing the startup machine',
  'guest.cd-start': 'Starting up from a CD',
  'machines.import': 'Importing a machine',
  'machines.remove': 'Removing a Mac',
  'checkpoint.create': 'Creating a checkpoint',
  'checkpoint.restore': 'Restoring a checkpoint',
  'graphics.install': 'Installing ATI graphics software',
  'diagnostics.bundle': 'Collecting evidence',
  'wifi.scan': 'Scanning for Wi-Fi networks',
  'wifi.connect': 'Joining a Wi-Fi network',
  'installer.install': 'Installing RetroBoot',
  'installer.downloads': 'Installing Macs',
  'installer.signin': 'Signing in to RetroBoot downloads',
  'update.install': 'Installing an update',
  'update.check': 'Checking for updates',
  'update.restart': 'Restarting to update',
  'update.rollback': 'Going back to the previous version',
};

const ACTIVE = new Set(['queued', 'running']);
// Jobs whose last step restarts or turns off RetroBoot (the job is gone afterwards).
const RESTARTS = new Set(['host.reboot', 'host.poweroff', 'update.restart', 'update.rollback']);
const entries = new Map(); // id -> {job, label, waiters, dismissed, finishedAt, quiet}
let timer = null;
let trayEl = null;

export const isActive = (job) => job && ACTIVE.has(job.state);
export const labelFor = (job) => (job && (KIND_LABELS[job.kind] || job.label)) || 'Working';

/** Track a job until it finishes. Resolves with the final job object (never rejects). */
export function track(job, { label, quiet = false } = {}) {
  let entry = entries.get(job.id);
  if (!entry) {
    entry = { job, label: label || labelFor(job), waiters: [], dismissed: false, finishedAt: null, quiet };
    entries.set(job.id, entry);
  } else {
    entry.job = job;
    if (label) entry.label = label;
    if (!quiet) entry.quiet = false;
  }
  const done = new Promise((resolve) => {
    if (!isActive(entry.job)) resolve(entry.job);
    else entry.waiters.push(resolve);
  });
  if (!isActive(job)) finish(entry);
  changed();
  ensurePolling();
  return done;
}

/** Adopt jobs reported by status (started elsewhere or before a reload). */
export function adopt(list) {
  if (!Array.isArray(list)) return;
  for (const job of list) if (job && job.id && isActive(job) && !entries.has(job.id)) track(job);
}

export function activeJobs(prefix) {
  return [...entries.values()].filter((e) => isActive(e.job) && (!prefix || String(e.job.kind || '').startsWith(prefix))).map((e) => e.job);
}

/** The running job that owns Mac OS's lifecycle, if any (guest.* or a host power request). */
export function lifecycleJob() {
  return activeJobs('guest.')[0] || activeJobs('host.')[0] || null;
}

/** A running force action or host power request: nothing else may start while one runs. */
export function forceOrPowerJob() {
  return activeJobs('guest.force-')[0] || activeJobs('host.')[0] || null;
}

/**
 * The job's message when it reads as an outcome. A finished job can still carry
 * its last progress step ("Starting Mac OS…"), which would be misleading as a
 * success message, so those (and a bare "Done.") fall back to our own wording.
 */
export function doneMessage(job, fallback) {
  const m = job && job.message ? String(job.message).trim() : '';
  if (!m || /(…|\.\.\.)$/.test(m) || /^done\.?$/i.test(m)) return fallback;
  return m;
}

// A forced action finishes what its clean request started.
const family = (kind) => ({ 'guest.force-restart': 'guest.restart', 'guest.force-stop': 'guest.shutdown' })[kind] || kind;

function finish(entry) {
  if (!entry.finishedAt) entry.finishedAt = Date.now();
  const waiters = entry.waiters.splice(0);
  for (const w of waiters) w(entry.job);
  emit('job-finished', entry.job);
  // Once an action succeeds, earlier failed attempts at it are no longer news.
  if (entry.job.state === 'succeeded' && entry.job.kind) {
    for (const [id, e] of entries) if (e !== entry && e.job.state === 'failed' && family(e.job.kind) === family(entry.job.kind)) entries.delete(id);
  }
  // Successes are announced by a toast from the caller; failures stay until dismissed.
  if (entry.job.state === 'succeeded') setTimeout(() => dismiss(entry.job.id), entry.quiet ? 0 : 2500);
}

export function dismiss(id) {
  const e = entries.get(id);
  if (!e) return;
  if (isActive(e.job)) {
    e.dismissed = true;
  } else {
    entries.delete(id);
  }
  changed();
}

function changed() {
  emit('jobs');
  renderTray();
}

function ensurePolling() {
  if (timer) return;
  const tick = async () => {
    timer = null;
    const active = [...entries.values()].filter((e) => isActive(e.job));
    if (!active.length) return;
    await Promise.all(
      active.map(async (e) => {
        try {
          const job = await get(`/api/v1/jobs/${encodeURIComponent(e.job.id)}`, { timeout: 8000 });
          e.job = job;
          if (!isActive(job)) finish(e);
        } catch (err) {
          if (err instanceof ApiError && err.status === 404 && RESTARTS.has(e.job.kind)) {
            // The job ended by restarting RetroBoot: the new boot doesn't know it, as expected.
            e.job = { ...e.job, state: 'succeeded', progress: 1, message: 'RetroBoot restarted.', error: null };
            finish(e);
          } else if (err instanceof ApiError && err.status === 404) {
            e.job = { ...e.job, state: 'failed', message: 'RetroBoot no longer has this job. It may have restarted; check the current state.', error: { code: 'job-lost' } };
            finish(e);
          }
          // Offline: keep waiting; the connectivity banner explains.
        }
      }),
    );
    changed();
    if ([...entries.values()].some((e) => isActive(e.job))) timer = setTimeout(tick, document.visibilityState === 'visible' ? 1000 : 4000);
  };
  timer = setTimeout(tick, 700);
}

// ---- Tray

export function mountTray(el) {
  trayEl = el;
  renderTray();
}

function jobRow(e) {
  const job = e.job;
  const active = isActive(job);
  const failed = job.state === 'failed';
  const icon = active ? null : failed ? 'stop' : 'note';
  const message = failed && job.error && job.error.message && job.error.message !== job.message ? job.error.message : active || failed ? job.message : doneMessage(job, 'Done.');
  return h(
    'li',
    { class: ['job', failed && 'job-failed', !active && !failed && 'job-done'] },
    h(
      'div',
      { class: 'job-head' },
      icon ? h('span', { class: 'job-icon', html: pixelIcon(icon, 16) }) : h('span', { class: 'spinner spinner-small', 'aria-hidden': 'true' }),
      h('strong', { class: 'job-label' }, e.label),
      active ? null : button('Dismiss', { variant: 'quiet', icon: 'close', className: 'btn-icon-only', ariaLabel: `Dismiss ${e.label}`, onClick: () => dismiss(job.id) }),
    ),
    message ? h('p', { class: 'job-message' }, message) : null,
    active ? progressBar(typeof job.progress === 'number' ? job.progress : null, e.label) : null,
  );
}

const rows = new Map(); // id -> {el, sig}
let listEl = null;
let liveEl = null;

function renderTray() {
  if (!trayEl) return;
  const visible = [...entries.values()].filter((e) => !e.quiet && !e.dismissed);
  trayEl.hidden = visible.length === 0;
  if (!listEl) {
    listEl = h('ul', { class: 'job-list' });
    liveEl = h('p', { class: 'visually-hidden', 'aria-live': 'polite' });
    trayEl.append(h('div', { class: 'tray-title' }, h('span', { class: 'stripes', 'aria-hidden': 'true' }), h('h2', null, 'Activity'), h('span', { class: 'stripes', 'aria-hidden': 'true' })), listEl, liveEl);
  }
  const keep = new Set(visible.map((e) => e.job.id));
  for (const [id, r] of rows) {
    if (!keep.has(id)) {
      r.el.remove();
      rows.delete(id);
    }
  }
  for (const e of visible) {
    const job = e.job;
    const sig = JSON.stringify([job.state, job.message, e.label, typeof job.progress === 'number']);
    const r = rows.get(job.id);
    if (r && r.sig === sig) {
      // Only progress moved: update the bar in place so animations don't restart.
      const bar = r.el.querySelector('.progress');
      if (bar && typeof job.progress === 'number') {
        const pct = Math.round(job.progress * 100);
        bar.querySelector('.progress-fill').style.setProperty('--pct', String(pct));
        bar.setAttribute('aria-valuenow', String(pct));
        bar.setAttribute('aria-valuetext', `${pct}%`);
      }
      continue;
    }
    const el = jobRow(e);
    if (r) r.el.replaceWith(el);
    else listEl.append(el);
    if (r && r.sig && !isActive(job)) liveEl.textContent = `${e.label}: ${job.state === 'failed' ? 'failed. ' : ''}${job.message || ''}`;
    rows.set(job.id, { el, sig });
  }
}

/** Convenience: human message for a finished job. */
export function jobOutcome(job) {
  if (!job) return '';
  if (job.state === 'succeeded') return doneMessage(job, 'Done.');
  return (job.error && job.error.message) || job.message || friendlyMessage(null);
}

subscribe('status', (s) => s && adopt(s.jobs));
