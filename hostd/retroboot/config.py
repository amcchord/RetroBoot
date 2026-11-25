"""Filesystem layout (ARCHITECTURE section 2) and runtime configuration.

Every path derives from a single root so tests and development runs can use a
scratch tree (``--root``).  Production uses ``/``.  Optional overrides come
from ``/etc/retroboot/hostd.json`` (or ``--config``), then this computer's own
overlay on RBDATA, ``/var/lib/retroboot/config/hostd.local.json``, then
command-line flags.

``/etc`` belongs to the system slot, which every Software Update replaces, so
per-device configuration lives in the overlay, which both slots share.  The
overlay is read leniently: a key this build doesn't know (written by a newer
build before Go Back) is skipped with a note instead of stopping hostd.  At
start hostd moves keys found only in ``/etc/retroboot/hostd.json`` (and
different from the defaults) into the overlay once (``migrate_etc_overlay``).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass
class Paths:
    root: Path = Path("/")
    qemu_package_override: Path | None = None
    qemu_m68k_package_override: Path | None = None
    web_static_override: Path | None = None
    installer_override: Path | None = None
    guestprep_override: Path | None = None
    platinum_override: Path | None = None
    ndrv_override: Path | None = None
    classicvirtio_override: Path | None = None
    apps_override: Path | None = None
    qemu_osx_package_override: Path | None = None

    def _r(self, rel: str) -> Path:
        return self.root / rel.lstrip("/")

    # Release (read-only) locations
    @property
    def lib(self) -> Path:
        return self._r("usr/lib/retroboot")

    @property
    def qemu_package(self) -> Path:
        return self.qemu_package_override or self.lib / "qemu"

    @property
    def qemu_osx_package(self) -> Path:
        """Pinned native OS X runtime; never falls back to the classic emulator."""
        return self.qemu_osx_package_override or self.lib / "qemu-osx"

    @property
    def qemu_m68k_package(self) -> Path:
        """The 68k emulator package (``linux-m68k-rN``: qemu-system-m68k for the Quadra 800
        Macs, D044), installed beside the PowerPC one.  Optional: without it only the
        68k Macs can't start."""
        return self.qemu_m68k_package_override or self.lib / "qemu-m68k"

    @property
    def ndrv_dir(self) -> Path:
        """RetroBoot video driver variant (emulator/ndrv) installed beside the package."""
        return self.ndrv_override or self.lib / "qemu-ndrv"

    @property
    def classicvirtio_dir(self) -> Path:
        """RetroBoot classicvirtio loader variant (emulator/classicvirtio) installed beside the package."""
        return self.classicvirtio_override or self.lib / "qemu-classicvirtio"

    @property
    def web_static(self) -> Path:
        return self.web_static_override or self.lib / "web"

    @property
    def installer_dir(self) -> Path:
        return self.installer_override or self.lib / "installer"

    @property
    def guestprep_lib(self) -> Path:
        return self.guestprep_override or self.lib / "guestprep"

    @property
    def platinum_lib(self) -> Path:
        """rbplatinum (Platinum asset bundle builder), platinum/ in the source tree."""
        return self.platinum_override or self.lib / "platinum"

    @property
    def platinum_assets(self) -> Path:
        """Generated Platinum bundle (Apple-derived, from the user's own disk; served at /assets/platinum/)."""
        return self.data / "assets" / "platinum"

    @property
    def apps_lib(self) -> Path:
        """Classic Mac media tools (apps/rbapps) used by the media library."""
        return self.apps_override or self.lib / "apps"

    @property
    def etc(self) -> Path:
        return self._r("etc/retroboot")

    @property
    def defaults_json(self) -> Path:
        return self.etc / "defaults.json"

    @property
    def hostd_json(self) -> Path:
        return self.etc / "hostd.json"

    @property
    def hostd_local_json(self) -> Path:
        """This computer's configuration overlay (RBDATA: survives updates and Go Back)."""
        return self.data / "config" / "hostd.local.json"

    @property
    def hostd_image_json(self) -> Path:
        """The image's own hostd.json as shipped, if an image ever ships one: keys equal to it are
        image defaults and are not migrated into the overlay."""
        return self.lib / "hostd" / "hostd.image.json"

    # Data partition
    @property
    def data(self) -> Path:
        return self._r("var/lib/retroboot")

    @property
    def settings(self) -> Path:
        return self.data / "config" / "settings.json"

    @property
    def machines(self) -> Path:
        return self.data / "machines"

    @property
    def share(self) -> Path:
        return self.data / "share"

    @property
    def checkpoints(self) -> Path:
        return self.data / "checkpoints"

    @property
    def rom_store(self) -> Path:
        """Verified Mac ROMs kept by hash (quadra.RomStore: the Quadra 800 ROM of an imported
        68k Mac or one added on the web page), copied into new 68k Macs.  Apple code: on
        RBDATA only, never in the image or an update."""
        return self.data / "roms"

    @property
    def media_library(self) -> Path:
        """Disc images and software archives added by the user (media.py)."""
        return self.data / "media"

    @property
    def state(self) -> Path:
        return self.data / "state"

    @property
    def nm_connections(self) -> Path:
        """NetworkManager keyfiles (0700 directory, 0600 files): on the data
        partition, so host network profiles survive A/B root updates."""
        return self.data / "network" / "connections"

    @property
    def bluetooth_storage(self) -> Path:
        """BlueZ's pairing store (a bind mount of RBDATA's bluetooth/ on an installed
        system; the live installer's own directory, carried over by install-to-disk)."""
        return self._r("var/lib/bluetooth")

    @property
    def nm_legacy_connections(self) -> Path:
        """Where releases up to 0.2.0 kept NetworkManager keyfiles (root slot)."""
        return self._r("etc/NetworkManager/system-connections")

    @property
    def platinum_icon(self) -> Path:
        """The Platinum bundle's Happy Mac (wave4/platinum), for the startup indicator."""
        return self.data / "assets" / "platinum" / "icons" / "overview-32.rbi"

    @property
    def startup_icon(self) -> Path:
        """Mac OS's own Happy Mac, captured by the presenter from the first startup
        screen that shows it (private, D012): the startup indicator's exact art."""
        return self.state / "startup-icon.rbi"

    @property
    def downloads(self) -> Path:
        return self.data / "downloads"

    @property
    def logs(self) -> Path:
        return self.data / "logs"

    @property
    def lifecycle_log(self) -> Path:
        return self.logs / "lifecycle.jsonl"

    @property
    def serial_log(self) -> Path:
        return self.logs / "guest-serial.log"

    @property
    def hostd_log(self) -> Path:
        return self.logs / "hostd.log"

    @property
    def evidence(self) -> Path:
        return self.logs / "evidence"

    @property
    def bundles(self) -> Path:
        return self.logs / "bundles"

    # Runtime
    @property
    def run(self) -> Path:
        return self._r("run/retroboot")

    @property
    def display_sock(self) -> Path:
        return self.run / "display.sock"

    @property
    def presenter_sock(self) -> Path:
        return self.run / "presenter.sock"

    @property
    def remote_sock(self) -> Path:
        """Presenter remote screen socket (frame tap and remote input; ARCHITECTURE 4.6)."""
        return self.run / "remote.sock"

    @property
    def qmp_control(self) -> Path:
        return self.run / "qmp-control.sock"

    @property
    def qmp_events(self) -> Path:
        return self.run / "qmp-events.sock"

    @property
    def vnc_sock(self) -> Path:
        return self.run / "vnc.sock"

    @property
    def lab_qmp(self) -> Path:
        return self.run / "qmp-lab.sock"

    @property
    def guest_dir(self) -> Path:
        return self.run / "guest"

    @property
    def argv_json(self) -> Path:
        return self.guest_dir / "argv.json"

    @property
    def status_json(self) -> Path:
        return self.guest_dir / "status.json"

    @property
    def screens(self) -> Path:
        return self.run / "screens"

    @property
    def media(self) -> Path:
        return self.run / "media"

    def ensure(self) -> None:
        """Create the writable directories hostd needs (idempotent)."""
        for p in (
            self.settings.parent, self.machines, self.share, self.checkpoints,
            self.state, self.downloads, self.logs, self.evidence, self.bundles,
            self.run, self.guest_dir, self.screens, self.media, self.media_library,
        ):
            p.mkdir(parents=True, exist_ok=True)
        for p in (self.state, self.nm_connections.parent, self.nm_connections):
            try:
                p.mkdir(parents=True, exist_ok=True)
                os.chmod(p, 0o700)
            except OSError:
                pass


MODES = ("normal", "installer")


def boot_mode(configured: str, environ, cmdline: str) -> str:
    """hostd mode without --installer: ``RETROBOOT_MODE`` (environment) wins, then
    ``retroboot.mode=`` on the kernel command line (set only by the installer
    ISO's GRUB entry), then hostd.json / the default."""
    env = (environ.get("RETROBOOT_MODE") or "").strip().lower()
    if env in MODES:
        return env
    for arg in cmdline.split():
        if arg.startswith("retroboot.mode="):
            value = arg.split("=", 1)[1].strip().lower()
            if value in MODES:
                return value
    return configured if configured in MODES else "normal"


@dataclass
class Config:
    """Runtime options.  Field names double as hostd.json keys."""

    root: str = "/"
    mode: str = "normal"                 # normal | installer
    dev: bool = False
    guest_runner: str = "systemd"        # systemd | subprocess
    guest_unit: str = "retroboot-guest.service"
    display_backend: str = "retroboot"   # retroboot | vnc (development fallback)
    content_probe: bool = False          # synthesize guest_content from QMP screendumps
    finder_probe: bool = True            # detect the Finder (finder.py, any Appearance theme) on QMP screendumps
    guest_content_timeout: float = 60.0  # upper bound for the first-content deadline
    # First-content deadline learned per machine (emulator/reports/pregray-hang-20261011.md):
    # factor x the median of the machine's last five first-content times, at least twice
    # the slowest of them and at least floor; guest_content_timeout while it has no
    # history.  Never above guest_content_timeout.
    guest_content_factor: float = 3.0
    guest_content_floor: float = 20.0
    handoff_deadline: float = 40.0       # accelerated boot HMP fallback, seconds after start
    clean_stop_timeout: float = 90.0
    # A 68k Mac's Restart is a Shut Down and a new start (supervisor._restart_by_shutdown): a
    # Shut Down Mac OS finishes this long after hostd's Return (an application asked to save
    # first, after the request itself gave up) still restarts it.
    restart_by_shutdown_ttl_s: float = 600.0
    # A Shut Down, Restart RetroBoot, Restart to Update or Go Back that gave up waiting for Mac OS
    # (an application asked to save) completes when Mac OS finishes that Shut Down this long
    # after hostd's answer (supervisor._finish_late_stop), instead of powering off.
    late_stop_ttl_s: float = 600.0
    # Mac OS's power-key dialog (powerdlg.py, hostd/reports/powerdlg-2026-10-12.md).  The first
    # key goes as soon as the Finder is up: a busy Mac OS holds it and shows the dialog when it
    # can (power_idle_first: wait until it is idle first, QEMU's TCG thread below power_idle_busy
    # for power_ready_quiet_s or, unmeasurable, a still screen, at most power_ready_max_s; on the
    # GPD that cost 5-6 s).  A key is pressed again only when Mac OS went idle for
    # power_retry_idle_s without drawing the dialog (it dropped the key), at least
    # power_retry_min_s after it, up to power_key_attempts keys within power_request_budget_s.
    power_idle_first: bool = False
    power_idle_busy: float = 0.6
    power_ready_quiet_s: float = 1.5
    power_ready_max_s: float = 30.0
    power_retry_idle_s: float = 3.0
    power_retry_min_s: float = 4.0
    power_key_attempts: int = 3
    # Before the first key, when the menu bar is not on screen: the pointer moves 8 pixels for
    # half a second and back, which ends a screen saver.  After Dark's Flying Toasters (Deluxe,
    # SuperDeluxe) keep Mac OS busy and cover its dialog, and the ADB power key does not end
    # them; hostd then waits up to power_wake_settle_s for the screen to hold still
    # (supervisor._wake_screen).
    power_wake_nudge: bool = True
    power_wake_settle_s: float = 4.0
    power_request_budget_s: float = 60.0
    # The last resort when the Finder is not recognized (Supervisor._started_watch, mac_os_started,
    # for every dependent of mac_os_up): Mac OS counts as started power_started_after_s after its
    # content once it has idled for power_started_idle_s.
    power_started_after_s: float = 20.0
    power_started_idle_s: float = 5.0
    power_fallback_s: float = 1.0        # any Appearance theme: a modal box that appeared with the menus
                                         # changing state, unchanged this long on an idle Mac OS, is the dialog
    power_dialog_wait_s: float = 12.0    # a power-button key's dialog: how long a request waits for it
    power_key_pending_s: float = 60.0    # a request reuses a power key this recent if its dialog is up
    power_button_repeat_s: float = 10.0  # host power button presses this close send one power key
    force_kill_after_s: float = 3.0      # force stop: SIGKILL the emulator if SIGTERM has not ended it
    qmp_watchdog_s: float = 10.0         # QMP query-status interval (emulator responsiveness)
    qmp_watchdog_timeout_s: float = 5.0
    finder_wait_timeout: float = 150.0
    adopt_settled_s: float = 180.0       # an adopted guest this old is taken to be at the Finder
    known_good_after: float = 30.0
    confirm_seconds: float | None = None     # override the schema's 15 s window (tests only)
    presenter_fallback_start: float = 30.0   # start headless if the presenter never reports a display
    https_port: int = 443
    https_bind: str = "0.0.0.0"
    http_port: int = 80
    # Plain HTTP: the full app for this computer's own tools (loopback); every other
    # client is redirected to HTTPS, so typing the bare address in a browser works.
    http_bind: str = "0.0.0.0"
    poweroff_cmd: list = field(default_factory=lambda: ["systemctl", "poweroff"])
    reboot_cmd: list = field(default_factory=lambda: ["systemctl", "reboot"])
    sync_cmd: list = field(default_factory=lambda: ["sync"])
    hostname_cmd: list = field(default_factory=lambda: ["hostnamectl", "set-hostname"])
    timezone_cmd: list = field(default_factory=lambda: ["timedatectl", "set-timezone"])
    timezone_guess_urls: list[str] | None = None
    timezone_guess_allow_http: bool = False
    # Rewrites GRUB's display.cfg/checkerboard for display.pixel_scale (image rb-bootdisplay).
    bootdisplay_cmd: list = field(default_factory=lambda: ["systemctl", "--no-block", "start",
                                                           "retroboot-bootdisplay.service"])
    catalog_url: str | None = None       # defaults to the built-in catalog for this board
    # LAB ONLY (never set in product images): an extra CA certificate trusted, in addition to
    # the system CAs, for a test machine catalog on the LAN and its archive downloads.
    catalog_ca_file: str | None = None
    # LAB ONLY (never set in product images): a 0600 file with RB_LAB_HTTP_USER/_PASS/_BASE_URL;
    # catalog, archive, update feed and bundle downloads under that base get HTTP Basic auth
    # (retroboot/labauth.py).  Never logged or shown.
    lab_http_auth_file: str | None = None
    run_as_user: str = "rbguest"
    python: str = sys.executable
    qemu_package: str | None = None
    qemu_osx_package: str | None = None
    qemu_m68k_package: str | None = None     # 68k emulator package (default <lib>/qemu-m68k)
    web_static: str | None = None
    installer_dir: str | None = None
    # The installer's disk layouts (install-to-disk --board): "auto" is "rpi" on a Raspberry
    # Pi and on any other aarch64 board (QEMU's virt in the lab), else "x86"; "x86"/"rpi" force.
    installer_board: str = "auto"
    # Raspberry Pi: where the installed system mounts its FAT boot partition (config.txt,
    # tryboot.txt; Recovery's Start Installer… when the installer is on the same card).
    pi_boot_dir: str = "/boot/firmware"
    guestprep_path: str | None = None
    platinum_path: str | None = None     # rbplatinum package directory (default <lib>/platinum)
    platinum_build: bool = True          # build the Platinum asset bundle from the guest disk when missing
    apps_path: str | None = None         # apps/rbapps library directory (default <lib>/apps)
    ndrv_dir: str | None = None          # video driver variant directory (default <lib>/qemu-ndrv)
    classicvirtio_dir: str | None = None # shared-folder driver loader variant (default <lib>/qemu-classicvirtio)
    prep_max_attempts: int = 3
    # Frozen-guest check (supervisor.FrozenGuestCheck): the guest framebuffer
    # has not changed for frozen_stale_s (the Mac OS menu-bar clock changes
    # every minute) and nobody used the keyboard/pointer for frozen_quiet_s ->
    # 1-pixel pointer nudge; no cursor redraw within frozen_nudge_wait_s ->
    # guest.responsive = false.  Never restarts anything.
    frozen_check: bool = True
    frozen_stale_s: float = 75.0
    frozen_quiet_s: float = 30.0
    frozen_nudge_wait_s: float = 3.0
    frozen_poll_s: float = 5.0
    # LAB ONLY: an extra QMP socket (<run>/qmp-lab.sock) so a test can freeze
    # the guest with QMP "stop" while hostd's own QMP connection stays alive.
    lab_qmp: bool = False
    cmdline_path: str = "/proc/cmdline"
    allow_network_tools: bool = True     # nmcli/ip/amixer; tests disable
    # AirPort bridge (retroboot/airport): auto = NetworkManager with Wi-Fi hardware, else the
    # virtual network for the wired uplink; "fixture" reads airport_fixture (lab); "off" disables.
    # Battery passthrough (battery.py): auto (PMU machine with a battery when the
    # host has one), cuda (never), pmu (always; lab)
    power_controller: str = "auto"
    # USB mouse for Mac OS's right button and wheel (pointer.py): auto (when the disk has
    # USB Overdrive), on, off
    usb_mouse: str = "auto"
    # Bluetooth input devices (retroboot/bluetooth.py): "auto" (BlueZ through python3-dbus-fast
    # when host tools are allowed), "bluez", "fake" (simulated demo devices), "off".
    # bluetooth_assistant: "auto" pairs a Bluetooth keyboard (and mouse) found in pairing
    # mode while no keyboard is attached, "off" never, "always" (lab) as if none were.
    bluetooth_backend: str = "auto"
    bluetooth_assistant: str = "auto"
    airport_backend: str = "auto"
    # LAB: accept Mesa's software renderer for the experimental ATI Radeon card
    # (graphics.py) when this computer has no GPU render node.
    graphics_allow_software: bool = False
    graphics_dri_dir: str | None = None     # lab/tests: where render nodes appear (default /dev/dri)
    airport_fixture: str | None = None
    airport_virtual_ssid: str = "RetroBoot"
    airport_uplink: str | None = None
    # Modern Web Helper (retroboot/webproxy.py): "systemd" runs retroboot-webproxy.service and
    # redirects the guest user's web/DNS traffic to it; "subprocess" (development) runs it as a
    # child without the redirect; "off" disables it.  Ports are on host loopback only.
    # Guest isolation (retroboot/guestnet.py): the nftables table that keeps the QEMU user away
    # from this computer's own addresses; hostd checks it at start and before each launch.
    guest_isolation: bool = True
    webproxy_runner: str = "systemd"
    webproxy_port: int = 8080
    webproxy_dns_port: int = 8053
    webproxy_path: str | None = None     # directory holding rbwebproxy (default <lib>/webproxy)
    # Laptop power policy (retroboot/laptop.py): while discharging, warn at
    # battery_warn_percent, cleanly shut Mac OS down and power off at
    # battery_critical_percent; force the guest only if that fails at
    # battery_floor_percent.
    battery_warn_percent: int = 10
    battery_critical_percent: int = 4
    # With battery passthrough (battery.py) Mac OS puts itself to sleep when no
    # reserve power remains (5 % on its own scale), so hostd's clean shutdown
    # starts above that.
    battery_passthrough_critical_percent: int = 8
    battery_floor_percent: int = 2
    battery_poll_s: float = 30.0
    # Gauges that lie (the N5105's EC: a constant 3.8 W and 7.6 V, so power_now
    # promised 222 min with half an hour left, and it died at a 4 % reading):
    # the time to empty comes from the observed percent-per-minute slope when it
    # disagrees with power_now; the critical shutdown also starts when that slope
    # projects fewer than battery_critical_minutes to battery_floor_percent, and
    # a clean shutdown that fails is forced once the slope projects
    # battery_force_minutes to the floor or battery_force_after_s have passed.
    battery_critical_minutes: float = 3.0
    battery_force_minutes: float = 1.0
    battery_force_after_s: float = 180.0
    # "auto" (system battery or a portable SMBIOS chassis type), "portable" or "desktop":
    # decides whether product text says "the laptop" (retroboot/formfactor.py).
    form_factor: str = "auto"
    sysfs_path: str = "/sys"
    # Device nodes (retroboot/optical.py opens the CD-ROM drives sysfs_path lists here).
    dev_path: str = "/dev"
    # The board (retroboot/platform.py): a Raspberry Pi's device tree, else DMI under
    # sysfs_path.  Lab/tests: a fake tree.
    device_tree: str = "/proc/device-tree"
    # Explicit ALSA PCM for the guest's sound (e.g. "default:CARD=vc4hdmi1"); empty:
    # hostd chooses at every launch (audio.py).  Per device, in hostd.local.json.
    audio_device: str = ""
    # Exact machine IDs in this appliance's local overlay. Empty keeps the default.
    osx_48k_machine: str = ""
    osx_fast_cores_machine: str = ""
    # The emulator's host-stream pre-roll in ms (audio.screamer_options): -1 follows the
    # board profile (Gemini PDA 120, strict; QEMU's 40 elsewhere), 0 passes nothing
    # (QEMU's defaults), N > 0 that lead. Lab A/B: hostd.local.json, then restart Mac OS.
    audio_host_lead_ms: int = -1
    # Raspberry Pi only: Wi-Fi settings a user put on the FAT boot partition, imported
    # once (same format and checks as the installer's retroboot/network.conf).
    boot_network_conf: str = "/boot/firmware/retroboot/network.conf"
    # Where the emulator's threads run while Mac OS runs (retroboot/cpuplace.py). "auto"
    # follows the board's profile (Gemini PDA: QEMU's TCG thread on cpu8-9, its other
    # threads, hostd and the presenter on cpu4-7, unbound workqueues on cpu0-7, the
    # performance governor on the A72 policy; nothing on PCs and Raspberry Pis), "on"
    # applies the keys below on any board, "off" touches nothing. An empty key takes the
    # profile's value, "none" leaves that part alone. Lab A/B: hostd.local.json, then
    # restart hostd (a running Mac OS is adopted and placed again).
    cpu_placement: str = "auto"
    cpu_vcpu: str = ""              # cpulist for QEMU's TCG thread(s), e.g. "8-9"
    cpu_emulator: str = ""          # QEMU's other threads
    cpu_host: str = ""              # hostd, rb-presenter and guest-launcher
    cpu_workqueue: str = ""         # unbound workqueue cpumask (as a cpulist)
    cpu_governor: str = ""          # cpufreq governor of the vCPU policy while Mac OS runs
    cpu_min_mhz: int = -1           # raise that policy's scaling_min_freq (MHz); 0 leaves it, -1 profile
    cpu_max_mhz: int = -1           # that policy's scaling_max_freq while Mac OS runs (MHz, clamped to
                                    # cpuinfo_max_freq; lifts the a72dvfs boot cap); 0 leaves it, -1 profile
    cpu_poll_s: float = 2.0         # re-check (late CPUs, new threads, drifted masks)
    proc_path: str = "/proc"        # tests: a fake /proc for cpuplace.py
    # Closing the lid (retroboot/lidsleep.py): "auto" (the board profile: "sleep" on the
    # Gemini PDA, "blank" elsewhere), "sleep" (Mac OS's own Sleep through its power-key
    # dialog; the lid opening wakes it) or "blank" (the internal panel only, as before).
    # lid_sleep_delay_s: a bounce shorter than this is not a close; lid_sleep_wait_s: the
    # bridge's sleep after S; lid_busy_wait_s: how long a busy Mac OS is waited for before
    # the key is pressed anyway (as the power button does).
    lid_action: str = "auto"
    lid_sleep_delay_s: float = 1.0
    lid_sleep_wait_s: float = 15.0
    lid_busy_wait_s: float = 30.0
    # Low power while the lid is closed (retroboot/lidpower.py): every cpufreq policy at
    # its lowest frequency and the keyboard matrix polled every lid_keypad_poll_ms (0:
    # left alone).  "auto": the board profile (on for the Gemini PDA), "on", "off".
    lid_low_power: str = "auto"
    lid_keypad_poll_ms: int = 1000
    # Gemini PDA: the image's speaker/headphone hook (speakers.py; empty: the board
    # profile's /usr/lib/retroboot/gemini/rb-gemini-audio, "none": no choice offered).
    gemini_audio_cmd: str = ""
    # Gemini PDA built-in keyboard (presenter gemkbd.c): the keycaps' layout, "us" or "uk"
    # (which symbols Shift and Fn give); empty: us.
    gemini_keyboard: str = ""
    # Touchscreen axes (presenter, before the panel orientation): comma-separated
    # "swap-xy", "invert-x", "invert-y", applied in that order like the device tree's
    # touchscreen-* properties; empty: as the kernel reports. Lab fix for a unit whose
    # touch controller is mounted differently.
    touch_transform: str = ""
    # display.rotation "auto" (retroboot/orientation.py): the accelerometer, through
    # iio-sensor-proxy on the system bus (dbus_system_address, default the system bus)
    # or the IIO devices under iio_root. Lab: a fake sysfs tree or a private bus.
    orientation_sensor: bool = True
    iio_root: str = "/sys/bus/iio/devices"
    udev_data: str = "/run/udev/data"
    dbus_system_address: str | None = None
    # Auto rotation's orientation reaches GRUB's background (rb-bootdisplay) once it
    # has stayed this long, so turning a tablet rarely writes the ESP.
    rotation_boot_delay_s: float = 60.0
    # Software Update (retroboot/update, ARCHITECTURE 13).
    update_esp: str = "/boot/efi"        # RBEFI mount (systemd automount)
    update_keys_dir: str | None = None   # trusted signing keys (default <lib>/update/keys)
    update_bwlimit_kbps: int = 32768     # slot write rate limit while Mac OS runs (KiB/s)
    update_panel_health_s: float = 20.0  # trial confirmed by a stable classic panel (Mac OS not started)
    update_usb_poll_s: float = 10.0
    update_ca_file: str | None = None    # LAB ONLY: extra CA for a test update server
    update_allow_http: bool = False      # LAB ONLY: plain-HTTP update server (signatures still checked)
    update_layout: dict | None = None    # tests: slot layout instead of the running disk's
    # Web Screen page (retroboot/remote): frames per second at most, the share of one
    # core encoding may use (slower updates when frames are expensive), and the pace of
    # typed text and key bar keys in Mac OS keyboard events (a burst, then a steady rate
    # below what Mac OS drains from QEMU's 128-event ADB keyboard queue).
    remote_max_fps: float = 30.0
    remote_cpu_budget: float = 0.35
    remote_key_events_per_s: float = 20.0
    remote_key_burst: float = 96.0
    remote_trace_keys: bool = False      # LAB ONLY: log every remote key event (never in product images)
    journal: bool = True                 # read logs through journalctl when available
    log_level: str = "INFO"

    @property
    def paths(self) -> Paths:
        p = Paths(Path(self.root))
        if self.ndrv_dir:
            p.ndrv_override = Path(self.ndrv_dir)
        if self.classicvirtio_dir:
            p.classicvirtio_override = Path(self.classicvirtio_dir)
        if self.qemu_package:
            p.qemu_package_override = Path(self.qemu_package)
        if self.qemu_osx_package:
            p.qemu_osx_package_override = Path(self.qemu_osx_package)
        if self.qemu_m68k_package:
            p.qemu_m68k_package_override = Path(self.qemu_m68k_package)
        if self.web_static:
            p.web_static_override = Path(self.web_static)
        if self.installer_dir:
            p.installer_override = Path(self.installer_dir)
        if self.guestprep_path:
            p.guestprep_override = Path(self.guestprep_path)
        if self.platinum_path:
            p.platinum_override = Path(self.platinum_path)
        if self.apps_path:
            p.apps_override = Path(self.apps_path)
        return p

    def read_cmdline(self) -> str:
        """The kernel command line (production root only: a scratch --root never
        inherits the development host's boot mode unless cmdline_path is set)."""
        if self.root not in ("/", "") and self.cmdline_path == "/proc/cmdline":
            return ""
        try:
            return Path(self.cmdline_path).read_text(errors="replace")[:4096]
        except OSError:
            return ""

    def update(self, values: dict[str, Any]) -> None:
        known = {f.name: f for f in fields(self)}
        for key, value in values.items():
            if key not in known:
                raise ValueError(f"Unknown hostd configuration key: {key}")
            setattr(self, key, value)

    @classmethod
    def load(cls, root: str = "/", config_file: str | None = None,
             overrides: dict[str, Any] | None = None, migrate: bool = False) -> "Config":
        cfg = cls(root=root)
        cfg.config_notes = []
        path = Path(config_file) if config_file else cfg.paths.hostd_json
        if config_file is None and migrate:
            try:
                moved = migrate_etc_overlay(cfg.paths)
                if moved:
                    cfg.config_notes.append(f"moved {', '.join(sorted(moved))} from {path} to "
                                            f"{cfg.paths.hostd_local_json}")
            except (OSError, ValueError) as e:
                cfg.config_notes.append(f"could not move {path} into the overlay: {e}")
        if path.is_file():
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError("hostd configuration must be a JSON object")
            if config_file is None:
                data.pop("root", None)
            cfg.update(data)
        if config_file is None:
            local, notes = read_overlay(cfg.paths.hostd_local_json)
            cfg.config_notes += notes
            cfg.update(local)
        if overrides:
            cfg.update({k: v for k, v in overrides.items() if v is not None})
        if not (overrides or {}).get("mode"):
            cfg.mode = boot_mode(cfg.mode, os.environ, cfg.read_cmdline())
        if cfg.dev:
            # Development runs never power off, reboot or rename the machine
            # they run on unless a hook was configured explicitly.  An empty
            # command means "record in the lifecycle log only".
            if cfg.poweroff_cmd == ["systemctl", "poweroff"]:
                cfg.poweroff_cmd = []
            if cfg.reboot_cmd == ["systemctl", "reboot"]:
                cfg.reboot_cmd = []
            if cfg.hostname_cmd == ["hostnamectl", "set-hostname"]:
                cfg.hostname_cmd = []
            if cfg.timezone_cmd == ["timedatectl", "set-timezone"]:
                cfg.timezone_cmd = []
            if cfg.bootdisplay_cmd == ["systemctl", "--no-block", "start", "retroboot-bootdisplay.service"]:
                cfg.bootdisplay_cmd = []
        return cfg


# --- Per-device overlay (RBDATA) --------------------------------------------------

def _known_keys() -> set[str]:
    return {f.name for f in fields(Config)} - {"root"}


def read_overlay(path: Path) -> tuple[dict[str, Any], list[str]]:
    """The overlay's known keys and notes about anything skipped.  Never raises for a bad file."""
    try:
        raw = Path(path).read_text()
    except FileNotFoundError:
        return {}, []
    except OSError as e:
        return {}, [f"{path} can't be read ({e.strerror or type(e).__name__}); ignored"]
    try:
        data = json.loads(raw)
    except ValueError:
        return {}, [f"{path} is not valid JSON; ignored"]
    if not isinstance(data, dict):
        return {}, [f"{path} is not a JSON object; ignored"]
    known = _known_keys()
    skipped = sorted(k for k in data if k not in known)
    out = {k: v for k, v in data.items() if k in known}
    return out, ([f"{path}: skipped keys this version doesn't know: {', '.join(skipped)}"] if skipped else [])


def migrate_etc_overlay(paths: Paths) -> list[str]:
    """Move keys that only /etc/retroboot/hostd.json has (and that differ from the defaults and
    from the image's shipped copy) into the RBDATA overlay, once per /etc content.  Keys already
    in the overlay win.  Returns the keys moved."""
    import hashlib
    src = paths.hostd_json
    if not src.is_file():
        return []
    raw = src.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    marker = paths.hostd_local_json.with_name("hostd.local.migrated")
    try:
        if marker.read_text().strip() == digest:
            return []
    except OSError:
        pass
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("hostd configuration must be a JSON object")
    image: dict[str, Any] = {}
    try:
        image = json.loads(paths.hostd_image_json.read_text())
        image = image if isinstance(image, dict) else {}
    except (OSError, ValueError):
        pass
    defaults = Config()
    known = _known_keys()
    local, _ = read_overlay(paths.hostd_local_json)
    try:
        current = json.loads(paths.hostd_local_json.read_text())
        current = current if isinstance(current, dict) else {}
    except (OSError, ValueError):
        current = {}
    moved = []
    for k, v in data.items():
        if k not in known or k in current or k in local:
            continue
        if getattr(defaults, k) == v or (k in image and image[k] == v):
            continue
        current[k] = v
        moved.append(k)
    paths.hostd_local_json.parent.mkdir(parents=True, exist_ok=True)
    if moved:
        tmp = paths.hostd_local_json.with_name(".hostd.local.json.tmp")
        with open(tmp, "w") as f:
            json.dump(current, f, indent=1, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, paths.hostd_local_json)
    tmp = marker.with_name(".hostd.local.migrated.tmp")
    tmp.write_text(digest + "\n")
    os.replace(tmp, marker)
    return moved
