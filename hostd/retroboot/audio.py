"""Host audio output selection for the guest's sound (QEMU ``-audiodev alsa``).

ALSA's ``default`` device is card 0, and on real hardware card 0 is not always a
playback device: on the first mini PC tested (Ryzen HX 370) a USB microphone
enumerated as card 0, QEMU logged "Could not create a backend for voice
'screamer'" and Mac OS was silent. hostd therefore picks an explicit playback
PCM from ``/proc/asound`` at every guest launch:

1. an HDMI/DisplayPort output whose sink is present, has a valid ELD and
   advertises LPCM audio (a monitor or TV with speakers);
2. an analog codec output (laptop speakers, headphone/line out);
3. a USB audio device with playback;
4. otherwise ALSA ``default`` (no explicit device).

On a laptop whose built-in screen is the main display (``prefer="analog"``),
1 and 2 swap: Mac OS plays through the built-in speakers (or headphones) even
when an external display with speakers is connected (GPD MicroPC: the
JetKVM's HDMI sink otherwise took the sound away from the speakers).

The device string uses ``plughw`` (format and rate conversion in alsa-lib) and
the card *id* rather than its index, which can change between boots.

Sound Open Firmware (ASoC) cards, common on Intel laptops (N5105 laptop: Everest
ES8336, card driver ``sof-essx8336``), differ in two ways:

* their PCM ids end in `` (*)`` and the codec output is not on an "HDA" card, so
  a non-HDMI PCM of a ``sof-*`` card counts as analog;
* their mixer routing is not usable until the card's ALSA UCM profile has run
  (on the ES8336 the DAC is not even connected to the output stage). hostd runs
  the profile's boot sequence, the ``HiFi`` verb and its Speaker (or Headphones)
  device once per card with ``alsaucm`` (:func:`ucm_prepare`), and takes the
  profile's playback master element (ES8336: ``DAC``) as the volume control.

The ES8336 machine driver also mutes the speaker amplifier at every PCM stop and
unmutes it only when the codec's output stage powers up again. QEMU keeps the
PCM open and stops it after each Mac OS sound, so only the first sound after
opening was audible. For such cards the guest plays through ``rb_keepalive``
(``/etc/alsa/conf.d/50-retroboot.conf``: plug -> dmix -> hw), which keeps the
hardware stream running while QEMU has the device open.

HD Audio cards on non-Intel controllers (AMD, ATI, NVIDIA, VIA...) play through
``rb_keepalive`` too.  snd_hda_intel rounds their period and buffer sizes to 128
bytes (32 stereo 16-bit frames; Intel's controllers are exempt,
``AZX_DCAPS_NO_ALIGN_BUFSIZE``), and QEMU asks for its stream at 44.1 kHz with a
92 880 us buffer.  When the codec has no 44.1 kHz, ``plughw`` resamples to 48 kHz
and alsa-lib finds no aligned buffer near that time: every Mac OS sound failed
with "Failed to set buffer time to 92880: Invalid argument" and Mac OS was silent
(ThinkPad Z13 Gen 1: Realtek ALC287 with two CS35L41 amplifiers on AMD's
``1022:15e3``).  The dmix slave's fixed 48 kHz, 512/4096-frame configuration is
aligned on every controller and QEMU's request is met on the client side
(image/reports/z13-audio-2026-10-17.md).  ``snd_hda_intel.align_buffer_size``,
when set, overrides the vendor rule (:func:`hda_aligned`).

An HD Audio codec's output stages sit below its ``Master`` control, which hostd
uses as the volume (network.AudioMixer).  At boot ``alsactl`` initializes each card:
its generic rules turn Speaker and Headphone on at 0 dB, but on AMD machines with
ACP digital microphones alsa-ucm-conf's ``HDA`` profile does it instead
(``ucm2/HDA/init.conf``), and it leaves Speaker and Headphone off at 60 % and
Auto-Mute off for a sound server to switch.  RetroBoot has none, so the speakers
stayed muted (ThinkPad Z13 Gen 1: QEMU played without errors, the codec's speaker
pin muted).  Before each launch, once alsactl has finished (:func:`alsactl_running`),
hostd turns the chosen analog card's output stages
on, at 0 dB under a Master, and Auto-Mute on, so headphones mute the speakers by
the codec's own jack detection; on an HDMI/DP card it turns the IEC958 switches on
(each converter's digital output enable), which no init sets
(:func:`hda_output_stage`).

Raspberry Pi 4 and 5 (``prefer="pi"``):

* HDMI audio is the ``vc4-hdmi`` driver, one card per port (``vc4hdmi0`` is
  HDMI-A-1, ``vc4hdmi1`` HDMI-A-2).  Its ``hw:`` device takes only IEC958
  subframes, so the guest gets alsa-lib's card configuration
  (``/usr/share/alsa/cards/vc4-hdmi.conf``): the card's default, plug ->
  softvol -> IEC958, as ``default:CARD=vc4hdmi0`` or ``sysdefault:CARD=vc4hdmi0``
  (:func:`vc4_pcm`); the raw ``hdmi:CARD=vc4hdmi0,DEV=0`` only without that
  file.  These
  cards have no ELD in ``/proc/asound``; a port counts as connected when DRM
  reports its connector connected and the display's EDID offers audio, and only
  while RetroBoot drives that port (the presenter's primary first, then its
  mirrors): vc4 plays HDMI audio only on an enabled output.
* Then a USB audio device, then the Pi 4's 3.5 mm jack (``bcm2835 Headphones``,
  ``dtparam=audio=on``; the Pi 5 has none).
* vc4 HDMI has no hardware volume, but alsa-lib's ``default`` PCM for it is
  plug -> softvol -> IEC958, and the softvol control ("PCM Playback Volume" on
  the card, ``PCM`` to amixer) exists once that PCM has been opened.
  :func:`vc4_softvol` opens it once with 10 ms of silence before hostd probes
  the mixer, so ``audio.volume`` and the volume keys work on HDMI too.

Gemini PDA (``prefer="gemini"``):

* One card, the mainline MT6797 AFE with the MT6351 codec (driver and name
  ``mt6797-mt6351``).  Its front-end PCMs are DPCM links; ``Playback_1``
  (device 0, the AFE's DL1) is the one wired to the codec's DAC, and counts
  as the analog output; the other PCMs (DL2, DL3, hostless loopbacks) never
  do.  ``plughw`` converts to the AFE's S16_LE.
* Playback is silent until DL1 is routed to the ADDA interface and the
  headphone muxes select it, and the speakers hang off the same headphone
  drivers through two amplifiers on SoC GPIOs: the Gemini image's
  ``rb-gemini-audio`` sets the route at boot and switches the amplifiers with
  the stream (speakers.py offers speakers or headphones only through it).
* Volume: the codec's ``Headphone Volume`` (``Headphone`` to amixer, 19 steps,
  -10 to +8 dB, no switch), so mute is 0 % (-10 dB).
* The emulator's host-stream pre-roll is longer (:func:`screamer_options`, the
  profile's ``screamer`` key).  The screamer completes Mac OS's sound DMA in real
  time but waits, up to ``irq-hold-ms`` (100 ms), while Mac OS has not serviced the
  previous sound interrupt; on the Gemini's slow vCPU that wait reached 95 ms when
  QuickTime started a movie's sound (alerts: under 1 ms).  The default 40 ms pre-roll
  then ran ALSA dry (one XRUN and a ~110 ms gap at the start of the soundtrack);
  120 ms covers the whole hold.  ``host-lead-strict`` (packages with the
  screamer-lead-strict feature) keeps that pre-roll when the DMA pauses between two
  of Mac OS's DBDMA programs, which otherwise started the stream with 35 ms in one
  run (wave7/gemini-audio, emulator/reports/gemini-audio-20261014.md).  PCs and
  Raspberry Pis keep QEMU's defaults.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("retroboot.audio")

CHIME_NAMES = ("os9", "quadra", "powermac", "macii", "se")
CHIME_SETTINGS = ("auto", *CHIME_NAMES, "off")
CHIMES_DIR_DEFAULT = Path("/usr/share/retroboot/chimes")

ASOUND = Path("/proc/asound")
DRM_SYSFS = Path("/sys/class/drm")
SYSFS = Path("/sys")
# PCI class of HD Audio controllers (pending_sound).
HDA_PCI_CLASS = "0x0403"
# A sound card still being set up at boot is waited for (pending_sound): at most
# this long in all per hostd run, and only this soon after the kernel started.
SOUND_WAIT_S = 8.0
SOUND_WAIT_UNTIL_UPTIME_S = 30.0
# Raspberry Pi: vc4's HDMI cards (one per port) and the Pi 4's analog jack.
VC4_HDMI_DRIVER = "vc4-hdmi"
PI_ANALOG_DRIVERS = ("bcm2835_headpho", "bcm2835 Headpho", "bcm2835_headphones")
UCM_ROOT = Path("/usr/share/alsa/ucm2/conf.d")
# Machine drivers that mute the speaker amplifier at each PCM stop and unmute it
# only on the next output power-up (sound/soc/intel/boards/sof_es8336.c).
KEEPALIVE_DRIVERS = {"sof-essx8336"}
# snd_hda_intel aligns buffers on every HD Audio controller but Intel's (hda_aligned).
INTEL_PCI_VENDOR = "0x8086"
# HD Audio output stages below Master, as snd-hda's generic parser names them (hda_output_stage).
HDA_OUTPUT_STAGES = ("Front", "Line Out", "Speaker", "Bass Speaker", "Headphone")
# 'Auto-Mute Mode' items that mute the speakers when headphones are plugged in, preferred first.
HDA_AUTO_MUTE = ("Enabled", "Speaker Only")
# Gemini PDA: the card's driver name and the PCM wired to the codec's DAC.
GEMINI_CARD_DRIVERS = {"mt6797-mt6351"}
GEMINI_PCM = "Playback_1"
GEMINI_MIXER = "Headphone"
_ucm_done: dict[str, dict | None] = {}
_CARD_LINE = re.compile(r"^\s*(\d+)\s+\[([^\]]+?)\s*\]:\s*(\S+)\s*-\s*(.*)$")


def _kv(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
        else:
            parts = line.split(None, 1)
            if len(parts) == 2:
                out[parts[0]] = parts[1].strip()
    return out


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def hda_aligned(card: int, sys_root: Path) -> bool:
    """Does snd_hda_intel round this HD Audio card's period and buffer sizes to 128
    bytes?  ``align_buffer_size`` when set (0 or 1; -1 is automatic), otherwise every
    controller but an Intel one (hda_intel.c: AZX_DCAPS_NO_ALIGN_BUFSIZE)."""
    try:
        param = int(_read(sys_root / "module" / "snd_hda_intel" / "parameters" / "align_buffer_size").strip())
    except ValueError:
        param = -1
    if param >= 0:
        return param > 0
    vendor = _read(sys_root / "class" / "sound" / f"card{card}" / "device" / "vendor").strip().lower()
    return bool(vendor) and vendor != INTEL_PCI_VENDOR


def playback_outputs(root: Path = ASOUND, sys_root: Path | None = None) -> list[dict]:
    """Every playback PCM with its kind (hdmi, analog, usb, other) and sink state.
    ``sys_root``: the sysfs beside ``root`` (default: /sys for the live /proc/asound,
    none for another tree), read for HD Audio buffer alignment (hda_aligned)."""
    if sys_root is None and root == ASOUND:
        sys_root = SYSFS
    cards = {}
    for line in _read(root / "cards").splitlines():
        m = _CARD_LINE.match(line)
        if m:
            cards[int(m.group(1))] = {"id": m.group(2).strip(), "driver": m.group(3), "name": m.group(4).strip()}
    outputs = []
    for index, card in sorted(cards.items()):
        cdir = root / f"card{index}"
        elds = {}
        for eld in sorted(cdir.glob("eld#*")):
            info = {}
            for line in _read(eld).splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2:
                    info[parts[0]] = parts[1].strip()
            pin = eld.name.rsplit(".", 1)[-1]
            if pin.isdigit():
                elds[int(pin)] = info
        for info_path in sorted(cdir.glob("pcm*p/info")):
            info = _kv(_read(info_path))
            if info.get("stream") != "PLAYBACK":
                continue
            try:
                device = int(info.get("device", ""))
            except ValueError:
                continue
            pid = re.sub(r"\s*\(\*\)$", "", info.get("id", ""))       # ASoC marks PCMs "(*)"
            out = {"card": index, "card_id": card["id"], "driver": card["driver"], "device": device,
                   "name": re.sub(r"\s*\(\*\)$", "", info.get("name") or "") or pid, "kind": "other",
                   "connected": None}
            asoc = card["driver"].startswith("sof-")
            m = re.fullmatch(r"(HDMI|DP)\s*(\d+)", pid)
            if m:
                out["kind"] = "hdmi"
                eld = elds.get(int(m.group(2)), {})
                out["connected"] = (eld.get("monitor_present") == "1" and eld.get("eld_valid") == "1"
                                    and _sad_count(eld) > 0)
                if eld.get("monitor_name"):
                    out["sink"] = eld["monitor_name"]
            elif card["driver"] == VC4_HDMI_DRIVER:
                # vc4hdmi0 / "vc4-hdmi-0" -> HDMI-A-1 (DRM numbers connectors from 1)
                port = re.search(r"(\d+)$", card["id"]) or re.search(r"(\d+)$", card["name"])
                out["kind"] = "hdmi"
                out["vc4"] = True
                out["connector"] = f"HDMI-A-{int(port.group(1)) + 1}" if port else None
            elif card["driver"] in GEMINI_CARD_DRIVERS:
                # Gemini PDA: only DL1 reaches the codec (speakers and the jack)
                if pid == GEMINI_PCM:
                    out["kind"] = "analog"
                    out["gemini"] = True
                    out["mixer"] = GEMINI_MIXER
            elif card["driver"] == "USB-Audio":
                out["kind"] = "usb"
            elif card["driver"].startswith("HDA") or asoc or card["driver"] in PI_ANALOG_DRIVERS \
                    or card["name"].startswith("bcm2835 Headphones"):
                out["kind"] = "analog"
            if asoc:
                out["asoc"] = True
            if card["driver"] in KEEPALIVE_DRIVERS and out["kind"] == "analog":
                out["keepalive"] = True
            if (card["driver"].startswith("HDA") and out["kind"] in ("analog", "hdmi") and sys_root is not None
                    and hda_aligned(index, sys_root)):
                out["aligned"] = out["keepalive"] = True
            outputs.append(out)
    return outputs


def _sad_count(eld: dict) -> int:
    try:
        return int(eld.get("sad_count", "0"))
    except ValueError:
        return 0


def _match_output(outputs: list[dict], output: str | None, prefer: str = "hdmi",
                  *, fallback: bool = True, bluetooth_sinks: list[dict] | None = None) -> dict | None:
    req = (output or "auto").strip()
    if not req or req == "auto":
        return pick_output(outputs, prefer)
    lower = req.lower()
    if lower in ("speakers", "analog"):
        found = next((o for o in outputs if o.get("kind") == "analog"), None)
        return found if (found is not None or not fallback) else pick_output(outputs, prefer)
    if lower == "hdmi":
        found = next((o for o in outputs if o.get("kind") == "hdmi" and o.get("connected")), None)
        if found is None:
            found = next((o for o in outputs if o.get("kind") == "hdmi" and o.get("connected") is not False), None)
        return found if (found is not None or not fallback) else pick_output(outputs, prefer)
    if lower == "usb":
        found = next((o for o in outputs if o.get("kind") == "usb"), None)
        return found if (found is not None or not fallback) else pick_output(outputs, prefer)
    if lower == "bluetooth" or req.startswith("bt:"):
        sinks = list(bluetooth_sinks or ())
        if req.startswith("bt:"):
            target = req[3:]
            found = next((s for s in sinks if s.get("pw_sink") == target or s.get("id") == req), None)
        else:
            found = next((s for s in sinks if s.get("available", True)), None)
        if found is None:
            found = next((o for o in outputs if o.get("kind") == "bluetooth"), None)
        return found if (found is not None or not fallback) else pick_output(outputs, prefer)
    if req.startswith("card:"):
        spec = req[5:]
        card_id, _, dev_str = spec.partition(":")
        for o in outputs:
            if o.get("card_id") == card_id and (not dev_str or str(o.get("device")) == dev_str):
                return o
        return pick_output(outputs, prefer) if fallback else None
    if ":" in req or "=" in req:
        try:
            return configured(req)
        except ValueError:
            return pick_output(outputs, prefer) if fallback else None
    found = next((o for o in outputs if o.get("card_id") == req or o.get("id") == req), None)
    return found if (found is not None or not fallback) else pick_output(outputs, prefer)


def pick_output(outputs: list[dict], prefer: str = "hdmi", output: str = "auto") -> dict | None:
    if output and output != "auto":
        return _match_output(outputs, output, prefer, fallback=True)
    hdmi = lambda o: o["kind"] == "hdmi" and o["connected"]  # noqa: E731
    analog = lambda o: o["kind"] == "analog"  # noqa: E731
    usb = lambda o: o["kind"] == "usb"  # noqa: E731
    if prefer == "pi":
        order = (hdmi, usb, analog)        # Raspberry Pi: the HDMI port shown on, USB, the Pi 4's jack
    elif prefer == "gemini":
        order = (usb, analog)              # Gemini PDA: a USB sound card when plugged in, else the codec
    else:
        first = (analog, hdmi) if prefer == "analog" else (hdmi, analog)
        order = (*first, usb)
    for want in order:
        for o in outputs:
            if want(o):
                return o
    return None


def edid_has_audio(edid: bytes) -> bool | None:
    """Does a display's EDID offer audio (a CEA-861 extension with the basic-audio
    flag or an Audio Data Block)?  None when there is no usable EDID."""
    if len(edid) < 128 or edid[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None
    for i in range(1, edid[126] + 1):
        block = edid[128 * i:128 * (i + 1)]
        if len(block) < 128:
            break
        if block[0] != 0x02:                       # not a CEA-861 extension
            continue
        if block[1] >= 2 and block[3] & 0x40:      # basic audio support
            return True
        end, j = min(block[2], 127), 4
        while 4 <= j < end:
            tag, length = block[j] >> 5, block[j] & 0x1f
            if tag == 1 and length >= 3:           # Audio Data Block with a short audio descriptor
                return True
            j += 1 + length
    return False


def _drm_connector(drm: Path, name: str) -> Path | None:
    """sysfs directory of connector `name` (e.g. HDMI-A-1) on the vc4 card (any card as
    a fallback); None when it does not exist."""
    found = []
    try:
        entries = sorted(drm.iterdir())
    except OSError:
        return None
    for d in entries:
        m = re.fullmatch(r"card(\d+)-(.+)", d.name)
        if not m or m.group(2) != name:
            continue
        try:
            driver = (drm / f"card{m.group(1)}" / "device" / "driver").resolve().name
        except OSError:
            driver = ""
        found.append((0 if "vc4" in driver else 1, d))
    return min(found)[1] if found else None


def vc4_port_state(output: dict, drm: Path = DRM_SYSFS) -> dict:
    """{"status": DRM connector status or None, "audio": EDID audio (True/False/None)}."""
    conn = _drm_connector(drm, output.get("connector") or "")
    if conn is None:
        return {"status": None, "audio": None}
    try:
        status = (conn / "status").read_text().strip()
    except OSError:
        status = None
    try:
        edid = (conn / "edid").read_bytes()
    except OSError:
        edid = b""
    return {"status": status, "audio": edid_has_audio(edid)}


def mark_vc4_ports(outputs: list[dict], driving: list[str] | None, drm: Path = DRM_SYSFS) -> list[dict]:
    """Raspberry Pi HDMI cards: connected = the port's display is connected, offers
    audio (or its EDID is unreadable) and RetroBoot shows a picture on it (``driving``,
    the presenter's connectors, primary first; None while the presenter has not
    reported: then any connected port, HDMI-A-1 first, as the presenter picks).
    The driven ports come first, in the presenter's order."""
    rank = {}
    for o in outputs:
        if not o.get("vc4"):
            continue
        st = vc4_port_state(o, drm)
        o["drm_status"] = st["status"]
        o["edid_audio"] = st["audio"]
        shown = driving is None or (o.get("connector") in driving)
        o["connected"] = st["status"] == "connected" and st["audio"] is not False and shown
        if driving and o.get("connector") in driving:
            rank[id(o)] = driving.index(o["connector"])
    return sorted(outputs, key=lambda o: (0, rank[id(o)]) if id(o) in rank else (1, 0))


def list_pcms(run=subprocess.run) -> set[str] | None:
    """PCM names alsa-lib knows (``aplay -L``); None when aplay is missing or fails."""
    if not shutil.which("aplay"):
        return None
    try:
        r = run(["aplay", "-L"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return {line.strip() for line in r.stdout.splitlines() if line and not line[0].isspace()}


ALSA_CARDS = Path("/usr/share/alsa/cards")


def vc4_pcm(card_id: str, pcms: set[str] | None, cards: Path = ALSA_CARDS) -> str:
    """The PCM that takes plain PCM samples on a vc4 HDMI card: the card's default
    from alsa-lib's ``vc4-hdmi.conf`` (plug -> softvol -> IEC958, so it has a volume
    control), named ``default:CARD=`` or ``sysdefault:CARD=`` (the same definition;
    ``aplay -L`` lists the second), and only without that configuration its raw
    ``hdmi:`` IEC958 device (no volume control)."""
    default, sysdefault, hdmi = (f"default:CARD={card_id}", f"sysdefault:CARD={card_id}",
                                 f"hdmi:CARD={card_id},DEV=0")
    if pcms is None or default in pcms:
        return default
    if sysdefault in pcms:
        return sysdefault
    if (cards / "vc4-hdmi.conf").is_file():
        return default            # defined there even when aplay -L does not list it
    return hdmi


_softvol_done: dict[str, bool] = {}


def vc4_softvol(output: dict | None, run=subprocess.run) -> bool | None:
    """Make sure the softvol control of a vc4 HDMI card's ``default`` PCM exists (see the
    module docstring): True when it does, False when it could not be created (the HDMI
    output not enabled yet; tried again at the next launch), None when not applicable.
    Opens the PCM only when the control is missing, once per card per hostd process."""
    alsa = str((output or {}).get("alsa") or "")
    if not output or not output.get("vc4") or not alsa.startswith(("default:", "sysdefault:")):
        return None
    card = output["card_id"]
    if _softvol_done.get(card):
        return True

    def present() -> bool:
        r = run(["amixer", "-c", card, "scontrols"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0 and any(m.group(1) == "PCM" for m in _SCONTROL.finditer(r.stdout or ""))
    try:
        ok = present()
        if not ok:
            run(["aplay", "-q", "-D", output["alsa"], "-t", "raw", "-f", "S16_LE", "-r", "48000", "-c", "2",
                 "-s", "480", "/dev/zero"], capture_output=True, text=True, timeout=10)
            ok = present()
    except (OSError, subprocess.SubprocessError):
        ok = False
    if ok:
        _softvol_done[card] = True
    return ok


def alsa_device(output: dict | None, pcms: set[str] | None = None) -> str | None:
    if not output:
        return None
    if output.get("keepalive"):
        return f"rb_keepalive:CARD={output['card_id']},DEV={output['device']}"
    if output.get("vc4"):
        return vc4_pcm(output["card_id"], pcms)
    return f"plughw:CARD={output['card_id']},DEV={output['device']}"


def has_ucm(output: dict, root: Path = UCM_ROOT) -> bool:
    drv = output.get("driver")
    return bool(output.get("asoc")) and bool(drv) and (root / str(drv)).is_dir() and bool(shutil.which("alsaucm"))


def ucm_prepare(output: dict | None, run=subprocess.run, root: Path = UCM_ROOT,
                asound: Path = ASOUND) -> dict | None:
    """Run an ASoC card's UCM boot sequence, HiFi verb and output device once (per
    hostd process); returns {"device", "mixer"} or None (no UCM, or it failed)."""
    if not output or not has_ucm(output, root):
        return None
    card = output["card_id"]
    if card in _ucm_done:
        return _ucm_done[card]
    result = None
    hw = f"hw:{card}"
    try:
        listing = run(["alsaucm", "-c", hw, "list", "_devices/HiFi"], capture_output=True, text=True, timeout=10)
        devices = re.findall(r"^\s*\d+:\s*(\S+)\s*$", listing.stdout, re.M)
        device = next((d for d in ("Speaker", "Headphones") if d in devices), None)
        argv = ["alsaucm", "-c", hw, "set", "_boot", "", "set", "_verb", "HiFi"]
        if device:
            argv += ["set", "_enadev", device, "get", f"PlaybackMasterElem/{device}"]
        done = run(argv, capture_output=True, text=True, timeout=15)
        if done.returncode == 0:
            m = re.search(r"PlaybackMasterElem/\S+=(.+)$", done.stdout, re.M)
            result = {"device": device, "mixer": m.group(1).strip() if m else None}
            # Warm the card's codec output even when HDMI on the same card was chosen first:
            # UCM runs once per card, and hostd's first choice at start can be HDMI (the
            # presenter has not reported the built-in panel yet), the next one the speakers.
            target = output if output.get("keepalive") else next(
                (o for o in playback_outputs(asound) if o["card_id"] == card and o.get("keepalive")), None)
            if target:
                result["warmed"] = warm_up(target, device, run, asound)
    except (OSError, subprocess.SubprocessError):
        result = None
    _ucm_done[card] = result
    return result


# DAPM pin switches of sof_es8336: the speaker amplifier and the headphone output.
PIN_SWITCHES = ("Speaker Switch", "Headphone Switch")
WARM_UP_FRAMES = 28800          # 0.6 s at 48 kHz


def stream_closed(output: dict, root: Path = ASOUND) -> bool:
    """True when nobody plays on the output's PCM (its first substream is closed)."""
    try:
        status = (root / f"card{output['card']}" / f"pcm{output['device']}p" / "sub0" / "status").read_text()
    except OSError:
        return False
    return status.strip() == "closed"


def _pin(run, card: str, name: str, value: str | None = None) -> str | None:
    """Read (value None) or set a pin switch; returns its value ("on"/"off") or None."""
    argv = ["amixer", "-c", card, "cget" if value is None else "cset", f"name={name}"]
    r = run(argv + ([value] if value is not None else []), capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        return None
    m = re.search(r":\s*values=(on|off)", r.stdout)
    return m.group(1) if m else value


def warm_up(output: dict, device: str, run=subprocess.run, root: Path = ASOUND) -> bool:
    """Turn the speaker amplifier off once, silently, before Mac OS first opens the output.

    sof_es8336 enables the speaker amplifier when it probes and turns it off only when a
    stream stops; later streams power the codec first and the amplifier after it.  So
    the first stream after power-on brought the codec's outputs up through a live
    amplifier: a loud pop at every boot, when QEMU opened the device (hardware, N5105:
    the first stream pops, every later one is quiet).  A short silent stream with both
    pin switches off powers no analog path, and when it stops the driver switches the
    amplifier off; the pins are then restored.  Skipped while a stream runs (hostd
    restarted under a running guest)."""
    if not stream_closed(output, root):
        return False
    card = output["card_id"]
    saved = {name: _pin(run, card, name) for name in PIN_SWITCHES}
    if any(v is None for v in saved.values()):
        return False
    try:
        for name in PIN_SWITCHES:
            _pin(run, card, name, "off")
        r = run(["aplay", "-q", "-D", f"hw:CARD={card},DEV={output['device']}", "-t", "raw", "-f", "S16_LE",
                 "-r", "48000", "-c", "2", "-s", str(WARM_UP_FRAMES), "/dev/zero"],
                capture_output=True, text=True, timeout=10)
        ok = r.returncode == 0
    finally:
        for name, value in saved.items():
            _pin(run, card, name, value)
    return ok


_SCONTROL = re.compile(r"^Simple mixer control '([^']+)',(\d+)\s*$", re.M)


def hda_output_stage(output: dict | None, run=subprocess.run) -> list[str] | None:
    """Turn on the codec output stages of the HD Audio card hostd plays on (see the
    module docstring), before QEMU opens it: an analog card's HDA_OUTPUT_STAGES on, at
    0 dB when the card has a Master control, and 'Auto-Mute Mode' on when it is off; an
    HDMI/DP card's IEC958 switches on.  Returns what was set (e.g. ["Speaker",
    "Headphone", "Auto-Mute Mode=Enabled"]), or None when the output is not an HD Audio
    analog or HDMI/DP one or amixer failed.  Safe while a stream plays."""
    if (not output or not str(output.get("driver") or "").startswith("HDA")
            or output.get("kind") not in ("analog", "hdmi")):
        return None
    card = output["card_id"]

    def amixer(*args: str) -> str | None:
        r = run(["amixer", "-c", card, *args], capture_output=True, text=True, timeout=5)
        return r.stdout if r.returncode == 0 else None
    try:
        listing = amixer("scontrols")
        if listing is None:
            return None
        names = [(m.group(1), int(m.group(2))) for m in _SCONTROL.finditer(listing)]
        done = []
        if output["kind"] == "hdmi":
            for name, index in names:
                if name == "IEC958" and amixer("-q", "sset", f"{name},{index}", "unmute") is not None:
                    done.append(f"{name},{index}")
            return done
        master = ("Master", 0) in names
        for name, index in names:
            if name not in HDA_OUTPUT_STAGES:
                continue
            elem = name if index == 0 else f"{name},{index}"
            caps = amixer("sget", elem) or ""
            values = (["100%"] if master and "pvolume" in caps else []) + (["unmute"] if "pswitch" in caps else [])
            if values and amixer("-q", "sset", elem, *values) is not None:
                done.append(elem)
        if ("Auto-Mute Mode", 0) in names:
            info = amixer("sget", "Auto-Mute Mode") or ""
            listed = re.search(r"^\s*Items:(.*)$", info, re.M)
            current = re.search(r"^\s*Item0:\s*'([^']+)'", info, re.M)
            items = re.findall(r"'([^']+)'", listed.group(1)) if listed else []
            want = next((i for i in HDA_AUTO_MUTE if i in items), None)
            if (current and current.group(1) == "Disabled" and want
                    and amixer("-q", "sset", "Auto-Mute Mode", want) is not None):
                done.append(f"Auto-Mute Mode={want}")
        return done
    except (OSError, subprocess.SubprocessError):
        return None


def pending_sound(sys_root: Path = SYSFS) -> list[str]:
    """HD Audio controllers whose codecs were found but whose card has no playback
    PCM yet: the driver is still loading codec modules. QEMU's sound device is
    chosen once per launch, so a launch in that window starts Mac OS without sound.
    On a ThinkPad X1 Carbon the card registered 4 s after its codecs appeared: the
    HDMI codec module's load waited for a slow USB card reader's probe (a module load
    synchronizes with every asynchronous probe), and hostd, started earlier by then,
    launched Mac OS silent (image/reports/lenovo-2026-10-15.md).

    A controller without codecs never registers a card (a Proxmox q35 VM's ICH9 HD
    Audio: waiting for it held every launch 8 s twice), and one whose codecs are not
    enumerated yet is not waited for either, so this never costs more than the
    window it was made for. Sound Open Firmware DSPs (class 0x0401) are not covered."""
    ready = set()
    for card in sorted((sys_root / "class" / "sound").glob("card*")):
        try:
            if any(card.glob("pcmC*D*p")):
                ready.add((card / "device").resolve().name)
        except OSError:
            continue
    pending = []
    for dev in sorted((sys_root / "bus" / "pci" / "devices").glob("*")):
        try:
            cls = (dev / "class").read_text().strip()
            if (cls.startswith(HDA_PCI_CLASS) and (dev / "driver").exists() and dev.name not in ready
                    and any(dev.glob("hdaudioC*D*"))):
                pending.append(dev.name)
        except OSError:
            continue
    return pending


PROC = Path("/proc")


def alsactl_running(proc: Path = PROC) -> bool:
    """Is alsactl still initializing sound cards (alsa-restore.service or udev at boot)?
    Its init sets the output stages that hda_output_stage turns on, so it must finish
    first: on the ThinkPad Z13 it ran from 3.9 to 5.5 s after the kernel started, the
    card's UCM init last, and hostd's first choice of output came at 5.3 s."""
    try:
        entries = list(proc.iterdir())
    except OSError:
        return False
    for p in entries:
        if p.name.isdigit():
            try:
                if (p / "comm").read_text().strip() == "alsactl":
                    return True
            except OSError:
                continue
    return False


def sound_setup_pending(sys_root: Path = SYSFS, proc: Path = PROC) -> list[str]:
    """What QEMU's sound device waits for at boot: HD Audio controllers whose card has no
    playback PCM yet (pending_sound) and a running alsactl ("alsactl")."""
    return pending_sound(sys_root) + (["alsactl"] if alsactl_running(proc) else [])


def _probe_bluetooth_sinks(run=subprocess.run, which=shutil.which) -> list[dict]:
    """Discover active Bluetooth audio sinks via pactl if a sound server is running."""
    pactl = which("pactl") if which else None
    if not pactl:
        return []
    try:
        r = run([pactl, "list", "short", "sinks"], capture_output=True, text=True, timeout=1.5)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0 or not r.stdout:
        return []
    sinks = []
    for line in r.stdout.splitlines():
        cols = line.split("\t")
        if len(cols) < 2:
            continue
        sink_name = cols[1].strip()
        if "bluez" not in sink_name.lower():
            continue
        label = sink_name.replace("bluez_output.", "").replace("bluez_sink.", "").split(".")[0].replace("_", ":")
        sinks.append({"kind": "bluetooth", "card": None, "card_id": None, "device": None,
                      "driver": "bluez", "name": f"Bluetooth Audio ({label})", "connected": True,
                      "pw_sink": sink_name, "alsa": "pulse" if which("paplay") else None})
    return sinks


def _prepared_outputs(root: Path = ASOUND, driving: list[str] | None = None,
                      drm: Path = DRM_SYSFS, pcms=list_pcms,
                      sys_root: Path | None = None,
                      bluetooth_sinks: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    outputs = playback_outputs(root, sys_root)
    if any(o.get("vc4") for o in outputs):
        outputs = mark_vc4_ports(outputs, driving, drm)
    if bluetooth_sinks is None:
        bt = _probe_bluetooth_sinks() if root == ASOUND else []
    else:
        bt = [dict(s, kind="bluetooth") for s in bluetooth_sinks]
    return outputs, bt


def list_outputs(root: Path = ASOUND, driving: list[str] | None = None, drm: Path = DRM_SYSFS,
                 pcms=list_pcms, active_output: str | None = None, *,
                 sys_root: Path | None = None, bluetooth_sinks: list[dict] | None = None) -> list[dict]:
    """Clean selectable audio outputs for Display & Sound UI (always starts with ``auto``)."""
    outputs, bt = _prepared_outputs(root, driving, drm, pcms, sys_root, bluetooth_sinks)
    names = None
    if any(o.get("vc4") for o in outputs):
        names = pcms() if callable(pcms) else pcms
    cur = (active_output or "auto").strip() or "auto"
    items: list[dict] = [{
        "id": "auto",
        "kind": "auto",
        "label": "Automatic",
        "card_id": None,
        "device": None,
        "alsa": None,
        "sink": None,
        "pw_sink": None,
        "available": True,
        "active": cur == "auto",
    }]
    primary_used: set[str] = set()
    device_items: list[dict] = []
    for out in outputs:
        kind = out["kind"]
        available = out.get("connected") is not False
        if kind == "hdmi" and not available:
            continue
        if kind == "analog":
            oid = "speakers" if "speakers" not in primary_used else f"card:{out['card_id']}:{out['device']}"
            if oid == "speakers":
                primary_used.add("speakers")
            label_detail = out.get("name") or out.get("card_id") or ""
            label = f"Built-in Speakers ({label_detail})" if label_detail else "Built-in Speakers"
        elif kind == "hdmi":
            oid = "hdmi" if "hdmi" not in primary_used else f"card:{out['card_id']}:{out['device']}"
            if oid == "hdmi":
                primary_used.add("hdmi")
            detail = out.get("sink") or out.get("connector") or out.get("name") or out.get("card_id") or ""
            label = f"HDMI / DisplayPort ({detail})" if detail else "HDMI / DisplayPort Audio"
        else:
            oid = "usb" if "usb" not in primary_used else f"card:{out['card_id']}:{out['device']}"
            if oid == "usb":
                primary_used.add("usb")
            detail = out.get("name") or out.get("card_id") or ""
            label = f"USB Audio ({detail})" if detail else "USB Audio"
        device_items.append({
            "id": oid,
            "kind": kind,
            "label": label,
            "card_id": out.get("card_id"),
            "device": out.get("device"),
            "alsa": alsa_device(out, names),
            "sink": out.get("sink"),
            "pw_sink": None,
            "available": available,
            "active": cur == oid or (oid == "speakers" and cur == "analog"),
        })
    for s in bt:
        sink = s.get("pw_sink") or s.get("sink")
        oid = "bluetooth" if "bluetooth" not in primary_used else f"bt:{sink}"
        primary_used.add("bluetooth")
        device_items.append({
            "id": oid,
            "kind": "bluetooth",
            "label": s.get("name") or "Bluetooth Audio",
            "card_id": None,
            "device": None,
            "alsa": s.get("alsa"),
            "sink": s.get("sink"),
            "pw_sink": sink,
            "available": bool(s.get("available", True)),
            "active": cur == oid,
        })
    order = {"analog": 0, "hdmi": 1, "usb": 2, "bluetooth": 3}
    device_items.sort(key=lambda item: (order.get(item["kind"], 9), 0 if item["id"] in ("speakers", "hdmi", "usb", "bluetooth") else 1))
    items.extend(device_items)
    return items


def choose(root: Path = ASOUND, prefer: str = "hdmi", driving: list[str] | None = None,
           drm: Path = DRM_SYSFS, pcms=list_pcms, sys_root: Path | None = None,
           output: str = "auto", bluetooth_sinks: list[dict] | None = None) -> dict | None:
    """The output to use now, with its ALSA device string (None: use ALSA default).
    ``driving`` and ``drm`` matter only for Raspberry Pi HDMI cards (mark_vc4_ports);
    ``pcms`` is a set of PCM names or a function returning one (``aplay -L``), asked
    only when a vc4 HDMI card is chosen.  ``sys_root``: as in playback_outputs.
    ``output`` honors ``audio.output`` (``auto``, ``speakers``, ``hdmi``, ``usb``,
    ``bluetooth``, or a card/device selector)."""
    outputs, bt = _prepared_outputs(root, driving, drm, pcms, sys_root, bluetooth_sinks)
    out = _match_output(outputs, output, prefer, fallback=True, bluetooth_sinks=bt)
    if out is None:
        return None
    if out.get("kind") == "bluetooth":
        return dict(out)
    if out.get("kind") == "configured":
        return dict(out)
    names = None
    if out.get("vc4"):
        names = pcms() if callable(pcms) else pcms
    return dict(out, alsa=alsa_device(out, names))


def supports_48khz(output: dict | None, root: Path = ASOUND) -> bool:
    """Whether ``output`` supports 48 kHz playback natively for Tiger OS X output."""
    if output is None:
        return True
    if output.get("keepalive") or output.get("vc4") or output.get("kind") in ("hdmi", "usb", "bluetooth"):
        return True
    if str(output.get("driver") or "").startswith(("HDA", "sof-", "vc4")):
        return True
    card = output.get("card")
    if card is None:
        return True
    card_dir = root / f"card{card}"
    try:
        codecs = sorted(card_dir.glob("codec*"))
    except OSError:
        return True
    found_rates = False
    for codec in codecs:
        try:
            text = codec.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if "rates [" in line:
                found_rates = True
                if "48000" in line:
                    return True
    return not found_rates


def resolve_chimes_dir(override: Path | str | None = None) -> Path:
    if override is not None:
        return Path(override)
    env = os.environ.get("RB_CHIMES_DIR", "").strip()
    return Path(env) if env else CHIMES_DIR_DEFAULT


def resolve_chime_name(family: str = "powermac", chime_setting: str = "auto") -> str | None:
    """Map ``audio.startup_chime`` and Mac family to the runtime WAV stem (or None)."""
    setting = (chime_setting or "auto").strip().lower()
    if setting in ("off", "none", ""):
        return None
    if setting == "auto":
        return "quadra" if family in ("quadra800", "68k", "m68k") else "os9"
    return setting if setting in CHIME_NAMES else None


def chime_path(family: str = "powermac", chime_setting: str = "auto",
               chimes_dir: Path | str | None = None) -> Path | None:
    """Return the genuine WAV path for this chime if present on disk, else None."""
    name = resolve_chime_name(family, chime_setting)
    if name is None:
        return None
    path = resolve_chimes_dir(chimes_dir) / f"{name}.wav"
    try:
        return path if path.is_file() else None
    except OSError:
        return None


def playback_command(wav: Path | str, output: dict | None = None, *, which=shutil.which) -> list[str]:
    """Player command for an async WAV playback on the active output sink/device."""
    path = str(wav)
    pw_sink = (output or {}).get("pw_sink")
    if pw_sink:
        if which("pw-play"):
            return ["pw-play", "--target", pw_sink, path]
        if which("paplay"):
            return ["paplay", "--device", pw_sink, path]
    alsa = (output or {}).get("alsa")
    if alsa and alsa not in ("pulse", "pipewire"):
        return ["aplay", "-q", "-D", alsa, path]
    if alsa == "pulse" and which("paplay"):
        return ["paplay", path]
    if which("aplay") or not (which("pw-play") or which("paplay")):
        return ["aplay", "-q", path]
    if which("pw-play"):
        return ["pw-play", path]
    return ["paplay", path]


def prepare_output_for_playback(output: dict | None, run=subprocess.run, root: Path = ASOUND,
                                warm_up_fn=warm_up) -> None:
    """Apply UCM routing, HDA stage unmuting, vc4 softvol, and ES8336 warm-up before playing."""
    if not output or output.get("kind") == "bluetooth":
        return
    try:
        if has_ucm(output):
            ucm_prepare(output, run=run, asound=root)
        if output.get("vc4"):
            vc4_softvol(output, run=run)
        hda_output_stage(output, run=run)
        alsa = output.get("alsa")
        if output.get("keepalive") and alsa and warm_up_fn is not None:
            warm_up_fn(output, alsa, run=run, root=root)
    except Exception as exc:
        log.debug("audio output prep skipped: %s", exc)


def play_startup_chime(family: str = "powermac", chime_setting: str = "auto", *,
                       output: str = "auto", audio_out: dict | None = None,
                       root: Path = ASOUND, prefer: str = "hdmi",
                       driving: list[str] | None = None, drm: Path = DRM_SYSFS,
                       chimes_dir: Path | str | None = None,
                       muted: bool = False, volume: int | None = None,
                       run=subprocess.run, popen=subprocess.Popen,
                       warm_up_fn=warm_up, which=shutil.which,
                       bluetooth_sinks: list[dict] | None = None) -> bool:
    """Play a genuine WAV startup chime asynchronously without popping amps or failing if missing."""
    if muted or (volume is not None and int(volume) <= 0):
        return False
    wav = chime_path(family, chime_setting, chimes_dir)
    if wav is None:
        return False
    out = audio_out
    if out is None:
        out = choose(root=root, prefer=prefer, driving=driving, drm=drm,
                     output=output, bluetooth_sinks=bluetooth_sinks)
    prepare_output_for_playback(out, run=run, root=root, warm_up_fn=warm_up_fn)
    cmd = playback_command(wav, out, which=which)
    try:
        popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("startup chime not played: %s", exc)
        return False


def _test_beep_wav(rate: int = 48000) -> bytes:
    """Plain 440 Hz diagnostic test beep WAV (used only by Test Sound when chimes WAVs are absent)."""
    n = int(rate * 0.25)
    frames = bytearray()
    for i in range(n):
        env = min(1.0, i / (rate * 0.015)) * min(1.0, (n - i) / (rate * 0.05))
        sample = int(9000 * env * math.sin(2 * math.pi * 440 * i / rate))
        frames += struct.pack("<h", sample)
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(frames), b"WAVE", b"fmt ",
                       16, 1, 1, rate, rate * 2, 2, 16, b"data", len(frames)) + bytes(frames)


def test_sound(output: str = "auto", *, audio_out: dict | None = None,
               root: Path = ASOUND, prefer: str = "hdmi",
               driving: list[str] | None = None, drm: Path = DRM_SYSFS,
               pcms=list_pcms, chimes_dir: Path | str | None = None,
               chime_setting: str = "auto", family: str = "powermac",
               muted: bool = False, run=subprocess.run, popen=subprocess.Popen,
               warm_up_fn=warm_up, which=shutil.which,
               bluetooth_sinks: list[dict] | None = None,
               run_dir: Path | None = None) -> dict:
    """Play a short test sound on the selected output (Sound UI Test Sound button)."""
    req = (output or "auto").strip() or "auto"
    out = audio_out
    if out is None:
        outputs, bt = _prepared_outputs(root, driving, drm, pcms, None, bluetooth_sinks)
        out = _match_output(outputs, req, prefer, fallback=(req == "auto"), bluetooth_sinks=bt)
        if out is None:
            if req != "auto":
                return {"ok": False, "output": req, "device": None, "player": None,
                        "error": f"Selected audio output '{req}' is not available."}
        elif out.get("kind") not in ("bluetooth", "configured"):
            names = (pcms() if callable(pcms) else pcms) if out.get("vc4") else None
            out = dict(out, alsa=alsa_device(out, names))
    if muted:
        return {"ok": False, "output": req, "device": (out or {}).get("alsa") or (out or {}).get("pw_sink"),
                "player": None, "error": "Sound is muted."}
    wav = chime_path(family, "auto" if chime_setting == "off" else chime_setting, chimes_dir)
    if wav is None:
        croot = resolve_chimes_dir(chimes_dir)
        for name in CHIME_NAMES:
            candidate = croot / f"{name}.wav"
            if candidate.is_file():
                wav = candidate
                break
    if wav is None:
        target_dir = Path(run_dir) if run_dir is not None else Path(tempfile.gettempdir())
        target_dir.mkdir(parents=True, exist_ok=True)
        wav = target_dir / "retroboot-test-sound.wav"
        if not wav.exists():
            wav.write_bytes(_test_beep_wav())
    prepare_output_for_playback(out, run=run, root=root, warm_up_fn=warm_up_fn)
    cmd = playback_command(wav, out, which=which)
    device = (out or {}).get("pw_sink") or (out or {}).get("alsa")
    try:
        popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return {"ok": True, "output": req, "device": device, "player": cmd[0], "file": str(wav), "error": None}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "output": req, "device": device, "player": cmd[0], "error": str(exc)}


def configured(device: str) -> dict | None:
    """An explicit ALSA PCM from hostd.json ``audio_device`` as an output record."""
    device = (device or "").strip()
    if not device:
        return None
    qemu_audiodev(device)        # raises ValueError for anything QEMU's option parser could misread
    m = re.search(r"CARD=([A-Za-z0-9_-]+)", device)
    d = re.search(r"DEV=(\d+)", device)
    return {"kind": "configured", "card": None, "card_id": m.group(1) if m else None,
            "device": int(d.group(1)) if d else 0, "driver": None, "name": device, "connected": None,
            "alsa": device}


# The screamer's host-stream pre-roll properties and the package features that bring
# them (emulator/patches/retroboot-screamer-realtime.patch, -lead-strict.patch); a
# package without one refuses its -global, so it is only passed when the feature is there.
SCREAMER_FEATURES = {"host-lead-ms": "screamer-realtime", "host-lead-max-ms": "screamer-realtime",
                     "host-lead-strict": "screamer-lead-strict"}
SCREAMER_DEFAULT_MAX_MS = 120          # the package's host-lead-max-ms default


def package_features(package_root: Path) -> set[str]:
    """The emulator package's manifest features (empty when unreadable)."""
    try:
        m = json.loads((Path(package_root) / "manifest.json").read_text())
    except (OSError, ValueError):
        return set()
    return set(m.get("features") or [])


def screamer_options(profile: dict | None, host_lead_ms: int = -1, features: set[str] | None = None) -> dict:
    """Screamer properties for a launch: the board profile's (``screamer`` in
    platform.GEMINI_PROFILE; none elsewhere), with hostd.json's ``audio_host_lead_ms``
    over it when set: 0 passes nothing (QEMU's 40/120 ms, no strict pre-roll), N > 0
    starts the host stream after N ms with room to grow to at least the default
    maximum.  ``features``: the package's (package_features); properties it lacks are
    left out (None: no filter)."""
    opts = {k: v for k, v in (profile or {}).items() if k in SCREAMER_FEATURES}
    if host_lead_ms is not None and host_lead_ms == 0:
        return {}
    if host_lead_ms is not None and host_lead_ms > 0:
        lead = int(host_lead_ms)
        top = int(opts.get("host-lead-max-ms") or SCREAMER_DEFAULT_MAX_MS)
        opts.update({"host-lead-ms": lead, "host-lead-max-ms": max(lead, top)})
    if features is not None:
        opts = {k: v for k, v in opts.items() if SCREAMER_FEATURES[k] in features}
    return opts


def screamer_args(options: dict | None) -> list[str]:
    """``-global screamer.KEY=VALUE`` arguments: whole milliseconds up to 1000 for the
    leads, true/false for host-lead-strict; anything else is refused."""
    args = []
    unknown = set(options or {}) - set(SCREAMER_FEATURES)
    if unknown:
        raise ValueError(f"unknown screamer option {sorted(unknown)[0]}")
    for key in SCREAMER_FEATURES:
        if not options or key not in options:
            continue
        value = options[key]
        if key == "host-lead-strict":
            if type(value) is not bool:
                raise ValueError("screamer host-lead-strict must be true or false")
            value = "on" if value else "off"
        elif type(value) is not int or not 0 <= value <= 1000:
            raise ValueError(f"screamer {key} must be 0-1000 ms")
        args += ["-global", f"screamer.{key}={value}"]
    return args


def qemu_audiodev(device: str | None) -> str:
    """``-audiodev`` value; QEMU option values escape a literal comma as ``,,``."""
    if not device:
        return "alsa,id=snd0"
    if not re.fullmatch(r"[A-Za-z0-9_:=,.-]{1,80}", device):
        raise ValueError("unexpected ALSA device name")
    return "alsa,id=snd0,out.dev=" + device.replace(",", ",,")
