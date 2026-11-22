"""A fake bluetoothd on a private D-Bus (python3-dbus-fast), for the real BluezBackend.

It owns ``org.bluez`` and answers what hostd uses of BlueZ 5: ObjectManager
``GetManagedObjects`` at ``/`` with InterfacesAdded/Removed signals,
AgentManager1 at ``/org/bluez``, one Adapter1 ``/org/bluez/hci0`` (Powered
through Properties.Set, Discovering, the discovery filter, RemoveDevice) and
Device1 objects (Pair, CancelPairing, Connect, Disconnect, Trusted through
Properties.Set) with Battery1 once connected.  ``Pair`` calls the registered
agent as bluetoothd does: DisplayPasskey for a keyboard (then waits for
``type_code``), RequestPinCode for a legacy device, RequestConfirmation for
numeric comparison.  ``ask`` lets a test make the unsolicited requests a nearby
device could cause.  Every call is recorded in ``calls``.

Properties and GetManagedObjects are answered by a message handler (BlueZ's
optional properties such as Class or RSSI come and go), and signals are sent by
hand, exactly as BlueZ sends them.

``private_bus()`` starts a dbus-daemon with an allow-all policy and returns
(process, address, tempdir).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile

from dbus_fast import BusType, DBusError, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

ADAPTER_PATH = "/org/bluez/hci0"
PROPS = "org.freedesktop.DBus.Properties"
OM = "org.freedesktop.DBus.ObjectManager"
HID = "00001124-0000-1000-8000-00805f9b34fb"

CONF = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>system</type>
  <listen>unix:path={path}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow user="*"/>
    <allow own="*"/>
    <allow send_type="method_call"/>
    <allow send_destination="*"/>
    <allow receive_sender="*"/>
  </policy>
</busconfig>
"""

SIGS = {"Address": "s", "AddressType": "s", "Name": "s", "Alias": "s", "Paired": "b", "Bonded": "b",
        "Trusted": "b", "Blocked": "b", "Connected": "b", "LegacyPairing": "b", "Adapter": "o",
        "UUIDs": "as", "Class": "u", "Appearance": "q", "RSSI": "n", "Powered": "b", "PowerState": "s",
        "Discovering": "b", "Percentage": "y"}


def private_bus() -> tuple[subprocess.Popen, str, tempfile.TemporaryDirectory]:
    exe = shutil.which("dbus-daemon")
    if not exe:
        raise FileNotFoundError("dbus-daemon")
    td = tempfile.TemporaryDirectory(prefix="rbbus-", dir="/tmp")
    sock = os.path.join(td.name, "bus")
    conf = os.path.join(td.name, "bus.conf")
    with open(conf, "w") as f:
        f.write(CONF.format(path=sock))
    proc = subprocess.Popen([exe, "--config-file", conf, "--nofork", "--print-address"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    addr = proc.stdout.readline().strip()
    return proc, addr, td


def dev_path(address: str) -> str:
    return f"{ADAPTER_PATH}/dev_{address.replace(':', '_')}"


def variants(props: dict) -> dict:
    return {k: Variant(SIGS[k], v) for k, v in props.items()}


class AgentManager(ServiceInterface):
    def __init__(self, bluez: "FakeBluez"):
        super().__init__("org.bluez.AgentManager1")
        self.bluez = bluez

    @method()
    def RegisterAgent(self, agent: "o", capability: "s"):
        self.bluez.calls.append(("RegisterAgent", agent, capability))
        if self.bluez.agent and self.bluez.agent[1] == agent and self.bluez.agent[0] == self.bluez.current_sender:
            raise DBusError("org.bluez.Error.AlreadyExists", "Already Exists")
        self.bluez.agent = (self.bluez.current_sender, agent)

    @method()
    def UnregisterAgent(self, agent: "o"):
        self.bluez.calls.append(("UnregisterAgent", agent))
        self.bluez.agent = None

    @method()
    def RequestDefaultAgent(self, agent: "o"):
        self.bluez.calls.append(("RequestDefaultAgent", agent))


class Adapter(ServiceInterface):
    def __init__(self, bluez: "FakeBluez"):
        super().__init__("org.bluez.Adapter1")
        self.bluez = bluez
        self.props = {"Address": "00:1A:7D:DA:71:13", "Name": "BlueZ 5.82", "Alias": "retroboot",
                      "Powered": False, "PowerState": "off", "Discovering": False}

    def set_prop(self, name, value):
        if name != "Powered":
            raise DBusError("org.bluez.Error.InvalidArguments", "read only")
        self.bluez.calls.append(("Powered", value))
        if self.bluez.blocked and value:
            raise DBusError("org.bluez.Error.Blocked", "Blocked through rfkill")
        self.bluez.changed(ADAPTER_PATH, "org.bluez.Adapter1", self.props,
                           Powered=value, PowerState="on" if value else "off")
        if not value and self.props["Discovering"]:
            self.bluez.changed(ADAPTER_PATH, "org.bluez.Adapter1", self.props, Discovering=False)

    @method()
    def SetDiscoveryFilter(self, flt: "a{sv}"):
        self.bluez.calls.append(("SetDiscoveryFilter", {k: v.value for k, v in flt.items()}))

    @method()
    def StartDiscovery(self):
        self.bluez.calls.append(("StartDiscovery",))
        if not self.props["Powered"]:
            raise DBusError("org.bluez.Error.NotReady", "Resource Not Ready")
        self.bluez.changed(ADAPTER_PATH, "org.bluez.Adapter1", self.props, Discovering=True)
        for d in self.bluez.devices.values():
            if d.nearby and not d.props["Paired"]:
                self.bluez.appear(d)

    @method()
    def StopDiscovery(self):
        self.bluez.calls.append(("StopDiscovery",))
        self.bluez.changed(ADAPTER_PATH, "org.bluez.Adapter1", self.props, Discovering=False)
        for d in self.bluez.devices.values():
            if d.exported and "RSSI" in d.props:
                d.props.pop("RSSI")
                self.bluez.signal(d.path, PROPS, "PropertiesChanged", "sa{sv}as",
                                  ["org.bluez.Device1", {}, ["RSSI"]])

    @method()
    def RemoveDevice(self, device: "o"):
        self.bluez.calls.append(("RemoveDevice", device))
        for d in self.bluez.devices.values():
            if d.path == device and d.exported:
                d.props.update(Paired=False, Bonded=False, Trusted=False, Connected=False)
                self.bluez.disappear(d)
                return
        raise DBusError("org.bluez.Error.DoesNotExist", "Does Not Exist")


class Device(ServiceInterface):
    """Device1; ``method_`` is how it pairs: passkey | pin | justworks | confirm | unreachable."""

    def __init__(self, bluez: "FakeBluez", address: str, name: str, cls: int | None = None,
                 appearance: int | None = None, method_: str = "justworks", paired: bool = False,
                 connected: bool = False, battery: int | None = None, rssi: int = -55, nearby: bool = True,
                 uuids: list | None = None):
        super().__init__("org.bluez.Device1")
        self.bluez = bluez
        self.address = address
        self.path = dev_path(address)
        self.method = method_
        self.nearby = nearby
        self.rssi = rssi
        self.exported = False
        self.battery = battery
        self.typed: asyncio.Future | None = None
        self.props = {"Address": address, "AddressType": "public", "Name": name, "Alias": name,
                      "Paired": paired, "Bonded": paired, "Trusted": paired, "Blocked": False,
                      "Connected": connected, "LegacyPairing": method_ == "pin", "Adapter": ADAPTER_PATH,
                      "UUIDs": uuids if uuids is not None else [HID]}
        if cls is not None:
            self.props["Class"] = cls
        if appearance is not None:
            self.props["Appearance"] = appearance

    def ifaces(self) -> dict:
        out = {"org.bluez.Device1": variants(self.props)}
        if self.battery is not None and self.props["Connected"]:
            out["org.bluez.Battery1"] = variants({"Percentage": self.battery})
        return out

    def set_prop(self, name, value):
        self.bluez.calls.append(("Set", self.address, name, value))
        if name != "Trusted":
            raise DBusError("org.bluez.Error.InvalidArguments", "read only")
        self.bluez.changed(self.path, "org.bluez.Device1", self.props, Trusted=value)

    def set(self, **kw) -> None:
        self.bluez.changed(self.path, "org.bluez.Device1", self.props, **kw)

    @method()
    async def Pair(self):
        b = self.bluez
        b.calls.append(("Pair", self.address))
        await asyncio.sleep(0.02)
        if self.props["Paired"]:
            raise DBusError("org.bluez.Error.AlreadyExists", "Already Exists")
        if self.method == "unreachable":
            await asyncio.sleep(0.1)
            raise DBusError("org.bluez.Error.Failed", "Page Timeout")
        if self.method == "passkey":
            passkey = 314159
            reply = await b.ask("DisplayPasskey", "ouq", [self.path, passkey, 0])
            if reply.message_type == MessageType.ERROR:
                raise DBusError("org.bluez.Error.AuthenticationRejected", "Authentication Rejected")
            self.typed = asyncio.get_running_loop().create_future()
            typed = await asyncio.wait_for(self.typed, 30)
            if typed == "cancel":
                raise DBusError("org.bluez.Error.AuthenticationCanceled", "Authentication Canceled")
            if typed != f"{passkey:06d}":
                raise DBusError("org.bluez.Error.AuthenticationFailed", "Authentication Failed")
        elif self.method == "pin":
            reply = await b.ask("RequestPinCode", "o", [self.path])
            if reply.message_type == MessageType.ERROR:
                raise DBusError("org.bluez.Error.AuthenticationRejected", "Authentication Rejected")
            b.pins.append(reply.body[0])
            if self.props.get("Class", 0) & 0xC0 == 0x40:      # a keyboard: the PIN is typed on it
                self.typed = asyncio.get_running_loop().create_future()
                if await asyncio.wait_for(self.typed, 30) != reply.body[0]:
                    raise DBusError("org.bluez.Error.AuthenticationFailed", "Authentication Failed")
            elif reply.body[0] != "0000":
                raise DBusError("org.bluez.Error.AuthenticationFailed", "Authentication Failed")
        elif self.method == "confirm":
            reply = await b.ask("RequestConfirmation", "ou", [self.path, 271828])
            if reply.message_type == MessageType.ERROR:
                raise DBusError("org.bluez.Error.AuthenticationRejected", "Authentication Rejected")
        self.set(Paired=True, Bonded=True)

    @method()
    def CancelPairing(self):
        self.bluez.calls.append(("CancelPairing", self.address))
        if self.typed and not self.typed.done():
            self.typed.set_result("cancel")

    @method()
    async def Connect(self):
        self.bluez.calls.append(("Connect", self.address))
        await asyncio.sleep(0.02)
        if not self.nearby:
            raise DBusError("org.bluez.Error.Failed", "br-connection-page-timeout")
        if self.props["Connected"]:
            raise DBusError("org.bluez.Error.AlreadyConnected", "Already Connected")
        self.set(Connected=True)
        if self.battery is not None:
            self.bluez.signal("/", OM, "InterfacesAdded", "oa{sa{sv}}",
                              [self.path, {"org.bluez.Battery1": variants({"Percentage": self.battery})}])

    @method()
    def Disconnect(self):
        self.bluez.calls.append(("Disconnect", self.address))
        self.set(Connected=False)


class FakeBluez:
    def __init__(self, address: str):
        self.address = address
        self.bus: MessageBus | None = None
        self.calls: list[tuple] = []
        self.pins: list[str] = []
        self.agent: tuple[str, str] | None = None
        self.current_sender: str | None = None
        self.blocked = False
        self.devices: dict[str, Device] = {}
        self.adapter = Adapter(self)

    async def start(self) -> None:
        self.bus = await MessageBus(bus_address=self.address, bus_type=BusType.SYSTEM).connect()
        self.bus.add_message_handler(self._handle)
        self.bus.export("/org/bluez", AgentManager(self))
        self.bus.export(ADAPTER_PATH, self.adapter)
        for d in self.devices.values():
            if d.props["Paired"]:
                self.appear(d, announce=False)
        await self.bus.request_name("org.bluez")

    async def stop(self) -> None:
        if self.bus:
            self.bus.disconnect()
            self.bus = None

    def add(self, *args, **kw) -> Device:
        d = Device(self, *args, **kw)
        self.devices[d.address] = d
        return d

    # -- plumbing ----------------------------------------------------------------------
    def _handle(self, msg: Message):
        if msg.message_type != MessageType.METHOD_CALL:
            return None
        self.current_sender = msg.sender
        if msg.interface == OM and msg.member == "GetManagedObjects":
            return Message.new_method_return(msg, "a{oa{sa{sv}}}", [self.managed()])
        if msg.interface == PROPS:
            obj = self.adapter if msg.path == ADAPTER_PATH else next(
                (d for d in self.devices.values() if d.exported and d.path == msg.path), None)
            if obj is None:
                raise DBusError("org.freedesktop.DBus.Error.UnknownObject", msg.path)
            if msg.member == "Set":
                obj.set_prop(msg.body[1], msg.body[2].value)
                return Message.new_method_return(msg)
            if msg.member == "GetAll":
                return Message.new_method_return(msg, "a{sv}", [variants(obj.props)])
            if msg.member == "Get":
                return Message.new_method_return(msg, "v", [Variant(SIGS[msg.body[1]], obj.props[msg.body[1]])])
        return None

    def signal(self, path: str, iface: str, member: str, signature: str, body: list) -> None:
        if self.bus:
            self.bus.send(Message.new_signal(path, iface, member, signature, body))

    def changed(self, path: str, iface: str, props: dict, **kw) -> None:
        props.update(kw)
        self.signal(path, PROPS, "PropertiesChanged", "sa{sv}as", [iface, variants(kw), []])

    def appear(self, d: Device, announce: bool = True) -> None:
        if d.nearby and self.adapter.props["Discovering"] and not d.props["Paired"]:
            d.props["RSSI"] = d.rssi
        if d.exported:
            if "RSSI" in d.props:
                self.changed(d.path, "org.bluez.Device1", d.props, RSSI=d.rssi)
            return
        d.exported = True
        self.bus.export(d.path, d)
        if announce:
            self.signal("/", OM, "InterfacesAdded", "oa{sa{sv}}", [d.path, d.ifaces()])

    def disappear(self, d: Device) -> None:
        if not d.exported:
            return
        d.exported = False
        self.bus.unexport(d.path)
        d.props.pop("RSSI", None)
        self.signal("/", OM, "InterfacesRemoved", "oas", [d.path, ["org.bluez.Device1", "org.bluez.Battery1"]])

    def managed(self) -> dict:
        out = {ADAPTER_PATH: {"org.bluez.Adapter1": variants(self.adapter.props)}}
        for d in self.devices.values():
            if d.exported:
                out[d.path] = d.ifaces()
        return out

    async def ask(self, member: str, signature: str, body: list) -> Message:
        """Call the registered agent the way bluetoothd does."""
        if not self.agent:
            raise DBusError("org.bluez.Error.AuthenticationFailed", "No agent")
        owner, path = self.agent
        reply = await self.bus.call(Message(destination=owner, path=path, interface="org.bluez.Agent1",
                                            member=member, signature=signature, body=body))
        self.calls.append(("agent", member, reply.error_name if reply.message_type == MessageType.ERROR else None))
        return reply

    def type_code(self, address: str, code: str) -> None:
        d = self.devices[address]
        if d.typed and not d.typed.done():
            d.typed.set_result(code)
