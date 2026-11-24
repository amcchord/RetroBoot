import { h, swap, Scope } from '../util.js';
import { win, kv, pageHeader, loading } from '../ui.js';
import { store, subscribe } from '../store.js';
import { settingsPanels, startupFamily } from '../settings.js';
import { formatValue, machineFamily, FAMILY_TEXT } from '../schema.js';

export const meta = { title: 'Machine & Startup', icon: 'machine' };

function summary() {
  const m = store.settings;
  const s = store.status;
  if (!m) return loading();
  const row = (key, label) => {
    const def = m.defs.get(key);
    return def ? [label || def.label, formatValue(def, m.applied[key])] : null;
  };
  const g = (s && s.guest) || {};
  // A 68k Mac (Quadra 800) has its own memory setting and always the 68040.
  const fam = g.family || machineFamily(m.defs, m.applied['machine.active']);
  const appliedMachine = m.defs.get('machine.active')?.choices?.find((c) => c.value === m.applied['machine.active']);
  const osx = (appliedMachine?.guest_os || '').startsWith('macosx-');
  const osxRam = m.applied['machine.osx_ram_mb'];
  const ram = osx && osxRam != null && osxRam !== 'auto' ? osxRam : m.applied['machine.ram_mb'];
  const memory = ['Memory', `${ram} MB`];
  const cpuLabel = g.cpu || g.cpu_label || appliedMachine?.cpu_label;
  const rows = fam === 'quadra800'
    ? [['Computer', FAMILY_TEXT.quadra800.machine], ['Processor', cpuLabel || FAMILY_TEXT.quadra800.cpu], row('machine.quadra_ram_mb')]
    : osx
      ? [memory, cpuLabel ? ['Processor', cpuLabel] : row('machine.cpu')].filter(Boolean)
      : [memory, cpuLabel ? ['Processor', cpuLabel] : row('machine.cpu'), row('machine.boot_speed')].filter(Boolean);
  return [
    kv([
      ['Running now', g.machine_name || g.system || formatValue(m.defs.get('machine.active'), m.applied['machine.active'])],
      g.system && g.system !== g.machine_name ? ['System', g.system] : null,
      ...rows,
    ]),
    h('p', { class: 'muted small' }, 'These are the values Mac OS is using now. Changes to the machine take effect when Mac OS restarts. Disks, imports and checkpoints are on ', h('a', { href: '#/storage' }, 'Storage & Media'), '.'),
  ];
}

export function render() {
  const scope = new Scope();
  const sumEl = h('div');
  const panels = scope.add(
    settingsPanels('machine', {
      extras: {
        // The startup Mac's processor when it is a 68k Mac: fixed, so a line instead of a setting.
        Machine: () => {
          const el = h('div');
          const update = () => swap(el, startupFamily() === 'quadra800' ? h('p', { class: 'muted small' }, `Processor: ${FAMILY_TEXT.quadra800.cpu} (a 68k Mac is a ${FAMILY_TEXT.quadra800.machine}).`) : null);
          update();
          return { el, update };
        },
      },
    }),
  );
  const el = h(
    'div',
    { class: 'page' },
    pageHeader('machine', 'Machine & Startup', 'The emulated Mac that runs Mac OS (a Power Mac G4, or a Quadra 800 for 68k Macs), and what happens when RetroBoot powers on.'),
    h('div', { class: 'two-col' }, h('div', { class: 'col' }, panels.el), h('div', { class: 'col' }, win({ title: 'In Use Now', icon: 'mac' }, sumEl))),
  );
  const update = () => swap(sumEl, ...[].concat(summary()));
  update();
  scope.add(subscribe('settings', update));
  scope.add(subscribe('status', update));
  return { el, destroy: () => scope.destroy() };
}
