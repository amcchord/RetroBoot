// Copying files to Mac OS: drag and drop, the Copy window and the transfers behind it.
//
// Files go into the shared folder Mac OS shows as "RetroBoot Share" (hostd retroboot/share.py),
// one at a time over POST /api/v1/share/upload, so each one has its own progress and Stop.
// hostd decodes MacBinary, BinHex and AppleSingle into real resource forks, gives plain files
// a type and creator, and expands StuffIt archives when asked.  CD images can instead go to
// the media library and the CD-ROM drive (POST /api/v1/media, setting media.cd).
// Drops work on the Files page (into the folder it shows) and on a live Mac OS screen (into
// the top of the shared folder, like dropping onto the disk's icon).

import { get, post, patch, asJob, friendlyMessage, setCsrf, ApiError } from './api.js';
import { h, formatBytes, plural, uid } from './util.js';
import { button, dialog, toast, progressBar, docWindow } from './ui.js';
import { store, refreshSettings, refreshStatus, guestRunning, pollFast } from './store.js';
import { track } from './jobs.js';
import { fileIcon, iconForName } from './fileicons.js';

(function stylesheet() {
  if (document.querySelector('link[data-files-css]')) return;
  document.head.append(h('link', { rel: 'stylesheet', href: 'css/files.css', dataset: { filesCss: '' } }));
})();

export const DEFAULT_VOLUME = 'RetroBoot Share';
const CD_EXT = ['iso', 'toast', 'cdr', 'dmg', 'img'];
const CD_DEFAULT = ['iso', 'toast', 'cdr'];
const TEXT_EXT = ['txt', 'text', 'md', 'markdown', 'csv', 'tsv', 'log', 'nfo', 'html', 'htm', 'css', 'xml', 'json', 'rtf', 'ini', 'cfg', 'conf', 'c', 'h', 'py', 'js', 'sh', 'pl', 'rb', 'tex', 'srt'];
const SKIP_NAMES = ['.DS_Store', 'Thumbs.db', 'desktop.ini', '__MACOSX', '.Spotlight-V100', '.Trashes', '.fseventsd'];

const extOf = (name) => {
  const m = /\.([A-Za-z0-9]{1,8})$/.exec(name || '');
  return m ? m[1].toLowerCase() : '';
};
const isStuffit = (name) => /\.(sit|sea)(\.(hqx|bin))?$/i.test(name || '');
const isCd = (name) => CD_EXT.includes(extOf(name));
const isText = (name) => TEXT_EXT.includes(extOf(name));
const q = (s) => `“${s}”`;

/** Remembered share state (volume name etc.) from the last listing, for drops outside the Files page. */
export const shareState = { volume: DEFAULT_VOLUME, state: null };
export function noteShareState(st) {
  if (!st) return;
  shareState.state = st;
  shareState.volume = st.volume_name || DEFAULT_VOLUME;
}

const changeListeners = new Set();
/** fn(folderPath) runs whenever items landed in a folder of the share. */
export function onShareChanged(fn) {
  changeListeners.add(fn);
  return () => changeListeners.delete(fn);
}
const announce = (path) => changeListeners.forEach((fn) => fn(path));

// ---- What was dropped --------------------------------------------------------------------

/** Entries from a DataTransfer.  Call synchronously in the drop handler: the browser
 * forgets them after the event.  Resolves [{kind:'file', file} | {kind:'folder', name,
 * files:[{file, rel}], dirs:[rel]}]. */
export function readDrop(dt) {
  const items = [...(dt.items || [])].filter((i) => i.kind === 'file');
  const entries = items.map((i) => (typeof i.webkitGetAsEntry === 'function' ? i.webkitGetAsEntry() : null));
  const files = [...(dt.files || [])];
  return (async () => {
    if (entries.length && entries.every(Boolean)) {
      const out = [];
      for (const e of entries) {
        if (e.isDirectory) out.push(await readFolder(e));
        else if (!skip(e.name)) out.push({ kind: 'file', file: await entryFile(e) });
      }
      return out;
    }
    return files.filter((f) => !skip(f.name)).map((file) => ({ kind: 'file', file }));
  })();
}

const skip = (name) => SKIP_NAMES.includes(name) || (name.startsWith('.') && !name.startsWith('._'));
const entryFile = (e) => new Promise((resolve, reject) => e.file(resolve, reject));

function readAll(dir) {
  const reader = dir.createReader();
  return new Promise((resolve, reject) => {
    const all = [];
    const more = () =>
      reader.readEntries((batch) => {
        if (!batch.length) resolve(all);
        else {
          all.push(...batch);
          more();
        }
      }, reject);
    more();
  });
}

async function readFolder(top) {
  const folder = { kind: 'folder', name: top.name, files: [], dirs: [] };
  async function walk(dir, rel, depth) {
    if (depth > 30) return;
    for (const e of await readAll(dir)) {
      if (skip(e.name)) continue;
      if (e.isDirectory) {
        const r = rel ? `${rel}/${e.name}` : e.name;
        folder.dirs.push(r);
        await walk(e, r, depth + 1);
      } else {
        folder.files.push({ file: await entryFile(e), rel });
      }
    }
  }
  await walk(top, '', 0);
  return folder;
}

/** Entries from an <input type=file> (webkitdirectory gives each file its relative path). */
export function readPicked(fileList) {
  const out = [];
  const folders = new Map();
  for (const file of fileList) {
    const parts = (file.webkitRelativePath || '').split('/').filter(Boolean);
    if (parts.length < 2) {
      if (!skip(file.name)) out.push({ kind: 'file', file });
      continue;
    }
    if (parts.some((p, i) => i < parts.length && skip(p))) continue;
    const [top, ...rest] = parts;
    if (!folders.has(top)) {
      const f = { kind: 'folder', name: top, files: [], dirs: [] };
      folders.set(top, f);
      out.push(f);
    }
    const f = folders.get(top);
    const rel = rest.slice(0, -1).join('/');
    for (let i = 1; i <= rest.length - 1; i++) {
      const d = rest.slice(0, i).join('/');
      if (!f.dirs.includes(d)) f.dirs.push(d);
    }
    f.files.push({ file, rel });
  }
  return out;
}

// ---- Transfers ------------------------------------------------------------------------------

function csrf() {
  return (store.session && store.session.csrf) || '';
}

async function refreshCsrf() {
  const s = await get('/api/v1/session', { quiet401: true });
  store.session = { ...(store.session || {}), ...s };
  setCsrf(s.csrf);
}

/** POST a File as the request body with upload progress; resolves the JSON reply. */
function sendFile(url, file, { onProgress, onSent, signal }, retried = false) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.setRequestHeader('X-RetroBoot-CSRF', csrf());
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.responseType = 'json';
    xhr.upload.onprogress = (e) => onProgress && onProgress(e.loaded, e.lengthComputable ? e.total : file.size);
    xhr.upload.onload = () => onSent && onSent();
    xhr.onerror = () => reject(new ApiError(0, 'unreachable', "Can't reach RetroBoot. Check that it's on and on the same network."));
    xhr.onabort = () => reject(new ApiError(0, 'stopped', 'Stopped.'));
    xhr.onload = async () => {
      const body = xhr.response || {};
      if (xhr.status >= 200 && xhr.status < 300) return resolve(body);
      const e = body.error || {};
      if (xhr.status === 403 && e.code === 'csrf-invalid' && !retried) {
        try {
          await refreshCsrf();
          return resolve(await sendFile(url, file, { onProgress, onSent, signal }, true));
        } catch (err) {
          return reject(err);
        }
      }
      reject(new ApiError(xhr.status, e.code || `http-${xhr.status}`, e.message || `RetroBoot answered ${xhr.status}.`, e.fields || null, body));
    };
    if (signal) {
      if (signal.aborted) return xhr.abort();
      signal.addEventListener('abort', () => xhr.abort(), { once: true });
    }
    xhr.send(file);
  });
}

function uploadUrl(dest, file, rel, opts) {
  const p = new URLSearchParams({ path: dest, name: file.name, conflict: opts.replace ? 'replace' : 'rename' });
  if (rel) p.set('relpath', rel);
  if (opts.expand) p.set('expand', '1');
  if (opts.text) p.set('text', '1');
  if (file.lastModified) p.set('mtime', String(file.lastModified));
  return `/api/v1/share/upload?${p}`;
}

/** Apply one setting now (live) or at the next Mac OS start; optionally restart Mac OS for it. */
export async function setSetting(key, value, { restart = false, label } = {}) {
  let m = await refreshSettings();
  let res;
  try {
    res = await patch('/api/v1/settings', { revision: m.revision, changes: { [key]: value } });
  } catch (err) {
    if (err.status !== 409) throw err;
    m = await refreshSettings();
    res = await patch('/api/v1/settings', { revision: m.revision, changes: { [key]: value } });
  }
  const job = asJob(await post('/api/v1/settings/apply', { revision: res.revision, restart }));
  if (restart) pollFast(30000);
  const final = job ? await track(job, { label: label || (restart ? 'Restarting Mac OS' : 'Applying settings') }) : null;
  await Promise.all([refreshSettings().catch(() => {}), refreshStatus().catch(() => {})]);
  return final;
}

// ---- The options sheet (only when a drop needs a decision) --------------------------------------

function radio(name, label, checked, disabled = false, hint = null) {
  const id = uid('r');
  const input = h('input', { type: 'radio', name, id, checked, disabled });
  return { input, el: h('div', null, h('label', { class: 'radio', for: id }, input, h('span', null, label)), hint ? h('p', { class: 'dlg-option-hint' }, hint) : null) };
}

async function optionsSheet({ dest, tops, conflicts, share }) {
  const files = tops.filter((t) => t.kind === 'file').map((t) => t.file);
  const cds = files.filter((f) => isCd(f.name));
  const sits = [...files, ...tops.filter((t) => t.kind === 'folder').flatMap((t) => t.files.map((x) => x.file))].filter((f) => isStuffit(f.name));
  const texts = [...files, ...tops.filter((t) => t.kind === 'folder').flatMap((t) => t.files.map((x) => x.file))].filter((f) => isText(f.name));
  if (!cds.length && !sits.length && !texts.length && !conflicts.length) return { expand: false, text: false, replace: false, cd: new Set() };

  const sections = [];
  const canExpand = !share || share.expand_available !== false;
  let expandYes = null;
  if (sits.length) {
    const name = uid('sit');
    const what = sits.length === 1 ? q(sits[0].name) : `${sits.length} StuffIt archives`;
    const yes = radio(name, 'Expand now, so the items are ready to use', canExpand, !canExpand, canExpand ? null : share.expand_reason);
    const no = radio(name, 'Copy as it is, for StuffIt Expander', !canExpand);
    expandYes = yes.input;
    sections.push(h('div', { class: 'dlg-section' }, h('h3', null, sits.length === 1 ? `StuffIt archive ${what}` : what), h('div', { class: 'radio-row', role: 'radiogroup' }, yes.el, no.el)));
  }
  const cdChoices = [];
  if (cds.length) {
    let cdPicked = false;
    const rows = cds.map((f) => {
      const name = uid('cd');
      const wantCd = !cdPicked && CD_DEFAULT.includes(extOf(f.name));
      if (wantCd) cdPicked = true;
      const mount = radio(name, 'Put it in the CD-ROM drive (Mount as CD)', wantCd);
      const copy = radio(name, `Copy it to ${q(dest.label)}`, !wantCd);
      cdChoices.push({ file: f, input: mount.input });
      return h('div', { class: 'dlg-section' }, h('h3', null, `Disc image ${q(f.name)}`), h('div', { class: 'radio-row', role: 'radiogroup' }, mount.el, copy.el));
    });
    sections.push(...rows);
    if (cds.length > 1) sections.push(h('p', { class: 'dlg-option-hint' }, 'Mac OS has one CD-ROM drive, so only one disc goes in it.'));
  }
  let textBox = null;
  if (texts.length) {
    const id = uid('txt');
    textBox = h('input', { type: 'checkbox', id });
    sections.push(
      h(
        'div',
        { class: 'dlg-section' },
        h('div', { class: 'dlg-option' }, h('label', { class: 'check', for: id }, textBox, h('span', null, `Convert ${texts.length === 1 ? 'the text file' : `${texts.length} text files`} for Mac OS`))),
        h('p', { class: 'dlg-option-hint' }, 'Mac line endings and accented letters, so SimpleText shows them correctly. Leave it off to copy the files exactly.'),
      ),
    );
  }
  let replaceYes = null;
  if (conflicts.length) {
    const name = uid('conf');
    const first = conflicts[0];
    const openOnes = conflicts.filter((c) => c.open);
    const title = conflicts.length === 1 ? `An item named ${q(first.mac_name)} already exists in this location.` : `${conflicts.length} items with these names already exist in this location.`;
    const keep = radio(name, `Keep both (the new ${conflicts.length === 1 ? 'one is' : 'ones are'} named “… copy”)`, true);
    const unknownOpen = conflicts.some((c) => c.open == null && c.existing_kind === 'file');
    const rep = radio(name, 'Replace the existing items', false, false, openOnes.length ? `${q(openOnes[0].mac_name)} is open in Mac OS and can't be replaced.` : unknownOpen ? 'If Mac OS has one of them open, close it there first.' : null);
    replaceYes = rep.input;
    sections.push(h('div', { class: 'dlg-section' }, h('h3', null, title), h('div', { class: 'radio-row', role: 'radiogroup' }, keep.el, rep.el)));
  }
  const count = tops.length;
  const value = await dialog({
    title: `Copy to ${q(dest.label)}`,
    icon: 'note',
    wide: true,
    message: `${plural(count, 'item')} to copy.`,
    content: h('div', { class: 'copy-options' }, ...sections),
    buttons: [
      { label: 'Cancel', value: 'cancel' },
      { label: 'Copy', value: 'ok', variant: 'default', autofocus: true },
    ],
  });
  if (value !== 'ok') return null;
  return {
    expand: !!(expandYes && expandYes.checked),
    text: !!(textBox && textBox.checked),
    replace: !!(replaceYes && replaceYes.checked),
    cd: new Set(cdChoices.filter((c) => c.input.checked).slice(0, 1).map((c) => c.file)),   // one drive
  };
}

// ---- The Copy window -------------------------------------------------------------------------------

function about(seconds) {
  if (!isFinite(seconds) || seconds < 0) return 'Calculating…';
  if (seconds < 5) return 'Less than 5 seconds';
  if (seconds < 60) return `About ${Math.max(5, Math.round(seconds / 5) * 5)} seconds`;
  const m = Math.round(seconds / 60);
  return m === 1 ? 'About a minute' : `About ${m} minutes`;
}

const STATUS = { waiting: 'Waiting', stopped: 'Stopped', done: 'Copied' };

class CopyWindow {
  constructor(batch) {
    this.batch = batch;
    this.count = h('span', { class: 'copy-count snap-text' });
    this.label = h('span', { class: 'copy-label' }, 'Items remaining to be copied:');
    this.bar = h('div', { class: 'copy-bar' });
    this.stop = button('Stop', { className: 'copy-stop', onClick: () => (this.done ? this.close() : batch.stop()) });
    this.now = h('span', { class: 'copy-now' });
    this.summary = h('summary', null, this.now);
    this.list = h('ul', { class: 'copy-items', 'aria-label': 'Items' });
    this.details = h('details', { class: 'disclosure copy-details' }, this.summary, this.list);
    this.rows = new Map();
    const title = `Copy to ${q(batch.dest.label)}`;
    this.win = docWindow(
      { title, icon: null, className: 'copy-win', boxes: ['collapse'], onBox: () => this.win.classList.toggle('is-collapsed') },
      h('div', { class: 'copy-body', role: 'group', 'aria-label': title }, h('p', { class: 'copy-line' }, this.label, this.count), h('div', { class: 'copy-progress' }, this.bar, this.stop), this.details),
    );
    this.win.setAttribute('aria-live', 'polite');
    const dock = document.querySelector('.dock');
    if (dock) dock.prepend(this.win);
    else document.body.append(h('div', { class: 'dock' }, this.win));
    for (const it of batch.items) this.addRow(it);
    this.update();
  }

  addRow(it) {
    const status = h('span', { class: 'copy-status' });
    const mini = h('span', { class: 'copy-mini' });
    const cancel = button('Stop', { variant: 'quiet', icon: 'close', className: 'btn-icon-only copy-cancel', ariaLabel: `Stop copying ${it.name}`, onClick: () => this.batch.cancel(it) });
    const li = h('li', { class: 'copy-item' }, h('span', { class: 'copy-icon', html: fileIcon(it.icon) }), h('span', { class: 'copy-name', title: it.name }, it.name), h('span', { class: 'copy-size' }, it.kind === 'folder' ? plural(it.files, 'item') : formatBytes(it.size)), status, mini, cancel);
    this.rows.set(it, { li, status, mini, cancel });
    this.list.append(li);
  }

  update() {
    const b = this.batch;
    const left = b.items.filter((i) => ['waiting', 'copying', 'processing'].includes(i.state)).length;
    const failed = b.items.filter((i) => i.state === 'failed');
    this.count.textContent = this.done && failed.length ? String(failed.length) : left.toLocaleString();
    this.label.textContent = this.done && failed.length ? "Items that couldn't be copied:" : 'Items remaining to be copied:';
    const total = b.totalBytes || 1;
    const frac = Math.min(1, b.sentBytes / total);
    const cur = b.current;
    this.bar.replaceChildren(progressBar(cur && cur.state === 'processing' && frac >= 0.999 ? null : frac, 'Copy progress'));
    if (this.done) this.now.textContent = failed.length ? 'Show the problems' : 'Done';
    else if (!b.started) this.now.textContent = 'Preparing to copy…';
    else if (cur && cur.state === 'processing') this.now.textContent = `${cur.working || 'Finishing'} ${q(cur.name)}…`;
    else this.now.textContent = `Time remaining: ${about(b.secondsLeft())}`;
    for (const [it, r] of this.rows) {
      let text = STATUS[it.state] || '';
      if (it.state === 'copying') text = `${Math.floor((it.sent / Math.max(1, it.size)) * 100)}%`;
      if (it.state === 'processing') text = `${it.working || 'Finishing'}…`;
      if (it.state === 'failed') text = it.error;
      if (it.state === 'done' && it.toCd) text = 'In the CD-ROM drive list';
      r.status.textContent = text;
      r.status.title = it.notes && it.notes.length ? it.notes.join(' ') : text;
      r.li.className = `copy-item is-${it.state}`;
      r.cancel.hidden = this.done || !['waiting', 'copying'].includes(it.state);
      if (it.state === 'copying') r.mini.replaceChildren(progressBar(it.sent / Math.max(1, it.size), `${it.name} progress`));
      else r.mini.replaceChildren();
    }
    this.stop.querySelector('.btn-label').textContent = this.done ? 'OK' : 'Stop';
    this.stop.classList.toggle('btn-default', !!this.done);
  }

  finish() {
    this.done = true;
    const failed = this.batch.items.some((i) => i.state === 'failed');
    if (failed) this.details.open = true;
    this.update();
    if (!failed) setTimeout(() => this.close(), 700);
    else this.stop.focus({ preventScroll: true });
  }

  close() {
    this.win.remove();
  }
}

// ---- A batch: the items of one drop, copied in order ------------------------------------------------

class Batch {
  constructor(dest, tops, opts) {
    this.dest = dest;
    this.opts = opts;
    this.items = [];
    for (const t of tops) {
      if (t.kind === 'folder') {
        this.items.push({ kind: 'folder', name: t.name, icon: 'folder', size: t.files.reduce((s, x) => s + x.file.size, 0), files: t.files.length, folder: t, sent: 0, state: 'waiting' });
      } else {
        const toCd = opts.cd.has(t.file);
        this.items.push({ kind: toCd ? 'cd' : 'file', name: t.file.name, icon: iconForName(t.file.name), size: t.file.size, file: t.file, sent: 0, state: 'waiting', toCd });
      }
    }
    // AppleDouble header files ("._Name") go after the file they belong to.
    this.items.sort((a, b) => (a.name.startsWith('._') ? 1 : 0) - (b.name.startsWith('._') ? 1 : 0));
    this.totalBytes = this.items.reduce((s, i) => s + i.size, 0);
    this.sentBytes = 0;
    this.started = 0;
    this.current = null;
    this.stopped = false;
    this.ctrl = null;
    this.results = [];
    this.cdItem = null;
  }

  secondsLeft() {
    const t = (Date.now() - this.started) / 1000;
    if (t < 1.5 || this.sentBytes < 1) return NaN;
    return (this.totalBytes - this.sentBytes) / (this.sentBytes / t);
  }

  stop() {
    this.stopped = true;
    for (const it of this.items) if (it.state === 'waiting') it.state = 'stopped';
    if (this.ctrl) this.ctrl.abort();
    this.win.update();
  }

  cancel(it) {
    if (it.state === 'waiting') it.state = 'stopped';
    else if (it === this.current && this.ctrl) this.ctrl.abort();
    this.win.update();
  }

  async run() {
    this.win = new CopyWindow(this);
    this.started = Date.now();
    const tick = setInterval(() => this.win.update(), 500);
    try {
      for (const it of this.items) {
        if (this.stopped || it.state !== 'waiting') continue;
        this.current = it;
        const before = this.sentBytes;
        try {
          if (it.kind === 'folder') await this.copyFolder(it);
          else if (it.kind === 'cd') await this.copyCd(it);
          else await this.copyFile(it, this.dest.path, '', it);
          it.state = 'done';
        } catch (err) {
          if (err.code === 'stopped') it.state = 'stopped';
          else {
            it.state = 'failed';
            it.error = friendlyMessage(err);
          }
          this.sentBytes = before + it.size;
        }
        this.win.update();
      }
    } finally {
      clearInterval(tick);
      this.current = null;
      this.win.finish();
    }
    announce(this.dest.path);
    return this;
  }

  async copyFile(file, path, rel, row) {
    const it = row;
    const f = file.file || file;
    this.ctrl = new AbortController();
    it.state = 'copying';
    const base = this.sentBytes;
    const done = await sendFile(uploadUrl(path, f, rel, this.opts), f, {
      signal: this.ctrl.signal,
      onProgress: (loaded) => {
        if (it.kind === 'folder') it.sent = (it.sentBefore || 0) + loaded;
        else it.sent = loaded;
        this.sentBytes = base + loaded;
      },
      onSent: () => {
        if (it.kind === 'folder') return;
        it.state = 'processing';
        it.working = /\.(hqx|bin)$/i.test(f.name) ? 'Decoding' : isStuffit(f.name) && this.opts.expand ? 'Expanding' : 'Finishing';
        this.win.update();
      },
    });
    this.ctrl = null;
    it.notes = [...(it.notes || []), ...(done.notes || [])];
    this.results.push(...(done.items || []));
    return done;
  }

  async copyFolder(it) {
    const t = it.folder;
    it.state = 'copying';
    const made = await post('/api/v1/share/mkdir', { path: this.dest.path, name: t.name, conflict: 'rename', sanitize: true });
    const top = made.item.path;
    if (made.item.mac_name !== t.name) it.notes = [`Named ${q(made.item.mac_name)} in Mac OS.`];
    it.sentBefore = 0;
    // "._Name" AppleDouble files after the files they describe
    const files = [...t.files].sort((a, b) => (a.file.name.startsWith('._') ? 1 : 0) - (b.file.name.startsWith('._') ? 1 : 0));
    let failures = 0;
    for (const x of files) {
      if (this.stopped || it.state === 'stopped') throw new ApiError(0, 'stopped', 'Stopped.');
      it.working = x.file.name;
      try {
        await this.copyFile(x, top, x.rel, it);
      } catch (err) {
        if (err.code === 'stopped') throw err;
        failures += 1;
        it.notes = [...(it.notes || []), `${x.file.name}: ${friendlyMessage(err)}`];
      }
      it.sentBefore += x.file.size;
      it.state = 'copying';
    }
    for (const d of [...t.dirs].sort((a, b) => a.split('/').length - b.split('/').length)) {
      const parts = d.split('/');
      try {
        await post('/api/v1/share/mkdir', { path: top, relpath: parts.slice(0, -1).join('/'), name: parts[parts.length - 1], sanitize: true });
      } catch (err) {
        if (err.code !== 'name-exists') it.notes = [...(it.notes || []), `${d}: ${friendlyMessage(err)}`];
      }
    }
    if (failures) throw new ApiError(0, 'partial', `${plural(failures, 'item')} inside couldn't be copied. ${it.notes.slice(-1)[0] || ''}`);
  }

  async copyCd(it) {
    this.ctrl = new AbortController();
    it.state = 'copying';
    const base = this.sentBytes;
    const res = await sendFile(`/api/v1/media?name=${encodeURIComponent(it.file.name)}`, it.file, {
      signal: this.ctrl.signal,
      onProgress: (loaded) => {
        it.sent = loaded;
        this.sentBytes = base + loaded;
      },
      onSent: () => {
        it.state = 'processing';
        it.working = 'Checking';
        this.win.update();
      },
    }).catch(async (err) => {
      if (err.code !== 'unsupported-media') throw err;
      // Not a disc RetroBoot can put in the drive (a UDIF .dmg, say): copy it instead.
      this.sentBytes = base;
      it.kind = 'file';
      it.toCd = false;
      it.notes = [`It isn't a CD image RetroBoot can use, so it was copied to ${q(this.dest.label)} instead.`];
      await this.copyFile(it, this.dest.path, '', it);
      return null;
    });
    this.ctrl = null;
    if (res) this.cdItem = res.item;
  }
}

// ---- Entry point -------------------------------------------------------------------------------------

let running = null;

/** Copy dropped or picked entries into a folder of the share. dest: {path, label}. */
export async function copyToMac(tops, dest = { path: '', label: shareState.volume }) {
  tops = tops.filter(Boolean);
  if (!tops.length) {
    toast('Nothing to copy: the drop had no files.', { level: 'warning' });
    return null;
  }
  if (running) {
    toast('Wait for the current copy to finish, then try again.', { level: 'warning' });
    return null;
  }
  running = true;
  try {
    let share = shareState.state;
    try {
      const listing = await get(`/api/v1/share/list?path=${encodeURIComponent(dest.path || '')}`);
      noteShareState(listing.share);
      share = listing.share;
      if (!dest.label) dest.label = shareState.volume;
    } catch (err) {
      toast(`Can't open ${q(dest.label || shareState.volume)}. ${friendlyMessage(err)}`, { level: 'error', timeout: 9000 });
      return null;
    }
    const names = tops.map((t) => (t.kind === 'folder' ? t.name : t.file.name));
    let conflicts = [];
    try {
      const c = await post('/api/v1/share/check', { path: dest.path || '', names });
      conflicts = (c.items || []).filter((i) => i.exists);
    } catch {
      conflicts = [];
    }
    const tooBig = tops.filter((t) => t.kind === 'file' && share && t.file.size > share.max_file_bytes && !isCd(t.file.name));
    if (tooBig.length) {
      await dialog({ title: `${q(tooBig[0].file.name)} is too large`, icon: 'caution', message: `Mac OS 9 can use files up to 2 GB, and ${q(tooBig[0].file.name)} is ${formatBytes(tooBig[0].file.size)}. Disc images that large can go in the CD-ROM drive instead.`, buttons: [{ label: 'OK', value: 'ok', variant: 'default', autofocus: true }] });
      tops = tops.filter((t) => !tooBig.includes(t));
      if (!tops.length) return null;
    }
    const opts = await optionsSheet({ dest, tops, conflicts: conflicts.filter((c) => !tops.some((t) => t.kind === 'file' && t.file.name === c.name && isCd(t.file.name))), share });
    if (!opts) return null;
    const batch = new Batch(dest, tops, opts);
    await batch.run();
    running = false;            // a disc question or a settings job doesn't hold up the next copy
    await afterCopy(batch);
    return batch;
  } finally {
    running = false;
  }
}

async function afterCopy(batch) {
  const ok = batch.items.filter((i) => i.state === 'done');
  const files = ok.filter((i) => i.kind !== 'cd' || !batch.cdItem);
  if (files.length) {
    const one = files.length === 1;
    const what = one ? q(files[0].name) : plural(files.length, 'item');
    const notes = one && files[0].notes && files[0].notes.length ? ` ${files[0].notes.join(' ')}` : '';
    // The Finder doesn't look at an open window of this volume again by itself (classicvirtio
    // reports no folder dates; web/reports/webfiles-2026-10-11.md), so say how to see new items.
    const later = guestRunning()
      ? ` If ${q(shareState.volume)} is already open in Mac OS, close its window and open it again to see ${one ? 'it' : 'them'}.`
      : ` Mac OS shows ${one ? 'it' : 'them'} in ${q(shareState.volume)} when it starts.`;
    toast(`Copied ${what} to ${q(batch.dest.label)}.${notes}${later}`, { level: 'ok', timeout: 7000 });
  }
  const cd = ok.find((i) => i.kind === 'cd');
  if (cd && batch.cdItem) await insertCd(batch.cdItem);
}

/** Put a media-library CD image in the CD-ROM drive (setting media.cd); Mac OS sees it when it starts. */
export async function insertCd(item) {
  // ISO volume names are padded (hdiutil pads with NULs, others with spaces)
  const name = String(item.volume_name || '').replace(/[\u0000\s]+$/, '') || item.name;
  const macOn = guestRunning();
  let restart = false;
  if (macOn) {
    const v = await dialog({
      title: `${q(name)} is ready for the CD-ROM drive`,
      icon: 'cd',
      message: ['Mac OS notices a new disc when it starts. Restart Mac OS now to put it in the drive?', 'Restart asks Mac OS to quit its applications first, like the Special menu does.'],
      buttons: [
        { label: 'Later', value: 'later' },
        { label: 'Restart Mac OS', value: 'restart', variant: 'default', autofocus: true },
      ],
    });
    restart = v === 'restart';
  }
  try {
    const final = await setSetting('media.cd', item.id, { restart, label: restart ? `Restarting Mac OS with ${q(name)} in the CD-ROM drive` : 'Choosing the CD' });
    if (final && final.state === 'failed') throw new ApiError(0, (final.error && final.error.code) || 'failed', (final.error && final.error.message) || final.message || 'The disc could not be chosen.');
    toast(restart ? `${q(name)} is in the CD-ROM drive.` : macOn ? `${q(name)} goes in the CD-ROM drive the next time Mac OS starts.` : `${q(name)} is in the CD-ROM drive for the next Mac OS start.`, { level: 'ok', timeout: 7000 });
  } catch (err) {
    toast(`${q(name)} was added to the media library but couldn't be put in the drive. ${friendlyMessage(err)}`, { level: 'error', timeout: 10000 });
  }
}

// ---- Drag and drop anywhere a drop makes sense ---------------------------------------------------------

const hasFiles = (e) => !!(e.dataTransfer && [...(e.dataTransfer.types || [])].includes('Files'));

function dropZone(target) {
  if (!(target instanceof Element)) return null;
  const files = target.closest('[data-share-drop]');
  if (files) return { el: files, dest: { path: files.dataset.sharePath || '', label: files.dataset.shareLabel || shareState.volume } };
  const screen = target.closest('.screen, .screen-frame, [data-screen-drop]');
  if (screen) {
    const el = screen.closest('.screen') || screen;
    return { el, dest: { path: '', label: shareState.volume }, screen: true };
  }
  return null;
}

let lit = null;
let litTimer = null;
function light(zone) {
  if (lit && (!zone || lit !== zone.el)) {
    lit.classList.remove('is-drop-target');
    lit = null;
  }
  if (zone && !lit) {
    lit = zone.el;
    lit.classList.add('is-drop-target');
    if (zone.screen) lit.dataset.dropLabel = `Copy to ${q(shareState.volume)}`;
  }
  clearTimeout(litTimer);
  if (zone) litTimer = setTimeout(() => light(null), 250);
}

document.addEventListener('dragover', (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();          // never let the browser open a dropped file and leave the app
  const zone = document.body.classList.contains('is-app') ? dropZone(e.target) : null;
  e.dataTransfer.dropEffect = zone ? 'copy' : 'none';
  light(zone);
});

document.addEventListener('drop', (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  const zone = document.body.classList.contains('is-app') ? dropZone(e.target) : null;
  light(null);
  if (!zone) return;
  const reading = readDrop(e.dataTransfer);
  reading
    .then((tops) => copyToMac(tops, zone.dest))
    .catch((err) => toast(`The dropped items couldn't be read. ${friendlyMessage(err)}`, { level: 'error' }));
});

