// Keyboard, mouse and touch for the Screen page.
//
// Positions are absolute, in Mac screen pixels of the current picture (the
// surface generation travels with them, so a position meant for a previous
// picture is dropped instead of misplaced).  Keys are sent as
// KeyboardEvent.code; hostd maps them to Mac keys (hostd/retroboot/remote/keymap.py
// lists the same codes).  Mac OS repeats held keys itself, so the browser's
// auto-repeat is not forwarded.  Everything this page holds is released when
// the canvas or the window loses focus.

/** KeyboardEvent.code values hostd forwards to Mac OS (keymap.py CODE_TO_EVDEV). */
export const KEY_CODES = new Set([
  ...'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('').map((c) => `Key${c}`),
  ...'0123456789'.split('').map((d) => `Digit${d}`),
  ...Array.from({ length: 24 }, (_, i) => `F${i + 1}`),
  ...'0123456789'.split('').map((d) => `Numpad${d}`),
  'Escape', 'Minus', 'Equal', 'Backspace', 'Tab', 'BracketLeft', 'BracketRight', 'Enter', 'ControlLeft', 'ControlRight',
  'Semicolon', 'Quote', 'Backquote', 'Backslash', 'ShiftLeft', 'ShiftRight', 'Comma', 'Period', 'Slash',
  'AltLeft', 'AltRight', 'MetaLeft', 'MetaRight', 'OSLeft', 'OSRight', 'Space', 'CapsLock', 'NumLock', 'ScrollLock',
  'NumpadMultiply', 'NumpadSubtract', 'NumpadAdd', 'NumpadDecimal', 'NumpadEnter', 'NumpadDivide', 'NumpadEqual',
  'NumpadComma', 'IntlBackslash', 'IntlRo', 'IntlYen', 'PrintScreen', 'Pause', 'ContextMenu', 'Help',
  'Home', 'End', 'PageUp', 'PageDown', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Insert', 'Delete',
]);

const MODIFIERS = new Set(['ShiftLeft', 'ShiftRight', 'ControlLeft', 'ControlRight', 'AltLeft', 'AltRight', 'MetaLeft', 'MetaRight', 'OSLeft', 'OSRight']);
const LONG_PRESS_MS = 550;
const TAP_SLOP = 10; // CSS pixels a finger may wander and still tap
const DRAG_SLOP = 18; // CSS pixels of immediate roll before committing a touch drag
const DRAG_HOLD_MS = 120; // ms grace window so clumsy taps/staggered 2-finger touches don't drag Finder icons
const DOUBLE_TAP_MS = 320;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class InputController {
  /**
   * @param {object} o
   * @param {HTMLCanvasElement} o.canvas
   * @param {import('./stream.js').ScreenStream} o.stream
   * @param {HTMLTextAreaElement} [o.keyboardSink]  focused to bring up a touch keyboard
   * @param {() => boolean} o.canControl
   * @param {(latched: Set<string>) => void} [o.onLatch]
   * @param {(ev: object) => void} [o.onPinch]
   * @param {() => boolean} [o.canPan]
   */
  constructor({ canvas, stream, keyboardSink, canControl, onLatch, onPinch, canPan }) {
    this.canvas = canvas;
    this.stream = stream;
    this.sink = keyboardSink || null;
    this.canControl = canControl;
    this.onLatch = onLatch || (() => {});
    this.onPinch = onPinch || null;
    this.canPan = canPan || (() => false);
    this.trackpad = false;
    this.latched = new Set(); // 'shift' | 'control' | 'option' | 'command'
    this.latchKey = null;
    this.pressed = new Set(); // codes this page holds down
    this.buttons = new Set();
    this.phys = 0; // physical mouse buttons down (PointerEvent.buttons)
    this.pending = null;
    this.raf = 0;
    this.touches = new Map();
    this.touchState = null;
    this.twoMode = null;
    this.twoDist0 = 0;
    this.twoDistLast = 0;
    this.twoMidLast = null;
    this.twoCenter = null;
    this.vp = null;
    this.lastTapEnd = 0;
    this.wheelAcc = 0;
    this.composing = false;
    this.compText = '';
    this.committedDuringComp = false;
    this.probe = null;
    this.latency = [];
    const on = (el, type, fn, opts) => {
      el.addEventListener(type, fn, opts);
      this.offs.push(() => el.removeEventListener(type, fn, opts));
    };
    this.offs = [];
    on(canvas, 'pointerdown', (e) => this.pointerDown(e));
    on(canvas, 'pointermove', (e) => this.pointerMove(e));
    on(canvas, 'pointerup', (e) => this.pointerUp(e));
    on(canvas, 'pointercancel', (e) => this.pointerCancel(e));
    on(canvas, 'wheel', (e) => this.wheel(e), { passive: false });
    on(canvas, 'contextmenu', (e) => e.preventDefault());
    on(canvas, 'keydown', (e) => this.keyDown(e));
    on(canvas, 'keyup', (e) => this.keyUp(e));
    on(canvas, 'blur', () => this.releaseAll());
    on(window, 'blur', () => this.releaseAll());
    if (this.sink) {
      on(this.sink, 'keydown', (e) => this.keyDown(e, true));
      on(this.sink, 'keyup', (e) => this.keyUp(e, true));
      on(this.sink, 'compositionstart', () => this.compositionStart());
      on(this.sink, 'compositionupdate', (e) => this.compositionUpdate(e));
      on(this.sink, 'compositionend', (e) => this.compositionEnd(e));
      on(this.sink, 'beforeinput', (e) => this.beforeInput(e));
      on(this.sink, 'input', (e) => this.sinkInput(e));
      on(this.sink, 'blur', () => {
        this.flushComposition();
        this.releaseAll();
      });
    }
  }

  destroy() {
    this.releaseAll();
    cancelAnimationFrame(this.raf);
    clearTimeout(this.touchTimer);
    for (const off of this.offs.splice(0)) off();
  }

  get platform() {
    return this.stream.platform;
  }

  get swap() {
    return !!(this.stream.hello && this.stream.hello.swap);
  }

  /** The browser code that reaches Mac OS as Option / Command after hostd's Alt/Meta choice. */
  modifierCode(name) {
    if (name === 'shift') return 'ShiftLeft';
    if (name === 'control') return 'ControlLeft';
    if (name === 'option') return this.swap ? 'MetaLeft' : 'AltLeft';
    return this.swap ? 'AltLeft' : 'MetaLeft';
  }

  // ---- positions ------------------------------------------------------------

  pos(e) {
    const s = this.stream.surface;
    if (!s || !s.w) return null;
    const r = this.canvas.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    const x = Math.min(s.w - 1, Math.max(0, Math.floor(((e.clientX - r.left) * s.w) / r.width)));
    const y = Math.min(s.h - 1, Math.max(0, Math.floor(((e.clientY - r.top) * s.h) / r.height)));
    return { x, y, gen: s.gen };
  }

  sendMove(p) {
    if (!p || !this.stream.send({ t: 'move', x: p.x, y: p.y, gen: p.gen })) return;
    // Latency probe: the time until a drawn frame changes pixels near the new position
    // (the Mac redraws its pointer there).
    if (!this.probe) this.probe = { t: performance.now(), x: p.x, y: p.y };
  }

  queueMove(p) {
    this.pending = p;
    if (!this.raf) {
      this.raf = requestAnimationFrame(() => {
        this.raf = 0;
        this.flushMove();
      });
    }
  }

  flushMove() {
    if (this.pending) {
      const p = this.pending;
      this.pending = null;
      this.sendMove(p);
    }
  }

  /** Called after every drawn frame (stream.onFrame). */
  frameDrawn(frame, at) {
    const p = this.probe;
    if (!p) return;
    if (at - p.t > 2000) {
      this.probe = null;
      return;
    }
    const hit = frame.tiles.some((t) => t.x <= p.x + 24 && t.x + t.w >= p.x - 24 && t.y <= p.y + 24 && t.y + t.h >= p.y - 24);
    if (hit) {
      this.latency.push(at - p.t);
      if (this.latency.length > 60) this.latency.shift();
      this.probe = null;
    }
  }

  latencyStats() {
    if (!this.latency.length) return null;
    const s = [...this.latency].sort((a, b) => a - b);
    return { median: s[Math.floor(s.length / 2)], p95: s[Math.min(s.length - 1, Math.floor(s.length * 0.95))], n: s.length };
  }

  // ---- buttons and keys ------------------------------------------------------

  key(code, down, caps) {
    if (down) this.pressed.add(code);
    else this.pressed.delete(code);
    const msg = { t: 'key', code, down };
    if (typeof caps === 'boolean') msg.caps = caps; // keeps Mac OS's Caps Lock in step with this keyboard's
    this.stream.send(msg);
  }

  async tapKey(code) {
    this.key(code, true);
    await sleep(30);
    this.key(code, false);
  }

  button(b, down, p) {
    if (down) this.buttons.add(b);
    else this.buttons.delete(b);
    const msg = { t: 'button', b, down };
    if (p) Object.assign(msg, { x: p.x, y: p.y, gen: p.gen });
    this.stream.send(msg);
  }

  latchedDown() {
    for (const m of this.latched) this.key(this.modifierCode(m), true);
  }

  latchedUp() {
    for (const m of this.latched) this.key(this.modifierCode(m), false);
    this.latched.clear();
    this.onLatch(this.latched);
  }

  toggleLatch(name) {
    if (this.latched.has(name)) this.latched.delete(name);
    else this.latched.add(name);
    this.onLatch(this.latched);
  }

  /** A click with Mac button b: 0 primary, 2 secondary (a contextual menu). */
  async click(p, { button = 0 } = {}) {
    this.latchedDown();
    this.sendMove(p);
    this.button(button, true, p);
    await sleep(40);
    this.button(button, false, p);
    this.latchedUp();
  }

  releaseAll() {
    this.flushMove();
    for (const code of [...this.pressed]) this.key(code, false);
    for (const b of [...this.buttons]) this.button(b, false);
    this.phys = 0;
    this.touches.clear();
    this.touchState = null;
    clearTimeout(this.touchTimer);
    this.stream.send({ t: 'release' });
  }

  // ---- mouse and pen ---------------------------------------------------------

  pointerDown(e) {
    if (e.pointerType === 'touch') return this.touchDown(e);
    if (!this.canControl()) return;
    e.preventDefault();
    this.canvas.focus({ preventScroll: true });
    try {
      this.canvas.setPointerCapture(e.pointerId);
    } catch {
      /* capture is a convenience */
    }
    const p = this.pos(e);
    if (!p) return;
    this.pending = null;
    this.sendMove(p);
    this.syncButtons(e, p);
  }

  pointerMove(e) {
    if (e.pointerType === 'touch') return this.touchMove(e);
    if (!this.canControl()) return;
    const p = this.pos(e);
    if (!p) return;
    if ((e.buttons & 7) !== this.phys) {
      // A second button pressed or released while another is held (chorded buttons come as moves).
      this.pending = null;
      this.sendMove(p);
      this.syncButtons(e, p);
    } else {
      this.queueMove(p);
    }
  }

  pointerUp(e) {
    if (e.pointerType === 'touch') return this.touchUp(e);
    if (!this.canControl()) return;
    this.flushMove();
    this.syncButtons(e, this.pos(e));
  }

  pointerCancel(e) {
    if (e.pointerType === 'touch') return this.touchCancel(e);
    this.syncButtons({ buttons: 0 }, null);
  }

  // Physical buttons (PointerEvent.buttons: 1 left, 2 right, 4 middle) are Mac buttons 0, 2
  // and 1.  The right button is a real secondary button: USB Overdrive makes it its
  // Control-click, and without it classicvirtio does (apps/reports/mouse-2026-10-11.md).
  // Every mouse event reconciles the whole set (chords arrive as pointermove), so no
  // release is missed whatever the order of presses.  Latched modifiers go down with the
  // first button and up after the last.
  syncButtons(e, p) {
    const now = (e.buttons || 0) & 7;
    const was = this.phys;
    if (now === was) return;
    this.phys = now;
    for (const [bit, b] of [[1, 0], [4, 1], [2, 2]]) {
      const down = !!(now & bit);
      if (down === !!(was & bit)) continue;
      if (down) {
        if (!this.buttons.size) this.latchedDown();
        this.button(b, true, p);
      } else {
        this.button(b, false, p);
        if (!this.buttons.size && this.latched.size) this.latchedUp();
      }
    }
  }

  wheel(e) {
    if (!this.canControl()) return;
    e.preventDefault();
    const unit = e.deltaMode === 1 ? 20 : e.deltaMode === 2 ? 400 : 1;
    this.wheelAcc += e.deltaY * unit;
    const notches = Math.trunc(this.wheelAcc / 60);
    if (notches) {
      this.wheelAcc -= notches * 60;
      const p = this.pos(e);
      this.stream.send(p ? { t: 'wheel', dy: notches, x: p.x, y: p.y, gen: p.gen } : { t: 'wheel', dy: notches });
    }
  }

  // ---- keyboard --------------------------------------------------------------

  keyDown(e, fromSink = false) {
    if (!this.canControl()) return;
    if (e.isComposing || this.composing || e.keyCode === 229) return;
    if (!KEY_CODES.has(e.code)) return; // a touch keyboard: text arrives as beforeinput
    e.preventDefault();
    if (e.repeat) return;
    const code = e.code === 'OSLeft' ? 'MetaLeft' : e.code === 'OSRight' ? 'MetaRight' : e.code;
    if (code === 'CapsLock') {
      // Mac OS's Caps Lock is a locking key: hostd holds it down while this keyboard's lock is on.
      this.stream.send({ t: 'caps', on: e.getModifierState('CapsLock') });
      return;
    }
    if (this.latched.size && !MODIFIERS.has(code)) {
      this.latchedDown();
      this.latchKey = code;
    }
    this.key(code, true, e.getModifierState('CapsLock'));
    if ((this.platform === 'mac' || this.platform === 'ios') && e.metaKey && !MODIFIERS.has(code)) {
      // macOS sends no keyup for a key released while Command is held.
      setTimeout(() => this.pressed.has(code) && this.key(code, false), 120);
    }
    void fromSink;
  }

  keyUp(e) {
    if (!this.canControl()) return;
    if (!KEY_CODES.has(e.code)) return;
    e.preventDefault();
    const code = e.code === 'OSLeft' ? 'MetaLeft' : e.code === 'OSRight' ? 'MetaRight' : e.code;
    if (code === 'CapsLock') {
      this.stream.send({ t: 'caps', on: e.getModifierState('CapsLock') });
      return;
    }
    if (this.pressed.has(code)) this.key(code, false);
    if (code === 'MetaLeft' || code === 'MetaRight') {
      for (const c of [...this.pressed]) if (!MODIFIERS.has(c)) this.key(c, false);
    }
    if (code === this.latchKey) {
      this.latchKey = null;
      this.latchedUp();
    }
  }

  compositionStart() {
    this.composing = true;
    this.compText = '';
    this.committedDuringComp = false;
  }

  compositionUpdate(e) {
    this.composing = true;
    if (typeof e.data === 'string') this.compText = e.data;
  }

  compositionEnd(e) {
    this.composing = false;
    const text = !this.committedDuringComp ? (typeof e.data === 'string' ? e.data : this.compText || (this.sink ? this.sink.value : '')) : '';
    this.compText = '';
    this.committedDuringComp = false;
    if (this.sink) this.sink.value = '';
    if (text && this.canControl()) this.stream.send({ t: 'type', text });
  }

  flushComposition() {
    if (!this.composing) return;
    const text = !this.committedDuringComp ? (this.compText || (this.sink ? this.sink.value : '')) : '';
    this.composing = false;
    this.compText = '';
    this.committedDuringComp = false;
    if (this.sink) this.sink.value = '';
    if (text && this.canControl()) this.stream.send({ t: 'type', text });
  }

  sinkInput(e) {
    if (!this.sink) return;
    if (this.composing || (e && e.isComposing)) {
      this.compText = this.sink.value;
      return;
    }
    if (this.sink.value && this.canControl()) {
      this.stream.send({ t: 'type', text: this.sink.value });
    }
    this.sink.value = '';
  }

  beforeInput(e) {
    if (!this.canControl()) return;
    const t = e.inputType;
    if (t === 'insertCompositionText' || t === 'deleteCompositionText' || t === 'deleteByComposition') {
      // Android Gboard / Samsung IME: never preventDefault or wipe sink.value mid-composition.
      this.composing = true;
      if (typeof e.data === 'string') this.compText = e.data;
      else if (t === 'deleteCompositionText') this.compText = '';
    } else if (t === 'insertFromComposition') {
      e.preventDefault();
      const text = e.data || this.compText || (this.sink ? this.sink.value : '');
      this.composing = false;
      this.compText = '';
      this.committedDuringComp = true;
      if (this.sink) this.sink.value = '';
      if (text) this.stream.send({ t: 'type', text });
    } else if (t === 'insertText' || t === 'insertReplacementText' || t === 'insertFromPaste') {
      if (e.isComposing || this.composing) return;
      e.preventDefault();
      if (e.data) this.stream.send({ t: 'type', text: e.data });
      if (this.sink) this.sink.value = '';
    } else if (t === 'insertLineBreak' || t === 'insertParagraph') {
      e.preventDefault();
      this.flushComposition();
      if (this.sink) this.sink.value = '';
      this.tapKey('Enter');
    } else if (t === 'deleteContentBackward') {
      if (this.composing || e.isComposing) return;
      e.preventDefault();
      this.tapKey('Backspace');
    } else if (t === 'deleteContentForward') {
      e.preventDefault();
      this.tapKey('Delete');
    }
  }

  // ---- touch -----------------------------------------------------------------
  // Direct: tap clicks where the finger is, a long press is a right click, a drag
  // drags, two fingers scroll.  Trackpad: the finger moves the pointer relatively,
  // tap clicks, tap-then-drag drags, a two-finger tap is a right click.

  touchDown(e) {
    e.preventDefault();
    if (!this.canControl()) return;
    try {
      this.canvas.setPointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    const now = performance.now();
    this.touches.set(e.pointerId, { x0: e.clientX, y0: e.clientY, x: e.clientX, y: e.clientY, t0: now });
    if (this.touches.size === 1) {
      const p = this.pos(e);
      this.touchStart = p;
      if (this.trackpad) {
        this.touchState = now - this.lastTapEnd < DOUBLE_TAP_MS ? 'tapdrag' : 'pending';
        if (!this.vp && this.stream.surface) this.vp = { x: this.stream.surface.w >> 1, y: this.stream.surface.h >> 1, gen: this.stream.surface.gen };
      } else {
        this.touchState = 'pending';
        clearTimeout(this.touchTimer);
        this.touchTimer = setTimeout(() => {
          if (this.touchState !== 'pending' || !p) return;
          this.touchState = 'longpress';
          if (navigator.vibrate) navigator.vibrate(10);
          this.click(p, { button: 2 });
        }, LONG_PRESS_MS);
      }
    } else if (this.touches.size === 2) {
      clearTimeout(this.touchTimer);
      if (this.touchState === 'drag' && this.buttons.has(0)) this.button(0, false);
      this.touchState = 'two';
      this.twoAt = now;
      this.twoMoved = 0;
      this.twoMode = null;
      const [t1, t2] = [...this.touches.values()];
      this.twoDist0 = Math.hypot(t2.x - t1.x, t2.y - t1.y);
      this.twoDistLast = this.twoDist0;
      this.twoMidLast = { x: (t1.x + t2.x) / 2, y: (t1.y + t2.y) / 2 };
      this.twoMid0 = { ...this.twoMidLast };
      this.twoCenter = this.pos({ clientX: this.twoMidLast.x, clientY: this.twoMidLast.y });
    }
  }

  touchMove(e) {
    const t = this.touches.get(e.pointerId);
    if (!t || !this.canControl()) return;
    e.preventDefault();
    const dx = e.clientX - t.x, dy = e.clientY - t.y;
    t.x = e.clientX;
    t.y = e.clientY;
    const moved = Math.hypot(t.x - t.x0, t.y - t.y0);
    if (this.touchState === 'two') {
      this.twoMoved += Math.hypot(dx, dy);
      if (this.touches.size === 2) {
        const [t1, t2] = [...this.touches.values()];
        const mid = { x: (t1.x + t2.x) / 2, y: (t1.y + t2.y) / 2 };
        const midDx = mid.x - (this.twoMidLast ? this.twoMidLast.x : mid.x);
        const midDy = mid.y - (this.twoMidLast ? this.twoMidLast.y : mid.y);
        const dist = Math.hypot(t2.x - t1.x, t2.y - t1.y);
        if (!this.twoMode) {
          if (this.onPinch && this.twoDist0 > 10 && Math.abs(dist - this.twoDist0) > 12) {
            this.twoMode = 'pinch';
            this.onPinch({ phase: 'start', midX: mid.x, midY: mid.y });
          } else if (this.onPinch && this.canPan() && this.twoMid0 && Math.hypot(mid.x - this.twoMid0.x, mid.y - this.twoMid0.y) > TAP_SLOP) {
            this.twoMode = 'pan';
            this.onPinch({ phase: 'start', midX: mid.x, midY: mid.y });
          } else if (Math.abs(dy) > 4) {
            this.twoMode = 'scroll';
          }
        }
        if (this.twoMode === 'pinch' && this.onPinch) {
          const ratio = this.twoDistLast > 0 ? dist / this.twoDistLast : 1;
          this.onPinch({ phase: 'move', scaleRatio: ratio, midX: mid.x, midY: mid.y, dx: midDx, dy: midDy });
        } else if (this.twoMode === 'pan' && this.onPinch) {
          this.onPinch({ phase: 'move', scaleRatio: 1, midX: mid.x, midY: mid.y, dx: midDx, dy: midDy });
        } else {
          this.wheelAcc += dy;
          const notches = Math.trunc(this.wheelAcc / 24);
          if (notches) {
            this.wheelAcc -= notches * 24;
            this.stream.send({ t: 'wheel', dy: -notches });
          }
        }
        this.twoDistLast = dist;
        this.twoMidLast = mid;
      }
      return;
    }
    if (this.trackpad) {
      const s = this.stream.surface;
      const r = this.canvas.getBoundingClientRect();
      if (!s || !r.width || !this.vp) return;
      const speed = Math.hypot(dx, dy);
      const gain = (s.w / r.width) * (1 + Math.min(2, speed / 12));
      this.vp = { x: Math.min(s.w - 1, Math.max(0, this.vp.x + dx * gain)), y: Math.min(s.h - 1, Math.max(0, this.vp.y + dy * gain)), gen: s.gen };
      if (this.touchState === 'tapdrag' && moved > TAP_SLOP) {
        this.touchState = 'drag';
        this.button(0, true, this.round(this.vp));
      } else if (this.touchState === 'pending' && moved > TAP_SLOP) {
        this.touchState = 'moving';
      }
      this.queueMove(this.round(this.vp));
      return;
    }
    const elapsed = performance.now() - t.t0;
    if (this.touchState === 'pending' && (moved > DRAG_SLOP || (moved > TAP_SLOP && elapsed >= DRAG_HOLD_MS))) {
      clearTimeout(this.touchTimer);
      this.touchState = 'drag';
      if (this.touchStart) {
        this.sendMove(this.touchStart);
        this.button(0, true, this.touchStart);
      }
    }
    if (this.touchState === 'drag') {
      const p = this.pos(e);
      if (p) this.queueMove(p);
    }
  }

  round(p) {
    return { x: Math.round(p.x), y: Math.round(p.y), gen: p.gen };
  }

  touchUp(e) {
    const t = this.touches.get(e.pointerId);
    if (!t) return;
    e.preventDefault();
    this.touches.delete(e.pointerId);
    const now = performance.now();
    if (!this.canControl()) return;
    if (this.touchState === 'two') {
      if (this.touches.size === 0) {
        if (now - this.twoAt < 300 && this.twoMoved < TAP_SLOP) {
          const target = this.trackpad ? (this.vp && this.round(this.vp)) : this.twoCenter;
          if (target) this.click(target, { button: 2 });
        }
        if (this.twoMode === 'pinch' || this.twoMode === 'pan') this.onPinch?.({ phase: 'end' });
        this.touchState = null;
        this.twoMode = null;
      }
      return;
    }
    if (this.touches.size) return;
    clearTimeout(this.touchTimer);
    this.flushMove();
    const state = this.touchState;
    this.touchState = null;
    const moved = Math.hypot(t.x - t.x0, t.y - t.y0);
    if (state === 'drag') {
      this.button(0, false, this.trackpad && this.vp ? this.round(this.vp) : this.pos(e));
    } else if (state === 'tapdrag' || (state === 'pending' && (this.trackpad || moved <= DRAG_SLOP))) {
      const p = this.trackpad ? this.vp && this.round(this.vp) : (this.touchStart || this.pos(e));
      if (p) this.click(p);
      this.lastTapEnd = state === 'pending' ? now : 0;
    }
  }

  touchCancel(e) {
    this.touches.delete(e.pointerId);
    clearTimeout(this.touchTimer);
    if (this.touchState === 'drag' && this.buttons.has(0)) this.button(0, false);
    if (!this.touches.size) this.touchState = null;
  }
}
