"""rbapps on synthetic media: a hand-built HFS volume, Disk Copy 4.2 and NDIF
wrappers, ADC, AppleDouble and the canonical tree (no private media needed)."""
import binascii
import io
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rbapps import diskimage, expand, resfork, tree, volume  # noqa: E402

BLOCK = 512
FIRST = 16          # first allocation block, in sectors


def _key(parent: int, name: bytes) -> bytes:
    k = struct.pack(">BI", 0, parent) + bytes([len(name)]) + name
    k = bytes([len(k)]) + k
    return k + (b"\0" if len(k) % 2 else b"")


def _node(kind: int, records: list[bytes], height: int = 1, flink: int = 0) -> bytes:
    node = bytearray(BLOCK)
    node[0:14] = struct.pack(">IIbBHH", flink, 0, kind, height, len(records), 0)
    off = 14
    offsets = []
    for r in records:
        offsets.append(off)
        node[off:off + len(r)] = r
        off += len(r)
    offsets.append(off)
    for i, o in enumerate(offsets):
        node[BLOCK - 2 * (i + 1):BLOCK - 2 * i] = struct.pack(">H", o)
    return bytes(node)


def _header(first_leaf: int, max_key: int, nodes: int) -> bytes:
    hdr = struct.pack(">HIIIIHHII", 1 if first_leaf else 0, first_leaf, 1 if first_leaf else 0, first_leaf,
                      first_leaf, BLOCK, max_key, nodes, 0) + bytes(76)
    return _node(1, [hdr, bytes(128), bytes(BLOCK - 14 - len(hdr) - 128 - 8)], height=0)


def build_hfs(name: bytes = b"Test", data: bytes = b"hello world", rsrc: bytes = b"RSRC" * 10) -> bytes:
    """A minimal HFS volume: root folder and one application file with both forks."""
    vol = bytearray(1600 * BLOCK)
    folder = struct.pack(">BBHHIIII", 1, 0, 0, 1, 2, 0xB0000000, 0xB0000001, 0) + bytes(16) + bytes(32)
    finfo = b"APPLTEST" + struct.pack(">H", 0x2100) + bytes(6)
    filerec = (struct.pack(">BBBB", 2, 0, 0, 0) + finfo + struct.pack(">I", 16)
               + struct.pack(">HII", 3, len(data), BLOCK) + struct.pack(">HII", 4, len(rsrc), BLOCK)
               + struct.pack(">III", 0xB0000002, 0xB0000003, 0) + bytes(16) + struct.pack(">H", 0)
               + struct.pack(">HHHHHH", 3, 1, 0, 0, 0, 0) + struct.pack(">HHHHHH", 4, 1, 0, 0, 0, 0) + bytes(4))
    thread = struct.pack(">BB8xI", 3, 0, 1) + bytes([len(name)]) + name.ljust(31, b"\0")
    leaf = _node(-1, [_key(1, name) + folder, _key(2, b"") + thread, _key(2, b"App") + filerec])
    cat = _header(1, 37, 2) + leaf
    ext = _header(0, 7, 1)
    base = FIRST * BLOCK
    vol[base:base + BLOCK] = ext
    vol[base + BLOCK:base + 3 * BLOCK] = cat
    vol[base + 3 * BLOCK:base + 3 * BLOCK + len(data)] = data
    vol[base + 4 * BLOCK:base + 4 * BLOCK + len(rsrc)] = rsrc
    mdb = bytearray(162)
    mdb[0:2] = b"BD"
    struct.pack_into(">HI", mdb, 0x12, 1580, BLOCK)        # drNmAlBlks, drAlBlkSiz
    struct.pack_into(">H", mdb, 0x1C, FIRST)
    struct.pack_into(">H", mdb, 0x22, 1575)
    mdb[0x24] = len(name)
    mdb[0x25:0x25 + len(name)] = name
    struct.pack_into(">I", mdb, 0x82, BLOCK)
    struct.pack_into(">HH", mdb, 0x86, 0, 1)
    struct.pack_into(">I", mdb, 0x92, 2 * BLOCK)
    struct.pack_into(">HH", mdb, 0x96, 1, 2)
    vol[1024:1024 + 162] = mdb
    return bytes(vol)


def dc42(raw: bytes, name: bytes = b"Floppy") -> bytes:
    hdr = bytes([len(name)]) + name.ljust(63, b"\0") + struct.pack(">IIII", len(raw), 0,
                                                                     diskimage.dc42_checksum(raw), 0)
    return hdr + b"\x01\x22\x01\x00" + raw


def build_rsrc(resources) -> bytes:
    """Tiny resource fork builder (type, id, data)."""
    data = b"".join(struct.pack(">I", len(d)) + d for _t, _i, d in resources)
    types = {}
    off = 0
    for t, i, d in resources:
        types.setdefault(t, []).append((i, off))
        off += 4 + len(d)
    tl = struct.pack(">H", len(types) - 1)
    refs = b""
    ref_base = 2 + 8 * len(types)
    for t, items in types.items():
        tl += t + struct.pack(">HH", len(items) - 1, ref_base + len(refs))
        for i, o in items:
            refs += struct.pack(">hHII", i, 0xFFFF, o, 0)
    body = tl + refs
    rmap = bytes(16) + bytes(8) + struct.pack(">HH", 28, 28 + len(body)) + body
    hdr = struct.pack(">IIII", 256, 256 + len(data), len(data), len(rmap))
    return hdr + bytes(240) + data + hdr + rmap[16:]


class VolumeTests(unittest.TestCase):
    def test_streamed_classic_catalog_matches_existing_leaf_records(self):
        raw = build_hfs()
        v = volume.open_volume(io.BytesIO(raw), len(raw))
        data, size, first = v._tree_nodes("catalog")
        existing = [v._parse_catalog(node, off) for node, off in v._leaf_records(data, size, first)]
        streamed = [v._parse_catalog(node, off) for node, off in v._tree_leaf_records("catalog")]
        self.assertEqual(streamed, existing)

    def test_fragmented_range_crosses_extents_without_reading_the_fork(self):
        class SizedReads(io.BytesIO):
            def read(self, size=-1):
                if not 0 <= size <= 3:
                    raise AssertionError("range reader requested unrelated fork data")
                return super().read(size)
        v = volume.HFSPlusVolume.__new__(volume.HFSPlusVolume)
        volume._BTreeVolume.__init__(v, SizedReads(b"__abc__defg__"), 0, 13)
        v.block_size = 1
        self.assertEqual(v._read_extent_range([(2, 3), (7, 4)], 7, 1, 5), b"bcdef")
        for offset, length in ((-1, 1), (6, 2), (8, 0)):
            with self.assertRaises(volume.VolumeError):
                v._read_extent_range([(2, 3), (7, 4)], 7, offset, length)
        with self.assertRaises(volume.VolumeError):
            v._read_extent_range([(20, 3)], 3, 0, 1)

    def test_streamed_node_rejects_a_short_underlying_read(self):
        raw = build_hfs()
        class ShortLeaf(io.BytesIO):
            def read(self, size=-1):
                data = super().read(size)
                return data[:-1] if size == BLOCK else data
        v = volume.open_volume(ShortLeaf(raw), len(raw))
        with self.assertRaises(volume.VolumeError):
            list(v._tree_leaf_records("catalog"))

    def test_hfs_entries_and_forks(self):
        raw = build_hfs()
        v = volume.open_volume(io.BytesIO(raw), len(raw))
        self.assertEqual((v.kind, v.name), ("hfs", "Test"))
        entries = v.entries()
        self.assertEqual([e.path for e in entries], [("App",)])
        app = entries[0]
        self.assertEqual((app.type, app.creator, app.flags), (b"APPL", b"TEST", 0x2100))
        self.assertEqual(v.read_fork(app, "data"), b"hello world")
        self.assertEqual(v.read_fork(app, "rsrc"), b"RSRC" * 10)

    def test_partitioned_image(self):
        raw = build_hfs()
        img = bytearray(64 * 512) + raw
        img[0:4] = b"ER\x02\x00"
        for i, (t, start, count) in enumerate(((b"Apple_partition_map", 1, 63), (b"Apple_HFS", 64, len(raw) // 512)),
                                              start=1):
            img[512 * i:512 * i + 80] = (b"PM\0\0" + struct.pack(">III", 2, start, count) + b"p".ljust(32, b"\0")
                                         + t.ljust(32, b"\0"))
        f = io.BytesIO(bytes(img))
        self.assertEqual([p.type for p in volume.partitions(f, len(img))], ["Apple_partition_map", "Apple_HFS"])
        self.assertEqual(volume.find_volumes(f, len(img)), [(64 * 512, len(raw), "hfs")])
        self.assertEqual(volume.open_volume(f, len(img)).entries()[0].path, ("App",))


class ImageTests(unittest.TestCase):
    def test_dc42_checksum_and_unwrap(self):
        raw = build_hfs()
        blob = dc42(raw)
        self.assertTrue(diskimage.is_dc42(blob))
        out, info = diskimage.unwrap(blob)
        self.assertEqual(out, raw)
        self.assertEqual(info["container"], "dc42")
        bad = bytearray(blob)
        bad[84 + 5000] ^= 1
        with self.assertRaises(diskimage.ImageError):
            diskimage.unwrap(bytes(bad))

    def test_adc(self):
        # literal "abcd"; 2-byte match: 3 bytes from distance 4; 3-byte match: 5 bytes from distance 7
        src = bytes([0x83]) + b"abcd" + bytes([0x00, 0x03]) + bytes([0x41, 0x00, 0x06])
        self.assertEqual(diskimage.adc_decompress(src, 12), b"abcdabcabcda")
        with self.assertRaises(diskimage.ImageError):
            diskimage.adc_decompress(bytes([0x00, 0x09]), 3)

    def test_ndif_raw_zero_and_adc_chunks(self):
        raw = build_hfs()
        sectors = len(raw) // 512
        first = raw[:4096]
        lits = b"".join(bytes([0x80 + len(c) - 1]) + c for c in (first[i:i + 128] for i in range(0, 4096, 128)))
        rest = raw[4096:]
        zero_from = (len(rest.rstrip(b"\0")) + 511) // 512 * 512
        body = lits + rest[:zero_from]
        chunks = [(0, 0x83, 0, len(lits)), (8, 0x02, len(lits), zero_from),
                  (8 + zero_from // 512, 0x00, 0, 0), (sectors, 0xFF, 0, 0)]
        bcem = struct.pack(">HH", 10, 0) + (bytes([4]) + b"Test").ljust(64, b"\0") + struct.pack(">I", sectors)
        bcem = bcem.ljust(124, b"\0") + struct.pack(">I", len(chunks))
        for start, t, off, ln in chunks:
            bcem += struct.pack(">III", (start << 8) | t, off, ln)
        rsrc = build_rsrc([(b"bcem", 128, bcem)])
        out, info = diskimage.unwrap(body, rsrc, (b"dimg", b"ddsk"))
        self.assertEqual(out, raw)
        self.assertEqual(info["chunk_types"], {"0x00": 1, "0x02": 1, "0x83": 1})


class TreeAndExpandTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="rbapps-")
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)

    def test_expand_disk_image_to_tree(self):
        src = self.root / "floppy.image"
        src.write_bytes(dc42(build_hfs()))
        m = expand.expand(src, self.root / "t")
        self.assertEqual(m["source"]["container"], "dc42")
        self.assertEqual(m["top"], ["Test"])          # a volume with loose files is wrapped in its name
        t = tree.Tree(self.root / "t")
        app = next(e for e in t.entries if e["path"] == ["Test", "App"])
        self.assertEqual((app["type"], app["creator"]), ("APPL", "TEST"))
        self.assertEqual(app["flags"], 0x2000)        # kHasBeenInited cleared, bundle bit kept
        self.assertEqual(t.read_data(app), b"hello world")
        self.assertEqual(t.read_rsrc(app), b"RSRC" * 10)
        self.assertEqual(t.verify(), [])
        t.data_path(app).write_bytes(b"tampered")
        self.assertTrue(t.verify())
        info = expand.identify(src)
        self.assertEqual((info["kind"], info["format"]), ("disk-image", "dc42"))

    @unittest.skipUnless(shutil.which("unar") and shutil.which("lsar"), "needs unar/lsar (Debian package unar)")
    def test_expand_macbinary_with_unar(self):
        data, rsrc = b"data fork bytes", build_rsrc([(b"vers", 1, b"\x01\x00")])
        name = b"Sim City Classic"
        hdr = bytearray(128)
        hdr[1] = len(name)
        hdr[2:2 + len(name)] = name
        hdr[65:73] = b"APPLMCRP"
        hdr[73] = 0x21                      # Finder flags, high byte (hasBundle, hasBeenInited)
        struct.pack_into(">II", hdr, 83, len(data), len(rsrc))
        hdr[122] = hdr[123] = 129
        struct.pack_into(">H", hdr, 124, binascii.crc_hqx(bytes(hdr[:124]), 0))
        pad = lambda b: b + bytes(-len(b) % 128)
        src = self.root / "app.bin"
        src.write_bytes(bytes(hdr) + pad(data) + pad(rsrc))
        m = expand.expand(src, self.root / "t")
        t = tree.Tree(self.root / "t")
        e = next(e for e in t.entries if e["kind"] == "file")
        self.assertEqual(e["path"], ["Sim City Classic"])
        self.assertEqual((e["type"], e["creator"], e["flags"]), ("APPL", "MCRP", 0x2000))
        self.assertEqual((t.read_data(e), t.read_rsrc(e)), (data, rsrc))
        self.assertIn("lsar", m["source"]["verified_against"])

    def test_tree_names(self):
        w = tree.TreeWriter(self.root / "n", {"kind": "test"})
        w.file(["A/B ƒ", "Doc"], b"x", b"", tree.file_finder_info(b"TEXT", b"ttxt", 0))
        m = w.finish()
        self.assertTrue((self.root / "n/data/A:B ƒ/Doc").is_file())
        self.assertEqual([e["path"] for e in m["entries"]], [["A/B ƒ"], ["A/B ƒ", "Doc"]])
        for bad in ("a:b", "", "x" * 32, "日本"):
            with self.assertRaises(tree.TreeError):
                tree.check_name(bad)

    def test_lsar_slash_names(self):
        # XADMaster lists a Mac '/' inside a name as ':' (theme collections:
        # "GlassBox w/ Icons", "http://..." clippings); unar writes it as '_'.
        items = expand._parse_lsar({"lsarContents": [
            {"XADFileName": "Themes/GlassBox w: Icons", "XADIsResourceFork": 1, "XADFileSize": 7},
            {"XADFileName": "Themes/GlassBox w: Icons", "XADFileSize": 0}]})
        self.assertIn(("Themes", "GlassBox w/ Icons"), items)
        self.assertEqual(items[("Themes", "GlassBox w/ Icons")]["rsrc"], 7)

    def test_installer_placement_does_not_replace_payload(self):
        panel = {"XADFileName": "Panel", "XADIsResourceFork": 1, "XADFileSize": 7,
                 "XADFileType": int.from_bytes(b"cdev", "big"), "XADIndex": 0}
        folder = {"XADFileName": "Modules", "XADIsDirectory": 1, "XADIndex": 1}
        directive = {"XADFileName": "Panel", "XADFileSize": 0,
                     "XADFileType": int.from_bytes(b"STal", "big"),
                     "XADFileCreator": int.from_bytes(b"STin", "big"), "XADIndex": 2}
        listing, skipped = expand._installer_aliases({"lsarContents": [panel, folder, directive]})
        items = expand._parse_lsar(listing)
        self.assertEqual(items[("Panel",)]["type"], b"cdev")
        self.assertEqual(items[("Panel",)]["rsrc"], 7)
        self.assertTrue(items[("Modules",)]["dir"])
        self.assertEqual(skipped, [directive])

    def test_installer_filter_keeps_real_and_unknown_files(self):
        entries = [{"XADFileName": "Alias", "XADFileSize": 0},
                   {"XADFileName": "Data", "XADFileSize": 8,
                    "XADFileType": int.from_bytes(b"STal", "big"),
                    "XADFileCreator": int.from_bytes(b"STin", "big")}]
        listing, skipped = expand._installer_aliases({"lsarContents": entries})
        self.assertEqual(listing["lsarContents"], entries)
        self.assertEqual(skipped, [])

    def test_appledouble(self):
        fi = b"APPLMCRP" + struct.pack(">H", 0x2100) + bytes(22)
        rsrc = b"R" * 33
        hdr = struct.pack(">II", 0x00051607, 0x00020000) + bytes(16) + struct.pack(">H", 2)
        off = len(hdr) + 24
        blob = hdr + struct.pack(">III", 9, off, 32) + struct.pack(">III", 2, off + 32, len(rsrc)) + fi + rsrc
        ad = resfork.parse_appledouble(blob)
        self.assertEqual((ad[9], ad[2]), (fi, rsrc))
        with self.assertRaises(resfork.ForkError):
            resfork.parse_appledouble(b"not appledouble at all......")

    def test_resource_parser(self):
        fork = build_rsrc([(b"vers", 1, b"\x01\x00"), (b"STR ", 128, b"\x02hi")])
        got = [(t, i, d) for t, i, _n, _p, d in resfork.parse_resource_fork(fork)]
        self.assertEqual(got, [("vers", 1, b"\x01\x00"), ("STR ", 128, b"\x02hi")])


if __name__ == "__main__":
    unittest.main()
