#!/usr/bin/env python3
"""Generate an empty OS X NVRAM seed; contains no Apple code or user data.

Matches mac99 with macio-nvram.osx-partitions=on and output-device=ttya.
The emulator qtest compares it with the actual device before packaging.
Copy it into a Mac's private writable directory; never share one backing
file between running Macs or replace an existing Mac's NVRAM with it.
"""
import argparse
from pathlib import Path
import struct
import zlib


def header(signature, length, name):
    data = bytearray(struct.pack(">BBH12s", signature, 0, length // 16,
                                 name[:11] + b"\0"))
    total = sum(data)
    while total > 255:
        total = (total & 255) + (total >> 8)
    data[1] = total
    return data


def seed():
    data = bytearray(8192)
    data[:16] = header(0x70, 0xc10, b"system")
    variables = b"output-device=ttya\0\0"
    data[16:16 + len(variables)] = variables
    data[0xc10:0xc20] = header(0x7f, 0x3f0, b"free")
    data[0x1000:0x1010] = header(0x5a, 4096, b"wwwwwwwwwwww")
    struct.pack_into(">I", data, 0x1014, 2)
    struct.pack_into(">I", data, 0x1010, zlib.adler32(data[0x1014:], 0))
    return bytes(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_bytes(seed())


if __name__ == "__main__":
    main()
