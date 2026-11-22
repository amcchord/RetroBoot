"""A small asyncio D-Bus client (standard library only).

Enough of the D-Bus wire protocol for hostd to talk to iio-sensor-proxy
(orientation.py) and for lab mocks of such services: EXTERNAL
authentication on a unix socket, method calls and replies, signals, match
rules and name requests.  Marshalling covers the basic types, arrays,
structs, dict entries and variants; file descriptors are not supported.

    conn = await Connection.connect()          # the system bus
    names = await conn.call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                            "org.freedesktop.DBus", "ListNames")
"""
from __future__ import annotations

import asyncio
import os
import struct
from dataclasses import dataclass, field
from typing import Any, Callable

SYSTEM_BUS = "unix:path=/run/dbus/system_bus_socket"
METHOD_CALL, METHOD_RETURN, ERROR, SIGNAL = 1, 2, 3, 4
NO_REPLY_EXPECTED = 0x1
_FIELDS = {1: "path", 2: "interface", 3: "member", 4: "error_name", 5: "reply_serial", 6: "destination",
           7: "sender", 8: "signature"}
_FIELD_SIG = {1: "o", 2: "s", 3: "s", 4: "s", 5: "u", 6: "s", 7: "s", 8: "g"}
_ALIGN = {"y": 1, "b": 4, "n": 2, "q": 2, "i": 4, "u": 4, "x": 8, "t": 8, "d": 8, "s": 4, "o": 4, "g": 1,
          "a": 4, "(": 8, "{": 8, "v": 1, "h": 4}
_FMT = {"y": "B", "b": "I", "n": "h", "q": "H", "i": "i", "u": "I", "x": "q", "t": "Q", "d": "d", "h": "I"}


class DBusError(Exception):
    def __init__(self, name: str, message: str = ""):
        super().__init__(f"{name}: {message}" if message else name)
        self.name = name
        self.message = message


@dataclass
class Message:
    type: int
    serial: int = 0
    flags: int = 0
    path: str | None = None
    interface: str | None = None
    member: str | None = None
    error_name: str | None = None
    reply_serial: int | None = None
    destination: str | None = None
    sender: str | None = None
    signature: str = ""
    body: list = field(default_factory=list)


class Variant:
    """A value with an explicit signature (for marshalling 'v')."""

    def __init__(self, signature: str, value: Any):
        self.signature = signature
        self.value = value

    def __repr__(self) -> str:
        return f"Variant({self.signature!r}, {self.value!r})"


def split_signature(sig: str) -> list[str]:
    """Complete types of a signature: 'sa{sv}as' -> ['s', 'a{sv}', 'as']."""
    out, i = [], 0
    while i < len(sig):
        j = _complete(sig, i)
        out.append(sig[i:j])
        i = j
    return out


def _complete(sig: str, i: int) -> int:
    c = sig[i]
    if c == "a":
        return _complete(sig, i + 1)
    if c in "({":
        close = ")" if c == "(" else "}"
        j = i + 1
        while sig[j] != close:
            j = _complete(sig, j)
        return j + 1
    if c in _ALIGN:
        return i + 1
    raise ValueError(f"bad signature {sig!r}")


def _guess_signature(v: Any) -> str:
    if isinstance(v, Variant):
        return "v"
    if isinstance(v, bool):
        return "b"
    if isinstance(v, int):
        return "i"
    if isinstance(v, float):
        return "d"
    if isinstance(v, str):
        return "s"
    raise ValueError(f"cannot guess a D-Bus type for {v!r}")


class _Writer:
    def __init__(self, endian: str = "<"):
        self.b = bytearray()
        self.e = endian

    def align(self, n: int) -> None:
        self.b += b"\0" * (-len(self.b) % n)

    def put(self, t: str, v: Any) -> None:
        c = t[0]
        self.align(_ALIGN[c])
        if c in _FMT:
            self.b += struct.pack(self.e + _FMT[c], int(bool(v)) if c == "b" else v)
        elif c in "so":
            data = v.encode()
            self.b += struct.pack(self.e + "I", len(data)) + data + b"\0"
        elif c == "g":
            data = v.encode()
            self.b += bytes([len(data)]) + data + b"\0"
        elif c == "v":
            sig, val = (v.signature, v.value) if isinstance(v, Variant) else (_guess_signature(v), v)
            self.put("g", sig)
            self.put(sig, val)
        elif c == "a":
            elem = t[1:]
            self.b += b"\0\0\0\0"
            at = len(self.b) - 4
            self.align(_ALIGN[elem[0]])
            start = len(self.b)
            items = v.items() if elem[0] == "{" and isinstance(v, dict) else v
            for item in items:
                self.put(elem, item)
            struct.pack_into(self.e + "I", self.b, at, len(self.b) - start)
        elif c in "({":
            for sub, item in zip(split_signature(t[1:-1]), v):
                self.put(sub, item)
        else:
            raise ValueError(f"cannot marshal {t!r}")


class _Reader:
    def __init__(self, data: bytes, endian: str, offset: int = 0):
        self.d = data
        self.e = endian
        self.i = offset

    def align(self, n: int) -> None:
        self.i += -self.i % n

    def get(self, t: str) -> Any:
        c = t[0]
        self.align(_ALIGN[c])
        if c in _FMT:
            f = self.e + _FMT[c]
            (v,) = struct.unpack_from(f, self.d, self.i)
            self.i += struct.calcsize(f)
            return bool(v) if c == "b" else v
        if c in "so":
            (n,) = struct.unpack_from(self.e + "I", self.d, self.i)
            v = self.d[self.i + 4:self.i + 4 + n].decode(errors="replace")
            self.i += 4 + n + 1
            return v
        if c == "g":
            n = self.d[self.i]
            v = self.d[self.i + 1:self.i + 1 + n].decode()
            self.i += 1 + n + 1
            return v
        if c == "v":
            sig = self.get("g")
            return self.get(sig)
        if c == "a":
            (n,) = struct.unpack_from(self.e + "I", self.d, self.i)
            self.i += 4
            elem = t[1:]
            self.align(_ALIGN[elem[0]])
            end = self.i + n
            items = []
            while self.i < end:
                items.append(self.get(elem))
            return dict(items) if elem[0] == "{" else items
        if c in "({":
            return tuple(self.get(sub) for sub in split_signature(t[1:-1]))
        raise ValueError(f"cannot unmarshal {t!r}")


def encode(msg: Message) -> bytes:
    body = _Writer()
    for t, v in zip(split_signature(msg.signature), msg.body):
        body.put(t, v)
    fields = []
    for code, name in _FIELDS.items():
        v = msg.signature if name == "signature" else getattr(msg, name)
        if v:
            fields.append((code, Variant(_FIELD_SIG[code], v)))
    h = _Writer()
    h.b += struct.pack("<cBBBII", b"l", msg.type, msg.flags, 1, len(body.b), msg.serial)
    h.put("a(yv)", fields)
    h.align(8)
    return bytes(h.b) + bytes(body.b)


def decode(data: bytes) -> tuple[Message | None, int]:
    """(message, bytes used) or (None, 0) when data holds no complete message."""
    if len(data) < 16:
        return None, 0
    endian = "<" if data[0:1] == b"l" else ">"
    mtype, flags, _ver, blen, serial, flen = struct.unpack_from(endian + "BBBIII", data, 1)
    hlen = 16 + flen
    hlen += -hlen % 8
    if len(data) < hlen + blen:
        return None, 0
    r = _Reader(data, endian, 12)
    fields = r.get("a(yv)")
    msg = Message(type=mtype, serial=serial, flags=flags)
    for code, value in fields:
        if code in _FIELDS:
            setattr(msg, _FIELDS[code], value)
    msg.signature = msg.signature or ""
    body = _Reader(data[hlen:hlen + blen], endian)
    msg.body = [body.get(t) for t in split_signature(msg.signature)]
    return msg, hlen + blen


def _socket_path(address: str) -> str:
    for part in address.split(";"):
        if part.startswith("unix:"):
            kv = dict(p.split("=", 1) for p in part[5:].split(",") if "=" in p)
            if "path" in kv:
                return kv["path"]
            if "abstract" in kv:
                return "\0" + kv["abstract"]
    raise DBusError("org.freedesktop.DBus.Error.BadAddress", address)


class Connection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.serial = 0
        self.unique_name: str | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self.signal_handlers: list[Callable[[Message], Any]] = []
        self.method_handler: Callable[[Message], Any] | None = None
        self._task: asyncio.Task | None = None
        self.closed = asyncio.Event()

    @classmethod
    async def connect(cls, address: str | None = None, timeout: float = 3.0) -> "Connection":
        address = address or os.environ.get("DBUS_SYSTEM_BUS_ADDRESS") or SYSTEM_BUS
        path = _socket_path(address)
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path), timeout)
        writer.write(b"\0AUTH EXTERNAL " + str(os.getuid()).encode().hex().encode() + b"\r\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
        if not line.startswith(b"OK "):
            writer.close()
            raise DBusError("org.freedesktop.DBus.Error.AuthFailed", line.decode(errors="replace").strip())
        writer.write(b"BEGIN\r\n")
        conn = cls(reader, writer)
        conn._task = asyncio.get_running_loop().create_task(conn._read_loop())
        (conn.unique_name,) = await conn.call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                              "org.freedesktop.DBus", "Hello", timeout=timeout)
        return conn

    def _send(self, msg: Message) -> int:
        self.serial += 1
        msg.serial = self.serial
        self.writer.write(encode(msg))
        return msg.serial

    async def call(self, destination: str, path: str, interface: str, member: str, signature: str = "",
                   args: list | tuple = (), timeout: float = 5.0) -> list:
        fut = asyncio.get_running_loop().create_future()
        serial = self._send(Message(METHOD_CALL, path=path, interface=interface, member=member,
                                    destination=destination, signature=signature, body=list(args)))
        self._pending[serial] = fut
        try:
            await self.writer.drain()
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(serial, None)

    async def add_match(self, rule: str) -> None:
        await self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch",
                        "s", [rule])

    async def request_name(self, name: str) -> int:
        (r,) = await self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                               "RequestName", "su", [name, 4])   # DBUS_NAME_FLAG_DO_NOT_QUEUE
        return r

    def emit(self, path: str, interface: str, member: str, signature: str = "", args: list | tuple = ()) -> None:
        self._send(Message(SIGNAL, path=path, interface=interface, member=member, signature=signature,
                           body=list(args)))

    def reply(self, call: Message, signature: str = "", args: list | tuple = ()) -> None:
        self._send(Message(METHOD_RETURN, reply_serial=call.serial, destination=call.sender, signature=signature,
                           body=list(args)))

    def reply_error(self, call: Message, name: str, text: str = "") -> None:
        self._send(Message(ERROR, reply_serial=call.serial, destination=call.sender, error_name=name,
                           signature="s" if text else "", body=[text] if text else []))

    async def _read_loop(self) -> None:
        buf = b""
        try:
            while True:
                chunk = await self.reader.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    msg, used = decode(buf)
                    if msg is None:
                        break
                    buf = buf[used:]
                    self._dispatch(msg)
        except (ConnectionError, OSError, ValueError, struct.error):
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(DBusError("org.freedesktop.DBus.Error.Disconnected", "connection closed"))
            self.closed.set()

    def _dispatch(self, msg: Message) -> None:
        if msg.type in (METHOD_RETURN, ERROR):
            fut = self._pending.get(msg.reply_serial or 0)
            if fut and not fut.done():
                if msg.type == ERROR:
                    fut.set_exception(DBusError(msg.error_name or "error", msg.body[0] if msg.body else ""))
                else:
                    fut.set_result(msg.body)
        elif msg.type == SIGNAL:
            for h in list(self.signal_handlers):
                h(msg)
        elif msg.type == METHOD_CALL and self.method_handler:
            self.method_handler(msg)

    def close(self) -> None:
        if self._task:
            self._task.cancel()
        try:
            self.writer.close()
        except Exception:
            pass
