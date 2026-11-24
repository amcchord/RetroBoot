// RetroBoot web management: shell, router and session handling.

import { get, setCsrf, setSessionRefresher, on, friendlyMessage, isReachable } from './api.js';
import { h } from './util.js';
import { pixelIcon, wordmark, lineIcon } from './icons.js';
import { store, subscribe, refreshStatus, refreshSettings, startPolling, stopPolling } from './store.js';
import { button, toast, mountToasts, guestStateInfo, led, docWindow, dialog } from './ui.js';
import { menubar } from './menubar.js';
import { loadPlatinum, pixelPref, setPixelPref, autoScale, scaleExact } from './platinum.js';
import { hw, formFactor } from './words.js';
import { lifecycle, lifecycleAvailability, hostPower, collectEvidence } from './actions.js';
import { mountTray } from './jobs.js';
import { mountChangesBar, mountConfirmBanner, discardDraft } from './settings.js';
import * as pair from './pages/pair.js';
import * as overview from './pages/overview.js';
import * as screen from './pages/screen.js';
import * as machine from './pages/machine.js';
import * as display from './pages/display.js';
import * as sound from './pages/sound.js';
import * as storage from './pages/storage.js';
import * as network from './pages/network.js';
import * as diagnostics from './pages/diagnostics.js';
import * as recovery from './pages/recovery.js';
import * as devices from './pages/devices.js';
import * as files from './pages/files.js';
import * as update from './pages/update.js';

const PAGES = { overview, screen, files, machine, display, sound, storage, network, diagnostics, recovery, update, devices };
const NAV = [
  { label: null, items: ['overview', 'screen', 'files'] },
  { label: 'Settings', items: ['machine', 'display', 'sound', 'storage', 'network'] },
  { label: 'Maintenance', items: ['diagnostics', 'recovery', 'update', 'devices'] },
];

const root = document.getElementById('app');
let shell = null;
let current = null;
let firstRoute = true;

// ---- Session ---------------------------------------------------------------

async function refreshSession() {
  const s = await get('/api/v1/session', { quiet401: true });
  store.session = s;
  setCsrf(s.csrf);
  return s;
}

function teardownPage() {
  if (current && current.view && current.view.destroy) current.view.destroy();
  current = null;
}

function signedOut(message) {
  stopPolling();
  discardDraft();
  teardownPage();
  if (shell) shell.destroy();
  shell = null;
  store.status = null;
  store.settings = null;
  store.session = null;
  for (const d of document.querySelectorAll('dialog[open]')) d.close();
  refreshSession()
    .catch(() => null)
    .finally(() => showPairing(message));
}

function showPairing(message) {
  teardownPage();
  if (shell) shell.destroy();
  shell = null;
  document.body.className = 'is-pairing';
  document.title = 'Pair · RetroBoot';
  const view = pair.render({ message, onPaired: paired });
  current = { name: 'pair', view };
  root.replaceChildren(view.el, h('div', { class: 'dock' }, h('div', { class: 'toasts', 'aria-live': 'polite' })));
  mountToasts(root.querySelector('.toasts'));
}

async function paired() {
  try {
    await refreshSession();
  } catch (err) {
    toast(friendlyMessage(err), { level: 'error' });
    return;
  }
  if (!store.session || !store.session.paired) {
    toast('Pairing did not complete. Try again.', { level: 'error' });
    return;
  }
  startApp();
  toast(`Paired as “${store.session.device_name || 'this browser'}”.`, { level: 'ok' });
}

// ---- Shell -----------------------------------------------------------------

function navItem(name) {
  const p = PAGES[name];
  return h('li', null, h('a', { class: 'nav-item', href: `#/${name}`, dataset: { page: name } }, h('span', { class: 'nav-icon', html: pixelIcon(p.meta.icon, 16) }), h('span', null, p.meta.title)));
}

const MOBILE_NAV_LABELS = {
  machine: 'Machine',
  display: 'Display',
  storage: 'Storage',
  update: 'Updates',
  devices: 'Devices',
};

function mobileNavPill(name) {
  const p = PAGES[name];
  return h(
    'a',
    { class: 'mobile-nav-pill', href: `#/${name}`, dataset: { page: name } },
    h('span', { class: 'mobile-nav-icon', html: pixelIcon(p.meta.icon, 16) }),
    h('span', null, MOBILE_NAV_LABELS[name] || p.meta.title),
  );
}

function currentPage() {
  return location.hash.replace(/^#\/?/, '').split(/[/?]/)[0] || 'overview';
}

function aboutRetroBoot() {
  const s = store.status || {};
  const v = (s.update && s.update.current && s.update.current.label) || (s.host && s.host.version) || (s.versions && s.versions.RetroBoot) || 'unknown';
  return dialog({ title: 'RetroBoot', icon: 'mac', message: `Version ${v}.`, buttons: [{ label: 'OK', value: 'ok', variant: 'default', autofocus: true }] });
}

function buildMenus() {
  const pageItems = () =>
    NAV.flatMap((g, i) => [i > 0 ? 'separator' : null, ...g.items.map((n) => ({ label: PAGES[n].meta.title, icon: PAGES[n].meta.icon, href: `#/${n}`, checked: currentPage() === n }))]).filter(Boolean);
  const sizes = () => {
    const pref = pixelPref();
    const auto = autoScale();
    return [
      { label: `Browser Pixel Size: Automatic (${auto}×)`, checked: pref === 'auto' || !scaleExact(Number(pref)), onSelect: () => setPixelPref('auto') },
      'separator',
      // Only sizes this display can draw exactly (a whole number of its pixels per CSS pixel).
      ...[1, 2, 3, 4].map((k) => ({ label: `${k}× (${k} screen pixel${k > 1 ? 's' : ''} per Mac pixel)`, checked: pref === String(k) && scaleExact(k), disabled: !scaleExact(k), onSelect: () => setPixelPref(String(k)) })),
    ];
  };
  const macItems = () => {
    const a = lifecycleAvailability(store.status);
    return [
      { label: 'Start Mac OS', onSelect: () => lifecycle('start'), disabled: !a.canStart },
      { label: 'Restart', onSelect: () => lifecycle('restart'), disabled: !a.canRestart },
      { label: 'Shut Down…', onSelect: () => lifecycle('shutdown'), disabled: !a.canShutdown },
      'separator',
      { label: 'Force Restart…', href: '#/recovery' },
      { label: 'Force Shut Down…', href: '#/recovery' },
      'separator',
      { label: 'Collect Evidence', onSelect: () => collectEvidence() },
    ];
  };
  const rbItems = () => {
    const a = lifecycleAvailability(store.status);
    return [
      { label: 'About RetroBoot…', onSelect: aboutRetroBoot },
      { label: 'Software Update…', href: '#/update' },
      'separator',
      { label: 'Paired Devices', icon: 'devices', href: '#/devices' },
      { label: 'Diagnostics', icon: 'diagnostics', href: '#/diagnostics' },
      'separator',
      { label: 'Restart RetroBoot…', onSelect: () => hostPower('reboot'), disabled: !a.canPower },
      { label: 'Turn Off RetroBoot…', onSelect: () => hostPower('poweroff'), disabled: !a.canPower },
    ];
  };
  return [
    { id: 'retroboot', title: h('span', { class: 'mb-logo', html: pixelIcon('mac', 16) }), label: 'RetroBoot', items: rbItems },
    { id: 'macos', title: 'Mac OS', items: macItems },
    { id: 'go', title: 'Go', items: pageItems },
    { id: 'view', title: 'View', items: sizes },
  ];
}

function buildShell() {
  document.body.className = 'is-app';
  const statePill = h('a', { class: 'mb-status state-pill', href: '#/overview', 'aria-live': 'polite' }, led('off'), h('span', { class: 'state-pill-text' }, 'Checking…'));
  const hostLabel = h('span', { class: 'host-label visually-hidden' });
  const appTitle = h('span', { class: 'mb-app-title' });
  const menus = buildMenus();
  menus.push({ id: 'app', title: appTitle, align: 'right', className: 'mb-app', label: 'Sections', items: menus[2].items });
  const header = menubar(menus, { right: [statePill, hostLabel] });
  // The status sits left of the application menu.
  const right = header.querySelector('.mb-right');
  right.prepend(statePill, hostLabel);
  const offline = h('div', { class: 'offline-banner', hidden: true, role: 'alert' });
  const confirmEl = h('div', { class: 'confirm-host', hidden: true });
  const whoami = h('p', { class: 'nav-foot' });
  const navList = h(
    'div',
    { class: 'nav-lists' },
    ...NAV.map((g) => h('div', { class: 'nav-group' }, g.label ? h('h2', { class: 'nav-heading' }, g.label) : null, h('ul', null, ...g.items.map(navItem)))),
    whoami,
  );
  const nav = h('nav', { id: 'nav', class: 'sidenav', 'aria-label': 'Sections' }, docWindow({ title: 'RetroBoot', icon: 'mac', className: 'nav-window' }, navList));
  const mobileNav = h('nav', { class: 'mobile-nav', 'aria-label': 'Sections' }, ...NAV.flatMap((g) => g.items.map(mobileNavPill)));
  const main = h('main', { id: 'main', class: 'main', tabindex: '-1' });
  const toasts = h('div', { class: 'toasts' });
  const tray = h('section', { class: 'tray', hidden: true, 'aria-label': 'Activity' });
  const bar = h('section', { class: 'changes-bar', hidden: true, 'aria-label': 'Unsaved and pending settings' });
  const dock = h('div', { class: 'dock' }, toasts, tray, bar);

  root.replaceChildren(h('a', { class: 'skip-link', href: '#main', onclick: (e) => (e.preventDefault(), main.focus()) }, 'Skip to content'), h('div', { class: 'top' }, header, offline, confirmEl), h('div', { class: 'layout' }, mobileNav, nav, main), dock);
  mountToasts(toasts);
  mountTray(tray);
  const cleanups = [mountChangesBar(bar), mountConfirmBanner(confirmEl), () => header.destroy()];

  function setMenu() {
    /* The section list lives in the Go and application menus on every width. */
  }

  const updateHeader = () => {
    const s = store.status;
    const info = isReachable() ? guestStateInfo(s) : { word: 'Not connected', tone: 'off' };
    statePill.replaceChildren(led(info.tone), h('span', { class: 'state-pill-text' }, info.word));
    statePill.setAttribute('aria-label', isReachable() ? `Mac OS: ${info.word}` : 'Not connected to RetroBoot');
    statePill.className = `mb-status state-pill tone-${info.tone}`;
    const hn = (s && s.host && s.host.hostname) || (store.session && store.session.hostname);
    hostLabel.textContent = hn ? `${hn}.local` : '';
    whoami.replaceChildren(
      ...[
        store.session && store.session.device_name ? h('span', null, 'Signed in as ', h('strong', null, store.session.device_name)) : null,
        s && s.host && s.host.version ? h('span', { class: 'muted' }, `RetroBoot ${s.host.version}`) : null,
      ].filter(Boolean),
    );
  };
  cleanups.push(subscribe('status', updateHeader));
  updateHeader();

  let offlineTimer = null;
  const setOffline = (reachable) => {
    document.body.classList.toggle('is-offline', !reachable);
    updateHeader();
    if (!reachable) {
      offline.hidden = false;
      offline.replaceChildren(
        h('span', { class: 'bar-icon', html: pixelIcon('caution', 16) }),
        h('p', null, h('strong', null, 'Can’t reach RetroBoot. '), 'It may be restarting, turned off, or on a different network. Showing the last information received; retrying automatically.'),
        button('Retry Now', { className: 'btn-small', icon: 'refresh', onClick: () => refreshStatus().catch(() => {}) }),
      );
      clearInterval(offlineTimer);
      offlineTimer = setInterval(() => refreshStatus().catch(() => {}), 4000);
    } else if (!offline.hidden) {
      offline.hidden = true;
      clearInterval(offlineTimer);
      toast('Reconnected to RetroBoot.', { level: 'ok', timeout: 3000 });
      refreshSettings().catch(() => {});
    }
  };
  cleanups.push(() => clearInterval(offlineTimer));
  shell = { main, nav, mobileNav, appTitle, setMenu, setOffline, destroy: () => cleanups.forEach((f) => f()) };
}

/** Wrap a page view in its document window: the page's h1 becomes the window title. */
function pageWindow(name, view) {
  const page = PAGES[name];
  const h1 = view.el.querySelector('.page-head h1') || h('h1', { tabindex: '-1', class: 'page-title' }, page.meta.title);
  h1.classList.add('page-title');
  const win = docWindow(
    {
      title: h1,
      icon: page.meta.icon,
      className: `page-window page-window-${name}`,
      boxes: name === 'overview' ? ['collapse'] : view.zoom ? ['close', 'zoom', 'collapse'] : ['close', 'collapse'],
      onBox: (kind) => {
        if (kind === 'close') location.hash = '#/overview';
        if (kind === 'collapse') win.classList.toggle('is-collapsed');
        if (kind === 'zoom' && view.zoom) view.zoom();
      },
    },
    view.el,
  );
  return win;
}

function route() {
  if (!shell) return;
  const name = location.hash.replace(/^#\/?/, '').split(/[/?]/)[0] || 'overview';
  const page = PAGES[name];
  if (!page) {
    history.replaceState(null, '', '#/overview');
    return route();
  }
  if (current && current.name === name) return;
  teardownPage();
  const view = page.render({ signedOut });
  current = { name, view };
  shell.main.replaceChildren(pageWindow(name, view));
  shell.appTitle.replaceChildren(h('span', { class: 'mb-app-icon', html: pixelIcon(page.meta.icon, 16) }), h('span', null, page.meta.title));
  for (const a of shell.nav.querySelectorAll('.nav-item')) {
    if (a.dataset.page === name) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  for (const p of shell.mobileNav.querySelectorAll('.mobile-nav-pill')) {
    if (p.dataset.page === name) {
      p.setAttribute('aria-current', 'page');
      p.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    } else {
      p.removeAttribute('aria-current');
    }
  }
  document.title = `${page.meta.title} · RetroBoot`;
  if (!firstRoute) {
    window.scrollTo(0, 0);
    const h1 = shell.main.querySelector('h1');
    if (h1) h1.focus({ preventScroll: true });
  }
  firstRoute = false;
}

function printKey(s) {
  const pdf = s && s.printed_pdf;
  if (!pdf || !pdf.path) return null;
  return pdf.seq != null ? `${pdf.seq}:${pdf.path}:${pdf.at || pdf.timestamp || ''}` : `${pdf.path}:${pdf.at || pdf.timestamp || ''}`;
}

function notifyPrintedPdf(pdf) {
  if (!pdf || !pdf.path) return;
  const name = pdf.name || pdf.path.split('/').pop() || 'document.pdf';
  let closeToast = null;
  const previewBtn = button('Preview', {
    variant: 'default',
    className: 'btn-small',
    onClick: () => {
      if (closeToast) closeToast();
      const dir = pdf.path.includes('/') ? pdf.path.slice(0, pdf.path.lastIndexOf('/')) : '';
      const q = [];
      if (dir) q.push(`path=${encodeURIComponent(dir)}`);
      q.push(`preview=${encodeURIComponent(pdf.path)}`);
      location.hash = `#/files?${q.join('&')}`;
    },
  });
  closeToast = toast(`Printed “${name}” to your shared folder.`, { level: 'ok', timeout: 10000, action: previewBtn });
}

async function startApp() {
  buildShell();
  if (!location.hash || location.hash === '#/pair') history.replaceState(null, '', '#/overview');
  // Pages say "the laptop" or "the computer" (words.js): wait briefly for the first status.
  await Promise.race([refreshStatus().catch(() => {}), new Promise((r) => setTimeout(r, 1500))]);
  if (!shell) return;   // signed out meanwhile
  let seenForm = formFactor();
  let seenPrint = printKey(store.status);
  route();
  const offForm = subscribe('status', (s) => {
    if (formFactor() !== seenForm) {
      seenForm = formFactor();
      rerender();
    }
    const pk = printKey(s);
    if (pk && pk !== seenPrint) {
      seenPrint = pk;
      notifyPrintedPdf(s.printed_pdf);
    }
  });
  const destroy = shell.destroy;
  shell.destroy = () => (offForm(), destroy());
  refreshSettings().catch(() => {});
  startPolling();
}

/** Render the current page again (its fixed wording depends on the form factor). */
function rerender() {
  if (!current) return;
  teardownPage();
  route();
}

// ---- RetroBoot turned off or restarting ----------------------------------------

let powerTimer = null;
const stopPowerWatch = () => {
  clearTimeout(powerTimer);
  powerTimer = null;
};

/** Shown after Turn Off / Restart RetroBoot: the page has nothing to manage until it is back. */
function showPowerScreen({ action, bootId, heading, text: customText }) {
  stopPolling();
  discardDraft();
  teardownPage();
  if (shell) shell.destroy();
  shell = null;
  for (const d of document.querySelectorAll('dialog[open]')) d.close();
  stopPowerWatch();
  const off = action === 'poweroff';
  document.body.className = 'is-pairing';
  document.title = `${off ? 'Turned Off' : 'Restarting'} · RetroBoot`;
  const statusLine = h('p', { class: 'muted small', 'aria-live': 'polite' }, off ? 'You can close this page.' : 'Waiting for RetroBoot to come back…');
  const text = customText || (off
    ? [`Mac OS has shut down and ${hw().the} is turning off.`, `To use it again, press ${hw().its} power button. Mac OS starts by itself, and this page works again once RetroBoot is on.`]
    : [`Mac OS has shut down and ${hw().the} is restarting.`, 'This page reconnects by itself when RetroBoot is back, usually within a minute or two.']);
  const el = h(
    'div',
    { class: 'pair-screen' },
    h(
      'main',
      { class: 'pair-main', id: 'main', tabindex: '-1' },
      docWindow(
        { title: h('h1', { id: 'power-title' }, 'RetroBoot'), className: 'pair-win' },
        h('div', { class: 'boot-error power-state' }, h('span', { html: pixelIcon('mac', 32) }), h('div', null, h('h2', null, heading || (off ? 'RetroBoot is turning off' : 'RetroBoot is restarting')), ...text.map((t) => h('p', null, t)), statusLine)),
        h('div', { class: 'pair-buttons' }, button(off ? 'Reconnect' : 'Try Now', { variant: off ? null : 'default', icon: 'refresh', onClick: () => (off ? boot() : probe()) })),
      ),
    ),
  );
  root.replaceChildren(el, h('div', { class: 'dock' }, h('div', { class: 'toasts', 'aria-live': 'polite' })));
  mountToasts(root.querySelector('.toasts'));
  el.querySelector('main').focus({ preventScroll: true });
  if (off) return;

  // Restart: reconnect once the old instance has gone away or a new boot answers.
  let sawOffline = false;
  const started = Date.now();
  async function probe() {
    stopPowerWatch();
    try {
      const s = await get('/api/v1/status', { quiet401: true, timeout: 4000 });
      const newBoot = bootId && s && s.host && s.host.boot_id && s.host.boot_id !== bootId;
      if (sawOffline || newBoot) {
        boot();
        return;
      }
    } catch (err) {
      if (err.status === 401) {
        boot();
        return;
      }
      sawOffline = true;
    }
    if (Date.now() - started > 5 * 60000) statusLine.textContent = `RetroBoot hasn’t come back yet. Check ${hw().its} screen, then try again.`;
    powerTimer = setTimeout(probe, 3000);
  }
  powerTimer = setTimeout(probe, 3000);
}

// ---- Boot ------------------------------------------------------------------

function bootError(err) {
  document.body.className = 'is-pairing';
  document.title = 'RetroBoot';
  let retryIn = 5;
  const count = h('span', null, String(retryIn));
  const el = h(
    'div',
    { class: 'pair-screen' },
    h(
      'main',
      { class: 'pair-main', id: 'main' },
      docWindow(
        { title: h('h1', { id: 'boot-title' }, 'RetroBoot'), className: 'pair-win' },
        h('div', { class: 'boot-error' }, h('span', { html: pixelIcon('caution', 32) }), h('div', null, h('h2', null, 'Can’t reach RetroBoot'), h('p', null, err && err.status === 0 ? 'Check that RetroBoot is turned on and that this device is on the same network. If RetroBoot is restarting, this page connects again by itself.' : friendlyMessage(err)), h('p', { class: 'muted small', 'aria-live': 'polite' }, 'Trying again in ', count, ' s.'))),
        h('div', { class: 'pair-buttons' }, button('Try Again', { variant: 'default', icon: 'refresh', onClick: () => boot() })),
      ),
    ),
  );
  root.replaceChildren(el);
  const t = setInterval(() => {
    retryIn -= 1;
    count.textContent = String(Math.max(0, retryIn));
    if (retryIn <= 0) {
      clearInterval(t);
      boot();
    }
  }, 1000);
}

let listenersInstalled = false;
async function boot() {
  stopPowerWatch();
  if (!listenersInstalled) {
    listenersInstalled = true;
    subscribe('host-power', showPowerScreen);
    setSessionRefresher(refreshSession);
    on('unauthorized', () => {
      if (store.session && store.session.paired) signedOut(`This browser was signed out (the pairing was removed or expired). Pair again with a code from ${hw().the}.`);
    });
    on('connectivity', (ok) => shell && shell.setOffline(ok));
    window.addEventListener('hashchange', route);
  }
  let s;
  try {
    s = await refreshSession();
  } catch (err) {
    bootError(err);
    return;
  }
  if (!s.paired) showPairing();
  else startApp();
}

void loadPlatinum();
boot();
