"""hostd entry point: ``python3 -m retroboot.hostd [--installer] [--dev] [--root DIR]``.

Installer mode: ``--installer``, ``RETROBOOT_MODE=installer`` or the kernel
command line ``retroboot.mode=installer`` (the installer ISO's GRUB entry).

Wires the settings service, presenter client, supervisor, panel, web server,
network adapter, machines/catalog/checkpoints and diagnostics, and exposes the
typed job helpers shared by the local panel and the web API.
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import re
import shlex
import shutil
import signal
import sys
import time
from pathlib import Path

from . import __version__, audio, schema
from .guestprep import UNAVAILABLE, GuestPrep
from .airport import AirPort, prep_options as airport_prep_options
from .apply import ApplyEngine
from .battery import Battery
from .bluetooth import Bluetooth, BluetoothError
from .keys import FunctionKeys
from .catalog import ArchiveSources, CatalogClient, ExtractLimits, ImportError_, download_of, install_machine
from .checkpoints import CheckpointError, Checkpoints
from .config import Config
from .diagnostics import Diagnostics
from .guestrun import make_runner
from .jobs import Job, JobFailed, JobManager
from .laptop import Laptop
from .lifecycle import LifecycleLog
from .machines import (FAMILIES, FAMILY_CHOICES, POWERMAC, QUADRA800, QUADRA_BLANK_DEFAULT_GB,
                       QUADRA_BLANK_NOTE, MachineError, MachineRegistry, PackageCache, blank_sizes_gb,
                       default_machine_settings, validate_machine_settings)
from . import quadra
from .media import MediaError, MediaLibrary
from .network import AudioMixer, NetworkAdapter, NetworkError
from .optical import OpticalDrives
from .presenter import PresenterClient
from .security import PairingService
from .settings import SettingsService, SettingsStore
from .startup import StartupProgress
from .supervisor import Supervisor
from .update.manager import UpdateManager
from .util import Redactor, mono, read_json, wall

log = logging.getLogger("retroboot.hostd")


class RedactingFilter(logging.Filter):
    def __init__(self, redactor: Redactor):
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        record.msg = self.redactor(msg)
        record.args = ()
        return True


class _Deferred:
    """Builds the real object on first attribute access and then stands in for it."""

    def __init__(self, factory):
        object.__setattr__(self, "_factory", factory)
        object.__setattr__(self, "_obj", None)

    def _get(self):
        obj = object.__getattribute__(self, "_obj")
        if obj is None:
            obj = object.__getattribute__(self, "_factory")()
            object.__setattr__(self, "_obj", obj)
        return obj

    def __getattr__(self, name):
        return getattr(self._get(), name)

    def __setattr__(self, name, value):
        setattr(self._get(), name, value)


class Hostd:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.paths = cfg.paths
        self.mode = cfg.mode
        self.paths.ensure()
        # The board (Raspberry Pi or PC) before anything that depends on it: on a slow
        # board the startup/shutdown deadlines still at their defaults are stretched.
        from . import platform as hostplatform
        self.platform = hostplatform.detect(cfg.device_tree, Path(cfg.sysfs_path) / "class" / "dmi" / "id",
                                            cfg.proc_path)
        self.platform = hostplatform.image_profile(self.platform, self.paths.lib / "rpi-profile.json")
        self.platform_scaled = hostplatform.scale_timeouts(cfg, self.platform)
        # A Pi's or Gemini's power supply and temperature (Diagnostics, status host.throttled): None on PCs.
        self.health = hostplatform.health_for(self.platform, cfg.sysfs_path)
        from .rpidisplay import PiDisplay
        self.rpi_display = PiDisplay(self)
        self._health_task: asyncio.Task | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.lifecycle = LifecycleLog(self.paths.lifecycle_log)
        self.redactor = Redactor()
        image_defaults = None
        if self.paths.defaults_json.is_file():
            try:
                image_defaults = read_json(self.paths.defaults_json, 65536)
            except (OSError, ValueError):
                log.error("image defaults unreadable; using schema defaults")
        if self.platform.setting_defaults:
            # The board's own defaults (Gemini PDA: Alt is Command), below the image's.
            image_defaults = {**self.platform.setting_defaults, **(image_defaults or {})}
        self.store = SettingsStore(self.paths.settings, self.paths.state / "known-good.json", image_defaults)
        self.settings = SettingsService(self.store)
        self.machines = MachineRegistry(self.paths)
        self.package = PackageCache(self.paths.qemu_package, self.paths.ndrv_dir, self.paths.classicvirtio_dir)
        from .osx import PACKAGE_FILES as OSX_PACKAGE_FILES
        self.package_osx = PackageCache(self.paths.qemu_osx_package, files=OSX_PACKAGE_FILES)
        # 68k Macs (quadra.py): their own emulator package and the ROMs kept on RBDATA.
        self.package_m68k = PackageCache(self.paths.qemu_m68k_package, files=quadra.PACKAGE_FILES)
        self.roms = quadra.RomStore(self.paths.rom_store)
        self.jobs = JobManager()
        self.presenter = PresenterClient(self.paths.presenter_sock)
        self.network = NetworkAdapter(enabled=cfg.allow_network_tools, hostname_cmd=cfg.hostname_cmd,
                                      nm_connections_dir=self.paths.nm_connections)
        self.mixer = AudioMixer(enabled=cfg.allow_network_tools)
        from .mdns import MdnsWatch
        from .util import run as run_cmd
        self.mdns = MdnsWatch(self, run_cmd, sysfs=Path(cfg.sysfs_path))   # the published .local name
        self.laptop = Laptop(self)          # backlight/volume keys, lid, battery policy
        from .speakers import SpeakerSwitch
        self.speakers = SpeakerSwitch(self)  # Gemini PDA: speakers or headphones only (the image's hook)
        self.battery = Battery(self)        # battery passthrough: power bridge to QEMU's PMU
        self.keys = FunctionKeys(self)      # brightness/volume/mute keys: Mac OS's buttons or the host
        self.bluetooth = Bluetooth(self)    # Bluetooth keyboards, mice and trackpads (BlueZ)
        self._bluetooth_task: asyncio.Task | None = None
        self.pairing = PairingService(self.paths.state)
        self.pairing.on_change.append(self._new_pairing_code)
        from . import labauth
        # RetroBoot downloads sign-in: hostd.json/hostd.local.json lab_http_auth_file (the installer
        # writes RBDATA/config/lab-auth.env and names it there).  The password only ever goes into
        # Authorization headers for URLs under its base; the redactor keeps it out of every text.
        self.lab_auth = labauth.load(cfg.lab_http_auth_file or (self.paths.data / "config" / "lab-auth.env"))
        if self.lab_auth is not None:
            self.redactor.add(self.lab_auth.secrets()[1])
        from .catalogs import RETROBOOT_CATALOG, Catalogs
        # The public RetroBoot Mac library on every board (D078, D085); the runtime filter
        # (catalogs.runnable) hides the Macs this computer can't start.  ClassicMac's
        # catalog stays available as an image server.
        catalog_url = cfg.catalog_url
        if catalog_url is None:
            catalog_url = RETROBOOT_CATALOG
        self.catalog = CatalogClient(catalog_url, self.paths.downloads, ca_file=cfg.catalog_ca_file,
                                     auth=self.lab_auth)
        self.catalogs = Catalogs(self)    # RetroBoot downloads (with a sign-in) and the Internet catalog
        self.archives = ArchiveSources(self.paths.media, self.paths.installer_dir / "embedded",
                                       enabled=cfg.allow_network_tools)
        self.checkpoints = Checkpoints(self.paths.checkpoints)
        self.guestprep = GuestPrep(self.paths.guestprep_lib)
        self.media = MediaLibrary(self.paths)   # media library: CD images, software to install
        # this computer's CD-ROM drives (a USB CD-ROM drive): discs for Mac OS, Start Up from CD
        self.optical = OpticalDrives(cfg.sysfs_path, getattr(cfg, "dev_path", "/dev"))
        from .platinum import PlatinumAssets
        self.platinum = PlatinumAssets(self.paths.platinum_assets, self.paths.platinum_lib,
                                       enabled=cfg.platinum_build, lifecycle=self.lifecycle)
        from . import formfactor
        self.hardware = formfactor.detect(Path(cfg.sysfs_path), cfg.form_factor,   # "the laptop" or not
                                          self.platform.profile.get("system_batteries", ()))
        # --- Modern Web Helper (retroboot/webproxy.py) ------------------------------
        from .webproxy import WebHelper
        self.webproxy = WebHelper(self)
        # --- AirPort hook (retroboot/airport) -------------------------------------
        self.airport = AirPort(self)
        from .graphics import Graphics
        self.graphics = Graphics(self)
        self.guestprep.options_provider = lambda: airport_prep_options(self.settings.applied,
                                                                       self.paths.qemu_package)
        # ---------------------------------------------------------------------------
        self.diagnostics = Diagnostics(cfg, self.redactor)
        self.runner = make_runner(cfg)
        from . import cpuplace
        self.cpuplace = cpuplace.from_app(self)   # QEMU's threads on the fast cores (Gemini PDA)
        self.supervisor = Supervisor(self)
        from .lidsleep import LidSleep
        from .lidpower import LidPower
        self.lidsleep = LidSleep(self)      # lid_action "sleep": Mac OS sleeps with the lid closed
        self.lidpower = LidPower(self)      # lid_low_power: slow clocks and keyboard poll meanwhile
        self.cpuplace.lid_hold = self.lidpower.hold
        self.startup = StartupProgress(self)   # the startup indicator's timing (4.4.1)
        if self.mode == "installer":
            from .panel.installer import InstallerPanel
            self.panel = InstallerPanel(self)
        else:
            from .panel.controller import Panel
            self.panel = Panel(self)
        self.apply_engine = ApplyEngine(self)
        from .orientation import Rotation
        self.rotation = Rotation(self)      # display.rotation: fixed, or "auto" with the tilt sensor
        from .remote import RemoteScreen
        self.remote = RemoteScreen(self)    # web Screen page: frame tap, remote input
        self.updates = UpdateManager(self)      # Software Update (A/B system slots)
        # The management web server is built on first use, normally by _start_services()
        # after QEMU has started: importing aiohttp and building the routes is about a
        # third of hostd's start-up imports (hostboot report).
        self.web = _Deferred(self._make_web)
        self.scene: dict = {}
        self.stopping = False
        self.powered_off = False
        self.started_mono = mono()
        self.extra_hosts: list[str] = []
        self._boot_task: asyncio.Task | None = None
        self._launch_ready = asyncio.Event()     # set by start() once a launch has what it needs
        self._services_task: asyncio.Task | None = None
        self.services_wait_s = 15.0
        self._wifi_available: bool | None = None
        self._wifi_task: asyncio.Task | None = None
        self._boot_network_task: asyncio.Task | None = None
        self.wifi_watch_s = (2.0, 90.0)          # period, how long to look for a late Wi-Fi driver
        self.guest_isolation: dict = {"loaded": None, "detail": "not checked yet"}
        self.corrupt_disk_recovery: dict | None = None
        self.last_printed_pdf: dict | None = None
        self._printed_pdf_seq = 0
        self._chime_seq = 0
        from .cups import CupsManager
        from .printer import PrintServer
        from .usbshare import UsbShare
        self.cups = CupsManager()
        self.printer = PrintServer(
            self.paths.share,
            enabled=lambda: bool(self.settings.applied.get("printer.enabled", True)),
            on_printed=self._on_printed_pdf,
            cups=self.cups,
            target=lambda: str(self.settings.applied.get("printer.target", "pdf")),
            keep_pdf=lambda: bool(self.settings.applied.get("printer.keep_pdf", False)),
            raw_port=9100)
        self.usbshare = UsbShare(
            self.paths.share,
            enabled=lambda: bool(self.settings.applied.get("usb_share.enabled", True)),
            poll_s=5.0 if self.cfg.allow_network_tools else 0.0)
        self.diagnostics.guest_shot = self.supervisor.guest_screenshot
        self.diagnostics.composed_shot = self.composed_screenshot
        self.diagnostics.status_provider = self.status
        self.diagnostics.guest_stuck = lambda: self.supervisor.qmp_ok is False
        self.diagnostics.last_guest_frame = lambda: self.supervisor.last_frame_png
        self._hook_chime_lifecycle()

    async def _watch_health(self, period_s: float = 15.0) -> None:
        """Raspberry Pi: sample the under-voltage alarm every period_s so a dip between
        two status reads is still reported "since start" (the hwmon driver clears the
        firmware's sticky bits every 2 s).  A few small sysfs reads."""
        while True:
            was = self.health.under_voltage_seen
            try:
                self.health.sample()
            except Exception:  # noqa: BLE001 -- never let this end hostd
                pass
            if self.health.under_voltage_seen and not was:
                self.lifecycle.event("under_voltage")
            await asyncio.sleep(period_s)

    async def _import_boot_network(self, wait_s: float = 120.0) -> dict | None:
        """Raspberry Pi: Wi-Fi settings on the FAT boot partition (``boot_network_conf``,
        ``/boot/firmware/retroboot/network.conf``, editable on any computer) become a
        stored profile, as the installer does with its media's file (netconf.py).  Once
        per file content (state/boot-network.json keeps its SHA-256, never the values).
        Waits up to wait_s for the Wi-Fi adapter (brcmfmac binds while hostd starts)."""
        from . import netconf
        import hashlib
        path = Path(self.cfg.boot_network_conf)
        marker = self.paths.state / "boot-network.json"
        if not self.network.enabled:
            return None
        try:
            net = await asyncio.to_thread(netconf.read_file, path, "boot")
            if net is None:
                return None
            if net.password:
                self.redactor.add(net.password)
            digest = hashlib.sha256(await asyncio.to_thread(path.read_bytes)).hexdigest()
            try:
                if read_json(marker, 4096).get("sha256") == digest:
                    return None          # this file was imported on an earlier start
            except (OSError, ValueError, AttributeError):
                pass
            deadline = mono() + wait_s
            while not await self.network.wifi_device():
                if mono() >= deadline:
                    raise NetworkError("No Wi-Fi hardware", "no_wifi")
                await asyncio.sleep(2.0)
            result = await netconf.import_network(self.network, net)
            from .util import atomic_write_json
            atomic_write_json(marker, {"sha256": digest, "imported": wall(), "connected": result["connected"]},
                              0o600)
            self.lifecycle.event("boot_network", imported=True, connected=result["connected"])
            return result
        except netconf.NetConfError as exc:
            self.lifecycle.event("boot_network_invalid", reason=str(exc))
        except NetworkError as exc:
            self.lifecycle.event("boot_network_failed", code=exc.code)
        except Exception as exc:  # noqa: BLE001 -- never disturb the start over this file
            log.warning("boot partition network settings: %s", type(exc).__name__)
        return None

    async def _detect_wifi(self) -> bool:
        """Wi-Fi hardware present: NetworkManager lists a Wi-Fi device, or the kernel has a
        radio NetworkManager hasn't picked up yet (hostd starts beside NetworkManager)."""
        try:
            return bool(await self.network.wifi_device()) or self.network.wifi_radio_registered()
        except Exception:
            return False

    async def _watch_wifi(self) -> None:
        """A Wi-Fi driver that binds after hostd started: the wifi capability follows, and an
        AirPort bridge that fell back to the RetroBoot network moves to the real Wi-Fi."""
        period, limit = self.wifi_watch_s
        deadline = mono() + limit
        while mono() < deadline:
            await asyncio.sleep(period)
            if await self._detect_wifi():
                self._wifi_available = True
                self.lifecycle.event("wifi_late", after_s=round(mono() - self.started_mono, 1))
                await self.airport.wifi_appeared()
                return

    async def ensure_guest_isolation(self, force: bool = False) -> bool | None:
        """Load the guest isolation table (retroboot/guestnet.py): always with ``force`` (hostd
        start, so a changed port or guest user takes effect), else only when it is missing.
        None when disabled (development) or nft is unavailable; never blocks a launch."""
        if not (self.cfg.guest_isolation and self.cfg.allow_network_tools) or os.geteuid() != 0:
            self.guest_isolation = {"loaded": None, "detail": "not managed here"}
            return None
        from . import guestnet
        try:
            loaded = False if force else await asyncio.to_thread(guestnet.present)
            detail = "loaded"
            if loaded is False:
                ok, detail = await asyncio.to_thread(guestnet.apply, str(self.paths.hostd_json), "nft",
                                                     str(self.paths.hostd_local_json))
                loaded = ok
                self.lifecycle.event("guest_isolation_applied", ok=ok, detail=detail[:200])
        except Exception as exc:                       # never let this stop Mac OS from starting
            loaded, detail = None, f"{type(exc).__name__}: {exc}"[:200]
        self.guest_isolation = {"loaded": loaded, "detail": detail}
        return loaded

    def _new_pairing_code(self, code: str) -> None:
        self.redactor.add(code)
        self.lifecycle.event("pairing_code_issued")

    # -- presenter plumbing --------------------------------------------------------
    async def show_scene(self, scene: str, **kw) -> None:
        if scene == "guest":
            machine = self.supervisor.machine
            kw["classic_guest"] = machine is not None and not machine.osx
        elif scene == "panel":
            # A stopped Mac may leave a different next-start Mac selected. Refresh
            # its neutral backdrop before displaying recovery or missing media.
            await self.startup.push_config()
        self.scene = {"scene": scene, **kw}
        self.lifecycle.event("scene", scene=scene, **kw)
        await self.presenter.try_request("set_scene", scene=scene, **kw)

    def is_pairing_visible(self) -> bool:
        """True when the local panel or installer is currently showing a pairing screen."""
        for attr in ("panel", "installer_panel"):
            p = getattr(self, attr, None)
            if p is None:
                continue
            fn = getattr(p, "is_pairing_visible", None)
            if callable(fn) and fn():
                return True
            if getattr(p, "visible", False) and getattr(getattr(p, "screen", None), "doc_id", None) in {
                    "network-web", "installer-network"}:
                return True
        return False

    def pairing_masked_png(self) -> bytes:
        from .remote.encode import pairing_masked_png
        mode = self.display_mode() or (800, 600)
        return pairing_masked_png(max(480, min(mode[0], 1280)), max(360, min(mode[1], 800)))

    async def composed_screenshot(self, dest: str) -> bool:
        if self.is_pairing_visible():
            out = Path(dest)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(self.pairing_masked_png())
            return True
        r = await self.presenter.try_request("screenshot", path=dest, what="composed", timeout=10)
        return r is not None and Path(dest).exists()

    async def _on_presenter_connect(self, hello: dict) -> None:
        self.lifecycle.event("presenter_connected", version=hello.get("version"), scene=hello.get("scene"),
                             display=(hello.get("display") or {}).get("mode"), connections=self.presenter.connections)
        self.rotation.on_presenter_display(hello.get("display"))
        await self.rotation.sync()
        await self.apply_engine.push_presenter()
        if self.mode == "normal":
            await self.startup.on_presenter_connect()
        if self.scene:
            await self.presenter.try_request("set_scene", **self.scene)
        await self.panel.repush()
        if hello.get("display"):
            # the display as it is now: the pushes above may have turned or re-moded it
            await self._on_display(self.presenter.display or hello["display"])

    async def _on_display(self, display: dict) -> None:
        self.lifecycle.event("presenter_display", mode=display.get("mode"), driver=display.get("driver"),
                             connector=display.get("connector"), scale=display.get("scale") or display.get("suggested_scale"),
                             pixel_scale=display.get("pixel_scale"), rotation=display.get("rotation"))
        # auto rotation follows the tilt sensor only while the built-in display is the main one
        self.rotation.on_presenter_display(display)
        await self.rotation.sync()
        await self.lidsleep.on_display()    # an external display with the lid closed: full speed
        shown = display.get("user_rotation_pending", display.get("user_rotation"))
        if display.get("device") and shown is not None and int(shown) != self.rotation.target():
            await self.apply_engine.push_display()
        self._start_boot()

    def _on_presenter_event(self, msg: dict):
        ev = msg.get("event")
        if ev == "display":
            return self._on_display({k: v for k, v in msg.items() if k != "event"})
        if ev in ("ui_action", "ui_change"):
            return self.panel.on_event(msg)
        if ev == "host_key":
            return self.keys.on_host_key(str(msg.get("key") or ""), bool(msg.get("repeat")))
        if ev == "lid":
            return self.laptop.on_lid(bool(msg.get("open")))
        if self.mode == "installer":
            return None
        return self.supervisor.on_presenter_event(msg)

    def _start_boot(self) -> None:
        # Never once hostd is stopping: a display report arriving during shutdown() would
        # start a launch that nothing ends (shutdown, _finish).
        if self._boot_task is None and not self.stopping:
            self._boot_task = self.loop.create_task(self._boot())

    async def _boot(self) -> None:
        await self._launch_ready.wait()
        if self.mode == "installer":
            await self.panel.start()
            return
        notice = self.store.notice
        if notice:
            done = asyncio.Event()

            async def cont():
                done.set()
            await self.panel.show_settings_notice(notice, cont)
            await done.wait()
            self.panel.forget()
        await self.supervisor.boot()

    async def _presenter_fallback(self) -> None:
        await asyncio.sleep(self.cfg.presenter_fallback_start)
        if self._boot_task is None and self.mode == "normal" and self.cfg.presenter_fallback_start > 0:
            self.lifecycle.event("presenter_missing", waited_s=self.cfg.presenter_fallback_start)
            self._start_boot()

    # -- capabilities / validation ---------------------------------------------------
    def host_modes(self) -> list[str]:
        d = self.presenter.display or {}
        out = []
        for m in d.get("modes") or []:
            try:
                s = schema.format_host_mode(m)
            except (TypeError, ValueError):
                continue
            if s not in out:
                out.append(s)
        return out

    async def wait_for_sound(self, sys_root: Path = audio.SYSFS, poll_s: float = 0.1) -> float:
        """Before QEMU's sound device is chosen: wait while an HD Audio card is still
        being set up (audio.pending_sound) or alsactl is still initializing the cards
        (their output stages are set after it, audio.hda_output_stage), at most
        SOUND_WAIT_S over the whole hostd run and only in the first
        SOUND_WAIT_UNTIL_UPTIME_S after the kernel started, so a card whose driver
        never finishes costs one boot a few seconds and no later launch anything.
        Returns the seconds waited."""
        if not self.cfg.allow_network_tools:
            return 0.0
        start = time.monotonic()        # CLOCK_MONOTONIC: seconds since the kernel started
        spent = getattr(self, "_sound_wait_spent", 0.0)
        limit = min(audio.SOUND_WAIT_S - spent, audio.SOUND_WAIT_UNTIL_UPTIME_S - start)
        if limit <= 0:
            return 0.0
        pending = await asyncio.to_thread(audio.sound_setup_pending, sys_root)
        if not pending:
            return 0.0
        while pending and time.monotonic() - start < limit:
            await asyncio.sleep(poll_s)
            pending = await asyncio.to_thread(audio.sound_setup_pending, sys_root)
        waited = round(time.monotonic() - start, 2)
        self._sound_wait_spent = spent + waited    # one budget per hostd run, not per launch
        self.lifecycle.event("audio_wait", waited_s=waited, pending=pending)
        return waited

    async def choose_audio_output_when_ready(self) -> dict | None:
        await self.wait_for_sound()
        return await asyncio.to_thread(self.choose_audio_output)

    def _hook_chime_lifecycle(self) -> None:
        orig_event = self.lifecycle.event

        def hooked(_event: str, /, **fields):
            rec = orig_event(_event, **fields)
            if _event == "guest_start" and self.mode == "normal":
                if not getattr(self.supervisor, "chime_played", False):
                    mid = fields.get("machine")
                    m = self.machines.get(mid) if mid else self.supervisor.active_machine()
                    loop = self.loop
                    if loop is not None and not loop.is_closed():
                        loop.create_task(self.play_startup_chime(m))
            elif _event == "corrupt_disk_recovery":
                self.on_corrupt_disk_recovery(dict(fields))
            return rec

        self.lifecycle.event = hooked

    def _on_printed_pdf(self, info: dict) -> None:
        self._printed_pdf_seq += 1
        rec = {**info, "seq": self._printed_pdf_seq, "timestamp": wall()}
        self.last_printed_pdf = rec
        self.lifecycle.event("printed_pdf", **rec)

    def choose_audio_output(self, output_override: str | None = None) -> dict | None:
        """The host playback device for the guest (audio.py); None: ALSA default."""
        if not self.cfg.allow_network_tools:
            return None
        if self.cfg.audio_device:
            try:
                return audio.configured(self.cfg.audio_device)      # the device's own choice (hostd.json)
            except ValueError:
                log.warning("audio_device in the hostd configuration is not a usable ALSA name; ignored")
        selected = output_override if output_override is not None else self.settings.applied.get("audio.output", "auto")
        # A laptop showing Mac OS on its built-in screen plays through its own
        # speakers; HDMI audio wins only on desktops or with an external main display.
        display = self.presenter.display or {}
        outputs = display.get("outputs") or []
        laptop_main = (any(o.get("internal") for o in outputs)
                       and self.settings.applied.get("display.primary", "internal") == "internal")
        supports_out = "output" in inspect.signature(audio.choose).parameters
        extra = {"output": selected} if supports_out else {}
        try:
            if self.platform.is_gemini:
                # A USB sound card when one is plugged in, else the codec (speakers and jack;
                # the image routes it and switches the speaker amplifiers).
                out = audio.choose(prefer="gemini", **extra)
            elif self.platform.is_raspberry_pi:
                # The HDMI port Mac OS is shown on (primary first), then USB, then the Pi 4's jack.
                driving = [o["connector"] for o in sorted(outputs, key=lambda o: o.get("role") != "primary")
                           if o.get("connector")] if outputs else None
                out = audio.choose(prefer="pi", driving=driving, **extra)
                if out and out.get("vc4"):
                    out = dict(out, softvol=audio.vc4_softvol(out))   # the volume control, before the probe
            else:
                out = audio.choose(prefer="analog" if laptop_main else "hdmi", **extra)
                if out and out.get("asoc"):
                    ucm = audio.ucm_prepare(out)        # ASoC routing (once per card), before QEMU opens it
                    if ucm:
                        out = dict(out, ucm_device=ucm["device"], mixer=ucm["mixer"], warmed=ucm.get("warmed"))
                elif out:
                    stage = audio.hda_output_stage(out)     # HD Audio speakers/headphones or IEC958 on, each launch
                    if stage is not None:
                        out = dict(out, stage=stage)
            if out and out.get("needs_softvol") and hasattr(audio, "ensure_softvol"):
                sv = audio.ensure_softvol(out)
                out = dict(out, softvol=sv, mixer=audio.SOFTVOL_CONTROL if sv else out.get("mixer"))
            return out
        except Exception as exc:  # never block a launch on sound
            log.warning("audio output selection failed: %s", exc)
            return None

    def list_audio_outputs(self) -> list[dict]:
        active = self.settings.applied.get("audio.output", "auto")
        fn = getattr(audio, "list_outputs", None)
        if callable(fn):
            display = self.presenter.display or {}
            outputs = display.get("outputs") or []
            driving = [o["connector"] for o in outputs if o.get("connector")] if outputs else None
            try:
                return list(fn(driving=driving, active_output=active))
            except TypeError:
                return list(fn())
            except Exception as exc:
                log.debug("list_outputs failed: %s", exc)
        return [{"id": "auto", "kind": "auto", "label": "Automatic", "card_id": None,
                 "device": None, "alsa": None, "available": True, "active": active == "auto"}]

    async def sync_audio_output(self, output: str | None = None) -> bool:
        out = await asyncio.to_thread(self.choose_audio_output, output)
        pw_sink = (out or {}).get("pw_sink")
        kw = {"pw_sink": pw_sink} if pw_sink else {}
        await self.mixer.probe(out["card_id"] if out else None, (out or {}).get("mixer"),
                               detect_switch=True, **kw)
        if self.mixer.available:
            await self.apply_engine.push_volume()
            return True
        return bool(out)

    def _is_audio_muted(self) -> bool:
        vol = self.settings.applied.get("audio.volume", 70)
        return bool(getattr(self.laptop, "muted", False) or getattr(self.mixer, "muted", False) or int(vol if vol is not None else 70) <= 0)

    async def play_startup_chime(self, machine=None) -> bool:
        chime = self.settings.applied.get("audio.startup_chime", "auto")
        muted = self._is_audio_muted()
        if chime == "off" or muted:
            return False
        m = machine or self.supervisor.active_machine()
        sup_fn = getattr(self.supervisor, "play_startup_chime", None)
        if callable(sup_fn):
            res = sup_fn(m)
            return bool(await res if inspect.isawaitable(res) else res)
        fn = getattr(audio, "play_startup_chime", None)
        if not callable(fn):
            return False
        family = getattr(m, "family", POWERMAC) if m is not None else POWERMAC
        out = await asyncio.to_thread(self.choose_audio_output)
        return bool(await asyncio.to_thread(
            fn, family=family, chime_setting=chime, output=self.settings.applied.get("audio.output", "auto"),
            audio_out=out, volume=self.settings.applied.get("audio.volume", 70), muted=muted))

    async def test_sound(self, output: str | None = None) -> dict:
        target = output if output is not None else self.settings.desired.get("audio.output", "auto")
        muted = self._is_audio_muted()
        sup_fn = getattr(self.supervisor, "test_sound", None)
        if callable(sup_fn):
            res = sup_fn(target)
            out = await res if inspect.isawaitable(res) else res
        elif callable(getattr(audio, "test_sound", None)):
            chosen = await asyncio.to_thread(self.choose_audio_output, target)
            m = self.supervisor.active_machine()
            out = await asyncio.to_thread(
                audio.test_sound, target, audio_out=chosen,
                chime_setting=self.settings.applied.get("audio.startup_chime", "auto"),
                family=getattr(m, "family", POWERMAC) if m is not None else POWERMAC,
                volume=self.settings.applied.get("audio.volume", 70), muted=muted)
        else:
            out = {"ok": False, "output": target, "device": None, "player": None,
                   "error": "Audio test is not available."}
        self.lifecycle.event("audio_test", output=target, ok=bool(out.get("ok")), error=out.get("error"))
        return dict(out)

    def audio_capable(self) -> bool:
        snd = Path("/dev/snd")
        try:
            return snd.is_dir() and any(p.name.startswith("pcmC") for p in snd.iterdir())
        except OSError:
            return False

    def capabilities(self) -> dict:
        return {"display_modes": bool(self.host_modes()), "presenter": self.presenter.connected.is_set(),
                "audio": self.mixer.available, "audio_output": self.audio_capable(), "share": True,
                "wifi": bool(self._wifi_available), "guest_prep": self.guestprep.available()[0],
                "emulator": (self.paths.qemu_package / "bin/qemu-system-ppc").exists(),
                "emulator_68k": quadra.package_present(self.paths),
                "web": True, "checkpoints": True, "airport": self.airport.capable(),
                "ati_radeon": self.graphics.capable(),
                "battery": self.battery.capable(),
                "bluetooth": self.bluetooth.capable(),
                "mac_os_keys": self.keys.capable(),
                "web_proxy": self.webproxy.available(),
                "media_install": self.media.install_available()[0], "remote_screen": True,
                "internal_display": any(o.get("internal") for o in
                                        (self.presenter.display or {}).get("outputs") or []),
                # touchscreens and pens (presenter "input"), the tilt sensor (orientation.py)
                "touch": bool((self.presenter.input or {}).get("touchscreens") or
                              (self.presenter.input or {}).get("pens")),
                "accelerometer": self.rotation.capable,
                "speaker_switch": self.speakers.enabled}

    def display_mode(self) -> tuple[int, int] | None:
        """The presenter's current mode (pixel scales are offered for it)."""
        mode = (self.presenter.display or {}).get("mode")
        try:
            return (int(mode[0]), int(mode[1])) if mode else None
        except (TypeError, ValueError, IndexError):
            return None

    def validation_context(self) -> schema.ValidationContext:
        machines = self.machines.list()
        return schema.ValidationContext(machines={m.id for m in machines},
                                        machine_names={m.id: m.name for m in machines},
                                        machine_families={m.id: m.family for m in machines},
                                        quadra_ram_max_mb=128 if self.platform.is_zero2w else None,
                                        machine_guest_os={m.id: m.guest_os for m in machines},
                                        host_modes=self.host_modes() if self.presenter.display else None,
                                        capabilities=self.capabilities(),
                                        media_cds=self.media.cd_choices(),
                                        display_mode=self.display_mode(),
                                        values=dict(self.settings.desired),
                                        graphics=self.graphics.ui_context(),
                                        audio_outputs={o["id"]: o["label"] for o in self.list_audio_outputs()})

    BOOT_LABELS = {"guest_start": "Mac OS launched", "qmp_connected": "Emulator ready",
                   "guest_content": "Mac OS startup screen", "guest_revealed": "Shown on the display",
                   "boot_handoff": "Real-time clock restored", "finder_detected": "Finder ready",
                   "osx_finder_visible": "Finder visible",
                   "mac_os_started": "Mac OS started",
                   "guest_paused": "Paused at startup options", "guest_resumed": "Resumed",
                   "disk_repair": "Disk checked", "guest_prep": "Disk prepared",
                   "clean_request": "Shut down requested", "power_key_sent": "Power key sent",
                   "guest_exited": "Mac OS stopped"}

    def notices(self) -> list[dict]:
        out = []
        n = self.store.notice
        if n:
            out.append({"id": n["code"], "level": "warning", "message": n["message"], "at": n.get("at")})
        sup = self.supervisor
        if sup.web_state() == "failed" and sup.failure:
            out.append({"id": "guest-failed", "level": "error",
                        "message": f"{sup.failure.get('message')} Evidence {sup.failure.get('evidence')}."})
        m = sup.active_machine()
        prep = (m.meta.get("prep") or {}) if m else {}
        if prep.get("result") == "failed":
            out.append({"id": "guest-prep-failed", "level": "warning",
                        "message": "RetroBoot could not prepare this Mac's disk, so Mac OS may check its disk "
                                   "after an unexpected stop."})
        if self.guest_isolation.get("loaded") is False:
            out.append({"id": "guest-isolation-missing", "level": "warning",
                        "message": "Mac OS can reach this computer's own services, because RetroBoot could not "
                                   "load its network rules. Restart RetroBoot; if this stays, capture diagnostics."})
        if sup.responsive is False and sup.state in ("running", "stopping"):
            if sup.unresponsive_reason == "display":
                msg = ("Mac OS is not responding: the screen has not changed and the pointer does not move. "
                       "Use Force Restart if it stays frozen.")
            else:
                msg = "The emulator is not responding. Use Force Restart if Mac OS stays frozen."
            out.append({"id": "guest-unresponsive", "level": "warning", "message": msg,
                        "evidence": sup.unresponsive_evidence})
        rec = getattr(self.supervisor, "corrupt_disk_recovery", None) or self.corrupt_disk_recovery
        if rec:
            out.append({"id": "corrupt-disk-recovery", "level": "error",
                        "message": "Mac OS's disk could not be repaired automatically. Restore a checkpoint "
                                   "or reset to Fresh Install before starting.",
                        "recovery": rec})
        out += self.graphics.notices()
        out += self.updates.notices()
        return out

    def computer_rows(self) -> dict:
        """Diagnostics rows naming this computer (web Versions block; the panel's
        Diagnostics shows the same): the board or DMI model and, on a Raspberry Pi,
        the firmware's power-supply and temperature throttling flags."""
        rows = {}
        if self.platform.model:
            ram = self.platform.ram_mb
            # Whole GB, rounded up: /proc/meminfo (without a revision code) shows a little less.
            sized = self.platform.is_raspberry_pi or self.platform.is_gemini
            size = (f", {ram} MB" if ram < 1024 else f", {-(-ram // 1024)} GB") if sized and ram else ""
            rows["Computer"] = self.platform.model + size
        if self.platform.is_zero2w:
            rows["Memory"] = "512 MB: 68040 Macs use 64 MB by default, with a maximum of 128 MB."
        elif self.platform.small_memory:
            rows["Memory"] = ("1 GB or less: RetroBoot is untested on this Raspberry Pi and Mac OS may run out "
                              "of memory. Use a Raspberry Pi with 2 GB or more.")
        if self.health is not None:
            t = self.health.sample()["text"]
            if t:
                rows["Power and temperature"] = t
        return rows

    async def status(self) -> dict:
        from . import display as dpolicy
        sup = self.supervisor
        hn = self.settings.applied["management.hostname"]
        records = await self.network.address_records()
        primary = await self.network.primary_address()
        mdns = self.mdns.display_name(hn)
        urls = [f"https://{mdns}/"] + [self.web.url_for(r["address"]) for r in records if r["family"] == "ipv4"]
        guest = sup.public()
        m = sup.machine or sup.active_machine()
        applied = self.settings.applied
        guest.update(machine=guest.get("machine") or (m.id if m else None), machine_name=m.name if m else None,
                     system=m.meta.get("os_version") if m else None, content=sup.content_seen,
                     family=m.family if m else None,
                     cpu=m.cpu_label(applied.get("machine.cpu")) if m else None)
        timeline = sup.timeline()
        boot = {"started_at": sup.started_wall,
                "events": [{"id": e["event"].replace("_", "-"), "label": self.BOOT_LABELS.get(e["event"], e["event"]),
                            "t_ms": int(e["t"] * 1000)} for e in timeline if e["t"] >= 0]}
        disp = dict(self.presenter.display or {})
        disp["pixel_scale"] = applied["display.pixel_scale"]
        if disp.get("mode"):
            w, h = int(disp["mode"][0]), int(disp["mode"][1])
            # The presenter reports the scale in effect; compute it for older presenters.
            disp["scale"] = disp.get("scale") or dpolicy.effective_scale(w, h, applied["display.pixel_scale"])
            disp["guest_size"] = list(dpolicy.guest_geometry(disp, applied["display.guest_size"],
                                                             applied["display.pixel_scale"]))
            # With the ATI Radeon Mac OS picks one of ATI's sizes (graphics.py): report the
            # screen Mac OS actually shows while it runs.
            surface = getattr(sup, "guest_surface", None)
            if sup.active and self.graphics.running == "ati-radeon" and surface:
                disp["guest_size"] = list(surface)
            # Capability: the pixel sizes this display offers and the resulting Mac screens.
            disp["pixel_scales"] = dpolicy.pixel_scale_choices(w, h)
            note = schema.fixed_size_note(applied, (w, h))
            if note:
                disp["note"] = note
        disp["scaling"] = applied["display.scaling"]
        pkg = self.package.summary()
        qhash = ((pkg.get("files") or {}).get("bin/qemu-system-ppc") or "")
        m68k = self.package_m68k.summary() if quadra.package_present(self.paths) else {"present": False}
        caps = self.capabilities()
        return {
            "guest": guest,
            "boot": boot,
            "startup": self.startup.public(),
            "host": {"name": "RetroBoot", "hostname": hn, "mdns": mdns, "version": __version__, "mode": self.mode,
                     "dev": self.cfg.dev, "uptime_s": int(mono() - self.started_mono), "boot_id": self.lifecycle.boot_id,
                     "time": wall(), "urls": urls, "addresses": records, "primary_address": primary,
                     "fingerprint": self.web.fingerprint, "form_factor": self.hardware["form_factor"],
                     "model": self.platform.model, "platform": self.platform.public(),
                     **({"throttled": self.health.sample()} if self.health is not None else {}),
                     **({"cpu_placement": self.cpuplace.public()} if self.cpuplace.plan.enabled else {}),
                     **({"speakers": self.speakers.public()} if self.speakers.enabled else {})},
            "presenter": {"connected": self.presenter.connected.is_set(),
                          "version": (self.presenter.hello or {}).get("version"), "scene": self.scene.get("scene")},
            "display": disp,
            "addresses": [r["address"] for r in records],
            "web": {"url": self.web.url_for(primary), "fingerprint_sha256": self.web.fingerprint,
                    "enabled": self.settings.applied["management.web"] == "on"},
            "capabilities": {"wifi": caps["wifi"], "audio": caps["audio"], "share": caps["share"],
                             **{k: v for k, v in caps.items() if k not in ("wifi", "audio", "share")}},
            # Product wording: no Linux names here (the diagnostics bundle has the kernel).
            "versions": {"RetroBoot service": __version__,
                         "Display service": str((self.presenter.hello or {}).get("version") or "not connected"),
                         "Emulator": f"QEMU {pkg.get('qemu') or 'not installed'}",
                         "Package manifest": f"sha256:{qhash[:16]}" if qhash else "missing",
                         **({"68k emulator": f"QEMU {m68k.get('qemu') or 'unknown'}"} if m68k.get("present") else {}),
                         "Settings schema": str(schema.SCHEMA_VERSION),
                         **self.computer_rows()},
            "machines": {"count": len(self.machines.list()), "active": self.settings.applied.get("machine.active")},
            "emulator": {k: v for k, v in pkg.items() if k != "files"},
            "emulator_68k": {k: v for k, v in m68k.items() if k not in ("files", "ndrv", "classicvirtio")},
            "settings": {"revision": self.settings.revision, "pending": self.settings.pending_keys(),
                         "pending_confirm": self.settings.pending_confirm_public()},
            "pending": self.settings.pending_keys(),
            "settings_revision": self.settings.revision,
            "jobs": [j.public() for j in self.jobs.active()],
            "notices": self.notices(),
            "evidence": {"last": self.diagnostics.last_evidence},
            "airport": self.airport.public(),
            "graphics": self.graphics.public(),
            "battery_passthrough": self.battery.public(),
            "keys": self.keys.public(),
            "web_proxy": self.webproxy.public(),
            "guest_isolation": dict(self.guest_isolation),
            "power": self.laptop.public(),
            "bluetooth": self.bluetooth.public(),
            "remote": self.remote.public(),
            "update": self.updates.public(),
            "rotation": self.rotation.public(),
            "printer": self.printer.public(),
            "printed_pdf": self.last_printed_pdf,
            "corrupt_disk_recovery": getattr(self.supervisor, "corrupt_disk_recovery", None) or self.corrupt_disk_recovery,
            **({"pi_display": self.rpi_display.public()} if self.rpi_display.enabled else {}),
            "input": {k: v for k, v in (self.presenter.input or {}).items()
                      if k in ("touchscreens", "pens", "touch_devices", "touch_gestures")},
        }

    async def panel_paired(self, name: str) -> None:
        if self.panel.visible:
            await self.panel.refresh()

    # -- jobs shared by panel and web -------------------------------------------------
    def lifecycle_job(self, kind: str, poweroff: bool | None = None, origin: str = "web") -> Job:
        sup = self.supervisor
        if kind == "start":
            body = sup.job_start
            job = self.jobs.start("guest.start", body, resources=("guest",), preemptible=True)
        elif kind in ("restart", "shutdown"):
            po = bool(poweroff) if kind == "shutdown" else False
            job = self.jobs.start(f"guest.{kind}", lambda j: sup.job_clean(j, kind, po), resources=("guest",),
                                  params={"power_off": po}, preemptible=True)
        elif kind in ("force-stop", "force-restart"):
            k = "restart" if kind == "force-restart" else "stop"
            po = bool(poweroff) if kind == "force-stop" else False
            job = self.jobs.start(f"guest.{kind}", lambda j: sup.job_force(j, k, po, origin), resources=("guest",),
                                  params={"power_off": po},
                                  preempts=("guest.start", "guest.restart", "guest.shutdown"))
        else:
            raise ValueError(kind)
        self.lifecycle.event("lifecycle_request", kind=kind, power_off=poweroff, origin=origin, job=job.id)
        return job

    def host_job(self, action: str, force: bool = False, origin: str = "web") -> Job:
        """POST /api/v1/host/{poweroff,reboot}: clean guest shutdown first; force only when asked."""
        async def body(job: Job):
            sup = self.supervisor
            if sup.active or sup.launch_pending:
                if force and sup.active:
                    await sup.job_force(job, "stop", False, origin)
                else:
                    await sup.job_clean(job, "shutdown", poweroff=False, purpose=f"host-{action}",
                                        resume=(lambda: self.host_job("reboot", origin=origin))
                                        if action == "reboot" else None)
            job.update(0.95, "Powering off RetroBoot…" if action == "poweroff" else "Restarting RetroBoot…")
            self.lifecycle.event("host_power_request", action=action, force=force, origin=origin, job=job.id)
            if action == "poweroff":
                await sup.poweroff(f"{origin} request")
            else:
                await sup.reboot_host(f"{origin} request")
            return {"action": action, "forced_guest_stop": bool(force),
                    "dev_hook": not bool(self.cfg.poweroff_cmd if action == "poweroff" else self.cfg.reboot_cmd)}
        job = self.jobs.start(f"host.{action}", body, resources=("guest", "host"), preemptible=not force,
                              preempts=("guest.start", "guest.restart", "guest.shutdown", "host.poweroff",
                                        "host.reboot") if force else ())
        self.lifecycle.event("lifecycle_request", kind=f"host-{action}", force=force, origin=origin, job=job.id)
        return job

    def apply_job(self, revision: int, restart: bool, origin: str = "web") -> Job:
        needs_guest = restart and self.supervisor.active
        return self.jobs.start("settings.apply",
                               lambda j: self.apply_engine.apply(revision, restart, j, origin),
                               resources=("guest", "settings") if needs_guest else ("settings",))

    def select_machine_job(self, machine_id: str, origin: str = "panel") -> Job:
        async def body(job: Job):
            rev = self.settings.patch(self.settings.revision, {"machine.active": machine_id}, self.validation_context())
            res = await self.apply_engine.apply(rev, False, job, origin)
            if res["pending_restart"]:
                job.message = "The new Mac starts the next time Mac OS restarts."
            return res
        return self.jobs.start("machines.select", body, resources=("settings",))

    def blank_kinds(self) -> list[dict]:
        """New Blank Disk's kinds of Mac (panel and web): each with its disk sizes and whether
        it can be made here (a 68k Mac needs the 68k emulator and a Quadra 800 ROM)."""
        out = []
        from .machines import GUEST_OS_CHOICES
        from .osx import package_capable as osx_capable
        for family in FAMILIES:
            why = self.platform.machine_unavailable(family)
            if why is None and family == QUADRA800:
                if not quadra.package_present(self.paths):
                    why = "This RetroBoot doesn't have the 68k emulator yet."
                elif not self.roms.available(QUADRA800):
                    why = ("A 68k Mac needs a Quadra 800 ROM. Add the ROM on the web page (Storage & Media), "
                           "or install a 68k Mac that comes with its ROM.")
            sizes = list(blank_sizes_gb(family))
            out.append({"family": family, "label": FAMILY_CHOICES[family], "available": why is None, "reason": why,
                        "sizes_gb": sizes, "default_gb": QUADRA_BLANK_DEFAULT_GB if family == QUADRA800 else 8,
                        "note": QUADRA_BLANK_NOTE if family == QUADRA800 else None})
            if family == POWERMAC and osx_capable(self.paths.qemu_osx_package):
                out[-1]["label"] = "PowerPC Mac"
                out[-1]["guest_os_choices"] = [{"value": k, "label": v} for k, v in GUEST_OS_CHOICES.items()]
        return out

    def create_blank_machine(self, name: str, size_gb: int, origin: str = "panel", family: str = POWERMAC,
                             guest_os: str = "classic") -> dict:
        """New Blank Disk: a Mac with an empty disk, for Mac OS installed from a CD.  Quick
        (a sparse file), so no job.  Raises MachineError with a sentence for the user.  A 68k
        Mac (``family`` quadra800) gets a copy of the ROM store's Quadra 800 ROM."""
        if family not in FAMILIES:
            raise MachineError("Choose the kind of Mac.")
        from .machines import CLASSIC, OSX_PROFILES, OSX_TEMPLATE_FAMILY, guest_os_of_template
        from . import osx
        tag = OSX_TEMPLATE_FAMILY if isinstance(guest_os, str) and guest_os in OSX_PROFILES else \
            "powerMacG4" if family == POWERMAC else family
        if family != POWERMAC and guest_os != CLASSIC:
            raise MachineError("Mac OS X needs a PowerPC Mac.")
        profile = guest_os_of_template({"machineFamily": tag,
                                        "guestOS": guest_os})
        if profile != CLASSIC and not osx.package_capable(self.paths.qemu_osx_package):
            raise MachineError(osx.NO_PACKAGE)
        kind = next(k for k in self.blank_kinds() if k["family"] == family)
        if not kind["available"]:
            raise MachineError(kind["reason"])
        if type(size_gb) is not int or size_gb not in blank_sizes_gb(family):
            raise MachineError("Choose one of the disk sizes.")
        free = shutil.disk_usage(self.paths.machines if self.paths.machines.exists() else self.paths.root).free
        if free < 2 * 1024 ** 3:
            raise MachineError("There isn't enough free space on this computer for a new Mac. "
                               "Delete a Mac you don't use, then try again.")
        rom = self.roms.find(QUADRA800) if family == QUADRA800 else None
        m = self.machines.create_blank(name, size_gb * 1024 ** 3, family=family, rom=rom, guest_os=profile)
        if family == QUADRA800:
            self.lifecycle.event("machine_created", machine=m.id, kind="blank", size_gb=size_gb, origin=origin,
                                 family=family)
        else:
            self.lifecycle.event("machine_created", machine=m.id, kind="blank", size_gb=size_gb, origin=origin)
        return m.public()

    def add_rom_bytes(self, data: bytes, origin: str = "web") -> dict:
        """Keep a Quadra 800 ROM (web upload) in the ROM store, checked by its SHA-256 (never
        its bytes in a log).  Raises quadra.RomError with a sentence for the user, OSError when
        it can't be written."""
        info = self.roms.add_bytes(data)
        self.lifecycle.event("rom_added", sha256=info["sha256"][:16], family=info["family"], added=info["added"],
                             origin=origin)
        return info

    def cd_start_job(self, machine_id: str, source: str | None = None, origin: str = "panel") -> Job:
        """Start Up from CD: ``machine_id`` becomes the startup Mac (if it isn't) and Mac OS
        starts up from the CD once, restarting first if it runs.  ``source`` is a CD source
        id (Supervisor.cd_sources); None: the disc in this computer's CD-ROM drive.  A blank
        disk keeps the CD until Mac OS is installed on it (machine.json ``cd_start``)."""
        async def body(job: Job):
            m = self.machines.get(machine_id)
            if m is None:
                raise JobFailed("That Mac isn't installed on this RetroBoot.", None, "not_found")
            sup = self.supervisor
            drives = await asyncio.to_thread(self.optical.list, True)
            cd = sup.resolve_cd(source or "drive:", drives)
            if cd is None:
                raise JobFailed("There's no CD to start up from. Put a Mac OS CD in this computer's CD-ROM drive, "
                                "or add a CD image.", None, "no_cd")
            if await asyncio.to_thread(m.needs_cd):
                m.update_meta(cd_start=cd["id"])
            if self.settings.desired.get("machine.active") != machine_id:
                # Applied just before Mac OS starts (apply_pending_restart), as Use This Mac's.
                self.settings.patch(self.settings.revision, {"machine.active": machine_id},
                                    self.validation_context())
            sup.request_cd_start(machine_id, cd["id"])
            self.lifecycle.event("cd_start_request", machine=machine_id, cd=cd["id"], kind=cd["kind"],
                                 origin=origin, job=job.id)
            if sup.active:
                job.update(0.1, "Restarting Mac OS from the CD…")
                res = await sup.job_clean(job, "restart", poweroff=False)
            else:
                res = await sup.job_start(job)
            return {"machine": machine_id, "cd": {k: cd[k] for k in ("id", "label", "kind")}, "result": res}
        return self.jobs.start("guest.cd-start", body, resources=("guest", "settings"),
                               params={"machine": machine_id})

    def import_job(self, spec: dict, origin: str = "web") -> Job:
        async def body(job: Job):
            def progress(f, m):
                job.update(f, m)
            cleanup: Path | None = None
            try:
                if spec["source"] == "catalog":
                    found = await self.catalogs.find(spec["id"], spec.get("catalog"))
                    if found is None:
                        raise JobFailed("That machine is no longer in the catalog.", None, "not_found")
                    which, client, entry = found
                    from .catalog import UNRUNNABLE
                    from .catalogs import entry_family, runnable_families
                    family = entry_family(entry)
                    if family not in runnable_families(self.paths, self.platform):
                        # Only listed where it can start; a direct API request is refused the same way.
                        raise JobFailed(UNRUNNABLE.get(family, "This kind of Mac can't start on this RetroBoot."),
                                        None, "unsupported")
                    job.update(0.0, "Downloading…")
                    self._check_import_room(client, entry)
                    job.cancellable = True       # a stopped download resumes from its .part file
                    try:
                        archive = await client.download(entry, lambda f, m: job.update(f * 0.5 if f else None, m))
                    finally:
                        job.cancellable = False
                    limits = ExtractLimits(entry["installedBytes"], entry.get("requiredStorageBytes"))
                    name, osv = spec.get("name") or entry["name"], entry["osVersion"]
                    dl = download_of(entry)
                    source = {"type": "catalog", "template_id": entry["id"], "archive_sha256": dl["sha256"],
                              "archive_format": dl["format"], "catalog_url": client.url, "catalog": which}
                    scale = (0.5, 0.45)
                elif spec["source"] == "archive":
                    a = self.archives.resolve(spec["id"]) or (await self.archives.list() and self.archives.resolve(spec["id"]))
                    if not a:
                        raise JobFailed("That archive is no longer available.", None, "not_found")
                    archive = Path(a["_path"])
                    if a.get("sha256"):
                        job.update(0.0, "Checking the archive…")
                        from .catalog import _sha256
                        if await asyncio.to_thread(_sha256, archive) != a["sha256"]:
                            raise JobFailed("The archive did not pass its integrity check.", None, "integrity")
                    limits = ExtractLimits(None, None)
                    name, osv = spec.get("name") or Path(a["name"]).name.split(".tar")[0], None
                    source = {"type": a["origin"].split(":")[0], "archive_name": a["name"],
                              "archive_sha256": a.get("sha256")}
                    scale = (0.05, 0.9)
                elif spec["source"] == "upload":
                    archive = cleanup = Path(spec["path"])
                    limits = ExtractLimits(None, None)
                    name, osv = spec.get("name") or "Uploaded Mac", None
                    source = {"type": "upload"}
                    scale = (0.05, 0.9)
                else:
                    raise JobFailed("Unknown import source.", None, "invalid")
                from .catalogs import entry_family, runnable_families
                # A second "Mac OS 7.5.5" is "Mac OS 7.5.5 2" (as New Blank Disk's "Untitled Mac 2"),
                # so Startup Disk never lists two Macs by the same name.
                names = {m.name for m in self.machines.list()}
                if name in names:
                    n = 2
                    while f"{name} {n}" in names:
                        n += 1
                    name = f"{name} {n}"
                mid = await install_machine(archive, self.machines, name, osv, source, limits,
                                            lambda f, m: job.update(scale[0] + (f or 0) * scale[1], m),
                                            allowed_families=runnable_families(self.paths, self.platform),
                                            expected_family=entry_family(entry) if spec["source"] == "catalog"
                                            else None,
                                            expected_guest_os=entry.get("guestOS") if spec["source"] == "catalog" else None,
                                            os_build=entry.get("osBuild") if spec["source"] == "catalog" else None)
                job.update(0.96, "Preparing Mac OS…")
                m = self.machines.get(mid)
                if m is None:
                    raise JobFailed("The imported Mac's settings could not be read.", None, "invalid_machine")
                if m.quadra:
                    # Its ROM goes to the ROM store (for New Blank Disk); one without a ROM gets the store's.
                    rom = await asyncio.to_thread(self.adopt_rom, m)
                    if rom:
                        self.lifecycle.event("rom_" + rom, machine=mid)
                res = await self.supervisor.run_prep(m, "import")
                rec_settings = (
                    (entry.get("recommendedSettings") if spec["source"] == "catalog" and isinstance(entry, dict) else None)
                    or ((m.meta.get("template") or {}).get("recommendedSettings")
                        if isinstance(m.meta.get("template"), dict) else None)
                )
                try:
                    ui_gfx = self.graphics.ui_context() or {}
                    gl_avail = bool((ui_gfx.get("by_machine") or {}).get(m.id, ui_gfx).get("available"))
                except Exception:
                    gl_avail = bool(self.graphics.capable())
                seeded = default_machine_settings(
                    m, recommended_settings=rec_settings, host_ram_mb=self.platform.ram_mb,
                    gl_available=gl_avail,
                    quadra_max_mb=128 if self.platform.is_zero2w else None)
                if seeded or rec_settings:
                    m.update_meta(
                        **({"settings": seeded} if seeded else {}),
                        **({"recommended_settings": dict(rec_settings)} if rec_settings else {}))
                if m.osx and not os.path.lexists(m.nvram):
                    try:
                        from . import osx as osx_mod
                        await asyncio.to_thread(osx_mod.prepare, m, self.paths.qemu_osx_package)
                    except Exception as exc:
                        log.debug("deferring OS X NVRAM seed before Fresh Install checkpoint: %s", exc)
                if m.state().get("clean", True):
                    try:
                        pkg_sum = self.package_for(m).summary().get("files")
                    except Exception:
                        pkg_sum = None
                    try:
                        cp = await self.checkpoints.create(
                            m, "Fresh Install", dict(self.settings.applied), self.settings.revision,
                            pkg_sum, kind="fresh-install")
                        self.lifecycle.event("checkpoint_created", machine=mid, checkpoint=cp["id"],
                                             label=cp["label"], method=cp["method"], kind="fresh-install",
                                             origin=origin, automatic=True)
                    except Exception as exc:
                        log.warning("automatic Fresh Install checkpoint failed for %s: %s", mid, exc)
                if not self.settings.applied.get("machine.active") or \
                        self.machines.get(self.settings.applied.get("machine.active")) is None:
                    first_patch: dict = {"machine.active": mid}
                    if seeded.get("osx_ram_mb") is not None:
                        first_patch["machine.osx_ram_mb"] = seeded["osx_ram_mb"]
                    elif seeded.get("ram_mb") is not None:
                        first_patch["machine.quadra_ram_mb" if m.quadra else "machine.ram_mb"] = seeded["ram_mb"]
                    if seeded.get("graphics") == "ati-radeon" and gl_avail:
                        first_patch["display.graphics"] = "ati-radeon"
                    elif seeded.get("graphics") in ("vga", "standard"):
                        first_patch["display.graphics"] = "standard"
                    self.settings.set_desired_and_applied(first_patch, self.validation_context())
                    await self.apply_engine.apply_restart_keys(list(first_patch))
                self.lifecycle.event("machine_imported", machine=mid, kind=source.get("type"), origin=origin)
                job.update(1.0, f"{m.name} is installed.")
                result = {"machine": mid, "prep": res.get("result")}
                if res.get("result") == "failed":
                    result["prep_error"] = res.get("error")
                    job.message = (f"{m.name} is installed, but RetroBoot could not prepare its disk. "
                                   "Mac OS may check its disk after an unexpected stop.")
                return result
            except ImportError_ as e:
                raise JobFailed(e.message, None, e.code)
            finally:
                if cleanup is not None:
                    cleanup.unlink(missing_ok=True)
                if spec["source"] == "archive":
                    await self.archives.release()
        params = {"source": spec["source"]}
        if spec["source"] == "catalog":
            params.update(id=spec["id"], catalog=spec.get("catalog"))
        return self.jobs.start("machines.import", body, resources=("machines",), params=params)

    def _check_import_room(self, client, entry: dict) -> None:
        """Refuse a catalog download before it starts when the archive and the Mac it unpacks
        into can't both fit (install_machine checks again after the download)."""
        from .catalog import RESERVE, download_of
        dl = download_of(entry)
        cached = client.downloads / f"{dl['sha256']}.tar.gz"
        part = client.downloads / f"{dl['sha256']}.part"
        have = cached.stat().st_size if cached.exists() else part.stat().st_size if part.exists() else 0
        remaining = max(0, int(dl["archiveBytes"]) - have)
        installed = int(entry.get("requiredStorageBytes") or entry.get("installedBytes") or 0)
        try:
            free = shutil.disk_usage(self.paths.machines if self.paths.machines.exists() else self.paths.data).free
        except OSError:
            return
        need = remaining + installed + RESERVE
        if free < need:
            from .panel.ui import human_bytes
            raise JobFailed(f"{entry.get('name') or 'This Mac'} needs {human_bytes(need)} of free space to download "
                            f"and install, but this computer has {human_bytes(free)} free. Delete a Mac in "
                            "Startup Disk, or choose a smaller one.", None, "no_space")

    def adopt_rom(self, machine) -> str | None:
        """An imported 68k Mac: keep its ROM in the ROM store ("stored"), or give it the store's
        when it came without one ("supplied") or with a wrong one ("replaced").  None when
        none of these applies."""
        try:
            try:
                quadra.check_rom(machine.rom)
            except MachineError:
                return quadra.supply_rom(machine, self.roms)
            return "stored" if self.roms.add_file(machine.rom)["added"] else None
        except (OSError, MachineError) as exc:
            log.warning("ROM of %s: %s", machine.id, exc)
        return None

    def remove_blocked(self, machine) -> tuple[str, str] | None:
        """Why a machine can't be removed now, as (code, reason); None when it can.  The
        Mac OS that runs or starts, the startup Mac (applied, or chosen for the next
        start) and the only Mac stay."""
        sup = self.supervisor
        if (sup.active or sup.launch_pending) and sup.machine is not None and sup.machine.id == machine.id:
            return ("guest-running", "Mac OS is running from this Mac. Shut down Mac OS and choose another "
                                     "startup Mac before you remove it.")
        if machine.id == self.settings.applied.get("machine.active"):
            return ("machine-active", "This is the startup Mac. Choose another startup Mac in Machine & Startup "
                                      "before you remove it.")
        if machine.id == self.settings.desired.get("machine.active"):
            return ("machine-active", "This Mac is chosen to start next. Choose another startup Mac before you "
                                      "remove it.")
        if len(self.machines.list()) <= 1:
            return ("last-machine", "This is the only Mac on this RetroBoot. Install another before you remove it.")
        return None

    def remove_machine_job(self, machine_id: str, origin: str = "web") -> Job:
        """DELETE /api/v1/machines/{id}: the machine's directory (disk, settings, state and
        any copy an install left), its checkpoints and its downloaded catalog archive
        when no other Mac came from it.  Crash-safe (MachineRegistry.trash): the
        machine leaves the registry by a rename, the files go afterwards, and the next
        start finishes an interrupted removal."""
        for j in self.jobs.active():
            if j.kind == "machines.remove" and j.params.get("id") == machine_id:
                self.jobs.last_created = False
                return j

        async def body(job: Job):
            m = self.machines.get(machine_id)
            if m is None:
                raise JobFailed("That Mac isn't installed on this RetroBoot.", None, "not_found")
            blocked = self.remove_blocked(m)
            if blocked:
                raise JobFailed(blocked[1], None, blocked[0])
            name, cps = m.name, self.checkpoints.list(m.id)
            archive = (m.meta.get("source") or {}).get("archive_sha256")
            free_before = shutil.disk_usage(self.paths.machines).free
            # No await between the check above and this rename: nothing can make the
            # Mac the startup Mac in between.
            try:
                trashed = self.machines.trash(m)
            except OSError as exc:
                raise JobFailed(f"{name} could not be removed: {exc.strerror or exc}.", None, "remove-failed")
            job.update(0.3, f"Deleting {name}’s disk and checkpoints…")
            cached: list[Path] = []
            if isinstance(archive, str) and re.fullmatch(r"[0-9a-f]{64}", archive) and not any(
                    (o.meta.get("source") or {}).get("archive_sha256") == archive for o in self.machines.list()):
                cached = [p for p in (self.paths.downloads / f"{archive}.tar.gz",
                                      self.paths.downloads / f"{archive}.part") if p.exists()]
            allocated = await asyncio.to_thread(self.machines.allocated, trashed + cached)
            await asyncio.to_thread(self.machines.purge, trashed + cached)
            freed = max(0, shutil.disk_usage(self.paths.machines).free - free_before)
            self.lifecycle.event("machine_removed", machine=machine_id, name=name, checkpoints=len(cps),
                                 allocated_bytes=allocated, freed_bytes=freed,
                                 archive_removed=bool(cached) or None, origin=origin)
            job.outcome = f"{name} was removed."
            if getattr(self.panel, "visible", False):
                try:
                    await self.panel.refresh()
                except Exception:
                    log.exception("panel refresh after a machine removal failed")
            return {"machine": machine_id, "name": name, "checkpoints": len(cps),
                    "allocated_bytes": allocated, "freed_bytes": freed}
        job = self.jobs.start("machines.remove", body, resources=("guest", "machines"),
                              params={"id": machine_id}, dedupe=False)
        self.lifecycle.event("machine_remove_request", machine=machine_id, origin=origin, job=job.id)
        return job

    async def _purge_leftovers(self, paths: list[Path]) -> None:
        """Delete what an interrupted removal or import left (MachineRegistry.recover_removals),
        in a worker thread beside the start: never on the launch path."""
        try:
            size = await asyncio.to_thread(self.machines.allocated, paths)
            done = await asyncio.to_thread(self.machines.purge, paths)
            self.lifecycle.event("machine_leftovers_removed", items=[p.name for p in paths][:20], count=done,
                                 allocated_bytes=size)
        except Exception:
            log.exception("deleting leftovers of removed machines failed")

    def checkpoint_job(self, machine_id: str, label: str, origin: str = "web") -> Job:
        async def body(job: Job):
            m = self.machines.get(machine_id)
            if m is None:
                raise JobFailed("No such machine.", None, "not_found")
            sup = self.supervisor
            was_running = sup.active and sup.machine is not None and sup.machine.id == machine_id
            if was_running:
                job.update(0.1, "Shutting down Mac OS…")
                await sup.ensure_stopped_clean(job)
            elif sup.active:
                pass  # a different machine is running; this disk is not in use
            job.update(0.5, "Saving the checkpoint…")
            try:
                info = await self.checkpoints.create(m, label, dict(self.settings.applied), self.settings.revision,
                                                     self.package_for(m).summary().get("files"))
            except CheckpointError as e:
                raise JobFailed(e.message, None, e.code)
            self.lifecycle.event("checkpoint_created", machine=machine_id, checkpoint=info["id"],
                                 method=info["method"], origin=origin)
            if was_running:
                job.update(0.8, "Starting Mac OS again…")
                await sup.start_guest("user")
                await sup.wait_started(job, self.cfg.guest_content_timeout * 2 + 30)
            return info
        return self.jobs.start("checkpoint.create", body, resources=("guest",))

    def media_install_job(self, name: str, dest: str | None, replace: bool, restart: bool,
                          origin: str = "web") -> Job:
        """Install a media-library archive/disk image into the active machine's disk.
        Mac OS must be shut down; with ``restart`` the job shuts it down cleanly
        first and starts it again afterwards (also after a failure: the disk is
        replaced only after the installed files verify)."""
        async def body(job: Job):
            sup = self.supervisor
            machine = sup.active_machine()
            if machine is None:
                raise JobFailed("No Mac OS machine is installed.", None, "not_found")
            ok, why = self.media.install_available(machine)
            if not ok:
                raise JobFailed(why, None, "capability-missing")
            was_running = sup.active and sup.machine is not None and sup.machine.id == machine.id
            if was_running:
                if not restart:
                    raise JobFailed("Shut down Mac OS first.", None, "guest_running")
                job.update(0.05, "Shutting down Mac OS…")
                await sup.ensure_stopped_clean(job)
            try:
                result = await self.media.install(machine, name, dest, replace, job.update)
                rec = result["installed"]
                self.lifecycle.event("media_installed", media=name, machine=machine.id, top=rec["top"],
                                     dest=rec["dest"], files=rec["files"], origin=origin)
                job.outcome = f"{', '.join(rec['top'] or [name])} is installed in {rec['dest'] or 'Mac OS'}."
            except MediaError as e:
                self.lifecycle.event("media_install_failed", media=name, code=e.code, origin=origin)
                raise JobFailed(e.message, None, e.code)
            finally:
                if was_running:
                    job.update(0.9, "Starting Mac OS again…")
                    await sup.start_guest("user")
            if was_running:
                await sup.wait_started(job, self.cfg.guest_content_timeout * 2 + 30)
            return result
        return self.jobs.start("media.install", body, resources=("guest",), params={"media": name})

    def machine_settings(self, machine_id: str) -> dict:
        m = self.machines.get(machine_id)
        if m is None:
            raise MachineError("That machine is not installed.")
        base = default_machine_settings(
            m, host_ram_mb=self.platform.ram_mb, gl_available=self.graphics.capable(),
            quadra_max_mb=128 if self.platform.is_zero2w else None)
        merged = {**base, **m.settings()}
        if self.settings.desired.get("machine.active") == m.id and not m.settings():
            a = self.settings.desired
            if m.osx and isinstance(a.get("machine.osx_ram_mb"), int):
                merged["osx_ram_mb"] = a["machine.osx_ram_mb"]
            elif not m.osx:
                k = "machine.quadra_ram_mb" if m.quadra else "machine.ram_mb"
                if isinstance(a.get(k), int):
                    merged["ram_mb"] = a[k]
            if a.get("display.graphics") in ("vga", "ati-radeon"):
                merged["graphics"] = a["display.graphics"]
        return merged

    async def update_machine_settings(self, machine_id: str, changes: dict, *, origin: str = "web") -> dict:
        m = self.machines.get(machine_id)
        if m is None:
            raise MachineError("That machine is not installed.")
        updated = validate_machine_settings(
            m, changes, host_ram_mb=self.platform.ram_mb,
            quadra_max_mb=128 if self.platform.is_zero2w else None,
            gl_available=self.graphics.capable())
        m.update_meta(settings=updated)
        self.lifecycle.event("machine_settings_updated", machine=machine_id, settings=updated, origin=origin)
        if self.settings.desired.get("machine.active") == m.id:
            sync: dict = {}
            if "osx_ram_mb" in changes or (m.osx and "ram_mb" in changes):
                sync["machine.osx_ram_mb"] = updated["osx_ram_mb"]
            elif "ram_mb" in changes:
                sync["machine.quadra_ram_mb" if m.quadra else "machine.ram_mb"] = updated["ram_mb"]
            if "graphics" in changes:
                sync["display.graphics"] = "standard" if updated["graphics"] == "vga" else updated["graphics"]
            if "guest_adapter" in changes and not m.osx and not m.quadra:
                sync["network.guest_adapter"] = updated["guest_adapter"]
            if "cpu" in changes and not m.osx and not m.quadra:
                sync["machine.cpu"] = updated["cpu"]
            if sync:
                ctx = self.validation_context()
                if not self.supervisor.active:
                    self.settings.set_desired_and_applied(sync, ctx)
                    await self.apply_engine.apply_restart_keys(list(sync))
                else:
                    rev = self.settings.patch(self.settings.revision, sync, ctx)
                    await self.apply_engine.apply(rev, False, None, origin)
        return self.machine_settings(machine_id)

    def latest_clean_checkpoint(self, machine_id: str) -> dict | None:
        cps = self.checkpoints.list(machine_id)
        for c in cps:
            if c.get("kind") != "pre-fsck" and not str(c.get("label") or "").lower().startswith("pre-repair"):
                return c
        return cps[0] if cps else None

    def fresh_install_checkpoint(self, machine_id: str) -> dict | None:
        cps = self.checkpoints.list(machine_id)
        for c in cps:
            if c.get("kind") == "fresh-install" or c.get("label") == "Fresh Install":
                return c
        return None

    def on_corrupt_disk_recovery(self, info: dict) -> None:
        self.corrupt_disk_recovery = dict(info or {})
        fn = getattr(self.panel, "show_corrupt_disk_recovery", None)
        if callable(fn) and self.loop is not None and not self.loop.is_closed():
            self.loop.create_task(self._deliver_corrupt_recovery(fn, dict(self.corrupt_disk_recovery)))

    async def _deliver_corrupt_recovery(self, fn, info: dict) -> None:
        try:
            res = fn(info)
            if inspect.isawaitable(res):
                await res
        except Exception:
            log.exception("panel corrupt_disk_recovery presentation failed")

    def restore_last_clean_checkpoint_job(self, machine_id: str, origin: str = "panel") -> Job:
        cp = self.latest_clean_checkpoint(machine_id)
        if cp is None:
            raise CheckpointError("No clean checkpoint is available for this Mac.", "not_found")
        return self.restore_job(machine_id, cp["id"], origin=origin)

    def reset_to_fresh_install_job(self, machine_id: str, origin: str = "panel") -> Job:
        cp = self.fresh_install_checkpoint(machine_id)
        if cp is None:
            raise CheckpointError("No Fresh Install checkpoint is available for this Mac.", "not_found")
        return self.restore_job(machine_id, cp["id"], origin=origin)

    def restore_job(self, machine_id: str, cp_id: str, origin: str = "web") -> Job:
        async def body(job: Job):
            m = self.machines.get(machine_id)
            if m is None:
                raise JobFailed("No such machine.", None, "not_found")
            sup = self.supervisor
            uses = sup.active and sup.machine is not None and sup.machine.id == machine_id
            if uses:
                job.update(0.1, "Shutting down Mac OS…")
                await sup.ensure_stopped_clean(job)
            job.update(0.4, "Restoring the disk…")
            try:
                info = await self.checkpoints.restore(m, cp_id)
            except CheckpointError as e:
                raise JobFailed(e.message, None, e.code)
            rec = self.corrupt_disk_recovery
            if rec and machine_id in (rec.get("machine"), rec.get("machine_id")):
                self.corrupt_disk_recovery = None
            sup_rec = getattr(sup, "corrupt_disk_recovery", None)
            if sup_rec and machine_id in (sup_rec.get("machine"), sup_rec.get("machine_id")):
                try:
                    sup.corrupt_disk_recovery = None
                except Exception:
                    pass
            self.lifecycle.event("checkpoint_restored", machine=machine_id, checkpoint=cp_id, method=info["method"],
                                 origin=origin)
            if uses or (not sup.active and self.settings.applied.get("machine.active") == machine_id
                        and self.mode == "normal"):
                job.update(0.7, "Starting Mac OS…")
                self.panel.forget()
                await sup.start_guest("user")
                await sup.wait_started(job, self.cfg.guest_content_timeout * 2 + 30)
            return info
        return self.jobs.start("checkpoint.restore", body, resources=("guest",))

    def bundle_job(self, origin: str = "web") -> Job:
        async def body(job: Job):
            extra = {"emulator": self.package.summary(), "machines": [m.public() for m in self.machines.list()],
                     "origin": origin}
            info = await self.diagnostics.bundle(self.settings.snapshot(None, with_schema=False), extra, job.update)
            self.lifecycle.event("diagnostics_bundle", bundle=info["id"], origin=origin)
            return dict(info, filename=f"{info['id']}.tar.gz", size_bytes=info["bytes"],
                        download=f"/api/v1/diagnostics/bundle/{info['id']}")
        return self.jobs.start("diagnostics.bundle", body)

    def wifi_job(self, op: str, origin: str = "web", **kw) -> Job:
        async def body(job: Job):
            try:
                if op == "scan":
                    job.update(None, "Scanning…")
                    return {"networks": await self.network.scan()}
                if op == "connect":
                    job.update(None, f"Joining {kw['ssid']}…")
                    res = await self.network.connect(kw["ssid"], kw.get("psk"), kw.get("hidden", False))
                    self.lifecycle.event("wifi_connected", ssid=kw["ssid"], origin=origin)
                    return res
            except NetworkError as e:
                raise JobFailed(str(e), None, e.code)
            finally:
                self.redactor.discard(None)
            raise JobFailed("Unknown operation.", None, "invalid")
        return self.jobs.start(f"wifi.{op}", body, resources=("network",))

    def bluetooth_job(self, op: str, origin: str = "web", **kw) -> Job:
        """bluetooth.pair / bluetooth.connect: one Bluetooth operation at a time."""
        async def body(job: Job):
            try:
                if op == "pair":
                    return await self.bluetooth.pair(kw["address"], origin, job, kw.get("owner"))
                if op == "connect":
                    return await self.bluetooth.connect(kw["address"], job)
            except BluetoothError as e:
                raise JobFailed(e.detail or str(e), None, e.code)
            raise JobFailed("Unknown operation.", None, "invalid")
        job = self.jobs.start(f"bluetooth.{op}", body, resources=("bluetooth",), dedupe=False,
                              params={"address": kw.get("address"), "origin": origin})
        if op == "pair" and origin == "assistant":
            self.loop.create_task(self.panel_bluetooth_finished(job))
        return job

    async def panel_bluetooth_finished(self, job: Job) -> None:
        """The setup assistant's pairing ended: say so on this computer's screen."""
        await job.done.wait()
        from .panel.bluetooth import assistant_finished
        try:
            await assistant_finished(self.panel, job, self.bluetooth.session)
        except Exception:
            log.exception("Bluetooth assistant result could not be shown")

    # -- run ---------------------------------------------------------------------------
    def _fix_permissions(self) -> None:
        """QEMU runs as rbguest: it needs to write screendumps (tmpfiles does this on the image)."""
        if os.geteuid() != 0 or not self.cfg.run_as_user:
            return
        import pwd
        try:
            gid = pwd.getpwnam(self.cfg.run_as_user).pw_gid
        except KeyError:
            log.warning("run_as user %s does not exist", self.cfg.run_as_user)
            return
        for d, mode in ((self.paths.run, 0o1775), (self.paths.screens, 0o770)):
            try:
                os.chown(d, 0, gid)
                os.chmod(d, mode)
            except OSError:
                pass

    async def start(self) -> None:
        """Start order (image/reports/hostboot-2026-10-12.md): settings, then the
        presenter connection, then everything a Mac OS launch needs (``_launch_ready``;
        _boot() waits for it), and only after the first launch the services Mac OS
        doesn't need to start: the web server, the Modern Web Helper and mDNS."""
        self.loop = asyncio.get_running_loop()
        self._fix_permissions()
        if not self.settings.state:
            self.settings.store.load()
        self.lifecycle.event("hostd_start", version=__version__, mode=self.mode, dev=self.cfg.dev,
                             settings_revision=self.settings.revision,
                             settings_notice=(self.store.notice or {}).get("source"), pid=os.getpid())
        # Hash the emulator package (about 70 MB) in a worker thread now; the launch's own
        # check then finds it done, and QEMU's binary is in the page cache.
        self._package_task = self.loop.create_task(asyncio.to_thread(self._verify_package_early))
        # A machine removal or an import that a power cut interrupted: finish the renames
        # now (instant), delete the files beside the start.
        leftovers = self.machines.recover_removals()
        if leftovers:
            self._leftovers_task = self.loop.create_task(self._purge_leftovers(leftovers), name="machine-leftovers")
        if self.platform.is_raspberry_pi or self.platform.is_gemini:
            extra = {"cpu_placement": self.cpuplace.public()["plan"]} if self.platform.is_gemini else {}
            self.lifecycle.event("platform", **self.platform.public(), revision=self.platform.revision,
                                 scaled_timeouts={k: v[1] for k, v in self.platform_scaled.items()}, **extra)
        self.rpi_display.capture_boot()   # recover the preceding display preset before Mac OS starts
        self._seed_from_install()
        await self._migrate_network_profiles()
        if self.health is not None:
            self._health_task = self.loop.create_task(self._watch_health(), name="pi-health")
        if self.platform.is_raspberry_pi and self.mode == "normal" and self.cfg.boot_network_conf:
            self._boot_network_task = self.loop.create_task(self._import_boot_network(), name="boot-network")
        await self.apply_engine.startup()
        # The presenter's hello and display report overlap the rest of the start.
        self.presenter.on_connect.append(self._on_presenter_connect)
        self.presenter.event_handlers.append(self._on_presenter_event)
        self.presenter.on_disconnect.append(lambda: self.lifecycle.event("presenter_disconnected"))
        self.presenter.start()
        self._fallback = self.loop.create_task(self._presenter_fallback())
        # The audio output (with an ASoC card's UCM setup and its silent warm-up stream,
        # about 0.6 s, which must finish before QEMU first opens the device) is chosen
        # in a worker thread while the steps below run; the launch waits for it. A PCI
        # sound card still being set up early in the boot is waited for first.
        audio_out = self.loop.create_task(self.choose_audio_output_when_ready())
        self.graphics.begin()                # host GL probe for the ATI Radeon card (graphics.py)
        try:
            await self.updates.start()
        except Exception:   # never keep Mac OS from starting over Software Update
            log.exception("Software Update start failed")
            self.updates.unavailable = "Software Update couldn't start. Restart RetroBoot to try again."
        if self.mode == "normal":
            await self.apply_engine.apply_timezone()
        self._wifi_available = await self._detect_wifi()
        await self.airport.start()          # AirPort hook: bridge socket for the emulated card
        if not self._wifi_available and self.mode == "normal":
            self._wifi_task = self.loop.create_task(self._watch_wifi(), name="wifi-watch")
        if self.mode == "normal":
            await self.ensure_guest_isolation(force=True)
        await self.laptop.start()
        # the lid's low-power values an earlier hostd saved (lidpower.py)
        self.lidsleep.closed = self.laptop.lid_open is False
        self.lidpower.start(self.laptop.lid_open)
        await self.rotation.start()         # the tilt sensor, before the presenter's first display_config
        await self.battery.start()          # battery passthrough: power bridge socket (after the first battery read)
        # Bluetooth (BlueZ: power per bluetooth.enabled, the pairing agent, reconnects, the
        # setup assistant) starts beside the launch: bluetoothd reconnects paired keyboards
        # by itself, and waiting for it must never delay Mac OS.
        from .panel.bluetooth import assistant_prompt, assistant_update
        self.bluetooth.prompt_hook = lambda sess: assistant_prompt(self.panel, sess)
        self.bluetooth.assistant_hook = lambda stage, ctx: assistant_update(self.panel, stage, ctx)
        self._bluetooth_task = self.loop.create_task(self.bluetooth.start(), name="bluetooth-start")
        out = await audio_out
        pw_sink = (out or {}).get("pw_sink")
        kw = {"pw_sink": pw_sink} if pw_sink else {}
        await self.mixer.probe(out["card_id"] if out else None, (out or {}).get("mixer"),
                               detect_switch=True, **kw)
        if self.mixer.available:
            await self.apply_engine.push_volume()
        self._launch_ready.set()
        self.lifecycle.event("hostd_launch_ready")
        self._services_task = self.loop.create_task(self._start_services(), name="hostd-services")

    def _make_web(self):
        from .web.server import WebServer
        return WebServer(self)

    def _verify_package_early(self) -> None:
        try:
            self.package.verify()
        except Exception:   # reported by the launch's own check, with the panel's message
            pass
        try:
            m = self.supervisor.active_machine()
            if m is not None and m.quadra and quadra.package_present(self.paths):
                self.package_m68k.verify()
        except Exception:
            pass

    def package_for(self, machine) -> PackageCache:
        """The emulator package a Mac starts with."""
        if getattr(machine, "osx", False):
            return self.package_osx
        return self.package_m68k if getattr(machine, "quadra", False) else self.package

    async def _start_services(self) -> None:
        """What Mac OS doesn't need to start: the web server, the Modern Web Helper (a
        Python service started through systemd) and mDNS. They start once QEMU has been
        started, the boot has stopped at a panel or failed, or after ``services_wait_s``,
        so on a slow CPU they don't compete with the launch."""
        deadline = mono() + self.services_wait_s
        while mono() < deadline:
            task = self._boot_task
            if task is not None and (task.done() or self.supervisor.handle is not None):
                break
            await asyncio.sleep(0.1)
        else:
            self.lifecycle.event("hostd_services_after_wait", waited_s=self.services_wait_s)
        try:
            if self.mode == "normal":
                await self.webproxy.start()     # Modern Web Helper: follow network.web_proxy
            try:
                self.web.tls()
            except Exception:
                log.exception("TLS certificate unavailable")
            await self.web.start()
            if self.cfg.allow_network_tools:
                await self.mdns.start()
            if self.mode == "normal" and self.cfg.allow_network_tools:
                await self.printer.start()
                await self.usbshare.start()
            self.lifecycle.event("hostd_services_started")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("starting the web server, Modern Web Helper or mDNS failed")
            self.lifecycle.event("hostd_services_failed", error=f"{type(exc).__name__}: {exc}"[:200])

    async def _migrate_network_profiles(self) -> None:
        """Keyfiles of releases up to 0.2.0 sit in /etc on the root slot; move them to
        the data partition (NetworkManager reads only that directory now)."""
        moved = self.network.migrate_legacy_profiles(self.paths.nm_legacy_connections)
        if moved:
            self.lifecycle.event("network_profiles_migrated", count=moved)
            if self.network.enabled:
                await self.network.run(["nmcli", "connection", "reload"], timeout=15)

    @staticmethod
    async def _finish(tasks, timeout: float = 10.0) -> None:
        """Cancel ``tasks`` and let them end while the event loop still runs normally.

        Left for asyncio.run's (or a test's) final sweep, a task stopped inside
        create_subprocess_exec waits for ever: the sweep also cancels the transport's own
        _connect_pipes task, whose pending callbacks then never run, so the exit the
        cancelled launch waits for is never delivered (CPython 3.13 base_subprocess).  A
        stop during a launch then hung hostd, and the tests' teardown."""
        me = asyncio.current_task()
        tasks = [t for t in tasks if t is not None and t is not me and not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    async def shutdown(self) -> None:
        self.stopping = True
        self.lifecycle.event("hostd_stop")
        self._fallback.cancel()
        # A launch in flight stops here, before the guest is stopped (it may have started one).
        await self._finish([self._boot_task])
        if self._services_task is not None and not self._services_task.done():
            self._services_task.cancel()
            try:
                await self._services_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.cfg.dev and self.cfg.guest_runner == "subprocess" and self.supervisor.handle:
            await self.supervisor.handle.stop()
            try:
                await asyncio.wait_for(self.supervisor.exit_event.wait(), 20)
            except asyncio.TimeoutError:
                pass
        session_tasks = list(self.supervisor._tasks)
        self.supervisor._cancel_session()
        await self._finish(session_tasks)
        await self.updates.stop()
        if self._wifi_task is not None and not self._wifi_task.done():
            self._wifi_task.cancel()
        if self._boot_network_task is not None and not self._boot_network_task.done():
            self._boot_network_task.cancel()
        if self._health_task is not None and not self._health_task.done():
            self._health_task.cancel()
        await self.airport.stop()           # AirPort hook
        await self.battery.stop()
        if self._bluetooth_task is not None and not self._bluetooth_task.done():
            self._bluetooth_task.cancel()
        await self.bluetooth.stop()
        await self.webproxy.stop()          # leaves the systemd unit running, like the guest
        await self.laptop.stop()
        # A guest that keeps running (hostd restart) keeps its placement; the next hostd adopts it.
        await self.lidsleep.stop()
        self.cpuplace.guest_stopped(restore=self.supervisor.handle is None)
        await self.rotation.stop()
        await self.printer.stop()
        await self.usbshare.stop()
        await self.mdns.stop()
        await self.remote.close()
        await self.presenter.stop()
        await self.web.stop()

    async def run(self) -> None:
        await self.start()
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            self.loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        await self.shutdown()

    def _seed_from_install(self) -> None:
        """First start after installation: adopt the installer's seed and startup Mac.

        The installer writes one seed, ``RBDATA/config/install-seed.json``
        (``install-to-disk --seed``); installers before several Macs wrote it into the
        machine's directory, which is still read.  A startup Mac that is not installed
        (its download was skipped) falls back to the first Mac."""
        if not self.store.fresh:
            return
        changes: dict = {}
        ms = self.machines.list()
        seeds = [self.paths.data / "config" / "install-seed.json"] + [m.dir / "install-seed.json" for m in ms]
        for seed in seeds:
            if seed.is_file():
                try:
                    data = read_json(seed, 65536)
                    for k, v in (data.get("settings") or {}).items():
                        if k in schema.BY_KEY:
                            changes[k] = v
                    seed.rename(seed.with_name("install-seed.applied.json"))
                except (OSError, ValueError, AttributeError):
                    pass
        if "machine.active" not in changes and ms:
            changes["machine.active"] = ms[0].id
        if changes:
            ctx = schema.ValidationContext(machines=self.machines.ids(), host_modes=None, check_capabilities=False)
            good = {}
            for k, v in changes.items():
                try:
                    schema.validate_changes(self.settings.desired, {k: v}, ctx)
                    good[k] = v
                except schema.ValidationError:
                    pass
            if "machine.active" not in good and ms:
                good["machine.active"] = ms[0].id
            if good:
                self.settings.set_desired_and_applied(good, ctx)
                self.lifecycle.event("install_seed_applied", keys=sorted(good))


def parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, dict]:
    ap = argparse.ArgumentParser(prog="retroboot-hostd", description="RetroBoot host service")
    ap.add_argument("--root", default=os.environ.get("RETROBOOT_ROOT", "/"),
                    help="prefix for every RetroBoot path (tests and development)")
    ap.add_argument("--config", help="hostd JSON configuration (default <root>/etc/retroboot/hostd.json)")
    ap.add_argument("--installer", action="store_true",
                    help="run the installer flow (also RETROBOOT_MODE=installer or retroboot.mode=installer "
                         "on the kernel command line)")
    ap.add_argument("--dev", action="store_true", help="development mode: no poweroff/reboot/hostname changes")
    ap.add_argument("--guest-runner", choices=("systemd", "subprocess"))
    ap.add_argument("--display-backend", choices=("retroboot", "vnc"))
    ap.add_argument("--content-probe", action="store_true", default=None,
                    help="derive guest_content from QMP screendumps (development without a presenter)")
    ap.add_argument("--http-port", type=int)
    ap.add_argument("--https-port", type=int)
    ap.add_argument("--https-bind")
    ap.add_argument("--qemu-package")
    ap.add_argument("--qemu-osx-package", help="the separate OS X emulator package (default <lib>/qemu-osx)")
    ap.add_argument("--qemu-m68k-package", help="the 68k emulator package (default <lib>/qemu-m68k)")
    ap.add_argument("--web-static")
    ap.add_argument("--installer-dir")
    ap.add_argument("--catalog-url")
    ap.add_argument("--poweroff-hook", help="command run instead of systemctl poweroff (development)")
    ap.add_argument("--reboot-hook")
    ap.add_argument("--no-host-tools", action="store_true", help="never call ip/nmcli/amixer/mount (tests)")
    ap.add_argument("--no-journal", action="store_true")
    ap.add_argument("--guest-content-timeout", type=float)
    ap.add_argument("--handoff-deadline", type=float)
    ap.add_argument("--presenter-fallback", type=float)
    ap.add_argument("--log-level")
    a = ap.parse_args(argv)
    ov = {"mode": "installer" if a.installer else None, "dev": True if a.dev else None,
          "guest_runner": a.guest_runner, "display_backend": a.display_backend, "content_probe": a.content_probe,
          "http_port": a.http_port, "https_port": a.https_port, "https_bind": a.https_bind,
          "qemu_package": a.qemu_package, "qemu_m68k_package": a.qemu_m68k_package,
          "qemu_osx_package": a.qemu_osx_package,
          "web_static": a.web_static, "installer_dir": a.installer_dir,
          "catalog_url": a.catalog_url,
          "poweroff_cmd": shlex.split(a.poweroff_hook) if a.poweroff_hook is not None else None,
          "reboot_cmd": shlex.split(a.reboot_hook) if a.reboot_hook is not None else None,
          "allow_network_tools": False if a.no_host_tools else None, "journal": False if a.no_journal else None,
          "guest_content_timeout": a.guest_content_timeout, "handoff_deadline": a.handoff_deadline,
          "presenter_fallback_start": a.presenter_fallback, "log_level": a.log_level}
    return a, ov


def main(argv: list[str] | None = None) -> int:
    a, ov = parse_args(argv)
    cfg = Config.load(root=a.root, config_file=a.config, overrides=ov, migrate=True)
    if cfg.dev and ov.get("guest_runner") is None and cfg.guest_runner == "systemd" and os.geteuid() != 0:
        cfg.guest_runner = "subprocess"
    app = Hostd(cfg)
    app.settings.store.load()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if cfg.dev or not cfg.journal:
        handlers.append(logging.FileHandler(cfg.paths.hostd_log))
    flt = RedactingFilter(app.redactor)
    for h in handlers:
        h.addFilter(flt)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO), handlers=handlers, force=True)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    log.info("hostd %s starting (mode=%s root=%s runner=%s display=%s)", __version__, cfg.mode, cfg.root,
             cfg.guest_runner, cfg.display_backend)
    for note in getattr(cfg, "config_notes", []):
        log.warning("configuration: %s", note)
    if app.lab_auth is not None:
        log.info("lab HTTP auth on for %s (hostd.json lab_http_auth_file)", app.lab_auth.base_url)
    asyncio.run(app.run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
