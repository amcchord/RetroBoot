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

const EXT_IMG = new Set(['png', 'jpg', 'jpeg', 'gif', 'bmp', 'tif', 'tiff', 'pict', 'pct']);
const EXT_PDF = new Set(['pdf']);
const EXT_TEXT = new Set(['txt', 'text', 'md', 'log', 'csv', 'json', 'xml', 'htm', 'html', 'ini', 'cfg', 'rtf']);

function extOf(name) {
  const i = (name || '').lastIndexOf('.');
  return i > 0 ? name.slice(i + 1).toLowerCase() : '';
}

function previewKind(it) {
  if (!it || it.kind !== 'file') return null;
  const t = (it.type || '').trim();
  const ext = extOf(it.mac_name || it.path);
  const lbl = (it.kind_label || '').toLowerCase();
  if (EXT_IMG.has(ext) || t === 'PICT' || t === 'PNGf' || t === 'JPEG' || t === 'GIFf' || lbl.includes('pict') || lbl.includes('picture') || lbl.includes('image')) return 'image';
  if (EXT_PDF.has(ext) || t === 'PDF' || it.icon === 'pdf' || lbl.includes('pdf')) return 'pdf';
  if (EXT_TEXT.has(ext) || t === 'TEXT' || t === 'ttro' || it.icon === 'text' || lbl.includes('text')) return 'text';
  return null;
}

function previewUrl(relPath) {
  return `/api/v1/files/preview?path=${encodeURIComponent(relPath)}`;
}

function pathFromHash() {
  const m = /[?&]path=([^&]*)/.exec(location.hash);
  return m ? decodeURIComponent(m[1]) : '';
}

function previewFromHash() {
  const m = /[?&]preview=([^&]*)/.exec(location.hash);
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
  let pendingPreview = previewFromHash();
  let data = null;
  let usbDrives = [];
  let loadError = null;
  let selected = null; // item path
  let sort = { key: 'name', dir: 1 };
  let loadSeq = 0;
  let draggingPath = null;

  const volume = () => (data && data.share && data.share.volume_name) || shareState.volume || DEFAULT_VOLUME;
  const folderLabel = () => (data && data.crumbs && data.crumbs.length ? data.crumbs[data.crumbs.length - 1].mac_name : volume());

  const header = pageHeader('storage', volume(), 'Loading…');
  const title = header.querySelector('h1');
  const intro = header.querySelector('.page-intro-text');
  const notices = h('div', { class: 'files-notices' });
  const usbEl = h('div', { class: 'files-usb' });
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
    preview: button('Preview', { className: 'btn-small', onClick: () => previewItem(selectedItem()), title: 'Preview the selected image, PDF, or text file' }),
    download: button('Download', { icon: 'download', className: 'btn-small', onClick: () => download(selectedItem()) }),
    move: button('Move…', { className: 'btn-small', onClick: () => moveDialog(selectedItem()), title: 'Move the selected item to another folder' }),
    rename: button('Rename…', { className: 'btn-small', onClick: () => rename(selectedItem()) }),
    del: button('Delete…', { icon: 'trash', className: 'btn-small', onClick: () => remove(selectedItem()) }),
  };
  const toolbar = h('div', { class: 'files-toolbar', role: 'toolbar', 'aria-label': 'Shared folder' }, pathEl, h('div', { class: 'files-buttons' }, btns.copy, btns.folder, btns.mkdir, h('span', { class: 'files-sep', 'aria-hidden': 'true' }), btns.preview, btns.download, btns.move, btns.rename, btns.del));

  const hint = h(
    'p',
    { class: 'files-hint small' },
    'Drag files or folders here to copy them to Mac OS, or drop them on the Mac OS screen on the Overview. Mac files in MacBinary (.bin), BinHex (.hqx) or AppleSingle keep their resource forks; StuffIt archives can be expanded. ',
    'If this folder’s window is already open in Mac OS, close it and open it again to see new items.',
  );

  const el = h('div', { class: 'page page-files', dataset: { shareDrop: '', sharePath: path, shareLabel: volume() } }, header, toolbar, notices, usbEl, listEl, selEl, hint, pickFiles, pickFolder);

  // -- loading --------------------------------------------------------------------------------
  let lastJson = '';
  let lastUsbJson = '';
  async function loadUsb() {
    try {
      const res = await get('/api/v1/usb/drives');
      const drives = (res && res.drives) || [];
      const json = JSON.stringify(drives);
      if (json === lastUsbJson) return;
      lastUsbJson = json;
      usbDrives = drives;
      drawUsb();
    } catch {
      /* usb endpoint unavailable or empty */
    }
  }

  async function load({ keepSelection = true } = {}) {
    const seq = ++loadSeq;
    loadUsb();
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
        maybeOpenHashPreview();
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
    maybeOpenHashPreview();
  }

  function maybeOpenHashPreview() {
    if (!pendingPreview) return;
    const rel = pendingPreview;
    pendingPreview = '';
    const hash = path ? `#/files?path=${encodeURIComponent(path)}` : '#/files';
    if (location.hash !== hash) history.replaceState(null, '', hash);
    const hit = (data && data.items && data.items.find((i) => i.path === rel)) || {
      path: rel,
      mac_name: rel.split('/').pop() || rel,
      kind: 'file',
      type: extOf(rel) === 'pdf' ? 'PDF' : '',
    };
    if (previewKind(hit)) previewItem(hit);
    else download(hit);
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
    const pv = previewFromHash();
    if (pv) pendingPreview = pv;
    if (p !== path) {
      path = p;
      el.dataset.sharePath = path;
      selected = null;
      load({ keepSelection: false });
    } else if (pv) {
      load({ keepSelection: true });
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
    const isMoveDrag = (e) => !!(e.dataTransfer && [...(e.dataTransfer.types || [])].includes('application/x-retroboot-move'));
    const canDropInto = it.kind === 'folder';
    const r = h(
      'div',
      {
        class: ['fl-row', isSel && 'is-selected', it.open && 'is-open'],
        role: 'row',
        tabindex: isSel || (!selected && it === sorted(data.items)[0]) ? '0' : '-1',
        'aria-selected': String(isSel),
        draggable: !it.open ? 'true' : 'false',
        dataset: { path: it.path },
        onclick: () => select(it.path),
        ondblclick: () => openItem(it),
        ondragstart: (e) => {
          if (it.open) {
            e.preventDefault();
            return;
          }
          draggingPath = it.path;
          e.dataTransfer.effectAllowed = 'move';
          e.dataTransfer.setData('application/x-retroboot-move', JSON.stringify({ path: it.path, name: it.path.split('/').pop() }));
        },
        ondragend: () => {
          draggingPath = null;
          for (const el2 of el.querySelectorAll('.is-move-target')) el2.classList.remove('is-move-target');
        },
        ondragover: canDropInto ? (e) => {
          if (!isMoveDrag(e) || draggingPath === it.path) return;
          e.preventDefault();
          e.stopPropagation();
          e.dataTransfer.dropEffect = 'move';
          r.classList.add('is-move-target');
        } : null,
        ondragleave: canDropInto ? () => r.classList.remove('is-move-target') : null,
        ondrop: canDropInto ? (e) => {
          if (!isMoveDrag(e)) return;
          e.preventDefault();
          e.stopPropagation();
          r.classList.remove('is-move-target');
          let payload = null;
          try {
            payload = JSON.parse(e.dataTransfer.getData('application/x-retroboot-move') || '{}');
          } catch {
            return;
          }
          if (!payload.path || payload.path === it.path || it.path.startsWith(`${payload.path}/`)) return;
          const base = payload.name || payload.path.split('/').pop();
          movePath(payload.path, `${it.path}/${base}`);
        } : null,
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
    const parentPath = crumbs.length >= 2 ? crumbs[crumbs.length - 2].path : null;
    const up = button('Enclosing Folder', { className: 'btn-small', disabled: crumbs.length < 2, onClick: () => go(parentPath) });
    if (parentPath !== null) {
      const isMoveDrag = (e) => !!(e.dataTransfer && [...(e.dataTransfer.types || [])].includes('application/x-retroboot-move'));
      up.addEventListener('dragover', (e) => {
        if (!isMoveDrag(e)) return;
        e.preventDefault();
        e.stopPropagation();
        up.classList.add('is-move-target');
      });
      up.addEventListener('dragleave', () => up.classList.remove('is-move-target'));
      up.addEventListener('drop', (e) => {
        if (!isMoveDrag(e)) return;
        e.preventDefault();
        e.stopPropagation();
        up.classList.remove('is-move-target');
        try {
          const payload = JSON.parse(e.dataTransfer.getData('application/x-retroboot-move') || '{}');
          if (!payload.path) return;
          const base = payload.name || payload.path.split('/').pop();
          movePath(payload.path, parentPath ? `${parentPath}/${base}` : base);
        } catch {
          /* ignore */
        }
      });
    }
    pathEl.replaceChildren(h('span', { class: 'select-wrap' }, sel), up);
  }

  function drawUsb() {
    if (!usbDrives.length) {
      usbEl.replaceChildren();
      return;
    }
    usbEl.replaceChildren(
      ...usbDrives.map((d) => {
        const titleText = d.label || d.name || d.device || 'USB Flash Drive';
        const meta = [
          d.fstype || 'USB drive',
          d.bytes ? `${formatBytes(d.bytes)} total` : null,
          d.free_bytes != null ? `${formatBytes(d.free_bytes)} free` : null,
          d.read_only ? 'Read-only' : null,
        ].filter(Boolean).join(' · ');
        return h(
          'div',
          { class: 'usb-drive' },
          h(
            'div',
            null,
            h('div', { class: 'usb-drive-head' }, h('strong', null, titleText)),
            h('p', { class: 'usb-drive-meta' }, meta),
          ),
          h(
            'div',
            { class: 'usb-drive-actions' },
            d.share_path != null ? button('Open', { className: 'btn-small', onClick: () => go(d.share_path) }) : null,
            button('Eject USB', { className: 'btn-small', onClick: () => ejectUsb(d) }),
          ),
        );
      }),
    );
  }

  async function ejectUsb(d) {
    const label = d.label || d.name || d.device || 'USB drive';
    try {
      await post('/api/v1/usb/eject', { device: d.device });
      toast(`Safe to unplug ${q(label)}.`, { level: 'ok', timeout: 5000 });
      lastUsbJson = '';
      await Promise.all([loadUsb(), load()]);
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error', timeout: 8000 });
    }
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
    const pvKind = it ? previewKind(it) : null;
    btns.preview.disabled = !it || !pvKind;
    btns.download.disabled = !it;
    btns.move.disabled = !it || busyOpen;
    btns.rename.disabled = !it || busyOpen;
    btns.del.disabled = !it || busyOpen;
    for (const b of [btns.move, btns.rename, btns.del]) b.title = busyOpen ? `${it.mac_name} is open in Mac OS. Close it there first.` : '';
    // The label stays the same: a wider button would re-wrap the toolbar and move the list
    // under the pointer between the two clicks of a double-click.  The line below the list
    // says when a file downloads as MacBinary.
    btns.download.title = it && it.kind === 'folder'
      ? 'Downloads this folder as a .zip archive'
      : it && it.download === 'macbinary'
        ? 'Downloads as MacBinary (.bin), keeping the resource fork'
        : '';
    if (!it) {
      selEl.replaceChildren();
      return;
    }
    const parts = [h('strong', null, it.mac_name), ` — ${it.kind_label || it.kind}`];
    if (it.kind === 'folder') {
      parts.push('. ', h('a', { href: zipUrl(it.path), download: `${it.mac_name}.zip` }, 'Download folder as .zip'), '.');
    } else if (it.kind === 'file') {
      parts.push(`, ${formatBytes(it.data_bytes)} data`);
      if (it.rsrc_bytes) parts.push(` + ${formatBytes(it.rsrc_bytes)} resource fork`);
      if (it.type) parts.push(`, type ${it.type}/${it.creator || '????'}`);
      parts.push('.');
      if (pvKind) parts.push(' ', h('button', { type: 'button', class: 'btn btn-small', onclick: () => previewItem(it) }, 'Preview'), '.');
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

  function zipUrl(relPath) {
    return `/api/v1/files/zip?path=${encodeURIComponent(relPath)}`;
  }

  function openItem(it) {
    if (it.kind === 'folder') go(it.path);
    else if (previewKind(it)) previewItem(it);
    else download(it);
  }

  function download(it) {
    if (!it) return;
    if (it.kind === 'folder') {
      const a = h('a', { href: zipUrl(it.path), download: `${it.mac_name}.zip`, class: 'visually-hidden' });
      document.body.append(a);
      a.click();
      a.remove();
      toast(`Downloading ${q(`${it.mac_name}.zip`)}.`, { timeout: 3500 });
      return;
    }
    const a = h('a', { href: downloadUrl(it), download: it.download === 'macbinary' ? `${it.mac_name}.bin` : it.mac_name, class: 'visually-hidden' });
    document.body.append(a);
    a.click();
    a.remove();
    toast(`Downloading ${q(it.mac_name)}${it.download === 'macbinary' ? ' as MacBinary' : ''}.`, { timeout: 3500 });
  }

  async function previewItem(it) {
    if (!it) return;
    const kind = previewKind(it);
    if (!kind) {
      download(it);
      return;
    }
    const src = previewUrl(it.path);
    const frame = h('div', { class: 'preview-frame' });
    const box = h(
      'div',
      { class: 'preview-box' },
      frame,
    );
    if (kind === 'image') {
      const img = h('img', { class: 'preview-img', src, alt: it.mac_name });
      img.addEventListener('error', () => {
        frame.replaceChildren(h('p', { class: 'small' }, 'Preview unavailable for this image. You can still download it.'));
      });
      frame.replaceChildren(img);
    } else if (kind === 'pdf') {
      frame.replaceChildren(
        h('iframe', { class: 'preview-pdf', src, title: it.mac_name }),
        h('p', { class: 'small', style: 'margin: 6px 0 0;' }, h('a', { href: src, target: '_blank', rel: 'noopener' }, 'Open PDF in a new tab')),
      );
    } else {
      frame.replaceChildren(loading());
      fetch(src, { credentials: 'same-origin' })
        .then(async (res) => {
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          return res.text();
        })
        .then((txt) => {
          frame.replaceChildren(h('pre', { class: 'preview-text' }, txt));
        })
        .catch(() => {
          frame.replaceChildren(h('p', { class: 'small' }, 'Could not load a text preview. Use Download to open it locally.'));
        });
    }
    const choice = await dialog({
      title: it.mac_name,
      icon: 'note',
      content: box,
      buttons: [
        { label: 'Download', value: 'download' },
        { label: 'Close', value: 'close', variant: 'default', autofocus: true },
      ],
    });
    if (choice === 'download') download(it);
  }

  async function movePath(src, dst) {
    if (!src || !dst || src === dst) return;
    try {
      const r = await post('/api/v1/files/move', { src, dst });
      const moved = (r && r.moved) || {};
      const destName = (moved.to || dst).split('/').pop();
      toast(`Moved ${q(destName)}.`, { level: 'ok', timeout: 3500 });
      selected = moved.to || null;
      await load({ keepSelection: false });
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error', timeout: 8000 });
    }
  }

  async function moveDialog(it) {
    if (!it) return;
    const baseName = it.path.split('/').pop();
    const curParent = it.path.includes('/') ? it.path.slice(0, it.path.lastIndexOf('/')) : '';
    const options = new Map();
    options.set('', `${volume()} (top level)`);
    if (data && data.crumbs) {
      for (const c of data.crumbs) options.set(c.path, c.mac_name);
    }
    if (data && data.items) {
      for (const sub of data.items) {
        if (sub.kind === 'folder' && sub.path !== it.path) {
          options.set(sub.path, `${sub.mac_name} (in ${folderLabel()})`);
        }
      }
    }
    try {
      const rootRes = await get('/api/v1/share/list?path=');
      if (rootRes && rootRes.items) {
        for (const sub of rootRes.items) {
          if (sub.kind === 'folder' && sub.path !== it.path && !sub.path.startsWith(`${it.path}/`)) {
            if (!options.has(sub.path)) options.set(sub.path, sub.mac_name);
          }
        }
      }
    } catch {
      /* fallback to visible folders */
    }
    options.delete(it.path);
    const entries = [...options.entries()].filter(([p]) => p !== curParent);
    if (!entries.length) {
      toast('Create a folder first to move this item into.', { level: 'info' });
      return;
    }
    const id = uid('move-dst');
    const sel = h('select', { id }, ...entries.map(([p, label]) => h('option', { value: p }, label)));
    const wrap = h('div', { class: 'dlg-section' }, h('label', { for: id, class: 'setting-label' }, 'Destination folder:'), h('span', { class: 'select-wrap' }, sel));
    const res = await dialog({
      title: `Move ${q(it.mac_name)}`,
      icon: 'note',
      content: wrap,
      buttons: [
        { label: 'Cancel', value: 'cancel' },
        { label: 'Move', value: 'ok', variant: 'default' },
      ],
      onOpen: () => sel.focus(),
    });
    if (res !== 'ok') return;
    const destFolder = sel.value;
    await movePath(it.path, destFolder ? `${destFolder}/${baseName}` : baseName);
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

