import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rbguestprep import hfs, install68k as inst


def digest(blob):
    return {"size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}


def write_tree(root, *, data=b"document data", resource=b"resource bytes"):
    (root / "data" / "Apps").mkdir(parents=True)
    (root / "rsrc" / "Apps").mkdir(parents=True)
    (root / "data" / "Apps" / "Sample").write_bytes(data)
    (root / "rsrc" / "Apps" / "Sample").write_bytes(resource)
    folder_info = bytes.fromhex("0000000000000000a000000000000000") + bytes(range(16))
    file_info = b"TEXTttxt" + bytes.fromhex("a000001000200000") + bytes(reversed(range(16)))
    entries = [{"path": ["Apps"], "kind": "folder", "finder_info": folder_info.hex()},
               {"path": ["Apps", "Sample"], "kind": "file", "finder_info": file_info.hex(),
                "created": "1995-10-03T12:00:00", "modified": "1997-01-02T03:04:05",
                "data": digest(data), "rsrc": digest(resource)}]
    (root / "manifest.json").write_text(json.dumps({"format": "retroboot-mac-tree", "version": 1,
                                                   "entries": entries}))
    return entries


def synthetic_hfs(path, *, data=b"document data", resource=b"resource bytes"):
    """A synthetic two-leaf HFS catalog with a real fork allocation to transfer.

    It exercises on-disk catalog offsets, not a mocked Volume. Only the
    structures used by the offline reader are populated; Linux/fsck integration
    uses a separately formatted real volume.
    """
    image = bytearray(4 << 20)
    mdb = memoryview(image)[1024:1536]
    mdb[:2] = b"BD"
    struct.pack_into(">H", mdb, 0x0a, hfs.ATRB_UNMOUNTED)
    struct.pack_into(">H", mdb, 0x12, 4096)
    struct.pack_into(">I", mdb, 0x14, 512)
    struct.pack_into(">H", mdb, 0x1c, 8)
    struct.pack_into(">I", mdb, 0x1e, 20)
    struct.pack_into(">H", mdb, 0x22, 4000)
    mdb[0x24] = 9
    mdb[0x25:0x2e] = b"Synthetic"
    struct.pack_into(">HH", mdb, 0x86, 5, 2)
    struct.pack_into(">I", mdb, 0x92, 2048)
    struct.pack_into(">HH", mdb, 0x96, 10, 4)

    def record(parent, name, cnid, kind, content=b"", block=0):
        raw = name.encode("mac_roman")
        key = b"\0" + struct.pack(">I", parent) + bytes([len(raw)]) + raw
        prefix = bytes([len(key)]) + key
        prefix += bytes(len(prefix) % 2)
        rec = bytearray(70 if kind == hfs.REC_FOLDER else 102)
        rec[0] = kind
        struct.pack_into(">I", rec, 6 if kind == hfs.REC_FOLDER else hfs.F_CNID, cnid)
        if kind == hfs.REC_FILE and content:
            struct.pack_into(">HII", rec, hfs.F_STBLK, block, len(content), 512)
            struct.pack_into(">HH", rec, hfs.F_EXTREC, block, 1)
            start = (8 + block) * 512
            image[start:start + len(content)] = content
        return prefix + rec

    records = [record(1, "Synthetic", 2, hfs.REC_FOLDER), record(2, "Apps", 16, hfs.REC_FOLDER),
               record(16, "Sample", 17, hfs.REC_FILE, data, 50),
               record(2, "temporary", 18, hfs.REC_FOLDER),
               record(18, "000000", 19, hfs.REC_FILE, resource, 51)]
    cat = (8 + 10) * 512
    header = memoryview(image)[cat:cat + 512]
    header[8] = 1
    struct.pack_into(">HIIIIH", header, 14, 1, 1, 5, 1, 2, 512)
    for number, rows in ((1, records[:3]), (2, records[3:])):
        node = memoryview(image)[cat + number * 512:cat + (number + 1) * 512]
        struct.pack_into(">IIbBH", node, 0, 2 if number == 1 else 0, 0 if number == 1 else 1, -1, 1, len(rows))
        pos = 14
        for i, row in enumerate(rows):
            struct.pack_into(">H", node, 512 - 2 * (i + 1), pos)
            node[pos:pos + len(row)] = row
            pos += len(row)
        struct.pack_into(">H", node, 512 - 2 * (len(rows) + 1), pos)
    path.write_bytes(image)


class ValidationTests(unittest.TestCase):
    def test_installer_lock_cannot_be_bypassed_with_a_hardlink(self):
        with tempfile.TemporaryDirectory() as d:
            image = Path(d) / "disk.img"
            image.write_bytes(b"disk")
            alias = Path(d) / "alias.img"
            os.link(image, alias)
            with mock.patch.object(inst, "assert_offline"), inst._exclusive(image):
                with self.assertRaises(inst.InstallError) as caught:
                    with inst._exclusive(alias):
                        self.fail("The second installer acquired the same disk")
                self.assertEqual(caught.exception.code, "disk-in-use")

    def test_macbinary_header_and_crc_include_macroman_name(self):
        import binascii
        entry = {"path": ["Caf\u00e9"], "finder_info": (b"APPLTEST" + bytes.fromhex("a001") + bytes(22)).hex()}
        encoded = inst._macbinary(entry, b"data", b"resource")
        self.assertEqual(encoded[2:6], "Caf\u00e9".encode("mac_roman"))
        self.assertEqual(encoded[73], 0xa0)
        self.assertEqual(encoded[101], 1)
        self.assertEqual(struct.unpack(">II", encoded[83:91]), (4, 8))
        self.assertEqual(struct.unpack(">H", encoded[124:126])[0], binascii.crc_hqx(encoded[:124], 0))
        self.assertEqual(encoded[128:132], b"data")
        self.assertEqual(encoded[256:264], b"resource")

    def test_fork_mismatch_rejected_before_disk_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            tree = Path(d) / "tree"
            write_tree(tree)
            (tree / "data" / "Apps" / "Sample").write_bytes(b"corrupt")
            with mock.patch.object(inst.prep68k, "fsck") as check:
                with self.assertRaises(inst.InstallError) as caught:
                    inst.install_tree(Path(d) / "missing.img", [], tree)
                self.assertEqual(caught.exception.code, "invalid-tree")
                check.assert_not_called()

    def test_symlink_and_case_collision_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tree = root / "tree"
            write_tree(tree)
            fork = tree / "data" / "Apps" / "Sample"
            fork.unlink()
            outside = root / "outside"
            outside.write_bytes(b"document data")
            fork.symlink_to(outside)
            with self.assertRaises(inst.InstallError):
                inst.Tree(tree)
            fork.unlink()
            fork.write_bytes(b"document data")
            manifest = json.loads((tree / "manifest.json").read_text())
            manifest["entries"].append({"path": ["apps"], "kind": "folder", "finder_info": bytes(32).hex()})
            (tree / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(inst.InstallError):
                inst.Tree(tree)

    def test_live_emulator_refusal_uses_inode_for_both_architectures(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            disk, alias = root / "disk.img", root / "alias.img"
            disk.write_bytes(b"disk")
            os.link(disk, alias)
            proc = root / "proc" / "123"
            (proc / "fd").mkdir(parents=True)
            (proc / "fd" / "9").symlink_to(alias)
            for exe in inst.QEMU_NAMES:
                (proc / "cmdline").write_bytes(os.fsencode("/usr/bin/" + exe) + b"\0-drive\0file=other\0")
                with self.subTest(exe=exe), self.assertRaises(inst.InstallError) as caught:
                    inst.assert_offline(disk, proc_root=root / "proc")
                self.assertEqual(caught.exception.code, "disk-in-use")


class CatalogTests(unittest.TestCase):
    def test_overflow_allocation_is_refused_before_target_changes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            image = root / "disk.img"
            synthetic_hfs(image)
            entries = write_tree(root / "tree")
            with hfs.Volume(str(image), writable=True) as vol:
                temp = vol.lookup(["temporary", "000000"])
                allocation = temp.fork("data")
                allocation.physical_size = 4 * 512
                vol.set_fork_record(temp, "data", allocation)
                vol.flush()
            with self.assertRaises(inst.InstallError) as caught:
                inst._move_resources(image, [], "temporary", [(entries[1], "000000")])
            self.assertEqual(caught.exception.code, "fragmented")
            with hfs.Volume(str(image)) as vol:
                self.assertEqual(vol.lookup(["Apps", "Sample"]).fork("rsrc").logical_size, 0)

    def test_transfer_preserves_both_forks_and_32_byte_file_folder_info(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            image = root / "disk.img"
            synthetic_hfs(image)
            entries = write_tree(root / "tree")
            inst._move_resources(image, [], "temporary", [(entries[1], "000000")])
            with hfs.Volume(str(image), writable=True) as vol:
                index = vol.index()
                for entry in entries:
                    inst._metadata(vol, vol.lookup(entry["path"], index), entry)
                vol.flush()
            with mock.patch.object(inst, "assert_offline"):
                result = inst.verify_tree(image, [], root / "tree")
                self.assertEqual(result["verified_forks"], 2)
                inst.export_tree(image, ["Apps"], root / "export")
            exported = inst.Tree(root / "export")
            self.assertEqual(exported.read(exported.entries[0], "data"), b"document data")
            self.assertEqual(exported.read(exported.entries[0], "rsrc"), b"resource bytes")
            self.assertEqual(exported.entries[0]["finder_info"], entries[1]["finder_info"])
            with hfs.Volume(str(image)) as vol:
                self.assertEqual(vol.lookup(["temporary", "000000"]).fork("data").logical_size, 0)

    def test_resource_corruption_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            image = root / "disk.img"
            synthetic_hfs(image)
            entries = write_tree(root / "tree")
            inst._move_resources(image, [], "temporary", [(entries[1], "000000")])
            with hfs.Volume(str(image), writable=True) as vol:
                index = vol.index()
                for entry in entries:
                    inst._metadata(vol, vol.lookup(entry["path"], index), entry)
                vol.write_fork_in_place(vol.lookup(["Apps", "Sample"], index), "rsrc", 0, b"X")
                vol.flush()
            with mock.patch.object(inst, "assert_offline"), self.assertRaises(inst.InstallError) as caught:
                inst.verify_tree(image, [], root / "tree")
            self.assertEqual(caught.exception.code, "verify-failed")


if __name__ == "__main__":
    unittest.main()
