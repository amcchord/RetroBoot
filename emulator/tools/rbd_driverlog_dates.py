#!/usr/bin/env python3
"""Dates in a classicvirtio driver log (rbd_share.py up --driver-log).

    rbd_driverlog_dates.py DRIVER.LOG

Prints JSON: the last Mac date (seconds since 1904, local) the driver reported
for each name (GetCatInfo results), the modification dates Mac OS set
(SetCatInfo inputs) and the driver's "UTC offset" lines.  Lab tool for
emulator/reports/sharefix2-20261012.md; Python standard library only.
"""
import re, sys, json
cur = None; out = {}; sets = []
for raw in open(sys.argv[1], "rb").read().decode("latin-1").splitlines():
    line = re.sub(r"^(\[[^\]]*\])?9P\(-?\d+\) ", "", raw)
    m = re.match(r"FS_(\w+)\(", line)
    if m:
        cur = {"call": m.group(1)}
        continue
    if cur is None:
        continue
    m = re.match(r"\s*(->|<-)\s+(\w+)\s+(.*)$", line)
    if not m:
        continue
    d, k, v = m.groups()
    cur[d + k] = v.strip()
    if cur["call"] == "GetCatInfo" and d == "<-" and k in ("ioFlMdDat", "ioDrMdDat"):
        nm = re.search(r"\"(.*)\"", cur.get("<-ioNamePtr", "") or cur.get("->ioNamePtr", ""))
        if nm:
            out[nm.group(1)] = int(v.split()[0], 16)
    if cur["call"] == "SetCatInfo" and d == "->" and k in ("ioFlMdDat", "ioDrMdDat"):
        nm = re.search(r"\"(.*)\"", cur.get("->ioNamePtr", ""))
        sets.append({"name": nm.group(1) if nm else None, "mdat": int(v.split()[0], 16)})
off = [l for l in open(sys.argv[1], "rb").read().decode("latin-1").splitlines() if "UTC offset" in l]
print(json.dumps({"dates": out, "sets": sets[-12:], "offset_lines": off[-3:]}))
