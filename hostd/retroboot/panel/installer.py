"""Installer mode (ARCHITECTURE section 8): hostd --installer.

welcome -> hardware check -> destination disk -> explicit erase confirmation
naming model and size -> Choose Macs to Install (every Mac on this disc, in
RetroBoot downloads, the Internet catalog and on USB disks; one or more, and the
one that starts first: :mod:`.picker`) -> display check -> optional network/web
access -> install with progress (Macs to download are downloaded straight onto the
new disk; a failed download offers Retry or Skip, the other Macs stay installed)
-> "Remove the installation USB stick or disc, then click Restart."

Networks: the hardware check names the Wi-Fi adapter and offers "Wi-Fi…" while
the computer is offline; ``retroboot/network.conf`` on the installation media
(:mod:`retroboot.netconf`) is imported when the installer starts. Every Wi-Fi
profile stored during installation (NetworkManager keyfiles in
``paths.nm_connections``) is copied to the installed system's data partition
(``install-to-disk --network-profiles``), which joins it at its first start.

Contract with the image workstream:

* ``/usr/lib/retroboot/installer/list-disks`` prints a JSON array of
  ``{path, model, size_bytes, removable, transport, is_boot_medium}``.
* ``/usr/lib/retroboot/installer/install-to-disk --target DEV [--machine-dir DIR ...]
  --seed FILE [--hostname NAME]`` writes JSON progress lines ``{"phase","progress",
  "message"}`` (after each machine also ``machine_dir`` and ``machine_id``) and exits 0
  on success.  It copies every machine directory and puts the seed (settings the
  installed hostd adopts on first start) at ``RBDATA/config/install-seed.json``.
* ``install-to-disk --target DEV --mount-data DIR`` / ``--unmount-data DIR`` open the
  new disk's RBDATA for the Macs hostd downloads and extracts onto it
  (:func:`catalog.install_machine` with ``machines_dir``), and close it again.

Raspberry Pi (ARCHITECTURE 8.2; :attr:`InstallerPanel.board` "rpi"): every
install-to-disk call gets ``--board rpi``. The installer's own SD card or USB stick is
offered as "This SD card: free space after the installer" when list-disks' boot-medium
record has an eligible ``free_space`` plan (``--mode free-space``: the installer is
kept, RetroBoot goes after it, Recovery's Start Installer… opens the installer again);
other disks are erased with the Pi's standard layout. A disk that already has RetroBoot
is not updated in place on a Pi (no Software Update there). install-to-disk's last line
says whether the medium was released ("released"), stays ("kept") or must be removed
after a shut down ("in-use": too little memory to copy the installer into RAM).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from .. import labauth, netconf, schema
from ..catalog import CatalogClient, ExtractLimits, ImportError_, download_of, install_machine
from ..jobs import Job, JobFailed, SUCCEEDED
from ..network import NetworkError
from ..util import atomic_write_json, run as run_cmd
from . import ui
from .controller import JobScreen, KeepSetting, Panel, Screen, TimeZone, WiFi
from .picker import ChooseMacs

log = logging.getLogger("retroboot.installer")

GiB = 1024 ** 3
# install-to-disk's free-space layout besides RBDATA (rpi_layout.FREE_FIXED_BYTES: the 3 GiB
# root on a 4 MiB boundary); the picker subtracts it and its 1 GiB margin from the free space.
PI_FREE_FIXED_BYTES = 3 * GiB + 4 * 1024 ** 2


def keyboard_present(devices_file: Path = Path("/proc/bus/input/devices")) -> bool:
    try:
        text = devices_file.read_text()
    except OSError:
        return False
    for block in text.split("\n\n"):
        handlers = re.search(r"^H: Handlers=(.*)$", block, re.M)
        ev = re.search(r"^B: EV=([0-9a-f]+)$", block, re.M)
        if handlers and "kbd" in handlers.group(1).split() and ev and int(ev.group(1), 16) & 0x100002 == 0x100002:
            return True
    return False


class InstallerState:
    def __init__(self):
        self.disks: list[dict] | None = None
        self.disk_error: str | None = None
        self.target: dict | None = None
        self.erase_confirmed = False
        self.picker = None                             # picker.Picker: the Macs found and checked
        self.host_only = False
        self.plan: list[dict] = []                     # the chosen Macs: {key, choice, id, name, startup}
        self.results: dict[str, dict] = {}             # per Mac: {state, id, error}
        self.media_network: dict | None = None        # {"ssid", "profile", "connected"}
        self.media_network_error: str | None = None
        # RetroBoot downloads (labauth): from the medium, or typed in the Sign In sheet. The
        # installed system keeps it (RBDATA/config/lab-auth.env, 0600) so another Mac can be
        # downloaded later: always for the medium's sign-in, for a typed one unless "Remember
        # on this computer" (checked by default) was unchecked.
        self.media_lab: labauth.MediaLab | None = None
        self.media_lab_error: str | None = None
        self.lab_auth: labauth.LabAuth | None = None
        self.lab_remember = True
        # Raspberry Pi: why the installer's own card isn't offered (list-disks' free_space),
        # how the installation medium is named, and what install-to-disk did with it.
        self.free_note: str | None = None
        self.medium_name = "installation USB stick or disc"   # the PC's boot medium, when list-disks names it
        self.medium: str | None = None          # "released" | "kept" | "in-use" (--board rpi)

    @property
    def remember_auth(self) -> labauth.LabAuth | None:
        """The sign-in the installed system keeps (the one the installer uses)."""
        if self.lab_auth is not None:
            return self.lab_auth if self.lab_remember else None
        if self.media_lab and self.media_lab.auth:
            return self.media_lab.auth
        return None

    def names(self) -> str:
        """"Mac OS 9.2.2 Deluxe v4", or "3 Macs"."""
        if len(self.plan) == 1:
            return self.plan[0]["name"]
        return f"{len(self.plan)} Macs" if self.plan else "Mac OS"


class InstallerPanel(Panel):
    def __init__(self, app):
        super().__init__(app)
        self.st = InstallerState()

    async def start(self) -> None:
        self.app.lifecycle.event("installer_start")
        self.media_task = self.app.loop.create_task(self.import_media_network())
        self.lab_task = self.app.loop.create_task(self.import_media_lab())
        await self.show(Welcome(self))

    async def import_media_lab(self, medium: Path | None = None) -> None:
        """retroboot/lab-auth.env on the boot medium: where RetroBoot downloads are, and
        maybe the sign-in (labauth.py).  Values are never logged or shown."""
        try:
            found = await labauth.find_on_media(medium or self.app.paths.run / "live" / "medium",
                                                runner=self.app.network.run, mount_root=self.app.paths.run,
                                                fat=self.app.network.enabled)
        except labauth.LabAuthError as exc:
            self.st.media_lab_error = str(exc)
            self.app.lifecycle.event("installer_media_lab_invalid", reason=str(exc))
            return
        except Exception:  # noqa: BLE001 -- the medium's file never blocks the installer
            log.exception("reading retroboot/lab-auth.env failed")
            return
        if found is None:
            return
        if found.auth is not None:
            for secret in found.auth.secrets():
                self.app.redactor.add(secret)
        self.st.media_lab, self.st.media_lab_error = found, None
        self.app.lifecycle.event("installer_media_lab", found_on=found.source, signed_in=found.auth is not None,
                                 remember=found.remember)
        ps = self.st.picker
        if ps is not None and isinstance(self.screen, ChooseMacs):
            await ps.load_hosted()

    async def import_media_network(self, medium: Path | None = None) -> None:
        """retroboot/network.conf on the boot medium -> a stored Wi-Fi profile."""
        if not self.app.network.enabled:
            return
        try:
            net = await netconf.find_on_media(medium or self.app.paths.run / "live" / "medium",
                                              runner=self.app.network.run, mount_root=self.app.paths.run)
            if net is None:
                return
            if net.password:
                self.app.redactor.add(net.password)
            result = await netconf.import_network(self.app.network, net)
            self.st.media_network = result
            self.app.lifecycle.event("installer_media_network", found_on=net.source, connected=result["connected"])
        except netconf.NetConfError as exc:
            self.st.media_network_error = str(exc)
            self.app.lifecycle.event("installer_media_network_invalid", reason=str(exc))
        except NetworkError as exc:
            self.st.media_network_error = str(exc).rstrip(".")
            self.app.lifecycle.event("installer_media_network_failed", code=exc.code)
        if self.screen is not None and self.screen.doc_id in ("installer-hardware", "installer-network"):
            await self.refresh()

    async def show_recovery(self) -> None:  # no recovery in the installer
        return

    @property
    def board(self) -> str:
        """install-to-disk's layout: "rpi" on a Raspberry Pi (and any other aarch64 board, such
        as QEMU's virt in the lab), else "x86"; hostd.json ``installer_board`` forces one."""
        b = str(self.app.cfg.installer_board or "auto").lower()
        if b in ("x86", "rpi"):
            return b
        plat = self.app.platform
        return "rpi" if plat.is_raspberry_pi or plat.machine == "aarch64" else "x86"

    async def list_disks(self) -> list[dict]:
        tool = self.app.paths.installer_dir / "list-disks"
        rc, out, err = await run_cmd([str(tool)], timeout=30)
        if rc != 0:
            raise RuntimeError(err.strip()[:200] or f"list-disks exited {rc}")
        data = json.loads(out)
        if not isinstance(data, list):
            raise RuntimeError("list-disks returned no list")
        disks = []
        for d in data:
            if not isinstance(d, dict) or not isinstance(d.get("path"), str) or not d["path"].startswith("/dev/"):
                continue
            contents = d.get("contents") if isinstance(d.get("contents"), dict) else {}
            disk = {"path": d["path"], "model": str(d.get("model") or "Disk")[:60],
                    "size_bytes": int(d.get("size_bytes") or 0), "removable": bool(d.get("removable")),
                    "transport": str(d.get("transport") or ""), "is_boot_medium": bool(d.get("is_boot_medium")),
                    "serial": str(d.get("serial") or "")[:40],
                    "contents": str(contents.get("summary") or "")[:60],
                    "retroboot": contents.get("os") == "RetroBoot", "key": d["path"], "mode": "disk"}
            disks.append(disk)
            if disk["is_boot_medium"] and self.board != "rpi":
                self.st.medium_name = f"installation {medium_name(disk)}"
            if disk["is_boot_medium"] and self.board == "rpi":
                free = self.free_space_target(disk, d.get("free_space"))
                if free:
                    disks.insert(0, free)
        return disks

    def free_space_target(self, medium: dict, fs) -> dict | None:
        """The installer's own card as a destination: only the free space after the installer."""
        name = medium_name(medium)
        self.st.medium_name = f"installation {name}"
        if not isinstance(fs, dict):
            self.st.free_note = None
            return None
        if not fs.get("eligible"):
            why = str(fs.get("message") or fs.get("reason") or "it has no room after the installer")[:120]
            self.st.free_note = f"This {name} isn’t offered: {why}."
            return None
        self.st.free_note = None
        existing = fs.get("existing") == "RetroBoot"
        return {"path": medium["path"], "key": medium["path"] + "#free-space", "mode": "free-space",
                "model": f"This {name}", "medium_name": name, "size_bytes": int(fs.get("bytes") or 0),
                "disk_bytes": medium["size_bytes"], "fixed_bytes": PI_FREE_FIXED_BYTES,
                "removable": medium["removable"], "transport": medium["transport"], "is_boot_medium": False,
                "serial": medium["serial"], "existing": "RetroBoot" if existing else None,
                "contents": ("RetroBoot after the installer" if existing else "Free space after the installer"),
                "retroboot": False}


def medium_name(d: dict) -> str:
    """"SD card", "USB stick", "disc" or "disk": the installation medium, in the windows."""
    if d["path"].startswith("/dev/mmcblk"):
        return "SD card"
    if d.get("transport") == "usb":
        return "USB stick"
    if d["path"].startswith("/dev/sr"):
        return "disc"
    return "disk"


def target_args(panel: "InstallerPanel", st: "InstallerState") -> list[str]:
    """install-to-disk's --board/--mode for the chosen destination ([] on a PC)."""
    if panel.board != "rpi":
        return []
    return ["--board", "rpi"] + (["--mode", "free-space"] if (st.target or {}).get("mode") == "free-space" else [])


def disk_key(d: dict) -> str:
    """A destination's row id: the disk's path; the free space after the installer on the
    installer's own Raspberry Pi card is "<path>#free-space"."""
    return d.get("key") or d["path"]


def disk_detail(d: dict, disks: list[dict]) -> str:
    """Size, transport, what the disk holds now and, for look-alike disks, a serial suffix."""
    if d.get("mode") == "free-space":     # the window's note says that the installer is kept
        return f"{ui.human_bytes(d['size_bytes'])} " + ("with RetroBoot, after the installer" if d.get("existing")
                                                        else "free after the installer")
    parts = [ui.human_bytes(d["size_bytes"]), d["transport"] or "disk"]
    if d["removable"]:
        parts.append("removable")
    twins = [x for x in disks if x["model"] == d["model"] and x["size_bytes"] == d["size_bytes"]]
    if len(twins) > 1 and d.get("serial"):
        parts.append("…" + d["serial"][-4:])      # before the contents, which may be cut off
    if d.get("contents"):
        parts.append(d["contents"])
    return " · ".join(parts)


class IScreen(Screen):
    guest_background_ok = False

    @property
    def st(self) -> InstallerState:
        return self.panel.st


class Welcome(IScreen):
    doc_id = "installer-welcome"

    def doc(self):
        text = ("This installer sets up RetroBoot and Mac OS on this computer, or updates RetroBoot on a "
                "disk that already has it. You choose the disk; nothing is erased until you confirm.")
        if self.panel.board == "rpi":      # no updates in place on a Pi; its own card can hold RetroBoot
            text = ("This installer sets up RetroBoot and Mac OS on this computer: in the free space on the "
                    "installer’s own card or stick, or on another disk. You choose; nothing is erased until "
                    "you confirm.")
        return ui.window(self.doc_id, "RetroBoot Installer", [
            ui.text("Welcome to RetroBoot", "emphasis"),
            ui.text(text),
            ui.buttons(ui.btn("poweroff", "Shut Down"), ui.btn("continue", "Continue", default=True)),
        ], width=480)

    async def action(self, id, values):
        if id == "continue":
            await self.panel.show(HardwareCheck(self.panel))
        elif id == "poweroff":
            await self.app.supervisor.poweroff("installer")


class HardwareCheck(IScreen):
    doc_id = "installer-hardware"

    def __init__(self, panel):
        super().__init__(panel)
        self.addresses: list[str] | None = None
        self.wifi: dict | None = None
        self.app.loop.create_task(self._load())

    async def _load(self):
        try:
            self.st.disks = await self.panel.list_disks()
            self.st.disk_error = None
        except Exception as exc:
            self.st.disks = []
            self.st.disk_error = str(exc)
            log.warning("list-disks failed: %s", exc)
        self.addresses = await self.app.network.all_addresses()
        try:
            self.wifi = await self.app.network.wifi_hardware()
        except Exception:   # an adapter problem never blocks the installer
            self.wifi = None
        self.app.lifecycle.event("installer_hardware", disks=len(self.st.disks),
                                 candidates=len(self.candidates()), addresses=len(self.addresses),
                                 display=(self.app.presenter.display or {}).get("mode"), keyboard=keyboard_present())
        if self.panel.screen is self:
            await self.panel.refresh()

    def candidates(self) -> list[dict]:
        return [d for d in (self.st.disks or []) if not d["is_boot_medium"]]

    def doc(self):
        d = self.app.presenter.display or {}
        mode = d.get("mode")
        lines = [
            ("Display", f"{mode[0]} × {mode[1]}" if mode else "Not detected"),
            ("Keyboard", "Ready" if keyboard_present() else "Not detected (the pointer still works)"),
        ]
        plat = self.app.platform
        if plat.is_raspberry_pi and plat.model:
            ram = plat.ram_mb
            lines.insert(0, ("Computer", plat.model + (f", {-(-ram // 1024)} GB" if ram and ram >= 512 else "")))
            health = self.app.health.sample() if self.app.health else None
            if health and health.get("text"):
                lines.insert(1, ("Power", health["text"]))
        if self.st.disks is None:
            lines.append(("Disks", "Checking…"))
        elif self.st.disk_error:
            lines.append(("Disks", "Could not be listed"))
        else:
            n = len(self.candidates())
            lines.append(("Disks", f"{n} disk{'s' if n != 1 else ''} available" if n else "No disk available"))
        if self.addresses is None:
            lines.append(("Network", "Checking…"))
        else:
            lines.append(("Network", "Connected" if self.addresses else "Not connected (optional)"))
        if self.wifi:
            lines.append(("Wi-Fi", self.wifi["label"]))
        items = [ui.text("Checking this computer…" if self.st.disks is None else "This computer is ready."
                         if self.candidates() else "RetroBoot needs a disk to install on.", "emphasis")]
        items += [ui.text(f"{k}: {v}") for k, v in lines]
        media = netconf.describe(self.st.media_network, self.st.media_network_error)
        if media:
            items.append(ui.text(media, "small"))
        offline = self.addresses is not None and not self.addresses
        buttons = [ui.btn("back", "Back"), ui.btn("rescan", "Check Again")]
        if self.wifi and offline:
            buttons.append(ui.btn("wifi", "Wi-Fi…"))
        buttons.append(ui.btn("continue", "Continue", default=True, disabled=not self.candidates()))
        items.append(ui.buttons(*buttons))
        return ui.window(self.doc_id, "Hardware Check", items, width=520)

    async def action(self, id, values):
        if id == "back":
            await self.panel.show(Welcome(self.panel))
        elif id == "rescan":
            self.st.disks = None
            await self.panel.refresh()
            await self._load()
        elif id == "wifi":
            p = self.panel
            await p.show(WiFi(p, back=lambda: p.show(HardwareCheck(p))))
        elif id == "continue":
            await self.panel.show(Destination(self.panel))


class Destination(IScreen):
    doc_id = "installer-destination"

    def __init__(self, panel):
        super().__init__(panel)
        cands = [d for d in (self.st.disks or []) if not d["is_boot_medium"]]
        # Preselect only an unambiguous single disk: with several candidates the user
        # chooses explicitly (on the first hardware tested, two identical 2 TB disks
        # were listed and the preselected one held data, not the Windows install).
        self.selected = disk_key(self.st.target) if self.st.target else \
            (disk_key(cands[0]) if len(cands) == 1 else None)

    def doc(self):
        cands = [d for d in self.st.disks or [] if not d["is_boot_medium"]]
        rows = [ui.row(disk_key(d), d["model"], disk_detail(d, cands), "disk") for d in cands]
        pi = self.panel.board == "rpi"
        free = [d for d in cands if d.get("mode") == "free-space"]
        if free:
            note = (f"“{free[0]['model']}” keeps the installer and uses only the free space after it"
                    + ("; any other disk you choose will be erased." if len(cands) > 1 else "."))
        elif any(d.get("retroboot") for d in cands) and not pi:
            note = "A disk that already has RetroBoot can be updated; any other disk you choose will be erased."
        else:
            note = "The disk you choose will be erased."
        items = [ui.text("Choose where to install RetroBoot." if free else "Choose the disk to install RetroBoot on."),
                 ui.lst("disk", rows, self.selected, rows=5), ui.text(note, "small")]
        if pi and self.st.free_note:
            items.append(ui.text(self.st.free_note, "small"))
        items.append(ui.buttons(ui.btn("back", "Back"),
                                ui.btn("continue", "Continue", default=True, disabled=not self.selected)))
        return ui.window(self.doc_id, "Choose a Disk", items, width=500)

    async def change(self, id, value):
        if id == "disk":
            self.selected = value
            await self.panel.refresh()      # Continue is enabled by a selection

    async def action(self, id, values):
        if id == "back":
            await self.panel.show(HardwareCheck(self.panel))
        elif id == "continue":
            sel = values.get("disk") or self.selected
            disk = next((d for d in self.st.disks or [] if disk_key(d) == sel and not d["is_boot_medium"]), None)
            if disk:
                self.st.target = disk
                if disk.get("retroboot") and self.panel.board != "rpi":
                    await self.panel.show(UpdateOrErase(self.panel))
                else:
                    await self.panel.show(EraseConfirm(self.panel))


class UpdateOrErase(IScreen):
    """A disk that already has RetroBoot: update it in place (Mac OS, documents and settings kept;
    ARCHITECTURE 13) or erase it."""
    doc_id = "installer-update-or-erase"

    def doc(self):
        d = self.st.target
        from .. import __version__
        return ui.window(self.doc_id, "RetroBoot Is Installed", [
            ui.text(f"“{d['model']}” already has RetroBoot.", "emphasis"),
            ui.text(f"Update it to RetroBoot {self.app.updates.release.get('version') or __version__}. Mac OS, "
                    "your documents and your settings are kept, and the version it has now stays on the disk: "
                    "if the new one doesn't start, the computer goes back to it by itself."),
            ui.text("Or erase the disk and install RetroBoot from the beginning.", "small"),
            ui.buttons(ui.btn("back", "Back"), ui.btn("erase", "Erase and Install…"),
                       ui.btn("update", "Update", default=True)),
        ], width=520)

    async def action(self, id, values):
        p = self.panel
        if id == "back":
            await p.show(Destination(p))
        elif id == "erase":
            await p.show(EraseConfirm(p))
        elif id == "update":
            self.app.lifecycle.event("installer_update_chosen", target=self.st.target["path"],
                                     model=self.st.target["model"])
            await p.start_job(lambda: update_job(self.app, self.st), "Updating RetroBoot", self._after)

    async def _after(self, job: Job):
        p = self.panel
        if job.state == SUCCEEDED:
            await p.show(Updated(p))
        else:
            await p.message("stop", "RetroBoot wasn't updated.", job.message, then=lambda: p.show(Destination(p)))


def update_job(app, st: InstallerState) -> Job:
    """``python3 -m retroboot.update update-disk``: the installer's root into the disk's other slot,
    one trial at the next start.  Refuses disks it doesn't recognize; touches only that disk."""
    async def body(job: Job):
        import os
        import sys
        import retroboot
        target = st.target["path"]
        env = dict(os.environ, PYTHONPATH=str(Path(retroboot.__file__).resolve().parent.parent))
        argv = [sys.executable, "-m", "retroboot.update", "update-disk", "--target", target]
        app.lifecycle.event("installer_update_started", target=target)
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE, env=env)
        last = {}
        async for raw in proc.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            last = msg
            prog = msg.get("progress")
            job.update(float(prog) if isinstance(prog, (int, float)) else None,
                       str(msg.get("message") or "Updating…")[:120])
        err = (await proc.stderr.read()).decode("utf-8", "replace")
        rc = await proc.wait()
        if rc != 0 or last.get("phase") != "armed":
            (app.paths.logs / "update-disk.stderr.log").write_text(err[-20000:])
            app.lifecycle.event("installer_update_failed", rc=rc, code=last.get("code"))
            raise JobFailed(last.get("message") if last.get("phase") == "error" else "Writing to the disk failed.",
                            None, last.get("code") or "update_failed")
        app.lifecycle.event("installer_update_finished", target=target, slot=last.get("slot"),
                            converted_grub=last.get("converted_grub"))
        toram = app.paths.lib / "image" / "rb-live-toram"
        if Path("/run/retroboot/live/medium").is_dir() and toram.exists():
            job.update(0.99, "Finishing…")     # the disc can be removed before Restart
            await run_cmd([str(toram)], timeout=600)
        return {"target": target, "slot": last.get("slot")}
    return app.jobs.start("installer.update", body, resources=("install", "machines"))


class Updated(IScreen):
    doc_id = "installer-updated"

    def doc(self):
        return ui.alert(self.doc_id, "note", "RetroBoot is updated.",
                        f"Remove the {self.st.medium_name}, then click Restart. Mac OS starts as before.",
                        ui.buttons(ui.btn("restart", "Restart", default=True)))

    async def action(self, id, values):
        if id == "restart":
            await self.app.supervisor.reboot_host("installer update finished")


class EraseConfirm(IScreen):
    doc_id = "installer-erase"

    def doc(self):
        d = self.st.target
        if d.get("mode") == "free-space":
            return self.free_space_doc(d)
        holds = f" It contains: {d['contents']}." if d.get("contents") and d["contents"] != "Empty" else ""
        twins = [x for x in self.st.disks or [] if x.get("model") == d["model"] and x.get("size_bytes") == d["size_bytes"]]
        which = f" (serial number ending {d['serial'][-4:]})" if len(twins) > 1 and d.get("serial") else ""
        return ui.alert(self.doc_id, "caution", f"Erase “{d['model']}” ({ui.human_bytes(d['size_bytes'])})?",
                        f"Everything on this disk{which} will be permanently erased and replaced with "
                        f"RetroBoot.{holds} This cannot be undone.",
                        ui.buttons(ui.btn("cancel", "Cancel", default=True), ui.btn("erase", "Erase and Continue")))

    def free_space_doc(self, d):
        """The installer's own card: only the space after the installer is used."""
        name, size = d.get("medium_name") or "card", ui.human_bytes(d["size_bytes"])
        if d.get("existing") == "RetroBoot":
            return ui.alert(self.doc_id, "caution", f"Replace RetroBoot on this {name}?",
                            f"The RetroBoot installed after the installer on this {name} will be "
                            f"permanently erased, with its Macs, documents and settings, and installed again in the "
                            f"same {size}. The installer stays on the {name}. This cannot be undone.",
                            ui.buttons(ui.btn("cancel", "Cancel", default=True), ui.btn("erase", "Erase and Continue")))
        return ui.alert(self.doc_id, "caution", f"Install in the free space on this {name}?",
                        f"RetroBoot uses only the {size} of free space after the installer on this {name}. "
                        f"The installer stays on the {name} and nothing before it is changed. "
                        f"Afterwards the {name} starts RetroBoot; Recovery’s Start Installer… opens the installer "
                        "again.",
                        ui.buttons(ui.btn("cancel", "Cancel", default=True), ui.btn("erase", "Use Free Space")))

    async def action(self, id, values):
        if id == "cancel":
            self.st.erase_confirmed = False
            await self.panel.show(Destination(self.panel))
        elif id == "erase":
            self.st.erase_confirmed = True
            if self.st.target.get("mode") == "free-space":
                self.app.lifecycle.event("installer_erase_confirmed", target=self.st.target["path"],
                                         model=self.st.target["model"], size_bytes=self.st.target["size_bytes"],
                                         mode="free-space", existing=self.st.target.get("existing"))
            else:
                self.app.lifecycle.event("installer_erase_confirmed", target=self.st.target["path"],
                                         model=self.st.target["model"], size_bytes=self.st.target["size_bytes"])
            await self.panel.show(ChooseMacs(self.panel))


class DisplayCheck(IScreen):
    doc_id = "installer-display"

    def __init__(self, panel):
        super().__init__(panel)
        self.mode = self.app.settings.desired["display.host_mode"]
        self.size = self.app.settings.desired["display.guest_size"]

    def doc(self):
        d = self.app.presenter.display or {}
        mode = d.get("mode")
        modes = self.app.host_modes()
        mode_opts = [("auto", "Automatic")] + [(m, m.replace("x", " × ").replace("@", " @ ") + " Hz") for m in modes]
        return ui.window(self.doc_id, "Display", [
            ui.text("Is this screen sharp and fully visible?", "emphasis"),
            ui.text(f"Current mode: {mode[0]} × {mode[1]} at {mode[2]} Hz, scale {d.get('scale') or d.get('suggested_scale', 1)}×."
                    if mode else "The display mode could not be read."),
            ui.popup("display.host_mode", "Resolution:", mode_opts, self.mode, disabled=not modes),
            ui.popup("display.guest_size", "Mac screen:", [(s, "Best for this display" if s == "auto" else
                                                            s.replace("x", " × ")) for s in schema.GUEST_SIZES], self.size),
            ui.buttons(ui.btn("back", "Back"), ui.btn("apply", "Try Resolution", disabled=not modes),
                       ui.btn("continue", "Continue", default=True)),
        ], width=500)

    async def change(self, id, value):
        if id == "display.host_mode":
            self.mode = value
        elif id == "display.guest_size":
            self.size = value

    async def action(self, id, values):
        p = self.panel
        self.mode = values.get("display.host_mode", self.mode)
        self.size = values.get("display.guest_size", self.size)
        if id == "back":
            await p.show(ChooseMacs(p))
        elif id == "apply":
            try:
                res = await self.app.apply_engine.change_and_apply({"display.host_mode": self.mode}, origin="installer")
            except schema.ValidationError as exc:
                await p.message("caution", "That mode is not available.", "; ".join(exc.fields.values()),
                                then=lambda: p.show(self))
                return
            if res.get("confirm"):
                await p.show(KeepSetting(p, DisplayCheck(p)))
        elif id == "continue":
            try:
                if self.size != self.app.settings.desired["display.guest_size"]:
                    await self.app.apply_engine.change_and_apply({"display.guest_size": self.size}, origin="installer")
            except schema.ValidationError:
                pass
            await p.show(TimeZone(p, back=lambda: p.show(DisplayCheck(p)), next_=lambda: p.show(NetworkStep(p))))


class NetworkStep(IScreen):
    doc_id = "installer-network"
    tick = 5.0

    def __init__(self, panel):
        super().__init__(panel)
        self.addresses: list[str] = []
        self.primary = None
        self.app.pairing.current_code()
        self.app.loop.create_task(self._load())

    async def _load(self):
        self.addresses = await self.app.network.all_addresses()
        self.primary = await self.app.network.primary_address()
        if self.panel.screen is self:
            await self.panel.refresh()

    async def on_tick(self):
        await self._load()

    def doc(self):
        items = [ui.text("Network and web access (optional)", "emphasis"),
                 ui.text("RetroBoot uses a network to download Macs and updates, and shares it with Mac OS. "
                         "Mac OS also works without one. To join Wi-Fi later, press Control-Alt-Delete and "
                         "open Network & Web Access.")]
        url = self.app.web.url_for(self.primary)
        if url:
            code, _ = self.app.pairing.current_code()
            items += [ui.text("To follow the installation from a phone or computer, open:"), ui.text(url, "emphasis"),
                      ui.text("Pairing code:"), ui.text(" ".join(code), "large"),
                      ui.text("Browsers paired now must pair again after the restart.", "small")]
        else:
            items.append(ui.text("Not connected to a network.", "small"))
        media = netconf.describe(self.st.media_network, self.st.media_network_error)
        if media:
            items.append(ui.text(media, "small"))
        items.append(ui.buttons(ui.btn("wifi", "Wi-Fi…"), ui.btn("back", "Back"),
                                ui.btn("continue", "Install", default=True)))
        return ui.window(self.doc_id, "Network", items, width=520)

    async def action(self, id, values):
        p = self.panel
        if id == "back":
            await p.show(DisplayCheck(p))
        elif id == "wifi":
            await p.show(WiFi(p, back=lambda: p.show(NetworkStep(p))))
        elif id == "continue":
            await p.show(ReadyToInstall(p))


class ReadyToInstall(IScreen):
    doc_id = "installer-ready"

    def doc(self):
        d = self.st.target
        downloads = [m["name"] for m in self.st.plan if m["choice"].kind == "catalog"]
        extra = [ui.text(f"{join_names(downloads)} {'is' if len(downloads) == 1 else 'are'} downloaded onto the "
                         "disk during the installation.", "small")] if downloads else None
        systems = "RetroBoot" if self.st.host_only else f"RetroBoot and {self.st.names()}"
        if d.get("mode") == "free-space":
            name = d.get("medium_name") or "card"
            return ui.alert(self.doc_id, "caution", "Ready to install.",
                            f"{systems} will be installed in the free space after the "
                            f"installer on this {name} ({ui.human_bytes(d['size_bytes'])}). "
                            + ("The RetroBoot there now will be erased. " if d.get("existing") == "RetroBoot" else "")
                            + "The installer is kept.",
                            ui.buttons(ui.btn("install", "Install"), ui.btn("cancel", "Back", default=True)),
                            extra=extra)
        return ui.alert(self.doc_id, "caution", "Ready to install.",
                        f"{systems} will be installed on “{d['model']}” "
                        f"({ui.human_bytes(d['size_bytes'])}). The disk will be erased now.",
                        ui.buttons(ui.btn("install", "Erase and Install"), ui.btn("cancel", "Back", default=True)),
                        extra=extra)

    async def action(self, id, values):
        p = self.panel
        if id == "cancel":
            await p.show(NetworkStep(p))
        elif id == "install" and self.st.erase_confirmed:
            await run_install(p, lambda: install_job(self.app, self.st), "Installing RetroBoot")


def join_names(names: list[str]) -> str:
    quoted = [f"“{n}”" for n in names]
    if len(quoted) <= 2:
        return " and ".join(quoted)
    return ", ".join(quoted[:-1]) + " and " + quoted[-1]


async def run_install(p: Panel, starter, title: str) -> None:
    """An install or download job in the installer's progress window (per-Mac lines), then
    Done, the download problem alert (Try Again / Skip) or the failure message."""
    from ..jobs import Busy
    try:
        job = starter()
    except Busy as exc:
        await p.message("caution", "RetroBoot is busy.", f"Wait for “{exc.job.kind}” to finish, then try again.")
        return
    screen = InstallProgress(p, job, title, lambda j: after_install(p, j))
    await p.show(screen)
    p.app.loop.create_task(p._watch(job, screen))


async def after_install(p: Panel, job: Job) -> None:
    st = p.st
    if job.state != SUCCEEDED:
        eid = (job.result or {}).get("evidence") if isinstance(job.result, dict) else None
        await p.message("stop", "The installation did not finish.",
                        f"{job.message}" + (f" Evidence {eid}." if eid else ""),
                        then=lambda: p.show(Destination(p)))
        return
    if failed_macs(st):
        await p.show(DownloadProblem(p))
        return
    await p.app.archives.release()      # USB disks read for archives
    await p.show(Done(p))


def failed_macs(st: InstallerState) -> list[dict]:
    return [m for m in st.plan if st.results.get(m["key"], {}).get("state") == "failed"]


STATUS_WORDS = {"waiting": "waiting", "copying": "copying…", "downloading": "downloading…",
                "unpacking": "unpacking…", "installed": "installed", "failed": "not installed", "skipped": "skipped"}


class InstallProgress(JobScreen):
    """The job progress window plus one line per Mac when there are several or any download."""
    doc_id = "installer-progress"
    guest_background_ok = False

    def __init__(self, panel, job: Job, title: str, then):
        super().__init__(panel, job, title, then, False)

    def doc(self):
        j, st = self.job, self.panel.st
        items = [ui.text(self.title, "emphasis"), ui.text(j.message or "Working…"), ui.progress(j.progress)]
        if len(st.plan) > 1 or any(m["choice"].remote for m in st.plan):
            for m in st.plan[:8]:
                r = st.results.get(m["key"]) or {}
                word = STATUS_WORDS.get(r.get("state", "waiting"), "")
                if r.get("state") == "downloading" and isinstance(r.get("fraction"), (int, float)):
                    word = f"downloading… {int(r['fraction'] * 100)} %"
                items.append(ui.text(f"{m['name']}: {word}", "small"))
        return ui.window(self.doc_id, "RetroBoot", items, width=460)


class DownloadProblem(IScreen):
    """A Mac to download (or a USB archive) did not install; the others did."""
    doc_id = "installer-download-problem"

    def doc(self):
        st = self.st
        failed = failed_macs(st)
        done = [m["name"] for m in st.plan if st.results.get(m["key"], {}).get("state") == "installed"]
        msg = (f"“{failed[0]['name']}” could not be installed." if len(failed) == 1
               else f"{len(failed)} Macs could not be installed.")
        lines = [ui.text(f"{m['name']}: {st.results[m['key']].get('error') or 'It could not be installed.'}", "small")
                 for m in failed[:4]]
        detail = ("RetroBoot is installed" + (f" with {join_names(done)}" if done else "") +
                  ". Try again, or skip " + ("it" if len(failed) == 1 else "them") +
                  " and add " + ("it" if len(failed) == 1 else "them") + " later with Download a Mac.")
        return ui.alert(self.doc_id, "caution", msg, detail,
                        ui.buttons(ui.btn("wifi", "Wi-Fi…", align="left"), ui.btn("skip", "Skip"),
                                   ui.btn("retry", "Try Again", default=True)), extra=lines, width=460)

    async def action(self, id, values):
        p = self.panel
        if id == "retry":
            self.app.lifecycle.event("installer_downloads_retry", macs=len(failed_macs(self.st)))
            await run_install(p, lambda: downloads_job(self.app, self.st), "Installing Mac OS")
        elif id == "skip":
            self.app.lifecycle.event("installer_downloads_skipped", macs=len(failed_macs(self.st)))
            await run_install(p, lambda: downloads_job(self.app, self.st, skip=True), "Finishing")
        elif id == "wifi":
            await p.show(WiFi(p, back=lambda: p.show(DownloadProblem(p))))


def network_profiles(nm_dir: Path) -> list[Path]:
    """Stored NetworkManager keyfiles (regular files only) the installer carries over."""
    try:
        return sorted(p for p in Path(nm_dir).glob("*.nmconnection") if p.is_file() and not p.is_symlink())
    except OSError:
        return []


SEED_KEYS = ("display.host_mode", "display.pixel_scale", "display.guest_size", "display.scaling",
             "management.hostname", "audio.volume", "input.swap_command_option", "system.timezone", "catalog.urls")
SYSTEM_WEIGHT = 1536 * 1024 ** 2      # the system copy, in "bytes" of progress
INSTALLED_LAB_AUTH = "/var/lib/retroboot/config/lab-auth.env"   # RBDATA on the installed system


def install_job(app, st: InstallerState) -> Job:
    async def body(job: Job):
        if not (st.target and st.erase_confirmed and (st.plan or st.host_only)):
            raise JobFailed("The installation is not ready.", None, "not_ready")
        st.results = {m["key"]: {"state": "waiting", "id": m["id"]} for m in st.plan}
        local = [m for m in st.plan if not m["choice"].remote]
        remote = [m for m in st.plan if m["choice"].remote]
        startup = next((m for m in st.plan if m["startup"]), next(iter(st.plan), None))
        seed = {"settings": {k: app.settings.applied[k] for k in SEED_KEYS}}
        if startup:
            seed["settings"]["machine.active"] = startup["id"]
        seed_path = app.paths.run / "install-seed.json"
        atomic_write_json(seed_path, seed)
        tool = app.paths.installer_dir / "install-to-disk"
        argv = [str(tool), "--target", st.target["path"]] + target_args(app.panel, st)
        st.medium = None
        for m in local:
            argv += ["--machine-dir", str(m["choice"].path)]
        argv += ["--seed", str(seed_path), "--hostname", app.settings.applied["management.hostname"]]
        # Wi-Fi joined during installation (on screen or from the media's network.conf)
        # goes to the installed system's data partition; it joins at first start.
        profiles = network_profiles(app.network.nm_dir)
        if profiles:
            argv += ["--network-profiles", str(app.network.nm_dir)]
        # A Bluetooth keyboard or mouse paired during installation (the setup assistant pairs
        # one when no keyboard is attached) keeps working on the installed system.
        from ..bluetooth import stored_pairings
        bt_pairings = stored_pairings(app.paths.bluetooth_storage)
        if bt_pairings:
            argv += ["--bluetooth", str(app.paths.bluetooth_storage)]
        local_w = sum(m["choice"].size or 0 for m in local)
        remote_w = sum(_weight(m) for m in remote)
        share = (SYSTEM_WEIGHT + local_w) / (SYSTEM_WEIGHT + local_w + remote_w)
        extra = {"board": "rpi", "mode": st.target.get("mode", "disk")} if app.panel.board == "rpi" else {}
        app.lifecycle.event("install_started", target=st.target["path"], model=st.target["model"],
                            size_bytes=st.target["size_bytes"], machines=len(st.plan), embedded=len(local),
                            downloads=len(remote), startup=startup["id"] if startup else None, network_profiles=len(profiles),
                            bluetooth_pairings=bt_pairings, **extra)
        by_dir = {str(m["choice"].path): m for m in local}

        def on_line(msg: dict) -> None:
            if msg.get("medium") in ("released", "kept", "in-use"):
                st.medium = msg["medium"]
            m = by_dir.get(msg.get("machine_dir"))
            if m is None:
                return
            if isinstance(msg.get("machine_id"), str):
                st.results[m["key"]] = {"state": "installed", "id": msg["machine_id"]}
            else:
                st.results[m["key"]]["state"] = "copying"

        rc, last_phase, err_lines = await _run_tool(app, argv, job, 0.0, share, on_line)
        if rc != 0:
            (app.paths.logs / "install-to-disk.stderr.log").write_text("\n".join(err_lines) + "\n")
            eid = await app.diagnostics.capture_evidence("install_failed", f"install-to-disk exited {rc}",
                                                         {"phase": last_phase, "stderr_tail": err_lines[-20:]})
            app.lifecycle.event("install_failed", rc=rc, phase=last_phase, evidence=eid)
            raise JobFailed("Writing to the disk failed.", {"evidence": eid, "rc": rc}, "install_failed")
        for m in local:     # an install-to-disk without per-machine lines (older contract)
            if st.results[m["key"]]["state"] != "installed":
                st.results[m["key"]] = {"state": "installed", "id": m["id"]}
        if remote or st.remember_auth is not None:
            await target_pass(app, st, job, share, 1.0)
        result = install_summary(st)
        if app.panel.board == "rpi":
            app.lifecycle.event("install_finished", target=st.target["path"], installed=len(result["installed"]),
                                failed=len(result["failed"]), medium=st.medium)
        else:
            app.lifecycle.event("install_finished", target=st.target["path"], installed=len(result["installed"]),
                                failed=len(result["failed"]))
        return result
    return app.jobs.start("installer.install", body, resources=("install", "machines"))


def downloads_job(app, st: InstallerState, skip: bool = False) -> Job:
    """Try Again (or Skip) for the Macs whose download or unpacking failed."""
    async def body(job: Job):
        await target_pass(app, st, job, 0.0, 1.0, skip=skip)
        return install_summary(st)
    return app.jobs.start("installer.downloads", body, resources=("install", "machines"))


def install_summary(st: InstallerState) -> dict:
    res = st.results
    return {"target": st.target["path"],
            "installed": [res[m["key"]]["id"] for m in st.plan if res.get(m["key"], {}).get("state") == "installed"],
            "failed": [m["id"] for m in st.plan if res.get(m["key"], {}).get("state") == "failed"],
            "skipped": [m["id"] for m in st.plan if res.get(m["key"], {}).get("state") == "skipped"]}


def _weight(m: dict) -> int:
    c = m["choice"]
    return (c.download or 0) + (c.size or 0)


async def _run_tool(app, argv: list[str], job: Job, lo: float, hi: float, on_line=None):
    """install-to-disk with its JSON progress lines mapped to lo..hi of the job."""
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    err_lines: list[str] = []

    async def read_err():
        async for line in proc.stderr:
            err_lines.append(line.decode("utf-8", "replace").rstrip())
            del err_lines[:-200]

    et = asyncio.get_running_loop().create_task(read_err())
    last_phase = None
    async for raw in proc.stdout:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        prog = msg.get("progress")
        job.update(lo + (hi - lo) * float(prog) if isinstance(prog, (int, float)) else None,
                   str(msg.get("message") or msg.get("phase") or "Installing…")[:120])
        if on_line:
            on_line(msg)
        if msg.get("phase") != last_phase:
            last_phase = msg.get("phase")
            app.lifecycle.event("install_phase", phase=last_phase, progress=prog)
    rc = await proc.wait()
    await et
    return rc, last_phase, err_lines


async def target_pass(app, st: InstallerState, job: Job, lo: float, hi: float, skip: bool = False) -> None:
    """Open the new disk's RBDATA (install-to-disk --mount-data), download and unpack every
    Mac still to install straight onto it (one failure never stops the others), or with
    ``skip`` drop them and their partial downloads; then keep the sign-in if asked, point
    the seed's startup Mac at an installed one, and close the disk again."""
    tool = str(app.paths.installer_dir / "install-to-disk")
    target = st.target["path"]
    mnt = app.paths.run / "install-target"
    pending = [m for m in st.plan if m["choice"].remote and
               st.results.get(m["key"], {}).get("state") not in ("installed", "skipped")]
    job.update(lo, "Opening the new disk…")
    extra = target_args(app.panel, st)
    rc, last_phase, err = await _run_tool(app, [tool, "--target", target, *extra, "--mount-data", str(mnt)], job,
                                          lo, lo)
    if rc != 0:
        # RetroBoot and the Macs from the disc are installed; only these Macs are missing
        # (Try Again opens the disk again; Skip finishes without them: the installed hostd
        # starts the first installed Mac when the seed names a missing one).
        app.lifecycle.event("install_target_open_failed", rc=rc)
        (app.paths.logs / "install-to-disk.stderr.log").write_text("\n".join(err) + "\n")
        for m in pending:
            st.results[m["key"]] = {"state": "skipped" if skip else "failed", "id": m["id"],
                                    "error": "The new disk could not be opened to add this Mac.", "code": "target"}
        job.update(hi, "The new disk could not be opened.")
        return
    try:
        if skip:
            for m in pending:
                st.results[m["key"]] = {"state": "skipped", "id": m["id"]}
                _drop_download(mnt, m["choice"])
        else:
            total = sum(_weight(m) for m in pending) or 1
            done = 0
            for m in pending:
                w = _weight(m)

                def progress(f, message, base=done, w=w):
                    job.update(lo + (hi - lo) * min(1.0, (base + (f or 0.0) * w) / total), message[:120])
                c = m["choice"]
                try:
                    mid = await install_remote(app, st, m, mnt, progress)
                    st.results[m["key"]] = {"state": "installed", "id": mid}
                except ImportError_ as exc:
                    st.results[m["key"]] = {"state": "failed", "id": m["id"], "error": exc.message, "code": exc.code}
                except JobFailed as exc:
                    st.results[m["key"]] = {"state": "failed", "id": m["id"], "error": exc.message, "code": exc.code}
                except Exception as exc:  # noqa: BLE001 -- the other Macs still install
                    log.exception("installing %s failed", m["id"])
                    st.results[m["key"]] = {"state": "failed", "id": m["id"],
                                            "error": f"Unexpected error ({type(exc).__name__}).", "code": "internal"}
                r = st.results[m["key"]]
                app.lifecycle.event("installer_mac", machine=r["id"], source=c.source, kind=c.kind, state=r["state"],
                                    code=r.get("code"))
                done += w
        auth = st.remember_auth
        if auth is not None:
            write_lab_auth(mnt, auth)
            app.lifecycle.event("installer_lab_auth_kept")
        finalize_seed(mnt, st)
    finally:
        rc, _, err = await _run_tool(app, [tool, "--target", target, *extra, "--unmount-data", str(mnt)], job, hi, hi)
        if rc != 0:
            log.warning("closing the new disk failed: %s", (err or [""])[-1][:200])
    job.update(hi, "Mac OS is installed." if not failed_macs(st) else "Some Macs could not be installed.")


async def install_remote(app, st: InstallerState, m: dict, data: Path, progress) -> str:
    """One Mac from a catalog (downloaded to data/downloads, verified, unpacked, the archive
    removed) or an archive file (its sidecar SHA-256 checked) into data/machines."""
    c = m["choice"]
    res = st.results[m["key"]]
    machines = data / "machines"
    if c.kind == "catalog":
        res["state"] = "downloading"
        client = CatalogClient(c.catalog_url, data / "downloads", ca_file=app.cfg.catalog_ca_file, auth=c.auth)

        def dl_progress(f, message):
            res["fraction"] = f
            progress((f or 0.0) * 0.7, f"{c.name}: {message}")
        archive = await client.download(c.entry, dl_progress)
        res["state"] = "unpacking"
        dl = download_of(c.entry)
        source = {"type": "catalog", "template_id": c.entry["id"], "archive_sha256": dl["sha256"],
                  "archive_format": dl["format"], "catalog_url": c.catalog_url,
                  "catalog": c.source, "via": "installer"}
        limits = ExtractLimits(c.entry["installedBytes"], c.entry.get("requiredStorageBytes"))
        mid = await install_machine(archive, None, c.name, c.entry.get("osVersion"), source, limits,
                                    lambda f, msg: progress(0.7 + (f or 0.0) * 0.3, f"Unpacking {c.name}…"),
                                    machines_dir=machines, machine_id=m["id"],
                                    expected_guest_os=c.entry.get("guestOS"), os_build=c.entry.get("osBuild"))
        archive.unlink(missing_ok=True)     # verified and unpacked: the space goes back to the Macs
        return mid
    res["state"] = "unpacking"
    if c.sha256:
        progress(0.0, f"Checking {c.name}…")
        from ..catalog import _sha256
        if await asyncio.to_thread(_sha256, c.path) != c.sha256:
            raise ImportError_("The archive did not pass its integrity check.", "integrity")
    source = {"type": (c.origin or "usb").split(":")[0], "archive_name": c.path.name, "archive_sha256": c.sha256,
              "via": "installer"}
    return await install_machine(c.path, None, c.name, None, source, ExtractLimits(None, None),
                                 lambda f, msg: progress(f or 0.0, f"Unpacking {c.name}…"),
                                 machines_dir=machines, machine_id=m["id"])


def _drop_download(data: Path, c) -> None:
    """A skipped Mac's partial or verified download on the new disk."""
    if c.kind != "catalog" or not c.sha256:
        return
    for suffix in (".part", ".tar.gz"):
        try:
            (data / "downloads" / f"{c.sha256}{suffix}").unlink()
        except FileNotFoundError:
            pass


def finalize_seed(data: Path, st: InstallerState) -> None:
    """machine.active: the chosen startup Mac when it is installed, else the first installed."""
    path = data / "config" / "install-seed.json"
    try:
        seed = json.loads(path.read_text())
        if not isinstance(seed, dict) or not isinstance(seed.get("settings"), dict):
            raise ValueError
    except (OSError, ValueError):
        seed = {"settings": {}}
    res = st.results
    installed = [m for m in st.plan if res.get(m["key"], {}).get("state") == "installed"]
    start = next((m for m in installed if m["startup"]), installed[0] if installed else None)
    if start:
        seed["settings"]["machine.active"] = res[start["key"]]["id"]
    else:
        seed["settings"].pop("machine.active", None)
    atomic_write_json(path, seed)


def write_lab_auth(data: Path, auth: labauth.LabAuth) -> None:
    """"Remember on this computer": the 0600 lab-auth file on the new disk's RBDATA and
    hostd.local.json naming it (the installed hostd's lab_http_auth_file)."""
    cfg = data / "config"
    labauth.write_env(cfg / "lab-auth.env", auth)
    local = cfg / "hostd.local.json"
    try:
        current = json.loads(local.read_text())
        if not isinstance(current, dict):
            current = {}
    except (OSError, ValueError):
        current = {}
    current["lab_http_auth_file"] = INSTALLED_LAB_AUTH
    atomic_write_json(local, current)


class Done(IScreen):
    doc_id = "installer-done"

    def doc(self):
        st = self.st
        res = st.results
        missing = [m["name"] for m in st.plan if res.get(m["key"], {}).get("state") in ("skipped", "failed")]
        installed = [m for m in st.plan if res.get(m["key"], {}).get("state") == "installed"]
        detail = f"Remove the {st.medium_name}, then click Restart."
        button = ui.btn("restart", "Restart", default=True)
        extra = []
        if self.panel.board == "rpi":
            detail, button, extra = self.pi_finish()
        if missing and installed:
            extra.append(ui.text(f"{join_names(missing)} {'was' if len(missing) == 1 else 'were'} not installed. "
                                 "You can add Macs later with Download a Mac (Control-Alt-Delete).", "small"))
        elif missing or st.host_only:
            extra.append(ui.text("No Mac OS system was installed. RetroBoot asks for one when it starts.", "small"))
        return ui.alert(self.doc_id, "note", "RetroBoot is installed.", detail, ui.buttons(button), extra=extra)

    def pi_finish(self):
        """Raspberry Pi: the card stays (free space), comes out before Restart, or after a
        Shut Down when the installer could not be copied into memory; a USB disk starts
        only when the bootloader's boot order reaches it."""
        st, t = self.st, self.st.target or {}
        medium = st.medium_name
        if t.get("mode") == "free-space":
            name = t.get("medium_name") or "card"
            return (f"Click Restart to start RetroBoot from this {name}. The installer stays on it: "
                    "RetroBoot Recovery’s Start Installer… opens it again.",
                    ui.btn("restart", "Restart", default=True), [])
        extra = []
        if not t.get("path", "").startswith("/dev/mmcblk"):
            extra.append(ui.text("The Raspberry Pi starts from this disk when its boot order includes it and no "
                                 "SD card that starts is inserted first (Raspberry Pi 4: SD card, then USB, by "
                                 "default). RetroBoot doesn’t change the boot order; rpi-eeprom-config sets it.",
                                 "small"))
        if st.medium == "in-use":
            return (f"Click Shut Down, remove the {medium}, then start the Raspberry Pi again.",
                    ui.btn("shutdown", "Shut Down", default=True), extra)
        return f"Remove the {medium}, then click Restart.", ui.btn("restart", "Restart", default=True), extra

    async def action(self, id, values):
        if id == "restart":
            await self.app.supervisor.reboot_host("installer finished")
        elif id == "shutdown":
            await self.app.supervisor.poweroff("installer finished")
