"""Startup progress (retroboot/startup.py, ARCHITECTURE 4.4.1): the milestones
hostd sends to the presenter's startup indicator and the timing model it
learns from previous startups."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from retroboot import startup as sp


class FakePresenter:
    def __init__(self):
        self.requests = []

    async def try_request(self, cmd, **kw):
        self.requests.append((cmd, kw))
        return {"ok": True}


class FakeLog:
    def __init__(self):
        self.events = []

    def event(self, name, **kw):
        self.events.append((name, kw))


class FakeApply:
    def __init__(self):
        self.calls = []

    async def update_boot_display(self, why=""):
        self.calls.append(why)
        return True


def make_app(root: Path, enabled=True):
    paths = SimpleNamespace(state=root / "state", run=root / "run",
                            platinum_icon=root / "assets/platinum/icons/overview-32.rbi",
                            startup_icon=root / "state/startup-icon.rbi")
    settings = SimpleNamespace(applied={"startup.progress_indicator": enabled, "machine.active": "os9"})
    app = SimpleNamespace(paths=paths, settings=settings, presenter=FakePresenter(), lifecycle=FakeLog(),
                          supervisor=SimpleNamespace(active=False, booted=False), mode="normal", loop=None,
                          apply_engine=FakeApply())
    return app


def progress(app):
    return [kw for cmd, kw in app.presenter.requests if cmd == "startup_progress"]


class StartupProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_(self, coro):
        return asyncio.run(coro)

    def test_ema(self):
        self.assertEqual(sp.ema(None, 7000), 7000)
        self.assertAlmostEqual(sp.ema(7000, 8000), 7350)
        # a 60 s hang moves a 7 s model only a little
        self.assertAlmostEqual(sp.ema(7000, 60000), 7000 + 0.1 * 53000)

    def test_cold_boot_milestones_and_learning(self):
        app = make_app(self.root)
        s = sp.StartupProgress(app)
        s.cold = True   # hostd early in a boot (boot_ms() of the test machine says otherwise)
        # no model: presenter connect -> prep + default launch
        self.run_(s.on_presenter_connect())
        self.assertEqual([c for c, _ in app.presenter.requests][:1], ["startup_progress"])
        first = progress(app)[0]
        self.assertEqual(first["phase"], "host")
        self.assertFalse(first["reset"])
        self.assertEqual(first["remaining_ms"], round(sp.DEFAULT_PREP_MS + sp.DEFAULT_LAUNCH_MS))
        # the cold boot's own start continues the bar (no reset)
        self.run_(s.on_start("boot", 1, "os9"))
        self.assertFalse(progress(app)[-1]["reset"])
        self.assertEqual(progress(app)[-1]["phase"], "prepare")
        self.run_(s.on_launched())
        self.assertEqual(progress(app)[-1]["phase"], "launch")
        s.attempt["t_launch"] -= 7.0      # QEMU ran 7 s before Mac OS's startup screen
        s.on_revealed({"event": "guest_revealed", "boot_ms": 11500})
        self.assertAlmostEqual(s.model["launch_ms"]["os9"], 7000, delta=60)
        self.assertEqual(s.model["cold_ms"], 11500)
        saved = json.loads((self.root / "state/startup-timing.json").read_text())
        self.assertEqual(saved["cold_ms"], 11500)
        self.assertFalse(s.cold)
        # a guest Restart: a new startup, reset, timed by the learned launch
        app.presenter.requests.clear()
        self.run_(s.on_start("guest-reset", 1, "os9"))
        self.assertTrue(progress(app)[-1]["reset"])
        self.run_(s.on_launched())
        self.assertAlmostEqual(progress(app)[-1]["remaining_ms"], 7000, delta=60)
        s.attempt["t_launch"] -= 8.0
        s.on_revealed({"event": "guest_revealed", "boot_ms": 90000})
        self.assertEqual(s.model["cold_ms"], 11500)     # relaunches never touch the cold model
        self.assertAlmostEqual(s.model["launch_ms"]["os9"], 7000 + 0.35 * 1000, delta=80)
        # the next run loads the model
        s2 = sp.StartupProgress(app)
        self.assertEqual(s2.model["cold_ms"], 11500)
        self.assertIn("os9", s2.model["launch_ms"])

    def test_cold_model_only_from_a_boot_and_no_outliers(self):
        app = make_app(self.root)
        s = sp.StartupProgress(app)
        s.cold = True                      # as if hostd started early in this boot
        self.run_(s.on_start("boot", 1, "os9"))
        self.run_(s.on_launched())
        s.attempt["t_launch"] -= 7.0
        s.on_revealed({"event": "guest_revealed", "boot_ms": 15000})
        self.assertEqual(s.model["cold_ms"], 15000)
        self.assertTrue((self.root / "run/startup-cold-done").exists())
        # a hostd restarted later in the same boot is never "cold"
        s2 = sp.StartupProgress(app)
        self.assertFalse(s2.cold)
        # established model: a far-off sample (a hang, a lab restart) is not learned
        s.model["samples"] = 5
        s.cold = True
        self.run_(s.on_start("boot", 1, "os9"))
        self.run_(s.on_launched())
        s.attempt["t_launch"] -= 70.0
        s.on_revealed({"event": "guest_revealed", "boot_ms": 500000})
        self.assertEqual(s.model["cold_ms"], 15000)
        self.assertAlmostEqual(s.model["launch_ms"]["os9"], 7000, delta=60)

    def test_presenter_connect_uses_learned_cold_time(self):
        app = make_app(self.root)
        (self.root / "state").mkdir(parents=True)
        (self.root / "state/startup-timing.json").write_text(json.dumps(
            {"version": 1, "cold_ms": 41000, "launch_ms": {"os9": 20000}, "prep_ms": 400, "samples": 3}))
        s = sp.StartupProgress(app)
        s.cold = True
        self.assertEqual(s.model["cold_ms"], 41000)
        s.model["cold_ms"] = sp.boot_ms() + 30000   # this host's clock: 30 s before the learned cut-over
        self.run_(s.on_presenter_connect())
        self.assertAlmostEqual(progress(app)[-1]["remaining_ms"], 30000, delta=500)
        # already past the learned time: at least a launch
        s.model["cold_ms"] = 1.0
        app.presenter.requests.clear()
        self.run_(s.on_presenter_connect())
        self.assertEqual(progress(app)[-1]["remaining_ms"], 20400)
        # a presenter reconnect while Mac OS runs sends no timing
        app.supervisor.booted = True
        app.presenter.requests.clear()
        self.run_(s.on_presenter_connect())
        self.assertEqual(progress(app), [])

    def test_retry_and_pause_are_not_learned(self):
        app = make_app(self.root)
        s = sp.StartupProgress(app)
        s.cold = True
        self.run_(s.on_start("boot", 1, "os9"))
        self.run_(s.on_launched())
        s.on_failed()                      # no content in time
        self.run_(s.on_start("retry", 2, "os9"))
        self.assertTrue(progress(app)[-1]["reset"])   # the bar starts again
        self.assertEqual(progress(app)[-1]["phase"], "retry")
        self.run_(s.on_launched())
        s.attempt["t_launch"] -= 7.0
        s.on_revealed({"event": "guest_revealed", "boot_ms": 80000})
        self.assertIsNone(s.model["cold_ms"])         # a retried boot is not a cold-boot sample
        self.assertIn("os9", s.model["launch_ms"])
        # paused at the startup options: resumed with what was left, not learned
        self.run_(s.on_start("user", 1, "os9"))
        self.run_(s.on_launched())
        s.attempt["t_launch"] -= 2.0
        s.on_paused()
        s.attempt["t_pause"] = s.attempt["t_launch"] + 2.0
        self.run_(s.on_resumed())
        self.assertAlmostEqual(progress(app)[-1]["remaining_ms"], s.model["launch_ms"]["os9"] - 2000, delta=50)
        before = dict(s.model["launch_ms"])
        s.attempt["t_launch"] -= 60.0
        s.on_revealed({"event": "guest_revealed"})
        self.assertEqual(s.model["launch_ms"], before)

    def test_disabled_sends_no_progress(self):
        app = make_app(self.root, enabled=False)
        s = sp.StartupProgress(app)
        s.cold = True
        self.run_(s.on_presenter_connect())
        self.run_(s.on_start("boot", 1, "os9"))
        self.assertEqual(progress(app), [])
        cap = str(self.root / "state/startup-icon.rbi")
        self.assertEqual(app.presenter.requests[0], ("startup_config", {"enabled": False, "style": "classic", "art": None, "capture": cap,
                                                                        "happy_mac": "prompt"}))
        self.run_(s.push_config())
        self.assertEqual(app.presenter.requests[-1], ("startup_config", {"enabled": False, "style": "classic", "art": None, "capture": cap,
                                                                         "happy_mac": "prompt"}))

    def test_happy_mac_follows_the_card(self):
        app = make_app(self.root)
        s = sp.StartupProgress(app)
        self.assertEqual(s.config()["happy_mac"], "prompt")         # no graphics: the standard card
        app.graphics = SimpleNamespace(startup_guest_size=lambda: [640, 480])
        self.assertEqual(s.config()["happy_mac"], "late")           # the ATI Radeon draws it late
        app.graphics = SimpleNamespace(startup_guest_size=lambda: None)
        self.assertEqual(s.config()["happy_mac"], "prompt")

        def broken():
            raise RuntimeError("probe")
        app.graphics = SimpleNamespace(startup_guest_size=broken)
        self.assertEqual(s.config()["happy_mac"], "prompt")

    def test_art_prefers_the_captured_icon(self):
        app = make_app(self.root)
        s = sp.StartupProgress(app)
        self.assertIsNone(s.config()["art"])
        self.assertEqual(s.config()["capture"], str(self.root / "state/startup-icon.rbi"))
        icon = self.root / "assets/platinum/icons/overview-32.rbi"
        icon.parent.mkdir(parents=True)
        icon.write_bytes(b"RBI1")
        self.assertEqual(s.config()["art"], str(icon))          # the bundle seeds it
        cap = self.root / "state/startup-icon.rbi"
        cap.parent.mkdir(parents=True)
        cap.write_bytes(b"RBI1")
        self.assertEqual(s.config()["art"], str(cap))           # Mac OS's own copy wins
        # a new capture is pushed to the presenter
        self.run_(s.on_icon({"event": "startup_icon", "valid": True, "saved": True, "written": True}))
        self.assertEqual(app.presenter.requests[-1][0], "startup_config")
        self.assertIn(("startup_icon", {"valid": True, "saved": True, "written": True}), app.lifecycle.events)
        n = len(app.presenter.requests)
        self.run_(s.on_icon({"event": "startup_icon", "valid": True, "saved": True, "written": False}))
        self.assertEqual(len(app.presenter.requests), n)

    def test_corrupt_model_falls_back(self):
        app = make_app(self.root)
        (self.root / "state").mkdir(parents=True)
        (self.root / "state/startup-timing.json").write_text("{not json")
        s = sp.StartupProgress(app)
        self.assertIsNone(s.model["cold_ms"])
        (self.root / "state/startup-timing.json").write_text(json.dumps(
            {"version": 1, "cold_ms": -5, "launch_ms": {"a": "x", "b": 5000}}))
        s = sp.StartupProgress(app)
        self.assertIsNone(s.model["cold_ms"])
        self.assertEqual(s.model["launch_ms"], {"b": 5000.0})


if __name__ == "__main__":
    unittest.main()
