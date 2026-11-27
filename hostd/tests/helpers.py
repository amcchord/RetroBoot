"""Shared test scaffolding: scratch roots, fake emulator package, fake guest prep."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from retroboot.config import Config  # noqa: E402
from retroboot.guestprep import GuestPrep  # noqa: E402
from retroboot.util import atomic_write_json  # noqa: E402

FAKES = HERE / "fakes"


class FakePrepModule:
    """Stands in for rbguestprep.prep (the lab builder has no hfsplus module)."""
    RETROBOOT_TEST_FAKE = True

    class PrepError(Exception):
        pass

    def __init__(self, fail_apply: bool = False, fsck_ok: bool = True, delay: float = 0.0):
        self.calls: list[tuple] = []
        self.fail_apply = fail_apply
        self.fsck_ok = fsck_ok
        self.delay = delay

    def apply(self, image: str) -> dict:
        self.calls.append(("apply", image))
        if self.delay:
            time.sleep(self.delay)
        if self.fail_apply:
            raise self.PrepError("volume could not be repaired: injected")
        with open(image, "r+b") as f:
            f.seek(4096)
            f.write(b"PREPARED")
        return {"image": image, "fsck_before": "ok", "shutdown_warning": "disabled (0x00000042 -> 0x00000002)",
                "tcpip": "installed-ethernet-dhcp", "fsck_after": "ok"}

    def fsck(self, image: str, repair: bool):
        self.calls.append(("fsck", image, repair))
        return self.fsck_ok, "** The volume Macintosh HD appears to be OK." if self.fsck_ok else "** FAILED"


def make_package(root: Path) -> Path:
    pkg = root / "usr/lib/retroboot/qemu"
    (pkg / "bin").mkdir(parents=True, exist_ok=True)
    (pkg / "share/qemu").mkdir(parents=True, exist_ok=True)
    q = pkg / "bin/qemu-system-ppc"
    q.write_text(f"#!{sys.executable}\n" + (FAKES / "fake_qemu.py").read_text())
    q.chmod(0o755)
    files = {}
    for name in ("share/qemu/openbios-ppc", "share/qemu/qemu_vga.ndrv", "share/qemu/ndrvloader"):
        (pkg / name).write_bytes(b"fixture " + name.encode())
    for name in ("bin/qemu-system-ppc", "share/qemu/openbios-ppc", "share/qemu/qemu_vga.ndrv",
                 "share/qemu/ndrvloader"):
        files[name] = {"sha256": hashlib.sha256((pkg / name).read_bytes()).hexdigest()}
    (pkg / "manifest.json").write_text(json.dumps({"schema_version": 1, "files": files,
                                                   "sources": {"qemu": {"tag": "v11.0.2-fake"}}}))
    return pkg


# --- 68k Macs (quadra.py) ---------------------------------------------------------------
# A stand-in for the Quadra 800 ROM: RetroBoot's own bytes (never Apple's), accepted by
# quadra.py only inside ``with test_rom():``.
TEST_ROM = (b"RetroBoot test ROM for the Quadra tests; not Apple code. " * 20000)[:1024 * 1024]
TEST_ROM_SHA256 = hashlib.sha256(TEST_ROM).hexdigest()
# A stand-in PRAM seed with the bytes hostd reads or writes where the package's seed has
# them: the initialized mark at 0x0C, the qfb slot's screen mode at 0x68 (0x83: 8 bits), the
# default startup device at 0x78 (ffffffdf: the SCSI disk at ID 0) and 32-bit addressing at 0x8A.
_seed = bytearray(range(256))
_seed[0x0C:0x10] = b"NuMc"                      # the ROM's initialized-PRAM mark
_seed[0x68] = 0x83                              # the qfb slot's mode: 256 colours
_seed[0x78:0x7C] = bytes.fromhex("ffffffdf")    # default startup device: SCSI ID 0
_seed[0x8A] = 0x45                              # 32-bit addressing on (bits 0 and 2), as the package's seed
PRAM_SEED = bytes(_seed)


def test_rom():
    """Patch quadra.py so TEST_ROM is the one ROM a 68k Mac may start with."""
    from unittest import mock
    from retroboot import quadra
    info = {"family": "quadra800", "bytes": len(TEST_ROM), "label": "Quadra 800 ROM", "models": "Quadra 610, 650 and 800"}
    stack = mock.patch.multiple(quadra, ROM_SHA256=TEST_ROM_SHA256, ROMS={TEST_ROM_SHA256: info})
    return stack


def make_m68k_package(root: Path) -> Path:
    """The 68k emulator package (linux-m68k-rN layout) with the fake QEMU as qemu-system-m68k."""
    pkg = root / "usr/lib/retroboot/qemu-m68k"
    (pkg / "bin").mkdir(parents=True, exist_ok=True)
    (pkg / "share/qemu/keymaps").mkdir(parents=True, exist_ok=True)
    q = pkg / "bin/qemu-system-m68k"
    q.write_text(f"#!{sys.executable}\n" + (FAKES / "fake_qemu.py").read_text())
    q.chmod(0o755)
    (pkg / "share/qemu/mac_qfb.rom").write_bytes(b"fixture nubus-qfb declaration ROM")
    (pkg / "share/qemu/classicvirtio-68k.rom").write_bytes(b"fixture classicvirtio 68k declaration ROM")
    (pkg / "share/qemu/q800-pram-seed.img").write_bytes(PRAM_SEED)
    files = {n: {"sha256": hashlib.sha256((pkg / n).read_bytes()).hexdigest()}
             for n in ("bin/qemu-system-m68k", "share/qemu/mac_qfb.rom", "share/qemu/classicvirtio-68k.rom",
                       "share/qemu/q800-pram-seed.img")}
    (pkg / "manifest.json").write_text(json.dumps({"schema_version": 1, "files": files,
                                                   "features": ["q800", "nubus-qfb", "classicvirtio-68k"],
                                                   "sources": {"qemu": {"tag": "v11.0.2-fake-m68k"}}}))
    return pkg


def make_quadra_machine(root: Path, mid: str = "mac-os-8-1", name: str = "Mac OS 8.1",
                        os_version: str = "8.1", rom: bytes | None = TEST_ROM, template: dict | None = None) -> Path:
    """A 68k Mac (machine.json family quadra800) with its ROM (TEST_ROM unless told otherwise)."""
    d = root / "var/lib/retroboot/machines" / mid
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "disk.img", "wb") as f:
        f.truncate(8 * 1024 * 1024)
    if rom is not None:
        (d / "Quadra800.rom").write_bytes(rom)
    atomic_write_json(d / "machine.json", {"schema_version": 1, "id": mid, "name": name, "os_version": os_version,
                                           "disk": "disk.img", "family": "quadra800", "rom": "Quadra800.rom",
                                           "pram": "pram.img",
                                           "template": template or {"machineFamily": "quadra800", "ramMB": 128,
                                                                    "width": 800, "height": 600, "depth": 8}})
    atomic_write_json(d / "state.json", {"clean": True, "last_exit": None})
    return d


class FakePrep68kModule:
    """Stands in for rbguestprep's 68k module (guestprep.MODULE_68K)."""
    RETROBOOT_TEST_FAKE = True
    PREP_VERSION = 1

    class PrepError(Exception):
        pass

    def __init__(self):
        self.calls: list[tuple] = []

    def apply(self, image: str) -> dict:
        self.calls.append(("apply", image))
        return {"image": image, "fsck_before": "ok", "retroboot_idle": "installed",
                "shutdown_warning": "cleared", "fsck_after": "ok"}

    def fsck(self, image: str, repair: bool):
        self.calls.append(("fsck", image, repair))
        return True, "The volume Macintosh HD appears to be OK."


def fake_qemu_conf(pkg: Path, **conf) -> None:
    (pkg / "fake-qemu.json").write_text(json.dumps(conf))
    try:
        (pkg / "fake-qemu.state").unlink()
    except FileNotFoundError:
        pass


def fake_qemu_log(pkg: Path) -> list[dict]:
    try:
        return [json.loads(l) for l in (pkg / "fake-qemu.log").read_text().splitlines() if l.strip()]
    except OSError:
        return []


def make_machine(root: Path, mid: str = "mac-os-9", name: str = "Mac OS 9") -> Path:
    d = root / "var/lib/retroboot/machines" / mid
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "disk.img", "wb") as f:
        f.truncate(8 * 1024 * 1024)
    atomic_write_json(d / "machine.json", {"schema_version": 1, "id": mid, "name": name,
                                           "os_version": "Mac OS 9.2.1", "disk": "disk.img"})
    atomic_write_json(d / "state.json", {"clean": True, "last_exit": None})
    return d


def make_config(root: Path, **overrides) -> Config:
    cfg = Config(root=str(root))
    cfg.dev = True
    cfg.guest_runner = "subprocess"
    cfg.display_backend = "retroboot"
    cfg.allow_network_tools = False
    cfg.journal = False
    cfg.http_port = 0
    cfg.https_port = 0
    cfg.poweroff_cmd = []
    cfg.reboot_cmd = []
    cfg.hostname_cmd = []
    cfg.timezone_cmd = []
    cfg.timezone_guess_urls = []
    cfg.bootdisplay_cmd = []
    cfg.sync_cmd = []
    cfg.run_as_user = ""
    cfg.guest_content_timeout = 6.0
    cfg.finder_wait_timeout = 10.0
    cfg.clean_stop_timeout = 10.0
    cfg.known_good_after = 1.0
    cfg.handoff_deadline = 3.0
    cfg.presenter_fallback_start = 0
    cfg.python = sys.executable
    cfg.sysfs_path = str(root / "sys")          # no host battery/DMI: a desktop unless a test says otherwise
    cfg.device_tree = str(root / "proc/device-tree")   # no device tree: a PC unless a test makes one (platform.py)
    cfg.catalog_url = "https://127.0.0.1:9/catalog.json"   # tests never reach the Internet catalog
    cfg.update_default_feed_url = ""                       # tests never reach the public update feed
    cfg.installer_board = "x86"   # the installer's PC layouts on any test host (aarch64 chroots included)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def make_device_tree(root: Path, model: str, compatible: list[str], revision: int | None = None) -> Path:
    """A Raspberry Pi's /proc/device-tree (model, compatible, system/linux,revision) under root."""
    dt = root / "proc/device-tree"
    (dt / "system").mkdir(parents=True, exist_ok=True)
    (dt / "model").write_bytes(model.encode() + b"\0")
    (dt / "compatible").write_bytes(b"".join(c.encode() + b"\0" for c in compatible))
    if revision is not None:
        (dt / "system/linux,revision").write_bytes(revision.to_bytes(4, "big"))
    return dt


def scratch() -> tuple[tempfile.TemporaryDirectory, Path]:
    td = tempfile.TemporaryDirectory(prefix="rbh-", dir="/tmp")
    return td, Path(td.name)


async def wait_for(pred, timeout: float = 10.0, interval: float = 0.05, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = pred()
        if asyncio.iscoroutine(r):
            r = await r
        if r:
            return r
        await asyncio.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")


def events(app, name: str) -> list[dict]:
    return [r for r in app.lifecycle.recent if r["event"] == name]


def install_fake_prep(app, module: FakePrepModule | None = None,
                      module_68k: "FakePrep68kModule | None" = None) -> FakePrepModule:
    module = module or FakePrepModule()
    app.guestprep = GuestPrep(Path("/nonexistent"), module=module, module_68k=module_68k)
    return module
