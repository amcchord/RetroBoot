"""BlueZ over D-Bus (python3-dbus-fast): the real Bluetooth backend.

hostd talks to ``bluetoothd`` on the system bus with plain messages (no
introspection): ObjectManager for adapters and devices, PropertiesChanged and
InterfacesAdded/Removed for updates, Adapter1/Device1 methods for the
operations.  It exports one ``org.bluez.Agent1`` object, registered as the
default agent with capability ``KeyboardDisplay``, so a keyboard gets a passkey
to type; the agent hands every request to :meth:`Bluetooth.agent_request`.

``bluetoothd`` may start after hostd or restart: the backend follows the
``org.bluez`` name, reloads the objects and registers the agent again.  The
system bus can be replaced with ``DBUS_SYSTEM_BUS_ADDRESS`` (tests run a
private bus with a fake BlueZ).
"""
from __future__ import annotations

import asyncio
import logging
import re

from dbus_fast import BusType, DBusError, Message, MessageType, Variant, unpack_variants
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

from .bluetooth import AgentCanceled, AgentRejected, AgentRequest, Backend, BluetoothError

log = logging.getLogger("retroboot.bluetooth")

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
BATTERY = "org.bluez.Battery1"
AGENT_MANAGER = "org.bluez.AgentManager1"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
PROPERTIES = "org.freedesktop.DBus.Properties"
AGENT_PATH = "/org/retroboot/bluetooth/agent"
CAPABILITY = "KeyboardDisplay"

ERRORS = {
    "org.bluez.Error.AuthenticationFailed": "auth_failed",
    "org.bluez.Error.AuthenticationCanceled": "canceled",
    "org.bluez.Error.AuthenticationRejected": "rejected",
    "org.bluez.Error.AuthenticationTimeout": "timeout",
    "org.bluez.Error.ConnectionAttemptFailed": "unreachable",
    "org.bluez.Error.AlreadyExists": "already_paired",
    "org.bluez.Error.InProgress": "busy",
    "org.bluez.Error.DoesNotExist": "not_found",
    "org.bluez.Error.NotReady": "off",
    "org.bluez.Error.Blocked": "blocked",
    "org.bluez.Error.NotConnected": "not_connected",
    "org.bluez.Error.AlreadyConnected": "already_connected",
    "org.bluez.Error.NotAvailable": "unavailable",
    "org.freedesktop.DBus.Error.UnknownObject": "not_found",
    "org.freedesktop.DBus.Error.UnknownMethod": "not_found",
    "org.freedesktop.DBus.Error.NoReply": "timeout",
    "org.freedesktop.DBus.Error.ServiceUnknown": "unavailable",
    "org.freedesktop.DBus.Error.NameHasNoOwner": "unavailable",
}
UNREACHABLE_RE = re.compile(r"page.?timeout|host is down|no route|abort-by-local|connection.?timed.?out|"
                            r"create-socket|connection refused|le-connection", re.I)


def map_error(name: str | None, text: str | None) -> BluetoothError:
    code = ERRORS.get(name or "", "failed")
    if code in ("failed", "unavailable") and text and UNREACHABLE_RE.search(text):
        code = "unreachable"
    return BluetoothError(code, f"{(name or '').rsplit('.', 1)[-1]}: {text or ''}"[:200])


def device_path_address(path: str) -> str:
    m = re.search(r"/dev_([0-9A-Fa-f]{2}(?:_[0-9A-Fa-f]{2}){5})$", path or "")
    return m.group(1).replace("_", ":").upper() if m else ""


class Agent(ServiceInterface):
    """org.bluez.Agent1: every request goes to the service's policy."""

    def __init__(self, backend: "BluezBackend"):
        super().__init__("org.bluez.Agent1")
        self.backend = backend

    async def _ask(self, req: AgentRequest):
        handler = self.backend.agent
        if handler is None:
            raise DBusError("org.bluez.Error.Rejected", "No pairing is in progress.")
        try:
            return await handler(req)
        except AgentRejected:
            raise DBusError("org.bluez.Error.Rejected", "Rejected")
        except AgentCanceled:
            raise DBusError("org.bluez.Error.Canceled", "Canceled")

    def _addr(self, device: str) -> str:
        return self.backend.address_of(device)

    @method()
    def Release(self):
        log.info("Bluetooth agent released by BlueZ")

    @method()
    async def RequestPinCode(self, device: "o") -> "s":
        return str(await self._ask(AgentRequest("request_pin", self._addr(device))))

    @method()
    async def DisplayPinCode(self, device: "o", pincode: "s"):
        await self._ask(AgentRequest("display_pin", self._addr(device), pin=pincode))

    @method()
    async def RequestPasskey(self, device: "o") -> "u":
        return int(await self._ask(AgentRequest("request_passkey", self._addr(device))))

    @method()
    async def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        await self._ask(AgentRequest("display_passkey", self._addr(device), passkey=passkey, entered=entered))

    @method()
    async def RequestConfirmation(self, device: "o", passkey: "u"):
        await self._ask(AgentRequest("request_confirmation", self._addr(device), passkey=passkey))

    @method()
    async def RequestAuthorization(self, device: "o"):
        await self._ask(AgentRequest("request_authorization", self._addr(device)))

    @method()
    async def AuthorizeService(self, device: "o", uuid: "s"):
        await self._ask(AgentRequest("authorize_service", self._addr(device), uuid=uuid))

    @method()
    async def Cancel(self):
        await self._ask(AgentRequest("cancel"))


class BluezBackend(Backend):
    name = "bluez"

    def __init__(self, bus_address: str | None = None):
        super().__init__()
        self.bus_address = bus_address
        self.bus: MessageBus | None = None
        self.objects: dict[str, dict[str, dict]] = {}
        self.adapter_path: str | None = None
        self.owner: str | None = None
        self.agent_registered = False
        self._ready = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._up_task: asyncio.Task | None = None

    # -- connection --------------------------------------------------------------------
    async def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._supervise(), name="bluez")
        try:
            await asyncio.wait_for(self._ready.wait(), 5.0)
        except asyncio.TimeoutError:
            log.info("BlueZ is not answering yet")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self.bus:
            try:
                if self.owner and self.agent_registered:
                    await asyncio.wait_for(self._call("/org/bluez", AGENT_MANAGER, "UnregisterAgent", "o",
                                                      [AGENT_PATH]), 2.0)
            except Exception:
                pass
            self.bus.disconnect()
            self.bus = None

    async def _supervise(self) -> None:
        delay = 1.0
        while True:
            try:
                await self._connect()
                delay = 1.0
                self.error = None
                await self.bus.wait_for_disconnect()
                log.warning("system bus connection lost")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                log.info("BlueZ connection: %s", self.error)
            self._down()
            self.bus = None
            self._ready.set()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _connect(self) -> None:
        bus = MessageBus(bus_address=self.bus_address, bus_type=BusType.SYSTEM)
        self.bus = await bus.connect()
        self.bus.add_message_handler(self._on_message)
        for rule in ("type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',"
                     "member='NameOwnerChanged',arg0='org.bluez'",
                     "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.ObjectManager'",
                     "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties',"
                     "member='PropertiesChanged'"):
            await self._bus_call("AddMatch", "s", [rule])
        self.bus.export(AGENT_PATH, Agent(self))
        try:
            body = await self._bus_call("GetNameOwner", "s", [BLUEZ])
            owner = body[0] if body else None
        except BluetoothError:
            owner = None
        if owner:
            await self._up(owner)
        self._ready.set()

    async def _bus_call(self, member: str, signature: str, body: list) -> list:
        reply = await self.bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                            interface="org.freedesktop.DBus", member=member,
                                            signature=signature, body=body))
        if reply.message_type == MessageType.ERROR:
            raise map_error(reply.error_name, reply.body[0] if reply.body else "")
        return reply.body

    async def _up(self, owner: str) -> None:
        self.owner = owner
        body = await self._call("/", OBJECT_MANAGER, "GetManagedObjects")
        self.objects = {path: {iface: dict(props) for iface, props in ifaces.items()}
                        for path, ifaces in unpack_variants(body[0]).items()}
        self._pick_adapter()
        self.agent_registered = False
        try:
            await self._call("/org/bluez", AGENT_MANAGER, "RegisterAgent", "os", [AGENT_PATH, CAPABILITY])
        except BluetoothError as exc:
            if exc.code != "already_paired":     # AlreadyExists: ours from before
                raise
        await self._call("/org/bluez", AGENT_MANAGER, "RequestDefaultAgent", "o", [AGENT_PATH])
        self.agent_registered = True
        if self.adapter_path:
            self.generation += 1        # the service powers it again if bluetoothd left it off
        log.info("BlueZ connected (%s), adapter %s", owner, self.adapter_path)
        self.changed()

    def _down(self) -> None:
        if self.owner or self.objects:
            self.owner = None
            self.objects = {}
            self.adapter_path = None
            self.agent_registered = False
            self.changed()

    def _pick_adapter(self) -> None:
        adapters = sorted(p for p, i in self.objects.items() if ADAPTER in i)
        self.adapter_path = adapters[0] if adapters else None

    def _on_message(self, msg: Message):
        if msg.message_type != MessageType.SIGNAL:
            return None
        try:
            if msg.member == "NameOwnerChanged" and msg.body and msg.body[0] == BLUEZ:
                new = msg.body[2]
                if new:
                    self._up_task = asyncio.get_running_loop().create_task(self._up_safe(new))
                else:
                    log.warning("bluetoothd stopped")
                    self._down()
            elif msg.interface == OBJECT_MANAGER and msg.member == "InterfacesAdded":
                path, ifaces = msg.body[0], unpack_variants(msg.body[1])
                cur = self.objects.setdefault(path, {})
                for iface, props in ifaces.items():
                    cur.setdefault(iface, {}).update(props)
                if ADAPTER in ifaces and self.adapter_path is None:
                    self._pick_adapter()
                    if self.adapter_path:
                        self.generation += 1
                self.changed()
            elif msg.interface == OBJECT_MANAGER and msg.member == "InterfacesRemoved":
                path, gone = msg.body[0], msg.body[1]
                cur = self.objects.get(path, {})
                for iface in gone:
                    cur.pop(iface, None)
                if not cur:
                    self.objects.pop(path, None)
                if path == self.adapter_path and ADAPTER not in cur:
                    self.adapter_path = None
                    self._pick_adapter()
                self.changed()
            elif msg.interface == PROPERTIES and msg.member == "PropertiesChanged":
                iface, changed, invalidated = msg.body[0], unpack_variants(msg.body[1]), msg.body[2]
                if msg.path in self.objects or iface in (ADAPTER, DEVICE, BATTERY):
                    props = self.objects.setdefault(msg.path, {}).setdefault(iface, {})
                    props.update(changed)
                    for k in invalidated:
                        props.pop(k, None)
                    self.changed()
        except Exception:
            log.exception("BlueZ signal not understood")
        return None

    async def _up_safe(self, owner: str) -> None:
        try:
            await asyncio.sleep(0.2)
            await self._up(owner)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("BlueZ came back but could not be read: %s", self.error)

    async def _call(self, path: str, iface: str, member: str, signature: str = "", body: list | None = None,
                    destination: str = BLUEZ) -> list:
        if self.bus is None:
            raise BluetoothError("unavailable", "no system bus")
        reply = await self.bus.call(Message(destination=destination, path=path, interface=iface, member=member,
                                            signature=signature, body=body or []))
        if reply is None:
            return []
        if reply.message_type == MessageType.ERROR:
            raise map_error(reply.error_name, reply.body[0] if reply.body else "")
        return reply.body

    # -- state ---------------------------------------------------------------------------
    def address_of(self, path: str) -> str:
        props = self.objects.get(path, {}).get(DEVICE, {})
        return str(props.get("Address") or device_path_address(path)).upper()

    def adapter(self) -> dict | None:
        if not self.owner or not self.adapter_path:
            return None
        a = self.objects.get(self.adapter_path, {}).get(ADAPTER)
        if a is None:
            return None
        return {"address": a.get("Address"), "name": a.get("Alias") or a.get("Name"),
                "powered": bool(a.get("Powered")), "discovering": bool(a.get("Discovering")),
                "power_state": a.get("PowerState"), "path": self.adapter_path}

    def devices(self) -> list[dict]:
        if not self.adapter_path:
            return []
        out = []
        for path, ifaces in self.objects.items():
            props = ifaces.get(DEVICE)
            if not props or props.get("Adapter") != self.adapter_path:
                continue
            d = dict(props)
            bat = ifaces.get(BATTERY, {}).get("Percentage")
            if bat is not None:
                d["Battery"] = int(bat)
            d["_path"] = path
            out.append(d)
        return out

    def _device_path(self, address: str) -> str:
        for path, ifaces in self.objects.items():
            props = ifaces.get(DEVICE)
            if props and str(props.get("Address", "")).upper() == address and \
                    props.get("Adapter") == self.adapter_path:
                return path
        if not self.adapter_path:
            raise BluetoothError("no_bluetooth")
        raise BluetoothError("not_found")

    def _adapter_path(self) -> str:
        if not self.adapter_path:
            raise BluetoothError("no_bluetooth")
        return self.adapter_path

    # -- operations ----------------------------------------------------------------------
    async def set_powered(self, on: bool) -> None:
        await self._call(self._adapter_path(), PROPERTIES, "Set", "ssv", [ADAPTER, "Powered", Variant("b", on)])

    async def start_discovery(self) -> None:
        path = self._adapter_path()
        flt = {"Transport": Variant("s", "auto"), "DuplicateData": Variant("b", False),
               "Discoverable": Variant("b", True)}
        try:
            await self._call(path, ADAPTER, "SetDiscoveryFilter", "a{sv}", [flt])
        except BluetoothError as exc:
            log.info("discovery filter not accepted: %s", exc.detail)
        try:
            await self._call(path, ADAPTER, "StartDiscovery")
        except BluetoothError as exc:
            if exc.code != "busy":           # InProgress: already discovering for us
                raise

    async def stop_discovery(self) -> None:
        try:
            await self._call(self._adapter_path(), ADAPTER, "StopDiscovery")
        except BluetoothError as exc:
            log.info("stop discovery: %s", exc.detail)

    async def pair(self, address: str) -> None:
        await self._call(self._device_path(address), DEVICE, "Pair")

    async def cancel_pairing(self, address: str) -> None:
        try:
            await self._call(self._device_path(address), DEVICE, "CancelPairing")
        except BluetoothError as exc:
            log.info("cancel pairing: %s", exc.detail)

    async def set_trusted(self, address: str, trusted: bool) -> None:
        await self._call(self._device_path(address), PROPERTIES, "Set", "ssv",
                         [DEVICE, "Trusted", Variant("b", trusted)])

    async def connect(self, address: str) -> None:
        try:
            await self._call(self._device_path(address), DEVICE, "Connect")
        except BluetoothError as exc:
            if exc.code != "already_connected":
                raise

    async def disconnect(self, address: str) -> None:
        try:
            await self._call(self._device_path(address), DEVICE, "Disconnect")
        except BluetoothError as exc:
            if exc.code != "not_connected":
                raise

    async def remove(self, address: str) -> None:
        await self._call(self._adapter_path(), ADAPTER, "RemoveDevice", "o", [self._device_path(address)])

    def describe(self) -> dict:
        return {"backend": self.name, "connected": self.owner is not None, "agent": self.agent_registered,
                "adapter": self.adapter_path, "error": self.error}
