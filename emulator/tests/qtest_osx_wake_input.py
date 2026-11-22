#!/usr/bin/env python3
"""Check original physical wake events through real PMU and OHCI HID reports."""
import argparse
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import time

spec = importlib.util.spec_from_file_location("pmu_probe", Path(__file__).with_name("qtest_osx_sleep.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def machine(qemu, support, enabled, kind, preserve=False, incoming=False):
    original = subprocess.Popen
    def devices(argv, *args, **kwargs):
        return original(argv + ["-device", "pci-ohci,id=wake-ohci", "-device",
                               "usb-" + kind + ",bus=wake-ohci.0" +
                               (",x-keep-keys-on-reset=on" if preserve else "")] + (["-incoming", "defer"] if incoming else []), *args, **kwargs)
    subprocess.Popen = devices
    try:
        return probe.Probe(qemu, support, enabled)
    finally:
        subprocess.Popen = original


class OHCI:
    """Minimal control list in guest RAM; no host-side HID-state shortcut."""
    def __init__(self, q):
        self.q = q
        slot, = [slot for slot in range(32) if q.cfg(slot, 8) >> 8 == 0x0c0310]
        q.cfg(slot, 0x10, 0x81000000)
        q.cfg(slot, 4, 6)
        self.base = 0x81000000
        assert self.read(self.base) == 0x10
        q.wr(0x10400, bytes(256))
        self.write(self.base + 0x18, 0x10400)  # HCCA, separate from CPU wake vector
        self.write(self.base + 0x54, 0x110)  # port power and USB reset
        time.sleep(.03)
        assert self.read(self.base + 0x54) & 3 == 3

    def reset(self):
        self.write(self.base + 0x54, 0x10)
        time.sleep(.02)

    def write(self, address, value):
        self.q.wr(address, struct.pack("<I", value))

    def read(self, address):
        return struct.unpack("<I", self.q.rd(address, 4))[0]

    def interrupt_report(self):
        # Standard SET_CONFIGURATION, then a periodic endpoint-1 IN TD. Unlike
        # GET_REPORT this must NAK if a migrated pending-state report was lost.
        ed, td, setup = 0x10000, 0x10100, 0x10200
        self.q.wr(setup, bytes.fromhex("0009010000000000"))
        self.q.wr(td, struct.pack("<4I", 0xf2000000, setup, td + 16, setup + 7))
        self.q.wr(td + 16, struct.pack("<4I", 0xf3100000, 0, td + 32, 0))
        self.q.wr(td + 32, bytes(16))
        self.q.wr(ed, struct.pack("<4I", 8 << 16, td + 32, td, 0))
        self.write(self.base + 0x20, ed)
        self.write(self.base + 8, 2)
        self.write(self.base + 4, 0x90)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and self.read(ed + 8) & ~15 != td + 32:
            time.sleep(.005)
        assert all(self.read(td + i * 16) >> 28 == 0 for i in range(2))
        self.write(self.base + 4, 0x80)
        ed, td, data = 0x10500, 0x10600, 0x10700
        self.q.wr(data, bytes(8))
        self.q.wr(td, struct.pack("<4I", 0xf0140000, data, td + 16, data + 7))
        self.q.wr(td + 16, bytes(16))
        self.q.wr(ed, struct.pack("<4I", (8 << 16) | (2 << 11) | (1 << 7), td + 16, td, 0))
        self.q.wr(0x10400, struct.pack("<32I", *([ed] * 32)))
        self.write(self.base + 0x40, 0x2a2f)
        self.write(self.base + 4, 0x84)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and self.read(ed + 8) & ~15 != td + 16:
            time.sleep(.005)
        self.write(self.base + 4, 0x80)
        assert self.read(ed + 8) & ~15 == td + 16, "pending HID interrupt report was lost"
        assert self.read(td) >> 28 == 0
        return self.q.rd(data, 8)

    def report(self):
        ed, td, setup, data = 0x10000, 0x10100, 0x10200, 0x10300
        self.q.wr(setup, bytes.fromhex("a101000100000800"))  # HID GET_REPORT
        self.q.wr(data, b"\xcc" * 8)
        self.q.wr(td, struct.pack("<4I", 0xf2000000, setup, td + 16, setup + 7))
        self.q.wr(td + 16, struct.pack("<4I", 0xf3140000, data, td + 32, data + 7))
        self.q.wr(td + 32, struct.pack("<4I", 0xf3080000, 0, td + 48, 0))
        self.q.wr(td + 48, bytes(16))
        self.q.wr(ed, struct.pack("<4I", 8 << 16, td + 48, td, 0))
        self.write(self.base + 0x20, ed)
        self.write(self.base + 8, 2)  # control list filled
        self.write(self.base + 4, 0x90)  # operational + control list enabled
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and self.read(ed + 8) & ~15 != td + 48:
            time.sleep(.005)
        flags = [self.read(td + i * 16) for i in range(3)]
        assert all(flag >> 28 == 0 for flag in flags), [hex(flag) for flag in flags]
        assert self.read(ed + 8) & ~15 == td + 48
        self.write(self.base + 4, 0x80)
        remaining = self.read(td + 20)
        length = remaining - data if remaining else 8
        return self.q.rd(data, length)


def button(q, down):
    q.execute("input-send-event", {"events": [{"type": "btn", "data": {
        "button": "left", "down": down}}]})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--qemu", required=True)
    ap.add_argument("--support", required=True)
    args = ap.parse_args()
    results = {}
    for kind in ("kbd", "tablet"):
        for phase in ("off", "pending", "asleep"):
            q = machine(args.qemu, args.support, phase != "off", kind)
            r = None
            try:
                r = probe.begin_cpu(q)
                usb = OHCI(q)
                assert usb.report()[0] == 0
                if phase != "off":
                    assert q.pmu(0x7f, b"MATT", count=1) == [0x70]
                if phase == "asleep":
                    probe.enter_power(q, r)
                    probe.wait_for(q, {"asleep": True, "wake-latched": False})
                event = probe.key if kind == "kbd" else button
                event(q, True)
                if phase == "asleep":
                    probe.wait_for(q, {"asleep": False, "sleep-pending": False,
                                       "wake-latched": False})
                elif phase == "pending":
                    assert probe.state(q)["sleep-pending"]
                    assert probe.state(q)["wake-latched"]
                    assert "NIP 00001000" in probe.registers(q)
                expected = 2 if kind == "kbd" else 1
                held = usb.report()
                assert held[0] == expected, (kind, phase, held.hex())
                # A second report reflects held state, not another synthetic press.
                assert usb.report()[0] == expected
                event(q, False)
                released = usb.report()
                assert released[0] == 0, (kind, phase, released.hex())
                results[kind + "_" + phase] = {"held": held.hex(), "released": released.hex()}
            finally:
                if r:
                    r.s.close()
                q.close()
    for preserve in (False, True):
        q = machine(args.qemu, args.support, True, "kbd", preserve)
        r = None
        try:
            r = probe.begin_cpu(q)
            usb = OHCI(q)
            def key(code, down):
                q.execute("input-send-event", {"events": [{"type": "key", "data": {
                    "down": down, "key": {"type": "qcode", "data": code}}}]})
            for code, modifier, usage in (("shift", 2, 0), ("ctrl_r", 16, 0), ("x", 0, 0x1b)):
                key(code, True)  # deliberately reset before the queued event was polled
                for repeat in range(3):
                    usb.reset()
                    report = usb.report()
                    expected = (modifier, usage) if preserve else (0, 0)
                    assert (report[0], report[2]) == expected, (preserve, code, report.hex())
                key(code, False)
                usb.reset()
                assert usb.report() == bytes(8)
            key("shift", True)
            key("shift", False)
            usb.reset()
            assert usb.report() == bytes(8), "released keys were replayed across reset"
            for repeat in range(20):
                key("shift", True)  # fill the protocol queue before a physical release
            key("shift", False)
            usb.reset()
            assert usb.report() == bytes(8), "queue-full release left a physical modifier held"
            q.execute("system_reset")
            time.sleep(.03)
            # The untouched matrix is neutral on a cold reset too.
            assert not any(probe.state(q).values())
            results["physical_reset_on" if preserve else "legacy_reset_off"] = True
        finally:
            if r:
                r.s.close()
            q.close()
    q = machine(args.qemu, args.support, True, "kbd", True)
    dst = None
    r = None
    try:
        r = probe.begin_cpu(q)
        usb = OHCI(q)
        probe.key(q, True)
        usb.reset()  # pending current-state report, n=0; do not consume it
        image = q.root / "held-migration.bin"
        def complete(vm):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                status = vm.execute("query-migrate")
                if status.get("status") == "completed":
                    return
                assert status.get("status") != "failed", status
                time.sleep(.01)
            raise AssertionError(vm.execute("query-migrate"))
        q.execute("migrate", {"uri": "exec:cat > " + str(image)})
        complete(q)
        dst = machine(args.qemu, args.support, True, "kbd", True, incoming=True)
        dst.execute("migrate-incoming", {"uri": "exec:cat " + str(image)})
        complete(dst)
        dst.execute("cont")
        # Retain loaded OHCI state: construction would reset the USB port and
        # accidentally hide loss of the migrated pending-report flag.
        usb2 = OHCI.__new__(OHCI)
        usb2.q, usb2.base = dst, usb.base
        report = usb2.interrupt_report()
        assert report[0] == 2, report.hex()
        probe.key(dst, False)
        assert usb2.interrupt_report() == bytes(8)
        results["held_reset_migration_first_interrupt_and_release"] = True
        dst.close()
        dst = None
        blob = image.read_bytes()
        name = b"HIDKeyboardDevice/physical-keys"
        offsets = [i for i in range(2, len(blob)) if blob.startswith(name, i) and
                   blob[i - 2:i] == bytes((5, len(name)))]
        assert len(offsets) == 1, offsets
        offset = offsets[0] + len(name) + 4  # subsection version, then fields
        assert int.from_bytes(blob[offset:offset + 8], "big") == 2
        assert blob[offset + 24] == 1  # pending current-state report
        for case, position, mask, preserve in (
                ("reserved_bit_zero", offset + 7, 1, True),
                ("unused_tail_bit", offset + 16, 128, True),
                ("destination_property_off", None, None, False)):
            data = bytearray(blob)
            if position is not None:
                data[position] |= mask
            invalid = q.root / (case + ".bin")
            invalid.write_bytes(data)
            bad = machine(args.qemu, args.support, True, "kbd", preserve, incoming=True)
            rejected = False
            try:
                try:
                    bad.execute("migrate-incoming", {"uri": "exec:cat " + str(invalid)})
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        status = bad.execute("query-migrate")
                        if status.get("status") == "failed":
                            rejected = True
                            break
                        assert status.get("status") != "completed", (case, status)
                        time.sleep(.01)
                except (RuntimeError, EOFError, OSError, ValueError):
                    rejected = True
                bad.log.flush()
                bad.log.seek(0)
                log = bad.log.read()
                assert rejected and (
                    "Failed to load usb-kbd:hid" in log or
                    "HIDKeyboardDevice/physical-keys" in log
                ), (case, rejected, log[-1000:])
                results[case + "_rejected"] = True
            finally:
                # A rejected incoming migration can close QMP before cleanup.
                # Reap that expected failed child and close every remaining
                # channel even when flushing its dead peer raises EPIPE.
                for name in ("qf", "qmp", "f", "sock"):
                    item = getattr(bad, name, None)
                    if item:
                        try:
                            item.close()
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                if bad.proc.poll() is None:
                    bad.proc.terminate()
                bad.proc.wait(5)
                bad.log.close()
                bad.tmp.cleanup()
                bad.closed = True
    finally:
        if r:
            r.s.close()
        if dst:
            dst.close()
        q.close()
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
