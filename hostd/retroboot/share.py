"""The shared volume, managed from the web client (ARCHITECTURE §7, ``/api/v1/share``).

``/var/lib/retroboot/share/`` (setting ``share.enabled``) is the host side of
the classicvirtio volume Mac OS shows as "RetroBoot Share".  This module lists
it the way Mac OS sees it and copies files in and out with their Mac file
semantics (``macfiles.py`` documents the on-disk representation):

* Uploads stream into a hidden staging folder on the same filesystem
  (``.retroboot-incoming``; classicvirtio hides names starting with '.'), are
  decoded there (MacBinary, BinHex, AppleSingle/AppleDouble; StuffIt through
  ``rbapps`` when asked; text conversion when asked) and only then move into
  place: sidecars first, the data fork last, so Mac OS never sees a file
  without its type, creator or resource fork.  Folders are built complete in
  staging and appear with one rename.
* Every path is walked one component at a time with ``O_NOFOLLOW`` directory
  descriptors: no '..', no hidden names or sidecars, no symlinks, no devices.
* Names become Mac OS Roman, at most 31 bytes, unique ignoring case
  ("Name copy"), written decomposed exactly as the guest writes them.  A host
  name Mac OS can't show as it is (longer, other characters, a second spelling)
  is listed under the shortened "Name#CNID.ext" Mac OS shows for it
  (``macfiles.shown_name``, classicvirtio-6).
* Mac OS's open files are seen through QEMU's descriptors in ``/proc``: a data
  fork QEMU holds open, or a resource fork in classicvirtio's per-boot cache
  (named by the CNID it derives from the inode).  Such files can't be
  replaced, renamed or deleted from the web.
* After a change the folder's and the volume's modification dates move, which
  is what makes the Finder look again (verified in the guest; see
  web/reports/webfiles-2026-10-11.md).

Standard library only; ``rbapps`` (apps/) is imported on demand for StuffIt.
"""
from __future__ import annotations

import asyncio
import ctypes
import errno
import importlib
import os
import re
import secrets
import shutil
import stat
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable, Iterator

from . import macfiles as mf
from .macfiles import FormatError, MacFile

STAGING = ".retroboot-incoming"
CV_CACHE = ".classicvirtio.nosync.noindex"
MAX_FILE = 2 ** 31 - 1           # Mac OS 9 file sizes are signed 32-bit
RESERVE = 256 * 1024 * 1024      # keep this much of RBDATA free
MAX_DEPTH = 32
MAX_RDUMP = 128 * 1024 * 1024    # a 16 MB resource fork is about 80 MB of Rez text
OPEN_CACHE_S = 1.0
# Folders the Finder keeps at a volume's top level.  classicvirtio-5 keeps the invisible flag
# Mac OS gives them (FOLDER.idump, macfiles.py); earlier drivers kept no folder Finder info,
# so the web listing leaves them out by name too (Desktop Folder and Trash only while they
# are empty, since they hold the user's items).
FINDER_ITEMS = {"TheVolumeSettingsFolder", "TheFindByContentFolder", "Temporary Items", "Network Trash Folder",
                "Cleanup At Startup", "Shutdown Check", "Desktop DB", "Desktop DF", "VM Storage"}
FINDER_HOLDERS = {"Desktop Folder", "Trash"}
# Folders Mac OS keeps invisible at a volume's top level: the Finder's volume settings and
# Find by Content index, the File Manager's Temporary Items (FindFolder's temporary folder)
# and AppleShare's Network Trash Folder.  Mac OS sets the flag when it creates them; one a driver before
# classicvirtio-5 left behind keeps showing in the share's window, since the Finder never
# flags an existing folder again, so hostd sets the flag Mac OS would have set once
# (hide_finder_folders).  Only a folder without Finder flags of its own is touched.
FINDER_INVISIBLE = ("TheVolumeSettingsFolder", "TheFindByContentFolder", "Temporary Items", "Network Trash Folder")


class ShareError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def _q(name: str) -> str:
    return f"“{name}”"


def human(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1000
    return f"{n} TB"


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def check_component(p: str) -> None:
    if (not p or p in (".", "..") or p.startswith(".") or "/" in p or "\0" in p or mf.is_sidecar(p)
            or len(p.encode("utf-8", "surrogateescape")) > 255):
        raise ShareError(400, "invalid-path", "That isn't a file or folder in the shared folder.")


def split_path(rel: str | None) -> list[str]:
    rel = rel or ""
    if len(rel) > 4096 or "\0" in rel:
        raise ShareError(400, "invalid-path", "That isn't a file or folder in the shared folder.")
    parts = [p for p in rel.split("/") if p]
    if len(parts) > MAX_DEPTH:
        raise ShareError(400, "invalid-path", "That folder is nested too deeply.")
    for p in parts:
        check_component(p)
    return parts


# O_NOFOLLOW: a symlink is never followed out of the share.  O_NONBLOCK: opening a FIFO
# someone left in the share must not hang hostd (it is then refused as not a regular file).
_O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_O_FILE = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def cnid_for(path: int) -> int:
    """classicvirtio QID2CNID() for a file, from a 9P qid.path."""
    c = (path & 0x3FFFFFFF) ^ ((path & 0x0FFFFFFFC0000000) >> 30) ^ ((path & 0xF000000000000000) >> 40)
    if c < 16:
        c += 0x12342454
    return c | 0x40000000


def share_cnid(ino: int) -> int:
    """The CNID classicvirtio gives a share item: QEMU's 9P server sends the inode
    remapped, (ino << 1) | 1 (-fsdev multidevs=remap, QEMU's default; checked against
    the driver's CNIDs with package r18).  It names items Mac OS sees shortened."""
    return cnid_for(((ino << 1) | 1) & 0xFFFFFFFFFFFFFFFF)


def cnids_for(ino: int) -> set[int]:
    """The CNIDs Mac OS may know a host file by.  QEMU's 9P server sends the inode as the
    qid path, or with -fsdev multidevs=remap (QEMU's default since 9.x) the inode shifted
    left with the export device's 1-bit affix: (ino << 1) | 1.  Measured on package r6:
    classicvirtio's resource fork cache names match the remapped form."""
    return {cnid_for(ino), cnid_for(((ino << 1) | 1) & 0xFFFFFFFFFFFFFFFF)}


def qemu_pids() -> list[int] | None:
    """PIDs of running qemu-system processes (None: no /proc to look at)."""
    if not os.path.isdir("/proc/self/fd"):
        return None
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            exe = os.readlink(f"/proc/{d}/exe")
        except OSError:
            continue
        if os.path.basename(exe).startswith("qemu-system"):
            out.append(int(d))
    return out


_libc = None


def _rename_noreplace(src: str, dst: str, src_fd: int, dst_fd: int) -> None:
    """rename() that fails with FileExistsError instead of replacing (Linux renameat2)."""
    global _libc
    if sys.platform.startswith("linux"):
        if _libc is None:
            _libc = ctypes.CDLL(None, use_errno=True)
        fn = getattr(_libc, "renameat2", None)
        if fn is not None:
            if fn(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), 1) == 0:   # RENAME_NOREPLACE
                return
            e = ctypes.get_errno()
            if e not in (errno.EINVAL, errno.ENOSYS):
                raise OSError(e, os.strerror(e), dst)
    try:
        os.stat(dst, dir_fd=dst_fd, follow_symlinks=False)
        raise FileExistsError(errno.EEXIST, "exists", dst)
    except FileNotFoundError:
        os.rename(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd)


# ---------------------------------------------------------------------------
# Items being copied in
# ---------------------------------------------------------------------------

@dataclass
class Node:
    """A file or folder built in staging, about to move into the share."""
    mac: bytes
    kind: str                       # file | folder
    path: str                       # its staging path (file: data fork; folder: the directory)
    file: MacFile | None = None
    children: list["Node"] = field(default_factory=list)


@dataclass
class Options:
    expand: bool = False            # expand StuffIt archives (else leave them for StuffIt Expander)
    convert_text: bool = False      # Mac line endings and characters for text files
    conflict: str = "rename"        # rename ("Name copy") | replace
    mtime: float | None = None      # the browser's File.lastModified, seconds


class SharedFolder:
    def __init__(self, root: Path, *, apps_lib: Path | None = None,
                 guest_pids: Callable[[], list[int] | None] | None = None,
                 guest_open: Callable[[], tuple[set, set] | None] | None = None,
                 max_file: int = MAX_FILE, reserve: int = RESERVE):
        self.root = Path(root)
        self.apps_lib = Path(apps_lib) if apps_lib else None
        self.guest_pids = guest_pids or qemu_pids
        self._guest_open_override = guest_open
        self.max_file = max_file
        self.reserve = reserve
        self.lock = threading.Lock()
        self._rsrc_sizes: dict[tuple, int | None] = {}
        self._open_cache: tuple[float, tuple | None] = (0.0, None)
        self._staging_ready = False
        self._expand = None
        self.events: list[dict] = []          # recent operations (hostd logs them)

    # -- low level -------------------------------------------------------------------
    def _root_fd(self) -> int:
        try:
            return os.open(self.root, _O_DIR)
        except FileNotFoundError:
            raise ShareError(503, "share-missing", "The shared folder doesn't exist on RetroBoot yet. "
                                                   "It's created when Mac OS starts with sharing on.")

    def _dir_fd(self, parts: list[str]) -> int:
        fd = self._root_fd()
        for p in parts:
            try:
                nfd = os.open(p, _O_DIR, dir_fd=fd)
            except (FileNotFoundError, NotADirectoryError):
                os.close(fd)
                raise ShareError(404, "not-found", f"There's no folder named {_q(self._shown(p))} there any more.")
            except OSError as e:
                os.close(fd)
                if e.errno == errno.ELOOP:
                    raise ShareError(404, "not-found", "That isn't a folder in the shared folder.")
                raise
            os.close(fd)
            fd = nfd
        return fd

    @staticmethod
    def _shown(host: str) -> str:
        m = mf.mac_bytes(host)
        return mf.display(m) if m else host

    @staticmethod
    def _mac_name(dfd: int, host: str, st: os.stat_result) -> bytes | None:
        """The name Mac OS shows for ``host`` in the folder ``dfd`` (macfiles.shown_name)."""
        def exists(name: str) -> bool:
            try:
                os.stat(name, dir_fd=dfd, follow_symlinks=False)
                return True
            except (OSError, ValueError):
                return False
        return mf.shown_name(host, share_cnid(st.st_ino), exists)

    def _owner(self) -> tuple[int, int] | None:
        if os.geteuid() != 0:
            return None
        st = os.stat(self.root)
        uid, gid = st.st_uid, st.st_gid
        if uid == 0:
            try:
                import pwd
                pw = pwd.getpwnam("rbguest")
                uid = pw.pw_uid
                gid = gid or pw.pw_gid
            except (ImportError, KeyError):
                pass
        return uid, gid

    def _own(self, path: str | None = None, *, fd: int | None = None, name: str | None = None,
             is_dir: bool = False) -> None:
        mode = 0o775 if is_dir else 0o664
        owner = self._owner()
        if name is not None:            # always a name hostd just created in a directory it holds open
            os.chmod(name, mode, dir_fd=fd)
            if owner:
                os.chown(name, *owner, dir_fd=fd, follow_symlinks=False)
        else:
            os.chmod(path, mode)
            if owner:
                os.chown(path, *owner, follow_symlinks=False)

    def hide_finder_folders(self) -> list[str]:
        """Before Mac OS starts: give the documented Finder folders at the top of the share
        the invisible flag Mac OS gives them (FOLDER.idump), where none is kept yet.  Returns
        the names it marked."""
        out = []
        try:
            dfd = os.open(self.root, _O_DIR)
        except OSError:
            return out
        try:
            for name in FINDER_INVISIBLE:
                try:
                    if not stat.S_ISDIR(os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mode):
                        continue
                    fd = os.open(name + ".idump", os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_NOFOLLOW", 0), 0o664, dir_fd=dfd)
                except (FileNotFoundError, FileExistsError):
                    continue
                with os.fdopen(fd, "wb") as f:
                    f.write(mf.idump(bytes(4), bytes(4), 0x4000))
                self._own(fd=dfd, name=name + ".idump")
                out.append(name)
        finally:
            os.close(dfd)
        return out

    def clear_driver_cache(self) -> tuple[int, int]:
        """Before Mac OS starts: delete the resource fork caches of earlier boots.  classicvirtio
        makes a new numbered folder in ``.classicvirtio.nosync.noindex/resforks`` at every start
        (multifork-3.c init3, "TODO: delete stale directories") and keeps a copy of every resource
        fork it read there, also after the file is deleted; nothing ever removed them, so the share
        grew by a folder per boot.  The running driver never reads an earlier boot's folder.  The
        catalog beside it is left alone (file IDs).  Returns (folders, bytes) removed."""
        base = os.path.join(self.root, CV_CACHE, "resforks")
        try:
            for p in (os.path.join(self.root, CV_CACHE), base):
                if not stat.S_ISDIR(os.lstat(p).st_mode):
                    return 0, 0
            names = os.listdir(base)
        except OSError:
            return 0, 0
        folders = size = 0
        for name in names:
            path = os.path.join(base, name)
            try:
                if not name.isdigit() or not stat.S_ISDIR(os.lstat(path).st_mode):
                    continue
                for dirpath, _, files in os.walk(path):
                    for f in files:
                        try:
                            size += os.lstat(os.path.join(dirpath, f)).st_size
                        except OSError:
                            pass
            except OSError:
                continue
            shutil.rmtree(path, ignore_errors=True)
            folders += 1
        return folders, size

    def _staging(self) -> Path:
        st = self.root / STAGING
        if not self._staging_ready:
            if st.is_symlink():
                st.unlink()
            if st.is_dir():
                shutil.rmtree(st, ignore_errors=True)    # leftovers from an interrupted upload
            st.mkdir(mode=0o700, exist_ok=True)
            self._staging_ready = True
        elif not st.is_dir():
            st.mkdir(mode=0o700, exist_ok=True)
        return st

    def _touch(self, dfd: int | None) -> None:
        """Move the folder's and the volume's modification dates: the Finder rescans."""
        now = time.time()
        for target in ((dfd,) if dfd is not None else ()) + (None,):
            try:
                if target is None:
                    os.utime(self.root, (now, now))
                elif os.utime in os.supports_fd:
                    os.utime(target, (now, now))
            except OSError:
                pass

    def free_bytes(self) -> tuple[int, int]:
        try:
            u = shutil.disk_usage(self.root if self.root.exists() else self.root.parent)
            return u.free, u.total
        except OSError:
            return 0, 0

    def _need_space(self, n: int, what: str) -> None:
        free, _ = self.free_bytes()
        if free - self.reserve < n:
            raise ShareError(507, "no-space", f"There isn't enough free space on RetroBoot for {_q(what)}: it needs "
                                              f"{human(n)} and {human(max(0, free - self.reserve))} is available.",
                             free_bytes=max(0, free - self.reserve))

    # -- Mac OS's open files ------------------------------------------------------------
    def guest_open(self) -> tuple[set, set] | None:
        """(inodes of data forks, CNIDs of resource forks) QEMU holds open; None if unknown."""
        if self._guest_open_override is not None:
            return self._guest_open_override()
        t, cached = self._open_cache
        if time.monotonic() - t < OPEN_CACHE_S:
            return cached
        result = self._scan_open()
        self._open_cache = (time.monotonic(), result)
        return result

    def _scan_open(self) -> tuple[set, set] | None:
        pids = self.guest_pids()
        if pids is None:
            return None
        inos, cnids = set(), set()
        root = os.path.realpath(self.root) + "/"
        cache = root + CV_CACHE + "/"
        for pid in pids:
            d = f"/proc/{pid}/fd"
            try:
                fds = os.listdir(d)
            except FileNotFoundError:
                continue
            except PermissionError:
                return None
            for n in fds:
                try:
                    target = os.readlink(f"{d}/{n}")
                except OSError:
                    continue
                if not target.startswith(root):
                    continue
                if target.startswith(cache):
                    leaf = target.rsplit("/", 1)[-1]
                    if re.fullmatch(r"[0-9a-f]{8}", leaf):
                        cnids.add(int(leaf, 16))
                    continue
                try:
                    st = os.stat(f"{d}/{n}")
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    inos.add((st.st_dev, st.st_ino))
        return inos, cnids

    def _is_open(self, st: os.stat_result, known: tuple | None) -> bool | None:
        if known is None:
            return None
        inos, cnids = known
        return (st.st_dev, st.st_ino) in inos or bool(cnids_for(st.st_ino) & cnids)

    def open_detection(self) -> bool:
        return self.guest_open() is not None

    # -- listing --------------------------------------------------------------------------
    def _sidecar(self, dfd: int, name: str, limit: int) -> bytes | None:
        try:
            fd = os.open(name, _O_FILE, dir_fd=dfd)
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
                return None
            with os.fdopen(os.dup(fd), "rb") as f:
                return f.read(limit + 1)
        finally:
            os.close(fd)

    def _rsrc_size(self, dfd: int, host: str) -> int | None:
        """Binary size of a file's resource fork (cached by the .rdump's identity); None if unreadable."""
        try:
            st = os.stat(host + ".rdump", dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return 0
        if not stat.S_ISREG(st.st_mode):
            return 0
        key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if key not in self._rsrc_sizes:
            text = self._sidecar(dfd, host + ".rdump", MAX_RDUMP)
            try:
                self._rsrc_sizes[key] = mf.rez_fork_size(text) if text is not None else None
            except FormatError:
                self._rsrc_sizes[key] = None
            if len(self._rsrc_sizes) > 4096:
                self._rsrc_sizes.pop(next(iter(self._rsrc_sizes)))
        return self._rsrc_sizes[key]

    def _entry(self, dfd: int, host: str, st: os.stat_result, known, rel: str) -> dict:
        mac = self._mac_name(dfd, host, st)
        e = {"name": host, "path": f"{rel}/{host}" if rel else host, "mac_name": mf.display(mac) if mac else host,
             "visible": mac is not None, "modified": iso(st.st_mtime)}
        if stat.S_ISDIR(st.st_mode):
            e.update(kind="folder", kind_label="folder", icon="folder", bytes=None)
            return e
        t, c, flags = mf.parse_idump(self._sidecar(dfd, host + ".idump", 4096) or b"")
        rsize = self._rsrc_size(dfd, host)
        kind, icon = mf.kind_for(host, t, c)
        e.update(kind="file", type=mf.display(t) if any(t) else None, creator=mf.display(c) if any(c) else None,
                 flags=flags, data_bytes=st.st_size, rsrc_bytes=rsize or 0, bytes=st.st_size + (rsize or 0),
                 kind_label=kind, icon=icon, open=self._is_open(st, known),
                 download="macbinary" if rsize else "data")
        if rsize is None:
            e["rsrc_error"] = True
        return e

    def _scan(self, dfd: int, top: bool = False, shown_only: bool = False) -> Iterator[tuple[str, os.stat_result]]:
        for host in os.listdir(dfd):
            if host.startswith(".") or mf.is_sidecar(host):
                continue
            try:
                st = os.stat(host, dir_fd=dfd, follow_symlinks=False)
            except OSError:
                continue
            if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                continue
            if shown_only:
                if top and (host in FINDER_ITEMS or (host in FINDER_HOLDERS and not self._has_items(dfd, host))):
                    continue
                if stat.S_ISREG(st.st_mode) and host == "Icon\r":
                    continue
                if mf.parse_idump(self._sidecar(dfd, host + ".idump", 4096) or b"")[2] & 0x4000:
                    continue                          # kIsInvisible (file or folder): the Finder doesn't show it either
            yield host, st

    @staticmethod
    def _has_items(dfd: int, host: str) -> bool:
        try:
            fd = os.open(host, _O_DIR, dir_fd=dfd)
        except OSError:
            return False
        try:
            return any(not n.startswith(".") and not mf.is_sidecar(n) for n in os.listdir(fd))
        finally:
            os.close(fd)

    def list(self, rel: str = "") -> dict:
        parts = split_path(rel)
        dfd = self._dir_fd(parts)
        known = self.guest_open()
        try:
            rel = "/".join(parts)
            items = [self._entry(dfd, h, st, known, rel) for h, st in self._scan(dfd, not parts, True)]
        finally:
            os.close(dfd)
        items.sort(key=lambda e: e["mac_name"].casefold())
        free, total = self.free_bytes()
        crumbs = []
        for i, p in enumerate(parts):
            crumbs.append({"name": p, "mac_name": self._crumb(parts[:i], p), "path": "/".join(parts[:i + 1])})
        return {"path": rel, "crumbs": crumbs, "items": items, "free_bytes": max(0, free - self.reserve),
                "total_bytes": total, "open_detection": known is not None}

    def _crumb(self, parent: list[str], host: str) -> str:
        try:
            dfd = self._dir_fd(parent)
        except ShareError:
            return self._shown(host)
        try:
            m = self._mac_name(dfd, host, os.stat(host, dir_fd=dfd, follow_symlinks=False))
        except OSError:
            m = None
        finally:
            os.close(dfd)
        return mf.display(m) if m else self._shown(host)

    def check(self, rel: str, names: list[str]) -> list[dict]:
        """What uploading ``names`` into ``rel`` would do: Mac names and conflicts."""
        parts = split_path(rel)
        dfd = self._dir_fd(parts)
        known = self.guest_open()
        try:
            taken = {}
            for h, st in self._scan(dfd):
                m = self._mac_name(dfd, h, st)
                if m:
                    taken[mf.fold(m)] = (h, st)
            out = []
            for n in names[:500]:
                mac = mf.safe_mac(n if not n.startswith("._") else n[2:])
                hit = taken.get(mf.fold(mac))
                r = {"name": n, "mac_name": mf.display(mac), "exists": bool(hit)}
                if hit:
                    r.update(existing=hit[0], existing_kind="folder" if stat.S_ISDIR(hit[1].st_mode) else "file",
                             open=self._is_open(hit[1], known) if stat.S_ISREG(hit[1].st_mode) else False)
                out.append(r)
            return out
        finally:
            os.close(dfd)

    # -- names ------------------------------------------------------------------------------
    def _taken(self, dfd: int) -> dict[str, str]:
        """fold(Mac name) -> host name, for everything in a folder (hidden names count by host name)."""
        out = {}
        for host in os.listdir(dfd):
            m = None
            if not host.startswith(".") and not mf.is_sidecar(host):
                try:
                    m = self._mac_name(dfd, host, os.stat(host, dir_fd=dfd, follow_symlinks=False))
                except OSError:
                    pass
            out[mf.fold(m) if m else "\0" + host] = host
        return out

    @staticmethod
    def _unique(mac: bytes, taken: dict[str, str]) -> bytes:
        cand, n = mac, 1
        while mf.fold(cand) in taken or "\0" + mf.host_name(cand) in taken:
            cand = mf.copy_name(mac, n)
            n += 1
        return cand

    @staticmethod
    def typed_name(name: str) -> bytes:
        """A name typed by a person (New Folder, Rename): Mac rules, refused rather than altered."""
        name = name.strip()
        if not name:
            raise ShareError(422, "invalid-name", "Type a name.", fields={"name": "Type a name."})
        if ":" in name:
            raise ShareError(422, "invalid-name", "Names can't contain a colon (:) in Mac OS.",
                             fields={"name": "Names can't contain a colon."})
        if name.startswith("."):
            raise ShareError(422, "invalid-name", "Names that start with a period are hidden in Mac OS.",
                             fields={"name": "Names can't start with a period."})
        try:
            mac = name.encode("mac_roman")
        except UnicodeEncodeError as e:
            bad = name[e.start]
            raise ShareError(422, "invalid-name", f"Mac OS can't show {_q(bad)} in a name.",
                             fields={"name": f"Mac OS can't show {_q(bad)} in a name."})
        if len(mac) > mf.MAX_NAME:
            raise ShareError(422, "invalid-name", "Names can be up to 31 characters long in Mac OS.",
                             fields={"name": "Up to 31 characters."})
        if any(c < 0x20 for c in mac) or mf.is_sidecar(mf.host_name(mac)):
            raise ShareError(422, "invalid-name", "Mac OS can't use that name.", fields={"name": "Choose another name."})
        return mac

    # -- folders, rename, delete ---------------------------------------------------------------
    def mkdir(self, rel: str, name: str, conflict: str = "fail", sanitize: bool = False, relpath: str = "") -> dict:
        """New folder.  A typed name follows Mac rules strictly; ``sanitize`` maps a dropped
        folder's name instead.  ``relpath`` walks (creating) folders below ``rel`` first."""
        parts = split_path(rel)
        sub = self._relpath(parts, relpath)
        mac = mf.safe_mac(name) if sanitize else self.typed_name(name)
        with self.lock:
            dfd, hosts = self._subfolders(self._dir_fd(parts), sub)
            try:
                taken = self._taken(dfd)
                if mf.fold(mac) in taken:
                    if conflict != "rename":
                        raise ShareError(409, "name-exists",
                                         f"An item named {_q(mf.display(mac))} already exists in this location.")
                    mac = self._unique(mac, taken)
                host = mf.host_name(mac)
                os.mkdir(host, 0o775, dir_fd=dfd)
                self._own(fd=dfd, name=host, is_dir=True)
                self._touch(dfd)
                st = os.stat(host, dir_fd=dfd, follow_symlinks=False)
                return self._entry(dfd, host, st, None, "/".join(parts + hosts))
            finally:
                os.close(dfd)

    @staticmethod
    def _relpath(parts: list[str], relpath: str | None) -> list[str]:
        sub = [p for p in (relpath or "").split("/") if p]
        if len(parts) + len(sub) > MAX_DEPTH or any(p in (".", "..") or "\0" in p for p in sub):
            raise ShareError(400, "invalid-path", "That folder path isn't allowed.")
        return sub

    def rename(self, rel: str, new_name: str) -> dict:
        parts = split_path(rel)
        if not parts:
            raise ShareError(400, "invalid-path", "The shared folder itself is renamed in the settings (Shared volume name).")
        mac = self.typed_name(new_name)
        with self.lock:
            dfd = self._dir_fd(parts[:-1])
            try:
                old = parts[-1]
                try:
                    st = os.stat(old, dir_fd=dfd, follow_symlinks=False)
                except FileNotFoundError:
                    raise ShareError(404, "not-found", f"{_q(self._shown(old))} isn't there any more.")
                new = mf.host_name(mac)
                taken = self._taken(dfd)
                other = taken.get(mf.fold(mac))
                if other is not None and other != old:
                    raise ShareError(409, "name-exists", f"An item named {_q(mf.display(mac))} already exists in "
                                                         f"this location.")
                if stat.S_ISREG(st.st_mode) and self._is_open(st, self.guest_open()):
                    raise ShareError(409, "file-open", f"{_q(self._shown(old))} is open in Mac OS. Close it there "
                                                       f"first, then rename it.")
                if new != old:
                    os.rename(old, new, src_dir_fd=dfd, dst_dir_fd=dfd)
                    # a folder's Finder flags (FOLDER.idump) go with it as a file's sidecars do
                    for sc in (mf.SIDECARS if stat.S_ISREG(st.st_mode) else (".idump",)):
                            try:
                                os.rename(old + sc, new + sc, src_dir_fd=dfd, dst_dir_fd=dfd)
                            except FileNotFoundError:
                                pass
                self._touch(dfd)
                st = os.stat(new, dir_fd=dfd, follow_symlinks=False)
                return self._entry(dfd, new, st, self.guest_open(), "/".join(parts[:-1]))
            finally:
                os.close(dfd)

    def delete(self, rel: str, recursive: bool = False) -> dict:
        parts = split_path(rel)
        if not parts:
            raise ShareError(400, "invalid-path", "The shared folder itself can't be deleted.")
        with self.lock:
            dfd = self._dir_fd(parts[:-1])
            try:
                name = parts[-1]
                try:
                    st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                except FileNotFoundError:
                    raise ShareError(404, "not-found", f"{_q(self._shown(name))} isn't there any more.")
                known = self.guest_open()
                if stat.S_ISREG(st.st_mode):
                    if self._is_open(st, known):
                        raise ShareError(409, "file-open", f"{_q(self._shown(name))} is open in Mac OS. Close it "
                                                           f"there first, then delete it.")
                    os.unlink(name, dir_fd=dfd)
                    for sc in mf.SIDECARS:
                        try:
                            os.unlink(name + sc, dir_fd=dfd)
                        except FileNotFoundError:
                            pass
                    files = 1
                elif stat.S_ISDIR(st.st_mode):
                    sub = os.open(name, _O_DIR, dir_fd=dfd)
                    try:
                        count, opened = self._count(sub, known)
                    finally:
                        os.close(sub)
                    if opened:
                        raise ShareError(409, "file-open", f"{_q(opened)}, inside {_q(self._shown(name))}, is open in "
                                                           f"Mac OS. Close it there first.")
                    if count and not recursive:
                        raise ShareError(409, "folder-not-empty", f"{_q(self._shown(name))} isn't empty.",
                                         items=count)
                    shutil.rmtree(name, dir_fd=dfd)     # fd-based: never follows links out of the share
                    try:
                        os.unlink(name + ".idump", dir_fd=dfd)   # the folder's Finder flags
                    except FileNotFoundError:
                        pass
                    files = count
                else:
                    raise ShareError(404, "not-found", "That isn't a file or folder in the shared folder.")
                self._touch(dfd)
                return {"deleted": "/".join(parts), "items": files}
            finally:
                os.close(dfd)

    def _count(self, dfd: int, known, depth: int = 0) -> tuple[int, str | None]:
        """(visible items inside, name of an item Mac OS has open) for a folder."""
        n = 0
        for host in os.listdir(dfd):
            try:
                st = os.stat(host, dir_fd=dfd, follow_symlinks=False)
            except OSError:
                continue
            if not (host.startswith(".") or mf.is_sidecar(host)):
                n += 1
            if stat.S_ISREG(st.st_mode) and self._is_open(st, known):
                return n, self._shown(host)
            if stat.S_ISDIR(st.st_mode) and depth < MAX_DEPTH:
                sub = os.open(host, _O_DIR, dir_fd=dfd)
                try:
                    k, opened = self._count(sub, known, depth + 1)
                finally:
                    os.close(sub)
                n += k
                if opened:
                    return n, opened
        return n, None

    # -- copying in ------------------------------------------------------------------------------
    def expand_available(self) -> tuple[bool, str | None]:
        if self._expand is None:
            try:
                if self.apps_lib and self.apps_lib.is_dir() and str(self.apps_lib) not in sys.path:
                    sys.path.insert(0, str(self.apps_lib))
                self._expand = importlib.import_module("rbapps.expand")
            except Exception as exc:  # noqa: BLE001 - optional tool
                self._expand = False
                self._expand_error = f"{type(exc).__name__}: {exc}"
        if not self._expand:
            return False, "RetroBoot's archive tools aren't installed, so StuffIt archives are copied as they are."
        if shutil.which("unar") is None or shutil.which("lsar") is None:
            return False, "The archive tools (unar) aren't installed, so StuffIt archives are copied as they are."
        return True, None

    async def receive(self, chunks: AsyncIterator[bytes], length: int, rel: str, name: str,
                      relpath: str = "", opts: Options | None = None) -> dict:
        """Stream one dropped file into the share.  Returns {items, source, notes}."""
        opts = opts or Options()
        parts = split_path(rel)
        sub = self._relpath(parts, relpath)
        name = os.path.basename(name or "").strip() or "untitled"
        if length < 0:
            raise ShareError(411, "length-required", "Send the file with its size.")
        if length > self.max_file:
            raise ShareError(413, "too-large", f"{_q(name)} is {human(length)}. Mac OS 9 can use files up to 2 GB; "
                                               f"put larger disc images in the CD-ROM drive instead.",
                             max_bytes=self.max_file)
        if opts.conflict not in ("rename", "replace"):
            raise ShareError(422, "invalid", "conflict must be rename or replace.")
        os.close(self._dir_fd(parts))                    # the destination must exist before any bytes move
        encoded = mf.extension(name) in ("bin", "hqx", "as", "macbin", "sit", "sea", "cpt")
        self._need_space(length * (2 if encoded or opts.expand else 1), name)
        staging = await asyncio.to_thread(self._staging)
        work = Path(tempfile.mkdtemp(prefix="up-", dir=staging))
        tmp = work / "upload"
        got = 0
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as f:
                async for chunk in chunks:
                    got += len(chunk)
                    if got > length:
                        break
                    f.write(chunk)
            if got != length:
                raise ShareError(400, "upload-incomplete", f"The copy of {_q(name)} didn't complete.")
            return await asyncio.to_thread(self._finish, work, tmp, parts, sub, name, opts)
        finally:
            await asyncio.to_thread(shutil.rmtree, work, True)

    # -- decoding (worker thread) -------------------------------------------------------------------
    def _decode(self, work: Path, tmp: Path, name: str) -> MacFile:
        kind = mf.sniff(str(tmp))
        size = tmp.stat().st_size
        try:
            if kind == "macbinary":
                f = mf.macbinary_decode(str(tmp))
            elif kind == "binhex":
                f = mf.binhex_decode(str(tmp), str(work / "binhex-data"), self.max_file + mf.MAX_RSRC + 4096)
            elif kind in ("applesingle", "appledouble"):
                f = mf.apple_decode(str(tmp))
            else:
                f = MacFile(name=b"", data_path=str(tmp), data_length=size, source="plain")
        except FormatError as e:
            raise ShareError(422, "damaged-file", f"{_q(name)} looks like a {kind} file but couldn't be decoded: {e}.")
        if f.data_length > self.max_file:
            raise ShareError(413, "too-large", f"The data in {_q(name)} is larger than the 2 GB Mac OS 9 can use.")
        if f.source == "plain":
            codes = mf.codes_for(name)
            if codes:
                f.type, f.creator = codes
        else:
            f.flags &= ~mf.FLAG_HAS_BEEN_INITED     # let the Finder register its icons and place it
        return f

    def _data_file(self, work: Path, f: MacFile) -> str:
        """A staging file holding exactly the data fork (the upload itself when it's plain)."""
        if f.data_offset == 0 and f.data_path and os.path.getsize(f.data_path) == f.data_length:
            return f.data_path
        out = str(work / f"data-{secrets.token_hex(4)}")
        off, left = f.data_offset, f.data_length
        with open(f.data_path, "rb") as src, open(out, "wb") as dst:
            try:                            # in-kernel copy (a reflink on XFS) where available
                while left:
                    n = os.copy_file_range(src.fileno(), dst.fileno(), min(left, 1 << 30), off)
                    if n <= 0:
                        break
                    off, left = off + n, left - n
            except (AttributeError, OSError):
                pass
            src.seek(off)
            while left:
                b = src.read(min(1 << 20, left))
                if not b:
                    raise ShareError(422, "damaged-file", "The file is shorter than its header says.")
                dst.write(b)
                left -= len(b)
        return out

    def _finish(self, work: Path, tmp: Path, parts: list[str], sub: list[str], name: str, opts: Options) -> dict:
        notes: list[str] = []
        f = self._decode(work, tmp, name)
        if f.source == "appledouble" or (name.startswith("._") and f.source == "applesingle" and not f.data_length):
            target = name[2:] if name.startswith("._") else (mf.display(f.name) if f.name else name)
            return self._apply_double(parts, sub, target, f)
        if f.source == "plain":
            mac = mf.safe_mac(name)
            if mf.display(mac) != name:
                notes.append(f"Named {_q(mf.display(mac))} in Mac OS.")
        else:
            stem = name.rsplit(".", 1)[0] if mf.extension(name) in ("bin", "hqx", "as", "macbin") else name
            mac = mf.safe_mac_bytes(f.name) if f.name else mf.safe_mac(stem)
            notes.append({"macbinary": "Decoded from MacBinary.", "binhex": "Decoded from BinHex.",
                          "applesingle": "Decoded from AppleSingle."}[f.source])
        shown = mf.display(mac)
        if mf.is_stuffit(f, shown) and opts.expand:
            nodes = self._expand_stuffit(work, f, mac, notes)
        else:
            if mf.is_stuffit(f, shown):
                notes.append("Left as it is, for StuffIt Expander.")
            if opts.convert_text and f.source == "plain" and mf.is_text_name(shown) and f.data_length:
                self._convert(work, f, notes)
            f.data_path = self._data_file(work, f)
            f.data_offset = 0
            if f.modified is None and opts.mtime:
                f.modified = opts.mtime
            nodes = [Node(mac, "file", f.data_path, f)]
        with self.lock:
            placed = self._place(parts, sub, nodes, opts.conflict, notes)
        return {"items": placed, "source": f.source, "notes": notes}

    def _convert(self, work: Path, f: MacFile, notes: list) -> None:
        with open(f.data_path, "rb") as src:
            src.seek(f.data_offset)
            head = src.read(65536)
            if not mf.looks_like_text(head):
                notes.append("Not converted: it isn't plain text.")
                return
            src.seek(f.data_offset)
            out = str(work / f"text-{secrets.token_hex(4)}")
            with open(out, "wb") as dst:
                r = mf.convert_text(src, dst, f.data_length)
        f.data_path, f.data_offset, f.data_length = out, 0, os.path.getsize(out)
        f.type = b"TEXT"
        if not any(f.creator):
            f.creator = b"ttxt"
        notes.append("Converted to Mac line endings" + (" and characters." if r["charset"] else "."))

    def _expand_stuffit(self, work: Path, f: MacFile, mac: bytes, notes: list) -> list[Node]:
        ok, why = self.expand_available()
        if not ok:
            raise ShareError(409, "capability-missing", why)
        archive = Path(self._data_file(work, f))
        if archive.parent != work:
            link = work / "archive"
            os.link(archive, link)
            archive = link
        tree_dir = work / "tree"
        try:
            listing = self._expand.list_archive(archive)
            self._need_space(sum(int(e.get("XADFileSize", 0)) for e in listing.get("lsarContents", [])),
                             mf.display(mac))
            self._expand.expand(archive, tree_dir, "keep", work)
        except ShareError:
            raise
        except Exception as e:  # noqa: BLE001 - rbapps.ExpandError and its tree/volume errors
            raise ShareError(422, "damaged-file", f"{_q(mf.display(mac))} couldn't be expanded: {str(e)[:300]}")
        tree_mod = importlib.import_module("rbapps.tree")
        t = tree_mod.Tree(tree_dir)
        root = Node(mac, "folder", "")
        index = {(): root}
        for e in t.entries:          # parents first
            path = tuple(e["path"])
            parent = index.get(path[:-1])
            if parent is None:
                continue
            emac = mf.fit(e["path"][-1].encode("mac_roman"))
            if e["kind"] == "folder":
                node = Node(emac, "folder", "")
            else:
                fi = bytes.fromhex(e["finder_info"])
                node = Node(emac, "file", str(t.data_path(e)),
                            MacFile(name=emac, type=fi[0:4], creator=fi[4:8],
                                    flags=int.from_bytes(fi[8:10], "big") & ~mf.FLAG_HAS_BEEN_INITED & ~1,
                                    data_path=str(t.data_path(e)), data_length=e["data"]["size"],
                                    rsrc=t.read_rsrc(e), source="stuffit"))
            parent.children.append(node)
            index[path] = node
        files = sum(1 for e in t.entries if e["kind"] == "file")
        notes.append(f"Expanded {files} item{'s' if files != 1 else ''} from the StuffIt archive.")
        if len(root.children) == 1:
            return root.children
        stem = mf.display(mac)
        if mf.extension(stem) in ("sit", "sea", "sitx"):
            stem = stem.rsplit(".", 1)[0]
        root.mac = mf.safe_mac(stem or "Archive")
        return [root]

    # -- placing (worker thread, under self.lock) -------------------------------------------------------
    def _subfolders(self, dfd: int, sub: list[str]) -> tuple[int, list[str]]:
        """Walk into (creating) the dropped folder's path below the destination.
        Returns the innermost directory and the host names walked."""
        hosts = []
        try:
            for comp in sub:
                mac = mf.safe_mac(comp)
                host = self._taken(dfd).get(mf.fold(mac))
                if host is None:
                    host = mf.host_name(mac)
                    try:
                        os.mkdir(host, 0o775, dir_fd=dfd)
                        self._own(fd=dfd, name=host, is_dir=True)
                        self._touch(dfd)
                    except FileExistsError:
                        pass
                try:
                    nfd = os.open(host, _O_DIR, dir_fd=dfd)
                except OSError:
                    raise ShareError(409, "name-exists",
                                     f"A file named {_q(mf.display(mac))} is in the way of that folder.")
                os.close(dfd)
                dfd = nfd
                hosts.append(host)
        except BaseException:
            os.close(dfd)
            raise
        return dfd, hosts

    def _place(self, parts: list[str], sub: list[str], nodes: list[Node], conflict: str, notes: list) -> list[dict]:
        dfd, hosts = self._subfolders(self._dir_fd(parts), sub)
        rel = "/".join(parts + hosts)
        known = self.guest_open()
        out = []
        try:
            for node in nodes:
                taken = self._taken(dfd)
                replace = None
                hit = taken.get(mf.fold(node.mac))
                if hit is not None:
                    st = os.stat(hit, dir_fd=dfd, follow_symlinks=False)
                    if conflict == "replace" and node.kind == "file" and stat.S_ISREG(st.st_mode):
                        if self._is_open(st, known):
                            raise ShareError(409, "file-open", f"{_q(self._shown(hit))} is open in Mac OS, so it "
                                                               f"can't be replaced. Close it there, or keep both.")
                        replace = hit
                    else:
                        node.mac = self._unique(node.mac, taken)
                        notes.append(f"Named {_q(mf.display(node.mac))} because "
                                     f"{_q(self._shown(hit))} already exists.")
                host = mf.host_name(node.mac)
                built = self._build_folder(node) if node.kind == "folder" else None
                while True:
                    try:
                        if built is None:
                            self._put_file(dfd, host, node.file, replace)
                        else:
                            sfd = os.open(os.path.dirname(built), _O_DIR)
                            try:
                                _rename_noreplace(os.path.basename(built), host, sfd, dfd)
                            finally:
                                os.close(sfd)
                        break
                    except FileExistsError:          # Mac OS took the name a moment ago
                        node.mac = self._unique(node.mac, self._taken(dfd))
                        host = mf.host_name(node.mac)
                st = os.stat(host, dir_fd=dfd, follow_symlinks=False)
                out.append(self._entry(dfd, host, st, None, rel))
            self._touch(dfd)
        finally:
            os.close(dfd)
        return out

    def _write_sidecars(self, sfd: int, base: str, f: MacFile) -> list[str]:
        """Write .rdump/.idump for ``f`` as ``base``+suffix in the directory ``sfd``."""
        made = []
        if f.rsrc:
            try:
                resources = mf.parse_fork(f.rsrc)
            except FormatError as e:
                raise ShareError(422, "damaged-file", f"The resource fork of {_q(mf.display(f.name or b'?'))} is "
                                                      f"damaged: {e}.")
            if resources:
                fd = os.open(base + ".rdump", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                             0o664, dir_fd=sfd)
                with os.fdopen(fd, "wb") as out:
                    mf.derez(resources, out)
                made.append(".rdump")
        if any(f.type) or any(f.creator) or f.flags:
            fd = os.open(base + ".idump", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                         0o664, dir_fd=sfd)
            with os.fdopen(fd, "wb") as out:
                out.write(mf.idump(f.type, f.creator, f.flags))
            made.append(".idump")
        for sc in made:
            self._own(fd=sfd, name=base + sc)
            if f.modified:
                os.utime(base + sc, (f.modified, f.modified), dir_fd=sfd)
        return made

    def _put_file(self, dfd: int, host: str, f: MacFile, replace: str | None) -> None:
        sdir = os.path.dirname(f.data_path)
        sfd = os.open(sdir, _O_DIR)
        try:
            tag = "f" + secrets.token_hex(4)
            data = os.path.basename(f.data_path)
            made = self._write_sidecars(sfd, tag, f)
            self._own(fd=sfd, name=data)
            if f.modified:
                os.utime(data, (f.modified, f.modified), dir_fd=sfd)
            if replace is not None and replace != host:
                for sc in ("",) + mf.SIDECARS:
                    try:
                        os.unlink(replace + sc, dir_fd=dfd)
                    except FileNotFoundError:
                        pass
            for sc in mf.SIDECARS:              # sidecars first: Mac OS never sees the file without them
                if sc in made:
                    os.rename(tag + sc, host + sc, src_dir_fd=sfd, dst_dir_fd=dfd)
                else:
                    try:
                        os.unlink(host + sc, dir_fd=dfd)    # stale sidecar of a replaced file or an orphan
                    except FileNotFoundError:
                        pass
            if replace is not None:
                os.rename(data, host, src_dir_fd=sfd, dst_dir_fd=dfd)
            else:
                _rename_noreplace(data, host, sfd, dfd)
        finally:
            os.close(sfd)

    def _build_folder(self, node: Node) -> str:
        """Build a folder tree in staging (host names, sidecars, data forks); returns its path."""
        top = tempfile.mkdtemp(prefix="dir-", dir=self._staging())
        self._own(top, is_dir=True)

        def fill(path: str, n: Node):
            dfd = os.open(path, _O_DIR)
            try:
                seen: dict[str, str] = {}
                for c in n.children:
                    c.mac = self._unique(c.mac, seen)
                    host = mf.host_name(c.mac)
                    seen[mf.fold(c.mac)] = host
                    if c.kind == "folder":
                        os.mkdir(host, 0o775, dir_fd=dfd)
                        self._own(fd=dfd, name=host, is_dir=True)
                        fill(os.path.join(path, host), c)
                        continue
                    f = c.file
                    self._write_sidecars(dfd, host, f)
                    os.rename(f.data_path, os.path.join(path, host))     # same filesystem (staging)
                    self._own(fd=dfd, name=host)
                    if f.modified:
                        os.utime(host, (f.modified, f.modified), dir_fd=dfd)
            finally:
                os.close(dfd)
        fill(top, node)
        return top

    def _apply_double(self, parts: list[str], sub: list[str], target: str, f: MacFile) -> dict:
        """An AppleDouble header file ("._Name"): give its resource fork and Finder info to Name."""
        mac = mf.safe_mac(target)
        with self.lock:
            dfd, hosts = self._subfolders(self._dir_fd(parts), sub)
            try:
                host = self._taken(dfd).get(mf.fold(mac)) or mf.host_name(mac)
                f.flags &= ~mf.FLAG_HAS_BEEN_INITED
                try:
                    st = os.stat(host, dir_fd=dfd, follow_symlinks=False)
                    if not stat.S_ISREG(st.st_mode):
                        raise ShareError(409, "name-exists", f"{_q(mf.display(mac))} is a folder.")
                    if self._is_open(st, self.guest_open()):
                        raise ShareError(409, "file-open", f"{_q(mf.display(mac))} is open in Mac OS.")
                except FileNotFoundError:
                    fd = os.open(host, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o664, dir_fd=dfd)
                    os.close(fd)
                    self._own(fd=dfd, name=host)
                staging = self._staging()
                sfd = os.open(staging, _O_DIR)
                try:
                    tag = "d" + secrets.token_hex(4)
                    made = self._write_sidecars(sfd, tag, f)
                    for sc in made:
                        os.rename(tag + sc, host + sc, src_dir_fd=sfd, dst_dir_fd=dfd)
                finally:
                    os.close(sfd)
                self._touch(dfd)
                st = os.stat(host, dir_fd=dfd, follow_symlinks=False)
                entry = self._entry(dfd, host, st, None, "/".join(parts + hosts))
            finally:
                os.close(dfd)
        return {"items": [entry], "source": "appledouble",
                "notes": [f"Added the resource fork and Finder info to {_q(mf.display(mac))}."]}

    # -- copying out ---------------------------------------------------------------------------------------
    def download(self, rel: str, fmt: str = "auto") -> "Download":
        parts = split_path(rel)
        if not parts:
            raise ShareError(400, "invalid-path", "Choose a file to download.")
        if fmt not in ("auto", "macbinary", "data"):
            raise ShareError(422, "invalid", "format must be auto, macbinary or data.")
        dfd = self._dir_fd(parts[:-1])
        try:
            name = parts[-1]
            try:
                fd = os.open(name, _O_FILE, dir_fd=dfd)
            except FileNotFoundError:
                raise ShareError(404, "not-found", f"{_q(self._shown(name))} isn't there any more.")
            except OSError as e:
                if e.errno == errno.ELOOP:
                    raise ShareError(404, "not-found", "That isn't a file in the shared folder.")
                raise
            st = os.fstat(fd)
            if stat.S_ISDIR(st.st_mode):
                os.close(fd)
                raise ShareError(422, "is-folder", "Folders can't be downloaded yet. Download the files inside it.")
            if not stat.S_ISREG(st.st_mode):
                os.close(fd)
                raise ShareError(404, "not-found", "That isn't a file in the shared folder.")
            t, c, flags = mf.parse_idump(self._sidecar(dfd, name + ".idump", 4096) or b"")
            rsrc = b""
            if fmt != "data":
                text = self._sidecar(dfd, name + ".rdump", MAX_RDUMP)
                if text:
                    try:
                        rsrc = mf.build_fork(mf.rez(text))
                    except FormatError as e:
                        os.close(fd)
                        raise ShareError(422, "damaged-resource-fork",
                                         f"The resource fork of {_q(self._shown(name))} can't be read ({e}). "
                                         f"Download its data fork only, or copy it in Mac OS.")
        finally:
            os.close(dfd)
        mac = mf.mac_bytes(name) or mf.safe_mac(name)
        if not mf.mac_bytes(name) or mf.host_name(mac) != name:
            # the name Mac OS shows (a shortened or second spelling), as the Files list shows it
            try:
                dfd = self._dir_fd(parts[:-1])
                try:
                    mac = self._mac_name(dfd, name, st) or mac
                finally:
                    os.close(dfd)
            except ShareError:
                pass
        as_mb = fmt == "macbinary" or (fmt == "auto" and bool(rsrc))
        return Download(fd, mac, st.st_size, as_mb, t, c, flags, rsrc, st.st_mtime)


class Download:
    def __init__(self, fd: int, mac: bytes, dlen: int, macbinary: bool, type_: bytes, creator: bytes, flags: int,
                 rsrc: bytes, mtime: float):
        self.f = os.fdopen(fd, "rb")
        self.mac, self.dlen, self.macbinary = mac, dlen, macbinary
        self.type, self.creator, self.flags, self.rsrc, self.mtime = type_, creator, flags, rsrc, mtime
        shown = mf.display(mac).replace("/", "-")
        self.filename = shown + (".bin" if macbinary else "")
        self.length = mf.macbinary_length(dlen, len(rsrc)) if macbinary else dlen

    def chunks(self, size: int = 256 * 1024) -> Iterator[bytes]:
        if self.macbinary:
            yield from mf.macbinary_stream(self.mac, self.type, self.creator, self.flags, self.f, self.dlen,
                                           self.rsrc, self.mtime, size)
            return
        left = self.dlen
        while left:
            b = self.f.read(min(size, left))
            if not b:
                raise FormatError("the file got shorter while it was being read")
            left -= len(b)
            yield b

    def close(self) -> None:
        self.f.close()
