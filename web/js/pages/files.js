// Files: the shared folder Mac OS shows as "RetroBoot Share", as a Finder list view.
// Drop files or folders anywhere on the page to copy them to Mac OS (filecopy.js); select an
// item to download, rename or delete it.  GET /api/v1/share/list (hostd retroboot/share.py).

import { get, post, friendlyMessage } from '../api.js';
import { h, Scope, formatBytes, plural, uid, toDate } from '../util.js';
import { button, notice, loading, errorState, dialog, confirm, toast, pageHeader } from '../ui.js';
import { subscribe, guestRunning } from '../store.js';
import { fileIcon } from '../fileicons.js';
import { copyToMac, readPicked, onShareChanged, noteShareState, setSetting, shareState, DEFAULT_VOLUME } from '../filecopy.js';
import { lifecycle } from '../actions.js';

export const meta = { title: 'Files', icon: 'storage' };

const q = (s) => `“${s}”`;
const dateFmt = new Intl.DateTimeFormat(undefined, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' });
const timeFmt = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });

/** The Finder's list-view dates: "Today, 2:18 PM", "Yesterday, ...", "Fri, Sep 11, 2026, 1:26 PM". */
function finderDate(v) {
  const d = toDate(v);
  if (!d) return '—';
  const day = new Date();
  day.setHours(0, 0, 0, 0);
  if (d >= day) return `Today, ${timeFmt.format(d)}`;
  if (d >= new Date(day.getTime() - 86400000)) return `Yesterday, ${timeFmt.format(d)}`;
  return dateFmt.format(d);
}

/** The Finder's sizes: "4K", "1.2 MB", "--" for folders (it counts 1K blocks, rounded up). */
function finderSize(it) {
  if (it.kind === 'folder') return '--';
  const n = it.bytes || 0;
  if (n < 1024 * 1000) return `${Math.max(1, Math.ceil(n / 1024))}K`;
  return formatBytes(n);
}

function pathFromHash() {
  const m = /[?&]path=([^&]*)/.exec(location.hash);
  return m ? decodeURIComponent(m[1]) : '';
}

const COLUMNS = [
  { key: 'name', label: 'Name' },
  { key: 'modified', label: 'Date Modified' },
  { key: 'bytes', label: 'Size' },
  { key: 'kind', label: 'Kind' },
];

export function render() {
  const scope = new Scope();
  let path = pathFromHash();
  let data = null;
  let loadError = null;
  let selected = null; // item path
  let sort = { key: 'name', dir: 1 };
  let loadSeq = 0;

  const volume = () => (data && data.share && data.share.volume_name) || shareState.volume || DEFAULT_VOLUME;
  const folderLabel = () => (data && data.crumbs && data.crumbs.length ? data.crumbs[data.crumbs.length - 1].mac_name : volume());

  const header = pageHeader('storage', volume(), 'Loading…');
  const title = header.querySelector('h1');
  const intro = header.querySelector('.page-intro-text');
  const notices = h('div', { class: 'files-notices' });
  const pathEl = h('div', { class: 'files-path' });
  const listEl = h('div', { class: 'files-list-wrap' }, loading());
  const selEl = h('p', { class: 'files-selection small', 'aria-live': 'polite' });

  const pickFiles = h('input', { type: 'file', multiple: true, hidden: true, onchange: () => picked(pickFiles) });
  const pickFolder = h('input', { type: 'file', multiple: true, hidden: true, webkitdirectory: true, onchange: () => picked(pickFolder) });
  const folderSupported = 'webkitdirectory' in pickFolder;

  const btns = {
    copy: button('Copy to Mac…', { icon: 'plus', className: 'btn-small', onClick: () => pickFiles.click(), title: 'Choose files on this device to copy to Mac OS' }),
    folder: folderSupported ? button('Copy Folder…', { className: 'btn-small', onClick: () => pickFolder.click(), title: 'Choose a folder on this device to copy to Mac OS' }) : null,
    mkdir: button('New Folder', { className: 'btn-small', onClick: () => newFolder() }),
    download: button('Download', { icon: 'download', className: 'btn-small', onClick: () => download(selectedItem()) }),
    rename: button('Rename…', { className: 'btn-small', onClick: () => rename(selectedItem()) }),
    del: button('Delete…', { icon: 'trash', className: 'btn-small', onClick: () => remove(selectedItem()) }),
  };
  const toolbar = h('div', { class: 'files-toolbar', role: 'toolbar', 'aria-label': 'Shared folder' }, pathEl, h('div', { class: 'files-buttons' }, btns.copy, btns.folder, btns.mkdir, h('span', { class: 'files-sep', 'aria-hidden': 'true' }), btns.download, btns.rename, btns.del));

  const hint = h(
    'p',
    { class: 'files-hint small' },
    'Drag files or folders here to copy them to Mac OS, or drop them on the Mac OS screen on the Overview. Mac files in MacBinary (.bin), BinHex (.hqx) or AppleSingle keep their resource forks; StuffIt archives can be expanded. ',
    'If this folder’s window is already open in Mac OS, close it and open it again to see new items.',
  );

  const el = h('div', { class: 'page page-files', dataset: { shareDrop: '', sharePath: path, shareLabel: volume() } }, header, toolbar, notices, listEl, selEl, hint, pickFiles, pickFolder);

  // -- loading --------------------------------------------------------------------------------
  let lastJson = '';
  async function load({ keepSelection = true } = {}) {
    const seq = ++loadSeq;
    try {
      const res = await get(`/api/v1/share/list?path=${encodeURIComponent(path)}`);
      if (seq !== loadSeq) return;
      // Redraw only when something changed: a redraw between the two clicks of a
      // double-click (or under the pointer) would lose them.
      const json = JSON.stringify(res);
      const same = json === lastJson && keepSelection && !loadError;
      lastJson = json;
      data = res;
      loadError = null;
      noteShareState(res.share);
      if (!keepSelection || !res.items.some((i) => i.path === selected)) selected = null;
      if (same) {
        drawNotices();
        return;
      }
    } catch (err) {
      if (seq !== loadSeq) return;
      loadError = err;
      if (err.status === 404 && path) {
        toast(`${friendlyMessage(err)} Showing ${q(volume())} instead.`, { level: 'warning' });
        go('');
        return;
      }
    }
    draw();
  }

  function go(next) {
    path = next || '';
    selected = null;
    el.dataset.sharePath = path;
    const hash = path ? `#/files?path=${encodeURIComponent(path)}` : '#/files';
    if (location.hash !== hash) history.pushState(null, '', hash);
    listEl.replaceChildren(loading());
    load({ keepSelection: false });
  }

  const onHash = () => {
    if (!location.hash.startsWith('#/files')) return;
    const p = pathFromHash();
    if (p !== path) {
      path = p;
      el.dataset.sharePath = path;
      selected = null;
      load({ keepSelection: false });
    }
  };
  window.addEventListener('hashchange', onHash);
  window.addEventListener('popstate', onHash);
  scope.add(() => window.removeEventListener('hashchange', onHash));
  scope.add(() => window.removeEventListener('popstate', onHash));
  scope.add(onShareChanged((p) => (p === path || !p ? load() : null)));
  scope.interval(() => document.visibilityState === 'visible' && load(), 5000);
  scope.add(subscribe('status', () => drawNotices()));

  // -- drawing --------------------------------------------------------------------------------
  function sorted(items) {
    const k = sort.key;
    const val = (i) => (k === 'name' ? i.mac_name.toLocaleLowerCase() : k === 'modified' ? i.modified || '' : k === 'bytes' ? (i.kind === 'folder' ? -1 : i.bytes || 0) : (i.kind_label || '').toLocaleLowerCase());
    return [...items].sort((a, b) => {
      const va = val(a);
      const vb = val(b);
      const c = va < vb ? -1 : va > vb ? 1 : a.mac_name.localeCompare(b.mac_name);
      return c * sort.dir;
    });
  }

  function draw() {
    const vol = volume();
    title.textContent = folderLabel();
    el.dataset.shareLabel = folderLabel();
    document.title = `${folderLabel()} · RetroBoot`;
    if (data) {
      const n = data.items.length;
      intro.textContent = `${plural(n, 'item')}, ${formatBytes(data.free_bytes)} available`;
    } else intro.textContent = '';
    drawPath(vol);
    drawNotices();
    if (loadError && !data) {
      listEl.replaceChildren(errorState(loadError, () => load()));
      drawSelection();
      return;
    }
    if (!data) return;
    const rows = sorted(data.items).map(row);
    const head = h('div', { class: 'fl-head', role: 'row' }, ...COLUMNS.map((c) => h('button', { type: 'button', role: 'columnheader', class: ['fl-col', `fl-${c.key}`, sort.key === c.key && 'is-sorted'], 'aria-sort': sort.key === c.key ? (sort.dir > 0 ? 'ascending' : 'descending') : 'none', onclick: () => setSort(c.key) }, c.label)), h('button', { type: 'button', class: 'fl-dir', 'aria-label': sort.dir > 0 ? 'Sort descending' : 'Sort ascending', onclick: () => setSort(sort.key, -sort.dir) }));
    const body = rows.length
      ? h('div', { class: 'fl-body', role: 'rowgroup' }, ...rows)
      : h('div', { class: 'fl-empty' }, h('p', null, path ? 'This folder is empty.' : `${q(vol)} is empty.`), h('p', { class: 'small' }, 'Drag files here, or use Copy to Mac.'));
    const list = h('div', { class: ['finder-list', sort.key && `sorted-${sort.key}`], role: 'grid', 'aria-label': `Contents of ${folderLabel()}`, 'aria-rowcount': String(rows.length + 1), tabindex: rows.length ? null : '0', onkeydown: onKey }, head, body);
    const hadFocus = listEl.contains(document.activeElement);
    listEl.replaceChildren(list);
    if (hadFocus) {
      const again = listEl.querySelector('.fl-row.is-selected') || listEl.querySelector('.fl-row');
      if (again) again.focus({ preventScroll: true });
    }
    drawSelection();
  }

  function row(it) {
    const isSel = it.path === selected;
    const badges = [];
    if (it.open) badges.push(h('span', { class: 'fl-badge', title: 'Mac OS has this file open' }, 'Open in Mac OS'));
    if (!it.visible) badges.push(h('span', { class: 'fl-badge', title: "Mac OS can't show this name (longer than 93 bytes)" }, 'Not shown in Mac OS'));
    const r = h(
      'div',
      {
        class: ['fl-row', isSel && 'is-selected', it.open && 'is-open'],
        role: 'row',
        tabindex: isSel || (!selected && it === sorted(data.items)[0]) ? '0' : '-1',
        'aria-selected': String(isSel),
        dataset: { path: it.path },
        onclick: () => select(it.path),
        ondblclick: () => openItem(it),
      },
      h('span', { class: 'fl-cell fl-name', role: 'gridcell' }, h('span', { class: 'fl-icon', html: fileIcon(it.icon || (it.kind === 'folder' ? 'folder' : 'document')) }), h('span', { class: 'fl-text' }, it.mac_name), ...badges),
      h('span', { class: 'fl-cell fl-modified', role: 'gridcell' }, finderDate(it.modified)),
      h('span', { class: 'fl-cell fl-bytes', role: 'gridcell' }, finderSize(it)),
      h('span', { class: 'fl-cell fl-kind', role: 'gridcell' }, it.kind_label || (it.kind === 'folder' ? 'folder' : 'document')),
    );
    return r;
  }

  function drawPath(vol) {
    const crumbs = [{ mac_name: vol, path: '' }, ...((data && data.crumbs) || [])];
    const sel = h(
      'select',
      { 'aria-label': 'Folder', onchange: (e) => go(e.target.value) },
      ...crumbs.map((c, i) => h('option', { value: c.path, selected: i === crumbs.length - 1 }, `${' '.repeat(i)}${c.mac_name}`)),
    );
    const up = button('Enclosing Folder', { className: 'btn-small', disabled: crumbs.length < 2, onClick: () => go(crumbs[crumbs.length - 2].path) });
    pathEl.replaceChildren(h('span', { class: 'select-wrap' }, sel), up);
  }

  function drawNotices() {
    const st = data && data.share;
    const out = [];
    if (st && !st.enabled) {
      out.push(
        notice({
          level: 'warning',
          title: 'The shared folder is turned off',
          message: `Mac OS doesn't show ${q(st.volume_name)} while sharing is off. Files you copy here wait until it's turned on.`,
          actions: [button('Turn On Sharing', { variant: 'default', className: 'btn-small', onClick: turnOn })],
        }),
      );
    } else if (st && st.pending_restart) {
      out.push(
        notice({
          level: 'info',
          title: 'Sharing changes when Mac OS restarts',
          message: `Mac OS picks up the shared folder setting the next time it starts.`,
          actions: guestRunning() ? [button('Restart Mac OS…', { className: 'btn-small', onClick: () => lifecycle('restart') })] : null,
        }),
      );
    } else if (st && !st.guest_running) {
      out.push(h('p', { class: 'files-note small' }, `Mac OS is shut down. Items copied now appear in ${q(st.volume_name)} when it starts.`));
    }
    notices.replaceChildren(...out);
  }

  function drawSelection() {
    const it = selectedItem();
    const busyOpen = it && it.open;
    btns.download.disabled = !it || it.kind === 'folder';
    btns.rename.disabled = !it || busyOpen;
    btns.del.disabled = !it || busyOpen;
    for (const b of [btns.rename, btns.del]) b.title = busyOpen ? `${it.mac_name} is open in Mac OS. Close it there first.` : '';
    // The label stays the same: a wider button would re-wrap the toolbar and move the list
    // under the pointer between the two clicks of a double-click.  The line below the list
    // says when a file downloads as MacBinary.
    btns.download.title = it && it.download === 'macbinary' ? 'Downloads as MacBinary (.bin), keeping the resource fork' : '';
    if (!it) {
      selEl.replaceChildren();
      return;
    }
    const parts = [h('strong', null, it.mac_name), ` — ${it.kind_label || it.kind}`];
    if (it.kind === 'file') {
      parts.push(`, ${formatBytes(it.data_bytes)} data`);
      if (it.rsrc_bytes) parts.push(` + ${formatBytes(it.rsrc_bytes)} resource fork`);
      if (it.type) parts.push(`, type ${it.type}/${it.creator || '????'}`);
      parts.push('.');
      if (it.download === 'macbinary') parts.push(' It downloads as MacBinary (.bin) so the resource fork survives; ', h('a', { href: downloadUrl(it, 'data'), download: it.mac_name }, 'download the data fork only'), '.');
      if (it.open) parts.push(' Mac OS has it open.');
    }
    selEl.replaceChildren(...parts);
  }

  // -- selection and keys -------------------------------------------------------------------------
  const selectedItem = () => (data && selected ? data.items.find((i) => i.path === selected) : null);

  function select(p, focus = false) {
    selected = p;
    for (const r of listEl.querySelectorAll('.fl-row')) {
      const on = r.dataset.path === p;
      r.classList.toggle('is-selected', on);
      r.setAttribute('aria-selected', String(on));
      r.tabIndex = on ? 0 : -1;
      if (on && focus) r.focus();
    }
    drawSelection();
  }

  function onKey(e) {
    const rows = [...listEl.querySelectorAll('.fl-row')];
    if (!rows.length) return;
    const i = rows.findIndex((r) => r.dataset.path === selected);
    const it = selectedItem();
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const n = Math.max(0, Math.min(rows.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)));
      select(rows[n].dataset.path, true);
    } else if (e.key === 'Enter' && it) {
      e.preventDefault();
      openItem(it);
    } else if ((e.key === 'Delete' || e.key === 'Backspace') && it) {
      e.preventDefault();
      remove(it);
    } else if (e.key === 'ArrowLeft' && (e.metaKey || e.ctrlKey) && path) {
      e.preventDefault();
      go(path.split('/').slice(0, -1).join('/'));
    }
  }

  function setSort(key, dir) {
    sort = { key, dir: dir || (sort.key === key ? -sort.dir : 1) };
    draw();
  }

  // -- actions ----------------------------------------------------------------------------------------
  function downloadUrl(it, format = 'auto') {
    return `/api/v1/share/download?path=${encodeURIComponent(it.path)}${format === 'auto' ? '' : `&format=${format}`}`;
  }

  function openItem(it) {
    if (it.kind === 'folder') go(it.path);
    else download(it);
  }

  function download(it) {
    if (!it || it.kind === 'folder') return;
    const a = h('a', { href: downloadUrl(it), download: it.download === 'macbinary' ? `${it.mac_name}.bin` : it.mac_name, class: 'visually-hidden' });
    document.body.append(a);
    a.click();
    a.remove();
    toast(`Downloading ${q(it.mac_name)}${it.download === 'macbinary' ? ' as MacBinary' : ''}.`, { timeout: 3500 });
  }

  async function picked(input) {
    const tops = readPicked(input.files);
    input.value = '';
    if (tops.length) await copyToMac(tops, { path, label: folderLabel() });
  }

  function nameField(value) {
    const id = uid('name');
    const err = h('p', { class: 'setting-error', id: `${id}-err`, hidden: true });
    const input = h('input', { type: 'text', id, value, maxlength: '31', autocomplete: 'off', spellcheck: 'false', 'aria-describedby': `${id}-err`, class: 'field' });
    const wrap = h('div', { class: 'dlg-section' }, h('label', { for: id, class: 'setting-label' }, 'Name:'), input, err);
    const show = (msg) => {
      err.textContent = msg || '';
      err.hidden = !msg;
      input.setAttribute('aria-invalid', msg ? 'true' : 'false');
    };
    return { input, wrap, show };
  }

  async function askName({ title, label, initial, run }) {
    const f = nameField(initial);
    let result = null;
    await dialog({
      title,
      icon: 'note',
      content: f.wrap,
      buttons: [
        { label: 'Cancel', value: 'cancel' },
        { label, value: 'ok', variant: 'default' },
      ],
      onOpen: () => {
        f.input.focus();
        const dot = initial.lastIndexOf('.');
        f.input.setSelectionRange(0, dot > 0 ? dot : initial.length);
      },
      validate: (value, d) => {
        if (value !== 'ok') return true;
        const name = f.input.value.trim();
        if (!name) {
          f.show('Type a name.');
          return false;
        }
        if (name.includes(':')) {
          f.show("Names can't contain a colon (:) in Mac OS.");
          return false;
        }
        // The server checks the exact rules (Mac OS Roman, 31 bytes); run it, then close.
        run(name)
          .then((r) => {
            result = r;
            d.close('ok');
          })
          .catch((err) => f.show(friendlyMessage(err)));
        return false;
      },
    });
    return result;
  }

  async function newFolder() {
    const r = await askName({ title: `New folder in ${q(folderLabel())}`, label: 'Create', initial: 'untitled folder', run: (name) => post('/api/v1/share/mkdir', { path, name, conflict: name === 'untitled folder' ? 'rename' : 'fail' }) });
    if (r && r.item) {
      selected = r.item.path;
      await load();
      select(r.item.path, true);
    }
  }

  async function rename(it) {
    if (!it) return;
    const r = await askName({ title: `Rename ${q(it.mac_name)}`, label: 'Rename', initial: it.mac_name, run: (name) => post('/api/v1/share/rename', { path: it.path, name }) });
    if (r && r.item) {
      selected = r.item.path;
      await load();
      select(r.item.path, true);
    }
  }

  async function remove(it) {
    if (!it) return;
    if (it.open) {
      toast(`${q(it.mac_name)} is open in Mac OS. Close it there first.`, { level: 'warning' });
      return;
    }
    const unknownOpen = it.open == null && data && !data.open_detection && guestRunning();
    const folder = it.kind === 'folder';
    const ok = await confirm({
      title: `Delete ${q(it.mac_name)}?`,
      message: [
        folder ? `${q(it.mac_name)} and everything in it are removed from ${q(volume())} right away.` : `${q(it.mac_name)} is removed from ${q(volume())} right away.`,
        "This can't be undone; it doesn't go to the Trash.",
        unknownOpen ? 'If Mac OS is using it, close it there first.' : null,
      ].filter(Boolean),
      confirmLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    try {
      const res = await post('/api/v1/share/delete', { path: it.path, recursive: folder });
      toast(folder && res.items ? `Deleted ${q(it.mac_name)} and ${plural(res.items, 'item')} in it.` : `Deleted ${q(it.mac_name)}.`, { level: 'ok' });
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error', timeout: 8000 });
    }
    await load({ keepSelection: false });
  }

  async function turnOn() {
    let restart = false;
    if (guestRunning()) {
      const v = await dialog({
        title: 'Turn on the shared folder?',
        icon: 'note',
        message: [`Mac OS shows ${q(volume())} after it restarts.`, 'Restart now, or let it appear the next time Mac OS starts.'],
        buttons: [
          { label: 'Cancel', value: 'cancel' },
          { label: 'Later', value: 'later' },
          { label: 'Restart Mac OS', value: 'restart', variant: 'default', autofocus: true },
        ],
      });
      if (!v || v === 'cancel') return;
      restart = v === 'restart';
    }
    try {
      await setSetting('share.enabled', true, { restart, label: restart ? 'Turning on sharing and restarting Mac OS' : 'Turning on sharing' });
      toast(restart ? `${q(volume())} is on.` : `Sharing is on. ${q(volume())} appears in Mac OS the next time it starts.`, { level: 'ok', timeout: 7000 });
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error', timeout: 8000 });
    }
    load();
  }

  load();
  return { el, destroy: () => scope.destroy() };
}

