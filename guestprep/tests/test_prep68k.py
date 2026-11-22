"""68k (HFS) guest preparation tests.

    python3 -m unittest discover -s tests -v

Template and classification tests need nothing.  The HFS reader tests build a
small volume with hfsutils (``hformat``/``hmkdir``/``hcopy``; skipped when
absent).  RB_FIXTURE_68K_IMAGE=/copy/disk.img (root, Linux with the hfs
module and fsck.hfs) runs prep68k.apply twice on a disposable copy.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from rbguestprep import hfs, hfsplus, pram68k, prep68k  # noqa: E402

# Installer defaults found on fresh 7.5.3 and 8.1 installs (TCP/IP inactive:
# 'unld' 3, port name "enet" without its unit number)
INSTALLER_IITF = bytes.fromhex(
    "0001030000000000000000012a05656e65740000000000000000000000000000000000000000000000000000"
    "000000000008656e657444525652000000000000000000000000000000000000000000000000000001")


def fork(resources):
    return hfsplus.build_resource_fork(resources)


class TemplateTests(unittest.TestCase):
    def test_tcpip_template_is_the_panels_output(self):
        res = {(t, i): d for t, i, _n, d in prep68k.TCPIP_68K_RESOURCES}
        self.assertEqual(res[("dtyp", 128)], b"\x00\x0a")       # Using DHCP Server
        self.assertEqual(res[("unld", 128)], b"\x00\x01")       # active, load only when needed
        self.assertEqual(res[("port", 128)], b"\x08Ethernet")
        self.assertIn(b"\x05enet0", res[("iitf", 128)])
        self.assertIn(b"\x08enetDRVR", res[("iitf", 128)])
        self.assertEqual(prep68k.classify_tcpip(fork(prep68k.TCPIP_68K_RESOURCES)), "retroboot-dhcp")

    def test_tcpip_ownership(self):
        base = prep68k.TCPIP_68K_RESOURCES
        # the 7.5.3 and 8.0 'vers' stamps and a moved window do not decide ownership
        stamped = base + [("vers", 2, "", bytes.fromhex("08008000000003382e30"))]
        stamped = [(t, i, n, b"\x00\x40\x00\x40" if t == "wpos" else d) for t, i, n, d in stamped]
        self.assertEqual(prep68k.classify_tcpip(fork(stamped)), "retroboot-dhcp")
        installer = [(t, i, n, b"\x00\x03" if t == "unld" else (INSTALLER_IITF if t == "iitf" else d))
                     for t, i, n, d in base]
        self.assertEqual(prep68k.classify_tcpip(fork(installer)), "user")
        manual = [(t, i, n, b"\x00\x00" if t == "dtyp" else d) for t, i, n, d in base]
        self.assertEqual(prep68k.classify_tcpip(fork(manual)), "user")
        self.assertEqual(prep68k.classify_tcpip(fork(base + [("cnam", 129, "Office", b"")])), "user")
        self.assertEqual(prep68k.classify_tcpip(b"junk" * 8), "user")

    def test_appletalk_templates(self):
        for major, template in prep68k.APPLETALK_68K_TEMPLATES.items():
            res = {(t, i): d for t, i, _n, d in template}
            self.assertEqual(len(res[("atpf", 128)]), 274)
            self.assertEqual(res[("atpf", 128)][92], 0)          # inactive
            self.assertEqual(res[("neta", -16400)][4:], b"OTOn")
            self.assertEqual(prep68k.classify_appletalk(fork(template)), "retroboot-inactive", major)

    def test_appletalk_ownership_ignores_pointers_and_hints_only(self):
        template = prep68k.APPLETALK_68K_TEMPLATES["8"]

        def with_atpf(fn):
            return [(t, i, n, fn(bytearray(d)) if t == "atpf" else d) for t, i, n, d in template]

        def scribble(b, lo, hi):
            for k in range(lo, hi):
                b[k] ^= 0x5A
            return bytes(b)
        # a later save: new in-memory pointers and node/network hint
        self.assertEqual(prep68k.classify_appletalk(fork(with_atpf(lambda b: scribble(b, 12, 44)))),
                         "retroboot-inactive")
        self.assertEqual(prep68k.classify_appletalk(fork(with_atpf(lambda b: scribble(b, 93, 96)))),
                         "retroboot-inactive")
        self.assertEqual(prep68k.classify_appletalk(fork(with_atpf(lambda b: scribble(b, 124, 148)))),
                         "retroboot-inactive")
        # AppleTalk made active again (byte 92 as on the installer's file): the user's
        self.assertEqual(prep68k.classify_appletalk(fork(with_atpf(lambda b: scribble(b, 92, 93)))), "user")
        self.assertEqual(prep68k.classify_appletalk(fork(with_atpf(lambda b: scribble(b, 60, 61)))), "user")
        other_port = [(t, i, n, b"\x0cPrinter Port" if t == "port" else d) for t, i, n, d in template]
        self.assertEqual(prep68k.classify_appletalk(fork(other_port)), "user")
        self.assertEqual(prep68k.classify_appletalk(b"junk" * 8), "user")


def macbinary(name: str, type_: bytes, creator: bytes, data: bytes = b"", rsrc: bytes = b"", flags: int = 0) -> bytes:
    """A MacBinary II file (hfsutils' hcopy -m input)."""
    h = bytearray(128)
    raw = name.encode("mac_roman")
    h[1] = len(raw)
    h[2:2 + len(raw)] = raw
    h[65:69], h[69:73] = type_, creator
    h[73] = flags >> 8
    h[83:87] = struct.pack(">I", len(data))
    h[87:91] = struct.pack(">I", len(rsrc))
    h[101] = flags & 0xFF
    h[122] = h[123] = 129
    crc = 0
    for byte in h[:124]:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    h[124:126] = struct.pack(">H", crc)

    def pad(b):
        return b + bytes(-len(b) % 128)
    return bytes(h) + pad(data) + pad(rsrc)


HFSUTILS = all(shutil.which(t) for t in ("hformat", "hmount", "hmkdir", "hcopy", "humount"))


@unittest.skipUnless(HFSUTILS, "needs hfsutils to build a test volume")
class HFSVolumeTests(unittest.TestCase):
    """A 4 MB HFS volume with a System Folder like a 68k Mac's."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="rbprep68k-test-")
        cls.image = os.path.join(cls.dir, "vol.img")
        env = dict(os.environ, HOME=cls.dir)     # hfsutils keeps its state in ~/.hcwd
        with open(cls.image, "wb") as f:
            f.truncate(4 << 20)

        def run(*cmd):
            subprocess.run(cmd, check=True, capture_output=True, env=env)
        run("hformat", "-l", "Macintosh HD", cls.image)
        try:
            for d in ("System Folder", "System Folder:Preferences", "System Folder:Extensions",
                      "System Folder:Extensions (Disabled)"):
                run("hmkdir", ":" + d)
            files = {
                "System Folder:General Controls Prefs": macbinary(
                    "General Controls Prefs", b"pref", b"MACS",
                    rsrc=fork([("Smrt", 0, "", bytes.fromhex("00000042"))])),
                "System Folder:System": macbinary(
                    "System", b"zsys", b"MACS",
                    rsrc=fork([("vers", 1, "", bytes.fromhex("081080000000") + b"\x038.1\x0aMac OS 8.1"),
                               ("vm  ", 0, "", bytes.fromhex("00000000"))])),
                "Shutdown Check": macbinary("Shutdown Check", b"pref", b"MACS", flags=0x4000),
                "System Folder:Preferences:TCP/IP Preferences": macbinary(
                    "TCP/IP Preferences", b"pref", b"ztcp", rsrc=fork(prep68k.TCPIP_68K_RESOURCES)),
            }
            for i, (dest, blob) in enumerate(files.items()):
                src = os.path.join(cls.dir, f"f{i}.bin")
                with open(src, "wb") as f:
                    f.write(blob)
                parent, _, leaf = dest.rpartition(":")
                if leaf == "General Controls Prefs":
                    dest = "System Folder:Preferences:General Controls Prefs"
                run("hcopy", "-m", src, ":" + dest)
        finally:
            subprocess.run(["humount"], capture_output=True, env=env)
        # bless the System Folder (drFndrInfo[0]) as Mac OS does
        with hfs.Volume(cls.image) as vol:
            sysf = vol.lookup(["System Folder"])
        with open(cls.image, "r+b") as f:
            f.seek(1024 + 0x5C)
            f.write(struct.pack(">I", sysf.cnid))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_reader(self):
        self.assertEqual(hfs.locate_volume(self.image), (0, 4 << 20))
        with hfs.Volume(self.image) as vol:
            self.assertEqual(vol.name, "Macintosh HD")
            self.assertTrue(vol.clean)
            rec = vol.lookup(["system folder", "preferences", "TCP/IP PREFERENCES"])   # case-insensitive
            self.assertEqual(rec.type_creator, (b"pref", b"ztcp"))
            self.assertEqual(vol.read_fork(rec, "rsrc"), fork(prep68k.TCPIP_68K_RESOURCES))
            check = vol.lookup(["Shutdown Check"])
            self.assertEqual(check.finder_flags & 0x4000, 0x4000)

    def test_inspect(self):
        r = prep68k.inspect(self.image, "none")
        self.assertEqual(r["filesystem"], "hfs")
        self.assertEqual(r["system_folder"], ["System Folder"])
        self.assertEqual(r["os_version"], "8.1")
        self.assertEqual(r["shutdown_check"], "present")
        self.assertEqual(r["shutdown_warning"], "on")
        self.assertEqual(r["virtual_memory"], "off")
        self.assertEqual(r["retroboot_idle"], "absent")
        self.assertEqual(r["network_stack"], "open-transport")        # Mac OS 8
        self.assertEqual(r["tcpip"], "retroboot-dhcp")
        self.assertEqual(r["appletalk"], "missing")

    def test_shutdown_warning_off_in_place_and_idempotent(self):
        copy = os.path.join(self.dir, "warn.img")
        shutil.copyfile(self.image, copy)
        self.assertEqual(prep68k.disable_shutdown_warning(copy), "disabled (0x00000042 -> 0x00000002)")
        self.assertEqual(prep68k.disable_shutdown_warning(copy), "already-disabled")
        self.assertEqual(prep68k.inspect(copy, "none")["shutdown_warning"], "off")

    def test_volume_attributes_written_to_both_mdbs(self):
        copy = os.path.join(self.dir, "attr.img")
        shutil.copyfile(self.image, copy)
        with hfs.Volume(copy, writable=True) as vol:
            vol.set_attributes(vol.attributes & ~hfs.ATRB_UNMOUNTED)
            vol.flush()
        with hfs.Volume(copy) as vol:
            self.assertFalse(vol.clean)
        with open(copy, "rb") as f:
            f.seek((4 << 20) - 1024 + 0x0A)
            self.assertEqual(struct.unpack(">H", f.read(2))[0] & hfs.ATRB_UNMOUNTED, 0)

    def test_hfs_plus_wrapper_is_not_a_68k_disk(self):
        wrapped = os.path.join(self.dir, "wrapped.img")
        shutil.copyfile(self.image, wrapped)
        with open(wrapped, "r+b") as f:
            f.seek(1024 + 0x7C)
            f.write(b"H+")
        self.assertFalse(hfs.is_hfs_standard(wrapped))
        with self.assertRaises(prep68k.PrepError):
            prep68k.apply(wrapped)


class PramTests(unittest.TestCase):
    def test_ensure_32bit(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pram.img")
            p = bytearray(256)
            p[0x0C:0x10] = b"NuMc"
            p[0x8A] = 0x40                        # 7.5.5's Memory panel: 32-bit addressing off
            p[0x78:0x7C] = bytes.fromhex("ffffffdc")
            with open(path, "wb") as f:
                f.write(p)
            r = pram68k.inspect(path)
            self.assertFalse(r["addressing_32bit"])
            self.assertEqual(r["startup_device"]["scsi_id"], 3)
            self.assertEqual(pram68k.ensure_32bit(path), "turned-on (0x40 -> 0x45)")
            self.assertEqual(pram68k.ensure_32bit(path), "already-on")
            self.assertTrue(pram68k.inspect(path)["addressing_32bit"])
            with open(path, "wb") as f:
                f.write(bytes(256))
            self.assertEqual(pram68k.ensure_32bit(path), "not-initialized")

    def test_ensure_32bit_never_through_a_link(self):
        """The Mac's directory belongs to the guest user: a pram.img link is refused and its
        target never written, and no temporary file is left behind."""
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "victim")
            p = bytearray(256)
            p[0x0C:0x10] = b"NuMc"
            p[0x8A] = 0x40
            with open(target, "wb") as f:
                f.write(p)
            link = os.path.join(d, "pram.img")
            os.symlink(target, link)
            with self.assertRaises(OSError):
                pram68k.ensure_32bit(link)
            with open(target, "rb") as f:
                self.assertEqual(f.read(), bytes(p))
            self.assertEqual(sorted(os.listdir(d)), ["pram.img", "victim"])


@unittest.skipUnless(os.environ.get("RB_FIXTURE_68K_IMAGE") and hasattr(os, "geteuid") and os.geteuid() == 0,
                     "set RB_FIXTURE_68K_IMAGE to a disposable 68k disk copy (root, Linux)")
class FixtureApplyTests(unittest.TestCase):
    def test_apply_twice(self):
        image = os.environ["RB_FIXTURE_68K_IMAGE"]
        first = prep68k.apply(image)
        print(json.dumps(first))
        self.assertEqual(first["fsck_after"], "ok")
        self.assertIn(first["retroboot_idle"], ("installed", "updated", "present", "disabled-by-user"))
        second = prep68k.apply(image)
        self.assertEqual(second["shutdown_check"], "absent")
        self.assertIn(second["shutdown_warning"], ("already-disabled", "smrt-resource-missing"))
        self.assertIn(second["retroboot_idle"], ("present", "disabled-by-user"))
        for key in ("tcpip", "appletalk"):
            if key in second:
                self.assertIn(second[key], ("kept-retroboot", "existing-configuration-kept"))
        r = prep68k.inspect(image)
        self.assertTrue(r["clean"])
        self.assertEqual(r["shutdown_check"], "absent")


if __name__ == "__main__":
    unittest.main()
