import { get, post, del, asJob, friendlyMessage, uploadFile } from '../api.js';
import { h, swap, Scope, formatBytes, fmtDateTime, relTime, uid, createTransferMeter, formatTransferText } from '../util.js';
import { win, kv, button, badge, pageHeader, notice, loading, errorState, emptyState, progressBar, dialog, confirm, toast, copyButton } from '../ui.js';
import { pixelIcon } from '../icons.js';
import { store, subscribe, refreshSettings, refreshStatus } from '../store.js';
import { settingsPanels, setDraft, currentValue, goToSetting } from '../settings.js';
import { track, activeJobs, doneMessage } from '../jobs.js';
import { lifecycle } from '../actions.js';

export const meta = { title: 'Storage & Media', icon: 'storage' };

const importJobs = new Map(); // "catalog:id" (lab or internet) -> job id (this browser)
const entryKey = (c) => `${c.catalog || 'internet'}:${c.id}`;
const osxMachine = (m) => ['macosx-10.2', 'macosx-10.3', 'macosx-10.4'].includes(m.guest_os);

/** "9.2.2" (a bare catalog osVersion) reads better as "Mac OS 9.2.2". */
const systemName = (s) => {
  if (typeof s !== 'string') return s;
  const v = s.trim();
  if (/^10\.[234](\.\d+)*$/.test(v)) return `Mac OS X ${v}`;
  return /^\d+(\.\d+)*$/.test(v) ? `Mac OS ${v}` : s;
};
/** A 68k Mac (Quadra 800) says so: "Mac OS 8.1 · 68k". Power Macs read as before. */
const withFamily = (text, family) => (family === 'quadra800' ? [text, '68k'].filter(Boolean).join(' · ') : text);

export function render() {
  const scope = new Scope();
  let data = null;
  let loadError = null;
  let selected = null;
  let cps = null;
  let cpError = null;
  let cpLoading = false;
  let catalogWait = null; // hostd answers catalog_state "loading" until it has fetched the catalog
  let catalogError = null;
  let appliedImageServers = JSON.stringify(store.settings?.applied?.['catalog.urls'] || []);
  let showOlder = false; // older versions of a family (Deluxe v1–v3 beside v4) stay hidden until asked

  const machinesEl = h('div', null, loading());
  const catalogEl = h('div', null, loading());
  const fileEl = h('div');
  const cpEl = h('div', null, loading());
  const shared = scope.add(settingsPanels('storage'));
  let fileImport = null; // {name, fraction (upload), job}
  const fileInput = h('input', { type: 'file', accept: '.tar.gz,.tgz,application/gzip', class: 'visually-hidden', 'aria-label': 'Machine archive', 'data-machine-file': '' });
  fileInput.addEventListener('change', () => {
    const file = fileInput.files && fileInput.files[0];
    fileInput.value = '';
    if (file) importFile(file);
  });
  const fileWin = win({ title: 'Import from a File', icon: 'box' }, fileEl, fileInput);
  // 68k Macs (Quadra 800): the ROM a new 68k Mac needs (GET/POST /api/v1/roms; never its bytes back).
  let roms = null;
  const romEl = h('div');
  const romInput = h('input', { type: 'file', class: 'visually-hidden', 'aria-label': 'Quadra 800 ROM file', 'data-rom-file': '' });
  romInput.addEventListener('change', () => {
    const file = romInput.files && romInput.files[0];
    romInput.value = '';
    if (file) addRom(file);
  });
  const romWin = win({ title: '68k Macs', icon: 'mac' }, romEl, romInput);

  const el = h(
    'div',
    { class: 'page' },
    pageHeader('storage', 'Storage & Media', 'Mac OS machines and their disks, checkpoints, and the shared volume.'),
    h(
      'div',
      { class: 'two-col' },
      h('div', { class: 'col' }, win({ title: 'Machines', icon: 'storage' }, machinesEl), win({ title: 'Checkpoints', icon: 'storage' }, cpEl)),
      h('div', { class: 'col' }, win({ title: 'Import from Catalog', icon: 'network' }, catalogEl), fileWin, romWin, shared.el),
    ),
  );

  async function load() {
    try {
      data = await get('/api/v1/machines');
      loadError = null;
      const ids = (data.machines || []).map((m) => m.id);
      if (!selected || !ids.includes(selected)) selected = (data.machines || []).find((m) => m.active)?.id || ids[0] || null;
    } catch (err) {
      loadError = err;
    }
    renderMachines();
    renderCatalog();
    renderFileImport();
    if (data && data.catalog_state === 'loading' && !catalogError) waitForCatalog();
    loadRoms();
    await loadCheckpoints();
  }

  async function loadRoms() {
    try {
      roms = await get('/api/v1/roms');
    } catch {
      roms = null;
    }
    renderRoms();
  }

  function renderRoms() {
    if (!roms) return swap(romEl, h('p', { class: 'muted small' }, 'Not available.'));
    const have = (roms.roms || []).find((r) => r.family === 'quadra800');
    swap(
      romEl,
      h('p', { class: 'win-intro' }, 'A 68k Mac runs Mac OS 7.5 to 8.1 on an emulated Quadra 800. It needs the Quadra 800 ROM (the same ROM as the Quadra 610 and 650), which Apple made and RetroBoot doesn’t include. A 68k Mac you import may bring its own.'),
      kv([
        ['68k emulator', roms.emulator_68k ? 'Installed' : 'Not in this version of RetroBoot'],
        ['Quadra 800 ROM', have ? 'On this computer' : 'Not added yet'],
      ]),
      h('div', { class: 'row-actions' }, button(have ? 'Replace ROM File…' : 'Add ROM File…', { className: 'btn-small', focusKey: 'add-rom', onClick: () => romInput.click() })),
      h('p', { class: 'muted small' }, 'New Blank Disk offers a 68k Mac once the ROM is here. RetroBoot checks the file before keeping it.'),
    );
  }

  async function addRom(file) {
    if (file.size > 4 * 1024 * 1024) {
      toast('That file is too large to be a Mac ROM.', { level: 'error' });
      return;
    }
    try {
      const res = await uploadFile('/api/v1/roms', file, 'application/octet-stream');
      const r = res && res.rom;
      toast(r && r.added === false ? 'This computer already has that ROM.' : `${(r && r.label) || 'The ROM'} was added. You can now make a 68k Mac with New Blank Disk.`, { level: 'ok', timeout: 8000 });
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error', timeout: 9000 });
    }
    await load();
  }

  /** GET /machines/catalog waits for the download (or reports why it failed); then reload the list. */
  function waitForCatalog() {
    if (catalogWait) return;
    catalogError = null;
    catalogWait = get('/api/v1/machines/catalog', { timeout: 60000 })
      .then(() => {
        catalogWait = null;
        return load();
      })
      .catch((err) => {
        catalogWait = null;
        catalogError = err;
        renderCatalog();
      });
  }

  async function loadCheckpoints() {
    if (!selected) {
      cps = null;
      renderCheckpoints();
      return;
    }
    cpLoading = true;
    renderCheckpoints();
    try {
      cps = await get(`/api/v1/machines/${encodeURIComponent(selected)}/checkpoints`);
      cpError = null;
    } catch (err) {
      cpError = err;
    }
    cpLoading = false;
    renderCheckpoints();
  }

  function machineCard(m) {
    const activeDesired = currentValue('machine.active');
    const appliedActive = store.settings && store.settings.applied['machine.active'];
    const tags = [];
    if (m.in_use) tags.push(badge('Running', 'ok'));
    if (m.id === appliedActive) tags.push(badge('Startup machine', 'neutral'));
    if (m.id === activeDesired && m.id !== appliedActive) tags.push(badge('Next start', 'pending'));
    const used = m.disk_used_bytes;
    const total = m.disk_bytes;
    const pct = used != null && total ? used / total : null;
    const cap = pct != null ? progressBar(pct, `${m.name} disk usage`) : null;
    if (cap) cap.classList.add('capacity');
    const canChoose = m.id !== activeDesired && store.settings && store.settings.defs.has('machine.active');
    return h(
      'li',
      { class: 'machine' },
      h('span', { class: 'machine-icon', html: pixelIcon('storage', 32) }),
      h(
        'div',
        { class: 'machine-main' },
        h('div', { class: 'machine-head' }, h('strong', null, m.name), h('span', { class: 'tag-row' }, ...tags)),
        m.blank && m.startup_volume !== 'system'
          ? h('p', { class: 'muted' }, withFamily(osxMachine(m) ? 'No Mac OS X yet: start it up from an install disc.' : 'No Mac OS yet: start it up from a Mac OS CD to install Mac OS.', m.family))
          : systemName(m.system) && systemName(m.system) !== m.name
            ? h('p', { class: 'muted' }, withFamily(systemName(m.system), m.family))
            : m.family === 'quadra800' ? h('p', { class: 'muted' }, '68k Mac (Quadra 800)') : null,
        cap,
        h('p', { class: 'muted small' }, used != null ? `${formatBytes(used)} used of ${formatBytes(total)}` : `${formatBytes(total)} disk`, m.checkpoints != null ? ` · ${m.checkpoints} checkpoint${m.checkpoints === 1 ? '' : 's'}` : ''),
        h(
          'div',
          { class: 'row-actions' },
          canChoose
            ? button('Use at Startup', {
                className: 'btn-small',
                focusKey: `use-${m.id}`,
                onClick: () => {
                  setDraft('machine.active', m.id);
                  toast(`${m.name} will be the startup machine after you apply and restart. Review the change at the bottom of the page.`, { level: 'info', timeout: 7000 });
                  renderMachines();
                },
              })
            : null,
          button('Checkpoints', {
            className: 'btn-small',
            variant: 'quiet',
            focusKey: `cps-${m.id}`,
            onClick: () => {
              selected = m.id;
              loadCheckpoints();
              cpEl.closest('.win').scrollIntoView({ behavior: 'smooth', block: 'start' });
            },
          }),
          button(osxMachine(m) ? 'Start Up from Disc…' : 'Start Up from CD…', { className: 'btn-small', variant: 'quiet', focusKey: `cd-${m.id}`, onClick: () => startFromCd(m) }),
          button('Remove…', { className: 'btn-small', variant: 'quiet', focusKey: `remove-${m.id}`, onClick: () => removeMachine(m) }),
        ),
      ),
    );
  }

  function renderMachines() {
    if (loadError) return swap(machinesEl, errorState(loadError, load));
    if (!data) return;
    const list = data.machines || [];
    swap(
      machinesEl,
      list.length ? h('ul', { class: 'machine-list' }, ...list.map(machineCard)) : emptyState('No machines yet. Import one from the catalog.', 'storage'),
      h('div', { class: 'row-actions' }, button('New Blank Disk…', { icon: 'plus', className: 'btn-small', focusKey: 'new-blank', onClick: newBlankDisk })),
      h('p', { class: 'muted small' }, 'The startup machine is chosen in ', h('a', { href: '#/machine', onclick: (e) => (e.preventDefault(), goToSetting('machine.active')) }, 'Machine & Startup'), '. Changing it takes effect when Mac OS restarts.'),
    );
  }

  function tagBadge(label) {
    const b = badge(label, 'quiet');
    if (/kids/i.test(label)) b.classList.add('badge-kids');
    else if (/68k|classic|os\s*x|ppc|power\s*mac/i.test(label)) b.classList.add('badge-arch');
    return b;
  }

  function catalogItem(c, several) {
    const myJobId = importJobs.get(entryKey(c)) || c.job;
    const job = activeJobs('machines.import').find((j) => j.id === myJobId);
    const rawTags = Array.isArray(c.tags) ? c.tags : [];
    const seenTags = new Set(rawTags.map((t) => String(t).toLowerCase()));
    const extraTags = [];
    if (c.machine_family === 'quadra800' && !seenTags.has('68k')) extraTags.push('68k');
    const paused = !job && !c.imported && c.partial_bytes > 0 && !c.partial_complete;
    const pctPartial = paused && c.download_bytes ? Math.min(1, c.partial_bytes / c.download_bytes) : null;
    const highlights = Array.isArray(c.highlights) ? c.highlights : [];
    return h(
      'li',
      { class: 'catalog-item' },
      h(
        'div',
        { class: 'machine-head' },
        h('strong', null, c.name),
        h(
          'span',
          { class: 'tag-row' },
          several && c.catalog_label ? badge(c.catalog_label, 'quiet') : null,
          c.recommended ? badge('Recommended', 'ok') : null,
          ...rawTags.map((t) => tagBadge(t)),
          ...extraTags.map((t) => tagBadge(t)),
          c.older ? badge('Older version', 'quiet') : null,
          c.imported ? badge('Imported', 'ok') : null,
        ),
      ),
      c.description || c.summary ? h('p', null, c.description || c.summary) : null,
      highlights.length
        ? h('div', { class: 'catalog-highlights' }, ...highlights.map((item) => badge(item, 'quiet')))
        : null,
      h('p', { class: 'muted small' }, `Download ${formatBytes(c.download_bytes)}`, c.disk_bytes || c.installed_bytes ? ` · ${formatBytes(c.disk_bytes || c.installed_bytes)} disk` : '', c.machine_family === 'quadra800' ? ' · 68k Mac' : ''),
      paused
        ? h(
            'div',
            { class: 'catalog-paused', role: 'status' },
            h('p', { class: 'muted small' }, `Paused · ${formatBytes(c.partial_bytes)}${c.download_bytes ? ` of ${formatBytes(c.download_bytes)}` : ''} downloaded`),
            pctPartial != null ? progressBar(pctPartial, `Paused download of ${c.name}`) : null,
          )
        : null,
      c.sha256
        ? h(
            'details',
            { class: 'disclosure catalog-details' },
            h('summary', null, h('span', { class: 'small muted' }, 'Verification details')),
            h('p', { class: 'hash' }, h('span', { class: 'muted small' }, 'SHA-256 '), h('code', { title: c.sha256 }, c.sha256), copyButton(c.sha256, 'Copy', { what: 'checksum', ariaLabel: `Copy SHA-256 for ${c.name}`, compact: true })),
          )
        : null,
      job
        ? h('div', { class: 'inline-job', role: 'status' }, progressBar(typeof job.progress === 'number' ? job.progress : null, `Importing ${c.name}`), h('p', { class: 'muted small' }, job.message || 'Importing…'))
        : c.imported
          ? null
          : h('div', { class: 'row-actions' }, button(paused ? 'Resume' : 'Import', { icon: 'download', className: 'btn-small', focusKey: `import-${entryKey(c)}`, onClick: () => startImport(c) })),
    );
  }

  function renderCatalog() {
    if (loadError) return swap(catalogEl, errorState(loadError, load));
    if (!data) return;
    const all = data.catalog || [];
    const catalogs = data.catalogs || [];
    const several = catalogs.length > 1;
    const olderCount = all.filter((c) => c.older).length;
    const items = all.filter((c) => showOlder || !c.older || importJobs.has(entryKey(c)));
    const fetching = data.catalog_state === 'loading' && !all.length;
    const olderId = 'catalog-older';
    const olderToggle = olderCount
      ? h(
          'p',
          { class: 'small' },
          h('input', { type: 'checkbox', id: olderId, checked: showOlder, onchange: (e) => ((showOlder = e.target.checked), renderCatalog()) }),
          ' ',
          h('label', { for: olderId }, `Show older versions (${olderCount})`),
        )
      : null;
    // A catalog that failed while another loaded: say which one (RetroBoot downloads, Internet).
    const failed = all.length ? catalogs.filter((k) => k.state === 'error') : [];
    const err = catalogError || (data.catalog_state === 'error' ? { message: data.catalog_error || 'The catalog could not be loaded.' } : null);
    let body;
    if (items.length) body = h('ul', { class: 'catalog-list' }, ...items.map((c) => catalogItem(c, several)));
    else if (err) body = notice({ level: 'warning', title: 'The catalog couldn’t be loaded', message: friendlyMessage(err), actions: [button('Try Again', { icon: 'refresh', className: 'btn-small', focusKey: 'catalog-retry', onClick: () => (renderCatalogLoading(), waitForCatalog()) })] });
    else if (fetching) body = loading('Loading the catalog…');
    else body = emptyState('The catalog has no machines right now.', 'network');
    swap(
      catalogEl,
      h(
        'p',
        { class: 'win-intro' },
        catalogs.some((k) => k.id === 'lab')
          ? 'Download a ready-made Mac OS machine from RetroBoot downloads or the internet catalog. RetroBoot checks the file’s SHA-256 checksum before using it. Mac OS keeps running meanwhile.'
          : 'Download a ready-made Mac OS machine. RetroBoot checks the file’s SHA-256 checksum before using it. Imports need an internet connection; Mac OS keeps running meanwhile.',
      ),
      body,
      olderToggle,
      ...failed.map((k) => notice({ level: 'warning', title: `${k.label} couldn’t be loaded`, message: k.error || '' })),
      data.upload_supported ? null : h('p', { class: 'muted small planned' }, badge('Planned', 'quiet'), ' Importing a disk archive from this device is not available yet.'),
    );
  }

  function renderCatalogLoading() {
    catalogError = null;
    renderCatalog();
  }

  /** Machine archives: a .tar.gz with config.json and disk.img (the catalog's format, sparse-friendly). */
  function renderFileImport() {
    fileWin.hidden = !(data && data.upload_supported);
    if (fileWin.hidden) return;
    const job = fileImport && fileImport.job ? activeJobs('machines.import').find((j) => j.id === fileImport.job.id) || fileImport.job : null;
    let body;
    if (fileImport && !fileImport.job) {
      body = [h('p', null, formatTransferText(`Uploading ${fileImport.name}…`, fileImport)), progressBar(fileImport.fraction, `Uploading ${fileImport.name}`)];
    } else if (job) {
      body = [h('p', null, job.message || `Importing ${fileImport.name}…`), progressBar(typeof job.progress === 'number' ? job.progress : null, `Importing ${fileImport.name}`)];
    } else {
      body = [
        h('div', { class: 'row-actions' }, button('Choose a File…', { icon: 'box', className: 'btn-small', focusKey: 'import-file', onClick: () => fileInput.click() })),
      ];
    }
    swap(
      fileEl,
      h('p', { class: 'win-intro' }, 'Add a Mac OS machine from a machine archive (a .tar.gz file with config.json and disk.img, as in the catalog). RetroBoot checks the archive and adds a new machine; your current machine isn’t changed. OS X archives may include startup memory and require RetroBoot with OS X support.'),
      ...body,
    );
  }

  async function importFile(file) {
    if (fileImport) return;
    if (!/\.(tar\.gz|tgz)$/i.test(file.name)) {
      toast('Choose a machine archive (its name ends in .tar.gz).', { level: 'error' });
      return;
    }
    const ok = await confirm({
      title: `Import ${file.name}?`,
      icon: 'note',
      message: [`RetroBoot uploads ${formatBytes(file.size)}, checks the archive, and adds it as a new machine. Your current machine isn't changed. Mac OS keeps running meanwhile.`],
      confirmLabel: 'Import',
    });
    if (!ok) return;
    const name = file.name.replace(/\.(tar\.gz|tgz)$/i, '').slice(0, 100);
    const meter = createTransferMeter(file.size);
    fileImport = { name: file.name, fraction: 0, loaded: 0, total: file.size, speed: null, eta: null, job: null };
    renderFileImport();
    try {
      const res = await uploadFile(`/api/v1/machines/import?name=${encodeURIComponent(name)}`, file, 'application/gzip', (f, info) => {
        fileImport.fraction = f;
        if (info && typeof info.loaded === 'number') {
          fileImport.loaded = info.loaded;
          fileImport.total = info.total;
          const r = meter.update(info.loaded);
          fileImport.speed = r.speed;
          fileImport.eta = r.eta;
        }
        renderFileImport();
      });
      const job = asJob(res);
      if (job) {
        fileImport.job = job;
        renderFileImport();
        const final = await track(job, { label: `Importing ${name}` });
        if (final.state === 'succeeded') toast(doneMessage(final, `${name} was imported. Choose Use at Startup to start up from it.`), { level: 'ok', timeout: 8000 });
        else toast(doneMessage(final, `${name} wasn't imported.`), { level: 'error', timeout: 8000 });
      }
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error', timeout: 8000 });
    }
    fileImport = null;
    renderFileImport();
    await Promise.all([load(), refreshSettings().catch(() => {})]);
  }

  async function startImport(c) {
    const resuming = !c.imported && c.partial_bytes > 0 && !c.partial_complete;
    const rem = resuming && c.download_bytes ? Math.max(0, c.download_bytes - c.partial_bytes) : c.download_bytes;
    const ok = await confirm({
      title: `${resuming ? 'Resume downloading' : 'Import'} ${c.name}?`,
      icon: 'note',
      message: [
        resuming
          ? `RetroBoot continues the paused download (${formatBytes(rem)} remaining), checks the SHA-256 checksum, and adds ${c.name}. Your current machine isn't changed.`
          : `RetroBoot downloads ${formatBytes(c.download_bytes)}, verifies it, and adds it as a new machine. Your current machine isn't changed.`,
      ],
      confirmLabel: resuming ? 'Resume' : 'Import',
    });
    if (!ok) return;
    try {
      const job = asJob(await post('/api/v1/machines/import', c.catalog ? { catalog_id: c.id, catalog: c.catalog } : { catalog_id: c.id }));
      if (!job) return;
      importJobs.set(entryKey(c), job.id);
      renderCatalog();
      const final = await track(job, { label: `Importing ${c.name}` });
      importJobs.delete(entryKey(c));
      if (final.state === 'succeeded') toast(doneMessage(final, `${c.name} was imported. Choose it in Machine & Startup to use it.`), { level: 'ok', timeout: 8000 });
      await Promise.all([load(), refreshSettings().catch(() => {})]);
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error' });
      load();
    }
  }

  function renderCheckpoints() {
    if (loadError) return swap(cpEl, errorState(loadError, load));
    if (!data) return;
    const machines = data.machines || [];
    if (!machines.length) return swap(cpEl, emptyState('No machines to checkpoint.', 'storage'));
    const m = machines.find((x) => x.id === selected) || machines[0];
    const pickId = uid('cpm');
    const picker =
      machines.length > 1
        ? h(
            'div',
            { class: 'field-inline' },
            h('label', { for: pickId }, 'Machine'),
            h('div', { class: 'select-wrap' }, h('select', { id: pickId, 'data-focus-key': 'cp-machine', onchange: (e) => ((selected = e.target.value), loadCheckpoints()) }, ...machines.map((x) => h('option', { value: x.id, selected: x.id === m.id }, x.name)))),
          )
        : null;
    const explain = h('p', { class: 'win-intro' }, m.guest_os?.startsWith('macosx-')
      ? 'A checkpoint keeps this Mac’s disk and startup memory together. Restoring brings both back. Mac OS X must shut down cleanly first.'
      : 'A checkpoint is a copy of a machine’s disk that you can go back to later. RetroBoot takes and restores checkpoints only while Mac OS is shut down cleanly, so the disk is never copied halfway through a write.');
    let body;
    if (cpLoading && !cps) body = loading();
    else if (cpError) body = errorState(cpError, loadCheckpoints);
    else if (cps) {
      const blocked = cps.blocked_reason;
      const running = m.in_use;
      const blockNote = blocked
        ? notice({
            level: 'info',
            title: running ? 'Shut down Mac OS first' : 'Checkpoints are unavailable right now',
            message: blocked,
            actions: running ? [button('Shut Down Mac OS…', { icon: 'power', focusKey: 'cp-shutdown', onClick: async () => { await lifecycle('shutdown'); refreshStatus().catch(() => {}); load(); } })] : null,
          })
        : null;
      const isFresh = (cp) => cp.label === 'Fresh Install' || cp.kind === 'fresh';
      const list = (cps.checkpoints || []).length
        ? h(
            'ul',
            { class: 'cp-list' },
            ...cps.checkpoints.map((cp) => {
              const fresh = isFresh(cp);
              return h(
                'li',
                { class: 'cp' },
                h(
                  'div',
                  null,
                  h('div', { class: 'machine-head' }, h('strong', null, cp.label || 'Checkpoint'), fresh ? badge('Automatic', 'quiet') : null),
                  h('p', { class: 'muted small' }, `${fmtDateTime(cp.created_at)} · ${relTime(cp.created_at)}${cp.size_bytes ? ` · ${formatBytes(cp.size_bytes)}` : ''}`),
                ),
                button(fresh ? 'Reset to Fresh Install…' : 'Restore…', {
                  className: 'btn-small',
                  focusKey: `restore-${cp.id}`,
                  disabled: !!blocked || cps.can_restore === false,
                  onClick: () => (fresh ? resetFresh(m, cp) : restore(m, cp)),
                }),
              );
            }),
          )
        : emptyState(`${m.name} has no checkpoints yet.`, 'storage');
      body = h(
        'div',
        null,
        blockNote,
        list,
        h('div', { class: 'row-actions' }, button('Create Checkpoint…', { icon: 'plus', focusKey: 'cp-create', disabled: !!blocked || cps.can_create === false, onClick: () => create(m) })),
      );
    }
    swap(cpEl, explain, picker, body);
  }

  /** DELETE /api/v1/machines/{id}: never the running, startup or only Mac (hostd says why). */
  async function removeMachine(m) {
    if (m.can_remove === false) {
      await dialog({
        title: `“${m.name}” can’t be removed`,
        icon: 'caution',
        message: [m.remove_blocked_reason || 'RetroBoot can’t remove this Mac right now.'],
        buttons: [{ label: 'OK', value: 'ok', variant: 'default', autofocus: true }],
      });
      return;
    }
    const used = m.disk_used_bytes;
    const disk = used != null ? `${formatBytes(used)} used of ${formatBytes(m.disk_bytes)}` : formatBytes(m.disk_bytes);
    const n = m.checkpoints || 0;
    const what = n
      ? `its disk (${disk}) and its ${n} checkpoint${n === 1 ? '' : 's'}${m.checkpoints_bytes ? ` (${formatBytes(m.checkpoints_bytes)})` : ''}`
      : `its disk (${disk}); it has no checkpoints`;
    const sys = systemName(m.system) && systemName(m.system) !== m.name ? ` (${systemName(m.system)})` : '';
    const ok = await confirm({
      title: `Remove “${m.name}”?`,
      icon: 'stop',
      danger: true,
      message: [
        `RetroBoot deletes this Mac${sys} from this computer: ${what}.`,
        'This can’t be undone. Documents, applications and settings saved on this Mac’s disk are lost. Files in the shared volume are not affected.',
      ],
      acknowledge: `I understand that ${m.name} and the documents on it will be deleted.`,
      confirmLabel: 'Remove',
    });
    if (!ok) return;
    try {
      const job = asJob(await del(`/api/v1/machines/${encodeURIComponent(m.id)}`));
      if (job) {
        const final = await track(job, { label: `Removing ${m.name}` });
        if (final.state === 'succeeded') toast(doneMessage(final, `${m.name} was removed.`), { level: 'ok', timeout: 8000 });
        else toast(doneMessage(final, `${m.name} wasn’t removed.`), { level: 'error', timeout: 8000 });
      }
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error', timeout: 8000 });
    }
    if (selected === m.id) selected = null;
    await Promise.all([load(), refreshSettings().catch(() => {})]);
  }

  /** POST /api/v1/machines/blank: a Mac with an empty disk; Mac OS is installed on it from a CD. */
  async function newBlankDisk() {
    const nameId = uid('bn');
    const sizeId = uid('bs');
    const osId = uid('bo');
    const names = new Set(((data && data.machines) || []).map((m) => m.name));
    let def = 'Untitled Mac';
    for (let n = 2; names.has(def); n++) def = `Untitled Mac ${n}`;
    const input = h('input', { type: 'text', id: nameId, maxlength: '63', value: def, autocomplete: 'off' });
    // The kind of Mac (hostd blank_kinds): PowerPC, or a 68k Quadra when this computer has the
    // 68k emulator and a Quadra 800 ROM; each kind has its own disk sizes.
    const rawKinds = (data && data.blank_kinds) || [{ family: 'powermac', label: 'PowerPC: Mac OS 8.5–9.2.2', available: true, supported: true, sizes_gb: (data && data.blank_sizes_gb) || [2, 4, 8, 16, 32, 64], default_gb: 8 }];
    const supportedKinds = rawKinds.filter((k) => k.supported !== false);
    const kinds = supportedKinds.length ? supportedKinds : rawKinds;
    let kind = kinds.find((k) => k.available) || kinds[0];
    const select = h('select', { id: sizeId });
    const note = h('p', { class: 'muted small' });
    const osSelect = h('select', { id: osId });
    const osRow = h('div', { class: 'field' }, h('label', { for: osId }, 'Operating system'), h('div', { class: 'select-wrap' }, osSelect));
    const how = h('p', { class: 'muted small' });
    const fillOsHelp = () => {
      how.textContent = osSelect.value !== 'classic'
        ? 'Install Mac OS X from an install CD or DVD in a connected drive, or a disc image in Files.'
        : 'Install Mac OS from a Mac OS CD in a connected CD-ROM drive, or a CD image in Files.';
    };
    osSelect.addEventListener('change', fillOsHelp);
    const fillSizes = () => {
      select.replaceChildren(...kind.sizes_gb.map((g) => h('option', { value: String(g), selected: g === kind.default_gb }, `${g} GB`)));
      note.textContent = kind.note || '';
      note.hidden = !kind.note;
      osRow.hidden = !kind.guest_os_choices;
      osSelect.replaceChildren(...(kind.guest_os_choices || [{ value: 'classic', label: 'Mac OS' }]).map((c) => h('option', { value: c.value }, c.label)));
      fillOsHelp();
    };
    const kindName = uid('bk');
    const kindRows = kinds.map((k) =>
      h(
        'label',
        { class: 'radio' },
        h('input', { type: 'radio', name: kindName, value: k.family, checked: k.family === kind.family, disabled: !k.available, onchange: () => ((kind = k), fillSizes()) }),
        h('span', null, k.label),
      ),
    );
    const why = kinds.filter((k) => !k.available && k.reason).map((k) => h('p', { class: 'muted small' }, k.reason));
    fillSizes();
    const kindField = kinds.length > 1
      ? h('div', { class: 'field', role: 'radiogroup', 'aria-label': 'Kind of Mac' }, h('span', { class: 'setting-label' }, 'Kind of Mac'), h('div', { class: 'radio-row' }, ...kindRows), ...why)
      : h('div', { class: 'field' }, h('span', { class: 'setting-label' }, 'Kind of Mac'), h('p', { class: 'muted small' }, kind.label), ...why);
    const choice = await dialog({
      title: 'New Blank Disk',
      icon: 'note',
      message: ['Makes an empty disk for a new Mac. The disk takes space on the RetroBoot computer only as Mac OS fills it.'],
      content: h(
        'div',
        null,
        h('div', { class: 'field' }, h('label', { for: nameId }, 'Name'), input),
        kindField,
        osRow,
        how,
        h('div', { class: 'field' }, h('label', { for: sizeId }, 'Size'), h('div', { class: 'select-wrap' }, select)),
        note,
      ),
      buttons: [
        { label: 'Cancel', value: 'cancel' },
        { label: 'Create', value: 'ok', variant: 'default', primary: true },
      ],
      onOpen: () => input.select(),
    });
    if (choice !== 'ok') return;
    try {
      const res = await post('/api/v1/machines/blank', { name: input.value.trim(), size_gb: Number(select.value), family: kind.family, guest_os: osSelect.value });
      const m = res && res.machine;
      const osx = osSelect.value !== 'classic';
      toast(`${m ? m.name : 'The new Mac'} is ready. Choose Start Up from ${osx ? 'Disc' : 'CD'} to install ${osx ? 'Mac OS X' : 'Mac OS'} on it.`, { level: 'ok', timeout: 9000 });
      if (m) selected = m.id;
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error', timeout: 8000 });
    }
    await Promise.all([load(), refreshSettings().catch(() => {})]);
  }

  /** POST /api/v1/machines/{id}/start-cd: this Mac starts up from a CD once (Mac OS restarts if it runs). */
  async function startFromCd(m) {
    await load();
    const osx = osxMachine(m);
    const disc = osx ? 'disc' : 'CD';
    const system = osx ? 'Mac OS X' : 'Mac OS';
    const cds = (data && data.cds) || [];
    const running = store.status && store.status.guest && ['running', 'starting', 'paused'].includes(store.status.guest.state);
    if (!cds.length) {
      const drives = (data && data.cd_drives) || [];
      await dialog({
        title: `No ${disc} to start up from`,
        icon: 'caution',
        message: [osx ? (drives.length ? 'The disc drive is empty. Put a Mac OS X install disc in it, then try again.' : 'Connect a USB CD or DVD drive with a Mac OS X install disc, or add a disc image in Files, then try again.') : drives.length ? 'The CD-ROM drive connected to the RetroBoot computer is empty. Put a Mac OS CD in it, then try again.' : 'Connect a CD-ROM drive (USB) with a Mac OS CD in it to the RetroBoot computer, or add a CD image in Files, then try again.'],
        buttons: [{ label: 'OK', value: 'ok', variant: 'default', autofocus: true }],
      });
      return;
    }
    const pickId = uid('cdp');
    const select = h('select', { id: pickId }, ...cds.map((c) => h('option', { value: c.id }, `${c.title} (${c.where})`)));
    const blank = m.blank && m.startup_volume !== 'system';
    const choice = await dialog({
      title: `Start Up “${m.name}” from a ${disc}`,
      icon: 'note',
      message: [
        blank ? `${m.name} has no ${system} yet. The installer on the ${disc} can install ${system} on its empty disk.` : `${m.name} starts up from the ${disc} once; its own disk stays available.`,
        running ? 'Mac OS restarts. Unsaved work in Mac OS is lost unless you save it first.' : `${m.name} becomes the startup machine.`,
      ],
      content: h('div', { class: 'field' }, h('label', { for: pickId }, osx ? 'Disc' : 'CD'), h('div', { class: 'select-wrap' }, select)),
      buttons: [
        { label: 'Cancel', value: 'cancel' },
        { label: running ? `Restart from ${disc}` : 'Start Up', value: 'ok', variant: 'default', primary: true },
      ],
    });
    if (choice !== 'ok') return;
    try {
      const job = asJob(await post(`/api/v1/machines/${encodeURIComponent(m.id)}/start-cd`, { source: select.value }));
      if (job) {
        const final = await track(job, { label: `Starting ${m.name} from the CD` });
        if (final.state === 'succeeded') toast(doneMessage(final, `${m.name} started up from the CD.`), { level: 'ok', timeout: 8000 });
        else toast(doneMessage(final, `${m.name} didn’t start up from the CD.`), { level: 'error', timeout: 9000 });
      }
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error', timeout: 8000 });
    }
    await Promise.all([load(), refreshSettings().catch(() => {}), refreshStatus().catch(() => {})]);
  }

  async function create(m) {
    const inputId = uid('cpl');
    const input = h('input', { type: 'text', id: inputId, maxlength: '60', placeholder: 'e.g. Before installing Myst', autocomplete: 'off' });
    const choice = await dialog({
      title: 'Create a Checkpoint',
      icon: 'note',
      message: [`Saves a copy of ${m.name}’s disk as it is now. Mac OS stays shut down while the copy is made.`],
      content: h('div', { class: 'field' }, h('label', { for: inputId }, 'Name (optional)'), input),
      buttons: [
        { label: 'Cancel', value: 'cancel' },
        { label: 'Create', value: 'ok', variant: 'default', primary: true },
      ],
      onOpen: () => input.focus(),
    });
    if (choice !== 'ok') return;
    try {
      const job = asJob(await post(`/api/v1/machines/${encodeURIComponent(m.id)}/checkpoints`, { label: input.value.trim() }));
      if (job) {
        const final = await track(job);
        if (final.state === 'succeeded') toast(doneMessage(final, 'Checkpoint created.'), { level: 'ok', timeout: 8000 });
      }
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error', timeout: 8000 });
    }
    load();
  }

  async function resetFresh(m, cp) {
    const ok = await confirm({
      title: `Reset “${m.name}” to Fresh Install?`,
      icon: 'stop',
      danger: true,
      message: [
        `${m.name}’s ${m.guest_os?.startsWith('macosx-') ? 'disk and startup memory go' : 'disk goes'} back to how it was right after it was installed.`,
        'Everything saved on that disk since then is lost, including documents, installed applications and settings made in Mac OS. Files in the shared volume are not affected.',
      ],
      acknowledge: 'I understand that changes since Fresh Install will be lost.',
      confirmLabel: 'Reset to Fresh Install',
    });
    if (!ok) return;
    try {
      const res = await post(`/api/v1/machines/${encodeURIComponent(m.id)}/reset-fresh`, {})
        .catch((err) => (err.status === 404 && cp ? post(`/api/v1/machines/${encodeURIComponent(m.id)}/checkpoints/${encodeURIComponent(cp.id)}/restore`, {}) : Promise.reject(err)));
      const job = asJob(res);
      if (job) {
        const final = await track(job, { label: `Resetting ${m.name} to Fresh Install` });
        if (final.state === 'succeeded') toast(doneMessage(final, `${m.name} reset to Fresh Install.`), { level: 'ok', timeout: 9000 });
      }
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error', timeout: 8000 });
    }
    load();
  }

  async function restore(m, cp) {
    const ok = await confirm({
      title: `Restore “${cp.label || 'checkpoint'}”?`,
      icon: 'stop',
      danger: true,
      message: [
        `${m.name}’s ${m.guest_os?.startsWith('macosx-') ? 'disk and startup memory go' : 'disk goes'} back to how ${m.guest_os?.startsWith('macosx-') ? 'they were' : 'it was'} on ${fmtDateTime(cp.created_at)}.`,
        'Everything saved on that disk since then is lost, including documents, installed applications and settings made in Mac OS. Files in the shared volume are not affected.',
      ],
      acknowledge: 'I understand that changes since this checkpoint will be lost.',
      confirmLabel: 'Restore',
    });
    if (!ok) return;
    try {
      const job = asJob(await post(`/api/v1/machines/${encodeURIComponent(m.id)}/checkpoints/${encodeURIComponent(cp.id)}/restore`, {}));
      if (job) {
        const final = await track(job);
        if (final.state === 'succeeded') toast(doneMessage(final, 'Checkpoint restored.'), { level: 'ok', timeout: 9000 });
      }
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error', timeout: 8000 });
    }
    load();
  }

  load();
  scope.add(subscribe('jobs', () => data && (renderCatalog(), renderFileImport())));
  scope.add(subscribe('settings', () => {
    const configured = JSON.stringify(store.settings?.applied?.['catalog.urls'] || []);
    if (configured !== appliedImageServers) {
      appliedImageServers = configured;
      load();
    } else if (data) renderMachines();
  }));
  let lastGuest = store.status && store.status.guest && store.status.guest.state;
  scope.add(
    subscribe('status', (s) => {
      const st = s && s.guest && s.guest.state;
      if (st !== lastGuest) {
        lastGuest = st;
        load();
      }
    }),
  );
  return { el, destroy: () => scope.destroy() };
}
