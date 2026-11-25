// Small DOM and formatting helpers shared by every page.

/**
 * Create an element. Children may be strings, numbers, nodes, arrays or null.
 * Props: class, dataset, on<Event> handlers, html (trusted markup only: our icons),
 * value/checked/selected (set as properties), booleans as presence attributes.
 */
export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = Array.isArray(v) ? v.filter(Boolean).join(' ') : v;
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k === 'html') el.innerHTML = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === 'value' || k === 'checked' || k === 'selected' || k === 'indeterminate') el[k] = v;
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, String(v));
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false || c === true) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** Replace a container's content, keeping keyboard focus on an equivalent element when possible. */
export function swap(container, ...children) {
  const active = document.activeElement;
  const key = active && container.contains(active) ? active.dataset.focusKey : null;
  container.replaceChildren();
  append(container, children);
  if (key) {
    const again = container.querySelector(`[data-focus-key="${CSS.escape(key)}"]`);
    if (again) again.focus({ preventScroll: true });
  }
}

let idCounter = 0;
export const uid = (prefix = 'id') => `${prefix}-${++idCounter}`;

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function plural(n, one, many = one + 's') {
  return `${n} ${n === 1 ? one : many}`;
}

export function formatBytes(n) {
  if (n == null || isNaN(n)) return '—';
  const units = ['bytes', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = Number(n);
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000;
    i += 1;
  }
  if (i === 0) return `${v} bytes`;
  return `${v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)} ${units[i]}`;
}

export function formatSpeed(bps) {
  if (bps == null || isNaN(bps) || bps <= 0) return '';
  const mib = 1024 * 1024;
  const kib = 1024;
  if (bps >= 100 * mib) return `${(bps / mib).toFixed(0)} MB/s`;
  if (bps >= mib) return `${(bps / mib).toFixed(1)} MB/s`;
  if (bps >= 10 * kib) return `${(bps / kib).toFixed(0)} KB/s`;
  if (bps >= kib) return `${(bps / kib).toFixed(1)} KB/s`;
  return `${Math.round(bps)} B/s`;
}

export function formatEta(seconds) {
  if (seconds == null || isNaN(seconds) || seconds <= 0 || seconds > 48 * 3600) return '';
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} s left`;
  if (seconds < 300) {
    const s = Math.round(seconds);
    return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s left`;
  }
  if (seconds < 3600) return `${Math.round(seconds / 60)} min left`;
  const m = Math.round(seconds / 60);
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m left`;
}

export function createTransferMeter(total = null, { windowMs = 4000, clock = () => performance.now() } = {}) {
  const samples = [];
  let lastDone = 0;
  const start = clock();
  samples.push([start, 0]);
  return {
    update(done) {
      const n = Math.max(0, Number(done) || 0);
      if (n < lastDone) {
        samples.length = 0;
        samples.push([clock(), n]);
        lastDone = n;
        return { speed: null, eta: null };
      }
      const now = clock();
      lastDone = n;
      samples.push([now, n]);
      const cutoff = now - Math.max(500, windowMs);
      while (samples.length > 2 && samples[1][0] <= cutoff) samples.shift();
      const [t0, b0] = samples[0];
      const dt = (now - t0) / 1000;
      const db = n - b0;
      if (dt < 0.15 || db <= 0) return { speed: null, eta: null };
      const speed = db / dt;
      const eta = total && total > n && speed > 0 ? (total - n) / speed : null;
      return { speed, eta };
    },
  };
}

export function formatTransferText(prefix, { fraction = null, loaded = null, total = null, speed = null, eta = null } = {}) {
  const parts = [];
  if (typeof fraction === 'number' && !isNaN(fraction)) parts.push(`${Math.round(Math.max(0, Math.min(1, fraction)) * 100)}%`);
  if (loaded != null && total != null && total > 0) parts.push(`${formatBytes(loaded)} of ${formatBytes(total)}`);
  const spd = formatSpeed(speed);
  if (spd) parts.push(spd);
  const left = formatEta(eta);
  if (left) parts.push(left);
  return parts.length ? `${prefix} ${parts.join(' · ')}` : prefix;
}

export function formatDuration(seconds) {
  if (seconds == null || isNaN(seconds)) return '—';
  const s = Math.max(0, Math.floor(seconds));
  const d = Math.floor(s / 86400);
  const hr = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d} d ${hr} h`;
  if (hr) return `${hr} h ${m} min`;
  if (m) return `${m} min`;
  return `${s} s`;
}

export function formatMs(ms) {
  if (ms == null) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`;
}

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' });
const shortTimeFmt = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
const dateTimeFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' });

export function toDate(v) {
  if (v == null) return null;
  const d = v instanceof Date ? v : new Date(v);
  return isNaN(d) ? null : d;
}

export const fmtTime = (v) => (toDate(v) ? timeFmt.format(toDate(v)) : '—');
export const fmtShortTime = (v) => (toDate(v) ? shortTimeFmt.format(toDate(v)) : '—');
export const fmtDateTime = (v) => (toDate(v) ? dateTimeFmt.format(toDate(v)) : '—');

export function relTime(v, now = Date.now()) {
  const d = toDate(v);
  if (!d) return '—';
  const s = Math.round((now - d.getTime()) / 1000);
  if (s < 0) return 'just now';
  if (s < 10) return 'just now';
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 14) return `${Math.floor(s / 86400)} d ago`;
  return fmtDateTime(d);
}

export function stamp(d = new Date()) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

/** A friendly default name for this browser, used when pairing. */
export function guessDeviceName() {
  const ua = navigator.userAgent || '';
  let device = 'Computer';
  if (/iPhone/.test(ua)) device = 'iPhone';
  else if (/iPad/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)) device = 'iPad';
  else if (/Android/.test(ua)) device = /Mobile/.test(ua) ? 'Android phone' : 'Android tablet';
  else if (/Macintosh|Mac OS X/.test(ua)) device = 'Mac';
  else if (/Windows/.test(ua)) device = 'Windows PC';
  else if (/CrOS/.test(ua)) device = 'Chromebook';
  else if (/Linux/.test(ua)) device = 'Linux computer';
  let browser = '';
  if (/Edg\//.test(ua)) browser = 'Edge';
  else if (/Firefox\//.test(ua)) browser = 'Firefox';
  else if (/Chrome\//.test(ua) || /CriOS/.test(ua)) browser = 'Chrome';
  else if (/Safari\//.test(ua)) browser = 'Safari';
  return browser ? `${device} (${browser})` : device;
}

/** Summarize a raw User-Agent string ("Chrome on macOS"); other text passes through. */
export function describeUserAgent(ua) {
  if (!ua || !/Mozilla\/|AppleWebKit|Gecko\//.test(ua)) return ua || '';
  const os = /iPhone/.test(ua) ? 'iOS' : /iPad/.test(ua) ? 'iPadOS' : /Android/.test(ua) ? 'Android' : /Mac OS X|Macintosh/.test(ua) ? 'macOS' : /Windows/.test(ua) ? 'Windows' : /CrOS/.test(ua) ? 'ChromeOS' : /Linux/.test(ua) ? 'Linux' : 'an unknown system';
  const browser = /Edg\//.test(ua) ? 'Edge' : /Firefox\/|FxiOS/.test(ua) ? 'Firefox' : /Chrome\/|CriOS/.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : 'A browser';
  return `${browser} on ${os}`;
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Fallback for non-secure contexts or older browsers.
    const ta = h('textarea', { class: 'visually-hidden', 'aria-hidden': 'true' });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch {
      ok = false;
    }
    ta.remove();
    return ok;
  }
}

/** Trigger a download of a Blob with a filename. */
export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = h('a', { href: url, download: filename, class: 'visually-hidden' });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

export const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

export function sameValue(a, b) {
  if (a === b) return true;
  if (typeof a === 'number' && typeof b === 'number') return Math.abs(a - b) < 1e-9;
  return JSON.stringify(a) === JSON.stringify(b);
}

/** Collects cleanup functions for a mounted page or component. */
export class Scope {
  constructor() {
    this.fns = [];
  }
  add(fn) {
    if (typeof fn === 'function') this.fns.push(fn);
    else if (fn && typeof fn.destroy === 'function') this.fns.push(() => fn.destroy());
    return fn;
  }
  interval(fn, ms) {
    const t = setInterval(fn, ms);
    this.fns.push(() => clearInterval(t));
    return t;
  }
  destroy() {
    for (const fn of this.fns.splice(0)) {
      try {
        fn();
      } catch {
        /* ignore cleanup errors */
      }
    }
  }
}
