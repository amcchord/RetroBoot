#!/usr/bin/env python3
"""Test image/gemini/rootfs/usr/lib/retroboot/gemini/rb-gemini-typec with a fake I2C bus.

Usage: python3 -I test-rb-gemini-typec.py [path/to/rb-gemini-typec]

The left port must behave exactly as before (DRP unless SINK or DRP). The right port
must never be set to DRP or SINK: SOURCE on an older kernel, and no I2C access at all
when the rightport kernel driver owns the chip.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    HERE, "../../../rootfs/usr/lib/retroboot/gemini/rb-gemini-typec")

fails = 0


def check(cond, msg):
    global fails
    if not cond:
        fails += 1
        print("FAIL:", msg)


def load():
    loader = importlib.machinery.SourceFileLoader("rb_gemini_typec", TOOL)
    spec = importlib.util.spec_from_loader("rb_gemini_typec", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def run(argv, chips, driver_on_right=False):
    """Run main() against fake chips {bus: {reg: value}}; return (rc, output, writes)."""
    mod = load()
    writes = []
    fds = {}
    with tempfile.TemporaryDirectory() as sysfs:
        mod.SYS_I2C = sysfs
        if driver_on_right:
            dev = os.path.join(sysfs, "0-0025")
            os.makedirs(dev)
            os.makedirs(os.path.join(sysfs, "drivers", "gemini-right-port"))
            os.symlink(os.path.join(sysfs, "drivers", "gemini-right-port"), os.path.join(dev, "driver"))
            for name, val in (("state", "sourcing"), ("vbus", "1"),
                              ("regs", "ok=1 mode=0x01 status=0x19 type=0x10 pad94=1 pad72=1")):
                with open(os.path.join(dev, name), "w") as f:
                    f.write(val + "\n")

        def open_chip(bus):
            if bus not in chips:
                raise OSError("no FUSB301A")
            fd = os.open(os.devnull, os.O_RDONLY)
            fds[fd] = bus
            return fd

        def xfer(fd, rw, reg, value=0):
            regs = chips[fds[fd]]
            if rw == mod.I2C_SMBUS_WRITE:
                writes.append((fds[fd], reg, value))
                regs[reg] = value
                return value
            return regs.get(reg, 0)

        mod.open_chip = open_chip
        mod._xfer = xfer
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mod.main(["rb-gemini-typec"] + argv)
    return rc, out.getvalue(), writes


def chip(mode, status=0, typ=0):
    return {0x01: 0x12, 0x02: mode, 0x11: status, 0x12: typ}


SRC, SNK, DRP = 0x01, 0x04, 0x10

# 1. Older kernel, the state the current image leaves: both in DRP, a charger on the right.
chips = {1: chip(DRP, 0x2f, 0x08), 0: chip(DRP, 0x29, 0x08)}
rc, out, writes = run(["apply"], chips)
check(rc == 0, f"rc {rc}")
check(writes == [(0, 0x02, SRC)], f"writes {writes}")
check("right port (i2c-0): was mode dual-role" in out and "set source" in out, out)
check(chips[1][0x02] == DRP, "left changed")

# 2. Older kernel after Gemian: both SOURCE. Left -> DRP (as before), right stays SOURCE.
chips = {1: chip(SRC), 0: chip(SRC)}
rc, out, writes = run(["apply"], chips)
check(writes == [(1, 0x02, DRP)], f"writes {writes}")
check("left port (i2c-1): was mode source" in out and "set dual-role" in out, out)

# 3. Older kernel, power-on state: both SINK. Left kept, right -> SOURCE.
chips = {1: chip(SNK), 0: chip(SNK)}
rc, out, writes = run(["apply"], chips)
check(writes == [(0, 0x02, SRC)], f"writes {writes}")

# 4. Every left mode: the old rule exactly (DRP unless SINK or DRP).
for m in range(256):
    chips = {1: chip(m), 0: chip(SRC)}
    rc, out, writes = run(["apply"], chips)
    want = [] if m in (SNK, DRP) else [(1, 0x02, DRP)]
    check(writes == want, f"left mode 0x{m:02x}: writes {writes}")

# 5. Every right mode, older kernel: only ever SOURCE written, never DRP or SINK.
for m in range(256):
    chips = {1: chip(DRP), 0: chip(m)}
    rc, out, writes = run(["apply"], chips)
    want = [] if m == SRC else [(0, 0x02, SRC)]
    check(writes == want, f"right mode 0x{m:02x}: writes {writes}")

# 6. Rightport kernel: the right chip is not touched, even in DRP; its state is reported.
chips = {1: chip(SRC), 0: chip(DRP)}
rc, out, writes = run(["apply"], chips, driver_on_right=True)
check(writes == [(1, 0x02, DRP)], f"writes {writes}")
check("right port (i2c-0): managed by the kernel (gemini-right-port): state sourcing; vbus 1" in out, out)
check(rc == 0, f"rc {rc}")

# 7. status never writes.
for drv in (False, True):
    chips = {1: chip(SRC), 0: chip(DRP)}
    rc, out, writes = run(["status"], chips, driver_on_right=drv)
    check(writes == [], f"status wrote {writes}")

# 8. No chips at all: rc 1 (as before); right owned by the kernel counts as found.
rc, out, writes = run(["apply"], {})
check(rc == 1, f"no chips rc {rc}")
rc, out, writes = run(["apply"], {}, driver_on_right=True)
check(rc == 0 and writes == [], f"kernel-owned right, no left: rc {rc} writes {writes}")

print(f"rb-gemini-typec: {'FAIL' if fails else 'ok'} ({fails} failures)")
sys.exit(1 if fails else 0)
