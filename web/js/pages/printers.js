import { get, post, del, friendlyMessage } from '../api.js';
import { h, swap, Scope, uid, relTime, formatBytes } from '../util.js';
import { win, kv, button, badge, pageHeader, notice, loading, errorState, emptyState, dialog, confirm, toast, copyButton } from '../ui.js';
import { pixelIcon } from '../icons.js';
import { store, subscribe, refreshSettings, refreshStatus } from '../store.js';
import { currentValue, setDraft, onDraftChange } from '../settings.js';

export const meta = { title: 'Printers', icon: 'printer' };

const DEFAULT_PORTS = { ipp: '631', ipps: '631', socket: '9100', lpd: '515' };
const DEFAULT_PATHS = { ipp: '/ipp/print', ipps: '/ipp/print', lpd: 'auto', socket: '' };
const SOURCE_LABELS = {
  lpd: 'Mac OS LPD',
  ipp: 'Mac OS IPP',
  raw: 'Mac OS JetDirect',
  socket: 'Mac OS JetDirect',
  jetdirect: 'Mac OS JetDirect',
  test: 'Test Page',
};

function stateBadge(state) {
  if (state === 'idle') return badge('Ready', 'ok');
  if (state === 'processing') return badge('Printing', 'pending');
  if (state === 'stopped') return badge('Stopped', 'bad');
  return badge(state || 'Unknown', 'neutral');
}

function jobStatusBadge(status) {
  const s = String(status || '').toLowerCase();
  if (['completed', 'printed', 'succeeded', 'saved'].includes(s)) return badge(status, 'ok');
  if (['printing', 'pending', 'processing', 'queued'].includes(s)) return badge(status, 'pending');
  if (['failed', 'error'].includes(s)) return badge(status, 'bad');
  if (['canceled', 'cancelled'].includes(s)) return badge(status, 'off');
  return badge(status || 'Unknown', 'neutral');
}

function filesPreviewHref(path) {
  const dir = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '';
  const q = [];
  if (dir) q.push(`path=${encodeURIComponent(dir)}`);
  q.push(`preview=${encodeURIComponent(path)}`);
  return `#/files?${q.join('&')}`;
}

export function render() {
  const scope = new Scope();
  let data = null;
  let error = null;
  let discovered = null;
  let discovering = false;
  let discoverError = null;
  let busy = false;
  let testingTarget = null;

  const outputEl = h('div', null, loading());
  const configuredEl = h('div', null, loading());
  const addEl = h('div', null, loading());
  const jobsEl = h('div', null, loading());

  const el = h(
    'div',
    { class: 'page' },
    pageHeader('printer', 'Printers', 'Print from Mac OS to PDF files in RetroBoot Share or to a physical USB or network printer.'),
    h(
      'div',
      { class: 'two-col' },
      h(
        'div',
        { class: 'col' },
        win({ title: 'Mac Printer Output', icon: 'printer' }, outputEl),
        win({ title: 'Configured Linux Printers', icon: 'printer' }, configuredEl),
      ),
      h(
        'div',
        { class: 'col' },
        win({ title: 'Add a Printer', icon: 'printer' }, addEl),
        win({ title: 'Recent Print Jobs', icon: 'note' }, jobsEl),
      ),
    ),
  );

  async function load() {
    try {
      data = await get('/api/v1/printers');
      error = null;
    } catch (err) {
      error = err;
    }
    renderAll();
  }

  function settingBool(key, fallback) {
    const v = currentValue(key);
    return v !== undefined ? !!v : !!fallback;
  }

  function settingToggle(key, label, fallback, help) {
    const m = store.settings;
    const def = m && m.defs.get(key);
    const id = uid('pr-chk');
    const val = settingBool(key, fallback);
    const edited = !!(def && m.desired[key] !== val);
    const pending = !!(def && m.pending.has(key));
    const input = h('input', {
      type: 'checkbox',
      id,
      checked: val,
      disabled: busy || (def && !def.available),
      onchange: (e) => toggleSetting(key, e.target.checked),
    });
    return h(
      'div',
      { class: 'printer-toggle' },
      h(
        'div',
        { class: 'setting-head' },
        h('label', { class: 'check', for: id }, input, h('span', null, label)),
        edited ? badge('Edited', 'edited') : pending ? badge('Pending', 'pending') : null,
        def && def.apply === 'guest_restart' ? badge('Mac OS restart', 'quiet') : null,
      ),
      help ? h('p', { class: 'muted small' }, help) : null,
    );
  }

  function toggleSetting(key, checked) {
    const m = store.settings;
    if (m && m.defs.has(key)) {
      setDraft(key, checked);
      renderOutput();
      return;
    }
    // Fallback if schema does not expose the key yet.
    toast('Settings schema is still loading.', { level: 'warning' });
  }

  function activeTarget() {
    const v = (data && data.virtual) || {};
    return v.target || currentValue('printer.target') || 'pdf';
  }

  function renderOutput() {
    if (error && !data) return swap(outputEl, errorState(error, load));
    if (!data) return;
    const v = data.virtual || {};
    const printers = data.printers || [];
    const target = activeTarget();
    const selId = uid('pr-target');
    const options = [
      h('option', { value: 'pdf', selected: target === 'pdf' }, 'Save as PDF (RetroBoot Share/Printed PDFs)'),
      ...printers.map((p) => {
        const val = `cups:${p.name}`;
        const label = p.description && p.description !== p.name ? `${p.description} (${p.name})` : p.name;
        return h('option', { value: val, selected: target === val }, label);
      }),
    ];
    if (target.startsWith('cups:') && !printers.some((p) => `cups:${p.name}` === target)) {
      options.push(h('option', { value: target, selected: true }, target.slice(5)));
    }
    const targetSelect = h(
      'select',
      {
        id: selId,
        'data-focus-key': 'printer-target',
        disabled: busy,
        onchange: (e) => setTarget(e.target.value),
      },
      ...options,
    );

    const testQueue = target.startsWith('cups:') ? target.slice(5) : 'pdf';
    const guestIp = v.guest_ip || '10.0.2.2';
    const queue = v.queue || 'retroboot';
    const lpdPort = v.lpd_port || 515;
    const ippPort = v.ipp_port || 631;
    const rawPort = v.raw_port || 9100;
    const ippUrl = `ipp://${guestIp}:${ippPort}/printers/${queue}`;
    const enabled = !!v.enabled;
    const enableId = uid('pr-enable');
    const enableToggle = h(
      'div',
      { class: 'printer-toggle' },
      h(
        'label',
        { class: 'check', for: enableId },
        h('input', {
          type: 'checkbox',
          id: enableId,
          'data-focus-key': 'enable-mac-printing',
          checked: enabled,
          disabled: busy,
          onchange: (e) => setEnabled(e.target.checked),
        }),
        h('span', null, 'Enable Mac printing'),
      ),
    );

    swap(
      outputEl,
      h(
        'div',
        { class: 'state-line' },
        h('span', null, 'Emulated Printer:'),
        h('strong', null, 'Color LaserWriter 12/600 PS'),
        enabled && v.running ? badge('Running', 'ok') : enabled ? badge('Enabled', 'pending') : badge('Off', 'off'),
        v.jobs ? h('span', { class: 'muted small' }, `${v.jobs} job${v.jobs === 1 ? '' : 's'} processed`) : null,
      ),
      enableToggle,
      h(
        'div',
        { class: 'field' },
        h('label', { for: selId }, 'Active destination'),
        h('div', { class: 'select-wrap' }, targetSelect),
      ),
      h(
        'div',
        { class: 'row-actions' },
        button(testingTarget === testQueue ? 'Printing Test…' : 'Test Mac Printer', {
          icon: 'printer',
          className: 'btn-small',
          busy: testingTarget === testQueue,
          disabled: busy || !enabled || (testingTarget !== null && testingTarget !== testQueue),
          focusKey: 'test-mac-printer',
          onClick: () => printTest(testQueue),
        }),
      ),
      h(
        'div',
        { class: 'printer-toggles' },
        settingToggle('printer.keep_pdf', 'Also save a PDF copy in RetroBoot Share', v.keep_pdf),
        settingToggle(
          'printer.mac_auto_setup',
          'Auto-configure Mac OS 9 Desktop Printer',
          true,
          'Creates “RetroBoot Printer” (Color LaserWriter 12/600 PS via TCP/IP LPR, keeping AppleTalk off for fast boot).',
        ),
      ),
      h(
        'div',
        { class: 'subsection' },
        h('h4', { class: 'sub-title' }, 'How to print from Mac OS'),
        h('p', { class: 'muted small' }, enabled
          ? 'Mac OS 9 starts with “RetroBoot Printer” (Color LaserWriter 12/600 PS) already selected — just choose File → Print. AppleTalk stays off so startup stays fast.'
          : 'Turn on “Enable Mac printing” above to print from Mac OS to PDF or a configured printer.'),
        enabled ? h('p', { class: 'muted small' }, `Mac OS X: Printer Setup Utility → Add → IP Printer → HP JetDirect - Socket (or IPP) at ${guestIp}, model Apple Color LaserWriter 12/600 PS.`) : null,
      ),
      h(
        'details',
        { class: 'disclosure' },
        h('summary', null, 'Manual setup addresses & Desktop Printer Utility'),
        h(
          'div',
          { class: 'disclosure-body' },
          kv(
            [
              ['Emulated model', h('strong', null, 'Apple Color LaserWriter 12/600 PS')],
              ['Printer address', h('span', { class: 'with-copy' }, h('code', null, guestIp), copyButton(guestIp, 'Copy', { what: 'address', compact: true }))],
              ['LPD queue', h('span', null, h('code', null, queue), h('span', { class: 'muted small' }, ` (port ${lpdPort})`))],
              ['IPP URL', h('span', { class: 'with-copy' }, h('code', null, ippUrl), copyButton(ippUrl, 'Copy', { what: 'IPP URL', compact: true }))],
              ['AppSocket', h('code', null, `${guestIp}:${rawPort}`)],
            ],
            'kv-compact',
          ),
          h('p', { class: 'muted small' }, `If setting up manually in Mac OS 9: open Desktop Printer Utility → Printer (LPR), click Change under PostScript Printer Description (PPD) and pick LaserWriter Color 12/600 PS, then click Change under Internet Printer and enter ${guestIp} with queue ${queue}.`),
        ),
      ),
    );
  }

  function renderConfigured() {
    if (error && !data) return swap(configuredEl, '');
    if (!data) return;
    if (data.cups_available === false) {
      swap(
        configuredEl,
        notice({
          level: 'info',
          title: 'CUPS printing is unavailable',
          message: data.cups_reason || 'CUPS is not installed on this RetroBoot. Print to PDF in RetroBoot Share still works normally.',
        }),
      );
      return;
    }
    const printers = data.printers || [];
    const target = activeTarget();
    swap(
      configuredEl,
      printers.length
        ? h(
            'ul',
            { class: 'printer-list' },
            ...printers.map((p) => {
              const isTarget = p.is_target || target === `cups:${p.name}`;
              return h(
                'li',
                { class: 'printer-row' },
                h('span', { class: 'printer-icon', html: pixelIcon('printer', 32) }),
                h(
                  'div',
                  { class: 'printer-main' },
                  h(
                    'div',
                    { class: 'machine-head' },
                    h('strong', null, p.description || p.name),
                    h(
                      'span',
                      { class: 'tag-row' },
                      stateBadge(p.state),
                      p.accepting === false ? badge('Paused', 'warning') : null,
                      isTarget ? badge('Mac OS output', 'ok') : null,
                    ),
                  ),
                  p.description && p.description !== p.name ? h('p', { class: 'muted small' }, `Queue: ${p.name}${p.location ? ` · ${p.location}` : ''}`) : p.location ? h('p', { class: 'muted small' }, p.location) : null,
                  p.uri ? h('p', { class: 'printer-uri' }, h('code', null, p.uri)) : null,
                  p.state_message && p.state_message !== 'Ready' ? h('p', { class: 'muted small' }, p.state_message) : null,
                  h(
                    'div',
                    { class: 'row-actions' },
                    !isTarget
                      ? button('Use for Mac', {
                          className: 'btn-small',
                          disabled: busy,
                          focusKey: `use-printer-${p.name}`,
                          onClick: () => setTarget(`cups:${p.name}`),
                        })
                      : null,
                    button(testingTarget === p.name ? 'Printing…' : 'Test Page', {
                      className: 'btn-small',
                      variant: 'quiet',
                      busy: testingTarget === p.name,
                      disabled: busy || (testingTarget !== null && testingTarget !== p.name),
                      focusKey: `test-printer-${p.name}`,
                      onClick: () => printTest(p.name),
                    }),
                    button('Remove…', {
                      className: 'btn-small',
                      variant: 'quiet',
                      disabled: busy,
                      focusKey: `remove-printer-${p.name}`,
                      onClick: () => removePrinter(p),
                    }),
                  ),
                ),
              );
            }),
          )
        : emptyState('No Linux printers configured yet. Find a nearby printer or add one by address.', 'printer'),
    );
  }

  function renderAdd() {
    if (error && !data) return swap(addEl, '');
    if (!data) return;
    if (data.cups_available === false) {
      swap(
        addEl,
        h('p', { class: 'muted small' }, 'Install CUPS on RetroBoot to discover and add physical USB or network printers.'),
      );
      return;
    }
    const configured = data.printers || [];
    const isAdded = (d) =>
      !!d.already_added ||
      configured.some((p) => (d.uri && p.uri === d.uri) || p.name === d.name);

    let discBody = null;
    if (discovering) {
      discBody = loading('Searching for nearby network, AirPrint, and USB printers…');
    } else if (discoverError) {
      discBody = notice({
        level: 'warning',
        title: 'Printer discovery failed',
        message: friendlyMessage(discoverError),
      });
    } else if (discovered !== null) {
      discBody = discovered.length
        ? h(
            'ul',
            { class: 'printer-list' },
            ...discovered.map((d) => {
              const added = isAdded(d);
              return h(
                'li',
                { class: 'printer-row' },
                h('span', { class: 'printer-icon', html: pixelIcon('printer', 32) }),
                h(
                  'div',
                  { class: 'printer-main' },
                  h(
                    'div',
                    { class: 'machine-head' },
                    h('strong', null, d.name || d.model || d.address || 'Printer'),
                    h(
                      'span',
                      { class: 'tag-row' },
                      d.transport ? badge(String(d.transport).toUpperCase(), 'quiet') : null,
                      added ? badge('Added', 'ok') : null,
                    ),
                  ),
                  d.model && d.model !== d.name ? h('p', { class: 'muted small' }, d.model) : null,
                  d.address ? h('p', { class: 'muted small' }, `Address: ${d.address}`) : null,
                  d.uri ? h('p', { class: 'printer-uri' }, h('code', null, d.uri)) : null,
                  added
                    ? null
                    : h(
                        'div',
                        { class: 'row-actions' },
                        button('Add', {
                          className: 'btn-small',
                          variant: 'default',
                          disabled: busy,
                          focusKey: `add-disc-${d.id || d.name}`,
                          onClick: () => addDiscovered(d),
                        }),
                      ),
                ),
              );
            }),
          )
        : emptyState('No nearby or USB printers were found. Try Add Printer by Address…', 'printer');
    }

    swap(
      addEl,
      h('p', { class: 'win-intro' }, 'Discover printers on the local network or USB, or connect directly by IP address, hostname, or URI.'),
      h(
        'div',
        { class: 'row-actions' },
        button(discovering ? 'Searching…' : 'Find Nearby & USB Printers', {
          icon: 'refresh',
          className: 'btn-small',
          busy: discovering,
          disabled: busy,
          focusKey: 'discover-printers',
          onClick: discover,
        }),
        button('Add Printer by Address…', {
          icon: 'plus',
          className: 'btn-small',
          disabled: busy,
          focusKey: 'add-printer-manual',
          onClick: openAddManualDialog,
        }),
      ),
      discBody,
    );
  }

  function renderJobs() {
    if (error && !data) return swap(jobsEl, '');
    if (!data) return;
    const jobs = data.jobs || [];
    swap(
      jobsEl,
      jobs.length
        ? h(
            'ul',
            { class: 'printer-list' },
            ...jobs.map((j) => {
              const s = String(j.status || j.state || '').toLowerCase();
              const canCancel = ['active', 'pending', 'printing', 'processing', 'queued'].includes(s) && (j.id || j.job_id);
              const src = SOURCE_LABELS[String(j.source || '').toLowerCase()] || j.source || 'Mac OS';
              const metaParts = [
                j.timestamp ? relTime(j.timestamp) : j.date || null,
                src,
                j.destination_label || j.destination || j.queue,
                j.bytes != null ? formatBytes(j.bytes) : null,
              ].filter(Boolean);
              return h(
                'li',
                { class: 'printer-job' },
                h(
                  'div',
                  { class: 'machine-head' },
                  h('strong', null, j.job || (j.queue ? `Job on ${j.queue}` : 'Print Job')),
                  h('span', { class: 'tag-row' }, jobStatusBadge(j.status || j.state)),
                ),
                h('p', { class: 'muted small' }, metaParts.join(' · ')),
                j.error ? h('p', { class: 'field-error' }, j.error) : null,
                j.path || canCancel
                  ? h(
                      'div',
                      { class: 'row-actions' },
                      j.path ? h('a', { class: 'btn btn-small', href: filesPreviewHref(j.path) }, 'Open PDF') : null,
                      canCancel
                        ? button('Cancel', {
                            className: 'btn-small',
                            variant: 'quiet',
                            disabled: busy,
                            focusKey: `cancel-job-${j.id || j.job_id}`,
                            onClick: () => cancelJob(j),
                          })
                        : null,
                    )
                  : null,
              );
            }),
          )
        : emptyState('No recent print jobs yet.', 'printer'),
    );
  }

  function renderAll() {
    renderOutput();
    renderConfigured();
    renderAdd();
    renderJobs();
  }

  async function setEnabled(enabled) {
    if (busy) return;
    busy = true;
    renderOutput();
    renderConfigured();
    try {
      await post('/api/v1/printers/enabled', { enabled: !!enabled });
      toast(enabled ? 'Mac printing enabled.' : 'Mac printing turned off.', { level: 'ok' });
      await Promise.all([load(), refreshSettings().catch(() => {}), refreshStatus().catch(() => {})]);
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error' });
    } finally {
      busy = false;
      renderAll();
    }
  }

  async function setTarget(target) {
    if (busy) return;
    busy = true;
    renderOutput();
    renderConfigured();
    try {
      await post('/api/v1/printers/target', { target });
      toast(target === 'pdf' ? 'Mac OS now prints to PDF in RetroBoot Share.' : `Mac OS now prints to ${target.slice(5)}.`, { level: 'ok' });
      await Promise.all([load(), refreshSettings().catch(() => {})]);
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error' });
    } finally {
      busy = false;
      renderAll();
    }
  }

  async function printTest(name) {
    if (testingTarget !== null) return;
    testingTarget = name;
    renderOutput();
    renderConfigured();
    try {
      await post(`/api/v1/printers/${encodeURIComponent(name)}/test`, {});
      toast(name === 'pdf' ? 'Test page saved to RetroBoot Share/Printed PDFs.' : `Test page sent to ${name}.`, { level: 'ok' });
      await Promise.all([load(), refreshStatus().catch(() => {})]);
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error' });
    } finally {
      testingTarget = null;
      renderAll();
    }
  }

  async function discover() {
    if (discovering) return;
    discovering = true;
    discoverError = null;
    renderAdd();
    try {
      const res = await post('/api/v1/printers/discover', {});
      discovered = (res && res.printers) || [];
    } catch (err) {
      discoverError = err;
    } finally {
      discovering = false;
      renderAdd();
    }
  }

  async function addDiscovered(d) {
    if (busy) return;
    busy = true;
    renderAdd();
    try {
      const body = {
        name: d.name,
        description: d.model || d.name,
        uri: d.uri,
        driver: 'auto',
      };
      await post('/api/v1/printers', body);
      toast(`Added printer “${d.name || d.model}”.`, { level: 'ok' });
      await Promise.all([load(), refreshSettings().catch(() => {})]);
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error' });
    } finally {
      busy = false;
      renderAll();
    }
  }

  async function removePrinter(p) {
    const label = p.description || p.name;
    const ok = await confirm({
      title: `Remove “${label}”?`,
      icon: 'caution',
      danger: true,
      message: [
        `RetroBoot removes the printer queue “${p.name}”.`,
        activeTarget() === `cups:${p.name}` ? 'Mac OS printing will switch back to Save as PDF.' : '',
      ].filter(Boolean),
      confirmLabel: 'Remove',
    });
    if (!ok) return;
    busy = true;
    renderAll();
    try {
      await del(`/api/v1/printers/${encodeURIComponent(p.name)}`);
      toast(`Removed printer “${label}”.`, { level: 'ok' });
      await Promise.all([load(), refreshSettings().catch(() => {})]);
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error' });
    } finally {
      busy = false;
      renderAll();
    }
  }

  async function cancelJob(j) {
    if (busy) return;
    const jid = j.id || j.job_id;
    if (!jid) return;
    busy = true;
    renderJobs();
    try {
      await post(`/api/v1/printers/jobs/${encodeURIComponent(jid)}/cancel`, {});
      toast('Print job canceled.', { level: 'info' });
      await load();
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error' });
    } finally {
      busy = false;
      renderAll();
    }
  }

  async function openAddManualDialog() {
    const ids = {
      name: uid('pn'),
      host: uid('ph'),
      proto: uid('pp'),
      port: uid('po'),
      path: uid('pa'),
      driver: uid('pd'),
      def: uid('pdef'),
    };
    const nameIn = h('input', { type: 'text', id: ids.name, placeholder: 'e.g. Office_LaserJet (optional)', maxlength: '64', autocomplete: 'off', spellcheck: 'false' });
    const hostIn = h('input', { type: 'text', id: ids.host, placeholder: '192.168.1.50 or ipp://192.168.1.50:631/ipp/print', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false' });
    const protoSel = h(
      'select',
      { id: ids.proto },
      h('option', { value: 'ipp', selected: true }, 'IPP (ipp://)'),
      h('option', { value: 'ipps' }, 'IPP Secure (ipps://)'),
      h('option', { value: 'socket' }, 'AppSocket / JetDirect (socket://)'),
      h('option', { value: 'lpd' }, 'LPD (lpd://)'),
    );
    const portIn = h('input', { type: 'number', id: ids.port, min: '1', max: '65535', placeholder: DEFAULT_PORTS.ipp, autocomplete: 'off' });
    const pathIn = h('input', { type: 'text', id: ids.path, placeholder: DEFAULT_PATHS.ipp, autocomplete: 'off', spellcheck: 'false' });
    const driverSel = h(
      'select',
      { id: ids.driver },
      h('option', { value: 'auto', selected: true }, 'Automatic (Driverless / IPP Everywhere)'),
      h('option', { value: 'generic_ps' }, 'Generic PostScript'),
      h('option', { value: 'generic_pcl' }, 'Generic PCL'),
      h('option', { value: 'raw' }, 'Raw Queue'),
    );
    const setDefaultChk = h('input', { type: 'checkbox', id: ids.def, checked: true });
    const errEl = h('p', { class: 'field-error', hidden: true, role: 'alert' });

    const syncProtocol = () => {
      const p = protoSel.value;
      const isFullUri = /^[a-z][a-z0-9+.-]*:\/\//i.test(hostIn.value.trim());
      protoSel.disabled = isFullUri;
      portIn.disabled = isFullUri;
      pathIn.disabled = isFullUri || p === 'socket';
      portIn.placeholder = DEFAULT_PORTS[p] || '';
      pathIn.placeholder = p === 'socket' ? 'Not used for AppSocket' : DEFAULT_PATHS[p] || '';
    };
    protoSel.addEventListener('change', syncProtocol);
    hostIn.addEventListener('input', syncProtocol);
    syncProtocol();

    let payload = null;
    const choice = await dialog({
      title: 'Add Printer by Address',
      icon: 'printer',
      wide: true,
      message: ['Enter the printer’s IP address, hostname, or full URI.'],
      content: h(
        'div',
        { class: 'subsection' },
        h('div', { class: 'field' }, h('label', { for: ids.name }, 'Printer Name'), nameIn),
        h('div', { class: 'field' }, h('label', { for: ids.host }, 'IP Address, Hostname, or Full URI'), hostIn),
        h(
          'div',
          { class: 'printer-dlg-grid' },
          h('div', { class: 'field' }, h('label', { for: ids.proto }, 'Protocol'), h('div', { class: 'select-wrap' }, protoSel)),
          h('div', { class: 'field' }, h('label', { for: ids.port }, 'Port'), portIn),
        ),
        h(
          'div',
          { class: 'printer-dlg-grid' },
          h('div', { class: 'field' }, h('label', { for: ids.path }, 'Path / Queue'), pathIn),
          h('div', { class: 'field' }, h('label', { for: ids.driver }, 'Driver'), h('div', { class: 'select-wrap' }, driverSel)),
        ),
        h('label', { class: 'check', for: ids.def }, setDefaultChk, h('span', null, 'Set as Mac OS default printer')),
        errEl,
      ),
      buttons: [
        { label: 'Cancel', value: 'cancel' },
        { label: 'Add Printer', value: 'ok', variant: 'default', primary: true },
      ],
      onOpen: () => hostIn.focus(),
      validate: () => {
        const rawHost = hostIn.value.trim();
        const rawPort = portIn.value.trim();
        let msg = null;
        if (!rawHost) {
          msg = 'Enter an IP address, hostname, or URI.';
        } else if (rawPort && (!/^\d+$/.test(rawPort) || Number(rawPort) < 1 || Number(rawPort) > 65535)) {
          msg = 'Port must be between 1 and 65535.';
        }
        if (msg) {
          errEl.hidden = false;
          errEl.textContent = msg;
          hostIn.focus();
          return false;
        }
        const fallbackName = rawHost.replace(/^[a-z][a-z0-9+.-]*:\/\//i, '').split(/[/:?#]/)[0] || 'Printer';
        const body = {
          name: nameIn.value.trim() || fallbackName,
          driver: driverSel.value,
          set_as_target: setDefaultChk.checked,
        };
        if (/^[a-z][a-z0-9+.-]*:\/\//i.test(rawHost)) {
          body.uri = rawHost;
        } else {
          body.protocol = protoSel.value;
          body.host = rawHost;
          if (rawPort) body.port = Number(rawPort);
          if (pathIn.value.trim()) body.path = pathIn.value.trim();
        }
        payload = body;
        return true;
      },
    });
    if (choice !== 'ok' || !payload) return;
    busy = true;
    renderAll();
    try {
      const res = await post('/api/v1/printers', payload);
      const addedName = (res && res.printer && (res.printer.description || res.printer.name)) || payload.name || payload.host || 'Printer';
      toast(`Added printer “${addedName}”.`, { level: 'ok' });
      await Promise.all([load(), refreshSettings().catch(() => {})]);
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error' });
    } finally {
      busy = false;
      renderAll();
    }
  }

  load();
  scope.interval(() => document.visibilityState === 'visible' && load(), 10000);
  scope.add(subscribe('settings', () => data && renderOutput()));
  scope.add(onDraftChange(() => data && renderOutput()));
  return { el, destroy: () => scope.destroy() };
}
