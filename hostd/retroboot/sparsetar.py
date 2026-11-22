"""Thin machine archives: ``disk.img`` as a GNU PAX sparse 1.0 tar member.

A *thin archive* is a schema-1 machine archive (gzip, then a tar of
``config.json``, ``disk.img`` and optional ``preview.png``) whose ``disk.img``
is stored the way ``tar --posix --sparse`` (GNU tar >= 1.23, libarchive) stores
sparse files: a PAX extended header with exactly ``GNU.sparse.major=1``,
``GNU.sparse.minor=0``, ``GNU.sparse.name`` and ``GNU.sparse.realsize``, then a
ustar member whose data is the sparse map (decimal: extent count, then offset
and size per extent, newline separated, NUL padded to 512 bytes) followed by
the data extents back to back.  Only the disk's non-zero 4 KiB blocks are
compressed and transferred, and an import writes only them.

Reading: :class:`GuardedReader` bounds what ``tarfile`` may read while it
parses headers (single reads, total header bytes, no backward seeks), and
:func:`check_sparse_member` accepts a sparse member only when its map is
sorted, non-overlapping, 512-byte aligned, inside the declared disk size,
within :data:`MAX_EXTENTS`, and its stored data length matches the tar header
exactly.  Other GNU sparse encodings (old GNU ``S`` members, PAX 0.0/0.1) are
refused.

Writing (image builders and lab tools, not the device): :func:`data_extents`
finds a raw disk's non-zero 4 KiB blocks with ``SEEK_DATA``/``SEEK_HOLE`` plus
a zero scan, and :func:`write_thin` / :func:`write_classic` produce
deterministic archives (mtime 0, uid/gid 0, gzip without name or time).
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import tarfile
import zlib
from pathlib import Path
from typing import BinaryIO, Callable, Iterable

BLOCK = 512
GRANULE = 4096                  # producer granularity: the XFS/ext4 block size on RBDATA
MiB = 1024 * 1024
CHUNK = MiB
MAX_EXTENTS = 1 << 18           # sparse map entries; a 375 MiB Mac OS disk has a few thousand
MAX_READ = 2 * MiB              # largest single read tarfile may make (pax/long-name headers included)
HEADER_BUDGET = 8 * MiB         # bytes tarfile may read while parsing one member's headers and sparse map
FORMAT = "retroboot-thin-1"     # catalog ``thinArchive.format``
SPARSE_KEYS = {"GNU.sparse.major", "GNU.sparse.minor", "GNU.sparse.name", "GNU.sparse.realsize"}
SPARSE_PAX_ALLOWED = SPARSE_KEYS | {"mtime", "atime", "ctime", "uid", "gid", "uname", "gname"}
DIGITS = re.compile(r"^[0-9]{1,15}$")


class Unsafe(Exception):
    """The archive is malformed or outside the accepted subset."""


def block(n: int) -> int:
    return (n + BLOCK - 1) // BLOCK * BLOCK


# --- Reading ------------------------------------------------------------------------------

class GuardedReader:
    """File object for ``tarfile.TarFile`` over a decompressed stream.

    ``headers()`` before each ``TarFile.next()`` gives header parsing a fresh
    budget of ``header_budget`` bytes (pax records, long names and a sparse map
    included); ``data()`` afterwards lifts it while the caller reads member data
    in chunks of at most ``max_read``.  Backward seeks (which would restart
    decompression) are refused, so the stream is decompressed once.
    """

    def __init__(self, f: BinaryIO, max_read: int | None = None, header_budget: int | None = None):
        self._f = f
        self.max_read = max_read or MAX_READ
        self.header_budget = header_budget or HEADER_BUDGET
        self._left: int | None = self.header_budget     # TarFile() parses the first header at once

    def headers(self) -> None:
        self._left = self.header_budget

    def data(self) -> None:
        self._left = None

    def _spend(self, n: int) -> None:
        if self._left is not None:
            if n > self._left:
                raise Unsafe("archive headers are too large")
            self._left -= n

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0 or n > self.max_read:
            raise Unsafe("unbounded read")
        self._spend(n)
        return self._f.read(n)

    def seek(self, pos: int, whence: int = 0) -> int:
        here = self._f.tell()
        if whence == 1:
            pos += here
        elif whence != 0:
            raise Unsafe("unsupported seek")
        if pos < here:
            raise Unsafe("backward seek")
        if pos - here > self.max_read:
            raise Unsafe("archive skips too much data")
        self._spend(pos - here)
        return self._f.seek(pos)

    def tell(self) -> int:
        return self._f.tell()


def validate_map(extents, realsize: int, max_extents: int | None = None) -> int:
    """Check a sparse map; returns the stored (data) byte count.

    Extents must be (offset, size) integers, 512-byte aligned, sorted and
    non-overlapping (touching is allowed), and end inside ``realsize``.  A
    zero-size extent is only allowed as the last entry at ``realsize`` (GNU tar's
    end marker for a file that ends in a hole).
    """
    if type(realsize) is not int or realsize <= 0 or realsize % BLOCK:
        raise Unsafe("invalid sparse file size")
    if not isinstance(extents, list) or not 1 <= len(extents) <= (max_extents or MAX_EXTENTS):
        raise Unsafe("invalid sparse map length")
    pos = stored = 0
    last = len(extents) - 1
    for i, ext in enumerate(extents):
        if not isinstance(ext, tuple) or len(ext) != 2:
            raise Unsafe("invalid sparse map entry")
        off, size = ext
        if type(off) is not int or type(size) is not int:
            raise Unsafe("invalid sparse map entry")
        if off < pos or size < 0 or off % BLOCK or size % BLOCK or off + size > realsize:
            raise Unsafe("sparse map entries overlap, are unaligned or out of range")
        if size == 0 and not (i == last and off == realsize):
            raise Unsafe("empty sparse map entry")
        pos = off + size
        stored += size
    return stored


def check_plain_member(member: tarfile.TarInfo) -> None:
    """A non-sparse member must not carry any GNU sparse header fields."""
    if member.sparse is not None or any(k.startswith("GNU.sparse.") for k in member.pax_headers):
        raise Unsafe("sparse fields on a regular member")


def check_sparse_member(member: tarfile.TarInfo, next_header: int,
                        max_extents: int | None = None) -> tuple[list[tuple[int, int]], int]:
    """Validate a PAX sparse 1.0 member parsed by tarfile.

    ``next_header`` is ``TarFile.offset`` right after ``next()`` returned the
    member: where the following header starts.  Returns (data extents without
    the end marker, stored bytes).
    """
    pax = member.pax_headers
    if member.type != tarfile.REGTYPE or member.sparse is None:
        raise Unsafe("not a PAX sparse member")
    if pax.get("GNU.sparse.major") != "1" or pax.get("GNU.sparse.minor") != "0":
        raise Unsafe("only GNU PAX sparse format 1.0 is supported")
    if not SPARSE_KEYS <= set(pax) or set(pax) - SPARSE_PAX_ALLOWED:
        raise Unsafe("unexpected sparse header fields")
    if pax["GNU.sparse.name"] != member.name or not DIGITS.fullmatch(pax["GNU.sparse.realsize"]):
        raise Unsafe("invalid sparse header fields")
    realsize = int(pax["GNU.sparse.realsize"])
    if member.size != realsize:
        raise Unsafe("sparse size mismatch")
    stored = validate_map(member.sparse, realsize, max_extents)
    if member.offset_data % BLOCK or next_header - member.offset_data != block(stored):
        raise Unsafe("sparse data length does not match its map")
    return [e for e in member.sparse if e[1]], stored


# --- Writing (builders) -------------------------------------------------------------------

def data_extents(fd: int, size: int, granule: int = GRANULE) -> list[tuple[int, int]]:
    """Non-zero ``granule`` blocks of a file as merged (offset, length) extents.

    Holes are skipped with SEEK_DATA/SEEK_HOLE where the filesystem reports
    them; allocated regions are read and their all-zero granules dropped.  The
    last extent may end at ``size`` when that is not granule aligned.
    """
    zero = bytes(granule)
    out: list[list[int]] = []

    def add(off: int, n: int) -> None:
        if out and out[-1][0] + out[-1][1] == off:
            out[-1][1] += n
        else:
            out.append([off, n])

    pos = 0
    while pos < size:
        try:
            data = os.lseek(fd, pos, os.SEEK_DATA)
        except OSError as exc:
            if exc.errno == errno.ENXIO:     # only a hole remains
                break
            data = pos                       # no SEEK_DATA: scan everything
        try:
            hole = min(os.lseek(fd, data, os.SEEK_HOLE), size)
        except OSError:
            hole = size
        p = data - data % granule
        if out:
            p = max(p, out[-1][0] + out[-1][1])
        while p < hole:
            buf = os.pread(fd, min(CHUNK - p % CHUNK, hole - p), p)
            if not buf:
                break
            if buf.count(0) != len(buf):
                for i in range(0, len(buf), granule):
                    g = buf[i:i + granule]
                    if g != zero[:len(g)]:
                        add(p + i, len(g))
            p += len(buf)
        pos = max(hole, p)
    return [tuple(e) for e in out]


def coalesce(extents: list[tuple[int, int]], max_extents: int | None = None) -> list[tuple[int, int]]:
    """Merge across the smallest gaps until at most ``max_extents`` remain (the gaps become stored zeros)."""
    max_extents = max_extents or MAX_EXTENTS
    if len(extents) <= max_extents:
        return extents
    gaps = sorted(range(len(extents) - 1),
                  key=lambda i: extents[i + 1][0] - extents[i][0] - extents[i][1])[: len(extents) - max_extents]
    join = set(gaps)
    out: list[list[int]] = []
    for i, (off, n) in enumerate(extents):
        if out and (i - 1) in join:
            out[-1][1] = off + n - out[-1][0]
        else:
            out.append([off, n])
    return [tuple(e) for e in out]


def _octal(n: int, width: int) -> bytes:
    if n < 8 ** (width - 1):
        return b"%0*o\0" % (width - 1, n)
    # GNU base-256 for sizes beyond ustar's 8 GiB (tarfile, GNU tar and libarchive read it)
    return bytes([0x80]) + n.to_bytes(width - 1, "big")


def ustar_header(name: str, size: int, typeflag: bytes = b"0", mtime: int = 0, mode: int = 0o644,
                 gnu: bool = False) -> bytes:
    raw = name.encode()
    prefix = b""
    if len(raw) > 100:
        prefix, _, raw = raw.rpartition(b"/")
        if len(raw) > 100 or len(prefix) > 155:
            raise ValueError(f"name too long: {name}")
    h = bytearray(BLOCK)
    h[0:len(raw)] = raw
    h[100:108] = b"%07o\0" % mode
    h[108:116] = b"0000000\0"
    h[116:124] = b"0000000\0"
    h[124:136] = _octal(size, 12)
    h[136:148] = _octal(mtime, 12)
    h[148:156] = b" " * 8
    h[156:157] = typeflag
    h[257:265] = b"ustar  \0" if gnu else b"ustar\x0000"
    h[345:345 + len(prefix)] = prefix
    h[148:156] = b"%06o\0 " % sum(h)
    return bytes(h)


def pax_records(fields: dict[str, str]) -> bytes:
    out = b""
    for k, v in fields.items():
        body = b" %s=%s\n" % (k.encode(), v.encode())
        n = len(body) + 1
        while len(str(n)) + len(body) != n:
            n = len(str(n)) + len(body)
        out += str(n).encode() + body
    return out


def pad(data: bytes) -> bytes:
    return data + bytes(block(len(data)) - len(data))


class _Gzip:
    """Deterministic gzip writer that also hashes and counts the compressed bytes."""

    def __init__(self, out: BinaryIO, level: int):
        self.out = out
        self.comp = zlib.compressobj(level, zlib.DEFLATED, 31, 9)   # gzip wrapper: no name, mtime 0
        self.sha = hashlib.sha256()
        self.bytes = 0
        self.tar_bytes = 0

    def write(self, data) -> None:
        self.tar_bytes += len(data)
        self._emit(self.comp.compress(data))

    def _emit(self, c: bytes) -> None:
        if c:
            self.out.write(c)
            self.sha.update(c)
            self.bytes += len(c)

    def finish(self) -> None:
        rec = 20 * BLOCK                         # end-of-archive blocks, padded to GNU's 10 KiB record
        self.write(bytes(2 * BLOCK + (-(self.tar_bytes + 2 * BLOCK)) % rec))
        self._emit(self.comp.flush())


def storage_charge(n: int) -> int:
    """ClassicMac/hostd requiredStorageBytes accounting: whole MiB chunks."""
    return (n + MiB - 1) // MiB * MiB


def _disk_charge(extents: Iterable[tuple[int, int]]) -> int:
    chunks = set()
    for off, n in extents:
        chunks.update(range(off // MiB, (off + n - 1) // MiB + 1))
    return len(chunks) * MiB


Progress = Callable[[int, int], None]


def write_thin(out: BinaryIO, files: list[tuple[str, Path]], disk_name: str = "disk.img", level: int = 9,
               mtime: int = 0, progress: Progress | None = None) -> dict:
    """Write a thin archive of ``files`` (name, path) in order; returns catalog facts."""
    gz = _Gzip(out, level)
    facts = {"format": FORMAT, "installedBytes": 0, "requiredStorageBytes": 0}
    for name, path in files:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            if name != disk_name:
                data = f.read()
                if len(data) != st.st_size:
                    raise ValueError(f"{path} changed while reading")
                gz.write(ustar_header(name, len(data), mtime=mtime))
                gz.write(pad(data))
                facts["installedBytes"] += len(data)
                facts["requiredStorageBytes"] += storage_charge(len(data))
                continue
            size = st.st_size
            if size <= 0 or size % BLOCK:
                raise ValueError("disk.img must be a raw, sector-aligned disk")
            extents = coalesce(data_extents(f.fileno(), size))
            stored = sum(n for _, n in extents)
            entries = list(extents)
            if not entries or entries[-1][0] + entries[-1][1] != size:
                entries.append((size, 0))
            smap = pad(("%d\n" % len(entries) + "".join("%d\n%d\n" % e for e in entries)).encode())
            pax = pax_records({"GNU.sparse.major": "1", "GNU.sparse.minor": "0",
                               "GNU.sparse.name": name, "GNU.sparse.realsize": str(size)})
            gz.write(ustar_header(f"PaxHeaders/{name}", len(pax), b"x", mtime))
            gz.write(pad(pax))
            gz.write(ustar_header(f"GNUSparseFile.0/{name}", len(smap) + stored, mtime=mtime))
            gz.write(smap)
            done = 0
            for off, n in extents:
                p = off
                while p < off + n:
                    buf = os.pread(f.fileno(), min(CHUNK, off + n - p), p)
                    if not buf:
                        raise ValueError(f"{path} was truncated while reading")
                    gz.write(buf)
                    p += len(buf)
                    done += len(buf)
                    if progress:
                        progress(done, stored)
            gz.write(bytes(block(stored) - stored))
            after = os.fstat(f.fileno())
            if (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                raise ValueError(f"{path} changed while reading")
            facts.update(diskCapacityBytes=size, storedBytes=stored, extents=len(extents))
            facts["installedBytes"] += size
            facts["requiredStorageBytes"] += _disk_charge(extents)
    gz.finish()
    facts.update(archiveBytes=gz.bytes, sha256=gz.sha.hexdigest())
    return facts


def write_classic(out: BinaryIO, files: list[tuple[str, Path]], disk_name: str = "disk.img", level: int = 6,
                  mtime: int = 0, progress: Progress | None = None) -> dict:
    """Write a ClassicMac schema-1 archive (GNU tar, regular members, every disk byte stored).

    For ClassicMac's own reader and hostd versions before thin archives.  Zero
    runs still pass through the compressor, so this takes minutes for a 32 GiB
    disk; the facts match ClassicMac's ``package-machine-template.py``.
    """
    gz = _Gzip(out, level)
    facts = {"format": "classic", "installedBytes": 0, "requiredStorageBytes": 0}
    zero = bytes(CHUNK)
    for name, path in files:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            size = st.st_size
            if name == disk_name and (size <= 0 or size % BLOCK):
                raise ValueError("disk.img must be a raw, sector-aligned disk")
            gz.write(ustar_header(name, size, mtime=mtime, gnu=True))
            if name != disk_name:
                data = f.read()
                gz.write(pad(data))
                facts["installedBytes"] += size
                facts["requiredStorageBytes"] += storage_charge(size)
                continue
            extents = data_extents(f.fileno(), size)
            p = 0
            for off, n in extents + [(size, 0)]:
                while p < off:                       # hole: zeros, without reading the file
                    k = min(CHUNK, off - p)
                    gz.write(zero[:k] if k < CHUNK else zero)
                    p += k
                while p < off + n:
                    buf = os.pread(f.fileno(), min(CHUNK, off + n - p), p)
                    if not buf:
                        raise ValueError(f"{path} was truncated while reading")
                    gz.write(buf)
                    p += len(buf)
                if progress:
                    progress(p, size)
            gz.write(bytes(block(size) - size))
            facts.update(diskCapacityBytes=size)
            facts["installedBytes"] += size
            facts["requiredStorageBytes"] += _disk_charge(extents)
    gz.finish()
    facts.update(archiveBytes=gz.bytes, sha256=gz.sha.hexdigest())
    return facts
