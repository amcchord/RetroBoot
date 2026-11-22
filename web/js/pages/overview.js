import { h, swap, Scope, fmtShortTime, formatDuration, formatMs, relTime } from '../util.js';
import { win, button, kv, notice, pageHeader, guestStateInfo, led, copyButton, loading } from '../ui.js';
import { pixelIcon } from '../icons.js';
import { store, subscribe } from '../store.js';
import { liveScreen } from '../screen.js';
import { lifecycle, lifecycleAvailability, collectEvidence, evidenceNote, hostPower } from '../actions.js';
import { hw } from '../words.js';

export const meta = { title: 'Overview', icon: 'mac' };

/** Boot timeline: proportional bar (decorative) plus an ordered list. */
export function timeline(status) {
  const boot = status && status.boot;
  // Only the first occurrence of each step belongs to the startup (hostd may record
  // a later "shown on the display" each time the display returns from a panel).
  const seen = new Set();
  const events = ((boot && (boot.events || boot.timeline)) || []).filter((e) => {
    const k = e.id || e.label;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
  if (!events.length) return h('p', { class: 'muted' }, 'No startup timing recorded yet.');
  const max = Math.max(1, ...events.map((e) => e.t_ms || 0));
  const bar = h('div', { class: 'tl-bar', 'aria-hidden': 'true' });
  for (const e of events) {
    const m = h('span', { class: 'tl-mark', title: `${e.label}: ${formatMs(e.t_ms)}` });
    m.style.setProperty('--at', String(((e.t_ms || 0) / max) * 100));
    bar.append(m);
  }
  const list = h(
    'ol',
    { class: 'tl-list' },
    ...events.map((e, i) =>
      h('li', { class: i === events.length - 1 ? 'is-last' : null }, h('span', { class: 'tl-time' }, i === 0 && !e.t_ms ? '0 s' : `+${formatMs(e.t_ms)}`), h('span', { class: 'tl-label' }, e.label || e.id)),
    ),
  );
  const started = boot.started_at ? h('p', { class: 'muted tl-started' }, `Started ${fmtShortTime(boot.started_at)} (${relTime(boot.started_at)}).`) : null;
  return h('div', { class: 'timeline' }, started, bar, list);
}

function stateBlock(status) {
  const info = guestStateInfo(status);
  const g = (status && status.guest) || {};
  const hung = g.state === 'running' && g.responsive === false;
  const dur = (t) => {
    const secs = (Date.now() - Date.parse(t)) / 1000;
    return secs < 60 ? 'just now' : formatDuration(secs);
  };
  const since = hung && g.unresponsive_since
    ? `Not responding since ${fmtShortTime(g.unresponsive_since)} (${dur(g.unresponsive_since)})`
    : g.since
      ? `Since ${fmtShortTime(g.since)} (${dur(g.since)})`
      : null;
  const parts = [
    h(
      'div',
      { class: `state-hero tone-${info.tone}` },
      h('span', { class: 'state-hero-icon', html: pixelIcon('mac', 64) }),
      h('div', null, h('p', { class: 'state-word' }, led(info.tone), h('span', null, info.word)), h('p', { class: 'state-detail' }, info.detail), since && g.state !== 'failed' ? h('p', { class: 'muted' }, since) : null),
    ),
  ];
  if (g.state === 'running' && g.responsive === false) {
    parts.push(
      notice({
        level: 'error',
        title: 'This page still works',
        message: "Collect evidence first if you want to report the problem, then try Restart. If a normal restart doesn't work, use Force Restart on the Recovery page.",
        actions: [h('a', { class: 'btn btn-small', href: '#/recovery', 'data-focus-key': 'hung-recovery' }, 'Recovery Options')],
      }),
    );
  }
  if (g.state === 'failed') {
    parts.push(
      notice({
        level: 'error',
        title: 'Mac OS stopped unexpectedly',
        message: `${g.detail || 'RetroBoot tried to start Mac OS again and it did not succeed.'} Collect evidence if you want to report it, then try starting Mac OS again.`,
      }),
      evidenceNote(g.evidence_id),
    );
  }
  if (g.last_exit && g.last_exit.forced && g.state === 'stopped') parts.push(h('p', { class: 'muted' }, 'Mac OS was stopped without shutting down cleanly. The startup disk may be checked when it starts.'));
  return parts;
}

function quickActions(status) {
  const a = lifecycleAvailability(status);
  const g = (status && status.guest) || {};
  const busyLine = a.busy ? h('p', { class: 'busy-line', role: 'status' }, h('span', { class: 'spinner spinner-small', 'aria-hidden': 'true' }), `${a.busyLabel || guestStateInfo(status).word}…`) : null;
  const evidence = button('Collect Evidence', { icon: 'box', onClick: () => collectEvidence(), focusKey: 'qa-evidence' });
  if (g.state === 'stopped' || g.state === 'failed') {
    // Mac OS is shut down and RetroBoot is still on: starting it again is the main thing to offer.
    return [
      busyLine,
      h(
        'div',
        { class: 'start-cta' },
        button('Start Mac OS', { variant: 'default', icon: 'power', className: 'btn-large', onClick: () => lifecycle('start'), disabled: !a.canStart, focusKey: 'qa-start' }),
        h('p', { class: 'muted small' }, g.state === 'failed' ? `RetroBoot tries to start Mac OS again on ${hw().the}.` : `Mac OS starts on ${hw().the}, the same as when RetroBoot powers on.`),
      ),
      h('div', { class: 'button-stack' }, button('Turn Off RetroBoot…', { icon: 'power', onClick: () => hostPower('poweroff'), disabled: !a.canPower, focusKey: 'qa-poweroff' }), evidence),
      h('p', { class: 'muted small' }, `Turn Off RetroBoot turns off ${hw().the}; press its power button to turn it on again. `, h('a', { href: '#/recovery' }, 'Restart RetroBoot and other options'), '.'),
    ];
  }
  return [
    busyLine,
    h(
      'div',
      { class: 'button-stack' },
      button('Restart Mac OS', { icon: 'restart', onClick: () => lifecycle('restart'), disabled: !a.canRestart, focusKey: 'qa-restart' }),
      button('Shut Down Mac OS…', { icon: 'power', onClick: () => lifecycle('shutdown'), disabled: !a.canShutdown, focusKey: 'qa-shutdown' }),
      evidence,
    ),
    h('p', { class: 'muted small' }, 'Restart and Shut Down ask Mac OS to finish cleanly, like its Special menu. ', h('a', { href: '#/recovery' }, 'Force options, Turn Off RetroBoot and recovery'), '.'),
  ];
}

function minutes(m) {
  if (m == null) return null;
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`;
}

// status.power (hostd laptop.py): battery and AC adapter, null on desktops.
function batteryText(power) {
  const b = power && power.battery;
  if (!b || b.percent == null) return null;
  const pct = `${b.percent} %`;
  if (b.status === 'Charging') return `${pct}, charging${b.time_to_full_min != null ? ` (${minutes(b.time_to_full_min)} to full)` : ''}`;
  if (b.status === 'Discharging' && !power.ac) return `${pct}, on battery${b.time_to_empty_min != null ? ` (about ${minutes(b.time_to_empty_min)} left)` : ''}`;
  if (power.ac) return `${pct}, on the power adapter`;
  return pct;
}

// Mirrors of the laptop display (presenter outputs other than the primary).
function otherDisplays(display) {
  const outs = (display.outputs || []).filter((o) => o.role !== 'primary');
  if (!outs.length) return null;
  return outs.map((o) => `${o.mode[0]} × ${o.mode[1]}${o.scale > 1 ? ` (${o.scale}×)` : ''}${o.on === false ? ', off' : ''}`).join('; ');
}

function hostBlock(status) {
  const host = (status && status.host) || {};
  const display = (status && status.display) || {};
  const addrs = (host.addresses || []).map((a) => (typeof a === 'string' ? { address: a } : a)).filter((a) => a.family !== 'ipv6' || !/^fe80/i.test(a.address));
  const urls = host.urls || [];
  const mode = display.mode ? `${display.mode[0]} × ${display.mode[1]}${display.mode[2] ? `, ${display.mode[2]} Hz` : ''}` : null;
  const guestSize = display.guest_size ? `${display.guest_size[0]} × ${display.guest_size[1]}` : null;
  return kv(
    [
    ['Name', host.hostname ? `${host.hostname}.local` : host.name],
    [
      'Web address',
      urls.length
        ? h('ul', { class: 'plain-list' }, ...urls.map((u) => h('li', { class: 'with-copy' }, h('code', null, u), copyButton(u, 'Copy', { what: 'address', ariaLabel: `Copy ${u}`, compact: true }))))
        : null,
    ],
    ['IP addresses', addrs.length ? h('ul', { class: 'plain-list' }, ...addrs.map((a) => h('li', null, h('code', null, a.address), a.interface ? h('span', { class: 'muted' }, ` ${a.interface}`) : null))) : 'None'],
    ['RetroBoot version', host.version],
    ['Up for', host.uptime_s != null ? formatDuration(host.uptime_s) : null],
    [hw().Display, mode ? `${mode}${display.scale > 1 ? ` (scale ${display.scale}×)` : ''}` : null],
    otherDisplays(display) ? ['Other displays', otherDisplays(display)] : null,
    (display.failed_outputs || []).length ? ['Not working', `${display.failed_outputs.join(', ')} (could not be turned on at startup; Restart RetroBoot to try again)`] : null,
    ['Mac OS screen', guestSize],
    batteryText(status && status.power) ? ['Battery', batteryText(status && status.power)] : null,
    ],
    'kv-stacked',
  );
}

export function render() {
  const scope = new Scope();
  const noticesEl = h('div', { class: 'notices' });
  const stateEl = h('div', { class: 'state-block' }, loading());
  const actionsEl = h('div', { class: 'quick-actions' });
  const tlEl = h('div');
  const hostEl = h('div');
  const screen = scope.add(
    liveScreen({
      what: 'guest',
      interval: 2000,
      placeholder: () => {
        const s = store.status;
        if (!s) return 'Waiting for RetroBoot…';
        const g = s.guest || {};
        if (g.state === 'stopped') return "Mac OS is shut down. Start it to see its screen here.";
        if (g.state === 'failed') return "Mac OS isn't running.";
        if (g.state === 'starting' && !g.content) return 'Mac OS is starting. Its screen appears once it draws the startup screen.';
        return null;
      },
      stale: () => {
        const g = (store.status && store.status.guest) || {};
        return g.state === 'running' && g.responsive === false ? 'Last picture before Mac OS stopped responding' : null;
      },
    }),
  );

  const el = h(
    'div',
    { class: 'page page-overview' },
    pageHeader('mac', 'Overview', null),
    noticesEl,
    h(
      'div',
      { class: 'overview-grid' },
      h('div', { class: 'col' }, win({ title: 'Mac OS', icon: 'mac' }, stateEl, actionsEl), win({ title: 'Screen', icon: 'display' }, screen.el, h('p', { class: 'screen-open' }, h('a', { class: 'btn btn-small', href: '#/screen' }, 'Use Mac OS from This Browser')))),
      h('div', { class: 'col' }, win({ title: 'Startup Timeline', icon: 'diagnostics' }, tlEl), win({ title: 'This RetroBoot', icon: 'network' }, hostEl)),
    ),
  );

  const sigs = {};
  const patch = (key, target, sig, build) => {
    if (sigs[key] === sig) return;
    sigs[key] = sig;
    swap(target, ...[].concat(build()));
  };

  const update = () => {
    const s = store.status;
    if (!s) return;
    const g = s.guest || {};
    // The Mac OS window already explains an unresponsive guest; skip hostd's duplicate notice.
    const notices = (s.notices || []).filter((n) => !(n.id === 'guest-unresponsive' && g.state === 'running' && g.responsive === false));
    patch('notices', noticesEl, JSON.stringify(notices), () => notices.map((n) => notice({ level: n.level || 'info', message: n.message })));
    patch('state', stateEl, JSON.stringify([g.state, g.responsive, g.content, g.detail, g.since, g.evidence_id, g.machine_name, g.unresponsive_since, g.last_exit, Math.floor(Date.now() / 60000)]), () => stateBlock(s));
    patch('actions', actionsEl, JSON.stringify([g.state, g.responsive, lifecycleAvailability(s)]), () => quickActions(s).filter(Boolean));
    patch('tl', tlEl, JSON.stringify([s.boot, Math.floor(Date.now() / 60000)]), () => timeline(s));
    patch('host', hostEl, JSON.stringify([s.host && { ...s.host, uptime_s: Math.floor((s.host.uptime_s || 0) / 60) }, s.display]), () => hostBlock(s));
    screen.poke();
  };
  update();
  scope.add(subscribe('status', update));
  scope.add(subscribe('jobs', update));
  return { el, destroy: () => scope.destroy() };
}

