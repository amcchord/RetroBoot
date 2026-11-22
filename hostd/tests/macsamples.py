"""Small classic Mac test files made from scratch (no Apple software, no third-party media).

``make_sit`` writes a StuffIt 1.5 ("SIT!") archive with stored (uncompressed)
forks, which The Unarchiver and StuffIt Expander both read.  The other
encoders live in retroboot.macfiles (MacBinary III, BinHex 4.0, AppleSingle);
web/reports/webfiles-2026-10-11.md records that unar decodes all of them to
the same forks.
"""
from __future__ import annotations

import struct

from retroboot import macfiles as mf


def crc16_arc(data: bytes, crc: int = 0) -> int:
    """CRC-16/ARC (poly 0xA001 reflected), which StuffIt 1.5 uses."""
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _sit_header(name: bytes, rmethod: int, dmethod: int, type_: bytes = bytes(4), creator: bytes = bytes(4),
                flags: int = 0, rsrc: bytes = b"", data: bytes = b"") -> bytes:
    h = bytearray(112)
    h[0], h[1], h[2] = rmethod, dmethod, len(name)
    h[3:3 + len(name)] = name
    h[66:70], h[70:74] = type_, creator
    h[74:76] = struct.pack(">H", flags)
    h[76:84] = struct.pack(">II", 0xB5E3F000, 0xB5E3F000)        # 1996
    h[84:100] = struct.pack(">IIII", len(rsrc), len(data), len(rsrc), len(data))
    h[100:104] = struct.pack(">HH", crc16_arc(rsrc), crc16_arc(data))
    h[110:112] = struct.pack(">H", crc16_arc(bytes(h[:110])))
    return bytes(h)


def make_sit(entries: list) -> bytes:
    """entries: ("file", name, type, creator, flags, data, rsrc) or ("folder", name, [entries])."""
    def body(items):
        out = b""
        for e in items:
            if e[0] == "folder":
                out += _sit_header(e[1], 32, 32) + body(e[2]) + _sit_header(e[1], 33, 33)
            else:
                _, name, t, c, fl, data, rsrc = e
                out += _sit_header(name, 0, 0, t, c, fl, rsrc, data) + rsrc + data
        return out
    b = body(entries)
    total = 22 + len(b)
    return b"SIT!" + struct.pack(">HI", len(entries), total) + b"rLau" + bytes([2]) + bytes(7) + b


def sample_fork() -> bytes:
    """A resource fork with an 'ICN#' (a square), a named STR and a styl, all original.
    Built in map order, so it is the canonical layout classicvirtio's Rez() rebuilds."""
    icon = bytes([0xFF] * 4 + [0x80, 0, 0, 0x01] * 30 + [0xFF] * 4) * 2
    return mf.build_fork([
        mf.Resource(b"ICN#", 128, None, 0, icon),
        mf.Resource(b"STR ", 128, b"\xa9 RetroBoot \"test\"", 0x20, b"\x0eHello from web"),
        mf.Resource(b"styl", 128, None, 0, bytes.fromhex("0001000000000010000c00030000000c000000000000"))])
