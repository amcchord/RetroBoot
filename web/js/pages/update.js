// Software Update (GET/POST /api/v1/update…; docs/ARCHITECTURE.md section 13).
// Install a newer RetroBoot while Mac OS keeps running, restart once to finish,
// or go back to the version that was installed before.

import { post, csrf, asJob, friendlyMessage, ApiError } from '../api.js';
import { h, swap, Scope, fmtDateTime, formatBytes } from '../util.js';
import { win, button, pageHeader, progressBar, notice, kv, confirm, toast, dialog } from '../ui.js';
import { store, subscribe, refreshStatus, pollFast, emit } from '../store.js';
import { track, activeJobs, jobOutcome } from '../jobs.js';
import { trackUntilGone } from '../actions.js';
import { settingsPanels } from '../settings.js';
import { hw } from '../words.js';

export const meta = { title: 'Software Update', icon: 'update' };

const u = () => (store.status && store.status.update) || null;
const updateJob = () => activeJobs('update.')[0] || null;

function onSave() {
  return `Applications can ask to save open documents; answer those questions on ${hw().the}.`;
}

async function followInstall(job) {
  pollFast(60000);
  const final = await track(job, { label: 'Installing an update' });
  await refreshStatus().catch(() => {});
  if (final.state === 'succeeded') toast(jobOutcome(final), { level: 'ok', timeout: 8000 });
  else dialog({ title: 'The update couldn’t be installed', icon: 'caution', message: jobOutcome(final), buttons: [{ label: 'OK', value: 'ok', variant: 'default', autofocus: true }] });
  return final;
}

async function restartToUpdate() {
  const st = (u() || {}).staged;
  if (!st) return;
  const ok = await confirm({
    title: `Restart to Install RetroBoot ${st.version}?`,
    icon: 'note',
    message: [`Mac OS shuts down first, the same as choosing Shut Down from the Special menu. ${onSave()}`, `Then ${hw().the} restarts with RetroBoot ${st.version}. Your Mac, documents and settings are kept. This page reconnects by itself.`],
    confirmLabel: 'Restart to Update',
  });
  if (!ok) return;
  await powerJob('/api/v1/update/restart', `Installing RetroBoot ${st.version}`, [`Mac OS has shut down and ${hw().the} is restarting with RetroBoot ${st.version}.`, 'This page reconnects by itself when RetroBoot is back, usually within a minute or two.']);
}

async function goBack() {
  const prev = (u() || {}).previous;
  if (!prev) return;
  const word = prev.newer ? 'Switch to' : 'Go Back to';
  const message = [`Mac OS shuts down first, the same as choosing Shut Down from the Special menu. ${onSave()}`, `Then ${hw().the} restarts with RetroBoot ${prev.label}. Your Mac, documents and settings are kept.`];
  if (prev.update_capable === false) message.push(`RetroBoot ${prev.version} can’t install updates itself. To update again later, use the RetroBoot installer USB disk.`);
  const ok = await confirm({ title: `${word} RetroBoot ${prev.version}?`, icon: 'caution', message, confirmLabel: `${word} ${prev.version}` });
  if (!ok) return;
  await powerJob('/api/v1/update/rollback', `Switching to RetroBoot ${prev.version}`, [`Mac OS has shut down and ${hw().the} is restarting with RetroBoot ${prev.version}.`, 'This page reconnects by itself when RetroBoot is back.']);
}

async function powerJob(path, heading, text) {
  const bootId = store.status && store.status.host && store.status.host.boot_id;
  let job;
  try {
    job = asJob(await post(path, {}));
  } catch (err) {
    toast(friendlyMessage(err), { level: 'error', timeout: 8000 });
    return;
  }
  pollFast(60000);
  if (!job) return;
  const final = await trackUntilGone(job);
  if (final.state === 'succeeded' || (final.error && final.error.code === 'job-lost')) {
    emit('host-power', { action: 'reboot', bootId, heading, text });
  } else {
    await refreshStatus().catch(() => {});
    dialog({ title: 'RetroBoot didn’t restart', icon: 'caution', message: jobOutcome(final), buttons: [{ label: 'OK', value: 'ok', variant: 'default', autofocus: true }] });
  }
}

function uploadFile(file, onProgress) {
  // XMLHttpRequest for upload progress; same session cookie, CSRF header and JSON errors as api.js.
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/v1/update/bundle');
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.setRequestHeader('X-RetroBoot-CSRF', csrf() || '');
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      let body = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        body = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else {
        const e = (body && body.error) || {};
        reject(new ApiError(xhr.status, e.code || `http-${xhr.status}`, e.message || 'The upload failed.', e.fields || null, body));
      }
    };
    xhr.onerror = () => reject(new ApiError(0, 'unreachable', ''));
    xhr.send(file);
  });
}

function versionBlock(info) {
  const cur = info.current || {};
  const rows = [['Installed', cur.label ? `RetroBoot ${cur.label}` : 'Unknown']];
  if (info.previous) rows.push([info.previous.newer ? 'Also installed' : 'Previous version', `RetroBoot ${info.previous.label}`]);
  const out = [kv(rows)];
  const last = info.last_result;
  if (last && last.message) {
    const level = last.result === 'failed' ? 'warning' : 'info';
    out.push(notice({ level, title: { failed: 'The last update didn’t finish', installed: 'Updated', 'rolled-back': 'Went back' }[last.result] || 'Software Update', message: `${last.message}${last.at ? ` (${fmtDateTime(last.at)})` : ''}` }));
  }
  if (info.boot && info.boot.kind === 'trial' && !info.boot.committed) out.push(notice({ level: 'info', title: 'Checking the new version', message: 'RetroBoot confirms this version as soon as Mac OS starts. If it can’t, it goes back to the previous version by itself.' }));
  return out;
}

function stagedBlock(info) {
  const st = info.staged;
  const job = updateJob();
  if (job && job.kind === 'update.install') {
    return [h('p', { class: 'state-line' }, h('strong', null, job.message || 'Installing…')), progressBar(job.progress, 'Installing the update'), h('p', { class: 'muted small' }, 'Mac OS keeps running while the update is written. RetroBoot asks you to restart when it’s ready.')];
  }
  if (!st) return null;
  if (st.status === 'writing') return [h('p', null, `RetroBoot ${st.label} is being written…`)];
  const failed = st.status === 'failed';
  return [
    h('p', null, h('strong', null, failed ? `RetroBoot ${st.label} didn’t finish starting last time.` : `RetroBoot ${st.label} is ready to install.`)),
    st.notes ? h('p', { class: 'muted' }, st.notes) : null,
    h('p', { class: 'muted small' }, 'Installing restarts RetroBoot once. Mac OS shuts down cleanly first.'),
    h(
      'div',
      { class: 'row-actions' },
      button('Don’t Install', {
        onClick: async () => {
          try {
            await post('/api/v1/update/discard', {});
            await refreshStatus();
          } catch (err) {
            toast(friendlyMessage(err), { level: 'error' });
          }
        },
        disabled: !!job,
      }),
      button(failed ? 'Try Again…' : 'Restart to Update…', { variant: 'default', icon: 'restart', onClick: restartToUpdate, disabled: !!job }),
    ),
  ].filter(Boolean);
}

export function render() {
  const scope = new Scope();
  const verEl = h('div');
  const stagedEl = h('div');
  const uploadEl = h('div');
  const usbEl = h('div');
  const serverEl = h('div');
  const prevEl = h('div');
  const historyEl = h('div');
  const stagedWin = win({ title: 'Ready to Install', icon: 'update' }, stagedEl);
  const panels = scope.add(settingsPanels('update'));
  let uploading = null; // {name, fraction}
  const fileInput = h('input', { type: 'file', accept: '.rbu,application/octet-stream', class: 'visually-hidden', 'aria-label': 'Update file' });

  fileInput.addEventListener('change', async () => {
    const file = fileInput.files && fileInput.files[0];
    fileInput.value = '';
    if (!file) return;
    if (!/\.rbu$/i.test(file.name)) {
      toast('Choose a RetroBoot update file (its name ends in .rbu).', { level: 'error' });
      return;
    }
    uploading = { name: file.name, fraction: 0 };
    paint();
    let res;
    try {
      res = await uploadFile(file, (f) => {
        uploading.fraction = f;
        paint();
      });
    } catch (err) {
      uploading = null;
      paint();
      toast(friendlyMessage(err), { level: 'error', timeout: 8000 });
      return;
    }
    uploading = null;
    const job = asJob(res);
    paint();
    if (job) await followInstall(job);
  });

  async function installFrom(body) {
    let job;
    try {
      job = asJob(await post('/api/v1/update/install', body));
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error', timeout: 8000 });
      return;
    }
    if (job) await followInstall(job);
  }

  async function check() {
    let job;
    try {
      job = asJob(await post('/api/v1/update/check', {}));
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error', timeout: 8000 });
      return;
    }
    if (!job) return;
    const final = await track(job, { label: 'Checking for updates', quiet: true });
    await refreshStatus().catch(() => {});
    toast(jobOutcome(final), { level: final.state === 'succeeded' ? 'ok' : 'error', timeout: 6000 });
  }

  async function lookUsb() {
    try {
      await post('/api/v1/update/usb', {});
      await refreshStatus();
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error' });
    }
  }

  const el = h(
    'div',
    { class: 'page' },
    pageHeader('update', 'Software Update', 'Install a newer RetroBoot, or go back to the version you had before. Mac OS keeps running while an update installs; RetroBoot restarts once at the end.'),
    h(
      'div',
      { class: 'two-col' },
      h('div', { class: 'col' }, win({ title: 'This RetroBoot', icon: 'mac' }, verEl), stagedWin, win({ title: 'Install an Update File', icon: 'box' }, uploadEl, fileInput), win({ title: 'USB Disk', icon: 'storage' }, usbEl)),
      h('div', { class: 'col' }, win({ title: 'Update Server', icon: 'network' }, serverEl, panels.el), win({ title: 'Previous Version', icon: 'recovery' }, prevEl), win({ title: 'History', icon: 'diagnostics' }, historyEl)),
    ),
  );

  const sigs = {};
  const patch = (k, target, sig, build) => {
    if (sigs[k] === sig) return;
    sigs[k] = sig;
    swap(target, ...[].concat(build()).filter(Boolean));
  };

  function paint() {
    const info = u();
    const job = updateJob();
    if (!info) {
      patch('ver', verEl, 'none', () => h('p', { class: 'muted' }, 'Loading…'));
      return;
    }
    if (!info.available) {
      patch('ver', verEl, JSON.stringify(['na', info.reason, info.current]), () => [kv([['Installed', info.current && info.current.label ? `RetroBoot ${info.current.label}` : 'Unknown']]), notice({ level: 'info', title: 'Software Update isn’t available', message: info.reason })]);
      stagedWin.hidden = true;
      for (const e of [uploadEl, usbEl, serverEl, prevEl, historyEl]) swap(e);
      return;
    }
    const busy = !!job || !!uploading || !info.can_install;
    patch('ver', verEl, JSON.stringify([info.current, info.previous, info.last_result, info.boot]), () => versionBlock(info));
    const staged = stagedBlock(info);
    stagedWin.hidden = !staged;
    patch('staged', stagedEl, JSON.stringify([info.staged, job && [job.id, job.message, job.progress]]), () => staged || []);
    patch('upload', uploadEl, JSON.stringify([uploading, busy]), () =>
      uploading
        ? [h('p', null, `Uploading ${uploading.name}…`), progressBar(uploading.fraction, 'Uploading the update')]
        : [h('p', null, 'Choose a RetroBoot update file (.rbu) on this device. RetroBoot checks its signature before installing it.'), h('div', { class: 'row-actions' }, button('Choose Update File…', { icon: 'download', onClick: () => fileInput.click(), disabled: busy }))],
    );
    patch('usb', usbEl, JSON.stringify([info.usb, busy]), () => {
      const list = info.usb || [];
      return [
        list.length ? h('ul', { class: 'change-list' }, ...list.map((b) => h('li', null, h('strong', null, `RetroBoot ${b.label}`), ` on ${b.disk} (${formatBytes(b.bytes)}) `, button('Install', { className: 'btn-small', onClick: () => installFrom({ source: 'usb', id: b.id }), disabled: busy })))) : h('p', { class: 'muted' }, `No update files found. Copy a .rbu file to a USB disk and connect it to ${hw().the}.`),
        h('div', { class: 'row-actions' }, button('Look Again', { icon: 'refresh', onClick: lookUsb, disabled: busy })),
      ];
    });
    patch('server', serverEl, JSON.stringify([info.url, info.check, busy]), () => {
      const found = info.check && info.check.found;
      if (!info.url) return h('p', { class: 'muted' }, 'Enter the address of an update server below to check it for updates. RetroBoot never installs anything by itself.');
      return [
        found ? h('p', null, h('strong', null, `RetroBoot ${found.label} is available`), ` (${formatBytes(found.bytes)}).`) : h('p', null, info.check ? `RetroBoot is up to date (checked ${fmtDateTime(info.check.at)}).` : 'Not checked yet.'),
        found && found.notes ? h('p', { class: 'muted' }, found.notes) : null,
        h('div', { class: 'row-actions' }, button('Check for Updates', { icon: 'refresh', onClick: check, disabled: busy }), found ? button('Download and Install', { variant: 'default', icon: 'download', onClick: () => installFrom({ source: 'server' }), disabled: busy }) : null),
      ];
    });
    patch('prev', prevEl, JSON.stringify([info.previous, busy]), () => {
      const prev = info.previous;
      if (!prev) return h('p', { class: 'muted' }, 'After an update, the version you had before stays on this computer, and you can go back to it here.');
      const word = prev.newer ? 'Switch to' : 'Go Back to';
      return [h('p', null, `RetroBoot ${prev.label} is still on this computer.`), h('div', { class: 'row-actions' }, button(`${word} ${prev.version}…`, { icon: 'restart', onClick: goBack, disabled: busy }))];
    });
    patch('history', historyEl, JSON.stringify(info.history), () => {
      const hist = info.history || [];
      if (!hist.length) return h('p', { class: 'muted' }, 'No updates yet.');
      return h('ul', { class: 'change-list' }, ...hist.map((r) => h('li', null, h('span', { class: 'muted' }, `${fmtDateTime(r.at)} · `), r.message || `${r.kind} ${r.result}`)));
    });
  }

  paint();
  scope.add(subscribe('status', paint));
  scope.add(subscribe('jobs', paint));
  refreshStatus().catch(() => {});
  return { el, destroy: () => scope.destroy() };
}
