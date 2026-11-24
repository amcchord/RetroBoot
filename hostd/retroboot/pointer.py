"""USB mouse for Mac OS's right button and scroll wheel (USB Overdrive).

The virtio tablet gives Mac OS the absolute pointer position (classicvirtio's
driver), which the web Screen page and pixel-scaled screens need.  Mac OS 9 has
no right-click or scroll wheel of its own; USB Overdrive 1.4, preinstalled and
registered on the base disk (``rbguestprep usb-overdrive``), turns a USB
mouse's right button into Control-click, its wheel into scrolling (3 lines a
notch) and its middle button into Command-click.

So when the machine's disk has USB Overdrive, hostd adds a QEMU ``usb-mouse``
on mac99's OHCI bus next to the tablet.  QEMU routes each input event to the
most recently activated handler that takes its kind: the USB mouse activates
when Mac OS's USB driver first polls it (during the extension parade), so from
then on every button and wheel notch goes to it, while absolute positions still
go to the tablet (a usb-mouse takes no absolute events).  The pointer is never
moved twice and its position stays exact; before the USB driver loads, buttons
reach the tablet as before.  ``wheel-per-report=1`` (package feature
``hid-wheel-per-report``) makes QEMU deliver every notch in its own HID report,
because USB Overdrive scrolls once per report whatever the notch count in it.

A disk without USB Overdrive keeps the tablet alone: there Mac OS's own USB
driver would turn the right button into a plain click, whereas classicvirtio
makes it a Control-click.  hostd.json ``usb_mouse`` overrides: ``auto``
(default), ``on``, ``off``.
"""
from __future__ import annotations

import json
from pathlib import Path

FEATURE = "hid-wheel-per-report"
MODES = ("auto", "on", "off")
WHEEL_PER_REPORT = 1


def package_wheel_per_report(package_root: Path) -> bool:
    """The emulator package's usb-mouse has ``wheel-per-report``."""
    try:
        m = json.loads((Path(package_root) / "manifest.json").read_text())
    except (OSError, ValueError):
        return False
    return FEATURE in (m.get("features") or [])


def wanted(mode: str, overdrive_present: bool | None) -> bool:
    """Whether this disk boot gets the USB mouse."""
    if mode == "off":
        return False
    if mode == "on":
        return True
    return bool(overdrive_present)


def device_arg(wheel_per_report: int | None) -> str:
    """QEMU ``-device`` value for the USB mouse."""
    if wheel_per_report is None:
        return "usb-mouse"
    if not 1 <= int(wheel_per_report) <= 127:
        raise ValueError("wheel-per-report must be between 1 and 127")
    return f"usb-mouse,wheel-per-report={int(wheel_per_report)}"


def supports_usb_overdrive(machine=None, *, family: str | None = None,
                           guest_os: str | None = None, os_version: str | None = None) -> bool:
    """Whether this Mac / OS version can run USB Overdrive (False for 68k & pre-8.5 systems)."""
    if machine is not None:
        if getattr(machine, "quadra", False):
            return False
        family = family or getattr(machine, "family", None)
        meta = getattr(machine, "meta", None) or {}
        guest_os = guest_os or getattr(machine, "guest_os", None) or meta.get("guest_os")
        os_version = os_version or meta.get("os_version")
    if family == "quadra800":
        return False
    os_str = str(guest_os or "").strip().lower()
    ver_str = str(os_version or "").strip().lower()
    if os_str.startswith(("system-7", "macos-7", "mac-os-7", "macos-8.0", "macos-8.1", "mac-os-8.0", "mac-os-8.1")):
        return False
    if ver_str.startswith(("7.", "8.0", "8.1")):
        return False
    return True


def pointer_capabilities(machine=None, overdrive_present: bool | None = None, *,
                         family: str | None = None, guest_os: str | None = None,
                         os_version: str | None = None, mode: str = "auto") -> dict:
    """Expose clear right-click / scroll capability & fallback notice for panel/web UIs (#28)."""
    supported = supports_usb_overdrive(machine, family=family, guest_os=guest_os, os_version=os_version)
    use_mouse = wanted(mode, overdrive_present) if supported else False
    if not supported:
        notice = ("This Mac OS system does not support USB Overdrive; use Control-click for right-click "
                  "(scroll wheel and USB mouse emulation require Mac OS 8.5–9.2.2).")
    elif overdrive_present is False:
        notice = "USB Overdrive is not installed on this disk; use Control-click for right-click."
    else:
        notice = None
    return {
        "usb_overdrive_supported": supported,
        "usb_overdrive_present": bool(overdrive_present) if (supported and overdrive_present is not None) else (
            False if not supported else None
        ),
        "usb_mouse": use_mouse,
        "scroll_wheel": bool(supported and (overdrive_present is True or mode == "on")),
        "right_click_mode": "usb-overdrive" if use_mouse else "control-click",
        "notice": notice,
    }
