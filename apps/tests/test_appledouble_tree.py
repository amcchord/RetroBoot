from pathlib import Path
import struct
import tempfile
import unittest

from appledouble_tree import convert
from rbapps.tree import Tree, TreeError


def sidecar(entries):
    start = 26 + 12 * len(entries)
    table, payload = b"", b""
    for key, value in entries:
        table += struct.pack(">III", key, start + len(payload), len(value))
        payload += value
    return struct.pack(">II16sH", 0x00051607, 0x00020000, bytes(16), len(entries)) + table + payload


class AppleDoubleDirectoryTests(unittest.TestCase):
    def test_preserves_resource_only_application_and_finder_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input"
            source.mkdir()
            (source / "Application").write_bytes(b"")
            info = b"APPLTEST" + struct.pack(">H", 0x2100) + bytes(range(22))
            resource = b"resource-only executable fixture"
            (source / "._Application").write_bytes(sidecar([(9, info), (2, resource)]))
            convert(source, root / "output", ("Utilities",))
            tree = Tree(root / "output")
            entry = next(e for e in tree.entries if e["kind"] == "file")
            self.assertEqual(tree.read_data(entry), b"")
            self.assertEqual(tree.read_rsrc(entry), resource)
            self.assertEqual(entry["type"], "APPL")
            self.assertEqual(entry["creator"], "TEST")
            self.assertEqual(tree.finder_info(entry)[10:], info[10:])
            self.assertEqual(entry["flags"], 0x2000)
            self.assertEqual(tree.verify(), [])

    def test_refuses_orphan_sidecar_and_nested_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input"
            source.mkdir()
            (source / "._Missing").write_bytes(sidecar([(2, b"fork")]))
            with self.assertRaisesRegex(TreeError, "orphan"):
                convert(source, Path(tmp) / "output")
            with self.assertRaisesRegex(TreeError, "inside"):
                convert(source, source / "output")


if __name__ == "__main__":
    unittest.main()
