"""A Raspberry Pi system installed beside the installer on its own card (ARCHITECTURE 8.2).

``install-to-disk --board rpi --mode free-space`` keeps the installer's partitions on
the SD card or USB stick and adds RetroBoot after them. The card's FAT boot partition
(RBINSTEFI, mounted at ``/boot/firmware`` by the installed system) then has:

* ``config.txt``: RetroBoot's, first line :data:`CONFIG_MARK`, with ``os_prefix``
  ``retroboot/os/`` (RetroBoot's kernel, initramfs, cmdline.txt, device trees);
* ``tryboot.txt``: the installer's own configuration. The Pi firmware reads it instead
  of ``config.txt`` for the one boot after ``reboot "0 tryboot"`` (Raspberry Pi
  documentation, "Fail-safe OS updates (tryboot)"; every Raspberry Pi model), so
  Recovery's **Start Installer…** opens the installer once and a later start without
  installing comes back to RetroBoot;
* ``retroboot/installer-config.txt``: the same, kept for the next installation.

Nothing here writes to the card.
"""
from __future__ import annotations

import re
from pathlib import Path

CONFIG_MARK = "# RetroBoot: installed beside the installer on this card (ARCHITECTURE 8.2)"
TRYBOOT_ARG = "0 tryboot"        # reboot(2) argument: default partition, tryboot flag
LIVE_RE = re.compile(r"(?:^|\s)boot=retroboot-live(?:\s|$)")
MARKER_RE = re.compile(r"(?:^|\s)rb\.live=(\S+)")


def _read(path: Path, limit: int = 65536) -> str | None:
    try:
        with open(path, "rb") as f:
            return f.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def cmdline_file(boot: Path, config: str) -> Path:
    """The command line a configuration file names: ``cmdline=`` (default cmdline.txt)
    under ``os_prefix=``, from its [all] sections (rb-bootdisplay-rpi's rule)."""
    prefix, name, active = "", "cmdline.txt", True
    for raw in config.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            active = line.lower() == "[all]"
            continue
        key, sep, value = line.partition("=")
        if not active or not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "os_prefix":
            prefix = value
        elif key == "cmdline" and value:
            name = value
    rel = name.lstrip("/") if name.startswith("/") else prefix + name
    if ".." in Path(rel).parts:
        rel = "cmdline.txt"
    return boot / rel


def installer_beside(boot: str | Path) -> dict | None:
    """{"tryboot", "cmdline", "build"} when this card starts RetroBoot and keeps the installer
    for a tryboot restart; None otherwise (any other Pi card, a PC)."""
    boot = Path(boot)
    config = _read(boot / "config.txt")
    if config is None or not config.lstrip("﻿").startswith(CONFIG_MARK):
        return None
    tryboot = _read(boot / "tryboot.txt")
    if tryboot is None:
        return None
    cmd_path = cmdline_file(boot, tryboot)
    cmdline = _read(cmd_path, 8192) or ""
    if not LIVE_RE.search(cmdline):
        return None
    m = MARKER_RE.search(cmdline)
    build = m.group(1).removeprefix(".retroboot-installer-") if m else None
    return {"tryboot": str(boot / "tryboot.txt"), "cmdline": str(cmd_path), "build": build}
