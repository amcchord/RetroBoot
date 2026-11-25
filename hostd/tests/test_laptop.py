"""Laptop integration: backlight steps, power-supply parsing, lid and keys,
and the low-battery policy (warn once, clean shutdown at critical, force only
at the floor after a failed clean shutdown)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import helpers  # noqa: F401  (sys.path)
from retroboot.config import Config
from retroboot.jobs import FAILED, SUCCEEDED, Busy
from retroboot.laptop import Backlight, Laptop, PowerSupplies


def _w(p: Path, v) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{v}\n")


def make_backlight(root: Path, cur=600, mx=1200, kind="raw", name="intel_backlight"):
    d = root / "sys/class/backlight" / name
    _w(d / "brightness", cur)
    _w(d / "max_brightness", mx)
    _w(d / "type", kind)
    return d


def make_battery(root: Path, pct=50, status="Discharging", ac=False, energy=True):
    b = root / "sys/class/power_supply/BAT0"
    _w(b / "type", "Battery")
    _w(b / "present", 1)
    _w(b / "status", status)
    _w(b / "capacity", pct)
    _w(b / "voltage_now", 7600000)
    if energy:
        _w(b / "energy_now", int(30_000_000 * pct / 100))
        _w(b / "energy_full", 30_000_000)
        _w(b / "power_now", 6_000_000)
    else:
        _w(b / "charge_now", int(4_000_000 * pct / 100))
        _w(b / "charge_full", 4_000_000)
        _w(b / "current_now", 800_000)
    a = root / "sys/class/power_supply/ADP1"
    _w(a / "type", "Mains")
    _w(a / "online", 1 if ac else 0)
    return b, a


def test_backlight_prefers_firmware_and_steps(tmp_path):
    make_backlight(tmp_path, kind="raw", name="intel_backlight")
    fw = make_backlight(tmp_path, cur=50, mx=100, kind="firmware", name="acpi_video0")
    bl = Backlight(tmp_path)
    assert bl.device() == fw
    assert bl.percent() == 50.0
    seen = []
    for _ in range(20):
        seen.append(bl.step(+1))
    assert seen[-1] == 100.0 and all(b >= a for a, b in zip(seen, seen[1:]))
    for _ in range(30):
        last = bl.step(-1)
    assert last == Backlight.MIN_PERCENT          # never fully dark from the keys
    assert int((fw / "brightness").read_text()) >= 1


def test_backlight_absent(tmp_path):
    bl = Backlight(tmp_path)
    assert bl.device() is None and bl.percent() is None and bl.step(1) is None


def test_power_supplies(tmp_path):
    make_battery(tmp_path, pct=40, status="Discharging", ac=False)
    r = PowerSupplies(tmp_path).read()
    assert r["ac"] is False and r["batteries"] == 1
    b = r["battery"]
    assert b["percent"] == 40 and b["power_w"] == 6.0 and b["energy_wh"] == 12.0
    assert b["time_to_empty_min"] == 120
    # charge_* / current_* batteries (µAh, µA) are converted with the voltage
    make_battery(tmp_path / "c", pct=50, status="Charging", ac=True, energy=False)
    for f in ("energy_now", "energy_full", "power_now"):
        (tmp_path / "c/sys/class/power_supply/BAT0" / f).unlink(missing_ok=True)
    r = PowerSupplies(tmp_path / "c").read()
    assert r["ac"] is True and r["battery"]["energy_wh"] == 15.2 and "time_to_full_min" in r["battery"]


class FakeJob:
    def __init__(self, state):
        self.id = f"j-{state}"
        self.kind = "host.poweroff"
        self.state = state
        self.done = asyncio.Event()
        self.done.set()


class FakeApp:
    def __init__(self, root: Path, outcomes):
        self.cfg = Config(root=str(root), battery_poll_s=0.01)
        self.mode = "normal"
        self.events = []
        self.lifecycle = SimpleNamespace(event=lambda name, **kw: self.events.append((name, kw)))
        self.requests = []
        self.messages = []
        self.jobs_started = []
        self.outcomes = list(outcomes)
        app = self

        class Presenter:
            async def try_request(self, cmd, **kw):
                app.requests.append((cmd, kw))
                return {"ok": True}
        self.presenter = Presenter()

        class Panel:
            visible = False

            async def message(self, icon, message, detail=None, then=None, button="OK", auto_seconds=None):
                app.messages.append((icon, message, button))
                if then:
                    await then()

            async def close(self, to_guest=True):
                pass
        self.panel = Panel()
        self.supervisor = SimpleNamespace(active=True)
        self.settings = SimpleNamespace(applied={"audio.volume": 70})

        def set_da(values, ctx=None):
            self.settings.applied.update(values)
        self.settings.set_desired_and_applied = set_da
        self.mixer = SimpleNamespace(available=False, control=None)
        self.volume_pushes = 0

        async def push_volume():
            self.volume_pushes += 1
            return True
        self.apply_engine = SimpleNamespace(push_volume=push_volume)

    def validation_context(self):
        return None

    def host_job(self, action, force=False, origin="web"):
        self.jobs_started.append((action, force, origin))
        return FakeJob(self.outcomes.pop(0) if self.outcomes else SUCCEEDED)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_lid_and_keys(tmp_path):
    make_backlight(tmp_path, cur=600, mx=1200)
    app = FakeApp(tmp_path, [])
    lp = Laptop(app)

    async def go():
        await lp.on_lid(False)
        await lp.on_lid(True)
        await lp.on_host_key("volume_up")
        await lp.on_host_key("volume_down", repeat=True)
        await lp.on_host_key("volume_down")
        before = lp.backlight.percent()
        await lp.on_host_key("brightness_up")
        return before, lp.backlight.percent()
    before, after = run(go())
    assert app.requests == [("display_power", {"output": "internal", "on": False}),
                            ("display_power", {"output": "internal", "on": True})]
    assert app.settings.applied["audio.volume"] == 70 + 6 - 6 - 6
    assert app.volume_pushes == 3
    assert after > before
    assert lp.public()["lid"] == "open"


def _policy(tmp_path, readings, outcomes, ac=False):
    b, _ = make_battery(tmp_path, pct=readings[0], ac=ac)
    app = FakeApp(tmp_path, outcomes)
    lp = Laptop(app)

    async def go():
        lp.power = lp.supplies.read()
        for pct in readings:
            _w(b / "capacity", pct)
            await lp.poll()
            if lp._critical_task:
                await asyncio.wait_for(lp._critical_task, 5)
    return app, lp, b, go


def test_low_battery_warns_once_then_clean_shutdown(tmp_path):
    app, lp, _, go = _policy(tmp_path, [20, 10, 9, 8, 4, 3], [SUCCEEDED])
    run(go())
    warns = [m for m in app.messages if m[1].startswith("Your battery")]
    assert len(warns) == 1
    assert app.jobs_started == [("poweroff", False, "battery")]   # clean, never forced
    assert lp.battery_state == "critical"
    assert ("battery_critical", {"percent": 3}) in app.events


def test_single_low_sample_is_ignored(tmp_path):
    app, lp, _, go = _policy(tmp_path, [20, 3, 20], [])
    run(go())
    assert app.jobs_started == []


def test_on_ac_never_acts(tmp_path):
    app, lp, b, go = _policy(tmp_path, [5, 3, 2, 1], [], ac=True)
    _w(b / "status", "Charging")
    run(go())
    assert app.jobs_started == [] and app.messages == []


def test_failed_clean_shutdown_forces_only_at_floor(tmp_path, monkeypatch):
    app, lp, b, go = _policy(tmp_path, [4, 4], [FAILED, FAILED, SUCCEEDED])
    real_sleep = asyncio.sleep
    levels = iter([3, 1])

    async def fast_sleep(s):
        if s == 10:
            _w(b / "capacity", next(levels))
        await real_sleep(0)
    monkeypatch.setattr("retroboot.laptop.asyncio.sleep", fast_sleep)
    run(go())
    # clean attempt, clean retry at 3 %, then forced at 1 % (<= floor 2 %)
    assert app.jobs_started == [("poweroff", False, "battery"), ("poweroff", False, "battery"),
                                ("poweroff", True, "battery-floor")]
    assert lp.battery_state == "forced"


def test_busy_job_is_followed(tmp_path):
    app, lp, _, go = _policy(tmp_path, [4, 4], [])

    def busy(action, force=False, origin="web"):
        app.jobs_started.append((action, force, origin))
        raise Busy(FakeJob(SUCCEEDED))
    app.host_job = busy
    run(go())
    assert app.jobs_started == [("poweroff", False, "battery")]


def test_lid_state_read_at_start(tmp_path):
    assert Laptop(FakeApp(tmp_path, [])).public()["lid"] is None
    _w(tmp_path / "proc/acpi/button/lid/LID0/state", "state:      closed")
    assert Laptop(FakeApp(tmp_path, [])).public()["lid"] == "closed"
    _w(tmp_path / "proc/acpi/button/lid/LID0/state", "state:      open")
    assert Laptop(FakeApp(tmp_path, [])).public()["lid"] == "open"


def test_critical_notice_closing_by_itself_still_shuts_down(tmp_path, monkeypatch):
    """N5105 hardware: the "battery is almost empty" notice closed itself after
    8 s and the clean shutdown never started. The real Panel's ticker cancelled
    itself in panel.close(), inside the notice's continuation."""
    from retroboot.panel import controller

    orig = controller.MessageAlert.__init__

    def quick(self, *a, **kw):
        orig(self, *a, **kw)
        if self.auto:
            self.auto = self.tick = 0.05         # the 8 s auto-close, shortened
    monkeypatch.setattr(controller.MessageAlert, "__init__", quick)
    app, lp, _, go = _policy(tmp_path, [9, 3, 3], [SUCCEEDED])
    app.scenes = []

    async def show_scene(scene, **kw):
        app.scenes.append(scene)
        await asyncio.sleep(0)                   # a real scene change awaits the presenter
    app.show_scene = show_scene
    app.supervisor = SimpleNamespace(active=True, state="running", content_seen=True, paused=False)

    async def run_it():
        app.loop = asyncio.get_running_loop()
        app.panel = controller.Panel(app)
        await go()
        for _ in range(200):                     # the notice acts on its own, without a click
            if lp._critical_task:
                break
            await asyncio.sleep(0.01)
        assert lp._critical_task, "the clean shutdown never started after the notice closed itself"
        await asyncio.wait_for(lp._critical_task, 5)
    run(run_it())
    assert ("battery_critical", {"percent": 3}) in app.events
    assert app.jobs_started == [("poweroff", False, "battery")]
    assert app.scenes[0] == "panel" and app.scenes[-1] == "guest"
    assert not app.panel.visible


def test_panel_alert_continuation_survives_closing_the_panel():
    """Generic form: an alert whose auto-close continuation closes the panel and
    then keeps going (any MessageAlert with auto_seconds)."""
    from retroboot.panel import controller

    async def run_it():
        app = SimpleNamespace(mode="normal", loop=asyncio.get_running_loop(),
                              supervisor=SimpleNamespace(active=True, state="running", content_seen=True,
                                                         paused=False))

        class Presenter:
            async def try_request(self, cmd, **kw):
                await asyncio.sleep(0)
                return {}
        app.presenter = Presenter()

        async def show_scene(scene, **kw):
            await asyncio.sleep(0)
        app.show_scene = show_scene
        panel = controller.Panel(app)
        done = asyncio.Event()

        async def then():
            await panel.close()
            await asyncio.sleep(0)
            done.set()
        await panel.message("caution", "Closing by itself.", then=then, auto_seconds=0.05)
        await asyncio.wait_for(done.wait(), 2)
        assert not panel.visible and panel._ticker is None
    run(run_it())


def test_volume_down_while_muted_respects_mute_and_updates_mixer(tmp_path):
    from retroboot.network import AudioMixer
    make_backlight(tmp_path, cur=600, mx=1200)
    calls = []

    async def amixer_run(argv, timeout=None):
        calls.append(argv)
        if "scontrols" in argv:
            return 0, "Simple mixer control 'Master',0\n", ""
        if argv[-2:] == ["sget", "Master"]:
            return 0, "Simple mixer control 'Master',0\n  Capabilities: pvolume pswitch\n", ""
        return 0, "", ""

    app = FakeApp(tmp_path, [])
    app.mixer = AudioMixer(runner=amixer_run)
    lp = Laptop(app)

    async def go():
        await app.mixer.probe("PCH", detect_switch=True)
        await lp.set_mute(True)
        assert lp.muted is True
        calls.clear()
        res = await lp.on_host_key("volume_down")
        assert res["muted"] is True
        assert lp.muted is True
        assert app.settings.applied["audio.volume"] == 64
        # ALSA must NOT be sent "unmute" when volume_down is pressed while muted (#40).
        assert app.volume_pushes == 0
        assert calls == [["amixer", "-c", "PCH", "-q", "sset", "Master", "37%"]]
        # Pressing volume_up unmutes cleanly at the new level.
        res_up = await lp.on_host_key("volume_up")
        assert res_up["muted"] is False and lp.muted is False
        assert app.settings.applied["audio.volume"] == 70
        assert app.volume_pushes == 1

    run(go())


def test_volume_down_while_muted_switchless_keeps_zero_percent(tmp_path):
    from retroboot.network import AudioMixer
    make_backlight(tmp_path, cur=600, mx=1200)
    calls = []

    async def amixer_run(argv, timeout=None):
        calls.append(argv)
        if "scontrols" in argv:
            return 0, "Simple mixer control 'PCM',0\n", ""
        if argv[-2:] == ["sget", "PCM"]:
            return 0, "Simple mixer control 'PCM',0\n  Capabilities: pvolume\n", ""
        return 0, "", ""

    app = FakeApp(tmp_path, [])
    app.mixer = AudioMixer(runner=amixer_run)
    lp = Laptop(app)

    async def go():
        await app.mixer.probe("USBDAC", detect_switch=True)
        await lp.set_mute(True)
        assert calls[-1] == ["amixer", "-c", "USBDAC", "-q", "sset", "PCM", "0%"]
        calls.clear()
        await lp.on_host_key("volume_down")
        assert lp.muted is True
        assert app.mixer.last_percent == 64
        assert calls == []     # never raise ALSA above 0% while switchless mute is active
        await lp.set_mute(False)
        assert calls[-1] == ["amixer", "-c", "USBDAC", "-q", "sset", "PCM", "37%"]

    run(go())


def test_volume_hotkey_reprobes_unprobed_mixer_and_bypasses_capability_gate(tmp_path):
    from retroboot import schema
    from retroboot.network import AudioMixer
    calls = []

    async def amixer_run(argv, timeout=None):
        calls.append(argv)
        if "scontrols" in argv:
            return 0, "Simple mixer control 'Master',0\n", ""
        if argv[-2:] == ["sget", "Master"]:
            return 0, ("Simple mixer control 'Master',0\n"
                       "  Capabilities: pvolume pswitch\n"
                       "  Mono: Playback 64 [100%] [0.00dB] [on]\n"), ""
        return 0, "", ""

    app = FakeApp(tmp_path, [])
    app.mixer = AudioMixer(runner=amixer_run)
    app.validation_context = lambda: schema.ValidationContext(check_capabilities=True)
    seen_ctxs = []
    app.settings.set_desired_and_applied = lambda v, ctx=None: (seen_ctxs.append(ctx), app.settings.applied.update(v))

    async def real_push():
        app.volume_pushes += 1
        return await app.mixer.set_volume(app.settings.applied["audio.volume"])
    app.apply_engine.push_volume = real_push
    assert app.mixer.available is False
    lp = Laptop(app)

    async def go():
        res = await lp.on_host_key("volume_down")
        assert abs(res["level"] - 0.64) < 1e-6
        assert app.settings.applied["audio.volume"] == 64
        assert app.mixer.available is True
        assert app.mixer.mapped is True
        assert app.volume_pushes == 1
        assert seen_ctxs and seen_ctxs[-1].check_capabilities is False
        assert ["amixer", "-M", "-q", "sset", "Master", "64%", "unmute"] in calls

    run(go())


def test_push_volume_clears_laptop_muted(tmp_path):
    from retroboot.apply import ApplyEngine
    from retroboot.network import AudioMixer
    app = FakeApp(tmp_path, [])

    async def amixer_run(argv, timeout=None):
        if "scontrols" in argv:
            return 0, "Simple mixer control 'RetroBoot',0\n", ""
        if argv[-2:] == ["sget", "RetroBoot"]:
            return 0, "Simple mixer control 'RetroBoot',0\n  Capabilities: volume\n  Front Left: 255 [100%] [0.00dB]\n", ""
        return 0, "", ""

    app.mixer = AudioMixer(runner=amixer_run)
    app.laptop = SimpleNamespace(muted=True)
    app.settings.applied["audio.volume"] = 70
    engine = ApplyEngine(app)

    async def go():
        await app.mixer.probe("Generic_1", "RetroBoot", detect_switch=True)
        assert await engine.push_volume() is True
        assert app.laptop.muted is False

    run(go())
