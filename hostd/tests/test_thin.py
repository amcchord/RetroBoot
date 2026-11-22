"""Thin machine archives: GNU PAX sparse disk.img import, strict map validation, producers, catalog field."""
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
import test_web_api
from retroboot import catalog, sparsetar
from retroboot.config import Paths
from retroboot.machines import MachineRegistry
from retroboot.sparsetar import BLOCK, pad, pax_records, ustar_header

MiB = 1024 * 1024
CFG = json.dumps({"name": "Mac", "machineFamily": "powerMacG4"}).encode()


def plain(name: str, data: bytes) -> bytes:
    return ustar_header(name, len(data)) + pad(data)


def sparse(realsize: int, entries: list[tuple[int, int]], data: bytes | None = None, *, name: str = "disk.img",
           pax: dict | None = None, count: int | None = None, size_delta: int = 0, typeflag: bytes = b"0") -> bytes:
    """A PAX sparse 1.0 member built by hand; ``data`` defaults to 0xAB for every mapped byte."""
    fields = {"GNU.sparse.major": "1", "GNU.sparse.minor": "0", "GNU.sparse.name": name,
              "GNU.sparse.realsize": str(realsize)}
    fields.update(pax or {})
    fields = {k: v for k, v in fields.items() if v is not None}
    stored = sum(n for _, n in entries)
    if data is None:
        data = b"\xab" * stored
    smap = pad(("%d\n" % (len(entries) if count is None else count)
                + "".join("%d\n%d\n" % e for e in entries)).encode())
    rec = pax_records(fields)
    return (ustar_header(f"PaxHeaders/{name}", len(rec), b"x") + pad(rec)
            + ustar_header(f"GNUSparseFile.0/{name}", len(smap) + len(data) + size_delta, typeflag)
            + smap + pad(data))


def archive(path: Path, *parts: bytes, trailer: bool = True) -> Path:
    body = b"".join(parts) + (bytes(2 * BLOCK) if trailer else b"")
    path.write_bytes(gzip.compress(body, mtime=0))
    return path


def make_disk(path: Path, size: int, blocks: dict[int, bytes]) -> bytes:
    """A sparse file with data at the given offsets; returns its full contents."""
    with open(path, "wb") as f:
        f.truncate(size)
        for off, data in blocks.items():
            f.seek(off)
            f.write(data)
    return path.read_bytes()


class ThinArchiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.n = 0

    def extract(self, arc: Path, limits=None) -> tuple[dict, Path]:
        self.n += 1
        staging = self.root / f"st{self.n}"
        info = catalog.extract_archive(arc, staging, limits or catalog.ExtractLimits())
        return info, staging

    def rejects(self, arc: Path, limits=None, code: str = "unsafe_archive"):
        self.n += 1
        with self.assertRaises(catalog.ImportError_) as cm:
            catalog.extract_archive(arc, self.root / f"bad{self.n}", limits or catalog.ExtractLimits())
        self.assertEqual(cm.exception.code, code, cm.exception.message)

    def machine(self, size=8 * MiB, tail: bool = False) -> tuple[Path, bytes]:
        self.n += 1
        mdir = self.root / f"m{self.n}"
        mdir.mkdir()
        (mdir / "config.json").write_bytes(CFG)
        blocks = {0: b"LK" + os.urandom(1000), 8192: b"\0" * 4096 + b"x",     # a zero block inside data
                  3 * MiB + 512: os.urandom(70_000)}
        if tail:
            blocks[size - 512] = b"END!"
        return mdir, make_disk(mdir / "disk.img", size, blocks)

    # --- valid archives ---------------------------------------------------------------------

    def test_thin_round_trip_writes_only_data(self):
        for tail in (False, True):
            with self.subTest(ends_in_data=tail):
                mdir, disk = self.machine(tail=tail)
                arc = self.root / f"thin{tail}.tar.gz"
                with open(arc, "wb") as f:
                    facts = sparsetar.write_thin(f, [("config.json", mdir / "config.json"),
                                                     ("disk.img", mdir / "disk.img")])
                self.assertEqual(facts["sha256"], hashlib.sha256(arc.read_bytes()).hexdigest())
                self.assertEqual(facts["archiveBytes"], arc.stat().st_size)
                self.assertEqual(facts["installedBytes"], len(disk) + len(CFG))
                self.assertLessEqual(facts["storedBytes"], 4096 * 22)
                limits = catalog.ExtractLimits(facts["installedBytes"], facts["requiredStorageBytes"])
                info, st = self.extract(arc, limits)
                self.assertEqual((st / "disk.img").read_bytes(), disk)
                self.assertEqual((st / "config.json").read_bytes(), CFG)
                rec = info["files"]["disk.img"]
                self.assertEqual((rec["format"], rec["stored_bytes"], rec["sha256"]),
                                 ("sparse", facts["storedBytes"], None))
                self.n += 1
                hashed = catalog.extract_archive(arc, self.root / f"st{self.n}", limits, hash_disk=True)
                self.assertEqual(hashed["files"]["disk.img"]["sha256"], hashlib.sha256(disk).hexdigest())
                self.assertLess(rec["allocated_bytes"], 1 * MiB)
                self.assertLess(info["stored_bytes"], 200_000)
                # tarfile (and so GNU tar/libarchive) read the same member
                with tarfile.open(arc) as tf:
                    self.assertEqual(tf.extractfile("disk.img").read(), disk)

    def test_classic_writer_and_old_archives_still_import(self):
        mdir, disk = self.machine()
        arc = self.root / "classic.tar.gz"
        with open(arc, "wb") as f:
            facts = sparsetar.write_classic(f, [("config.json", mdir / "config.json"), ("disk.img", mdir / "disk.img")])
        with tarfile.open(arc) as tf:
            m = tf.getmember("disk.img")
            self.assertFalse(m.issparse())
            self.assertEqual(tf.extractfile(m).read(), disk)
        self.n += 1
        info = catalog.extract_archive(arc, self.root / f"st{self.n}", catalog.ExtractLimits(
            facts["installedBytes"], facts["requiredStorageBytes"]), hash_disk=True)
        self.assertEqual((self.root / f"st{self.n}" / "disk.img").read_bytes(), disk)
        self.assertEqual(info["files"]["disk.img"]["sha256"], hashlib.sha256(disk).hexdigest())
        self.assertEqual(info["files"]["disk.img"]["format"], "regular")
        # a schema-1 archive written by Python's tarfile (ClassicMac's packager) too
        old = self.root / "old.tar.gz"
        with tarfile.open(old, "w:gz", format=tarfile.GNU_FORMAT) as tf:
            for n, data in (("config.json", CFG), ("disk.img", disk)):
                ti = tarfile.TarInfo(n)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        info, st = self.extract(old, catalog.ExtractLimits(len(disk) + len(CFG)))
        self.assertEqual((st / "disk.img").read_bytes(), disk)
        self.assertLessEqual((st / "disk.img").stat().st_blocks * 512, 2 * MiB)   # two 1 MiB chunks hold data

    def test_thin_and_classic_report_the_same_catalog_values(self):
        mdir, _ = self.machine(size=16 * MiB, tail=True)
        files = [("config.json", mdir / "config.json"), ("disk.img", mdir / "disk.img")]
        with open(self.root / "a.tar.gz", "wb") as f:
            thin = sparsetar.write_thin(f, files)
        with open(self.root / "b.tar.gz", "wb") as f:
            classic = sparsetar.write_classic(f, files)
        for k in ("installedBytes", "diskCapacityBytes", "requiredStorageBytes"):
            self.assertEqual(thin[k], classic[k], k)
        # ClassicMac's accounting: config 1 MiB + the 1 MiB chunks holding data (0, 3 and the last)
        self.assertEqual(thin["requiredStorageBytes"], 4 * MiB)
        # deterministic
        with open(self.root / "c.tar.gz", "wb") as f:
            again = sparsetar.write_thin(f, files)
        self.assertEqual(again["sha256"], thin["sha256"])

    @unittest.skipUnless(shutil.which("tar") and b"GNU tar" in subprocess.run(
        ["tar", "--version"], capture_output=True).stdout, "GNU tar not available")
    def test_gnu_tar_posix_sparse_archives_import_and_ours_extract(self):
        mdir, disk = self.machine(tail=False)
        arc = self.root / "gnu.tar.gz"
        subprocess.run(["tar", "--posix", "--sparse", "-czf", str(arc), "-C", str(mdir), "config.json", "disk.img"],
                       check=True)
        _, st = self.extract(arc, catalog.ExtractLimits(len(disk) + len(CFG)))
        self.assertEqual((st / "disk.img").read_bytes(), disk)
        ours = self.root / "ours.tar.gz"
        with open(ours, "wb") as f:
            sparsetar.write_thin(f, [("config.json", mdir / "config.json"), ("disk.img", mdir / "disk.img")])
        out = self.root / "gnu-out"
        out.mkdir()
        subprocess.run(["tar", "-xzf", str(ours), "-C", str(out)], check=True)
        self.assertEqual((out / "disk.img").read_bytes(), disk)

    async def test_install_machine_from_thin_archive(self):
        mdir, disk = self.machine()
        arc = self.root / "m.tar.gz"
        with open(arc, "wb") as f:
            facts = sparsetar.write_thin(f, [("config.json", mdir / "config.json"), ("disk.img", mdir / "disk.img")])
        reg = MachineRegistry(Paths(self.root))
        prog = []
        mid = await catalog.install_machine(arc, reg, "Mac OS", None, {"type": "test"},
                                            catalog.ExtractLimits(facts["installedBytes"],
                                                                  facts["requiredStorageBytes"]),
                                            lambda f, m: prog.append(f))
        m = reg.get(mid)
        self.assertEqual(m.disk.read_bytes(), disk)
        self.assertEqual(m.meta["files"]["disk.img"]["format"], "sparse")

    def test_data_extents_and_coalesce(self):
        p = self.root / "d.img"
        make_disk(p, 4 * MiB, {4096: b"a", 8192: bytes(4096), 12288: b"b", 2 * MiB: b"c" * 5000,
                               4 * MiB - 512: b"z"})
        with open(p, "rb") as f:
            ext = sparsetar.data_extents(f.fileno(), 4 * MiB)
        self.assertEqual(ext, [(4096, 4096), (12288, 4096), (2 * MiB, 8192), (4 * MiB - 4096, 4096)])
        self.assertEqual(sparsetar.coalesce(ext, 2), [(4096, 2 * MiB + 4096), (4 * MiB - 4096, 4096)])

    # --- hostile or malformed archives -------------------------------------------------------

    def test_malformed_sparse_maps_are_rejected(self):
        R = 1 * MiB
        cases = {
            "overlap": sparse(R, [(0, 8192), (4096, 4096)]),
            "unsorted": sparse(R, [(8192, 4096), (0, 4096)]),
            "duplicate-extent": sparse(R, [(4096, 4096), (4096, 4096)]),
            "out-of-range": sparse(R, [(R - 4096, 8192)]),
            "beyond-size": sparse(R, [(R, 512)]),
            "unaligned-offset": sparse(R, [(100, 512)]),
            "unaligned-size": sparse(R, [(0, 100)]),
            "negative": sparse(R, [(0, 512), (-512, 512)]),
            "empty-middle": sparse(R, [(0, 0), (512, 512)]),
            "end-marker-misplaced": sparse(R, [(0, 512), (4096, 0)]),
            "no-entries": sparse(R, []),
            "odd-realsize": sparse(R + 1, [(0, 512)]),
            "realsize-not-digits": sparse(R, [(0, 512)], pax={"GNU.sparse.realsize": "1e6"}),
            "realsize-missing": sparse(R, [(0, 512)], pax={"GNU.sparse.realsize": None}),
            "count-lies-low": sparse(R, [(0, 512), (4096, 512)], count=1),
            "count-lies-high": sparse(R, [(0, 512)], count=50),
            "data-truncated": sparse(R, [(0, 4096)], data=b"\xab" * 2048),
            "data-extra": sparse(R, [(0, 512)], data=b"\xab" * 1536),
            "header-short": sparse(R, [(0, 4096)], size_delta=-1024),
            "header-long": sparse(R, [(0, 512)], size_delta=4096),
            "version-0.1": sparse(R, [(0, 512)], pax={"GNU.sparse.minor": "1"}),
            "pax-0.1-map": sparse(R, [(0, 512)], pax={"GNU.sparse.map": "0,512"}),
            "path-override": sparse(R, [(0, 512)], pax={"path": "disk.img"}),
            "size-override": sparse(R, [(0, 512)], pax={"size": "512"}),
            "name-traversal": sparse(R, [(0, 512)], name="../disk.img"),
            "name-absolute": sparse(R, [(0, 512)], name="/disk.img"),
            "name-nested": sparse(R, [(0, 512)], name="x/disk.img"),
            "sparse-config": sparse(R, [(0, 512)], name="config.json"),
            "symlink-type": sparse(R, [(0, 512)], typeflag=b"2"),
        }
        for name, member in cases.items():
            with self.subTest(case=name):
                arc = archive(self.root / f"{name}.tar.gz", plain("config.json", CFG), member)
                self.rejects(arc)

    def test_other_gnu_sparse_encodings_and_global_headers_rejected(self):
        # old GNU 'S' member: one sparse struct at 386 (offset 0, 512 bytes), realsize at 483
        h = bytearray(ustar_header("disk.img", 512, b"S", gnu=True))
        h[386:398] = b"%011o\0" % 0
        h[398:410] = b"%011o\0" % 512
        h[483:495] = b"%011o\0" % 4096
        h[148:156] = b" " * 8
        h[148:156] = b"%06o\0 " % sum(h)
        self.rejects(archive(self.root / "gnu-S.tar.gz", plain("config.json", CFG), bytes(h) + b"\xab" * 512))
        g = pax_records({"comment": "x"})
        self.rejects(archive(self.root / "global.tar.gz", ustar_header("pax_global_header", len(g), b"g") + pad(g),
                             plain("config.json", CFG), sparse(MiB, [(0, 512)])))
        # GNU.sparse.* on a regular member
        rec = pax_records({"GNU.sparse.realsize": "1048576"})
        self.rejects(archive(self.root / "plain-sparse-keys.tar.gz", plain("config.json", CFG),
                             ustar_header("PaxHeaders/disk.img", len(rec), b"x") + pad(rec),
                             plain("disk.img", bytes(512))))

    def test_header_and_map_bombs_are_bounded(self):
        # a pax record larger than any single read tarfile may make
        big = pax_records({"comment": "x" * (3 * MiB)})
        self.rejects(archive(self.root / "pax-bomb.tar.gz", plain("config.json", CFG),
                             ustar_header("PaxHeaders/disk.img", len(big), b"x") + pad(big), plain("disk.img", b"")))
        # a long-name record likewise
        self.rejects(archive(self.root / "longname.tar.gz",
                             ustar_header("././@LongLink", 3 * MiB, b"L", gnu=True) + bytes(3 * MiB)))
        # a sparse map bigger than the header budget (many small lines, highly compressible)
        with mock.patch.object(sparsetar, "HEADER_BUDGET", 64 * 1024):
            entries = [(i * 1024, 512) for i in range(20_000)]
            self.rejects(archive(self.root / "map-bomb.tar.gz", plain("config.json", CFG),
                                 sparse(64 * MiB, entries)))
        with mock.patch.object(sparsetar, "MAX_EXTENTS", 4):
            self.rejects(archive(self.root / "too-many.tar.gz", plain("config.json", CFG),
                                 sparse(MiB, [(i * 4096, 512) for i in range(5)])))

    def test_size_and_space_limits(self):
        R = 128 * MiB
        member = sparse(R, [(0, 4096), (MiB, 4096)])
        arc = archive(self.root / "ok.tar.gz", plain("config.json", CFG), member)
        self.extract(arc, catalog.ExtractLimits(R + len(CFG), MiB))
        # realsize beyond the catalog's installedBytes: refused before anything is written
        self.rejects(arc, catalog.ExtractLimits(R + len(CFG) - 512))
        self.rejects(arc, catalog.ExtractLimits(R + len(CFG) + 512))
        # stored data beyond the advertised storage (plus 64 MiB slack): refused up front
        big = archive(self.root / "big.tar.gz", plain("config.json", CFG), sparse(R, [(0, 66 * MiB)]))
        self.rejects(big, catalog.ExtractLimits(None, MiB), code="no_space")
        self.assertFalse((self.root / f"bad{self.n}" / "disk.img").exists())
        # not enough free space for the declared data
        with mock.patch.object(catalog, "_free_bytes", lambda p: 67 * MiB):
            self.rejects(big, catalog.ExtractLimits(None, None, 2 * MiB), code="no_space")
        # disk larger than the 140 GiB ceiling
        self.rejects(archive(self.root / "huge.tar.gz", plain("config.json", CFG),
                             sparse(catalog.MAX_ARCHIVE + 512, [(0, 512)])))

    def test_truncated_and_corrupt_streams(self):
        mdir, _ = self.machine()
        arc = self.root / "t.tar.gz"
        with open(arc, "wb") as f:
            sparsetar.write_thin(f, [("config.json", mdir / "config.json"), ("disk.img", mdir / "disk.img")])
        data = arc.read_bytes()
        for cut in (20, len(data) // 2, len(data) - 12):
            with self.subTest(cut=cut):
                self.rejects(self._write(f"cut{cut}", data[:cut]))
        flipped = bytearray(data)
        flipped[len(data) // 2] ^= 0xFF
        self.rejects(self._write("flip", bytes(flipped)))
        self.rejects(self._write("not-gzip", b"\x00" * 4096))
        # data after the end of the tar (another archive appended) or a bad gzip trailer
        body = gzip.decompress(data)
        self.rejects(self._write("appended", gzip.compress(body + plain("evil.sh", b"x") + bytes(1024))))
        self.rejects(self._write("crc", data[:-8] + bytes(8)))
        # a duplicate disk (thin and regular)
        self.rejects(archive(self.root / "dup.tar.gz", plain("config.json", CFG), sparse(MiB, [(0, 512)]),
                             plain("disk.img", bytes(MiB))))

    def _write(self, name: str, data: bytes) -> Path:
        p = self.root / f"{name}.tar.gz"
        p.write_bytes(data)
        return p

    def test_guarded_reader(self):
        g = sparsetar.GuardedReader(io.BytesIO(bytes(10 * MiB)), max_read=MiB, header_budget=2 * MiB)
        g.read(MiB)
        with self.assertRaises(sparsetar.Unsafe):
            g.read(MiB + 1)
        with self.assertRaises(sparsetar.Unsafe):
            g.read(-1)
        g.read(MiB)
        with self.assertRaises(sparsetar.Unsafe):          # budget spent
            g.read(1)
        g.data()
        g.read(MiB)
        with self.assertRaises(sparsetar.Unsafe):
            g.seek(0)                                       # backward
        with self.assertRaises(sparsetar.Unsafe):
            g.seek(g.tell() + 2 * MiB)                      # long skip


class ThinUploadTests(unittest.IsolatedAsyncioTestCase):
    """The web "Import from a File" path (POST /api/v1/machines/import, streamed upload) takes thin archives."""
    asyncSetUp = test_web_api.ApiTests.asyncSetUp
    h = test_web_api.ApiTests.h
    pair = test_web_api.ApiTests.pair
    wait_job = test_web_api.ApiTests.wait_job

    async def upload(self, data: bytes):
        r = await self.client.post("/api/v1/machines/import?name=Thin%20Mac", data=data,
                                   headers=self.h(extra={"Content-Type": "application/gzip"}))
        self.assertEqual(r.status, 202, await r.text())
        return await self.wait_job((await r.json())["job"]["id"])

    async def test_upload_thin_archive(self):
        await self.pair()
        mdir = self.root / "src"
        mdir.mkdir()
        (mdir / "config.json").write_bytes(CFG)
        disk = make_disk(mdir / "disk.img", 16 * MiB, {0: b"ER" + os.urandom(600), 9 * MiB: os.urandom(9000)})
        buf = io.BytesIO()
        sparsetar.write_thin(buf, [("config.json", mdir / "config.json"), ("disk.img", mdir / "disk.img")])
        job = await self.upload(buf.getvalue())
        self.assertEqual(job["state"], "succeeded", job)
        m = self.app.machines.get(job["result"]["machine"])
        got = m.disk.read_bytes()                               # the fake guest prep marks the disk afterwards
        self.assertEqual(len(got), len(disk))
        self.assertEqual((got[:602], got[9 * MiB:9 * MiB + 9000]), (disk[:602], disk[9 * MiB:9 * MiB + 9000]))
        self.assertEqual(m.meta["files"]["disk.img"]["format"], "sparse")
        self.assertLess(m.disk.stat().st_blocks * 512, MiB)
        bad = gzip.compress(plain("config.json", CFG) + sparse(MiB, [(4096, 4096), (0, 8192)], os.urandom(12288))
                            + bytes(1024))                       # overlapping extents
        job = await self.upload(bad)
        self.assertEqual(job["state"], "failed", job)
        self.assertFalse([p for p in self.app.paths.machines.iterdir() if p.name.startswith(".staging")])
        self.assertFalse(list(self.app.paths.downloads.glob("upload-*")))


class ThinCatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.entry = {"id": "mac-os-9.2.1-test", "name": "Mac OS 9.2.1", "summary": "Test",
                      "osVersion": "Mac OS 9.2.1", "archiveURL": "https://example.invalid/a.tar.gz",
                      "archiveBytes": 100, "installedBytes": 4 * MiB + 100, "sha256": "0" * 64}
        self.thin = {"format": sparsetar.FORMAT, "archiveURL": "https://example.invalid/a.thin.tar.gz",
                     "archiveBytes": 50, "sha256": "1" * 64}

    def test_thin_archive_field(self):
        e = catalog.validate_entry(dict(self.entry, thinArchive=self.thin))
        self.assertEqual(e["thinArchive"], self.thin)
        self.assertEqual(catalog.download_of(e)["archiveURL"], self.thin["archiveURL"])
        self.assertEqual(catalog.download_of(e)["sha256"], "1" * 64)
        plain_entry = catalog.validate_entry(self.entry)
        self.assertNotIn("thinArchive", plain_entry)
        self.assertEqual(catalog.download_of(plain_entry),
                         {"format": "classic", "archiveURL": self.entry["archiveURL"], "archiveBytes": 100,
                          "sha256": "0" * 64})
        # a future format is ignored, not an error
        e = catalog.validate_entry(dict(self.entry, thinArchive=dict(self.thin, format="retroboot-thin-9")))
        self.assertNotIn("thinArchive", e)
        for patch in ({"archiveURL": "http://example.invalid/a"}, {"sha256": "x"}, {"archiveBytes": 0},
                      {"archiveBytes": "50"}):
            with self.subTest(patch=patch), self.assertRaises(catalog.ImportError_):
                catalog.validate_entry(dict(self.entry, thinArchive=dict(self.thin, **patch)))
        with self.assertRaises(catalog.ImportError_):
            catalog.validate_entry(dict(self.entry, thinArchive="https://example.invalid/x"))

    async def test_download_prefers_thin_archive(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        payload = os.urandom(5000)
        thin = dict(self.thin, archiveBytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        entry = catalog.validate_entry(dict(self.entry, thinArchive=thin))
        client = catalog.CatalogClient("https://example.invalid/catalog.json", root / "dl")
        seen = []

        class Resp:
            status = 200
            headers = {}

            def __init__(self):
                self.content = self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def iter_chunked(self, n):
                yield payload

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        async def fake_get(session, url, headers=None):
            seen.append(url)
            return Resp()
        with mock.patch.object(client, "_session", lambda: Session()), mock.patch.object(client, "_get", fake_get):
            path = await client.download(entry, lambda f, m: None)
        self.assertEqual(seen, [thin["archiveURL"]])
        self.assertEqual(path.read_bytes(), payload)
        self.assertEqual(path.name, f"{thin['sha256']}.tar.gz")


if __name__ == "__main__":
    unittest.main()
