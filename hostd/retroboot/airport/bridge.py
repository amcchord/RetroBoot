"""The AirPort bridge socket (ARCHITECTURE §10): hostd listens, QEMU connects.

``AF_UNIX`` ``SOCK_STREAM``, newline-delimited JSON objects (UTF-8, at most
64 KiB per line).  One QEMU connection at a time; a new connection replaces
the old one.  The card is a reconnecting chardev client, so hostd restarts
and QEMU restarts are both tolerated.  Writes never block hostd: messages to
a peer that stopped reading are dropped beyond a small queue.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("retroboot.airport")

PROTOCOL_VERSION = 1
LINE_MAX = 65536
QUEUE_MAX = 256

GUEST_TYPES = {"hello", "state", "scan", "join", "leave"}


def validate(msg: object) -> dict | None:
    """Return a normalized card message or None (ignored, as the protocol requires)."""
    if not isinstance(msg, dict) or msg.get("type") not in GUEST_TYPES:
        return None
    t = msg["type"]
    out: dict = {"type": t}
    if t in ("scan", "join"):
        seq = msg.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or not 0 <= seq < 2 ** 32:
            return None
        out["seq"] = seq
    if t in ("join", "state"):
        key = "ssid" if t == "join" else "desired"
        raw = msg.get(key, "")
        try:
            b = bytes.fromhex(raw) if isinstance(raw, str) else None
        except ValueError:
            b = None
        if b is None or len(b) > 32:
            return None
        out[key] = b
    if t == "join":
        out["wep"] = bool(msg.get("wep", False))
    if t == "state":
        out["enabled"] = bool(msg.get("enabled", False))
        out["mode"] = "ibss" if msg.get("mode") == "ibss" else "bss"
    if t == "hello":
        out["version"] = msg.get("version") if isinstance(msg.get("version"), int) else 0
        mac = msg.get("mac")
        out["mac"] = mac if isinstance(mac, str) and len(mac) == 17 else None
        out["firmware"] = msg.get("firmware") if isinstance(msg.get("firmware"), str) else None
    return out


class Connection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, serial: int):
        self.reader, self.writer, self.serial = reader, writer, serial
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(QUEUE_MAX)
        self.closed = False
        self.dropped = 0

    def send(self, obj: dict) -> None:
        if self.closed:
            return
        data = json.dumps(obj, separators=(",", ":")).encode() + b"\n"
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            self.dropped += 1

    async def writer_loop(self) -> None:
        try:
            while True:
                data = await self.queue.get()
                self.writer.write(data)
                await self.writer.drain()
        except (ConnectionError, asyncio.CancelledError, RuntimeError):
            pass

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.writer.close()
            except RuntimeError:
                pass


class BridgeServer:
    """Accepts the card; hands validated messages to ``on_message``."""

    def __init__(self, path: Path, on_message: Callable[[Connection, dict], Awaitable[None]],
                 on_connect: Callable[[Connection], Awaitable[None]],
                 on_disconnect: Callable[[Connection], Awaitable[None]],
                 group_gid: int | None = None):
        self.path = Path(path)
        self.on_message, self.on_connect, self.on_disconnect = on_message, on_connect, on_disconnect
        self.group_gid = group_gid
        self.server: asyncio.AbstractServer | None = None
        self.current: Connection | None = None
        self.connections = 0

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.server = await asyncio.start_unix_server(self._client, path=str(self.path), limit=LINE_MAX)
        # QEMU runs as rbguest: owner root, group rbguest, no access for others.
        try:
            if self.group_gid is not None and os.geteuid() == 0:
                os.chown(self.path, 0, self.group_gid)
                os.chmod(self.path, 0o660)
            elif os.geteuid() != 0:
                os.chmod(self.path, 0o600)
        except OSError as exc:
            log.warning("airport socket permissions: %s", exc)

    async def stop(self) -> None:
        if self.current:
            self.current.close()
        if self.server:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 2)
            except asyncio.TimeoutError:
                pass
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def send(self, obj: dict) -> bool:
        if self.current and not self.current.closed:
            self.current.send(obj)
            return True
        return False

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        conn = Connection(reader, writer, self.connections)
        old, self.current = self.current, conn
        if old:
            old.close()
        wtask = asyncio.get_running_loop().create_task(conn.writer_loop())
        try:
            await self.on_connect(conn)
            while not conn.closed:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    log.warning("airport bridge: oversized line; closing")
                    break
                except ConnectionError:
                    break
                if not line:
                    break
                try:
                    msg = validate(json.loads(line))
                except ValueError:
                    msg = None
                if msg is None:
                    continue
                try:
                    await self.on_message(conn, msg)
                except Exception:
                    log.exception("airport bridge message failed")
        finally:
            wtask.cancel()
            conn.close()
            if self.current is conn:
                self.current = None
                await self.on_disconnect(conn)
