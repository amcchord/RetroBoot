// Shared client state with simple topic subscriptions and visibility-aware polling.

import { get } from './api.js';
import { normalizeSettings } from './schema.js';

export const store = {
  session: null, // {paired, device_name, device_id, csrf, hostname}
  status: null, // GET /api/v1/status
  statusAt: 0,
  statusError: null,
  settings: null, // normalized model (schema.js)
  settingsError: null,
};

const subs = new Map();

export function subscribe(topic, fn) {
  if (!subs.has(topic)) subs.set(topic, new Set());
  subs.get(topic).add(fn);
  return () => subs.get(topic).delete(fn);
}

export function emit(topic, detail) {
  const set = subs.get(topic);
  if (set) for (const fn of [...set]) fn(detail);
}

let statusInflight = null;
export function refreshStatus() {
  if (statusInflight) return statusInflight;
  statusInflight = get('/api/v1/status')
    .then((s) => {
      store.status = s;
      store.statusAt = Date.now();
      store.statusError = null;
      emit('status', s);
      return s;
    })
    .catch((err) => {
      store.statusError = err;
      emit('status-error', err);
      throw err;
    })
    .finally(() => {
      statusInflight = null;
    });
  return statusInflight;
}

let settingsInflight = null;
export function refreshSettings() {
  if (settingsInflight) return settingsInflight;
  settingsInflight = get('/api/v1/settings')
    .then((raw) => {
      store.settings = normalizeSettings(raw, store.status && store.status.capabilities);
      store.settingsError = null;
      emit('settings', store.settings);
      return store.settings;
    })
    .catch((err) => {
      store.settingsError = err;
      emit('settings-error', err);
      throw err;
    })
    .finally(() => {
      settingsInflight = null;
    });
  return settingsInflight;
}

// ---- Polling. Status every few seconds; settings less often (and on focus).

const timers = new Map();
let fastUntil = 0;
let running = false;

/** Poll status quickly for a while (e.g. during lifecycle changes). */
export function pollFast(ms = 20000) {
  fastUntil = Math.max(fastUntil, Date.now() + ms);
}

function schedule(name, fn, delayFn) {
  const loop = async () => {
    if (!running) return;
    if (document.visibilityState === 'visible') {
      try {
        await fn();
      } catch {
        /* reported through store errors / connectivity */
      }
    }
    if (running) timers.set(name, setTimeout(loop, delayFn()));
  };
  timers.set(name, setTimeout(loop, delayFn()));
}

export function startPolling() {
  if (running) return;
  running = true;
  schedule('status', refreshStatus, () => (Date.now() < fastUntil ? 1200 : 3000));
  schedule('settings', refreshSettings, () => (Date.now() < fastUntil ? 3000 : 10000));
}

export function stopPolling() {
  running = false;
  for (const t of timers.values()) clearTimeout(t);
  timers.clear();
}

document.addEventListener('visibilitychange', () => {
  if (running && document.visibilityState === 'visible') {
    refreshStatus().catch(() => {});
    refreshSettings().catch(() => {});
  }
});

/** Guest lifecycle helpers derived from status. */
export function guestState(status = store.status) {
  return (status && status.guest && status.guest.state) || 'unknown';
}

export function guestRunning(status = store.status) {
  const s = guestState(status);
  return s === 'running' || s === 'starting' || s === 'stopping' || s === 'restarting';
}

export function guestHung(status = store.status) {
  return !!(status && status.guest && status.guest.state === 'running' && status.guest.responsive === false);
}

export function hasCap(name, status = store.status) {
  const caps = status && status.capabilities;
  if (!caps) return null;
  const v = caps[name];
  if (v && typeof v === 'object') return v.present !== false && v.available !== false;
  return !!v;
}
