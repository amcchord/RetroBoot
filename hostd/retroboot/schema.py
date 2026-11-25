"""Versioned settings schema (ARCHITECTURE section 5).

One schema is shared by the local classic panel, the web API, the supervisor
and the host adapters.  Each setting declares its type, default, allowed
values, apply policy, capability prerequisite, timed-confirmation seconds and
user-facing label/help.  Validation is pure and side-effect free; dynamic
choices (registered machines, the presenter's mode list) arrive through a
``ValidationContext``.
"""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from . import display as dpolicy
from .webproxy import DEFAULT_BYPASS_HOSTS, DEFAULT_REDIRECTS, SEARCH_CHOICES, SEARCH_LABELS

SCHEMA_VERSION = 1

LIVE = "live"
GUEST_RESTART = "guest_restart"
HOST_REBOOT = "host_reboot"
APPLY_POLICIES = (LIVE, GUEST_RESTART, HOST_REBOOT)

GUEST_SIZES = ("auto", "640x480", "800x600", "1024x768", "1152x870", "1280x1024")
ROTATIONS = ("auto", "0", "90", "180", "270")
ROTATION_LABELS = {"auto": "Automatic", "0": "Standard", "90": "90° clockwise", "180": "180°",
                   "270": "90° counterclockwise"}
PIXEL_SCALES = ("auto", "1", "2", "3", "4")
RECOVERY_CHORDS = ("ctrl+alt+delete", "ctrl+alt+backspace", "ctrl+alt+escape")
CHIME_CHOICES = ("auto", "os9", "quadra", "powermac", "macii", "se", "off")
CHIME_LABELS = {"auto": "Automatic (by Mac model)", "os9": "Mac OS 9", "quadra": "Quadra",
                "powermac": "Power Mac", "macii": "Mac II", "se": "Macintosh SE", "off": "Off"}
GAMEPAD_MODES = ("arrows", "wasd", "off")
GAMEPAD_MODE_LABELS = {"arrows": "Arrow keys", "wasd": "WASD", "off": "Off"}
AUDIO_OUTPUT_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}\Z")
AUDIO_OUTPUT_LABELS = {"auto": "Automatic", "speakers": "Speakers", "analog": "Analog",
                       "hdmi": "HDMI", "usb": "USB Audio", "bluetooth": "Bluetooth"}
HOSTNAME_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
VOLUME_NAME_RE = re.compile(r"^[A-Za-z0-9 _-]{1,27}$")
UPDATE_URL_RE = re.compile(r"^(|https://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?(/[A-Za-z0-9._~%/+=&?:@!$,;-]{0,1900})?)$")
MACHINE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
HOST_MODE_RE = re.compile(r"^(\d{3,5})x(\d{3,5})@(\d{1,3})$")
TIMEZONE_RE = re.compile(r"^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}$")
MEDIA_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+,'&!-]{0,126}$")   # media.py library names
REDIRECT_HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
REDIRECT_URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?(/[^\s]*)?$")
PRINTER_TARGET_RE = re.compile(r"^(pdf|cups:[A-Za-z0-9][A-Za-z0-9_-]{0,47})\Z")
MAX_REDIRECTS = 64
MAX_BYPASS_HOSTS = 128
MAX_IMAGE_SERVERS = 8
MOUSE_DPI_MIN = 100
MOUSE_DPI_MAX = 100000
# A 68k Mac's memory (quadra.py; machine.quadra_ram_mb).  The emulator workstream measures
# what Mac OS 7.5.5 and 8.1 use well; extend the choices here.
QUADRA_RAM_CHOICES = (64, 128, 256)
QUADRA_RAM_DEFAULT = 128
# Settings that apply to one kind of Mac only (Setting.families): the web page and the panel
# show them for the startup Mac's family; the values stay for the other family's Macs.
POWERMAC_ONLY = ("powermac",)
QUADRA_ONLY = ("quadra800",)

_TZ_CACHE: set[str] | None = None


def available_timezones() -> set[str]:
    """IANA zone names from the system tz database (the list timedatectl offers)."""
    global _TZ_CACHE
    if _TZ_CACHE is None:
        zones: set[str] = set()
        try:
            import zoneinfo
            zones = {z for z in zoneinfo.available_timezones()
                     if not z.startswith(("posix/", "right/")) and z not in ("Factory", "localtime")}
        except Exception:
            zones = set()
        zones.add("UTC")
        _TZ_CACHE = zones
    return _TZ_CACHE


TZ_REGIONS = ("Africa", "America", "Antarctica", "Arctic", "Asia", "Atlantic", "Australia", "Europe", "Indian",
              "Pacific")


def listed_timezones(zoneinfo_dir: Path | None = None) -> set[str]:
    """The zones the classic panel lists: tzdata's per-country tables (zone.tab, zone1970.tab)
    plus UTC, without backward-compatible aliases such as "ROK", "UCT", "US/Eastern" or
    "Etc/GMT+5".  Every name in available_timezones() stays valid as a setting."""
    every = available_timezones()
    listed: set[str] = set()
    for base in ([zoneinfo_dir] if zoneinfo_dir else []) + [Path(p) for p in _tzpath()]:
        for name in ("zone.tab", "zone1970.tab"):
            try:
                text = (base / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                cols = line.split("\t")
                if line and not line.startswith("#") and len(cols) >= 3:
                    listed.add(cols[2].strip())
        if listed:
            break
    listed &= every
    if not listed:
        listed = {z for z in every if z.split("/", 1)[0] in TZ_REGIONS}
    return listed | {"UTC"}


def _tzpath() -> tuple[str, ...]:
    try:
        import zoneinfo
        return tuple(zoneinfo.TZPATH) or ("/usr/share/zoneinfo",)
    except Exception:
        return ("/usr/share/zoneinfo",)


def timezone_regions(zones: set[str] | None = None) -> dict[str, list[str]]:
    """Group zones by region for the classic panel (Etc/UTC-style zones under "Other")."""
    out: dict[str, list[str]] = {}
    for z in sorted(zones or available_timezones()):
        region = z.split("/", 1)[0] if "/" in z else "Other"
        if region == "Etc":
            region = "Other"
        out.setdefault(region, []).append(z)
    return out


@dataclass
class ValidationContext:
    """Dynamic facts used for validation of a candidate configuration."""

    machines: set[str] | None = None            # None = do not check membership
    machine_names: dict[str, str] | None = None
    timezones: set[str] | None = None           # None = system tz database
    host_modes: list[str] | None = None         # "WxH@R" strings; None = do not check
    capabilities: dict[str, bool] = field(default_factory=dict)
    check_capabilities: bool = True
    media_cds: dict[str, str] | None = None     # CD images in the media library: name -> label
    display_mode: tuple[int, int] | None = None  # presenter's current mode; None = do not check pixel scales
    values: dict | None = None                  # desired values, for notes on related settings
    # 3D acceleration (graphics.py availability): {"available", "reason", "message",
    # "ati_screen": [w, h] | None (the Mac screen with the card)}; None = do not check
    graphics: dict | None = None
    machine_families: dict[str, str] | None = None   # machine id -> family (machines.FAMILIES)
    quadra_ram_max_mb: int | None = None            # image/board budget (Zero 2 W: 128)
    machine_guest_os: dict[str, str] | None = None   # exact selected operating-system profile
    audio_outputs: dict[str, str] | None = None      # available audio outputs: id -> label


@dataclass(frozen=True)
class Setting:
    key: str
    type: str                      # enum | bool | int | float | string | machine | host_mode | audio_output
    default: Any
    apply: str
    label: str
    help: str
    group: str
    choices: tuple | None = None
    choice_labels: dict | None = None
    range: tuple | None = None
    capability: str | None = None
    confirm: int | None = None
    pattern: str | None = None
    nullable: bool = False
    step: float | None = None
    hidden_choices: tuple = ()     # still valid (stored values, API) but never offered to people
    families: tuple | None = None  # the kinds of Mac it applies to (None: every Mac)

    def describe(self, ctx: ValidationContext | None = None) -> dict:
        """Wire form for GET /api/v1/settings (dynamic kinds are presented as enums)."""
        wire_type = {"machine": "enum", "host_mode": "enum", "timezone": "enum", "media": "enum",
                     "audio_output": "enum", "float": "number", "pixel_scale": "enum",
                     "redirects": "list", "bypass_hosts": "list", "catalog_urls": "list"}.get(self.type, self.type)
        d: dict[str, Any] = {
            "key": self.key, "type": wire_type, "kind": self.type, "default": self.default,
            "apply": self.apply, "capability": self.capability,
            "confirm": self.confirm, "label": self.label, "help": self.help,
            "group": GROUP_TITLES.get(self.group, self.group.title()), "group_id": self.group,
            "nullable": self.nullable,
        }
        if self.families is not None:
            d["families"] = list(self.families)
        choices = self.dynamic_choices(ctx)
        if self.type == "pixel_scale":
            d["choices"] = pixel_scale_choices(ctx)
        elif choices is not None:
            labels = dict(self.choice_labels or {})
            if self.type == "audio_output":
                labels = {**AUDIO_OUTPUT_LABELS, **labels}
            if ctx is not None and self.type == "machine":
                labels.update(ctx.machine_names or {})
            if ctx is not None and self.type == "media":
                labels.update(ctx.media_cds or {})
            if ctx is not None and self.type == "audio_output" and ctx.audio_outputs is not None:
                labels.update(ctx.audio_outputs)
            d["choices"] = [{"value": c, "label": labels.get(c, choice_label(self.type, c))} for c in choices]
            if ctx is not None and self.type == "machine" and ctx.machine_families is not None:
                for c in d["choices"]:
                    c["family"] = ctx.machine_families.get(c["value"], "powermac")
                    c["guest_os"] = (ctx.machine_guest_os or {}).get(c["value"], "classic")
        if self.range is not None:
            d["range"] = {"min": self.range[0], "max": self.range[1], "step": self.step or 1}
            d["min"], d["max"] = self.range
        if self.step is not None:
            d["step"] = self.step
        if self.key in UNITS:
            d["unit"] = UNITS[self.key]
        if self.key in WARNINGS:
            d["warning_values"] = WARNINGS[self.key]
        if self.pattern is not None:
            d["pattern"] = self.pattern
            if self.type == "string":
                d["min_length"], d["max_length"] = {"share.volume_name": (1, 27),
                                                    "update.url": (0, 2048),
                                                    "printer.target": (3, 53)}.get(self.key, (1, 63))
        if ctx is not None and self.capability:
            d["available"] = bool(ctx.capabilities.get(self.capability))
        else:
            d["available"] = True
        if self.key in DISPLAY_NOTES:
            DISPLAY_NOTES[self.key](d, ctx)
        if ctx is not None and (ctx.machine_guest_os or {}).get((ctx.values or {}).get("machine.active"),
                                                               "classic").startswith("macosx-"):
            if self.key == "display.guest_size":
                d["label"] = "Startup screen size"
                d["help"] = ("The early startup screen size. Choose the desktop size in Mac OS X System "
                             "Preferences → Displays. Available desktop sizes depend on the Mac's display driver.")
            if self.key == "system.timezone":
                d["label"] = "RetroBoot time zone"
                d["help"] = ("Mac OS X keeps its own time zone. Change it in Mac OS X System "
                             "Preferences → Date & Time.")
            if self.key == "display.graphics":
                d["help"] = ("Use the native ATI driver for Quartz Extreme and OpenGL on qualified Tiger "
                             "Macs. Choose the desktop size in Mac OS X System Preferences → Displays. "
                             "The Mac starts again to change graphics cards.")
            note = OSX_CONTROL_NOTES.get(self.key)
            if note:
                d["available"] = False
                d["unavailable_reason"] = d["help"] = note
                if self.key == "machine.cpu":
                    d["label"] = "Classic Mac OS processor"
        return d

    def dynamic_choices(self, ctx: ValidationContext | None) -> tuple | list | None:
        if self.key == "machine.osx_ram_mb" and ctx is not None:
            active = (ctx.values or {}).get("machine.active")
            profile = (ctx.machine_guest_os or {}).get(active) if isinstance(active, str) else None
            if profile == "macosx-10.4":
                return tuple(v for v in self.choices if v != 128)
        if self.type == "machine":
            return sorted(ctx.machines) if ctx and ctx.machines is not None else None
        if self.type == "timezone":
            return sorted(ctx.timezones if ctx and ctx.timezones is not None else available_timezones())
        if self.type == "media":
            return ["none"] + (sorted(ctx.media_cds) if ctx and ctx.media_cds is not None else [])
        if self.type == "host_mode":
            if ctx and ctx.host_modes is not None:
                return ["auto"] + [m for m in ctx.host_modes if m != "auto"]
            return ["auto"]
        if self.key == "machine.quadra_ram_mb" and ctx and ctx.quadra_ram_max_mb is not None:
            return tuple(c for c in self.choices if c <= ctx.quadra_ram_max_mb)
        if self.type == "audio_output":
            if ctx and ctx.audio_outputs is not None:
                return ["auto"] + [k for k in ctx.audio_outputs if k != "auto"]
            return ["auto"]
        return self.choices

    def validate(self, value: Any, ctx: ValidationContext | None = None) -> Any:
        """Return the normalized value or raise ValueError with a user-facing message."""
        if value is None:
            if self.nullable:
                return None
            raise ValueError("A value is required.")
        t = self.type
        if t == "bool":
            if type(value) is not bool:
                raise ValueError("Must be true or false.")
            return value
        if t == "enum":
            if self.key == "machine.osx_ram_mb" and value != "auto" and type(value) is not int:
                raise ValueError("Choose Automatic or a listed whole number of megabytes.")
            choices = self.dynamic_choices(ctx)
            if isinstance(value, bool) or (value not in choices and value not in self.hidden_choices):
                raise ValueError("Choose one of: " + ", ".join(str(c) for c in choices) + ".")
            return value
        if t == "int":
            if type(value) is not int:
                raise ValueError("Must be a whole number.")
            lo, hi = self.range
            if self.key == "input.mouse_dpi" and value != 0 and not MOUSE_DPI_MIN <= value <= hi:
                raise ValueError(f"Enter 0 for automatic, or a DPI from {MOUSE_DPI_MIN} to {hi}.")
            if not lo <= value <= hi:
                raise ValueError(f"Must be between {lo} and {hi}.")
            return value
        if t == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Must be a number.")
            value = float(value)
            lo, hi = self.range
            if not (lo <= value <= hi) or value != value:
                raise ValueError(f"Must be between {lo} and {hi}.")
            return round(value, 3)
        if t == "string":
            if not isinstance(value, str) or not re.fullmatch(self.pattern, value):
                raise ValueError(self.help)
            if self.key == "printer.target" and value.startswith("cups:"):
                from .cups import CupsError, validate_queue_name
                try:
                    validate_queue_name(value[5:])
                except CupsError as exc:
                    raise ValueError(exc.message)
            return value
        if t == "machine":
            if not isinstance(value, str) or not MACHINE_ID_RE.fullmatch(value):
                raise ValueError("Not a valid machine identifier.")
            if ctx is not None and ctx.machines is not None and value not in ctx.machines:
                raise ValueError("That machine is not installed.")
            return value
        if t == "timezone":
            if not isinstance(value, str) or not TIMEZONE_RE.fullmatch(value):
                raise ValueError("Use a time zone name such as Europe/Berlin.")
            zones = ctx.timezones if ctx is not None and ctx.timezones is not None else available_timezones()
            if value not in zones:
                raise ValueError("Unknown time zone.")
            return value
        if t == "media":
            if value == "none":
                return value
            if not isinstance(value, str) or not MEDIA_NAME_RE.fullmatch(value):
                raise ValueError("Choose a disc image from the media library, or none.")
            if ctx is not None and ctx.media_cds is not None and value not in ctx.media_cds:
                raise ValueError("That disc image is not in the media library.")
            return value
        if t == "pixel_scale":
            if type(value) is int:
                value = str(value)
            if value not in PIXEL_SCALES:
                raise ValueError("Choose Automatic, 1×, 2×, 3× or 4×.")
            mode = ctx.display_mode if ctx is not None else None
            if value != "auto" and mode:
                reason = dpolicy.unavailable_reason(mode[0], mode[1], int(value))
                if reason:
                    raise ValueError(f"{value}× isn't available on this display. {reason}")
            return value
        if t == "catalog_urls":
            return validate_catalog_urls(value)
        if t == "redirects":
            return validate_redirects(value)
        if t == "bypass_hosts":
            return validate_bypass_hosts(value)
        if t == "host_mode":
            if value == "auto":
                return value
            if not isinstance(value, str) or not HOST_MODE_RE.fullmatch(value):
                raise ValueError("Use auto or a mode such as 1920x1080@60.")
            if ctx is not None and ctx.host_modes is not None and value not in ctx.host_modes:
                raise ValueError("This display does not offer that mode.")
            return value
        if t == "audio_output":
            if not isinstance(value, str) or not AUDIO_OUTPUT_RE.fullmatch(value):
                raise ValueError("Choose a valid sound output.")
            return value
        raise ValueError("Unsupported setting type.")


def validate_catalog_urls(value: Any) -> list[str]:
    from urllib.parse import urlsplit
    from .catalog import secure_url

    if not isinstance(value, list) or len(value) > MAX_IMAGE_SERVERS:
        raise ValueError(f"Enter up to {MAX_IMAGE_SERVERS} image server URLs.")
    urls = []
    for url in value:
        if not secure_url(url) or "?" in url or "#" in url:
            raise ValueError("Use a full HTTPS catalog URL without credentials, a query or a fragment.")
        if not urlsplit(url).path or urlsplit(url).path == "/":
            raise ValueError("Enter the full address of the catalog file.")
        if url not in urls:
            urls.append(url)
    return urls


def validate_redirects(value: Any) -> list[dict]:
    """network.web_proxy_redirects: [{host, path, to}], "to" an http(s) address or "search"."""
    if not isinstance(value, list):
        raise ValueError("Provide a list of redirects.")
    if len(value) > MAX_REDIRECTS:
        raise ValueError(f"Up to {MAX_REDIRECTS} redirects.")
    out, seen = [], set()
    for i, e in enumerate(value, 1):
        if not isinstance(e, dict) or set(e) - {"host", "path", "to"}:
            raise ValueError(f"Redirect {i} is not valid.")
        host = str(e.get("host") or "").strip().lower().rstrip(".")
        path = str(e.get("path") or "").strip()
        to = str(e.get("to") or "").strip()
        if not REDIRECT_HOST_RE.fullmatch(host):
            raise ValueError(f"Redirect {i}: enter a host name such as home.netscape.com.")
        if path and (not path.startswith("/") or len(path) > 200 or any(c.isspace() for c in path)):
            raise ValueError(f"Redirect {i}: a page path starts with / (leave it empty for the whole site).")
        if to != "search" and (not REDIRECT_URL_RE.fullmatch(to) or len(to) > 500):
            raise ValueError(f"Redirect {i}: the new address must start with http:// or https://.")
        if (host, path) in seen:
            raise ValueError(f"Redirect {i}: {host}{path} is already in the list.")
        seen.add((host, path))
        out.append({"host": host, "path": path, "to": to})
    return out


def validate_bypass_hosts(value: Any) -> list[str]:
    """network.web_proxy_bypass: host names or IPv4 addresses the helper passes through untouched."""
    if not isinstance(value, list):
        raise ValueError("Provide a list of site names or IP addresses.")
    if len(value) > MAX_BYPASS_HOSTS:
        raise ValueError(f"Up to {MAX_BYPASS_HOSTS} whitelisted sites.")
    out: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(value, 1):
        if not isinstance(raw, str):
            raise ValueError(f"Site {i}: enter a site name such as example.com.")
        host = raw.strip().lower()
        host = re.sub(r"^https?://", "", host)
        host = host.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        if ":" in host and not host.startswith("["):
            host = host.rsplit(":", 1)[0]
        while host.startswith("*."):
            host = host[2:]
        host = host.strip(".")
        if not host:
            raise ValueError(f"Site {i}: enter a site name such as example.com.")
        ok = bool(REDIRECT_HOST_RE.fullmatch(host))
        if not ok:
            try:
                ok = isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address)
            except ValueError:
                ok = False
        if not ok:
            raise ValueError(f"Site {i}: “{raw.strip()}” is not a valid host name or IPv4 address.")
        if host not in seen:
            seen.add(host)
            out.append(host)
    return out


GROUP_TITLES = {"machine": "Machine", "system": "Date & Time", "startup": "Startup", "display": "Display",
                "input": "Keyboard & Mouse", "bluetooth": "Bluetooth", "audio": "Sound", "network": "Network",
                "share": "Sharing", "storage": "Storage", "management": "Web Management", "media": "CD-ROM",
                "remote": "Remote Screen", "update": "Software Update", "power": "Battery"}
WARNINGS = {
    "management.web": {"off": "Turning web management off ends this session. Only the RetroBoot panel on the "
                              "computer itself can turn it back on."},
    "management.hostname": {"*": "After the name changes, reconnect using the new address."},
    "machine.active": {"*": "Mac OS restarts with the other machine when you apply this."},
}
UNITS = {"machine.ram_mb": "MB", "machine.osx_ram_mb": "MB", "machine.quadra_ram_mb": "MB", "audio.volume": "%", "input.pointer_speed": "×"}


def choice_label(kind: str, value) -> str:
    if kind == "enum" and isinstance(value, int):
        return f"{value} MB"
    if isinstance(value, str) and re.fullmatch(r"\d+x\d+", value):
        return value.replace("x", " × ")
    if kind == "host_mode" and isinstance(value, str) and value != "auto":
        w, rest = value.split("x")
        h, r = rest.split("@")
        return f"{w} × {h}, {r} Hz"
    if kind == "host_mode" and value == "auto":
        return "Automatic (keep current mode)"
    if kind == "timezone" and isinstance(value, str):
        return value.replace("_", " ")
    if value in ("on", "off"):
        return value.title()
    return str(value)


def _s(**kw) -> Setting:
    return Setting(**kw)


# --- display.pixel_scale (ARCHITECTURE 4.5) ------------------------------------------
PIXEL_SCALE_HELP = ("Pixel doubling: every Mac pixel becomes a sharp square of 2 × 2, 3 × 3 or 4 × 4 screen "
                    "pixels. Larger pixels give a smaller Mac screen. Sizes that would give Mac OS less than "
                    "640 × 480 aren't offered.")


def pixel_scale_label(value: str, mode: tuple[int, int] | None) -> str:
    if value == "auto":
        if not mode:
            return "Automatic"
        s = dpolicy.suggested_scale(*mode)
        gw, gh = dpolicy.auto_guest_size(mode[0], mode[1], s)
        return f"Automatic ({s}× — {gw} × {gh})"
    s = int(value)
    if not mode:
        return f"{s}×"
    gw, gh = dpolicy.scaled_guest_size(mode[0], mode[1], s)
    if dpolicy.pixel_scale_valid(mode[0], mode[1], s):
        return f"{s}× — {gw} × {gh}"
    lo, _hi = dpolicy.guest_limits(mode[0], mode[1])
    if gw < lo[0] or gh < lo[1]:
        return f"{s}× — needs a larger display"
    return f"{s}× — Mac screen too large"


def pixel_scale_choices(ctx: "ValidationContext | None") -> list[dict]:
    """Dynamic choices: the resulting Mac resolution for each pixel size on the
    current display; sizes the display can't use are disabled with a reason."""
    mode = ctx.display_mode if ctx is not None else None
    out = []
    for v in PIXEL_SCALES:
        c: dict[str, Any] = {"value": v, "label": pixel_scale_label(v, mode)}
        if v == "auto":
            c["help"] = "The largest pixels that keep the Mac screen at least 720 × 540."
        elif mode:
            s = int(v)
            gw, gh = dpolicy.scaled_guest_size(mode[0], mode[1], s)
            c["size"] = [gw, gh]
            reason = dpolicy.unavailable_reason(mode[0], mode[1], s)
            if reason:
                c["disabled"] = True
                c["reason"] = reason
                c["help"] = reason
        out.append(c)
    return out


def fixed_size_note(values: dict, mode: tuple[int, int] | None, pixel_scale: str | None = None,
                    guest_size: str | None = None) -> str | None:
    """How a fixed Mac screen size is drawn at the chosen pixel size, when that
    differs from the pixel size (it doesn't fit), for the panel and web."""
    pixel_scale = pixel_scale or values.get("display.pixel_scale", "auto")
    guest_size = guest_size or values.get("display.guest_size", "auto")
    if not mode or guest_size == "auto" or values.get("display.scaling") == "fit":
        return None
    W, H = mode
    gw, gh = dpolicy.oriented_size(W, H, *parse_size(guest_size))   # on its side on a portrait display
    if gw > W or gh > H:
        return (f"{gw} × {gh} is larger than this display, so it is reduced smoothly to fit.")
    if pixel_scale == "auto":
        return None
    s = dpolicy.effective_scale(W, H, pixel_scale)
    k = dpolicy.integer_factor(W, H, gw, gh, pixel_scale, s)
    if k < s:
        return f"{gw} × {gh} doesn't fit at {s}× on this display, so it is shown at {k}×."
    return None


def _note_pixel_scale(d: dict, ctx: "ValidationContext | None") -> None:
    if ctx is None or ctx.values is None:
        return
    mode = ctx.display_mode
    note = fixed_size_note(ctx.values, mode)
    if note:
        d["help"] = PIXEL_SCALE_HELP + " " + note
    # Before applying: a pixel size the fixed Mac screen size can't use.
    warn = {}
    for v in PIXEL_SCALES[1:]:
        n = fixed_size_note(ctx.values, mode, pixel_scale=v)
        if n and mode and dpolicy.pixel_scale_valid(mode[0], mode[1], int(v)):
            warn[v] = n
    if warn:
        d["warning_values"] = warn


def _note_guest_size(d: dict, ctx: "ValidationContext | None") -> None:
    if ctx is None or ctx.values is None:
        return
    note = fixed_size_note(ctx.values, ctx.display_mode)
    if note:
        d["help"] = d["help"] + " " + note
    warn = {}
    for v in GUEST_SIZES[1:]:
        n = fixed_size_note(ctx.values, ctx.display_mode, guest_size=v)
        if n:
            warn[v] = n
    if warn:
        d["warning_values"] = warn


GRAPHICS_HELP = ("3D acceleration lets Mac OS games and other 3D programs (OpenGL, QuickDraw 3D RAVE) draw "
                 "with this computer's graphics chip through an emulated ATI Radeon and ATI's own Mac OS "
                 "software. Everyday drawing is not faster, and Mac OS uses ATI's screen sizes. Mac OS "
                 "starts again to change this.")


def _note_graphics(d: dict, ctx: "ValidationContext | None") -> None:
    """3D acceleration: the ATI choice is disabled with the reason when it can't be
    used; the setting as a whole is unavailable unless it is on (so it can always be
    turned off).  When it can be used, the help names the Mac screen size it gives."""
    g = ctx.graphics if ctx is not None else None
    if not g:
        return
    if g.get("osx"):
        d["help"] = ("Uses the ATI Radeon for OpenGL and Quartz Extreme. The Mac starts again "
                     "when the graphics card changes. Choose the desktop resolution in Mac OS X Displays.")
    current = (ctx.values or {}).get("display.graphics", "standard")
    if not g.get("available"):
        for c in d.get("choices") or []:
            if c["value"] == "ati-radeon":
                c["disabled"] = True
                c["reason"] = c["help"] = g.get("message")
        if current != "ati-radeon":
            d["available"] = False
            d["unavailable_reason"] = g.get("message")
        return
    size = g.get("ati_screen")
    if size:
        if g.get("osx"):
            d["help"] += f" The ATI desktop can be {size[0]} × {size[1]} on this display."
        else:
            d["help"] = (d["help"] + f" On this display Mac OS uses {size[0]} × {size[1]} with it "
                         "(the largest ATI screen size that fits at the pixel size).")


DISPLAY_NOTES: dict[str, Callable[[dict, "ValidationContext | None"], None]] = {
    "display.pixel_scale": _note_pixel_scale,
    "display.guest_size": _note_guest_size,
    "display.graphics": _note_graphics,
}

OSX_CONTROL_NOTES = {
    "machine.cpu": "Mac OS X uses a PowerPC G4 (7400). This choice is preserved for classic Mac OS.",
    "machine.boot_speed": "Mac OS X starts in real time.",
    "system.battery_passthrough": "The Mac OS X profile does not use the classic PowerBook battery driver.",
    "input.mac_os_keys": "The Mac OS X profile does not use the classic PowerBook volume and brightness driver.",
    "network.guest_adapter": "Mac OS X uses Ethernet. AirPort support has not been qualified for Mac OS X.",
    "share.enabled": "Shared volumes have not been qualified for Mac OS X.",
    "share.volume_name": "Shared volumes have not been qualified for Mac OS X.",
    "startup.progress_indicator": "Mac OS X shows its own startup screen and progress.",
}


SETTINGS: tuple[Setting, ...] = (
    _s(key="machine.active", type="machine", default=None, nullable=True, apply=GUEST_RESTART,
       group="machine", label="Startup machine",
       help="The installed Mac that starts when RetroBoot turns on."),
    _s(key="machine.ram_mb", type="enum", default=512, choices=(256, 384, 512, 768, 896),
       apply=GUEST_RESTART, group="machine", label="Memory", families=POWERMAC_ONLY,
       help="Memory available to Mac OS, in megabytes."),
    _s(key="machine.osx_ram_mb", type="enum", default="auto",
       choices=("auto", 128, 256, 384, 512, 768, 896, 1024, 1536, 2048),
       choice_labels={"auto": "Automatic", **{v: f"{v} MB" for v in (128, 256, 384, 512, 768, 896, 1024, 1536, 2048)}},
       apply=GUEST_RESTART, group="machine",
       label="Memory", families=POWERMAC_ONLY,
       help="Memory available to Mac OS X. Automatic keeps the existing memory size."),
    # A 68k Mac (quadra.py): its own memory; its processor is always the Quadra 800's 68040.
    _s(key="machine.quadra_ram_mb", type="enum", default=QUADRA_RAM_DEFAULT, choices=QUADRA_RAM_CHOICES,
       apply=GUEST_RESTART, group="machine", label="Memory", families=QUADRA_ONLY,
       help="Memory available to Mac OS on a 68k Mac, in megabytes."),
    _s(key="machine.cpu", type="enum", default="7400", choices=("7400", "g3"),
       choice_labels={"7400": "PowerPC G4 (7400)", "g3": "PowerPC G3"},
       apply=GUEST_RESTART, group="machine", label="Processor", families=POWERMAC_ONLY,
       help="The emulated PowerPC processor."),
    # "accelerated" (icount until the Finder) leaves Mac OS's clock 94-102 s fast for the
    # whole session (qemu-display qualification): a diagnostic option, valid but not offered.
    _s(key="machine.boot_speed", type="enum", default="realtime", choices=("realtime",),
       hidden_choices=("accelerated",),
       choice_labels={"realtime": "Real-time startup",
                      "accelerated": "Accelerated startup (experimental: the Mac clock runs about 1.5 minutes fast)"},
       apply=GUEST_RESTART, group="machine", label="Startup speed", families=POWERMAC_ONLY,
       help="Mac OS starts in real time, so its clock is exact."),
    _s(key="system.timezone", type="timezone", default="UTC", apply=GUEST_RESTART, group="system",
       label="Time zone",
       help="The Mac clock shows local time in this zone. Mac OS reads the clock when it starts."),
    # Battery passthrough (retroboot/battery.py): needs a host battery and a capable package.
    _s(key="system.battery_passthrough", type="bool", default=True, apply=GUEST_RESTART,
       capability="battery", group="power", label="Show the battery in Mac OS", families=POWERMAC_ONLY,
       help="Mac OS shows this computer's battery in its Control Strip and menu bar and warns when it "
            "runs low. Mac OS then runs as a PowerBook; it starts again to change this."),
    _s(key="startup.autostart", type="bool", default=True, apply=LIVE, group="startup",
       label="Start Mac OS automatically",
       help="When off, RetroBoot opens the startup options instead of starting Mac OS."),
    _s(key="startup.progress_indicator", type="bool", default=True, apply=LIVE, group="startup",
       label="Show startup progress",
       help="While Mac OS starts, a Mac on the checkerboard shows RetroBoot's progress until Mac OS's "
            "own startup screen appears. Applies from the next startup."),
    _s(key="display.pixel_scale", type="pixel_scale", default="auto", choices=PIXEL_SCALES,
       apply=LIVE, group="display", label="Pixel size",
       help=PIXEL_SCALE_HELP),
    _s(key="display.guest_size", type="enum", default="auto", choices=GUEST_SIZES,
       choice_labels={"auto": "Best for this display"},
       apply=LIVE, group="display", label="Mac screen size",
       help="The resolution offered to Mac OS. Best for this display follows the display and the pixel size."),
    _s(key="display.scaling", type="enum", default="integer", choices=("integer", "fit"),
       choice_labels={"integer": "Sharp (whole pixels)", "fit": "Fill the screen (smooth)"},
       apply=LIVE, group="display", label="Scaling",
       help="How the Mac screen is enlarged to fill the display."),
    _s(key="display.primary", type="enum", default="internal", choices=("internal", "external"),
       choice_labels={"internal": "Built-in display", "external": "External display when connected"},
       apply=LIVE, capability="internal_display", group="display", label="Main display",
       help="Mac OS takes its screen size from this display; any other display shows the same picture."),
    # Rotation (ARCHITECTURE 4.5; retroboot/orientation.py for "auto").
    _s(key="display.rotation", type="enum", default="auto", choices=ROTATIONS, choice_labels=ROTATION_LABELS,
       apply=LIVE, group="display", label="Rotation",
       help="Turns the picture on the main display; at 90° Mac OS gets a portrait screen. Automatic keeps a "
            "built-in screen upright and, on a computer with a tilt sensor, follows how it is held."),
    _s(key="display.rotation_lock", type="bool", default=False, apply=LIVE, capability="accelerometer",
       group="display", label="Rotation Lock",
       help="Keeps the current orientation when the computer is turned (with Automatic rotation)."),
    # Graphics card (graphics.py, research/gfx): the emulated ATI Radeon, "3D acceleration";
    # offered only with a capable package, a hardware host GL and ATI's software in Mac OS
    # (ValidationContext.graphics), shown disabled with the reason otherwise.
    _s(key="display.graphics", type="enum", default="standard", choices=("standard", "ati-radeon"),
       choice_labels={"standard": "Standard", "ati-radeon": "3D acceleration (ATI Radeon)"},
       apply=GUEST_RESTART, group="display", label="Graphics card", families=POWERMAC_ONLY,
       help=GRAPHICS_HELP),
    _s(key="display.host_mode", type="host_mode", default="auto", apply=LIVE, confirm=15,
       capability="display_modes", group="display", label="Display resolution",
       help="The physical display mode. You have 15 seconds to keep a new mode before it reverts."),
    _s(key="input.swap_command_option", type="bool", default=False, apply=LIVE, group="input",
       label="Swap Command and Option",
       help="Use Alt as Command and the Windows key as Option."),
    _s(key="input.recovery_chord", type="enum", default="ctrl+alt+delete", choices=RECOVERY_CHORDS,
       choice_labels={"ctrl+alt+delete": "Control-Alt-Delete",
                      "ctrl+alt+backspace": "Control-Alt-Backspace",
                      "ctrl+alt+escape": "Control-Alt-Escape"},
       apply=LIVE, group="input", label="Recovery keys",
       help="The key combination that opens RetroBoot Recovery."),
    _s(key="input.pointer_speed", type="float", default=1.0, range=(0.25, 4.0), step=0.25,
       apply=LIVE, group="input", label="Pointer speed", help="Pointer speed multiplier."),
    _s(key="input.legacy_mouse", type="bool", default=True, apply=GUEST_RESTART, group="input",
       label="Legacy mouse tracking",
       help="Let Mac OS 7, 8 or 9 track your local mouse. Takes effect when Mac OS restarts."),
    _s(key="input.mouse_dpi", type="int", default=0, range=(0, MOUSE_DPI_MAX), apply=LIVE, group="input",
       label="Mouse DPI", help="Enter your mouse's current DPI for Legacy tracking. Enter 0 for automatic."),
    _s(key="input.touch_gestures", type="bool", default=True, apply=LIVE, capability="touch", group="input",
       label="Touch gestures",
       help="On a touchscreen: tap to click, drag to move things, touch and hold or tap with two fingers for a "
            "contextual menu, and drag two fingers to scroll. Off: a touch is a plain mouse button."),
    _s(key="input.gamepad_mode", type="enum", default="arrows", choices=GAMEPAD_MODES,
       choice_labels=GAMEPAD_MODE_LABELS, apply=LIVE, group="input", label="Game controller",
       help="Map USB and Bluetooth game controllers to Mac OS Arrow keys or WASD, or turn mapping off."),
    _s(key="input.natural_scroll", type="bool", default=False, apply=LIVE, group="input",
       label="Natural scrolling",
       help="Move content in the same direction your fingers drag on a trackpad or scroll wheel."),
    _s(key="input.mac_os_keys", type="bool", default=True, apply=GUEST_RESTART, capability="mac_os_keys",
       group="input", label="Volume and brightness keys in Mac OS", families=POWERMAC_ONLY,
       help="On a laptop the volume, mute and brightness keys work as on a PowerBook: Mac OS changes its own "
            "volume and brightness and shows its own windows. Mac OS starts again to change this."),
    # Bluetooth keyboards, mice and trackpads (retroboot/bluetooth.py): the radio is on
    # only while this is on; paired devices reconnect by themselves.
    _s(key="bluetooth.enabled", type="bool", default=True, apply=LIVE, capability="bluetooth",
       group="bluetooth", label="Bluetooth",
       help="Use Bluetooth keyboards, mice, trackpads and game controllers. When off, the Bluetooth radio "
            "is turned off and paired devices stay paired for later."),
    # The setup assistant (bluetooth.py): never over the Mac OS startup; after the
    # Finder, with Mac OS stopped, or in first-time setup.
    _s(key="bluetooth.offer_keyboard_setup", type="bool", default=True, apply=LIVE, capability="bluetooth",
       group="bluetooth", label="Offer to pair a keyboard when none is connected",
       help="When no keyboard is connected, RetroBoot asks once Mac OS has reached the Finder whether to pair "
            "a Bluetooth keyboard. With no keyboard or mouse at all, it looks for one in pairing mode by "
            "itself. Not Now keeps the question away until the next restart or until a keyboard is connected "
            "or disconnected."),
    _s(key="audio.enabled", type="bool", default=True, apply=GUEST_RESTART, group="audio",
       label="Sound", help="Play Mac OS sound through this computer."),
    _s(key="audio.volume", type="int", default=70, range=(0, 100), apply=LIVE,
       capability="audio", group="audio", label="Volume", help="Output volume, 0 to 100."),
    _s(key="audio.startup_chime", type="enum", default="auto", choices=CHIME_CHOICES,
       choice_labels=CHIME_LABELS, apply=LIVE, group="audio", label="Startup chime",
       help="Classic Macintosh chime played when Mac OS starts."),
    _s(key="audio.output", type="audio_output", default="auto", apply=GUEST_RESTART, group="audio",
       label="Sound output",
       help="Audio device used for sound and chimes. Changes apply to host controls right away and to Mac OS "
            "when it restarts."),
    _s(key="network.guest", type="enum", default="nat", choices=("nat", "none"),
       choice_labels={"nat": "Share this computer's connection", "none": "No network"},
       apply=GUEST_RESTART, group="network", label="Mac OS networking",
       help="Gives Mac OS an Ethernet connection through this computer."),
    # AirPort (hostd/retroboot/airport): needs an emulator package with the card.
    _s(key="network.guest_adapter", type="enum", default="ethernet", choices=("ethernet", "airport"),
       choice_labels={"ethernet": "Ethernet", "airport": "AirPort"},
       apply=GUEST_RESTART, capability="airport", group="network", label="Mac OS network adapter",
       families=POWERMAC_ONLY,
       help="With AirPort, Mac OS's own AirPort software lists and joins Wi-Fi networks through this "
            "computer. Mac OS keeps Ethernet if its AirPort software is not installed."),
    # Modern Web Helper (retroboot/webproxy.py, webproxy/): live, no Mac OS restart.
    _s(key="network.web_proxy", type="enum", default="on", choices=("on", "off"),
       choice_labels={"on": "On", "off": "Off"}, apply=LIVE, capability="web_proxy", group="network",
       label="Modern Web Helper",
       help="Opens today's web in Mac OS's browsers. RetroBoot fetches each page, including secure "
            "(https) sites, and translates it for Internet Explorer and Netscape. Turn it off to let "
            "Mac OS connect directly."),
    _s(key="network.web_proxy_view", type="enum", default="auto", choices=("auto", "simplified", "full"),
       choice_labels={"auto": "Automatic", "simplified": "Simplified pages", "full": "Full pages"},
       apply=LIVE, capability="web_proxy", group="network", label="Page view",
       help="Simplified shows an article's text and pictures, which old browsers display quickly and "
            "cleanly. Full keeps each page's own layout without scripts and styles. Automatic "
            "simplifies articles and keeps every other page full."),
    _s(key="network.web_proxy_homepages", type="bool", default=True, apply=LIVE, capability="web_proxy",
       group="network", label="Send old home pages to new ones",
       help="The start pages built into Mac OS and its browsers (Apple's Live Page, Netscape, MSN) "
            "are gone. RetroBoot opens System 7 Today instead, and old search pages open your "
            "search page."),
    _s(key="network.web_proxy_search", type="enum", default="frogfind", choices=SEARCH_CHOICES,
       choice_labels=SEARCH_LABELS, apply=LIVE, capability="web_proxy", group="network",
       label="Search page", help="Where the browsers' old search pages go."),
    _s(key="network.web_proxy_redirects", type="redirects", default=[dict(e) for e in DEFAULT_REDIRECTS],
       apply=LIVE, capability="web_proxy", group="network", label="Home page redirects",
       help="Each old site (and optional page path) opens a new address, or your search page. An "
            "empty path covers the whole site; / covers only its front page."),
    _s(key="network.web_proxy_lan", type="bool", default=True, apply=LIVE, capability="web_proxy",
       group="network", label="Allow pages on the local network",
       help="Lets Mac OS's browsers open routers, printers and other computers on your network, as any "
            "browser can. Mac OS can never open RetroBoot's own pages through the helper."),
    _s(key="network.web_proxy_bypass", type="bypass_hosts", default=list(DEFAULT_BYPASS_HOSTS),
       apply=LIVE, capability="web_proxy", group="network",
       label="Sites not to help (whitelist)",
       help="Web sites the Modern Web Helper passes through untouched without rewriting pages, images, "
            "or redirects. Subdomains are included automatically."),
    # Media library (retroboot/media.py): a registered disc image, never a host path.
    _s(key="media.cd", type="media", default="none", apply=GUEST_RESTART, group="media",
       choice_labels={"none": "No disc"}, label="CD-ROM drive",
       help="The disc image in Mac OS's CD-ROM drive. Mac OS notices a new disc when it starts, "
            "so a change applies when Mac OS restarts."),
    _s(key="share.enabled", type="bool", default=True, apply=GUEST_RESTART, group="share",
       label="Shared volume", help="Show a shared folder as a disk in Mac OS."),
    _s(key="share.volume_name", type="string", default="RetroBoot Share",
       pattern=VOLUME_NAME_RE.pattern, apply=GUEST_RESTART, group="share",
       label="Shared volume name",
       help="1 to 27 letters, digits, spaces, hyphens or underscores."),
    _s(key="printer.enabled", type="bool", default=True, apply=LIVE, group="storage",
       label="Print to PDF",
       help="Listen for Mac OS PostScript print jobs and save PDFs into RetroBoot Share/Printed PDFs."),
    _s(key="printer.target", type="string", default="pdf", pattern=PRINTER_TARGET_RE.pattern,
       apply=LIVE, group="storage", label="Mac printer output",
       help="Choose pdf to save into RetroBoot Share, or cups:<name> to send print jobs to a configured printer."),
    _s(key="printer.keep_pdf", type="bool", default=False, apply=LIVE, group="storage",
       label="Also save a PDF copy in RetroBoot Share",
       help="When printing to a physical printer, also save a PDF copy inside RetroBoot Share/Printed PDFs."),
    _s(key="usb_share.enabled", type="bool", default=True, apply=LIVE, group="storage",
       label="USB flash drives in Share",
       help="Show plugged-in USB flash drives inside RetroBoot Share/USB Drives."),
    _s(key="management.web", type="enum", default="on", choices=("on", "off"), apply=LIVE,
       group="management", label="Web management",
       help="Allow paired browsers on the local network to manage this computer."),
    _s(key="management.hostname", type="string", default="retroboot",
       pattern=HOSTNAME_RE.pattern, apply=LIVE, group="management", label="Computer name",
       help="Lowercase letters, digits and hyphens (1 to 63 characters)."),
    # Web Screen page (retroboot/remote): paired browsers are already trusted with
    # restart, force stop and settings, so control is the default.
    _s(key="remote.screen", type="enum", default="control", choices=("off", "view", "control"),
       choice_labels={"off": "Off", "view": "View only", "control": "View and control"},
       apply=LIVE, group="remote", label="Remote screen",
       help="Lets paired browsers see the Mac OS screen on the Screen page and, with View and control, "
            "use its keyboard and mouse. While a browser is connected, a small symbol in the Mac OS "
            "menu bar shows it on this computer."),
    _s(key="catalog.urls", type="catalog_urls", default=[], apply=LIVE, group="catalogs",
       label="Image server URLs", help="Enter one HTTPS catalog URL per line."),
    # Software Update (ARCHITECTURE 13): checked only when asked; never installs by itself.
    _s(key="update.url", type="string", default="", pattern=UPDATE_URL_RE.pattern, apply=LIVE,
       group="update", label="Update server",
       help="The https:// address of an update list, or empty. Empty on a computer signed in to "
            "RetroBoot downloads uses its update list. RetroBoot checks it only when you "
            "choose Check for Updates, and installs only updates signed by RetroBoot."),
)

BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS}


def defaults(overrides: dict | None = None) -> dict[str, Any]:
    """Schema defaults, optionally overlaid with image defaults (/etc/retroboot/defaults.json)."""
    values = {s.key: s.default for s in SETTINGS}
    for k, v in (overrides or {}).items():
        if k in BY_KEY:
            try:
                values[k] = BY_KEY[k].validate(v, None)
            except ValueError:
                pass
    return values


class ValidationError(ValueError):
    def __init__(self, fields: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in fields.items()))
        self.fields = fields


def validate_changes(current: dict, changes: Any, ctx: ValidationContext | None) -> dict:
    """Validate a change set against the complete candidate.  Returns the candidate."""
    if not isinstance(changes, dict) or not changes:
        raise ValidationError({"changes": "Provide at least one setting to change."})
    errors: dict[str, str] = {}
    candidate = dict(current)
    if ctx is not None:
        ctx = replace(ctx, values={**current, **changes})
    for key, value in changes.items():
        setting = BY_KEY.get(key)
        if setting is None:
            errors[key] = "Unknown setting."
            continue
        if ctx is not None and ctx.check_capabilities and setting.capability \
                and not ctx.capabilities.get(setting.capability):
            errors[key] = "Not available on this computer."
            continue
        if key == "display.graphics" and value == "ati-radeon" and current.get(key) != "ati-radeon" \
                and ctx is not None and ctx.check_capabilities and ctx.graphics is not None:
            # judged for the Mac that will start (the same change may switch machine.active)
            g = (ctx.graphics.get("by_machine") or {}).get(changes.get("machine.active")) or ctx.graphics
            if not g.get("available"):
                errors[key] = g.get("message") or "3D acceleration isn't available."
                continue
        try:
            candidate[key] = setting.validate(value, ctx)
        except ValueError as exc:
            errors[key] = str(exc)
    if ctx is not None:
        active = candidate.get("machine.active")
        profile = (ctx.machine_guest_os or {}).get(active) if isinstance(active, str) else None
        if profile == "macosx-10.4" and candidate.get("machine.osx_ram_mb") == 128:
            errors["machine.osx_ram_mb"] = "Tiger needs at least 256 MB of memory."
    if errors:
        raise ValidationError(errors)
    cross = cross_validate(candidate)
    if cross:
        raise ValidationError(cross)
    return candidate


def cross_validate(values: dict) -> dict[str, str]:
    """Rules spanning several settings."""
    errors = {}
    if values.get("machine.cpu") == "g3" and values.get("machine.ram_mb", 0) > 768:
        errors["machine.ram_mb"] = "The G3 profile supports up to 768 MB."
    return errors


def validate_complete(values: Any) -> dict:
    """Static validation of a whole stored configuration (used when loading)."""
    if not isinstance(values, dict):
        raise ValidationError({"values": "Not an object."})
    errors = {}
    out = {}
    for s in SETTINGS:
        if s.key not in values:
            errors[s.key] = "Missing."
            continue
        try:
            out[s.key] = s.validate(values[s.key], None)
        except ValueError as exc:
            errors[s.key] = str(exc)
    unknown = set(values) - set(BY_KEY)
    for k in unknown:
        errors[k] = "Unknown setting."
    errors.update(cross_validate(out) if not errors else {})
    if errors:
        raise ValidationError(errors)
    return out


def complete_with_defaults(values: dict, base: dict) -> dict:
    """Fill missing keys from defaults and drop unknown ones (schema migration)."""
    out = dict(base)
    for k, v in (values or {}).items():
        if k in BY_KEY:
            try:
                out[k] = BY_KEY[k].validate(v, None)
            except ValueError:
                pass
    return out


def describe(ctx: ValidationContext | None = None) -> list[dict]:
    hidden = set()
    if ctx is not None:
        active = (ctx.values or {}).get("machine.active")
        profile = (ctx.machine_guest_os or {}).get(active, "classic") if isinstance(active, str) else "classic"
        hidden.add("machine.ram_mb" if profile.startswith("macosx-") else "machine.osx_ram_mb")
    return [s.describe(ctx) for s in SETTINGS if s.key not in hidden]


def parse_size(value: str) -> tuple[int, int] | None:
    if value == "auto" or not value:
        return None
    w, h = value.split("x")
    return int(w), int(h)


def parse_host_mode(value: str) -> list[int] | None:
    if value == "auto" or not value:
        return None
    m = HOST_MODE_RE.fullmatch(value)
    if not m:
        return None
    return [int(m.group(1)), int(m.group(2)), int(m.group(3))]


def format_host_mode(mode) -> str:
    w, h, r = mode
    return f"{int(w)}x{int(h)}@{int(round(r))}"


Validator = Callable[[dict], None]
