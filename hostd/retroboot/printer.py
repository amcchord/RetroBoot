"""Virtual Print-to-PDF and CUPS forwarding server for classic Mac OS and Mac OS X (#50).

Accepts PostScript (or PDF) print jobs over:
* LPD (RFC 1179, TCP port 515) — classic Mac LaserWriter Utility / LPR Desktop Printer;
* IPP (RFC 8010/8011, HTTP TCP port 631) — Mac OS X / LPR IPP printers;
* AppSocket / JetDirect (TCP port 9100) — raw PostScript / PDF streams.

Converts PostScript to PDF via Ghostscript (``gs -dSAFER``) or ``ps2pdf``, and routes
jobs either into ``RetroBoot Share/Printed PDFs/`` or to a configured CUPS printer queue
(optionally keeping a PDF copy when ``keep_pdf`` is enabled).
"""
from __future__ import annotations

import asyncio
from collections import deque
import logging
import os
import re
import shutil
import struct
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

from .util import wall

log = logging.getLogger("retroboot.printer")

LPD_PORT = 515
IPP_PORT = 631
RAW_PORT = 9100
MAX_JOB_BYTES = 64 * 1024 * 1024
DEFAULT_BIND = ("127.0.0.1",)
PRINTER_NAME = "RetroBoot Print to PDF"
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9 ._()-]+")
TITLE_COMMENT_RE = re.compile(rb"^%%Title:\s*(.+)$", re.MULTILINE)


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


def _strip_eot(data: bytes) -> bytes:
    """Strip leading/trailing LaserWriter EOT (0x04) framing bytes without mutating inner bytes."""
    return data.strip(b"\x04")


def _extract_ps_title(data: bytes) -> str:
    m = TITLE_COMMENT_RE.search(data[:4096])
    if not m:
        return "Mac OS Print"
    raw = m.group(1).strip().strip(b"()\r\n ")
    if not raw:
        return "Mac OS Print"
    return raw.decode("mac_roman", "replace")


def _parse_lpd_control(data: bytes) -> str:
    for line in data.decode("mac_roman", "replace").splitlines():
        if line.startswith(("J", "N")) and line[1:].strip():
            return line[1:].strip()
    return "Mac OS Print"


def _extract_ipp_queue(path: str) -> str:
    p = urlsplit(path or "/").path.rstrip("/")
    if p.startswith("/printers/"):
        q = unquote(p[len("/printers/"):].split("/", 1)[0]).strip()
        if q:
            return q
    return "retroboot"


def _parse_ipp_request_ex(body: bytes) -> tuple[int, int, dict, bytes]:
    """Return ``(op_id, request_id, attrs, payload)`` from a raw IPP request."""
    if len(body) < 8:
        raise ValueError("IPP request too short")
    _maj, _min, op_id, req_id = struct.unpack_from(">BBHI", body, 0)
    pos = 8
    attrs: dict = {"job-name": "Mac OS Print"}
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
            attrs["job-name"] = val.decode("utf-8", "replace")
        elif key == "job-id" and len(val) == 4 and tag in (0x21, 0x23):
            attrs["job-id"] = struct.unpack_from(">i", val, 0)[0]
        elif key == "job-uri" and val and "job-id" not in attrs:
            m_uri = re.search(r"/jobs/(\d+)\Z", val.decode("utf-8", "replace").rstrip("/"))
            if m_uri:
                attrs["job-id"] = int(m_uri.group(1))
        elif key == "last-document" and len(val) == 1 and tag == 0x22:
            attrs["last-document"] = bool(val[0])
    return op_id, req_id, attrs, body[pos:]


def _parse_ipp_request(body: bytes) -> tuple[int, int, str, bytes]:
    """Return ``(op_id, request_id, job_name, payload)`` from a raw IPP request."""
    op_id, req_id, attrs, payload = _parse_ipp_request_ex(body)
    return op_id, req_id, attrs.get("job-name", "Mac OS Print"), payload


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
    """LPD + IPP + Raw 9100 virtual printer saving into ``Share/Printed PDFs`` or forwarding to CUPS."""

    def __init__(self, share_root: Path, *, enabled: Callable[[], bool] | bool = True,
                 on_printed: Callable[[dict], None] | None = None,
                 lpd_port: int = LPD_PORT, ipp_port: int = IPP_PORT,
                 bind_hosts: tuple[str, ...] = DEFAULT_BIND, converter=None,
                 cups=None, target: Callable[[], str] | str = "pdf",
                 keep_pdf: Callable[[], bool] | bool = False,
                 raw_port: int | None = None):
        self.share_root = Path(share_root)
        self._enabled_fn = enabled if callable(enabled) else (lambda: bool(enabled))
        self.on_printed = on_printed
        self.lpd_port = lpd_port
        self.ipp_port = ipp_port
        self.raw_port = raw_port
        self.bind_hosts = tuple(bind_hosts)
        self._converter = converter or _ps_to_pdf_subproc
        self.cups = cups
        self._target_fn = target if callable(target) else (lambda: str(target or "pdf"))
        self._keep_pdf_fn = keep_pdf if callable(keep_pdf) else (lambda: bool(keep_pdf))
        self._lpd_servers: list[asyncio.AbstractServer] = []
        self._ipp_servers: list[asyncio.AbstractServer] = []
        self._raw_servers: list[asyncio.AbstractServer] = []
        self._lock = asyncio.Lock()
        self.running = False
        self.bound_lpd_port: int | None = None
        self.bound_ipp_port: int | None = None
        self.bound_raw_port: int | None = None
        self.jobs_count = 0
        self.history: deque[dict] = deque(maxlen=20)
        self._ipp_jobs: dict[int, dict] = {}
        self._next_ipp_job_id = 0
        self._last_ipp_job_id: int | None = None

    def is_enabled(self) -> bool:
        try:
            return bool(self._enabled_fn())
        except Exception:
            return False

    def current_target(self) -> str:
        try:
            return str(self._target_fn() or "pdf")
        except Exception:
            return "pdf"

    def should_keep_pdf(self) -> bool:
        try:
            return bool(self._keep_pdf_fn())
        except Exception:
            return False

    @property
    def output_dir(self) -> Path:
        return self.share_root / "Printed PDFs"

    def list_jobs(self) -> list[dict]:
        return list(reversed(self.history))

    async def start(self) -> dict:
        async with self._lock:
            if not self.is_enabled():
                await self._stop_locked()
                return self.public()
            if self.running:
                return self.public()
            self._lpd_servers = []
            self._ipp_servers = []
            self._raw_servers = []
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
                if self.raw_port is not None:
                    try:
                        srv = await asyncio.start_server(self._handle_raw, host, self.raw_port, reuse_address=True)
                        self._raw_servers.append(srv)
                        if srv.sockets:
                            self.bound_raw_port = srv.sockets[0].getsockname()[1]
                    except OSError as exc:
                        log.debug("Raw port %s unavailable on %s: %s", self.raw_port, host, exc)
            self.running = bool(self._lpd_servers or self._ipp_servers or self._raw_servers)
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
        all_srvs = self._lpd_servers + self._ipp_servers + self._raw_servers
        for srv in all_srvs:
            srv.close()
        for srv in all_srvs:
            try:
                await asyncio.wait_for(srv.wait_closed(), timeout=2)
            except (OSError, asyncio.TimeoutError):
                pass
        self._lpd_servers = []
        self._ipp_servers = []
        self._raw_servers = []
        self.running = False
        self.bound_lpd_port = None
        self.bound_ipp_port = None
        self.bound_raw_port = None

    def public(self) -> dict:
        return {
            "enabled": self.is_enabled(),
            "running": self.running,
            "lpd_port": self.bound_lpd_port,
            "ipp_port": self.bound_ipp_port,
            "raw_port": self.bound_raw_port,
            "target": self.current_target(),
            "keep_pdf": self.should_keep_pdf(),
            "jobs": self.jobs_count,
            "history": self.list_jobs(),
        }

    async def _resolve_destination(self, queue: str) -> tuple[str, str | None]:
        q = (queue or "").strip()
        q_low = q.lower()
        if q_low in ("pdf", "print-to-pdf"):
            return "pdf", None
        if self.cups is not None and q_low not in ("", "lp", "retroboot", "default"):
            try:
                printers = await self.cups.list_printers()
            except Exception:
                printers = []
            for p in printers:
                if p.get("name") == q:
                    return "cups", p["name"]
            for p in printers:
                if str(p.get("name") or "").lower() == q_low:
                    return "cups", p["name"]
        tgt = self.current_target()
        if isinstance(tgt, str) and tgt.startswith("cups:") and self.cups is not None:
            cups_q = tgt.split(":", 1)[1].strip()
            if cups_q:
                return "cups", cups_q
        return "pdf", None

    def _save_pdf_atomic(self, src_pdf: Path, clean: str, *, copy_only: bool = False) -> tuple[Path, int]:
        out_dir = self.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H.%M.%S", time.localtime())
        base = f"{stamp} - {clean}"
        dst = out_dir / f"{base}.pdf"
        n = 1
        while dst.exists():
            n += 1
            dst = out_dir / f"{base} ({n}).pdf"
        if copy_only:
            fd, tmp_copy = tempfile.mkstemp(prefix=".print-copy-", suffix=".pdf", dir=str(out_dir))
            os.close(fd)
            copy_path = Path(tmp_copy)
            try:
                shutil.copyfile(src_pdf, copy_path)
                os.chmod(copy_path, 0o644)
                os.replace(copy_path, dst)
            except Exception:
                copy_path.unlink(missing_ok=True)
                raise
        else:
            os.chmod(src_pdf, 0o644)
            os.replace(src_pdf, dst)
        return dst, dst.stat().st_size

    def _record_history(self, rec: dict) -> None:
        self.history.append(rec)

    async def process_job(
        self, data: bytes, job_name: str = "Mac OS Print", *, source: str = "lpd", queue: str = "retroboot"
    ) -> dict:
        if not self.is_enabled():
            raise PrintError("Print to PDF is turned off.", "disabled")
        clean_data = _strip_eot(data) if data else b""
        clean = _sanitize_job_name(job_name)
        if not clean_data or len(clean_data) > MAX_JOB_BYTES:
            err = PrintError("The print job is empty or too large.", "bad_job")
            self._record_history({
                "id": f"rb-{int(time.time() * 1000)}",
                "job": clean,
                "source": source,
                "queue": queue,
                "destination": "unknown",
                "target": self.current_target(),
                "status": "failed",
                "bytes": len(clean_data),
                "error": err.message,
                "timestamp": wall(),
            })
            raise err

        dest_mode, cups_queue = await self._resolve_destination(queue)
        keep_pdf = self.should_keep_pdf()
        need_pdf_file = (dest_mode == "pdf") or keep_pdf
        if need_pdf_file:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            tmp_dir = str(self.output_dir)
        else:
            tmp_dir = None

        fd, tmp_name = tempfile.mkstemp(prefix=".print-", suffix=".pdf", dir=tmp_dir)
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            stripped = clean_data.lstrip()
            if stripped.startswith(b"%PDF-"):
                tmp.write_bytes(clean_data)
            else:
                await self._converter(clean_data, tmp)
            if not tmp.is_file() or tmp.stat().st_size == 0:
                raise PrintError("Converted PDF is empty.", "convert_failed")
            pdf_bytes = tmp.stat().st_size

            info: dict = {
                "job": clean,
                "bytes": pdf_bytes,
                "source": source,
                "queue": queue,
                "destination": dest_mode,
            }
            if dest_mode == "cups" and cups_queue:
                cups_res = await self.cups.print_pdf(cups_queue, tmp, clean)
                info["cups_queue"] = cups_queue
                info["cups_job_id"] = cups_res.get("job_id")
                info["target"] = f"cups:{cups_queue}"
                if keep_pdf:
                    dst, pdf_bytes = self._save_pdf_atomic(tmp, clean, copy_only=True)
                    info["name"] = dst.name
                    info["path"] = f"Printed PDFs/{dst.name}"
                    info["bytes"] = pdf_bytes
            else:
                dst, pdf_bytes = self._save_pdf_atomic(tmp, clean, copy_only=False)
                info["name"] = dst.name
                info["path"] = f"Printed PDFs/{dst.name}"
                info["bytes"] = pdf_bytes
                info["target"] = "pdf"
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            msg = getattr(exc, "message", str(exc))
            code = getattr(exc, "code", "print_failed")
            self._record_history({
                "id": f"rb-{int(time.time() * 1000)}",
                "job": clean,
                "source": source,
                "queue": queue,
                "destination": dest_mode,
                "target": f"cups:{cups_queue}" if dest_mode == "cups" and cups_queue else "pdf",
                "status": "failed",
                "bytes": len(clean_data),
                "error": msg,
                "timestamp": wall(),
            })
            if isinstance(exc, PrintError):
                raise
            raise PrintError(msg, code) from exc
        finally:
            tmp.unlink(missing_ok=True)

        self.jobs_count += 1
        self._record_history({
            "id": info.get("cups_job_id") or f"rb-{self.jobs_count}",
            "job": clean,
            "source": source,
            "queue": queue,
            "destination": dest_mode,
            "target": info["target"],
            "status": "completed",
            "bytes": info["bytes"],
            "path": info.get("path"),
            "cups_job_id": info.get("cups_job_id"),
            "timestamp": wall(),
        })
        if self.on_printed is not None and ("path" in info or dest_mode == "pdf"):
            try:
                self.on_printed(info)
            except Exception:
                log.exception("on_printed callback failed")
        return info

    async def print_test_page(self, target_name: str, version: str = "") -> dict:
        from .cups import generate_test_postscript
        raw = str(target_name or "pdf").strip()
        if raw.startswith("cups:"):
            q = raw[5:].strip()
            label = q
        elif raw == "pdf":
            q = "pdf"
            label = "Print to PDF"
        else:
            q = raw
            label = raw
        ps_data = generate_test_postscript(label, version)
        return await self.process_job(
            ps_data,
            f"RetroBoot Test Page ({label})",
            source="test",
            queue=q,
        )

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
            queue = line[1:].decode("latin-1", "replace").strip() or "retroboot"
            writer.write(b"\x00")
            await writer.drain()
            job_name = "Mac OS Print"
            payloads: list[bytes] = []
            while True:
                try:
                    sub = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=60)
                except asyncio.IncompleteReadError:
                    break
                # Classic Mac OS 9 Desktop PrintMonitor can emit leading NUL padding before subcommands.
                sub = sub.lstrip(b"\x00")
                if not sub or sub == b"\n":
                    continue
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
                    chunks = []
                    total = 0
                    while True:
                        chunk = await asyncio.wait_for(reader.read(65536), timeout=30)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > MAX_JOB_BYTES:
                            raise PrintError("LPD print job exceeded size limit")
                    data = b"".join(chunks)
                else:
                    buf = bytearray()
                    eof_early = False
                    while len(buf) < count:
                        ch = await asyncio.wait_for(reader.read(min(65536, count - len(buf))), timeout=120)
                        if not ch:
                            eof_early = True
                            break
                        # Classic Mac OS 9 Desktop PrintMonitor can re-send the subcommand header line
                        # right after receiving 0x00 and wait for a second 0x00 ACK before sending data.
                        if not buf and ch.startswith(sub):
                            writer.write(b"\x00")
                            await writer.drain()
                            rest = ch[len(sub):]
                            if rest:
                                buf += rest
                            continue
                        buf += ch
                    if len(buf) != count and eof_early:
                        raise asyncio.IncompleteReadError(bytes(buf), count)
                    data = bytes(buf)
                    try:
                        term = await asyncio.wait_for(reader.readexactly(1), timeout=15)
                    except asyncio.IncompleteReadError as exc:
                        if exc.partial:
                            raise
                        term = b"\x00"
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
                await self.process_job(b"".join(payloads), job_name, source="lpd", queue=queue)
        except Exception as exc:
            log.warning("LPD print error: %r", exc)
        finally:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=2)
            except (OSError, asyncio.TimeoutError):
                pass

    # -- AppSocket / JetDirect (TCP 9100) -------------------------------------------------
    async def _handle_raw(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            buf = bytearray()
            while True:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=60)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_JOB_BYTES:
                    log.warning("Raw 9100 print job exceeded MAX_JOB_BYTES")
                    return
            if buf:
                data = bytes(buf)
                title = _extract_ps_title(_strip_eot(data))
                await self.process_job(data, title, source="raw", queue="retroboot")
        except Exception as exc:
            log.warning("Raw 9100 print error: %s", exc)
        finally:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=2)
            except (OSError, asyncio.TimeoutError):
                pass

    # -- IPP (RFC 8010/8011 over HTTP) ----------------------------------------------------
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
                req_path = req_line[1] if len(req_line) > 1 else "/"
                ipp_queue = _extract_ipp_queue(req_path)
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
                    resp_ipp = await self._dispatch_ipp(body, queue=ipp_queue)
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

    def _new_ipp_job(self, job_name: str, queue: str, state: int = 4) -> dict:
        self._next_ipp_job_id += 1
        jid = self._next_ipp_job_id
        self._last_ipp_job_id = jid
        if len(self._ipp_jobs) >= 50:
            oldest = next(iter(self._ipp_jobs))
            self._ipp_jobs.pop(oldest, None)
        rec = {"id": jid, "job_name": job_name, "queue": queue, "state": state, "chunks": []}
        self._ipp_jobs[jid] = rec
        return rec

    def _ipp_job_attrs(self, rec: dict) -> bytes:
        port = self.bound_ipp_port or self.ipp_port
        juri = f"ipp://10.0.2.2:{port}/jobs/{rec['id']}".encode("utf-8")
        return (
            b"\x02"  # job-attributes-tag
            + _ipp_attr(0x21, b"job-id", struct.pack(">i", rec["id"]))
            + _ipp_attr(0x45, b"job-uri", juri)
            + _ipp_attr(0x23, b"job-state", struct.pack(">i", rec["state"]))
            + _ipp_attr(0x42, b"job-name", rec["job_name"].encode("utf-8"))
        )

    async def _dispatch_ipp(self, body: bytes, *, queue: str = "retroboot") -> bytes:
        try:
            op_id, req_id, attrs, payload = _parse_ipp_request_ex(body)
        except ValueError:
            return _build_ipp_response(0x0400, 1)
        job_name = attrs.get("job-name", "Mac OS Print")
        port = self.bound_ipp_port or self.ipp_port
        q_label = queue if queue and queue != "retroboot" else PRINTER_NAME
        printer_uri = f"ipp://10.0.2.2:{port}/printers/{queue or 'retroboot'}".encode("utf-8")

        if op_id == 0x000B:  # Get-Printer-Attributes
            ops = (2, 4, 5, 6, 8, 9, 10, 11)
            ops_bytes = _ipp_attr(0x23, b"operations-supported", struct.pack(">i", ops[0]))
            for op in ops[1:]:
                ops_bytes += _ipp_attr(0x23, b"", struct.pack(">i", op))
            extra = (
                b"\x04"  # printer-attributes-tag
                + _ipp_attr(0x45, b"printer-uri-supported", printer_uri)
                + _ipp_attr(0x44, b"uri-security-supported", b"none")
                + _ipp_attr(0x44, b"uri-authentication-supported", b"none")
                + _ipp_attr(0x42, b"printer-name", q_label.encode("utf-8"))
                + _ipp_attr(0x23, b"printer-state", struct.pack(">i", 3))
                + _ipp_attr(0x44, b"printer-state-reasons", b"none")
                + _ipp_attr(0x22, b"printer-is-accepting-jobs", b"\x01")
                + ops_bytes
                + _ipp_attr(0x44, b"pdl-override-supported", b"not-attempted")
                + _ipp_attr(0x22, b"color-supported", b"\x01")
                + _ipp_attr(0x49, b"document-format-default", b"application/postscript")
                + _ipp_attr(0x49, b"document-format-supported", b"application/postscript")
                + _ipp_attr(0x49, b"", b"application/pdf")
                + _ipp_attr(0x47, b"charset-configured", b"utf-8")
                + _ipp_attr(0x47, b"charset-supported", b"utf-8")
                + _ipp_attr(0x48, b"natural-language-configured", b"en")
                + _ipp_attr(0x48, b"generated-natural-language-supported", b"en")
            )
            return _build_ipp_response(0x0000, req_id, extra)

        if op_id == 0x0004:  # Validate-Job
            return _build_ipp_response(0x0000, req_id)

        if op_id == 0x0005:  # Create-Job
            rec = self._new_ipp_job(job_name, queue, state=4)
            return _build_ipp_response(0x0000, req_id, self._ipp_job_attrs(rec))

        if op_id == 0x0006:  # Send-Document
            jid = attrs.get("job-id") or self._last_ipp_job_id
            rec = self._ipp_jobs.get(jid) if jid is not None else None
            if rec is None:
                return _build_ipp_response(0x0406, req_id)  # client-error-not-found
            if rec["state"] == 7:
                return _build_ipp_response(0x0407, req_id)  # client-error-gone / canceled
            if payload:
                rec["chunks"].append(payload)
            if job_name and job_name != "Mac OS Print" and rec["job_name"] == "Mac OS Print":
                rec["job_name"] = job_name
            last_doc = attrs.get("last-document", True)
            if not last_doc:
                rec["state"] = 3
                return _build_ipp_response(0x0000, req_id, self._ipp_job_attrs(rec))
            full_data = b"".join(rec["chunks"])
            rec["chunks"].clear()
            try:
                await self.process_job(full_data, rec["job_name"], source="ipp", queue=rec["queue"])
            except PrintError:
                rec["state"] = 8  # aborted
                return _build_ipp_response(0x0500, req_id)
            rec["state"] = 9  # completed
            return _build_ipp_response(0x0000, req_id, self._ipp_job_attrs(rec))

        if op_id == 0x0008:  # Cancel-Job
            jid = attrs.get("job-id")
            rec = self._ipp_jobs.get(jid) if jid is not None else None
            if rec is None:
                return _build_ipp_response(0x0406, req_id)
            rec["state"] = 7  # canceled
            rec["chunks"].clear()
            return _build_ipp_response(0x0000, req_id)

        if op_id == 0x0009:  # Get-Job-Attributes
            jid = attrs.get("job-id")
            rec = self._ipp_jobs.get(jid) if jid is not None else None
            if rec is None:
                return _build_ipp_response(0x0406, req_id)
            return _build_ipp_response(0x0000, req_id, self._ipp_job_attrs(rec))

        if op_id == 0x000A:  # Get-Jobs
            extra = b"".join(self._ipp_job_attrs(rec) for rec in list(self._ipp_jobs.values())[-10:])
            return _build_ipp_response(0x0000, req_id, extra)

        if op_id == 0x0002:  # Print-Job
            rec = self._new_ipp_job(job_name, queue, state=5)
            try:
                await self.process_job(payload, job_name, source="ipp", queue=queue)
            except PrintError:
                rec["state"] = 8
                return _build_ipp_response(0x0500, req_id)
            rec["state"] = 9
            return _build_ipp_response(0x0000, req_id, self._ipp_job_attrs(rec))

        return _build_ipp_response(0x0501, req_id)  # server-error-operation-not-supported
