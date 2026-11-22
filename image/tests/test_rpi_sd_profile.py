"""Zero SD layout and embedded guest policy; no privileged host operations."""
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

IMAGE = Path(__file__).resolve().parents[1]
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
            info = sd.write_mbr(path, 256, 3072, 4096, disk_id=0x7C34AB61)
            with path.open("rb") as file:
                mbr = file.read(512)
            self.assertEqual(mbr[510:], b"\x55\xaa")
            self.assertEqual(struct.unpack_from("<I", mbr, 440)[0], 0x7C34AB61)
            entries = [struct.unpack_from("<B3sB3sII", mbr, 446 + 16*i) for i in range(4)]
            self.assertEqual([e[2] for e in entries], [0x0c, 0x83, 0x83, 0])
            self.assertEqual([e[0] for e in entries], [0x80, 0, 0, 0])
            self.assertEqual(entries[0][4], 2048)
            for i, entry in enumerate(entries[:3]):
                self.assertEqual(entry[4] % 2048, 0)
                self.assertEqual(entry[5] % 2048, 0)
                if i:
                    self.assertEqual(entry[4], entries[i-1][4] + entries[i-1][5])
            self.assertEqual(path.stat().st_size, (entries[2][4] + entries[2][5]) * 512)
            self.assertEqual(info["disk_id"], "7c34ab61")
            # The writer refuses to overwrite an existing file, including symlinks.
            with self.assertRaises(FileExistsError):
                sd.write_mbr(path, 256, 3072, 4096)
            self.assertEqual(path.stat().st_size, (entries[2][4] + entries[2][5]) * 512)

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


if __name__ == "__main__":
    unittest.main()
