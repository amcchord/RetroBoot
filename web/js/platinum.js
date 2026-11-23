// Platinum asset bundle and integer pixel scaling.
//
// hostd serves a bundle generated from the user's own Mac OS 9 disk at
// /assets/platinum/ (fonts drawn from Charcoal/Geneva/Monaco, icons, the
// desktop pattern and Platinum control sprites; see platinum/README.md).  When
// it is present this module registers the fonts, exposes every sprite as a CSS
// custom property and marks <html> with .pt / .pt-fonts.  When it is absent the
// page uses RetroBoot's own bitmap fonts, icons, and CSS window chrome.
//
// Every Mac pixel is drawn as an integer number of device pixels: the body is
// zoomed by k / devicePixelRatio, where k is the "Pixel size" (Auto, 1x-4x).

const BASE = '/assets/platinum/';
const PREF_KEY = 'rb.pixelSize';
const root = document.documentElement;

export const pt = { ready: false, manifest: null, version: '', icons: {}, k: 1, zoom: 1 };
const listeners = new Set();
export const onPlatinum = (fn) => (listeners.add(fn), () => listeners.delete(fn));

// Our icon names -> bundle icon names.
const ICON_MAP = {
  mac: 'overview', overview: 'overview', machine: 'machine', display: 'display', sound: 'sound', storage: 'storage',
  network: 'network', wifi: 'network', web: 'network', diagnostics: 'diagnostics', recovery: 'recovery',
  devices: 'devices', note: 'note', caution: 'caution', stop: 'stop', folder: 'folder', disk: 'disk', cd: 'cd',
  document: 'document', trash: 'trash',
};

function url(rel) {
  return `${BASE}${rel}?v=${encodeURIComponent(pt.version)}`;
}

/** URL of a bundle icon (16 or 32) for one of our icon names, or null. */
export function bundleIcon(name, size) {
  if (!pt.ready) return null;
  const files = pt.icons[ICON_MAP[name] || name];
  if (!files) return null;
  const want = size <= 20 ? '16' : '32';
  // Never scale a Mac icon by a fraction: without a 16 px version, small sizes use our own art.
  const rel = want === '16' ? files['16'] : files['32'] || files['16'];
  return rel ? { src: url(rel), px: rel.endsWith('-16.png') ? 16 : 32 } : null;
}

// ---- Pixel scale ---------------------------------------------------------------

export function pixelPref() {
  try {
    const v = localStorage.getItem(PREF_KEY);
    return ['1', '2', '3', '4'].includes(v) ? v : 'auto';
  } catch {
    return 'auto';
  }
}

export function setPixelPref(v) {
  try {
    if (v === 'auto') localStorage.removeItem(PREF_KEY);
    else localStorage.setItem(PREF_KEY, String(v));
  } catch {
    /* private mode: keep for this page only */
  }
  applyScale(v);
}

/** Device pixels per Mac pixel. */
export function autoScale(dpr = window.devicePixelRatio || 1) {
  return Math.max(1, Math.round(dpr));
}

/**
 * Whether pixel size k can be drawn exactly on this display: the body zoom k / dpr
 * must be a whole number, or the browser lays boxes out on fractions of a Mac pixel
 * (and rounds 1-pixel borders).  On a fractional-ratio display nothing is exact and
 * Automatic is the closest.
 */
export function scaleExact(k, dpr = window.devicePixelRatio || 1) {
  const z = k / dpr;
  return Math.abs(z - Math.round(z)) < 1e-6 && Math.round(z) >= 1;
}

let mql = null;
export function applyScale(pref = pixelPref()) {
  const dpr = window.devicePixelRatio || 1;
  const k = pref === 'auto' || !scaleExact(Number(pref), dpr) ? autoScale(dpr) : Number(pref);
  pt.k = k;
  pt.zoom = k / dpr;
  root.style.setProperty('--pt-zoom', String(pt.zoom));
  root.dataset.pixelSize = String(k);
  // Layout breakpoints are in Mac pixels: the CSS media queries cannot see the zoom.
  const macWidth = window.innerWidth / pt.zoom;
  root.classList.toggle('mac-narrow', macWidth < 620);
  root.classList.toggle('mac-wide', macWidth >= 980);
  if (mql) mql.removeEventListener('change', onDprChange);
  mql = window.matchMedia(`(resolution: ${dpr}dppx)`);
  mql.addEventListener('change', onDprChange);
  for (const fn of listeners) fn(pt);
  snapText();
}
function onDprChange() {
  applyScale();
}
window.addEventListener('resize', () => applyScale());

// ---- Whole-pixel text ----------------------------------------------------------
// Boxes are painted on whole layout pixels, but text is drawn exactly where layout
// puts it.  A centered label can land on half a Mac pixel; at 1x the browser rounds
// it to a device pixel, at 2x and 3x it would draw it between Mac pixels.  These
// labels are nudged (position: relative) onto the Mac pixel grid, rounding the way
// QuickDraw does.  Column splits are rounded in the CSS instead.
// Boxes first (a vertically centered button at zoom 2 sits half a Mac pixel off), then
// the text and icons inside them.  Boxes round halves up, as the browser snaps them at 1x.
const SNAP_BOXES = '.btn, .led, .live-dot, .conn-dot, .dock';
const SNAP_SELECTOR = '.btn-label, .page-intro-text, .screen-status, .state-line > .muted, .hash .muted, .hash > code, .with-copy > code, .range-end, .range-value, svg.px, svg.px-line, img.pt-icon, .snap-text';
let snapQueued = false;
let snapObserver = null;

export function snapText() {
  if (snapQueued) return;
  snapQueued = true;
  requestAnimationFrame(() => {
    snapQueued = false;
    runSnap();
  });
}

function nudge(v, halfUp) {
  const f = v - Math.floor(v);
  if (f < 0.01 || f > 0.99) return 0;
  // Exactly half: text rounds down (as QuickDraw and the browser at 1x), boxes up.
  return f < 0.5 || (f === 0.5 && !halfUp) ? -f : 1 - f;
}

function runSnap() {
  // A centered header that wraps can't keep every line on whole pixels: left-align it.
  const heads = [...document.querySelectorAll('.page-head')];
  for (const head of heads) head.classList.remove('is-wrapped');
  const wrapped = heads.filter((head) => (head.querySelector('.page-intro-text')?.getClientRects().length || 0) > 1);
  for (const head of wrapped) head.classList.add('is-wrapped');
  // Viewport px per Mac px (getBoundingClientRect sees the zoom).
  const probe = document.body.appendChild(document.createElement('div'));
  probe.style.cssText = 'position:absolute;left:0;top:0;width:100px;height:0;visibility:hidden';
  const ratio = probe.getBoundingClientRect().width / 100 || 1;
  probe.remove();
  snapPass([...document.querySelectorAll(SNAP_BOXES)], ratio, true);
  snapPass([...document.querySelectorAll(SNAP_SELECTOR)], ratio, false);
}

function snapPass(els, ratio, boxes) {
  if (!els.length) return;
  for (const el of els) {
    el.style.left = '';
    el.style.top = '';
    el.style.translate = '';
  }
  const moves = [];
  const range = document.createRange();
  for (const el of els) {
    // Where the text is drawn (aligned text can sit at a fraction inside a whole-pixel box).
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let t = walker.nextNode();
    while (t && !t.data.trim()) t = walker.nextNode();
    if (t) range.selectNodeContents(t);
    const r = (t && !boxes ? range : el).getClientRects()[0];
    if (!r) continue;
    const dx = nudge((r.left + window.scrollX) / ratio, boxes);
    const dy = nudge((r.top + window.scrollY) / ratio, boxes);
    if (dx || dy) moves.push([el, dx, dy]);
  }
  for (const [el, dx, dy] of moves) {
    const pos = getComputedStyle(el).position;
    if (pos === 'static' || pos === 'relative') {
      el.style.position = 'relative';
      if (dx) el.style.left = `${dx}px`;
      if (dy) el.style.top = `${dy}px`;
    } else {
      el.style.translate = `${dx}px ${dy}px`;   // fixed and absolute boxes (the toast dock)
    }
  }
}

/** Keep labels on the Mac pixel grid as the page changes. */
export function watchSnap(node = document.body) {
  if (snapObserver) return;
  snapObserver = new MutationObserver(snapText);
  snapObserver.observe(node, { childList: true, subtree: true, characterData: true });
  if ('ResizeObserver' in window) new ResizeObserver(snapText).observe(document.documentElement);
  document.fonts?.addEventListener?.('loadingdone', snapText);
  snapText();
}

// ---- Bundle --------------------------------------------------------------------

const FONT_VARS = {
  'charcoal-12': '--pt-font-system',
  'charcoal-12-mono': '--pt-font-system-mono',
  'charcoal-24': '--pt-font-large',
  'geneva-9': '--pt-font-tiny',
  'geneva-9-bold': '--pt-font-tiny-bold',
  'geneva-10': '--pt-font-small',
  'geneva-10-bold': '--pt-font-small-bold',
  'geneva-12': '--pt-font-app',
  'monaco-9': '--pt-font-mono',
  'monaco-12': '--pt-font-mono-large',
  'chicago-12': '--pt-font-chicago',
};

export async function loadPlatinum() {
  applyScale();
  let manifest;
  try {
    const r = await fetch(`${BASE}manifest.json`, { credentials: 'same-origin', cache: 'no-cache' });
    if (!r.ok) throw new Error(String(r.status));
    manifest = await r.json();
  } catch {
    root.classList.add('pt-absent');
    return false;
  }
  pt.manifest = manifest;
  pt.version = String(manifest.created || manifest.generator || '1');
  pt.icons = manifest.icons || {};
  const loads = [];
  if ('FontFace' in window && document.fonts) {
    for (const [name, f] of Object.entries(manifest.fonts || {})) {
      const ttf = (f.files || []).find((p) => p.endsWith('.ttf'));
      if (!ttf) continue;
      const face = new FontFace(f.family, `url("${url(ttf)}") format("truetype")`, { display: 'block' });
      document.fonts.add(face);
      loads.push(face.load().then(() => FONT_VARS[name] && root.style.setProperty(FONT_VARS[name], `"${f.family}"`)).catch(() => null));
    }
  }
  for (const [name, c] of Object.entries(manifest.chrome || {})) {
    root.style.setProperty(`--pt-${name}`, `url("${url(c.file)}")`);
  }
  for (const [name, c] of Object.entries(manifest.cursors || {})) {
    root.style.setProperty(`--pt-cursor-${name}`, `url("${url(c.file)}") ${c.hotspot[0]} ${c.hotspot[1]}`);
    root.style.setProperty(`--pt-cursor-${name}-img`, `url("${url(c.file)}")`);
  }
  if (manifest.desktop) root.style.setProperty('--pt-desktop', `url("${url(manifest.desktop.file)}")`);
  const pal = manifest.palette || {};
  (pal.ramp || []).forEach((c, i) => root.style.setProperty(`--pt-ramp-${i}`, c));
  if (pal.highlight) root.style.setProperty('--pt-highlight', pal.highlight);
  pt.ready = true;
  root.classList.add('pt');
  await Promise.race([Promise.all(loads), new Promise((res) => setTimeout(res, 3000))]);
  if (manifest.fonts && manifest.fonts['charcoal-12']) root.classList.add('pt-fonts');
  for (const fn of listeners) fn(pt);
  watchSnap();
  return true;
}
