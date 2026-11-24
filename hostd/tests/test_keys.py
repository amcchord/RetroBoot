"""Brightness, volume and mute keys (retroboot/keys.py): the buttons device
argv, one route per key (Mac OS's buttons or the host), the presenter's
indicator only where Mac OS shows none, Mac OS's backlight on the host panel,
and the host's click while Mac OS is not running."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import helpers  # noqa: F401  (sys.path)
from retroboot import keys as K
from retroboot import schema
from retroboot.laptop import Backlight, Laptop
from retroboot.machines import LaunchOptions, build_argv
from test_laptop import FakeApp, make_backlight


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# -- pure helpers ---------------------------------------------------------------------
def test_machine_args():
    assert K.machine_args(None, "cuda") == []
    assert K.machine_args(K.DESKTOP, "cuda") == ["-device", "adb-buttons,bus=adb.0,id=keys0,handler=0x02"]
    pb = K.machine_args(K.POWERBOOK, "pmu-adb")
    assert pb == ["-global", "via-pmu.backlight=on", "-global", "VGA.builtin-panel=on",
                  "-device", "adb-buttons,bus=adb.0,id=keys0,handler=0x1f"]
    with pytest.raises(ValueError):
        K.machine_args(K.POWERBOOK, "cuda")


def test_buttons_policy():
    on, off = {K.SETTING: True}, {K.SETTING: False}
    assert K.buttons_for(on, capable=True, pmu=True) == K.POWERBOOK
    assert K.buttons_for(on, capable=True, pmu=False) is None      # Cuda: Mac OS loads no button driver
    assert K.buttons_for(off, capable=True, pmu=True) is None
    assert K.buttons_for(on, capable=False, pmu=False) is None
    assert K.guest_handles("brightness_up", K.POWERBOOK) and not K.guest_handles("brightness_up", K.DESKTOP)
    assert K.guest_handles("mute", K.DESKTOP) and not K.guest_handles("mute", None)
    assert not K.guest_handles("eject", K.POWERBOOK)


def test_setting_and_package(tmp_path):
    s = schema.BY_KEY[K.SETTING]
    assert s.default is True and s.apply == schema.GUEST_RESTART and s.capability == "mac_os_keys"
    assert not K.package_capable(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps({"features": ["pmu-battery-1", K.FEATURE]}))
    assert K.package_capable(tmp_path)
    assert K.ndrv_builtin_panel({"define": "RETROBOOT_PREFER_BOOT_MODE RETROBOOT_BUILTIN_PANEL"})
    assert not K.ndrv_builtin_panel({"define": "RETROBOOT_PREFER_BOOT_MODE"}) and not K.ndrv_builtin_panel(None)


def test_build_argv_adds_the_buttons():
    from retroboot.config import Paths
    from retroboot.machines import MachineRegistry
    td, root = helpers.scratch()
    try:
        helpers.make_package(root)
        helpers.make_machine(root)
        paths = Paths(root)
        paths.ensure()
        m = MachineRegistry(paths).get("mac-os-9")
        display = {"mode": [1920, 1080, 60], "suggested_scale": 2}
        settings = schema.defaults()
        plain = " ".join(build_argv(m, settings, paths, display))
        assert "adb-buttons" not in plain and "builtin-panel" not in plain
        desk = " ".join(build_argv(m, settings, paths, display, LaunchOptions(buttons=K.DESKTOP)))
        assert "-device adb-buttons,bus=adb.0,id=keys0,handler=0x02" in desk
        pb = " ".join(build_argv(m, settings, paths, display,
                                 LaunchOptions(power_socket=paths.run / "power.sock", buttons=K.POWERBOOK)))
        assert "via=pmu-adb" in pb and "-global VGA.builtin-panel=on" in pb and "handler=0x1f" in pb
        cd = Path(root) / "cd.iso"
        cd.write_bytes(b"\0" * 2048)
        install = " ".join(build_argv(m, settings, paths, display, LaunchOptions(boot_cd=cd, buttons=K.DESKTOP)))
        assert "adb-buttons" not in install            # the CD route keeps the plain machine
    finally:
        td.cleanup()


def test_follow_is_relative_and_bounded():
    # Mac OS dims by a ninth of LEVEL_SPAN: the host moves the same way, from where it is
    a = K.follow(50.0, 25, 30, 2.0)
    assert a < 50.0
    assert K.follow(a, 30, 25, 2.0) == pytest.approx(50.0, abs=0.01)
    # the same move on another panel table (the GPD MicroPC's 12, 14, ...)
    assert K.follow(50.0, 12, 17, 2.0) == pytest.approx(a, abs=0.01)
    assert K.follow(50.0, 25, 70, 2.0) == 2.0          # the whole span down: the host's floor
    assert K.follow(10.0, 70, 25, 2.0) == 100.0


# -- the routes ---------------------------------------------------------------------------
class QMP:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def execute(self, command, arguments=None, timeout=15.0):
        if self.fail:
            raise RuntimeError("QMP gone")
        self.calls.append((command, arguments))
        return {}


class KeysApp(FakeApp):
    def __init__(self, root: Path, *, running=True, finder=True, panel=False):
        super().__init__(root, [])
        self.laptop = Laptop(self)
        self.laptop.lid_open = True
        self.qmp = QMP()
        self.supervisor = SimpleNamespace(active=running, session="s1", finder_seen=finder, paused=False,
                                          qmp=self.qmp)
        self.battery = SimpleNamespace(asleep=False)
        self.panel.visible = panel
        self.scene = {"scene": "panel" if panel else "guest"}
        self.paths = SimpleNamespace(run=root, qemu_package=root / "pkg")
        self.clicks = []

    def choose_audio_output(self):
        return {"alsa": "plughw:CARD=PCH,DEV=0"}


def make(tmp_path, buttons=K.POWERBOOK, native=True, **kw):
    make_backlight(tmp_path, cur=600, mx=1200)
    app = KeysApp(tmp_path, **kw)
    k = K.FunctionKeys(app)
    k.TAP_S = 0.0
    k.BRIGHTNESS_WAIT_S = 0.05
    k.LATE_S = 0.05
    k.launched("s1", buttons, {"define": "RETROBOOT_BUILTIN_PANEL"} if native else None)
    app.keys = k
    return app, k


def presses(app):
    return [a["value"] for c, a in app.qmp.calls if c == "qom-set"]


def test_volume_goes_to_mac_os(tmp_path):
    app, k = make(tmp_path)

    async def go():
        await k.on_host_key("volume_up")
        await k.on_host_key("mute")
    run(go())
    assert presses(app) == ["volume-up:down", "volume-up:up", "mute:down", "mute:up"]
    assert app.volume_pushes == 0                      # the host mixer stays put
    assert not [r for r in app.requests if r[0] == "osd"]   # Mac OS draws its own window
    assert ("key", {"key": "volume_up", "route": "mac-os"}) in app.events


def test_muted_host_is_unmuted_before_mac_os_takes_the_keys(tmp_path):
    app, k = make(tmp_path)
    unmuted = []

    async def unmute():
        unmuted.append(True)
    app.laptop.muted = True
    app.laptop.unmute = unmute
    run(k.on_host_key("volume_up"))
    assert unmuted == [True] and presses(app)[:1] == ["volume-up:down"]


def test_desktop_keeps_brightness_on_the_host(tmp_path):
    app, k = make(tmp_path, buttons=K.DESKTOP, native=False)
    run(k.on_host_key("brightness_up"))
    assert presses(app) == []
    assert app.laptop.backlight.percent() > 50.0
    assert ("osd", {"kind": "brightness", "level": app.laptop.backlight.percent() / 100.0,
                    "muted": False}) in app.requests


@pytest.mark.parametrize("why", ["starting", "panel", "stopped", "asleep", "qmp-error"])
def test_host_route_and_its_indicator(tmp_path, why):
    kw = {"starting": dict(finder=False), "panel": dict(panel=True), "stopped": dict(running=False)}.get(why, {})
    app, k = make(tmp_path, **kw)
    if why == "asleep":
        app.battery.asleep = True
    if why == "qmp-error":
        app.qmp.fail = True
    run(k.on_host_key("volume_up"))
    assert presses(app) == []
    assert app.volume_pushes == 1 and app.settings.applied["audio.volume"] == 76
    osd = [r for r in app.requests if r[0] == "osd"]
    if why in ("starting", "asleep", "qmp-error"):
        # Mac OS is running: nothing over its startup; asleep, the screen is off anyway;
        # a lost button still shows the host's change once the Finder was seen
        assert osd == ([] if why == "starting" else [("osd", {"kind": "volume", "level": 0.76, "muted": False})])
    else:
        assert osd == [("osd", {"kind": "volume", "level": 0.76, "muted": False})]


def test_mac_os_up_without_a_recognized_finder(tmp_path):
    # An Appearance theme whose Finder hostd does not recognize: the supervisor's mac_os_up
    # (Mac OS started without it) lets the keys go to Mac OS, as the Finder would
    app, k = make(tmp_path, finder=False)
    assert not k._mac_os_up()
    app.supervisor.mac_os_up = True
    assert k._mac_os_up()
    run(k.on_host_key("volume_up"))
    assert presses(app) != []


def test_click_while_stopped_or_panel_open(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(K.subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd))
    app, k = make(tmp_path, running=False)
    run(k.on_host_key("volume_down"))
    assert spawned and spawned[0][:4] == ["aplay", "-q", "-D", "plughw:CARD=PCH,DEV=0"]
    assert Path(spawned[0][-1]).read_bytes()[:4] == b"RIFF"
    spawned.clear()
    # Local panel open over running Mac OS: key-click audio feedback is enabled (#41).
    app2, k2 = make(tmp_path / "b", panel=True)
    run(k2.on_host_key("volume_down"))
    assert spawned and spawned[0][:4] == ["aplay", "-q", "-D", "plughw:CARD=PCH,DEV=0"]
    spawned.clear()
    # Running Mac OS without panel (e.g. QMP error / starting): no host click.
    app3, k3 = make(tmp_path / "c", finder=False)
    run(k3.on_host_key("volume_down"))
    assert spawned == []


# -- Mac OS's backlight -------------------------------------------------------------------
def test_brightness_key_follows_mac_os(tmp_path):
    app, k = make(tmp_path)
    bl = app.laptop.backlight

    async def go():
        # Mac OS's startup level is only remembered
        await k.on_backlight({"type": "backlight", "level": 25, "on": True, "why": "level"})
        assert bl.percent() == 50.0
        await k.on_host_key("brightness_down")
        assert presses(app) == ["brightness-down:down", "brightness-down:up"]
        await k.on_backlight({"type": "backlight", "level": 26, "on": True, "why": "level"})
        after = bl.percent()
        assert after < 50.0
        await asyncio.sleep(0.1)                    # no watchdog step on top
        assert bl.percent() == after
        # at Mac OS's limit the key still moves the panel one host step
        await k.on_host_key("brightness_up")
        await k.on_backlight({"type": "backlight", "level": 25, "on": True, "why": "level"})
        await k.on_host_key("brightness_up")
        await k.on_backlight({"type": "backlight", "level": 25, "on": True, "why": "level"})
        assert bl.percent() > 50.0
    run(go())
    assert not [r for r in app.requests if r[0] == "osd"]      # Mac OS's own Brightness window


def test_brightness_indicator_without_the_builtin_panel(tmp_path):
    app, k = make(tmp_path, native=False)

    async def go():
        await k.on_backlight({"type": "backlight", "level": 25, "on": True, "why": "level"})
        await k.on_host_key("brightness_down")
        await k.on_backlight({"type": "backlight", "level": 26, "on": True, "why": "level"})
    run(go())
    assert [r[1]["kind"] for r in app.requests if r[0] == "osd"] == ["brightness"]


def test_brightness_watchdog_steps_the_host(tmp_path):
    app, k = make(tmp_path)

    async def go():
        await k.on_backlight({"type": "backlight", "level": 25, "on": True, "why": "level"})
        await k.on_host_key("brightness_up")
        await asyncio.sleep(0.07)                  # Mac OS has not answered in time
        once = app.laptop.backlight.percent()
        # its late answer is only remembered: no second step
        await k.on_backlight({"type": "backlight", "level": 24, "on": True, "why": "level"})
        assert app.laptop.backlight.percent() == once and k.guest_level == 24
        await asyncio.sleep(0.1)
    run(go())
    assert app.laptop.backlight.percent() > 50.0
    assert ("key", {"key": "brightness_up", "route": "host", "why": "no-backlight-report"}) in app.events


def test_monitors_slider_moves_the_panel_and_repeats_are_ignored(tmp_path):
    app, k = make(tmp_path)

    async def go():
        await k.on_backlight({"type": "backlight", "level": 25, "on": True, "why": "level"})
        await k.on_backlight({"type": "backlight", "level": 70, "on": True, "why": "level"})
        low = app.laptop.backlight.percent()
        for _ in range(5):
            await k.on_backlight({"type": "backlight", "level": 70, "on": True, "why": "level"})
        assert app.laptop.backlight.percent() == low == Backlight.MIN_PERCENT
        await k.on_backlight({"type": "backlight", "level": 70, "on": True, "why": "hello"})
    run(go())


def test_shut_down_never_darkens_the_host_panel(tmp_path):
    """GPD MicroPC: Mac OS's Shut Down set its backlight to 127 and hostd followed
    it to 2 %, leaving the panel dark under RetroBoot's own screens."""
    app, k = make(tmp_path)
    bl = app.laptop.backlight

    async def go():
        await k.on_backlight({"type": "backlight", "level": 22, "on": True, "why": "level"})
        await k.on_backlight({"type": "backlight", "level": 127, "on": True, "why": "level"})
        assert bl.percent() == 50.0
        await k.on_backlight({"type": "backlight", "level": 22, "on": True, "why": "level"})   # back: no jump
        assert bl.percent() == 50.0
        app.supervisor.intent = "shutdown-host"          # hostd is shutting Mac OS down
        await k.on_backlight({"type": "backlight", "level": 40, "on": True, "why": "level"})
        assert bl.percent() == 50.0
    run(go())


def test_lamp_off_and_back(tmp_path):
    app, k = make(tmp_path)

    async def go():
        await k.on_backlight({"type": "backlight", "level": 25, "on": False, "why": "power"})
        await k.on_backlight({"type": "backlight", "level": 25, "on": True, "why": "power"})
        await k.on_backlight({"type": "backlight", "level": 25, "on": False, "why": "power"})
        await k.guest_stopped()
    run(go())
    dp = [r[1] for r in app.requests if r[0] == "display_power"]
    assert dp == [{"output": "internal", "on": False}, {"output": "internal", "on": True},
                  {"output": "internal", "on": False}, {"output": "internal", "on": True}]


def test_lamp_stays_dark_when_shut_down_powers_off(tmp_path):
    """hostboot2: Mac OS's Shut Down darkens the panel; when the host powers off next,
    the lamp is not lit again for the black frame before power-off."""
    app, k = make(tmp_path)

    async def go():
        await k.on_backlight({"type": "backlight", "level": 25, "on": False, "why": "power"})
        await k.guest_stopped(host_power_off=True)
    run(go())
    dp = [r[1] for r in app.requests if r[0] == "display_power"]
    assert dp == [{"output": "internal", "on": False}]
    assert not k.lamp_off           # the next start begins from a clean state


def test_laptop_reports_what_changed(tmp_path):
    make_backlight(tmp_path, cur=600, mx=1200)
    app = FakeApp(tmp_path, [])
    lp = Laptop(app)

    async def go():
        b = await lp.on_host_key("brightness_up")
        v = await lp.on_host_key("volume_down")
        m = await lp.on_host_key("mute")
        x = await lp.on_host_key("eject")
        return b, v, m, x
    b, v, m, x = run(go())
    assert b["kind"] == "brightness" and b["level"] > 0.5
    assert v == {"kind": "volume", "level": 0.64, "muted": False}
    assert m["kind"] == "mute" and m["level"] == 0.64 and m["muted"] is False   # no mixer in the fake
    assert x is None
