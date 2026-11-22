import { get } from '../api.js';
import { h, swap, Scope, uid, stamp, downloadBlob, fmtTime, formatBytes } from '../util.js';
import { win, button, kv, pageHeader, loading, errorState, copyButton, progressBar } from '../ui.js';
import { store, subscribe } from '../store.js';
import { liveScreen } from '../screen.js';
import { timeline } from './overview.js';
import { collectEvidence, latestBundle, downloadBundle } from '../actions.js';
import { activeJobs } from '../jobs.js';
import { hw } from '../words.js';

export const meta = { title: 'Diagnostics', icon: 'diagnostics' };

const SOURCES = [
  { id: 'hostd', label: 'RetroBoot service' },
  { id: 'lifecycle', label: 'Startup and shutdown events' },
  { id: 'guest', label: 'Mac OS serial console' },
  { id: 'launcher', label: 'Emulator' },
  { id: 'presenter', label: 'Display' },
  { id: 'kernel', label: 'Host kernel' },
];

// Friendlier names for hostd's version keys; anything else is shown as sent.
const VERSION_LABELS = {
  'RetroBoot hostd': 'RetroBoot service',
  hostd: 'RetroBoot service',
  'rb-presenter': 'Display service',
  'Linux kernel': 'Host kernel',
};
const LINE_COUNTS = [100, 500, 2000];

/** Lifecycle records are JSON lines; show them as "time event key=value …" (copy/download keep the raw lines). */
function prettyLine(l) {
  if (typeof l !== 'string') return [l.time || l.t, l.level, l.message || l.msg].filter(Boolean).join(' ');
  if (!l.startsWith('{')) return l;
  try {
    // hostd's redaction can leave a bare [REDACTED] where a JSON string was.
    const r = JSON.parse(l.replace(/: \[REDACTED\]/g, ': "[REDACTED]"'));
    if (!r || typeof r !== 'object' || !r.event) return l;
    const skip = new Set(['t_mono', 't_wall', 'boot_id', 'source', 'event']);
    const rest = Object.entries(r)
      .filter(([k, v]) => !skip.has(k) && v !== null && v !== undefined)
      .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`);
    const t = r.t_wall ? fmtTime(r.t_wall) : '';
    return [t, r.event, ...rest].filter(Boolean).join(' ');
  } catch {
    return l;
  }
}

function versionsBlock() {
  const s = store.status;
  if (!s) return loading();
  const v = s.versions || {};
  const rows = Object.entries(v).map(([k, val]) => [VERSION_LABELS[k] || k, val]);
  if (!rows.length && s.host && s.host.version) rows.push(['RetroBoot', s.host.version]);
  const text = () => rows.map(([k, val]) => `${k}: ${val}`).join('\n');
  return [kv(rows.map(([k, val]) => [k, h('code', null, String(val))])), h('div', { class: 'row-actions' }, copyButton(text, 'Copy Version Info', { what: 'version info' }))];
}

function logViewer(scope) {
  const srcId = uid('src');
  const linesId = uid('lines');
  const filterId = uid('filter');
  const autoId = uid('auto');
  const wrapId = uid('wrap');
  let source = 'hostd';
  let lines = 500;
  let data = null;
  let error = null;
  let auto = false;
  let loadingNow = false;

  const pre = h('pre', { class: 'log', tabindex: '0', 'aria-label': 'Log lines' }, 'Loading…');
  const errorHost = h('div');
  const meta = h('p', { class: 'muted small log-meta', 'aria-live': 'polite' });
  const filter = h('input', { type: 'search', id: filterId, placeholder: 'Filter lines', autocomplete: 'off', spellcheck: 'false', oninput: () => paint() });
  const sourceSel = h('select', { id: srcId, onchange: (e) => ((source = e.target.value), load()) }, ...SOURCES.map((s) => h('option', { value: s.id }, s.label)));
  const linesSel = h('select', { id: linesId, onchange: (e) => ((lines = Number(e.target.value)), load()) }, ...LINE_COUNTS.map((n) => h('option', { value: String(n), selected: n === lines }, `Last ${n} lines`)));
  const autoBox = h('input', { type: 'checkbox', id: autoId, onchange: (e) => (auto = e.target.checked) });
  const wrapBox = h('input', { type: 'checkbox', id: wrapId, checked: true, onchange: (e) => pre.classList.toggle('nowrap', !e.target.checked) });
  const refreshBtn = button('Refresh', { icon: 'refresh', className: 'btn-small', onClick: () => load() });

  function paint() {
    if (error && !data) {
      swap(errorHost, errorState(error, load));
      pre.hidden = true;
      meta.textContent = '';
      return;
    }
    errorHost.replaceChildren();
    pre.hidden = false;
    if (!data) return;
    const q = filter.value.trim().toLowerCase();
    const all = Array.isArray(data.lines) ? data.lines : String(data.lines || data || '').split('\n');
    const pretty = all.map(prettyLine);
    const shown = q ? pretty.filter((l) => l.toLowerCase().includes(q)) : pretty;
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
    pre.replaceChildren(...shown.map((l) => h('span', { class: /\b(ERROR|CRIT|FATAL)\b/.test(l) ? 'log-error' : /\bWARN(ING)?\b/.test(l) ? 'log-warn' : null }, l + '\n')));
    if (!shown.length) pre.textContent = q ? 'No lines match the filter.' : 'This log is empty.';
    pre.setAttribute('aria-label', `${SOURCES.find((s) => s.id === source).label} log`);
    meta.textContent = `${shown.length}${q ? ` of ${all.length}` : ''} lines${data.truncated ? ` (older lines omitted)` : ''} · loaded ${fmtTime(new Date())}${error ? ' · last refresh failed' : ''}`;
    if (atBottom || !pre.dataset.painted) pre.scrollTop = pre.scrollHeight;
    pre.dataset.painted = '1';
  }

  async function load() {
    if (loadingNow) return;
    loadingNow = true;
    refreshBtn.disabled = true;
    try {
      data = await get(`/api/v1/logs?source=${encodeURIComponent(source)}&lines=${lines}`);
      error = null;
    } catch (err) {
      error = err;
    } finally {
      loadingNow = false;
      refreshBtn.disabled = false;
    }
    paint();
  }

  const downloadBtn = button('Download', { icon: 'download', className: 'btn-small', variant: 'quiet', onClick: () => data && downloadBlob(new Blob([(data.lines || []).join('\n') + '\n'], { type: 'text/plain' }), `retroboot-${source}-${stamp()}.log`) });
  const el = h(
    'div',
    { class: 'logs' },
    h(
      'div',
      { class: 'log-controls' },
      h('div', { class: 'field-inline' }, h('label', { for: srcId }, 'Source'), h('div', { class: 'select-wrap' }, sourceSel)),
      h('div', { class: 'field-inline' }, h('label', { for: linesId }, 'Show'), h('div', { class: 'select-wrap' }, linesSel)),
      h('div', { class: 'field-inline grow' }, h('label', { for: filterId }, 'Filter'), filter),
    ),
    h(
      'div',
      { class: 'log-toolbar' },
      refreshBtn,
      h('label', { class: 'check', for: autoId }, autoBox, h('span', null, 'Refresh every 5 s')),
      h('label', { class: 'check', for: wrapId }, wrapBox, h('span', null, 'Wrap lines')),
      h('span', { class: 'grow' }),
      copyButton(() => (data ? (data.lines || []).join('\n') : ''), 'Copy', { what: 'log' }),
      downloadBtn,
    ),
    errorHost,
    pre,
    meta,
    h('p', { class: 'muted small' }, 'Logs are redacted: passwords, pairing secrets and Wi-Fi keys never appear.'),
  );
  load();
  scope.interval(() => auto && document.visibilityState === 'visible' && load(), 5000);
  return el;
}

function evidenceBlock(scope) {
  const out = h('div');
  const render = () => {
    const running = activeJobs('diagnostics.bundle')[0];
    const last = latestBundle();
    swap(
      out,
      h('p', { class: 'win-intro' }, 'An evidence bundle gathers logs, both screenshots, versions, settings and the startup timeline into one file you can attach to a bug report. Passwords and other credentials are removed.'),
      running ? h('div', { class: 'inline-job', role: 'status' }, progressBar(typeof running.progress === 'number' ? running.progress : null, 'Collecting evidence'), h('p', { class: 'muted small' }, running.message || 'Collecting…')) : null,
      h(
        'div',
        { class: 'row-actions' },
        button(running ? 'Collecting…' : 'Collect Evidence', { variant: last ? null : 'default', icon: 'box', busy: !!running, focusKey: 'ev-collect', onClick: () => collectEvidence({ announce: false }).then(render) }),
        last ? button('Download', { variant: 'default', icon: 'download', focusKey: 'ev-download', onClick: () => downloadBundle(last) }) : null,
      ),
      last ? h('p', { class: 'muted small' }, `${last.filename || last.id} · ${last.size_bytes ? formatBytes(last.size_bytes) : ''} · collected ${fmtTime(last.at)}`) : null,
    );
  };
  render();
  scope.add(subscribe('jobs', render));
  return out;
}

function screenshots(scope) {
  const outer = scope.add(liveScreen({ what: 'composed', interval: 0, allowPause: false }));
  const inner = scope.add(
    liveScreen({
      what: 'guest',
      interval: 0,
      allowPause: false,
      placeholder: () => {
        const g = store.status && store.status.guest;
        return g && (g.state === 'stopped' || g.state === 'failed') ? "Mac OS isn't running, so there's no Mac OS screen." : null;
      },
    }),
  );
  const both = async () => {
    await Promise.all([outer.refresh(), inner.refresh()]);
  };
  const cell = (title, desc, shot) =>
    h(
      'div',
      { class: 'shot' },
      h('h3', null, title),
      h('p', { class: 'muted small' }, desc),
      shot.el,
      h('div', { class: 'row-actions' }, button('Download', { icon: 'download', className: 'btn-small', variant: 'quiet', onClick: () => shot.download() })),
    );
  return h(
    'div',
    null,
    h('div', { class: 'shots' }, cell(`${hw().screenLower} (outer)`, `Exactly what the ${hw().display} shows, including borders and RetroBoot panels.`, outer), cell('Mac OS screen (inner)', 'The Mac OS framebuffer at its own resolution.', inner)),
    h('div', { class: 'row-actions' }, button('Capture Both Again', { icon: 'camera', onClick: both })),
  );
}

export function render() {
  const scope = new Scope();
  const versionsEl = h('div');
  const tlEl = h('div');
  const el = h(
    'div',
    { class: 'page' },
    pageHeader('diagnostics', 'Diagnostics', 'Versions, screenshots, logs and evidence for troubleshooting. Works even when Mac OS is not responding.'),
    h(
      'div',
      { class: 'two-col' },
      h('div', { class: 'col' }, win({ title: 'Evidence Bundle', icon: 'storage' }, evidenceBlock(scope)), win({ title: 'Screenshots', icon: 'display' }, screenshots(scope))),
      h('div', { class: 'col' }, win({ title: 'Versions', icon: 'machine' }, versionsEl), win({ title: 'Startup Timeline', icon: 'diagnostics' }, tlEl)),
    ),
    win({ title: 'Logs', icon: 'diagnostics', className: 'win-logs' }, logViewer(scope)),
  );
  let sig = null;
  const update = () => {
    const s = store.status;
    const next = JSON.stringify([s && s.versions, s && s.boot]);
    if (next === sig) return;
    sig = next;
    swap(versionsEl, ...[].concat(versionsBlock()));
    swap(tlEl, s ? timeline(s) : loading());
  };
  update();
  scope.add(subscribe('status', update));
  return { el, destroy: () => scope.destroy() };
}
