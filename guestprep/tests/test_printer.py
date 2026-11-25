"""Tests for rbguestprep.printer (Mac OS 9 LPR Desktop Printer provisioning)."""
from __future__ import annotations

import struct
import unittest
from types import SimpleNamespace
from unittest import mock

from rbguestprep import hfsplus, printer


class ResourceBuildersTest(unittest.TestCase):
    def test_papa_round_trip(self):
        raw = printer.build_papa(name="RetroBoot Printer", host="10.0.2.2", queue="retroboot")
        self.assertEqual(len(raw), 1024)
        parsed = printer.parse_papa_lpr(raw)
        self.assertEqual(
            parsed,
            {
                "name": "RetroBoot Printer",
                "type": "LaserWriter",
                "zone": "=LPR",
                "host": "10.0.2.2",
                "queue": "retroboot",
                "tag_type": "=LPR",
            },
        )

    def test_papa_rejects_oversized_fields(self):
        with self.assertRaises(ValueError):
            printer.build_papa(host="x" * 1000)

    def test_str_list_and_cnfg_encoding(self):
        sl = printer.build_str_list(["=LPR", "RetroBoot Printer", "LaserWriter"])
        self.assertEqual(sl[:2], b"\x00\x03")
        self.assertIn(b"\x04=LPR", sl)
        self.assertIn(b"\x11RetroBoot Printer", sl)
        cnfg = printer.build_cnfg("LaserWriter 8")
        self.assertEqual(len(cnfg), 40)
        self.assertTrue(cnfg.startswith(b"nlprNLPR\rLaserWriter 8"))

    def test_alias_v2_folder_and_file_layouts(self):
        folder_alis = printer.build_alias(
            kind=1,
            vol_name="Macintosh HD",
            vol_cdate=0xE6F18DDF,
            parent_name="Desktop Folder",
            parent_cnid=19,
            target_name="RetroBoot Printer",
            target_cnid=4816,
            target_cdate=0xE72C0ED3,
            full_path="Macintosh HD:Desktop Folder:RetroBoot Printer",
        )
        size, version, kind = struct.unpack(">HHH", folder_alis[4:10])
        self.assertEqual((size, version, kind), (len(folder_alis), 2, 1))
        self.assertEqual(folder_alis[42:46], b"BD\x00\x00")
        self.assertEqual(struct.unpack(">II", folder_alis[114:122]), (4816, 0xE72C0ED3))

        file_alis = printer.build_alias(
            kind=0,
            vol_name="Macintosh HD",
            vol_cdate=0xE6F18DDF,
            parent_name="Extensions",
            parent_cnid=32,
            target_name="LaserWriter 8",
            target_cnid=360,
            target_cdate=0xB69C5D40,
            full_path="Macintosh HD:System Folder:Extensions:LaserWriter 8",
            type_creator=(b"PRER", b"vgrd"),
            cnid_chain=(32, 30),
        )
        size2, version2, kind2 = struct.unpack(">HHH", file_alis[4:10])
        self.assertEqual((size2, version2, kind2), (len(file_alis), 2, 0))
        self.assertEqual(file_alis[122:130], b"PRERvgrd")

    def test_upsert_resource_fork_preserves_existing_attributes(self):
        base = hfsplus.build_resource_fork([
            ("CODE", 1, "Main", b"\x00\x01\x02\x03"),
            ("STR ", -8192, "", b"\x04Old!"),
        ])
        # Inject non-zero resource attribute 0x40 on CODE 1 and 0x8D map attributes.
        doff, moff, dlen, mlen = struct.unpack(">IIII", base[:16])
        mod = bytearray(base)
        mod[moff + 22:moff + 24] = struct.pack(">H", 0x008D)
        tl_off = struct.unpack(">H", mod[moff + 24:moff + 26])[0]
        tlist_pos = moff + tl_off
        ntypes = struct.unpack(">H", mod[tlist_pos:tlist_pos + 2])[0] + 1
        for i in range(ntypes):
            rt, cnt, ref = struct.unpack(">4sHH", mod[tlist_pos + 2 + 8 * i:tlist_pos + 10 + 8 * i])
            if rt == b"CODE":
                mod[tlist_pos + ref + 4] = 0x40
        updated = printer.upsert_resource_fork(
            bytes(mod),
            [
                ("STR ", -8192, "", 0xC0, b"\rLaserWriter 8"),
                ("alis", -8192, "", 0x00, b"ALIASDATA"),
            ],
        )
        parsed = {(t, i): (n, d) for t, i, n, _p, d in hfsplus.parse_resource_fork(updated)}
        self.assertEqual(parsed[("CODE", 1)], ("Main", b"\x00\x01\x02\x03"))
        self.assertEqual(parsed[("STR ", -8192)], ("", b"\rLaserWriter 8"))
        self.assertEqual(parsed[("alis", -8192)], ("", b"ALIASDATA"))

        # Check preserved map attributes and resource attributes.
        _d2, moff2, _dl2, ml2 = struct.unpack(">IIII", updated[:16])
        rmap2 = updated[moff2:moff2 + ml2]
        self.assertEqual(struct.unpack(">H", rmap2[22:24])[0], 0x008D)
        tl2 = struct.unpack(">H", rmap2[24:26])[0]
        nt2 = struct.unpack(">H", rmap2[tl2:tl2 + 2])[0] + 1
        attrs = {}
        for i in range(nt2):
            rt, cnt, ref = struct.unpack(">4sHH", rmap2[tl2 + 2 + 8 * i:tl2 + 10 + 8 * i])
            for j in range(cnt + 1):
                rid, _no, ao = struct.unpack(">hHI", rmap2[tl2 + ref + 12 * j:tl2 + ref + 12 * j + 8])
                attrs[(rt.decode("mac_roman"), rid)] = (ao >> 24) & 0xFF
        self.assertEqual(attrs[("CODE", 1)], 0x40)
        self.assertEqual(attrs[("STR ", -8192)], 0xC0)
        self.assertEqual(attrs[("alis", -8192)], 0x00)


class PPDSelectionTest(unittest.TestCase):
    def test_prefers_builtin_laserwriter_ppd_or_falls_back(self):
        self.assertEqual(
            printer.choose_guest_ppd([
                "Some Other PPD",
                "LaserWriter 8500 PPD v1.2",
                "LaserWriter Color 12/600 PS",
                "Generic",
            ]),
            "LaserWriter Color 12/600 PS",
        )
        self.assertEqual(
            printer.choose_guest_ppd(["Some Other PPD", "LaserWriter 8500 PPD v1.2", "Generic"]),
            "LaserWriter 8500 PPD v1.2",
        )
        self.assertEqual(
            printer.choose_guest_ppd(["Apple Color LW 12/660", "Generic"]),
            "Generic",
        )
        self.assertEqual(printer.choose_guest_ppd(["HP LaserJet 4"]), "HP LaserJet 4")
        self.assertEqual(printer.choose_guest_ppd([]), "Generic")


def _make_fake_rec(cnid: int, parent_id: int, name: str, *, is_file: bool = True,
                   tc: tuple[bytes, bytes] = (b"\x00\x00\x00\x00", b"\x00\x00\x00\x00"),
                   rsrc: bytes = b""):
    return SimpleNamespace(
        cnid=cnid,
        parent_id=parent_id,
        name=name,
        is_file=is_file,
        type_creator=tc,
        _rsrc=rsrc,
    )


class _FakeVolume:
    def __init__(self, records: list, *, system_folder=("System Folder",)):
        self._recs = list(records)
        self._sys = list(system_folder)
        self.block_size = 4096

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def records(self):
        return iter(self._recs)

    def lookup(self, path: list[str]):
        parent = hfsplus.ROOT_FOLDER_ID
        rec = None
        for comp in path:
            rec = next(
                (r for r in self._recs if r.parent_id == parent and r.name.lower() == comp.lower()),
                None,
            )
            if rec is None:
                raise FileNotFoundError(":".join(path))
            parent = rec.cnid
        if rec is None:
            raise FileNotFoundError(":".join(path))
        return rec

    def read_fork(self, rec, which: str) -> bytes:
        assert which == "rsrc"
        return rec._rsrc


class OwnershipAndStateTest(unittest.TestCase):
    def _base_records(self, *, with_lw8=True, with_disabled=False):
        recs = [
            _make_fake_rec(2, 1, "Macintosh HD", is_file=False),
            _make_fake_rec(16, 2, "TheVolumeSettingsFolder", is_file=False),
            _make_fake_rec(19, 2, "Desktop Folder", is_file=False),
            _make_fake_rec(30, 2, "System Folder", is_file=False),
            _make_fake_rec(32, 30, "Extensions", is_file=False),
            _make_fake_rec(33, 30, "Extensions (Disabled)", is_file=False),
            _make_fake_rec(34, 30, "Control Strip Modules", is_file=False),
            _make_fake_rec(35, 30, "Control Strip Modules (Disabled)", is_file=False),
            _make_fake_rec(36, 32, "Printer Descriptions", is_file=False),
            _make_fake_rec(37, 36, "LaserWriter 8500 PPD v1.2", tc=(b"TEXT", b"vgrd")),
            _make_fake_rec(
                4287,
                30,
                "System",
                tc=(b"zsys", b"MACS"),
                rsrc=hfsplus.build_resource_fork([("STR ", -8192, "", b"\rLaserWriter 8"), ("alis", -8192, "", b"A")]),
            ),
        ]
        if with_lw8:
            recs.append(
                _make_fake_rec(
                    360,
                    32,
                    "LaserWriter 8",
                    tc=(b"PRER", b"vgrd"),
                    rsrc=hfsplus.build_resource_fork([("PAPA", -8192, "", printer.build_papa())]),
                )
            )
        if with_disabled:
            recs.extend([
                _make_fake_rec(401, 33, "Desktop Printer Spooler", tc=(b"appe", b"dtps")),
                _make_fake_rec(402, 33, "Desktop PrintMonitor", tc=(b"APPL", b"prmt")),
                _make_fake_rec(403, 33, "Desktop Printer Menu", tc=(b"INIT", b"dtpm")),
                _make_fake_rec(404, 35, "Printer Selector", tc=(b"sdev", b"dpcs")),
            ])
        else:
            recs.extend([
                _make_fake_rec(401, 32, "Desktop Printer Spooler", tc=(b"appe", b"dtps")),
                _make_fake_rec(402, 32, "Desktop PrintMonitor", tc=(b"APPL", b"prmt")),
                _make_fake_rec(403, 32, "Desktop Printer Menu", tc=(b"INIT", b"dtpm")),
                _make_fake_rec(404, 34, "Printer Selector", tc=(b"sdev", b"dpcs")),
            ])
        return recs

    def _run_state(self, recs):
        fake = _FakeVolume(recs)
        with mock.patch.object(printer.hfsplus, "Volume", return_value=fake), \
             mock.patch.object(printer.prep, "_system_folder", return_value=["System Folder"]):
            return printer.state("disk.img")

    def test_missing_when_no_printers_exist(self):
        st = self._run_state(self._base_records())
        self.assertEqual(st["result"], "missing")
        self.assertEqual(st["ppd"], "LaserWriter 8500 PPD v1.2")

    def test_no_laserwriter8_detected(self):
        st = self._run_state(self._base_records(with_lw8=False))
        self.assertEqual(st["result"], "no-laserwriter8")

    def test_user_dplt_file_never_overwritten(self):
        recs = self._base_records() + [
            _make_fake_rec(900, 19, "Office Laser", tc=(b"dplt", b"dcdc")),
        ]
        st = self._run_state(recs)
        self.assertEqual(st["result"], "existing-printer-kept")
        with mock.patch.object(printer, "state", return_value=st), \
             mock.patch.object(printer, "enable_printer_extensions") as en:
            out = printer.ensure_printer("disk.img")
        self.assertEqual(out["result"], "existing-printer-kept")
        en.assert_not_called()

    def test_user_desktop_printer_folder_never_overwritten(self):
        recs = self._base_records() + [
            _make_fake_rec(900, 19, "My Network Printer", is_file=False),
            _make_fake_rec(901, 900, "Configuration", tc=(b"dtpc", b"dcdc")),
        ]
        st = self._run_state(recs)
        self.assertEqual(st["result"], "existing-printer-kept")

    def test_modified_retroboot_printer_never_overwritten(self):
        custom_papa = printer.build_papa(name="RetroBoot Printer", host="192.168.1.50", queue="office")
        cfg_rsrc = hfsplus.build_resource_fork([("PAPA", -8192, "", custom_papa)])
        recs = self._base_records() + [
            _make_fake_rec(900, 19, "RetroBoot Printer", is_file=False),
            _make_fake_rec(901, 900, "Configuration", tc=(b"dtpc", b"dcdc"), rsrc=cfg_rsrc),
        ]
        st = self._run_state(recs)
        self.assertEqual(st["result"], "existing-printer-kept")

    def test_retroboot_configured_is_idempotent(self):
        papa = printer.build_papa()
        cfg_rsrc = hfsplus.build_resource_fork([("PAPA", -8192, "", papa)])
        alis = printer.build_alias(
            kind=1, vol_name="Macintosh HD", vol_cdate=1,
            parent_name="Desktop Folder", parent_cnid=19,
            target_name="RetroBoot Printer", target_cnid=900, target_cdate=1,
            full_path="Macintosh HD:Desktop Folder:RetroBoot Printer",
        )
        db_rsrc = hfsplus.build_resource_fork([("alis", 18772, "", alis)])
        recs = self._base_records() + [
            _make_fake_rec(900, 19, "RetroBoot Printer", is_file=False),
            _make_fake_rec(901, 900, "Configuration", tc=(b"dtpc", b"dcdc"), rsrc=cfg_rsrc),
            _make_fake_rec(902, 16, "DesktopPrinters DB", tc=(b"dtpl", b"dcdc"), rsrc=db_rsrc),
        ]
        st = self._run_state(recs)
        self.assertEqual(st["result"], "retroboot-configured")
        with mock.patch.object(printer, "state", return_value=st), \
             mock.patch.object(printer, "enable_printer_extensions") as en:
            out = printer.ensure_printer("disk.img")
        self.assertEqual(out["result"], "retroboot-configured")
        en.assert_not_called()

    def test_disabled_extension_plan(self):
        recs = self._base_records(with_disabled=True)
        fake = _FakeVolume(recs)
        moves = printer._disabled_enable_plan(fake, printer._children_by_parent(fake), ["System Folder"])
        moved_names = [m[2] for m in moves]
        self.assertEqual(
            moved_names,
            ["Desktop Printer Spooler", "Desktop PrintMonitor", "Desktop Printer Menu", "Printer Selector"],
        )


if __name__ == "__main__":
    unittest.main()
