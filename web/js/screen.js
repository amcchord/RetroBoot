// Live screenshots via GET /api/v1/screen.png?what=guest|composed.
// Refreshes only while visible on screen and the tab is in the foreground.

import { api, friendlyMessage } from './api.js';
import { h, fmtTime, stamp, downloadBlob } from './util.js';
import { button, dialog } from './ui.js';
import { lineIcon } from './icons.js';
import { hw } from './words.js';

const WHAT_TEXT = {
  guest: { name: 'Mac OS screen', alt: 'Mac OS screen' },
  get composed() {
    return { name: hw().screenLower, alt: `What the ${hw().display} shows` };
  },
};

/**
 * @param {object} o
 * @param {'guest'|'composed'} o.what
 * @param {number} o.interval refresh period in ms (0 = manual only)
 * @param {() => string|null} o.placeholder text to show instead of fetching (e.g. guest stopped)
 */
/** Let the CSS give a scaled screen picture a whole-pixel height (see .screen-img). */
function wholeHeight(img) {
  if (img.naturalWidth) img.style.setProperty('--ar', String(img.naturalHeight / img.naturalWidth));
}

export function liveScreen({ what = 'guest', interval = 2000, placeholder = () => null, stale = () => null, caption = true, allowPause = true } = {}) {
  const img = h('img', { class: 'screen-img', alt: '', draggable: 'false' });
  img.addEventListener('load', () => wholeHeight(img));
  const ph = h('div', { class: 'screen-placeholder', hidden: true });
  const frame = h('button', { type: 'button', class: 'screen-frame', 'aria-label': `Open a larger view of the ${WHAT_TEXT[what].name}` }, img, ph);
  const status = h('span', { class: 'screen-status' }, 'Loading…');
  const pauseBtn = allowPause ? button('Pause', { variant: 'quiet', icon: 'pause', className: 'btn-small', onClick: () => setPaused(!paused) }) : null;
  const el = h('figure', { class: 'screen' }, frame, caption ? h('figcaption', { class: 'screen-caption' }, h('span', { class: 'live-dot', 'aria-hidden': 'true' }), status, pauseBtn) : null);

  let url = null;
  let blob = null;
  let capturedAt = null;
  let timer = null;
  let inflight = null;
  let visible = false;
  let paused = false;
  let destroyed = false;
  let lastError = null;

  function setPaused(p) {
    paused = p;
    if (pauseBtn) {
      pauseBtn.querySelector('.btn-label').textContent = p ? 'Resume' : 'Pause';
      pauseBtn.querySelector('.btn-icon').innerHTML = lineIcon(p ? 'play' : 'pause', 18);
    }
    el.classList.toggle('is-paused', p);
    updateStatus();
    schedule(p ? null : 0);
  }

  function updateStatus() {
    const msg = placeholder();
    const staleText = !msg && stale();
    if (msg) status.textContent = 'Not available';
    else if (staleText) status.textContent = staleText;
    else if (lastError) status.textContent = 'Not updating';
    else if (!capturedAt) status.textContent = 'Loading…';
    else status.textContent = `${paused || !interval ? 'Captured' : 'Live · updated'} ${fmtTime(capturedAt)}`;
    el.classList.toggle('is-live', !!capturedAt && !paused && !!interval && !msg && !lastError && !staleText);
    if (pauseBtn) pauseBtn.hidden = !!msg && !paused;
    el.classList.toggle('is-stale', !!staleText);
  }

  async function refresh() {
    if (destroyed || inflight) return inflight;
    const msg = placeholder();
    if (msg) {
      showPlaceholder(msg);
      updateStatus();
      return null;
    }
    inflight = (async () => {
      try {
        const res = await api('GET', `/api/v1/screen.png?what=${what}&t=${Date.now()}`, undefined, { raw: true, timeout: 10000 });
        const b = await res.blob();
        const next = URL.createObjectURL(b);
        await new Promise((resolve) => {
          const probe = new Image();
          probe.onload = probe.onerror = resolve;
          probe.src = next;
        });
        if (destroyed) {
          URL.revokeObjectURL(next);
          return;
        }
        const prev = url;
        url = next;
        blob = b;
        img.src = next;
        capturedAt = new Date();
        img.alt = `${WHAT_TEXT[what].alt}, captured ${fmtTime(capturedAt)}`;
        lastError = null;
        ph.hidden = true;
        img.hidden = false;
        if (prev) setTimeout(() => URL.revokeObjectURL(prev), 1000);
      } catch (err) {
        lastError = err;
        if (!url || err.code === 'guest-not-running') showPlaceholder(err.code === 'guest-not-running' ? "Mac OS isn't running, so there's no Mac OS screen to show." : `Couldn't capture the screen. ${friendlyMessage(err)}`);
      } finally {
        inflight = null;
        updateStatus();
      }
    })();
    return inflight;
  }

  function showPlaceholder(msg) {
    ph.replaceChildren(h('span', { class: 'screen-placeholder-text' }, msg));
    ph.hidden = false;
    img.hidden = !!(!url || placeholder());
  }

  function schedule(delay) {
    if (timer) clearTimeout(timer);
    timer = null;
    if (destroyed || delay == null) return;
    timer = setTimeout(async () => {
      timer = null;
      if (visible && document.visibilityState === 'visible' && !paused) await refresh();
      if (interval && !paused) schedule(interval);
    }, delay);
  }

  const io = 'IntersectionObserver' in window ? new IntersectionObserver((entries) => {
    visible = entries.some((e) => e.isIntersecting);
    if (visible && interval && !paused) schedule(0);
  }) : null;
  if (io) io.observe(el);
  else visible = true;
  const onVis = () => {
    if (document.visibilityState === 'visible' && interval && !paused) schedule(0);
  };
  document.addEventListener('visibilitychange', onVis);

  frame.addEventListener('click', () => {
    if (!url) return;
    const big = h('img', { class: 'screen-large', src: url, alt: img.alt });
    big.addEventListener('load', () => wholeHeight(big));
    dialog({
      title: WHAT_TEXT[what].name,
      icon: what === 'guest' ? 'mac' : 'display',
      content: h('div', { class: 'screen-large-wrap' }, big),
      wide: true,
      buttons: [
        { label: 'Download PNG', value: 'download' },
        { label: 'Close', value: 'close', variant: 'default', autofocus: true },
      ],
    }).then((v) => {
      if (v === 'download') download();
    });
  });

  function download() {
    if (blob) downloadBlob(blob, `retroboot-${what === 'guest' ? 'macos' : 'display'}-screen-${stamp(capturedAt || new Date())}.png`);
  }

  // First capture straight away (even when paused / manual).
  refresh();
  if (interval) schedule(interval);

  return {
    el,
    refresh: async () => {
      await refresh();
      return blob;
    },
    download,
    hasImage: () => !!blob,
    /** Re-evaluate the placeholder after state changes (e.g. guest started). */
    poke() {
      updateStatus();
      const msg = placeholder();
      if (msg) {
        showPlaceholder(msg);
        updateStatus();
      } else if (!ph.hidden || !url) {
        refresh();
      }
    },
    destroy() {
      destroyed = true;
      if (timer) clearTimeout(timer);
      if (io) io.disconnect();
      document.removeEventListener('visibilitychange', onVis);
      if (url) URL.revokeObjectURL(url);
    },
  };
}
