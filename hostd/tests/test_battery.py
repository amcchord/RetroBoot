"""Battery passthrough (retroboot/battery.py): the bridge message from
/sys/class/power_supply, the machine choice, build_argv, the power bridge
(hello, battery updates, sleep/wake) and the single low-battery warning path."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import helpers  # noqa: F401  (sys.path)
from retroboot import battery as bat
from retroboot import schema
from retroboot.config import Config
from retroboot.jobs import SUCCEEDED
from retroboot.laptop import Laptop, PowerSupplies
from retroboot.machines import LaunchOptions, build_argv
from test_laptop import FakeApp, _w, make_battery


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# -- pure helpers ---------------------------------------------------------------------
def test_guest_battery_discharging(tmp_path):
    make_battery(tmp_path, pct=40, status="Discharging", ac=False)
    msg = bat.guest_battery(PowerSupplies(tmp_path).read())
    assert msg == {"type": "battery", "present": True, "ac": False, "charging": False, "percent": 40,
                   "time_to_empty_min": 120, "voltage_mv": 7600}


def test_guest_battery_charging_and_full(tmp_path):
    make_battery(tmp_path, pct=55, status="Charging", ac=True)
    msg = bat.guest_battery(PowerSupplies(tmp_path).read())
    assert msg["ac"] is True and msg["charging"] is True and msg["percent"] == 55
    assert "time_to_full_min" in msg and "time_to_empty_min" not in msg
    make_battery(tmp_path / "f", pct=100, status="Full", ac=True)
    msg = bat.guest_battery(PowerSupplies(tmp_path / "f").read())
    assert msg["ac"] is True and msg["charging"] is False


def test_guest_battery_without_mains_supply_infers_adapter():
    power = {"ac": None, "batteries": 1, "battery": {"percent": 80.4, "status": "Not charging"}}
    assert bat.guest_battery(power)["ac"] is True
    power["battery"]["status"] = "Discharging"
    assert bat.guest_battery(power)["ac"] is False and bat.guest_battery(power)["percent"] == 80


def test_guest_battery_absent():
    assert bat.guest_battery({"ac": None, "battery": None, "batteries": 0}) == \
        {"type": "battery", "present": False, "ac": True}


def test_use_pmu_policy():
    on, off = {bat.SETTING: True}, {bat.SETTING: False}
    assert bat.use_pmu(on, capable=True, has_battery=True)
    assert not bat.use_pmu(off, capable=True, has_battery=True)          # the setting turns it off
    assert not bat.use_pmu(on, capable=True, has_battery=False)          # desktops and VMs keep Cuda
    assert not bat.use_pmu(on, capable=False, has_battery=True)          # package without the feature
    assert bat.use_pmu(off, capable=True, has_battery=False, controller="pmu")   # lab override
    assert not bat.use_pmu(on, capable=True, has_battery=True, controller="cuda")
    assert not bat.use_pmu(on, capable=False, has_battery=False, controller="pmu")


def test_package_capable(tmp_path):
    assert not bat.package_capable(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps({"features": ["airport-bridge-1"]}))
    assert not bat.package_capable(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps({"features": ["airport-bridge-1", bat.FEATURE]}))
    assert bat.package_capable(tmp_path)


def test_setting_schema():
    s = schema.BY_KEY[bat.SETTING]
    assert s.default is True and s.capability == "battery" and s.apply == schema.GUEST_RESTART


def test_machine_args_and_build_argv():
    assert bat.machine_args(None) == ("cuda", [])
    via, extra = bat.machine_args(Path("/run/retroboot/power.sock"))
    assert via == "pmu-adb"
    assert extra == ["-global", "via-pmu.battery=on",
                     "-chardev", "socket,id=power0,path=/run/retroboot/power.sock,reconnect-ms=1000",
                     "-global", "via-pmu.chardev=power0"]
    from retroboot.config import Paths
    from retroboot.machines import MachineRegistry
    td, root = helpers.scratch()
    try:
        helpers.make_package(root)
        helpers.make_machine(root)
        paths = Paths(root)
        paths.ensure()
        m = MachineRegistry(paths).get("mac-os-9")
        settings = schema.defaults()
        display = {"mode": [1920, 1080, 60], "suggested_scale": 2}
        cuda = " ".join(build_argv(m, settings, paths, display))
        assert "-M mac99,via=cuda,audiodev=snd0" in cuda and "via-pmu" not in cuda
        sock = paths.run / bat.SOCKET_NAME
        pmu = " ".join(build_argv(m, settings, paths, display, LaunchOptions(power_socket=sock)))
        assert "-M mac99,via=pmu-adb,audiodev=snd0" in pmu
        assert f"-chardev socket,id=power0,path={sock},reconnect-ms=1000 -global via-pmu.chardev=power0" in pmu
    finally:
        td.cleanup()


# -- the bridge -----------------------------------------------------------------------
class BridgeApp(FakeApp):
    def __init__(self, root: Path, sock_dir: Path):
        super().__init__(root, [])
        self.cfg = Config(root=str(root), battery_poll_s=0.01)
        self.paths = SimpleNamespace(run=sock_dir, qemu_package=root / "pkg")
        self.laptop = Laptop(self)
        self.laptop.power = self.laptop.supplies.read()
        self.laptop.lid_open = True
        self.supervisor = SimpleNamespace(active=True, session="s1")
        self.settings = SimpleNamespace(applied={bat.SETTING: True, "audio.volume": 70})


class FakeQemu:
    def __init__(self, path: Path):
        self.path = path
        self.lines: list[dict] = []

    async def connect(self, hello: dict):
        self.r, self.w = await asyncio.open_unix_connection(str(self.path))
        self.w.write(json.dumps(hello).encode() + b"\n")
        await self.w.drain()

    async def read(self, n: int, timeout: float = 5.0) -> list[dict]:
        while len(self.lines) < n:
            line = await asyncio.wait_for(self.r.readline(), timeout)
            self.lines.append(json.loads(line))
        return self.lines

    async def send(self, obj: dict):
        self.w.write(json.dumps(obj).encode() + b"\n")
        await self.w.drain()


def test_bridge_battery_sleep_and_wake(tmp_path):
    import tempfile
    make_battery(tmp_path, pct=57, status="Discharging", ac=False)
    sock_dir = Path(tempfile.mkdtemp(prefix="rbbat"))       # short AF_UNIX path
    app = BridgeApp(tmp_path, sock_dir)
    b = bat.Battery(app)
    b.POLL_S = 0.05
    b.WAKE_SETTLE_S = 0.0

    async def go():
        await b.start()
        q = FakeQemu(sock_dir / bat.SOCKET_NAME)
        await q.connect({"type": "hello", "version": 1, "battery": True})
        lines = await q.read(2)
        assert lines[0] == {"type": "hello", "version": 1}
        assert lines[1]["type"] == "battery" and lines[1]["percent"] == 57 and lines[1]["ac"] is False
        # a change of the host battery reaches QEMU within a poll
        _w(tmp_path / "sys/class/power_supply/BAT0/capacity", 41)
        lines = await q.read(3)
        assert lines[2]["percent"] == 41
        # Mac OS sleeps: displays off, no frozen-guest check
        await q.send({"type": "sleep"})
        for _ in range(100):
            if b.asleep:
                break
            await asyncio.sleep(0.01)
        assert b.asleep and ("display_power", {"output": "all", "on": False}) in app.requests

        async def qemu_wakes():
            msgs = await q.read(4)
            assert msgs[3] == {"type": "wake"}
            await q.send({"type": "wake", "reason": "host"})
        waker = asyncio.get_running_loop().create_task(qemu_wakes())
        await b.ensure_awake("power-dialog")
        await waker
        assert not b.asleep
        assert ("display_power", {"output": "internal", "on": True}) in app.requests
        assert ("guest_wake", {"reason": "host"}) in app.events
        # a reconnecting QEMU that is still asleep says so in its hello
        q2 = FakeQemu(sock_dir / bat.SOCKET_NAME)
        await q2.connect({"type": "hello", "version": 1, "battery": True, "sleeping": True})
        await q2.read(2)
        for _ in range(100):
            if b.asleep:
                break
            await asyncio.sleep(0.01)
        assert b.asleep
        await b.stop()
    run(go())


def test_launch_socket_follows_host_battery(tmp_path):
    import tempfile
    sock_dir = Path(tempfile.mkdtemp(prefix="rbbat"))
    make_battery(tmp_path, pct=80, status="Discharging", ac=False)
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "manifest.json").write_text(json.dumps({"features": [bat.FEATURE]}))
    app = BridgeApp(tmp_path, sock_dir)
    b = bat.Battery(app)
    assert b.capable()
    assert b.launch_socket({bat.SETTING: True}) == sock_dir / bat.SOCKET_NAME
    assert b.launch_socket({bat.SETTING: False}) is None
    assert b.launch_socket({bat.SETTING: True}, boot_cd=True) is None
    app.laptop.power = {"ac": None, "battery": None, "batteries": 0}     # a desktop or VM
    assert b.launch_socket({bat.SETTING: True}) is None and not b.capable()
    b.launched("s1", True)
    assert b.passthrough_active()
    b.launched("s2", False)
    assert not b.passthrough_active()


# -- exactly one low-battery warning path ------------------------------------------------
def _warn_app(tmp_path, passthrough: bool, readings=(20, 10, 9, 4, 3)):
    b, _ = make_battery(tmp_path, pct=20, ac=False)
    app = FakeApp(tmp_path, [SUCCEEDED])
    app.battery = SimpleNamespace(passthrough_active=lambda: passthrough)
    lp = Laptop(app)

    async def go():
        lp.power = lp.supplies.read()
        for pct in readings:
            _w(b / "capacity", pct)
            await lp.poll()
            if lp._critical_task:
                await asyncio.wait_for(lp._critical_task, 5)
    run(go())
    return app, lp


def test_retroboot_warns_without_passthrough(tmp_path):
    app, lp = _warn_app(tmp_path, passthrough=False)
    assert [m for m in app.messages if m[1].startswith("Your battery")]
    assert app.jobs_started == [("poweroff", False, "battery")]


def test_mac_os_warns_with_passthrough(tmp_path):
    app, lp = _warn_app(tmp_path, passthrough=True)
    assert not [m for m in app.messages if m[1].startswith("Your battery")]     # Mac OS's own alert only
    assert ("battery_low", {"percent": 10, "mac_os_warns": True}) in app.events
    assert app.jobs_started == [("poweroff", False, "battery")]                # critical stays hostd's


def test_passthrough_shuts_down_before_mac_os_force_sleeps(tmp_path):
    # Mac OS 9.2.1 sleeps by itself at 5 % (no reserve left); with passthrough
    # hostd's clean shutdown starts at battery_passthrough_critical_percent.
    app, lp = _warn_app(tmp_path, passthrough=True, readings=(20, 9, 8, 7))
    assert app.jobs_started == [("poweroff", False, "battery")]
    assert ("battery_critical", {"percent": 7}) in app.events
    app, lp = _warn_app(tmp_path / "plain", passthrough=False, readings=(20, 9, 8, 7))
    assert app.jobs_started == []
