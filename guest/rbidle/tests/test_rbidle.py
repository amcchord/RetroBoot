"""RetroBoot Idle tests.

    python3 -m unittest discover -s guest/rbidle/tests -v

The build tests need a built INIT (guest/rbidle/build.sh; RBIDLE_BUILD
overrides the build directory) and Retro68's m68k objdump
(RETRO68_TOOLCHAIN); they are skipped otherwise.  The checker tests use
synthetic RAM images only.
"""
from __future__ import annotations

import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
RBIDLE = HERE.parent
REPO = RBIDLE.parents[1]
sys.path.insert(0, str(RBIDLE / "tools"))
sys.path.insert(0, str(REPO / "apps"))
import rbidle_check  # noqa: E402

BUILD = Path(os.environ.get("RBIDLE_BUILD", RBIDLE / "build"))
TOOLCHAIN = Path(os.environ.get("RETRO68_TOOLCHAIN",
                                "/Users/austinmcchord/Development/ClassicMac/vendor/Retro68-build/toolchain"))
OBJDUMP = TOOLCHAIN / "bin" / "m68k-apple-macos-objdump"

# The handler as it must be: (mnemonic, operands) in objdump's MIT syntax.
EXPECTED_HANDLER = [
    ("movew", "%sr,%sp@-"),
    ("oriw", "#1792,%sr"),                 # mask interrupts (IPL 7)
    ("movel", "%d0,%sp@-"),
    ("lea", "%pc@(BLOCK),%a0"),            # the info block, PC-relative
    ("addql", "#1,%a0@(12)"),              # idle calls
    ("tstl", "0x14c"),                     # EventQueue.qHead
    ("bnes", "BUSY"),
    ("cmpib", "#32,%sp@(4)"),              # saved SR high byte == $20
    ("bnes", "DONE"),
    ("movel", "0x16a,%d0"),                # Ticks
    ("cmpl", "%a0@(20),%d0"),
    ("beqs", "COUNT"),
    ("movel", "%d0,%a0@(20)"),
    ("clrw", "%a0@(24)"),
    ("addqw", "#1,%a0@(24)"),              # COUNT
    ("movew", "%a0@(24),%d0"),
    ("cmpw", "%a0@(26),%d0"),              # min_run
    ("bcss", "DONE"),
    ("addql", "#1,%a0@(16)"),              # STOPs
    ("stop", "#8192"),                     # STOP #$2000
    ("bras", "DONE"),
    ("clrw", "%a0@(24)"),                  # BUSY
    ("movel", "%sp@+,%d0"),                # DONE
    ("movew", "%sp@+,%sr"),
    ("moveal", "0x2b6,%a0"),               # what the replaced instructions did
    ("addaw", "#128,%a0"),
    ("rts", ""),
]


def _flat() -> bytes | None:
    p = BUILD / "RetroBootIdle.flt"
    return p.read_bytes() if p.exists() else None


@unittest.skipUnless(_flat() is not None and OBJDUMP.exists(), "needs a built INIT and Retro68's objdump")
class HandlerCode(unittest.TestCase):
    def setUp(self):
        self.flat = _flat()
        self.at = self.flat.index(b"RBidIdle")

    def disassemble(self, start: int, end: int) -> list[tuple[int, str, str]]:
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(self.flat[self.at:self.at + end])
            path = f.name
        try:
            out = subprocess.run([str(OBJDUMP), "-D", "-b", "binary", "-m", "m68k:68040",
                                  f"--start-address={start}", path], capture_output=True, text=True, check=True).stdout
        finally:
            os.unlink(path)
        insns = []
        for line in out.splitlines():
            m = re.match(r"\s*([0-9a-f]+):\t[0-9a-f ]+\t(\S+)\s*(.*)$", line)
            if m:
                insns.append((int(m.group(1), 16), m.group(2), m.group(3).strip()))
        return insns

    def test_block_header(self):
        b = self.flat[self.at:self.at + 48]
        self.assertEqual(b[:8], b"RBidIdle")
        layout, sites, calls, stops, tick, run, min_run = struct.unpack(">HHIIIHH", b[8:28])
        self.assertEqual((layout, sites, calls, stops, tick, run), (1, 0, 0, 0, 0, 0))
        self.assertGreaterEqual(min_run, 1)
        self.assertEqual(b[28:48], bytes(20))

    def test_handler_decodes_to_the_intended_instructions(self):
        insns = self.disassemble(48, 48 + 0x2e + 4 * 0 + 0x30)
        labels = {}
        got = []
        for addr, op, args in insns[:len(EXPECTED_HANDLER)]:
            got.append((addr, op, args))
        self.assertEqual(len(got), len(EXPECTED_HANDLER), insns)
        addr_of = {}
        for i, ((addr, op, args), (want_op, want_args)) in enumerate(zip(got, EXPECTED_HANDLER)):
            self.assertEqual(op, want_op, f"instruction {i} at {addr:#x}: {op} {args}")
            if want_args.startswith(("BUSY", "DONE", "COUNT")):
                labels.setdefault(want_args, set()).add(int(args.split()[0], 16))
                continue
            if "BLOCK" in want_args:
                self.assertEqual(args, "%pc@(0x0),%a0")      # PC-relative to offset 0 of the block
                continue
            self.assertEqual(args, want_args, f"instruction {i} at {addr:#x}")
            addr_of[i] = addr
        # every branch to one label lands on the same instruction, the one the listing names
        by_index = {i: a for i, (a, _o, _r) in enumerate(got)}
        self.assertEqual(labels["COUNT"], {by_index[14]})
        self.assertEqual(labels["BUSY"], {by_index[21]})
        self.assertEqual(labels["DONE"], {by_index[22]})
        # nothing follows the rts inside the block
        self.assertEqual(got[-1][1], "rts")

    def test_handler_is_position_independent(self):
        # no absolute long addressing in the copied block (only $14C, $16A, $2B6 as absolute short)
        insns = self.disassemble(48, 0x2e + 48 + 0x30)
        for _a, op, args in insns[:len(EXPECTED_HANDLER)]:
            if op.startswith("b"):          # relative branches print their target
                continue
            for n in re.findall(r"0x([0-9a-f]+)\b", args):
                if args.startswith("%pc@") or int(n, 16) in (0x14c, 0x16a, 0x2b6, 0x0):
                    continue
                self.fail(f"absolute reference in handler: {args}")

    def test_init_does_not_carry_the_signature(self):
        # the scan must never find (and patch) RetroBoot Idle's own code or block
        self.assertNotIn(rbidle_check.SITE, self.flat)
        self.assertEqual(rbidle_check.signature_sites(self.flat), [])


@unittest.skipUnless((BUILD / "tree" / "manifest.json").exists(), "needs build.sh's tree")
class InitTree(unittest.TestCase):
    def test_tree_is_one_init_file(self):
        from rbapps import tree
        t = tree.Tree(BUILD / "tree")
        self.assertEqual(t.verify(), [])
        entries = t.manifest["entries"]
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["path"], ["RetroBoot Idle"])
        fi = bytes.fromhex(e["finder_info"])
        self.assertEqual(fi[:8], b"INITRBid")
        flags = struct.unpack(">H", fi[8:10])[0]
        self.assertFalse(flags & 0x0080, "kHasNoINITs set: Mac OS would never run it")
        self.assertEqual(e["data"]["size"], 0)


def _ram_with_site(size=0x40000, site=0x10000, power_gap=0x48):
    ram = bytearray(size)
    ram[site:site + 8] = rbidle_check.SITE
    ram[site + power_gap:site + power_gap + 4] = rbidle_check.POWER
    return ram


class Checker(unittest.TestCase):
    def test_unpatched_signature_found(self):
        ram = _ram_with_site()
        r = rbidle_check.check(bytes(ram))
        self.assertFalse(r["active"])
        self.assertEqual(r["unpatched_signature_sites"], ["0x10000"])

    def test_signature_too_far_from_power_call_is_ignored(self):
        ram = _ram_with_site(power_gap=0x90)
        self.assertEqual(rbidle_check.check(bytes(ram))["unpatched_signature_sites"], [])

    def test_patched_site_verified(self):
        ram = _ram_with_site()
        block = 0x20000
        hdr = b"RBidIdle" + struct.pack(">HHIIIHH", 1, 1, 500, 300, 99, 1, 2)
        hdr += struct.pack(">4I", 0x10000, 0, 0, 0) + struct.pack(">I", 0x0100)
        ram[block:block + 48] = hdr
        ram[0x10000:0x10008] = b"\x4e\xb9" + struct.pack(">I", block + 48) + b"\x4e\x71"
        r = rbidle_check.check(bytes(ram))
        self.assertTrue(r["active"], r)
        self.assertEqual((r["idle_calls"], r["stops"], r["min_run"], r["version"]), (500, 300, 2, "1.0"))
        self.assertEqual(r["unpatched_signature_sites"], [])

    def test_wrong_jump_target_is_not_active(self):
        ram = _ram_with_site()
        block = 0x20000
        ram[block:block + 48] = (b"RBidIdle" + struct.pack(">HHIIIHH", 1, 1, 0, 0, 0, 0, 2)
                                 + struct.pack(">4I", 0x10000, 0, 0, 0) + struct.pack(">I", 0x0100))
        ram[0x10000:0x10008] = b"\x4e\xb9" + struct.pack(">I", block + 52) + b"\x4e\x71"
        self.assertFalse(rbidle_check.check(bytes(ram))["active"])

    def test_template_block_without_sites_is_ignored(self):
        ram = bytearray(0x1000)
        ram[0x100:0x100 + 48] = b"RBidIdle" + struct.pack(">HH", 1, 0) + bytes(36)
        r = rbidle_check.check(bytes(ram))
        self.assertFalse(r["active"])


if __name__ == "__main__":
    unittest.main()
