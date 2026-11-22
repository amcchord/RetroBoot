"""Shared folder from the web: Mac file formats, names, the share service and its API.

Golden vectors: GUEST_RDUMP / GUEST_IDUMP were written by Mac OS 9.2.1's
SimpleText through classicvirtio on the lab builder (FILE-01 evidence); the
BinHex, MacBinary, AppleSingle and StuffIt samples come from macsamples.py and
were checked against The Unarchiver (web/reports/webfiles-2026-10-11.md).
"""
import asyncio
import io
import os
import shutil
import stat
import struct
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

import macsamples as ms
from retroboot import macfiles as mf
from retroboot.share import MAX_FILE, Options, SharedFolder, ShareError, cnid_for, cnids_for, share_cnid, split_path

REPO = Path(__file__).resolve().parents[2]
GUEST_RDUMP = (b"data 'styl' (128) {\n"
               b'\t$"0001 0000 0000 0010 000C 0003 0000 000C"            /* ................ */\n'
               b'\t$"0000 0000 0000"                                     /* ...... */\n'
               b"};\n\n")
GUEST_IDUMP = b"TEXTttxt\nkHasBeenInited\n"
HAVE_UNAR = shutil.which("unar") is not None and shutil.which("lsar") is not None


async def _chunks(data: bytes, size: int = 7):
    for i in range(0, len(data), size):
        yield data[i:i + size]


def _macbinary(name: bytes, t: bytes, c: bytes, flags: int, data: bytes, rsrc: bytes) -> bytes:
    pad = lambda b: b + bytes(-len(b) % 128)  # noqa: E731
    return mf.macbinary_header_bytes(name, t, c, flags, len(data), len(rsrc)) + pad(data) + (pad(rsrc) if rsrc else b"")


class NameTests(unittest.TestCase):
    def test_mac_roman_matches_classicvirtio(self):
        # unicode.c: 53 decomposed letters are accepted before ASCII, every Mac OS Roman
        # character in composed form after it; utf8char() writes NFD.
        self.assertEqual(len(mf._NFD3), 53)
        for c in range(256):
            u = bytes([c]).decode("mac_roman")
            for form in ("NFC", "NFD"):
                host = unicodedata.normalize(form, u)
                if c in (0x2F, 0x3A, 0):
                    continue
                self.assertEqual(mf.mac_bytes(host), bytes([c]), (c, form))
            if c not in (0, 0x2F, 0x3A):
                self.assertEqual(mf.host_name(bytes([c])), unicodedata.normalize("NFD", u))
        self.assertEqual(mf.mac_bytes("a:b"), b"a/b")             # host ':' is Mac '/'
        self.assertEqual(mf.host_name(b"a/b"), "a:b")
        self.assertEqual(mf.mac_bytes("x" * 31), b"x" * 31)
        self.assertIsNone(mf.mac_bytes("x" * 32))                  # mr31name: too long -> invisible
        self.assertIsNone(mf.mac_bytes("snow ☃"))             # not in Mac OS Roman
        self.assertIsNone(mf.mac_bytes(b"bad\xff".decode("utf-8", "surrogateescape")))
        self.assertEqual(mf.host_name(b"Caf\x8e").encode(), b"Cafe\xcc\x81")

    def test_shown_names_match_classicvirtio_6(self):
        """catalog.c CatalogMacName: what Mac OS shows for every host name.  The expected
        names were listed by the Finder of Mac OS 9.2.2 with classicvirtio-6 in the lab
        (emulator/reports/sharenames-20261013.md), for these inodes' CNIDs."""
        nfd = lambda t: unicodedata.normalize("NFD", t)       # noqa: E731
        nfc = lambda t: unicodedata.normalize("NFC", t)       # noqa: E731
        cnid = 0x4058EB67
        shown = lambda h, sib=None: mf.shown_name(h, cnid, sib)  # noqa: E731
        # exactly 31 characters: shown as they are (the fleet7 speedo reports)
        self.assertEqual(shown("speedo-n5105-hostd-c-210137.txt"), b"speedo-n5105-hostd-c-210137.txt")
        self.assertEqual(shown(nfd("é" * 31)), b"\x8e" * 31)
        # longer: shortened, '#' and the CNID's low 30 bits, a short extension kept
        self.assertEqual(shown("speedo-n5105-hostd-c-2101370.txt"), b"speedo-n5105-hostd-c#58EB67.txt")
        self.assertEqual(shown("A rather long report name for testing.txt"), b"A rather long report#58EB67.txt")
        self.assertEqual(shown("abcdefghijklmnopqrstuvwxyzABCDEF"), b"abcdefghijklmnopqrstuvwx#58EB67")
        self.assertEqual(shown("x" * 40 + ".tar.gzip"), b"x" * 19 + b"#58EB67.gzip")
        self.assertEqual(shown("y" * 40 + ".longext"), b"y" * 24 + b"#58EB67")
        self.assertEqual(len(shown("z" * 93)), 31)
        # characters Mac OS Roman lacks: one '_' each, whatever their UTF-8 length
        self.assertEqual(shown("snow ☃ man.txt"), b"snow _ man#58EB67.txt")
        self.assertEqual(shown("smile \U0001F600.txt"), b"smile _#58EB67.txt")
        self.assertEqual(shown("日本語.txt"), b"___#58EB67.txt")
        self.assertEqual(shown(b"bad\xff\xfe.txt".decode("utf-8", "surrogateescape")), b"bad__#58EB67.txt")
        # ':' is '/', control characters and spaces are kept
        self.assertEqual(shown("a:b colon.txt"), b"a/b colon.txt")
        self.assertEqual(shown("trailing space "), b"trailing space ")
        self.assertEqual(shown("tab\there.txt"), b"tab\there.txt")
        # decomposed (as the guest writes) or precomposed while no decomposed sibling exists
        self.assertEqual(shown(nfd("café.txt")), b"caf\x8e.txt")
        self.assertEqual(shown(nfc("café.txt"), lambda c: False), b"caf\x8e.txt")
        self.assertEqual(shown(nfc("résumé.txt"), lambda c: c == nfd("résumé.txt")), b"r\x8esum\x8e#58EB67.txt")
        mixed = nfd("ré") + nfc("sumé.txt")                    # neither spelling: shortened
        self.assertEqual(shown(mixed, lambda c: False), b"r\x8esum\x8e#58EB67.txt")
        # case is Mac OS's business: both spellings shown as they are
        self.assertEqual((shown("Dup Case.txt"), shown("dup case.txt")), (b"Dup Case.txt", b"dup case.txt"))
        # classicvirtio's MAXNAME: longer host names stay invisible
        self.assertIsNone(shown("w" * 94))
        self.assertTrue(mf.visible("w" * 93) and not mf.visible("w" * 94) and not mf.visible(".x"))
        self.assertTrue(mf.visible("A rather long report name for testing.txt") and mf.visible("snow ☃"))
        # the CNID classicvirtio sees: QEMU remaps the inode to (ino << 1) | 1
        self.assertEqual(share_cnid(0x2C75B3), 0x4058EB67)
        self.assertIn(share_cnid(0x2C75B3), cnids_for(0x2C75B3))

    def test_safe_names(self):
        self.assertEqual(mf.safe_mac("Read Me.txt"), b"Read Me.txt")
        long = mf.safe_mac("A very long holiday photograph name.jpeg")
        self.assertEqual((long, len(long)), (b"A very long holiday photo\xc9.jpeg", 31))
        self.assertEqual(mf.safe_mac("x" * 40), b"x" * 30 + b"\xc9")
        self.assertEqual(mf.safe_mac(".hidden"), b"_hidden")
        self.assertEqual(mf.safe_mac("notes.rdump"), b"notes_rdump")
        self.assertEqual(mf.safe_mac("a:b.txt"), b"a/b.txt")
        self.assertEqual(mf.safe_mac("tab\there"), b"tab_here")
        self.assertEqual(mf.safe_mac("łódź \U0001F600.txt"), b"_\x97dz _.txt")
        self.assertEqual(mf.safe_mac("“quoted” – café"), b"\xd2quoted\xd3 \xd0 caf\x8e")
        self.assertEqual(mf.safe_mac("   "), b"untitled")
        self.assertEqual(mf.copy_name(b"photo.jpg", 1), b"photo.jpg copy")
        self.assertEqual(mf.copy_name(b"photo.jpg", 3), b"photo.jpg copy 3")
        self.assertEqual(len(mf.copy_name(b"y" * 31, 12)), 31)
        self.assertEqual(mf.safe_mac_bytes(b".Icon:x"), b"_Icon-x")

    def test_typed_names_are_refused_not_altered(self):
        for bad, code in (("", "invalid-name"), ("a:b", "invalid-name"), (".x", "invalid-name"),
                          ("x" * 32, "invalid-name"), ("snow ☃", "invalid-name"), ("a.rdump", "invalid-name")):
            with self.assertRaises(ShareError) as cm:
                SharedFolder.typed_name(bad)
            self.assertEqual((cm.exception.status, cm.exception.code), (422, code), bad)
        self.assertEqual(SharedFolder.typed_name(" Café "), b"Caf\x8e")

    def test_cnids_match_the_guest(self):
        # resource fork cache names classicvirtio wrote for three files on the lab builder
        # (package r6): inode -> cache name
        for ino, name in ((6034526, 0x40B828BD), (6034027, 0x40B824D7), (6033849, 0x40B82373)):
            self.assertIn(name, cnids_for(ino))

    def test_paths_refused(self):
        for bad in ("..", "a/../b", ".retroboot-incoming", ".classicvirtio.nosync.noindex/resforks",
                    "Doc.rdump", "Doc.idump", "x\0y", "/".join(["d"] * 40), "a/" + "b" * 300):
            with self.assertRaises(ShareError, msg=bad) as cm:
                split_path(bad)
            self.assertEqual(cm.exception.status, 400)
        self.assertEqual(split_path("/a//b/"), ["a", "b"])


class FormatTests(unittest.TestCase):
    def test_idump_matches_the_guest(self):
        self.assertEqual(mf.idump(b"TEXT", b"ttxt", 0x0100), GUEST_IDUMP)
        self.assertEqual(mf.parse_idump(GUEST_IDUMP), (b"TEXT", b"ttxt", 0x0100))
        self.assertEqual(mf.idump(bytes(4), bytes(4), 0), b"????????\n")
        self.assertEqual(mf.parse_idump(b"????????\n"), (bytes(4), bytes(4), 0))
        for bit, _ in mf.FLAG_NAMES:
            self.assertEqual(mf.parse_idump(mf.idump(b"APPL", b"abcd", bit))[2], bit)
        self.assertEqual(mf.parse_idump(mf.idump(b"APPL", b"abcd", 0x2000 | 0x4000 | 0x0C))[2], 0x600C)

    def test_derez_matches_the_guest(self):
        res = mf.rez(GUEST_RDUMP)
        self.assertEqual([(r.type, r.id, r.name, r.attrs, len(r.data)) for r in res], [(b"styl", 128, None, 0, 22)])
        self.assertEqual(mf.derez_bytes(res), GUEST_RDUMP)

    def test_derez_details(self):
        r = [mf.Resource(b"TE'X", -1, b'say "\xa9" \\', 0x20 | 0x10, b"*/x*/" + bytes([0x2A, 0x01, 0x2F]))]
        text = mf.derez_bytes(r)
        self.assertTrue(text.startswith(b"data 'TE\\'X' (-1, \"say \\\"\xa9\\\" \\\\\", purgeable, locked) {\n"))
        self.assertIn(b"/* *.x*.*.. */", text)              # DeRez: never "*/" inside, and a control char keeps it
        self.assertEqual(mf.rez(text)[0].data, r[0].data)
        self.assertEqual(mf.rez(text)[0].name, r[0].name)
        self.assertIn(b", $83)", mf.derez_bytes([mf.Resource(b"abcd", 1, None, 0x83, b"")]))
        # the guest writes \0xHH escapes; they mean the byte (classicvirtio's own Rez misreads them)
        g = b"data '\\0x8Fabc' (5, \"\\0xA9 \\n\\r\\?\") {\n};\n"
        self.assertEqual((mf.rez(g)[0].type, mf.rez(g)[0].name), (b"\x8fabc", b"\xa9 \r\n\x7f"))
        with self.assertRaises(mf.FormatError):
            mf.rez(b"data 'abc' (1) {};")
        with self.assertRaises(mf.FormatError):
            mf.derez_bytes([mf.Resource(b"abcd", i, None, 0, b"") for i in range(mf.MAX_RESOURCES + 1)])

    def test_fork_layout_matches_rez(self):
        fork = ms.sample_fork()
        doff, moff, dlen, mlen = struct.unpack(">IIII", fork[:16])
        self.assertEqual((doff, moff), (256, 256 + dlen))
        self.assertEqual(len(fork), 256 + dlen + mlen)
        res = mf.parse_fork(fork)
        self.assertEqual([r.type for r in res], [b"ICN#", b"STR ", b"styl"])     # map sorted by type
        self.assertEqual(mf.build_fork(mf.rez(mf.derez_bytes(res))), fork)
        self.assertEqual(mf.parse_fork(b""), [])
        for bad in (b"x" * 10, struct.pack(">IIII", 256, 9999, 0, 30) + bytes(300)):
            with self.assertRaises(mf.FormatError):
                mf.parse_fork(bad)

    def test_macbinary(self):
        fork = ms.sample_fork()
        data = b"hello"
        blob = _macbinary(b"Doc", b"TEXT", b"ttxt", 0x0100, data, fork)
        h = mf.macbinary_header(blob[:128], len(blob))
        self.assertEqual((h["level"], h["name"], h["type"], h["data_length"], h["rsrc_length"]),
                         (3, b"Doc", b"TEXT", 5, len(fork)))
        # MacBinary I: no CRC, zeroed tail
        one = bytearray(blob[:128])
        one[99:128] = bytes(29)
        self.assertEqual(mf.macbinary_header(bytes(one), len(blob))["level"], 1)
        # strict: random data, wrong length, bad CRC with a non-zero tail, ':' in the name
        self.assertIsNone(mf.macbinary_header(os.urandom(128), 4096))
        self.assertIsNone(mf.macbinary_header(blob[:128], len(blob) + 4096))
        bad = bytearray(blob[:128])
        bad[124] ^= 1
        self.assertIsNone(mf.macbinary_header(bytes(bad), len(blob)))
        colon = bytearray(blob[:128])
        colon[3] = 0x3A
        self.assertIsNone(mf.macbinary_header(bytes(colon), len(blob)))
        out = b"".join(mf.macbinary_stream(b"Doc", b"TEXT", b"ttxt", 0x0100, io.BytesIO(data), 5, fork))
        self.assertEqual(len(out), mf.macbinary_length(5, len(fork)))
        self.assertEqual(out[128:133], data)

    def test_binhex(self):
        fork = ms.sample_fork()
        data = b"BinHex " * 40 + b"\x90" * 9 + b"\x00" * 300 + b"\x90\x00\x90"
        enc = mf.binhex_encode(b"Hex Doc", b"APPL", b"ttxt", 0x2100, data, fork)
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td)
        src = os.path.join(td, "x.hqx")
        with open(src, "wb") as f:
            f.write(b"From: someone\r\n\r\n" + enc)
        self.assertEqual(mf.sniff(src), "binhex")
        for block in (256 * 1024, 5, 3, 1):          # runs and 0x90 markers across read boundaries
            old = mf.HQX_BLOCK
            mf.HQX_BLOCK = block
            try:
                m = mf.binhex_decode(src, os.path.join(td, f"d{block}"), 1 << 20)
            finally:
                mf.HQX_BLOCK = old
            with open(m.data_path, "rb") as f:
                self.assertEqual(f.read(), data, block)
            self.assertEqual((m.name, m.type, m.creator, m.flags, m.rsrc), (b"Hex Doc", b"APPL", b"ttxt", 0x2100, fork))
        broken = enc.replace(enc[200:201], b"!" if enc[200:201] != b"!" else b"#")
        with open(src, "wb") as f:
            f.write(broken)
        with self.assertRaises(mf.FormatError):
            mf.binhex_decode(src, os.path.join(td, "e"), 1 << 20)

    def test_applesingle(self):
        fork = ms.sample_fork()
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td)
        p = os.path.join(td, "x.as")
        with open(p, "wb") as f:
            f.write(mf.applesingle_encode(b"AS Doc", b"TEXT", b"ttxt", 0x0101, b"data!", fork))
        self.assertEqual(mf.sniff(p), "applesingle")
        m = mf.apple_decode(p)
        self.assertEqual((m.name, m.type, m.flags, m.rsrc, m.data_length), (b"AS Doc", b"TEXT", 0x0100, fork, 5))
        with open(p, "wb") as f:
            f.write(mf.applesingle_encode(b"AS Doc", b"TEXT", b"ttxt", 0, b"", fork, double=True))
        self.assertEqual(mf.sniff(p), "appledouble")

    def test_text_conversion(self):
        cases = [(b"a\nb\r\nc\rd\n", b"a\rb\rc\rd\r"),
                 ("café — \U0001F600\n".encode(), b"caf\x8e \xd1 ?\r"),
                 (b"\xef\xbb\xbfBOM\n", b"BOM\r"),
                 (b"latin \xe9\n", b"latin \xe9\r")]                   # not UTF-8: only line endings
        for src, want in cases:
            for chunk in (1, 2, 1 << 20):
                out = io.BytesIO()
                mf.convert_text(io.BytesIO(src), out, len(src), chunk)
                self.assertEqual(out.getvalue(), want, (src, chunk))

    def test_type_creator_table(self):
        want = {"a.txt": (b"TEXT", b"ttxt"), "a.JPG": (b"JPEG", b"ogle"), "a.gif": (b"GIFf", b"ogle"),
                "a.pdf": (b"PDF ", b"CARO"), "a.mp3": (b"MPG3", b"TVOD"), "a.mov": (b"MooV", b"TVOD"),
                "a.html": (b"TEXT", b"MSIE"), "a.sit": (b"SIT!", b"SITx"), "a.hqx": (b"TEXT", b"SITx"),
                "a.png": (b"PNGf", b"ogle"), "a.zip": (b"ZIP ", b"SITx"),
                "a.iso": (b"GImg", b"VCDM"), "a.TOAST": (b"GImg", b"VCDM"),
                "a.cdr": (b"GImg", b"VCDM")}
        for name, codes in want.items():
            self.assertEqual(mf.codes_for(name), codes, name)
        self.assertIsNone(mf.codes_for("noextension"))
        self.assertIsNone(mf.codes_for("a.weird"))
        self.assertEqual(mf.kind_for("x", b"APPL", b"ttxt"), ("application program", "application"))
        self.assertEqual(mf.kind_for("x.html", b"TEXT", b"MSIE"), ("Web page", "text"))
        self.assertTrue(mf.is_text_name("x.md") and not mf.is_text_name("x.hqx") and not mf.is_text_name("x.jpg"))


class ShareTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.td, True)
        self.root = Path(self.td) / "share"
        self.root.mkdir()
        self.open_set = None
        self.share = SharedFolder(self.root, apps_lib=REPO / "apps", guest_pids=lambda: None,
                                  guest_open=lambda: self.open_set)

    async def up(self, name, data, rel="", relpath="", **kw):
        return await self.share.receive(_chunks(data), len(data), rel, name, relpath, Options(**kw))

    def names(self, rel=""):
        return sorted(os.listdir(self.root / rel))

    async def test_plain_file_gets_type_creator_and_guest_style_name(self):
        r = await self.up("Café notes.txt", b"hello\n", mtime=1_000_000_000)
        self.assertEqual(r["items"][0]["mac_name"], "Café notes.txt")
        host = unicodedata.normalize("NFD", "Café notes.txt")
        self.assertEqual(self.names(), sorted([".retroboot-incoming", host, host + ".idump"]))
        self.assertEqual((self.root / (host + ".idump")).read_bytes(), b"TEXTttxt\n")
        self.assertEqual((self.root / host).read_bytes(), b"hello\n")              # text untouched by default
        self.assertEqual(int((self.root / host).stat().st_mtime), 1_000_000_000)   # the browser's date
        item = self.share.list("")["items"][0]
        self.assertEqual((item["type"], item["creator"], item["kind_label"], item["icon"], item["download"]),
                         ("TEXT", "ttxt", "SimpleText text document", "text", "data"))
        r = await self.up("blob", b"\x00\x01")
        self.assertFalse((self.root / "blob.idump").exists())      # unknown: no Finder info, like a PC file

    async def test_disc_image_uploads_open_virtual_cd_without_changing_payload(self):
        payload = b"\x00disc image bytes\xff"
        for name in ("Mount.iso", "Mount.TOAST"):
            await self.up(name, payload)
            self.assertEqual((self.root / name).read_bytes(), payload)
            self.assertEqual(mf.parse_idump((self.root / (name + ".idump")).read_bytes())[:2],
                             (b"GImg", b"VCDM"))
        # A wrapped image's explicit application association remains authoritative.
        await self.up("Original.bin", _macbinary(b"Original.iso", b"BINA", b"CDr3", 0, payload, b""))
        self.assertEqual(mf.parse_idump((self.root / "Original.iso.idump").read_bytes())[:2],
                         (b"BINA", b"CDr3"))

    async def test_names_collisions_and_replace(self):
        await self.up("Photo.jpg", b"one")
        r = await self.up("PHOTO.JPG", b"two")                       # Mac OS ignores case
        self.assertEqual(r["items"][0]["mac_name"], "PHOTO.JPG copy")
        self.assertIn("already exists", " ".join(r["notes"]))
        r = await self.up("Photo.jpg", b"three")
        self.assertEqual(r["items"][0]["mac_name"], "Photo.jpg copy 2")
        r = await self.up("x" * 40 + ".gif", b"g")
        self.assertEqual(len(mf.mac_bytes(r["items"][0]["name"])), 31)
        # replace: a file with a resource fork replaced by a plain file loses the stale sidecar
        fork = ms.sample_fork()
        await self.up("Doc.bin", _macbinary(b"Doc", b"TEXT", b"ttxt", 0, b"old", fork))
        self.assertTrue((self.root / "Doc.rdump").exists())
        r = await self.up("Doc", b"new", conflict="replace")
        self.assertEqual(r["items"][0]["name"], "Doc")
        self.assertEqual((self.root / "Doc").read_bytes(), b"new")
        self.assertFalse((self.root / "Doc.rdump").exists() or (self.root / "Doc.idump").exists())
        # a folder is never replaced by a file
        os.mkdir(self.root / "Box")
        r = await self.up("Box", b"file", conflict="replace")
        self.assertEqual(r["items"][0]["mac_name"], "Box copy")

    async def test_encoded_uploads_become_real_forks(self):
        fork = ms.sample_fork()
        data = b"Hello\r" * 20
        for name, blob in (("Mb Doc.bin", _macbinary(b"Mb Doc", b"TEXT", b"ttxt", 0x0500, data, fork)),
                           ("Hq Doc.hqx", mf.binhex_encode(b"Hq Doc", b"TEXT", b"ttxt", 0x0500, data, fork)),
                           ("As Doc.as", mf.applesingle_encode(b"As Doc", b"TEXT", b"ttxt", 0x0500, data, fork))):
            r = await self.up(name, blob)
            it = r["items"][0]
            host = it["name"]
            self.assertEqual(host, name.rsplit(".", 1)[0])
            self.assertEqual((self.root / host).read_bytes(), data)
            self.assertEqual(mf.parse_idump((self.root / (host + ".idump")).read_bytes()),
                             (b"TEXT", b"ttxt", 0x0400))               # kHasBeenInited cleared for the Finder
            self.assertEqual(mf.build_fork(mf.rez((self.root / (host + ".rdump")).read_bytes())), fork)
            self.assertEqual((it["rsrc_bytes"], it["download"]), (len(fork), "macbinary"))
            d = self.share.download(host)
            blob2 = b"".join(d.chunks())
            d.close()
            self.assertEqual(d.filename, host + ".bin")
            h = mf.macbinary_header(blob2[:128], len(blob2))
            self.assertEqual((h["name"], h["type"], h["creator"]), (host.encode(), b"TEXT", b"ttxt"))
            self.assertEqual(blob2[h["data_offset"]:h["data_offset"] + h["data_length"]], data)
            self.assertEqual(blob2[h["rsrc_offset"]:h["rsrc_offset"] + h["rsrc_length"]], fork)
            d = self.share.download(host, "data")
            self.assertEqual((b"".join(d.chunks()), d.filename), (data, host))
            d.close()

    async def test_appledouble_header_file_joins_its_sibling(self):
        fork = ms.sample_fork()
        await self.up("Song.aiff", b"FORM....")
        r = await self.up("._Song.aiff", mf.applesingle_encode(b"Song.aiff", b"AIFF", b"SNDM", 0, b"", fork, True))
        self.assertEqual(r["source"], "appledouble")
        self.assertEqual(self.names().count("Song.aiff"), 1)
        self.assertEqual(mf.parse_idump((self.root / "Song.aiff.idump").read_bytes())[:2], (b"AIFF", b"SNDM"))
        self.assertTrue((self.root / "Song.aiff.rdump").exists())

    async def test_folder_drops_build_paths(self):
        top = self.share.mkdir("", "Photos", "rename")
        self.assertEqual(top["name"], "Photos")
        again = self.share.mkdir("", "photos", "rename")
        self.assertEqual(again["mac_name"], "photos copy")
        await self.up("a.jpg", b"1", rel="Photos", relpath="2024/Summer")
        await self.up("b.jpg", b"2", rel="Photos", relpath="2024/Summer")
        self.assertEqual(self.names("Photos/2024/Summer"), ["a.jpg", "a.jpg.idump", "b.jpg", "b.jpg.idump"])
        with self.assertRaises(ShareError) as cm:
            self.share.mkdir("", "Photos")
        self.assertEqual(cm.exception.code, "name-exists")
        empty = self.share.mkdir("Photos", "Empty \u2603", "fail", sanitize=True, relpath="2024")   # dropped folder
        self.assertEqual((empty["mac_name"], empty["path"]), ("Empty _", "Photos/2024/Empty _"))
        with self.assertRaises(ShareError):
            self.share.mkdir("Photos", "x", relpath="../..")

    async def test_text_conversion_is_opt_in(self):
        await self.up("unix.txt", b"a\nb\n")
        self.assertEqual((self.root / "unix.txt").read_bytes(), b"a\nb\n")
        r = await self.up("conv.txt", "café\r\nx\n".encode(), convert_text=True)
        self.assertEqual((self.root / "conv.txt").read_bytes(), b"caf\x8e\rx\r")
        self.assertIn("Converted", " ".join(r["notes"]))
        await self.up("pic.jpg", b"a\nb", convert_text=True)
        self.assertEqual((self.root / "pic.jpg").read_bytes(), b"a\nb")              # only text files

    async def test_stuffit_left_as_is_by_default(self):
        sit = ms.make_sit([("file", b"Inner", b"TEXT", b"ttxt", 0, b"x", b"")])
        r = await self.up("Archive.sit", sit)
        self.assertEqual(r["items"][0]["name"], "Archive.sit")
        self.assertEqual(mf.parse_idump((self.root / "Archive.sit.idump").read_bytes())[:2], (b"SIT!", b"SITx"))
        self.assertIn("StuffIt Expander", " ".join(r["notes"]))

    @unittest.skipUnless(HAVE_UNAR, "unar/lsar not installed")
    async def test_stuffit_expanded_on_request(self):
        fork = ms.sample_fork()
        sit = ms.make_sit([("folder", b"Sit Folder", [("file", b"Caf\x8e Doc", b"TEXT", b"ttxt", 0x0500, b"doc", fork),
                                                      ("file", b"Plain", b"BINA", b"????", 0, b"plain", b"")])])
        r = await self.up("Archive.sit", sit, expand=True)
        self.assertEqual([i["name"] for i in r["items"]], ["Sit Folder"])
        doc = unicodedata.normalize("NFD", "Café Doc")
        self.assertEqual(self.names("Sit Folder"), sorted([doc, doc + ".idump", doc + ".rdump", "Plain", "Plain.idump"]))
        self.assertEqual(mf.build_fork(mf.rez((self.root / "Sit Folder" / (doc + ".rdump")).read_bytes())), fork)
        self.assertEqual(mf.parse_idump((self.root / "Sit Folder" / (doc + ".idump")).read_bytes()),
                         (b"TEXT", b"ttxt", 0x0400))
        # two top-level items: wrapped in a folder named after the archive, like StuffIt Expander
        sit2 = ms.make_sit([("file", b"One", b"TEXT", b"ttxt", 0, b"1", b""), ("file", b"Two", b"TEXT", b"ttxt", 0, b"2", b"")])
        r = await self.up("Pair.sit", sit2, expand=True)
        self.assertEqual(r["items"][0]["name"], "Pair")
        self.assertEqual(self.names("Pair"), ["One", "One.idump", "Two", "Two.idump"])
        # a BinHex-wrapped archive is decoded, then expanded
        hqx = mf.binhex_encode(b"Wrapped.sit", b"SIT!", b"SITx", 0, sit2, b"")
        r = await self.up("Wrapped.sit.hqx", hqx, expand=True)
        self.assertEqual(r["items"][0]["name"], "Wrapped")

    async def test_limits(self):
        consumed = []

        async def never():
            consumed.append(1)
            yield b"x"
        with self.assertRaises(ShareError) as cm:
            await self.share.receive(never(), MAX_FILE + 1, "", "huge.iso", "", Options())
        self.assertEqual((cm.exception.status, cm.exception.code, consumed), (413, "too-large", []))
        self.assertIn("2 GB", cm.exception.message)
        self.share.reserve = 1 << 62
        with self.assertRaises(ShareError) as cm:
            await self.up("x.txt", b"x")
        self.assertEqual((cm.exception.status, cm.exception.code), (507, "no-space"))
        self.share.reserve = 0
        with self.assertRaises(ShareError) as cm:
            await self.share.receive(_chunks(b"short"), 10, "", "x.txt", "", Options())
        self.assertEqual(cm.exception.code, "upload-incomplete")
        self.assertEqual(os.listdir(self.root / ".retroboot-incoming"), [])            # nothing left behind
        self.assertNotIn("x.txt", self.names())
        with self.assertRaises(ShareError) as cm:
            await self.up("x.txt", b"x", rel="missing")
        self.assertEqual(cm.exception.status, 404)

    async def test_traversal_links_and_devices(self):
        await self.up("ok.txt", b"ok")
        os.symlink("/etc", self.root / "etc-link")
        os.symlink("/etc/passwd", self.root / "passwd-link")
        os.mkfifo(self.root / "fifo")
        os.mkdir(self.root / ".hidden")
        listed = [i["name"] for i in self.share.list("")["items"]]
        self.assertEqual(listed, ["ok.txt"])
        for rel in ("etc-link", "passwd-link", "fifo"):
            with self.assertRaises(ShareError) as cm:
                self.share.download(rel)
            self.assertEqual(cm.exception.status, 404, rel)
        for call in (lambda: self.share.list("etc-link"), lambda: self.share.list("../"),
                     lambda: self.share.download("../share/ok.txt"), lambda: self.share.download("ok.txt.idump"),
                     lambda: self.share.rename("../x", "y"), lambda: self.share.delete(".hidden"),
                     lambda: self.share.mkdir(".hidden", "x")):
            with self.assertRaises(ShareError):
                call()
        with self.assertRaises(ShareError):
            await self.up("x.txt", b"x", rel="etc-link")
        r = await self.up("../../escape.txt", b"x")                       # the name is only a name
        self.assertEqual(r["items"][0]["name"], "escape.txt")
        with self.assertRaises(ShareError) as cm:
            await self.up("y.txt", b"y", relpath="../../evil")
        self.assertEqual(cm.exception.status, 400)
        r = await self.up("y.txt", b"y", relpath=".git/objects")              # hidden folders become visible names
        self.assertTrue((self.root / "_git" / "objects" / "y.txt").exists())
        self.assertFalse(Path(self.td, "evil").exists())
        self.assertTrue(Path("/etc/passwd").exists())

    async def test_open_files_are_protected(self):
        await self.up("busy.txt", b"x")
        st = (self.root / "busy.txt").stat()
        self.open_set = ({(st.st_dev, st.st_ino)}, set())
        self.assertIs(self.share.list("")["items"][0]["open"], True)
        for call in (lambda: self.share.rename("busy.txt", "other"), lambda: self.share.delete("busy.txt")):
            with self.assertRaises(ShareError) as cm:
                call()
            self.assertEqual(cm.exception.code, "file-open")
        with self.assertRaises(ShareError) as cm:
            await self.up("busy.txt", b"y", conflict="replace")
        self.assertEqual(cm.exception.code, "file-open")
        r = await self.up("busy.txt", b"y")                                  # keeping both is fine
        self.assertEqual(r["items"][0]["mac_name"], "busy.txt copy")
        # a resource fork open through classicvirtio's cache counts too, with either qid form
        for qid in (st.st_ino, (st.st_ino << 1) | 1):
            self.open_set = (set(), {cnid_for(qid)})
            self.assertIs(next(i for i in self.share.list("")["items"] if i["name"] == "busy.txt")["open"], True)
        os.mkdir(self.root / "Folder")
        os.rename(self.root / "busy.txt", self.root / "Folder" / "busy.txt")
        with self.assertRaises(ShareError) as cm:
            self.share.delete("Folder", recursive=True)
        self.assertEqual(cm.exception.code, "file-open")
        self.open_set = None
        self.assertIsNone(self.share.list("Folder")["items"][0]["open"])      # unknown -> the client warns

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs /proc")
    async def test_open_files_seen_through_proc(self):
        await self.up("held.txt", b"x")
        await self.up("app", b"y")
        share = SharedFolder(self.root, guest_pids=lambda: [os.getpid()])
        f = open(self.root / "held.txt", "rb")
        self.addCleanup(f.close)
        cache = self.root / ".classicvirtio.nosync.noindex" / "resforks" / "0"
        cache.mkdir(parents=True)
        ino = (self.root / "app").stat().st_ino
        (cache / ("%08x" % cnid_for((ino << 1) | 1))).write_bytes(b"")
        g = open(cache / ("%08x" % cnid_for((ino << 1) | 1)), "rb")
        self.addCleanup(g.close)
        items = {i["name"]: i["open"] for i in share.list("")["items"]}
        self.assertEqual(items, {"held.txt": True, "app": True})

    async def test_folders_rename_delete(self):
        fork = ms.sample_fork()
        await self.up("Doc.bin", _macbinary(b"Doc", b"TEXT", b"ttxt", 0, b"d", fork))
        e = self.share.rename("Doc", "Café Doc")
        host = unicodedata.normalize("NFD", "Café Doc")
        self.assertEqual(e["name"], host)
        self.assertEqual(sorted(self.names()), sorted([".retroboot-incoming", host, host + ".idump", host + ".rdump"]))
        self.share.rename(host, "CAFÉ doc")                                  # case-only rename is allowed
        await self.up("other", b"o")
        with self.assertRaises(ShareError) as cm:
            self.share.rename("other", "café DOC")
        self.assertEqual(cm.exception.code, "name-exists")
        self.share.mkdir("", "Box")
        await self.up("in.txt", b"i", rel="Box")
        with self.assertRaises(ShareError) as cm:
            self.share.delete("Box")
        self.assertEqual((cm.exception.code, cm.exception.extra["items"]), ("folder-not-empty", 1))
        self.assertEqual(self.share.delete("Box", recursive=True)["items"], 1)
        self.share.delete(unicodedata.normalize("NFD", "CAFÉ doc"))
        self.assertEqual(self.names(), [".retroboot-incoming", "other"])

    async def test_long_and_foreign_names_are_listed_as_mac_os_shows_them(self):
        """Every host name the Finder lists (classicvirtio-6) is in the Files list under the
        Finder's name: 31 characters, longer names, other characters, both spellings."""
        names = {"speedo-n5105-hostd-c-210137.txt": None, "speedo-n5105-hostd-c-2101370.txt": None,
                 "snow ☃ man.txt": None, unicodedata.normalize("NFC", "résumé.txt"): None,
                 unicodedata.normalize("NFD", "résumé.txt"): None, unicodedata.normalize("NFC", "café.txt"): None,
                 "v" * 94: None}
        for n in names:
            (self.root / n).write_bytes(b"x")
        os.mkdir(self.root / "A folder with a name far too long for Mac OS")
        def short(host, stem, ext=""):                         # stem shortened to make room for '#CNID'
            suffix = "#%X" % (share_cnid((self.root / host).stat().st_ino) & 0x3FFFFFFF)
            return stem[:31 - len(suffix) - len(ext)] + suffix + ext
        items = {i["name"]: i for i in self.share.list("")["items"]}
        self.assertEqual(len(items), 8)
        shown = {h: (i["mac_name"], i["visible"]) for h, i in items.items()}
        self.assertEqual(shown["speedo-n5105-hostd-c-210137.txt"], ("speedo-n5105-hostd-c-210137.txt", True))
        long = "speedo-n5105-hostd-c-2101370.txt"
        self.assertEqual(shown[long], (short(long, "speedo-n5105-hostd-c-2101370", ".txt"), True))
        self.assertEqual(shown["snow ☃ man.txt"], (short("snow ☃ man.txt", "snow _ man", ".txt"), True))
        pre = unicodedata.normalize("NFC", "résumé.txt")
        self.assertEqual(shown[unicodedata.normalize("NFD", "résumé.txt")], ("résumé.txt", True))
        self.assertEqual(shown[pre], (short(pre, "résumé", ".txt"), True))
        self.assertEqual(shown[unicodedata.normalize("NFC", "café.txt")], ("café.txt", True))
        folder = "A folder with a name far too long for Mac OS"
        self.assertEqual(shown[folder], (short(folder, folder), True))
        self.assertEqual(shown["v" * 94], ("v" * 94, False))                  # beyond the driver: flagged
        # its window's path shows the Finder's name too
        self.assertEqual(self.share.list(folder)["crumbs"][0]["mac_name"], short(folder, folder))
        # the upload check sees the precomposed name as taken, as Mac OS does
        hit = self.share.check("", ["café.txt"])[0]
        self.assertEqual((hit["exists"], hit["existing"]), (True, unicodedata.normalize("NFC", "café.txt")))
        # a download is named as the Finder names it
        d = self.share.download("speedo-n5105-hostd-c-2101370.txt", "macbinary")
        try:
            self.assertEqual(mf.display(d.mac), short(long, "speedo-n5105-hostd-c-2101370", ".txt"))
        finally:
            d.close()

    async def test_driver_cache_of_earlier_boots_is_cleared(self):
        """classicvirtio keeps a resource fork cache folder per boot and never deletes it, nor
        the entry of a deleted file: hostd clears earlier boots' folders before a launch."""
        rf = self.root / ".classicvirtio.nosync.noindex" / "resforks"
        for d in ("0", "1"):
            (rf / d).mkdir(parents=True)
            (rf / d / "4059198d").write_bytes(b"r" * 673)
            (rf / d / "4059198d-rezstat").write_bytes(b"s" * 124)
        (rf / "notes").mkdir()                                   # not a boot folder: left alone
        os.symlink(self.td, rf / "2")                            # never followed
        cat = self.root / ".classicvirtio.nosync.noindex" / "catalog"
        cat.mkdir()
        (cat / "4059198d").write_bytes(b"x")
        self.assertEqual(self.share.clear_driver_cache(), (2, 2 * (673 + 124)))
        self.assertEqual(sorted(os.listdir(rf)), ["2", "notes"])
        self.assertTrue((cat / "4059198d").exists() and Path(self.td).exists())
        self.assertEqual(self.share.clear_driver_cache(), (0, 0))
        shutil.rmtree(self.root / ".classicvirtio.nosync.noindex")
        self.assertEqual(self.share.clear_driver_cache(), (0, 0))  # nothing there yet

    async def test_folder_finder_flags_follow_the_folder(self):
        """classicvirtio-5 keeps a folder's Finder flags in FOLDER.idump: the web list hides a
        folder Mac OS made invisible, and rename and delete carry or remove its flags."""
        for d in ("Hidden Box", "Labelled"):
            self.share.mkdir("", d)
        (self.root / "Hidden Box.idump").write_bytes(mf.idump(bytes(4), bytes(4), 0x4000))
        (self.root / "Labelled.idump").write_bytes(mf.idump(bytes(4), bytes(4), 0x1000 | 0x000C))
        self.assertEqual((self.root / "Labelled.idump").read_bytes(), b"????????\nkColor6\nkNameLocked\n")
        listed = self.share.list("")["items"]
        self.assertEqual([i["name"] for i in listed], ["Labelled"])
        self.assertTrue(all(not i["name"].endswith(".idump") for i in listed))
        self.share.rename("Labelled", "Kept")
        self.assertEqual(self.names(), ["Hidden Box", "Hidden Box.idump", "Kept", "Kept.idump"])
        self.assertEqual(mf.parse_idump((self.root / "Kept.idump").read_bytes())[2], 0x100C)
        self.share.delete("Kept")
        self.share.delete("Hidden Box")
        self.assertEqual(self.names(), [])

    async def test_finder_folders_an_earlier_driver_left_get_their_flag(self):
        for d in ("TheVolumeSettingsFolder", "Temporary Items", "Network Trash Folder", "Desktop Folder", "Mine"):
            os.mkdir(self.root / d)
        (self.root / "TheFindByContentFolder").write_bytes(b"")                   # not a folder: left alone
        (self.root / "Temporary Items.idump").write_bytes(mf.idump(bytes(4), bytes(4), 0x000C))   # its own flags
        self.assertEqual(self.share.hide_finder_folders(), ["TheVolumeSettingsFolder", "Network Trash Folder"])
        self.assertEqual((self.root / "TheVolumeSettingsFolder.idump").read_bytes(), b"????????\nkIsInvisible\n")
        self.assertEqual(mf.parse_idump((self.root / "Temporary Items.idump").read_bytes())[2], 0x000C)
        self.assertFalse((self.root / "Desktop Folder.idump").exists() or (self.root / "Mine.idump").exists())
        self.assertEqual(self.share.hide_finder_folders(), [])                   # once

    async def test_finder_bookkeeping_hidden_and_dates_move(self):
        for d in ("TheVolumeSettingsFolder", "Trash", "Desktop Folder", "Temporary Items"):
            os.mkdir(self.root / d)
        (self.root / "Desktop Folder" / "On Desktop").write_bytes(b"")
        (self.root / "Ghost").write_bytes(b"")
        (self.root / "Ghost.idump").write_bytes(mf.idump(b"TEXT", b"ttxt", 0x4000))
        self.assertEqual([i["name"] for i in self.share.list("")["items"]], ["Desktop Folder"])
        os.utime(self.root, (1, 1))
        os.mkdir(self.root / "Sub")
        os.utime(self.root / "Sub", (1, 1))
        await self.up("new.txt", b"n", rel="Sub")
        self.assertGreater(self.root.stat().st_mtime, 1)                # the volume's date moves...
        self.assertGreater((self.root / "Sub").stat().st_mtime, 1)      # ...and the folder's

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "needs root")
    async def test_new_items_belong_to_the_guest_user(self):
        os.chown(self.root, 12345, 23456)
        r = await self.up("own.txt", b"x")
        for n in ("own.txt", "own.txt.idump"):
            st = (self.root / n).stat()
            self.assertEqual((st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)), (12345, 23456, 0o664))
        self.share.mkdir("", "Dir")
        st = (self.root / "Dir").stat()
        self.assertEqual((st.st_uid, stat.S_IMODE(st.st_mode)), (12345, 0o775))
        del r


class ShareApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp.test_utils import TestClient, TestServer

        import helpers
        from retroboot.hostd import Hostd
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        self.app = Hostd(helpers.make_config(self.root))
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()
        self.app.web.tls()
        self.client = TestClient(TestServer(self.app.web.web_app, host="127.0.0.1"))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        code, _ = self.app.pairing.current_code()
        r = await self.client.post("/api/v1/pair", json={"code": code, "device_name": "Files test"})
        self.cookie = r.headers.getall("Set-Cookie")[0].split("rb_session=", 1)[1].split(";", 1)[0]
        self.csrf = (await r.json())["csrf"]
        self.share_dir = self.app.paths.share

    def h(self, csrf=True, **extra):
        hd = {"Cookie": f"rb_session={self.cookie}"}
        if csrf:
            hd["X-RetroBoot-CSRF"] = self.csrf
        hd.update(extra)
        return hd

    async def test_pairing_and_csrf_required(self):
        for method, path in (("GET", "/api/v1/share/list"), ("POST", "/api/v1/share/upload?name=x"),
                             ("GET", "/api/v1/share/download?path=x"), ("POST", "/api/v1/share/mkdir")):
            r = await self.client.request(method, path)
            self.assertEqual(r.status, 401, path)
        for path, body in (("/api/v1/share/mkdir", {"name": "x"}), ("/api/v1/share/rename", {"path": "x", "name": "y"}),
                           ("/api/v1/share/delete", {"path": "x"}), ("/api/v1/share/check", {"names": ["x"]})):
            r = await self.client.post(path, json=body, headers=self.h(csrf=False))
            self.assertEqual((r.status, (await r.json())["error"]["code"]), (403, "csrf-invalid"), path)
        r = await self.client.post("/api/v1/share/upload?name=x.txt", data=b"x",
                                   headers=self.h(csrf=False, **{"Content-Type": "application/octet-stream"}))
        self.assertEqual(r.status, 403)
        r = await self.client.post("/api/v1/share/upload?name=x.txt", data=b"x",
                                   headers=self.h(**{"Content-Type": "application/octet-stream",
                                                     "Origin": "https://evil.example"}))
        self.assertEqual(r.status, 403)
        self.assertFalse((self.share_dir / "x.txt").exists())

    async def test_upload_list_download(self):
        fork = ms.sample_fork()
        blob = _macbinary(b"Styled Note", b"TEXT", b"ttxt", 0x0100, b"Bold words", fork)
        r = await self.client.post("/api/v1/share/upload?path=&name=Styled%20Note.bin&mtime=1700000000000",
                                   data=blob, headers=self.h(**{"Content-Type": "application/octet-stream"}))
        self.assertEqual(r.status, 201, await r.text())
        res = await r.json()
        self.assertEqual((res["source"], res["items"][0]["name"]), ("macbinary", "Styled Note"))
        r = await self.client.get("/api/v1/share/list?path=", headers=self.h(csrf=False))
        body = await r.json()
        self.assertEqual([i["mac_name"] for i in body["items"]], ["Styled Note"])
        self.assertTrue(body["share"]["enabled"])
        self.assertEqual(body["share"]["volume_name"], "RetroBoot Share")
        self.assertEqual(body["share"]["max_file_bytes"], MAX_FILE)
        r = await self.client.get("/api/v1/share/download?path=Styled%20Note", headers=self.h(csrf=False))
        self.assertEqual(r.status, 200)
        self.assertIn("attachment;", r.headers["Content-Disposition"])
        self.assertIn("filename*=UTF-8''Styled%20Note.bin", r.headers["Content-Disposition"])
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        got = await r.read()
        self.assertEqual(int(r.headers["Content-Length"]), len(got))
        h = mf.macbinary_header(got[:128], len(got))
        self.assertEqual(got[h["rsrc_offset"]:h["rsrc_offset"] + h["rsrc_length"]], fork)
        r = await self.client.get("/api/v1/share/download?path=Styled%20Note&format=data", headers=self.h(csrf=False))
        self.assertEqual(await r.read(), b"Bold words")
        r = await self.client.post("/api/v1/share/check", json={"path": "", "names": ["styled note", "new.txt"]},
                                   headers=self.h())
        self.assertEqual([(i["exists"], i.get("existing")) for i in (await r.json())["items"]],
                         [(True, "Styled Note"), (False, None)])
        r = await self.client.post("/api/v1/share/rename", json={"path": "Styled Note", "name": "Note"}, headers=self.h())
        self.assertEqual((await r.json())["item"]["name"], "Note")
        r = await self.client.post("/api/v1/share/mkdir", json={"path": "", "name": "Folder"}, headers=self.h())
        self.assertEqual(r.status, 201)
        r = await self.client.post("/api/v1/share/delete", json={"path": "Note"}, headers=self.h())
        self.assertEqual((await r.json())["deleted"], "Note")
        events = [e["event"] for e in self.app.lifecycle.recent]
        for ev in ("share_added", "share_renamed", "share_folder_created", "share_deleted"):
            self.assertIn(ev, events)

    async def test_upload_refusals(self):
        ct = {"Content-Type": "application/octet-stream"}
        r = await self.client.post("/api/v1/share/upload?name=x.txt", data=b"x", headers=self.h(**{"Content-Type": "text/plain"}))
        self.assertEqual(r.status, 415)

        async def gen():
            yield b"abc"
        r = await self.client.post("/api/v1/share/upload?name=x.txt", data=gen(), headers=self.h(**ct))
        self.assertEqual((r.status, (await r.json())["error"]["code"]), (411, "length-required"))
        self.client.server.app  # noqa: B018 - keep the server referenced
        self.app.web.share_api.share.max_file = 4
        r = await self.client.post("/api/v1/share/upload?name=big.txt", data=b"12345", headers=self.h(**ct))
        body = await r.json()
        self.assertEqual((r.status, body["error"]["code"]), (413, "too-large"))
        self.assertIn("big.txt", body["error"]["message"])
        for q in ("path=..&name=x", "path=.retroboot-incoming&name=x", "path=a%00b&name=x"):
            r = await self.client.post(f"/api/v1/share/upload?{q}", data=b"x", headers=self.h(**ct))
            self.assertIn(r.status, (400, 404), q)
        r = await self.client.get("/api/v1/share/list?path=..%2F..", headers=self.h(csrf=False))
        self.assertEqual(r.status, 400)
        r = await self.client.post("/api/v1/share/delete", json={"path": "x", "recursive": "yes"}, headers=self.h())
        self.assertEqual(r.status, 422)

    async def test_share_state_follows_settings(self):
        s = self.app.settings
        rev = s.snapshot(self.app.validation_context())["revision"]
        s.patch(rev, {"share.enabled": False}, self.app.validation_context())
        r = await self.client.get("/api/v1/share/list", headers=self.h(csrf=False))
        st = (await r.json())["share"]
        self.assertEqual((st["enabled"], st["applied"], st["pending_restart"], st["guest_running"]),
                         (False, True, True, False))


if __name__ == "__main__":
    unittest.main()
