#!/usr/bin/env python3
"""Is RetroBoot Idle active in a running 68k Mac?  (lab check, no guest UI)

    rbidle_check.py --qmp RUN/qmp.sock [--ram-mb 128] [--json]
    rbidle_check.py --dump RAM.bin [--json]

With --qmp it saves the guest's RAM with QMP ``pmemsave`` (physical address
0; Mac OS on a 68040 with virtual memory off maps RAM one to one) and reads
it; with --dump it reads an existing dump.  It finds RetroBoot Idle's info
block by its magic ("RBidIdle", guest/rbidle/rbidle.c), checks the layout,
and verifies that every recorded site holds ``jsr <handler>.l ; nop``
(4EB9 hhhh hhhh 4E71) pointing at the block's handler.  It also counts
unpatched signature sites (Basilisk II's SynchIdleTime signature) so a Mac
without RetroBoot Idle reports where it would patch.

Exit status 0 when RetroBoot Idle is active with every site verified, 1
otherwise.  The counters (idle calls, STOPs) grow while the guest idles;
take two readings to see it working.
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

MAGIC = b"RBidIdle"
HANDLER_OFFSET = 48
SITE = bytes.fromhex("207802b641e80080")       # movea.l $2B6.w,a0 ; lea $80(a0),a0
POWER = bytes.fromhex("7003a09f")              # moveq #3,d0 ; _PowerDispatch
SEARCH_BACK = 0x80


def parse_block(ram: bytes, at: int) -> dict | None:
    b = ram[at:at + HANDLER_OFFSET]
    if len(b) < HANDLER_OFFSET or b[:8] != MAGIC:
        return None
    layout, nsites, calls, stops, tick, run, min_run = struct.unpack(">HHIIIHH", b[8:28])
    sites = list(struct.unpack(">4I", b[28:44]))
    version = struct.unpack(">I", b[44:48])[0]
    if layout != 1 or not 0 < nsites <= 4:
        return None
    return {"address": at, "layout": layout, "sites_patched": nsites, "idle_calls": calls, "stops": stops,
            "min_run": min_run, "version": f"{version >> 8 & 0xFF}.{version >> 4 & 0xF}",
            "sites": sites[:nsites]}


def signature_sites(ram: bytes) -> list[int]:
    found = set()
    i = ram.find(POWER)
    while i != -1:
        if i % 2 == 0:
            lo = max(0, i - SEARCH_BACK)
            j = ram.find(SITE, lo, i)
            while j != -1:
                if j % 2 == 0:
                    found.add(j)
                j = ram.find(SITE, j + 2, i)
        i = ram.find(POWER, i + 2)
    return sorted(found)


def check(ram: bytes) -> dict:
    blocks = []
    i = ram.find(MAGIC)
    while i != -1:
        blk = parse_block(ram, i)
        if blk:
            blocks.append(blk)
        i = ram.find(MAGIC, i + 2)
    report = {"ram_bytes": len(ram), "unpatched_signature_sites": [hex(s) for s in signature_sites(ram)]}
    # The INIT resource itself (purged after startup, or still in RAM) carries
    # the template block with no sites; only filled-in blocks count.
    if not blocks:
        report.update(active=False, reason="no RetroBoot Idle info block in RAM")
        return report
    if len(blocks) > 1:
        report["note"] = f"{len(blocks)} info blocks found; using the last"
    blk = blocks[-1]
    handler = blk["address"] + HANDLER_OFFSET
    want = b"\x4e\xb9" + struct.pack(">I", handler) + b"\x4e\x71"
    verified = []
    for s in blk["sites"]:
        ok = ram[s:s + 8] == want
        verified.append({"site": hex(s), "patched": ok, "bytes": ram[s:s + 8].hex()})
    report.update(active=all(v["patched"] for v in verified), block=hex(blk["address"]),
                  handler=hex(handler), version=blk["version"], min_run=blk["min_run"],
                  sites=verified, idle_calls=blk["idle_calls"], stops=blk["stops"])
    return report


def dump_via_qmp(sock: str, ram_mb: int) -> bytes:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "apps"))
    from rbapps import guestio
    q = guestio.QMP(sock)
    try:
        with tempfile.TemporaryDirectory(prefix="rbidle-") as d:
            path = os.path.join(d, "ram.bin")
            q.cmd("pmemsave", val=0, size=ram_mb << 20, filename=path, timeout=120)
            return Path(path).read_bytes()
    finally:
        q.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--qmp", help="QMP socket of the running guest")
    src.add_argument("--dump", help="raw RAM dump (pmemsave from address 0)")
    ap.add_argument("--ram-mb", type=int, default=128)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    ram = Path(a.dump).read_bytes() if a.dump else dump_via_qmp(a.qmp, a.ram_mb)
    report = check(ram)
    if a.json:
        print(json.dumps(report, indent=1))
    else:
        if report["active"]:
            print(f"RetroBoot Idle {report['version']} active: block {report['block']}, "
                  f"{len(report['sites'])} site(s) {', '.join(s['site'] for s in report['sites'])}, "
                  f"min_run {report['min_run']}, idle calls {report['idle_calls']}, STOPs {report['stops']}")
        else:
            print("RetroBoot Idle not active:", report.get("reason", report.get("sites")))
        if report["unpatched_signature_sites"]:
            print("unpatched signature sites:", ", ".join(report["unpatched_signature_sites"]))
    return 0 if report["active"] else 1


if __name__ == "__main__":
    sys.exit(main())
