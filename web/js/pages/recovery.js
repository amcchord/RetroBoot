import { h, swap, Scope, fmtDateTime, sameValue, plural } from '../util.js';
import { win, button, pageHeader, guestStateInfo, led, loading } from '../ui.js';
import { pixelIcon } from '../icons.js';
import { store, subscribe } from '../store.js';
import { lifecycle, lifecycleAvailability, collectEvidence, hostPower } from '../actions.js';
import { restoreKnownGood } from '../settings.js';
import { formatValue } from '../schema.js';
import { hw } from '../words.js';

export const meta = { title: 'Recovery', icon: 'recovery' };

function actionRow(title, text, btn) {
  return h('div', { class: 'action-row' }, h('div', { class: 'action-text' }, h('h3', null, title), h('p', null, text)), h('div', { class: 'action-btn' }, btn));
}

function cleanBlock(status) {
  const a = lifecycleAvailability(status);
  const info = guestStateInfo(status);
  const g = (status && status.guest) || {};
  const healthy = g.state === 'running' && g.responsive !== false;
  const secondary = healthy ? g.machine_name || g.system : info.detail;
  const rows = [h('p', { class: 'state-line' }, led(info.tone), h('strong', null, info.word), healthy && secondary ? h('span', { class: 'muted' }, `· ${secondary}`) : null)];
  if (!healthy && secondary) rows.push(h('p', { class: 'muted state-sub' }, secondary));
  if (a.busy) rows.push(h('p', { class: 'busy-line', role: 'status' }, h('span', { class: 'spinner spinner-small', 'aria-hidden': 'true' }), `${a.busyLabel || info.word}…`));
  if (g.state === 'stopped' || g.state === 'failed') {
    rows.push(actionRow('Start Mac OS', `Starts Mac OS on ${hw().the}, the same as at power on.`, button('Start Mac OS', { variant: 'default', icon: 'power', disabled: !a.canStart, focusKey: 'rc-start', onClick: () => lifecycle('start') })));
  }
  rows.push(
    actionRow('Restart', 'Asks Mac OS to restart, as if you chose Restart from the Special menu. Applications can ask to save documents first.', button('Restart Mac OS', { icon: 'restart', disabled: !a.canRestart, focusKey: 'rc-restart', onClick: () => lifecycle('restart') })),
    actionRow('Shut Down', 'Asks Mac OS to shut down, as if you chose Shut Down from the Special menu. RetroBoot stays on unless you also choose to turn it off.', button('Shut Down Mac OS…', { icon: 'power', disabled: !a.canShutdown, focusKey: 'rc-shutdown', onClick: () => lifecycle('shutdown') })),
  );
  return rows;
}

function forceBlock(status) {
  const a = lifecycleAvailability(status);
  const g = (status && status.guest) || {};
  const hung = g.state === 'running' && g.responsive === false;
  return [
    h(
      'div',
      { class: 'danger-intro' },
      h('span', { html: pixelIcon('stop', 32) }),
      h('p', null, h('strong', null, 'Unsaved work is lost. '), 'These stop Mac OS immediately, like pulling the power cord. Use them only when Mac OS is frozen and a clean restart or shut down hasn’t worked. ', hung ? h('strong', null, 'Mac OS is not responding right now.') : null),
    ),
    actionRow('Force Restart', 'Stops Mac OS at once and starts it again. The disk may be checked at startup.', button('Force Restart…', { variant: 'danger', disabled: !a.canForce, focusKey: 'rc-force-restart', onClick: () => lifecycle('force-restart') })),
    actionRow('Force Shut Down', 'Stops Mac OS at once. RetroBoot stays on.', button('Force Shut Down…', { variant: 'danger', disabled: !a.canForce || g.state === 'stopped', focusKey: 'rc-force-stop', onClick: () => lifecycle('force-stop') })),
    a.busy && a.canForce ? h('p', { class: 'muted small' }, 'A request is still waiting for Mac OS. If Mac OS is frozen, you can force it now instead of waiting.') : null,
  ];
}

function powerBlock(status) {
  const a = lifecycleAvailability(status);
  const on = a.on;
  return [
    h('p', { class: 'win-intro' }, on ? `Both shut down Mac OS cleanly first, as if you chose Shut Down from the Special menu. Applications can ask to save open documents; answer those questions on ${hw().the}.` : 'Mac OS is shut down, so RetroBoot turns off or restarts right away.'),
    actionRow('Restart RetroBoot', `Restarts the whole ${hw().Title.toLowerCase()}. This page loses its connection for a minute or two and reconnects by itself.`, button('Restart RetroBoot…', { icon: 'restart', disabled: !a.canPower, focusKey: 'rc-host-reboot', onClick: () => hostPower('reboot') })),
    actionRow('Turn Off RetroBoot', `Turns ${hw().the} off. To use it again, press its power button; this page can’t turn it back on.`, button('Turn Off RetroBoot…', { icon: 'power', disabled: !a.canPower, focusKey: 'rc-host-poweroff', onClick: () => hostPower('poweroff') })),
    !a.canPower ? h('p', { class: 'muted small' }, 'Available when the current action finishes.') : null,
  ];
}

function settingsBlock() {
  const m = store.settings;
  if (!m) return loading();
  const kg = m.knownGood;
  if (!kg || !kg.values) return h('p', { class: 'muted' }, 'RetroBoot hasn’t recorded a known working configuration yet. It records one after Mac OS starts successfully.');
  const diff = Object.keys(kg.values).filter((k) => m.defs.has(k) && !sameValue(kg.values[k], m.desired[k]));
  return [
    h('p', null, `The last configuration that started Mac OS successfully is revision ${kg.revision}${kg.at ? `, recorded ${fmtDateTime(kg.at)}` : ''}.`),
    diff.length
      ? h('ul', { class: 'change-list' }, ...diff.map((k) => h('li', null, h('strong', null, m.defs.get(k).label), `: now ${formatValue(m.defs.get(k), m.desired[k])}, known working ${formatValue(m.defs.get(k), kg.values[k])}`)))
      : h('p', { class: 'muted' }, 'Current settings match it.'),
    h('div', { class: 'row-actions' }, button(diff.length ? `Restore ${plural(diff.length, 'Setting')}…` : 'Restore Known Working Settings', { disabled: !diff.length, focusKey: 'rc-kg', onClick: restoreKnownGood })),
  ];
}

const CHORD_TEXT = { 'ctrl+alt+delete': 'Control-Alt-Delete', 'ctrl+alt+backspace': 'Control-Alt-Backspace', 'ctrl+alt+escape': 'Control-Alt-Escape' };

function laptopBlock() {
  const m = store.settings;
  const chord = m && (m.applied['input.recovery_chord'] || m.desired['input.recovery_chord']);
  const def = m && m.defs.get('input.recovery_chord');
  const keys = chord ? (def ? formatValue(def, chord) : CHORD_TEXT[chord] || chord) : 'the recovery keys';
  return [
    h('p', null, 'Press ', h('kbd', null, keys), ` on ${hw().the} to open RetroBoot Recovery, even if Mac OS is frozen. It works without a network or this page.`),
    h('p', { class: 'muted' }, 'From there you can restart Mac OS, restore a checkpoint, collect evidence, and show this page’s address and a new pairing code.'),
  ];
}

export function render() {
  const scope = new Scope();
  const cleanEl = h('div');
  const forceEl = h('div');
  const powerEl = h('div');
  const settingsEl = h('div');
  const laptopEl = h('div');
  const el = h(
    'div',
    { class: 'page' },
    pageHeader('recovery', 'Recovery', 'Restart or shut down Mac OS, force it when it is frozen, turn off or restart RetroBoot, or go back to settings that worked.'),
    h(
      'div',
      { class: 'two-col' },
      h('div', { class: 'col' }, win({ title: 'Restart or Shut Down', icon: 'mac' }, cleanEl), win({ title: 'Force Mac OS to Stop', icon: 'recovery', className: 'win-danger' }, forceEl)),
      h(
        'div',
        { class: 'col' },
        win({ title: 'Turn Off or Restart RetroBoot', icon: 'mac' }, powerEl),
        win({ title: 'Known Working Settings', icon: 'machine' }, settingsEl),
        win({ title: `Recovery on the ${hw().Title}`, icon: 'devices' }, laptopEl),
        win({ title: 'Before You Force', icon: 'diagnostics' }, h('p', null, 'Collecting evidence first records logs and screenshots of the problem.'), h('div', { class: 'row-actions' }, button('Collect Evidence', { icon: 'box', onClick: () => collectEvidence() }))),
      ),
    ),
  );
  const sigs = {};
  const patch = (k, target, sig, build) => {
    if (sigs[k] === sig) return;
    sigs[k] = sig;
    swap(target, ...[].concat(build()));
  };
  const update = () => {
    const s = store.status;
    const g = (s && s.guest) || {};
    const a = lifecycleAvailability(s);
    patch('clean', cleanEl, JSON.stringify([g.state, g.responsive, g.detail, a]), () => cleanBlock(s));
    patch('force', forceEl, JSON.stringify([g.state, g.responsive, a]), () => forceBlock(s));
    patch('power', powerEl, JSON.stringify([a.on, a.canPower]), () => powerBlock(s).filter(Boolean));
    const m = store.settings;
    patch('settings', settingsEl, JSON.stringify(m ? [m.knownGood, m.desired] : null), settingsBlock);
    patch('laptop', laptopEl, JSON.stringify(m ? [m.applied['input.recovery_chord']] : null), laptopBlock);
  };
  update();
  scope.add(subscribe('status', update));
  scope.add(subscribe('settings', update));
  scope.add(subscribe('jobs', update));
  return { el, destroy: () => scope.destroy() };
}
