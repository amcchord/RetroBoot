// Screen: the Mac OS screen, live, with its keyboard and mouse ("KVM").
// What the computer's own screen shows (Mac OS, or the classic panel and the
// checkerboard), streamed over GET /api/v1/screen/stream (js/kvm/stream.js) and
// driven with this browser's keyboard, mouse or touch screen (js/kvm/input.js).

import { h, Scope } from '../util.js';
import { button, dialog, led } from '../ui.js';
import { store, subscribe } from '../store.js';
import { onPlatinum } from '../platinum.js';
import { ScreenStream, WebAudioSink } from '../kvm/stream.js';
import { InputController } from '../kvm/input.js';
import { hw } from '../words.js';
import { lifecycle } from '../actions.js';

export const meta = { title: 'Screen', icon: 'screen' };

const SIZE_KEY = 'rb.screenSize';
const TRACKPAD_KEY = 'rb.screenTrackpad';
const SWAP_KEY = 'rb.screenAltIsCommand';
const GAMEBAR_KEY = 'rb.screenGameBar';
const SIZES = [
  ['fit', 'Fit in Window'],
  ['1', 'Actual Size'],
  ['2', 'Double Size'],
  ['3', 'Triple Size'],
];
const KEYS = [
  ['cmd-q', '⌘Q', 'Command-Q: quit the application'],
  ['cmd-w', '⌘W', 'Command-W: close the window'],
  ['cmd-tab', '⌘Tab', 'Command-Tab: switch to the next application'],
  ['cmd-period', '⌘.', 'Command-Period: cancel'],
  ['esc', 'Esc', 'Escape'],
  ['force-quit', 'Force Quit…', 'Command-Option-Escape: force the application to quit'],
];
const LATCHES = [
  ['shift', 'Shift'],
  ['control', 'Control'],
  ['option', 'Option'],
  ['command', '⌘'],
];

function pref(key, dflt) {
  try {
    return localStorage.getItem(key) ?? dflt;
  } catch {
    return dflt;
  }
}

function setPref(key, v) {
  try {
    if (v == null) localStorage.removeItem(key);
    else localStorage.setItem(key, String(v));
  } catch {
    /* private mode */
  }
}

const touchDevice = () => window.matchMedia('(pointer: coarse)').matches || navigator.maxTouchPoints > 0;

function popup(label, options, value, onChange) {
  const select = h('select', { 'aria-label': label, onchange: (e) => onChange(e.target.value) }, ...options.map(([v, text]) => h('option', { value: v, selected: v === value }, text)));
  return { el: h('label', { class: 'tb-field' }, h('span', { class: 'tb-label' }, `${label}:`), h('span', { class: 'select-wrap' }, select)), select };
}

export function render() {
  const scope = new Scope();
  let size = pref(SIZE_KEY, 'fit');
  if (!SIZES.some(([v]) => v === size)) size = 'fit';
  let mode = null; // view | control (from hostd)
  let viewers = [];
  let me = null;
  let typing = null;
  let streamState = 'connecting';
  let overlay = null; // {title, text, actions}
  let privateNow = false;
  let displayDown = false;
  let extraNote = null;
  let pinchScale = null;
  let audioOn = false;
  let gameBarOpen = pref(GAMEBAR_KEY, touchDevice() ? 'true' : 'false') === 'true';

  const title = h('h1', { tabindex: '-1', class: 'page-title' }, 'Screen');
  const canvas = h('canvas', { class: 'kvm-canvas', width: 640, height: 480, tabindex: '0', role: 'img', 'aria-label': 'Mac OS screen. Click it to use your keyboard and mouse with Mac OS.' });
  const sink = h('textarea', { class: 'kvm-sink', 'aria-label': 'Keyboard for Mac OS', autocapitalize: 'off', autocomplete: 'off', autocorrect: 'off', spellcheck: 'false' });
  const overlayEl = h('div', { class: 'screen-overlay', hidden: true, role: 'status' });
  // data-screen-drop: files dropped on the picture are copied to the shared volume (filecopy.js).
  const bezel = h('div', { class: 'kvm-bezel', 'data-screen-drop': '' }, canvas);
  const stage = h('div', { class: 'screen-stage' }, bezel, overlayEl, sink);
  const statusText = h('span', { class: 'kvm-status-text snap-text' }, 'Connecting…');
  const statusLed = h('span', { class: 'kvm-led' }, led('busy'));
  const extra = h('span', { class: 'kvm-status-extra' });
  const statusbar = h('div', { class: 'screen-statusbar', 'aria-live': 'polite' }, statusLed, statusText, extra);
  const audioSink = new WebAudioSink();

  const stream = new ScreenStream({
    canvas,
    source: 'composed',
    onState: (state, detail) => {
      streamState = state;
      if (state === 'refused') overlay = refusedOverlay(detail);
      else if (state === 'insecure') overlay = insecureOverlay();
      else if (state === 'live') overlay = null;
      update();
    },
    onMessage: (msg) => onMessage(msg),
    onFrame: (frame, at) => input.frameDrawn(frame, at),
    onAudio: (frame) => audioSink.feed(frame),
    onResize: () => layout(),
  });
  const swapPref = pref(SWAP_KEY, null);
  if (swapPref === 'true' || swapPref === 'false') stream.swap = swapPref === 'true';
  const input = new InputController({
    canvas,
    stream,
    keyboardSink: sink,
    canControl: () => mode === 'control' && stream.open && !privateNow,
    canPan: () => bezel.classList.contains('is-scrolling') || (pinchScale != null && pinchScale > 1.02),
    onPinch: (ev) => handlePinch(ev),
    onLatch: () => updateLatches(),
  });
  input.trackpad = pref(TRACKPAD_KEY, 'false') === 'true';

  // ---- toolbar -----------------------------------------------------------------
  const sizePopup = popup('Size', SIZES, size, (v) => {
    size = v;
    pinchScale = null;
    setPref(SIZE_KEY, v);
    layout();
  });
  const sourcePopup = popup('Show', [['composed', `What the ${hw().display} shows`], ['guest', 'Mac OS only']], 'composed', (v) => stream.setSource(v));
  const pasteBtn = button('Paste Text…', { className: 'btn-small', onClick: () => pasteDialog() });
  const kbdBtn = button('Keyboard', { className: 'btn-small', onClick: () => sink.focus({ preventScroll: true }) });
  const soundBtn = button('Sound: Off', {
    className: 'btn-small',
    title: 'Play Mac OS sound in this browser',
    onClick: () => toggleAudio(),
  });
  soundBtn.setAttribute('aria-pressed', 'false');
  async function toggleAudio() {
    audioOn = !audioOn;
    soundBtn.setAttribute('aria-pressed', String(audioOn));
    soundBtn.classList.toggle('is-pressed', audioOn);
    const lbl = soundBtn.querySelector('.btn-label');
    if (lbl) lbl.textContent = audioOn ? 'Sound: On' : 'Sound: Off';
    if (audioOn) await audioSink.enable();
    else audioSink.disable();
    stream.setAudio(audioOn);
  }
  const gameBtn = button('Game Keys', {
    className: 'btn-small',
    title: 'Show or hide touch arrow and game keys',
    onClick: () => {
      gameBarOpen = !gameBarOpen;
      setPref(GAMEBAR_KEY, gameBarOpen);
      gameBtn.setAttribute('aria-pressed', String(gameBarOpen));
      gameBtn.classList.toggle('is-pressed', gameBarOpen);
      update();
      requestAnimationFrame(layout);
    },
  });
  gameBtn.setAttribute('aria-pressed', String(gameBarOpen));
  gameBtn.classList.toggle('is-pressed', gameBarOpen);
  const trackpadBox = h('input', { type: 'checkbox', checked: input.trackpad, onchange: (e) => {
    input.trackpad = e.target.checked;
    setPref(TRACKPAD_KEY, e.target.checked);
  } });
  const trackpad = h('label', { class: 'check tb-check' }, trackpadBox, h('span', null, 'Trackpad'));
  const altBox = h('input', { type: 'checkbox', onchange: (e) => {
    setPref(SWAP_KEY, e.target.checked);
    stream.setSwap(e.target.checked);
  } });
  const altCmd = h('label', { class: 'check tb-check', title: 'Use the Alt key next to the space bar as the Mac’s Command key' }, altBox, h('span', null, 'Alt is Command'));
  const fsBtn = button('Full Screen', { className: 'btn-small', icon: 'expand', onClick: () => toggleFullscreen() });
  const fsAvailable = !!(document.fullscreenEnabled || document.webkitFullscreenEnabled);
  const toolbar = h(
    'div',
    { class: 'screen-toolbar', role: 'toolbar', 'aria-label': 'Screen' },
    sizePopup.el,
    sourcePopup.el,
    h('span', { class: 'tb-gap' }),
    soundBtn,
    pasteBtn,
    touchDevice() ? kbdBtn : null,
    gameBtn,
    touchDevice() ? trackpad : null,
    ['mac', 'ios'].includes(stream.platform) ? null : altCmd,
    fsAvailable ? fsBtn : null,
  );

  // ---- key bar -------------------------------------------------------------------
  const combo = (name) => {
    stream.send({ t: 'combo', name });
    canvas.focus({ preventScroll: true });
  };
  const latchBtns = new Map();
  const keybar = h(
    'div',
    { class: 'screen-keybar', role: 'toolbar', 'aria-label': 'Keys the browser keeps for itself' },
    ...KEYS.map(([name, label, tip]) => button(label, { className: 'btn-small key-btn', title: tip, ariaLabel: tip, onClick: () => combo(name) })),
    h('span', { class: 'tb-gap' }),
    button('Shut Down…', { className: 'btn-small key-btn', title: 'The Mac’s Power key: Mac OS asks whether to shut down', onClick: () => combo('power') }),
    button('Recovery…', { className: 'btn-small key-btn', title: `Opens the RetroBoot window on ${hw().the} (Mac OS keeps running)`, onClick: () => combo('recovery') }),
    h('span', { class: 'tb-gap' }),
    ...LATCHES.map(([name, label]) => {
      const b = button(label, { className: 'btn-small latch-btn', title: `${name === 'command' ? 'Command' : label}: held down for the next key or click`, onClick: () => {
        input.toggleLatch(name);
        canvas.focus({ preventScroll: true });
      } });
      b.setAttribute('aria-pressed', 'false');
      latchBtns.set(name, b);
      return b;
    }),
  );

  function gameKeyBtn(code, label, titleText, extraClass = '') {
    const b = h('button', {
      type: 'button',
      class: `btn btn-small game-key${extraClass ? ` ${extraClass}` : ''}`,
      title: titleText,
      'aria-label': titleText,
    }, h('span', { class: 'btn-label' }, label));
    const release = (e) => {
      if (!b.classList.contains('is-pressed')) return;
      b.classList.remove('is-pressed');
      if (e && e.pointerId != null) {
        try { b.releasePointerCapture(e.pointerId); } catch {}
      }
      input.key(code, false, false);
    };
    b.addEventListener('pointerdown', (e) => {
      if (b.disabled || !input.canControl()) return;
      e.preventDefault();
      b.classList.add('is-pressed');
      if (e.pointerId != null) {
        try { b.setPointerCapture(e.pointerId); } catch {}
      }
      input.key(code, true, false);
    });
    b.addEventListener('pointerup', release);
    b.addEventListener('pointercancel', release);
    b.addEventListener('lostpointercapture', release);
    b.addEventListener('keydown', (e) => {
      if (e.key === ' ' || e.key === 'Enter') {
        e.preventDefault();
        if (!e.repeat && input.canControl()) input.key(code, true, false);
      }
    });
    b.addEventListener('keyup', (e) => {
      if (e.key === ' ' || e.key === 'Enter') {
        e.preventDefault();
        input.key(code, false, false);
      }
    });
    return b;
  }

  const gamebar = h(
    'div',
    { class: 'screen-gamebar', role: 'toolbar', 'aria-label': 'Game keys', hidden: !gameBarOpen },
    h(
      'div',
      { class: 'game-dpad', role: 'group', 'aria-label': 'Arrow keys' },
      gameKeyBtn('ArrowUp', '▲', 'Up Arrow', 'game-key-up'),
      gameKeyBtn('ArrowLeft', '◀', 'Left Arrow', 'game-key-left'),
      gameKeyBtn('ArrowDown', '▼', 'Down Arrow', 'game-key-down'),
      gameKeyBtn('ArrowRight', '▶', 'Right Arrow', 'game-key-right'),
    ),
    h(
      'div',
      { class: 'game-actions', role: 'group', 'aria-label': 'Action keys' },
      gameKeyBtn('Space', 'Space', 'Space bar'),
      gameKeyBtn('Enter', 'Return', 'Return key'),
      gameKeyBtn('Escape', 'Esc', 'Escape key'),
    ),
  );

  function updateLatches() {
    for (const [name, b] of latchBtns) {
      const on = input.latched.has(name);
      b.setAttribute('aria-pressed', String(on));
      b.classList.toggle('is-pressed', on);
    }
  }

  const shell = h('div', { class: 'screen-shell' }, toolbar, stage, keybar, gamebar, statusbar);
  const el = h('div', { class: 'page page-screen' }, h('header', { class: 'page-head' }, title), shell);

  // ---- messages from hostd --------------------------------------------------------
  function onMessage(msg) {
    switch (msg.t) {
      case 'hello':
        mode = msg.mode;
        me = msg.viewer;
        altBox.checked = !!msg.swap;
        if (msg.source) sourcePopup.select.value = stream.source = msg.source;
        break;
      case 'mode':
        mode = msg.mode;
        if (mode !== 'control') input.releaseAll();
        break;
      case 'surface':
        requestAnimationFrame(layout);
        break;
      case 'viewers':
        viewers = msg.list || [];
        break;
      case 'source':
        sourcePopup.select.value = stream.source = msg.source;
        break;
      case 'options':
        altBox.checked = !!msg.swap;
        break;
      case 'state':
        if ('private' in msg) privateNow = !!msg.private;
        if ('display' in msg) displayDown = msg.display === 'unavailable';
        if (privateNow) input.releaseAll();
        break;
      case 'typing':
        typing = msg.state === 'running' ? msg : null;
        if (msg.state === 'error') dialog({ title: 'Can’t type that', icon: 'caution', message: msg.message, buttons: [{ label: 'OK', value: 'ok', variant: 'default', autofocus: true }] });
        else if (msg.state === 'done' && msg.skipped) {
          extraNote = `${msg.skipped} character${msg.skipped === 1 ? '' : 's'} could not be typed in Mac OS.`;
          setTimeout(() => ((extraNote = null), update()), 6000);
        }
        break;
      case 'notice':
        extraNote = msg.message;
        setTimeout(() => ((extraNote = null), update()), 5000);
        break;
      default:
        break;
    }
    update();
  }

  function refusedOverlay(detail) {
    const code = detail && detail.code;
    if (code === 'remote-screen-off') {
      return { title: 'The remote screen is off', text: 'Turn it on under Network → Remote Screen to see and use Mac OS from this browser.', actions: [h('a', { class: 'btn btn-small', href: '#/network' }, 'Open Network Settings')] };
    }
    return { title: 'The screen isn’t available', text: (detail && detail.message) || 'RetroBoot refused the connection.', actions: [button('Try Again', { className: 'btn-small', icon: 'refresh', onClick: () => stream.start() })] };
  }

  function insecureOverlay() {
    const urls = (store.status && store.status.host && store.status.host.urls) || [];
    return { title: 'A secure connection is needed', text: `The live screen works only over https. Open ${urls[1] || urls[0] || 'RetroBoot’s https address'} instead.`, actions: [] };
  }

  // ---- status bar and overlay -------------------------------------------------------
  function update() {
    const g = (store.status && store.status.guest) || {};
    const name = g.machine_name || (stream.hello && stream.hello.machine) || 'Mac OS';
    const host = (stream.hello && stream.hello.hostname) || (store.status && store.status.host && store.status.host.hostname);
    title.textContent = host ? `${name} on ${host}` : name;
    const s = stream.surface;
    let tone = 'busy';
    let text = 'Connecting…';
    if (streamState === 'live') {
      tone = 'ok';
      const parts = ['Live'];
      if (s && s.w) parts.push(`${s.w} × ${s.h}`);
      const fps = stream.fps();
      if (fps) parts.push(`${Math.round(fps)} fps`);
      const lat = input.latencyStats();
      if (lat && lat.n >= 3) parts.push(`${Math.round(lat.median)} ms`);
      if (mode === 'view') parts.push('View only');
      text = parts.join(' · ');
    } else if (streamState === 'reconnecting') {
      text = 'Reconnecting…';
    } else if (streamState === 'paused') {
      tone = 'off';
      text = 'Paused while this page is in the background.';
    } else if (streamState === 'refused' || streamState === 'insecure') {
      tone = 'bad';
      text = 'Not connected.';
    }
    statusLed.replaceChildren(led(tone));
    statusText.textContent = text;
    const others = viewers.filter((v) => v.id !== me);
    const bits = [];
    if (typing) {
      bits.push(h('span', null, `Typing ${typing.done} of ${typing.total}…`), button('Stop', { className: 'btn-small', onClick: () => stream.send({ t: 'cancel' }) }));
    } else if (extraNote) {
      bits.push(h('span', null, extraNote));
    }
    if (others.length) bits.push(h('span', { class: 'kvm-viewers' }, `Also here: ${others.map((v) => `${v.name}${v.mode === 'view' ? ' (watching)' : ''}`).join(', ')}`));
    extra.replaceChildren(...bits);
    // Overlay over the picture.
    let ov = overlay;
    if (!ov && streamState === 'live') {
      if (privateNow) {
        ov = { title: 'The RetroBoot panel is showing a pairing code', text: `Pairing codes appear only on ${hw().its} own screen.`, actions: mode === 'control' ? [button('Close That Panel Screen', { className: 'btn-small', onClick: () => stream.send({ t: 'panel-back' }) })] : [] };
      } else if (displayDown) {
        ov = { title: 'Reconnecting to the display…', text: 'The picture comes back by itself.', actions: [], soft: true };
      } else if (s && s.view === 'black' && g.state === 'stopped') {
        ov = { title: 'Mac OS is shut down', text: 'Start it to use it from here.', actions: mode === 'control' ? [button('Start Mac OS', { className: 'btn-small', variant: 'default', onClick: () => lifecycle('start') })] : [], soft: true };
      } else if (s && s.view === 'stopped') {
        ov = { title: 'Mac OS isn’t running', text: g.state === 'starting' ? 'Mac OS is starting.' : 'Choose “What the screen shows” to see RetroBoot’s own screen.', actions: [], soft: true };
      } else if (s && s.view === 'hold') {
        ov = { title: '', text: 'Showing the last picture.', actions: [], soft: true, corner: true };
      }
    } else if (!ov && streamState === 'reconnecting') {
      ov = { title: 'Reconnecting…', text: 'RetroBoot may be restarting. The picture comes back by itself.', actions: [], soft: true };
    } else if (!ov && streamState === 'connecting') {
      ov = { title: 'Connecting…', text: '', actions: [], soft: true };
    }
    overlayEl.hidden = !ov;
    overlayEl.className = ['screen-overlay', ov && ov.soft && 'is-soft', ov && ov.corner && 'is-corner'].filter(Boolean).join(' ');
    if (ov) overlayEl.replaceChildren(h('div', { class: 'screen-overlay-box' }, ov.title ? h('strong', null, ov.title) : null, ov.text ? h('p', null, ov.text) : null, ov.actions && ov.actions.length ? h('div', { class: 'screen-overlay-actions' }, ...ov.actions) : null));
    const control = mode === 'control' && streamState === 'live' && !privateNow;
    canvas.classList.toggle('is-control', control);
    keybar.hidden = mode === 'view';
    gamebar.hidden = mode === 'view' || !gameBarOpen;
    soundBtn.disabled = streamState !== 'live';
    pasteBtn.disabled = !control;
    kbdBtn.disabled = !control;
    gameBtn.disabled = !control;
    sourcePopup.select.disabled = mode !== 'control'; // one picture for every browser; changing it is control
    for (const b of keybar.querySelectorAll('button')) b.disabled = !control;
    for (const b of gamebar.querySelectorAll('button')) b.disabled = !control;
  }

  // ---- size --------------------------------------------------------------------------
  function ratio() {
    // Viewport pixels per Mac pixel (the body is zoomed by the browser pixel size).
    const r = stage.getBoundingClientRect();
    return stage.clientWidth ? r.width / stage.clientWidth : 1;
  }

  function currentScale(aw, ah, pic) {
    if (pinchScale != null) return pinchScale;
    if (size === 'fit') {
      const f = Math.min(aw / pic.w, ah / pic.h);
      return f >= 1 ? Math.floor(f) : Math.max(0.1, f);
    }
    return Number(size) || 1;
  }

  function handlePinch(ev) {
    const pic = { w: canvas.width, h: canvas.height };
    if (!pic.w || !pic.h) return;
    const aw = Math.max(100, stage.clientWidth - 24);
    const ah = Math.max(100, stage.clientHeight - 24);
    if (ev.phase === 'start') {
      if (pinchScale == null) pinchScale = currentScale(aw, ah, pic);
      return;
    }
    if (ev.phase !== 'move') return;
    const k = ratio() || 1;
    const rectBefore = canvas.getBoundingClientRect();
    const dx = ev.dx || 0;
    const dy = ev.dy || 0;
    const u = rectBefore.width ? ((ev.midX - dx) - rectBefore.left) / rectBefore.width : 0.5;
    const v = rectBefore.height ? ((ev.midY - dy) - rectBefore.top) / rectBefore.height : 0.5;
    if (ev.scaleRatio && ev.scaleRatio !== 1) {
      const base = pinchScale != null ? pinchScale : currentScale(aw, ah, pic);
      pinchScale = Math.max(0.25, Math.min(6, base * ev.scaleRatio));
    }
    layout();
    const rectAfter = canvas.getBoundingClientRect();
    const targetX = rectAfter.left + u * rectAfter.width;
    const targetY = rectAfter.top + v * rectAfter.height;
    stage.scrollLeft += (targetX - ev.midX) / k;
    stage.scrollTop += (targetY - ev.midY) / k;
  }

  function layout() {
    const s = stream.surface;
    const k = ratio() || 1;
    if (!document.fullscreenElement && !document.webkitFullscreenElement) {
      // The stage fills the window down to the bottom of the browser window.
      const top = stage.getBoundingClientRect().top + window.scrollY;
      const below = (keybar.hidden ? 0 : keybar.getBoundingClientRect().height)
        + (gamebar.hidden ? 0 : gamebar.getBoundingClientRect().height)
        + statusbar.getBoundingClientRect().height;
      const avail = Math.floor((window.innerHeight - top - below) / k) - 26;
      stage.style.height = `${Math.max(220, avail)}px`;
    } else {
      stage.style.height = '';
    }
    if (!s || !s.w) return;
    // The picture's size as the canvas has it: a new size takes effect with its first frame.
    const pic = { w: canvas.width, h: canvas.height };
    // The stage's padding (6 + 6) and the bezel (4 + 1 on each side, plus its 1-pixel shadow).
    const aw = stage.clientWidth - 24;
    const ah = stage.clientHeight - 24;
    const scale = currentScale(aw, ah, pic);
    // A phone held upright: the picture is limited by the width, so the stage shrinks to it
    // and the key bar stays close under the screen.
    const fullscreen = document.fullscreenElement || document.webkitFullscreenElement;
    if (!fullscreen && size === 'fit' && pinchScale == null && Math.round(pic.h * scale) + 24 < stage.clientHeight) {
      stage.style.height = `${Math.max(120, Math.round(pic.h * scale) + 24)}px`;
    }
    canvas.style.width = `${Math.round(pic.w * scale)}px`;
    canvas.style.height = `${Math.round(pic.h * scale)}px`;
    canvas.classList.toggle('is-smooth', scale < 1);
    bezel.classList.toggle('is-scrolling', pic.w * scale > aw || pic.h * scale > ah);
  }

  // ---- full screen -----------------------------------------------------------------------
  async function toggleFullscreen() {
    if (document.fullscreenElement || document.webkitFullscreenElement) {
      await (document.exitFullscreen ? document.exitFullscreen() : document.webkitExitFullscreen());
      return;
    }
    try {
      await (shell.requestFullscreen ? shell.requestFullscreen({ navigationUI: 'hide' }) : shell.webkitRequestFullscreen());
      // Chromium: keep Command-W, Command-T and friends for Mac OS while in full screen.
      if (navigator.keyboard && navigator.keyboard.lock) navigator.keyboard.lock().catch(() => {});
      canvas.focus({ preventScroll: true });
    } catch {
      /* the browser refused */
    }
  }
  const onFs = () => {
    const full = !!(document.fullscreenElement || document.webkitFullscreenElement);
    shell.classList.toggle('is-fullscreen', full);
    fsBtn.querySelector('.btn-label').textContent = full ? 'Exit Full Screen' : 'Full Screen';
    if (!full && navigator.keyboard && navigator.keyboard.unlock) navigator.keyboard.unlock();
    requestAnimationFrame(layout);
  };
  document.addEventListener('fullscreenchange', onFs);
  document.addEventListener('webkitfullscreenchange', onFs);
  scope.add(() => {
    document.removeEventListener('fullscreenchange', onFs);
    document.removeEventListener('webkitfullscreenchange', onFs);
    if (document.fullscreenElement === shell) document.exitFullscreen().catch(() => {});
  });

  // ---- paste -----------------------------------------------------------------------------
  async function pasteDialog() {
    const area = h('textarea', { class: 'paste-text', rows: '6', 'aria-label': 'Text to type in Mac OS' });
    const max = (stream.hello && stream.hello.typing && stream.hello.typing.max_chars) || 8000;
    const cps = (stream.hello && stream.hello.typing && stream.hello.typing.chars_per_second) || 20;
    const v = await dialog({
      title: 'Type Text in Mac OS',
      icon: 'note',
      message: [`RetroBoot types the text into the frontmost Mac OS application, about ${Math.round(cps)} characters a second. Characters Mac OS can’t show are skipped.`],
      content: area,
      buttons: [
        { label: 'Cancel', value: 'cancel' },
        { label: 'Type', value: 'type', variant: 'default' },
      ],
      onOpen: () => area.focus(),
      validate: (val) => val !== 'type' || (area.value.length > 0 && area.value.length <= max),
    });
    if (v === 'type' && area.value) {
      stream.send({ t: 'type', text: area.value });
      canvas.focus({ preventScroll: true });
    }
  }

  // ---- lifecycle ----------------------------------------------------------------------------
  const onVisibility = () => {
    if (document.visibilityState === 'hidden') {
      input.releaseAll();
      audioSink.disable();
      stream.stop('paused');
    } else {
      if (audioOn) audioSink.enable();
      stream.start();
    }
  };
  document.addEventListener('visibilitychange', onVisibility);
  scope.add(() => document.removeEventListener('visibilitychange', onVisibility));
  const ro = 'ResizeObserver' in window ? new ResizeObserver(() => layout()) : null;
  if (ro) ro.observe(stage);
  const onResize = () => layout();
  window.addEventListener('resize', onResize);
  scope.add(() => {
    if (ro) ro.disconnect();
    window.removeEventListener('resize', onResize);
  });
  scope.add(onPlatinum(() => requestAnimationFrame(layout)));
  scope.add(subscribe('status', update));
  scope.interval(update, 1000);
  scope.add(() => input.destroy());
  scope.add(() => audioSink.destroy());
  scope.add(() => stream.stop('closed'));
  stream.start();
  update();
  requestAnimationFrame(layout);
  // Measurement hook for lab tools (fps, latency, bytes): not used by the page itself.
  window.__rbScreen = { stream, input, layout };
  scope.add(() => {
    if (window.__rbScreen && window.__rbScreen.stream === stream) delete window.__rbScreen;
  });
  // The window's zoom box fills the screen with it.
  return { el, zoom: fsAvailable ? toggleFullscreen : null, destroy: () => scope.destroy() };
}
