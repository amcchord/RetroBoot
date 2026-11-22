#!/usr/bin/env python3
"""A stand-in for iio-sensor-proxy (net.hadess.SensorProxy) on a D-Bus bus.

For tests and the lab: hostd's orientation.py claims the accelerometer and
follows AccelerometerOrientation exactly as with the real service.  The
orientation is changed through a FIFO (one name per line: normal, left-up,
right-up, bottom-up) or from Python (FakeSensorProxy.set).

    fake_sensor_proxy.py --bus ADDRESS [--fifo PATH] [--orientation normal]
    fake_sensor_proxy.py --private DIR ...   # start a private bus too; prints its address

A private bus (dbus-daemon with a permissive custom configuration) keeps the
machine's system bus untouched; point hostd's dbus_system_address at it.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from retroboot import dbus_lite  # noqa: E402

NAME = "net.hadess.SensorProxy"
PATH = "/net/hadess/SensorProxy"
PROPS = "org.freedesktop.DBus.Properties"

BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>custom</type>
  <listen>unix:path={path}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow user="*"/>
    <allow own="*"/>
    <allow send_destination="*" eavesdrop="true"/>
    <allow receive_sender="*"/>
  </policy>
</busconfig>
"""


def start_private_bus(directory: str | Path) -> tuple[subprocess.Popen, str]:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    sock = d / "bus"
    sock.unlink(missing_ok=True)
    conf = d / "bus.conf"
    conf.write_text(BUS_CONFIG.format(path=sock))
    proc = subprocess.Popen(["dbus-daemon", f"--config-file={conf}", "--nofork", "--print-address"],
                            stdout=subprocess.PIPE, text=True)
    address = proc.stdout.readline().strip()
    return proc, address


class FakeSensorProxy:
    def __init__(self, orientation: str = "normal", has_accelerometer: bool = True):
        self.orientation = orientation
        self.has = has_accelerometer
        self.claims: set[str] = set()
        self.calls: list[str] = []
        self.conn: dbus_lite.Connection | None = None

    async def start(self, address: str) -> None:
        self.conn = await dbus_lite.Connection.connect(address)
        self.conn.method_handler = self._method
        await self.conn.request_name(NAME)

    def _props(self) -> dict:
        return {"HasAccelerometer": dbus_lite.Variant("b", self.has),
                "AccelerometerOrientation": dbus_lite.Variant("s", self.orientation),
                "HasAmbientLight": dbus_lite.Variant("b", False)}

    def _method(self, msg: dbus_lite.Message) -> None:
        self.calls.append(msg.member or "")
        c = self.conn
        if msg.interface == NAME and msg.member in ("ClaimAccelerometer", "ReleaseAccelerometer"):
            (self.claims.add if msg.member.startswith("Claim") else self.claims.discard)(msg.sender)
            c.reply(msg)
        elif msg.interface == PROPS and msg.member == "Get":
            prop = self._props().get(msg.body[1])
            if prop is None:
                c.reply_error(msg, "org.freedesktop.DBus.Error.UnknownProperty", msg.body[1])
            else:
                c.reply(msg, "v", [prop])
        elif msg.interface == PROPS and msg.member == "GetAll":
            c.reply(msg, "a{sv}", [self._props()])
        else:
            c.reply_error(msg, "org.freedesktop.DBus.Error.UnknownMethod", msg.member or "")

    def set(self, orientation: str) -> None:
        """A new orientation; signalled only while someone holds a claim (as the real service)."""
        self.orientation = orientation
        if self.claims and self.conn:
            self.conn.emit(PATH, PROPS, "PropertiesChanged", "sa{sv}as",
                           [NAME, {"AccelerometerOrientation": dbus_lite.Variant("s", orientation)}, []])


async def main_async(a) -> int:
    proc = None
    address = a.bus
    if a.private:
        proc, address = start_private_bus(a.private)
        print(address, flush=True)
    fake = FakeSensorProxy(a.orientation)
    await fake.start(address)
    if a.fifo:
        if not os.path.exists(a.fifo):
            os.mkfifo(a.fifo)
        while True:
            line = await asyncio.to_thread(Path(a.fifo).read_text)
            for name in line.split():
                print("orientation", name, "claims", len(fake.claims), flush=True)
                fake.set(name)
    else:
        await asyncio.Event().wait()
    if proc:
        proc.terminate()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bus", help="D-Bus address (default: the system bus)")
    p.add_argument("--private", metavar="DIR", help="start a private bus in DIR and print its address")
    p.add_argument("--fifo", help="read orientation names from this FIFO")
    p.add_argument("--orientation", default="normal")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
