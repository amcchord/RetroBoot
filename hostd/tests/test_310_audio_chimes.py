"""Tests for RetroBoot 3.1.0 chimes, mixer re-probing, pre-fsck recovery, resize, and pointer capabilities."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import helpers
from retroboot import audio, graphics, pointer, schema
from retroboot.config import Config, Paths
from retroboot.machines import MachineRegistry
from retroboot.network import AudioMixer
from retroboot.supervisor import Supervisor


def _fake_supervisor_app(root: Path, machine, mixer=None, audio_out=None, settings_vals=None):
    paths = Paths(root)
    paths.ensure()
    vals = {**schema.defaults(), "machine.active": getattr(machine, "id", None), **(settings_vals or {})}
    events = []
    callbacks = []
    launched = []

    class FakeLauncher:
        def start(self, spec):
            launched.append(spec)
            raise RuntimeError("stop-before-exec")

    app = SimpleNamespace(
        cfg=Config(root=str(root), battery_poll_s=0.01),
        paths=paths,
        platform=SimpleNamespace(
            is_raspberry_pi=False,
            is_gemini=False,
            is_laptop=True,
            is_zero2w=False,
            tcg_tb_size_mb=128,
            machine_unavailable=lambda f: None,
            profile={},
        ),
        mode="normal",
        machines=SimpleNamespace(
            get=lambda mid: machine if machine and mid == machine.id else None,
            validate=lambda m: [],
            list=lambda: [machine] if machine else [],
        ),
        settings=SimpleNamespace(applied=vals, desired=dict(vals), revision=1),
        lifecycle=SimpleNamespace(event=lambda name, **kw: events.append((name, kw))),
        presenter=SimpleNamespace(display={"mode": [1920, 1080, 60], "scale": 2, "suggested_scale": 2}),
        apply_engine=SimpleNamespace(
            apply_pending_restart=AsyncMock(return_value=[]),
            push_volume=AsyncMock(return_value=True),
            push_display=AsyncMock(return_value=True),
        ),
        startup=SimpleNamespace(on_start=AsyncMock(), on_launched=AsyncMock(), config=lambda: {}),
        media=SimpleNamespace(cd_for_launch=lambda *a, **kw: None, inserted=None),
        panel=SimpleNamespace(
            visible=False,
            forget=Mock(),
            show_setup=AsyncMock(),
            show_failure=AsyncMock(),
            show_needs_cd=AsyncMock(),
            show_startup_options=AsyncMock(),
            show_corrupt_disk_recovery=AsyncMock(),
        ),
        diagnostics=SimpleNamespace(capture_evidence=AsyncMock(return_value="eid-1")),
        guestprep=SimpleNamespace(
            apply=AsyncMock(return_value={"result": "applied", "report": {}}),
            repair=AsyncMock(return_value={"result": "failed", "error": "corrupt B-tree"}),
            version=lambda: 1,
            for_family=lambda f: {
                "apply": AsyncMock(return_value={"result": "applied", "report": {}}),
                "repair": AsyncMock(return_value={"result": "failed", "error": "corrupt B-tree"}),
                "version": lambda: 1,
            },
            usb_overdrive_present=Mock(return_value=False),
        ),
        airport=SimpleNamespace(prepare_launch=AsyncMock()),
        platinum=SimpleNamespace(ensure=AsyncMock()),
        optical=SimpleNamespace(present=lambda: False, list=lambda *a: [], probe=AsyncMock(return_value=[]), unlock=Mock()),
        battery=SimpleNamespace(launch_socket=lambda *a, **kw: None, launched=Mock()),
        keys=SimpleNamespace(launch_buttons=lambda *a, **kw: None, launched=Mock()),
        cpuplace=SimpleNamespace(plan=SimpleNamespace(enabled=False)),
        mixer=mixer or AudioMixer(runner=AsyncMock(return_value=(0, "", ""))),
        laptop=SimpleNamespace(muted=False),
        launcher=FakeLauncher(),
        show_scene=AsyncMock(),
        wait_for_sound=AsyncMock(return_value=0.0),
        audio_capable=lambda: True,
        choose_audio_output=lambda: audio_out,
        ensure_guest_isolation=AsyncMock(),
        package=SimpleNamespace(verify=Mock(), ndrv=None, loader=None, summary=lambda: {}),
        package_osx=SimpleNamespace(verify=Mock(), summary=lambda: {}),
        package_m68k=SimpleNamespace(verify=Mock(), summary=lambda: {}),
        package_for=lambda m: app.package_osx if getattr(m, "osx", False) else app.package,
        on_corrupt_disk_recovery=AsyncMock(side_effect=lambda info: callbacks.append(info)),
    )
    app.graphics = graphics.Graphics(app)
    return app, events, callbacks, launched


class StartupChimeTests(unittest.TestCase):
    def test_resolve_chime_name_contract(self):
        self.assertEqual(audio.resolve_chime_name("quadra800", "auto"), "quadra")
        self.assertEqual(audio.resolve_chime_name("powermac", "auto"), "os9")
        self.assertEqual(audio.resolve_chime_name("powermac-osx", "auto"), "os9")
        for explicit in ("os9", "quadra", "powermac", "macii", "se"):
            self.assertEqual(audio.resolve_chime_name("powermac", explicit), explicit)
        self.assertIsNone(audio.resolve_chime_name("powermac", "off"))
        self.assertIsNone(audio.resolve_chime_name("powermac", "invalid"))

    def test_play_startup_chime_uses_rb_chimes_dir_warm_up_and_skips_missing(self):
        with tempfile.TemporaryDirectory() as td:
            cdir = Path(td)
            spawned = []
            warmed = []

            def fake_popen(cmd, **kw):
                spawned.append(cmd)
                return SimpleNamespace(pid=123)

            with patch.dict(os.environ, {"RB_CHIMES_DIR": str(cdir)}):
                # Missing WAV skips cleanly without spawning or failing (#12).
                self.assertFalse(audio.play_startup_chime(
                    "quadra800", "auto", audio_out={"kind": "analog", "card_id": "PCH", "device": 0,
                                                    "alsa": "plughw:CARD=PCH,DEV=0"},
                    popen=fake_popen, warm_up_fn=lambda *a, **kw: warmed.append(True) or True,
                ))
                self.assertEqual(spawned, [])

                (cdir / "quadra.wav").write_bytes(b"RIFFfake")
                (cdir / "os9.wav").write_bytes(b"RIFFfake")

                # Keepalive ES8336 output calls warm_up before spawning aplay.
                out = {"kind": "analog", "card_id": "sofessx8336", "device": 0,
                       "alsa": "rb_keepalive:CARD=sofessx8336,DEV=0", "keepalive": True, "asoc": True}
                ok = audio.play_startup_chime(
                    "quadra800", "auto", audio_out=out, popen=fake_popen,
                    run=lambda *a, **kw: SimpleNamespace(returncode=1, stdout=""),
                    warm_up_fn=lambda o, d, **kw: warmed.append((o["card_id"], d)) or True,
                )
                self.assertTrue(ok)
                self.assertEqual(warmed, [("sofessx8336", "rb_keepalive:CARD=sofessx8336,DEV=0")])
                self.assertEqual(spawned[-1], ["aplay", "-q", "-D", "rb_keepalive:CARD=sofessx8336,DEV=0",
                                               str(cdir / "quadra.wav")])

                # Off or muted skips cleanly.
                self.assertFalse(audio.play_startup_chime("powermac", "off", audio_out=out, popen=fake_popen))
                self.assertFalse(audio.play_startup_chime("powermac", "auto", audio_out=out, muted=True,
                                                          popen=fake_popen))

                # Bluetooth sink plays via pw-play/paplay when available.
                bt_out = {"kind": "bluetooth", "pw_sink": "bluez_output.AA.1"}
                self.assertTrue(audio.play_startup_chime(
                    "powermac", "auto", audio_out=bt_out, popen=fake_popen,
                    which=lambda b: f"/usr/bin/{b}" if b == "pw-play" else None,
                ))
                self.assertEqual(spawned[-1], ["pw-play", "--target", "bluez_output.AA.1", str(cdir / "os9.wav")])

    def test_sound_generates_diagnostic_beep_or_uses_chime_and_rejects_missing_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            asound = root / "asound"
            asound.mkdir()
            (asound / "cards").write_text(" 0 [PCH ]: HDA-Intel - PCH\n   PCH\n")
            p = asound / "card0/pcm0p/info"
            p.parent.mkdir(parents=True)
            p.write_text("card: 0\ndevice: 0\nsubdevice: 0\nstream: PLAYBACK\nid: ALC287\nname: ALC287\n")

            spawned = []
            res = audio.test_sound(
                output="speakers",
                root=asound,
                chimes_dir=root / "empty-chimes",
                run_dir=root / "run",
                run=lambda *a, **kw: SimpleNamespace(returncode=1, stdout=""),
                popen=lambda cmd, **kw: spawned.append(cmd) or SimpleNamespace(pid=1),
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["player"], "aplay")
            self.assertEqual(res["device"], "plughw:CARD=PCH,DEV=0")
            self.assertTrue(Path(res["file"]).is_file())
            self.assertEqual(spawned[-1], ["aplay", "-q", "-D", "plughw:CARD=PCH,DEV=0", res["file"]])

            bad = audio.test_sound(output="usb", root=asound, chimes_dir=root / "empty-chimes")
            self.assertFalse(bad["ok"])
            self.assertIn("not available", bad["error"])


class Supervisor310IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_x86_mixer_reprobes_on_output_change_and_passes_osx_48k(self):
        with helpers.scratch()[0] as td:
            root = Path(td).resolve()
            helpers.make_package(root)
            helpers.make_machine(root)
            paths = Paths(root)
            paths.ensure()
            reg = MachineRegistry(paths)
            m = reg.get("mac-os-9")

            calls = []

            async def amixer_run(argv, timeout=None):
                calls.append(argv)
                if "scontrols" in argv:
                    return 0, "Simple mixer control 'Master',0\n", ""
                if argv[-2:] == ["sget", "Master"]:
                    return 0, "Simple mixer control 'Master',0\n  Capabilities: pvolume pswitch\n", ""
                return 0, "", ""

            mixer = AudioMixer(runner=amixer_run)
            await mixer.probe("Generic", detect_switch=True)
            self.assertEqual(mixer.card, "Generic")
            calls.clear()

            analog_out = {"kind": "analog", "card": 2, "card_id": "Generic_1", "device": 0,
                          "driver": "HDA-Intel", "name": "ALC897", "alsa": "plughw:CARD=Generic_1,DEV=0"}
            app, events, _, launched = _fake_supervisor_app(root, m, mixer=mixer, audio_out=analog_out)
            sup = Supervisor(app)
            app.supervisor = sup
            sup.prepare_disk = AsyncMock(return_value=None)
            sup.mac_cities = AsyncMock(return_value=None)
            sup.usb_pointer = AsyncMock(return_value=(False, None))

            def stop_at_spec(machine, argv, *a, **kw):
                launched.append(argv)
                return {"session": "s1", "argv": argv}

            with patch("retroboot.supervisor.launch_spec", side_effect=stop_at_spec):
                await sup._start_locked("test")
            self.assertEqual(len(launched), 1)
            self.assertEqual(mixer.card, "Generic_1")
            self.assertEqual(mixer.control, "Master")
            self.assertTrue(any("-c" in c and "Generic_1" in c for c in calls))
            app.apply_engine.push_volume.assert_awaited()

            # When muted, re-probing preserves muted state instead of unmuting (#39).
            app.laptop.muted = True
            calls.clear()
            mixer.card = "Generic"
            with patch("retroboot.supervisor.launch_spec", side_effect=stop_at_spec):
                await sup._start_locked("test")
            self.assertEqual(len(launched), 2)
            self.assertIn(["amixer", "-c", "Generic_1", "-q", "sset", "Master", "mute"], calls)
            self.assertIn(["amixer", "-c", "Generic_1", "-q", "sset", "Master", "mute"], calls)

    async def test_osx_48k_broad_policy_and_pre_fsck_corrupt_disk_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            helpers.make_package(root)
            helpers.make_machine(root)
            paths = Paths(root)
            paths.ensure()
            reg = MachineRegistry(paths)
            m = reg.get("mac-os-9")
            m.update_meta(prep={"result": "applied", "version": 1})
            m.set_state(clean=False, last_exit={"reason": "power-loss"})

            # Seed an existing "Fresh Install" clean checkpoint.
            cp_clean = paths.checkpoints / m.id / "20260101T000000Z"
            cp_clean.mkdir(parents=True)
            (cp_clean / "disk.img").write_bytes(b"clean")
            (cp_clean / "checkpoint.json").write_text(json.dumps({"id": "20260101T000000Z", "label": "Fresh Install"}))

            app, events, callbacks, launched = _fake_supervisor_app(root, m)
            sup = Supervisor(app)
            app.supervisor = sup
            ok = await sup._start_locked("boot")
            self.assertFalse(ok)
            self.assertEqual(launched, [])  # MUST NOT launch QEMU on failed fsck (#48)!
            self.assertIsNotNone(sup.corrupt_disk_recovery)
            self.assertEqual(sup.corrupt_disk_recovery["fresh_install_checkpoint"], "20260101T000000Z")
            self.assertEqual(sup.corrupt_disk_recovery["last_clean_checkpoint"], "20260101T000000Z")
            pre = sup.corrupt_disk_recovery["pre_fsck_checkpoint"]
            self.assertIsNotNone(pre)
            self.assertTrue((Path(pre["path"]) / "disk.img").is_file())
            self.assertTrue((Path(pre["path"]) / "checkpoint.json").is_file())
            self.assertTrue(any(e[0] == "corrupt_disk_recovery" for e in events))
            self.assertEqual(len(callbacks), 1)
            self.assertEqual(sup.public()["corrupt_disk_recovery"], sup.corrupt_disk_recovery)

    async def test_monitors_relayout_nudge_and_pointer_capabilities(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            helpers.make_package(root)
            helpers.make_machine(root)
            paths = Paths(root)
            paths.ensure()
            reg = MachineRegistry(paths)
            m = reg.get("mac-os-9")
            app, events, _, _ = _fake_supervisor_app(root, m)
            sup = Supervisor(app)
            app.supervisor = sup
            sup.state = "running"
            sup.machine = m
            sup.session = "s1"
            sup.guest_surface = [800, 600]
            app.graphics.running = "standard"

            async def qmp_exec(cmd, args=None):
                if cmd == "qom-list" and args["path"] == "/machine/peripheral-anon":
                    return [{"name": "device[0]"}]
                if cmd == "qom-list" and args["path"] == "/machine/peripheral-anon/device[0]":
                    return [{"name": "host-resize-request"}]
                return {}

            sup.qmp = SimpleNamespace(execute=AsyncMock(side_effect=qmp_exec))
            with patch("retroboot.supervisor.asyncio.sleep", new=AsyncMock()):
                await sup._handle_relayout_timeout()

            qom_sets = [c.args for c in sup.qmp.execute.await_args_list if c.args[0] == "qom-set"]
            self.assertEqual([a[1]["value"] for a in qom_sets], ["960x541", "960x540"])
            self.assertIsNotNone(sup.manual_resize_notice)
            self.assertTrue(any(n["id"] == "monitors-resize-override" for n in app.graphics.notices()))

            await sup.on_presenter_event({"event": "guest_surface", "size": [960, 540]})
            self.assertIsNone(sup.manual_resize_notice)

        # #28 pointer capability helpers for 68k / pre-8.5 vs OS 9.2.2
        cap_q = pointer.pointer_capabilities(family="quadra800", guest_os="system-7.5.5")
        self.assertFalse(cap_q["usb_overdrive_supported"])
        self.assertFalse(cap_q["usb_mouse"])
        self.assertIn("Control-click", cap_q["notice"])
        cap_81 = pointer.pointer_capabilities(family="powermac", guest_os="macos-8.1")
        self.assertFalse(cap_81["usb_overdrive_supported"])
        cap_9 = pointer.pointer_capabilities(family="powermac", guest_os="macos-9.2.2", overdrive_present=True)
        self.assertTrue(cap_9["usb_overdrive_supported"])
        self.assertTrue(cap_9["usb_mouse"])
        self.assertIsNone(cap_9["notice"])

    async def test_supervisor_enables_48k_broadly_for_tiger_machines(self):
        from retroboot.machines import Machine
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            helpers.make_package(root)
            paths = Paths(root)
            paths.ensure()
            m_dir = paths.machines / "tiger-clone"
            m_dir.mkdir(parents=True)
            disk = m_dir / "disk.img"
            disk.write_bytes(b"\x00" * 1024)
            nvram = m_dir / "nvram.img"
            nvram.write_bytes(b"\x00" * 16384)
            meta = {"schema_version": 3, "id": "tiger-clone", "name": "Tiger Clone", "family": "powermac",
                    "guest_os": "macosx-10.4", "os_version": "10.4.11", "os_build": "8S165",
                    "nvram": "nvram.img", "template": {"machineFamily": "powerMacOSX", "guestOS": "macosx-10.4"}}
            (m_dir / "machine.json").write_text(json.dumps(meta))
            m = Machine("tiger-clone", m_dir, meta)
            seen_opts = []
            analog_out = {"kind": "analog", "card": 0, "card_id": "PCH", "device": 0,
                          "driver": "HDA-Intel", "name": "ALC287", "alsa": "plughw:CARD=PCH,DEV=0"}
            app, _, _, _ = _fake_supervisor_app(root, m, audio_out=analog_out)
            app.cfg.osx_48k_machine = "some-other-id"
            sup = Supervisor(app)
            app.supervisor = sup
            sup.prepare_disk = AsyncMock(return_value=None)

            def fake_build_argv(machine, settings, p, disp, options):
                seen_opts.append(options)
                raise ValueError("stop-after-argv")

            with patch("retroboot.osx_identity.refresh", return_value={"os_version": "10.4.11", "os_build": "8S165"}), \
                    patch("retroboot.osx.prepare", return_value=False), \
                    patch("retroboot.supervisor.build_argv", side_effect=fake_build_argv):
                try:
                    await sup._start_locked("test")
                except ValueError:
                    pass

            self.assertEqual(len(seen_opts), 1)
            self.assertTrue(seen_opts[0].osx_48k_output)
