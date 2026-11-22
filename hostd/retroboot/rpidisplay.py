"""Raspberry Pi display presets shared by image authoring, the panel and web API.

The FAT include is the desired setting. A local/web selection is tried for one
start: hostd restores the preceding preset before launching Mac OS, and Keep
Display makes the new preset permanent. A subsequent power cycle therefore
recovers a display that cannot show the confirmation. An overlay that prevents
hostd itself from starting still needs the card's documented offline recovery.

Only fixed firmware directives are emitted; neither API accepts paths or text.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .util import atomic_write_bytes, atomic_write_json, read_json

INCLUDE = "include retroboot/display.txt"
RELATIVE = Path("retroboot/display.txt")
MARKER = "# RetroBoot display profile: "
LIMIT = 8192
DSI_BOARDS = {"pi4", "cm4", "cm4s", "pi5", "cm5"}
GPIO_BOARDS = DSI_BOARDS | {"zero2w", "pi-zero2w", "pi-zero-2-w", "pi02w"}
MODELS = ("pi4-5", "pi4", "pi5", "zero2w")


class DisplayError(ValueError):
    def __init__(self, message: str, code: str = "invalid-display"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    interface: str = "auto"
    overlay: str | None = None
    note: str = ""

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "interface": self.interface, "note": self.note}


PROFILES = (
    Profile("auto", "Automatic (HDMI or official DSI)", note="Detects official Raspberry Pi touch displays."),
    Profile("hdmi", "HDMI", "hdmi", note="Disables DSI display detection."),
    Profile("dsi-touch", "Raspberry Pi Touch Display (7-inch)", "dsi", "vc4-kms-dsi-7inch",
            "800 × 480. On Pi 5, connect to CAM/DISP1."),
    Profile("dsi-touch2-5", "Raspberry Pi Touch Display 2 (5-inch)", "dsi", "vc4-kms-dsi-ili9881-5inch",
            "720 × 1280. On Pi 5, connect to CAM/DISP1. Set Rotation for landscape."),
    Profile("dsi-touch2-7", "Raspberry Pi Touch Display 2 (7-inch)", "dsi", "vc4-kms-dsi-ili9881-7inch",
            "720 × 1280. On Pi 5, connect to CAM/DISP1. Set Rotation for landscape."),
    Profile("hyperpixel4", "Pimoroni HyperPixel 4.0", "dpi", "vc4-kms-dpi-hyperpixel4",
            "Starts in landscape; use Rotation to change it. Uses GPIO; disables GPIO serial, I2C, SPI and analogue audio."),
    Profile("hyperpixel4-square", "Pimoroni HyperPixel 4.0 Square", "dpi", "vc4-kms-dpi-hyperpixel4sq",
            "720 × 720. Touch requires a newer Square board marked HyperPixel XP. Uses the GPIO header."),
)
BY_ID = {p.id: p for p in PROFILES}


def render(profile: str) -> str:
    if not isinstance(profile, str) or profile not in BY_ID:
        raise DisplayError("Choose a display from the list.")
    p = BY_ID[profile]
    lines = [MARKER + p.id, "# Select a preset in Display & Sound, or copy a file from retroboot/displays/.",
             "[all]", "display_auto_detect=" + ("1" if p.id == "auto" else "0")]
    if p.interface == "dpi":
        # Empty dtoverlay ends vc4's parameter scope: audio=off must target the
        # base tree, not vc4's HDMI audio. GPIO14/15 belong to the DPI bus.
        lines += ["dtoverlay=", "dtparam=audio=off", "dtparam=i2c_arm=off", "dtparam=spi=off", "enable_uart=0"]
    if p.overlay:
        lines.append("dtoverlay=" + p.overlay + (",rotate=90" if p.id == "hyperpixel4" else ""))
    lines += ["[all]", ""]
    return "\n".join(lines)


def identify(text: str) -> str | None:
    # CRLF is normal when the FAT file has been edited on Windows. No custom
    # lines are silently discarded or treated as a managed preset.
    normal = text.replace("\r\n", "\n")
    return next((p.id for p in PROFILES if normal == render(p.id)), None)


def for_board(board: str | None) -> list[Profile]:
    return [p for p in PROFILES if p.interface in ("auto", "hdmi") or
            (p.interface == "dsi" and board in DSI_BOARDS) or
            (p.interface == "dpi" and board in GPIO_BOARDS)]


def for_model(model: str) -> list[Profile]:
    if model not in MODELS:
        raise DisplayError("Unknown Raspberry Pi image model.")
    return for_board("pi4" if model == "pi4-5" else model)


def setup_text(model: str) -> str:
    zero = model == "zero2w"
    profiles = for_model(model)
    return ("RetroBoot display setup\n\n"
            "HDMI works without changing files. " + ("Pi Zero 2 W has no DSI connector.\n" if zero else
            "Official Raspberry Pi touch displays are detected automatically.\n") +
            "\nBefore the first start, open this card on your computer. In retroboot/displays/,\n"
            "copy the file for your display to retroboot/display.txt, replacing that file.\n"
            "Keep the filename display.txt (not display.txt.txt). Eject the card safely.\n"
            "\nPresets on this image:\n" +
            "".join(f"  {p.id}.txt: {p.label}\n" for p in profiles) +
            "\nAfter setup, open RetroBoot with Control-Alt-Delete, then Display & Sound →\n"
            "Pi Display. Choose a display, click Try Next Start, then Restart Computer.\n"
            "After restart, click Keep Display. Until then, a further power cycle returns\n"
            "to the preceding preset. If startup stops before RetroBoot opens, turn off,\n"
            "move the card to another computer and copy auto.txt to retroboot/display.txt.\n"
            "\nFor manual card edits there is no trial or automatic reversion. Recover by\n"
            "copying auto.txt or hdmi.txt back to retroboot/display.txt.\n"
            "\nOn Pi 5 connect an explicitly selected DSI display to CAM/DISP1. Automatic\n"
            "detection can be used with either connector. Pi 4 uses its DSI display port.\n"
            "Touch Display 2 5/7-inch uses a different cable on Pi 5 (22-way to 15-way).\n"
            "Turn power off before connecting a display.\n"
            "\nHyperPixel 4.0 and Square use the 40-pin GPIO header. Remove other GPIO\n"
            "accessories first. Their presets disable GPIO serial, I2C, SPI and analogue\n"
            "audio. HDMI and USB sound remain available. Older pre-2021 Square boards\n"
            "need a different touch driver and are not supported by the Square preset.\n"
            "Use Rotation on the Monitor page for landscape on portrait panels.\n"
            "\nThese presets configure the included display drivers. Emulator tests cannot\n"
            "verify real display wiring, picture, touch, brightness or sound.\n")


def stage(bootfs: Path, model: str, overlay_dir: Path) -> None:
    profiles = for_model(model)
    missing = [p.overlay + ".dtbo" for p in profiles if p.overlay and not (overlay_dir / (p.overlay + ".dtbo")).is_file()]
    if missing:
        raise DisplayError("Display overlays are missing: " + ", ".join(missing))
    # Validate all inputs before creating any output.
    for p in profiles:
        atomic_write_bytes(bootfs / "retroboot/displays" / (p.id + ".txt"), render(p.id).encode())
    atomic_write_bytes(bootfs / RELATIVE, render("auto").encode())
    atomic_write_bytes(bootfs / "DISPLAY-SETUP.txt", setup_text(model).encode())
    atomic_write_json(bootfs / "retroboot/display-profiles.json",
                      {"schema": 1, "model": model, "profiles": [p.public() for p in profiles]})


def _read(path: Path) -> str:
    if path.is_symlink():
        raise DisplayError("The display setup file must be a regular file.")
    with path.open("rb") as f:
        raw = f.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise DisplayError("The display setup file is too large.")
    return raw.decode("utf-8")


class PiDisplay:
    """One shared, revision-checked authority for the panel and browser."""
    def __init__(self, app):
        self.app = app
        self.boot = Path(app.cfg.pi_boot_dir)
        self.file = self.boot / RELATIVE
        self.state_file = app.paths.state / "pi-display.json"
        self.state: dict = {}
        self.error: str | None = None
        self._captured = False

    @property
    def board(self) -> str | None:
        # The image marker also makes the picker testable on QEMU virt, whose
        # device tree correctly identifies a virtual Arm board instead of a Pi.
        platform = self.app.platform
        image_model = getattr(platform, "image_model", None)
        return platform.board or ({"pi4-5": "pi4", "pi4": "pi4", "pi5": "pi5", "zero2w": "zero2w"}.get(image_model))

    @property
    def enabled(self) -> bool:
        return bool((self.app.platform.is_raspberry_pi or getattr(self.app.platform, "image_model", None))
                    and self.board in GPIO_BOARDS)

    def _config(self) -> str:
        if self.boot.is_symlink() or (self.boot / "retroboot").is_symlink():
            raise DisplayError("The display setup folder must be on the startup card.")
        cfg = _read(self.boot / "config.txt")
        active, included = True, False
        for raw in cfg.splitlines():
            line = raw.split("#", 1)[0].strip()
            if line.startswith("["):
                active = line.lower() == "[all]"
            if active and line == INCLUDE:
                included = True
        if not included:
            raise DisplayError("This image does not have display presets. Install a current Raspberry Pi image.", "display-unavailable")
        return cfg

    def _overlays(self) -> Path:
        prefix, active = "", True
        for raw in self._config().splitlines():
            line = raw.split("#", 1)[0].strip()
            if line.startswith("["):
                active = line.lower() == "[all]"
            if active and line.startswith("os_prefix="):
                prefix = line.split("=", 1)[1].strip()
        if prefix.startswith("/") or ".." in Path(prefix).parts:
            raise DisplayError("The startup card has an unsupported display configuration.")
        path = self.boot / prefix / "overlays"
        if path.is_symlink():
            raise DisplayError("The display overlay folder must be on the startup card.")
        return path

    def _desired(self) -> tuple[str, str | None, str]:
        self._config()
        text = _read(self.file)
        # A baseline file repeats after every trial recovery. Bind the token to
        # the kernel boot and journal too, so an old queued confirmation cannot
        # accept a later trial that restored those same baseline bytes.
        context = {key: self.state.get(key) for key in ("boot_id", "active", "trial", "pending", "previous")}
        token = json.dumps(context, sort_keys=True, separators=(",", ":")).encode() + b"\0" + text.encode()
        return text, identify(text), hashlib.sha256(token).hexdigest()

    def _write(self, profile: str) -> None:
        self._config()
        if self.file.is_symlink():
            raise DisplayError("The display setup file must be a regular file.")
        atomic_write_bytes(self.file, render(profile).encode())

    def _persist(self):
        atomic_write_json(self.state_file, self.state)

    def capture_boot(self):
        if self._captured or not self.enabled:
            return
        self._captured = True
        try:
            _, profile, _ = self._desired()
            boot_id = _read(Path(self.app.cfg.proc_path) / "sys/kernel/random/boot_id").strip()
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", boot_id):
                raise DisplayError("The current startup could not be identified.")
            try:
                old = read_json(self.state_file, LIMIT)
                if not isinstance(old, dict):
                    old = {}
            except (OSError, ValueError):
                old = {}
            if old.get("boot_id") == boot_id:
                self.state = old
                # Finish a recovery write interrupted before hostd restarted.
                if old.get("trial") == profile and old.get("previous") in BY_ID:
                    self._write(old["previous"])
                return
            pending = old.get("pending")
            if pending in BY_ID and profile == pending and old.get("previous") in BY_ID:
                self.state = {"boot_id": boot_id, "active": profile, "trial": profile, "previous": old["previous"]}
                self._persist()
                self._write(old["previous"])
            else:
                self.state = {"boot_id": boot_id, "active": profile}
                self._persist()
        except (OSError, ValueError, UnicodeError) as exc:
            self.error = str(exc) if isinstance(exc, DisplayError) else "The startup card's display settings could not be read."

    def public(self) -> dict:
        if not self.enabled:
            return {"available": False}
        self.capture_boot()
        try:
            _, desired, revision = self._desired()
            overlays = self._overlays()
            choices = [p.public() for p in for_board(self.board)
                       if p.overlay is None or (overlays / (p.overlay + ".dtbo")).is_file()]
            return {"available": self.error is None, "message": self.error,
                    "profile": desired, "active": self.state.get("active"), "revision": revision,
                    "trial": self.state.get("trial"), "pending": self.state.get("pending"),
                    "restart_required": desired != self.state.get("active") and not self.state.get("trial"),
                    "custom": desired is None, "profiles": choices}
        except (OSError, ValueError, UnicodeError):
            return {"available": False, "message": self.error or "Display presets are unavailable on this startup card."}

    def select(self, profile, revision) -> dict:
        if not self.enabled:
            raise DisplayError("Display presets are available on Raspberry Pi images.", "display-unavailable")
        status = self.public()
        if not status.get("available"):
            raise DisplayError(status.get("message") or "Display presets are unavailable.", "display-unavailable")
        if not isinstance(revision, str) or revision != status["revision"]:
            raise DisplayError("The display was changed elsewhere. Reopen the display settings.", "stale-revision")
        if not isinstance(profile, str) or profile not in {p["id"] for p in status["profiles"]}:
            raise DisplayError("Choose a display supported by this Raspberry Pi image.")
        if status.get("trial"):
            raise DisplayError("Keep the display or use the previous display before choosing another.", "display-trial")
        if status.get("custom"):
            raise DisplayError("The startup card has custom display settings. Restore auto.txt before using the display picker.")
        if profile == status["profile"]:
            return status
        # The known-active preset, not a previous pending candidate, is the
        # recovery target. Refuse an unknown baseline rather than replacing it.
        previous = self.state.get("active")
        if previous not in BY_ID:
            raise DisplayError("Restart RetroBoot before changing the display.")
        before = dict(self.state)
        try:
            self.state.update(pending=profile, previous=previous)
            self._persist()                  # journal before the FAT write
            self._write(profile)
        except (OSError, ValueError):
            self.state = before
            self._persist()
            raise
        self.app.lifecycle.event("pi_display_selected", profile=profile, previous=previous)
        return self.public()

    def confirm(self, keep: bool, revision) -> dict:
        status = self.public()
        if not self.enabled or not status.get("available") or not self.state.get("trial"):
            raise DisplayError("There is no display waiting to be kept.", "display-trial")
        if not isinstance(revision, str) or revision != status["revision"]:
            raise DisplayError("The display was changed elsewhere. Reopen the display settings.", "stale-revision")
        chosen = self.state["trial"] if keep else self.state["previous"]
        self._write(chosen)
        self.state.pop("trial", None)
        self.state.pop("previous", None)
        self._persist()
        self.app.lifecycle.event("pi_display_confirmed", profile=chosen, keep=keep)
        return self.public()
