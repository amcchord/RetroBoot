"""Read-only access to classic Mac HFS and HFS+ volumes stored in a file.

Walks the catalog B-tree leaf chain and reads forks through their extent
records, including the extents overflow file.  Used for floppy images
(Speedometer), CD images (Myst) and to verify what was written into a guest
disk.  Nothing here writes.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import struct
from typing import BinaryIO, Iterator

SECTOR = 512
ROOT_PARENT_ID = 1
ROOT_FOLDER_ID = 2
MAC_EPOCH = _dt.datetime(1904, 1, 1)


class VolumeError(Exception):
    pass


def mac_date(seconds: int) -> str | None:
    """HFS dates count seconds from 1904-01-01 (HFS: local time; HFS+: UTC)."""
    if not seconds:
        return None
    return (MAC_EPOCH + _dt.timedelta(seconds=seconds)).isoformat()


@dataclasses.dataclass
class Entry:
    path: tuple[str, ...]          # Mac names below the volume root
    kind: str                      # "folder" | "file"
    cnid: int
    finder_info: bytes             # 32 bytes: FInfo+FXInfo (files) or DInfo+DXInfo (folders)
    created: int = 0
    modified: int = 0
    data_size: int = 0
    rsrc_size: int = 0
    _data: tuple = ()
    _rsrc: tuple = ()

    @property
    def type(self) -> bytes:
        return self.finder_info[0:4] if self.kind == "file" else b""

    @property
    def creator(self) -> bytes:
        return self.finder_info[4:8] if self.kind == "file" else b""

    @property
    def flags(self) -> int:
        return struct.unpack(">H", self.finder_info[8:10])[0]


@dataclasses.dataclass
class Partition:
    index: int
    name: str
    type: str
    offset: int
    length: int


def partitions(f: BinaryIO, size: int, block: int = SECTOR) -> list[Partition]:
    """Apple Partition Map entries of a disk or CD image (empty without a map)."""
    f.seek(0)
    ddm = f.read(SECTOR)
    if ddm[:2] != b"ER":
        return []
    blk = struct.unpack(">H", ddm[2:4])[0] or block
    out = []
    n_entries = None
    i = 1
    while n_entries is None or i <= n_entries:
        f.seek(blk * i)
        e = f.read(SECTOR)
        if e[:2] != b"PM":
            break
        n_entries = struct.unpack(">I", e[4:8])[0]
        start, count = struct.unpack(">II", e[8:16])
        name = e[16:48].split(b"\0")[0].decode("mac_roman")
        ptype = e[48:80].split(b"\0")[0].decode("mac_roman")
        out.append(Partition(i, name, ptype, start * blk, count * blk))
        i += 1
        if i > 256:
            break
    return out


def find_volumes(f: BinaryIO, size: int) -> list[tuple[int, int, str]]:
    """(offset, length, "hfs"|"hfs+") for every HFS/HFS+ volume in an image:
    Apple_HFS partitions of a partitioned disk/CD, or a bare volume."""
    out = []
    cands = [(p.offset, p.length) for p in partitions(f, size) if p.type == "Apple_HFS"]
    if not cands:
        cands = [(0, size)]
    for off, length in cands:
        f.seek(off + 1024)
        hdr = f.read(512)
        if hdr[:2] in (b"H+", b"HX"):
            out.append((off, length, "hfs+"))
        elif hdr[:2] == b"BD":
            if hdr[0x7C:0x7E] == b"H+":
                al_size = struct.unpack(">I", hdr[0x14:0x18])[0]
                al_start = struct.unpack(">H", hdr[0x1C:0x1E])[0]
                emb_start, emb_count = struct.unpack(">HH", hdr[0x7E:0x82])
                out.append((off + al_start * SECTOR + emb_start * al_size, emb_count * al_size, "hfs+"))
            else:
                out.append((off, length, "hfs"))
    return out


class _BTreeVolume:
    """Shared B-tree walking for HFS and HFS+."""

    signature = b""

    def __init__(self, f: BinaryIO, offset: int, length: int):
        self.f = f
        self.offset = offset
        self.length = length
        self._overflow: dict[tuple[int, int], list[tuple[int, int, int]]] | None = None

    # -- block access -----------------------------------------------------
    def _block_pos(self, block: int) -> int:
        raise NotImplementedError

    def _read_extents(self, extents: list[tuple[int, int]], size: int) -> bytes:
        out = bytearray()
        for start, count in extents:
            if len(out) >= size or not count:
                continue
            want = min(size - len(out), count * self.block_size)
            self.f.seek(self._block_pos(start))
            chunk = self.f.read(want)
            if len(chunk) != want:
                raise VolumeError("fork extends beyond the image")
            out += chunk
        if len(out) < size:
            raise VolumeError(f"fork extents cover {len(out)} of {size} bytes")
        return bytes(out[:size])

    def _read_extent_range(self, extents: list[tuple[int, int]], size: int,
                           offset: int, length: int) -> bytes:
        """Read one logical fork range without materializing the whole fork."""
        if offset < 0 or length < 0 or offset > size or length > size - offset:
            raise VolumeError("fork range is outside its logical size")
        out = bytearray()
        logical = 0
        for start, count in extents:
            span = count * self.block_size
            if offset >= logical + span:
                logical += span
                continue
            within = max(0, offset - logical)
            want = min(length - len(out), span - within)
            if want:
                pos = self._block_pos(start) + within
                if pos < self.offset or pos + want > self.offset + self.length:
                    raise VolumeError("fork extent is outside the volume")
                self.f.seek(pos)
                chunk = self.f.read(want)
                if len(chunk) != want:
                    raise VolumeError("fork extends beyond the image")
                out += chunk
                offset += want
            logical += span
            if len(out) == length:
                return bytes(out)
        if len(out) != length:
            raise VolumeError("fork extents do not cover the requested range")
        return bytes(out)

    def fork_extents(self, cnid: int, fork: int, first: list[tuple[int, int]], size: int) -> list[tuple[int, int]]:
        """All extents of a fork: the catalog record's, then overflow records."""
        ext = [e for e in first if e[1]]
        need = -(-size // self.block_size)
        have = sum(c for _s, c in ext)
        if have >= need:
            return ext
        overflow = self._overflow_index()
        while have < need:
            more = overflow.get((cnid, fork, have))
            if not more:
                raise VolumeError(f"missing overflow extents for file {cnid}")
            ext += [(s, c) for s, c in more if c]
            have = sum(c for _s, c in ext)
        return ext

    def read_fork(self, entry: Entry, which: str) -> bytes:
        size = entry.data_size if which == "data" else entry.rsrc_size
        if not size:
            return b""
        first = entry._data if which == "data" else entry._rsrc
        fork = 0x00 if which == "data" else 0xFF
        return self._read_extents(self.fork_extents(entry.cnid, fork, list(first), size), size)

    # -- B-trees ----------------------------------------------------------
    def _tree_fork(self, which: str):
        if which == "catalog":
            size = self.catalog_size
            ext = self.fork_extents(4, 0x00, list(self.catalog_extents), size)
        else:   # the extents overflow file never has overflow extents of its own
            size = self.extents_size
            ext = [e for e in self.extents_extents if e[1]]
        return size, ext

    def _tree_nodes(self, which: str) -> tuple[bytes, int, int]:
        size, ext = self._tree_fork(which)
        data = self._read_extents(ext, size)
        node_size = struct.unpack(">H", data[14 + 18:14 + 20])[0]
        first_leaf = struct.unpack(">I", data[14 + 10:14 + 14])[0]
        return data, node_size, first_leaf

    def _tree_leaf_records(self, which: str) -> Iterator[tuple[bytes, int]]:
        """Read active leaf nodes on demand, including fragmented catalogs.

        Identity lookup needs a fixed path, not an allocated copy of every
        catalog node. The caller's reader still owns the total I/O budget.
        """
        size, ext = self._tree_fork(which)
        header = self._read_extent_range(ext, size, 0, 64)
        node_size = struct.unpack_from(">H", header, 32)[0]
        if not 512 <= node_size <= 32768 or node_size & (node_size - 1) or size % node_size:
            raise VolumeError("invalid B-tree node size")
        n = struct.unpack_from(">I", header, 24)[0]
        seen = set()
        while n:
            if n in seen or n >= size // node_size:
                raise VolumeError("damaged B-tree leaf chain")
            seen.add(n)
            node = self._read_extent_range(ext, size, n * node_size, node_size)
            flink, _blink, kind, _h, count = struct.unpack(">IIbBH", node[:12])
            if kind != -1:
                raise VolumeError(f"B-tree node {n} is not a leaf")
            records_end = node_size - 2 * (count + 1)
            if records_end < 14:
                raise VolumeError("invalid B-tree record count")
            previous = 13
            for i in range(count):
                off = struct.unpack_from(">H", node, node_size - 2 * (i + 1))[0]
                if not previous < off < records_end:
                    raise VolumeError("invalid B-tree record offset")
                previous = off
                yield node, off
            n = flink

    def _leaf_records(self, data: bytes, node_size: int, first_leaf: int) -> Iterator[tuple[bytes, int]]:
        """(node bytes, record offset) for every leaf record in key order."""
        n = first_leaf
        seen = set()
        while n:
            if n in seen or (n + 1) * node_size > len(data):
                raise VolumeError("damaged B-tree leaf chain")
            seen.add(n)
            node = data[n * node_size:(n + 1) * node_size]
            flink, _blink, kind, _h, count = struct.unpack(">IIbBH", node[:12])
            if kind != -1:
                raise VolumeError(f"B-tree node {n} is not a leaf")
            for i in range(count):
                off = struct.unpack(">H", node[node_size - 2 * (i + 1):node_size - 2 * i])[0]
                yield node, off
            n = flink

    def _overflow_index(self):
        if self._overflow is None:
            self._overflow = {}
            if self.extents_size:
                data, node_size, first = self._tree_nodes("extents")
                if first:
                    for node, off in self._leaf_records(data, node_size, first):
                        fork, cnid, start, exts = self._parse_overflow(node, off)
                        self._overflow[(cnid, fork, start)] = exts
        return self._overflow

    # -- catalog ----------------------------------------------------------
    def entries(self) -> list[Entry]:
        """Every folder and file below the root, parents before children."""
        data, node_size, first = self._tree_nodes("catalog")
        folders: dict[int, tuple[int, str]] = {}
        raw: list[tuple[int, str, Entry]] = []
        for node, off in self._leaf_records(data, node_size, first):
            rec = self._parse_catalog(node, off)
            if rec is None:
                continue
            parent, name, entry = rec
            if entry.kind == "folder":
                folders[entry.cnid] = (parent, name)
            raw.append((parent, name, entry))

        def path_of(cnid: int) -> tuple[str, ...] | None:
            parts = []
            seen = set()
            while cnid != ROOT_FOLDER_ID:
                if cnid in seen or cnid not in folders:
                    return None
                seen.add(cnid)
                parent, name = folders[cnid]
                parts.insert(0, name)
                cnid = parent
            return tuple(parts)

        out = []
        for parent, name, entry in raw:
            if entry.cnid == ROOT_FOLDER_ID:
                continue
            base = path_of(parent)
            if base is None:
                continue
            entry.path = base + (name,)
            out.append(entry)
        out.sort(key=lambda e: (len(e.path), e.path))
        return out


class HFSVolume(_BTreeVolume):
    """Classic HFS (Mac OS Standard)."""

    def __init__(self, f: BinaryIO, offset: int, length: int):
        super().__init__(f, offset, length)
        f.seek(offset + 1024)
        mdb = f.read(162)
        if mdb[:2] != b"BD":
            raise VolumeError("not an HFS volume")
        self.block_size = struct.unpack(">I", mdb[0x14:0x18])[0]
        self.first_block = struct.unpack(">H", mdb[0x1C:0x1E])[0]
        self.total_blocks = struct.unpack(">H", mdb[0x12:0x14])[0]
        self.free_blocks = struct.unpack(">H", mdb[0x22:0x24])[0]
        self.name = mdb[0x25:0x25 + mdb[0x24]].decode("mac_roman")
        self.created = struct.unpack(">I", mdb[2:6])[0]
        self.modified = struct.unpack(">I", mdb[6:10])[0]
        self.attributes = struct.unpack(">H", mdb[10:12])[0]
        self.blessed = struct.unpack(">I", mdb[0x5C:0x60])[0]   # drFndrInfo[0]
        self.extents_size = struct.unpack(">I", mdb[0x82:0x86])[0]
        self.extents_extents = self._ext3(mdb[0x86:0x92])
        self.catalog_size = struct.unpack(">I", mdb[0x92:0x96])[0]
        self.catalog_extents = self._ext3(mdb[0x96:0xA2])
        self.kind = "hfs"

    @staticmethod
    def _ext3(raw: bytes) -> list[tuple[int, int]]:
        return [struct.unpack(">HH", raw[4 * i:4 * i + 4]) for i in range(3)]

    def _block_pos(self, block: int) -> int:
        return self.offset + self.first_block * SECTOR + block * self.block_size

    def _parse_overflow(self, node, off):
        _klen, fork, cnid, start = struct.unpack(">BBIH", node[off:off + 8])
        return fork, cnid, start, self._ext3(node[off + 8:off + 20])

    def _parse_catalog(self, node, off):
        klen = node[off]
        if klen == 0:
            return None
        parent = struct.unpack(">I", node[off + 2:off + 6])[0]
        name = node[off + 7:off + 7 + node[off + 6]].decode("mac_roman")
        d = off + 1 + klen
        if d % 2:
            d += 1
        rtype = node[d]
        if rtype == 1:      # folder
            cnid = struct.unpack(">I", node[d + 6:d + 10])[0]
            crdat, mddat = struct.unpack(">II", node[d + 10:d + 18])
            finfo = node[d + 22:d + 38] + node[d + 38:d + 54]
            return parent, name, Entry((), "folder", cnid, finfo, crdat, mddat)
        if rtype == 2:      # file
            r = node[d:d + 102]
            cnid = struct.unpack(">I", r[20:24])[0]
            dlen = struct.unpack(">I", r[26:30])[0]
            rlen = struct.unpack(">I", r[36:40])[0]
            crdat, mddat = struct.unpack(">II", r[44:52])
            finfo = r[4:20] + r[56:72]
            return parent, name, Entry((), "file", cnid, finfo, crdat, mddat, dlen, rlen,
                                       tuple(self._ext3(r[74:86])), tuple(self._ext3(r[86:98])))
        return None


class HFSPlusVolume(_BTreeVolume):
    """HFS+ (Mac OS Extended), bare or embedded in an HFS wrapper."""

    def __init__(self, f: BinaryIO, offset: int, length: int):
        super().__init__(f, offset, length)
        f.seek(offset + 1024)
        vh = f.read(512)
        if vh[:2] not in (b"H+", b"HX"):
            raise VolumeError("not an HFS+ volume")
        self.attributes = struct.unpack(">I", vh[4:8])[0]
        self.created = struct.unpack(">I", vh[0x10:0x14])[0]
        self.modified = struct.unpack(">I", vh[0x14:0x18])[0]
        self.block_size = struct.unpack(">I", vh[0x28:0x2C])[0]
        self.total_blocks = struct.unpack(">I", vh[0x2C:0x30])[0]
        self.free_blocks = struct.unpack(">I", vh[0x30:0x34])[0]
        self.blessed = struct.unpack(">I", vh[0x50:0x54])[0]
        self.extents_size = struct.unpack(">Q", vh[0xC0:0xC8])[0]
        self.extents_extents = self._ext8(vh[0xD0:0x110])
        self.catalog_size = struct.unpack(">Q", vh[0x110:0x118])[0]
        self.catalog_extents = self._ext8(vh[0x120:0x160])
        self.kind = "hfs+"
        self.name = ""
        for e in self._root_thread():
            self.name = e

    @staticmethod
    def _ext8(raw: bytes) -> list[tuple[int, int]]:
        return [struct.unpack(">II", raw[8 * i:8 * i + 8]) for i in range(8)]

    def _block_pos(self, block: int) -> int:
        return self.offset + block * self.block_size

    def _parse_overflow(self, node, off):
        _klen, fork, _pad, cnid, start = struct.unpack(">HBBII", node[off:off + 12])
        return fork, cnid, start, self._ext8(node[off + 12:off + 76])

    def _root_thread(self):
        for node, off in self._tree_leaf_records("catalog"):
            klen, parent = struct.unpack(">HI", node[off:off + 6])
            d = off + 2 + klen
            if parent == ROOT_FOLDER_ID and struct.unpack(">h", node[d:d + 2])[0] == 3:
                n = struct.unpack(">H", node[d + 8:d + 10])[0]
                yield node[d + 10:d + 10 + 2 * n].decode("utf-16-be")
                return

    def _parse_catalog(self, node, off):
        klen, parent = struct.unpack(">HI", node[off:off + 6])
        nlen = struct.unpack(">H", node[off + 6:off + 8])[0]
        name = node[off + 8:off + 8 + 2 * nlen].decode("utf-16-be").replace("\0", "")
        d = off + 2 + klen
        rtype = struct.unpack(">h", node[d:d + 2])[0]
        if rtype == 1:
            r = node[d:d + 88]
            cnid = struct.unpack(">I", r[8:12])[0]
            crdat, mddat = struct.unpack(">II", r[12:20])
            return parent, name, Entry((), "folder", cnid, r[48:80], crdat, mddat)
        if rtype == 2:
            r = node[d:d + 248]
            cnid = struct.unpack(">I", r[8:12])[0]
            crdat, mddat = struct.unpack(">II", r[12:20])
            dsize = struct.unpack(">Q", r[88:96])[0]
            rsize = struct.unpack(">Q", r[168:176])[0]
            return parent, name, Entry((), "file", cnid, r[48:80], crdat, mddat, dsize, rsize,
                                       tuple(self._ext8(r[104:168])), tuple(self._ext8(r[184:248])))
        return None


def open_volume(f: BinaryIO, size: int, index: int = 0) -> _BTreeVolume:
    vols = find_volumes(f, size)
    if not vols:
        raise VolumeError("no HFS or HFS+ volume found")
    if index >= len(vols):
        raise VolumeError(f"volume {index} not present ({len(vols)} found)")
    off, length, kind = vols[index]
    return HFSPlusVolume(f, off, length) if kind == "hfs+" else HFSVolume(f, off, length)
