"""Host audio output selection (retroboot/audio.py) against /proc/asound trees.

The first tree is the one observed on the Ryzen HX 370 mini PC (hardware report
image/reports/hardware-2026-10-11.md): a capture-only USB microphone is card 0,
HDMI on card 1 (sink on pin 1), the ALC897 analog codec on card 2.
"""
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from retroboot import audio
from retroboot.machines import LaunchOptions, build_argv  # noqa: F401  (import check)

CARDS = """ 0 [POROSVOC       ]: USB-Audio - POROSVOC
                      POROSVOC POROSVOC at usb-0000:65:00.4-1, full speed
 1 [Generic        ]: HDA-Intel - HD-Audio Generic
                      HD-Audio Generic at 0xdc4c8000 irq 129
 2 [Generic_1      ]: HDA-Intel - HD-Audio Generic
                      HD-Audio Generic at 0xdc4c0000 irq 130
 3 [acppdmmach     ]: acp-pdm-mach - acp-pdm-mach
                      AZW-SER9-Defaultstring
"""

ELD_PRESENT = """monitor_present		1
eld_valid		1
monitor_name		JetKVM v1
connection_type		HDMI
sad_count		1
sad0_coding_type	[0x1] LPCM
"""
ELD_ABSENT = "monitor_present\t\t0\neld_valid\t\t0\n"


def pcm(card, dev, ident, stream="PLAYBACK"):
    return f"card: {card}\ndevice: {dev}\nsubdevice: 0\nstream: {stream}\nid: {ident}\nname: {ident}\nsubname: subdevice #0\n"


def make_tree(root: Path, hdmi_sink=True, analog=True, usb_playback=False):
    (root / "cards").write_text(CARDS)
    files = {
        "card0/pcm0c/info": pcm(0, 0, "USB Audio", "CAPTURE"),
        "card1/pcm3p/info": pcm(1, 3, "HDMI 0"),
        "card1/pcm7p/info": pcm(1, 7, "HDMI 1"),
        "card1/pcm8p/info": pcm(1, 8, "HDMI 2"),
        "card1/eld#0.0": ELD_ABSENT,
        "card1/eld#0.1": ELD_PRESENT if hdmi_sink else ELD_ABSENT,
        "card1/eld#0.2": ELD_ABSENT,
        "card3/pcm0c/info": pcm(3, 0, "acp-pdm", "CAPTURE"),
    }
    if analog:
        files["card2/pcm0p/info"] = pcm(2, 0, "ALC897 Analog")
    if usb_playback:
        files["card0/pcm0p/info"] = pcm(0, 0, "USB Audio")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


class AudioOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_capture_only_card0_is_skipped_and_hdmi_sink_wins(self):
        make_tree(self.root)
        outs = audio.playback_outputs(self.root)
        self.assertEqual({(o["card_id"], o["device"]) for o in outs},
                         {("Generic", 3), ("Generic", 7), ("Generic", 8), ("Generic_1", 0)})
        hdmi1 = next(o for o in outs if o["device"] == 7)
        self.assertTrue(hdmi1["connected"])
        self.assertEqual(hdmi1["sink"], "JetKVM v1")
        self.assertFalse(next(o for o in outs if o["device"] == 3)["connected"])
        chosen = audio.choose(self.root)
        self.assertEqual(chosen["alsa"], "plughw:CARD=Generic,DEV=7")

    def test_laptop_prefers_its_speakers(self):
        make_tree(self.root)
        self.assertEqual(audio.choose(self.root, prefer="analog")["alsa"], "plughw:CARD=Generic_1,DEV=0")
        (self.root / "noanalog").mkdir()
        make_tree(self.root / "noanalog", analog=False)
        self.assertEqual(audio.choose(self.root / "noanalog", prefer="analog")["alsa"], "plughw:CARD=Generic,DEV=7")

    def test_analog_without_hdmi_sink(self):
        make_tree(self.root, hdmi_sink=False)
        self.assertEqual(audio.choose(self.root)["alsa"], "plughw:CARD=Generic_1,DEV=0")

    def test_usb_playback_last_resort(self):
        make_tree(self.root, hdmi_sink=False, analog=False, usb_playback=True)
        chosen = audio.choose(self.root)
        self.assertEqual((chosen["kind"], chosen["alsa"]), ("usb", "plughw:CARD=POROSVOC,DEV=0"))

    def test_nothing_usable(self):
        make_tree(self.root, hdmi_sink=False, analog=False)
        self.assertIsNone(audio.choose(self.root))
        self.assertIsNone(audio.choose(self.root / "missing"))

    def test_qemu_audiodev_escapes_commas(self):
        self.assertEqual(audio.qemu_audiodev(None), "alsa,id=snd0")
        self.assertEqual(audio.qemu_audiodev("plughw:CARD=Generic,DEV=7"),
                         "alsa,id=snd0,out.dev=plughw:CARD=Generic,,DEV=7")
        with self.assertRaises(ValueError):
            audio.qemu_audiodev("plughw:CARD=x,DEV=1 -monitor stdio")


class ScreamerOptionTests(unittest.TestCase):
    """The emulator's host-stream pre-roll (audio.screamer_options/screamer_args):
    the Gemini profile's 120/160 ms and strict pre-roll, hostd.json's
    audio_host_lead_ms over it, nothing on boards without a profile, and only the
    properties the package's features bring."""

    GEMINI = {"host-lead-ms": 120, "host-lead-max-ms": 160, "host-lead-strict": True}
    BOTH = {"screamer-realtime", "screamer-lead-strict"}

    def test_profile_and_override(self):
        self.assertEqual(audio.screamer_options(self.GEMINI), self.GEMINI)
        self.assertEqual(audio.screamer_options(self.GEMINI, -1, self.BOTH), self.GEMINI)
        self.assertEqual(audio.screamer_options(None), {})               # PCs and Pis: QEMU's defaults
        self.assertEqual(audio.screamer_options({}, -1, self.BOTH), {})
        self.assertEqual(audio.screamer_options(self.GEMINI, 0, self.BOTH), {})     # A/B: QEMU's 40/120
        self.assertEqual(audio.screamer_options(self.GEMINI, 80, self.BOTH),
                         {"host-lead-ms": 80, "host-lead-max-ms": 160, "host-lead-strict": True})
        self.assertEqual(audio.screamer_options(None, 200), {"host-lead-ms": 200, "host-lead-max-ms": 200})
        self.assertEqual(audio.screamer_options(None, 60), {"host-lead-ms": 60, "host-lead-max-ms": 120})
        self.assertEqual(audio.screamer_options({"irq-hold-ms": 0, **self.GEMINI}), self.GEMINI)

    def test_package_features_filter(self):
        # r18 (installed on the Gemini): the leads, not the strict pre-roll; before r15: nothing
        self.assertEqual(audio.screamer_options(self.GEMINI, -1, {"screamer-realtime"}),
                         {"host-lead-ms": 120, "host-lead-max-ms": 160})
        self.assertEqual(audio.screamer_options(self.GEMINI, -1, {"screamer-paced-dma"}), {})
        self.assertEqual(audio.screamer_options(self.GEMINI, -1, set()), {})

    def test_args(self):
        self.assertEqual(audio.screamer_args(self.GEMINI),
                         ["-global", "screamer.host-lead-ms=120", "-global", "screamer.host-lead-max-ms=160",
                          "-global", "screamer.host-lead-strict=on"])
        self.assertEqual(audio.screamer_args({"host-lead-strict": False}), ["-global", "screamer.host-lead-strict=off"])
        self.assertEqual(audio.screamer_args({}), [])
        self.assertEqual(audio.screamer_args(None), [])
        for bad in ({"host-lead-ms": "120"}, {"host-lead-ms": 1001}, {"host-lead-ms": -1},
                    {"host-lead-ms": 1.5}, {"irq-hold-ms": 0}, {"host-lead-strict": "on"}, {"host-lead-ms": True}):
            with self.assertRaises(ValueError):
                audio.screamer_args(bad)

    def test_package_features(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(audio.package_features(root), set())
            (root / "manifest.json").write_text("{not json")
            self.assertEqual(audio.package_features(root), set())
            (root / "manifest.json").write_text(json.dumps({"features": ["screamer-realtime", "screamer-lead-strict"]}))
            self.assertEqual(audio.package_features(root), self.BOTH)

    def test_build_argv(self):
        import helpers
        from retroboot import schema
        from retroboot.config import Paths
        from retroboot.machines import MachineError, MachineRegistry
        td, root = helpers.scratch()
        try:
            helpers.make_package(root)
            helpers.make_machine(root)
            paths = Paths(root)
            paths.ensure()
            m = MachineRegistry(paths).get("mac-os-9")
            settings = schema.defaults()
            display = {"mode": [1920, 1080, 60], "suggested_scale": 2}
            dev = "plughw:CARD=mt6797mt6351,DEV=0"
            plain = build_argv(m, settings, paths, display, LaunchOptions(audio_device=dev))
            self.assertNotIn("screamer.host-lead-ms=120", plain)
            argv = build_argv(m, settings, paths, display, LaunchOptions(audio_device=dev, screamer=self.GEMINI))
            i = argv.index("-audiodev")
            self.assertEqual(argv[i + 1], "alsa,id=snd0,out.dev=plughw:CARD=mt6797mt6351,,DEV=0")
            self.assertEqual(argv[i + 2:i + 8], ["-global", "screamer.host-lead-ms=120",
                                                 "-global", "screamer.host-lead-max-ms=160",
                                                 "-global", "screamer.host-lead-strict=on"])
            # everything else is the argv without the profile
            self.assertEqual(argv[:i + 2] + argv[i + 8:], plain)
            # no guest sound: no screamer properties either
            muted = build_argv(m, dict(settings, **{"audio.enabled": False}), paths, display,
                               LaunchOptions(screamer=self.GEMINI))
            self.assertNotIn("screamer.host-lead-ms=120", muted)
            self.assertIn("none,id=snd0", muted)
            with self.assertRaises(MachineError):
                build_argv(m, settings, paths, display, LaunchOptions(screamer={"host-lead-ms": 5000}))
        finally:
            td.cleanup()


# N5105 laptop (image/reports/hardware-n5105-2026-10-11.md): Sound Open Firmware
# card with an Everest ES8336 codec and three HDMI PCMs, all on card 0.
SOF_CARDS = """ 0 [sofessx8336    ]: sof-essx8336 - sof-essx8336
                      Defaultstring-Defaultstring-Defaultstring
"""


def make_sof_tree(root: Path):
    (root / "cards").write_text(SOF_CARDS)
    files = {"card0/pcm0p/info": pcm(0, 0, "ES8336 (*)").replace("name: ES8336 (*)", "name: "),
             "card0/pcm0c/info": pcm(0, 0, "ES8336 (*)", "CAPTURE"),
             "card0/pcm5p/info": pcm(0, 5, "HDMI 1 (*)"),
             "card0/pcm6p/info": pcm(0, 6, "HDMI 2 (*)"),
             "card0/eld#2.3": ELD_PRESENT}
    for rel, text in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)


class FakeRun:
    """subprocess.run stand-in for alsaucm."""
    def __init__(self, rc=0):
        self.calls, self.rc = [], rc

    def __call__(self, argv, **kw):
        import subprocess
        self.calls.append(argv)
        if "list" in argv:
            out = "  0: Speaker\n    Speakers\n  1: Headphones\n    Headphones\n  2: HDMI1\n    HDMI 1\n"
        else:
            out = "  PlaybackMasterElem/Speaker=DAC\n"
        return subprocess.CompletedProcess(argv, self.rc, out, "")


class AsocAudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_sof_tree(self.root)
        audio._ucm_done.clear()
        self.ucm = self.root / "ucm2conf.d"
        (self.ucm / "sof-essx8336").mkdir(parents=True)

    def tearDown(self):
        audio._ucm_done.clear()
        self.tmp.cleanup()

    def test_codec_pcm_is_analog_and_keeps_the_stream_alive(self):
        outs = audio.playback_outputs(self.root)
        codec = next(o for o in outs if o["device"] == 0)
        self.assertEqual((codec["kind"], codec["name"], codec.get("asoc"), codec.get("keepalive")),
                         ("analog", "ES8336", True, True))
        self.assertEqual(next(o for o in outs if o["device"] == 5)["kind"], "hdmi")
        chosen = audio.choose(self.root, prefer="analog")
        self.assertEqual(chosen["alsa"], "rb_keepalive:CARD=sofessx8336,DEV=0")
        self.assertEqual(audio.qemu_audiodev(chosen["alsa"]),
                         "alsa,id=snd0,out.dev=rb_keepalive:CARD=sofessx8336,,DEV=0")

    def test_ucm_runs_once_per_card(self):
        chosen = audio.choose(self.root, prefer="analog")
        run = FakeRun()
        with unittest.mock.patch("shutil.which", return_value="/usr/bin/alsaucm"):
            want = {"device": "Speaker", "mixer": "DAC", "warmed": False}    # no stream status: not warmed
            self.assertEqual(audio.ucm_prepare(chosen, run=run, root=self.ucm, asound=self.root), want)
            self.assertEqual(audio.ucm_prepare(chosen, run=run, root=self.ucm, asound=self.root), want)
        self.assertEqual(len(run.calls), 2)                                  # list + one set (cached after)
        self.assertEqual(run.calls[1], ["alsaucm", "-c", "hw:sofessx8336", "set", "_boot", "", "set", "_verb",
                                        "HiFi", "set", "_enadev", "Speaker", "get", "PlaybackMasterElem/Speaker"])

    def test_warm_up_turns_the_amplifier_off_silently_once(self):
        """sof_es8336: a silent stream with both pins off before QEMU's first open (boot pop)."""
        import subprocess
        status = self.root / "card0/pcm0p/sub0/status"
        status.parent.mkdir(parents=True)
        status.write_text("closed\n")
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            out = ""
            if argv[0] == "alsaucm":
                out = ("  0: Speaker\n    Speakers\n" if "list" in argv else "  PlaybackMasterElem/Speaker=DAC\n")
            elif argv[:1] == ["amixer"] and "cget" in argv:
                out = "numid=29,iface=MIXER,name='x'\n  ; type=BOOLEAN\n  : values=%s\n" % (
                    "on" if "Speaker" in argv[-1] else "off")
            return subprocess.CompletedProcess(argv, 0, out, "")
        chosen = audio.choose(self.root, prefer="analog")
        with unittest.mock.patch("shutil.which", return_value="/usr/bin/alsaucm"):
            res = audio.ucm_prepare(chosen, run=run, root=self.ucm, asound=self.root)
        self.assertTrue(res["warmed"])
        after_ucm = calls[2:]
        sets = [c for c in after_ucm if "cset" in c]
        self.assertEqual(sets[:2], [["amixer", "-c", "sofessx8336", "cset", "name=Speaker Switch", "off"],
                                    ["amixer", "-c", "sofessx8336", "cset", "name=Headphone Switch", "off"]])
        play = next(i for i, c in enumerate(after_ucm) if c[0] == "aplay")
        self.assertIn("hw:CARD=sofessx8336,DEV=0", after_ucm[play])
        self.assertTrue(all("cset" not in c or c[-1] == "off" for c in after_ucm[:play]))
        self.assertEqual(sets[2:], [["amixer", "-c", "sofessx8336", "cset", "name=Speaker Switch", "on"],
                                    ["amixer", "-c", "sofessx8336", "cset", "name=Headphone Switch", "off"]])
        # HDMI on the same card chosen first (hostd start, before the presenter reports the panel):
        # the codec output is still warmed, once.
        audio._ucm_done.clear()
        calls.clear()
        hdmi = next(o for o in audio.playback_outputs(self.root) if o["kind"] == "hdmi")
        with unittest.mock.patch("shutil.which", return_value="/usr/bin/alsaucm"):
            self.assertTrue(audio.ucm_prepare(hdmi, run=run, root=self.ucm, asound=self.root)["warmed"])
            self.assertTrue(audio.ucm_prepare(chosen, run=run, root=self.ucm, asound=self.root)["warmed"])
        self.assertEqual(sum(1 for c in calls if c[0] == "aplay"), 1)
        self.assertIn("hw:CARD=sofessx8336,DEV=0", next(c for c in calls if c[0] == "aplay"))
        # A running stream (hostd restarted under a guest): never touched.
        status.write_text("state: RUNNING\n")
        calls.clear()
        self.assertFalse(audio.warm_up(chosen, "Speaker", run, self.root))
        self.assertEqual(calls, [])

    def test_no_ucm_profile_or_hda_card(self):
        chosen = audio.choose(self.root, prefer="analog")
        with unittest.mock.patch("shutil.which", return_value="/usr/bin/alsaucm"):
            self.assertIsNone(audio.ucm_prepare(chosen, run=FakeRun(), root=self.root / "none"))
            hda = dict(chosen, asoc=False)
            self.assertIsNone(audio.ucm_prepare(hda, run=FakeRun(), root=self.ucm))
            audio._ucm_done.clear()
            self.assertIsNone(audio.ucm_prepare(chosen, run=FakeRun(rc=1), root=self.ucm))


class MixerTests(unittest.IsolatedAsyncioTestCase):
    async def test_ucm_master_on_the_mapped_scale(self):
        from retroboot.network import AudioMixer
        calls = []

        async def run(argv, timeout=None):
            calls.append(argv)
            if argv[-2:] == ["sget", "DAC"]:
                return 0, "Simple mixer control 'DAC',0\n  Capabilities: pvolume\n", ""
            return 0, "", ""
        m = AudioMixer(runner=run)
        self.assertTrue(await m.probe("sofessx8336", "DAC"))
        self.assertTrue(await m.set_volume(70))
        self.assertEqual(calls[-1], ["amixer", "-c", "sofessx8336", "-M", "-q", "sset", "DAC", "70%"])
        self.assertTrue(await m.set_mute(True))                  # no switch: mute is 0 %
        self.assertEqual(calls[-1][-1], "0%")
        self.assertTrue(await m.set_mute(False))
        self.assertEqual(calls[-1][-1], "70%")

    async def test_pi_softvol_has_no_switch(self):
        from retroboot.network import AudioMixer
        calls = []

        async def run(argv, timeout=None):
            calls.append(argv)
            if argv[-2:] == ["sget", "PCM"]:
                return 0, "Simple mixer control 'PCM',0\n  Capabilities: volume\n", ""
            return 0, "Simple mixer control 'PCM',0\nSimple mixer control 'IEC958',0\n", ""
        m = AudioMixer(runner=run)
        self.assertTrue(await m.probe("vc4hdmi0", detect_switch=True))
        self.assertFalse(m.has_switch)
        await m.set_volume(40)
        self.assertEqual(calls[-1], ["amixer", "-c", "vc4hdmi0", "-q", "sset", "PCM", "40%"])
        await m.set_mute(True)
        self.assertEqual(calls[-1][-1], "0%")
        await m.set_mute(False)
        self.assertEqual(calls[-1][-1], "40%")

    async def test_plain_cards_unchanged(self):
        from retroboot.network import AudioMixer
        calls = []

        async def run(argv, timeout=None):
            calls.append(argv)
            return 0, "Simple mixer control 'Master',0\nSimple mixer control 'PCM',0\n", ""
        m = AudioMixer(runner=run)
        self.assertTrue(await m.probe("PCH"))
        await m.set_volume(40)
        self.assertEqual(calls[-1], ["amixer", "-c", "PCH", "-q", "sset", "Master", "40%", "unmute"])
        await m.set_mute(True)
        self.assertEqual(calls[-1], ["amixer", "-c", "PCH", "-q", "sset", "Master", "mute"])



# Raspberry Pi 4 (vc4-kms-v3d, dtparam=audio=on): one vc4-hdmi card per HDMI port
# (PCM "MAI PCM i2s-hifi-0", no ELD in /proc/asound) and the 3.5 mm jack. Card
# numbers vary between boots; the Pi 5 has no Headphones card.
PI_CARDS = """ 0 [Headphones     ]: bcm2835_headpho - bcm2835 Headphones
                      bcm2835 Headphones
 1 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0
                      vc4-hdmi-0
 2 [vc4hdmi1       ]: vc4-hdmi - vc4-hdmi-1
                      vc4-hdmi-1
"""
PI5_CARDS = """ 0 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0
                      vc4-hdmi-0
 1 [vc4hdmi1       ]: vc4-hdmi - vc4-hdmi-1
                      vc4-hdmi-1
"""
USB_LINE = """ 3 [Device         ]: USB-Audio - USB Audio Device
                      C-Media Electronics Inc. USB Audio Device at usb-xhci-hcd.0-1, full speed
"""


def edid(audio=True, basic=True) -> bytes:
    """A 2-block EDID; the CEA-861 extension has the basic-audio flag and/or an
    Audio Data Block (LPCM 2 ch) when audio is on."""
    base = bytearray(128)
    base[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    base[126] = 1
    ext = bytearray(128)
    ext[0], ext[1] = 0x02, 0x03
    blocks = b"\x23\x09\x07\x07" if audio else b""          # tag 1 (audio), 3 bytes: LPCM, 2 ch
    ext[2] = 4 + len(blocks)
    ext[3] = 0x40 if (audio and basic) else 0x00
    ext[4:4 + len(blocks)] = blocks
    return bytes(base + ext)


def make_pi_tree(root: Path, cards=PI_CARDS, usb=False, displays=None, pi5=False):
    """/proc/asound and /sys/class/drm of a Pi; displays: {"HDMI-A-1": edid bytes or None (DVI, no EDID audio)}."""
    asound, drm = root / "asound", root / "drm"
    asound.mkdir(parents=True)
    (asound / "cards").write_text(cards + (USB_LINE if usb else ""))
    for line in (cards + (USB_LINE if usb else "")).splitlines():
        m = audio._CARD_LINE.match(line)
        if not m:
            continue
        n, cid = int(m.group(1)), m.group(2).strip()
        ident = "MAI PCM i2s-hifi-0" if cid.startswith("vc4hdmi") else "bcm2835 Headphones" \
            if cid == "Headphones" else "USB Audio"
        p = asound / f"card{n}/pcm0p/info"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(pcm(n, 0, ident))
    vc4 = drm / "card1"                        # card0 is v3d (render only, no connectors)
    (drm / "card0/device").mkdir(parents=True)
    (root / "drivers/v3d").mkdir(parents=True)
    (root / "drivers/vc4-drm").mkdir(parents=True)
    (drm / "card0/device/driver").symlink_to(root / "drivers/v3d")
    (vc4 / "device").mkdir(parents=True)
    (vc4 / "device/driver").symlink_to(root / "drivers/vc4-drm")
    for port in ("HDMI-A-1", "HDMI-A-2"):
        c = drm / f"card1-{port}"
        c.mkdir()
        shown = (displays or {}).get(port, "absent")
        (c / "status").write_text("disconnected\n" if shown == "absent" else "connected\n")
        (c / "edid").write_bytes(b"" if shown in ("absent", None) else shown)
    return asound, drm


class PiAudioTests(unittest.TestCase):
    PCMS = {"default", "default:CARD=vc4hdmi0", "hdmi:CARD=vc4hdmi0,DEV=0", "default:CARD=vc4hdmi1",
            "hdmi:CARD=vc4hdmi1,DEV=0", "default:CARD=Headphones", "plughw:CARD=Headphones,DEV=0"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def choose(self, driving, **tree):
        asound, drm = make_pi_tree(self.root, **tree)
        return audio.choose(asound, prefer="pi", driving=driving, drm=drm, pcms=self.PCMS)

    def test_classification(self):
        asound, drm = make_pi_tree(self.root, displays={"HDMI-A-1": edid()})
        outs = {o["card_id"]: o for o in audio.playback_outputs(asound)}
        self.assertEqual((outs["vc4hdmi0"]["kind"], outs["vc4hdmi0"]["connector"]), ("hdmi", "HDMI-A-1"))
        self.assertEqual(outs["vc4hdmi1"]["connector"], "HDMI-A-2")
        self.assertEqual(outs["Headphones"]["kind"], "analog")
        st = audio.vc4_port_state(outs["vc4hdmi0"], drm)
        self.assertEqual(st, {"status": "connected", "audio": True})
        self.assertEqual(audio.vc4_port_state(outs["vc4hdmi1"], drm), {"status": "disconnected", "audio": None})

    def test_tv_on_the_port_shown(self):
        out = self.choose(["HDMI-A-1"], displays={"HDMI-A-1": edid()})
        self.assertEqual((out["card_id"], out["alsa"]), ("vc4hdmi0", "default:CARD=vc4hdmi0"))
        self.assertEqual(audio.qemu_audiodev(out["alsa"]), "alsa,id=snd0,out.dev=default:CARD=vc4hdmi0")

    def test_second_port(self):
        out = self.choose(["HDMI-A-2"], displays={"HDMI-A-2": edid()})
        self.assertEqual(out["alsa"], "default:CARD=vc4hdmi1")

    def test_primary_before_mirror(self):
        both = {"HDMI-A-1": edid(), "HDMI-A-2": edid()}
        self.assertEqual(self.choose(["HDMI-A-2", "HDMI-A-1"], displays=both)["card_id"], "vc4hdmi1")

    def test_mirror_with_speakers_when_primary_has_none(self):
        disp = {"HDMI-A-1": edid(audio=False), "HDMI-A-2": edid()}
        self.assertEqual(self.choose(["HDMI-A-1", "HDMI-A-2"], displays=disp)["card_id"], "vc4hdmi1")

    def test_presenter_not_reported_yet(self):
        both = {"HDMI-A-1": edid(), "HDMI-A-2": edid()}
        self.assertEqual(self.choose(None, displays=both)["card_id"], "vc4hdmi0")

    def test_port_not_shown_is_skipped(self):
        # vc4 plays HDMI audio only on an enabled output.
        out = self.choose(["HDMI-A-1"], displays={"HDMI-A-1": edid(audio=False), "HDMI-A-2": edid()})
        self.assertEqual(out["card_id"], "Headphones")

    def test_monitor_without_speakers_uses_usb_then_the_jack(self):
        dvi = {"HDMI-A-1": edid(audio=False)}
        out = self.choose(["HDMI-A-1"], displays=dvi, usb=True)
        self.assertEqual((out["kind"], out["alsa"]), ("usb", "plughw:CARD=Device,DEV=0"))
        shutil.rmtree(self.root / "asound")
        shutil.rmtree(self.root / "drm")
        shutil.rmtree(self.root / "drivers")
        out = self.choose(["HDMI-A-1"], displays=dvi)
        self.assertEqual((out["kind"], out["alsa"]), ("analog", "plughw:CARD=Headphones,DEV=0"))

    def test_basic_audio_flag_or_audio_block(self):
        self.assertTrue(audio.edid_has_audio(edid(audio=True, basic=False)))
        self.assertTrue(audio.edid_has_audio(edid(audio=True, basic=True)))
        self.assertFalse(audio.edid_has_audio(edid(audio=False)))
        self.assertIsNone(audio.edid_has_audio(b""))
        self.assertIsNone(audio.edid_has_audio(b"\x00" * 128))
        base_only = bytearray(edid()[:128])
        base_only[126] = 0
        self.assertFalse(audio.edid_has_audio(bytes(base_only)))        # DVI: no CEA extension

    def test_unreadable_edid_counts_as_maybe(self):
        out = self.choose(["HDMI-A-1"], displays={"HDMI-A-1": None})
        self.assertEqual(out["card_id"], "vc4hdmi0")

    def test_pi5_without_hdmi_audio_has_nothing(self):
        out = self.choose(["HDMI-A-1"], cards=PI5_CARDS, displays={"HDMI-A-1": edid(audio=False)})
        self.assertIsNone(out)

    def test_pcm_names(self):
        conf = self.root / "cards"
        conf.mkdir()
        nocfg = self.root / "nocards"
        self.assertEqual(audio.vc4_pcm("vc4hdmi0", None), "default:CARD=vc4hdmi0")
        self.assertEqual(audio.vc4_pcm("vc4hdmi0", {"default:CARD=vc4hdmi0", "sysdefault:CARD=vc4hdmi0"}, nocfg),
                         "default:CARD=vc4hdmi0")
        # What aplay -L lists on a Pi: hdmi: and sysdefault:, not default: -> the softvol default, not raw IEC958.
        listed = {"hdmi:CARD=vc4hdmi0,DEV=0", "sysdefault:CARD=vc4hdmi0", "null"}
        self.assertEqual(audio.vc4_pcm("vc4hdmi0", listed, nocfg), "sysdefault:CARD=vc4hdmi0")
        # Only hdmi: listed but alsa-lib's vc4-hdmi.conf is there: its default exists anyway.
        (conf / "vc4-hdmi.conf").write_text("# vc4-hdmi.pcm.default { ... }\n")
        self.assertEqual(audio.vc4_pcm("vc4hdmi0", {"hdmi:CARD=vc4hdmi0,DEV=0"}, conf), "default:CARD=vc4hdmi0")
        # No card configuration at all: the raw IEC958 device is the only thing that plays.
        self.assertEqual(audio.vc4_pcm("vc4hdmi0", {"hdmi:CARD=vc4hdmi0,DEV=0"}, nocfg), "hdmi:CARD=vc4hdmi0,DEV=0")
        self.assertEqual(audio.vc4_pcm("vc4hdmi0", set(), nocfg), "hdmi:CARD=vc4hdmi0,DEV=0")
        self.assertEqual(audio.qemu_audiodev("sysdefault:CARD=vc4hdmi0"), "alsa,id=snd0,out.dev=sysdefault:CARD=vc4hdmi0")
        self.assertEqual(audio.qemu_audiodev("hdmi:CARD=vc4hdmi0,DEV=0"),
                         "alsa,id=snd0,out.dev=hdmi:CARD=vc4hdmi0,,DEV=0")
        # aplay -L is asked only when a vc4 card is chosen.
        asked = []
        asound, drm = make_pi_tree(self.root, displays={"HDMI-A-1": edid(audio=False)})
        audio.choose(asound, prefer="pi", driving=["HDMI-A-1"], drm=drm, pcms=lambda: asked.append(1) or set())
        self.assertEqual(asked, [])

    def test_configured_device(self):
        out = audio.configured("default:CARD=vc4hdmi1")
        self.assertEqual((out["kind"], out["card_id"], out["alsa"]), ("configured", "vc4hdmi1",
                                                                      "default:CARD=vc4hdmi1"))
        self.assertIsNone(audio.configured(""))
        with self.assertRaises(ValueError):
            audio.configured("hw:0 -monitor stdio")

    def test_pc_trees_ignore_pi_rules(self):
        make_tree(self.root)
        self.assertEqual(audio.choose(self.root, drm=self.root / "nodrm")["alsa"], "plughw:CARD=Generic,DEV=7")


class Vc4SoftvolTests(unittest.TestCase):
    """alsa-lib's default PCM for vc4 HDMI is plug -> softvol -> IEC958; its control ("PCM")
    exists once the PCM was opened. hostd opens it once with silence before the mixer probe."""

    def setUp(self):
        audio._softvol_done.clear()
        self.addCleanup(audio._softvol_done.clear)

    def runner(self, after_open=True, aplay_rc=0):
        calls, state = [], {"opened": False}

        def run(argv, **kw):
            import subprocess
            calls.append(argv)
            if argv[0] == "aplay":
                state["opened"] = aplay_rc == 0
                return subprocess.CompletedProcess(argv, aplay_rc, "", "")
            has = state["opened"] and after_open
            out = "Simple mixer control 'PCM',0\n" if has else "Simple mixer control 'IEC958',0\n"
            return subprocess.CompletedProcess(argv, 0, out, "")
        return calls, run

    OUT = {"vc4": True, "card_id": "vc4hdmi0", "alsa": "default:CARD=vc4hdmi0"}

    def test_opens_once_then_present(self):
        calls, run = self.runner()
        self.assertTrue(audio.vc4_softvol(self.OUT, run))
        self.assertEqual(calls[1], ["aplay", "-q", "-D", "default:CARD=vc4hdmi0", "-t", "raw", "-f", "S16_LE", "-r",
                                    "48000", "-c", "2", "-s", "480", "/dev/zero"])
        self.assertEqual(len(calls), 3)
        self.assertTrue(audio.vc4_softvol(self.OUT, run))      # cached: nothing runs again
        self.assertEqual(len(calls), 3)

    def test_output_not_enabled_is_tried_again(self):
        calls, run = self.runner(aplay_rc=1)
        self.assertFalse(audio.vc4_softvol(self.OUT, run))
        calls2, run2 = self.runner()
        self.assertTrue(audio.vc4_softvol(self.OUT, run2))

    def test_sysdefault_has_the_same_control(self):
        calls, run = self.runner()
        self.assertTrue(audio.vc4_softvol(dict(self.OUT, alsa="sysdefault:CARD=vc4hdmi0"), run))
        self.assertEqual(calls[1][3], "sysdefault:CARD=vc4hdmi0")

    def test_not_applicable(self):
        calls, run = self.runner()
        self.assertIsNone(audio.vc4_softvol(dict(self.OUT, alsa="hdmi:CARD=vc4hdmi0,DEV=0"), run))
        self.assertIsNone(audio.vc4_softvol({"kind": "analog", "card_id": "Headphones",
                                             "alsa": "plughw:CARD=Headphones,DEV=0"}, run))
        self.assertIsNone(audio.vc4_softvol(None, run))
        self.assertEqual(calls, [])


# ThinkPad Z13 Gen 1 (hardware, October 17): HDMI/DP on the Radeon's controller, the
# ALC287 (two CS35L41 amplifiers) on AMD's 1022:15e3, the ACP digital microphones.
Z13_CARDS = """ 0 [Generic        ]: HDA-Intel - HD-Audio Generic
                      HD-Audio Generic at 0xd0bc8000 irq 107
 1 [Generic_1      ]: HDA-Intel - HD-Audio Generic
                      HD-Audio Generic at 0xd0bc0000 irq 108
 2 [acp6x          ]: acp6x - acp6x
                      LENOVO-21D3S00H00-ThinkPadZ13Gen1-21D3S00H00
"""


def make_z13(root: Path, vendors=("0x1002", "0x1022"), align=None) -> Path:
    files = {"card0/pcm3p/info": pcm(0, 3, "HDMI 0"), "card0/pcm7p/info": pcm(0, 7, "HDMI 1"),
             "card0/eld#0.0": ELD_ABSENT, "card0/eld#0.1": ELD_PRESENT,
             "card1/pcm0p/info": pcm(1, 0, "ALC287 Analog"), "card1/pcm0c/info": pcm(1, 0, "ALC287 Analog", "CAPTURE"),
             "card2/pcm0c/info": pcm(2, 0, "acp6x-dmic", "CAPTURE")}
    for rel, text in files.items():
        f = root / "asound" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    (root / "asound" / "cards").write_text(Z13_CARDS)
    hda = {"0000:63:00.1": ("0x040300", True), "0000:63:00.6": ("0x040300", True)}
    sysfs = make_sound_sysfs(root, hda, {"card0": ("0000:63:00.1", ["pcmC0D3p"]),
                                         "card1": ("0000:63:00.6", ["pcmC1D0p"])})
    for dev, vendor in zip(("0000:63:00.1", "0000:63:00.6"), vendors):
        (sysfs / "devices" / "pci0000:00" / dev / "vendor").write_text(vendor + "\n")
    if align is not None:
        param = sysfs / "module" / "snd_hda_intel" / "parameters"
        param.mkdir(parents=True)
        (param / "align_buffer_size").write_text(f"{align}\n")
    return sysfs


class AlignedHdaTests(unittest.TestCase):
    """snd_hda_intel's 128-byte buffer alignment on non-Intel controllers broke QEMU's
    44.1 kHz stream through plughw on a 48 kHz codec (image/reports/z13-audio-2026-10-17.md)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_z13_speakers_play_through_keepalive(self):
        sysfs = make_z13(self.tmp)
        chosen = audio.choose(self.tmp / "asound", prefer="analog", sys_root=sysfs)
        self.assertEqual((chosen["card_id"], chosen["kind"], chosen.get("aligned")), ("Generic_1", "analog", True))
        self.assertEqual(chosen["alsa"], "rb_keepalive:CARD=Generic_1,DEV=0")
        self.assertEqual(audio.qemu_audiodev(chosen["alsa"]),
                         "alsa,id=snd0,out.dev=rb_keepalive:CARD=Generic_1,,DEV=0")
        # An external display with speakers on the Radeon's HDMI controller: aligned too.
        hdmi = audio.choose(self.tmp / "asound", prefer="hdmi", sys_root=sysfs)
        self.assertEqual(hdmi["alsa"], "rb_keepalive:CARD=Generic,DEV=7")

    def test_intel_controllers_keep_plughw(self):
        sysfs = make_z13(self.tmp, vendors=("0x8086", "0x8086"))
        self.assertEqual(audio.choose(self.tmp / "asound", prefer="analog", sys_root=sysfs)["alsa"],
                         "plughw:CARD=Generic_1,DEV=0")

    def test_module_parameter_overrides_the_vendor(self):
        off = make_z13(self.tmp / "off", align=0)
        self.assertEqual(audio.choose(self.tmp / "off" / "asound", prefer="analog", sys_root=off)["alsa"],
                         "plughw:CARD=Generic_1,DEV=0")
        on = make_z13(self.tmp / "on", vendors=("0x8086", "0x8086"), align=1)
        self.assertEqual(audio.choose(self.tmp / "on" / "asound", prefer="analog", sys_root=on)["alsa"],
                         "rb_keepalive:CARD=Generic_1,DEV=0")
        auto = make_z13(self.tmp / "auto", align=-1)
        self.assertTrue(audio.hda_aligned(1, auto))

    def test_no_sysfs_for_another_tree(self):
        make_z13(self.tmp)
        self.assertEqual(audio.choose(self.tmp / "asound", prefer="analog")["alsa"], "plughw:CARD=Generic_1,DEV=0")

    def test_capture_and_non_hda_cards_untouched(self):
        sysfs = make_z13(self.tmp)
        outs = audio.playback_outputs(self.tmp / "asound", sysfs)
        self.assertEqual({(o["card_id"], o["device"]) for o in outs if o.get("keepalive")},
                         {("Generic", 3), ("Generic", 7), ("Generic_1", 0)})
        self.assertFalse(any(o["card_id"] == "acp6x" for o in outs))


class FakeAmixer:
    """amixer's simple-control interface over one card's elements: {(name, index): {"caps",
    "vol", "sw"} or {"items", "item"}}; records every call."""

    def __init__(self, elements: dict, rc: int = 0):
        self.el, self.rc, self.calls = elements, rc, []

    @staticmethod
    def key(spec: str) -> tuple:
        name, _, index = spec.partition(",")
        return name, int(index or 0)

    def __call__(self, argv, **kw):
        import subprocess
        self.calls.append(argv)
        args = [a for a in argv[3:] if a != "-q"]          # amixer -c CARD ...
        out = ""
        if self.rc == 0 and args[0] == "scontrols":
            out = "".join(f"Simple mixer control '{n}',{i}\n" for n, i in self.el)
        elif self.rc == 0 and args[0] == "sget":
            e = self.el[self.key(args[1])]
            if "items" in e:
                out = (f"  Capabilities: enum\n  Items: {' '.join(repr(i) for i in e['items'])}\n"
                       f"  Item0: '{e['item']}'\n")
            else:
                out = f"  Capabilities: {e['caps']}\n  Front Left: Playback {e.get('vol')} [{e.get('sw')}]\n"
        elif self.rc == 0 and args[0] == "sset":
            e = self.el[self.key(args[1])]
            for v in args[2:]:
                if "items" in e:
                    e["item"] = v
                elif v.endswith("%"):
                    e["vol"] = 87 * int(v[:-1]) // 100
                else:
                    e["sw"] = "on" if v == "unmute" else "off"
        return subprocess.CompletedProcess(argv, self.rc, out, "")


def z13_codec() -> dict:
    """Card 1 of the ThinkPad Z13 Gen 1 as alsactl left it (hardware, October 18): alsa-ucm-conf's
    HDA BootSequence (ucm2/HDA/init.conf) with Master at hostd's 70 %."""
    return {("Master", 0): {"caps": "pvolume pvolume-joined pswitch pswitch-joined", "vol": 61, "sw": "on"},
            ("Headphone", 0): {"caps": "pvolume pswitch", "vol": 52, "sw": "off"},
            ("Speaker", 0): {"caps": "pvolume pswitch", "vol": 52, "sw": "off"},
            ("Mic ACP LED", 0): {"caps": "cswitch cswitch-joined"},
            ("Mic Boost", 0): {"caps": "volume"},
            ("Capture", 0): {"caps": "cvolume cswitch", "vol": 38, "sw": "off"},
            ("Auto-Mute Mode", 0): {"items": ["Disabled", "Enabled"], "item": "Disabled"}}


class HdaOutputStageTests(unittest.TestCase):
    """The Z13's speakers were muted by the card's UCM init and nothing turned them on
    (hostd only sets Master); hostd now turns the chosen card's output stages on."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sysfs = make_z13(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def chosen(self, prefer="analog"):
        return audio.choose(self.tmp / "asound", prefer=prefer, sys_root=self.sysfs)

    def test_z13_speakers_and_headphones_on_at_0db(self):
        run = FakeAmixer(z13_codec())
        self.assertEqual(audio.hda_output_stage(self.chosen(), run),
                         ["Headphone", "Speaker", "Auto-Mute Mode=Enabled"])
        el = run.el
        self.assertEqual([(el[(n, 0)]["vol"], el[(n, 0)]["sw"]) for n in ("Speaker", "Headphone")],
                         [(87, "on"), (87, "on")])
        self.assertEqual(el[("Auto-Mute Mode", 0)]["item"], "Enabled")
        self.assertEqual((el[("Master", 0)]["vol"], el[("Master", 0)]["sw"]), (61, "on"))   # hostd's volume
        self.assertEqual(el[("Capture", 0)]["sw"], "off")                                  # inputs untouched
        self.assertTrue(all(c[:3] == ["amixer", "-c", "Generic_1"] for c in run.calls))
        # Every launch: the same settings again, Auto-Mute left as it is now.
        self.assertEqual(audio.hda_output_stage(self.chosen(), run), ["Headphone", "Speaker"])

    def test_without_master_only_the_switches(self):
        """No Master: hostd's volume control is another element (AudioMixer), its level stays."""
        codec = {("Speaker", 0): {"caps": "pvolume pswitch", "vol": 40, "sw": "off"},
                 ("Headphone", 0): {"caps": "pvolume pswitch", "vol": 40, "sw": "off"},
                 ("Headphone", 1): {"caps": "pswitch", "sw": "off"}}
        run = FakeAmixer(codec)
        self.assertEqual(audio.hda_output_stage(self.chosen(), run), ["Speaker", "Headphone", "Headphone,1"])
        self.assertEqual([(e.get("vol"), e["sw"]) for e in codec.values()], [(40, "on"), (40, "on"), (None, "on")])

    def test_auto_mute_with_line_out_mutes_the_speakers_only(self):
        codec = z13_codec()
        codec[("Auto-Mute Mode", 0)] = {"items": ["Disabled", "Speaker Only", "Line Out+Speaker"], "item": "Disabled"}
        run = FakeAmixer(codec)
        self.assertIn("Auto-Mute Mode=Speaker Only", audio.hda_output_stage(self.chosen(), run))
        codec[("Auto-Mute Mode", 0)]["item"] = "Line Out+Speaker"       # a choice already made stays
        self.assertNotIn("Auto-Mute Mode=Speaker Only", audio.hda_output_stage(self.chosen(), run))
        self.assertEqual(codec[("Auto-Mute Mode", 0)]["item"], "Line Out+Speaker")

    def test_hdmi_digital_outputs_on(self):
        codec = {("IEC958", i): {"caps": "pswitch pswitch-joined", "sw": "off"} for i in range(4)}
        run = FakeAmixer(codec)
        hdmi = self.chosen("hdmi")
        self.assertEqual((hdmi["card_id"], hdmi["kind"]), ("Generic", "hdmi"))
        self.assertEqual(audio.hda_output_stage(hdmi, run), ["IEC958,0", "IEC958,1", "IEC958,2", "IEC958,3"])
        self.assertTrue(all(e["sw"] == "on" for e in codec.values()))

    def test_other_outputs_and_failures(self):
        run = FakeAmixer(z13_codec())
        for out in (None, {"kind": "usb", "driver": "USB-Audio", "card_id": "Device"},
                    {"kind": "analog", "driver": "sof-essx8336", "card_id": "sofessx8336", "asoc": True},
                    audio.configured("plughw:CARD=Generic_1,DEV=0"),
                    {"kind": "other", "driver": "HDA-Intel", "card_id": "Generic_1"}):
            self.assertIsNone(audio.hda_output_stage(out, run))
        self.assertEqual(run.calls, [])
        self.assertIsNone(audio.hda_output_stage(self.chosen(), FakeAmixer(z13_codec(), rc=1)))

        def missing(argv, **kw):
            raise FileNotFoundError("amixer")
        self.assertIsNone(audio.hda_output_stage(self.chosen(), missing))



if __name__ == "__main__":
    unittest.main()


def make_sound_sysfs(root: Path, controllers: dict, cards: dict, codecs: dict | None = None) -> Path:
    """controllers: {pci_name: (class, bound)}; cards: {cardN: (pci_name, [pcm names])};
    codecs: {pci_name: [hdaudioCxDy names]} (HD Audio codecs found on the controller)."""
    sysfs = root / "sys"
    pci = sysfs / "bus" / "pci" / "devices"
    for name, (cls, bound) in controllers.items():
        d = sysfs / "devices" / "pci0000:00" / name
        d.mkdir(parents=True)
        (d / "class").write_text(cls + "\n")
        if bound:
            drv = sysfs / "bus" / "pci" / "drivers" / "snd_hda_intel"
            drv.mkdir(parents=True, exist_ok=True)
            (d / "driver").symlink_to(drv)
        for codec in (codecs or {}).get(name, []):
            (d / codec).mkdir()
        pci.mkdir(parents=True, exist_ok=True)
        (pci / name).symlink_to(d)
    for card, (dev, pcms) in cards.items():
        c = sysfs / "devices" / "pci0000:00" / dev / "sound" / card
        c.mkdir(parents=True)
        (c / "device").symlink_to(sysfs / "devices" / "pci0000:00" / dev)
        for pcm in pcms:
            (c / pcm).mkdir()
        cls = sysfs / "class" / "sound"
        cls.mkdir(parents=True, exist_ok=True)
        (cls / card).symlink_to(c)
    return sysfs


HDA = {"0000:00:1f.3": ("0x040380", True)}
CODECS = {"0000:00:1f.3": ["hdaudioC0D0", "hdaudioC0D2"]}


class PendingSoundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_codecs_found_but_no_playback_pcm_is_pending(self):
        # The X1 Carbon: codecs enumerated, the card waits for a codec module's load.
        sysfs = make_sound_sysfs(self.tmp, HDA, {}, CODECS)
        self.assertEqual(audio.pending_sound(sysfs), ["0000:00:1f.3"])

    def test_registered_card_is_ready(self):
        sysfs = make_sound_sysfs(self.tmp, HDA, {"card0": ("0000:00:1f.3", ["pcmC0D0p", "pcmC0D0c", "pcmC0D3p"])},
                                 CODECS)
        self.assertEqual(audio.pending_sound(sysfs), [])

    def test_capture_only_card_still_pending(self):
        sysfs = make_sound_sysfs(self.tmp, HDA, {"card0": ("0000:00:1f.3", ["pcmC0D0c"])}, CODECS)
        self.assertEqual(audio.pending_sound(sysfs), ["0000:00:1f.3"])

    def test_controller_without_codecs_is_not_waited_for(self):
        # A Proxmox q35 VM's ICH9 HD Audio has no codecs and never registers a card.
        sysfs = make_sound_sysfs(self.tmp, {"0000:00:1b.0": ("0x040300", True)}, {})
        self.assertEqual(audio.pending_sound(sysfs), [])

    def test_unbound_non_audio_and_sof_devices_are_ignored(self):
        sysfs = make_sound_sysfs(self.tmp, {"0000:00:1f.3": ("0x040380", False),
                                            "0000:00:02.0": ("0x030000", True),
                                            "0000:00:1f.4": ("0x040100", True)}, {},
                                 {"0000:00:1f.3": ["hdaudioC0D0"]})
        self.assertEqual(audio.pending_sound(sysfs), [])

    def test_no_pci_bus(self):
        # Raspberry Pi and Gemini PDA: no PCI sound, never a wait.
        self.assertEqual(audio.pending_sound(self.tmp / "sys"), [])


class WaitForSoundTests(unittest.IsolatedAsyncioTestCase):
    def app(self):
        from types import SimpleNamespace
        events = []
        return SimpleNamespace(cfg=SimpleNamespace(allow_network_tools=True),
                               lifecycle=SimpleNamespace(event=lambda name, **kw: events.append((name, kw)))), events

    async def test_waits_until_the_card_registers(self):
        from retroboot.hostd import Hostd
        app, events = self.app()
        answers = [["0000:00:1f.3"]] * 3 + [[]]
        with unittest.mock.patch.object(audio, "pending_sound", side_effect=lambda root: answers.pop(0)), \
                unittest.mock.patch.object(audio, "SOUND_WAIT_UNTIL_UPTIME_S", 1e12):
            waited = await Hostd.wait_for_sound(app, poll_s=0.01)
        self.assertGreater(waited, 0)
        self.assertEqual(events[0][0], "audio_wait")
        self.assertEqual(events[0][1]["pending"], [])

    async def test_one_budget_per_run_and_never_late(self):
        from retroboot.hostd import Hostd
        app, events = self.app()
        with unittest.mock.patch.object(audio, "pending_sound", return_value=["0000:00:1f.3"]), \
                unittest.mock.patch.object(audio, "SOUND_WAIT_UNTIL_UPTIME_S", 1e12), \
                unittest.mock.patch.object(audio, "SOUND_WAIT_S", 0.05):
            waited = await Hostd.wait_for_sound(app, poll_s=0.01)
            self.assertLess(waited, 1.0)
            self.assertEqual(events[-1][1]["pending"], ["0000:00:1f.3"])
            # The launch after hostd's start (supervisor) gets no second budget.
            events.clear()
            self.assertEqual(await Hostd.wait_for_sound(app, poll_s=0.01), 0.0)
            self.assertEqual(events, [])
        # Past the boot window (a later launch): no wait at all.
        app2, events2 = self.app()
        with unittest.mock.patch.object(audio, "pending_sound", return_value=["0000:00:1f.3"]), \
                unittest.mock.patch.object(audio, "SOUND_WAIT_UNTIL_UPTIME_S", 0.0):
            self.assertEqual(await Hostd.wait_for_sound(app2, poll_s=0.01), 0.0)
        self.assertEqual(events2, [])

    async def test_waits_for_alsactl(self):
        """ThinkPad Z13: alsa-restore's UCM init muted the speakers until 5.5 s after the
        kernel started; hostd's first choice (and output stage) came at 5.3 s."""
        from retroboot.hostd import Hostd
        app, events = self.app()
        running = [True, True, False]
        with unittest.mock.patch.object(audio, "pending_sound", return_value=[]), \
                unittest.mock.patch.object(audio, "alsactl_running", side_effect=lambda proc: running.pop(0)), \
                unittest.mock.patch.object(audio, "SOUND_WAIT_UNTIL_UPTIME_S", 1e12):
            self.assertGreater(await Hostd.wait_for_sound(app, poll_s=0.01), 0)
        self.assertEqual(running, [])
        self.assertEqual(events, [("audio_wait", {"waited_s": events[0][1]["waited_s"], "pending": []})])

    def test_alsactl_running(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        for pid, comm in (("1", "systemd"), ("745", "alsactl"), ("self", "alsactl")):
            (tmp / pid).mkdir()
            (tmp / pid / "comm").write_text(comm + "\n")
        (tmp / "999").mkdir()                       # exited between listing and reading
        self.assertTrue(audio.alsactl_running(tmp))
        self.assertEqual(audio.sound_setup_pending(tmp / "nosys", tmp), ["alsactl"])
        (tmp / "745" / "comm").write_text("python3\n")
        self.assertFalse(audio.alsactl_running(tmp))
        self.assertFalse(audio.alsactl_running(tmp / "missing"))
