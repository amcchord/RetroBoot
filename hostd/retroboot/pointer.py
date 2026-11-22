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
