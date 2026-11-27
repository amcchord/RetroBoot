"""Read-only access to files on a Mac OS 9 guest disk (or an extracted folder).

Wraps rbguestprep's HFS+ reader (guestprep/rbguestprep/hfsplus.py) and adds a
one-pass catalog index, ':'-separated path lookup and fork reads that follow
the extents overflow file, so large or fragmented resource forks work too.
Nothing here ever writes to the image.
"""
from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
for cand in (os.environ.get("RB_GUESTPREP_PATH"), _HERE.parents[2] / "guestprep", "/usr/lib/retroboot/guestprep"):
    if cand and (Path(cand) / "rbguestprep").is_dir():
        if str(cand) not in sys.path:
            sys.path.insert(0, str(cand))
        break

from rbguestprep import hfs  # noqa: E402
from rbguestprep.hfsplus import Volume, Fork, Extent, HFSError, ROOT_FOLDER_ID  # noqa: E402


class MacDisk:
    """Files of an HFS+ guest volume, addressed as 'System Folder:Fonts:Geneva'."""

    def __init__(self, image: str | os.PathLike):
        self.vol = Volume(str(image), writable=False)
        self._index: dict[tuple[int, str], object] | None = None
        self._ext_fork = None
        vh = self.vol._read_abs(self.vol.offset + 1024, 512)
        self._ext_fork = Fork.parse(vh[0xC0:0x110])

    def close(self) -> None:
        self.vol.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- catalog ---------------------------------------------------------
    def _idx(self):
        if self._index is None:
            self._index = {}
            for rec in self.vol.records():
                self._index[(rec.parent_id, rec.name.casefold())] = rec
        return self._index

    def lookup(self, path: str):
        parent = ROOT_FOLDER_ID
        rec = None
        for comp in path.split(":"):
            rec = self._idx().get((parent, comp.casefold()))
            if rec is None:
                raise FileNotFoundError(path)
            parent = rec.cnid
        return rec

    def exists(self, path: str) -> bool:
        try:
            self.lookup(path)
            return True
        except FileNotFoundError:
            return False

    def children(self, path: str) -> list:
        folder = self.lookup(path) if path else None
        cnid = folder.cnid if folder else ROOT_FOLDER_ID
        return sorted((r for (p, _), r in self._idx().items() if p == cnid), key=lambda r: r.name.casefold())

    # -- forks -----------------------------------------------------------
    def _overflow_extents(self, cnid: int, fork_type: int, start_block: int) -> list[Extent]:
        """Extents beyond the first eight, from the extents overflow B-tree."""
        out: list[Extent] = []
        fork = self._ext_fork
        if fork is None or fork.logical_size == 0:
            return out
        header = self.vol._read_fork(fork, 0, 512)
        node_size = struct.unpack(">H", header[14 + 18:14 + 20])[0]
        n = struct.unpack(">I", header[14 + 10:14 + 14])[0]  # first leaf
        seen = set()
        while n and n not in seen:
            seen.add(n)
            node = self.vol._read_fork(fork, n * node_size, node_size)
            flink, _b, kind, _h, count = struct.unpack(">IIbBH", node[:12])
            if kind != -1:
                break
            for i in range(count):
                off = struct.unpack(">H", node[node_size - 2 * (i + 1):node_size - 2 * i])[0]
                klen, ftype, _pad, fid, sblock = struct.unpack(">HBBII", node[off:off + 12])
                if fid == cnid and ftype == fork_type:
                    rec = node[off + 2 + klen:off + 2 + klen + 64]
                    for j in range(8):
                        s, c = struct.unpack(">II", rec[8 * j:8 * j + 8])
                        if c:
                            out.append((sblock + sum(e.count for e in out), Extent(s, c)))
            n = flink
        out.sort(key=lambda t: t[0])
        return [e for _s, e in out]

    def read_fork(self, path_or_rec, which: str = "data") -> bytes:
        rec = self.lookup(path_or_rec) if isinstance(path_or_rec, str) else path_or_rec
        fork = rec.fork(which)
        if fork.covered_blocks() < fork.total_blocks:
            extra = self._overflow_extents(rec.cnid, 0x00 if which == "data" else 0xFF, fork.covered_blocks())
            fork = Fork(fork.logical_size, fork.clump_size, fork.total_blocks, [e for e in fork.extents if e.count] + extra)
        return self.vol._read_fork(fork, 0, fork.logical_size)

    def type_creator(self, path: str) -> tuple[str, str]:
        t, c = self.lookup(path).type_creator
        return t.decode("mac_roman"), c.decode("mac_roman")


class HfsStandardDisk:
    """Read-only view of a Mac OS Standard (HFS) volume, e.g. System 7.5.5 / Mac OS 8.1."""

    def __init__(self, image: str | os.PathLike):
        self.vol = hfs.Volume(str(image), writable=False)
        self._index: dict | None = None

    def close(self) -> None:
        self.vol.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _idx(self):
        if self._index is None:
            self._index = self.vol.index()
        return self._index

    def lookup(self, path: str):
        return self.vol.lookup(path.split(":"), self._idx())

    def exists(self, path: str) -> bool:
        try:
            self.lookup(path)
            return True
        except (FileNotFoundError, hfs.HFSError):
            return False

    def children(self, path: str) -> list:
        folder = self.lookup(path) if path else None
        cnid = folder.cnid if folder else hfs.ROOT_FOLDER_ID
        return sorted((r for (p, _), r in self._idx().items() if p == cnid), key=lambda r: r.name.casefold())

    def read_fork(self, path_or_rec, which: str = "data") -> bytes:
        rec = self.lookup(path_or_rec) if isinstance(path_or_rec, str) else path_or_rec
        return self.vol.read_fork(rec, which)

    def type_creator(self, path: str) -> tuple[str, str]:
        t, c = self.lookup(path).type_creator
        return t.decode("mac_roman"), c.decode("mac_roman")


class FolderDisk:
    """Same interface over a folder of extracted files: NAME (data fork) and
    NAME.rsrc or NAME/..namedfork/rsrc (resource fork), ':' mapped to '/'."""

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def _p(self, path: str) -> Path:
        return self.root.joinpath(*path.split(":"))

    def exists(self, path: str) -> bool:
        return self._p(path).exists() or self._p(path).with_name(self._p(path).name + ".rsrc").exists()

    def read_fork(self, path: str, which: str = "data") -> bytes:
        p = self._p(path)
        if which == "data":
            return p.read_bytes() if p.is_file() else b""
        for cand in (p.with_name(p.name + ".rsrc"), p / "..namedfork" / "rsrc"):
            try:
                return cand.read_bytes()
            except OSError:
                continue
        raise FileNotFoundError(path + " (resource fork)")

    def type_creator(self, path: str) -> tuple[str, str] | None:
        return None


def open_disk(src: str | os.PathLike):
    p = Path(src)
    if p.is_dir():
        return FolderDisk(p)
    if hfs.is_hfs_standard(str(p)):
        return HfsStandardDisk(p)
    return MacDisk(p)
