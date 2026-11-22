"""A battery gauge that lies (the N5105, image/reports/hardware-n5105-2026-10-11.md
§8): a constant 3.8 W and 7.6 V, 38 Wh "full", about 56 min from 99 % to dead
at a 4 % reading, while power_now promised 222 min.  The time to empty follows
the observed slope (and Mac OS is told it), the critical shutdown starts early
enough, and a failed clean shutdown is forced before the battery cuts out."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import helpers  # noqa: F401  (sys.path)
from retroboot import battery as bat
from retroboot.jobs import FAILED, SUCCEEDED
from retroboot.laptop import DrainEstimate, Laptop
from test_laptop import FakeApp, _w, make_battery


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class N5105:
    """The EC's battery: percent falls 2.2 %/min for 17 min from 99 %, then about
    1.7 %/min to 19 %, then 1.1 %/min; power and voltage never change."""

    def __init__(self, root: Path, start=99.0):
        self.dir, _ = make_battery(root, pct=int(start), ac=False)
        self.pct = start
        self.minutes = 0.0
        self.write()

    def rate(self) -> float:
        if self.minutes < 17:
            return 2.2
        return 1.68 if self.pct > 19 else 1.1

    def advance(self, minutes: float) -> None:
        step = 0.1
        while minutes > 1e-9:
            d = min(step, minutes)
            self.pct = max(0.0, self.pct - self.rate() * d)
            self.minutes += d
            minutes -= d
        self.write()

    def write(self) -> None:
        pct = int(self.pct)      # the EC reports whole percent, rounded down
        _w(self.dir / "capacity", pct)
        _w(self.dir / "energy_full", 38_000_000)
        _w(self.dir / "energy_now", int(38_000_000 * pct / 100))
        _w(self.dir / "power_now", 3_800_000)
        _w(self.dir / "voltage_now", 7_600_000)
        _w(self.dir / "status", "Discharging")


def test_drain_estimate_needs_a_span_and_a_drop():
    d = DrainEstimate()
    d.add(0, 50)
    d.add(60, 49)
    assert d.rate() is None                       # one minute, one percent: not yet
    for k in range(2, 6):
        d.add(60 * k, 50 - 1.5 * k)
    assert d.rate() == pytest.approx(1.5, rel=0.05)
    assert d.minutes_to(42.5, 2) == pytest.approx(27, rel=0.05)
    # the gauge's 222 min disagree: the slope's figure wins
    tte, source = d.effective({"percent": 37, "time_to_empty_min": 222})
    assert source == "slope" and 22 <= tte <= 25
    # a gauge that agrees keeps its own figure
    assert d.effective({"percent": 37, "time_to_empty_min": 26}) == (26, "gauge")
    d.reset()
    assert d.rate() is None and d.effective({"percent": 37, "time_to_empty_min": 222}) == (222, "gauge")


def _laptop(tmp_path, monkeypatch, outcomes=(SUCCEEDED,), passthrough=True):
    clock = Clock()
    gauge = N5105(tmp_path)
    app = FakeApp(tmp_path, list(outcomes))
    app.battery = type("B", (), {"passthrough_active": staticmethod(lambda: passthrough)})()
    lp = Laptop(app)
    lp._now = clock
    return app, lp, gauge, clock


def test_time_to_empty_follows_the_slope_and_reaches_mac_os(tmp_path, monkeypatch):
    app, lp, gauge, clock = _laptop(tmp_path, monkeypatch)

    async def go():
        lp.power = lp.supplies.read()
        for _ in range(40):                       # 20 min, a reading every 30 s
            gauge.advance(0.5)
            clock.t += 30
            await lp.poll()
    run(go())
    b = lp.public()["battery"]
    raw = lp.supplies.read()["battery"]
    assert raw["time_to_empty_min"] > 150         # the EC's 3.8 W fiction
    assert b["time_to_empty_source"] == "slope"
    assert b["gauge_time_to_empty_min"] == raw["time_to_empty_min"]
    assert 1.6 <= b["drain_percent_per_min"] <= 2.3
    assert b["time_to_empty_min"] == pytest.approx(b["percent"] / b["drain_percent_per_min"], abs=3)
    # Mac OS gets the estimate through the power bridge, not the gauge's figure
    msg = bat.guest_battery(lp.estimated(lp.supplies.read()))
    assert msg["time_to_empty_min"] == b["time_to_empty_min"]


def test_n5105_discharge_shuts_down_cleanly_before_the_4_percent_death(tmp_path, monkeypatch):
    app, lp, gauge, clock = _laptop(tmp_path, monkeypatch)
    lp.poll_s = 30.0
    trace = []

    async def go():
        lp.power = lp.supplies.read()
        while gauge.pct > 4 and not lp._critical_task:
            interval = lp._interval()
            gauge.advance(interval / 60)
            clock.t += interval
            await lp.poll()
            trace.append(int(gauge.pct))
        if lp._critical_task:
            await asyncio.wait_for(lp._critical_task, 5)
    run(go())
    assert app.jobs_started == [("poweroff", False, "battery")]
    crit = [e for e in app.events if e[0] == "battery_critical"][0][1]
    assert crit["percent"] >= 7                    # with minutes to spare before the 4 % reading


def test_a_fast_drain_starts_the_critical_shutdown_above_the_threshold(tmp_path, monkeypatch):
    app, lp, gauge, clock = _laptop(tmp_path, monkeypatch)
    gauge.rate = lambda: 3.5                       # a heavier load than the N5105's idle Finder
    gauge.pct = 30.0
    gauge.write()

    async def go():
        lp.power = lp.supplies.read()
        while gauge.pct > 2 and not lp._critical_task:
            interval = lp._interval()
            gauge.advance(interval / 60)
            clock.t += interval
            await lp.poll()
        await asyncio.wait_for(lp._critical_task, 5)
    run(go())
    crit = [e for e in app.events if e[0] == "battery_critical"][0][1]
    assert crit["percent"] > 8 and "minutes_to_floor" in crit and crit["minutes_to_floor"] < 3


def test_failed_clean_shutdown_is_forced_by_the_slope_before_a_dying_gauge(tmp_path, monkeypatch):
    app, lp, gauge, clock = _laptop(tmp_path, monkeypatch, outcomes=(FAILED,) * 60)
    gauge.rate = lambda: 1.1
    gauge.pct = 12.0
    gauge.write()
    real_sleep = asyncio.sleep

    async def fast_sleep(s):
        if s == lp.RETRY_S:
            gauge.advance(s / 60)
            clock.t += s
        await real_sleep(0)
    monkeypatch.setattr("retroboot.laptop.asyncio.sleep", fast_sleep)

    async def go():
        lp.power = lp.supplies.read()
        while not lp._critical_task:
            gauge.advance(0.5)
            clock.t += 30
            await lp.poll()
        await asyncio.wait_for(lp._critical_task, 5)
    run(go())
    assert app.jobs_started[-1] == ("poweroff", True, "battery-floor")
    forced = [e for e in app.events if e[0] == "battery_force_poweroff"][0][1]
    assert forced["why"] in ("slope", "time") and forced["percent"] > 2   # above the 2 % floor
    assert gauge.pct > 3                          # before this EC's 4 % "empty"


def test_time_backstop_forces_when_the_gauge_stops_moving(tmp_path, monkeypatch):
    app, lp, gauge, clock = _laptop(tmp_path, monkeypatch, outcomes=(FAILED,) * 40)
    lp.force_after_s = 120.0
    real_sleep = asyncio.sleep

    async def fast_sleep(s):
        if s == lp.RETRY_S:
            clock.t += s                          # the reading stays at 8 %
        await real_sleep(0)
    monkeypatch.setattr("retroboot.laptop.asyncio.sleep", fast_sleep)
    _w(gauge.dir / "capacity", 8)

    async def go():
        lp.power = lp.supplies.read()
        for _ in range(2):
            clock.t += 30
            await lp.poll()
        await asyncio.wait_for(lp._critical_task, 5)
    run(go())
    forced = [e for e in app.events if e[0] == "battery_force_poweroff"][0][1]
    assert forced == {"percent": 8, "why": "time"}
