"""Client of the presenter's remote screen socket (ARCHITECTURE section 4.6).

``/run/retroboot/remote.sock`` is SOCK_SEQPACKET.  The presenter shares a
sealed memfd (the tap buffer) per surface generation and changes it only
right before it answers a REQUEST, so the buffer is stable from the moment
every REQUEST has been answered until the next one is sent.  ``update()``
keeps that invariant: it returns only when nothing is outstanding.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import mmap
import os
import socket
import struct
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("retroboot.remote")

VERSION = 1
HELLO, REQUEST, SOURCE, SURFACE, UPDATE = 1, 2, 3, 4, 5
KEY, POINTER, BUTTON, RELEASE_ALL, VIEWERS = 16, 17, 18, 20, 21
SURFACE_LEN = 48
KINDS = {0: "none", 1: "guest", 2: "frame", 3: "checkerboard", 4: "black"}
SOURCES = {"composed": 0, "guest": 1}
OUTQ_MAX = 4096   # messages waiting for the presenter to read; beyond that it is stuck


class TapClosed(Exception):
    pass


@dataclass
class Surface:
    gen: int
    kind: str
    w: int
    h: int
    stride: int
    view: str
    buf: mmap.mmap | None

    @property
    def picture(self) -> bool:
        return self.buf is not None and self.w > 0 and self.h > 0


def pack(mtype: int, *args: int) -> bytes:
    return struct.pack(f"=II{len(args)}I", mtype, 4 * len(args), *[a & 0xFFFFFFFF for a in args])


def parse_surface(payload: bytes) -> tuple[int, int, int, int, int, int, int, str]:
    gen, kind, w, h, stride, fmt = struct.unpack_from("=6I", payload, 0)
    (size,) = struct.unpack_from("=Q", payload, 24)
    view = payload[32:48].split(b"\0", 1)[0].decode("ascii", "replace")
    return gen, kind, w, h, stride, fmt, size, view


def parse_update(payload: bytes) -> tuple[int, int, list[tuple[int, int, int, int]]]:
    seq, gen, count = struct.unpack_from("=3I", payload, 0)
    if len(payload) != 12 + 16 * count:
        raise ValueError("UPDATE length mismatch")
    rects = [struct.unpack_from("=4i", payload, 12 + 16 * i) for i in range(count)]
    return seq, gen, [r for r in rects if r[2] > 0 and r[3] > 0]


class TapClient:
    def __init__(self, path: str, on_surface: Callable[[Surface], None], on_close: Callable[[], None]):
        self.path = path
        self.on_surface = on_surface
        self.on_close = on_close
        self.sock: socket.socket | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._hello: asyncio.Future | None = None
        self._waiter: asyncio.Future | None = None
        self._outstanding = 0
        self._nudged = False
        self._acc: list[tuple[int, int, int, int]] = []
        self._seq = 0
        self.surface: Surface | None = None
        # Messages the socket could not take yet, in order.  Nothing is dropped:
        # a lost key or button release would leave it held in Mac OS.
        self._outq: collections.deque[tuple[int | None, bytes]] = collections.deque()
        self._writing = False
        self.stats = {"requests": 0, "updates": 0, "rects": 0, "surfaces": 0, "queued": 0}

    @property
    def connected(self) -> bool:
        return self.sock is not None

    @property
    def waiting(self) -> bool:
        return self._waiter is not None and not self._waiter.done()

    async def connect(self, source: str = "composed", timeout: float = 3.0) -> None:
        self.loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC)
        sock.setblocking(False)
        try:
            await asyncio.wait_for(self.loop.sock_connect(sock, self.path), timeout)
        except BaseException:
            sock.close()
            raise
        self.sock = sock
        self._hello = self.loop.create_future()
        self.loop.add_reader(sock.fileno(), self._readable)
        try:
            self._send(pack(HELLO, VERSION, SOURCES.get(source, 0)))
            await asyncio.wait_for(self._hello, timeout)
        except BaseException:
            self.close("handshake failed")
            raise

    def close(self, why: str = "closed") -> None:
        sock, self.sock = self.sock, None
        if sock is None:
            return
        try:
            self.loop.remove_reader(sock.fileno())
            if self._writing:
                self.loop.remove_writer(sock.fileno())
        except Exception:
            pass
        self._writing = False
        self._outq.clear()
        sock.close()
        self._outstanding = 0
        self._nudged = False
        if self._waiter and not self._waiter.done():
            self._waiter.set_exception(TapClosed(why))
        if self._hello and not self._hello.done():
            self._hello.set_exception(TapClosed(why))
        log.info("remote screen tap closed (%s)", why)
        self.on_close()

    # -- outgoing ------------------------------------------------------------------
    def _send(self, data: bytes, coalesce: int | None = None) -> bool:
        """Send in order; queue what the socket cannot take now.  coalesce: a message type
        whose newest copy replaces a queued one of the same type at the end (pointer moves)."""
        if self.sock is None:
            return False
        if not self._outq:
            try:
                self.sock.send(data)
                return True
            except BlockingIOError:
                pass
            except OSError as exc:
                self.close(str(exc))
                return False
        if coalesce is not None and self._outq and self._outq[-1][0] == coalesce:
            self._outq[-1] = (coalesce, data)
            return True
        if len(self._outq) >= OUTQ_MAX:
            # The presenter stopped reading.  Reconnecting makes it release every remote key.
            self.close("the presenter is not reading")
            return False
        self._outq.append((coalesce, data))
        self.stats["queued"] += 1
        if not self._writing:
            self._writing = True
            self.loop.add_writer(self.sock.fileno(), self._writable)
        return True

    def _writable(self) -> None:
        while self._outq and self.sock is not None:
            try:
                self.sock.send(self._outq[0][1])
            except BlockingIOError:
                return
            except OSError as exc:
                self.close(str(exc))
                return
            self._outq.popleft()
        if self.sock is not None and self._writing:
            self.loop.remove_writer(self.sock.fileno())
        self._writing = False

    def _request(self, wait_ms: int) -> None:
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        self._outstanding += 1
        self.stats["requests"] += 1
        if not self._send(pack(REQUEST, self._seq, max(0, min(5000, int(wait_ms))))):
            self.close("request failed")

    async def update(self, wait_ms: int) -> list[tuple[int, int, int, int]]:
        """Ask for the next update and wait until every request is answered.
        Returns the changed rectangles (tap buffer pixels of the current surface)."""
        if self.sock is None:
            raise TapClosed("not connected")
        self._acc = []
        self._nudged = False
        self._waiter = self.loop.create_future()
        self._request(wait_ms)
        try:
            await self._waiter
        finally:
            self._waiter = None
        rects, self._acc = self._acc, []
        return rects

    def nudge(self) -> None:
        """Answer the held request now (a viewer became ready while the screen is still)."""
        if self.waiting and not self._nudged and self.sock is not None:
            self._nudged = True
            self._request(0)

    def source(self, source: str) -> None:
        self._send(pack(SOURCE, SOURCES.get(source, 0)))

    def key(self, code: int, down: bool) -> None:
        self._send(pack(KEY, code, 1 if down else 0))

    def pointer(self, gen: int, x: int, y: int) -> None:
        self._send(pack(POINTER, gen, x, y), coalesce=POINTER)

    def button(self, button: int, down: bool) -> None:
        self._send(pack(BUTTON, button, 1 if down else 0))

    def release_all(self) -> None:
        self._send(pack(RELEASE_ALL))

    def viewers(self, viewers: int, controllers: int) -> None:
        self._send(pack(VIEWERS, viewers, controllers))

    # -- incoming ------------------------------------------------------------------
    def _readable(self) -> None:
        while self.sock is not None:
            try:
                data, anc, flags, _ = self.sock.recvmsg(4096, socket.CMSG_SPACE(4 * 4))
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                self.close(str(exc))
                return
            fds: list[int] = []
            for level, ctype, cdata in anc:
                if level == socket.SOL_SOCKET and ctype == socket.SCM_RIGHTS:
                    n = len(cdata) - len(cdata) % 4
                    fds.extend(struct.unpack(f"={n // 4}i", cdata[:n]))
            if not data:
                for fd in fds:
                    os.close(fd)
                self.close("closed by the presenter")
                return
            try:
                self._handle(data, fds)
            except (ValueError, struct.error, OSError) as exc:
                log.warning("remote screen: bad message from the presenter: %s", exc)
            finally:
                for fd in fds:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def _handle(self, data: bytes, fds: list[int]) -> None:
        if len(data) < 8:
            raise ValueError("short message")
        mtype, length = struct.unpack_from("=II", data, 0)
        payload = data[8:]
        if length != len(payload):
            raise ValueError("length mismatch")
        if mtype == HELLO:
            if self._hello and not self._hello.done():
                self._hello.set_result(struct.unpack_from("=I", payload, 0)[0] if len(payload) >= 4 else 0)
        elif mtype == SURFACE:
            if len(payload) != SURFACE_LEN:
                raise ValueError("SURFACE length")
            gen, kind, w, h, stride, _fmt, size, view = parse_surface(payload)
            buf = None
            if kind and fds:
                if w <= 0 or h <= 0 or w > 8192 or h > 8192 or stride < w * 4 or size < stride * h:
                    raise ValueError("SURFACE geometry")
                buf = mmap.mmap(fds[0], size, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ)
            self.surface = Surface(gen, KINDS.get(kind, "none"), w if buf else 0, h if buf else 0, stride, view, buf)
            self.stats["surfaces"] += 1
            self.on_surface(self.surface)
        elif mtype == UPDATE:
            seq, gen, rects = parse_update(payload)
            self.stats["updates"] += 1
            self.stats["rects"] += len(rects)
            if self.surface is not None and gen == self.surface.gen:
                self._acc.extend(rects)
            if self._outstanding > 0:
                self._outstanding -= 1
            if self._outstanding == 0 and self._waiter and not self._waiter.done():
                self._waiter.set_result(None)
