"""Unit and API tests for CUPS printer support, PrintServer routing, and /api/v1/printers."""
from __future__ import annotations

import asyncio
import os
import struct
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

import helpers
from retroboot import cups, printer, schema
from retroboot.hostd import Hostd


def _ipp_attr(tag: int, name: bytes, value: bytes) -> bytes:
    return bytes((tag,)) + struct.pack(">H", len(name)) + name + struct.pack(">H", len(value)) + value


def _ipp_req(op_id: int, req_id: int, attrs: bytes = b"", payload: bytes = b"") -> bytes:
    return (
        struct.pack(">BBHI", 1, 1, op_id, req_id)
        + b"\x01"
        + _ipp_attr(0x47, b"attributes-charset", b"utf-8")
        + _ipp_attr(0x48, b"attributes-natural-language", b"en")
        + attrs
        + b"\x03"
        + payload
    )


def _ipp_parse_resp(body: bytes) -> tuple[int, int, dict[str, list[bytes]]]:
    _maj, _min, status, req_id = struct.unpack_from(">BBHI", body, 0)
    pos = 8
    n = len(body)
    attrs: dict[str, list[bytes]] = {}
    last_key = ""
    while pos < n:
        tag = body[pos]
        pos += 1
        if tag == 0x03:
            break
        if tag <= 0x0F:
            continue
        klen = struct.unpack_from(">H", body, pos)[0]
        pos += 2
        key = body[pos:pos + klen].decode("utf-8", "replace")
        pos += klen
        vlen = struct.unpack_from(">H", body, pos)[0]
        pos += 2
        val = body[pos:pos + vlen]
        pos += vlen
        if key:
            last_key = key
        attrs.setdefault(last_key, []).append(val)
    return status, req_id, attrs


class ValidationTests(unittest.TestCase):
    def test_queue_names_and_slugify(self):
        self.assertEqual(cups.validate_queue_name("Brother_HL-2350DW"), "Brother_HL-2350DW")
        for bad in ("", "-leading", "_under", "has space", "bad/slash", "x" * 49, "a;rm"):
            with self.assertRaises(cups.CupsError, msg=bad):
                cups.validate_queue_name(bad)
        for reserved in ("pdf", "PDF", "print-to-pdf", "retroboot", "lp", "default", "raw", "all"):
            with self.assertRaises(cups.CupsError, msg=reserved):
                cups.validate_queue_name(reserved)
            slug = cups.slugify_printer_name(reserved)
            self.assertNotIn(slug.lower(), cups.RESERVED_QUEUE_NAMES)
            self.assertRegex(slug, r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")

        self.assertEqual(cups.slugify_printer_name("Brother HL-L2350DW series"), "Brother-HL-L2350DW-series")
        self.assertEqual(cups.slugify_printer_name("!!!"), "printer")
        self.assertLessEqual(len(cups.slugify_printer_name("A" * 80)), 48)

    def test_printer_uri_validation_and_manual_builder(self):
        for good in (
            "ipp://192.168.1.50/ipp/print",
            "ipps://printer.local:443/ipp/print",
            "socket://192.168.1.51:9100",
            "lpd://192.168.1.52/BINARY_P1",
            "usb://HP/LaserJet%20P1102w?serial=0000123",
            "usb://Brother/MFC-L8900CDW%20series?serial=XYZ&interface=1",
            "dnssd://Brother%20HL._ipp._tcp.local/?uuid=1234-5678",
            "dnssd://Brother%20MFC-L8900CDW%20series._ipp._tcp.local/?uuid=1234-5678&mdl=MFC-L8900CDW",
            "ipp://127.0.0.1:60000/ipp/print",
            "ipps://localhost:60001/ipp/print",
        ):
            self.assertEqual(cups.validate_printer_uri(good), good)

        for bad in (
            "",
            "http://192.168.1.50/ipp",
            "file:///etc/passwd",
            "ipp:///no-host/ipp",
            "ipp://192.168.1.50/ipp print",
            "ipp://192.168.1.50/ipp;id",
            "ipp://192.168.1.50/ipp|cat",
            "ipp://192.168.1.50/ipp&id",
            "ipp://192.168.1.50/$(whoami)",
            "ipp://192.168.1.50/`id`",
            "ipp://192.168.1.50/\x00bad",
            "ipp://user:pass@192.168.1.50/ipp",
            "ipp://127.0.0.1:631/printers/retroboot",
            "ipp://127.0.0.1/printers/retroboot",
            "ipp://localhost:631/printers/retroboot",
            "ipp://localhost/ipp/print",
            "lpd://127.0.0.1:515/lp",
            "socket://127.0.0.1:9100",
            "socket://[::1]:9100",
            "ipp://[::1]:631/ipp/print",
            "ipp://127.0.1.1:631/ipp/print",
            "ipp://127.0.0.1:8080/ipp/print",
        ):
            with self.assertRaises(cups.CupsError, msg=bad):
                cups.validate_printer_uri(bad)

        self.assertEqual(cups.build_manual_uri("ipp", "192.168.1.50"), "ipp://192.168.1.50/ipp/print")
        self.assertEqual(cups.build_manual_uri("ipps", "fe80::1", 631, "ipp/print"), "ipps://[fe80::1]:631/ipp/print")
        self.assertEqual(cups.build_manual_uri("socket", "192.168.1.51", 9100), "socket://192.168.1.51:9100")
        self.assertEqual(cups.build_manual_uri("lpd", "printer.lan", 515, "PASSTHRU"), "lpd://printer.lan:515/PASSTHRU")
        with self.assertRaises(cups.CupsError):
            cups.build_manual_uri("ipp", "192.168.1.50", 70000)
        with self.assertRaises(cups.CupsError):
            cups.build_manual_uri("http", "192.168.1.50")

    def test_generate_test_postscript(self):
        ps = cups.generate_test_postscript("Brother (Office)\\1", "3.2.0")
        text = ps.decode("ascii")
        self.assertTrue(text.startswith("%!PS-Adobe-3.0\n"))
        self.assertIn("RetroBoot 3.2.0 - Printer Test Page", text)
        self.assertIn(r"Target: Brother \(Office\)\\1", text)
        self.assertIn("showpage", text)
        self.assertIn("%%EOF", text)


class CupsManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_printers_discover_add_remove_print_cancel(self):
        calls: list[tuple[list[str], dict | None]] = []
        everywhere_fail = True

        lpstat_printers = (
            "printer Brother_HL is idle.  enabled since Tue Nov 24 10:00:00 2026\n"
            "\tDescription: Brother HL-L2350DW\n"
            "\tLocation: Office\n"
            "printer Old_Jet is disabled since Tue Nov 24 09:00:00 2026 -\n"
            "\treason unknown\n"
            "\tDescription: HP DeskJet\n"
            "\tLocation: Hall\n"
            "device for Brother_HL: ipp://192.168.1.50/ipp/print\n"
            "device for Old_Jet: socket://192.168.1.55:9100\n"
        )
        avahi_ipp = (
            "=;eth0;IPv4;Brother\\032HL-L2350DW;_ipp._tcp;local;brother.local;192.168.1.50;631;"
            "\"rp=ipp/print\" \"ty=Brother HL-L2350DW\" \"pdl=application/pdf,image/pwg-raster\"\n"
            "=;eth0;IPv6;Brother\\032HL-L2350DW;_ipp._tcp;local;brother.local;fe80::1234;631;"
            "\"rp=ipp/print\" \"ty=Brother HL-L2350DW\"\n"
            "=;lo;IPv4;RetroBoot\\032Print\\032to\\032PDF;_ipp._tcp;local;localhost;127.0.0.1;631;\"rp=printers/RetroBoot\"\n"
        )
        avahi_pdl = (
            "=;eth0;IPv4;Epson\\032ET;_pdl-datastream._tcp;local;epson.local;192.168.1.60;9100;"
            "\"ty=Epson EcoTank\"\n"
        )
        lpinfo_out = (
            "network socket\n"
            "direct usb://HP/LaserJet%20P1102w?serial=ABC123\n"
        )
        driverless_out = "ipp://127.0.0.1:60000/ipp/print\n"

        async def fake_run(cmd, timeout=15.0, env=None):
            calls.append((list(cmd), env))
            prog = cmd[0]
            if prog == "lpstat":
                if "-p" in cmd:
                    return 0, lpstat_printers, ""
                if "not-completed" in cmd:
                    return 0, "Brother_HL-12  rbguest  4096  Tue Nov 24 10:05:00 2026\n", ""
                if "completed" in cmd:
                    return 0, "Brother_HL-11  rbguest  2048  Tue Nov 24 10:01:00 2026\n", ""
            if prog == "avahi-browse":
                svc = cmd[-1]
                if svc == "_ipp._tcp":
                    return 0, avahi_ipp, ""
                if svc == "_pdl-datastream._tcp":
                    return 0, avahi_pdl, ""
                return 0, "", ""
            if prog == "lpinfo":
                return 0, lpinfo_out, ""
            if prog == "driverless":
                return 0, driverless_out, ""
            if prog == "lpadmin":
                if "-x" in cmd:
                    if cmd[-1] == "Missing":
                        return 1, "", "lpadmin: The printer or class does not exist."
                    return 0, "", ""
                if everywhere_fail and "everywhere" in cmd:
                    return 1, "", "lpadmin: Unable to connect to printer."
                return 0, "", ""
            if prog in ("cupsenable", "cupsaccept"):
                return 0, "", ""
            if prog == "lp":
                return 0, "request id is Brother_HL-13 (1 file(s))\n", ""
            if prog == "cancel":
                return 0, "", ""
            return 1, "", f"unexpected {cmd}"

        mgr = cups.CupsManager(socket_path="/run/cups/cups.sock", runner=fake_run)
        with patch("shutil.which", side_effect=lambda name: f"/usr/sbin/{name}" if name in ("lpadmin", "lp") else None):
            self.assertEqual(mgr.available(), (True, None))
        with patch("shutil.which", return_value=None):
            ok, reason = mgr.available()
            self.assertFalse(ok)
            self.assertIn("lpadmin", reason)

        listed = await mgr.list_printers()
        self.assertEqual(len(listed), 2)
        self.assertEqual(listed[0]["name"], "Brother_HL")
        self.assertEqual(listed[0]["state"], "idle")
        self.assertTrue(listed[0]["enabled"])
        self.assertEqual(listed[0]["uri"], "ipp://192.168.1.50/ipp/print")
        self.assertEqual(listed[0]["description"], "Brother HL-L2350DW")
        self.assertEqual(listed[0]["location"], "Office")
        self.assertEqual(listed[0]["ipp_url"], "ipp://10.0.2.2:631/printers/Brother_HL")
        self.assertEqual(listed[1]["state"], "disabled")
        self.assertFalse(listed[1]["enabled"])

        discovered = await mgr.discover(timeout=3.0)
        uris = [d["uri"] for d in discovered]
        self.assertIn("ipp://192.168.1.50/ipp/print", uris)
        self.assertIn("socket://192.168.1.60:9100", uris)
        self.assertIn("usb://HP/LaserJet%20P1102w?serial=ABC123", uris)
        self.assertIn("ipp://127.0.0.1:60000/ipp/print", uris)
        # RetroBoot loopback and IPv6 duplicate of Brother IPv4 were filtered/deduped.
        self.assertFalse(any("127.0.0.1:631" in u for u in uris))
        self.assertEqual(sum(1 for d in discovered if d["name"] == "Brother HL-L2350DW"), 1)
        brother_disc = next(d for d in discovered if d["uri"] == "ipp://192.168.1.50/ipp/print")
        self.assertTrue(brother_disc["already_added"])
        self.assertEqual(brother_disc["driver_hint"], "everywhere")
        epson_disc = next(d for d in discovered if d["uri"] == "socket://192.168.1.60:9100")
        self.assertFalse(epson_disc["already_added"])

        # Add printer with auto fallback (everywhere -> generic.ppd).
        added = await mgr.add_printer("Epson_ET", "socket://192.168.1.60:9100", description="Epson", location="Desk")
        self.assertEqual(added["driver"], "generic_ps")
        added_ipp = await mgr.add_printer("Canon_MF", "ipp://192.168.1.70/ipp/print", driver="auto")
        self.assertEqual(added_ipp["driver"], "generic_ps")
        added_pcl = await mgr.add_printer("HP_PCL", "socket://192.168.1.71:9100", driver="generic_pcl")
        self.assertEqual(added_pcl["driver"], "generic_pcl")
        pcl_cmd = [c for c, _ in calls if c[0] == "lpadmin" and "HP_PCL" in c][-1]
        self.assertIn("drv:///sample.drv/generpcl.ppd", pcl_cmd)

        # Print PDF checks `--`, `-h`, `CUPS_SERVER`, and request id parsing.
        pr = await mgr.print_pdf("Brother_HL", Path("/tmp/test.pdf"), "-leading-title")
        self.assertEqual(pr["job_id"], "Brother_HL-13")
        lp_cmd, lp_env = [x for x in calls if x[0][0] == "lp"][-1]
        self.assertEqual(lp_env["CUPS_SERVER"], "/run/cups/cups.sock")
        self.assertEqual(lp_cmd[:4], ["lp", "-h", "/run/cups/cups.sock", "-d"])
        self.assertIn("--", lp_cmd)

        jobs = await mgr.list_jobs()
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["job_id"], "Brother_HL-12")
        self.assertEqual(jobs[0]["state"], "active")
        self.assertEqual(jobs[1]["state"], "completed")

        self.assertEqual(await mgr.cancel_job("Brother_HL-12"), {"canceled": "Brother_HL-12"})
        with self.assertRaises(cups.CupsError):
            await mgr.cancel_job("-bad-id")

        self.assertEqual(await mgr.remove_printer("Epson_ET"), {"removed": "Epson_ET"})
        with self.assertRaises(cups.CupsError) as ctx:
            await mgr.remove_printer("Missing")
        self.assertEqual(ctx.exception.code, "not_found")

    async def test_brother_mfc_l8900cdw_dedup_everywhere_guard_and_partial_timeout(self):
        calls: list[list[str]] = []

        avahi_ipp = (
            "=;eth0;IPv4;Brother\\032MFC-L8900CDW\\032series;_ipp._tcp;local;"
            "brother-mfc.local;192.168.1.88;631;\"rp=ipp/print\" \"ty=Brother MFC-L8900CDW series\" "
            "\"pdl=application/pdf,image/pwg-raster,image/urf\"\n"
        )
        avahi_ipps = (
            "=;eth0;IPv4;Brother\\032MFC-L8900CDW\\032series;_ipps._tcp;local;"
            "brother-mfc.local;192.168.1.88;443;\"rp=ipp/print\" \"ty=Brother MFC-L8900CDW series\"\n"
        )
        avahi_pdl = (
            "=;eth0;IPv4;Brother\\032MFC-L8900CDW\\032series;_pdl-datastream._tcp;local;"
            "brother-mfc.local;192.168.1.88;9100;\"ty=Brother MFC-L8900CDW series\" "
            "\"pdl=application/pdf,application/postscript\"\n"
        )
        avahi_lpd = (
            "=;eth0;IPv4;Brother\\032MFC-L8900CDW\\032series;_printer._tcp;local;"
            "brother-mfc.local;192.168.1.88;515;\"rp=BINARY_P1\" \"ty=Brother MFC-L8900CDW series\"\n"
        )
        lpinfo_partial = (
            "network dnssd://Brother%20MFC-L8900CDW%20series._pdl-datastream._tcp.local/?uuid=abc&mdl=MFC-L8900CDW\n"
            "network dnssd://Brother%20MFC-L8900CDW%20series._ipp._tcp.local/?uuid=abc&mdl=MFC-L8900CDW\n"
        )

        async def fake_run(cmd, timeout=15.0, env=None, partial_on_timeout=False):
            calls.append(list(cmd))
            prog = cmd[0]
            if prog == "lpstat":
                return 1, "", "lpstat: No destinations added."
            if prog == "avahi-browse":
                svc = cmd[-1]
                if svc == "_ipp._tcp":
                    return 0, avahi_ipp, ""
                if svc == "_ipps._tcp":
                    return 0, avahi_ipps, ""
                if svc == "_pdl-datastream._tcp":
                    return 0, avahi_pdl, ""
                if svc == "_printer._tcp":
                    return 0, avahi_lpd, ""
            if prog == "lpinfo":
                # Simulate snmp backend timing out AFTER emitting partial output
                return (124 if partial_on_timeout else 0), lpinfo_partial, "lpinfo: timed out"
            if prog == "driverless":
                return 124, "ipp://Brother%20MFC-L8900CDW%20series._ipp._tcp.local/ipp/print\n", "driverless: timed out"
            if prog == "lpadmin":
                if "-m" in cmd:
                    model = cmd[cmd.index("-m") + 1]
                    uri = cmd[cmd.index("-v") + 1]
                    if model == "everywhere" and not uri.startswith(("ipp://", "ipps://")):
                        return 1, "", "lpadmin: IPP Everywhere driver requires an IPP connection."
                return 0, "", ""
            if prog in ("cupsenable", "cupsaccept"):
                return 0, "", ""
            return 0, "", ""

        mgr = cups.CupsManager(socket_path="/run/cups/cups.sock", runner=fake_run)
        found = await mgr.discover(timeout=5.0)
        # Collapsed into ONE single best IPP candidate for MFC-L8900CDW!
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["uri"], "ipp://192.168.1.88/ipp/print")
        self.assertEqual(found[0]["transport"], "ipp")
        self.assertEqual(found[0]["driver_hint"], "everywhere")

        # Even if lpinfo/driverless timed out (rc=124) without avahi, partial discovery still succeeds:
        async def avahi_timeout_run(cmd, timeout=15.0, env=None, partial_on_timeout=False):
            if cmd[0] == "avahi-browse":
                return 124, "", "avahi-browse: timed out"
            return await fake_run(cmd, timeout=timeout, env=env, partial_on_timeout=partial_on_timeout)

        mgr_partial = cups.CupsManager(socket_path="/run/cups/cups.sock", runner=avahi_timeout_run)
        found_partial = await mgr_partial.discover(timeout=5.0)
        self.assertEqual(len(found_partial), 1)
        self.assertEqual(found_partial[0]["transport"], "ipp")

        # Requesting driver="everywhere" on a socket:// or non-IPP dnssd:// URI must NEVER fail with IPP error:
        res_sock = await mgr.add_printer("MFC_Socket", "socket://192.168.1.88:9100", driver="everywhere")
        self.assertEqual(res_sock["driver"], "generic_ps")
        res_dnssd = await mgr.add_printer(
            "MFC_DNSSD",
            "dnssd://Brother%20MFC-L8900CDW%20series._pdl-datastream._tcp.local/?uuid=abc&mdl=MFC-L8900CDW",
            driver="everywhere",
        )
        self.assertEqual(res_dnssd["driver"], "generic_ps")
        # Confirm `-m everywhere` was never even passed for those non-IPP URIs
        lp_models = [c[c.index("-m") + 1] for c in calls if c[0] == "lpadmin" and "-m" in c]
        self.assertEqual(lp_models, ["drv:///sample.drv/generic.ppd", "drv:///sample.drv/generic.ppd"])


class PrintServerRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_lpd_raw_ipp_eot_and_cups_routing(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        printed = []
        converted_inputs: list[bytes] = []
        cups_prints: list[tuple[str, bytes, str]] = []

        async def mock_conv(ps: bytes, out_pdf: Path):
            converted_inputs.append(ps)
            out_pdf.write_bytes(b"%PDF-1.4\n%ps-len:" + str(len(ps)).encode() + b"\n%%EOF\n")

        class FakeCups:
            async def list_printers(self):
                return [{"name": "Brother_HL", "uri": "ipp://192.168.1.50/ipp/print", "state": "idle"}]

            async def print_pdf(self, queue: str, pdf_path: Path, job_name: str = "Mac OS Print"):
                cups_prints.append((queue, pdf_path.read_bytes(), job_name))
                return {"queue": queue, "job_id": f"{queue}-99", "title": job_name}

        target_state = {"val": "pdf", "keep": False}
        srv = printer.PrintServer(
            root,
            enabled=True,
            on_printed=printed.append,
            lpd_port=0,
            ipp_port=0,
            raw_port=0,
            converter=mock_conv,
            cups=FakeCups(),
            target=lambda: target_state["val"],
            keep_pdf=lambda: target_state["keep"],
        )
        await srv.start()
        self.addAsyncCleanup(srv.stop)
        self.assertIsNotNone(srv.bound_raw_port)

        # 1. LaserWriter EOT stripping preserves inner \x04.
        raw_ps = b"\x04%!PS-Adobe-3.0\n%inner:\x04keep\nshowpage\n%%EOF\n\x04"
        res_eot = await srv.process_job(raw_ps, "EOT Doc", source="lpd", queue="pdf")
        self.assertEqual(res_eot["destination"], "pdf")
        self.assertEqual(converted_inputs[-1], b"%!PS-Adobe-3.0\n%inner:\x04keep\nshowpage\n%%EOF\n")

        # 2. LPD queue routing: explicit CUPS queue even when target is "pdf".
        r, w = await asyncio.open_connection("127.0.0.1", srv.bound_lpd_port)
        w.write(b"\x02Brother_HL\n")
        self.assertEqual(await r.readexactly(1), b"\x00")
        ps = b"%!PS-Adobe-3.0\nshowpage\n%%EOF\n"
        w.write(f"\x03{len(ps)} dfA001mac\n".encode() + ps + b"\x00")
        self.assertEqual(await r.readexactly(1), b"\x00")
        self.assertEqual(await r.readexactly(1), b"\x00")
        w.close()
        await helpers.wait_for(lambda: len(cups_prints) >= 1, 5, 0.05, "LPD CUPS forward")
        self.assertEqual(cups_prints[-1][0], "Brother_HL")
        # keep_pdf=False so no extra PDF written to Printed PDFs for that job.
        self.assertEqual(len(printed), 1)

        # 3. Default target = "cups:Brother_HL" with keep_pdf=True -> dual output via Raw 9100!
        target_state["val"] = "cups:Brother_HL"
        target_state["keep"] = True
        rr, rw = await asyncio.open_connection("127.0.0.1", srv.bound_raw_port)
        raw_stream = b"%!PS-Adobe-3.0\n%%Title: (Laser Memo)\nshowpage\n%%EOF\n"
        rw.write(raw_stream)
        rw.write_eof()
        await rw.drain()
        rw.close()
        await helpers.wait_for(lambda: len(cups_prints) >= 2 and len(printed) >= 2, 5, 0.05, "Raw 9100 dual output")
        self.assertEqual(cups_prints[-1][0], "Brother_HL")
        self.assertEqual(cups_prints[-1][2], "Laser Memo")
        self.assertEqual(printed[-1]["job"], "Laser Memo")

        # 4. LPD queue="pdf" overrides target="cups:Brother_HL".
        cups_before = len(cups_prints)
        res_override = await srv.process_job(ps, "Force PDF", source="lpd", queue="pdf")
        self.assertEqual(res_override["destination"], "pdf")
        self.assertEqual(len(cups_prints), cups_before)

        # 5. IPP Get-Printer-Attributes, Create-Job, Send-Document, Get-Job-Attributes, Cancel-Job.
        ir, iw = await asyncio.open_connection("127.0.0.1", srv.bound_ipp_port)
        self.addCleanup(iw.close)

        async def ipp_call(op_id: int, req_id: int, attrs: bytes = b"", payload: bytes = b"") -> tuple[int, int, dict]:
            req = _ipp_req(op_id, req_id, attrs, payload)
            http = (
                f"POST /printers/Brother_HL HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                f"Content-Type: application/ipp\r\nContent-Length: {len(req)}\r\n\r\n"
            ).encode("latin-1") + req
            iw.write(http)
            await iw.drain()
            hdr = await ir.readuntil(b"\r\n\r\n")
            clen = 0
            for ln in hdr.decode("latin-1").split("\r\n"):
                if ln.lower().startswith("content-length:"):
                    clen = int(ln.split(":", 1)[1].strip())
            body = await ir.readexactly(clen)
            return _ipp_parse_resp(body)

        st, rid, pattrs = await ipp_call(0x000B, 10)
        self.assertEqual((st, rid), (0x0000, 10))
        self.assertIn(b"ipp://10.0.2.2:", pattrs["printer-uri-supported"][0])
        ops = [struct.unpack(">i", v)[0] for v in pattrs["operations-supported"]]
        self.assertEqual(ops, [2, 4, 5, 6, 8, 9, 10, 11])

        st_c, _, cattrs = await ipp_call(0x0005, 11, _ipp_attr(0x42, b"job-name", b"MultiStep"))
        self.assertEqual(st_c, 0x0000)
        jid = struct.unpack(">i", cattrs["job-id"][0])[0]

        st_s, _, sattrs = await ipp_call(
            0x0006, 12,
            _ipp_attr(0x21, b"job-id", struct.pack(">i", jid)) + _ipp_attr(0x22, b"last-document", b"\x01"),
            b"%PDF-1.4\n%%EOF\n",
        )
        self.assertEqual(st_s, 0x0000)
        self.assertEqual(struct.unpack(">i", sattrs["job-state"][0])[0], 9)
        self.assertEqual(cups_prints[-1][2], "MultiStep")

        st_g, _, gattrs = await ipp_call(0x0009, 13, _ipp_attr(0x21, b"job-id", struct.pack(">i", jid)))
        self.assertEqual(st_g, 0x0000)
        self.assertEqual(gattrs["job-name"][0], b"MultiStep")

        st_c2, _, cattrs2 = await ipp_call(0x0005, 14, _ipp_attr(0x42, b"job-name", b"CanceledDoc"))
        self.assertEqual(st_c2, 0x0000)
        jid2 = struct.unpack(">i", cattrs2["job-id"][0])[0]
        st_x, _, _ = await ipp_call(0x0008, 15, _ipp_attr(0x21, b"job-id", struct.pack(">i", jid2)))
        self.assertEqual(st_x, 0x0000)

        st_gj, _, gjattrs = await ipp_call(0x000A, 16)
        self.assertEqual(st_gj, 0x0000)
        self.assertIn("job-id", gjattrs)

        st_ju, _, juattrs = await ipp_call(0x0009, 17, _ipp_attr(0x45, b"job-uri", f"ipp://10.0.2.2:631/jobs/{jid}".encode()))
        self.assertEqual(st_ju, 0x0000)
        self.assertEqual(juattrs["job-name"][0], b"MultiStep")

        # 6. print_test_page & history
        test_res = await srv.print_test_page("pdf", "3.2.0")
        self.assertEqual(test_res["destination"], "pdf")
        self.assertEqual(test_res["source"], "test")
        self.assertGreaterEqual(len(srv.list_jobs()), 4)
        self.assertEqual(srv.public()["target"], "cups:Brother_HL")

    async def test_lpd_macos9_duplicate_subcommand_and_count_zero_streaming(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        converted: list[bytes] = []

        async def mock_conv(ps: bytes, out_pdf: Path):
            converted.append(ps)
            out_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")

        srv = printer.PrintServer(root, enabled=True, lpd_port=0, ipp_port=0, converter=mock_conv)
        await srv.start()
        self.addCleanup(srv.stop)

        # 1. Simulate Mac OS 9 Desktop PrintMonitor re-sending subcommands after 0x00 ACK
        r, w = await asyncio.open_connection("127.0.0.1", srv.bound_lpd_port)
        w.write(b"\x02retroboot\n")
        await w.drain()
        self.assertEqual(await r.readexactly(1), b"\x00")

        ctrl = b"H\nP\nldfA254\nUdfA254\nNPrintQA.txt\nJPrintQA.txt\n"
        cf_sub = f"\x02{len(ctrl)} cfA254\n".encode()
        w.write(cf_sub)
        await w.drain()
        self.assertEqual(await r.readexactly(1), b"\x00")
        w.write(cf_sub)
        await w.drain()
        self.assertEqual(await r.readexactly(1), b"\x00")
        w.write(ctrl + b"\x00")
        await w.drain()
        self.assertEqual(await r.readexactly(1), b"\x00")

        ps_body = b"%!PS-Adobe-3.0\n%%Title: PrintQA\nshowpage\n%%EOF\n"
        df_sub = f"\x03{len(ps_body)} dfA254\n".encode()
        w.write(df_sub)
        await w.drain()
        self.assertEqual(await r.readexactly(1), b"\x00")
        w.write(df_sub)
        await w.drain()
        self.assertEqual(await r.readexactly(1), b"\x00")
        w.write(ps_body + b"\x00")
        await w.drain()
        self.assertEqual(await r.readexactly(1), b"\x00")
        w.write(b"\x00")  # trailing NUL before FIN
        w.close()
        await w.wait_closed()
        await helpers.wait_for(lambda: srv.jobs_count >= 1, 5, 0.02, "Mac OS 9 LPD dup-sub")
        self.assertIn(b"PrintQA", converted[0])

        # 2. Simulate count == 0 multi-chunk EOF streaming
        r2, w2 = await asyncio.open_connection("127.0.0.1", srv.bound_lpd_port)
        w2.write(b"\x02retroboot\n")
        await w2.drain()
        self.assertEqual(await r2.readexactly(1), b"\x00")
        w2.write(b"\x030 dfA255\n")
        await w2.drain()
        self.assertEqual(await r2.readexactly(1), b"\x00")
        w2.write(b"%!PS-Adobe-3.0\n" + b"%chunk1\n" * 5000)
        await w2.drain()
        await asyncio.sleep(0.02)
        w2.write(b"%chunk2\n" * 5000 + b"\n%%EOF\n")
        await w2.drain()
        w2.write_eof()
        self.assertEqual(await r2.readexactly(1), b"\x00")
        w2.close()
        await w2.wait_closed()
        await helpers.wait_for(lambda: srv.jobs_count >= 2, 5, 0.02, "LPD count=0 EOF streaming")
        self.assertIn(b"%chunk2", converted[1])


class PrinterWebApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not hasattr(os, "sched_setaffinity"):
            p1 = patch.object(os, "sched_setaffinity", lambda *a: None, create=True)
            p2 = patch.object(os, "sched_getaffinity", lambda *a: {0}, create=True)
            p1.start()
            p2.start()
            self.addCleanup(p1.stop)
            self.addCleanup(p2.stop)
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        self.cfg = helpers.make_config(self.root, allow_network_tools=False)
        self.app = Hostd(self.cfg)
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()
        self.app.web.tls()
        self.client = TestClient(TestServer(self.app.web.web_app, host="127.0.0.1"))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        code, _ = self.app.pairing.current_code()
        r = await self.client.post("/api/v1/pair", json={"code": code, "device_name": "test"})
        self.cookie = r.headers.getall("Set-Cookie")[0].split("rb_session=", 1)[1].split(";", 1)[0]
        self.csrf = (await r.json())["csrf"]

    def h(self, csrf: bool = True, extra: dict | None = None) -> dict:
        hd = {"Cookie": f"rb_session={self.cookie}"}
        if csrf:
            hd["X-RetroBoot-CSRF"] = self.csrf
        if extra:
            hd.update(extra)
        return hd

    async def test_printers_api_auth_csrf_crud_target_and_test(self):
        # Auth & CSRF checks
        unpaired = await self.client.get("/api/v1/printers")
        self.assertEqual(unpaired.status, 401)
        no_csrf = await self.client.post("/api/v1/printers/discover", json={}, headers=self.h(csrf=False))
        self.assertEqual(no_csrf.status, 403)

        configured: list[dict] = []
        cups_jobs: list[dict] = [{"job_id": "Brother_HL-5", "queue": "Brother_HL", "user": "rbguest",
                                  "bytes": 512, "date": "Tue", "state": "active"}]

        async def fake_list_printers():
            return list(configured)

        async def fake_discover(timeout=6.0):
            return [{
                "id": "abc123", "name": "Brother HL", "model": "Brother HL-L2350DW",
                "uri": "ipp://192.168.1.50/ipp/print", "transport": "ipp",
                "driver_hint": "everywhere", "address": "192.168.1.50",
                "already_added": any(p["name"] == "Brother_HL" for p in configured),
            }]

        async def fake_add(queue, uri, *, description="", location="", driver="auto"):
            rec = {
                "name": queue, "state": "idle", "enabled": True, "accepting": True,
                "uri": uri, "description": description, "location": location,
                "lpd_queue": queue, "ipp_url": f"ipp://10.0.2.2:631/printers/{queue}",
                "driver": driver,
            }
            configured.append(rec)
            return rec

        async def fake_remove(queue):
            before = len(configured)
            configured[:] = [p for p in configured if p["name"] != queue]
            if len(configured) == before:
                raise cups.CupsError("Not found", "not_found")
            return {"removed": queue}

        async def fake_print_pdf(queue, pdf_path, job_name="Mac OS Print"):
            return {"queue": queue, "job_id": f"{queue}-77", "title": job_name}

        async def fake_list_jobs(queue=None):
            return list(cups_jobs)

        async def fake_cancel_job(job_id):
            if job_id != "Brother_HL-5":
                raise cups.CupsError("Not found", "not_found")
            cups_jobs.clear()
            return {"canceled": job_id}

        self.app.cups.available = lambda: (True, None)
        self.app.cups.list_printers = fake_list_printers
        self.app.cups.discover = fake_discover
        self.app.cups.add_printer = fake_add
        self.app.cups.remove_printer = fake_remove
        self.app.cups.print_pdf = fake_print_pdf
        self.app.cups.list_jobs = fake_list_jobs
        self.app.cups.cancel_job = fake_cancel_job

        async def fake_conv(ps: bytes, out_pdf: Path):
            out_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")

        self.app.printer._converter = fake_conv

        # 1. Discover
        dr = await self.client.post("/api/v1/printers/discover", json={"timeout": 4}, headers=self.h())
        self.assertEqual(dr.status, 200)
        self.assertEqual((await dr.json())["printers"][0]["uri"], "ipp://192.168.1.50/ipp/print")

        # 2. Add via URI + set_as_target=True
        ar = await self.client.post("/api/v1/printers", json={
            "name": "Brother HL",
            "uri": "ipp://192.168.1.50/ipp/print",
            "set_as_target": True,
        }, headers=self.h())
        self.assertEqual(ar.status, 201, await ar.text())
        abody = await ar.json()
        self.assertEqual(abody["printer"]["name"], "Brother-HL")
        self.assertEqual(abody["target"], "cups:Brother-HL")

        # 3. Add via manual protocol/host/port/path
        mr = await self.client.post("/api/v1/printers", json={
            "name": "Office Socket",
            "protocol": "socket",
            "host": "192.168.1.60",
            "port": 9100,
        }, headers=self.h())
        self.assertEqual(mr.status, 201, await mr.text())
        self.assertEqual((await mr.json())["printer"]["uri"], "socket://192.168.1.60:9100")

        # Loopback rejected with 422
        bad_add = await self.client.post("/api/v1/printers", json={
            "name": "Loopback", "uri": "ipp://127.0.0.1:631/printers/retroboot",
        }, headers=self.h())
        self.assertEqual(bad_add.status, 422)

        # 4. GET /api/v1/printers & /api/v1/status
        gr = await self.client.get("/api/v1/printers", headers=self.h())
        self.assertEqual(gr.status, 200)
        gbody = await gr.json()
        self.assertTrue(gbody["cups_available"])
        self.assertEqual(gbody["virtual"]["target"], "cups:Brother-HL")
        self.assertTrue(next(p for p in gbody["printers"] if p["name"] == "Brother-HL")["is_target"])
        self.assertEqual(gbody["jobs"][0]["job_id"], "Brother_HL-5")

        st = await (await self.client.get("/api/v1/status", headers=self.h())).json()
        self.assertEqual(st["printer"]["target"], "cups:Brother-HL")

        # 5. Test page to PDF and CUPS printer, plus unknown 404
        tr_pdf = await self.client.post("/api/v1/printers/pdf/test", json={}, headers=self.h())
        self.assertEqual(tr_pdf.status, 200)
        self.assertEqual((await tr_pdf.json())["job"]["destination"], "pdf")
        tr_cups = await self.client.post("/api/v1/printers/Brother-HL/test", json={}, headers=self.h())
        self.assertEqual(tr_cups.status, 200)
        self.assertEqual((await tr_cups.json())["job"]["cups_job_id"], "Brother-HL-77")
        tr_404 = await self.client.post("/api/v1/printers/Missing/test", json={}, headers=self.h())
        self.assertEqual(tr_404.status, 404)

        # 6. Cancel job
        cr = await self.client.post("/api/v1/printers/jobs/Brother_HL-5/cancel", json={}, headers=self.h())
        self.assertEqual(cr.status, 200)
        self.assertEqual((await cr.json())["canceled"], "Brother_HL-5")

        # 7. Target switch & validation
        bad_tgt = await self.client.post("/api/v1/printers/target", json={"target": "cups:Missing"}, headers=self.h())
        self.assertEqual(bad_tgt.status, 404)
        ok_tgt = await self.client.post(
            "/api/v1/printers/target",
            json={"target": "cups:Office-Socket", "keep_pdf": True},
            headers=self.h(),
        )
        self.assertEqual(ok_tgt.status, 200)
        self.assertEqual((await ok_tgt.json())["keep_pdf"], True)

        # 8. Delete active target resets printer.target back to "pdf"
        del_r = await self.client.delete("/api/v1/printers/Office-Socket", headers=self.h())
        self.assertEqual(del_r.status, 200)
        self.assertEqual((await del_r.json())["target"], "pdf")
        self.assertEqual(self.app.settings.applied["printer.target"], "pdf")

        # 9. Settings schema validation for printer.target / keep_pdf + apply live sync
        self.app.printer.sync = AsyncMock(return_value=self.app.printer.public())
        base = schema.defaults()
        good_s = schema.validate_changes(base, {
            "printer.target": "cups:Brother_HL",
            "printer.keep_pdf": True,
        }, ctx=None)
        self.assertEqual(good_s["printer.target"], "cups:Brother_HL")
        for bad_val in ("", "cups:", "cups:pdf", "cups:lp", "cups:-bad", "lpd://host"):
            with self.assertRaises(schema.ValidationError, msg=bad_val):
                schema.validate_changes(base, {"printer.target": bad_val}, ctx=None)

        await self.app.apply_engine.change_and_apply({"printer.target": "cups:Brother_HL"}, origin="web")
        self.app.printer.sync.assert_awaited()


if __name__ == "__main__":
    unittest.main()
