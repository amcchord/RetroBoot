#!/usr/bin/env python3
"""Extractor for Apple "Installation Tome" archives (Mac OS 9.x Mac OS Install era).

Pure Python 3, standard library only.  Written from a clean-room analysis of the
tome bytes against known plaintext; no Apple code or third-party tools were used.

Usage:
    tome.py TOMEDATA list
    tome.py TOMEDATA extract "Name" OUTDIR      -> OUTDIR/Name.data, OUTDIR/Name.rsrc
    tome.py TOMEDATA extract-all OUTDIR

TOMEDATA is the tome's data fork; its resource fork is not needed.  A '/' in a
Mac file name is written as ':' in the output file name.

FORMAT (as determined; all integers big-endian)
==============================================

File header, 0x24 bytes:
  +00 'kc'   +02 u16 1   +04 u32 16   +08 u32 2042   +0C u32 32768 (LZ window)
  +10 u32 76872   +14 u16 7478   +16 u16 0   +18 u16 directory offset (0x24)
  +1A u16 entry count (552 here)   +1C u16 0   +1E u16 count again   +20 u32 0
  (Only the directory offset and count are needed.)

Directory: <count> entries of 0x80 bytes each:
  +00 u16 5 (entry kind)       +02 u32 1-based index
  +06 Str31 name (Mac Roman)   +26 file type   +2A creator
  +2E creation date            +32 modification date (Mac epoch seconds)
  +36 u16 unknown              +38 u16 unknown (usually 0x8000)
  +3A u16 probably Finder flags (0x2000 on extensions, 0x0400 on custom-icon files)
  +3C data-fork descriptor     +4C resource-fork descriptor     +5C..+7F zero
  A fork descriptor is four u32 values: uncompressed length, absolute offset of
  the compressed stream, compressed length, and a 32-bit check value whose
  algorithm is unknown (not CRC-32/Adler/simple sums).  Length 0 = empty fork.

Compressed fork stream: a sequence of blocks.  Every block starts on a byte
boundary with a 4-byte header and ends once the output length reaches the next
multiple of 65536 (or the fork length).  A match may run past that boundary;
the next block then starts wherever the output actually is.  After a block the
bit reader skips to the next byte boundary.  The LZ window (32 KB) and the
output position p (counted from the start of the fork) carry across blocks.

  01 xx 00 00  stored block: raw bytes up to the block limit.
  00 01 00 00  LZ "method 1" (general data, 8-bit literals).
  00 00 00 00  LZ "method 0" (7-bit text: literals are 7 bits, different
               length code and distance-parameter schedule).

LZ bitstream (MSB-first).  The decoder has two states: "after match"
(also the state at block start and after a literal run of 63) and "after
literals".  In each step it reads a length symbol m with a static prefix code:

  method 1:  0b -> 1+b;  100 -> 3;  101b -> 4+b;  1100b -> 6+b;  1101bb -> 8+bb;
             1110bbb -> 12+bbb;  11110bbb -> 20+bbb;  11111 -> escape (base 28)
  method 0:  0bb -> 1+bb;  10bb -> 5+bb;  110bb -> 9+bb;  1110bbb -> 13+bbb;
             11110bbbb -> 21+bbbb;  11111 -> escape (base 37)
  escape:    j = count of 1 bits (at most 5; a 0 terminates j<5),
             then (5+j) bits v;  m = base + (2**(j+5) - 32) + v

  * In the "after match" state m==1 introduces a literal run: count n is
    k = up to five 1 bits (0-terminated if k<5), then k bits r, n = 2**k + r
    (1..63).  n literal bytes follow (8 or 7 bits each, unaligned).  The
    state becomes "after literals", or stays "after match" if n == 63.
  * Otherwise it is a match: length = m+1 in the "after match" state,
    m+2 in the "after literals" state (minimum match length 3), followed by a
    distance code, and the state becomes "after match".

Distance code (d = 1 is the previous byte; copies may overlap), with a width
parameter w that depends only on p and the method:
    0 + w bits v           -> d = v + 1                       (d <= 2**w)
    10 + (w+2) bits v      -> d = 2**w + v + 1                (up to 5*2**w)
    11 + nb bits v         -> d = 5*2**w + v + 1
  nb = bit_length(min(p, 32768) - 5*2**w - 1), except that in method 1
  with w == 7 nb is 11 for p >= 1645.
  w = number of thresholds t in the method's table with t < p:
    method 1: 10 20 40 80 160 672 1000 2688 5376 10752 21504  (w max 11)
    method 0: 10 20 40 80 160 320 832 1280 2560 5120 10240    (w max 11)
  Most method-0 values are 5*2**k ("far class non-empty").  Most method-1
  values from w=6 on are 21*2**(w-1).  The 1000, 832 and 1645 values break
  those patterns.  They were measured, not derived: each was the only value
  that let all 942 non-empty forks in the 9.2.1 tome decode to their exact
  length and exact compressed size.

Verification performed: all 942 non-empty forks of the 9.2.1 tome decode to the
recorded uncompressed length while consuming exactly the recorded compressed
length.  That covers 874 method-1-only forks, 40 method-0-only forks (LaserWriter
PPDs, Sherlock .cnl/.src, Help .htm, StopTypes), 4 forks that mix both methods
(CarbonLib data, Internet rsrc, Sound rsrc, UDF Volume Access data), and 24 forks
that use stored blocks.  The five supplied plaintext forks are byte-identical:
ATI Driver Update data, NVIDIA Driver data, OpenGLEngine data, and
OpenGLRendererATI data and rsrc.  The method-0 outputs are clean ASCII text.
They have no plaintext reference, so only exact length and compressed-size
consumption check them.  extract() does not use the per-fork check value.
"""
import os
import struct
import sys

__all__ = ['Tome', 'decompress', 'DecodeError']


class DecodeError(Exception):
    pass


WINDOW = 32768
_T1 = (10, 20, 40, 80, 160, 672, 1000, 2688, 5376, 10752, 21504)
_T0 = (10, 20, 40, 80, 160, 320, 832, 1280, 2560, 5120, 10240)


def _wtable(thresholds, size):
    """Precompute w for positions 0..size (positions beyond use the max)."""
    tab = bytearray(size + 1)
    w = 0
    for p in range(size + 1):
        while w < len(thresholds) and thresholds[w] < p:
            w += 1
        tab[p] = w
    return tab


_WMAX = 65536
_W1 = _wtable(_T1, _WMAX)
_W0 = _wtable(_T0, _WMAX)


def decompress(comp, size):
    """Decompress one fork stream `comp` (bytes) to exactly `size` bytes.

    Returns (data, consumed_bytes)."""
    C = comp
    L = len(C)
    pos = 0
    acc = 0
    nbits = 0
    out = bytearray()

    def get(k):
        nonlocal pos, acc, nbits
        while nbits < k:
            if pos >= L:
                if pos >= L + 8:
                    raise DecodeError('compressed stream overrun')
                acc = acc << 8
            else:
                acc = (acc << 8) | C[pos]
            pos += 1
            nbits += 8
        nbits -= k
        v = acc >> nbits
        acc &= (1 << nbits) - 1
        return v

    block = 0
    while len(out) < size:
        # byte-align, then 4-byte block header
        nbits -= nbits & 7
        acc &= (1 << nbits) - 1
        hdr = get(32)
        block += 1
        limit = min(size, 65536 * block)
        if hdr >> 24 == 1:                      # stored
            while len(out) < limit:
                out.append(get(8))
            continue
        if hdr == 0x00010000:
            method = 1
            wtab = _W1
            litbits = 8
        elif hdr == 0x00000000:
            method = 0
            wtab = _W0
            litbits = 7
        else:
            raise DecodeError('unknown block header %08x at output %d' % (hdr, len(out)))
        after_match = True
        while len(out) < limit:
            p = len(out)
            # ---- length / command symbol ----
            if method:
                if get(1) == 0:
                    m = 1 + get(1)
                elif get(1) == 0:
                    m = 3 if get(1) == 0 else 4 + get(1)
                elif get(1) == 0:
                    m = 6 + get(1) if get(1) == 0 else 8 + get(2)
                elif get(1) == 0:
                    m = 12 + get(3)
                elif get(1) == 0:
                    m = 20 + get(3)
                else:
                    j = 0
                    while j < 5 and get(1):
                        j += 1
                    m = 28 + (1 << (j + 5)) - 32 + get(5 + j)
            else:
                if get(1) == 0:
                    m = 1 + get(2)
                elif get(1) == 0:
                    m = 5 + get(2)
                elif get(1) == 0:
                    m = 9 + get(2)
                elif get(1) == 0:
                    m = 13 + get(3)
                elif get(1) == 0:
                    m = 21 + get(4)
                else:
                    j = 0
                    while j < 5 and get(1):
                        j += 1
                    m = 37 + (1 << (j + 5)) - 32 + get(5 + j)
            if after_match and m == 1:
                # ---- literal run ----
                k = 0
                while k < 5 and get(1):
                    k += 1
                n = (1 << k) + get(k)
                for _ in range(n):
                    out.append(get(litbits))
                after_match = (n == 63)
                continue
            # ---- match ----
            length = m + 1 if after_match else m + 2
            w = wtab[p] if p <= _WMAX else wtab[_WMAX]
            if get(1) == 0:
                d = get(w) + 1
            elif get(1) == 0:
                d = (1 << w) + get(w + 2) + 1
            else:
                base = 5 << w
                nb = (min(p, WINDOW) - base - 1).bit_length()
                if method == 1 and w == 7 and p >= 1645:
                    nb = 11
                d = base + get(nb) + 1
            if d > p or d > WINDOW:
                raise DecodeError('bad distance %d at output %d' % (d, p))
            if d >= length:
                s = p - d
                out += out[s:s + length]
            else:
                for _ in range(length):
                    out.append(out[-d])
            after_match = True
    consumed = pos - (nbits >> 3)
    return bytes(out), consumed


class Entry:
    __slots__ = ('index', 'name', 'ftype', 'creator', 'crdate', 'mddate',
                 'flags', 'finder_flags', 'data', 'rsrc', 'raw')

    def __repr__(self):
        return 'Entry(%d, %r)' % (self.index, self.name)


class Tome:
    def __init__(self, path):
        """path: the tome's data fork, as a file path or as bytes."""
        if isinstance(path, (bytes, bytearray)):
            self.buf = bytes(path)
        else:
            with open(path, 'rb') as f:
                self.buf = f.read()
        b = self.buf
        if b[:2] != b'kc':
            raise ValueError('not an Installation Tome (missing "kc" magic)')
        diroff, count = struct.unpack('>HH', b[0x18:0x1C])
        self.entries = []
        for i in range(count):
            e = b[diroff + i * 0x80: diroff + (i + 1) * 0x80]
            ent = Entry()
            ent.raw = e
            _, ent.index = struct.unpack('>HI', e[0:6])
            ent.name = e[7:7 + min(e[6], 31)].decode('mac_roman')
            ent.ftype = e[0x26:0x2A]
            ent.creator = e[0x2A:0x2E]
            ent.crdate, ent.mddate = struct.unpack('>II', e[0x2E:0x36])
            ent.flags, ent.finder_flags = struct.unpack('>HH', e[0x38:0x3C])
            ent.data = struct.unpack('>4I', e[0x3C:0x4C])
            ent.rsrc = struct.unpack('>4I', e[0x4C:0x5C])
            self.entries.append(ent)

    def find(self, name):
        for e in self.entries:
            if e.name == name:
                return e
        low = name.lower()
        hits = [e for e in self.entries if e.name.lower() == low]
        if len(hits) == 1:
            return hits[0]
        raise KeyError(name)

    def fork(self, entry, which):
        size, off, clen, _check = entry.data if which == 'data' else entry.rsrc
        if size == 0:
            return b''
        out, used = decompress(self.buf[off:off + clen], size)
        if len(out) != size or used != clen:
            raise DecodeError('%s %s fork: produced %d/%d bytes, consumed %d/%d'
                              % (entry.name, which, len(out), size, used, clen))
        return out


def _fname(name):
    return name.replace('/', ':').replace('\0', '_')


def _extract(t, e, outdir):
    os.makedirs(outdir, exist_ok=True)
    base = os.path.join(outdir, _fname(e.name))
    for which in ('data', 'rsrc'):
        with open(base + '.' + which, 'wb') as f:
            f.write(t.fork(e, which))
    return base


def main(argv):
    if len(argv) < 3:
        sys.stderr.write(__doc__.split('FORMAT')[0])
        return 2
    t = Tome(argv[1])
    cmd = argv[2]
    if cmd == 'list':
        for e in t.entries:
            print('%3d  %-4s %-4s  data %9d  rsrc %9d  %s' % (
                e.index, e.ftype.decode('mac_roman'), e.creator.decode('mac_roman'),
                e.data[0], e.rsrc[0], e.name))
        return 0
    if cmd == 'extract' and len(argv) == 5:
        try:
            e = t.find(argv[3])
        except KeyError:
            sys.stderr.write('no entry named %r\n' % argv[3])
            return 1
        base = _extract(t, e, argv[4])
        print('%s.data (%d bytes), %s.rsrc (%d bytes)  type %r creator %r'
              % (base, e.data[0], base, e.rsrc[0],
                 e.ftype.decode('mac_roman'), e.creator.decode('mac_roman')))
        return 0
    if cmd == 'extract-all' and len(argv) == 4:
        bad = 0
        for e in t.entries:
            try:
                _extract(t, e, argv[3])
            except DecodeError as ex:
                bad += 1
                sys.stderr.write('FAILED %s: %s\n' % (e.name, ex))
        print('extracted %d entries, %d failures' % (len(t.entries), bad))
        return 1 if bad else 0
    sys.stderr.write(__doc__.split('FORMAT')[0])
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
