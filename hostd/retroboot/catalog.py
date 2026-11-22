"""Machine catalog import (ClassicMac catalog schema 1) and archive sources.

Catalog entries are validated like ClassicMac's ``MachineCatalog.swift``.
Archives download over HTTPS only (redirects must keep scheme/host/port),
resume with ``Range``, and must match the advertised size and SHA-256 before
extraction.  An entry may also offer a RetroBoot *thin archive*
(``thinArchive``: format, URL, bytes, SHA-256), which hostd prefers; ClassicMac
ignores the field.  Extraction is streaming and single pass into a private
staging directory: only flat regular ``config.json`` (<= 64 KiB), ``disk.img``
(positive multiple of 512) and optional ``preview.png`` (<= 16 MiB) are
accepted; links, paths, directories, duplicates and extra files abort the
import and remove staging.  ``disk.img`` is either a regular member (written
sparse: all-zero 1 MiB chunks become holes) or a GNU PAX sparse 1.0 member
whose map ``sparsetar`` validates strictly (thin archives: only the disk's
data is decompressed and written).  The finished machine is moved into place
atomically and existing machines are never replaced.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import tarfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import urlsplit

from . import sparsetar
from .machines import (PRAM_FILE, QUADRA800, ROM_FILE, TEMPLATE_FAMILIES, OSX_CATALOG_FAMILY, OSX_PROFILES,
                       MachineError, MachineRegistry,
                       direct_framebuffer, family_fields, family_of_template, guest_os_of_template, unique_machine_id,
                       validate_meta)
from .util import atomic_write_json, run as run_cmd, wall

log = logging.getLogger("retroboot.catalog")

GiB = 1024 ** 3
MiB = 1024 ** 2
MAX_ARCHIVE = 140 * GiB
MAX_CATALOG = 1024 * 1024
CHUNK = MiB
ZERO = bytes(CHUNK)
RESERVE = 256 * MiB
ID_RE = re.compile(r"^[a-z0-9._-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^\d{1,6}\.\d{1,6}(\.\d{1,6})?$")


class ImportError_(Exception):
    def __init__(self, message: str, code: str = "import_failed"):
        super().__init__(message)
        self.message = message
        self.code = code


def _text(v, maximum) -> bool:
    return isinstance(v, str) and v.strip() != "" and len(v) <= maximum and \
        not any((ord(c) < 32 and c != "\n") or ord(c) == 127 for c in v)


def secure_url(url) -> bool:
    if not isinstance(url, str) or len(url) > 2048:
        return False
    u = urlsplit(url)
    return u.scheme == "https" and bool(u.hostname) and u.username is None and u.password is None and not u.fragment


def validate_entry(e) -> dict:
    if not isinstance(e, dict):
        raise ImportError_("A machine contains invalid download information.", "invalid_catalog")
    ok = (isinstance(e.get("id"), str) and ID_RE.fullmatch(e["id"]) and not e["id"].startswith(".")
          and _text(e.get("name"), 100) and _text(e.get("summary"), 1000)
          and _text(e.get("osVersion"), 80) and _text(e.get("gxMetalVersion", "n/a"), 80)
          and isinstance(e.get("minimumAppVersion", "3.0.0"), str)
          and VERSION_RE.fullmatch(e.get("minimumAppVersion", "3.0.0"))
          and secure_url(e.get("archiveURL"))
          and type(e.get("archiveBytes")) is int and 0 < e["archiveBytes"] <= MAX_ARCHIVE
          and type(e.get("installedBytes")) is int and 512 <= e["installedBytes"] <= MAX_ARCHIVE
          and isinstance(e.get("sha256"), str) and SHA_RE.fullmatch(e["sha256"]))
    if not ok:
        raise ImportError_("A machine contains invalid download information.", "invalid_catalog")
    cap = e.get("diskCapacityBytes")
    if cap is not None:
        if not (type(cap) is int and cap >= 512 and cap % 512 == 0 and cap < e["installedBytes"]
                and e["installedBytes"] - cap <= 16 * MiB + 65536):
            raise ImportError_("Its disk capacity does not match the expanded files.", "invalid_catalog")
    req = e.get("requiredStorageBytes")
    if req is not None:
        if not (cap is not None and type(req) is int and req >= MiB and req % MiB == 0
                and req <= e["installedBytes"] + 3 * MiB):
            raise ImportError_("Its initial storage requirement is not valid.", "invalid_catalog")
    fam = e.get("machineFamily")
    if fam is not None and (not isinstance(fam, str) or fam not in TEMPLATE_FAMILIES):
        raise ImportError_("This kind of Mac isn't supported by this RetroBoot.", "unsupported")
    try:
        profile = guest_os_of_template({"machineFamily": fam or "powerMacG4", **({"guestOS": e["guestOS"]} if "guestOS" in e else {})})
        if profile in OSX_PROFILES:
            from .osx_identity import validate
            # Catalog identity must fit canonical machine metadata. Native controls
            # still refresh SystemVersion.plist from the stopped disk before launch.
            validate(profile, e["osVersion"], e.get("osBuild"))
    except ValueError as exc:
        raise ImportError_(str(exc), "unsupported") from exc
    keep = ("id", "name", "summary", "osVersion", "gxMetalVersion", "minimumAppVersion", "archiveURL",
            "archiveBytes", "installedBytes", "sha256", "diskCapacityBytes", "requiredStorageBytes",
            "machineFamily", "guestOS", "osBuild")
    out = {k: e[k] for k in keep if k in e}
    thin = _thin_archive(e.get("thinArchive"))
    if thin is not None:
        out["thinArchive"] = thin
    return out


def _thin_archive(t) -> dict | None:
    """The optional RetroBoot thin archive of an entry (same machine, sparse disk member).

    A format this hostd does not know is ignored (the classic archive is used);
    a known format with invalid fields makes the entry invalid.
    """
    if t is None:
        return None
    if not isinstance(t, dict):
        raise ImportError_("A machine contains invalid download information.", "invalid_catalog")
    if t.get("format") != sparsetar.FORMAT:
        return None
    if not (secure_url(t.get("archiveURL")) and type(t.get("archiveBytes")) is int
            and 0 < t["archiveBytes"] <= MAX_ARCHIVE and isinstance(t.get("sha256"), str)
            and SHA_RE.fullmatch(t["sha256"])):
        raise ImportError_("A machine contains invalid download information.", "invalid_catalog")
    return {k: t[k] for k in ("format", "archiveURL", "archiveBytes", "sha256")}


def download_of(entry: dict) -> dict:
    """What hostd downloads for an entry: its thin archive when offered, else the classic one."""
    t = entry.get("thinArchive")
    if t:
        return {"format": t["format"], "archiveURL": t["archiveURL"], "archiveBytes": t["archiveBytes"],
                "sha256": t["sha256"]}
    return {"format": "classic", "archiveURL": entry["archiveURL"], "archiveBytes": entry["archiveBytes"],
            "sha256": entry["sha256"]}


def validate_catalog(data) -> list[dict]:
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        raise ImportError_("The machine catalog format is not recognized.", "invalid_catalog")
    machines = data.get("machines")
    if not isinstance(machines, list) or len(machines) > 50:
        raise ImportError_("The catalog's machine list is not valid.", "invalid_catalog")
    entries = []
    for m in machines:
        try:
            entries.append(validate_entry(m))
        except ImportError_ as exc:
            if exc.code != "unsupported":
                raise
            # A kind of Mac this RetroBoot can't run (ClassicMac's Copland machine): left out.
            log.info("catalog entry %r left out: machineFamily %r", m.get("id"), m.get("machineFamily"))
    if len({e["id"] for e in entries}) != len(entries):
        raise ImportError_("The catalog's machine list is not valid.", "invalid_catalog")
    return entries


Progress = Callable[[float | None, str], None]


class CatalogClient:
    def __init__(self, url: str, downloads: Path, timeout: float = 30.0, ca_file: str | None = None,
                 auth=None):
        self.url = url
        self.downloads = Path(downloads)
        self.timeout = timeout
        self.ca_file = ca_file   # LAB ONLY (hostd.json catalog_ca_file): trusted besides the system CAs
        self.auth = auth         # LAB ONLY (labauth.LabAuth): Basic auth for URLs under the lab base
        self.cache: list[dict] | None = None
        self.cache_time = 0.0
        self.verify_tls = True   # tests only turn this off for a local self-signed server

    def _ssl(self):
        if not self.verify_tls:
            return False
        if not self.ca_file:
            return None          # aiohttp's default verification against the system CAs
        import ssl
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cafile=self.ca_file)
        return ctx

    def _session(self):
        import aiohttp
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=self.timeout,
                                                                   sock_read=self.timeout),
                                     connector=aiohttp.TCPConnector(ssl=self._ssl()),
                                     auto_decompress=False)

    async def _get(self, session, url: str, headers: dict | None = None):
        """GET following only same-origin HTTPS redirects."""
        from .labauth import headers_for
        origin = urlsplit(url)
        for _ in range(5):
            resp = await session.get(url, headers={**(headers or {}), **headers_for(self.auth, url)},
                                     allow_redirects=False)
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location", "")
                resp.release()
                from yarl import URL
                nxt = urlsplit(str(resp.url.join(URL(loc))))
                if nxt.scheme != "https" or nxt.hostname != origin.hostname or nxt.port != origin.port:
                    raise ImportError_("The download redirected to another server.", "redirect_rejected")
                url = nxt.geturl()
                continue
            return resp
        raise ImportError_("Too many redirects.", "redirect_rejected")

    async def fetch(self, max_age: float = 60.0) -> list[dict]:
        if self.cache is not None and time.monotonic() - self.cache_time < max_age:
            return self.cache
        if not secure_url(self.url):
            raise ImportError_("The catalog address is not secure.", "invalid_catalog")
        try:
            async with self._session() as s:
                resp = await self._get(s, self.url)
                async with resp:
                    if resp.status == 401:
                        # A private catalog (lab downloads) without, or with a wrong, sign-in.
                        raise ImportError_("This catalog needs a user name and password.", "auth_required")
                    if resp.status != 200:
                        raise ImportError_(f"The catalog could not be loaded (HTTP {resp.status}).", "network")
                    # read(n) may return only the first available network fragment.
                    body = bytearray()
                    async for chunk in resp.content.iter_chunked(64 << 10):
                        if len(body) + len(chunk) > MAX_CATALOG:
                            raise ImportError_("The catalog is too large.", "invalid_catalog")
                        body.extend(chunk)
        except ImportError_:
            raise
        except Exception as exc:
            raise ImportError_("The catalog could not be loaded. Check the network connection.", "network") from exc
        try:
            data = json.loads(body)
        except ValueError:
            raise ImportError_("The machine catalog format is not recognized.", "invalid_catalog")
        self.cache = validate_catalog(data)
        self.cache_time = time.monotonic()
        return self.cache

    async def download(self, entry: dict, progress: Progress) -> Path:
        """Download (resuming) and verify size + SHA-256.  Returns the verified archive path.

        The entry's thin archive is preferred when it offers one (``download_of``).
        """
        entry = download_of(entry)
        self.downloads.mkdir(parents=True, exist_ok=True)
        final = self.downloads / f"{entry['sha256']}.tar.gz"
        total = entry["archiveBytes"]
        if final.exists() and final.stat().st_size == total:
            progress(0.0, "Checking the downloaded file…")
            if await asyncio.to_thread(_sha256, final) == entry["sha256"]:
                return final
            final.unlink()
        part = self.downloads / f"{entry['sha256']}.part"
        if shutil.disk_usage(self.downloads).free < total - (part.stat().st_size if part.exists() else 0) + RESERVE:
            raise ImportError_("There isn't enough free space for the download.", "no_space")
        h = hashlib.sha256()
        have = 0
        if part.exists():
            have = part.stat().st_size
            if have > total:
                part.unlink()
                have = 0
            else:
                await asyncio.to_thread(_hash_into, part, h)
        attempts = 0
        while have < total:
            attempts += 1
            try:
                async with self._session() as s:
                    headers = {"Range": f"bytes={have}-"} if have else {}
                    resp = await self._get(s, entry["archiveURL"], headers)
                    async with resp:
                        if resp.status == 200 and have:
                            # Server ignored Range: restart cleanly.
                            have = 0
                            h = hashlib.sha256()
                            mode = "wb"
                        elif resp.status == 206 and have:
                            cr = resp.headers.get("Content-Range", "")
                            if not cr.startswith(f"bytes {have}-"):
                                raise ImportError_("The server returned an unexpected range.", "network")
                            mode = "ab"
                        elif resp.status == 200:
                            mode = "wb"
                        elif resp.status == 401:
                            raise ImportError_("The server did not accept the sign-in.", "auth_required")
                        else:
                            raise ImportError_(f"The download failed (HTTP {resp.status}).", "network")
                        enc = resp.headers.get("Content-Encoding", "identity")
                        if enc not in ("", "identity"):
                            raise ImportError_("The server sent an encoded archive.", "network")
                        with open(part, mode) as f:
                            last = 0.0
                            async for chunk in resp.content.iter_chunked(256 * 1024):
                                have += len(chunk)
                                if have > total:
                                    raise ImportError_("The download is larger than expected.", "integrity")
                                f.write(chunk)
                                h.update(chunk)
                                now = time.monotonic()
                                if now - last > 0.25:
                                    last = now
                                    progress(have / total, f"Downloading… {have // MiB} of {total // MiB} MB")
            except ImportError_:
                raise
            except Exception as exc:
                if attempts >= 4:
                    raise ImportError_("The download could not finish. Check the network connection.", "network") from exc
                await asyncio.sleep(min(10, 2 ** attempts))
        if h.hexdigest() != entry["sha256"]:
            part.unlink(missing_ok=True)
            raise ImportError_("The downloaded file did not pass its integrity check. Please retry.", "integrity")
        os.replace(part, final)
        return final


def _hash_into(path: Path, h) -> None:
    with open(path, "rb") as f:
        while True:
            b = f.read(4 * MiB)
            if not b:
                return
            h.update(b)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    _hash_into(path, h)
    return h.hexdigest()


@dataclass
class ExtractLimits:
    installed_bytes: int | None = None     # exact expansion bound when known
    storage_budget: int | None = None      # allocation bound (requiredStorageBytes)
    # Stop before the file system holding the staging directory has less than this
    # free (an upload or USB archive has no advertised size; its sparse disk.img may
    # still need more space than is left).  None: no check.
    min_free_bytes: int | None = None


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


# A 68k Mac's archive may also carry its ROM and PRAM (quadra.py; checked in install_machine).
ALLOWED = {"config.json": 64 * 1024, "disk.img": MAX_ARCHIVE, "preview.png": 16 * MiB,
           ROM_FILE: 4 * MiB, PRAM_FILE: 4096, "nvram.img": 16384}
_DAMAGED = (tarfile.TarError, EOFError, OSError, ValueError, zlib.error)


class _Writer:
    """Writes one member into staging: only non-zero chunks, holes elsewhere, within the limits."""

    def __init__(self, out, name: str, staging: Path, budget: int | None, limits: ExtractLimits,
                 progress, cancelled, total: int, stored_before: int):
        self.out, self.fd, self.name, self.staging = out, out.fileno(), name, staging
        self.budget, self.limits, self.progress, self.cancelled = budget, limits, progress, cancelled
        self.total, self.stored, self.stored_before = max(1, total), stored_before, stored_before
        self.done = 0
        self.last = 0.0

    def put(self, offset: int, chunk: bytes) -> None:
        if self.cancelled():
            raise ImportError_("Cancelled.", "cancelled")
        self.done += len(chunk)
        if chunk != ZERO[: len(chunk)] or self.name != "disk.img":
            os.pwrite(self.fd, chunk, offset)
            self.stored += len(chunk)
            if self.budget is not None and self.stored > self.budget:
                raise ImportError_("The machine needs more space than advertised.", "no_space")
        now = time.monotonic()
        if self.progress and now - self.last > 0.5:
            self.last = now
            self.progress(self.done / self.total, f"Installing {self.name}…")
            if self.limits.min_free_bytes is not None and _free_bytes(self.staging) < self.limits.min_free_bytes:
                raise ImportError_("There isn't enough free space to install this machine.", "no_space")


def _check_trailer(gz) -> None:
    """After the tar's end: only zero padding (bounded), then a gzip end whose CRC and length match."""
    left = MiB
    try:
        while True:
            b = gz.read(64 * 1024)
            if not b:
                return
            left -= len(b)
            if left < 0 or b.count(0) != len(b):
                raise ImportError_("The archive has unexpected data after its end.", "unsafe_archive")
    except _DAMAGED as exc:
        raise ImportError_("The archive is damaged.", "unsafe_archive") from exc


def _hash_zeros(h, n: int) -> None:
    while n > 0:
        k = min(n, CHUNK)
        h.update(ZERO if k == CHUNK else ZERO[:k])
        n -= k


def extract_archive(archive: Path, staging: Path, limits: ExtractLimits,
                    progress: Callable[[float | None, str], None] | None = None,
                    cancelled: Callable[[], bool] = lambda: False, hash_disk: bool = False) -> dict:
    """Validate and extract in a single streaming pass.  Raises ImportError_; caller removes staging.

    ``disk.img`` may be a regular member (every byte stored; zero chunks become
    holes) or a thin GNU PAX sparse 1.0 member whose map passes
    ``sparsetar.check_sparse_member``; then only its data extents are read and
    written.  Either way the file is sized first, so writes never extend it
    (XFS keeps speculative preallocation it makes past EOF inside a file that
    grows by seeking, which inflated catalog imports by about 70 %).

    The disk's logical SHA-256 is computed only with ``hash_disk`` (lab checks):
    on a 32 GiB disk it cost three quarters of a classic import, and the
    archive's SHA-256 (catalog or sidecar) and gzip CRC already cover the bytes.
    """
    staging.mkdir(parents=True, exist_ok=False)
    seen: dict[str, dict] = {}
    expanded = 0
    # Data actually stored (zero chunks of disk.img become holes).  This, not st_blocks, is
    # compared with the advertised storage: XFS (RBDATA) counts speculative preallocation
    # beyond EOF in st_blocks while a large sparse file grows, which made every catalog
    # import with requiredStorageBytes fail at once on hardware.
    stored = 0
    budget = None if limits.storage_budget is None else limits.storage_budget + 64 * MiB
    bound = min(limits.installed_bytes or MAX_ARCHIVE, MAX_ARCHIVE)
    try:
        raw = open(archive, "rb")
    except OSError as exc:
        raise ImportError_("The archive is not a valid machine package.", "unsafe_archive") from exc
    with raw, gzip.GzipFile(fileobj=raw, mode="rb") as gz:
        guard = sparsetar.GuardedReader(gz)
        try:
            tf = tarfile.TarFile(fileobj=guard, mode="r")
        except (*_DAMAGED, sparsetar.Unsafe) as exc:
            raise ImportError_("The archive is not a valid machine package.", "unsafe_archive") from exc
        with tf:
            while True:
                guard.headers()
                try:
                    member = tf.next()
                except (*_DAMAGED, sparsetar.Unsafe) as exc:
                    raise ImportError_("The archive is damaged.", "unsafe_archive") from exc
                guard.data()
                if member is None:
                    _check_trailer(gz)
                    break
                name = member.name
                pp = PurePosixPath(name)
                if pp.is_absolute() or len(pp.parts) != 1 or name not in ALLOWED:
                    raise ImportError_("The archive contains unexpected files.", "unsafe_archive")
                if not member.isreg() or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or tf.pax_headers:
                    raise ImportError_("Links and special archive members are not supported.", "unsafe_archive")
                sparse = member.issparse()
                try:
                    if sparse:
                        if name != "disk.img":
                            raise sparsetar.Unsafe("only disk.img may be sparse")
                        extents, data_bytes = sparsetar.check_sparse_member(member, tf.offset)
                    else:
                        sparsetar.check_plain_member(member)
                        extents, data_bytes = ([(0, member.size)] if member.size else []), member.size
                except sparsetar.Unsafe as exc:
                    raise ImportError_("The archive's disk map is not valid.", "unsafe_archive") from exc
                if name in seen:
                    raise ImportError_("The archive contains duplicate files.", "unsafe_archive")
                if member.size > ALLOWED[name] or (name == "disk.img" and (member.size <= 0 or member.size % 512)):
                    raise ImportError_(f"The archive's {name} has an invalid size.", "unsafe_archive")
                expanded += member.size
                if expanded > bound:
                    raise ImportError_("The archive is larger than advertised.", "unsafe_archive")
                if sparse:
                    # A thin disk declares its data up front: check space before writing anything.
                    if budget is not None and stored + data_bytes > budget:
                        raise ImportError_("The machine needs more space than advertised.", "no_space")
                    if limits.min_free_bytes is not None and \
                            _free_bytes(staging) < data_bytes + limits.min_free_bytes:
                        raise ImportError_("There isn't enough free space to install this machine.", "no_space")
                out_path = staging / name
                h = hashlib.sha256() if name != "disk.img" or hash_disk else None
                with open(out_path, "xb") as out:
                    os.ftruncate(out.fileno(), member.size)
                    w = _Writer(out, name, staging, budget, limits, progress, cancelled, data_bytes, stored)
                    try:
                        guard.seek(member.offset_data)
                        pos = 0
                        for off, size in extents:
                            if h is not None:
                                _hash_zeros(h, off - pos)
                            end = off + size
                            while off < end:
                                chunk = guard.read(min(CHUNK, end - off))
                                if not chunk:
                                    raise ImportError_("The archive is damaged.", "unsafe_archive")
                                if h is not None:
                                    h.update(chunk)
                                w.put(off, chunk)
                                off += len(chunk)
                            pos = end
                        if h is not None:
                            _hash_zeros(h, member.size - pos)
                    except (*_DAMAGED, sparsetar.Unsafe) as exc:
                        raise ImportError_("The archive is damaged.", "unsafe_archive") from exc
                    stored = w.stored
                    out.flush()
                    os.fsync(out.fileno())
                if w.done != data_bytes:
                    raise ImportError_("The archive is damaged.", "unsafe_archive")
                os.chmod(out_path, 0o660 if name == "disk.img" else 0o644)
                st = out_path.stat()
                rec = {"bytes": member.size, "sha256": h.hexdigest() if h else None,
                       "allocated_bytes": st.st_blocks * 512}
                if name == "disk.img":
                    rec.update(format="sparse" if sparse else "regular", stored_bytes=w.stored - w.stored_before,
                               extents=len(extents) if sparse else None)
                seen[name] = rec
    if "config.json" not in seen or "disk.img" not in seen:
        raise ImportError_("The archive is missing required files.", "unsafe_archive")
    if limits.installed_bytes is not None and expanded != limits.installed_bytes:
        raise ImportError_("The archive does not match its catalog description.", "unsafe_archive")
    return {"files": seen, "expanded_bytes": expanded, "stored_bytes": stored}


def sanitize_template_config(raw: bytes) -> dict:
    try:
        cfg = json.loads(raw)
    except ValueError as exc:
        raise ImportError_("The machine settings in the archive are not readable.", "unsafe_archive") from exc
    if not isinstance(cfg, dict):
        raise ImportError_("The machine settings in the archive are not readable.", "unsafe_archive")
    if family_of_template(cfg) is None:
        # Power Mac G4 (mac99) and Quadra 800 (q800, quadra.py) Macs only.
        raise ImportError_("This kind of Mac isn't supported by this RetroBoot.", "unsupported")
    keep = {}
    try:
        profile = guest_os_of_template(cfg)
    except MachineError as exc:
        raise ImportError_(str(exc), "unsupported") from exc
    if "guestOS" in cfg:
        keep["guestOS"] = profile
    for k, t in (("name", str), ("width", int), ("height", int), ("depth", int), ("ramMB", int),
                 ("useG4CPU", bool), ("sound", bool), ("networking", bool), ("tabletInput", bool),
                 ("diskSizeGB", int), ("machineFamily", str)):
        if isinstance(cfg.get(k), t):
            keep[k] = cfg[k][:100] if isinstance(cfg[k], str) else cfg[k]
    if "directFramebuffer" in cfg:
        try:
            keep["directFramebuffer"] = direct_framebuffer(cfg)
        except MachineError as exc:
            raise ImportError_(str(exc), "unsafe_archive") from exc
    if "osxDisplaySetup" in cfg:
        if profile != "macosx-10.4" or cfg["osxDisplaySetup"] != "firmware-desktop-1":
            raise ImportError_("The startup display settings aren't supported for this Mac.", "unsafe_archive")
        keep["osxDisplaySetup"] = cfg["osxDisplaySetup"]
    return keep


async def install_machine(archive: Path, registry: MachineRegistry | None, display_name: str,
                          os_version: str | None, source: dict, limits: ExtractLimits,
                          progress: Progress, cancelled: Callable[[], bool] = lambda: False,
                          machines_dir: Path | None = None, machine_id: str | None = None,
                          allowed_families: set[str] | None = None,
                          expected_family: str | None = None, expected_guest_os: str | None = None,
                          os_build: str | None = None) -> str:
    """Extract into staging and atomically register a new machine.  Returns its id.

    ``allowed_families`` (the kinds of Mac this computer can start, catalogs.runnable_families)
    refuses an archive of another kind; ``expected_family`` (a catalog entry's) refuses an
    archive whose config.json names another kind than its catalog entry.

    ``machines_dir`` installs into another machines directory than the registry's: the
    installer extracts downloads straight onto the disk it is installing on (RBDATA of
    the target, mounted by ``install-to-disk --mount-data``), never into the live system's
    memory.  ``machine_id`` is the preferred id there (made unique in that directory).
    """
    machines = Path(machines_dir) if machines_dir is not None else registry.paths.machines
    machines.mkdir(parents=True, exist_ok=True)
    need = (limits.storage_budget or limits.installed_bytes or 0) + RESERVE
    if shutil.disk_usage(machines).free < need:
        raise ImportError_("There isn't enough free space to install this machine.", "no_space")
    staging = machines / f".staging-{os.getpid()}-{int(time.time() * 1000)}"
    if limits.min_free_bytes is None:
        limits = ExtractLimits(limits.installed_bytes, limits.storage_budget, RESERVE)
    try:
        info = await asyncio.to_thread(extract_archive, archive, staging, limits,
                                       lambda f, m: progress(f, m), cancelled)
        template = sanitize_template_config((staging / "config.json").read_bytes())
        family = family_of_template(template)
        profile = guest_os_of_template(template)
        catalog_family = OSX_CATALOG_FAMILY if profile in OSX_PROFILES else family
        if (expected_family is not None and catalog_family != expected_family) or \
                (expected_guest_os is not None and profile != expected_guest_os):
            raise ImportError_("The archive does not match its catalog description.", "unsafe_archive")
        if allowed_families is not None and catalog_family not in allowed_families:
            raise ImportError_(UNRUNNABLE.get(catalog_family, "This kind of Mac can't start on this RetroBoot."),
                               "unsupported")
        _check_family_files(staging, template, info["files"])
        if machines_dir is not None:
            mid = unique_machine_id(machine_id or display_name or template.get("name") or "mac-os", machines)
        else:
            mid = registry.unique_id(display_name or template.get("name") or "mac-os")
        meta = {"schema_version": 1, "id": mid, "name": (display_name or template.get("name") or "Mac OS")[:100],
                "os_version": os_version, "disk": "disk.img", "created": wall(), "source": source,
                "template": template, "files": info["files"], **family_fields(template)}
        if profile in OSX_PROFILES and os_build is not None:
            meta["os_build"] = os_build
        try:
            validate_meta(meta, mid)
        except MachineError as exc:
            raise ImportError_(str(exc), "unsafe_archive") from exc
        atomic_write_json(staging / "machine.json", meta)
        atomic_write_json(staging / "state.json", {"clean": True, "last_exit": None})
        os.chmod(staging, 0o770)
        dest = machines / mid
        os.rename(staging, dest)   # fails if dest exists: never replace a machine
        return mid
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


# Why a Mac can't be installed here (allowed_families).
UNRUNNABLE = {QUADRA800: "This is a 68k Mac, and this RetroBoot doesn't have the 68k emulator to start it. "
                         "Install the latest RetroBoot update first.",
             OSX_CATALOG_FAMILY: "This Mac needs Mac OS X support. Install the latest RetroBoot update first."}


def _check_family_files(staging: Path, template: dict, files: dict) -> None:
    """A 68k Mac's ROM and PRAM: only in a 68k Mac's archive, the ROM one RetroBoot can use
    (a Quadra Mac without one gets the ROM store's at its first start), the PRAM a PRAM."""
    if "nvram.img" in files:
        from .osx import read_nvram
        if not guest_os_of_template(template).startswith("macosx-"):
            raise ImportError_("Only a Mac OS X archive can contain OS X startup memory.", "unsafe_archive")
        try:
            read_nvram(staging / "nvram.img")
        except MachineError as exc:
            raise ImportError_(str(exc), "unsafe_archive") from exc
        os.chmod(staging / "nvram.img", 0o660)
    extra = [n for n in (ROM_FILE, PRAM_FILE) if n in files]
    if not extra:
        return
    if family_of_template(template) != QUADRA800:
        raise ImportError_("The archive contains unexpected files.", "unsafe_archive")
    if ROM_FILE in files:
        from .quadra import check_rom
        try:
            check_rom(staging / ROM_FILE)
        except MachineError:
            raise ImportError_("The archive's Quadra 800 ROM isn't one RetroBoot can use.", "unsupported_rom") \
                from None
    if PRAM_FILE in files:
        from .quadra import PRAM_SIZES
        if files[PRAM_FILE]["bytes"] not in PRAM_SIZES:
            raise ImportError_("The archive's PRAM file isn't valid.", "unsafe_archive")
        os.chmod(staging / PRAM_FILE, 0o660)


# --- Removable and embedded archive sources ------------------------------------

def _archive_id(path: Path) -> str:
    return hashlib.sha256(str(path).encode()).hexdigest()[:16]


def _sidecar_sha(path: Path) -> str | None:
    side = path.with_name(path.name + ".sha256")
    try:
        txt = side.read_text()[:200].strip().split()
        if txt and SHA_RE.fullmatch(txt[0].lower()):
            return txt[0].lower()
    except OSError:
        pass
    return None


def scan_archives(directory: Path, origin: str, depth: int = 1) -> list[dict]:
    found = []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return found
    for p in entries:
        if p.name.startswith("."):
            continue
        try:
            if p.is_symlink():
                continue
            if p.is_dir() and depth > 0:
                found += scan_archives(p, origin, depth - 1)
            elif p.is_file() and p.name.lower().endswith((".tar.gz", ".tgz")):
                size = p.stat().st_size
                if 1024 <= size <= MAX_ARCHIVE:
                    found.append({"id": _archive_id(p), "name": p.name, "bytes": size, "origin": origin,
                                  "sha256": _sidecar_sha(p), "_path": str(p)})
        except OSError:
            continue
    return found


class ArchiveSources:
    """Removable USB media (mounted read-only on demand) and the installer's embedded fixture."""

    FS_TYPES = ("vfat", "exfat", "ext4", "ext3", "ext2", "ntfs", "ntfs3", "iso9660", "udf", "xfs", "btrfs")

    def __init__(self, media_root: Path, embedded_dir: Path, enabled: bool = True, runner=None):
        self.media_root = Path(media_root)
        self.embedded_dir = Path(embedded_dir)
        self.enabled = enabled
        self.run = runner or run_cmd
        self.known: dict[str, dict] = {}
        self.mounted: list[Path] = []

    async def removable_partitions(self) -> list[dict]:
        if not self.enabled:
            return []
        rc, out, _ = await self.run(["lsblk", "-J", "-b", "-o",
                                     "NAME,PATH,RM,HOTPLUG,TRAN,FSTYPE,LABEL,MOUNTPOINT,SIZE,TYPE"], timeout=10)
        if rc != 0:
            return []
        try:
            data = json.loads(out)
        except ValueError:
            return []
        parts = []

        def walk(devs, removable):
            for d in devs or []:
                rem = removable or bool(d.get("rm")) or bool(d.get("hotplug")) or d.get("tran") == "usb"
                if d.get("fstype") in self.FS_TYPES and rem:
                    parts.append(d)
                walk(d.get("children"), rem)

        walk(data.get("blockdevices"), False)
        return parts

    async def list(self) -> list[dict]:
        found = []
        if self.embedded_dir.is_dir():
            found += scan_archives(self.embedded_dir, "embedded", depth=0)
        for part in await self.removable_partitions():
            mp = part.get("mountpoint")
            if not mp:
                name = re.sub(r"[^A-Za-z0-9_-]", "_", part.get("name") or "usb")
                target = self.media_root / name
                target.mkdir(parents=True, exist_ok=True)
                rc, _, err = await self.run(["mount", "-o", "ro,nosuid,nodev,noexec", part["path"], str(target)],
                                            timeout=20)
                if rc != 0:
                    log.info("mount %s failed: %s", part.get("path"), err.strip())
                    continue
                self.mounted.append(target)
                mp = str(target)
            found += scan_archives(Path(mp), "usb:" + (part.get("label") or part.get("name") or "USB"), depth=1)
        self.known = {a["id"]: a for a in found}
        return [{k: v for k, v in a.items() if not k.startswith("_")} for a in found]

    def resolve(self, archive_id: str) -> dict | None:
        return self.known.get(archive_id)

    async def release(self) -> None:
        for mp in self.mounted:
            await self.run(["umount", str(mp)], timeout=20)
        self.mounted = []
