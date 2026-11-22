"""Resource fork and AppleDouble parsing (read-only)."""
from __future__ import annotations

import struct


class ForkError(Exception):
    pass


def parse_resource_fork(fork: bytes) -> list[tuple[str, int, str, int, bytes]]:
    """(type, id, name, data offset in fork, data) for every resource."""
    if len(fork) < 16:
        return []
    doff, moff, dlen, mlen = struct.unpack(">IIII", fork[:16])
    if moff + mlen > len(fork) or doff + dlen > len(fork) or mlen < 30:
        raise ForkError("resource fork header out of range")
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


APPLEDOUBLE_MAGIC = 0x00051607
APPLESINGLE_MAGIC = 0x00051600


def parse_appledouble(blob: bytes) -> dict[int, bytes]:
    """Entries of an AppleDouble/AppleSingle file: {1: data, 2: rsrc, 9: finder info, ...}."""
    if len(blob) < 26:
        raise ForkError("short AppleDouble header")
    magic, version = struct.unpack(">II", blob[:8])
    if magic not in (APPLEDOUBLE_MAGIC, APPLESINGLE_MAGIC) or version not in (0x00010000, 0x00020000):
        raise ForkError("not an AppleDouble/AppleSingle file")
    n = struct.unpack(">H", blob[24:26])[0]
    out = {}
    for i in range(n):
        eid, off, ln = struct.unpack(">III", blob[26 + 12 * i:38 + 12 * i])
        if off + ln > len(blob):
            raise ForkError(f"AppleDouble entry {eid} out of range")
        out[eid] = blob[off:off + ln]
    return out
