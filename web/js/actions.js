// Guest lifecycle and RetroBoot power actions with explicit confirmations
// (ARCHITECTURE.md sections 6 and 7).

import { post, asJob, friendlyMessage, on as onApi } from './api.js';
import { h, uid } from './util.js';
import { confirm, dialog, toast } from './ui.js';
import { track, lifecycleJob, forceOrPowerJob, activeJobs, labelFor, jobOutcome, doneMessage } from './jobs.js';
import { refreshStatus, refreshSettings, pollFast, store, emit } from './store.js';
import { hw } from './words.js';

const saveNote = () => `Applications can ask to save open documents; answer those questions on ${hw().the}.`;

const COPY = {
  restart: {
    title: 'Restart Mac OS?',
    get message() {
      return [`Mac OS is asked to restart, the same as choosing Restart from the Special menu. ${saveNote()}`];
    },
    confirmLabel: 'Restart',
    icon: 'caution',
  },
  shutdown: {
    title: 'Shut Down Mac OS?',
    get message() {
      return [`Mac OS is asked to shut down, the same as choosing Shut Down from the Special menu. ${saveNote()}`];
    },
    confirmLabel: 'Shut Down',
    icon: 'caution',
  },
  'force-restart': {
    title: 'Force Restart Mac OS?',
    message: [
      'This stops Mac OS immediately, like pulling the power cord, and then starts it again.',
      'Unsaved work in every application is lost. The startup disk may be checked for errors when Mac OS starts.',
      'Use this only when Mac OS is frozen and a normal restart has not worked.',
    ],
    confirmLabel: 'Force Restart',
    icon: 'stop',
    danger: true,
    acknowledge: 'I understand that unsaved work will be lost.',
  },
  'force-stop': {
    title: 'Force Shut Down Mac OS?',
    message: [
      'This stops Mac OS immediately, like pulling the power cord. RetroBoot stays on.',
      'Unsaved work in every application is lost. The startup disk may be checked for errors next time Mac OS starts.',
      'Use this only when Mac OS is frozen and a normal shut down has not worked.',
    ],
    confirmLabel: 'Force Shut Down',
    icon: 'stop',
    danger: true,
    acknowledge: 'I understand that unsaved work will be lost.',
  },
};

const DONE_TEXT = {
  start: 'Mac OS is running.',
  restart: 'Mac OS restarted.',
  shutdown: 'Mac OS shut down cleanly. RetroBoot is still on.',
  'force-restart': 'Mac OS was force-restarted.',
  'force-stop': 'Mac OS was stopped. RetroBoot is still on.',
};

const staysOn = () => `RetroBoot stays on, so you can start Mac OS again from this page or ${hw().the}.`;
const turnsOff = () => `When Mac OS has shut down, ${hw().the} turns off. To use it again, press its power button; this page can’t turn it back on.`;

const ON_STATES = ['running', 'starting', 'stopping', 'restarting'];
const guestOn = (status = store.status) => ON_STATES.includes(((status && status.guest) || {}).state);
/** hostd --dev records power requests without acting on them. */
const devHost = (final) => !!((final && final.result && final.result.dev_hook) || (store.status && store.status.host && store.status.host.dev));

/** Shut Down confirmation with the "Also turn off RetroBoot" option. Resolves {powerOff} or null. */
async function shutdownDialog() {
  const c = COPY.shutdown;
  const boxId = uid('alsooff');
  const hintId = uid('alsooffhint');
  const box = h('input', { type: 'checkbox', id: boxId, 'aria-describedby': hintId });
  const hint = h('p', { class: 'dlg-option-hint', id: hintId }, staysOn());
  const content = h('div', { class: 'dlg-option' }, h('label', { class: 'check', for: boxId }, box, h('span', null, 'Also turn off RetroBoot')), hint);
  const value = await dialog({
    title: c.title,
    icon: c.icon,
    message: c.message,
    content,
    buttons: [
      { label: 'Cancel', value: 'cancel' },
      { label: c.confirmLabel, value: 'ok', variant: 'default', autofocus: true },
    ],
    onOpen: (d) => {
      const ok = d.querySelector('button[value="ok"]');
      box.addEventListener('change', () => {
        ok.textContent = box.checked ? 'Shut Down and Turn Off' : c.confirmLabel;
        hint.textContent = box.checked ? turnsOff() : staysOn();
      });
    },
  });
  return value === 'ok' ? { powerOff: box.checked } : null;
}

/** Follow a job that may end with RetroBoot going away: losing the connection counts as finished. */
export function trackUntilGone(job) {
  return new Promise((resolve) => {
    let settled = false;
    let off = () => {};
    const finish = (j) => {
      if (settled) return;
      settled = true;
      off();
      resolve(j);
    };
    off = onApi('connectivity', (ok) => {
      if (!ok) finish({ ...job, state: 'succeeded', lost: true });
    });
    track(job).then(finish);
  });
}

/**
 * Ask for confirmation, start the lifecycle job and follow it.
 * Duplicate requests return the running job (the API is exclusive); force
 * actions may preempt a clean request that is still waiting for Mac OS.
 */
export async function lifecycle(action, { skipConfirm = false } = {}) {
  const force = action.startsWith('force-');
  const blocking = force ? forceOrPowerJob() : lifecycleJob();
  if (blocking) {
    toast(`Already in progress: ${labelFor(blocking)}.`, { level: 'info' });
    return null;
  }
  let body = {};
  if (!skipConfirm && action !== 'start') {
    if (action === 'shutdown') {
      const choice = await shutdownDialog();
      if (!choice) return null;
      if (choice.powerOff) body = { power_off: true };
    } else {
      const c = COPY[action];
      const ok = await confirm({ title: c.title, message: c.message, confirmLabel: c.confirmLabel, icon: c.icon, danger: !!c.danger, acknowledge: c.acknowledge });
      if (!ok) return null;
    }
  }
  const bootId = store.status && store.status.host && store.status.host.boot_id;
  let job;
  try {
    job = asJob(await post(`/api/v1/guest/${action}`, body));
  } catch (err) {
    toast(friendlyMessage(err), { level: 'error', timeout: 8000 });
    refreshStatus().catch(() => {});
    return null;
  }
  pollFast(40000);
  refreshStatus().catch(() => {});
  if (!job) return null;
  if (job.kind && job.kind !== `guest.${action}`) toast(`Another action was already running: ${labelFor(job)}.`, { level: 'info' });
  const final = body.power_off ? await trackUntilGone(job) : await track(job);
  await refreshStatus().catch(() => {});
  refreshSettings().catch(() => {});
  if (superseded(final)) return final; // a force action took over; it reports for itself
  if (final.state === 'succeeded') {
    if (body.power_off && !devHost(final)) {
      emit('host-power', { action: 'poweroff', bootId });
    } else {
      toast(doneMessage(final, DONE_TEXT[action]) + (body.power_off ? ' (Development mode: RetroBoot stays on.)' : ''), { level: 'ok' });
    }
  } else if (!force && (action === 'restart' || action === 'shutdown') && (forceAvailable(final) || guestOn())) {
    await offerForce(action === 'shutdown' ? 'stop' : 'restart', failureText(action === 'restart' ? 'restart' : 'shut down', final));
  } else {
    await dialog({ title: `${labelFor(final)} didn't finish`, icon: 'stop', message: jobOutcome(final), buttons: [{ label: 'OK', value: 'ok', variant: 'default', autofocus: true }] });
  }
  return final;
}

const forceAvailable = (job) => !!(job && job.result && job.result.force_available);
/** hostd cancels a clean request when a force action preempts it. */
const superseded = (job) => !!(job && job.error && job.error.code === 'superseded');

/** Explain a failed clean request; hostd's own message when it says Mac OS didn't respond. */
function failureText(verb, final) {
  const why = jobOutcome(final);
  if (forceAvailable(final)) return why;
  return `Mac OS didn't ${verb}. ${why && why !== 'Done.' ? `RetroBoot reported: ${why}` : ''}`.trim();
}

/** After a clean request fails: explain and offer the explicit force action. */
export async function offerForce(kind, message) {
  const host = kind === 'poweroff' || kind === 'reboot';
  const force = kind === 'stop' ? 'force-stop' : 'force-restart';
  const forceLabel = { stop: 'Force Shut Down…', restart: 'Force Restart…', poweroff: 'Force Turn Off…', reboot: 'Force Restart…' }[kind];
  const choice = await dialog({
    title: "Mac OS Didn't Respond",
    icon: 'caution',
    message: [message || "Mac OS didn't respond in time.", /force/i.test(message || '') ? null : 'You can wait and try again later, or force it. Forcing loses unsaved work.'].filter(Boolean),
    buttons: [
      { label: 'Wait', value: 'wait', variant: 'default', autofocus: true },
      { label: forceLabel, value: 'force' },
    ],
  });
  if (choice !== 'force') return null;
  return host ? hostPower(kind, { force: true }) : lifecycle(force);
}

/** Buttons for the actions appropriate to the current state. */
export function lifecycleAvailability(status = store.status) {
  const g = (status && status.guest) || {};
  const busyJob = lifecycleJob();
  const forceJob = forceOrPowerJob();
  const busy = !!busyJob || ['starting', 'stopping', 'restarting'].includes(g.state);
  const on = ON_STATES.includes(g.state);
  return {
    busy,
    busyLabel: busyJob ? labelFor(busyJob) : null,
    canStart: !busy && (g.state === 'stopped' || g.state === 'failed'),
    canRestart: !busy && g.state === 'running',
    canShutdown: !busy && g.state === 'running',
    // Force actions preempt a clean request that is waiting for Mac OS, so they
    // stay available while Mac OS is on, unless a force or power request runs.
    canForce: (on || g.state === 'failed') && !forceJob,
    // RetroBoot power requests wait for any running Mac OS request to finish.
    canPower: !busyJob,
    on,
  };
}

export function evidenceNote(id) {
  return id ? h('p', { class: 'evidence-id' }, 'Evidence ID: ', h('code', null, id)) : null;
}

// ---- RetroBoot power (POST /api/v1/host/{poweroff,reboot}) -----------------

const POWER = {
  poweroff: {
    title: 'Turn Off RetroBoot?',
    get after() {
      return `Then ${hw().the} turns off. To use it again, press its power button; this page can’t turn it back on.`;
    },
    confirmLabel: 'Turn Off',
    forceTitle: 'Turn Off RetroBoot Anyway?',
    forceLabel: 'Force Turn Off',
  },
  reboot: {
    title: 'Restart RetroBoot?',
    get after() {
      return `Then the whole ${hw().the.replace(/^the /, '')} restarts. This page loses its connection for a minute or two and reconnects by itself.`;
    },
    confirmLabel: 'Restart RetroBoot',
    forceTitle: 'Restart RetroBoot Anyway?',
    forceLabel: 'Force Restart',
  },
};

function powerMessage(action, force) {
  const p = POWER[action];
  const g = (store.status && store.status.guest) || {};
  if (force) {
    return [
      `This stops Mac OS immediately, like pulling the power cord. ${p.after}`,
      'Unsaved work in every application is lost. The startup disk may be checked for errors next time Mac OS starts.',
    ];
  }
  if (!guestOn()) return [`Mac OS is already shut down. ${p.after.replace(/^Then the/, 'The')}`];
  return [
    `Mac OS is asked to shut down first, the same as choosing Shut Down from the Special menu. ${saveNote()}`,
    p.after,
    g.state === 'running' && g.responsive === false ? 'Mac OS isn’t responding right now, so it may not shut down cleanly. If it doesn’t, you’ll be offered to force it.' : null,
  ].filter(Boolean);
}

/** Turn off or restart the whole appliance, after a clean Mac OS shut down (or forced, when asked). */
export async function hostPower(action, { force = false } = {}) {
  const p = POWER[action];
  const blocking = force ? activeJobs('host.')[0] || activeJobs('guest.force-')[0] : lifecycleJob();
  if (blocking) {
    toast(`Already in progress: ${labelFor(blocking)}.`, { level: 'info' });
    return null;
  }
  const ok = await confirm({
    title: force ? p.forceTitle : p.title,
    icon: force ? 'stop' : 'caution',
    message: powerMessage(action, force),
    confirmLabel: force ? p.forceLabel : p.confirmLabel,
    danger: force,
    acknowledge: force ? 'I understand that unsaved work will be lost.' : undefined,
  });
  if (!ok) return null;
  const bootId = store.status && store.status.host && store.status.host.boot_id;
  let job;
  try {
    job = asJob(await post(`/api/v1/host/${action}`, force ? { force: true } : {}));
  } catch (err) {
    toast(friendlyMessage(err), { level: 'error', timeout: 8000 });
    refreshStatus().catch(() => {});
    return null;
  }
  pollFast(60000);
  refreshStatus().catch(() => {});
  if (!job) return null;
  if (job.kind && job.kind !== `host.${action}`) toast(`Another action was already running: ${labelFor(job)}.`, { level: 'info' });
  const final = await trackUntilGone(job);
  if (superseded(final)) return final;
  if (final.state === 'succeeded') {
    if (devHost(final)) {
      await refreshStatus().catch(() => {});
      toast(`Development mode: RetroBoot recorded the request to ${action === 'poweroff' ? 'turn off' : 'restart'} and stays on.`, { level: 'info', timeout: 8000 });
    } else {
      emit('host-power', { action, bootId });
    }
  } else {
    await refreshStatus().catch(() => {});
    if (!force && (forceAvailable(final) || guestOn())) await offerForce(action, failureText('shut down', final));
    else await dialog({ title: `${labelFor(final)} didn't finish`, icon: 'stop', message: jobOutcome(final), buttons: [{ label: 'OK', value: 'ok', variant: 'default', autofocus: true }] });
  }
  return final;
}

// ---- Evidence bundles (POST /api/v1/diagnostics/bundle → job → download)

let lastBundle = null;
export const latestBundle = () => lastBundle;

const BUNDLE_PATH = '/api/v1/diagnostics/bundle/';

/** Same-origin download path for a bundle; anything else hostd might send is ignored. */
export function bundleUrl(result) {
  if (!result) return null;
  const d = result.download;
  if (typeof d === 'string' && d.startsWith(BUNDLE_PATH) && !d.includes('//') && !d.includes('..')) return d;
  const id = result.id || result.bundle || result.bundle_id;
  return id ? `${BUNDLE_PATH}${encodeURIComponent(id)}` : null;
}

export function downloadBundle(result) {
  const href = bundleUrl(result);
  if (!href) return;
  const a = h('a', { href, download: result.filename || '', class: 'visually-hidden' });
  document.body.append(a);
  a.click();
  a.remove();
}

/** Start an evidence bundle job; resolves with the final job. */
export async function collectEvidence({ announce = true } = {}) {
  let job;
  try {
    job = asJob(await post('/api/v1/diagnostics/bundle', {}));
  } catch (err) {
    toast(friendlyMessage(err), { level: 'error' });
    return null;
  }
  if (!job) return null;
  const final = await track(job);
  if (final.state === 'succeeded') {
    lastBundle = { ...(final.result || {}), at: new Date() };
    if (announce) {
      const dl = h('button', { type: 'button', class: 'btn btn-small btn-default', onclick: () => downloadBundle(lastBundle) }, 'Download');
      toast(doneMessage(final, 'Evidence bundle ready.'), { level: 'ok', timeout: 15000, action: dl });
    }
  }
  return final;
}
