// Bluetooth window extras on the Network page (hostd/retroboot/web/bluetooth_api.py):
// the radio's state above the "Bluetooth" setting (bluetooth.enabled, applied through
// the ordinary changes bar), then the devices: paired ones first (✓ when connected, with
// battery), then keyboards, mice and trackpads in pairing mode with their signal. One
// click pairs a device; a keyboard's code appears here when the pairing started here.

import { get, post, asJob, friendlyMessage } from './api.js';
import { h, swap } from './util.js';
import { badge, button, dialog, confirm, toast, emptyState } from './ui.js';
import { pixelIcon } from './icons.js';
import { track, jobOutcome } from './jobs.js';

const STATE = {
  on: ['On', 'ok'],
  'turning-on': ['Turning on…', 'pending'],
  off: ['Off', 'off'],
  blocked: ['Off (switch)', 'warn'],
  unavailable: ['No hardware', 'off'],
};

function signalBars(n) {
  return h('span', { class: 'signal', role: 'img', 'aria-label': `Signal ${n} of 4`, title: `Signal ${n} of 4` }, ...[1, 2, 3, 4].map((i) => h('span', { class: i <= n ? 'on' : null })));
}

function detail(d) {
  const parts = [d.kind_label];
  if (d.paired) {
    parts.push(d.connected ? 'Connected' : 'Not connected');
    if (d.battery != null) parts.push(`Battery ${d.battery}%`);
  } else parts.push('Not paired');
  return parts.join(' · ');
}

/** The code, large, with a space between digits (as on the computer's screen). */
function codeBlock(code, entered) {
  return h(
    'div',
    { class: 'bt-code-box' },
    h('p', { class: 'bt-code', 'aria-label': code.split('').join(' ') }, code.split('').join(' ')),
    entered ? h('p', { class: 'muted small' }, `${entered} of ${code.length} digits typed`) : null,
  );
}

export function bluetoothExtras() {
  const top = h('div', { class: 'bt-status' });
  const el = h('div', { class: 'bt-body' });
  let data = null;
  let error = null;
  let timer = null;
  let alive = true;
  let pairing = null; // {address, name, dialog element, content element, job}
  let scannedOnce = false; // look for devices once when the window opens

  async function load() {
    try {
      data = await get('/api/v1/bluetooth');
      error = null;
    } catch (err) {
      error = err;
    }
    if (!alive) return;
    render();
    if (pairing) updatePairing();
    schedule();
    if (!scannedOnce && data && data.state === 'on') {
      scannedOnce = true;
      if (!data.scanning) scan();
    }
  }

  function schedule() {
    clearTimeout(timer);
    if (!alive) return;
    const busy = pairing || (data && (data.scanning || (data.pairing && !data.pairing.done)));
    timer = setTimeout(() => (document.visibilityState === 'visible' ? load() : schedule()), busy ? 800 : 4000);
  }

  let shown = null; // what is on screen: re-render only on a change, so clicks and focus survive polling
  function render() {
    const sig = JSON.stringify([error && !data ? String(error.message || error) : null, data && {
      state: data.state, scanning: data.scanning, available: data.available, reason: data.reason,
      busy: !!(data.pairing && !data.pairing.done), assistant: data.assistant && data.assistant.active,
      devices: (data.devices || []).map((d) => [d.address, d.name, d.kind_label, d.paired, d.connected, d.battery, d.signal]),
    }]);
    if (sig === shown) return;
    shown = sig;
    if (error && !data) {
      swap(top, h('p', { class: 'muted small' }, friendlyMessage(error)));
      swap(el, '');
      return;
    }
    if (!data) {
      swap(top, h('p', { class: 'muted small' }, 'Checking Bluetooth…'));
      return;
    }
    const [word, tone] = STATE[data.state] || [data.state, 'quiet'];
    const devices = data.devices || [];
    const connected = devices.filter((d) => d.paired && d.connected);
    let line;
    if (!data.available) line = data.reason || 'This computer has no Bluetooth hardware.';
    else if (data.state === 'blocked') line = 'Bluetooth is turned off with this computer’s wireless switch or key.';
    else if (data.state === 'off') line = 'Turn Bluetooth on to use Bluetooth keyboards, mice and trackpads.';
    else if (connected.length === 1) line = `“${connected[0].name}” is connected.`;
    else if (connected.length) line = `${connected.length} devices are connected.`;
    else line = 'No Bluetooth devices are connected.';
    swap(top, h('div', { class: 'bt-head' }, h('span', { class: 'bt-icon', html: pixelIcon('bluetooth', 32) }), h('p', { class: 'lead' }, badge(word, tone), ' ', line)));
    if (!data.available) {
      swap(el, h('p', { class: 'muted small' }, 'Use a USB keyboard and mouse instead.'));
      return;
    }
    const on = data.state === 'on';
    const paired = devices.filter((d) => d.paired);
    const found = devices.filter((d) => !d.paired);
    const busy = data.pairing && !data.pairing.done;
    const row = (d) => {
      const parts = [
        h('span', { class: 'net-check', 'aria-hidden': 'true' }, d.paired && d.connected ? '✓' : ''),
        h('span', { class: 'net-name' }, d.name),
        h('span', { class: 'muted small' }, detail(d)),
        d.paired ? null : signalBars(d.signal || 1),
      ];
      if (!d.paired) {
        // One click (or Return/Space) pairs it, like joining a Wi-Fi network.
        return h('li', { class: 'net' }, h('button', { type: 'button', class: 'net-row', dataset: { focusKey: `bt-pair-${d.address}` }, disabled: !on || busy, 'aria-label': `Pair ${d.name}`, onClick: () => pair(d) }, ...parts));
      }
      return h(
        'li',
        { class: 'net' },
        h('div', { class: 'net-row' }, ...parts),
        h(
          'span',
          { class: 'tag-row' },
          !d.connected ? button('Connect', { className: 'btn-small', disabled: !on || busy, focusKey: `bt-connect-${d.address}`, ariaLabel: `Connect ${d.name}`, onClick: () => connect(d) }) : null,
          button('Forget…', { className: 'btn-small', variant: 'quiet', focusKey: `bt-forget-${d.address}`, ariaLabel: `Forget ${d.name}…`, onClick: () => forget(d) }),
        ),
      );
    };
    const hint = !on ? null : found.length ? 'Click a device to pair it.' : data.scanning ? 'Looking for devices… Put your keyboard, mouse or trackpad in pairing mode.' : 'Put a device in pairing mode, then click Scan Again.';
    swap(
      el,
      h('div', { class: 'wifi-head' }, h('div', null, hint ? h('p', { class: 'muted small' }, hint) : null), button(data.scanning ? 'Scanning…' : 'Scan Again', { icon: 'refresh', className: 'btn-small', busy: data.scanning, disabled: !on, focusKey: 'bt-scan', onClick: scan })),
      h('h3', { class: 'subhead' }, 'My devices'),
      paired.length ? h('ul', { class: 'net-list' }, ...paired.map(row)) : h('p', { class: 'muted' }, 'No paired devices.'),
      on ? h('h3', { class: 'subhead' }, 'Devices nearby') : null,
      on ? (found.length ? h('ul', { class: 'net-list net-choose' }, ...found.map(row)) : emptyState(data.scanning ? 'Looking for devices…' : 'No devices in pairing mode found.', 'bluetooth')) : null,
      data.assistant && data.assistant.active ? h('p', { class: 'muted small' }, 'No keyboard is attached to the computer, so it is pairing a Bluetooth keyboard in pairing mode by itself.') : null,
      h('p', { class: 'muted small' }, 'Paired devices reconnect by themselves when they are switched on near the computer.'),
    );
  }

  async function scan() {
    try {
      await post('/api/v1/bluetooth/scan', { seconds: 40 });
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error' });
    }
    load();
  }

  // ---- pairing ----------------------------------------------------------------
  function updatePairing() {
    const p = data && data.pairing;
    if (!pairing || !p || p.address !== pairing.address) return;
    const name = pairing.name;
    if ((p.stage === 'passkey' || p.stage === 'pin') && p.passkey) {
      const keyboard = ['keyboard', 'combo', 'other'].includes(p.kind);
      swap(
        pairing.content,
        h('p', { class: 'lead' }, keyboard ? `Type this code on “${name}”, then press Return:` : `Enter this code on “${name}”:`),
        codeBlock(p.passkey, p.entered),
        h('p', { class: 'muted small' }, keyboard ? 'Type it on the Bluetooth keyboard itself, which must be near the RetroBoot computer.' : 'Follow the device’s instructions for entering a code.'),
      );
    } else if (p.stage === 'confirm' && p.passkey && !pairing.asked) {
      pairing.asked = true;
      askConfirm(p.passkey);
    } else if (p.stage === 'connecting') {
      swap(pairing.content, h('p', { class: 'lead' }, `Connecting to “${name}”…`));
    } else if (p.code_shown_on === 'computer') {
      swap(pairing.content, h('p', { class: 'lead' }, 'The code is shown on the RetroBoot computer’s screen.'));
    }
  }

  async function askConfirm(code) {
    const name = pairing.name;
    if (pairing.dialog) pairing.dialog.close('confirming');
    const choice = await dialog({
      title: `Does “${name}” show this code?`,
      icon: 'bluetooth',
      message: ['If the codes match, click Pair.'],
      content: codeBlock(code),
      buttons: [
        { label: 'Cancel', value: 'cancel' },
        { label: 'Pair', value: 'ok', variant: 'default', primary: true, autofocus: true },
      ],
    });
    try {
      await post('/api/v1/bluetooth/confirm', { accept: choice === 'ok' });
    } catch (err) {
      /* the pairing ended meanwhile */
    }
  }

  async function pair(d) {
    let job;
    try {
      job = asJob(await post('/api/v1/bluetooth/pair', { address: d.address }));
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error' });
      load();
      return;
    }
    const content = h('div', { class: 'bt-pairing', 'aria-live': 'polite' }, h('p', { class: 'lead' }, `Pairing with “${d.name}”…`), h('p', { class: 'muted small' }, 'Keep the device turned on and near the RetroBoot computer.'));
    pairing = { address: d.address, name: d.name, content, dialog: null, job, asked: false };
    load();
    const dlg = dialog({
      title: 'Bluetooth',
      icon: 'bluetooth',
      content,
      buttons: [{ label: 'Cancel', value: 'cancel', autofocus: true }],
      onOpen: (el) => {
        if (pairing) pairing.dialog = el;
      },
    });
    dlg.then(async (value) => {
      if (value === 'cancel' && pairing && pairing.job === job) {
        pairing.canceled = true;
        try {
          await post('/api/v1/bluetooth/cancel', {});
        } catch (err) {
          /* already over */
        }
      }
    });
    const final = job ? await track(job, { label: `Pairing with “${d.name}”`, quiet: true }) : null;
    const canceled = pairing && pairing.canceled;
    if (pairing && pairing.dialog && pairing.dialog.open) pairing.dialog.close('done');
    pairing = null;
    try {
      await post('/api/v1/bluetooth/dismiss', {});
    } catch (err) {
      /* ignore */
    }
    load();
    if (!final) return;
    if (final.state === 'succeeded') toast(jobOutcome(final), { level: 'ok' });
    else if (!canceled && !(final.error && final.error.code === 'canceled'))
      await dialog({ title: `Could not pair with “${d.name}”.`, icon: 'caution', message: [jobOutcome(final)] });
  }

  async function connect(d) {
    try {
      const job = asJob(await post('/api/v1/bluetooth/connect', { address: d.address }));
      load();
      if (job) {
        const final = await track(job, { label: `Connecting to “${d.name}”` });
        if (final.state !== 'succeeded') toast(jobOutcome(final), { level: 'error' });
      }
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error' });
    }
    load();
  }

  async function forget(d) {
    const ok = await confirm({ title: `Forget “${d.name}”?`, icon: 'caution', message: ['RetroBoot removes the pairing. To use the device again, put it in pairing mode and pair it again.'], confirmLabel: 'Forget Device', danger: true });
    if (!ok) return;
    try {
      await post('/api/v1/bluetooth/forget', { address: d.address });
      toast(`“${d.name}” was forgotten.`, { level: 'ok' });
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error' });
    }
    load();
  }

  load();
  return {
    top,
    el,
    update: () => {},
    destroy: () => {
      alive = false;
      clearTimeout(timer);
    },
  };
}
