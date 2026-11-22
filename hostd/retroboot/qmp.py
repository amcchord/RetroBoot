"""Asynchronous QMP client (hostd uses qmp-control.sock; the launcher uses qmp-events.sock).

Requests carry an ``id`` that QEMU echoes, so responses are correlated even
when events interleave.  Events are delivered to registered callbacks and to
an optional queue.  This module exposes typed helpers only; raw QMP is never
reachable from the management API.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import zlib
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("retroboot.qmp")


class QMPError(Exception):
    def __init__(self, error: dict | str):
        if isinstance(error, dict):
            super().__init__(f"{error.get('class', 'Error')}: {error.get('desc', '')}")
            self.error = error
        else:
            super().__init__(error)
            self.error = {"class": "Disconnected", "desc": error}


class QMPClient:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next = 1
        self._task: asyncio.Task | None = None
        self.event_handlers: list[Callable[[dict], None]] = []
        self.greeting: dict | None = None
        self.closed = asyncio.Event()
        self.events: list[dict] = []

    @property
    def connected(self) -> bool:
        return self.writer is not None and not self.closed.is_set()

    async def connect(self, timeout: float = 10.0, retry_interval: float = 0.1) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last: Exception | None = None
        while True:
            try:
                self.reader, self.writer = await asyncio.open_unix_connection(self.path, limit=16 * 1024 * 1024)
                break
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                last = exc
                if loop.time() >= deadline:
                    raise QMPError(f"cannot connect to QMP: {last}") from exc
                await asyncio.sleep(retry_interval)
        line = await asyncio.wait_for(self.reader.readline(), timeout=max(1.0, deadline - loop.time() + 5))
        if not line:
            raise QMPError("QMP closed before greeting")
        self.greeting = json.loads(line)
        self.closed.clear()
        self._task = asyncio.get_running_loop().create_task(self._read_loop(), name="qmp-reader")
        await self.execute("qmp_capabilities")

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "event" in msg:
                    self.events.append(msg)
                    if len(self.events) > 500:
                        del self.events[:250]
                    for h in list(self.event_handlers):
                        try:
                            h(msg)
                        except Exception:  # pragma: no cover
                            log.exception("QMP event handler failed")
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(QMPError("QMP connection closed"))
            self._pending.clear()

    async def execute(self, command: str, arguments: dict | None = None, timeout: float = 15.0) -> Any:
        if self.writer is None or self.closed.is_set():
            raise QMPError("QMP not connected")
        req_id = self._next
        self._next += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        msg: dict[str, Any] = {"execute": command, "id": req_id}
        if arguments:
            msg["arguments"] = arguments
        self.writer.write((json.dumps(msg) + "\n").encode())
        await self.writer.drain()
        try:
            resp = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise QMPError(f"QMP {command} timed out")
        if "error" in resp:
            raise QMPError(resp["error"])
        return resp.get("return")

    async def close(self) -> None:
        if self.writer is not None:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.closed.set()

    # -- typed helpers ----------------------------------------------------
    async def query_status(self) -> dict:
        return await self.execute("query-status")

    async def stop(self) -> None:
        await self.execute("stop")

    async def cont(self) -> None:
        await self.execute("cont")

    async def quit(self) -> None:
        await self.execute("quit")

    async def send_key(self, *qcodes: str, hold_ms: int = 100) -> None:
        await self.execute("send-key", {"keys": [{"type": "qcode", "data": q} for q in qcodes],
                                        "hold-time": hold_ms})

    async def key_event(self, qcode: str, down: bool) -> None:
        await self.execute("input-send-event", {"events": [
            {"type": "key", "data": {"down": down, "key": {"type": "qcode", "data": qcode}}}]})

    async def tap_key(self, qcode: str, hold_s: float = 0.1) -> None:
        """Press and release with input-send-event; fall back to send-key."""
        try:
            await self.key_event(qcode, True)
            try:
                await asyncio.sleep(hold_s)
            finally:
                await self.key_event(qcode, False)
        except QMPError:
            await self.send_key(qcode, hold_ms=int(hold_s * 1000))

    async def pointer_nudge(self, step: int = 8, hold_s: float = 0.5) -> None:
        """Move the pointer by ``step`` and, ``hold_s`` later, back (relative events): ends a
        screen saver without leaving the pointer anywhere new.  After Dark polls the pointer,
        so the move must last: +1 and back within 50 ms did not end its Flying Toasters in
        Mac OS 9.2.2 (lab), +8 held for 0.5 s ended them within 0.75 s."""
        for v, pause in ((step, hold_s), (-step, 0.0)):
            await self.execute("input-send-event", {"events": [
                {"type": "rel", "data": {"axis": "x", "value": v}},
                {"type": "rel", "data": {"axis": "y", "value": v}}]})
            if pause:
                await asyncio.sleep(pause)

    async def hmp(self, command_line: str) -> str:
        return await self.execute("human-monitor-command", {"command-line": command_line})

    async def screendump(self, filename: str, fmt: str = "png") -> None:
        args = {"filename": filename}
        if fmt != "ppm":
            args["format"] = fmt
        await self.execute("screendump", args, timeout=30.0)


# --- Frame analysis ----------------------------------------------------------

def read_ppm(data: bytes) -> tuple[int, int, bytes]:
    parts = data.split(b"\n", 3)
    if len(parts) < 4 or parts[0] != b"P6" or parts[2] != b"255":
        raise ValueError("unexpected PPM format")
    w, h = (int(v) for v in parts[1].split())
    rgb = parts[3]
    if len(rgb) < w * h * 3:
        raise ValueError("truncated PPM")
    return w, h, rgb[: w * h * 3]


def png_size(data: bytes) -> tuple[int, int]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return struct.unpack(">II", data[16:24])


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """Minimal RGB8 PNG encoder (stdlib only)."""
    raw = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


GRAY_FRACTION = 0.97      # ARCHITECTURE 4.3 (the presenter's rule; used here by the dev content probe)
NONBLACK_FRACTION = 0.25
SETTLED_CHANGE = 0.01


def frame_samples(width: int, height: int, rgb: bytes, side: int = 64) -> bytes:
    """A coarse grid of RGB samples, to tell whether a frame is still changing."""
    if width <= 0 or height <= 0:
        return b""
    out = bytearray()
    for j in range(side):
        y = (j * (height - 1)) // max(1, side - 1)
        for i in range(side):
            o = (y * width + (i * (width - 1)) // max(1, side - 1)) * 3
            out += rgb[o:o + 3]
    return bytes(out)


def samples_changed(a: bytes | None, b: bytes) -> float:
    if not a or len(a) != len(b):
        return 1.0
    n = len(b) // 3
    diff = sum(1 for k in range(0, len(b), 3) if a[k:k + 3] != b[k:k + 3])
    return diff / n if n else 0.0


def _block_mean(width: int, height: int, rgb: bytes, x: int, y: int) -> tuple[int, int, int]:
    x0 = x if x + 1 < width else max(0, x - 1)
    y0 = y if y + 1 < height else max(0, y - 1)
    xs = (x0, min(width - 1, x0 + 1))
    ys = (y0, min(height - 1, y0 + 1))
    acc = [0, 0, 0]
    for yy in ys:
        for xx in xs:
            o = (yy * width + xx) * 3
            acc[0] += rgb[o]
            acc[1] += rgb[o + 1]
            acc[2] += rgb[o + 2]
    return (acc[0] + 2) // 4, (acc[1] + 2) // 4, (acc[2] + 2) // 4


def genuine_content(width: int, height: int, rgb: bytes, samples: int = 4096) -> tuple[bool, float, float]:
    """ARCHITECTURE 4.3 sampling rule on one frame.

    Returns (startup_gray_rule, gray_fraction, nonblack_fraction): whether at
    least 97 % of the samples are one non-black gray level.  A sample is the
    mean of its 2x2 block, so Mac OS's 1-pixel 50 % dither (its startup
    background on a built-in LCD) reads as its mean gray.  The caller adds
    the settling condition (the frame must not be changing: unchanged on the
    next inspection for the gray rule, at most 1 % changed over >= 500 ms for
    the non-black rule).
    """
    if width <= 0 or height <= 0:
        return False, 0.0, 0.0
    side = max(1, int(samples ** 0.5))
    nx = min(width, side + 1)
    ny = min(height, side + 1)
    hist: dict[int, int] = {}
    nonblack = 0
    total = 0
    for j in range(ny):
        y = (j * (height - 1)) // max(1, ny - 1)
        for i in range(nx):
            x = (i * (width - 1)) // max(1, nx - 1)
            r, g, b = _block_mean(width, height, rgb, x, y)
            total += 1
            if max(r, g, b) > 16:
                nonblack += 1
            if abs(r - g) <= 12 and abs(g - b) <= 12 and abs(r - b) <= 12 and max(r, g, b) > 24:
                level = (r + g + b) // 3
                hist[level] = hist.get(level, 0) + 1
    best = 0
    for level in hist:
        near = sum(c for lv, c in hist.items() if abs(lv - level) <= 12)
        best = max(best, near)
    gray_frac = best / total if total else 0.0
    nb_frac = nonblack / total if total else 0.0
    return gray_frac >= GRAY_FRACTION, gray_frac, nb_frac


FINDER_DARK_PIXELS = 800


def finder_menu_bar(width: int, rgb: bytes) -> bool:
    """Finder heuristic adapted from ClassicMac: a bright menu bar band with dark menu titles.

    Rows 0-17 only: the menu bar's black bottom border (row 19 on Mac OS 9) would
    otherwise satisfy the dark-pixel count while the menu bar is still empty.  Dark
    pixels must also be spread over at least a third of the width (titles from the
    Apple menu to the clock), which an arrow cursor alone never satisfies, and the band
    must be mostly bright (a 50 % dither is not a menu bar).
    """
    band_height = 18
    band = memoryview(rgb)[: width * band_height * 3]
    dark = bright = 0
    columns: set[int] = set()
    bucket = max(1, width // 32)
    for off in range(0, len(band), 3):
        r, g, b = band[off], band[off + 1], band[off + 2]
        if max(r, g, b) < 50:
            dark += 1
            columns.add(((off // 3) % width) // bucket)
        if min(r, g, b) > 170:
            bright += 1
    total = width * band_height
    dark_required = min(FINDER_DARK_PIXELS, max(200, total // 25))
    # A menu bar is mostly white (Finder at 960x540: 6 % dark, 90 % bright).  Mac OS's
    # startup background on a built-in LCD (the PowerBook 50 % dither, r11 laptops) is half
    # dark and half bright in every row; with "bright >= half" it read as the Finder 0.3 s
    # after the first content.
    bright_required = max(2000, total * 3 // 5)
    dark_allowed = total * 2 // 5
    return (dark_required <= dark <= dark_allowed and bright >= bright_required
            and len(columns) >= 11)
