"""Offline access to the HFS (standard, "Mac OS Standard") startup volume of a
raw 68k Mac disk image.

68k Macs cannot start up from HFS+, so RetroBoot's Quadra Macs (Mac OS 7.5.5
and 8.1, D044) keep an HFS volume on an Apple Partition Map disk.  Like
``hfsplus``, this module only reads structures and rewrites fixed-size fields
in place (fork bytes, catalog fork descriptors, Finder info, the volume
attributes); creating or deleting catalog records is left to the Linux ``hfs``
driver (``hfs68k``), so the B-tree shape never changes here.

Use it only while the guest is stopped and nothing else has the image open.

HFS layout (Inside Macintosh: Files, "Data Organization on Volumes"): the
master directory block (MDB) is the volume's sector 2; allocation blocks start
at sector ``drAlBlSt``; the catalog and extents overflow files are B-trees of
512-byte nodes whose own extents are in the MDB; a file record keeps three
extents per fork (more go to the extents overflow file).
"""
from __future__ import annotations

import dataclasses
import os
import struct
from typing import Iterator

SECTOR = 512
NODE = 512
ROOT_PARENT_ID = 1
ROOT_FOLDER_ID = 2
EXTENTS_FILE_ID = 3
REC_FOLDER, REC_FILE, REC_FOLDER_THREAD, REC_FILE_THREAD = 1, 2, 3, 4

# drAtrb bits
ATRB_HW_LOCK = 0x0080
ATRB_UNMOUNTED = 0x0100        # kHFSVolumeUnmountedBit: set by a clean unmount
ATRB_SPARED = 0x0200
ATRB_NO_CACHE = 0x0400
ATRB_INCONSISTENT = 0x0800     # kHFSBootVolumeInconsistentBit (Mac OS 7.6+/8)
ATRB_SW_LOCK = 0x8000

# File record (cdrFilRec, 102 bytes) field offsets
F_FLAGS, F_FINFO, F_CNID, F_STBLK, F_LGLEN, F_PYLEN = 2, 4, 20, 24, 26, 30
F_RSTBLK, F_RLGLEN, F_RPYLEN, F_CRDAT, F_MDDAT, F_FXINFO = 34, 36, 40, 44, 48, 56
F_CLPSIZE, F_EXTREC, F_REXTREC = 72, 74, 86
FILE_REC_LEN, FOLDER_REC_LEN = 102, 70


class HFSError(Exception):
    pass


@dataclasses.dataclass
class Fork:
    """One fork of a file record: logical/physical length, start block and the
    three extents (start, count) kept in the catalog."""
    logical_size: int
    physical_size: int
    start_block: int
    extents: list

    @classmethod
    def parse(cls, rec: bytes, which: str) -> "Fork":
        if which == "data":
            stblk, lglen, pylen, ext = F_STBLK, F_LGLEN, F_PYLEN, F_EXTREC
        else:
            stblk, lglen, pylen, ext = F_RSTBLK, F_RLGLEN, F_RPYLEN, F_REXTREC
        extents = [struct.unpack(">HH", rec[ext + 4 * i:ext + 4 * i + 4]) for i in range(3)]
        return cls(struct.unpack(">I", rec[lglen:lglen + 4])[0], struct.unpack(">I", rec[pylen:pylen + 4])[0],
                   struct.unpack(">H", rec[stblk:stblk + 2])[0], extents)

    @classmethod
    def empty(cls) -> "Fork":
        return cls(0, 0, 0, [(0, 0)] * 3)

    def covered_blocks(self) -> int:
        return sum(c for _s, c in self.extents)


def locate_volume(image: str) -> tuple[int, int]:
    """(byte offset, byte length) of the HFS volume in a raw disk image: the
    first Apple_HFS partition of an Apple Partition Map whose MDB is plain HFS
    (not a wrapper around HFS+), or a bare HFS volume."""
    with open(image, "rb") as f:
        head = f.read(SECTOR)
        candidates = []
        if head[:2] == b"ER":
            for i in range(1, 64):
                f.seek(SECTOR * i)
                entry = f.read(SECTOR)
                if entry[:2] != b"PM":
                    break
                start, count = struct.unpack(">II", entry[8:16])
                if entry[48:80].split(b"\0")[0] == b"Apple_HFS":
                    candidates.append((start * SECTOR, count * SECTOR))
        else:
            candidates.append((0, os.fstat(f.fileno()).st_size))
        for start, length in candidates:
            f.seek(start + 1024)
            mdb = f.read(SECTOR)
            if mdb[:2] == b"BD" and mdb[0x7C:0x7E] not in (b"H+", b"HX"):
                return start, length
    raise HFSError("no HFS (Mac OS Standard) volume found in image")


def is_hfs_standard(image: str) -> bool:
    try:
        locate_volume(image)
        return True
    except (HFSError, OSError):
        return False


@dataclasses.dataclass
class CatalogRecord:
    parent_id: int
    name_raw: bytes           # Mac OS Roman bytes as stored
    rtype: int
    cnid: int
    node: int
    data_offset: int          # byte offset of the record data within the node
    data: bytes

    @property
    def name(self) -> str:
        return self.name_raw.decode("mac_roman")

    @property
    def is_folder(self) -> bool:
        return self.rtype == REC_FOLDER

    @property
    def is_file(self) -> bool:
        return self.rtype == REC_FILE

    def fork(self, which: str) -> Fork:
        return Fork.parse(self.data, which)

    @property
    def type_creator(self) -> tuple[bytes, bytes]:
        return self.data[F_FINFO:F_FINFO + 4], self.data[F_FINFO + 4:F_FINFO + 8]

    @property
    def finder_flags(self) -> int:
        off = F_FINFO + 8 if self.is_file else 22 + 8
        return struct.unpack(">H", self.data[off:off + 2])[0]


# Mac OS compares HFS names case-insensitively with diacritics significant
# (RelString(..., caseSens=false, diacSens=true)); upper-casing is enough here.
def fold(name: str | bytes) -> bytes:
    raw = name.encode("mac_roman") if isinstance(name, str) else name
    return raw.decode("mac_roman").upper().encode("mac_roman", "replace")


class Volume:
    """View of an HFS volume stored in a file at a byte offset."""

    def __init__(self, image: str, writable: bool = False):
        self.image = image
        self.offset, self.length = locate_volume(image)
        self._f = open(image, "r+b" if writable else "rb")
        self.writable = writable
        self._parse_mdb()
        header = self._read_file(self.catalog_extents, 0, NODE)
        if header[8] != 1:
            raise HFSError("catalog header node expected")
        (self.cat_depth, self.cat_root, self.cat_records, self.cat_first_leaf, self.cat_last_leaf,
         node_size) = struct.unpack(">HIIIIH", header[14:34])
        if node_size != NODE:
            raise HFSError(f"unexpected catalog node size {node_size}")
        self._extents_cache = None

    def _parse_mdb(self) -> None:
        m = self.mdb = self._read_abs(self.offset + 1024, 162)
        if m[:2] != b"BD":
            raise HFSError("bad HFS signature")
        self.attributes = struct.unpack(">H", m[0x0A:0x0C])[0]
        self.alloc_blocks = struct.unpack(">H", m[0x12:0x14])[0]
        self.block_size = struct.unpack(">I", m[0x14:0x18])[0]
        self.first_block_sector = struct.unpack(">H", m[0x1C:0x1E])[0]
        self.next_cnid = struct.unpack(">I", m[0x1E:0x22])[0]
        self.free_blocks = struct.unpack(">H", m[0x22:0x24])[0]
        self.name = m[0x25:0x25 + m[0x24]].decode("mac_roman")
        self.write_count = struct.unpack(">I", m[0x46:0x4A])[0]
        self.finder_info = m[0x5C:0x7C]
        self.extents_extents = [struct.unpack(">HH", m[0x86 + 4 * i:0x8A + 4 * i]) for i in range(3)]
        self.catalog_extents = [struct.unpack(">HH", m[0x96 + 4 * i:0x9A + 4 * i]) for i in range(3)]
        self.catalog_size = struct.unpack(">I", m[0x92:0x96])[0]
        if self.block_size == 0 or self.block_size % SECTOR:
            raise HFSError("bad allocation block size")
        if sum(c for _s, c in self.catalog_extents) * self.block_size < self.catalog_size:
            raise HFSError("catalog file uses the extents overflow file; unsupported")

    def close(self) -> None:
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def clean(self) -> bool:
        """Mac OS unmounted the volume cleanly (it clears the bit while mounted)."""
        return bool(self.attributes & ATRB_UNMOUNTED) and not self.attributes & ATRB_INCONSISTENT

    @property
    def blessed_folder_id(self) -> int:
        return struct.unpack(">I", self.finder_info[0:4])[0]

    # -- raw access -------------------------------------------------------
    def _read_abs(self, pos: int, size: int) -> bytes:
        self._f.seek(pos)
        data = self._f.read(size)
        if len(data) != size:
            raise HFSError("short read")
        return data

    def _write_abs(self, pos: int, data: bytes) -> None:
        if not self.writable:
            raise HFSError("volume opened read-only")
        if not (self.offset <= pos and pos + len(data) <= self.offset + self.length):
            raise HFSError("write outside the volume")
        self._f.seek(pos)
        self._f.write(data)

    def _block_pos(self, block: int) -> int:
        return self.offset + self.first_block_sector * SECTOR + block * self.block_size

    def _file_byte_pos(self, extents, file_offset: int) -> int:
        block, within = divmod(file_offset, self.block_size)
        for start, count in extents:
            if block < count:
                return self._block_pos(start + block) + within
            block -= count
        raise HFSError("offset beyond extents")

    def _read_file(self, extents, file_offset: int, size: int) -> bytes:
        out = bytearray()
        while size:
            pos = self._file_byte_pos(extents, file_offset)
            chunk = min(size, self.block_size - file_offset % self.block_size)
            out += self._read_abs(pos, chunk)
            file_offset += chunk
            size -= chunk
        return bytes(out)

    def _write_file(self, extents, file_offset: int, data: bytes) -> None:
        while data:
            pos = self._file_byte_pos(extents, file_offset)
            chunk = min(len(data), self.block_size - file_offset % self.block_size)
            self._write_abs(pos, data[:chunk])
            file_offset += chunk
            data = data[chunk:]

    def flush(self) -> None:
        self._f.flush()
        os.fsync(self._f.fileno())

    # -- B-trees ------------------------------------------------------------
    def _leaf_records(self, extents, first_leaf: int) -> Iterator[tuple[int, int, bytes, bytes]]:
        """(node, record offset, key bytes, record bytes) of every leaf record."""
        n = first_leaf
        seen = set()
        while n:
            if n in seen:
                raise HFSError("B-tree leaf chain loops")
            seen.add(n)
            node = self._read_file(extents, n * NODE, NODE)
            flink, _blink, kind, _height, count = struct.unpack(">IIbBH", node[:12])
            if kind != -1:
                raise HFSError(f"node {n} is not a leaf")
            for i in range(count):
                off = struct.unpack(">H", node[NODE - 2 * (i + 1):NODE - 2 * i])[0]
                klen = node[off]
                key = node[off + 1:off + 1 + klen]
                doff = off + 1 + klen
                doff += doff & 1
                yield n, doff, key, node[doff:]
            n = flink

    def _overflow_extents(self, cnid: int, fork_type: int) -> list[tuple[int, int, list]]:
        """Extents overflow records for a file fork: [(start FABN, extents)]."""
        if self._extents_cache is None:
            self._extents_cache = {}
            header = self._read_file(self.extents_extents, 0, NODE)
            first_leaf = struct.unpack(">I", header[24:28])[0]
            for _n, _off, key, rec in self._leaf_records(self.extents_extents, first_leaf):
                if len(key) < 7:
                    continue
                ftype, fnum, fabn = struct.unpack(">BIH", key[:7])
                exts = [struct.unpack(">HH", rec[4 * i:4 * i + 4]) for i in range(3)]
                self._extents_cache.setdefault((fnum, ftype), []).append((fabn, exts))
        return sorted(self._extents_cache.get((cnid, fork_type), []))

    def records(self) -> Iterator[CatalogRecord]:
        for n, doff, key, rec in self._leaf_records(self.catalog_extents, self.cat_first_leaf):
            parent = struct.unpack(">I", key[1:5])[0]
            name = key[6:6 + key[5]]
            rtype = rec[0]
            if rtype == REC_FILE:
                yield CatalogRecord(parent, name, rtype, struct.unpack(">I", rec[F_CNID:F_CNID + 4])[0], n, doff,
                                    rec[:FILE_REC_LEN])
            elif rtype == REC_FOLDER:
                yield CatalogRecord(parent, name, rtype, struct.unpack(">I", rec[6:10])[0], n, doff,
                                    rec[:FOLDER_REC_LEN])

    def index(self) -> dict:
        return {(rec.parent_id, fold(rec.name_raw)): rec for rec in self.records()}

    def lookup(self, path: list[str], index: dict | None = None) -> CatalogRecord:
        """Resolve Mac path components below the volume's root folder."""
        index = index if index is not None else self.index()
        parent = ROOT_FOLDER_ID
        rec = None
        for comp in path:
            rec = index.get((parent, fold(comp)))
            if rec is None:
                raise FileNotFoundError(":".join(path))
            parent = rec.cnid
        if rec is None:
            raise HFSError("empty path")
        return rec

    def children(self, folder_id: int) -> list[CatalogRecord]:
        return [r for r in self.records() if r.parent_id == folder_id]

    def path_of(self, cnid: int, index: dict | None = None) -> list[str]:
        """Path (names below the root folder) of a folder CNID."""
        folders = {r.cnid: r for r in (index or self.index()).values() if r.is_folder}
        path = []
        while cnid != ROOT_FOLDER_ID:
            rec = folders.get(cnid)
            if rec is None:
                raise FileNotFoundError(f"folder {cnid}")
            path.insert(0, rec.name)
            cnid = rec.parent_id
        return path

    # -- forks ----------------------------------------------------------------
    def fork_extents(self, rec: CatalogRecord, which: str) -> list:
        fork = rec.fork(which)
        extents = [e for e in fork.extents if e[1]]
        need = -(-fork.physical_size // self.block_size)
        if sum(c for _s, c in extents) < need:
            for _fabn, exts in self._overflow_extents(rec.cnid, 0 if which == "data" else 0xFF):
                extents += [e for e in exts if e[1]]
        if sum(c for _s, c in extents) * self.block_size < fork.logical_size:
            raise HFSError(f"{rec.name}: extents do not cover the {which} fork")
        return extents

    def read_fork(self, rec: CatalogRecord, which: str) -> bytes:
        fork = rec.fork(which)
        if not fork.logical_size:
            return b""
        return self._read_file(self.fork_extents(rec, which), 0, fork.logical_size)

    def write_fork_in_place(self, rec: CatalogRecord, which: str, offset: int, data: bytes) -> None:
        fork = rec.fork(which)
        if offset < 0 or offset + len(data) > fork.logical_size:
            raise HFSError("in-place write would grow the fork")
        self._write_file(self.fork_extents(rec, which), offset, data)

    # -- catalog records (fixed-size fields only) -------------------------------
    def _write_record_field(self, rec: CatalogRecord, field_offset: int, data: bytes) -> None:
        node_pos = rec.node * NODE + rec.data_offset + field_offset
        self._write_file(self.catalog_extents, node_pos, data)

    def set_fork_record(self, rec: CatalogRecord, which: str, fork: Fork) -> None:
        """Rewrite one fork descriptor (start block, lengths, three extents) in place."""
        if not rec.is_file:
            raise HFSError("not a file record")
        if which == "data":
            stblk, lens, ext = F_STBLK, F_LGLEN, F_EXTREC
        else:
            stblk, lens, ext = F_RSTBLK, F_RLGLEN, F_REXTREC
        self._write_record_field(rec, stblk, struct.pack(">H", fork.start_block))
        self._write_record_field(rec, lens, struct.pack(">II", fork.logical_size, fork.physical_size))
        self._write_record_field(rec, ext, b"".join(struct.pack(">HH", s, c) for s, c in fork.extents))

    def set_finder_info(self, rec: CatalogRecord, finfo: bytes) -> None:
        """The 16-byte FInfo of a file (type, creator, flags, location, folder)."""
        if not rec.is_file or len(finfo) != 16:
            raise HFSError("FInfo needs a file record and 16 bytes")
        self._write_record_field(rec, F_FINFO, finfo)

    # -- volume attributes ------------------------------------------------------
    def set_attributes(self, value: int) -> None:
        """drAtrb in the MDB and in the alternate MDB (second-to-last sector)."""
        packed = struct.pack(">H", value)
        self._write_abs(self.offset + 1024 + 0x0A, packed)
        alt = self.offset + self.length - 1024
        if self._read_abs(alt, 2) == b"BD":
            self._write_abs(alt + 0x0A, packed)
        self.attributes = value


def resource_fork_of(vol: Volume, path: list[str]) -> bytes:
    return vol.read_fork(vol.lookup(path), "rsrc")
