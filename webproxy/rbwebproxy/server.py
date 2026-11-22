"""A small HTTP/1.0 and 1.1 server for 1999-era browsers.

Written on asyncio streams rather than a framework so the helper accepts
exactly what old browsers send: origin-form requests with a Host header (the
transparent path), absolute-form requests (a browser configured to use a
proxy), ``CONNECT``, HTTP/1.0 with ``Keep-Alive``/``Proxy-Connection``, bare
LF line endings.  Responses always carry a Content-Length, or close the
connection after a body of unknown length (no chunked encoding to old
clients).
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

log = logging.getLogger("rbwebproxy.server")

MAX_LINE = 16 * 1024
MAX_HEADERS = 200
MAX_HEADER_BYTES = 128 * 1024
IDLE_S = 30.0
REASONS = {200: "OK", 204: "No Content", 206: "Partial Content", 301: "Moved Permanently", 302: "Found",
           303: "See Other", 304: "Not Modified", 307: "Temporary Redirect", 308: "Permanent Redirect",
           400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
           413: "Payload Too Large", 414: "URI Too Long", 431: "Request Header Fields Too Large",
           500: "Internal Server Error", 501: "Not Implemented", 502: "Bad Gateway",
           503: "Service Unavailable", 504: "Gateway Timeout"}


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Request:
    method: str
    target: str
    version: str
    headers: list[tuple[str, str]]
    body: bytes = b""
    peer: tuple | None = None
    original_dst: tuple | None = None

    def header(self, name: str, default: str | None = None) -> str | None:
        n = name.lower()
        for k, v in self.headers:
            if k.lower() == n:
                return v
        return default

    def all(self, name: str) -> list[str]:
        n = name.lower()
        return [v for k, v in self.headers if k.lower() == n]

    @property
    def http10(self) -> bool:
        return self.version == "HTTP/1.0"

    @property
    def absolute(self) -> bool:
        return "://" in self.target[:16]

    def wants_keep_alive(self) -> bool:
        tokens = ",".join(self.all("Connection") + self.all("Proxy-Connection")).lower()
        if "close" in tokens:
            return False
        return "keep-alive" in tokens if self.http10 else True


@dataclass
class Response:
    status: int = 200
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes | None = b""
    stream: AsyncIterator[bytes] | None = None
    length: int | None = None
    close: bool = False
    on_done: Callable[[], Awaitable[None]] | None = None

    def set(self, name: str, value: str) -> None:
        n = name.lower()
        self.headers = [(k, v) for k, v in self.headers if k.lower() != n]
        self.headers.append((name, value))

    def get(self, name: str) -> str | None:
        n = name.lower()
        for k, v in self.headers:
            if k.lower() == n:
                return v
        return None


def original_dst(sock: socket.socket | None) -> tuple | None:
    """The address Mac OS meant to reach before the redirect (IPv4 SO_ORIGINAL_DST)."""
    if sock is None:
        return None
    try:
        raw = sock.getsockopt(socket.SOL_IP, 80, 16)
        port, a, b, c, d = struct.unpack("!2xH4B8x", raw)
        return (f"{a}.{b}.{c}.{d}", port)
    except (OSError, struct.error):
        return None


async def _read_line(reader: asyncio.StreamReader, timeout: float) -> bytes:
    try:
        return await asyncio.wait_for(reader.readuntil(b"\n"), timeout)
    except asyncio.IncompleteReadError as e:
        return e.partial
    except asyncio.LimitOverrunError:
        raise HTTPError(431, "A request line is too long.")


async def read_request(reader: asyncio.StreamReader, idle: float, max_body: int) -> Request | None:
    line = b""
    for _ in range(4):                      # tolerate stray blank lines between requests
        line = await _read_line(reader, idle)
        if not line:
            return None
        if line.strip():
            break
    else:
        return None
    if len(line) > MAX_LINE:
        raise HTTPError(414, "The address is too long.")
    parts = line.decode("latin-1").strip().split()
    if len(parts) == 2:                     # HTTP/0.9 style "GET /"
        parts.append("HTTP/1.0")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise HTTPError(400, "Malformed request line.")
    method, target, version = parts[0].upper(), parts[1], parts[2]
    headers: list[tuple[str, str]] = []
    total = 0
    while True:
        h = await _read_line(reader, 20.0)
        if not h or h in (b"\r\n", b"\n"):
            break
        total += len(h)
        if total > MAX_HEADER_BYTES or len(headers) >= MAX_HEADERS:
            raise HTTPError(431, "Too many request headers.")
        text = h.decode("latin-1").rstrip("\r\n")
        if text[:1] in (" ", "\t") and headers:      # obsolete line folding
            k, v = headers[-1]
            headers[-1] = (k, v + " " + text.strip())
            continue
        if ":" not in text:
            continue
        k, v = text.split(":", 1)
        headers.append((k.strip(), v.strip()))
    req = Request(method, target, "HTTP/1.0" if version == "HTTP/1.0" else "HTTP/1.1", headers)
    te = (req.header("Transfer-Encoding") or "").lower()
    if "chunked" in te:
        req.body = await _read_chunked(reader, max_body)
    else:
        cl = req.header("Content-Length")
        if cl:
            try:
                n = int(cl)
            except ValueError:
                raise HTTPError(400, "Bad Content-Length.")
            if n < 0 or n > max_body:
                raise HTTPError(413, "The upload is too large.")
            try:
                req.body = await asyncio.wait_for(reader.readexactly(n), 120.0)
            except asyncio.IncompleteReadError:
                raise HTTPError(400, "The upload ended early.")
    return req


async def _read_chunked(reader: asyncio.StreamReader, max_body: int) -> bytes:
    out = bytearray()
    while True:
        size_line = await _read_line(reader, 60.0)
        try:
            size = int(size_line.split(b";")[0].strip(), 16)
        except ValueError:
            raise HTTPError(400, "Bad chunked upload.")
        if size == 0:
            while (await _read_line(reader, 20.0)).strip():
                pass
            return bytes(out)
        if len(out) + size > max_body:
            raise HTTPError(413, "The upload is too large.")
        out += await asyncio.wait_for(reader.readexactly(size), 60.0)
        await reader.readuntil(b"\n")


async def write_response(writer: asyncio.StreamWriter, req: Request | None, resp: Response,
                         keep_alive: bool) -> bool:
    """Send a response.  Returns whether the connection can carry another request."""
    head_only = req is not None and req.method == "HEAD"
    no_body = head_only or resp.status in (204, 304) or 100 <= resp.status < 200
    version = "HTTP/1.0" if (req is None or req.http10) else "HTTP/1.1"
    length = None
    if resp.stream is None:
        length = len(resp.body or b"")
    elif resp.length is not None:
        length = resp.length
    if resp.stream is not None and length is None and not no_body:
        keep_alive = False                  # body ends when the connection closes
    if resp.close:
        keep_alive = False
    lines = [f"{version} {resp.status} {REASONS.get(resp.status, 'Unknown')}"]
    for k, v in resp.headers:
        if k.lower() in ("content-length", "connection", "proxy-connection", "keep-alive", "transfer-encoding"):
            continue
        v = v.replace("\r", " ").replace("\n", " ")
        lines.append(f"{k}: {v}")
    if length is not None and not (resp.status in (204, 304)):
        lines.append(f"Content-Length: {length}")
    conn = "keep-alive" if keep_alive else "close"
    lines.append(f"Connection: {conn}")
    if req is not None and req.absolute:
        lines.append(f"Proxy-Connection: {conn}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", errors="replace"))
    try:
        if not no_body:
            if resp.stream is None:
                writer.write(resp.body or b"")
                await writer.drain()
            else:
                sent = 0
                async for chunk in resp.stream:
                    writer.write(chunk)
                    sent += len(chunk)
                    await writer.drain()
                if length is not None and sent != length:
                    keep_alive = False
        else:
            await writer.drain()
            if resp.stream is not None and hasattr(resp.stream, "aclose"):
                await resp.stream.aclose()
    finally:
        if resp.on_done is not None:
            await resp.on_done()
    return keep_alive


Handler = Callable[[Request], Awaitable[Response]]


class Server:
    def __init__(self, handler: Handler, error_page: Callable[[int, str], Response], *,
                 max_body: int = 32 * 1024 * 1024, allow_peer: Callable[[tuple], bool] | None = None,
                 refused: Callable[[], Response] | None = None):
        self.handler = handler
        self.error_page = error_page
        self.max_body = max_body
        self.allow_peer = allow_peer
        self.refused = refused
        self.servers: list[asyncio.base_events.Server] = []
        self.connections = 0

    async def start(self, hosts: list[str], port: int) -> None:
        for host in hosts:
            try:
                srv = await asyncio.start_server(self._client, host, port, limit=MAX_LINE * 4,
                                                 reuse_address=True)
            except OSError as e:
                if ":" in host:                 # no IPv6 loopback: not fatal
                    log.warning("not listening on [%s]:%s: %s", host, port, e)
                    continue
                raise
            self.servers.append(srv)

    async def close(self) -> None:
        for s in self.servers:
            s.close()
        for s in self.servers:
            await s.wait_closed()
        self.servers = []

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        sock = writer.get_extra_info("socket")
        self.connections += 1
        try:
            if self.allow_peer is not None and not self.allow_peer(peer):
                try:
                    await read_request(reader, 5.0, 0)
                except (HTTPError, asyncio.TimeoutError, OSError):
                    pass
                await write_response(writer, None, self.refused() if self.refused else
                                     self.error_page(403, "Not allowed."), False)
                return
            dst = original_dst(sock)
            keep = True
            while keep:
                try:
                    req = await read_request(reader, IDLE_S, self.max_body)
                except HTTPError as e:
                    await write_response(writer, None, self.error_page(e.status, e.message), False)
                    return
                except (asyncio.TimeoutError, ConnectionError, OSError):
                    return
                if req is None:
                    return
                req.peer = peer
                req.original_dst = dst
                try:
                    resp = await self.handler(req)
                except Exception:
                    log.exception("handler failed")
                    resp = self.error_page(500, "Something went wrong in the Modern Web Helper.")
                keep = await write_response(writer, req, resp, req.wants_keep_alive())
        except (ConnectionError, asyncio.TimeoutError, OSError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            self.connections -= 1
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 5.0)
            except (OSError, asyncio.TimeoutError, ConnectionError):
                pass
