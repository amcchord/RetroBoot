"""Virtual Print-to-PDF server for classic Mac OS and Mac OS X guests (#50).

Accepts PostScript (or PDF) print jobs over:
* LPD (RFC 1179, TCP port 515) — classic Mac LaserWriter Utility / LPR Desktop Printer;
* IPP (RFC 8010/8011, HTTP TCP port 631) — Mac OS X / LPR IPP printers.

Converts PostScript to PDF via Ghostscript (``gs -dSAFER``) or ``ps2pdf``, saves
timestamped PDFs into ``RetroBoot Share/Printed PDFs/``, and notifies ``hostd``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import struct
import tempfile
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("retroboot.printer")

LPD_PORT = 515
IPP_PORT = 631
MAX_JOB_BYTES = 64 * 1024 * 1024
DEFAULT_BIND = ("127.0.0.1",)
PRINTER_NAME = "RetroBoot Print to PDF"
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9 ._()-]+")


class PrintError(Exception):
    def __init__(self, message: str, code: str = "print_failed"):
        super().__init__(message)
        self.message = message
        self.code = code


def _sanitize_job_name(raw: str) -> str:
    name = SAFE_NAME_RE.sub(" ", (raw or "").strip())
    name = " ".join(name.split())
    for suffix in (".ps", ".eps", ".pdf"):
        if name.lower().endswith(suffix):
            name = name[:-len(suffix)]
    return (name.strip(" ._-") or "Mac OS Print")[:64]


def _parse_lpd_control(data: bytes) -> str:
    for line in data.decode("mac_roman", "replace").splitlines():
        if line.startswith(("J", "N")) and line[1:].strip():
            return line[1:].strip()
    return "Mac OS Print"


def _parse_ipp_request(body: bytes) -> tuple[int, int, str, bytes]:
    """Return ``(op_id, request_id, job_name, payload)`` from a raw IPP request."""
    if len(body) < 8:
        raise ValueError("IPP request too short")
    _maj, _min, op_id, req_id = struct.unpack_from(">BBHI", body, 0)
    pos = 8
    job_name = "Mac OS Print"
    n = len(body)
    while pos < n:
        tag = body[pos]
        pos += 1
        if tag == 0x03:  # end-of-attributes-tag
            break
        if tag <= 0x0F:  # delimiter / group tag
            continue
        if pos + 2 > n:
            break
        klen = struct.unpack_from(">H", body, pos)[0]
        pos += 2
        if pos + klen + 2 > n:
            break
        key = body[pos:pos + klen].decode("utf-8", "replace")
        pos += klen
        vlen = struct.unpack_from(">H", body, pos)[0]
        pos += 2
        if pos + vlen > n:
            break
        val = body[pos:pos + vlen]
        pos += vlen
        if key == "job-name" and val:
            job_name = val.decode("utf-8", "replace")
    return op_id, req_id, job_name, body[pos:]


def _ipp_attr(tag: int, name: bytes, value: bytes) -> bytes:
    return bytes((tag,)) + struct.pack(">H", len(name)) + name + struct.pack(">H", len(value)) + value


def _build_ipp_response(status: int, req_id: int, extra: bytes = b"") -> bytes:
    hdr = struct.pack(">BBHI", 1, 1, status, req_id)
    attrs = (
        b"\x01"  # operation-attributes-tag
        + _ipp_attr(0x47, b"attributes-charset", b"utf-8")
        + _ipp_attr(0x48, b"attributes-natural-language", b"en")
        + extra
        + b"\x03"
    )
    return hdr + attrs


async def _ps_to_pdf_subproc(ps_data: bytes, out_pdf: Path) -> None:
    gs = shutil.which("gs")
    ps2pdf = shutil.which("ps2pdf")
    if gs:
        cmd = [gs, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER", "-sDEVICE=pdfwrite", f"-sOutputFile={out_pdf}", "-"]
    elif ps2pdf:
        cmd = [ps2pdf, "-", str(out_pdf)]
    else:
        raise PrintError("Ghostscript (gs / ps2pdf) is not installed.", "converter_missing")
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await asyncio.wait_for(proc.communicate(ps_data), timeout=120)
    if proc.returncode != 0 or not out_pdf.is_file():
        raise PrintError(f"PDF conversion failed: {err.decode('utf-8', 'replace')[-200:].strip()}", "convert_failed")


class PrintServer:
    """LPD + IPP virtual Print-to-PDF server writing into ``Share/Printed PDFs``."""

    def __init__(self, share_root: Path, *, enabled: Callable[[], bool] | bool = True,
                 on_printed: Callable[[dict], None] | None = None,
                 lpd_port: int = LPD_PORT, ipp_port: int = IPP_PORT,
                 bind_hosts: tuple[str, ...] = DEFAULT_BIND, converter=None):
        self.share_root = Path(share_root)
        self._enabled_fn = enabled if callable(enabled) else (lambda: bool(enabled))
        self.on_printed = on_printed
        self.lpd_port = lpd_port
        self.ipp_port = ipp_port
        self.bind_hosts = tuple(bind_hosts)
        self._converter = converter or _ps_to_pdf_subproc
        self._lpd_servers: list[asyncio.AbstractServer] = []
        self._ipp_servers: list[asyncio.AbstractServer] = []
        self._lock = asyncio.Lock()
        self.running = False
        self.bound_lpd_port: int | None = None
        self.bound_ipp_port: int | None = None
        self.jobs_count = 0

    def is_enabled(self) -> bool:
        try:
            return bool(self._enabled_fn())
        except Exception:
            return False

    @property
    def output_dir(self) -> Path:
        return self.share_root / "Printed PDFs"

    async def start(self) -> dict:
        async with self._lock:
            if not self.is_enabled():
                await self._stop_locked()
                return self.public()
            if self.running:
                return self.public()
            self._lpd_servers = []
            self._ipp_servers = []
            for host in self.bind_hosts:
                try:
                    srv = await asyncio.start_server(self._handle_lpd, host, self.lpd_port, reuse_address=True)
                    self._lpd_servers.append(srv)
                    if srv.sockets:
                        self.bound_lpd_port = srv.sockets[0].getsockname()[1]
                except OSError as exc:
                    log.debug("LPD port %s unavailable on %s: %s", self.lpd_port, host, exc)
                try:
                    srv = await asyncio.start_server(self._handle_ipp, host, self.ipp_port, reuse_address=True)
                    self._ipp_servers.append(srv)
                    if srv.sockets:
                        self.bound_ipp_port = srv.sockets[0].getsockname()[1]
                except OSError as exc:
                    log.debug("IPP port %s unavailable on %s: %s", self.ipp_port, host, exc)
            self.running = bool(self._lpd_servers or self._ipp_servers)
            return self.public()

    async def sync(self) -> dict:
        if self.is_enabled():
            return await self.start()
        await self.stop()
        return self.public()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        for srv in self._lpd_servers + self._ipp_servers:
            srv.close()
        for srv in self._lpd_servers + self._ipp_servers:
            try:
                await asyncio.wait_for(srv.wait_closed(), timeout=2)
            except (OSError, asyncio.TimeoutError):
                pass
        self._lpd_servers = []
        self._ipp_servers = []
        self.running = False
        self.bound_lpd_port = None
        self.bound_ipp_port = None

    def public(self) -> dict:
        return {"enabled": self.is_enabled(), "running": self.running,
                "lpd_port": self.bound_lpd_port, "ipp_port": self.bound_ipp_port,
                "jobs": self.jobs_count}

    async def process_job(self, data: bytes, job_name: str = "Mac OS Print", *, source: str = "lpd") -> dict:
        if not self.is_enabled():
            raise PrintError("Print to PDF is turned off.", "disabled")
        if not data or len(data) > MAX_JOB_BYTES:
            raise PrintError("The print job is empty or too large.", "bad_job")
        clean = _sanitize_job_name(job_name)
        out_dir = self.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H.%M.%S", time.localtime())
        base = f"{stamp} - {clean}"
        dst = out_dir / f"{base}.pdf"
        n = 1
        while dst.exists():
            n += 1
            dst = out_dir / f"{base} ({n}).pdf"

        fd, tmp_name = tempfile.mkstemp(prefix=".print-", suffix=".pdf", dir=str(out_dir))
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            stripped = data.lstrip()
            if stripped.startswith(b"%PDF-"):
                tmp.write_bytes(data)
            else:
                await self._converter(data, tmp)
            if not tmp.is_file() or tmp.stat().st_size == 0:
                raise PrintError("Converted PDF is empty.", "convert_failed")
            os.chmod(tmp, 0o644)
            os.replace(tmp, dst)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        self.jobs_count += 1
        info = {"name": dst.name, "path": f"Printed PDFs/{dst.name}", "job": clean,
                "bytes": dst.stat().st_size, "source": source}
        if self.on_printed is not None:
            try:
                self.on_printed(info)
            except Exception:
                log.exception("on_printed callback failed")
        return info

    # -- LPD (RFC 1179) -------------------------------------------------------------------
    async def _handle_lpd(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=15)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError):
            writer.close()
            return
        try:
            if not line:
                return
            cmd = line[0]
            if cmd in (1, 3, 4):
                if cmd == 1:
                    writer.write(b"\x00")
                else:
                    writer.write(b"no entries\n")
                await writer.drain()
                return
            if cmd != 2:
                writer.write(b"\x01")
                await writer.drain()
                return
            writer.write(b"\x00")
            await writer.drain()
            job_name = "Mac OS Print"
            payloads: list[bytes] = []
            while True:
                try:
                    sub = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=60)
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        break
                    break
                if not sub:
                    break
                stype = sub[0]
                if stype == 1:  # abort
                    writer.write(b"\x00")
                    await writer.drain()
                    return
                if stype not in (2, 3):
                    writer.write(b"\x01")
                    await writer.drain()
                    return
                parts = sub[1:].decode("latin-1", "replace").strip().split(None, 1)
                if not parts:
                    writer.write(b"\x01")
                    await writer.drain()
                    return
                try:
                    count = int(parts[0])
                except ValueError:
                    writer.write(b"\x01")
                    await writer.drain()
                    return
                if count < 0 or count > MAX_JOB_BYTES:
                    writer.write(b"\x01")
                    await writer.drain()
                    return
                writer.write(b"\x00")
                await writer.drain()
                if count == 0:
                    data = await reader.read(MAX_JOB_BYTES)
                else:
                    data = await asyncio.wait_for(reader.readexactly(count), timeout=120)
                    term = await asyncio.wait_for(reader.readexactly(1), timeout=15)
                    if term != b"\x00":
                        writer.write(b"\x01")
                        await writer.drain()
                        return
                writer.write(b"\x00")
                await writer.drain()
                if stype == 2:
                    job_name = _parse_lpd_control(data)
                else:
                    payloads.append(data)
            if payloads:
                await self.process_job(b"".join(payloads), job_name, source="lpd")
        except Exception as exc:
            log.warning("LPD print error: %s", exc)
        finally:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=2)
            except (OSError, asyncio.TimeoutError):
                pass

    # -- IPP (RFC 8010/8011 over HTTP) ---------------------------------------------------
    async def _handle_ipp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = bytearray()
                while b"\r\n\r\n" not in head:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=30)
                    if not chunk:
                        return
                    head += chunk
                    if len(head) > 65536:
                        return
                sep = head.index(b"\r\n\r\n")
                hdr_bytes = bytes(head[:sep])
                rest = bytearray(head[sep + 4:])
                lines = hdr_bytes.decode("latin-1", "replace").split("\r\n")
                req_line = lines[0].split()
                method = req_line[0].upper() if req_line else "GET"
                headers: dict[str, str] = {}
                for hl in lines[1:]:
                    if ":" in hl:
                        k, v = hl.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                if headers.get("expect", "").lower() == "100-continue":
                    writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                    await writer.drain()
                if "chunked" in headers.get("transfer-encoding", "").lower():
                    body = await self._read_chunked(reader, rest)
                else:
                    clen = int(headers.get("content-length") or 0)
                    if clen < 0 or clen > MAX_JOB_BYTES:
                        return
                    while len(rest) < clen:
                        more = await asyncio.wait_for(reader.read(min(65536, clen - len(rest))), timeout=120)
                        if not more:
                            break
                        rest += more
                    body = bytes(rest[:clen])
                if method == "POST":
                    resp_ipp = await self._dispatch_ipp(body)
                    self._write_http(writer, 200, "application/ipp", resp_ipp)
                else:
                    self._write_http(writer, 200, "text/plain; charset=utf-8",
                                     f"{PRINTER_NAME}\n".encode("utf-8"))
                await writer.drain()
                if headers.get("connection", "").lower() == "close" or "HTTP/1.0" in lines[0]:
                    return
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError, ValueError):
            pass
        finally:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=2)
            except (OSError, asyncio.TimeoutError):
                pass

    @staticmethod
    async def _read_chunked(reader: asyncio.StreamReader, buf: bytearray) -> bytes:
        out = bytearray()
        while True:
            while b"\r\n" not in buf:
                more = await asyncio.wait_for(reader.read(4096), timeout=30)
                if not more:
                    return bytes(out)
                buf += more
            idx = buf.index(b"\r\n")
            size = int(bytes(buf[:idx]).split(b";", 1)[0].strip(), 16)
            del buf[:idx + 2]
            if size == 0:
                return bytes(out)
            if len(out) + size > MAX_JOB_BYTES:
                raise ValueError("IPP body too large")
            while len(buf) < size + 2:
                more = await asyncio.wait_for(reader.read(65536), timeout=60)
                if not more:
                    break
                buf += more
            out += buf[:size]
            del buf[:size + 2]

    @staticmethod
    def _write_http(writer: asyncio.StreamWriter, status: int, ctype: str, body: bytes) -> None:
        hdr = (f"HTTP/1.1 {status} OK\r\nContent-Type: {ctype}\r\n"
               f"Content-Length: {len(body)}\r\nConnection: keep-alive\r\n\r\n").encode("latin-1")
        writer.write(hdr + body)

    async def _dispatch_ipp(self, body: bytes) -> bytes:
        try:
            op_id, req_id, job_name, payload = _parse_ipp_request(body)
        except ValueError:
            return _build_ipp_response(0x0400, 1)
        if op_id == 0x000B:  # Get-Printer-Attributes
            extra = (
                b"\x04"  # printer-attributes-tag
                + _ipp_attr(0x42, b"printer-name", PRINTER_NAME.encode("utf-8"))
                + _ipp_attr(0x23, b"printer-state", struct.pack(">i", 3))
                + _ipp_attr(0x22, b"printer-is-accepting-jobs", b"\x01")
                + _ipp_attr(0x49, b"document-format-default", b"application/postscript")
                + _ipp_attr(0x49, b"document-format-supported", b"application/postscript")
                + _ipp_attr(0x49, b"", b"application/pdf")
            )
            return _build_ipp_response(0x0000, req_id, extra)
        if op_id == 0x0004:  # Validate-Job
            return _build_ipp_response(0x0000, req_id)
        if op_id == 0x000A:  # Get-Jobs
            return _build_ipp_response(0x0000, req_id)
        if op_id == 0x0002:  # Print-Job
            try:
                await self.process_job(payload, job_name, source="ipp")
            except PrintError:
                return _build_ipp_response(0x0500, req_id)
            extra = (
                b"\x02"  # job-attributes-tag
                + _ipp_attr(0x23, b"job-id", struct.pack(">i", max(1, self.jobs_count)))
                + _ipp_attr(0x23, b"job-state", struct.pack(">i", 9))  # completed
            )
            return _build_ipp_response(0x0000, req_id, extra)
        return _build_ipp_response(0x0501, req_id)  # server-error-operation-not-supported
