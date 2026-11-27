"""Zero SD layout and embedded guest policy; no privileged host operations."""
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

IMAGE = Path(__file__).resolve().parents[1]
REPO = IMAGE.parent
spec = importlib.util.spec_from_file_location("sd_profile", IMAGE / "rpi/scripts/sd_profile.py")
sd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sd)


class SDProfileTests(unittest.TestCase):
    def test_zero_policy_excludes_powerpc(self):
        self.assertEqual(sd.profile("zero2w")["machine_families"], ["quadra800"])
        self.assertEqual(sd.profile("zero2w")["quadra_ram_mb"], 64)
        self.assertEqual(sd.profile("pi4-5")["machine_families"], ["powermac", "quadra800"])

    def test_only_quadras_can_be_embedded_in_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text('{"machineFamily": "quadra800"}')
            sd.validate_machine("zero2w", root)
            (root / "machine.json").write_text('{"family": "powermac"}')
            with self.assertRaises(ValueError):
                sd.validate_machine("zero2w", root)
            (root / "machine.json").unlink()
            (root / "config.json").write_text('{"machineFamily": "powermac"}')
            with self.assertRaises(ValueError):
                sd.validate_machine("zero2w", root)

    def test_actual_mbr_bytes_are_compatible_and_contiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sd.img"
            info = sd.write_mbr(path, 256, 2560, 1024, disk_id=0x7C34AB61)
            with path.open("rb") as file:
                mbr = file.read(512)
            self.assertEqual(mbr[510:], b"\x55\xaa")
            self.assertEqual(struct.unpack_from("<I", mbr, 440)[0], 0x7C34AB61)
            entries = [struct.unpack_from("<B3sB3sII", mbr, 446 + 16*i) for i in range(4)]
            self.assertEqual([e[2] for e in entries], [0x0c, 0x83, 0x83, 0x83])
            self.assertEqual([e[0] for e in entries], [0x80, 0, 0, 0])
            self.assertEqual(entries[0][4], 2048)
            for i, entry in enumerate(entries):
                self.assertEqual(entry[4] % 2048, 0)
                self.assertEqual(entry[5] % 2048, 0)
                if i:
                    self.assertEqual(entry[4], entries[i-1][4] + entries[i-1][5])
            self.assertEqual(path.stat().st_size, (entries[3][4] + entries[3][5]) * 512)
            self.assertEqual(info["disk_id"], "7c34ab61")
            # The writer refuses to overwrite an existing file, including symlinks.
            with self.assertRaises(FileExistsError):
                sd.write_mbr(path, 256, 2560, 1024)
            self.assertEqual(path.stat().st_size, (entries[3][4] + entries[3][5]) * 512)

    def test_invalid_or_overflowing_sizes_rejected_before_creation(self):
        for sizes in ((0, 1, 1), (256, -1, 4096), (1, 1, 2**32), (True, 1, 1)):
            with self.subTest(sizes=sizes), self.assertRaises(ValueError):
                sd.mbr_partitions(*sizes)

    def test_mbr_fstab_uses_each_actual_partuuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "etc").mkdir()
            path = root / "etc/fstab"
            path.write_text("PARTLABEL=RBROOT_A / ext4 defaults 0 1\n"
                            "PARTLABEL=RBDATA /var/lib/retroboot xfs noatime 0 0\n"
                            "PARTLABEL=RBEFI /boot/firmware vfat noauto 0 0\n")
            sd.update_fstab(root, "1234abcd-01", "1234abcd-02", "1234abcd-03")
            lines = [line.split() for line in path.read_text().splitlines()]
            self.assertEqual({l[1]: l[0] for l in lines}, {
                "/": "PARTUUID=1234abcd-02", "/var/lib/retroboot": "PARTUUID=1234abcd-03",
                "/boot/firmware": "PARTUUID=1234abcd-01"})
            self.assertNotIn("PARTLABEL", path.read_text())

    def test_starter_macs_seed_requires_oobe_true(self):
        hostd_dir = str(REPO / "hostd")
        added = hostd_dir not in sys.path
        if added:
            sys.path.insert(0, hostd_dir)
        try:
            sm_spec = importlib.util.spec_from_file_location("starter_macs", IMAGE / "rpi/scripts/starter_macs.py")
            sm = importlib.util.module_from_spec(sm_spec)
            sm_spec.loader.exec_module(sm)
            with tempfile.TemporaryDirectory() as tmp:
                data = Path(tmp)
                (data / "state").mkdir(parents=True)
                (data / "config").mkdir(parents=True)
                ids = []
                for entry in sm.entries():
                    mid = sm.machine_slug(entry["name"])
                    ids.append(mid)
                    mdir = data / "machines" / mid
                    mdir.mkdir(parents=True)
                    with (mdir / "disk.img").open("wb") as f:
                        f.truncate(entry["diskCapacityBytes"])
                    (mdir / "Quadra800.rom").write_bytes(b"rom")
                    (mdir / "machine.json").write_text(json.dumps({
                        "schema_version": 2, "id": mid, "name": entry["name"],
                        "os_version": entry["osVersion"], "family": "quadra800",
                        "disk": "disk.img", "rom": "Quadra800.rom", "pram": "pram.img",
                        "source": sm.source_for(entry),
                    }))
                (data / sm.MARKER).write_text("".join(f"{mid}\n" for mid in ids))
                (data / sm.SEED).write_text(json.dumps({"settings": {"machine.active": ids[0]}, "oobe": True}))
                with mock.patch.object(sm, "check_rom"):
                    sm.verify(data)
                    (data / sm.SEED).write_text(json.dumps({"settings": {"machine.active": ids[0]}}))
                    with self.assertRaises(ValueError):
                        sm.verify(data)
        finally:
            if added and hostd_dir in sys.path:
                sys.path.remove(hostd_dir)


if __name__ == "__main__":
    unittest.main()
