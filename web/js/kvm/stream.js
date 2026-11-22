// Live screen over GET /api/v1/screen/stream (docs/ARCHITECTURE.md section 7).
//
// One WebSocket per page.  Binary messages are frames of lossless tiles (zlib of
// the tap buffer's BGRX rows, or PNG); each frame is drawn in order and then
// acknowledged, which is what lets hostd send the next one (a slow browser gets
// fewer, larger updates).  JSON messages carry the picture size, viewers, mode,
// typing progress and errors.  The stream reconnects by itself after a network
// drop, a hostd restart or a RetroBoot restart, and pauses while the tab is hidden.

import { store } from '../store.js';
import { get, setCsrf } from '../api.js';

const FRAME_MAGIC = 0x46;
const ENC_ZLIB = 1;
const ENC_PNG = 2;
// Close codes and error codes after which reconnecting cannot help.
const FATAL = new Set(['remote-screen-off', 'not-paired', 'tls-required', 'too-much-input', 'installer']);

export function browserPlatform() {
  const ua = navigator.userAgent || '';
  const p = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || '';
  if (/iPhone|iPad|iPod/.test(ua) || (/mac/i.test(p) && navigator.maxTouchPoints > 1)) return 'ios';
  if (/mac/i.test(p)) return 'mac';
  if (/win/i.test(p)) return 'windows';
  if (/Android/.test(ua)) return 'android';
  if (/CrOS/.test(ua)) return 'chromeos';
  if (/linux|x11/i.test(p)) return 'linux';
  return 'other';
}

const canInflate = typeof DecompressionStream === 'function';

async function inflate(bytes) {
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate'));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/** BGRX rows (little-endian XRGB8888) -> ImageData (RGBA). */
function bgrxToImage(bytes, w, h) {
  const img = new ImageData(w, h);
  const src = new Uint32Array(bytes.buffer, bytes.byteOffset, w * h);
  const dst = new Uint32Array(img.data.buffer);
  for (let i = 0; i < src.length; i++) {
    const v = src[i];
    dst[i] = 0xff000000 | ((v & 0xff) << 16) | (v & 0xff00) | ((v >>> 16) & 0xff);
  }
  return img;
}

export function parseFrame(buf) {
  const dv = new DataView(buf);
  if (dv.byteLength < 16 || dv.getUint8(0) !== FRAME_MAGIC || dv.getUint8(1) !== 1) return null;
  const n = dv.getUint16(2, true);
  const frame = { seq: dv.getUint32(4, true), gen: dv.getUint32(8, true), flags: dv.getUint32(12, true), tiles: [] };
  let o = 16;
  for (let i = 0; i < n; i++) {
    if (o + 16 > dv.byteLength) return null;
    const t = { x: dv.getUint16(o, true), y: dv.getUint16(o + 2, true), w: dv.getUint16(o + 4, true), h: dv.getUint16(o + 6, true), enc: dv.getUint8(o + 8) };
    const len = dv.getUint32(o + 12, true);
    o += 16;
    if (o + len > dv.byteLength) return null;
    t.data = new Uint8Array(buf, o, len);
    o += len;
    frame.tiles.push(t);
  }
  return frame;
}

/**
 * @param {object} o
 * @param {HTMLCanvasElement} o.canvas
 * @param {(state: string, detail?: object) => void} o.onState  connecting|live|reconnecting|paused|insecure|refused
 * @param {(msg: object) => void} o.onMessage  every JSON message from hostd
 * @param {(frame: object, drawnAt: number) => void} [o.onFrame]
 * @param {(surface: object) => void} [o.onResize]  the canvas took a new picture size (called before it is painted)
 */
export class ScreenStream {
  constructor({ canvas, onState, onMessage, onFrame, onResize, source = 'composed' }) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d', { alpha: false, desynchronized: true });
    this.onState = onState || (() => {});
    this.onMessage = onMessage || (() => {});
    this.onFrame = onFrame || (() => {});
    this.onResize = onResize || (() => {});
    this.resizeTo = null;     // a new picture size, applied with that picture's first frame
    this.source = source;
    this.platform = browserPlatform();
    this.swap = null;
    this.ws = null;
    this.wanted = false;
    this.retry = 0;
    this.timer = null;
    this.queue = Promise.resolve();
    this.surface = null;      // {gen, w, h, view, kind}
    this.hello = null;
    this.stats = { frames: 0, tiles: 0, bytes: 0, drawn: [], started: performance.now() };
    this.state = 'idle';
  }

  get open() {
    return !!this.ws && this.ws.readyState === WebSocket.OPEN && !!this.hello;
  }

  setState(state, detail) {
    this.state = state;
    this.onState(state, detail);
  }

  start() {
    this.wanted = true;
    if (location.protocol !== 'https:') {
      this.setState('insecure');
      return;
    }
    if (!this.ws) this.connect();
  }

  stop(state = 'paused') {
    this.wanted = false;
    clearTimeout(this.timer);
    this.timer = null;
    if (this.ws) {
      const ws = this.ws;
      this.ws = null;
      try {
        ws.close(1000, 'page');
      } catch {
        /* already closed */
      }
    }
    this.hello = null;
    this.setState(state);
  }

  send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN && this.hello) {
      this.ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }

  setSource(source) {
    this.source = source;
    this.send({ t: 'source', source });
  }

  setSwap(swap) {
    this.swap = swap;
    this.send({ t: 'options', swap });
  }

  connect() {
    clearTimeout(this.timer);
    this.timer = null;
    if (this.ws) return; // one socket at a time (a retry timer or a session refresh may race start())
    this.setState(this.retry ? 'reconnecting' : 'connecting');
    let ws;
    try {
      ws = new WebSocket(`wss://${location.host}/api/v1/screen/stream`);
    } catch (err) {
      this.scheduleRetry();
      return;
    }
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    let refused = null;
    ws.onopen = () => {
      const csrf = store.session && store.session.csrf;
      ws.send(JSON.stringify({ t: 'hello', v: 1, csrf, platform: this.platform, enc: canInflate ? ['zlib', 'png'] : ['png'], source: this.source, swap: this.swap }));
    };
    ws.onmessage = (ev) => {
      if (ws !== this.ws) return;
      if (typeof ev.data === 'string') {
        let msg;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (msg.t === 'hello') {
          this.hello = msg;
          this.retry = 0;
          this.setState('live', msg);
        } else if (msg.t === 'surface') {
          this.queue = this.queue.then(() => this.applySurface(msg));
        } else if (msg.t === 'error') {
          refused = msg;
        }
        this.onMessage(msg);
        return;
      }
      const frame = parseFrame(ev.data);
      if (!frame) return;
      this.stats.bytes += ev.data.byteLength;
      this.queue = this.queue.then(() => this.draw(frame, ws)).catch(() => {});
    };
    ws.onclose = (ev) => {
      if (ws !== this.ws) return;
      this.ws = null;
      this.hello = null;
      const code = (refused && refused.code) || (ev.code === 4403 ? 'remote-screen-off' : null);
      if (refused && refused.code === 'csrf-invalid' && this.retry < 1) {
        // The session's token may have changed (a new pairing); fetch it and try once more.
        this.retry += 1;
        get('/api/v1/session', { quiet401: true })
          .then((s) => {
            store.session = s;
            setCsrf(s.csrf);
          })
          .catch(() => {})
          .finally(() => this.wanted && !this.ws && this.connect());
        return;
      }
      if (code && FATAL.has(code)) {
        this.wanted = false;
        this.setState('refused', refused || { code, message: 'The remote screen is turned off.' });
        return;
      }
      if (this.wanted) this.scheduleRetry();
      else this.setState('paused');
    };
    ws.onerror = () => {};
  }

  scheduleRetry() {
    this.retry += 1;
    const delay = Math.min(5000, 400 * 2 ** Math.min(this.retry - 1, 4));
    this.setState('reconnecting', { delay });
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.wanted && !this.ws && this.connect(), delay);
  }

  /**
   * A new picture.  Its size takes effect with its first frame (draw), so the canvas keeps
   * the last picture until then instead of going black.  Through a Mac OS mode switch the
   * presenter holds its last good frame and sends no picture ("switching", w 0), then the
   * complete new screen: here, as on the computer's display, the old picture stays and the
   * new one replaces it in one step.
   */
  applySurface(s) {
    this.surface = s;
    this.resizeTo = s.w > 0 && s.h > 0 && (this.canvas.width !== s.w || this.canvas.height !== s.h) ? s : null;
  }

  /** Draw one frame, then acknowledge it on the socket it came from (never on a newer one). */
  async draw(frame, ws) {
    const current = () => this.surface && frame.gen === this.surface.gen;
    let drawn = false;
    const decoded = [];
    try {
      if (current()) {
        // Decode every tile first, then resize (a new picture's first frame) and paint in one
        // step: the page never shows a cleared or half-painted canvas.
        for (const t of frame.tiles) {
          if (t.enc === ENC_ZLIB) {
            const raw = await inflate(t.data);
            if (raw.byteLength >= t.w * t.h * 4) decoded.push({ t, img: bgrxToImage(raw, t.w, t.h) });
          } else if (t.enc === ENC_PNG) {
            decoded.push({ t, bmp: await createImageBitmap(new Blob([t.data], { type: 'image/png' })) });
          }
        }
        if (current()) {
          const r = this.resizeTo;
          if (r && r.gen === frame.gen) {
            this.resizeTo = null;
            this.canvas.width = r.w;
            this.canvas.height = r.h;
            this.onResize(r);
          }
          for (const d of decoded) {
            if (d.img) this.ctx.putImageData(d.img, d.t.x, d.t.y);
            else this.ctx.drawImage(d.bmp, d.t.x, d.t.y);
          }
          drawn = true;
        }
      }
    } finally {
      for (const d of decoded) if (d.bmp) d.bmp.close();
      // Always acknowledged (hostd sends nothing more once two frames are unanswered),
      // and only on the socket the frame came from.
      if (ws === this.ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: 'ack', seq: frame.seq }));
    }
    const now = performance.now();
    if (!drawn) return;
    this.stats.frames += 1;
    this.stats.tiles += frame.tiles.length;
    this.stats.drawn.push(now);
    while (this.stats.drawn.length && now - this.stats.drawn[0] > 2000) this.stats.drawn.shift();
    this.onFrame(frame, now);
  }

  /** Frames drawn per second over the last two seconds. */
  fps() {
    const now = performance.now();
    const recent = this.stats.drawn.filter((t) => now - t <= 2000);
    return recent.length / 2;
  }
}
