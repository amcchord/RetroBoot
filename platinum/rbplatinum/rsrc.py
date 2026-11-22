"""Resource fork parsing (Inside Macintosh: More Macintosh Toolbox, 1-121)."""
from __future__ import annotations

import struct
from dataclasses import dataclass

COMPRESSED_TAG = b"\xa8\x9f\x65\x72"


@dataclass
class Resource:
    type: str
    id: int
    name: str
    attrs: int
    data: bytes

    @property
    def compressed(self) -> bool:
        return self.data[:4] == COMPRESSED_TAG


class ResourceFile:
    """All resources of one resource fork, by (type, id)."""

    def __init__(self, fork: bytes):
        self.res: dict[tuple[str, int], Resource] = {}
        if len(fork) < 16:
            return
        doff, moff, dlen, mlen = struct.unpack(">IIII", fork[:16])
        if moff + mlen > len(fork) or doff + dlen > len(fork):
            raise ValueError("resource fork header out of range")
        rmap = fork[moff:moff + mlen]
        tl_off, nl_off = struct.unpack(">HH", rmap[24:28])
        tlist = rmap[tl_off:]
        nlist = rmap[nl_off:]
        ntypes = (struct.unpack(">H", tlist[:2])[0] + 1) & 0xFFFF
        for i in range(ntypes):
            rtype, count, ref = struct.unpack(">4sHH", tlist[2 + 8 * i:10 + 8 * i])
            t = rtype.decode("mac_roman")
            for j in range(count + 1):
                rid, noff, attr_off = struct.unpack(">hHI", tlist[ref + 12 * j:ref + 12 * j + 8])
                data_off = doff + (attr_off & 0xFFFFFF)
                length = struct.unpack(">I", fork[data_off:data_off + 4])[0]
                name = ""
                if noff != 0xFFFF:
                    name = nlist[noff + 1:noff + 1 + nlist[noff]].decode("mac_roman")
                self.res[(t, rid)] = Resource(t, rid, name, attr_off >> 24, fork[data_off + 4:data_off + 4 + length])

    def get(self, rtype: str, rid: int) -> Resource | None:
        return self.res.get((rtype, rid))

    def data(self, rtype: str, rid: int) -> bytes | None:
        r = self.res.get((rtype, rid))
        return None if r is None or r.compressed else r.data

    def of_type(self, rtype: str) -> list[Resource]:
        return sorted((r for (t, _), r in self.res.items() if t == rtype), key=lambda r: r.id)

    def named(self, rtype: str, name: str) -> Resource | None:
        for r in self.of_type(rtype):
            if r.name == name:
                return r
        return None

    def types(self) -> list[str]:
        return sorted({t for t, _ in self.res})


def pstring(b: bytes, off: int = 0) -> str:
    return b[off + 1:off + 1 + b[off]].decode("mac_roman")
