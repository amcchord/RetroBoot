import { h, swap, Scope } from '../util.js';
import { win, kv, pageHeader, loading, button, confirm, dialog, toast } from '../ui.js';
import { store, subscribe, refreshStatus, refreshSettings, guestRunning } from '../store.js';
import { get, post, asJob, friendlyMessage } from '../api.js';
import { track, doneMessage } from '../jobs.js';
import { settingsPanels } from '../settings.js';
import { liveScreen } from '../screen.js';
import { hw } from '../words.js';
import { hostPower } from '../actions.js';

export const meta = { title: 'Display & Input', icon: 'display' };

const TURNS = { 0: 'Standard', 90: '90° clockwise', 180: '180°', 270: '90° counterclockwise' };

function rotationText(s) {
  const d = s.display || {};
  const r = s.rotation || {};
  if (d.user_rotation == null) return null;
  let how = r.setting === 'auto' ? 'automatic' : 'fixed';
  if (r.setting === 'auto' && r.sensor && r.sensor.available) how = r.lock ? 'automatic, locked' : 'automatic, follows the tilt sensor';
  const pending = d.user_rotation_pending != null ? ` (turning to ${TURNS[d.user_rotation_pending]})` : '';
  return `${TURNS[d.user_rotation] || `${d.user_rotation}°`}${pending} — ${how}${d.orientation ? `, ${d.orientation}` : ''}`;
}

function touchText(s) {
  const devs = (s.input && s.input.touch_devices) || [];
  if (!devs.length) return null;
  return devs.map((t) => `${t.kind === 'pen' ? 'Pen' : 'Touchscreen'}${t.output ? ` on ${t.output}` : ''}`).join(', ');
}

const size = (a) => (a ? `${a[0]} × ${a[1]}` : null);

/** 3D acceleration (ATI Radeon): what Mac OS uses now, what it needs, and installing ATI's software. */
function graphicsInfo(onInstall) {
  const g = (store.status || {}).graphics;
  if (!g) return loading();
  if (g.family === 'quadra800') return h('p', { class: 'muted small' }, '3D acceleration needs a PowerPC Mac. The startup Mac is a 68k Mac (Quadra 800), which uses its own graphics card.');
  const running = g.running ? (g.running === 'ati-radeon' ? '3D acceleration (ATI Radeon)' : 'Standard') : null;
  const rows = [
    ['Mac OS uses', running || 'Not running'],
    ['3D acceleration', g.requested === 'ati-radeon' ? (g.effective === 'ati-radeon' ? 'On' : 'On, but not available') : 'Off'],
    g.ati_screen && g.requested === 'ati-radeon' ? ['Mac screen with it', `${size(g.ati_screen)} (starts at ${size(g.startup_screen)})`] : null,
    g.host_gl && g.host_gl.checked ? ['Graphics chip', g.host_gl.ok ? g.host_gl.renderer || 'Ready' : 'Not usable for 3D'] : null,
    ['ATI software in Mac OS', g.guest_software === 'present' ? (g.helper === false ? 'Installed (without RetroBoot Display)' : 'Installed') : g.guest_software === 'absent' ? 'Not installed' : 'Not checked yet'],
  ];
  const parts = [kv(rows)];
  if (!g.available && g.unavailable_message) parts.push(h('p', { class: 'muted small' }, g.unavailable_message));
  if (g.capable && (g.guest_software === 'absent' || g.helper === false)) {
    parts.push(h('div', { class: 'button-row' }, button('Install ATI Software…', { onClick: onInstall })));
  }
  return h('div', null, ...parts);
}

function current() {
  const s = store.status || {};
  const d = s.display || null;
  if (!d) return loading();
  const mode = d.mode ? `${d.mode[0]} × ${d.mode[1]}${d.mode[2] ? `, ${d.mode[2]} Hz` : ''}` : '—';
  return kv([
    [hw().Display, mode],
    ['Mac OS screen', d.guest_size ? `${d.guest_size[0]} × ${d.guest_size[1]}` : 'Not running'],
    ['Pixel scale', d.scale ? `${d.scale}×` : null],
    rotationText(s) ? ['Rotation', rotationText(s)] : null,
    touchText(s) ? ['Touch', touchText(s)] : null,
    d.connector || d.driver ? ['Output', [d.connector, d.driver].filter(Boolean).join(' · ')] : null,
  ]);
}

export function render() {
  const scope = new Scope();
  const curEl = h('div');
  const gfxEl = h('div');
  const piEl = h('div');
  let piSelected = null;
  let piBusy = false;
  const panels = scope.add(settingsPanels('display'));
  const outer = scope.add(liveScreen({ what: 'composed', interval: 3000, placeholder: () => null }));
  const el = h(
    'div',
    { class: 'page' },
    pageHeader('display', 'Display & Input', `How Mac OS appears on the ${hw().screen}, and how the keyboard and pointer behave.`),
    h(
      'div',
      { class: 'two-col' },
      h('div', { class: 'col' }, panels.el, piEl),
      h(
        'div',
        { class: 'col' },
        win({ title: hw().Screen, icon: 'display' }, outer.el, h('p', { class: 'muted small' }, `What the ${hw().display} shows right now. Watch it here when you change the display mode.`)),
        win({ title: 'Current Display', icon: 'display' }, curEl),
        win({ title: '3D Acceleration', icon: 'display' }, gfxEl),
      ),
    ),
  );
  let installing = false;
  async function installATI() {
    if (installing) return;
    installing = true;
    try {
      await installFlow();
    } finally {
      installing = false;
    }
  }

  async function installFlow() {
    let cds = [];
    try {
      const lib = await get('/api/v1/media');
      cds = (lib.media || []).filter((m) => m.can_insert);
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error' });
      return;
    }
    if (!cds.length) {
      await dialog({
        title: 'Add the Mac OS 9.2.1 CD first',
        icon: 'note',
        message: ["RetroBoot copies ATI's graphics software from your Mac OS 9.2.1 CD. Add its disc image (.iso or .toast) under Storage & Media, then choose Install ATI Software… again."],
        buttons: [{ label: 'OK', value: 'ok', variant: 'default', autofocus: true }],
      });
      return;
    }
    const pick = h('select', { 'aria-label': 'Mac OS 9.2.1 CD image' }, ...cds.map((c, i) => h('option', { value: String(i) }, c.label || c.name)));
    const running = guestRunning();
    const ok = await confirm({
      title: 'Install ATI graphics software?',
      icon: 'note',
      message: [
        "RetroBoot copies ATI Driver Update, ATI Radeon 3D Accelerator, ATI Resource Manager and its own RetroBoot Display from your Mac OS 9.2.1 CD into Mac OS's Extensions folder, and turns on Apple's OpenGL renderer for ATI.",
        running ? 'Mac OS shuts down while RetroBoot installs, then starts again.' : 'Mac OS is not running, so nothing else changes.',
      ],
      content: h('label', { class: 'field' }, h('span', null, 'CD image: '), pick),
      confirmLabel: 'Install',
    });
    if (!ok) return;
    const cd = cds[Number(pick.value)];
    try {
      const job = asJob(await post('/api/v1/graphics/install', { cd: cd.id, restart: running }));
      if (!job) return;
      const final = await track(job, { label: 'Installing ATI graphics software' });
      if (final.state === 'succeeded') toast(doneMessage(final, "ATI's graphics software is installed."), { level: 'ok', timeout: 8000 });
      else toast(doneMessage(final, "ATI's graphics software wasn't installed."), { level: 'error', timeout: 8000 });
    } catch (err) {
      toast(friendlyMessage(err), { level: 'error', timeout: 8000 });
    }
    await Promise.all([refreshStatus().catch(() => {}), refreshSettings().catch(() => {})]);
  }

  function piDisplay() {
    const d = (store.status || {}).pi_display;
    if (!d) return null;
    if (!d.available) return win({ title: 'Raspberry Pi Display', icon: 'display' },
      h('p', null, d.message || 'Display presets are unavailable on this startup card.'));
    const profiles = d.profiles || [];
    const selected = piSelected || d.trial || d.pending || d.profile || 'auto';
    const pick = h('select', { 'aria-label': 'Raspberry Pi display', disabled: !!d.trial || piBusy,
      onChange: () => { piSelected = pick.value; swap(piEl, piDisplay()); } },
      ...profiles.map((p) => h('option', { value: p.id, selected: p.id === selected }, p.label)));
    const note = (profiles.find((p) => p.id === selected) || {}).note;
    async function save(keep) {
      if (piBusy) return;
      piBusy = true;
      swap(piEl, piDisplay());
      try {
        await post(keep == null ? '/api/v1/pi-display' : '/api/v1/pi-display/confirm',
          keep == null ? { profile: pick.value, revision: d.revision } : { keep, revision: d.revision });
        piSelected = null;
        await refreshStatus();
        toast(keep == null ? 'Display saved for the next computer start.' : keep ? 'Display kept.' : 'The previous display will be used at the next start.', { level: 'ok' });
      } catch (err) {
        toast(friendlyMessage(err), { level: 'error' });
        await refreshStatus().catch(() => {});
      } finally {
        piBusy = false;
        swap(piEl, piDisplay());
      }
    }
    const controls = d.trial
      ? [h('p', null, 'This display is being tried for one start. Keep it to use it at every start.'),
        h('div', { class: 'button-row' },
          button('Use Previous Next Start', { disabled: piBusy, onClick: () => save(false) }),
          button('Keep Display', { disabled: piBusy, onClick: () => save(true) }))]
      : [h('div', { class: 'button-row' },
          button('Try Next Start', { disabled: piBusy || selected === d.profile || d.custom, onClick: () => save(null) }),
          button('Restart Computer…', { disabled: piBusy || !d.restart_required, onClick: () => hostPower('reboot') }))];
    return win({ title: 'Raspberry Pi Display', icon: 'display' },
      h('label', { class: 'field' }, h('span', null, 'Display: '), pick),
      note ? h('p', { class: 'muted small' }, note) : null,
      d.custom ? h('p', null, 'The card has custom display settings. Restore auto.txt before using this picker.') : null,
      ...controls,
      h('p', { class: 'muted small' }, 'Restart the computer to try a display. Until you click Keep Display after that start, a further power cycle restores the previous preset.'),
      h('p', { class: 'muted small' }, 'If startup stops before RetroBoot opens, move the SD card to another computer and copy auto.txt from retroboot/displays to retroboot/display.txt.'));
  }

  const update = () => {
    swap(piEl, piDisplay());
    swap(curEl, current());
    swap(gfxEl, graphicsInfo(installATI));
  };
  update();
  scope.add(subscribe('status', update));
  return { el, destroy: () => scope.destroy() };
}
