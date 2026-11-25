"""CUPS printer management over the local Unix domain socket (/run/cups/cups.sock).

Discovers network (Avahi / driverless) and USB (lpinfo / ipp-usb) printers,
manages RetroBoot-owned CUPS print queues via ``lpadmin``, submits converted PDF
jobs via ``lp``, and queries/cancels active CUPS jobs.

All subprocess execution uses ``asyncio.create_subprocess_exec`` with explicit
argument arrays (never a shell) and forces ``CUPS_SERVER=/run/cups/cups.sock``
plus ``-h /run/cups/cups.sock`` so RetroBoot never talks to an external server.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import ipaddress
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

log = logging.getLogger("retroboot.cups")

DEFAULT_SOCKET = "/run/cups/cups.sock"
QUEUE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}\Z")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
RESERVED_QUEUE_NAMES = frozenset({
    "pdf", "print-to-pdf", "retroboot", "lp", "default", "raw", "all",
})
ALLOWED_SCHEMES = frozenset({"ipp", "ipps", "socket", "lpd", "usb", "dnssd"})
MANUAL_PROTOCOLS = frozenset({"ipp", "ipps", "socket", "lpd"})
DEFAULT_PORTS = {"ipp": 631, "ipps": 631, "lpd": 515, "socket": 9100}
FORBIDDEN_LOOPBACK_PORTS = frozenset({515, 631, 9100})
IPP_USB_MIN_PORT = 60000
SHELL_META_RE = re.compile(r"[;|&$`<>(){}\\\'\"!\x00\r\n\t]")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
AVAHI_ESC_RE = re.compile(rb"\\([0-9]{3})")
AVAHI_SERVICES = ("_ipp._tcp", "_ipps._tcp", "_pdl-datastream._tcp", "_printer._tcp")
DRIVERS = ("auto", "everywhere", "generic_ps", "generic_pcl", "raw")


class CupsError(Exception):
    def __init__(self, message: str, code: str = "cups_error"):
        super().__init__(message)
        self.message = message
        self.code = code


def validate_queue_name(name: str) -> str:
    """Enforce ``^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$`` and reject reserved queue names."""
    if not isinstance(name, str) or not QUEUE_NAME_RE.fullmatch(name):
        raise CupsError(
            "Printer queue names must start with a letter or digit and use up to 48 "
            "letters, digits, hyphens, or underscores.",
            "invalid_name",
        )
    if name.lower() in RESERVED_QUEUE_NAMES:
        raise CupsError(f"The printer name {name!r} is reserved by RetroBoot.", "reserved_name")
    return name


def slugify_printer_name(raw: str) -> str:
    """Turn an arbitrary printer display name into a safe, non-reserved CUPS queue slug."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", str(raw or "").strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("_-")
    if not cleaned or not cleaned[0].isalnum():
        cleaned = "printer" + (("-" + cleaned) if cleaned else "")
    slug = cleaned[:48].rstrip("_-") or "printer"
    if slug.lower() in RESERVED_QUEUE_NAMES:
        slug = f"{slug[:40]}-printer"
    return validate_queue_name(slug)


def _is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    if h in ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"):
        return True
    try:
        ip = ipaddress.ip_address(h.split("%", 1)[0])
        return ip.is_loopback or ip.is_unspecified
    except ValueError:
        return False


def validate_printer_uri(uri: str) -> str:
    """Validate a CUPS device URI and block unsafe characters or RetroBoot loopback ports."""
    if not isinstance(uri, str):
        raise CupsError("Printer URI must be a string.", "invalid_uri")
    if not uri or len(uri) > 512:
        raise CupsError("Printer URI must be between 1 and 512 characters.", "invalid_uri")
    if any(ch.isspace() for ch in uri) or CONTROL_CHAR_RE.search(uri) or SHELL_META_RE.search(uri):
        raise CupsError("Printer URI contains unsupported whitespace or special characters.", "invalid_uri")
    if "://" not in uri:
        raise CupsError("Printer URI must include a scheme such as ipp:// or socket://.", "invalid_uri")
    parsed = urlsplit(uri)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise CupsError(
            f"Unsupported printer protocol {scheme!r}. Use ipp, ipps, socket, lpd, usb, or dnssd.",
            "invalid_uri",
        )
    if parsed.username or parsed.password:
        raise CupsError("Printer URI must not contain embedded credentials.", "invalid_uri")
    host = parsed.hostname
    if not host or not parsed.netloc:
        raise CupsError("Printer URI must include a valid host or device identifier.", "invalid_uri")
    try:
        port = parsed.port
    except ValueError:
        raise CupsError("Printer URI contains an invalid port number.", "invalid_uri")
    if port is not None and not (1 <= port <= 65535):
        raise CupsError("Printer port must be between 1 and 65535.", "invalid_uri")

    if _is_loopback_host(host):
        # Allow local ipp-usb bridge ports (>= 60000) on localhost / 127.0.0.1 for IPP/IPPS,
        # while blocking RetroBoot's own virtual printer ports (515, 631, 9100) and other local services.
        effective_port = port if port is not None else DEFAULT_PORTS.get(scheme)
        host_norm = host.lower().rstrip(".")
        is_ipp_usb = (
            scheme in ("ipp", "ipps")
            and host_norm in ("localhost", "127.0.0.1")
            and port is not None
            and port >= IPP_USB_MIN_PORT
        )
        if not is_ipp_usb or effective_port in FORBIDDEN_LOOPBACK_PORTS:
            raise CupsError(
                "Localhost printer addresses on RetroBoot ports cannot be added as a CUPS printer.",
                "loopback_forbidden",
            )
    return uri


def build_manual_uri(protocol: str, host: str, port: int | None = None, path: str | None = None) -> str:
    """Construct and validate a manual ``ipp://``, ``ipps://``, ``socket://``, or ``lpd://`` URI."""
    if not isinstance(protocol, str):
        raise CupsError("Choose a printer protocol.", "invalid_protocol")
    proto = protocol.strip().lower()
    if proto not in MANUAL_PROTOCOLS:
        raise CupsError("Protocol must be ipp, ipps, socket, or lpd.", "invalid_protocol")
    if not isinstance(host, str) or not host.strip():
        raise CupsError("Enter a printer hostname or IP address.", "invalid_host")
    raw_host = host.strip()
    if any(ch.isspace() for ch in raw_host) or CONTROL_CHAR_RE.search(raw_host) or SHELL_META_RE.search(raw_host):
        raise CupsError("Printer host contains invalid characters.", "invalid_host")
    if any(ch in raw_host for ch in ("/", "?", "#", "@")):
        raise CupsError("Enter only the printer hostname or IP address.", "invalid_host")

    if raw_host.startswith("[") and raw_host.endswith("]"):
        inner = raw_host[1:-1]
        try:
            ipaddress.IPv6Address(inner.split("%", 1)[0])
        except ValueError:
            raise CupsError("Invalid IPv6 printer address.", "invalid_host")
        host_part = f"[{inner}]"
    elif ":" in raw_host:
        try:
            ipaddress.IPv6Address(raw_host.split("%", 1)[0])
        except ValueError:
            raise CupsError("Enter a valid hostname or IP address (put port in the port field).", "invalid_host")
        host_part = f"[{raw_host}]"
    else:
        host_part = raw_host

    port_part = ""
    if port is not None and port != "":
        if isinstance(port, bool):
            raise CupsError("Port must be a number between 1 and 65535.", "invalid_port")
        try:
            pnum = int(port)
        except (TypeError, ValueError):
            raise CupsError("Port must be a number between 1 and 65535.", "invalid_port")
        if not (1 <= pnum <= 65535):
            raise CupsError("Port must be between 1 and 65535.", "invalid_port")
        port_part = f":{pnum}"

    clean_path = (path or "").strip() if isinstance(path, str) else ""
    if proto == "socket":
        path_part = ""
    elif proto in ("ipp", "ipps"):
        if clean_path:
            path_part = "/" + clean_path.lstrip("/")
        else:
            path_part = "/ipp/print"
    else:  # lpd
        path_part = ("/" + clean_path.lstrip("/")) if clean_path else ""

    uri = f"{proto}://{host_part}{port_part}{path_part}"
    return validate_printer_uri(uri)


def _unescape_avahi(raw: str) -> str:
    data = raw.encode("utf-8", "surrogateescape")
    data = AVAHI_ESC_RE.sub(lambda m: bytes((int(m.group(1)),)), data)
    return data.decode("utf-8", "replace")


def _parse_avahi_txt(txt_field: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for token in re.findall(r'"([^"]*)"', txt_field or ""):
        if "=" in token:
            k, v = token.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _clean_text(raw: str, max_len: int = 128) -> str:
    s = CONTROL_CHAR_RE.sub(" ", str(raw or "")).strip()
    return " ".join(s.split())[:max_len]


def _ps_escape(text: str) -> str:
    ascii_text = str(text or "").encode("ascii", "replace").decode("ascii")
    ascii_text = CONTROL_CHAR_RE.sub(" ", ascii_text)
    return ascii_text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def generate_test_postscript(target_label: str, version: str = "") -> bytes:
    """Return a valid 1-page ``%!PS-Adobe-3.0`` test page for ``target_label``."""
    ver_line = f"RetroBoot {version}".strip() if version else "RetroBoot"
    esc_ver = _ps_escape(ver_line)
    esc_target = _ps_escape(target_label or "RetroBoot Printer")
    ps = f"""%!PS-Adobe-3.0
%%Title: RetroBoot Printer Test Page
%%Creator: RetroBoot
%%Pages: 1
%%BoundingBox: 0 0 612 792
%%EndComments
%%Page: 1 1
/Helvetica-Bold findfont 22 scalefont setfont
72 710 moveto ({esc_ver} - Printer Test Page) show

/Helvetica findfont 12 scalefont setfont
72 682 moveto (Target: {esc_target}) show
72 664 moveto (Page size: US Letter 612 x 792 pt \\(PostScript Level 2/3\\)) show

% Outer border
0.8 setlinewidth
0 setgray
54 54 504 684 rectstroke

% Checkerboard test pattern
72 520 translate
0 1 7 {{
  /row exch def
  0 1 15 {{
    /col exch def
    row col add 2 mod 0 eq {{
      0.15 setgray
    }} {{
      0.92 setgray
    }} ifelse
    col 18 mul row 14 mul 18 14 rectfill
  }} for
}} for
0 setgray
0 0 288 112 rectstroke

% Grayscale ramp
0 -60 translate
0 1 9 {{
  /i exch def
  i 10 div setgray
  i 28 mul 0 28 32 rectfill
}} for
0 setgray
0 0 280 32 rectstroke

grestore
gsave
/Helvetica findfont 10 scalefont setfont
72 90 moveto (If you can read this page clearly, RetroBoot printing is working.) show
grestore
showpage
%%EOF
"""
    return ps.encode("ascii")


async def _exec_subprocess(
    cmd: list[str], *, timeout: float = 15.0, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return 124, "", f"{cmd[0]}: timed out"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class CupsManager:
    """CUPS queue, discovery, job submission, and cancellation wrapper."""

    def __init__(self, socket_path: str = DEFAULT_SOCKET, runner: Callable[..., Any] | None = None):
        self.socket_path = str(socket_path or DEFAULT_SOCKET)
        self._runner = runner

    def available(self) -> tuple[bool, str | None]:
        if not shutil.which("lpadmin") or not shutil.which("lp"):
            return False, "CUPS printing tools (lpadmin / lp) are not installed."
        return True, None

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["CUPS_SERVER"] = self.socket_path
        env["LC_ALL"] = "C"
        return env

    async def _run(self, cmd: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
        env = self._env()
        if self._runner is None:
            return await _exec_subprocess(cmd, timeout=timeout, env=env)
        try:
            sig = inspect.signature(self._runner)
            if "env" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                res = self._runner(cmd, timeout=timeout, env=env)
            else:
                res = self._runner(cmd, timeout=timeout)
        except (TypeError, ValueError):
            res = self._runner(cmd, timeout=timeout)
        if inspect.isawaitable(res):
            res = await res
        return res

    async def list_printers(self) -> list[dict]:
        rc, out, err = await self._run(["lpstat", "-h", self.socket_path, "-p", "-v", "-l"], timeout=10.0)
        combined = f"{out}\n{err}".lower()
        if rc != 0:
            if "no destinations" in combined:
                return []
            raise CupsError(err.strip() or out.strip() or "Could not query CUPS printers.", "cups_unavailable")

        printers: dict[str, dict] = {}
        order: list[str] = []
        current: dict | None = None

        def ensure(qname: str) -> dict:
            if qname not in printers:
                order.append(qname)
                printers[qname] = {
                    "name": qname,
                    "state": "unknown",
                    "enabled": True,
                    "accepting": True,
                    "uri": "",
                    "description": "",
                    "location": "",
                    "lpd_queue": qname,
                    "ipp_url": f"ipp://10.0.2.2:631/printers/{qname}",
                }
            return printers[qname]

        for line in out.splitlines():
            if not line.strip():
                continue
            m_dev = re.match(r"^device for ([^:\s]+):\s*(.+)$", line.strip())
            if m_dev:
                current = None
                p = ensure(m_dev.group(1))
                p["uri"] = m_dev.group(2).strip()
                continue
            m_pr = re.match(r"^printer ([^\s]+)\s+(.+)$", line)
            if m_pr:
                qname, rest = m_pr.group(1), m_pr.group(2)
                p = ensure(qname)
                current = p
                low = rest.lower()
                if low.startswith("is idle"):
                    p["state"] = "idle"
                    p["enabled"] = True
                elif low.startswith("is printing"):
                    p["state"] = "printing"
                    p["enabled"] = True
                elif "disabled" in low:
                    p["state"] = "disabled"
                    p["enabled"] = False
                if "not accepting" in low:
                    p["accepting"] = False
                elif "disabled" in low and "enabled since" not in low:
                    p["accepting"] = False
                continue
            if current is not None and line[0].isspace():
                stripped = line.strip()
                if stripped.startswith("Description:"):
                    current["description"] = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("Location:"):
                    current["location"] = stripped.split(":", 1)[1].strip()

        return [printers[q] for q in order]

    async def discover(self, timeout: float = 6.0) -> list[dict]:
        timeout = max(1.0, min(float(timeout), 30.0))
        tasks = [
            self._run(["avahi-browse", "-rtpk", svc], timeout=timeout)
            for svc in AVAHI_SERVICES
        ]
        tasks.append(self._run(["lpinfo", "-h", self.socket_path, "-v"], timeout=timeout))
        if self._runner is not None or shutil.which("driverless"):
            tasks.append(self._run(["driverless"], timeout=timeout))
        results = await asyncio.gather(*tasks, return_exceptions=True)

        avahi_results = results[:len(AVAHI_SERVICES)]
        lpinfo_res = results[len(AVAHI_SERVICES)]
        driverless_res = results[len(AVAHI_SERVICES) + 1] if len(results) > len(AVAHI_SERVICES) + 1 else None

        by_uri: dict[str, dict] = {}
        seen_service_proto: dict[tuple[str, str], dict] = {}

        def add_candidate(item: dict, *, priority: int = 1) -> None:
            uri = item["uri"]
            try:
                validate_printer_uri(uri)
            except CupsError:
                return
            existing = by_uri.get(uri)
            if existing is None or priority > existing.get("_prio", 0):
                item["_prio"] = priority
                by_uri[uri] = item

        # 1. Parse Avahi resolved rows ('=')
        for svc, res in zip(AVAHI_SERVICES, avahi_results):
            if isinstance(res, BaseException):
                continue
            rc, out, _ = res
            if rc != 0 or not out:
                continue
            for line in out.splitlines():
                if not line.startswith("=;"):
                    continue
                parts = line.split(";", 9)
                if len(parts) < 9:
                    continue
                _, iface, proto, raw_name, svc_type, _domain, host, addr, port_str = parts[:9]
                txt = _parse_avahi_txt(parts[9] if len(parts) > 9 else "")
                name = _unescape_avahi(raw_name).strip()
                if not name or "retroboot" in name.lower() and _is_loopback_host(addr):
                    continue
                try:
                    port = int(port_str)
                except ValueError:
                    continue
                if not (1 <= port <= 65535):
                    continue
                if iface == "lo" and not (
                    svc_type in ("_ipp._tcp", "_ipps._tcp")
                    and addr in ("127.0.0.1", "localhost")
                    and port >= IPP_USB_MIN_PORT
                ):
                    continue
                if _is_loopback_host(addr) and port in FORBIDDEN_LOOPBACK_PORTS:
                    continue

                is_ipv6 = ":" in addr
                # Prefer IPv4 or mDNS hostname over unzoned IPv6 link-local addresses.
                if is_ipv6 and addr.lower().startswith("fe80:") and host:
                    host_for_uri = host.rstrip(".")
                elif is_ipv6:
                    host_for_uri = f"[{addr}]"
                else:
                    host_for_uri = addr or host.rstrip(".")

                rp = txt.get("rp", "").lstrip("/")
                if svc_type == "_ipp._tcp":
                    transport = "ipp"
                    path = f"/{rp or 'ipp/print'}"
                    port_part = "" if port == 631 else f":{port}"
                    uri = f"ipp://{host_for_uri}{port_part}{path}"
                elif svc_type == "_ipps._tcp":
                    transport = "ipps"
                    path = f"/{rp or 'ipp/print'}"
                    port_part = "" if port == 631 else f":{port}"
                    uri = f"ipps://{host_for_uri}{port_part}{path}"
                elif svc_type == "_pdl-datastream._tcp":
                    transport = "socket"
                    port_part = f":{port}" if port != 9100 else ":9100"
                    uri = f"socket://{host_for_uri}{port_part}"
                elif svc_type == "_printer._tcp":
                    transport = "lpd"
                    queue = rp or "lp"
                    port_part = "" if port == 515 else f":{port}"
                    uri = f"lpd://{host_for_uri}{port_part}/{queue}"
                else:
                    continue

                model = txt.get("ty") or txt.get("usb_mdl") or txt.get("product") or name
                if model.startswith("(") and model.endswith(")") and len(model) > 2:
                    model = model[1:-1]
                pdl = txt.get("pdl", "").lower()
                if transport in ("ipp", "ipps") or any(
                    fmt in pdl for fmt in ("application/pdf", "image/pwg-raster", "image/urf")
                ):
                    driver_hint = "everywhere"
                else:
                    driver_hint = "generic_ps"

                cand = {
                    "id": hashlib.sha1(uri.encode("utf-8")).hexdigest()[:12],
                    "name": name,
                    "model": model,
                    "uri": uri,
                    "transport": transport,
                    "driver_hint": driver_hint,
                    "address": addr or host.rstrip("."),
                    "already_added": False,
                }
                # Deduplicate IPv4 vs IPv6 of the same service+transport (prefer IPv4).
                dedup_key = (name, transport)
                prev = seen_service_proto.get(dedup_key)
                if prev is not None and proto == "IPv6" and prev.get("_proto") == "IPv4":
                    continue
                if prev is not None and proto == "IPv4" and prev.get("_proto") == "IPv6":
                    by_uri.pop(prev["uri"], None)
                cand["_proto"] = proto
                seen_service_proto[dedup_key] = cand
                add_candidate(cand, priority=2)

        # 2. Parse lpinfo -v (USB and direct backends)
        if lpinfo_res is not None and not isinstance(lpinfo_res, BaseException):
            rc, out, _ = lpinfo_res
            if rc == 0 and out:
                for line in out.splitlines():
                    parts = line.strip().split(None, 1)
                    if len(parts) != 2 or "://" not in parts[1]:
                        continue
                    uri = parts[1].strip()
                    parsed = urlsplit(uri)
                    scheme = parsed.scheme.lower()
                    if scheme not in ALLOWED_SCHEMES:
                        continue
                    if scheme == "usb":
                        tail = unquote(parsed.path.lstrip("/"))
                        vendor = unquote(parsed.netloc)
                        label = f"{vendor} {tail}".strip() or "USB Printer"
                        address = "USB"
                        driver_hint = "generic_ps"
                    else:
                        label = unquote(parsed.hostname or uri).split("._", 1)[0]
                        address = parsed.hostname or ""
                        driver_hint = "everywhere" if scheme in ("ipp", "ipps", "dnssd") else "generic_ps"
                    add_candidate({
                        "id": hashlib.sha1(uri.encode("utf-8")).hexdigest()[:12],
                        "name": label,
                        "model": label,
                        "uri": uri,
                        "transport": scheme,
                        "driver_hint": driver_hint,
                        "address": address,
                        "already_added": False,
                    }, priority=1)

        # 3. Parse driverless
        if driverless_res is not None and not isinstance(driverless_res, BaseException):
            rc, out, _ = driverless_res
            if rc == 0 and out:
                for line in out.splitlines():
                    uri = line.strip()
                    if not uri or "://" not in uri:
                        continue
                    parsed = urlsplit(uri)
                    scheme = parsed.scheme.lower()
                    host = parsed.hostname or ""
                    if "._" in host:
                        label = unquote(host.split("._", 1)[0])
                    elif _is_loopback_host(host) and parsed.port and parsed.port >= IPP_USB_MIN_PORT:
                        label = unquote(parsed.path.strip("/").split("/")[-1] or "USB IPP Printer")
                    else:
                        label = host or uri
                    add_candidate({
                        "id": hashlib.sha1(uri.encode("utf-8")).hexdigest()[:12],
                        "name": label,
                        "model": label,
                        "uri": uri,
                        "transport": scheme,
                        "driver_hint": "everywhere",
                        "address": "USB" if (_is_loopback_host(host) and (parsed.port or 0) >= IPP_USB_MIN_PORT) else host,
                        "already_added": False,
                    }, priority=3 if scheme in ("ipp", "ipps") and not _is_loopback_host(host) else 2)

        try:
            configured = await self.list_printers()
            existing_uris = {p["uri"] for p in configured if p.get("uri")}
            existing_names = {p["name"] for p in configured if p.get("name")}
        except CupsError:
            existing_uris = set()
            existing_names = set()

        out_list: list[dict] = []
        for item in by_uri.values():
            clean = {k: v for k, v in item.items() if not k.startswith("_")}
            clean["already_added"] = (
                clean["uri"] in existing_uris
                or slugify_printer_name(clean["name"]) in existing_names
            )
            out_list.append(clean)
        out_list.sort(key=lambda d: (d["already_added"], d["name"].lower(), d["uri"]))
        return out_list

    async def add_printer(
        self,
        name: str,
        uri: str,
        *,
        description: str = "",
        location: str = "",
        driver: str = "auto",
    ) -> dict:
        queue = validate_queue_name(name)
        valid_uri = validate_printer_uri(uri)
        drv = (driver or "auto").strip().lower()
        if drv not in DRIVERS:
            raise CupsError(f"Unsupported printer driver {driver!r}.", "invalid_driver")
        clean_desc = _clean_text(description)
        clean_loc = _clean_text(location)
        scheme = urlsplit(valid_uri).scheme.lower()

        base = ["lpadmin", "-h", self.socket_path, "-p", queue, "-E", "-v", valid_uri]
        if clean_desc:
            base += ["-D", clean_desc]
        if clean_loc:
            base += ["-L", clean_loc]

        used_driver = drv
        if drv == "auto":
            if scheme in ("ipp", "ipps", "dnssd"):
                rc, out, err = await self._run(base + ["-m", "everywhere"], timeout=20.0)
                if rc == 0:
                    used_driver = "everywhere"
                else:
                    log.info("lpadmin everywhere failed for %s (%s); falling back to generic.ppd", queue, err.strip())
                    rc, out, err = await self._run(base + ["-m", "drv:///sample.drv/generic.ppd"], timeout=20.0)
                    used_driver = "generic_ps"
            else:
                rc, out, err = await self._run(base + ["-m", "drv:///sample.drv/generic.ppd"], timeout=20.0)
                used_driver = "generic_ps"
        elif drv == "everywhere":
            rc, out, err = await self._run(base + ["-m", "everywhere"], timeout=20.0)
        elif drv == "generic_ps":
            rc, out, err = await self._run(base + ["-m", "drv:///sample.drv/generic.ppd"], timeout=20.0)
        elif drv == "generic_pcl":
            rc, out, err = await self._run(base + ["-m", "drv:///sample.drv/generpcl.ppd"], timeout=20.0)
        else:  # raw
            rc, out, err = await self._run(base + ["-m", "raw"], timeout=20.0)

        if rc != 0:
            raise CupsError(err.strip() or out.strip() or f"Failed to add printer {queue}.", "add_failed")

        await self._run(["cupsenable", "-h", self.socket_path, queue], timeout=10.0)
        await self._run(["cupsaccept", "-h", self.socket_path, queue], timeout=10.0)
        return {
            "name": queue,
            "uri": valid_uri,
            "driver": used_driver,
            "description": clean_desc,
            "location": clean_loc,
            "lpd_queue": queue,
            "ipp_url": f"ipp://10.0.2.2:631/printers/{queue}",
        }

    async def remove_printer(self, name: str) -> dict:
        queue = validate_queue_name(name)
        rc, out, err = await self._run(["lpadmin", "-h", self.socket_path, "-x", queue], timeout=15.0)
        if rc != 0:
            msg = err.strip() or out.strip() or f"Failed to remove printer {queue}."
            low = msg.lower()
            if "does not exist" in low or "unknown" in low or "not found" in low:
                raise CupsError(f"Printer {queue!r} was not found.", "not_found")
            raise CupsError(msg, "remove_failed")
        return {"removed": queue}

    async def print_pdf(self, queue: str, pdf_path: Path, job_name: str = "Mac OS Print") -> dict:
        q = validate_queue_name(queue)
        title = _clean_text(job_name, 64) or "Mac OS Print"
        rc, out, err = await self._run(
            ["lp", "-h", self.socket_path, "-d", q, "-t", title, "--", str(pdf_path)],
            timeout=60.0,
        )
        if rc != 0:
            raise CupsError(err.strip() or out.strip() or f"CUPS print to {q} failed.", "print_failed")
        m = re.search(r"request id is\s+([A-Za-z0-9_.-]+)", out)
        job_id = m.group(1) if m else None
        return {"queue": q, "job_id": job_id, "title": title, "raw": out.strip()}

    async def list_jobs(self, queue: str | None = None) -> list[dict]:
        q_filter = validate_queue_name(queue) if queue is not None else None
        jobs: list[dict] = []
        for which, state in (("not-completed", "active"), ("completed", "completed")):
            cmd = ["lpstat", "-h", self.socket_path, "-W", which, "-o"]
            if q_filter is not None:
                cmd.append(q_filter)
            rc, out, _ = await self._run(cmd, timeout=10.0)
            if rc != 0 or not out:
                continue
            for line in out.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                parts = stripped.split(None, 3)
                if len(parts) < 3:
                    continue
                jid = parts[0]
                q_name = jid.rsplit("-", 1)[0] if "-" in jid else jid
                try:
                    size_bytes = int(parts[2])
                except ValueError:
                    size_bytes = 0
                jobs.append({
                    "job_id": jid,
                    "queue": q_name,
                    "user": parts[1],
                    "bytes": size_bytes,
                    "date": parts[3] if len(parts) > 3 else "",
                    "state": state,
                })
        return jobs[:30]

    async def cancel_job(self, job_id: str) -> dict:
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            raise CupsError("Invalid CUPS job ID.", "invalid_job_id")
        rc, out, err = await self._run(["cancel", "-h", self.socket_path, job_id], timeout=15.0)
        if rc != 0:
            msg = err.strip() or out.strip() or f"Could not cancel job {job_id}."
            low = msg.lower()
            if "not found" in low or "does not exist" in low or "canceled" in low or "already" in low:
                raise CupsError(f"Job {job_id!r} was not found.", "not_found")
            raise CupsError(msg, "cancel_failed")
        return {"canceled": job_id}
