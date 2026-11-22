"""Media library: disc images for the CD-ROM drive and software to install.

``/var/lib/retroboot/media/`` (``Paths.media_library``) holds files the user
added (web upload, or copied there by the lab/installer).  Every file is
identified by content, never by its extension alone:

* ``cd-image``  — Apple Partition Map, bare HFS/HFS+ or ISO 9660 image larger
  than 4 MiB (Myst).  Setting ``media.cd`` (apply ``guest_restart``) puts one
  in the emulated IDE CD-ROM drive for the next Mac OS start: Mac OS 9 does
  not notice IDE media changes while it runs.  Only a registered library file
  can be chosen, never a host path.
* ``archive``   — StuffIt, BinHex, MacBinary, zip (SimCity, Speedometer).
* ``disk-image`` — Disk Copy 4.2 / NDIF / small HFS images (floppies).

Archives and disk images are installed into the active machine's own disk
(job ``media.install``) while Mac OS is shut down: ``rbapps`` (installed at
``/usr/lib/retroboot/apps``) expands them into a canonical tree with data
forks, resource forks and Finder info, and ``rbguestprep.install`` copies the
tree into a reflink/sparse copy of the disk that replaces the original only
after its forks and ``fsck.hfsplus`` verify.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .util import run as run_cmd, wall

log = logging.getLogger("retroboot.media")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+,'&!-]{0,126}$")
CD_MIN_BYTES = 4 * 1024 * 1024
MAX_UPLOAD = 8 * 1024 ** 3
RESERVE = 512 * 1024 * 1024
CD_KINDS = ("cd-image",)
INSTALL_KINDS = ("archive", "disk-image")
KIND_LABELS = {"cd-image": "CD image", "archive": "Software archive", "disk-image": "Disk image",
               "unknown": "Unknown file"}


class MediaError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _hfs_name(head: bytes, off: int) -> str | None:
    """Volume name from an HFS MDB or (for HFS+) None; head holds bytes from ``off``."""
    mdb = head[off + 1024:off + 1024 + 64]
    if mdb[:2] == b"BD" and 0 < mdb[0x24] <= 27:
        return mdb[0x25:0x25 + mdb[0x24]].decode("mac_roman")
    return None


def identify(path: Path) -> dict:
    """Content sniffing (no extraction).  Returns kind, format, volume_name."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(64 * 1024)
    out: dict[str, Any] = {"kind": "unknown", "format": None, "volume_name": None}
    if head[:2] == b"ER" and head[512:514] == b"PM":
        n = struct.unpack(">I", head[516:520])[0]
        for i in range(1, min(n, 63) + 1):
            e = head[512 * i:512 * i + 512]
            if e[:2] != b"PM":
                break
            if e[48:80].split(b"\0")[0] == b"Apple_HFS":
                start = struct.unpack(">I", e[8:12])[0] * 512
                with open(path, "rb") as f:
                    f.seek(start)
                    out["volume_name"] = _hfs_name(f.read(2048), 0)
                break
        out.update(kind="cd-image" if size > CD_MIN_BYTES else "disk-image", format="apple-partition-map")
    elif len(head) > 1026 and head[1024:1026] in (b"BD", b"H+", b"HX"):
        out.update(kind="cd-image" if size > CD_MIN_BYTES else "disk-image",
                   format="hfs" if head[1024:1026] == b"BD" else "hfs+", volume_name=_hfs_name(head, 0))
    elif len(head) >= 0x8006 and head[0x8001:0x8006] == b"CD001":
        out.update(kind="cd-image", format="iso9660",
                   volume_name=head[0x8028:0x8048].decode("ascii", "replace").strip() or None)
    elif len(head) >= 84 and head[82:84] == b"\x01\x00" and 0 < head[0] <= 63 and \
            84 + sum(struct.unpack(">II", head[64:72])) == size:
        out.update(kind="disk-image", format="dc42", volume_name=head[1:1 + head[0]].decode("mac_roman"))
    elif head[:4] == b"SIT!" or head[:16] == b"StuffIt (c)1997-" or head[:4] in (b"ST46", b"ST50", b"ST60"):
        out.update(kind="archive", format="stuffit")
    elif b"(This file must be converted with BinHex" in head[:512]:
        out.update(kind="archive", format="binhex")
    elif head[:4] == b"PK\x03\x04":
        out.update(kind="archive", format="zip")
    elif len(head) >= 128 and head[0] == 0 and 0 < head[1] <= 63 and head[74] == 0 and head[82] == 0 \
            and head[65:69].isalnum():
        out.update(kind="archive", format="macbinary")
    return out


class MediaLibrary:
    def __init__(self, paths, apps_lib: Path | None = None, guestprep_lib: Path | None = None,
                 apps_module: Any = None, install_module: Any = None):
        self.paths = paths
        self.root = Path(paths.media_library)
        self.apps_lib = Path(apps_lib or paths.apps_lib)
        self.guestprep_lib = Path(guestprep_lib or paths.guestprep_lib)
        self._apps = apps_module
        self._install = install_module
        self._load_error: str | None = None
        self._cache: dict[str, tuple[tuple, dict]] = {}
        self.inserted: str | None = None      # disc in the drive for the current guest session

    # -- library --------------------------------------------------------------------
    @staticmethod
    def valid_name(name: Any) -> bool:
        return isinstance(name, str) and bool(NAME_RE.fullmatch(name)) and name not in (".", "..")

    def path(self, name: str) -> Path | None:
        """The library file for ``name``: a regular file directly in the library (no links)."""
        if not self.valid_name(name):
            return None
        p = self.root / name
        try:
            st = p.lstat()
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode) or p.parent.resolve() != self.root.resolve():
            return None
        return p

    def item(self, name: str) -> dict | None:
        p = self.path(name)
        if p is None:
            return None
        st = p.stat()
        key = (st.st_size, st.st_mtime_ns)
        cached = self._cache.get(name)
        if cached is None or cached[0] != key:
            try:
                info = identify(p)
            except OSError as exc:
                info = {"kind": "unknown", "format": None, "volume_name": None, "error": str(exc)}
            self._cache[name] = (key, info)
        info = self._cache[name][1]
        label = info.get("volume_name") or name
        if info.get("volume_name") and info["volume_name"] != name:
            label = f"{info['volume_name']} ({name})"
        return {"id": name, "name": name, "bytes": st.st_size,
                "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
                "kind": info["kind"], "kind_label": KIND_LABELS.get(info["kind"], info["kind"]),
                "format": info.get("format"), "volume_name": info.get("volume_name"), "label": label,
                "can_insert": info["kind"] in CD_KINDS, "can_install": info["kind"] in INSTALL_KINDS}

    def list(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        out = []
        for p in sorted(self.root.iterdir(), key=lambda p: p.name.lower()):
            if p.name.startswith("."):
                continue
            it = self.item(p.name)
            if it is not None:
                out.append(it)
        return out

    def cd_choices(self) -> dict[str, str]:
        """Setting ``media.cd`` choices: library name -> label."""
        return {it["id"]: it["label"] for it in self.list() if it["can_insert"]}

    def cd_for_launch(self, settings: dict, log_event=None) -> Path | None:
        """The CD image for the next guest start (None: empty drive).  A missing
        or no longer valid disc never blocks Mac OS from starting."""
        name = settings.get("media.cd", "none")
        self.inserted = None
        if not name or name == "none":
            return None
        it = self.item(name)
        if it is None or not it["can_insert"]:
            if log_event:
                log_event("media_missing", media=name)
            return None
        self.inserted = name
        return self.path(name)

    def unique_name(self, wanted: str) -> str:
        base = os.path.basename(wanted or "")
        base = re.sub(r"[^A-Za-z0-9 ._()+,'&!-]", "_", base).strip(" .") or "media"
        if not base[0].isalnum():
            base = "m" + base
        base = base[:110]
        stem, dot, ext = base.rpartition(".")
        if not dot or not stem:
            stem, ext = base, ""
        candidate, n = base, 2
        while (self.root / candidate).exists():
            candidate = f"{stem} {n}.{ext}" if ext else f"{stem} {n}"
            n += 1
        return candidate

    async def store(self, chunks, length: int, wanted: str) -> dict:
        """Write an upload into the library (temp file, then rename)."""
        if not 1024 <= length <= MAX_UPLOAD:
            raise MediaError(413, "bad-length", "Media files must be between 1 KB and 8 GB.")
        self.root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.root).free < length + RESERVE:
            raise MediaError(507, "no-space", "There isn't enough free space for this file.")
        fd, tmp = tempfile.mkstemp(prefix=".upload-", dir=self.root)
        got = 0
        try:
            with os.fdopen(fd, "wb") as f:
                async for chunk in chunks:
                    got += len(chunk)
                    if got > length:
                        break
                    f.write(chunk)
            if got != length:
                raise MediaError(400, "upload-incomplete", "The upload did not complete.")
            info = identify(Path(tmp))
            if info["kind"] == "unknown":
                raise MediaError(415, "unsupported-media",
                                 "RetroBoot can use CD images (Mac or ISO 9660), Disk Copy images and "
                                 "software archives (StuffIt, BinHex, MacBinary, zip).")
            name = self.unique_name(wanted)
            os.chmod(tmp, 0o640)
            os.replace(tmp, self.root / name)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return self.item(name)

    def delete(self, name: str, in_use: set[str]) -> None:
        p = self.path(name)
        if p is None:
            raise MediaError(404, "not-found", "That file isn't in the media library.")
        if name in in_use:
            raise MediaError(409, "media-in-use", "This disc is selected for the CD-ROM drive. "
                                                  "Choose another disc (or none) first.")
        p.unlink()
        self._cache.pop(name, None)

    # -- installing -----------------------------------------------------------------
    def _modules(self):
        if self._apps is None or self._install is None:
            try:
                for lib in (self.apps_lib, self.guestprep_lib):
                    if lib.is_dir() and str(lib) not in sys.path:
                        sys.path.insert(0, str(lib))
                self._apps = self._apps or importlib.import_module("rbapps.expand")
                self._install = self._install or importlib.import_module("rbguestprep.install")
            except Exception as exc:   # ImportError or a broken install
                self._load_error = f"{type(exc).__name__}: {exc}"
                return None, None
        return self._apps, self._install

    def install_available(self, machine=None) -> tuple[bool, str | None]:
        if machine is not None and machine.osx:
            return False, "Install Mac OS X software from inside Mac OS X. RetroBoot's disk installer supports classic Mac OS."
        apps, inst = self._modules()
        if apps is None:
            return False, "RetroBoot's software installer is not available on this computer."
        if os.geteuid() != 0 and not getattr(inst, "RETROBOOT_TEST_FAKE", False):
            return False, "Installing software needs RetroBoot's service to run as root."
        if not getattr(apps, "RETROBOOT_TEST_FAKE", False) and shutil.which("unar") is None:
            return False, "The archive tools (unar) are not installed."
        return True, None

    async def install(self, machine, name: str, dest: str | None, replace: bool, progress) -> dict:
        """Expand ``name`` and install it into ``machine``'s disk (Mac OS must be stopped)."""
        ok, why = self.install_available(machine)
        if not ok:
            raise MediaError(409, "capability-missing", why)
        it = self.item(name)
        if it is None:
            raise MediaError(404, "not-found", "That file isn't in the media library.")
        if not it["can_install"]:
            raise MediaError(422, "not-installable",
                             "Only software archives and disk images can be installed. "
                             "Put CD images in the CD-ROM drive instead.")
        apps, inst = self._modules()
        work = Path(tempfile.mkdtemp(prefix=".install-", dir=self.root))
        disk = Path(machine.disk)
        copy = disk.with_name(f".{disk.name}.install")
        try:
            progress(0.15, f"Expanding {name}…")
            tree = work / "tree"
            try:
                manifest = await asyncio.to_thread(apps.expand, self.path(name), tree, "extract", work)
            except Exception as exc:
                raise MediaError(422, "unsupported-media", f"{name} could not be expanded: {str(exc)[:300]}")
            progress(0.4, "Copying Mac OS's disk…")
            copy.unlink(missing_ok=True)
            rc, _, err = await run_cmd(["cp", "--reflink=auto", "--sparse=always", str(disk), str(copy)],
                                       timeout=3600)
            if rc != 0:
                raise MediaError(500, "copy-failed", "Mac OS's disk could not be copied: " + err.strip()[:200])
            progress(0.6, "Installing into Mac OS's disk…")
            try:
                report = await asyncio.to_thread(inst.install_tree, str(copy), dest, str(tree), replace=replace)
            except inst.InstallError as exc:
                raise MediaError(409 if exc.code in ("already-installed", "disk-full", "fragmented") else 422,
                                 exc.code, str(exc))
            st = disk.stat()
            os.chmod(copy, st.st_mode & 0o7777)
            try:
                os.chown(copy, st.st_uid, st.st_gid)
            except PermissionError:
                pass
            os.replace(copy, disk)
            record = {"media": name, "media_sha256": (manifest.get("source") or {}).get("sha256"),
                      "top": report.get("top"), "dest": ":".join(report.get("dest") or []),
                      "files": report.get("files"), "bytes": report.get("bytes"), "at": wall()}
            installed = list(machine.meta.get("installed") or [])[-49:] + [record]
            machine.update_meta(installed=installed)
            return {"machine": machine.id, "installed": record,
                    "report": {k: v for k, v in report.items() if k != "image"}}
        finally:
            copy.unlink(missing_ok=True)
            shutil.rmtree(work, ignore_errors=True)

    def public(self, settings_desired: dict, settings_applied: dict, guest_active: bool, machine=None) -> dict:
        ok, why = self.install_available(machine)
        return {"media": self.list(),
                "cd": {"desired": settings_desired.get("media.cd", "none"),
                       "applied": settings_applied.get("media.cd", "none"),
                       "inserted": self.inserted if guest_active else None},
                "install": {"available": ok, "reason": why},
                "upload_max_bytes": MAX_UPLOAD}
