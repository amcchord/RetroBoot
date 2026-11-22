"""PowerPC Mac OS X profiles, isolated from the classic Mac OS launch path.

The profile is per Mac, so switching Startup Disk changes both its launch contract
and its preboot presentation. No classic extension, disk preparation, shared-folder
driver or accelerated-boot timing is applied to an OS X volume.
"""
from __future__ import annotations

import re
import os
import struct
import zlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .audio import qemu_audiodev
from .machines import MachineError, MachineUnavailable, guest_geometry, safe_path

FEATURE = "macosx-runtime-1"
QEMU_FEATURE = "macosx-qemu-10.0-1"
NVRAM_FEATURE = "macosx-nvram-flash-1"
NVRAM_FILE = "nvram.img"
NVRAM_SEED = "share/qemu/macosx-flash-seed.img"
NVRAM_BYTES = 16384
FIRMWARE = "share/qemu/openbios-ppc-osx"
FIRMWARE_FEATURE = "macosx-firmware-1"
TABLET_FEATURE = "macosx-tablet-compat-1"
WAKE_HID_FEATURE = "macosx-pmu-hid-wake-1"
SLEEP_FEATURES = {"macosx-pmu-sleep-1", "macosx-7400-dl1hwf-1"}
CODEC_FEATURE = "macosx-screamer-codec-1"
TESTED_BUILDS = {"macosx-10.2": "6R73", "macosx-10.3": "7W98", "macosx-10.4": "8S165"}
PACKAGE_FILES = ("bin/qemu-system-ppc", FIRMWARE, NVRAM_SEED, "share/qemu/qemu_vga.ndrv")
QEMU_SOURCE = {
    "version": "10.0.13", "commit": "98f88efe8f07067667b8d9b1a5ce9551d34036c1",
    "debian_version": "1:10.0.13+ds-0+deb13u1",
    "debian_orig_sha256": "6a0888e806c2ffc0c5f0733e974f9aea55491950d3c47c98669c8b236dde59ae",
}
NO_PACKAGE = "This RetroBoot needs an emulator update before it can start Mac OS X."


def package_capable(root: Path) -> bool:
    from .util import read_json
    try:
        manifest = read_json(Path(root) / "manifest.json", 16 * 1024 * 1024)
        source = (manifest.get("sources") or {}).get("qemu") or {}
        return {FEATURE, QEMU_FEATURE, NVRAM_FEATURE, FIRMWARE_FEATURE,
                "display-retroboot"}.issubset(manifest.get("features") or ()) and \
               all(source.get(key) == value for key, value in QEMU_SOURCE.items())
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _header_sum(header: bytes) -> int:
    header = bytearray(header)
    header[1] = 0
    total = sum(header)
    while total > 255:
        total = (total & 255) + (total >> 8)
    return total


def valid_banks(data: bytes) -> list[bytes]:
    banks = []
    if len(data) == NVRAM_BYTES:
        for offset in (0, 8192):
            bank = data[offset:offset + 8192]
            if bank[0] == 0x5a and struct.unpack_from(">H", bank, 2)[0] == 2 \
                    and bank[1] == _header_sum(bank[:16]) \
                    and struct.unpack_from(">I", bank, 16)[0] == zlib.adler32(bank[20:], 1):
                banks.append(bank)
    return banks


def check_nvram(data: bytes) -> bytes:
    """One complete bank may survive an interrupted native flash update."""
    if valid_banks(data):
        return data
    raise MachineError("This Mac's OS X startup memory is damaged. Restore a checkpoint or import the Mac again.")


def rtc_offset(data: bytes) -> int:
    """Native PPC XNU subtracts signed24-bit XPRAM[0xEC] from its local RTC.

    Select the valid newest bank as the pinned firmware does. Do not guess on
    conflicting generations, invalid partition headers, or a missing XPRAM.
    """
    banks = valid_banks(check_nvram(data))
    banks.sort(key=lambda bank: struct.unpack_from(">I", bank, 20)[0])
    if len(banks) == 2:
        generations = [struct.unpack_from(">I", bank, 20)[0] for bank in banks]
        if (generations[0] == generations[1] and banks[0] != banks[1]) or generations[1] - generations[0] >= 0x80000000:
            raise MachineError("This Mac's startup-memory clock record is ambiguous.")
    bank, offset, found = banks[-1], 0, []
    while offset < len(bank):
        header = bank[offset:offset + 16]
        if len(header) != 16 or header[1] != _header_sum(header):
            raise MachineError("This Mac's startup-memory partition record is damaged.")
        length = struct.unpack_from(">H", header, 2)[0] * 16
        if length < 16 or offset + length > len(bank):
            raise MachineError("This Mac's startup-memory partition record is damaged.")
        if header[4:16].rstrip(b"\0") == b"APL,MacOS75":
            if length < 16 + 256:
                raise MachineError("This Mac's startup-memory clock record is incomplete.")
            value = struct.unpack_from(">I", bank, offset + 16 + 0xec)[0] & 0xffffff
            found.append(value - 0x1000000 if value & 0x800000 else value)
        offset += length
    if len(found) != 1 or not -86400 <= found[0] <= 86400:
        raise MachineError("This Mac's startup-memory clock record is not valid.")
    return found[0]


def rtc_base(data: bytes, now: datetime | None = None) -> str:
    offset = rtc_offset(data)
    if not offset:
        return "base=utc"
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc) + timedelta(seconds=offset)
    return "base=" + instant.strftime("%Y-%m-%dT%H:%M:%S")


def read_nvram(path: Path) -> bytes:
    from .quadra import read_regular
    try:
        return check_nvram(read_regular(path, NVRAM_BYTES))
    except (OSError, ValueError) as exc:
        if isinstance(exc, MachineError):
            raise
        raise MachineError("This Mac's OS X startup memory could not be read.") from exc


def prepare(machine, package: Path) -> bool:
    """Make a private per-Mac copy of the verified original seed, only when absent."""
    from .machines import verify_package
    from .quadra import write_new
    if os.path.lexists(machine.nvram):
        read_nvram(machine.nvram)
        return False
    if not package_capable(package):
        raise MachineUnavailable(NO_PACKAGE)
    try:
        verify_package(package, (NVRAM_SEED,))
        seed = read_nvram(Path(package) / NVRAM_SEED)
    except MachineError:
        raise MachineUnavailable("RetroBoot's OS X startup-memory template is not valid. Install an emulator update.") from None
    write_new(machine.dir, machine.nvram, seed, 0o660)
    return True


def build_argv(machine, settings: dict, paths, display: dict | None, options) -> list[str]:
    """Conservative G4/PMU profile; native OS X drivers own USB and Ethernet.

    The separate pinned runtime has no OS 9 CPU fast paths.
    ATI/Quartz acceleration is a separate qualification and is never inferred from a
    classic Mac's ATI extensions or host OpenGL capability.
    """
    if not package_capable(paths.qemu_osx_package):
        raise MachineUnavailable(NO_PACKAGE)
    ram = settings.get("machine.osx_ram_mb", "auto")
    if ram == "auto":
        ram = settings["machine.ram_mb"]
    floor = 256 if machine.guest_os == "macosx-10.4" else 128
    if type(ram) is not int or not floor <= ram <= 2048:
        raise MachineError(f"This Mac OS X version needs {floor} MB to 2048 MB of memory.")
    tb = options.tb_size_mb
    if type(tb) is not int or tb not in (128, 256, 512):
        raise MachineError("Unsupported translation buffer size")
    package = Path(safe_path(paths.qemu_osx_package, "OS X emulator package", "dir"))
    bios = package / "share/qemu"
    qemu = safe_path(package / "bin/qemu-system-ppc", "emulator")
    firmware = safe_path(package / FIRMWARE, "Mac OS X Open Firmware")
    disk = safe_path(machine.disk, "disk")
    rtc = rtc_base(read_nvram(machine.nvram))
    nvram = safe_path(machine.nvram, "OS X startup memory")
    w, h = guest_geometry(display, settings["display.guest_size"], settings.get("display.pixel_scale", "auto"))
    name = re.sub(r"[^A-Za-z0-9 ._-]", "", machine.name)[:40] or machine.id
    audio = qemu_audiodev(options.audio_device) if settings["audio.enabled"] and options.audio_capable else "none,id=snd0"
    args = [qemu, "-accel", f"tcg,tb-size={tb}", "-M", "mac99,via=pmu,usb=off,audiodev=snd0",
            "-cpu", "7400", "-m", str(ram),
            "-L", str(bios), "-bios", firmware, "-global", "macio-nvram.osx-flash=on",
            "-drive", f"if=none,id=osxnv,file={nvram},format=raw", "-global", "macio-nvram.drive=osxnv",
            "-vga", "std", "-global", "VGA.romfile=", "-global", "VGA.vgamem_mb=64", "-g", f"{w}x{h}x32",
            "-prom-env", "output-device=ttya", "-name", name, "-audiodev", audio,
            "-action", "reboot=shutdown", "-rtc", rtc,
            "-qmp", f"unix:{safe_path(paths.qmp_control, 'qmp control', 'output')},server=on,wait=off",
            "-qmp", f"unix:{safe_path(paths.qmp_events, 'qmp events', 'output')},server=on,wait=off",
            "-serial", f"file:{safe_path(paths.serial_log, 'serial log', 'output')}",
            "-drive", f"file={disk},format=raw,media=disk,index=0"]
    from .util import read_json
    features = read_json(package / "manifest.json", 16 * 1024 * 1024).get("features") or []
    tested_build = machine.meta.get("os_build") == TESTED_BUILDS.get(machine.guest_os)
    if not options.boot_cd and tested_build:
        # Native quiesce must complete before a real G4 hardware sleep/wake.
        # Neither capability alone qualifies the paired device/CPU repair.
        if machine.guest_os in ("macosx-10.3", "macosx-10.4") and SLEEP_FEATURES.issubset(features):
            args += ["-global", "via-pmu.osx-sleep=on"]
        # The native DMA/codec contract belongs to the Mac, even when host
        # playback is disabled or unavailable. The audiodev above alone selects
        # silent output without changing the native DMA/IRQ behavior.
        if CODEC_FEATURE in features:
            args += ["-global", "screamer.osx-codec=on", "-global", "screamer.dma-lead-us=0"]
            if audio.startswith("alsa,"):
                # Start the real ALSA buffer after 40 ms of native PCM. This
                # reduces startup underruns without synthesizing audio or
                # changing the Mac's DMA clock. Classic/silent paths retain
                # their own policy; first-client completeness remains open.
                index = args.index("-audiodev") + 1
                args[index] += ",out.try-poll=off,threshold=40000"
                if options.osx_48k_output and machine.guest_os == "macosx-10.4":
                    args[index] += ",out.frequency=48000"
    if "macosx-vga-edid-1" in features:
        # The separate OS X NDRV reads the detailed EDID timing. Its geometry
        # must agree with the firmware's initial framebuffer hint.
        args += ["-global", f"VGA.xres={w}", "-global", f"VGA.yres={h}"]
    from . import graphics
    if not options.boot_cd and graphics.effective(settings, machine, package) == graphics.ATI:
        for value in ("std", "VGA.romfile=", "VGA.vgamem_mb=64"):
            index = args.index(value)
            del args[index - 1:index + 1]
        for value in (f"VGA.xres={w}", f"VGA.yres={h}"):
            if value in args:
                index = args.index(value)
                del args[index - 1:index + 1]
        screen = graphics.ati_size((w, h), exact=True)
        from .osx_graphics import (COHERENT_FEATURE, I8_FEATURE, YUV_VIEW_FEATURE, YUV_RAM_FEATURE,
                                   DEPTH_MEMORY_FEATURE, SELECTIVE_2D_FEATURE,
                                   TEXTURE_PAGE_FEATURE, RAM_SOURCE_FEATURE)
        ram_source_profile = machine.guest_os == "macosx-10.4" and tested_build and {
            RAM_SOURCE_FEATURE, TEXTURE_PAGE_FEATURE, SELECTIVE_2D_FEATURE,
            COHERENT_FEATURE, DEPTH_MEMORY_FEATURE,
        }.issubset(features)
        # The qualified C17 Tiger driver exposes 1280x800 when it is the
        # primary timing, while retaining a 1920x1080 initial framebuffer.
        # Keep -g and the requested desktop geometry at the full display size.
        # Only newly qualified editions declare the paired firmware-desktop
        # helper. Legacy EDID-only login helpers would shrink the desktop.
        paired_desktop = machine.meta.get("template", {}).get("osxDisplaySetup") == "firmware-desktop-1"
        edid_screen = (1280, 800) if ram_source_profile and paired_desktop and screen == (1920, 1080) else screen
        device = ("ati-vga,model=r100,vgamem_mb=32,romfile=,addr=0x11,x-agp=on,guest_hwcursor=on,"
                  # EDID uses millihertz. Match the card's existing 60 Hz
                  # vblank; QEMU's unspecified EDID default is 75 Hz. This
                  # describes the display without changing its timer.
                  f"xres={edid_screen[0]},yres={edid_screen[1]},refresh_rate=60000")
        if COHERENT_FEATURE in features:
            device += ",x-coherent-vram=on"
        if I8_FEATURE in features:
            device += ",x-i8-byte-order=on"
        if machine.guest_os == "macosx-10.4" and tested_build and YUV_VIEW_FEATURE in features:
            # Qualified Tiger packed-YUV uploads use the legacy 16-bit CPU
            # component-pair view. Other guest builds retain their own path.
            device += ",x-yuv16-aperture-view=on"
        if (machine.guest_os, machine.meta.get("os_build")) == ("macosx-10.4", "8S165") and \
                {YUV_RAM_FEATURE, YUV_VIEW_FEATURE, COHERENT_FEATURE,
                 TEXTURE_PAGE_FEATURE}.issubset(features):
            # This separate capability qualifies packed-YUV sources in ordinary
            # RAM. Its explicit endian handling is independent of selective 2D.
            device += ",x-yuv-ram-byte-order=on"
        if machine.guest_os == "macosx-10.4" and tested_build and \
                DEPTH_MEMORY_FEATURE in features and COHERENT_FEATURE in features:
            # Shared physical depth memory requires the qualified guest,
            # coherent apertures and the separately verified renderer API.
            device += ",x-depth-memory=on"
        if machine.guest_os == "macosx-10.4" and tested_build and \
                {SELECTIVE_2D_FEATURE, COHERENT_FEATURE, DEPTH_MEMORY_FEATURE}.issubset(features):
            # Qualified shared-depth Tiger keeps ordered synchronization but
            # avoids exporting unrelated GPU targets for ordinary 2D copies.
            device += ",x-selective-2d-sync=on"
            if ram_source_profile:
                # Only proven ordinary-RAM sources may omit unrelated drains.
                # Older packages and unqualified guest profiles keep their path.
                device += ",x-selective-2d-ram=on"
        args += ["-vga", "none", "-device", device,
                 "-global", "uni-north-pci-pcihost.x-agp-compat=on"]
    if options.lab_qmp is not None:
        args += ["-qmp", f"unix:{safe_path(options.lab_qmp, 'lab qmp', 'output')},server=on,wait=off"]
    if options.display_backend == "retroboot":
        args += ["-display", f"retroboot,socket={safe_path(paths.display_sock, 'display socket', 'output')}"]
    elif options.display_backend == "vnc":
        args += ["-display", "none", "-vnc", f"unix:{safe_path(paths.vnc_sock, 'vnc socket', 'output')}"]
    else:
        raise MachineError("Unsupported display backend")
    cd = options.boot_cd or options.cd_image
    cd_drive = bool(options.boot_cd and options.boot_cd_drive)
    medium = "if=ide,index=2,media=cdrom,id=cd0,readonly=on"
    if cd is not None:
        if cd_drive:
            medium += f",file.driver=host_cdrom,file.filename={safe_path(cd, 'CD-ROM drive', 'device')},format=raw"
        else:
            medium += f",file={safe_path(cd, 'CD image')},format=raw"
    second = "if=ide,index=3,media=cdrom,id=cd1,readonly=on"
    if options.cd_drive is not None and not cd_drive:
        second += f",file.driver=host_cdrom,file.filename={safe_path(options.cd_drive, 'CD-ROM drive', 'device')},format=raw"
    args += ["-drive", medium, "-drive", second, "-boot", "d" if options.boot_cd else "c"]
    args += ["-nic", "user,model=sungem" if settings.get("network.guest", "nat") == "nat" else "none"]
    # mac99 otherwise creates its own keyboard + relative mouse. Own the USB
    # controller explicitly to avoid duplicate mice selecting the wrong pointer.
    pointer = "usb-mouse" if machine.guest_os == "macosx-10.2" else "usb-tablet"
    if machine.guest_os == "macosx-10.4" and machine.meta.get("os_build") == "8S165" \
            and TABLET_FEATURE in features:
        # Tiger's original HID driver trims 7.5% from each logical edge.
        # Panther uses the full range; applying this there distorts input.
        pointer += ",macosx-compat=on"
    keyboard = "usb-kbd,bus=osx-usb.0"
    if not options.boot_cd and machine.guest_os == "macosx-10.4" and tested_build \
            and WAKE_HID_FEATURE in features and SLEEP_FEATURES.issubset(features):
        # Keep the physical held state across Tiger's USB resets on wake.
        # Other versions retain their separately qualified keyboard behavior.
        keyboard += ",x-keep-keys-on-reset=on"
    args += ["-device", "pci-ohci,id=osx-usb", "-device", keyboard,
             "-device", f"{pointer},bus=osx-usb.0"]
    return args
