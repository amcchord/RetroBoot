"""Offline access to the HFS+ startup volume inside a raw classic Mac disk image.

Only ever use these helpers while the guest is stopped and nothing else has the
image open. Structural changes (creating or deleting catalog records) are left
to the Linux hfsplus driver; this module only reads structures and rewrites
fixed-size fields in place, so B-tree shape never changes here.
"""
from __future__ import annotations

import dataclasses
import os
import struct
from typing import Iterator, Optional

SECTOR = 512
ROOT_FOLDER_ID = 2
REC_FOLDER, REC_FILE = 1, 2


class HFSError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Extent:
    start: int
    count: int


@dataclasses.dataclass
class Fork:
    logical_size: int
    clump_size: int
    total_blocks: int
    extents: list

    @classmethod
    def parse(cls, raw: bytes) -> "Fork":
        size, clump, total = struct.unpack(">QII", raw[:16])
        extents = [Extent(*struct.unpack(">II", raw[16 + 8 * i:24 + 8 * i])) for i in range(8)]
        return cls(size, clump, total, extents)

    def pack(self) -> bytes:
        out = struct.pack(">QII", self.logical_size, self.clump_size, self.total_blocks)
        for e in self.extents:
            out += struct.pack(">II", e.start, e.count)
        return out

    @classmethod
    def empty(cls) -> "Fork":
        return cls(0, 0, 0, [Extent(0, 0)] * 8)

    def covered_blocks(self) -> int:
        return sum(e.count for e in self.extents)


def locate_volume(image: str) -> tuple[int, int]:
    """Return (byte offset, byte length) of the HFS+ volume in a raw disk image.

    Supports Apple Partition Map disks whose Apple_HFS partition is either a
    plain HFS+ volume or an HFS wrapper with an embedded HFS+ volume, and bare
    HFS+ volumes.
    """
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
                ptype = entry[48:80].split(b"\0")[0]
                if ptype == b"Apple_HFS":
                    candidates.append((start * SECTOR, count * SECTOR))
        else:
            candidates.append((0, os.fstat(f.fileno()).st_size))
        for start, length in candidates:
            f.seek(start + 1024)
            hdr = f.read(512)
            if hdr[:2] in (b"H+", b"HX"):
                return start, length
            if hdr[:2] == b"BD" and hdr[0x7C:0x7E] == b"H+":
                al_size = struct.unpack(">I", hdr[0x14:0x18])[0]
                al_start = struct.unpack(">H", hdr[0x1C:0x1E])[0]
                emb_start, emb_count = struct.unpack(">HH", hdr[0x7E:0x82])
                return (start + al_start * SECTOR + emb_start * al_size, emb_count * al_size)
    raise HFSError("no HFS+ volume found in image")


@dataclasses.dataclass
class CatalogRecord:
    parent_id: int
    name: str
    rtype: int
    cnid: int
    node: int
    data_offset: int          # byte offset of the record data within the node
    data: bytes

    @property
    def is_folder(self) -> bool:
        return self.rtype == REC_FOLDER

    @property
    def is_file(self) -> bool:
        return self.rtype == REC_FILE

    def fork(self, which: str) -> Fork:
        base = 88 if which == "data" else 168
        return Fork.parse(self.data[base:base + 80])

    @property
    def type_creator(self) -> tuple[bytes, bytes]:
        return self.data[48:52], self.data[52:56]


def _fold(name: str) -> str:
    return name.casefold()


class Volume:
    """Read-mostly view of an HFS+ volume stored in a file at a byte offset."""

    def __init__(self, image: str, writable: bool = False):
        self.image = image
        self.offset, self.length = locate_volume(image)
        self._f = open(image, "r+b" if writable else "rb")
        self.writable = writable
        vh = self._read_abs(self.offset + 1024, 512)
        if vh[:2] not in (b"H+", b"HX"):
            raise HFSError("bad HFS+ signature")
        self.attributes = struct.unpack(">I", vh[4:8])[0]
        self.block_size = struct.unpack(">I", vh[0x28:0x2C])[0]
        self.total_blocks = struct.unpack(">I", vh[0x2C:0x30])[0]
        self.catalog_fork = Fork.parse(vh[0x110:0x160])
        if self.catalog_fork.covered_blocks() < self.catalog_fork.total_blocks:
            raise HFSError("catalog file uses the extents overflow file; unsupported")
        header = self._read_fork(self.catalog_fork, 0, 512)
        self.node_size = struct.unpack(">H", header[14 + 18:14 + 20])[0]
        self.first_leaf = struct.unpack(">I", header[14 + 10:14 + 14])[0]

    def close(self) -> None:
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def clean(self) -> bool:
        return bool(self.attributes & 0x100) and not self.attributes & 0x800

    # -- raw access -------------------------------------------------------
    def _read_abs(self, pos: int, size: int) -> bytes:
        self._f.seek(pos)
        data = self._f.read(size)
        if len(data) != size:
            raise HFSError("short read")
        return data

    def _fork_byte_pos(self, fork: Fork, fork_offset: int) -> int:
        block, within = divmod(fork_offset, self.block_size)
        for e in fork.extents:
            if block < e.count:
                return self.offset + (e.start + block) * self.block_size + within
            block -= e.count
        raise HFSError("offset beyond fork extents")

    def _read_fork(self, fork: Fork, fork_offset: int, size: int) -> bytes:
        out = b""
        while size:
            pos = self._fork_byte_pos(fork, fork_offset)
            chunk = min(size, self.block_size - fork_offset % self.block_size)
            out += self._read_abs(pos, chunk)
            fork_offset += chunk
            size -= chunk
        return out

    def _write_fork(self, fork: Fork, fork_offset: int, data: bytes) -> None:
        if not self.writable:
            raise HFSError("volume opened read-only")
        while data:
            pos = self._fork_byte_pos(fork, fork_offset)
            chunk = min(len(data), self.block_size - fork_offset % self.block_size)
            self._f.seek(pos)
            self._f.write(data[:chunk])
            fork_offset += chunk
            data = data[chunk:]

    def flush(self) -> None:
        self._f.flush()
        os.fsync(self._f.fileno())

    def free_runs(self) -> list[tuple[int, int]]:
        """Free allocation-block runs (start, count) in volume order, from the
        allocation file (volume header 0x70)."""
        vh = self._read_abs(self.offset + 1024, 512)
        alloc = Fork.parse(vh[0x70:0xC0])
        bitmap = self._read_fork(alloc, 0, (self.total_blocks + 7) // 8)
        runs, cur, i = [], None, 0
        for byte in bitmap:
            if (byte == 0 and cur is not None) or (byte == 0xFF and cur is None):
                i += 8
                continue
            for k in range(8):
                if i + k >= self.total_blocks:
                    break
                used = (byte >> (7 - k)) & 1
                if not used and cur is None:
                    cur = i + k
                elif used and cur is not None:
                    runs.append((cur, i + k - cur))
                    cur = None
            i += 8
        if cur is not None and cur < self.total_blocks:
            runs.append((cur, self.total_blocks - cur))
        return runs

    # -- catalog ----------------------------------------------------------
    def _node(self, n: int) -> bytes:
        return self._read_fork(self.catalog_fork, n * self.node_size, self.node_size)

    def records(self) -> Iterator[CatalogRecord]:
        n = self.first_leaf
        seen = set()
        while n:
            if n in seen:
                raise HFSError("catalog leaf chain loops")
            seen.add(n)
            node = self._node(n)
            flink, _blink, kind, _height, count = struct.unpack(">IIbBH", node[:12])
            if kind != -1:
                raise HFSError(f"node {n} is not a leaf")
            for i in range(count):
                off = struct.unpack(">H", node[self.node_size - 2 * (i + 1):self.node_size - 2 * i])[0]
                klen = struct.unpack(">H", node[off:off + 2])[0]
                parent = struct.unpack(">I", node[off + 2:off + 6])[0]
                nlen = struct.unpack(">H", node[off + 6:off + 8])[0]
                name = node[off + 8:off + 8 + 2 * nlen].decode("utf-16-be")
                doff = off + 2 + klen
                rtype = struct.unpack(">h", node[doff:doff + 2])[0]
                if rtype not in (REC_FOLDER, REC_FILE):
                    continue
                size = 248 if rtype == REC_FILE else 88
                data = node[doff:doff + size]
                cnid = struct.unpack(">I", data[8:12])[0]
                yield CatalogRecord(parent, name, rtype, cnid, n, doff, data)
            n = flink

    def lookup(self, path: list[str]) -> CatalogRecord:
        """Resolve a list of Mac path components below the volume root."""
        index: dict[tuple[int, str], CatalogRecord] = {}
        for rec in self.records():
            index[(rec.parent_id, _fold(rec.name))] = rec
        parent = ROOT_FOLDER_ID
        rec = None
        for comp in path:
            rec = index.get((parent, _fold(comp)))
            if rec is None:
                raise FileNotFoundError(":".join(path))
            parent = rec.cnid
        if rec is None:
            raise HFSError("empty path")
        return rec

    def read_fork(self, rec: CatalogRecord, which: str) -> bytes:
        fork = rec.fork(which)
        return self._read_fork(fork, 0, fork.logical_size)

    def write_fork_in_place(self, rec: CatalogRecord, which: str, offset: int, data: bytes) -> None:
        fork = rec.fork(which)
        if offset + len(data) > fork.logical_size:
            raise HFSError("in-place write would grow the fork")
        self._write_fork(fork, offset, data)

    def set_fork_record(self, rec: CatalogRecord, which: str, fork: Fork) -> None:
        """Rewrite one 80-byte fork descriptor of a file record in place."""
        if not rec.is_file:
            raise HFSError("not a file record")
        base = 88 if which == "data" else 168
        node_pos = rec.node * self.node_size + rec.data_offset + base
        self._write_fork(self.catalog_fork, node_pos, fork.pack())


# -- resource forks -------------------------------------------------------

def parse_resource_fork(fork: bytes) -> list[tuple[str, int, str, int, bytes]]:
    """Return (type, id, name, data_offset_in_fork, data) for every resource."""
    if len(fork) < 16:
        return []
    doff, moff, dlen, mlen = struct.unpack(">IIII", fork[:16])
    if moff + mlen > len(fork) or doff + dlen > len(fork):
        raise HFSError("resource fork header out of range")
    rmap = fork[moff:moff + mlen]
    tl_off, nl_off = struct.unpack(">HH", rmap[24:28])
    tlist = rmap[tl_off:]
    nlist = rmap[nl_off:]
    ntypes = (struct.unpack(">H", tlist[:2])[0] + 1) & 0xFFFF
    out = []
    for i in range(ntypes):
        rtype, count, ref = struct.unpack(">4sHH", tlist[2 + 8 * i:10 + 8 * i])
        for j in range(count + 1):
            rid, noff, attr_off = struct.unpack(">hHI", tlist[ref + 12 * j:ref + 12 * j + 8])
            data_off = doff + (attr_off & 0xFFFFFF)
            length = struct.unpack(">I", fork[data_off:data_off + 4])[0]
            name = ""
            if noff != 0xFFFF:
                name = nlist[noff + 1:noff + 1 + nlist[noff]].decode("mac_roman")
            out.append((rtype.decode("mac_roman"), rid, name, data_off + 4,
                        fork[data_off + 4:data_off + 4 + length]))
    return out


def build_resource_fork(resources: list[tuple[str, int, str, bytes]]) -> bytes:
    """Build a resource fork from (type, id, name, data) tuples (attributes 0)."""
    data = b""
    offsets = []
    for _t, _rid, _name, blob in resources:
        offsets.append(len(data))
        data += struct.pack(">I", len(blob)) + blob
    by_type: dict[str, list[int]] = {}
    for i, (rtype, *_rest) in enumerate(resources):
        if len(rtype.encode("mac_roman")) != 4:
            raise ValueError(f"resource type must be 4 bytes: {rtype!r}")
        by_type.setdefault(rtype, []).append(i)
    type_list = struct.pack(">H", (len(by_type) - 1) & 0xFFFF)
    refs = b""
    names = b""
    ref_base = 2 + 8 * len(by_type)
    for rtype, idxs in by_type.items():
        type_list += rtype.encode("mac_roman") + struct.pack(">HH", len(idxs) - 1, ref_base + len(refs))
        for i in idxs:
            _t, rid, name, _blob = resources[i]
            noff = 0xFFFF
            if name:
                raw = name.encode("mac_roman")
                noff = len(names)
                names += bytes([len(raw)]) + raw
            refs += struct.pack(">hHII", rid, noff, offsets[i] & 0xFFFFFF, 0)
    types_and_refs = type_list + refs
    rmap = bytearray(16) + struct.pack(">IHH", 0, 0, 0) + struct.pack(">HH", 28, 28 + len(types_and_refs))
    rmap += types_and_refs + names
    header = struct.pack(">IIII", 256, 256 + len(data), len(data), len(rmap))
    rmap[0:16] = header
    return header + bytes(240) + data + bytes(rmap)
