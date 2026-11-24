"""Drive a running RetroBoot guest through QMP: input, screenshots and OCR.

Works against any QMP monitor of the guest's QEMU: the lab session's
``qmp-lab.sock`` (``apps/lab/rbsession.py``) or, on an appliance, hostd's lab
monitor ``/run/retroboot/qmp-lab.sock`` (``"lab_qmp": true`` in
``/etc/retroboot/hostd.json``; never in product images).  Input uses
``input-send-event`` (absolute pointer on the virtio tablet, ADB keyboard);
screenshots use ``screendump`` (PPM); OCR uses ``tesseract`` (Debian package
``tesseract-ocr``) on an enlarged copy.  Python standard library only.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

ABS_MAX = 0x7FFF

_SHIFTED = {'!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7',
            '*': '8', '(': '9', ')': '0', '_': 'minus', '+': 'equal',
            '{': 'bracket_left', '}': 'bracket_right', '|': 'backslash',
            ':': 'semicolon', '"': 'apostrophe', '~': 'grave_accent',
            '<': 'comma', '>': 'dot', '?': 'slash'}
_PLAIN = {' ': 'spc', '\n': 'ret', '\t': 'tab', '-': 'minus', '=': 'equal',
          '[': 'bracket_left', ']': 'bracket_right', '\\': 'backslash',
          ';': 'semicolon', "'": 'apostrophe', '`': 'grave_accent',
          ',': 'comma', '.': 'dot', '/': 'slash'}
# Friendly names -> QEMU qcodes (Mac: Command = meta_l, Option = alt)
KEY_ALIASES = {"command": "meta_l", "cmd": "meta_l", "option": "alt", "opt": "alt", "control": "ctrl",
               "return": "ret", "enter": "kp_enter", "escape": "esc", "space": "spc", "delete": "backspace",
               "backspace": "backspace", "fwd-delete": "delete"}


def char_to_key(ch: str) -> tuple[str, bool]:
    if ch.isascii() and ch.isalpha():
        return ch.lower(), ch.isupper()
    if ch.isascii() and ch.isdigit():
        return ch, False
    if ch in _PLAIN:
        return _PLAIN[ch], False
    if ch in _SHIFTED:
        return _SHIFTED[ch], True
    raise ValueError(f"no US-layout key for {ch!r}")


class QMPError(Exception):
    pass


class QMP:
    """Minimal synchronous QMP client with an event log."""

    def __init__(self, path: str, timeout: float = 10.0, stream=None):
        self.path = str(path)
        self.sock = None
        if stream is not None:          # a connected text stream (e.g. an SSH bridge)
            self.f = stream
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.sock.connect(self.path)
                    break
                except OSError:
                    self.sock.close()
                    if time.monotonic() > deadline:
                        raise QMPError(f"cannot connect to QMP at {self.path}")
                    time.sleep(0.1)
            self.f = self.sock.makefile("rw", encoding="utf-8")
        self.events: list[dict] = []
        self.replies: dict[int, dict] = {}
        self.cond = threading.Condition()
        self._id = 0
        greeting = json.loads(self.f.readline())
        if "QMP" not in greeting:
            raise QMPError("not a QMP monitor")
        threading.Thread(target=self._reader, daemon=True).start()
        self.cmd("qmp_capabilities")

    def _reader(self):
        while True:
            try:
                line = self.f.readline()
            except (OSError, ValueError):
                line = ""
            if not line:
                with self.cond:
                    self.events.append({"event": "_EOF", "mono": time.monotonic()})
                    self.cond.notify_all()
                return
            msg = json.loads(line)
            with self.cond:
                if "event" in msg:
                    msg["mono"] = time.monotonic()
                    self.events.append(msg)
                elif "id" in msg:
                    self.replies[msg["id"]] = msg
                self.cond.notify_all()

    def cmd(self, name: str, timeout: float = 30.0, **args):
        with self.cond:
            self._id += 1
            mid = self._id
        msg = {"execute": name, "id": mid}
        if args:
            msg["arguments"] = args
        self.f.write(json.dumps(msg) + "\n")
        self.f.flush()
        deadline = time.monotonic() + timeout
        with self.cond:
            while mid not in self.replies:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    raise QMPError(f"{name}: no reply")
                if self.events and self.events[-1].get("event") == "_EOF":
                    raise QMPError(f"{name}: QEMU closed the monitor")
                self.cond.wait(min(rem, 0.5))
            reply = self.replies.pop(mid)
        if "error" in reply:
            raise QMPError(f"{name}: {reply['error'].get('desc')}")
        return reply.get("return")

    def close(self):
        try:
            if self.sock is not None:
                self.sock.close()
            else:
                self.f.close()
        except OSError:
            pass


# Copies bytes between stdin/stdout and a UNIX socket; run with python3 on the
# QEMU host so QMP can be reached over SSH (no socat needed on the appliance).
_BRIDGE = ("import os,socket,sys,threading\n"
           "s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1])\n"
           "def up():\n"
           " while True:\n"
           "  d=os.read(0,65536)\n"
           "  if not d: break\n"
           "  s.sendall(d)\n"
           " s.shutdown(socket.SHUT_WR)\n"
           "threading.Thread(target=up,daemon=True).start()\n"
           "while True:\n"
           " d=s.recv(65536)\n"
           " if not d: break\n"
           " os.write(1,d)\n")


class LocalHost:
    """QEMU runs on this computer."""

    name = "local"

    def qmp(self, path: str) -> QMP:
        return QMP(path)

    def screendump_path(self) -> tuple[str, str]:
        d = tempfile.mkdtemp(prefix="rbshot-")
        os.chmod(d, 0o777)          # QEMU may run as rbguest
        return os.path.join(d, "s.ppm"), d

    def fetch(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def remove(self, path: str, d: str | None = None) -> None:
        try:
            os.unlink(path)
            if d:
                os.rmdir(d)
        except OSError:
            pass

    def run(self, argv: list[str]) -> str:
        return subprocess.run(argv, capture_output=True, text=True, timeout=30).stdout

    def clock_skew(self) -> float:
        return 0.0

    def utc_offset_s(self) -> int:
        """The QEMU host's UTC offset (Mac OS keeps local time: -rtc base=localtime)."""
        z = self.run(["date", "+%z"]).strip() or "+0000"
        sign = -1 if z.startswith("-") else 1
        return sign * (int(z[1:3]) * 3600 + int(z[3:5]) * 60)


class SSHHost(LocalHost):
    """QEMU runs on another computer reachable with ``ssh`` (the runner, OCR and
    evidence stay here; the appliance needs only python3, which hostd has)."""

    def __init__(self, target: str, ssh_args: list[str] | None = None,
                 screens_dir: str = "/run/retroboot/screens"):
        self.target = target
        # one multiplexed SSH connection for the many short commands (screendumps)
        # macOS's per-user temp directory is too long for a Unix socket path once ssh
        # expands %C (40 hex digits; limit 104 bytes): ssh then fails to start and
        # the QMP greeting is empty. Use /tmp for the control socket in that case.
        tmp = tempfile.gettempdir()
        ctl = os.path.join(tmp if len(tmp) <= 40 else "/tmp", "rbapps-ssh-%C")
        self.ssh = ["ssh", "-o", "ControlMaster=auto", "-o", f"ControlPath={ctl}", "-o", "ControlPersist=120",
                    *(ssh_args or []), target]
        self.screens_dir = screens_dir
        self.name = target
        self.procs: list[subprocess.Popen] = []

    def qmp(self, path: str) -> QMP:
        import base64
        code = base64.b64encode(_BRIDGE.encode()).decode()
        proc = subprocess.Popen(self.ssh + [f"python3 -c \"import base64;exec(base64.b64decode('{code}'))\" "
                                            f"'{path}'"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                text=True, encoding="utf-8", bufsize=1)
        self.procs.append(proc)

        class _Pipe:
            def readline(self_inner):
                return proc.stdout.readline()

            def write(self_inner, data):
                proc.stdin.write(data)

            def flush(self_inner):
                proc.stdin.flush()

            def close(self_inner):
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                proc.terminate()
        return QMP(path, stream=_Pipe())

    def screendump_path(self) -> tuple[str, str | None]:
        return f"{self.screens_dir}/rbapps-{os.getpid()}-{time.monotonic_ns()}.ppm", None

    def fetch(self, path: str) -> bytes:
        res = subprocess.run(self.ssh + [f"cat '{path}'"], capture_output=True, timeout=60)
        if res.returncode != 0:
            raise OSError(f"cannot read {path} on {self.target}: {res.stderr.decode(errors='replace')[:200]}")
        return res.stdout

    def remove(self, path: str, d: str | None = None) -> None:
        subprocess.run(self.ssh + [f"rm -f '{path}'"], capture_output=True, timeout=60)

    def run(self, argv: list[str]) -> str:
        import shlex
        return subprocess.run(self.ssh + [" ".join(shlex.quote(a) for a in argv)], capture_output=True,
                              text=True, timeout=60).stdout

    def clock_skew(self) -> float:
        """Remote clock minus this computer's clock (midpoint of an SSH round trip)."""
        best = None
        for _ in range(3):
            t0 = time.time()
            out = self.run(["date", "+%s.%N"]).strip()
            t1 = time.time()
            try:
                skew = float(out) - (t0 + t1) / 2
            except ValueError:
                continue
            if best is None or (t1 - t0) < best[1]:
                best = (skew, t1 - t0)
        return round(best[0], 3) if best else 0.0


@dataclass
class Image:
    width: int
    height: int
    rgb: bytes

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        o = (y * self.width + x) * 3
        return self.rgb[o], self.rgb[o + 1], self.rgb[o + 2]

    def crop(self, x0: int, y0: int, x1: int, y1: int) -> "Image":
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.width, x1), min(self.height, y1)
        rows = [self.rgb[(y * self.width + x0) * 3:(y * self.width + x1) * 3] for y in range(y0, y1)]
        return Image(x1 - x0, y1 - y0, b"".join(rows))

    def scaled(self, k: int) -> "Image":
        if k == 1:
            return self
        out = bytearray()
        w = self.width
        for y in range(self.height):
            row = self.rgb[y * w * 3:(y + 1) * w * 3]
            big = bytearray(w * k * 3)
            for j in range(k):
                for c in range(3):
                    big[j * 3 + c::3 * k] = row[c::3]
            out += bytes(big) * k
        return Image(w * k, self.height * k, bytes(out))

    def png(self) -> bytes:
        row = self.width * 3
        raw = b"".join(b"\x00" + self.rgb[y * row:(y + 1) * row] for y in range(self.height))

        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))

    def ppm(self) -> bytes:
        return b"P6\n%d %d\n255\n" % (self.width, self.height) + self.rgb

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.write_bytes(self.png() if path.suffix == ".png" else self.ppm())
        return path

    def signature(self, step: int = 7) -> int:
        return zlib.crc32(self.rgb[::step * 3 + 1])


def read_ppm(data: bytes) -> Image:
    if not data.startswith(b"P6"):
        raise ValueError("not a binary PPM")
    parts = []
    i = 2
    while len(parts) < 3:
        while data[i:i + 1].isspace():
            i += 1
        if data[i:i + 1] == b"#":
            while data[i:i + 1] not in (b"\n", b""):
                i += 1
            continue
        j = i
        while not data[j:j + 1].isspace():
            j += 1
        parts.append(int(data[i:j]))
        i = j
    i += 1
    w, h, maxval = parts
    if maxval != 255:
        raise ValueError("unsupported PPM depth")
    row = w * 3
    stride = (row + 3) & ~3
    if stride != row and len(data) - i >= h * stride:
        rgb = b"".join(data[i + r * stride:i + r * stride + row] for r in range(h))
    else:
        rgb = data[i:i + h * row]
    return Image(w, h, rgb)


def menu_bar_ready(img: Image) -> bool:
    """Mac OS menu bar drawn: the light top band with dark text (ClassicMac's
    rule, as in emulator/tools/rbd_presenter_stub.analyze_frame) plus the
    colored Apple-menu logo (not drawn on the "Starting Up" desktop)."""
    w, h = img.width, img.height
    if w < 64 or h < 40:
        return False
    rows = min(20, h)
    dark = bright = 0
    for y in range(rows):
        row = img.rgb[y * w * 3:(y + 1) * w * 3]
        for x in range(0, w * 3, 6):
            r, g, b = row[x], row[x + 1], row[x + 2]
            if r < 50 and g < 50 and b < 50:
                dark += 1
            elif r > 170 and g > 170 and b > 170:
                bright += 1
    total = (w // 2) * rows
    band = dark >= min(400, max(100, total // 25)) and bright >= max(1000, total // 2)
    colored = 0
    for y in range(2, 19):
        for x in range(12, 32):
            r, g, b = img.pixel(x, y)
            if max(r, g, b) - min(r, g, b) > 80:
                colored += 1
    return band and colored >= 3


@dataclass
class Word:
    text: str
    x: int
    y: int
    w: int
    h: int
    conf: float
    line: tuple

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


def ocr(img: Image, scale: int = 3, psm: int = 11, region: tuple | None = None) -> list[Word]:
    """Words with boxes in guest coordinates (tesseract on an enlarged copy)."""
    ox = oy = 0
    if region:
        ox, oy = region[0], region[1]
        img = img.crop(*region)
    big = img.scaled(scale)
    with tempfile.NamedTemporaryFile(suffix=".ppm", delete=False) as f:
        f.write(big.ppm())
        path = f.name
    try:
        res = subprocess.run(["tesseract", path, "stdout", "--psm", str(psm), "tsv"],
                             capture_output=True, text=True, timeout=120)
    except FileNotFoundError as exc:
        raise RuntimeError("tesseract is not installed (Debian package tesseract-ocr)") from exc
    finally:
        os.unlink(path)
    words = []
    for line in res.stdout.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 12 or not cols[11].strip():
            continue
        left, top, width, height = (int(c) for c in cols[6:10])
        words.append(Word(cols[11].strip(), ox + left // scale, oy + top // scale, max(1, width // scale),
                          max(1, height // scale), float(cols[10]), (int(cols[2]), int(cols[3]), int(cols[4]))))
    return words


def ocr_tiled(img: Image, scale: int = 3, psm: int = 11, tiles: int = 3) -> list[Word]:
    """OCR in overlapping tiles: tesseract's page layout analysis loses small
    dialogs on busy screens (Speedometer's LED display), but reads them in a
    crop.  Words seen in more than one tile are kept once."""
    words: list[Word] = []
    tw, th = img.width * 2 // (tiles + 1), img.height * 2 // (tiles + 1)
    seen = set()
    for ty in range(tiles):
        for tx in range(tiles):
            x0, y0 = tx * tw // 2, ty * th // 2
            for w in ocr(img, scale=scale, psm=psm, region=(x0, y0, x0 + tw, y0 + th)):
                key = (w.text, w.x // 6, w.y // 6)
                if key not in seen:
                    seen.add(key)
                    words.append(Word(w.text, w.x, w.y, w.w, w.h, w.conf, (ty, tx) + w.line))
    return words


def text_of(words: list[Word]) -> str:
    lines: dict[tuple, list[Word]] = {}
    for w in words:
        lines.setdefault(w.line, []).append(w)
    return "\n".join(" ".join(w.text for w in sorted(ws, key=lambda w: w.x))
                     for _k, ws in sorted(lines.items(), key=lambda kv: (min(w.y for w in kv[1]), kv[0])))


class Guest:
    """Input and screenshots for one guest through a QMP monitor."""

    def __init__(self, qmp_path: str, evidence: Path | None = None, host=None):
        self.host = host or LocalHost()
        self.qmp = self.host.qmp(qmp_path)
        self.evidence = Path(evidence) if evidence else None
        self.size = (1024, 768)
        self.pointer = (0, 0)
        self.held: set[str] = set()

    # -- screen ---------------------------------------------------------------
    def screenshot(self, name: str | None = None) -> Image:
        p, d = self.host.screendump_path()
        try:
            self.qmp.cmd("screendump", filename=p)
            img = read_ppm(self.host.fetch(p))
        finally:
            self.host.remove(p, d)
        self.size = (img.width, img.height)
        if name and self.evidence:
            img.save(self.evidence / (name if name.endswith(".png") else name + ".png"))
        return img

    def wait_still(self, seconds: float = 2.0, timeout: float = 60.0, poll: float = 0.5) -> Image:
        """Wait until the screen has not changed for ``seconds``."""
        deadline = time.monotonic() + timeout
        img = self.screenshot()
        sig, since = img.signature(), time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(poll)
            img = self.screenshot()
            s = img.signature()
            if s != sig:
                sig, since = s, time.monotonic()
            elif time.monotonic() - since >= seconds:
                return img
        return img

    def wait_menu_bar(self, timeout: float = 180.0, poll: float = 1.0) -> Image | None:
        deadline = time.monotonic() + timeout
        self.last_error = None
        while time.monotonic() < deadline:
            try:
                img = self.screenshot()
                if menu_bar_ready(img):
                    return img
            except (QMPError, OSError, ValueError) as exc:
                self.last_error = str(exc)
            time.sleep(poll)
        return None

    # -- pointer --------------------------------------------------------------
    def move(self, x: int, y: int):
        w, h = self.size
        x = max(0, min(w - 1, int(x)))
        y = max(0, min(h - 1, int(y)))
        self.qmp.cmd("input-send-event", events=[
            {"type": "abs", "data": {"axis": "x", "value": x * ABS_MAX // max(1, w - 1)}},
            {"type": "abs", "data": {"axis": "y", "value": y * ABS_MAX // max(1, h - 1)}}])
        self.pointer = (x, y)

    def button(self, down: bool, button: str = "left"):
        self.qmp.cmd("input-send-event", events=[{"type": "btn", "data": {"down": down, "button": button}}])

    def click(self, x: int, y: int, double: bool = False, hold: float = 0.1):
        self.move(x, y)
        time.sleep(0.15)
        for i in range(2 if double else 1):
            self.button(True)
            time.sleep(hold)
            self.button(False)
            time.sleep(0.12)

    def drag(self, x0: int, y0: int, x1: int, y1: int, steps: int = 12, step_delay: float = 0.05):
        self.move(x0, y0)
        time.sleep(0.15)
        self.button(True)
        time.sleep(0.2)
        try:
            for i in range(1, steps + 1):
                self.move(x0 + (x1 - x0) * i // steps, y0 + (y1 - y0) * i // steps)
                time.sleep(step_delay)
            time.sleep(0.2)
        finally:
            self.button(False)

    def press_at(self, x: int, y: int, seconds: float):
        """Hold the button down (menus, pop-ups)."""
        self.move(x, y)
        time.sleep(0.15)
        self.button(True)
        time.sleep(seconds)

    # -- keyboard -------------------------------------------------------------
    @staticmethod
    def qcode(name: str) -> str:
        return KEY_ALIASES.get(name, name)

    def key(self, name: str, down: bool):
        q = self.qcode(name)
        self.qmp.cmd("input-send-event", events=[{"type": "key", "data": {
            "down": down, "key": {"type": "qcode", "data": q}}}])
        (self.held.add if down else self.held.discard)(q)

    def tap(self, *names: str, hold: float = 0.08, gap: float = 0.08):
        """Press keys together (a chord such as tap('command', 'q')), then release."""
        try:
            for n in names:
                self.key(n, True)
                time.sleep(0.03)
            time.sleep(hold)
        finally:
            for n in reversed(names):
                self.key(n, False)
        time.sleep(gap)

    def type_text(self, text: str, gap: float = 0.06):
        for ch in text:
            name, shifted = char_to_key(ch)
            self.tap(*(["shift"] if shifted else []), name, hold=0.05, gap=gap)

    def release_all(self):
        for q in list(self.held):
            try:
                self.key(q, False)
            except (QMPError, OSError):
                pass
        try:
            self.button(False)
        except (QMPError, OSError):
            pass

    # -- OCR helpers ----------------------------------------------------------
    def find_text(self, img: Image, pattern, region=None, scale: int = 3, psm: int = 11) -> Word | None:
        import re
        rx = re.compile(pattern, re.IGNORECASE) if isinstance(pattern, str) else pattern
        for w in ocr(img, scale=scale, psm=psm, region=region):
            if rx.fullmatch(w.text) or rx.search(w.text):
                return w
        return None

    # Mac OS seconds for 2000-01-01 and 2100-01-01: anything else is not the clock
    _TIME_MIN, _TIME_MAX = 3029529600, 6185289600

    def guest_clock(self, attempts: int = 20) -> int:
        """Mac OS's clock: the low-memory global Time (0x20C, seconds since
        1904-01-01 in the Mac's local time), read through the vCPU's current
        translation.  A read taken while the vCPU runs with translation off
        (an exception handler) returns physical page 0 instead, so implausible
        values are discarded and the read repeated."""
        for _ in range(attempts):
            r = self.qmp.cmd("human-monitor-command", **{"command-line": "x /1wx 0x20c"})
            try:
                v = int(r.split(":", 1)[1].split()[0], 16)
            except (IndexError, ValueError):
                v = 0
            if self._TIME_MIN <= v <= self._TIME_MAX:
                return v
            time.sleep(0.01)
        raise QMPError("Mac OS clock (low-memory Time) not readable")

    def clock_offset(self, tz_offset_s: int = 0, timeout: float = 3.0) -> dict:
        """Guest clock minus host clock (seconds), measured at a guest second
        boundary so the result is accurate to about one poll interval."""
        first = self.guest_clock()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            v = self.guest_clock()
            host = time.time()
            if v != first:
                guest_unix = v - 2082844800 - tz_offset_s
                return {"guest_time": v, "host_time": round(host, 3), "offset_s": round(guest_unix - host, 2),
                        "resolution_s": 0.05}
            time.sleep(0.02)
        return {"guest_time": first, "host_time": round(time.time(), 3), "offset_s": None,
                "error": "Mac OS clock did not advance"}

    def close(self):
        self.release_all()
        self.qmp.close()
