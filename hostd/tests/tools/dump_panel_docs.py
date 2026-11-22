#!/usr/bin/env python3
"""Write every hostd panel document (normal and installer mode) as JSON for rbui validation.

    python3 tests/tools/dump_panel_docs.py OUTDIR
    for f in OUTDIR/*.json; do rbui-render "$f" "${f%.json}.png" --size 1024x768; done

Uses a scratch root with a fake machine; no presenter, QEMU or network is needed.
"""
import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

import helpers  # noqa: E402
from retroboot import schema  # noqa: E402
from retroboot.hostd import Hostd  # noqa: E402
from retroboot.jobs import Job  # noqa: E402
from retroboot.panel import controller as c  # noqa: E402
from retroboot.panel import installer as ins  # noqa: E402
from retroboot.panel import picker as pk  # noqa: E402
from retroboot.panel import bluetooth as btp  # noqa: E402
from retroboot.bluetooth import Bluetooth, FakeBackend, FakeDevice, PairingSession  # noqa: E402

DISPLAY = {"device": "/dev/dri/card0", "driver": "bochs-drm", "connector": "Virtual-1", "mode": [1024, 768, 60],
           "modes": [[1024, 768, 60], [1280, 800, 60], [1920, 1080, 60]], "suggested_scale": 1}


def sample_picker(ip, signed_in: bool = True):
    """The Choose Macs window's state as a Deluxe ISO sees it, without loading anything."""
    from pathlib import Path as P
    ps = pk.Picker(ip)
    done = asyncio.get_running_loop().create_future()
    done.set_result(None)
    ps.loading = done                    # the window shows this state as is
    GB, MB = 1024 ** 3, 1024 ** 2
    ps.found["disc"] = [
        pk.Choice("disc:os922-deluxe-v4", "disc", "dir", "Mac OS 9.2.2 Deluxe v4", "9.2.2", int(1.45 * GB),
                  machine_id="os922-deluxe-v4", path=P("/usr/lib/retroboot/installer/embedded/machines/os922-deluxe-v4"),
                  family="os922-deluxe", version=4),
        pk.Choice("disc:os922-full-v5", "disc", "dir", "Mac OS 9.2.2 (v5)", "9.2.2", 509 * MB,
                  machine_id="os922-full-v5", path=P("/usr/lib/retroboot/installer/embedded/machines/os922-full-v5"),
                  family="os922-full", version=5)]
    if signed_in:
        ps.found["lab"] = [
            pk.Choice("lab:os921-full-v4", "lab", "catalog", "Mac OS 9.2.1 (v4)", "9.2.1", 498 * MB, download=196 * MB,
                      machine_id="os921-full-v4", family="os921-full", version=4),
            pk.Choice("lab:os922-deluxe-v3", "lab", "catalog", "Mac OS 9.2.2 Deluxe v3", "9.2.2", int(1.45 * GB),
                      download=869 * MB, machine_id="os922-deluxe-v3", family="os922-deluxe", version=3, older=True)]
        ps.status["lab"] = {"state": "ready"}
        ps.found["catalog"] = [
            pk.Choice("catalog:mac-os-9.2.1-gxmetal-2.3.0-32gb-v2", "catalog", "catalog", "Mac OS 9", "Mac OS 9.2.1",
                      120 * MB, download=79 * MB, machine_id="mac-os-9.2.1-gxmetal-2.3.0-32gb-v2"),
            pk.Choice("catalog:copland-d11e4-activities-v3", "catalog", "catalog", "Copland D11E4 Activities",
                      "Copland D11E4", 94 * MB, download=36 * MB, machine_id="copland-d11e4-activities-v3")]
        ps.status["catalog"] = {"state": "ready"}
        ps.found["usb"] = [pk.Choice("usb:5f2a", "usb", "archive", "macos9-backup", None, 207 * MB, estimate=True,
                                     origin="usb:STICK")]
        ps.checked = {"disc:os922-deluxe-v4", "disc:os922-full-v5", "lab:os921-full-v4"}
    else:
        ps.status["lab"] = {"state": "signin"}
        ps.status["catalog"] = {"state": "offline"}
        ps.checked = {"disc:os922-deluxe-v4", "disc:os922-full-v5"}
    ps.status["disc"] = ps.status["usb"] = {"state": "ready"}
    ps.defaults_done = True
    ps.startup = "disc:os922-deluxe-v4"
    ip.st.picker = ps
    return ps


async def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    td, root = helpers.scratch()
    helpers.make_package(root)
    helpers.make_machine(root, "mac-os-9-2-1", "Mac OS 9.2.1")
    app = Hostd(helpers.make_config(root))
    app.loop = asyncio.get_running_loop()
    app.settings.store.load()
    app.settings.set_desired_and_applied({"machine.active": "mac-os-9-2-1"},
                                         schema.ValidationContext(machines={"mac-os-9-2-1"}, check_capabilities=False))
    app.presenter.display = DISPLAY
    app.presenter.hello = {"version": 1}
    app.web.tls()
    p = app.panel
    job = Job(id="j1", kind="guest.restart", resources=())
    job.update(0.4, "Asking Mac OS to restart…")
    nw = c.NetworkWeb(p)
    nw.primary, nw.addresses = "192.168.1.40", ["192.168.1.40", "10.1.2.3"]
    wifi = c.WiFi(p, back=None)
    wifi.state = {"available": True, "connected": "RetroBoot Home", "ssid": "Home", "profiles": []}
    wifi.networks = [{"id": "Home", "ssid": "Home", "signal": 80, "security": "WPA2", "in_use": True},
                     {"id": "Cafe", "ssid": "Cafe", "signal": 35, "security": "", "in_use": False}]
    wifi.busy = False
    nowifi = c.WiFi(p, back=None)
    nowifi.state = {"available": False}
    nowifi.busy = False
    from retroboot.catalogs import arrange

    async def never(*a, **k):          # the windows show the example lists below, not a fetch
        await asyncio.Event().wait()
    app.catalogs.fetch_all = never
    cat = c.CatalogList(p, back=None)
    cat.sources = ["internet"]
    url = {"archiveURL": "https://example.invalid/a.tar.gz", "sha256": "0" * 64}
    cat.items = arrange([("internet", [{"id": "mac-os-9.2.1", "name": "Mac OS 9.2.1", "osVersion": "Mac OS 9.2.1",
                                        "archiveBytes": 82844731, **url,
                                        "summary": "Mac OS 9.2.1 with GXMetal 2.3.0 on a 32 GB disk."}])])
    cat.selected = "internet:mac-os-9.2.1"
    # With a stored RetroBoot downloads sign-in: both catalogs, a family, an installed Mac.
    cat2 = c.CatalogList(p, back=None)
    cat2.sources = ["lab", "internet"]
    lab_entry = lambda i, n, b, s: {"id": i, "name": n, "osVersion": "9.2.2", "archiveBytes": b, "summary": s, **url}
    cat2.items = arrange([("lab", [lab_entry("os922-full-v5", "Mac OS 9.2.2 (v5)", 207111795,
                                             "Full Mac OS 9.2.2 from the user's CD with the 9.2.2 update."),
                                   lab_entry("os922-deluxe-v4", "Mac OS 9.2.2 Deluxe v4", 911174491,
                                             "Deluxe v4: sixteen games, After Dark, GoMac."),
                                   lab_entry("os922-deluxe-v3", "Mac OS 9.2.2 Deluxe v3", 911175606, "Deluxe v3.")]),
                          ("internet", [{"id": "mac-os-9.2.1", "name": "Mac OS 9", "osVersion": "Mac OS 9.2.1",
                                         "archiveBytes": 82844731, "summary": "Mac OS 9.2.1 with GXMetal 2.3.0.",
                                         **url}])],
                         {"os922-deluxe-v4"}, {"os922-deluxe": 4})
    cat2.selected = "lab:os922-full-v5"
    usb = c.UsbList(p, back=None)
    usb.items = [{"id": "a1", "name": "macos9.tar.gz", "bytes": 82844731, "origin": "usb:STICK"}]
    usb.selected = "a1"
    # Startup Disk (1.6.0): a second Mac chosen to start next, a blank disk, Start Up from CD.
    helpers.make_machine(root, "mac-os-9-2-2-superdeluxe-v2", "Mac OS 9.2.2 SuperDeluxe v2")
    blank = app.machines.create_blank("Untitled Mac", 8 * 1024 ** 3)
    disk = c.StartupDisk(p, back=None)
    disk_blank = c.StartupDisk(p, back=None, selected=blank.id)
    cd = c.StartFromCD(p, blank.id, disk)
    cd.cds = [{"id": "drive:sr0", "title": "Mac OS 9.2.1", "where": "USB CD-ROM drive", "drive": True},
              {"id": "MacOS922.iso", "title": "Mac OS 9.2.2 Install (MacOS922.iso)", "where": "CD image",
               "drive": False}]
    cd.selected = "drive:sr0"
    cd_none = c.StartFromCD(p, "mac-os-9-2-1", disk)
    cd_none.cds, cd_none.selected = [], None
    # 68k Macs (quadra.py): a Quadra Mac beside the Power Macs, New Blank Disk without and with
    # the 68k emulator and a ROM (helpers.TEST_ROM stands in for the Quadra 800 ROM), Display &
    # Sound for a 68k startup Mac (no 3D acceleration), and a 68k Mac that can't start.
    from retroboot import quadra

    class Fixed:
        def __init__(self, doc):
            self._doc = doc

        def doc(self):
            return self._doc
    rom_patch = helpers.test_rom()
    rom_patch.start()
    helpers.make_quadra_machine(root, "mac-os-8-1", "Mac OS 8.1", "8.1")
    no_emulator = Fixed(c.NewBlankDisk(p, disk).doc())
    helpers.make_m68k_package(root)
    no_rom = Fixed(c.NewBlankDisk(p, disk).doc())
    app.roms.add_bytes(helpers.TEST_ROM)
    nb68k = c.NewBlankDisk(p, disk)
    nb68k.family, nb68k.size = "quadra800", "2"
    ds68k = c.DisplaySound(p)
    ds68k.values["machine.active"] = "mac-os-8-1"
    quadra_screens = {
        "startup-disk-68k": c.StartupDisk(p, back=None, selected="mac-os-8-1"),
        "new-blank-disk-68k": nb68k, "new-blank-disk-68k-no-rom": no_rom,
        "new-blank-disk-68k-no-emulator": no_emulator, "display-sound-68k": ds68k,
    }
    app.supervisor.unavailable = quadra.NO_PACKAGE         # the last start was refused: Startup Disk… instead
    quadra_screens["failure-68k"] = Fixed(c.FailureAlert(p, "Mac OS could not start.", quadra.NO_PACKAGE,
                                                         "RB-20261024-101500-0a1b").doc())
    app.supervisor.unavailable = None
    screens = {
        **quadra_screens,
        "startup-disk-blank": disk_blank, "new-blank-disk": c.NewBlankDisk(p, disk),
        "confirm-delete": c.ConfirmDelete(p, app.machines.get("mac-os-9-2-2-superdeluxe-v2"), disk),
        "start-from-cd": cd, "start-from-cd-none": cd_none,
        "restart-for-mac": c.RestartForMac(p, "Mac OS 9.2.2 SuperDeluxe v2", disk),
        "needs-cd": c.NeedsCD(p, "Untitled Mac", True),
        "startup-options": c.RetroBootWindow(p, kind="startup"), "checkpoints": c.Checkpoints(p, "mac-os-9-2-1", disk),
        "unresponsive": c.Unresponsive(p, "Mac OS did not finish shutting down."),
        "confirm-force-restart": c.ConfirmForce(p, "restart", None),
        "confirm-force-shutdown": c.ConfirmForce(p, "shutdown", None),
        "failure": c.FailureAlert(p, "Mac OS could not start.", "Mac OS stopped unexpectedly.", "RB-20261011-054255-3944"),
        "stopped": c.Stopped(p, "Mac OS is shut down."), "power": c.PowerAlert(p),
        "diagnostics": c.Diagnostics(p), "network-web": nw, "wifi": wifi, "wifi-none": nowifi,
        "wifi-join": c.WiFiJoin(p, wifi.networks[0], wifi), "display-sound": c.DisplaySound(p),
        "display-sound-sound": c.DisplaySound(p, page="sound"), "display-sound-keyboard": c.DisplaySound(p, page="keyboard"),
        "startup-disk": c.StartupDisk(p, back=None), "confirm-restore": c.ConfirmRestore(p, "m", "20261011T050000Z", None),
        "setup-welcome": c.SetupWelcome(p), "setup-catalog": cat,
        "setup-catalog-downloads": cat2,
        "setup-usb": usb, "setup-done": c.Installed(p, "Mac OS 9.2.1"), "timezone": c.TimeZone(p, back=None),
        "job-progress": c.JobScreen(p, job, "Restarting Mac OS", None, True),
        "message": c.MessageAlert(p, "note", "Evidence captured.", "Evidence ID RB-20261011-054255-3944.", None),
    }
    # Bluetooth (panel/bluetooth.py) with simulated devices: a connected trackpad, a keyboard
    # and a mouse in pairing mode; then the keyboard's code, a comparison and Forget.
    fake = FakeBackend([FakeDevice("C8:2B:96:1A:4E:05", "Magic Trackpad", cls=0x002580, paired=True, trusted=True,
                                   connected=True, discoverable=False, battery=80),
                        FakeDevice("C8:2B:96:1A:4E:01", "Bluetooth Keyboard", cls=0x002540, rssi=-52),
                        FakeDevice("C8:2B:96:1A:4E:02", "Wireless Mouse", appearance=0x03C2, le=True, rssi=-71),
                        FakeDevice("C8:2B:96:1A:4E:04", "Game Controller", cls=0x000508, rssi=-83)])
    fake.powered = fake.discovering = True
    app.bluetooth = Bluetooth(app, fake)
    app.bluetooth.scan = lambda *a, **k: None
    bt_list = btp.BluetoothDevices(p)
    bt_list.selected = "C8:2B:96:1A:4E:01"
    kb = {"address": "C8:2B:96:1A:4E:01", "name": "Bluetooth Keyboard", "kind": "keyboard"}
    screens["bluetooth"] = bt_list
    screens["bluetooth-forget"] = btp.ForgetDevice(p, {"address": "C8:2B:96:1A:4E:05", "name": "Magic Trackpad"},
                                                   bt_list)
    # The setup assistant: the offer (a pointer, no keyboard) and the search (neither).
    screens["bluetooth-offer"] = btp.KeyboardOffer(p)
    screens["bluetooth-assistant"] = btp.BluetoothSearch(p)
    for name, stage, code, entered in (("bluetooth-passkey", "passkey", "482913", 2),
                                       ("bluetooth-compare", "confirm", "306517", None),
                                       ("bluetooth-pairing", "pairing", None, None)):
        dev = kb if stage != "confirm" else {"address": "C8:2B:96:1A:4E:07", "name": "Keyboard Cover",
                                             "kind": "combo"}
        sess = PairingSession(dev["address"], dev["name"], dev["kind"], "panel", stage=stage, passkey=code,
                              entered=entered)
        app.bluetooth.session = sess
        (out / f"{name}.json").write_text(json.dumps(btp.BluetoothPairing(p, None, dev, back=None).doc(), indent=1,
                                                     ensure_ascii=False))
    app.bluetooth.session = None
    app.settings.begin_confirm({"display.host_mode": "auto"}, 15)
    screens["keep-setting"] = c.KeepSetting(p, None)
    ip = ins.InstallerPanel(app)
    ip.st.disks = [{"path": "/dev/sr0", "model": "QEMU DVD-ROM", "size_bytes": 900000000, "removable": True,
                    "transport": "sata", "is_boot_medium": True},
                   {"path": "/dev/vda", "model": "Virtual Disk", "size_bytes": 42949672960, "removable": False,
                    "transport": "virtio", "is_boot_medium": False}]
    ip.st.target = ip.st.disks[1]
    hw = ins.HardwareCheck(ip)
    hw.addresses = ["192.168.1.50"]
    await asyncio.sleep(0.2)
    hw.addresses = ["192.168.1.50"]
    ip.st.disks = ip.st.disks
    # Choose Macs to Install (panel/picker.py): two Macs on the disc, RetroBoot downloads
    # (signed in), the Internet catalog and a USB archive; then a second picker that still
    # needs the sign-in and is offline for the Internet catalog.
    ps = sample_picker(ip)
    ip.st.plan = pk.plan_install(ps, app)
    screens.update({"installer-welcome": ins.Welcome(ip), "installer-hardware": hw,
                    "installer-destination": ins.Destination(ip), "installer-erase": ins.EraseConfirm(ip),
                    "installer-macs": pk.ChooseMacs(ip), "installer-signin": pk.SignIn(ip),
                    "installer-display": ins.DisplayCheck(ip),
                    "installer-network": ins.NetworkStep(ip), "installer-ready": ins.ReadyToInstall(ip)})
    ijob = Job(id="j2", kind="installer.install", resources=())
    ijob.update(0.71, "Mac OS 9.2.1 (v4): Downloading… 312 of 497 MB")
    ip.st.results = {m["key"]: {"state": s, "id": m["id"], **x} for m, (s, x) in zip(ip.st.plan, [
        ("installed", {}), ("installed", {}), ("downloading", {"fraction": 0.63})])}
    screens["installer-progress"] = ins.InstallProgress(ip, ijob, "Installing RetroBoot", None)
    for name, scr in (("installer-macs", screens.pop("installer-macs")), ("installer-signin", screens.pop("installer-signin")),
                      ("installer-progress", screens.pop("installer-progress"))):
        (out / f"{name}.json").write_text(json.dumps(scr.doc(), indent=1, ensure_ascii=False))
    ip.st.results[ip.st.plan[2]["key"]] = {"state": "failed", "id": ip.st.plan[2]["id"],
                                            "error": "The download could not finish. Check the network connection."}
    (out / "installer-download-problem.json").write_text(json.dumps(ins.DownloadProblem(ip).doc(), indent=1,
                                                                    ensure_ascii=False))
    ip.st.results[ip.st.plan[2]["key"]]["state"] = "skipped"
    (out / "installer-done-skipped.json").write_text(json.dumps(ins.Done(ip).doc(), indent=1, ensure_ascii=False))
    ip.st.results = {}
    screens["installer-done"] = ins.Done(ip)
    ps2 = sample_picker(ip, signed_in=False)
    (out / "installer-macs-signin.json").write_text(json.dumps(pk.ChooseMacs(ip).doc(), indent=1, ensure_ascii=False))
    ip.st.picker = ps
    await asyncio.sleep(0.2)
    nw.primary, nw.addresses = "192.168.1.40", ["192.168.1.40", "10.1.2.3"]
    screens["installer-network"].primary = "192.168.1.50"
    for name, scr in screens.items():
        doc = scr.doc()
        (out / f"{name}.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    # Software Update (panel/update.py) in its states, from a fixed update status.
    from retroboot.panel import update as upd
    cur = {"version": "1.7.2", "build_id": "1.7.2-20261024T174506Z-e27215d5", "flavor": "debug",
           "label": "1.7.2 (build 2026-10-24 17:45)", "slot": "a"}
    nxt = {"version": "1.7.3", "build_id": "1.7.3-20261025T020000Z-0a1b2c3d", "label": "1.7.3 (build 2026-10-25 02:00)",
           "bytes": 555591680, "notes": "RetroBoot 1.7.3: a simpler RetroBoot window, Download a Mac and Software "
                                        "Update from RetroBoot downloads. Keeps your Macs, documents and settings."}
    prev = {"version": "1.7.1", "build_id": "1.7.1-20261024T144637Z-a8a15d91", "label": "1.7.1 (build 2026-10-24 14:46)",
            "newer": False, "update_capable": True, "slot": "b"}
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = {"available": True, "reason": None, "current": cur, "boot": {"kind": "normal", "committed": True},
            "staged": None, "previous": prev, "url": "https://example.invalid/retroboot-lab/feed.json", "usb": []}

    async def no_usb():
        return []
    app.updates.scan_usb = no_usb
    states = {"software-update-available": dict(base, check={"at": now, "found": nxt}),
              "software-update-current": dict(base, check={"at": now, "found": None}),
              "software-update-ready": dict(base, check={"at": now, "found": nxt},
                                            staged=dict(nxt, status="ready", slot="b")),
              "software-update-none": dict(base, url="", previous=None, check={})}
    for name, st in states.items():
        app.updates.public = lambda st=st: st
        (out / f"{name}.json").write_text(json.dumps(upd.SoftwareUpdate(p).doc(), indent=1, ensure_ascii=False))
    app.updates.public = lambda: states["software-update-available"]
    # The RetroBoot window while Mac OS runs (Control-Alt-Delete), while the frozen-guest check
    # flags it, and paused while it starts (Escape); "is on this computer" after a download.
    sup = app.supervisor
    sup.state, sup.content_seen, sup.qmp_ok = "running", True, True
    sup.machine = app.machines.get("mac-os-9-2-1")
    for name in ("recovery-main", "recovery-running"):
        (out / f"{name}.json").write_text(json.dumps(c.RetroBootWindow(p).doc(), indent=1, ensure_ascii=False))
    (out / "downloaded.json").write_text(json.dumps(c.StartUpWithNew(p, "mac-os-8-1", None).doc(), indent=1,
                                                    ensure_ascii=False))
    sup.display_ok = False
    (out / "recovery-frozen.json").write_text(json.dumps(c.RetroBootWindow(p).doc(), indent=1, ensure_ascii=False))
    sup.display_ok, sup.paused = None, True
    (out / "boot-options.json").write_text(json.dumps(c.RetroBootWindow(p, kind="boot").doc(), indent=1,
                                                      ensure_ascii=False))
    sup.state, sup.qmp_ok, sup.display_ok, sup.paused, sup.machine = "stopped", None, None, False, None
    print(f"wrote {len(screens) + 18} documents to {out}")
    td.cleanup()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
