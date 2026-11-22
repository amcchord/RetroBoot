#!/usr/bin/env python3
"""Check the a72dvfs DTB against the vendor A72 tables (build-kernel.sh hook).

Usage: check-dtb.py DTB (reads the flattened tree itself; dtc text guesses types)

Independent copy of the vendor numbers (gemian/gemini-linux-kernel-3.18
mt_cpufreq.c @ 8cfe659: CPU_DVFS_FREQ*_B_SB0119 / CPU_DVFS_VOLT*_VPROC2_SB0119,
TT override in _mt_cpufreq_init): every enabled A72 OPP must be one of these
with exactly this voltage and opp-supported-hw; nothing above 1.2 V anywhere on
VPROC2 or VSRAM_B; OPPs up to 1495 MHz at the proven 1.000 V.
"""
import struct
import sys

MAX_UV = 1200000
# MHz: (uV, opp-supported-hw); BIT0 SB 0119, BIT1 SB 0119 TT, BIT2 other bins
EXPECTED = {
    845: (1000000, 0x7), 1001: (1000000, 0x7), 1131: (1000000, 0x7),
    1378: (1000000, 0x7), 1495: (1000000, 0x7),
    1677: (1040000, 0x3), 1885: (1080000, 0x3), 2093: (1110000, 0x3),
    2158: (1130000, 0x3), 2223: (1140000, 0x3), 2262: (1150000, 0x3),
    2327: (1160000, 0x3),
    2392: (1170000, 0x1), 2522: (1200000, 0x1),
    2431: (1180000, 0x2), 2587: (1200000, 0x2),
}
# vendor sign-off voltages (mV) of the SB 0119 table, for the record
VENDOR_SB0119 = {2522: 1200, 2392: 1170, 2327: 1160, 2262: 1150, 2223: 1140, 2158: 1130,
                 2093: 1110, 1885: 1080, 1677: 1040, 1495: 1000, 1378: 980, 1131: 930,
                 1001: 900, 845: 880, 676: 850, 338: 800}
VENDOR_TT = {2587: 1200, 2431: 1180}


def parse(blob):
    """FDT binary -> nested dict {"props": {name: bytes}, "nodes": {name: ...}}."""
    magic, total, off_struct, off_strings = struct.unpack(">4I", blob[:16])
    if magic != 0xd00dfeed:
        sys.exit("not an FDT")
    strings = blob[off_strings:]
    pos = off_struct
    stack = []
    root = None
    while True:
        (tok,) = struct.unpack(">I", blob[pos:pos + 4])
        pos += 4
        if tok == 1:
            end = blob.index(b"\0", pos)
            name = blob[pos:end].decode() or "/"
            pos = (end + 4) & ~3
            node = {"props": {}, "nodes": {}}
            if stack:
                stack[-1]["nodes"][name] = node
            else:
                root = node
            stack.append(node)
        elif tok == 2:
            stack.pop()
        elif tok == 3:
            ln, nameoff = struct.unpack(">II", blob[pos:pos + 8])
            pos += 8
            pname = strings[nameoff:strings.index(b"\0", nameoff)].decode()
            stack[-1]["props"][pname] = blob[pos:pos + ln]
            pos = (pos + ln + 3) & ~3
        elif tok == 4:
            continue
        elif tok == 9:
            return root
        else:
            sys.exit(f"bad FDT token {tok}")


def u32s(b):
    return list(struct.unpack(f">{len(b) // 4}I", b)) if b and len(b) % 4 == 0 else []


def strs(b):
    return [x.decode() for x in b.rstrip(b"\0").split(b"\0")] if b else []


def walk(node, path="/"):
    yield path, node
    for name, child in node["nodes"].items():
        yield from walk(child, path.rstrip("/") + "/" + name)


def main():
    root = parse(open(sys.argv[1], "rb").read())
    errors = []
    nodes = dict(walk(root))

    table = next((n for p, n in nodes.items() if p.endswith("/opp-table-cluster2")), None)
    if not table:
        sys.exit("no /opp-table-cluster2")
    seen = {}
    for name, opp in table["nodes"].items():
        p = opp["props"]
        hz = u32s(p.get("opp-hz", b""))
        hz = (hz[0] << 32 | hz[1]) if len(hz) == 2 else (hz[0] if hz else 0)
        mhz = hz // 1000000
        uv = (u32s(p.get("opp-microvolt", b"")) or [0])[0]
        status = (strs(p.get("status", b"okay")) or ["okay"])[0]
        hw = u32s(p.get("opp-supported-hw", b""))
        if uv > MAX_UV:
            errors.append(f"{name}: {uv} uV above {MAX_UV}")
        if status == "disabled":
            if mhz not in (338, 676):
                errors.append(f"{name}: unexpected disabled OPP")
            continue
        if mhz not in EXPECTED:
            errors.append(f"{name}: {mhz} MHz is not in the vendor table")
            continue
        want_uv, want_hw = EXPECTED[mhz]
        if uv != want_uv or hw != [want_hw]:
            errors.append(f"{name}: {uv} uV hw {hw}, want {want_uv} uV hw [{want_hw:#x}]")
        vendor_mv = VENDOR_TT.get(mhz) if want_hw == 0x2 else VENDOR_SB0119.get(mhz)
        if mhz > 1495 and vendor_mv * 1000 != uv:
            errors.append(f"{name}: {uv} uV != vendor {vendor_mv} mV")
        if mhz <= 1495 and uv < vendor_mv * 1000:
            errors.append(f"{name}: {uv} uV below the vendor {vendor_mv} mV")
        seen[mhz] = (uv, want_hw)
    missing = set(EXPECTED) - set(seen)
    if missing:
        errors.append(f"missing OPPs: {sorted(missing)}")

    vsram = next((n for p, n in nodes.items() if p.endswith("/regulator-vsram-b")), None)
    if not vsram or strs(vsram["props"].get("compatible", b"")) != ["mediatek,mt6797-sramldo-b"]:
        errors.append("no mediatek,mt6797-sramldo-b node")
    else:
        lo = (u32s(vsram["props"].get("regulator-min-microvolt", b"")) or [0])[0]
        hi = (u32s(vsram["props"].get("regulator-max-microvolt", b"")) or [0])[0]
        if (lo, hi) != (1000000, 1200000):
            errors.append(f"vsram_b range {lo}..{hi}, want 1000000..1200000")

    vproc2 = [n for p, n in nodes.items() if strs(n["props"].get("regulator-name", b"")) == ["vproc2"]]
    if len(vproc2) != 1:
        errors.append("vproc2 regulator not found once")
    else:
        hi = (u32s(vproc2[0]["props"].get("regulator-max-microvolt", b"")) or [0])[0]
        if hi > MAX_UV or hi == 0:
            errors.append(f"vproc2 max {hi} uV")
        if "regulator-always-on" in vproc2[0]["props"]:
            errors.append("vproc2 must not be always-on (cl2-power owns its enable)")

    cpu8 = [n for p, n in nodes.items() if "retroboot,boot-max-frequency-khz" in n["props"]]
    if len(cpu8) != 1:
        errors.append("cpu8 with retroboot,boot-max-frequency-khz not found once")
    else:
        pr = cpu8[0]["props"]
        for k in ("proc-supply", "sram-supply", "nvmem-cells"):
            if k not in pr:
                errors.append(f"cpu8: no {k}")
        if u32s(pr["retroboot,boot-max-frequency-khz"]) != [1495000]:
            errors.append("cpu8: boot cap is not 1495000 kHz")
        if u32s(pr.get("mediatek,intermediate-microvolt", b"")) != [1000000]:
            errors.append("cpu8: intermediate floor is not 1000000 uV")
        if strs(pr.get("nvmem-cell-names", b"")) != ["func-code", "date-code"]:
            errors.append(f"cpu8: nvmem-cell-names {strs(pr.get('nvmem-cell-names', b''))}")

    trip = next((n for p, n in nodes.items() if p.endswith("/trip-b-alert")), None)
    if not trip or u32s(trip["props"].get("temperature", b"")) != [80000] or \
       strs(trip["props"].get("type", b"")) != ["passive"]:
        errors.append("80 C passive trip-b-alert missing")
    alert = next((n for p, n in nodes.items() if p.endswith("/trip-alert")), None)
    if not alert or u32s(alert["props"].get("temperature", b"")) != [85000]:
        errors.append("85 C trip-alert changed")

    for mhz in sorted(seen):
        uv, hw = seen[mhz]
        print(f"  A72 OPP {mhz:4d} MHz  {uv / 1000:6.0f} mV  hw {hw:#x}")
    if errors:
        print("a72dvfs DTB check FAILED:\n  " + "\n  ".join(errors))
        sys.exit(1)
    print("a72dvfs DTB check: pass (vendor SB 0119/TT table, <= 1.2 V, boot cap 1495 MHz, 80 C A72 trip)")


if __name__ == "__main__":
    main()
