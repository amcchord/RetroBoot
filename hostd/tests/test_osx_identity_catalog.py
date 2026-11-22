"""Identity on large sparse, fragmented synthetic HFS+ catalogs; no guest media."""
import plistlib
from pathlib import Path
import struct
import sys
import tempfile
import unittest

from retroboot import osx_identity


BLOCK = 512
CATALOG_BYTES = 100 * 1024 * 1024
FIRST_BLOCKS = CATALOG_BYTES // BLOCK // 2
CATALOG_EXTENTS = [(8, FIRST_BLOCKS), (8 + FIRST_BLOCKS + 17, FIRST_BLOCKS)]
LEAVES = [1, 2, FIRST_BLOCKS - 1, FIRST_BLOCKS, CATALOG_BYTES // BLOCK - 1]
APPS = Path(__file__).resolve().parents[2] / "apps"


def _physical(logical):
    for start, count in CATALOG_EXTENTS:
        if logical < count * BLOCK:
            return start * BLOCK + logical
        logical -= count * BLOCK
    raise ValueError("outside synthetic catalog")


def _key(parent, name):
    encoded = name.encode("utf-16-be")
    return struct.pack(">HIH", len(encoded) + 6, parent, len(name)) + encoded


def _node(kind, records, next_node=0):
    node = bytearray(BLOCK)
    struct.pack_into(">IIbBHH", node, 0, next_node, 0, kind, 1 if kind == -1 else 0, len(records), 0)
    offset = 14
    for index, record in enumerate(records):
        struct.pack_into(">H", node, BLOCK - 2 * (index + 1), offset)
        node[offset:offset + len(record)] = record
        offset += len(record)
    struct.pack_into(">H", node, BLOCK - 2 * (len(records) + 1), offset)
    assert offset <= BLOCK - 2 * (len(records) + 1)
    return bytes(node)


def _folder(parent, name, cnid):
    record = bytearray(88)
    struct.pack_into(">h", record, 0, 1)
    struct.pack_into(">I", record, 8, cnid)
    return _key(parent, name) + record


def _image(path, last_next=0, bad_offset=False):
    data = plistlib.dumps({"ProductName": "Mac OS X", "ProductVersion": "10.4.11",
                          "ProductBuildVersion": "8S165"})
    data_block = CATALOG_EXTENTS[-1][0] + CATALOG_EXTENTS[-1][1] + 4
    header = bytearray(BLOCK)
    header[:2] = b"H+"
    struct.pack_into(">I", header, 0x28, BLOCK)
    struct.pack_into(">I", header, 0x2C, data_block + 2)
    struct.pack_into(">Q", header, 0x110, CATALOG_BYTES)
    for index, extent in enumerate(CATALOG_EXTENTS):
        struct.pack_into(">II", header, 0x120 + index * 8, *extent)
    tree_header = struct.pack(">HIIIIHHII", 1, 1, len(LEAVES), 1, LEAVES[-1], BLOCK,
                              516, CATALOG_BYTES // BLOCK, 0) + bytes(76)
    volume_name = "Synthetic".encode("utf-16-be")
    thread = _key(2, "") + struct.pack(">hHIH", 3, 0, 1, len("Synthetic")) + volume_name
    file_record = bytearray(248)
    struct.pack_into(">h", file_record, 0, 2)
    struct.pack_into(">I", file_record, 8, 13)
    struct.pack_into(">Q", file_record, 88, len(data))
    struct.pack_into(">II", file_record, 104, data_block, 1)
    records = [thread, _folder(2, "System", 10), _folder(10, "Library", 11),
               _folder(11, "CoreServices", 12), _key(12, "SystemVersion.plist") + file_record]
    with path.open("wb") as output:
        output.truncate((data_block + 2) * BLOCK)
        output.seek(1024)
        output.write(header)
        output.seek(_physical(0))
        output.write(_node(1, [tree_header]))
        for index, (number, record) in enumerate(zip(LEAVES, records)):
            next_node = LEAVES[index + 1] if index + 1 < len(LEAVES) else last_next
            node = bytearray(_node(-1, [record], next_node))
            if bad_offset and index == 2:
                struct.pack_into(">H", node, BLOCK - 2, BLOCK - 2)
            output.seek(_physical(number * BLOCK))
            output.write(node)
        output.seek(data_block * BLOCK)
        output.write(data)


@unittest.skipUnless(sys.platform.startswith("linux"), "identity child uses Linux address-space limits")
class LargeCatalogIdentity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="retroboot-catalog-")
        self.addCleanup(self.temp.cleanup)
        self.disk = Path(self.temp.name) / "synthetic.img"

    def test_large_fragmented_catalog_identity_under_existing_limits(self):
        _image(self.disk)
        self.assertEqual(osx_identity.read(self.disk, APPS),
                         {"os_version": "10.4.11", "os_build": "8S165"})

    def test_cyclic_leaf_chain_is_not_identity(self):
        _image(self.disk, last_next=1)
        self.assertIsNone(osx_identity.read(self.disk, APPS))

    def test_out_of_range_leaf_is_not_identity(self):
        _image(self.disk, last_next=CATALOG_BYTES // BLOCK)
        self.assertIsNone(osx_identity.read(self.disk, APPS))

    def test_invalid_record_offset_is_not_identity(self):
        _image(self.disk, bad_offset=True)
        self.assertIsNone(osx_identity.read(self.disk, APPS))

    def test_truncated_leaf_is_not_identity(self):
        _image(self.disk)
        with self.disk.open("r+b") as output:
            output.truncate(_physical(LEAVES[-1] * BLOCK) + BLOCK - 1)
        self.assertIsNone(osx_identity.read(self.disk, APPS))


if __name__ == "__main__":
    unittest.main()
