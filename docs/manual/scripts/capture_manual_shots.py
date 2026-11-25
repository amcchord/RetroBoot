#!/usr/bin/env python3
"""Generate and verify all 41 user-manual screenshots under docs/images/manual/.

Sources:
  - Group A (20 images): live Proxmox / ARM64 captures from MAIN_REPO/private/releases/.
    Centered setup/installer dialogs are cropped cleanly around the Platinum window
    with 44 px checkerboard margin on all 4 sides; full desktop/panel scenes are
    copied 1:1.
  - Group C (15 images): fresh 3.4.1 Web Management UI screenshots against an
    in-process HTTPS server wrapping web/dev/mock_server.py (MIT Platinum theme;
    RBUI_ASSETS / RB_PLATINUM_BUNDLE / --platinum never used), captured with
    chromium_headless_shell (or Chrome fallback).
  - Group D (6 images): fresh 3.4.1 local Platinum panel renders generated from
    hostd/retroboot/panel/ scripts on a scratch Hostd and rendered via
    presenter/ui/build-make/rbui-render (MIT Platinum, 960x540, checkerboard,
    --no-cursor) cropped to window bounds + 32 px checkerboard padding.

Run from the worktree root:
    python3 docs/manual/scripts/capture_manual_shots.py
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import glob
import hashlib
import http.server
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[3]


def detect_main_repo(worktree: Path) -> Path:
    raw = subprocess.check_output(
        ["git", "-C", str(worktree), "rev-parse", "--git-common-dir"],
        text=True,
    ).strip()
    git_common = Path(raw)
    if not git_common.is_absolute():
        git_common = worktree / git_common
    return git_common.resolve().parent


MAIN_REPO = detect_main_repo(WORKTREE)
OUT_DIR = WORKTREE / "docs" / "images" / "manual"
RBUI_RENDER = WORKTREE / "presenter" / "ui" / "build-make" / "rbui-render"

# ---------------------------------------------------------------------------
# Pure-Python PNG decoder / cropper / encoder (stdlib only; no Pillow)
# ---------------------------------------------------------------------------

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_png(path: Path) -> tuple[int, int, int, list[bytes]]:
    """Decode an 8-bit PNG into (width, height, channels_rgb_or_rgba, row_bytes_list)."""
    data = path.read_bytes()
    if data[:8] != PNG_SIG:
        raise ValueError(f"{path} is not a valid PNG")
    pos = 8
    w = h = bd = ct = inter = 0
    plte: list[tuple[int, int, int]] | None = None
    trns: bytes | None = None
    idat = bytearray()
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            w, h, bd, ct, _comp, _flt, inter = struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"PLTE":
            plte = [(chunk[i], chunk[i + 1], chunk[i + 2]) for i in range(0, len(chunk), 3)]
        elif ctype == b"tRNS":
            trns = chunk
        elif ctype == b"IDAT":
            idat.extend(chunk)
        elif ctype == b"IEND":
            break
    if inter != 0 or bd != 8:
        raise ValueError(f"{path}: unsupported PNG format (bd={bd}, interlace={inter})")
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ct]
    stride = w * bpp
    raw = zlib.decompress(bytes(idat))
    if len(raw) != (stride + 1) * h:
        raise ValueError(f"{path}: unexpected decompressed size {len(raw)} vs {(stride + 1) * h}")

    prev = bytearray(stride)
    rows_raw: list[bytes] = []
    idx = 0
    for _ in range(h):
        flt = raw[idx]
        idx += 1
        row = bytearray(raw[idx:idx + stride])
        idx += stride
        if flt == 1:
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif flt == 2:
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif flt == 3:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif flt == 4:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + _paeth(a, b, c)) & 0xFF
        elif flt != 0:
            raise ValueError(f"{path}: unsupported PNG filter {flt}")
        rows_raw.append(bytes(row))
        prev = row

    if ct in (2, 6):
        return w, h, bpp, rows_raw

    out_ch = 4 if (ct in (4, 6) or (ct == 3 and trns is not None)) else 3
    out_rows: list[bytes] = []
    for r in rows_raw:
        buf = bytearray(w * out_ch)
        if ct == 0:
            for x in range(w):
                v = r[x]
                buf[x * 3:x * 3 + 3] = bytes((v, v, v))
        elif ct == 3:
            assert plte is not None
            for x in range(w):
                rgb = plte[r[x]]
                if out_ch == 4:
                    a = trns[r[x]] if trns is not None and r[x] < len(trns) else 255
                    buf[x * 4:x * 4 + 4] = bytes((rgb[0], rgb[1], rgb[2], a))
                else:
                    buf[x * 3:x * 3 + 3] = bytes(rgb)
        elif ct == 4:
            for x in range(w):
                v, a = r[x * 2], r[x * 2 + 1]
                buf[x * 4:x * 4 + 4] = bytes((v, v, v, a))
        out_rows.append(bytes(buf))
    return w, h, out_ch, out_rows


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def encode_png(w: int, h: int, ch: int, rows: list[bytes]) -> bytes:
    ct = 2 if ch == 3 else 6
    raw = bytearray()
    for r in rows:
        raw.append(0)
        raw.extend(r)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, ct, 0, 0, 0)
    return PNG_SIG + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _png_chunk(b"IEND", b"")


def crop_png(src: Path, dst: Path, box: tuple[int, int, int, int]) -> tuple[int, int]:
    w, h, ch, rows = decode_png(src)
    x0, y0, x1, y1 = box
    x0 = max(0, x0)
    y0 = max(0, y0)
    x1 = min(w, x1)
    y1 = min(h, y1)
    cw, ch_h = x1 - x0, y1 - y0
    cropped = [rows[y][x0 * ch:x1 * ch] for y in range(y0, y1)]
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(encode_png(cw, ch_h, ch, cropped))
    return cw, ch_h


def verify_png(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data[:8] != PNG_SIG or len(data) < 33:
        raise ValueError(f"Invalid PNG header: {path}")
    w, h = struct.unpack(">II", data[16:24])
    if w <= 0 or h <= 0:
        raise ValueError(f"Invalid PNG dimensions {w}x{h}: {path}")
    # Also verify full decode works without error
    decode_png(path)
    return w, h


# ---------------------------------------------------------------------------
# Group A — 20 Live Proxmox / ARM64 Captures (from MAIN_REPO/private/releases)
# ---------------------------------------------------------------------------

GROUP_A_DIALOGS = [
    ("installer-welcome.png", "3.4.2/qa-350-step1.png"),
    ("installer-hardware.png", "3.4.2/qa-350-hwcheck.png"),
    ("installer-disk.png", "3.4.2/qa-350-disk.png"),
    ("installer-erase.png", "3.4.2/qa-350-erase.png"),
    ("installer-macs.png", "3.4.2/qa-350-macs.png"),
    ("installer-display.png", "3.4.0/qa-348-tz-actual.png"),
    ("installer-timezone.png", "3.4.0/qa-348-tz-screen.png"),
    ("installer-progress.png", "3.4.2/qa-350-inst-dl.png"),
    ("installer-done.png", "3.4.2/qa-350-inst2.png"),
    ("pi45-first-setup.png", "3.4.2/qa-pi45-latest.png"),
]

GROUP_A_FULL = [
    ("finder-mega-deluxe-v6.png", "3.4.2/qa-350-finder.png"),
    ("panel-main-341.png", "3.4.2/qa-350-cad.png"),
    ("panel-printers-341.png", "3.4.2/qa-350-printers-click.png"),
    ("macos9-print-dialog.png", "3.4.1/qa-348-printdlg2.png"),
    ("panel-keyboard-mouse.png", "3.2.0/qa-342-keyboard-mouse.png"),
    ("panel-web-helper.png", "3.2.0/qa-342-webhelper.png"),
    ("panel-web-helper-whitelist.png", "3.2.0/qa-342-whitelist.png"),
    ("panel-force-reboot.png", "3.2.0/qa-342-force-reboot.png"),
    ("macos9-volume-osd.png", "3.4.2/qa-350-vol-osd.png"),
    ("zero2w-system755.png", "3.4.2/qa-zero2w-latest.png"),
]


def detect_checkerboard_bbox(path: Path) -> tuple[int, int, int, int]:
    w, h, ch, rows = decode_png(path)
    c0 = rows[0][:3]
    c1 = rows[0][ch:ch + 3]
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        row = rows[y]
        for x in range(w):
            exp = c0 if (x + y) % 2 == 0 else c1
            off = x * ch
            if row[off:off + 3] != exp:
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y
    if maxx < minx or maxy < miny:
        raise ValueError(f"Could not detect window bounds in {path}")
    return minx, miny, maxx + 1, maxy + 1


def build_group_a() -> None:
    rel_root = MAIN_REPO / "private" / "releases"
    print("=== Group A: Live Proxmox / ARM64 Captures ===")
    for out_name, rel_src in GROUP_A_DIALOGS:
        src = rel_root / rel_src
        dst = OUT_DIR / out_name
        x0, y0, x1, y1 = detect_checkerboard_bbox(src)
        margin = 44
        cw, ch_h = crop_png(src, dst, (x0 - margin, y0 - margin, x1 + margin, y1 + margin))
        print(f"  [A-crop] {out_name:34s} <- {rel_src} ({cw}x{ch_h})")

    for out_name, rel_src in GROUP_A_FULL:
        src = rel_root / rel_src
        dst = OUT_DIR / out_name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        w, h = verify_png(dst)
        print(f"  [A-copy] {out_name:34s} <- {rel_src} ({w}x{h})")


# ---------------------------------------------------------------------------
# Group C — 15 Fresh 3.4.1 Web Management UI Screenshots
# ---------------------------------------------------------------------------

GROUP_C_SHOTS = [
    ("web-pairing.png", "/#/overview", (1024, 700), 1, False),
    ("web-overview.png", "/#/overview", (1280, 860), 1, True),
    ("web-screen.png", "/#/screen", (1280, 860), 1, True),
    ("web-files.png", "/#/files", (1280, 860), 1, True),
    ("web-machine.png", "/#/machine", (1280, 860), 1, True),
    ("web-display.png", "/#/display", (1280, 960), 1, True),
    ("web-sound.png", "/#/sound", (1280, 760), 1, True),
    ("web-printers.png", "/#/printers", (1280, 920), 1, True),
    ("web-storage.png", "/#/storage", (1280, 980), 1, True),
    ("web-network.png", "/#/network", (1280, 980), 1, True),
    ("web-diagnostics.png", "/#/diagnostics", (1280, 860), 1, True),
    ("web-recovery.png", "/#/recovery", (1280, 860), 1, True),
    ("web-update.png", "/#/update", (1280, 920), 1, True),
    ("web-devices.png", "/#/devices", (1280, 720), 1, True),
    ("web-phone-overview.png", "/#/overview", (390, 844), 2, True),
]

HEADLESS_SHELL_GLOBS = [
    "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-*/headless_shell",
    "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-*/headless_shell",
]

CHROME_CANDIDATES = [
    "chrome-headless-shell",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "chromium",
]


def find_chrome() -> str:
    for pattern in HEADLESS_SHELL_GLOBS:
        hits = sorted(glob.glob(os.path.expanduser(pattern)))
        if hits:
            return hits[-1]
    for c in CHROME_CANDIDATES:
        path = c if os.path.isabs(c) else shutil.which(c)
        if path and os.path.exists(path):
            return path
    raise SystemExit("No chromium_headless_shell or Chrome found.")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_manual_mock():
    sys.path.insert(0, str(WORKTREE / "web" / "dev"))
    import mock_server  # noqa: E402
    from retroboot import schema as hschema  # noqa: E402

    args = mock_server.parser().parse_args([
        "--profile", "laptop",
        "--form-factor", "desktop",
        "--code", "246810",
        "--quiet",
    ])
    args.platinum = None  # Force MIT Platinum (never Apple-derived bundle)
    m = mock_server.Mock(args)

    now = mock_server.now()
    m.machines = {
        "os922-mega-deluxe-v6": {
            "id": "os922-mega-deluxe-v6",
            "name": "Mac OS 9.2.2 Mega Deluxe v6",
            "system": "Mac OS 9.2.2",
            "cpu_label": "PowerPC G4 (7400)",
            "disk_bytes": 32 * 1024 ** 3,
            "disk_used_bytes": int(4.2 * 1024 ** 3),
            "created_at": mock_server.iso(now - 86400 * 14),
            "source": "catalog:os922-mega-deluxe-v6",
        },
        "os81-deluxe-v2": {
            "id": "os81-deluxe-v2",
            "name": "Mac OS 8.1 Deluxe v2",
            "system": "8.1",
            "cpu_label": "Motorola 68040",
            "disk_bytes": 2 * 1024 ** 3,
            "disk_used_bytes": int(0.42 * 1024 ** 3),
            "created_at": mock_server.iso(now - 86400 * 7),
            "source": "catalog:os81-deluxe-v2",
            "family": "quadra800",
            "family_label": "68k",
        },
        "osx-tiger": {
            "id": "osx-tiger",
            "name": "Mac OS X 10.4.11 Tiger",
            "system": "Mac OS X 10.4.11",
            "cpu_label": "PowerPC G4 (7400)",
            "disk_bytes": 32 * 1024 ** 3,
            "disk_used_bytes": int(5.6 * 1024 ** 3),
            "created_at": mock_server.iso(now - 86400 * 3),
            "source": "catalog:osx-tiger-deluxe",
            "guest_os": "macosx-10.4",
        },
    }
    m.desired["machine.active"] = "os922-mega-deluxe-v6"
    m.applied["machine.active"] = "os922-mega-deluxe-v6"
    m.desired["management.hostname"] = "retroboot"
    m.applied["management.hostname"] = "retroboot"
    m.desired["update.url"] = "https://updates.mcchord.net/retroboot/"
    m.applied["update.url"] = "https://updates.mcchord.net/retroboot/"
    m.known_good["values"] = dict(m.applied)

    m.checkpoints = {
        "os922-mega-deluxe-v6": [
            {"id": "cp-20261120-1415", "label": "Before installing Myst", "created_at": mock_server.iso(now - 86400 * 5), "size_bytes": int(4.0 * 1024 ** 3), "clean": True},
            {"id": "cp-20261111-0930", "label": "Fresh Install", "kind": "fresh", "created_at": mock_server.iso(now - 86400 * 14), "size_bytes": int(3.8 * 1024 ** 3), "clean": True},
        ],
        "os81-deluxe-v2": [
            {"id": "cp-20261118-1000", "label": "Fresh Install", "kind": "fresh", "created_at": mock_server.iso(now - 86400 * 7), "size_bytes": int(0.4 * 1024 ** 3), "clean": True},
        ],
        "osx-tiger": [
            {"id": "cp-20261122-1100", "label": "Fresh Install", "kind": "fresh", "created_at": mock_server.iso(now - 86400 * 3), "size_bytes": int(5.5 * 1024 ** 3), "clean": True},
        ],
    }

    m.wifi = {
        "radio": True,
        "state": "connected",
        "ssid": "Home",
        "networks": [
            {"ssid": "Home", "signal": 86, "security": "wpa2", "known": True, "active": True},
            {"ssid": "RetroBoot Lab", "signal": 72, "security": "wpa2", "known": True, "active": False},
            {"ssid": "Neighbors", "signal": 34, "security": "wpa2", "known": False, "active": False},
            {"ssid": "Cafe Guest", "signal": 19, "security": "open", "known": False, "active": False},
        ],
        "profiles": [
            {"id": "wp-home", "ssid": "Home", "security": "wpa2", "autoconnect": True},
            {"id": "wp-lab", "ssid": "RetroBoot Lab", "security": "wpa2", "autoconnect": True},
        ],
        "scanned_at": now - 35,
    }

    m.printers = [
        {
            "name": "Brother_MFC-L8900CDW",
            "description": "Brother MFC-L8900CDW",
            "location": "Studio Desk",
            "uri": "ipp://192.168.1.25/ipp/print",
            "state": "idle",
            "state_message": "Ready",
            "accepting": True,
            "lpd_queue": "Brother_MFC-L8900CDW",
            "ipp_url": "ipp://retroboot.local:631/printers/Brother_MFC-L8900CDW",
        },
    ]
    m.print_jobs = [
        {
            "id": "pj-1",
            "timestamp": mock_server.iso(now - 180),
            "job": "MacWrite Letter",
            "source": "lpd",
            "queue": "retroboot",
            "destination": "pdf",
            "destination_label": "Save as PDF (RetroBoot Share)",
            "status": "completed",
            "bytes": 48210,
            "path": "Printed PDFs/MacWrite Letter.pdf",
            "cups_job_id": None,
            "error": None,
        },
        {
            "id": "pj-2",
            "timestamp": mock_server.iso(now - 940),
            "job": "ClarisWorks Flyer",
            "source": "lpd",
            "queue": "Brother_MFC-L8900CDW",
            "destination": "cups:Brother_MFC-L8900CDW",
            "destination_label": "Brother MFC-L8900CDW",
            "status": "completed",
            "bytes": 124680,
            "path": None,
            "cups_job_id": 42,
            "error": None,
        },
    ]

    m.caps["bluetooth"] = True
    m.roms = [{"sha256": "0" * 64, "family": "quadra800", "label": "Quadra 800 ROM",
               "models": "Quadra 610, 650 and 800", "bytes": 1 << 20}]

    # Scrub 0.2.0-dev from seeded logs
    for src_name, lines in m.logs.items():
        m.logs[src_name] = [
            line.replace("0.2.0-dev", "3.4.2")
            .replace("0.2.0", "3.4.2")
            .replace("os921", "os922-mega-deluxe-v6")
            .replace("9.2.0-rb3", "9.2.0-rb18")
            for line in lines
        ]

    # Patch validation_context to include machine_guest_os
    def patched_vc():
        w, h, _ = m.host_mode()
        g = m.graphics_json()
        return hschema.ValidationContext(
            machines=set(m.machines),
            machine_names={k: v["name"] for k, v in m.machines.items()},
            machine_families={k: v.get("family", "powermac") for k, v in m.machines.items()},
            machine_guest_os={k: v.get("guest_os", "classic") for k, v in m.machines.items()},
            host_modes=[f"{mw}x{mh}@{mr}" for mw, mh, mr in m.host_modes],
            capabilities=dict(m.caps),
            media_cds={x["id"]: x["label"] for x in m.MEDIA if x["can_insert"]},
            display_mode=(w, h),
            values=dict(m.desired),
            graphics={
                "available": g["available"],
                "reason": g["unavailable_reason"],
                "message": g["unavailable_message"],
                "ati_screen": m.ati_screen(m.desired),
            },
        )

    m.validation_context = patched_vc

    # Patch bluetooth_json with simulated devices
    def patched_bt():
        return {
            "available": True,
            "state": "on",
            "enabled": m.desired["bluetooth.enabled"],
            "powered": True,
            "blocked": None,
            "scanning": False,
            "adapter": {"address": "00:1A:7D:DA:71:13", "name": "retroboot"},
            "devices": [
                {
                    "address": "C8:2B:96:1A:4E:05",
                    "name": "Magic Trackpad",
                    "kind": "trackpad",
                    "kind_label": "Trackpad",
                    "paired": True,
                    "connected": True,
                    "battery": 78,
                    "signal": 4,
                },
                {
                    "address": "C8:2B:96:1A:4E:01",
                    "name": "Bluetooth Keyboard",
                    "kind": "keyboard",
                    "kind_label": "Keyboard",
                    "paired": False,
                    "connected": False,
                    "battery": None,
                    "signal": 4,
                },
                {
                    "address": "C8:2B:96:1A:4E:02",
                    "name": "Wireless Mouse",
                    "kind": "mouse",
                    "kind_label": "Mouse",
                    "paired": False,
                    "connected": False,
                    "battery": None,
                    "signal": 3,
                },
            ],
            "pairing": None,
            "assistant": {
                "active": False,
                "looking_for": None,
                "stage": None,
                "offer": m.desired["bluetooth.offer_keyboard_setup"],
            },
            "reason": None,
        }

    m.bluetooth_json = patched_bt

    update_status = {
        "available": True,
        "reason": None,
        "current": {
            "version": "3.4.2",
            "build_id": "3.4.2-20261125T224558Z-43e6d5b7",
            "flavor": "product",
            "label": "3.4.2 (build 2026-11-25 22:45)",
            "slot": "a",
        },
        "boot": {"kind": "normal", "committed": True},
        "staged": None,
        "previous": {
            "version": "3.4.1",
            "build_id": "3.4.1-20261125T163512Z-27de0be8",
            "label": "3.4.1 (build 2026-11-25 16:35)",
            "newer": False,
            "update_capable": True,
            "slot": "b",
        },
        "last_result": {
            "kind": "update",
            "result": "installed",
            "version": "3.4.2",
            "at": "2026-11-25T22:50:00Z",
            "message": "RetroBoot was updated to 3.4.2.",
        },
        "history": [
            {
                "kind": "update",
                "result": "installed",
                "version": "3.4.2",
                "at": "2026-11-25T22:50:00Z",
                "message": "RetroBoot was updated to 3.4.2.",
            },
            {
                "kind": "update",
                "result": "installed",
                "version": "3.4.1",
                "at": "2026-11-25T16:40:00Z",
                "message": "RetroBoot was updated to 3.4.1.",
            },
        ],
        "usb": [],
        "check": {
            "at": "2026-11-25T23:00:00Z",
            "url": "https://updates.mcchord.net/retroboot/",
            "found": None,
            "latest": {
                "version": "3.4.2",
                "build_id": "3.4.2-20261125T224558Z-43e6d5b7",
                "label": "3.4.2 (build 2026-11-25 22:45)",
                "bytes": 602_152_960,
                "notes": None,
            },
        },
        "url": "https://updates.mcchord.net/retroboot/",
        "can_install": True,
    }

    orig_status_json = m.status_json

    def patched_status_json() -> dict:
        d = orig_status_json()
        d["host"]["version"] = "3.4.2"
        d["versions"] = {
            "RetroBoot": "3.4.2",
            "RetroBoot service": "3.4.2",
            "Display service": "3.4.2",
            "Emulator": "QEMU 9.2.0 + ClassicMac patches",
            "68k emulator": "QEMU 9.2.0 (Quadra 800)",
            "Host kernel": "6.12.48-amd64",
            "Image": "retroboot-3.4.2-20261125T224558Z-43e6d5b7",
            "Previous slot": "3.4.1-20261125T163512Z-27de0be8",
            "Package manifest": "sha256:7d1e9c0b4a…31f0",
            "Settings schema": str(m.schema_version),
        }
        d["update"] = update_status
        return d

    m.status_json = patched_status_json

    # Serve real 3.4.2 Mega Deluxe v6 Finder frame for live screen previews & WebSocket stream
    finder_png = (MAIN_REPO / "private" / "releases" / "3.4.2" / "qa-350-finder.png").read_bytes()
    orig_screenshot = m.screenshot

    def patched_screenshot(what: str) -> bytes:
        if m.guest["state"] == "running" and what in ("guest", "composed"):
            return finder_png
        return orig_screenshot(what)

    m.screenshot = patched_screenshot

    # Patch Handler subclass to serve /api/v1/update and /api/v1/screen/stream over WSS
    def _ws_send(conn, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", n))
        conn.sendall(bytes(header) + payload)

    def _ws_recv(conn) -> tuple[int, bytes] | None:
        b = conn.recv(2)
        if len(b) < 2:
            return None
        opcode = b[0] & 0x0F
        masked = bool(b[1] & 0x80)
        length = b[1] & 0x7F
        if length == 126:
            ext = conn.recv(2)
            if len(ext) < 2:
                return None
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = conn.recv(8)
            if len(ext) < 8:
                return None
            length = struct.unpack("!Q", ext)[0]
        mask = conn.recv(4) if masked else b""
        data = bytearray()
        while len(data) < length:
            chunk = conn.recv(length - len(data))
            if not chunk:
                return None
            data.extend(chunk)
        if masked:
            for i in range(length):
                data[i] ^= mask[i % 4]
        return opcode, bytes(data)

    class ManualHandler(mock_server.Handler):
        mock = m

        def _dispatch(self, method: str):
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/api/v1/update" and method == "GET":
                return self._json(200, update_status)
            if url.path == "/api/v1/bluetooth/scan" and method == "POST":
                return self._json(200, {"scanning": False})
            if url.path == "/api/v1/screen/stream" and method == "GET":
                key = self.headers.get("Sec-WebSocket-Key")
                if key:
                    accept = base64.b64encode(
                        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
                    ).decode()
                    self.send_response(101, "Switching Protocols")
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept", accept)
                    self.end_headers()
                    self.close_connection = True
                    try:
                        # Wait for client hello
                        _ws_recv(self.connection)
                        _ws_send(self.connection, 1, json.dumps({
                            "t": "hello",
                            "v": 1,
                            "mode": "control",
                            "viewer": "v1",
                            "machine": "Mac OS 9.2.2 Mega Deluxe v6",
                            "hostname": "retroboot",
                            "source": "composed",
                            "swap": False,
                        }).encode())
                        _ws_send(self.connection, 1, json.dumps({
                            "t": "surface",
                            "gen": 1,
                            "w": 1280,
                            "h": 800,
                            "view": "guest",
                            "kind": "guest",
                        }).encode())
                        frame_hdr = struct.pack("<BBHIII", 0x46, 1, 1, 1, 1, 0)
                        tile_hdr = struct.pack("<HHHHB3sI", 0, 0, 1280, 800, 2, b"\x00\x00\x00", len(finder_png))
                        _ws_send(self.connection, 2, frame_hdr + tile_hdr + finder_png)
                        # Keep stream open until client closes
                        while True:
                            msg = _ws_recv(self.connection)
                            if msg is None or msg[0] == 8:
                                break
                    except (OSError, ssl.SSLError):
                        pass
                    return
            super()._dispatch(method)

    return mock_server, m, ManualHandler


def create_self_signed_cert(tmp: Path) -> tuple[Path, Path]:
    cert = tmp / "cert.pem"
    key = tmp / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key), "-out", str(cert),
            "-days", "1", "-nodes",
            "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return cert, key


def capture_web(chrome: str, url: str, out: Path, size: tuple[int, int], scale: float) -> None:
    with tempfile.TemporaryDirectory(prefix="rb-manual-chrome-") as profile:
        shell = "headless_shell" in chrome or "headless-shell" in chrome
        cmd = [
            chrome,
            *([] if shell else ["--headless=new"]),
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--ignore-certificate-errors",
            f"--user-data-dir={profile}",
            f"--window-size={size[0]},{size[1]}",
            f"--force-device-scale-factor={scale}",
            "--virtual-time-budget=5000",
            f"--screenshot={out}",
            url,
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)


def build_group_c() -> None:
    print("=== Group C: 3.4.2 Web Management UI Screenshots ===")
    chrome = find_chrome()
    print(f"  Using browser: {chrome}")
    mock_server, _m, handler_cls = build_manual_mock()
    port = free_port()

    with tempfile.TemporaryDirectory(prefix="rb-manual-tls-") as td:
        cert, key = create_self_signed_cert(Path(td))
        httpd = mock_server.Server(("127.0.0.1", port), handler_cls)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"https://127.0.0.1:{port}"
        try:
            ssl_unverified = ssl._create_unverified_context()
            for _ in range(50):
                try:
                    urllib.request.urlopen(base + "/api/v1/session", timeout=1, context=ssl_unverified).read()
                    break
                except OSError:
                    time.sleep(0.1)

            for out_name, route, size, scale, paired in GROUP_C_SHOTS:
                url = (
                    f"{base}/__mock/login?next={urllib.parse.quote(route, safe='')}"
                    if paired
                    else f"{base}{route}"
                )
                dst = OUT_DIR / out_name
                capture_web(chrome, url, dst, size, scale)
                w, h = verify_png(dst)
                print(f"  [C-web ] {out_name:34s} <- {route:15s} ({w}x{h}, scale={scale})")
        finally:
            httpd.shutdown()
            httpd.server_close()


# ---------------------------------------------------------------------------
# Group D — 6 Fresh 3.4.1 Local Platinum Panel Renders (rbui-render, MIT Platinum)
# ---------------------------------------------------------------------------

def _pure_qr_matrix(text: str) -> list[str]:
    """Minimal pure-Python QR Code (Version 2-M, 25x25, byte mode up to 26 bytes) fallback."""
    data = text.encode("utf-8")
    if len(data) > 26:
        raise ValueError(f"QR payload too long for Version 2-M: {len(data)}")

    # GF(256) with primitive polynomial 0x11D
    exp = [0] * 512
    log = [0] * 256
    v = 1
    for i in range(255):
        exp[i] = v
        log[v] = i
        v <<= 1
        if v & 0x100:
            v ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]

    def gf_mul(a: int, b: int) -> int:
        return 0 if a == 0 or b == 0 else exp[log[a] + log[b]]

    # Generator polynomial for 16 EC codewords (Version 2-M: 28 data + 16 EC = 44)
    gen = [1]
    for i in range(16):
        ng = [0] * (len(gen) + 1)
        for j, g in enumerate(gen):
            ng[j] ^= g
            ng[j + 1] ^= gf_mul(g, exp[i])
        gen = ng

    # Encode byte mode bitstream
    bits: list[int] = []
    def push(val: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            bits.append((val >> shift) & 1)

    push(0b0100, 4)
    push(len(data), 8)
    for b in data:
        push(b, 8)
    push(0, min(4, 28 * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    codewords = [sum(bits[i + k] << (7 - k) for k in range(8)) for i in range(0, len(bits), 8)]
    pad_bytes = (0xEC, 0x11)
    pi = 0
    while len(codewords) < 28:
        codewords.append(pad_bytes[pi % 2])
        pi += 1

    # Reed-Solomon remainder
    rem = list(codewords) + [0] * 16
    for i in range(28):
        coef = rem[i]
        if coef != 0:
            for j, g in enumerate(gen):
                rem[i + j] ^= gf_mul(g, coef)
    all_cw = codewords + rem[28:]

    n = 25
    grid: list[list[int | None]] = [[None] * n for _ in range(n)]
    reserved = [[False] * n for _ in range(n)]

    def place_finder(ox: int, oy: int) -> None:
        for dy in range(-1, 8):
            for dx in range(-1, 8):
                x, y = ox + dx, oy + dy
                if 0 <= x < n and 0 <= y < n:
                    if dx in (-1, 7) or dy in (-1, 7):
                        val = 0
                    elif dx in (0, 6) or dy in (0, 6) or (2 <= dx <= 4 and 2 <= dy <= 4):
                        val = 1
                    else:
                        val = 0
                    grid[y][x] = val
                    reserved[y][x] = True

    place_finder(0, 0)
    place_finder(n - 7, 0)
    place_finder(0, n - 7)

    # Alignment pattern at (18, 18)
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            x, y = 18 + dx, 18 + dy
            val = 1 if (abs(dx) == 2 or abs(dy) == 2 or (dx == 0 and dy == 0)) else 0
            grid[y][x] = val
            reserved[y][x] = True

    # Timing patterns & dark module
    for i in range(8, n - 8):
        val = 1 if i % 2 == 0 else 0
        if not reserved[6][i]:
            grid[6][i] = val
            reserved[6][i] = True
        if not reserved[i][6]:
            grid[i][6] = val
            reserved[i][6] = True
    grid[17][8] = 1
    reserved[17][8] = True

    # Reserve format info cells
    for i in range(9):
        if i != 6:
            reserved[8][i] = True
            reserved[i][8] = True
    for i in range(8):
        reserved[8][n - 1 - i] = True
        reserved[n - 1 - i][8] = True

    # Place data bits zig-zag
    bit_stream = [(cw >> s) & 1 for cw in all_cw for s in range(7, -1, -1)]
    bi = 0
    col = n - 1
    up = True
    while col > 0:
        if col == 6:
            col -= 1
        rows_iter = range(n - 1, -1, -1) if up else range(n)
        for row in rows_iter:
            for dc in (0, -1):
                c = col + dc
                if not reserved[row][c]:
                    grid[row][c] = bit_stream[bi] if bi < len(bit_stream) else 0
                    bi += 1
        col -= 2
        up = not up

    # Mask 0: (r + c) % 2 == 0 (standard, clear, valid) — evaluate all 8 masks for best penalty
    def apply_mask(mask_id: int) -> list[list[int]]:
        fns = [
            lambda r, c: (r + c) % 2 == 0,
            lambda r, c: r % 2 == 0,
            lambda r, c: c % 3 == 0,
            lambda r, c: (r + c) % 3 == 0,
            lambda r, c: (r // 2 + c // 3) % 2 == 0,
            lambda r, c: ((r * c) % 2 + (r * c) % 3) == 0,
            lambda r, c: (((r * c) % 2 + (r * c) % 3) % 2) == 0,
            lambda r, c: (((r + c) % 2 + (r * c) % 3) % 2) == 0,
        ]
        fn = fns[mask_id]
        mgrid = [[0] * n for _ in range(n)]
        for r in range(n):
            for c in range(n):
                val = grid[r][c] or 0
                if not reserved[r][c] and fn(r, c):
                    val ^= 1
                mgrid[r][c] = val
        # Format bits for EC=M (00) + mask_id
        data5 = (0b00 << 3) | mask_id
        rem10 = data5 << 10
        for s in range(14, 9, -1):
            if rem10 & (1 << s):
                rem10 ^= 0x537 << (s - 10)
        fmt = ((data5 << 10) | rem10) ^ 0x5412
        for i in range(6):
            bit = (fmt >> i) & 1
            mgrid[8][i] = bit
            mgrid[n - 1 - i][8] = bit
        for i in range(6, 8):
            bit = (fmt >> i) & 1
            mgrid[8][i + 1] = bit
            mgrid[n - 1 - i][8] = bit
        mgrid[8][n - 8] = (fmt >> 7) & 1
        mgrid[7][8] = (fmt >> 8) & 1
        for i in range(9, 15):
            bit = (fmt >> i) & 1
            mgrid[14 - i][8] = bit
            mgrid[8][n - 15 + i] = bit
        return mgrid

    def penalty(mg: list[list[int]]) -> int:
        score = 0
        for line in mg + [[mg[r][c] for r in range(n)] for c in range(n)]:
            run = 1
            for i in range(1, n):
                if line[i] == line[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + (run - 5)
                    run = 1
            if run >= 5:
                score += 3 + (run - 5)
        dark = sum(sum(r) for r in mg)
        score += abs((dark * 100) // (n * n) - 50) // 5 * 10
        return score

    best = min((apply_mask(m) for m in range(8)), key=penalty)
    return ["".join("1" if cell else "0" for cell in row) for row in best]


async def generate_group_d_docs(doc_dir: Path) -> list[tuple[str, str]]:
    if not hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity = lambda _pid, _mask: None  # type: ignore[attr-defined]
    if not hasattr(os, "sched_getaffinity"):
        os.sched_getaffinity = lambda _pid: {0}  # type: ignore[attr-defined]

    sys.path.insert(0, str(WORKTREE / "hostd"))
    sys.path.insert(0, str(WORKTREE / "hostd" / "tests"))

    import helpers  # noqa: E402
    from retroboot import __version__, bluetooth as bt_mod, schema, security as sec_mod  # noqa: E402
    from retroboot.hostd import Hostd  # noqa: E402
    from retroboot.panel import bluetooth as bt_panel, controller as c, update as upd_panel  # noqa: E402

    # Ensure QR code renders even without python3-qrcode installed
    if sec_mod.qr_matrix("https://192.168.1.50/") is None:
        sec_mod.qr_matrix = lambda text, border=0: _pure_qr_matrix(text)  # type: ignore[assignment]
        c.qr_matrix = sec_mod.qr_matrix

    td, root = helpers.scratch()
    try:
        pkg = helpers.make_package(root)
        man = json.loads((pkg / "manifest.json").read_text())
        man["sources"] = {"qemu": {"tag": "v9.2.0-rb18"}}
        (pkg / "manifest.json").write_text(json.dumps(man))
        helpers.make_m68k_package(root)
        # 1. Mac OS 9.2.2 Mega Deluxe v6
        d9 = root / "var/lib/retroboot/machines/os922-mega-deluxe-v6"
        d9.mkdir(parents=True, exist_ok=True)
        with open(d9 / "disk.img", "wb") as f:
            f.truncate(8 * 1024 * 1024)
        (d9 / "machine.json").write_text(json.dumps({
            "schema_version": 1,
            "id": "os922-mega-deluxe-v6",
            "name": "Mac OS 9.2.2 Mega Deluxe v6",
            "os_version": "9.2.2",
            "disk": "disk.img",
        }))
        (d9 / "state.json").write_text(json.dumps({"clean": True, "last_exit": None}))

        # 2. Mac OS 8.1 Deluxe v2 (68k)
        helpers.make_quadra_machine(root, "os81-deluxe-v2", "Mac OS 8.1 Deluxe v2", os_version="8.1")

        # 3. Mac OS X 10.4.11 Tiger
        dx = root / "var/lib/retroboot/machines/osx-tiger"
        dx.mkdir(parents=True, exist_ok=True)
        with open(dx / "disk.img", "wb") as f:
            f.truncate(8 * 1024 * 1024)
        (dx / "machine.json").write_text(json.dumps({
            "schema_version": 3,
            "id": "osx-tiger",
            "name": "Mac OS X 10.4.11 Tiger",
            "os_version": "10.4.11",
            "disk": "disk.img",
            "template": {"machineFamily": "powerMacOSX", "guestOS": "macosx-10.4"},
        }))
        (dx / "state.json").write_text(json.dumps({"clean": True, "last_exit": None}))

        cfg = helpers.make_config(root)
        cfg.https_port = 443
        cfg.bluetooth_backend = "fake"
        app = Hostd(cfg)
        app.loop = asyncio.get_running_loop()
        app.settings.store.load()
        app.settings.set_desired_and_applied(
            {"machine.active": "os922-mega-deluxe-v6"},
            schema.ValidationContext(
                machines={"os922-mega-deluxe-v6", "os81-deluxe-v2", "osx-tiger"},
                machine_families={"os922-mega-deluxe-v6": "powermac", "os81-deluxe-v2": "quadra800", "osx-tiger": "powermac"},
                machine_guest_os={"os922-mega-deluxe-v6": "classic", "os81-deluxe-v2": "classic", "osx-tiger": "macosx-10.4"},
                check_capabilities=False,
            ),
        )
        app.presenter.display = {
            "device": "/dev/dri/card0",
            "driver": "bochs-drm",
            "connector": "Virtual-1",
            "mode": [1280, 800, 60],
            "modes": [[1920, 1080, 60], [1280, 800, 60], [1024, 768, 60]],
            "suggested_scale": 1,
        }
        app.presenter.hello = {"version": 1}

        class _StubWeb:
            fingerprint = "4F:1C:2B:88:0E:D1:7A:63:9C:41:5B:22:E0:8F:31:C6:77:0A:B9:5E:12:D4:6F:83:A1:3C:58:E9:07:B2:9A:E2"

            @staticmethod
            def url_for(addr: str) -> str:
                return f"https://{addr}/"

        try:
            app.web.tls()
        except ModuleNotFoundError:
            app.web = _StubWeb()

        # Example IP 192.168.1.50 and example pairing code 246810
        async def addresses():
            return ["192.168.1.50"]

        async def primary_address():
            return "192.168.1.50"

        async def wifi_hw():
            return {"label": "Realtek RTL8821CE 802.11ac"}

        app.network.all_addresses = addresses
        app.network.primary_address = primary_address
        app.network.wifi_hardware = wifi_hw

        app.pairing.code = "246810"
        app.pairing.code_expires = app.pairing.clock() + 300

        app.webproxy.available = lambda: True
        app.webproxy.state = "running"
        app.webproxy.stats = {"counts": {"pages": 42}}

        p = app.panel

        # 36. Network & Web Access
        net = c.NetworkWeb(p)
        await asyncio.sleep(0.2)
        net.addresses = ["192.168.1.50"]
        net.primary = "192.168.1.50"
        net.wifi_hw = {"label": "Realtek RTL8821CE 802.11ac"}
        # Override pairing code display if needed so 246 810 or 2 4 6 8 1 0 matches
        net_doc = net.doc()

        # 37. Startup Disk (Mega Deluxe v6 running, Mac OS 8.1, Tiger)
        app.supervisor.machine = app.machines.get("os922-mega-deluxe-v6")
        app.supervisor.state = "running"
        # Ensure sup.active is True for StartupDisk's running check
        orig_sup_cls = type(app.supervisor)
        sd_doc = None
        if not app.supervisor.active:
            class _RunningProxy:
                def __init__(self, real):
                    self._real = real
                def __getattr__(self, name):
                    if name == "active":
                        return True
                    return getattr(self._real, name)
            saved_sup = app.supervisor
            app.supervisor = _RunningProxy(saved_sup)
            sd = c.StartupDisk(p, back=lambda: None, selected="os922-mega-deluxe-v6")
            sd_doc = sd.doc()
            app.supervisor = saved_sup
        else:
            sd = c.StartupDisk(p, back=lambda: None, selected="os922-mega-deluxe-v6")
            sd_doc = sd.doc()

        # 38. Display & Sound (Monitor tab)
        ds = c.DisplaySound(p, page="monitor")
        ds_doc = ds.doc()

        # 39. Software Update (3.4.2 installed, 3.4.1 previous, Clean Update)
        app.updates.public = lambda: {
            "available": True,
            "reason": None,
            "current": {
                "version": __version__,
                "build_id": "3.4.2-20261125T224558Z-43e6d5b7",
                "flavor": "product",
                "label": "3.4.2 (build 2026-11-25 22:45)",
                "slot": "a",
            },
            "boot": {"kind": "normal", "committed": True},
            "staged": None,
            "previous": {
                "version": "3.4.1",
                "build_id": "3.4.1-20261125T163512Z-27de0be8",
                "label": "3.4.1 (build 2026-11-25 16:35)",
                "newer": False,
                "update_capable": True,
                "slot": "b",
            },
            "last_result": None,
            "history": [],
            "usb": [],
            "check": {
                "at": "2026-11-25T23:00:00Z",
                "url": "https://updates.mcchord.net/retroboot/",
                "found": None,
                "latest": {
                    "version": "3.4.2",
                    "build_id": "3.4.2-20261125T224558Z-43e6d5b7",
                    "label": "3.4.2 (build 2026-11-25 22:45)",
                    "bytes": 602_152_960,
                    "notes": None,
                },
            },
            "url": "https://updates.mcchord.net/retroboot/",
            "can_install": True,
        }
        async def _noop_scan_usb():
            return []
        app.updates.scan_usb = _noop_scan_usb
        orig_auto_check = upd_panel.SoftwareUpdate._auto_check
        upd_panel.SoftwareUpdate._auto_check = lambda self: None  # type: ignore[method-assign]
        try:
            su = upd_panel.SoftwareUpdate(p)
        finally:
            upd_panel.SoftwareUpdate._auto_check = orig_auto_check  # type: ignore[method-assign]
        su.scanning = False
        su_doc = su.doc()

        # 40. Bluetooth (simulated paired + available devices via FakeBackend.demo)
        app.bluetooth = bt_mod.Bluetooth(app, backend=bt_mod.FakeBackend.demo())
        app.bluetooth.backend.powered = True
        app.bluetooth.backend.discovering = True
        bt_scr = bt_panel.BluetoothDevices(p)
        bt_doc = bt_scr.doc()

        # 41. Diagnostics (3.4.2 and 192.168.1.50)
        diag = c.Diagnostics(p)
        await asyncio.sleep(0.2)
        diag.addresses = ["192.168.1.50"]
        diag_doc = diag.doc()

        docs = [
            ("panel-network-web-access.png", net_doc),
            ("panel-startup-disk.png", sd_doc),
            ("panel-display-sound.png", ds_doc),
            ("panel-software-update.png", su_doc),
            ("panel-bluetooth.png", bt_doc),
            ("panel-diagnostics.png", diag_doc),
        ]
        pairs = []
        for png_name, doc in docs:
            jpath = doc_dir / f"{png_name}.json"
            jpath.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
            pairs.append((png_name, str(jpath)))
        return pairs
    finally:
        td.cleanup()


def build_group_d() -> None:
    print("=== Group D: 3.4.2 Local Platinum Panel Renders (MIT Platinum) ===")
    if not RBUI_RENDER.exists():
        raise SystemExit(f"Missing rbui-render binary: {RBUI_RENDER}")

    # Clean environment: NEVER set RBUI_ASSETS or RB_PLATINUM_BUNDLE
    env = dict(os.environ)
    env.pop("RBUI_ASSETS", None)
    env.pop("RB_PLATINUM_BUNDLE", None)

    with tempfile.TemporaryDirectory(prefix="rb-manual-panels-") as td:
        td_path = Path(td)
        pairs = asyncio.run(generate_group_d_docs(td_path))
        for png_name, jpath in pairs:
            raw_png = td_path / f"raw-{png_name}"
            desc_raw = subprocess.check_output(
                [
                    str(RBUI_RENDER),
                    jpath,
                    str(raw_png),
                    "--size", "960x540",
                    "--scale", "1",
                    "--background", "checkerboard",
                    "--no-cursor",
                    "--describe",
                ],
                env=env,
                text=True,
            )
            desc = json.loads(desc_raw)
            x, y, w, h = desc["window"]
            pad = 32
            dst = OUT_DIR / png_name
            cw, ch_h = crop_png(raw_png, dst, (x - pad, y - pad, x + w + pad, y + h + pad))
            print(f"  [D-rbui] {png_name:34s} window={w}x{h} -> cropped {cw}x{ch_h}")


def verify_all() -> None:
    expected = (
        [name for name, _ in GROUP_A_DIALOGS]
        + [name for name, _ in GROUP_A_FULL]
        + [name for name, *_ in GROUP_C_SHOTS]
        + [
            "panel-network-web-access.png",
            "panel-startup-disk.png",
            "panel-display-sound.png",
            "panel-software-update.png",
            "panel-bluetooth.png",
            "panel-diagnostics.png",
        ]
    )
    assert len(expected) == 41, f"Expected 41 outputs, got {len(expected)}"
    for name in expected:
        p = OUT_DIR / name
        if not p.is_file():
            raise SystemExit(f"Missing expected output: {p}")
        w, h = verify_png(p)
        if p.stat().st_size == 0:
            raise SystemExit(f"Empty PNG: {p}")
    print(f"\nVerified all {len(expected)} PNG files in {OUT_DIR}.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--group", choices=["a", "c", "d"], help="Run only one group (default: all)")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.group in (None, "a"):
        build_group_a()
    if args.group in (None, "c"):
        build_group_c()
    if args.group in (None, "d"):
        build_group_d()
    if args.group is None:
        verify_all()


if __name__ == "__main__":
    main()
