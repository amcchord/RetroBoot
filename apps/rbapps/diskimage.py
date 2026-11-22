"""Classic Mac disk image containers -> raw volume bytes.

* Disk Copy 4.2 (type 'dImg', creator 'dCpy'): 84-byte header, then the
  volume and optional tag bytes; the data checksum is verified.
* NDIF / Disk Copy 6 (type 'dimg'/'rohd', creator 'ddsk'): the data fork holds
  chunks described by the resource fork's 'bcem' resource; raw (2), zero
  (0, 1) and ADC-compressed (0x83) chunks are supported.
* Raw images: a bare HFS/HFS+ volume or a partitioned (Apple Partition Map)
  disk/CD image are passed through unchanged.
"""
from __future__ import annotations

import struct

from .resfork import parse_resource_fork

DC42_HEADER = 84
NDIF_TYPES = {(b"dimg", b"ddsk"), (b"rohd", b"ddsk"), (b"dimg", b"dCpy"), (b"rohd", b"dCpy")}


class ImageError(Exception):
    pass


def dc42_checksum(data: bytes) -> int:
    s = 0
    for (word,) in struct.iter_unpack(">H", data[:len(data) & ~1]):
        s = (s + word) & 0xFFFFFFFF
        s = ((s >> 1) | ((s & 1) << 31)) & 0xFFFFFFFF
    return s


def is_dc42(data: bytes) -> bool:
    if len(data) < DC42_HEADER or data[82:84] != b"\x01\x00" or not 0 < data[0] <= 63:
        return False
    size, tags = struct.unpack(">II", data[64:72])
    return size > 0 and size % 512 == 0 and DC42_HEADER + size + tags == len(data)


def unwrap_dc42(data: bytes) -> tuple[bytes, dict]:
    if not is_dc42(data):
        raise ImageError("not a Disk Copy 4.2 image")
    name = data[1:1 + data[0]].decode("mac_roman")
    size, tags, dsum, _tsum = struct.unpack(">IIII", data[64:80])
    vol = data[DC42_HEADER:DC42_HEADER + size]
    got = dc42_checksum(vol)
    if got != dsum:
        raise ImageError(f"Disk Copy 4.2 data checksum mismatch ({got:08x} != {dsum:08x})")
    return vol, {"container": "dc42", "image_name": name, "bytes": size, "checksum": f"{dsum:08x}",
                 "format_byte": data[81]}


def adc_decompress(src: bytes, expect: int) -> bytes:
    """Apple Data Compression (NDIF chunk type 0x83)."""
    out = bytearray()
    i = 0
    n = len(src)
    while i < n and len(out) < expect:
        b = src[i]
        if b & 0x80:                      # literal run
            ln = (b & 0x7F) + 1
            out += src[i + 1:i + 1 + ln]
            i += 1 + ln
            continue
        if b & 0x40:                      # three-byte match
            ln = (b & 0x3F) + 4
            off = (src[i + 1] << 8) | src[i + 2]
            i += 3
        else:                             # two-byte match
            ln = ((b & 0x3C) >> 2) + 3
            off = ((b & 0x03) << 8) | src[i + 1]
            i += 2
        start = len(out) - off - 1
        if start < 0:
            raise ImageError("ADC back-reference before the start of the chunk")
        for k in range(ln):
            out.append(out[start + k])
    if len(out) != expect:
        raise ImageError(f"ADC chunk decoded to {len(out)} bytes, expected {expect}")
    return bytes(out)


def unwrap_ndif(data: bytes, rsrc: bytes) -> tuple[bytes, dict]:
    bcem = [d for t, _i, _n, _p, d in parse_resource_fork(rsrc) if t == "bcem"]
    if not bcem:
        raise ImageError("NDIF image without a 'bcem' resource")
    b = bcem[0]
    if len(b) < 128:
        raise ImageError("short 'bcem' resource")
    name = b[5:5 + b[4]].decode("mac_roman")
    sectors = struct.unpack(">I", b[68:72])[0]
    nchunks = struct.unpack(">I", b[124:128])[0]
    if 128 + 12 * nchunks > len(b):
        raise ImageError("'bcem' chunk list out of range")
    out = bytearray(sectors * 512)
    kinds = {}
    for i in range(nchunks):
        word, off, ln = struct.unpack(">III", b[128 + 12 * i:140 + 12 * i])
        start, ctype = word >> 8, word & 0xFF
        if ctype == 0xFF:
            break
        nxt = struct.unpack(">I", b[140 + 12 * i:144 + 12 * i])[0] >> 8 if i + 1 < nchunks else sectors
        span = (nxt - start) * 512
        kinds[ctype] = kinds.get(ctype, 0) + 1
        if ctype in (0, 1):
            continue
        chunk = data[off:off + ln]
        if len(chunk) != ln:
            raise ImageError("NDIF chunk beyond the data fork")
        if ctype == 2:
            block = chunk
        elif ctype == 0x83:
            block = adc_decompress(chunk, span)
        else:
            raise ImageError(f"unsupported NDIF chunk type 0x{ctype:02x} (KenCode or newer)")
        if start * 512 + len(block) > len(out):
            raise ImageError("NDIF chunk beyond the volume")
        out[start * 512:start * 512 + len(block)] = block
    return bytes(out), {"container": "ndif", "image_name": name, "bytes": len(out),
                        "chunk_types": {f"0x{k:02x}": v for k, v in sorted(kinds.items())}}


def unwrap(data: bytes, rsrc: bytes = b"", type_creator: tuple[bytes, bytes] | None = None) -> tuple[bytes, dict]:
    """Return (raw volume or partitioned disk bytes, info) for any supported image."""
    if is_dc42(data):
        return unwrap_dc42(data)
    if rsrc and (type_creator in NDIF_TYPES or b"bcem" in rsrc):
        try:
            return unwrap_ndif(data, rsrc)
        except ImageError:
            if type_creator in NDIF_TYPES:
                raise
    if len(data) >= 1536 and (data[1024:1026] in (b"BD", b"H+", b"HX") or data[:2] == b"ER"):
        return data, {"container": "raw", "bytes": len(data)}
    raise ImageError("not a recognized disk image (Disk Copy 4.2, NDIF, raw HFS/HFS+ or partitioned)")


def looks_like_image(type_creator: tuple[bytes, bytes] | None, name: str, data_head: bytes) -> bool:
    if type_creator in NDIF_TYPES or (type_creator and type_creator[0] == b"dImg"):
        return True
    low = name.lower()
    return low.endswith((".img", ".image", ".dsk", ".hfv", ".toast", ".iso", ".cdr", ".smi")) and \
        (len(data_head) >= 1026 and (data_head[1024:1026] in (b"BD", b"H+") or data_head[:2] == b"ER"
                                     or data_head[82:84] == b"\x01\x00"))
