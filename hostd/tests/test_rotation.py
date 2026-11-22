"""Rotation and touch in hostd (ARCHITECTURE 4.5, 4.8): portrait guest sizes,
the settings, the tilt sensor (iio-sensor-proxy's orientation rule, the IIO
sysfs reader, a fake iio-sensor-proxy on a private bus), the auto rotation
policy and what reaches the presenter."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import helpers  # noqa: F401  (sys.path)
from fakes import fake_iio_accel
from retroboot import dbus_lite, schema
from retroboot import display as D
from retroboot import orientation as O
from retroboot.apply import ApplyEngine
from retroboot.config import Config


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# -- portrait guest sizes (presenter scale.c, rb-bootdisplay) -------------------------
def test_portrait_rules_match_the_presenter():
    assert D.suggested_scale(1080, 1920) == D.suggested_scale(1920, 1080) == 2
    assert D.suggested_scale(720, 1280) == 1
    assert D.auto_guest_size(720, 1280, 1) == (720, 1280)
    assert D.auto_guest_size(1080, 1920, 2) == (536, 960)
    assert D.auto_guest_size(480, 800, 1) == (512, 800)
    assert D.pixel_scale_valid(1080, 1920, 2) and not D.pixel_scale_valid(1080, 1920, 3)
    assert not D.pixel_scale_valid(960, 1280, 2)
    assert D.effective_scale(1080, 1920, "auto") == 2
    assert D.oriented_size(768, 1366, 1024, 768) == (768, 1024)
    assert D.oriented_size(1080, 1920, 640, 480) == (640, 480)
    assert D.oriented_size(1080, 1920, 1152, 870) == (1152, 870)
    assert D.oriented_size(1920, 1080, 1024, 768) == (1024, 768)
    portrait = {"mode": [720, 1280, 60]}
    assert D.guest_geometry(portrait, "auto") == (720, 1280)
    assert D.guest_geometry(portrait, "1024x768") == (768, 1024)
    assert D.guest_geometry({"mode": [1280, 720, 60]}, "1024x768") == (1024, 768)
    assert "512 × 640" in D.unavailable_reason(960, 1280, 2)


def test_portrait_launch_and_notes(tmp_path):
    from retroboot.config import Paths
    from retroboot.machines import LaunchOptions, Machine, build_argv
    pkg = tmp_path / "pkg"
    (pkg / "bin").mkdir(parents=True)
    (pkg / "share/qemu").mkdir(parents=True)
    for f in ("bin/qemu-system-ppc", "share/qemu/openbios-ppc", "share/qemu/qemu_vga.ndrv", "share/qemu/ndrvloader"):
        (pkg / f).write_bytes(b"x")
    paths = Paths(root=tmp_path, qemu_package_override=pkg)
    paths.ensure()
    mdir = tmp_path / "m"
    mdir.mkdir()
    (mdir / "disk.img").write_bytes(b"\0" * 512)
    m = Machine("lab", mdir, {"schema_version": 1, "id": "lab", "disk": "disk.img", "name": "Mac OS 9"})
    settings = schema.defaults()
    argv = build_argv(m, settings, paths, {"mode": [1080, 1920, 60]}, LaunchOptions(display_backend="retroboot"))
    assert argv[argv.index("-g") + 1].startswith("536x960x")
    # a fixed size is drawn on its side, and the notes say so
    assert schema.fixed_size_note({"display.guest_size": "1280x1024", "display.pixel_scale": "auto"},
                                  (768, 1366)) == "1024 × 1280 is larger than this display, so it is reduced smoothly to fit."
    choices = schema.pixel_scale_choices(schema.ValidationContext(display_mode=(1080, 1920)))
    by = {c["value"]: c for c in choices}
    assert not by["2"].get("disabled") and by["3"].get("disabled")


# -- settings ---------------------------------------------------------------------------
def test_settings():
    r = schema.BY_KEY["display.rotation"]
    assert r.default == "auto" and r.apply == schema.LIVE and r.capability is None
    assert r.validate("90") == "90"
    with pytest.raises(ValueError):
        r.validate("45")
    lock = schema.BY_KEY["display.rotation_lock"]
    assert lock.default is False and lock.capability == "accelerometer"
    tg = schema.BY_KEY["input.touch_gestures"]
    assert tg.default is True and tg.capability == "touch" and tg.group == "input"
    ctx = schema.ValidationContext(capabilities={"touch": False, "accelerometer": True})
    d = {s["key"]: s for s in schema.describe(ctx)}
    assert d["input.touch_gestures"]["available"] is False and d["display.rotation_lock"]["available"] is True
    assert [c["label"] for c in d["display.rotation"]["choices"]] == [
        "Automatic", "Standard", "90° clockwise", "180°", "90° counterclockwise"]
    with pytest.raises(schema.ValidationError):
        schema.validate_changes(schema.defaults(), {"input.touch_gestures": False}, ctx)


# -- the orientation rule (iio-sensor-proxy) ----------------------------------------------
@pytest.mark.parametrize("pose,want", [("normal", "normal"), ("bottom-up", "bottom-up"), ("left-up", "left-up"),
                                       ("right-up", "right-up"), ("tilted-normal", "normal")])
def test_orientation_rule(pose, want):
    x, y, z = fake_iio_accel.POSES[pose]
    assert O.orientation_from_accel(x, y, z, None) == want


def test_orientation_rule_keeps_flat_and_boundary():
    assert O.orientation_from_accel(0, 0, -1, "left-up") == "left-up"       # flat on a table
    assert O.orientation_from_accel(0.7, -0.7, 0, "normal") == "normal"     # 45 degrees: on the boundary
    assert O.orientation_from_accel(0.7, -0.7, 0, None) is None
    assert O.ROTATION == {"normal": 0, "right-up": 90, "bottom-up": 180, "left-up": 270}


def test_mount_matrix():
    m = O.parse_mount_matrix("0, 1, 0; -1, 0, 0; 0, 0, 1")
    assert O.apply_mount_matrix(m, (1.0, 2.0, 3.0)) == (2.0, -1.0, 3.0)
    assert O.parse_mount_matrix("1, 0; 0, 1") is None and O.parse_mount_matrix("") is None
    assert O.parse_mount_matrix("a, b, c; 1, 2, 3; 4, 5, 6") is None


def test_iio_reader(tmp_path):
    root = tmp_path / "iio"
    assert O.IioAccelerometer.find(str(root)) is None
    # a sensor mounted sideways: the mount matrix makes it read the device's frame
    matrix = "0, 1, 0; -1, 0, 0; 0, 0, 1"
    fake_iio_accel.write(root, "left-up", matrix)
    (root / "iio:device1").mkdir()                       # not an accelerometer
    (root / "iio:device1" / "in_illuminance_raw").write_text("5\n")
    a = O.IioAccelerometer.find(str(root), str(tmp_path / "udev"))
    assert a and a.name == "accel_3d" and a.matrix
    x, y, z = a.read()
    assert O.orientation_from_accel(x, y, z, None) == "left-up"
    # udev's ACCEL_MOUNT_MATRIX (hwdb) wins over the sysfs attribute
    (root / "iio:device0" / "dev").write_text("240:0\n")
    udev = tmp_path / "udev"
    udev.mkdir()
    (udev / "c240:0").write_text("E:ACCEL_MOUNT_MATRIX=1, 0, 0; 0, 1, 0; 0, 0, 1\nE:ACCEL_LOCATION=display\n")
    a = O.IioAccelerometer.find(str(root), str(udev))
    assert a.matrix == [[1, 0, 0], [0, 1, 0], [0, 0, 1]] and a.location() == "display"


def test_iio_following_debounced(tmp_path):
    root = tmp_path / "iio"
    fake_iio_accel.write(root, "normal")
    seen = []

    async def body():
        async def changed(r):
            seen.append(r)
        o = O.Orientation(changed, iio_root=str(root), udev_data=str(tmp_path), use_sensor_proxy=False,
                          poll=0.02, debounce=0.1)
        assert await o.probe() == "iio"
        o.assume(0)
        await o.follow(True)
        await asyncio.sleep(0.2)
        assert seen == []                         # held as it already was: nothing turns
        fake_iio_accel.write(root, "left-up")
        await asyncio.sleep(0.05)
        fake_iio_accel.write(root, "normal")      # a wobble shorter than the debounce
        await asyncio.sleep(0.3)
        assert seen == []
        fake_iio_accel.write(root, "right-up")
        await asyncio.sleep(0.4)
        assert seen == [90] and o.orientation == "right-up"
        await o.follow(False)                     # locked / not auto: rests
        fake_iio_accel.write(root, "bottom-up")
        await asyncio.sleep(0.3)
        assert seen == [90]
        await o.follow(True)
        await asyncio.sleep(0.4)
        assert seen == [90, 180]
        await o.stop()
    run(body())


# -- D-Bus: the wire format and a fake iio-sensor-proxy on a private bus -----------------
def test_dbus_marshalling_roundtrip():
    msg = dbus_lite.Message(dbus_lite.SIGNAL, serial=7, path="/net/hadess/SensorProxy",
                            interface="org.freedesktop.DBus.Properties", member="PropertiesChanged",
                            signature="sa{sv}as",
                            body=["net.hadess.SensorProxy",
                                  {"AccelerometerOrientation": dbus_lite.Variant("s", "left-up"),
                                   "HasAccelerometer": dbus_lite.Variant("b", True)}, ["x"]])
    data = dbus_lite.encode(msg)
    back, used = dbus_lite.decode(data + b"junk")
    assert used == len(data)
    assert back.member == "PropertiesChanged" and back.serial == 7 and back.signature == "sa{sv}as"
    assert back.body == ["net.hadess.SensorProxy", {"AccelerometerOrientation": "left-up", "HasAccelerometer": True},
                         ["x"]]
    assert dbus_lite.decode(data[:-1]) == (None, 0)
    assert dbus_lite.split_signature("sa{sv}as(iu)v") == ["s", "a{sv}", "as", "(iu)", "v"]


@pytest.mark.skipif(not shutil.which("dbus-daemon"), reason="needs dbus-daemon")
def test_sensor_proxy_on_a_private_bus(tmp_path):
    from fakes.fake_sensor_proxy import FakeSensorProxy, start_private_bus
    proc, address = start_private_bus(tmp_path / "bus")
    seen = []

    async def body():
        fake = FakeSensorProxy("normal")
        await fake.start(address)

        async def changed(r):
            seen.append(r)
        o = O.Orientation(changed, dbus_address=address, iio_root=str(tmp_path / "none"), debounce=0.1)
        assert await o.probe() == "sensor-proxy"
        fake.orientation = "bottom-up"
        assert await o.read_now() == "bottom-up" and not fake.claims    # claimed, read, released
        fake.orientation = "normal"
        o.orientation = None
        await o.follow(True)
        await asyncio.sleep(0.3)
        assert fake.claims and seen == [0]        # normal (no assumption made)
        fake.set("left-up")
        await asyncio.sleep(0.4)
        assert seen == [0, 270]
        await o.follow(False)
        await asyncio.sleep(0.1)
        assert not fake.claims and "ReleaseAccelerometer" in fake.calls
        fake.set("right-up")                       # not claimed: no signal, nothing happens
        await asyncio.sleep(0.3)
        assert seen == [0, 270]
        await o.stop()
        # a bus without the service: no sensor
        o2 = O.Orientation(None, dbus_address=address, iio_root=str(tmp_path / "none"))
        fake.conn.close()
        await asyncio.sleep(0.1)
        assert await o2.probe() is None
        await o2.stop()
    try:
        run(body())
    finally:
        proc.terminate()
        proc.wait(5)


# -- the auto rotation policy and what hostd sends -----------------------------------------
class FakeApp:
    def __init__(self, tmp_path, applied=None, internal=True, sensor=True):
        self.cfg = Config(root=str(tmp_path), iio_root=str(tmp_path / "iio"), udev_data=str(tmp_path),
                          rotation_boot_delay_s=0.05)
        if sensor:
            fake_iio_accel.write(tmp_path / "iio", "normal")
        self.settings = SimpleNamespace(applied=dict(schema.defaults(), **(applied or {})))
        self.events = []
        self.lifecycle = SimpleNamespace(event=lambda name, **kw: self.events.append((name, kw)))
        self.pushes = []
        self.boot_updates = []
        app = self

        class Presenter:
            display = {"outputs": [{"connector": "eDP-1", "role": "primary", "internal": internal}]}
            input = {"touchscreens": 1, "pens": 0}

            async def try_request(self, cmd, **kw):
                app.pushes.append((cmd, kw))
                return {"ok": True}
        self.presenter = Presenter()
        self.apply_engine = ApplyEngine(self)

        async def update_boot_display(why=""):
            self.boot_updates.append(why)
            return True
        self.apply_engine.update_boot_display = update_boot_display
        self.rotation = O.Rotation(self)
        self.rotation.sensor.poll = 0.02
        self.rotation.sensor.debounce = 0.05


def test_manual_rotation_and_input_config(tmp_path):
    app = FakeApp(tmp_path, {"display.rotation": "90", "input.touch_gestures": False}, sensor=False)
    run(app.rotation.start())
    assert not app.rotation.capable
    cfg = app.apply_engine.display_config()
    assert cfg["rotation"] == 90
    assert app.apply_engine.input_config()["touch_gestures"] is False
    app.settings.applied["display.rotation"] = "auto"
    assert app.apply_engine.display_config()["rotation"] == 0   # no sensor: the panel upright


def test_auto_rotation_follows_the_sensor(tmp_path):
    app = FakeApp(tmp_path)

    async def body():
        await app.rotation.start()
        assert app.rotation.capable
        app.rotation.on_presenter_display({"user_rotation": 0})
        await app.rotation.sync()
        fake_iio_accel.write(tmp_path / "iio", "right-up")
        await asyncio.sleep(0.3)
        assert app.pushes and app.pushes[-1] == ("display_config", app.apply_engine.display_config())
        assert app.pushes[-1][1]["rotation"] == 90
        await asyncio.sleep(0.2)
        assert app.boot_updates == ["orientation"]          # GRUB's background follows, later
        # Rotation Lock keeps the orientation in effect
        app.settings.applied["display.rotation_lock"] = True
        await app.rotation.sync()
        n = len(app.pushes)
        fake_iio_accel.write(tmp_path / "iio", "bottom-up")
        await asyncio.sleep(0.3)
        assert len(app.pushes) == n and app.rotation.target() == 90
        # an external main display: auto keeps the picture upright, no sensor
        app.settings.applied["display.rotation_lock"] = False
        app.presenter.display = {"outputs": [{"connector": "HDMI-A-1", "role": "primary", "internal": False},
                                             {"connector": "eDP-1", "role": "mirror", "internal": True}]}
        await app.rotation.sync()
        assert app.rotation.target() == 0 and not app.rotation.sensor.active
        # back on the built-in panel: follows again (it is upside down now)
        app.presenter.display = {"outputs": [{"connector": "eDP-1", "role": "primary", "internal": True}]}
        await app.rotation.sync()
        await asyncio.sleep(0.3)
        assert app.rotation.target() == 180
        # no built-in display at all (a tablet whose panel is not reported as one): follows
        app.presenter.display = {"outputs": [{"connector": "Virtual-1", "role": "primary", "internal": False}]}
        await app.rotation.sync()
        assert app.rotation.target() == 180 and app.rotation.sensor.active
        await app.rotation.stop()
    run(body())


def test_startup_reads_the_sensor_at_once(tmp_path):
    """Held some way at startup: the first display_config is already turned (no
    debounce), so Mac OS's -g is portrait from the start; held as GRUB drew it,
    nothing turns later."""
    app = FakeApp(tmp_path)
    fake_iio_accel.write(tmp_path / "iio", "left-up")

    async def body():
        await app.rotation.start()
        assert app.rotation.target() == 270
        assert app.apply_engine.display_config()["rotation"] == 270
        app.rotation.on_presenter_display({"user_rotation": 0})   # GRUB drew it upright: the sensor wins
        assert app.rotation.target() == 270
        await app.rotation.sync()
        await asyncio.sleep(0.3)
        assert not app.pushes                                      # nothing more to turn
        await app.rotation.stop()
    run(body())


def test_startup_flat_keeps_the_rotation_on_screen(tmp_path):
    """Flat on a table at startup: the sensor cannot tell; GRUB's (the last) orientation stays."""
    app = FakeApp(tmp_path)
    fake_iio_accel.write(tmp_path / "iio", "flat")

    async def body():
        await app.rotation.start()
        app.rotation.on_presenter_display({"user_rotation": 90})
        assert app.rotation.target() == 90
        await app.rotation.sync()
        await asyncio.sleep(0.3)
        assert not app.pushes
        await app.rotation.stop()
    run(body())


def test_capabilities_and_status_wording():
    """hostd.capabilities: touch from the presenter's input report, the tilt sensor."""
    import inspect
    from retroboot import hostd
    src = inspect.getsource(hostd.Hostd.capabilities)
    assert '"touch"' in src and '"accelerometer"' in src
