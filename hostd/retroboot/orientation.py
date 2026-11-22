"""Device orientation for ``display.rotation`` "auto" (ARCHITECTURE 4.5).

On a convertible or tablet the picture follows how the computer is held.
The tilt comes from the first source that exists:

* iio-sensor-proxy on the system bus (``net.hadess.SensorProxy``): hostd
  claims the accelerometer and follows ``AccelerometerOrientation``;
* the kernel's IIO accelerometer directly (``/sys/bus/iio/devices``,
  ``in_accel_{x,y,z}_raw`` with their scale and mount matrix: the IIO
  ``mount_matrix`` attribute or udev's ``ACCEL_MOUNT_MATRIX`` from the
  hwdb), read every 200 ms while it is needed, with iio-sensor-proxy's own
  orientation rule (35 degrees from the axis; flat or on the boundary keeps
  the current orientation).

The orientations (iio-sensor-proxy's names) relative to the device's
natural orientation become the user rotation on top of the panel's own
orientation (presenter rotation.h): normal 0, right-up 90, bottom-up 180,
left-up 270.  A new orientation is applied once it has been steady for
DEBOUNCE seconds.  The tilt only turns the built-in display: with an
external main display "auto" keeps the picture upright.  Rotation Lock
(``display.rotation_lock``) keeps the orientation in effect.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("retroboot.orientation")

ORIENTATIONS = ("normal", "right-up", "bottom-up", "left-up")
ROTATION = {"normal": 0, "right-up": 90, "bottom-up": 180, "left-up": 270}
THRESHOLD = 35          # degrees (iio-sensor-proxy's default)
SAME_AXIS_LIMIT = 5
POLL = 0.2
DEBOUNCE = 0.5
IIO_ROOT = "/sys/bus/iio/devices"
UDEV_DATA = "/run/udev/data"
SENSOR_PROXY = "net.hadess.SensorProxy"
SENSOR_PROXY_PATH = "/net/hadess/SensorProxy"
PROPS = "org.freedesktop.DBus.Properties"


def orientation_from_accel(x: float, y: float, z: float, prev: str | None) -> str | None:
    """iio-sensor-proxy's orientation_calc: the angle of the gravity reading
    from the device's x and y axes (after the mount matrix; normal upright
    reads y = -1 g)."""
    portrait = round(math.degrees(math.atan2(x, math.sqrt(y * y + z * z))))
    landscape = round(math.degrees(math.atan2(y, math.sqrt(x * x + z * z))))
    if abs(portrait) > THRESHOLD and abs(landscape) > THRESHOLD:
        return prev
    if abs(portrait) > THRESHOLD:
        ret = "left-up" if portrait > 0 else "right-up"
        if prev in ("left-up", "right-up") and abs(portrait) < SAME_AXIS_LIMIT:
            ret = prev
        return ret
    if abs(landscape) > THRESHOLD:
        ret = "bottom-up" if landscape > 0 else "normal"
        if prev in ("bottom-up", "normal") and abs(landscape) < SAME_AXIS_LIMIT:
            ret = prev
        return ret
    return prev


def parse_mount_matrix(text: str | None) -> list[list[float]] | None:
    """'x1, y1, z1; x2, y2, z2; x3, y3, z3' (hwdb ACCEL_MOUNT_MATRIX and the IIO
    mount_matrix attribute) -> rows; None when absent or malformed."""
    if not text:
        return None
    try:
        rows = [[float(v) for v in row.split(",")] for row in text.strip().split(";")]
    except ValueError:
        return None
    if len(rows) != 3 or any(len(r) != 3 for r in rows):
        return None
    return rows


def apply_mount_matrix(m: list[list[float]] | None, v: tuple[float, float, float]) -> tuple[float, float, float]:
    if not m:
        return v
    x, y, z = v
    return tuple(r[0] * x + r[1] * y + r[2] * z for r in m)  # type: ignore[return-value]


class IioAccelerometer:
    """An IIO accelerometer read through sysfs."""

    def __init__(self, path: Path, udev_data: str = UDEV_DATA):
        self.path = path
        self.name = self._read("name") or path.name
        self.matrix = self._matrix(udev_data)
        self.scale = {a: self._float(f"in_accel_{a}_scale") or self._float("in_accel_scale") or 1.0 for a in "xyz"}

    @staticmethod
    def find(root: str = IIO_ROOT, udev_data: str = UDEV_DATA) -> "IioAccelerometer | None":
        try:
            devs = sorted(Path(root).iterdir())
        except OSError:
            return None
        found = []
        for d in devs:
            if (d / "in_accel_x_raw").exists() and (d / "in_accel_y_raw").exists():
                found.append(IioAccelerometer(d, udev_data))
        # 2-in-1s with a second sensor in the base: the display's (or one without a location)
        found.sort(key=lambda a: a.location() == "base")
        return found[0] if found else None

    def _read(self, name: str) -> str | None:
        try:
            return (self.path / name).read_text().strip()
        except OSError:
            return None

    def _float(self, name: str) -> float | None:
        try:
            return float(self._read(name) or "")
        except ValueError:
            return None

    def _udev(self, udev_data: str) -> dict:
        dev = self._read("dev")
        if not dev:
            return {}
        try:
            text = Path(udev_data, f"c{dev}").read_text()
        except OSError:
            return {}
        return dict(line[2:].split("=", 1) for line in text.splitlines() if line.startswith("E:") and "=" in line)

    def _matrix(self, udev_data: str):
        self.udev = self._udev(udev_data)
        return parse_mount_matrix(self.udev.get("ACCEL_MOUNT_MATRIX")) or \
            parse_mount_matrix(self._read("in_accel_mount_matrix")) or parse_mount_matrix(self._read("mount_matrix"))

    def location(self) -> str | None:
        """"base" or "display" (udev ACCEL_LOCATION, else the IIO label)."""
        loc = self.udev.get("ACCEL_LOCATION")
        if loc:
            return loc
        label = self._read("label") or ""
        return {"accel-base": "base", "accel-display": "display"}.get(label)

    def read(self) -> tuple[float, float, float] | None:
        try:
            raw = tuple(float(self._read(f"in_accel_{a}_raw") or "0") * self.scale[a] for a in "xyz")
        except ValueError:
            return None
        return apply_mount_matrix(self.matrix, raw)  # type: ignore[arg-type]


class Orientation:
    """The current orientation and the rotation "auto" applies.

    ``on_change`` (async) is called with the new rotation whenever the
    debounced orientation changes while auto rotation follows the sensor."""

    def __init__(self, on_change: Callable[[int], Awaitable[None]] | None = None, *, iio_root: str = IIO_ROOT,
                 udev_data: str = UDEV_DATA, dbus_address: str | None = None, use_sensor_proxy: bool = True,
                 poll: float = POLL, debounce: float = DEBOUNCE):
        self.on_change = on_change
        self.iio_root = iio_root
        self.udev_data = udev_data
        self.dbus_address = dbus_address
        self.use_sensor_proxy = use_sensor_proxy
        self.poll = poll
        self.debounce = debounce
        self.source: str | None = None       # "sensor-proxy" | "iio" | None
        self.device: str | None = None
        self.orientation: str | None = None  # debounced
        self.active = False                  # following (auto rotation, not locked)
        self._iio: IioAccelerometer | None = None
        self._bus = None
        self._raw: str | None = None
        self._raw_since = 0.0
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopping = False

    # -- discovery ---------------------------------------------------------------
    async def probe(self) -> str | None:
        """Find the sensor (once; cheap). Returns the source name or None."""
        if self.use_sensor_proxy and await self._probe_sensor_proxy():
            self.source = "sensor-proxy"
        else:
            self._iio = IioAccelerometer.find(self.iio_root, self.udev_data)
            if self._iio:
                self.source = "iio"
                self.device = self._iio.name
        if self.source:
            log.info("accelerometer: %s (%s)", self.source, self.device)
        return self.source

    async def _probe_sensor_proxy(self) -> bool:
        from . import dbus_lite
        try:
            bus = await dbus_lite.Connection.connect(self.dbus_address, timeout=2.0)
        except (OSError, asyncio.TimeoutError, dbus_lite.DBusError):
            return False
        try:
            names = set()
            for member in ("ListNames", "ListActivatableNames"):
                (n,) = await bus.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                      member, timeout=2.0)
                names.update(n)
            if SENSOR_PROXY not in names:
                bus.close()
                return False
            (has,) = await bus.call(SENSOR_PROXY, SENSOR_PROXY_PATH, PROPS, "Get", "ss",
                                    [SENSOR_PROXY, "HasAccelerometer"], timeout=5.0)
            if not has:
                bus.close()
                return False
        except (OSError, asyncio.TimeoutError, dbus_lite.DBusError) as exc:
            log.info("iio-sensor-proxy not usable: %s", exc)
            bus.close()
            return False
        self._bus = bus
        self.device = "iio-sensor-proxy"
        bus.signal_handlers.append(self._on_signal)
        await bus.add_match(f"type='signal',sender='{SENSOR_PROXY}',interface='{PROPS}',"
                            f"member='PropertiesChanged',path='{SENSOR_PROXY_PATH}'")
        return True

    @property
    def available(self) -> bool:
        return self.source is not None

    def rotation(self) -> int:
        return ROTATION.get(self.orientation or "normal", 0)

    async def read_now(self) -> str | None:
        """One reading without the debounce (startup): the orientation, or None
        when the sensor cannot tell (flat on a table, not ready)."""
        o = None
        if self.source == "iio" and self._iio:
            v = self._iio.read()
            o = orientation_from_accel(*v, None) if v else None
        elif self.source == "sensor-proxy" and self._bus:
            from . import dbus_lite
            try:
                await self._bus.call(SENSOR_PROXY, SENSOR_PROXY_PATH, SENSOR_PROXY, "ClaimAccelerometer")
                for _ in range(4):      # the service reads the sensor once claimed
                    (v,) = await self._bus.call(SENSOR_PROXY, SENSOR_PROXY_PATH, PROPS, "Get", "ss",
                                                [SENSOR_PROXY, "AccelerometerOrientation"])
                    if v in ORIENTATIONS:
                        o = v
                        break
                    await asyncio.sleep(0.1)
                if not self.active:
                    await self._bus.call(SENSOR_PROXY, SENSOR_PROXY_PATH, SENSOR_PROXY, "ReleaseAccelerometer")
            except (asyncio.TimeoutError, dbus_lite.DBusError) as exc:
                log.warning("iio-sensor-proxy: %s", exc)
        if o:
            self.orientation = self._raw = o
        return o

    def assume(self, rotation: int) -> None:
        """Start from the rotation already on screen (GRUB's, after a restart),
        so a device held the same way does not turn at startup."""
        for name, r in ROTATION.items():
            if r == rotation and self.orientation is None:
                self.orientation = name

    def public(self) -> dict:
        return {"available": self.available, "source": self.source, "device": self.device,
                "orientation": self.orientation, "following": self.active}

    # -- following -----------------------------------------------------------------
    async def follow(self, on: bool) -> None:
        """Follow the sensor (auto rotation, unlocked) or let it rest."""
        if not self.available or on == self.active:
            return
        self.active = on
        if self.source == "sensor-proxy":
            from . import dbus_lite
            try:
                await self._bus.call(SENSOR_PROXY, SENSOR_PROXY_PATH, SENSOR_PROXY,
                                     "ClaimAccelerometer" if on else "ReleaseAccelerometer")
                if on:
                    (o,) = await self._bus.call(SENSOR_PROXY, SENSOR_PROXY_PATH, PROPS, "Get", "ss",
                                                [SENSOR_PROXY, "AccelerometerOrientation"])
                    self._raw_update(o)
            except (asyncio.TimeoutError, dbus_lite.DBusError) as exc:
                log.warning("iio-sensor-proxy: %s", exc)
        if on and (self._task is None or self._task.done()):
            self._task = asyncio.get_running_loop().create_task(self._run(), name="orientation")
        self._wake.set()

    def _on_signal(self, msg) -> None:
        if msg.member != "PropertiesChanged" or not msg.body or msg.body[0] != SENSOR_PROXY:
            return
        changed = msg.body[1] if len(msg.body) > 1 else {}
        if "AccelerometerOrientation" in changed and self.active:
            self._raw_update(changed["AccelerometerOrientation"])

    def _raw_update(self, o) -> None:
        o = o if o in ORIENTATIONS else None
        if o is None:
            return     # "undefined": keep what we have
        if o != self._raw:
            self._raw = o
            self._raw_since = asyncio.get_running_loop().time()
            self._wake.set()

    def sample(self) -> None:
        """One IIO reading (the polling source)."""
        if not self._iio:
            return
        v = self._iio.read()
        if v is None:
            return
        self._raw_update(orientation_from_accel(*v, self._raw))

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stopping:
            if not self.active:
                self._wake.clear()
                await self._wake.wait()
                continue
            if self.source == "iio":
                self.sample()
            if self._raw and self._raw != self.orientation and loop.time() - self._raw_since >= self.debounce:
                self.orientation = self._raw
                log.info("orientation %s (rotation %d)", self.orientation, self.rotation())
                if self.on_change:
                    try:
                        await self.on_change(self.rotation())
                    except Exception:
                        log.exception("orientation change handler failed")
            self._wake.clear()
            wait = self.poll if self.source == "iio" else (self.debounce if self._raw != self.orientation else None)
            try:
                await asyncio.wait_for(self._wake.wait(), wait)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._bus:
            self._bus.close()


class Rotation:
    """``display.rotation`` and ``display.rotation_lock`` for hostd: the user
    rotation sent to the presenter (display_config ``rotation``).

    * 0/90/180/270: that rotation.
    * auto: the tilt sensor's orientation while the built-in display is the
      main one (or no display is a built-in one) and Rotation Lock is off; the orientation in effect when the lock
      went on while it is on; 0 (the panel upright) without a sensor or with an
      external main display.  At startup the rotation already on screen (GRUB's,
      the presenter's) is the starting point, so nothing turns unless the
      device is held another way.  A changed auto rotation reaches GRUB's
      background (rb-bootdisplay) after ``rotation_boot_delay_s``.
    """

    def __init__(self, app):
        self.app = app
        cfg = app.cfg
        self.sensor = Orientation(self._on_orientation, iio_root=cfg.iio_root, udev_data=cfg.udev_data,
                                  dbus_address=cfg.dbus_system_address)
        self.enabled = cfg.orientation_sensor
        self.current = 0             # the auto rotation in effect
        self._assumed = False
        self._boot_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.enabled:
            try:
                await self.sensor.probe()
            except Exception:       # a sensor must never keep Mac OS from starting
                log.exception("accelerometer probe failed")
        if self.sensor.available:
            # held some way at startup: Mac OS starts turned that way (the first
            # display_config already carries it, before the guest's -g is chosen)
            try:
                o = await self.sensor.read_now()
            except Exception:
                log.exception("accelerometer read failed")
                o = None
            if o:
                self.current = ROTATION[o]
                self._assumed = True
            self.app.lifecycle.event("accelerometer", source=self.sensor.source, device=self.sensor.device,
                                     orientation=o)

    async def stop(self) -> None:
        if self._boot_task:
            self._boot_task.cancel()
        await self.sensor.stop()

    @property
    def capable(self) -> bool:
        return self.sensor.available

    def _setting(self) -> str:
        return str(self.app.settings.applied.get("display.rotation", "auto"))

    def _internal_primary(self) -> bool:
        """The tilt sensor turns the built-in display: true when that is the main
        display, or when no display is a built-in one (a tablet whose panel is
        not reported as built in; the lab VM)."""
        outputs = (self.app.presenter.display or {}).get("outputs") or []
        prim = [o for o in outputs if o.get("role") == "primary"]
        if not any(o.get("internal") for o in outputs):
            return bool(prim)
        return bool(prim and prim[0].get("internal"))

    def _following(self) -> bool:
        return (self._setting() == "auto" and not self.app.settings.applied.get("display.rotation_lock")
                and self.sensor.available and self._internal_primary())

    def target(self) -> int:
        v = self._setting()
        if v != "auto":
            return int(v)
        if not self.sensor.available or not self._internal_primary():
            return 0
        return self.current

    def on_presenter_display(self, display: dict | None) -> None:
        """First contact: without a reading from the sensor at startup (flat on a
        table), adopt the rotation on screen as the auto starting point."""
        if self._assumed or not display:
            return
        self._assumed = True
        r = int(display.get("user_rotation") or 0)
        if self._setting() == "auto" and self.sensor.available and r in (0, 90, 180, 270):
            self.current = r
            self.sensor.assume(r)

    async def sync(self) -> None:
        """Follow the sensor or not (settings, main display, presenter changed)."""
        await self.sensor.follow(self._following())

    async def _on_orientation(self, rotation: int) -> None:
        if not self._following() or rotation == self.current:
            return
        self.current = rotation
        self.app.lifecycle.event("orientation", orientation=self.sensor.orientation, rotation=rotation)
        await self.app.apply_engine.push_display()
        self._schedule_boot_display()

    def _schedule_boot_display(self) -> None:
        if self._boot_task and not self._boot_task.done():
            self._boot_task.cancel()

        async def later():
            await asyncio.sleep(self.app.cfg.rotation_boot_delay_s)
            await self.app.apply_engine.update_boot_display(why="orientation")
        self._boot_task = asyncio.get_running_loop().create_task(later())

    def public(self) -> dict:
        return {"setting": self._setting(), "lock": bool(self.app.settings.applied.get("display.rotation_lock")),
                "rotation": self.target(), "sensor": self.sensor.public()}
