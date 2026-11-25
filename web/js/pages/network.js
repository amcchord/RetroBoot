import { get, post, asJob, friendlyMessage } from '../api.js';
import { h, swap, Scope, uid, relTime } from '../util.js';
import { win, button, badge, kv, pageHeader, notice, loading, errorState, emptyState, dialog, confirm, toast, copyButton } from '../ui.js';
import { lineIcon, pixelIcon } from '../icons.js';
import { bundleIcon } from '../platinum.js';
import { subscribe } from '../store.js';
import { settingsPanels } from '../settings.js';
import { webHelperExtras } from '../webhelper.js';
import { bluetoothExtras } from '../bluetooth.js';
import { track, activeJobs, jobOutcome } from '../jobs.js';

export const meta = { title: 'Network', icon: 'network' };

const STATE_WORD = { connected: 'Connected', disconnected: 'Not connected', connecting: 'Connecting…', off: 'Off', unavailable: 'Unavailable', up: 'Up', down: 'Down' };
const SECURITY = { open: 'Open', wep: 'WEP', wpa: 'WPA', wpa2: 'WPA2', wpa3: 'WPA3', 'wpa2-enterprise': 'WPA2 Enterprise' };

// The padlock of a secured network: the Keychain control strip's (Platinum bundle), else ours.
function lockIcon() {
  return bundleIcon('lock', 16) ? pixelIcon('lock', 16) : lineIcon('lock', 16);
}

const secured = (n) => !!n.security && !['open', '--', 'none', ''].includes(String(n.security).toLowerCase());

function signalBars(signal) {
  const n = signal > 75 ? 4 : signal > 50 ? 3 : signal > 25 ? 2 : 1;
  return h('span', { class: 'signal', role: 'img', 'aria-label': `Signal ${signal}%`, title: `Signal ${signal}%` }, ...[1, 2, 3, 4].map((i) => h('span', { class: i <= n ? 'on' : null })));
}

function stateBadge(state) {
  const tone = state === 'connected' || state === 'up' ? 'ok' : state === 'connecting' ? 'pending' : 'off';
  return badge(STATE_WORD[state] || state || 'Unknown', tone);
}

export function render() {
  const scope = new Scope();
  let data = null;
  let error = null;
  const connEl = h('div', null, loading());
  const wifiEl = h('div', null, loading());
  const guestEl = h('div', null, loading());
  // Modern Web Helper sits at the top of the right column so it and its whitelist are easy to
  // find; Host Connection, Wi-Fi and Bluetooth sit on the left; guest status and management below.
  const helperPanels = scope.add(settingsPanels('network', {
    groups: ['Modern Web Helper'],
    extras: { 'Modern Web Helper': webHelperExtras },
  }));
  const btPanels = scope.add(settingsPanels('network', {
    groups: ['Bluetooth'],
    extras: { Bluetooth: bluetoothExtras },
  }));
  const otherPanels = scope.add(settingsPanels('network', {
    groups: ['Mac OS Networking', 'Web Management', 'Remote Screen'],
  }));

  const jumpTo = (id) => {
    const target = document.getElementById(id);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  const jumpNav = h(
    'nav',
    { class: 'net-jump', 'aria-label': 'Network sections' },
    button('Connection', { className: 'btn-small', onClick: () => jumpTo('net-conn') }),
    button('Modern Web Helper', { className: 'btn-small', variant: 'default', onClick: () => jumpTo('net-webhelper') }),
    button('Wi-Fi', { className: 'btn-small', onClick: () => jumpTo('net-wifi') }),
    button('Bluetooth', { className: 'btn-small', onClick: () => jumpTo('net-bt') }),
    button('Mac OS Networking', { className: 'btn-small', onClick: () => jumpTo('net-guest') }),
    button('Web Management', { className: 'btn-small', onClick: () => jumpTo('net-mgmt') }),
  );

  const el = h(
    'div',
    { class: 'page' },
    pageHeader('network', 'Network', 'How RetroBoot gets online, Modern Web Helper and its whitelist, Wi-Fi, Bluetooth, and web management.'),
    jumpNav,
    h(
      'div',
      { class: 'two-col' },
      h('div', { class: 'col' },
        h('div', { id: 'net-conn' }, win({ title: 'Connection', icon: 'network' }, connEl)),
        h('div', { id: 'net-wifi' }, win({ title: 'Wi-Fi', icon: 'network' }, wifiEl)),
        h('div', { id: 'net-bt' }, btPanels.el),
      ),
      h('div', { class: 'col' },
        h('div', { id: 'net-webhelper' }, helperPanels.el),
        h('div', { id: 'net-guest' }, win({ title: 'Mac OS Network Status', icon: 'mac' }, guestEl)),
        h('div', { id: 'net-mgmt' }, otherPanels.el),
      ),
    ),
  );

  async function load() {
    try {
      data = await get('/api/v1/network');
      error = null;
    } catch (err) {
      error = err;
    }
    renderAll();
  }

  function renderAll() {
    if (error && !data) {
      swap(connEl, errorState(error, load));
      swap(wifiEl, '');
      swap(guestEl, '');
      return;
    }
    if (!data) return;
    renderConnection();
    renderWifi();
    renderGuest();
  }

  function renderConnection() {
    const host = data.host || {};
    const ifaces = data.interfaces || [];
    const primary = ifaces.find((i) => i.name === host.primary) || ifaces.find((i) => i.state === 'connected');
    const summary = primary
      ? `Connected through ${primary.label || primary.kind || primary.name}${primary.ssid ? ` (${primary.ssid})` : ''}.`
      : host.state === 'connected'
        ? 'Connected.'
        : 'Not connected to a network. Mac OS still starts and runs offline.';
    const mgmt = data.management || {};
    swap(
      connEl,
      h('p', { class: 'lead' }, summary),
      ifaces.length
        ? h(
            'ul',
            { class: 'iface-list' },
            ...ifaces.map((i) =>
              h(
                'li',
                { class: 'iface' },
                h('span', { class: 'iface-icon', html: pixelIcon('network', 32) }),
                h(
                  'div',
                  { class: 'iface-main' },
                  h('div', { class: 'machine-head' }, h('strong', null, `${i.label || i.kind} `, h('span', { class: 'muted' }, i.name)), h('span', { class: 'tag-row' }, stateBadge(i.state), i.management ? badge('Web management', 'quiet', 'This page can be reached through this connection') : null)),
                  kv(
                    [
                      i.ssid ? ['Network', i.ssid] : null,
                      i.addresses && i.addresses.length ? ['Addresses', h('ul', { class: 'plain-list' }, ...i.addresses.map((a) => h('li', null, h('code', null, a))))] : null,
                      i.gateway ? ['Router', h('code', null, i.gateway)] : null,
                      i.mac ? ['Hardware address', h('code', null, i.mac)] : null,
                    ],
                    'kv-compact',
                  ),
                ),
              ),
            ),
          )
        : emptyState('No network adapters were found.', 'network'),
      mgmt.urls && mgmt.urls.length
        ? h(
            'div',
            { class: 'subsection' },
            h('h3', null, 'Open this page from another device'),
            h('ul', { class: 'plain-list' }, ...mgmt.urls.map((u) => h('li', { class: 'with-copy' }, h('code', null, u), copyButton(u, 'Copy', { what: 'address', ariaLabel: `Copy ${u}`, compact: true })))),
            h('p', { class: 'muted small' }, 'If the .local name doesn’t work on your device, use the IP address.'),
          )
        : null,
    );
  }

  function renderGuest() {
    const g = data.guest || {};
    swap(
      guestEl,
      kv([
        ['Mac OS network', g.mode === 'none' ? 'Off' : g.mode === 'nat' ? 'Shared with RetroBoot (NAT)' : g.mode],
        ['State', stateBadge(g.state)],
        g.address ? ['Mac OS address', h('code', null, g.address)] : null,
        g.adapter ? ['Adapter in Mac OS', g.adapter] : null,
      ]),
      h('p', { class: 'muted small' }, 'Mac OS reaches the network through RetroBoot. Other computers can’t connect into Mac OS directly, and AppleTalk discovery isn’t forwarded.'),
    );
  }

  let joiningNow = null; // the network this page asked to join (job JSON has no parameters)
  function renderWifi() {
    const w = data.wifi || {};
    if (!w.available) {
      swap(
        wifiEl,
        h('div', { class: 'unavailable' }, h('p', null, badge('Unavailable', 'off'), ' ', w.reason || 'This RetroBoot has no supported Wi-Fi adapter.'), h('p', { class: 'muted small' }, 'Wired networking works normally. Wi-Fi controls appear here when a supported adapter is present.')),
      );
      return;
    }
    const scanning = activeJobs('wifi.scan').length > 0;
    const joining = activeJobs('wifi.connect')[0];
    const joiningSsid = joining ? joiningNow : null;
    const cur = w.state === 'connected' && w.ssid ? (w.networks || []).find((n) => n.active)?.ssid || w.ssid : null;
    // AirPort-menu order: the joined network first (with its check mark), then by signal.
    const networks = [...(w.networks || [])].sort((a, b) => (b.ssid === cur) - (a.ssid === cur) || b.signal - a.signal || a.ssid.localeCompare(b.ssid));
    const status = cur ? `Connected to “${cur}”.` : w.state === 'connecting' ? 'Connecting…' : w.radio === false ? 'Wi-Fi is turned off.' : 'Not connected to a Wi-Fi network.';
    const row = (n) => {
      const isCur = n.ssid === cur;
      const parts = [
        h('span', { class: 'net-check', 'aria-hidden': 'true' }, isCur ? '✓' : ''),
        h('span', { class: 'net-name' }, n.ssid || h('em', null, 'Hidden network')),
        h('span', { class: 'tag-row' }, isCur ? badge('Connected', 'ok') : joining && joiningSsid === n.ssid ? badge('Joining…', 'pending') : n.known ? badge('Saved', 'quiet') : null),
        secured(n) ? h('span', { class: 'net-lock', title: SECURITY[n.security] || n.security, 'aria-label': `Secured (${SECURITY[n.security] || n.security})`, html: lockIcon() }) : h('span', { class: 'net-lock net-open', 'aria-label': 'Open network' }),
        signalBars(n.signal),
      ];
      if (isCur) return h('li', { class: 'net is-joined', 'aria-current': 'true' }, h('div', { class: 'net-row' }, ...parts));
      // One click (or Return/Space) joins: a password dialog for a secured network, at once for an open one.
      return h('li', { class: 'net' }, h('button', { type: 'button', class: 'net-row', dataset: { focusKey: `join-${n.ssid}` }, disabled: !!joining, 'aria-label': `Join ${n.ssid}`, onClick: () => join(n) }, ...parts));
    };
    swap(
      wifiEl,
      h('div', { class: 'wifi-head' }, h('div', null, h('p', { class: 'lead' }, status), networks.length ? h('p', { class: 'muted small' }, 'Click a network to join it.') : null), button(scanning ? 'Scanning…' : 'Scan Again', { icon: 'refresh', className: 'btn-small', busy: scanning, focusKey: 'wifi-scan', onClick: scan })),
      joining ? notice({ level: 'info', title: joiningSsid ? `Joining “${joiningSsid}”…` : 'Joining a network…', message: joining.message && !/^Joining/.test(joining.message) ? joining.message : 'This takes a few seconds.' }) : null,
      h('h3', { class: 'subhead' }, 'Networks nearby', w.scanned_at ? h('span', { class: 'muted small' }, ` · scanned ${relTime(w.scanned_at)}`) : null),
      networks.length ? h('ul', { class: 'net-list net-choose' }, ...networks.map(row)) : emptyState('No networks found. Try scanning again.', 'network'),
      h('h3', { class: 'subhead' }, 'Saved networks'),
      (w.profiles || []).length
        ? h('ul', { class: 'net-list' }, ...w.profiles.map((p) => h('li', { class: 'net' }, h('span', { class: 'net-name' }, p.ssid), h('span', { class: 'muted small' }, SECURITY[p.security] || p.security || ''), button('Forget…', { className: 'btn-small', variant: 'quiet', focusKey: `forget-${p.id}`, onClick: () => forget(p) }))))
        : h('p', { class: 'muted' }, 'No saved networks.'),
      h('p', { class: 'muted small' }, 'Passwords are stored on RetroBoot, not in this browser, and are never shown again.'),
    );
  }

  async function scan() {
    try {
      const res = await post('/api/v1/network/wifi/scan', {});
      const job = asJob(res);
      renderWifi();
      if (job) {
        const final = await track(job, { quiet: true });
        if (final.state === 'failed') toast(jobOutcome(final), { level: 'error' }); // quiet jobs aren't in the tray
      }
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error' });
    }
    load();
  }

  async function join(n, prevError) {
    let password;
    if (secured(n)) {
      const pwId = uid('pw');
      const showId = uid('show');
      const pw = h('input', { type: 'password', id: pwId, autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', maxlength: '64', 'aria-invalid': prevError ? 'true' : null });
      // Show password: flips the field here only; nothing is sent until Join.
      const show = h('input', { type: 'checkbox', id: showId, onchange: (e) => (pw.type = e.target.checked ? 'text' : 'password') });
      const errEl = h('p', { class: 'field-error', hidden: !prevError, role: 'alert' }, prevError || '');
      const choice = await dialog({
        title: `The network “${n.ssid}” requires a password.`,
        icon: 'wifi',
        message: ['Type the password, then click Join.'],
        content: h('div', { class: 'field' }, h('label', { for: pwId }, 'Password:'), pw, errEl, h('label', { class: 'check', for: showId }, show, h('span', null, 'Show password'))),
        buttons: [
          { label: 'Cancel', value: 'cancel' },
          { label: 'Join', value: 'ok', variant: 'default', primary: true },
        ],
        onOpen: () => pw.focus(),
        validate: () => {
          const v = pw.value;
          const bad = !v ? 'Type the password for this network.' : !(v.length >= 8 && v.length <= 63) && v.length !== 64 ? 'Wi-Fi passwords are 8 to 63 characters.' : null;
          if (!bad) return true;
          errEl.hidden = false;
          errEl.textContent = bad;
          pw.setAttribute('aria-invalid', 'true');
          pw.focus();
          return false;
        },
      });
      password = pw.value;
      pw.value = '';
      if (choice !== 'ok') return;
    }
    const body = { ssid: n.ssid };
    if (password) body.password = password;
    password = undefined;
    try {
      const job = asJob(await post('/api/v1/network/wifi/connect', body));
      delete body.password;
      joiningNow = n.ssid;
      renderWifi();
      if (job) {
        const final = await track(job, { label: `Joining “${n.ssid}”` });
        joiningNow = null;
        load();
        if (final.state === 'succeeded') {
          toast(`Connected to “${n.ssid}”.`, { level: 'ok' });
        } else if (secured(n)) {
          return join(n, jobOutcome(final)); // the password again, with the reason
        } else {
          await dialog({ title: `Could not join “${n.ssid}”.`, icon: 'caution', message: [jobOutcome(final)] });
        }
        return;
      }
    } catch (err) {
      delete body.password;
      if (err.status === 422 && err.fields && err.fields.password) return join(n, err.fields.password);
      toast(friendlyMessage(err), { level: 'error' });
    }
    load();
  }

  async function forget(p) {
    const ok = await confirm({ title: `Forget “${p.ssid}”?`, icon: 'caution', message: ['RetroBoot removes the saved password. If it is connected to this network now, it disconnects, and this page may stop responding if that was its only connection.'], confirmLabel: 'Forget Network' });
    if (!ok) return;
    try {
      await post('/api/v1/network/wifi/forget', { id: p.id, ssid: p.ssid });
      toast(`Forgot “${p.ssid}”.`, { level: 'ok' });
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error' });
    }
    load();
  }

  load();
  scope.interval(() => document.visibilityState === 'visible' && load(), 10000);
  scope.add(subscribe('jobs', () => data && renderWifi()));
  return { el, destroy: () => scope.destroy() };
}
