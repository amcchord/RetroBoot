"""Which computer RetroBoot runs on: a Raspberry Pi or a Gemini PDA (device tree) or a PC (DMI).

Read once at start.  On a Raspberry Pi the firmware's device tree names the
board (``/proc/device-tree/model``, e.g. "Raspberry Pi 5 Model B Rev 1.0"), its
``compatible`` list the board and SoC (``raspberrypi,5-model-b``,
``brcm,bcm2712``) and ``system/linux,revision`` the board's revision code
(type, SoC, memory size).  A PC has no device tree but SMBIOS/DMI
(``/sys/class/dmi/id``); a Pi has no DMI.  Nothing here may raise: a missing or
odd file means "unknown".

What depends on it (each one branches on the board, never on the CPU
architecture alone):

* Diagnostics and ``GET /api/v1/status`` name the computer (``model``).
* Software Update: the Pi test build has no GRUB A/B slots (update/manager.py).
* Startup and shutdown deadlines (``timeout_scale``): a Pi 4 runs the PowerPC
  emulator about two to three times slower than the slowest PC tested (GPD
  MicroPC, N4100), so fixed deadlines tuned on PCs are stretched there.
* The host audio choice (audio.py: vc4 HDMI cards, the Pi 4's headphone jack).
* First-boot Wi-Fi from the Pi's FAT boot partition (netconf.py).

The Planet Computers Gemini PDA (``compatible = "planet,gemini-pda",
"mediatek,mt6797"``, MT6797X / Helio X27, Debian 13 on the community 6.6
kernel) is the ``gemini`` family.  Besides the deadlines and Software Update it
brings a profile (:attr:`Platform.profile`): its charger and fuel gauge for
the battery code, its codec card and speaker amplifiers for the sound
(audio.py, speakers.py), the CPU clusters for QEMU's threads (cpuplace.py),
and the recovery chord's default (Control-Alt-Backspace: Delete is Fn+Backspace
on its keyboard; the presenter's Gemini layer maps the keys).

On a PC every answer is the old default, so x86 behaviour does not change.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

DEVICE_TREE = "/proc/device-tree"
DMI = "/sys/class/dmi/id"

# Raspberry Pi new-style revision codes (bit 23 set): bits 4-11 board type,
# 12-15 processor, 20-22 memory size.  https://www.raspberrypi.com/documentation/
# computers/raspberry-pi.html#new-style-revision-codes
PI_TYPES = {0x12: "zero2w", 0x11: "pi4", 0x13: "pi400", 0x14: "cm4", 0x15: "cm4s", 0x17: "pi5", 0x18: "cm5",
            0x19: "pi500", 0x1a: "cm5"}
PI_SOCS = {2: "BCM2837", 3: "BCM2711", 4: "BCM2712"}
PI_MEMORY_MB = {0: 256, 1: 512, 2: 1024, 3: 2048, 4: 4096, 5: 8192, 6: 16384}

# compatible strings -> board (first match wins; checked before the model text)
PI_COMPATIBLE = (("raspberrypi,model-zero-2-w", "zero2w"),
                 ("raspberrypi,5-compute-module", "cm5"), ("raspberrypi,500", "pi500"),
                 ("raspberrypi,5-model-b", "pi5"), ("raspberrypi,4-compute-module", "cm4"),
                 ("raspberrypi,400", "pi400"), ("raspberrypi,4-model-b", "pi4"))
PI_FAMILY = {"zero2w": "zero2w", "pi4": "pi4", "pi400": "pi4", "cm4": "pi4", "cm4s": "pi4",
             "pi5": "pi5", "pi500": "pi5", "cm5": "pi5"}

# How much slower than the PCs the deadlines were tuned on (the GPD MicroPC's
# N4100 reaches the Mac OS gray 25-30 s after launch and the Finder at about
# 66 s after power-on; PowerPC emulation on a Cortex-A72 at 1.5-1.8 GHz is
# expected at roughly half to a third of that speed, a Cortex-A76 at 2.4 GHz
# near the N5105).  Generous on purpose: a deadline only matters when Mac OS
# hangs, and a misfire on a slow but healthy start is the worse failure.
#
# Gemini PDA: QEMU's TCG thread runs on the MT6797X's two Cortex-A72s (cpuplace.py)
# at 845-1495 MHz (VPROC2 fixed at 1.0 V by mt6797-cl2-power; the 2.3-2.6 GHz vendor
# points need voltage scaling the kernel lacks): the same core at the same clock as
# the slowest Raspberry Pi 4 (1.5 GHz; later boards run 1.8 GHz), so at least x3.
# Half a step more because its LPDDR3 is shared with a CPU-copied 9 MB scanout
# (geminipda-drm), the clamshell throttles at the 85 C trip with no heatsink, and a
# launch can start before the A72 cluster is online (the vCPU then runs on a
# 1.35 GHz Cortex-A53 until cpuplace.py moves it).
TIMEOUT_SCALE = {"zero2w": 4.0, "pi4": 3.0, "pi5": 2.0, "gemini": 3.5}
# The startup indicator's first guess at QEMU launch -> Mac OS's first screen
# before this machine has a learned model (startup.py; PCs: 9 s).
LAUNCH_MS = {"zero2w": 30000.0, "pi4": 75000.0, "pi5": 30000.0, "gemini": 87500.0}

# Planet Computers Gemini PDA (atzandrew/gemini-nixos mt6797-gemini-pda.dts).
GEMINI_COMPATIBLE = "planet,gemini-pda"
GEMINI_PROFILE = {
    # power_supply names (laptop.PowerSupplies, formfactor): the BQ25896 charger
    # ("bq25890-charger-N", type USB, so not "Mains") is the adapter; the MT6351 fuel
    # gauge ("mt6351-battery") is the battery even when mt6351_gauge was loaded without
    # system=1 (then its scope is "Device", which hostd otherwise skips as a mouse's).
    "ac_supplies": ("bq25890-charger",),
    "system_batteries": ("mt6351-battery",),
    # ALSA: the mainline MT6797 AFE + MT6351 codec card; its DL1 PCM ("Playback_1",
    # device 0) feeds the headphone drivers, which also drive the speakers through two
    # external amplifiers. The image routes the codec and switches the amplifiers
    # (rb-gemini-audio watch); hostd only offers speakers/headphones (speakers.py).
    "audio_card_driver": "mt6797-mt6351",
    "audio_pcm": "Playback_1",
    "audio_hook": "/usr/lib/retroboot/gemini/rb-gemini-audio",
    "audio_output_state": "gemini/audio-output",       # under /var/lib/retroboot
    # The emulator's host-stream pre-roll (audio.screamer_options): 120 ms covers the
    # screamer's whole wait for an unserviced sound interrupt (irq-hold-ms, 100), which
    # the slow vCPU reaches when QuickTime starts a sound; QEMU's 40 ms ran ALSA dry
    # there. Strict: a pause between the guest's DMA programs does not cut it short.
    "screamer": {"host-lead-ms": 120, "host-lead-max-ms": 160, "host-lead-strict": True},
    # CPU placement while Mac OS runs (cpuplace.py): QEMU's TCG thread on the A72s
    # (cpu8-9, online about 2 s into boot), QEMU's other threads, the presenter and
    # hostd on the faster Cortex-A53 cluster (cpu4-7, 1352 MHz), the rest of the
    # system and unbound kernel work (the scanout copy's commit worker) off the A72s.
    # max_mhz: the a72dvfs kernel boots the A72s capped at 1495 MHz; while Mac OS runs
    # the cap is lifted to the speed bin's top (clamped to cpuinfo_max_freq; every OPP
    # of this unit's SB-0119 TT table passed the step test on 2026-10-14) and the
    # kernel's 80 °C A72 trip and 85 °C trip throttle sustained load. Older kernels
    # report 1495 MHz as their top, so the write is a no-op there.
    # governor schedutil, not performance (device, 2026-10-14, battery, idle Finder, lid
    # open, 60 s averages): 977 mA / 3.70 W / 51 °C / A72 1346 MHz against 1170 mA /
    # 4.39 W / 58 °C / 2500 MHz, and the Finder 71.8 s after guest start against 78.1 s:
    # schedutil still runs the busy TCG thread at the top, and leaves thermal headroom.
    "cpu": {"vcpu": "8-9", "emulator": "4-7", "host": "4-7", "workqueue": "0-7",
            "governor": "schedutil", "min_mhz": 0, "max_mhz": 3000},
    # Diagnostics' "Power and temperature": the A72 policy's clock, the SoC zone.
    "health_cpus": ("cpu8", "cpu4", "cpu0"),
    "thermal_types": ("soc-thermal", "soc_thermal"),
    # The presenter's Gemini layer (gemkbd.c) makes Alt Command and Fn with Alt/Ctrl
    # Option itself, so input.swap_command_option keeps its default. The recovery
    # chord's Delete is Fn+Backspace there: Control-Alt-Backspace by default (the
    # presenter accepts Ctrl+Alt+Fn+Backspace as well).
    "setting_defaults": {"input.recovery_chord": "ctrl+alt+backspace"},
    # Closing the lid puts Mac OS to sleep (lidsleep.py): the host itself cannot sleep
    # (s2idle hangs the Gemini), Mac OS's Sleep halts the emulated processor.  Meanwhile
    # every cpufreq policy runs at its lowest frequency and the covered keyboard is polled
    # once a second instead of every 30 ms (lidpower.py; -48 and -60 mA on the device).
    # The Hall sensor's state at hostd start comes from its input device (no ACPI lid).
    "lid_action": "sleep",
    "lid_low_power": True,
    "keypad_poll_param": "module/matrix_keypad/parameters/poll_ms",
    "lid_evdev": True,
}

# hostd Config fields that are startup/shutdown deadlines (seconds).  Scaled by
# timeout_scale only while they still have their built-in default, so a value
# set in hostd.json (or by a test) always wins.
SCALED_TIMEOUTS = ("guest_content_timeout", "guest_content_floor", "handoff_deadline", "finder_wait_timeout",
                   "adopt_settled_s", "power_started_after_s", "power_ready_max_s", "power_dialog_wait_s",
                   "power_request_budget_s", "clean_stop_timeout", "qmp_watchdog_timeout_s",
                   "lid_sleep_wait_s", "lid_busy_wait_s")

DMI_JUNK = {"", "to be filled by o.e.m.", "default string", "system product name", "system manufacturer",
            "not applicable", "not specified", "none", "o.e.m.", "oem", "unknown", "type1productconfigid"}


@dataclass(frozen=True)
class Platform:
    family: str = "pc"                 # "raspberrypi" | "gemini" | "pc" (DMI, or nothing) | "other" (another DT board)
    board: str | None = None           # zero2w | pi4 | pi400 | cm4 | cm4s | pi5 | pi500 | cm5 | gemini-pda | None
    model: str | None = None           # "Raspberry Pi 5 Model B Rev 1.0" / "GPD MicroPC" / None
    soc: str | None = None             # BCM2711 | BCM2712 | MT6797
    machine: str = ""                  # uname -m
    ram_mb: int | None = None
    revision: str | None = None        # Pi revision code, hex
    source: str = "none"               # "device-tree" | "dmi" | "none"
    features: dict = field(default_factory=dict)
    image_model: str | None = None       # fixed build profile; also enforced under generic Arm emulation

    @property
    def is_zero2w(self) -> bool:
        return self.board == "zero2w" or self.image_model == "zero2w"

    @property
    def machine_families(self) -> frozenset[str]:
        """512 MB Zero images run only the 68040 Mac, including when moved to another board."""
        return frozenset(("quadra800",)) if self.is_zero2w else frozenset(("powermac", "quadra800"))

    def machine_unavailable(self, family: str) -> str | None:
        if family not in self.machine_families:
            return "The Raspberry Pi Zero 2 W image runs 68040 Macs (Mac OS 7.5.5 and 8.1). Use the Pi 4/5 image for PowerPC Macs."
        return None

    @property
    def is_raspberry_pi(self) -> bool:
        return self.family == "raspberrypi"

    @property
    def pi_family(self) -> str | None:
        """zero2w (BCM2837), pi4 (BCM2711) or pi5 (BCM2712); None elsewhere."""
        if not self.is_raspberry_pi:
            return None
        if self.board in PI_FAMILY:
            return PI_FAMILY[self.board]
        return {"BCM2711": "pi4", "BCM2712": "pi5"}.get(self.soc or "")

    @property
    def is_gemini(self) -> bool:
        return self.family == "gemini"

    @property
    def _speed_class(self) -> str:
        """The key of TIMEOUT_SCALE / LAUNCH_MS: zero2w, pi4, pi5, gemini or "" (PCs)."""
        return "zero2w" if self.is_zero2w else "gemini" if self.is_gemini else \
            (self.pi_family or {"pi4-5": "pi4", "pi4": "pi4", "pi5": "pi5"}.get(self.image_model, ""))

    @property
    def timeout_scale(self) -> float:
        return TIMEOUT_SCALE.get(self._speed_class, 1.0)

    @property
    def launch_ms_default(self) -> float | None:
        return LAUNCH_MS.get(self._speed_class)

    @property
    def profile(self) -> dict:
        """Board-specific knobs (GEMINI_PROFILE on a Gemini PDA); {} elsewhere, so every
        consumer keeps its PC behaviour."""
        return GEMINI_PROFILE if self.is_gemini else {}

    @property
    def setting_defaults(self) -> dict:
        """Settings defaults for this board, below the image's defaults.json."""
        return {**dict(self.profile.get("setting_defaults") or {}),
                **({"machine.quadra_ram_mb": 64} if self.is_zero2w else {})}

    @property
    def tcg_tb_size_mb(self) -> int:
        """QEMU's TCG translation buffer (``-accel tcg,tb-size=``).  Its pages are committed
        as code is translated, so a long session can grow toward the whole buffer beside the
        guest's RAM: 64 MB on the 68040-only Zero 2 W image, 256 MB on a Raspberry Pi
        with 2 GB or less, 128 MB with 1 GB or less; 512 MB elsewhere (PCs unchanged)."""
        if self.is_zero2w:
            return 64
        if not self.is_raspberry_pi or not self.ram_mb:
            return 512
        if self.ram_mb <= 1024:
            return 128
        if self.ram_mb <= 2048:
            return 256
        return 512

    @property
    def small_memory(self) -> bool:
        """A Raspberry Pi with 1 GB or less (the Zero has a separate 68040 budget)."""
        return self.is_raspberry_pi and bool(self.ram_mb) and self.ram_mb <= 1024

    @property
    def has_grub(self) -> bool:
        """The x86 image boots through GRUB (A/B slots, display.cfg); a Pi's firmware
        boots kernel8.img from its FAT partition, a Gemini's LK the Android boot.img
        flashed to its boot partition."""
        return not (self.is_raspberry_pi or self.is_gemini or self.image_model is not None)

    def public(self) -> dict:
        out = {"family": self.family, "board": self.board, "model": self.model, "soc": self.soc,
               "machine": self.machine, "ram_mb": self.ram_mb, "features": dict(self.features)}
        if self.is_raspberry_pi or self.is_gemini:
            out["tcg_tb_size_mb"] = self.tcg_tb_size_mb
        if self.image_model:
            out["image_model"] = self.image_model
        if self.is_zero2w:
            out["machine_families"] = sorted(self.machine_families)
            out["tcg_tb_size_mb"] = self.tcg_tb_size_mb
        return out


def image_profile(plat: Platform, path: Path) -> Platform:
    """Read only a recognized release profile, never mutable machine/settings input.

    The image profile can restrict a host, but cannot turn a physical Zero into a
    PowerPC-capable board. Unknown/malformed metadata leaves detection authoritative.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read(4097)
        if len(raw) > 4096:
            return plat
        profile = json.loads(raw)
        if type(profile) is dict and type(profile.get("schema_version")) is int \
                and profile["schema_version"] == 1 and profile.get("model") in ("pi4", "pi5", "pi4-5", "zero2w"):
            return replace(plat, image_model=profile["model"])
    except (OSError, ValueError):
        pass
    return plat


def _read_bytes(path: Path, limit: int = 4096) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return None


def _dt_string(path: Path) -> str | None:
    raw = _read_bytes(path)
    if raw is None:
        return None
    text = raw.split(b"\0", 1)[0].decode("utf-8", "replace").strip()
    return text or None


def _dt_strings(path: Path) -> list[str]:
    raw = _read_bytes(path)
    if not raw:
        return []
    return [s.decode("utf-8", "replace").strip() for s in raw.split(b"\0") if s.strip()]


def _dt_u32(path: Path) -> int | None:
    raw = _read_bytes(path, 16)
    if not raw or len(raw) < 4:
        return None
    return int.from_bytes(raw[:4], "big")


def _text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return None


def decode_revision(code: int | None) -> dict:
    """A new-style Raspberry Pi revision code -> {board, soc, ram_mb, rev}; {} for old style."""
    if code is None or not code & (1 << 23):
        return {}
    return {"board": PI_TYPES.get((code >> 4) & 0xff), "soc": PI_SOCS.get((code >> 12) & 0xf),
            "ram_mb": PI_MEMORY_MB.get((code >> 20) & 0x7), "rev": code & 0xf}


def _board_from_model(model: str) -> str | None:
    m = model.lower()
    for text, board in (("raspberry pi zero 2", "zero2w"),
                        ("compute module 5", "cm5"), ("raspberry pi 500", "pi500"), ("raspberry pi 5", "pi5"),
                        ("compute module 4s", "cm4s"), ("compute module 4", "cm4"),
                        ("raspberry pi 400", "pi400"), ("raspberry pi 4", "pi4")):
        if text in m:
            return board
    return None


def _mem_total_mb(proc: Path) -> int | None:
    text = _text(proc / "meminfo") or ""
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            try:
                return int(line.split()[1]) // 1024
            except (IndexError, ValueError):
                return None
    return None


def _dmi_model(dmi: Path) -> str | None:
    vendor = _text(dmi / "sys_vendor") or ""
    product = _text(dmi / "product_name") or ""
    if vendor.lower() in DMI_JUNK:
        vendor = ""
    if product.lower() in DMI_JUNK:
        product = ""
    if not product:
        product = _text(dmi / "board_name") or ""
        if product.lower() in DMI_JUNK:
            product = ""
    if vendor and product and product.lower().startswith(vendor.split()[0].lower()):
        vendor = ""          # "GPD" + "GPD MicroPC", "LENOVO" + "LENOVO ..." read better once
    name = " ".join(p for p in (vendor, product) if p).strip()
    return name[:80] or None


def pi_features(board: str | None, family: str | None) -> dict:
    """What a Raspberry Pi board has (the sysfs devices stay authoritative: a DSI
    display brings a backlight, a USB sound card a mixer)."""
    return {"battery": False, "lid": False, "backlight": False,
            # Pi 5 / 500 / CM5: the PMIC's power button is gpio-keys "pwr_button"
            # (KEY_POWER; a long press is a hard power-off).  Pi 4 has none.
            "power_button": family == "pi5",
            # Pi 5 family: RTC (rpi-rtc), kept only with the optional battery.
            "rtc": family == "pi5",
            # Only the Pi 4 Model B has the 3.5 mm jack (bcm2835 Headphones).
            "analog_audio": board == "pi4",
            "grub": False, "dmi": False}


def gemini_features() -> dict:
    """What a Gemini PDA has (gemini-nixos DTS; sysfs stays authoritative)."""
    return {"battery": True,            # mt6351-battery (fuel gauge) + bq25890 charger
            "lid": True,                # Hall sensor, gpio-keys-polled SW_LID
            "backlight": True,          # pwm-backlight "backlight"
            "power_button": True,       # Esc/On held 0.8 s: KEY_POWER (mt6351-keys); a tap is Escape
            "rtc": True,                # MT6351 RTC
            "analog_audio": True,       # speakers + 3.5 mm jack, no jack detection
            "speaker_switch": True,     # speakers or headphones only (the image's rb-gemini-audio)
            "keyboard": "gemini",       # AW9523B matrix, Fn = KEY_RIGHTALT (presenter gemkbd.c)
            "touchscreen": True,        # the only pointing device
            "bluetooth": True,          # MT6630 hci_stp
            "wifi": "networkmanager",   # MT6630 wlan0 (CONSYS)
            "grub": False, "dmi": False}


def _uname_machine() -> str:
    try:
        return os.uname().machine
    except (AttributeError, OSError):
        return ""


def detect(device_tree: str | Path = DEVICE_TREE, dmi: str | Path = DMI, proc: str | Path = "/proc",
           machine: str | None = None) -> Platform:
    """Never raises; see the module docstring."""
    dt, dmi, proc = Path(device_tree), Path(dmi), Path(proc)
    mach = machine if machine is not None else _uname_machine()
    try:
        model = _dt_string(dt / "model")
        compatible = _dt_strings(dt / "compatible")
        is_pi = bool(model and model.lower().startswith("raspberry pi")) or any(
            c.startswith("raspberrypi,") for c in compatible)
        if is_pi:
            rev = _dt_u32(dt / "system" / "linux,revision")
            info = decode_revision(rev)
            board = next((b for c, b in PI_COMPATIBLE if c in compatible), None) or info.get("board") \
                or _board_from_model(model or "")
            soc = info.get("soc") or ("BCM2712" if any(c.startswith("brcm,bcm2712") for c in compatible) else
                                      "BCM2711" if any(c.startswith("brcm,bcm2711") for c in compatible) else
                                      "BCM2837" if board == "zero2w" else None)
            ram = info.get("ram_mb") or _mem_total_mb(proc)
            p = Platform(family="raspberrypi", board=board, model=model or "Raspberry Pi", soc=soc, machine=mach,
                         ram_mb=ram, revision=f"{rev:06x}" if rev is not None else None, source="device-tree")
            return replace(p, features=pi_features(board, p.pi_family))
        if GEMINI_COMPATIBLE in compatible:
            soc = next((c.split(",", 1)[1].upper() for c in compatible if c.startswith("mediatek,")), None)
            return Platform(family="gemini", board="gemini-pda", model=model or "Planet Computers Gemini PDA",
                            soc=soc, machine=mach, ram_mb=_mem_total_mb(proc), source="device-tree",
                            features=gemini_features())
        if (dmi / "sys_vendor").exists() or (dmi / "product_name").exists():
            return Platform(family="pc", model=_dmi_model(dmi), machine=mach, ram_mb=_mem_total_mb(proc),
                            source="dmi", features={"dmi": True, "grub": True})
        if model:
            # Another device-tree board (QEMU's arm64 "virt" says "linux,dummy-virt"): not a
            # supported target; hostd keeps the PC defaults for everything.
            label = "Virtual Arm computer" if "linux,dummy-virt" in compatible or model == "linux,dummy-virt" else model
            return Platform(family="other", model=label, machine=mach, ram_mb=_mem_total_mb(proc),
                            source="device-tree", features={"dmi": False, "grub": True})
        return Platform(family="pc", model=None, machine=mach, ram_mb=_mem_total_mb(proc), source="none",
                        features={"dmi": False, "grub": True})
    except Exception:   # noqa: BLE001 -- detection must never keep hostd from starting
        return Platform(machine=mach)


def scale_timeouts(cfg, plat: Platform) -> dict:
    """Stretch hostd's startup/shutdown deadlines on a slow board (in place).  Only
    fields still at their built-in default change.  Returns {field: (old, new)}."""
    scale = plat.timeout_scale
    if scale == 1.0:
        return {}
    defaults = type(cfg)()
    known = {f.name for f in fields(cfg)}
    changed = {}
    for name in SCALED_TIMEOUTS:
        if name not in known:
            continue
        cur, default = getattr(cfg, name), getattr(defaults, name)
        if isinstance(cur, (int, float)) and not isinstance(cur, bool) and cur == default and cur > 0:
            new = round(float(cur) * scale, 1)
            setattr(cfg, name, new)
            changed[name] = (cur, new)
    return changed


HOT_C = 80.0          # the Pi 4 and Pi 5 firmware start slowing the CPU at 80 °C (Gemini: 85 °C trip)
CAPPED = 0.95         # running below 95 % of the top speed while hot or short of power: slowed


class Health:
    """A Raspberry Pi's (or Gemini PDA's) power supply and temperature for Diagnostics
    ("Power and temperature") and ``GET /api/v1/status`` ``host.throttled``, from sysfs only:

    * under-voltage now: the ``rpi_volt`` hwmon device (module raspberrypi-hwmon,
      autoloaded) ``in0_lcrit_alarm``.  The driver clears the firmware's sticky bits
      every 2 s, so "since start" is remembered here: every :meth:`sample` (status,
      Diagnostics and hostd's periodic check) that sees it sets ``under_voltage_seen``;
    * SoC temperature: ``class/thermal/thermal_zone0/temp`` (millidegrees);
    * CPU speed: cpu0's ``scaling_cur_freq`` against ``cpuinfo_max_freq``; "slowed"
      when a lower ``scaling_max_freq`` caps it, or when it runs below 95 % of the top
      speed while hot (80 °C) or short of power.  A lower speed alone is the governor
      idling, not throttling.

    (The firmware's ``get_throttled`` sysfs file is gone from Raspberry Pi kernels
    6.18.)  Every read is a few small files; a missing one leaves its part out.

    ``cpus`` and ``thermal_types`` pick another CPU and zone: on a Gemini PDA the
    A72 cluster that runs Mac OS (cpu8, cpu4 while it is offline) and the
    ``soc-thermal`` zone (the hottest of the SoC's sensors, 85 °C trip); it has no
    under-voltage alarm, so no power verdict."""

    def __init__(self, sysfs: str | Path = "/sys", cpus: tuple = ("cpu0",), thermal_types: tuple = ()):
        self.sysfs = Path(sysfs)
        self.cpus = tuple(cpus) or ("cpu0",)
        self.thermal_types = tuple(thermal_types)
        self.under_voltage_seen = False

    def _thermal_zone(self) -> str:
        """sysfs-relative path of the zone to read: the first one whose type is in
        thermal_types, else thermal_zone0."""
        if self.thermal_types:
            try:
                zones = sorted((self.sysfs / "class" / "thermal").glob("thermal_zone*"))
            except OSError:
                zones = []
            for z in zones:
                if _text(z / "type") in self.thermal_types:
                    return f"class/thermal/{z.name}/temp"
        return "class/thermal/thermal_zone0/temp"

    def _cpufreq(self) -> Path:
        for cpu in self.cpus:
            rel = Path("devices/system/cpu") / cpu / "cpufreq"
            if (self.sysfs / rel / "scaling_cur_freq").exists():
                return rel
        return Path("devices/system/cpu") / self.cpus[-1] / "cpufreq"

    def _int(self, rel: str | Path) -> int | None:
        v = _text(self.sysfs / rel)
        try:
            return int(v) if v is not None else None
        except ValueError:
            return None

    def _under_voltage(self) -> bool | None:
        try:
            devices = sorted((self.sysfs / "class" / "hwmon").glob("hwmon*"))
        except OSError:
            return None
        for d in devices:
            if _text(d / "name") == "rpi_volt":
                v = _text(d / "in0_lcrit_alarm")
                return None if v not in ("0", "1") else v == "1"
        return None

    def sample(self) -> dict:
        uv = self._under_voltage()
        if uv:
            self.under_voltage_seen = True
        milli = self._int(self._thermal_zone())
        temp = round(milli / 1000.0, 1) if milli is not None else None
        cpu = self._cpufreq()
        cur, top, limit = (self._int(cpu / n) for n in ("scaling_cur_freq", "cpuinfo_max_freq", "scaling_max_freq"))
        top = top or limit
        capped = False
        if cur and top:
            limited = bool(limit and limit < top * CAPPED)
            stressed = (temp is not None and temp >= HOT_C) or bool(uv)
            capped = limited or (cur < top * CAPPED and stressed)
        out = {"under_voltage": uv, "under_voltage_seen": self.under_voltage_seen, "temp_c": temp,
               "cpu_mhz": cur // 1000 if cur else None, "max_mhz": top // 1000 if top else None,
               "capped": capped}
        out["text"] = self.describe(out)
        return out

    @staticmethod
    def describe(h: dict) -> str | None:
        """Short, classic-panel friendly: "62 °C, 1.8 GHz of 1.8 GHz, power OK",
        "Under-voltage detected (now), …", "CPU slowed to 1.0 GHz (temperature 82 °C), …";
        None when nothing could be read."""
        ghz = lambda mhz: f"{mhz / 1000:.1f} GHz"   # noqa: E731
        temp = f"{h['temp_c']:.0f} °C" if h.get("temp_c") is not None else None
        parts = []
        if h.get("under_voltage"):
            parts.append("Under-voltage detected (now)")
        elif h.get("under_voltage_seen"):
            parts.append("Under-voltage detected (since start)")
        if h.get("capped") and h.get("cpu_mhz"):
            parts.append(f"CPU slowed to {ghz(h['cpu_mhz'])}" + (f" (temperature {temp})" if temp else ""))
        else:
            if temp:
                parts.append(temp)
            if h.get("cpu_mhz") and h.get("max_mhz"):
                parts.append(f"{ghz(h['cpu_mhz'])} of {ghz(h['max_mhz'])}")
        if h.get("under_voltage") is False and not h.get("under_voltage_seen"):
            parts.append("power OK")
        return ", ".join(parts) or None


def health_for(plat: Platform, sysfs: str | Path = "/sys") -> Health | None:
    """The Health reader for this board (Raspberry Pi, Gemini PDA); None on a PC."""
    if plat.is_raspberry_pi:
        return Health(sysfs)
    if plat.is_gemini:
        return Health(sysfs, cpus=plat.profile["health_cpus"], thermal_types=plat.profile["thermal_types"])
    return None


def boot_partition_paths(root: str | Path = "/") -> list[Path]:
    """Where a Raspberry Pi image mounts its FAT boot partition (Debian/Raspberry Pi
    OS: /boot/firmware)."""
    r = Path(root)
    return [r / "boot" / "firmware"]



def report(device_tree: str | Path = DEVICE_TREE, dmi: str | Path = DMI) -> dict:
    """What hostd would decide on this computer (read-only; ``python3 -m retroboot.platform``)."""
    from . import audio
    from .config import Config
    plat = detect(device_tree, dmi)
    cfg = Config()
    out: dict = {"platform": plat.public(), "revision": plat.revision, "source": plat.source,
                 "timeout_scale": plat.timeout_scale, "scaled_timeouts": {k: v[1] for k, v in
                                                                         scale_timeouts(cfg, plat).items()},
                 "software_update": ("Pi tryboot A/B" if plat.is_raspberry_pi else
                                     (f"unavailable ({plat.family} test build)" if not plat.has_grub else "GRUB A/B")),
                 "tcg_tb_size_mb": plat.tcg_tb_size_mb,
                 "throttled": health_for(plat).sample() if health_for(plat) else None, "boot_network_conf": None}
    if plat.is_raspberry_pi:
        p = Path(cfg.boot_network_conf)
        try:
            present = p.is_file()
        except OSError:          # an automount without its partition (the live installer)
            present = False
        out["boot_network_conf"] = {"path": str(p), "present": present}
    try:
        outputs = audio.playback_outputs()
        if any(o.get("vc4") for o in outputs):
            outputs = audio.mark_vc4_ports(outputs, None)
        out["audio_outputs"] = outputs
        out["audio_choice"] = audio.choose(prefer="pi" if plat.is_raspberry_pi else
                                           "gemini" if plat.is_gemini else "hdmi")
    except Exception as exc:  # noqa: BLE001
        out["audio_error"] = f"{type(exc).__name__}: {exc}"
    if plat.is_gemini:
        from . import cpuplace
        out["setting_defaults"] = plat.setting_defaults
        try:
            out["cpu_placement"] = cpuplace.describe(cpuplace.resolve(cfg, plat))
        except Exception as exc:  # noqa: BLE001
            out["cpu_placement_error"] = f"{type(exc).__name__}: {exc}"
    return out


if __name__ == "__main__":  # pragma: no cover - hardware check: python3 -m retroboot.platform
    import json
    import sys
    print(json.dumps(report(*sys.argv[1:3]), indent=1, default=str))
