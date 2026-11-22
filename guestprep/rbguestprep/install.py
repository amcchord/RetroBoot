"""Offline installation of classic Mac files into a stopped guest disk.

``install_tree(image, dest_folder, tree)`` copies a canonical tree (the
``retroboot-mac-tree`` format written by RetroBoot's ``apps/rbapps``:
``manifest.json``, ``data/<path>``, ``rsrc/<path>``) into the guest's HFS+
startup volume with every folder, data fork, resource fork and Finder info
(type, creator, flags).

It generalizes the resource-fork method of ``prep.install_tcpip_dhcp``:

1. With the volume mounted by the Linux hfsplus driver, create every folder
   and file (data fork written normally, Finder info through the
   ``osx.com.apple.FinderInfo`` xattr) and, for each file with a resource
   fork, a temporary file in a private folder whose data fork holds the
   resource-fork bytes, so the driver allocates the blocks and updates the
   bitmap.
2. With the volume unmounted, move each temporary file's 80-byte fork
   descriptor into its target's resource-fork descriptor in the catalog leaf
   record (fixed-size, in place) and empty the temporary descriptor.
3. Mount again and let the driver delete the now-empty temporary files.
4. Verify every fork through the driver (``<file>/rsrc`` for resource forks)
   and the Finder info, then ``fsck.hfsplus -fn``.

The Linux driver allocates a new file from the first free run on the volume
and extends it into the next free runs, so the small holes a Mac OS install
leaves near the start of the volume would split a resource fork into more than
the eight extents a catalog record holds.  Before writing, the small holes in
front of the first run large enough for the tree's biggest fork are filled
with temporary "plug" files (at most eight holes each, so no plug needs the
extents overflow file) that go away with the temporary folder.

Same requirements as ``prep``: root, losetup/mount, the hfsplus module,
fsck.hfsplus; the guest must be stopped.  Run on a reflink/sparse copy and
swap it in after success (``hostd`` does).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import struct
import tempfile
import unicodedata
import uuid

from . import hfsplus
from .prep import FINDER_INFO_XATTR, PrepError, _run, fsck, fsck_verify, loop_device

TREE_FORMAT = "retroboot-mac-tree"
TREE_VERSION = 1
DEFAULT_DESTINATIONS = (["Applications (Mac OS 9)"], ["Applications"])
FLAG_HAS_BEEN_INITED = 0x0100
SPACE_MARGIN = 16 * 1024 * 1024
PLUG_MIN_RUN = 256                  # blocks: runs this large are left free
PLUG_MAX_BYTES = 256 * 1024 * 1024  # never write more than this into plugs


class InstallError(PrepError):
    """A user-facing reason the files could not be installed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def linux_name(mac_name: str) -> str:
    return mac_name.replace("/", ":")


def _key(name: str) -> str:
    return unicodedata.normalize("NFD", name).casefold()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# -- the canonical tree (read side; see apps/rbapps/tree.py) -----------------

class Tree:
    def __init__(self, root: str):
        self.root = root
        try:
            with open(os.path.join(root, "manifest.json"), encoding="utf-8") as f:
                self.manifest = json.load(f)
        except (OSError, ValueError) as exc:
            raise InstallError("invalid-tree", f"No readable manifest in {root}: {exc}") from exc
        if self.manifest.get("format") != TREE_FORMAT or self.manifest.get("version") != TREE_VERSION:
            raise InstallError("invalid-tree", "Not a RetroBoot Mac tree.")
        self.entries = self.manifest.get("entries") or []
        if not self.entries:
            raise InstallError("invalid-tree", "There is nothing to install.")
        for e in self.entries:
            path = e.get("path")
            if not isinstance(path, list) or not path or e.get("kind") not in ("file", "folder"):
                raise InstallError("invalid-tree", "Malformed tree entry.")
            for p in path:
                if not isinstance(p, str) or not p or ":" in p or p in (".", ".."):
                    raise InstallError("invalid-tree", f"Invalid Mac name {p!r}.")
                try:
                    if not 0 < len(p.encode("mac_roman")) <= 31:
                        raise ValueError
                except (UnicodeEncodeError, ValueError):
                    raise InstallError("invalid-tree", f"Name {p!r} cannot be used on a Mac OS 9 disk.") from None
            if len(bytes.fromhex(e.get("finder_info", ""))) != 32:
                raise InstallError("invalid-tree", "Malformed Finder info.")
        self.entries.sort(key=lambda e: (len(e["path"]), e["path"]))
        self.top = sorted({e["path"][0] for e in self.entries})

    def _p(self, base: str, path: list[str]) -> str:
        return os.path.join(self.root, base, *[linux_name(p) for p in path])

    def data(self, e: dict) -> bytes:
        with open(self._p("data", e["path"]), "rb") as f:
            return f.read()

    def rsrc(self, e: dict) -> bytes:
        if not e.get("rsrc", {}).get("size"):
            return b""
        with open(self._p("rsrc", e["path"]), "rb") as f:
            return f.read()

    def bytes_needed(self, block: int) -> int:
        total = 0
        for e in self.entries:
            if e["kind"] == "file":
                for which in ("data", "rsrc"):
                    total += -(-e[which]["size"] // block) * block
        return total


@contextlib.contextmanager
def _mounted(image: str):
    offset, length = hfsplus.locate_volume(image)
    with loop_device(image, offset, length) as dev, tempfile.TemporaryDirectory(prefix="rbinst-") as mnt:
        _run(["mount", "-t", "hfsplus", "-o", "rw,nodev,nosuid,noexec,nls=utf8", dev, mnt])
        try:
            yield mnt
        finally:
            _run(["sync"])
            _run(["umount", mnt])


def _index(vol: hfsplus.Volume) -> dict:
    return {(rec.parent_id, _key(rec.name)): rec for rec in vol.records()}


def _resolve(index: dict, path: list[str]):
    parent = hfsplus.ROOT_FOLDER_ID
    rec = None
    for comp in path:
        rec = index.get((parent, _key(comp)))
        if rec is None:
            return None
        parent = rec.cnid
    return rec


def plug_plan(runs: list[tuple[int, int]], largest_fork_blocks: int) -> list[list[tuple[int, int]]]:
    """Group the free runs in front of the first run that is at least
    max(PLUG_MIN_RUN, a quarter of the largest fork) blocks long into plugs of
    at most eight runs each.  Empty when nothing needs plugging."""
    threshold = max(PLUG_MIN_RUN, -(-largest_fork_blocks // 4))
    small = []
    for run in runs:
        if run[1] >= threshold:
            break
        small.append(run)
    else:
        return []           # no run is large enough: plugging cannot help
    return [small[i:i + 8] for i in range(0, len(small), 8)]


def _free_bytes(vol: hfsplus.Volume) -> int:
    vh = vol._read_abs(vol.offset + 1024, 512)
    return struct.unpack(">I", vh[0x30:0x34])[0] * vol.block_size


def choose_destination(image: str, dest_folder=None) -> list[str]:
    """``dest_folder`` as a list of Mac names (or 'A:B'); default: the volume's
    'Applications (Mac OS 9)' folder, else 'Applications'."""
    if isinstance(dest_folder, str):
        dest_folder = [p for p in dest_folder.split(":") if p]
    if dest_folder:
        return list(dest_folder)
    with hfsplus.Volume(image) as vol:
        idx = _index(vol)
        for cand in DEFAULT_DESTINATIONS:
            rec = _resolve(idx, cand)
            if rec is not None and rec.is_folder:
                return list(cand)
    return list(DEFAULT_DESTINATIONS[-1])


def _verify(image: str, tree: Tree, dest: list[str]) -> int:
    """Read every installed item back: data forks and Finder info through the
    Linux driver, resource forks and Finder info from the catalog records."""
    checked = 0
    with _mounted(image) as mnt:
        base = os.path.join(mnt, *[linux_name(p) for p in dest])
        for e in tree.entries:
            name = ":".join(e["path"])
            path = os.path.join(base, *[linux_name(p) for p in e["path"]])
            fi = os.getxattr(path, FINDER_INFO_XATTR)
            want_fi = bytes.fromhex(e["finder_info"])
            if e["kind"] == "folder":
                if not os.path.isdir(path) or fi[8:10] != want_fi[8:10]:
                    raise InstallError("verify-failed", f"Folder {name} is missing or has other flags.")
                continue
            if fi[:10] != want_fi[:10]:
                raise InstallError("verify-failed", f"Type, creator or flags of {name} differ.")
            with open(path, "rb") as f:
                if _sha(f.read()) != e["data"]["sha256"]:
                    raise InstallError("verify-failed", f"Data fork of {name} differs.")
    with hfsplus.Volume(image) as vol:
        idx = _index(vol)
        for e in tree.entries:
            rec = _resolve(idx, dest + e["path"])
            if rec is None:
                raise InstallError("verify-failed", f"{':'.join(e['path'])} is not in the catalog.")
            if e["kind"] == "file":
                if rec.data[48:58] != bytes.fromhex(e["finder_info"])[:10]:
                    raise InstallError("verify-failed", f"Catalog Finder info of {':'.join(e['path'])} differs.")
                if _sha(vol.read_fork(rec, "rsrc")) != e["rsrc"]["sha256"]:
                    raise InstallError("verify-failed", f"Resource fork of {':'.join(e['path'])} differs.")
            checked += 1
    return checked


def install_tree(image: str, dest_folder, tree_root: str, *, replace: bool = False) -> dict:
    """Install a canonical tree into ``dest_folder`` of a stopped guest disk.

    Returns a report; raises ``InstallError`` (``code`` + user-facing message)."""
    tree = Tree(tree_root)
    report = {"image": os.path.abspath(image), "top": tree.top}
    ok, out = fsck(image, repair=True)
    report["fsck_before"] = "ok" if ok else out.splitlines()[-1:]
    if not ok:
        raise InstallError("disk-damaged", "The Mac OS disk has errors that could not be repaired.")
    dest = choose_destination(image, dest_folder)
    report["dest"] = dest
    with hfsplus.Volume(image) as vol:
        idx = _index(vol)
        block = vol.block_size
        node = _resolve(idx, dest)
        if node is not None and not node.is_folder:
            raise InstallError("invalid-destination", f"{':'.join(dest)} is not a folder.")
        existing = []
        if node is not None:
            existing = [t for t in tree.top if _resolve(idx, dest + [t]) is not None]
        if existing and not replace:
            raise InstallError("already-installed",
                               f"“{existing[0]}” is already in {dest[-1]}. Remove it in Mac OS first, or replace it.")
        need = tree.bytes_needed(block) + SPACE_MARGIN
        free = _free_bytes(vol)
        if need > free:
            raise InstallError("disk-full", f"The Mac OS disk needs {need // 1048576} MB free; "
                                            f"only {free // 1048576} MB are available.")
        largest = max((-(-e[w]["size"] // block) for e in tree.entries if e["kind"] == "file"
                       for w in ("data", "rsrc")), default=0)
        plugs = plug_plan(vol.free_runs(), largest)
        plug_bytes = sum(c for plug in plugs for _, c in plug) * block
        if plug_bytes > PLUG_MAX_BYTES or need + plug_bytes > free:
            plugs = []
    report["replaced"] = existing
    report["plugged_runs"] = sum(len(p) for p in plugs)
    tmp_name = f"rbinstall-{uuid.uuid4().hex[:8]}"
    pending = []          # (entry, temp name)
    # 1. Linux creates every catalog record and allocates every fork.
    with _mounted(image) as mnt:
        base = os.path.join(mnt, *[linux_name(p) for p in dest])
        os.makedirs(base, exist_ok=True)
        for t in existing:
            shutil.rmtree(os.path.join(base, linux_name(t)))
        tmp_dir = os.path.join(mnt, tmp_name)
        os.mkdir(tmp_dir)
        for n, plug in enumerate(plugs):
            with open(os.path.join(tmp_dir, f"plug-{n:04d}"), "wb") as f:
                f.write(bytes(sum(c for _, c in plug) * block))
                f.flush()
                os.fsync(f.fileno())
        for i, e in enumerate(tree.entries):
            path = os.path.join(base, *[linux_name(p) for p in e["path"]])
            if e["kind"] == "folder":
                os.mkdir(path)
                continue
            data = tree.data(e)
            rsrc = tree.rsrc(e)
            if _sha(data) != e["data"]["sha256"] or _sha(rsrc) != e["rsrc"]["sha256"]:
                raise InstallError("invalid-tree", f"{':'.join(e['path'])} does not match its manifest.")
            with open(path, "wb") as f:
                f.write(data)
            if rsrc:
                t = f"{i:06d}"
                with open(os.path.join(tmp_dir, t), "wb") as f:
                    f.write(rsrc)
                    f.flush()
                    os.fsync(f.fileno())
                pending.append((e, t))
        # Finder info last (folders after their contents exist)
        for e in tree.entries:
            path = os.path.join(base, *[linux_name(p) for p in e["path"]])
            os.setxattr(path, FINDER_INFO_XATTR, bytes.fromhex(e["finder_info"]))
    # 2. Move the allocated extents into the resource-fork descriptors.
    with hfsplus.Volume(image, writable=True) as vol:
        idx = _index(vol)
        tmp_rec = _resolve(idx, [tmp_name])
        if tmp_rec is None:
            raise InstallError("internal", "Temporary folder not found.")
        for e, t in pending:
            target = _resolve(idx, dest + e["path"])
            temp = idx.get((tmp_rec.cnid, _key(t)))
            if target is None or temp is None or not target.is_file:
                raise InstallError("internal", f"Catalog record for {':'.join(e['path'])} not found.")
            moved = temp.fork("data")
            if moved.logical_size != e["rsrc"]["size"] or moved.covered_blocks() < moved.total_blocks:
                raise InstallError("fragmented", "The Mac OS disk is too fragmented for an offline install "
                                                 f"({':'.join(e['path'])}).")
            if target.fork("rsrc").total_blocks or target.fork("rsrc").logical_size:
                raise InstallError("internal", f"{':'.join(e['path'])} unexpectedly has a resource fork.")
            vol.set_fork_record(target, "rsrc", moved)
            vol.set_fork_record(temp, "data", hfsplus.Fork.empty())
        vol.flush()
    # 3. The driver removes the now-empty temporary records.
    with _mounted(image) as mnt:
        shutil.rmtree(os.path.join(mnt, tmp_name))
    report["resource_forks_moved"] = len(pending)
    report["verified_items"] = _verify(image, tree, dest)
    report["files"] = sum(1 for e in tree.entries if e["kind"] == "file")
    report["folders"] = sum(1 for e in tree.entries if e["kind"] == "folder")
    report["bytes"] = sum(e["data"]["size"] + e["rsrc"]["size"] for e in tree.entries if e["kind"] == "file")
    ok, out, minor = fsck_verify(image)
    report["fsck_after"] = "ok" if ok else out.splitlines()[-1:]
    if minor:
        report["fsck_minor_repair"] = "unused B-tree node erased"
    if not ok:
        raise InstallError("verify-failed", "The Mac OS disk check failed after installing: " + out[-300:])
    return report


def remove_installed(image: str, dest_folder, names: list[str]) -> dict:
    """Delete top-level items from ``dest_folder`` (the driver frees both forks)."""
    dest = choose_destination(image, dest_folder)
    removed = []
    with _mounted(image) as mnt:
        base = os.path.join(mnt, *[linux_name(p) for p in dest])
        for n in names:
            p = os.path.join(base, linux_name(n))
            if os.path.lexists(p):
                shutil.rmtree(p) if os.path.isdir(p) else os.unlink(p)
                removed.append(n)
    ok, out, _ = fsck_verify(image)
    if not ok:
        raise InstallError("verify-failed", "The Mac OS disk check failed after removing: " + out[-300:])
    return {"dest": dest, "removed": removed, "fsck_after": "ok"}
