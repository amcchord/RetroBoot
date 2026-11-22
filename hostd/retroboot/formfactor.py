"""Is this RetroBoot a portable?

Product text says "the laptop" only on a portable computer; on a mini PC or
desktop it says "the computer" and "the display".  A machine is portable when
it has a system battery or its firmware says so (SMBIOS chassis type).  Read
once at start; ``form_factor`` in hostd.json overrides it.
"""
from __future__ import annotations

from pathlib import Path

# SMBIOS 3.x system enclosure types that are carried around.
PORTABLE_CHASSIS = {8: "Portable", 9: "Laptop", 10: "Notebook", 11: "Hand Held", 14: "Sub Notebook",
                    30: "Tablet", 31: "Convertible", 32: "Detachable"}


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


def detect(sysfs: Path = Path("/sys"), override: str = "auto", system_batteries: tuple = ()) -> dict:
    """{"form_factor": "portable"|"desktop", "chassis_type": int|None, "battery": bool}
    ``system_batteries``: name prefixes of the board's own battery whatever its scope
    (platform.py profile; a Gemini PDA's fuel gauge may say "Device")."""
    chassis = None
    raw = _read(sysfs / "class" / "dmi" / "id" / "chassis_type")
    if raw and raw.isdigit():
        chassis = int(raw)
    battery = False
    try:
        supplies = sorted((sysfs / "class" / "power_supply").iterdir())
    except OSError:
        supplies = []
    for d in supplies:
        # A peripheral's battery (mouse, keyboard) has scope "Device".
        named = bool(system_batteries) and d.name.startswith(tuple(system_batteries))
        if _read(d / "type") == "Battery" and (named or _read(d / "scope") != "Device") and \
                _read(d / "present") != "0":
            battery = True
    portable = battery or chassis in PORTABLE_CHASSIS
    if override in ("portable", "desktop"):
        portable = override == "portable"
    return {"form_factor": "portable" if portable else "desktop", "chassis_type": chassis, "battery": battery}
