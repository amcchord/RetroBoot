"""Bluetooth keyboards, mice, trackpads and game controllers (ARCHITECTURE section 15).

hostd is the BlueZ client: it powers the adapter with the ``bluetooth.enabled``
setting, scans for input devices, pairs, trusts and connects them, forgets
them, and registers the BlueZ pairing agent that shows a keyboard's code on
the classic panel (or on the web page that started the pairing).  Paired
devices then reach Mac OS like USB ones: BlueZ creates an ordinary evdev
device (hidp for classic HID, uhid for HID over GATT) and rb-presenter picks
it up through libinput's udev seat.

* :class:`Bluetooth` is the service shared by the panel and the web API.
* Backends answer the D-Bus questions: :class:`FakeBackend` (tests, development,
  ``hostd.json`` ``bluetooth_backend: "fake"``) here, ``bluez.BluezBackend``
  (dbus-fast, the real one) in :mod:`retroboot.bluez`.
* The agent policy lives here so both backends share it: a code is shown only
  for a pairing RetroBoot started, an unsolicited pairing is refused, and a
  passkey or PIN is never logged (it is added to the redactor while shown).
* The setup assistant never covers the Mac OS startup. With no keyboard
  attached it waits until Mac OS has reached the Finder (or is stopped), or
  for first-time setup and the installer, and then only offers: "No keyboard
  is connected. Pair a Bluetooth keyboard?". It scans and pairs only when the
  person accepts, except with neither a keyboard nor a pointer, when it looks
  for one and pairs it by itself, as a Mac's Bluetooth Setup Assistant does.
  ``bluetooth.offer_keyboard_setup`` turns it off; a dismissed offer stays away
  until the next boot or a keyboard change.

The pairing database (``/var/lib/bluetooth``) is a bind mount of
``/var/lib/retroboot/bluetooth`` on RBDATA, so pairings survive A/B updates.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .util import mono

if TYPE_CHECKING:  # pragma: no cover
    from .hostd import Hostd
    from .jobs import Job

log = logging.getLogger("retroboot.bluetooth")

HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"    # classic HID
HOGP_UUID = "00001812-0000-1000-8000-00805f9b34fb"   # HID over GATT (Bluetooth LE)
ADDRESS_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")

KIND_LABELS = {"keyboard": "Keyboard", "mouse": "Mouse", "trackpad": "Trackpad", "combo": "Keyboard and Mouse",
               "gamepad": "Game Controller", "tablet": "Graphics Tablet", "other": "Input Device"}
POINTER_KINDS = ("mouse", "trackpad", "tablet")
KEYBOARD_KINDS = ("keyboard", "combo")
CODE_STAGES = ("passkey", "pin", "confirm")


class BluetoothError(Exception):
    """A failed Bluetooth operation; ``code`` is one of the CODES keys."""

    def __init__(self, code: str = "failed", detail: str | None = None):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


class AgentRejected(Exception):
    """The agent refuses a request (BlueZ org.bluez.Error.Rejected)."""


class AgentCanceled(Exception):
    """The agent cancels a request (BlueZ org.bluez.Error.Canceled)."""


def message_for(code: str, name: str = "the device") -> str:
    """The sentence people see for an error code."""
    q = f"“{name}”" if name != "the device" else name
    return {
        "no_bluetooth": "This computer has no Bluetooth hardware.",
        "off": "Bluetooth is off. Turn it on, then try again.",
        "blocked": "Bluetooth is turned off with this computer's wireless switch or key.",
        "busy": "RetroBoot is already pairing a device. Wait for it to finish, then try again.",
        "not_found": f"RetroBoot can't find {q} any more. Make sure it is on and in pairing mode, then click Scan Again.",
        "auth_failed": f"Pairing with {q} didn't succeed. If it asked for a code, the code may have been mistyped. "
                       "Try again.",
        "canceled": "Pairing was canceled.",
        "rejected": f"{q[0].upper() + q[1:]} turned down the request to pair.",
        "timeout": f"{q[0].upper() + q[1:]} didn't answer in time. If a code was shown, type it on the device, "
                   "then press Return.",
        "unreachable": f"RetroBoot couldn't reach {q}. Make sure it is turned on, nearby and in pairing mode, "
                       "then try again.",
        "unsupported": f"{q[0].upper() + q[1:]} asks for a code from its own screen, which RetroBoot can't enter.",
        "not_paired": f"{q[0].upper() + q[1:]} isn't paired with this computer.",
        "connect_failed": f"RetroBoot couldn't connect to {q}. Turn it off and on again, or press a key or "
                          "click it to wake it.",
        "unavailable": "Bluetooth isn't working right now. Restart RetroBoot to try again.",
    }.get(code, f"Pairing with {q} didn't work. Try again.")


# --- classification ------------------------------------------------------------

def classify(props: dict) -> str | None:
    """The input kind of a BlueZ device from its Class of Device, GAP Appearance,
    icon, services and name; None when it is not an input device."""
    name = str(props.get("Name") or props.get("Alias") or "").lower()
    cls = props.get("Class")
    appearance = props.get("Appearance")
    icon = str(props.get("Icon") or "")
    uuids = {str(u).lower() for u in props.get("UUIDs") or []}
    kind = None
    if isinstance(appearance, int) and (appearance >> 6) == 0x0F:   # Human Interface Device category
        kind = {1: "keyboard", 2: "mouse", 3: "gamepad", 4: "gamepad", 5: "tablet", 7: "tablet",
                9: "trackpad"}.get(appearance & 0x3F, "other")
    if kind in (None, "other") and isinstance(cls, int) and ((cls >> 8) & 0x1F) == 0x05:   # Peripheral
        minor, sub = (cls >> 6) & 0x03, (cls >> 2) & 0x0F
        if minor == 1:
            kind = "keyboard"
        elif minor == 2:
            kind = "tablet" if sub == 5 else "mouse"
        elif minor == 3:
            kind = "combo"
        elif sub in (1, 2):
            kind = "gamepad"
        elif sub == 5:
            kind = "tablet"
        else:
            kind = kind or "other"
    if kind is None:
        kind = {"input-keyboard": "keyboard", "input-mouse": "mouse", "input-gaming": "gamepad",
                "input-tablet": "tablet"}.get(icon)
    if kind is None and (HID_UUID in uuids or HOGP_UUID in uuids):
        kind = "other"
    if kind in ("mouse", "other") and re.search(r"track ?pad|touch ?pad", name):
        kind = "trackpad"
    if kind == "other" and re.search(r"keyboard", name):
        kind = "keyboard"
    return kind


def signal_bars(rssi: Any) -> int | None:
    try:
        r = int(rssi)
    except (TypeError, ValueError):
        return None
    return 4 if r >= -60 else 3 if r >= -70 else 2 if r >= -80 else 1


def norm_address(address: Any) -> str:
    a = str(address or "").strip().upper().replace("-", ":").replace("_", ":")
    if not ADDRESS_RE.fullmatch(a):
        raise BluetoothError("not_found", "not a Bluetooth address")
    return a


# --- host facts: rfkill, input devices, batteries ---------------------------------

def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


class Rfkill:
    """The Bluetooth radio switches in /sys/class/rfkill (soft: software, hard: a switch)."""

    def __init__(self, sysfs: Path = Path("/sys")):
        self.base = Path(sysfs) / "class" / "rfkill"

    def entries(self) -> list[Path]:
        try:
            return [d for d in sorted(self.base.iterdir()) if _read(d / "type") == "bluetooth"]
        except OSError:
            return []

    def state(self) -> dict:
        soft = hard = False
        entries = self.entries()
        for d in entries:
            soft = soft or _read(d / "soft") == "1"
            hard = hard or _read(d / "hard") == "1"
        return {"present": bool(entries), "soft": soft, "hard": hard}

    def set_soft(self, blocked: bool) -> bool:
        ok = True
        for d in self.entries():
            try:
                if _read(d / "soft") != ("1" if blocked else "0"):
                    (d / "soft").write_text("1" if blocked else "0")
            except OSError as exc:
                log.warning("rfkill %s: %s", d.name, type(exc).__name__)
                ok = False
        return ok


def _bits(text: str | None) -> int:
    """A sysfs capability bitmap (hex longs, most significant first)."""
    v = 0
    for w in (text or "").split():
        try:
            v = (v << 64) | int(w, 16)
        except ValueError:
            return 0
    return v


def _has(bits: int, *nums: int) -> bool:
    return all(bits >> n & 1 for n in nums)


KEY_ENTER, KEY_A, KEY_Z, KEY_SPACE = 28, 30, 44, 57
BTN_LEFT, BTN_TOOL_FINGER, BTN_TOUCH = 0x110, 0x145, 0x14A
EV_KEY, EV_REL, EV_ABS = 1, 2, 3
BUS_USB, BUS_BLUETOOTH, BUS_I8042 = 0x03, 0x05, 0x11


class InputDevices:
    """Keyboards and pointers the kernel has, read from /sys/class/input."""

    def __init__(self, sysfs: Path = Path("/sys")):
        self.base = Path(sysfs) / "class" / "input"

    def scan(self) -> list[dict]:
        out = []
        try:
            nodes = sorted(d for d in self.base.iterdir() if re.fullmatch(r"input\d+", d.name))
        except OSError:
            return out
        for d in nodes:
            caps = d / "capabilities"
            ev, key = _bits(_read(caps / "ev")), _bits(_read(caps / "key"))
            rel, ab = _bits(_read(caps / "rel")), _bits(_read(caps / "abs"))
            try:
                bus = int(_read(d / "id" / "bustype") or "0", 16)
            except ValueError:
                bus = 0
            keyboard = _has(ev, EV_KEY) and _has(key, KEY_ENTER, KEY_A, KEY_Z, KEY_SPACE)
            pointer = (_has(ev, EV_REL) and _has(rel, 0, 1) and _has(key, BTN_LEFT)) or \
                      (_has(ev, EV_ABS) and _has(ab, 0, 1) and (_has(key, BTN_TOUCH) or _has(key, BTN_LEFT)
                                                                or _has(key, BTN_TOOL_FINGER)))
            out.append({"name": _read(d / "name") or "", "bus": bus, "uniq": (_read(d / "uniq") or "").upper(),
                        "keyboard": keyboard, "pointer": pointer})
        return out

    def presence(self, count_builtin: bool = True, devs: list[dict] | None = None) -> tuple[bool, bool]:
        """(keyboard, pointer) present. ``count_builtin`` False ignores i8042 devices:
        desktop boards often list a keyboard controller with nothing plugged in."""
        kb = ptr = False
        for d in (self.scan() if devs is None else devs):
            if d["bus"] == BUS_I8042 and not count_builtin:
                continue
            kb = kb or d["keyboard"]
            ptr = ptr or d["pointer"]
        return kb, ptr

    @staticmethod
    def keyboards(devs: list[dict], count_builtin: bool = True) -> tuple[str, ...]:
        """The attached keyboards, one string each: when this changes, a keyboard came or went."""
        return tuple(sorted(f"{d['bus']:04x}:{d['name']}:{d['uniq']}" for d in devs
                            if d["keyboard"] and (count_builtin or d["bus"] != BUS_I8042)))

    def bluetooth_inputs(self) -> dict[str, list[str]]:
        """Input devices made for Bluetooth devices, by remote address (hidp and
        uhid put the address in ``uniq``)."""
        out: dict[str, list[str]] = {}
        for d in self.scan():
            if d["uniq"] and (d["bus"] == BUS_BLUETOOTH or ADDRESS_RE.fullmatch(d["uniq"])):
                out.setdefault(d["uniq"], []).append(d["name"])
        return out


def stored_pairings(storage: Path) -> int:
    """Devices BlueZ keeps keys for (``<adapter>/<device>/info`` under /var/lib/bluetooth):
    the installer carries them to the installed system (install-to-disk --bluetooth)."""
    n = 0
    try:
        for adapter in Path(storage).iterdir():
            if not ADDRESS_RE.fullmatch(adapter.name.upper()) or not adapter.is_dir():
                continue
            for dev in adapter.iterdir():
                if ADDRESS_RE.fullmatch(dev.name.upper()) and (dev / "info").is_file():
                    n += 1
    except OSError:
        pass
    return n


def sysfs_battery(sysfs: Path, address: str) -> int | None:
    """A classic HID device's battery as the kernel reports it (hid-<addr>-battery)."""
    v = _read(Path(sysfs) / "class" / "power_supply" / f"hid-{address.lower()}-battery" / "capacity")
    try:
        return max(0, min(100, int(v))) if v is not None else None
    except ValueError:
        return None


# --- backends ------------------------------------------------------------------------

@dataclass
class AgentRequest:
    """One BlueZ Agent1 call, in RetroBoot's terms."""
    method: str            # request_pin display_pin request_passkey display_passkey request_confirmation
    #                        request_authorization authorize_service cancel release
    address: str = ""
    passkey: int | None = None
    entered: int | None = None
    pin: str | None = None
    uuid: str | None = None


AgentHandler = Callable[[AgentRequest], Awaitable[Any]]


class Backend:
    """What the service needs from BlueZ. Device dicts use BlueZ's Device1 property
    names (Address, Name, Alias, Class, Appearance, Icon, UUIDs, Paired, Bonded,
    Trusted, Connected, Blocked, LegacyPairing, RSSI, AddressType) plus Battery
    (Battery1.Percentage)."""

    name = "none"

    def __init__(self) -> None:
        self.on_change: Callable[[], None] | None = None
        self.agent: AgentHandler | None = None
        self.error: str | None = None
        self.generation = 0         # +1 each time bluetoothd (re)appears with an adapter

    def changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # pragma: no cover
                log.exception("bluetooth change listener failed")

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def adapter(self) -> dict | None:
        """{"address", "name", "powered", "discovering"} or None without an adapter."""
        return None

    def devices(self) -> list[dict]:
        return []

    async def set_powered(self, on: bool) -> None:
        raise BluetoothError("no_bluetooth")

    async def start_discovery(self) -> None:
        raise BluetoothError("no_bluetooth")

    async def stop_discovery(self) -> None:
        pass

    async def pair(self, address: str) -> None:
        raise BluetoothError("no_bluetooth")

    async def cancel_pairing(self, address: str) -> None:
        pass

    async def set_trusted(self, address: str, trusted: bool) -> None:
        raise BluetoothError("no_bluetooth")

    async def connect(self, address: str) -> None:
        raise BluetoothError("no_bluetooth")

    async def disconnect(self, address: str) -> None:
        raise BluetoothError("no_bluetooth")

    async def remove(self, address: str) -> None:
        raise BluetoothError("no_bluetooth")

    def describe(self) -> dict:
        return {"backend": self.name, "error": self.error}


@dataclass
class FakeDevice:
    """A simulated Bluetooth device. ``method`` is how it pairs: passkey (a keyboard
    that types the code shown), pin (legacy: fixed ``pin`` for a mouse, or the
    shown PIN typed on a keyboard), justworks, confirm (numeric comparison),
    unreachable, lecancel (an LE connection that fails: BlueZ says "canceled"), reject."""
    address: str
    name: str
    cls: int | None = None
    appearance: int | None = None
    method: str = "justworks"
    pin: str | None = None
    battery: int | None = None
    rssi: int = -55
    discoverable: bool = True      # in pairing mode
    on: bool = True                # in range and switched on
    paired: bool = False
    trusted: bool = False
    connected: bool = False
    le: bool = False
    hid: bool = True                   # offers the HID service (False: a phone, headphones)
    auto_type_s: float | None = None   # passkey/pin keyboards: "type" the code after this long
    typo: bool = False                 # ...but get it wrong


class FakeBackend(Backend):
    """Deterministic BlueZ for tests and development (``bluetooth_backend: fake``)."""

    name = "fake"

    def __init__(self, devices: list[FakeDevice] | None = None, adapter: bool = True, step_s: float = 0.05):
        super().__init__()
        self.has_adapter = adapter
        self.powered = False
        self.discovering = False
        self.step = step_s
        self.devs: dict[str, FakeDevice] = {d.address: d for d in (devices or [])}
        self.known: set[str] = set()        # seen in a scan: BlueZ keeps such devices for a while
        self.calls: list[tuple] = []
        self._typed: dict[str, asyncio.Future] = {}
        self._cancel: dict[str, asyncio.Event] = {}
        self.fail_power: str | None = None   # error code for set_powered (e.g. "blocked")

    @classmethod
    def demo(cls) -> "FakeBackend":
        """A keyboard that types its code after 4 s, a mouse, a legacy keyboard,
        a game controller and a paired trackpad (development and screenshots)."""
        return cls([
            FakeDevice("C8:2B:96:1A:4E:01", "Bluetooth Keyboard", cls=0x002540, method="passkey", auto_type_s=4.0,
                       battery=90),
            FakeDevice("C8:2B:96:1A:4E:02", "Wireless Mouse", appearance=0x03C2, method="justworks", le=True,
                       battery=64, rssi=-68),
            FakeDevice("C8:2B:96:1A:4E:03", "Old Keyboard", cls=0x000540, method="pin", auto_type_s=4.0, rssi=-77),
            FakeDevice("C8:2B:96:1A:4E:04", "Game Controller", cls=0x000508, method="justworks", rssi=-82),
            FakeDevice("C8:2B:96:1A:4E:05", "Magic Trackpad", cls=0x002580, method="justworks", paired=True,
                       trusted=True, connected=True, discoverable=False, battery=41),
        ])

    # -- state ------------------------------------------------------------------
    def adapter(self) -> dict | None:
        if not self.has_adapter:
            return None
        return {"address": "00:1A:7D:DA:71:13", "name": "retroboot", "powered": self.powered,
                "discovering": self.discovering}

    def devices(self) -> list[dict]:
        out = []
        if not self.has_adapter:
            return out
        for d in self.devs.values():
            seen = self.discovering and self.powered and d.on and d.discoverable and not d.paired
            if seen:
                self.known.add(d.address)
            if not (d.paired or seen or d.connected or d.address in self.known):
                continue
            p = {"Address": d.address, "Name": d.name, "Alias": d.name, "Paired": d.paired, "Bonded": d.paired,
                 "Trusted": d.trusted, "Connected": d.connected, "Blocked": False,
                 "LegacyPairing": d.method == "pin", "AddressType": "random" if d.le else "public",
                 "UUIDs": [HOGP_UUID if d.le else HID_UUID] if d.hid else []}
            if d.cls is not None:
                p["Class"] = d.cls
            if d.appearance is not None:
                p["Appearance"] = d.appearance
            if seen:
                p["RSSI"] = d.rssi
            if d.battery is not None and d.connected:
                p["Battery"] = d.battery
            out.append(p)
        return out

    def _dev(self, address: str) -> FakeDevice:
        d = self.devs.get(address)
        if d is None:
            raise BluetoothError("not_found")
        return d

    # -- operations -------------------------------------------------------------------
    async def set_powered(self, on: bool) -> None:
        self.calls.append(("power", on))
        if self.fail_power:
            raise BluetoothError(self.fail_power)
        self.powered = on
        if not on:
            self.discovering = False
            for d in self.devs.values():
                d.connected = False
        self.changed()

    async def start_discovery(self) -> None:
        self.calls.append(("discovery", True))
        if not self.powered:
            raise BluetoothError("off")
        self.discovering = True
        self.changed()

    async def stop_discovery(self) -> None:
        self.calls.append(("discovery", False))
        self.discovering = False
        self.changed()

    async def _ask(self, req: AgentRequest) -> Any:
        if self.agent is None:
            raise BluetoothError("auth_failed", "no agent")
        try:
            return await self.agent(req)
        except AgentRejected:
            raise BluetoothError("rejected")
        except AgentCanceled:
            raise BluetoothError("canceled")

    def type_code(self, address: str, code: str) -> None:
        """The person types ``code`` on the device and presses Return."""
        fut = self._typed.get(address)
        if fut and not fut.done():
            fut.set_result(code)

    async def _typing(self, d: FakeDevice, code: str) -> str:
        fut = asyncio.get_running_loop().create_future()
        self._typed[d.address] = fut
        if d.auto_type_s is not None:
            async def typist():
                for i in range(1, len(code) + 1):
                    await asyncio.sleep(d.auto_type_s / (len(code) + 1))
                    if d.method == "passkey" and not fut.done():
                        await self._ask(AgentRequest("display_passkey", d.address, passkey=int(code), entered=i))
                await asyncio.sleep(d.auto_type_s / (len(code) + 1))
                self.type_code(d.address, ("9" + code[1:]) if d.typo else code)
            asyncio.get_running_loop().create_task(typist())
        cancel = self._cancel[d.address]
        waiter = asyncio.ensure_future(cancel.wait())
        try:
            done, _ = await asyncio.wait({fut, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if fut not in done:
            raise BluetoothError("canceled")
        return fut.result()

    async def pair(self, address: str) -> None:
        self.calls.append(("pair", address))
        d = self._dev(address)
        if not self.powered:
            raise BluetoothError("off")
        if d.paired:
            return
        self._cancel[address] = asyncio.Event()
        await asyncio.sleep(self.step)
        if not d.on or d.method == "unreachable":
            await asyncio.sleep(self.step * 4)
            raise BluetoothError("unreachable", "Page Timeout")
        if d.method == "lecancel":      # BlueZ's word for a failed LE connection
            await asyncio.sleep(self.step * 4)
            raise BluetoothError("canceled", "AuthenticationCanceled: Authentication Canceled")
        if not d.discoverable:
            raise BluetoothError("rejected")
        if d.method == "reject":
            raise BluetoothError("rejected")
        if d.method == "passkey":
            code = f"{secrets.randbelow(1_000_000):06d}"
            await self._ask(AgentRequest("display_passkey", address, passkey=int(code), entered=0))
            typed = await self._typing(d, code)
            if typed != code:
                raise BluetoothError("auth_failed")
        elif d.method == "pin":
            pin = await self._ask(AgentRequest("request_pin", address))
            if d.pin is not None:
                if pin != d.pin:
                    raise BluetoothError("auth_failed")
            elif await self._typing(d, str(pin)) != pin:
                raise BluetoothError("auth_failed")
        elif d.method == "confirm":
            await self._ask(AgentRequest("request_confirmation", address, passkey=secrets.randbelow(1_000_000)))
        elif d.method == "display":
            await self._ask(AgentRequest("request_passkey", address))
        d.paired = True
        d.discoverable = False
        if d.le:
            d.connected = True     # LE pairing happens on a connection
        self.changed()

    async def cancel_pairing(self, address: str) -> None:
        self.calls.append(("cancel", address))
        ev = self._cancel.get(address)
        if ev:
            ev.set()

    async def set_trusted(self, address: str, trusted: bool) -> None:
        self.calls.append(("trust", address, trusted))
        self._dev(address).trusted = trusted
        self.changed()

    async def connect(self, address: str) -> None:
        self.calls.append(("connect", address))
        d = self._dev(address)
        if not self.powered:
            raise BluetoothError("off")
        await asyncio.sleep(self.step)
        if not d.on:
            await asyncio.sleep(self.step * 4)
            raise BluetoothError("connect_failed", "Host is down")
        if not d.paired:
            raise BluetoothError("not_paired")
        d.connected = True
        self.changed()

    async def disconnect(self, address: str) -> None:
        self.calls.append(("disconnect", address))
        self._dev(address).connected = False
        self.changed()

    async def remove(self, address: str) -> None:
        self.calls.append(("remove", address))
        d = self._dev(address)
        d.paired = d.trusted = d.connected = False
        self.known.discard(address)
        self.changed()

    # -- the device's side (tests) --------------------------------------------------------
    def device_reconnects(self, address: str) -> None:
        """A paired device switched on again connects by itself (BlueZ accepts trusted devices)."""
        d = self._dev(address)
        d.on = True
        if self.powered and d.paired:
            d.connected = True
            self.changed()

    def device_off(self, address: str) -> None:
        d = self._dev(address)
        d.on = False
        d.connected = False
        self.changed()

    def daemon_restarted(self) -> None:
        """bluetoothd restarted (seen on the GPD): the adapter is back at once but
        unpowered, and nothing is connected."""
        self.powered = self.discovering = False
        for d in self.devs.values():
            d.connected = False
        self.generation += 1
        self.changed()


def make_backend(cfg) -> Backend:
    """hostd.json ``bluetooth_backend``: auto (BlueZ when dbus-fast is installed and host
    tools are allowed), bluez, fake (the demo devices), off."""
    choice = getattr(cfg, "bluetooth_backend", "auto")
    if choice == "fake":
        return FakeBackend.demo()
    if choice == "off" or (choice == "auto" and not cfg.allow_network_tools):
        return Backend()
    try:
        from .bluez import BluezBackend
    except ImportError as exc:      # python3-dbus-fast missing: no Bluetooth, never a crash
        b = Backend()
        b.error = f"BlueZ client unavailable ({exc.name or type(exc).__name__})"
        return b
    return BluezBackend()


# --- the service ------------------------------------------------------------------------

@dataclass
class PairingSession:
    address: str
    name: str
    kind: str
    origin: str                    # panel | web | assistant
    stage: str = "starting"        # starting pairing passkey pin confirm connecting connected failed
    passkey: str | None = None     # the 6 digits shown (passkey, PIN or comparison number)
    entered: int | None = None     # digits typed so far (classic keyboards report them)
    error: str | None = None
    code: str | None = None
    done: bool = False
    canceled: bool = False
    started: float = field(default_factory=mono)
    confirm: asyncio.Future | None = None
    job_id: str | None = None
    owner: str | None = None       # the paired browser that started a web pairing
    shown: bool = False            # a code was shown (the device then knew of the pairing)

    def public(self, show_code: bool) -> dict:
        d = {"address": self.address, "id": self.address, "name": self.name, "kind": self.kind,
             "origin": self.origin, "stage": self.stage, "done": self.done, "error": self.error,
             "code": self.code, "job": self.job_id, "canceled": self.canceled}
        if self.stage in CODE_STAGES:
            d["code_shown_on"] = "here" if show_code else ("computer" if self.origin != "web" else "web")
            if show_code:
                d["passkey"] = self.passkey
                d["entered"] = self.entered
        return d


class Bluetooth:
    """hostd's Bluetooth authority (panel, web API, setup assistant)."""

    TICK_S = 1.0
    SCAN_S = 40.0
    PAIR_TIMEOUT_S = 90.0          # a person reading and typing a code
    CONNECT_TIMEOUT_S = 20.0
    CONFIRM_TIMEOUT_S = 60.0
    FOUND_KEEP_S = 120.0           # an unpaired device stays listed this long after it was last seen
    ASSISTANT_RETRY_S = 120.0
    ASSISTANT_MAX_TRIES = 2
    ASSISTANT_STEP_S = 2.5
    ASSISTANT_SETTLE_S = 8.0       # after the assistant paired a device, before it pairs another
    OFFER_DELAY_S = 25.0           # after the Finder (or a stop): keyboards and paired ones reconnect first
    SETUP_DELAY_S = 10.0           # first-time setup and the installer
    SEARCH_LIMIT_S = 180.0         # the unattended search over the Finder gives up after this long
    REPOWER_MIN_S = 5.0            # an enabled adapter found unpowered (bluetoothd restarted)
    REPOWER_MAX_S = 300.0

    def __init__(self, app: "Hostd", backend: Backend | None = None):
        self.app = app
        cfg = app.cfg
        self.sysfs = Path(cfg.sysfs_path)
        self.backend = backend if backend is not None else make_backend(cfg)
        self.backend.on_change = self._backend_changed
        self.backend.agent = self.agent_request
        self.rfkill = Rfkill(self.sysfs)
        self.inputs = InputDevices(self.sysfs)
        self.listeners: list[Callable[[], None]] = []
        self.prompt_hook: Callable[["PairingSession"], Awaitable[None]] | None = None   # panel (assistant)
        # The panel's side of the setup assistant: show the offer or the search window
        # (stage "offer"/"search") or take it away (anything else).
        self.assistant_hook: Callable[[str | None, str | None], Awaitable[None]] | None = None
        self.session: PairingSession | None = None
        self.assistant_mode = getattr(cfg, "bluetooth_assistant", "auto")
        self.assistant = {"active": False, "looking_for": None, "stage": None, "context": None, "tries": {},
                          "paired": []}
        self._need_since = self._ctx_since = self._search_since = 0.0
        self._offer_now = False         # the assistant just paired a mouse: offer the keyboard at once
        self._assistant_pointer_at = self._assistant_paired_at = -1e9
        self._dismissed: str | None = None   # why the offer went away (until the next boot or a keyboard change)
        self._kb_sig: tuple[str, ...] | None = None
        self._gen = 0
        self._unpowered_since: float | None = None
        self._repower_at = 0.0
        self._repower_wait = 0.0
        self._leases: dict[str, float] = {}
        self._seen: dict[str, tuple[float, int]] = {}      # address -> (mono, rssi)
        self._connected: set[str] = set()
        self._task: asyncio.Task | None = None
        self._sync_lock = asyncio.Lock()
        self._disc_lock = asyncio.Lock()
        self._sweep_lock = asyncio.Lock()
        self._had_adapter = False
        self._was_powered = False
        self._clock = self._clocks()
        self._paused_discovery = False
        self._restart_scan = False
        self._last_assistant = 0.0
        self.started = False
        self.counts = {"paired": 0, "pair_failed": 0, "reconnect_attempts": 0, "reconnected": 0}

    # -- facts ---------------------------------------------------------------------
    def capable(self) -> bool:
        return self.backend.adapter() is not None

    def enabled(self) -> bool:
        return bool(self.app.settings.applied.get("bluetooth.enabled", True))

    def powered(self) -> bool:
        a = self.backend.adapter()
        return bool(a and a.get("powered"))

    def discovering(self) -> bool:
        a = self.backend.adapter()
        return bool(a and a.get("discovering"))

    def _raw(self, address: str) -> dict | None:
        for p in self.backend.devices():
            if str(p.get("Address", "")).upper() == address:
                return p
        return None

    def device(self, address: str) -> dict | None:
        for d in self.devices():
            if d["address"] == address:
                return d
        return None

    def devices(self) -> list[dict]:
        """Paired devices (connected first), then input devices nearby in pairing mode
        (strongest signal first)."""
        now = mono()
        evdev = self.inputs.bluetooth_inputs()
        sess = self.session.address if self.session and not self.session.done else None
        paired, found = [], []
        for p in self.backend.devices():
            addr = str(p.get("Address", "")).upper()
            if not ADDRESS_RE.fullmatch(addr):
                continue
            kind = classify(p)
            is_paired = bool(p.get("Paired") or p.get("Bonded"))
            rssi = p.get("RSSI")
            seen = self._seen.get(addr)
            if rssi is None and seen and now - seen[0] < self.FOUND_KEEP_S:
                rssi = seen[1]
            if not is_paired:
                if kind is None:
                    continue
                if p.get("RSSI") is None and not (seen and now - seen[0] < self.FOUND_KEEP_S) and addr != sess:
                    continue
            kind = kind or "other"
            name = str(p.get("Name") or "").strip()
            if not name:
                alias = str(p.get("Alias") or "").strip()
                name = alias if alias and not re.fullmatch(r"[0-9A-Fa-f]{2}([-:][0-9A-Fa-f]{2}){5}", alias) else \
                    f"{KIND_LABELS[kind]} {addr[-5:]}"
            battery = p.get("Battery")
            if battery is None and is_paired and p.get("Connected"):
                battery = sysfs_battery(self.sysfs, addr)
            d = {"id": addr, "address": addr, "name": name[:64], "kind": kind, "kind_label": KIND_LABELS[kind],
                 "paired": is_paired, "trusted": bool(p.get("Trusted")), "connected": bool(p.get("Connected")),
                 "input": addr in evdev, "battery": battery if isinstance(battery, int) else None,
                 "rssi": rssi if isinstance(rssi, int) else None, "signal": signal_bars(rssi) if not is_paired else None,
                 "legacy": bool(p.get("LegacyPairing")), "le": p.get("AddressType") == "random" or
                 HOGP_UUID in {str(u).lower() for u in p.get("UUIDs") or []},
                 "blocked": bool(p.get("Blocked")), "pairing": addr == sess}
            (paired if is_paired else found).append(d)
        paired.sort(key=lambda d: (not d["connected"], d["name"].lower()))
        found.sort(key=lambda d: (-(d["rssi"] if d["rssi"] is not None else -999), d["name"].lower()))
        return paired + found

    def state_word(self) -> str:
        if not self.capable():
            return "unavailable"
        rf = self.rfkill.state()
        if rf["hard"]:
            return "blocked"
        if not self.enabled():
            return "off"
        return "on" if self.powered() else "turning-on"

    def public(self, viewer: str | None = None, viewer_id: str | None = None) -> dict:
        """State for the panel (viewer "panel"), the web API ("web", with the browser's
        pairing id) and status (None). A pairing code is included only for the viewer
        whose pairing it is: the browser that started it, or the panel (which also
        shows the setup assistant's)."""
        adapter = self.backend.adapter()
        sess = None
        if self.session:
            s = self.session
            show = (viewer == "panel" and s.origin in ("panel", "assistant")) or \
                   (viewer == "web" and s.origin == "web" and (s.owner is None or s.owner == viewer_id))
            sess = s.public(show)
        rf = self.rfkill.state()
        out = {"available": adapter is not None, "state": self.state_word(), "enabled": self.enabled(),
               "powered": bool(adapter and adapter.get("powered")),
               "blocked": "hard" if rf["hard"] else "soft" if rf["soft"] else None,
               "scanning": bool(adapter and adapter.get("discovering")),
               "adapter": {"address": adapter.get("address"), "name": adapter.get("name")} if adapter else None,
               "devices": self.devices() if adapter else [], "pairing": sess,
               "assistant": {"active": self.assistant["active"], "looking_for": self.assistant["looking_for"],
                             "stage": self.assistant["stage"], "offer": self.offer_enabled()},
               "backend": self.backend.describe()}
        if adapter is None:
            out["reason"] = "This computer has no Bluetooth hardware." if not self.backend.error else \
                "Bluetooth isn't available on this RetroBoot."
        return out

    # -- change notification ---------------------------------------------------------------
    def notify(self) -> None:
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:  # pragma: no cover
                log.exception("bluetooth listener failed")

    def _backend_changed(self) -> None:
        now = mono()
        connected = set()
        for p in self.backend.devices():
            addr = str(p.get("Address", "")).upper()
            if isinstance(p.get("RSSI"), int):
                self._seen[addr] = (now, p["RSSI"])
            if p.get("Connected") and (p.get("Paired") or p.get("Bonded")):
                connected.add(addr)
        for addr in sorted(connected - self._connected):
            self.app.lifecycle.event("bluetooth_connected", address=addr)
        for addr in sorted(self._connected - connected):
            self.app.lifecycle.event("bluetooth_disconnected", address=addr)
        self._connected = connected
        self.notify()

    # -- start / stop ------------------------------------------------------------------------
    async def start(self) -> None:
        try:
            await self.backend.start()
        except Exception as exc:
            self.backend.error = f"{type(exc).__name__}"
            log.warning("Bluetooth backend did not start: %s", type(exc).__name__)
        self.started = True
        self._load_cooldown()
        self._gen = self.backend.generation
        self._had_adapter = self.capable()
        if self._had_adapter:
            self.app.lifecycle.event("bluetooth_adapter", present=True, backend=self.backend.name,
                                     address=(self.backend.adapter() or {}).get("address"))
            await self.sync_power("start")
            self._was_powered = self.powered()
            if self._was_powered:
                self._spawn(self.reconnect_sweep("start"))
        self._task = asyncio.get_running_loop().create_task(self._run(), name="bluetooth")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self.session and not self.session.done:
            await self.cancel_pairing()
        try:
            await self.backend.stop()
        except Exception:
            pass

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        task.add_done_callback(lambda t: t.cancelled() or not t.exception() or
                               log.warning("bluetooth task failed: %r", t.exception()))

    @staticmethod
    def _clocks() -> tuple[float, float] | None:
        try:
            return time.clock_gettime(time.CLOCK_BOOTTIME), time.monotonic()
        except (AttributeError, OSError):
            return None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.TICK_S)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("bluetooth tick failed")

    async def _tick(self) -> None:
        has = self.capable()
        if has != self._had_adapter:
            self._had_adapter = has
            self.app.lifecycle.event("bluetooth_adapter", present=has, backend=self.backend.name)
            if has:
                self._gen = self.backend.generation
                await self.sync_power("adapter-added")
            self.notify()
        if not has:
            self.assistant.update(active=False, looking_for=None, stage=None)
            return
        if self.backend.generation != self._gen:
            # bluetoothd restarted: it may bring the adapter back unpowered (seen on
            # the GPD after ``systemctl restart bluetooth``), and paired devices
            # can't reconnect until it is on again.
            self._gen = self.backend.generation
            self.app.lifecycle.event("bluetooth_daemon_restarted", generation=self._gen)
            self._was_powered = False       # paired devices are asked to reconnect once it is on
            await self.sync_power("bluetoothd")
        await self._keep_powered()
        powered = self.powered()
        if powered and not self._was_powered:
            self._spawn(self.reconnect_sweep("powered"))
        self._was_powered = powered
        # The computer slept (CLOCK_BOOTTIME ran ahead of CLOCK_MONOTONIC): reconnect.
        clocks = self._clocks()
        if clocks and self._clock:
            slept = (clocks[0] - self._clock[0]) - (clocks[1] - self._clock[1])
            if slept > 5:
                self.app.lifecycle.event("bluetooth_resume", slept_s=round(slept, 1))
                await self.sync_power("resume")
                self._spawn(self.reconnect_sweep("resume"))
        self._clock = clocks
        await self._assistant_step()
        await self._sync_discovery()
        if self.session and self.session.origin == "assistant" and not self.session.done \
                and self.session.stage in CODE_STAGES and self.prompt_hook:
            await self.prompt_hook(self.session)

    # -- power ------------------------------------------------------------------------------
    async def _keep_powered(self) -> None:
        """While Bluetooth is on, an adapter found unpowered is powered again (with a
        growing pause between attempts, so a failing adapter doesn't flood the log)."""
        if not (self.enabled() and not self.powered() and not self.rfkill.state()["hard"]):
            self._unpowered_since = None
            self._repower_wait = 0.0
            return
        now = mono()
        if self._unpowered_since is None:
            self._unpowered_since = now
            self._repower_at = now + self.REPOWER_MIN_S
            return
        if now < self._repower_at:
            return
        self._repower_wait = min(max(self._repower_wait * 2, self.REPOWER_MIN_S), self.REPOWER_MAX_S)
        self._repower_at = now + self._repower_wait
        await self.sync_power("repower")

    async def sync_power(self, why: str = "setting") -> dict:
        """Radio on exactly while ``bluetooth.enabled`` (software rfkill off/on with it)."""
        async with self._sync_lock:
            if not self.capable():
                return {"available": False}
            want = self.enabled()
            rf = self.rfkill.state()
            result = {"enabled": want, "blocked": "hard" if rf["hard"] else None}
            try:
                if want:
                    if rf["soft"]:
                        self.rfkill.set_soft(False)
                        await asyncio.sleep(0.3)
                    if not rf["hard"] and not self.powered():
                        await self.backend.set_powered(True)
                else:
                    self._leases.clear()
                    if self.session and not self.session.done:
                        await self.cancel_pairing()
                    if self.discovering():
                        await self.backend.stop_discovery()
                    if self.powered():
                        await self.backend.set_powered(False)
                    if rf["present"] and not rf["soft"]:
                        self.rfkill.set_soft(True)
            except BluetoothError as exc:
                result["error"] = exc.code
            result["powered"] = self.powered()
            self.app.lifecycle.event("bluetooth_power", why=why, **result)
            self.notify()
            return result

    # -- discovery ---------------------------------------------------------------------------
    def scan(self, owner: str = "panel", seconds: float | None = None, restart: bool = False) -> None:
        """Look for devices for ``seconds`` (several owners may keep a scan running).
        ``restart`` (Scan Again) starts a fresh scan: controllers report each device once
        per scan, so a device forgotten or put back in pairing mode reappears at once."""
        self._leases[owner] = mono() + (seconds or self.SCAN_S)
        if restart:
            self._restart_scan = True
        self._spawn(self._sync_discovery())

    def stop_scan(self, owner: str) -> None:
        if self._leases.pop(owner, None) is not None:
            self._spawn(self._sync_discovery())

    def scanning_for(self, owner: str) -> bool:
        return self._leases.get(owner, 0) > mono()

    async def _sync_discovery(self) -> None:
        """Discovery on exactly while someone looks and no pairing runs. Serialized, and
        every BlueZ call is bounded: a scan must never be started under a pairing (the
        kernel queues it behind the connection, which held up the code window)."""
        async with self._disc_lock:
            now = mono()
            self._leases = {k: v for k, v in self._leases.items() if v > now}

            def wanted() -> bool:
                busy = self.session is not None and not self.session.done
                return bool(self._leases) and self.enabled() and self.powered() and not busy
            try:
                if wanted() and self._restart_scan and self.discovering():
                    self._restart_scan = False
                    await asyncio.wait_for(self.backend.stop_discovery(), 10)
                    await asyncio.sleep(0.2)
                if wanted() and not self.discovering():
                    self._restart_scan = False
                    await asyncio.wait_for(self.backend.start_discovery(), 10)
                elif not wanted() and self.discovering():
                    await asyncio.wait_for(self.backend.stop_discovery(), 10)
            except asyncio.TimeoutError:
                log.info("discovery change timed out")
            except BluetoothError as exc:
                log.info("discovery: %s", exc.code)

    # -- agent policy ---------------------------------------------------------------------
    def _mine(self, address: str) -> PairingSession | None:
        s = self.session
        return s if s and not s.done and s.address == address.upper() else None

    def _show(self, sess: PairingSession, stage: str, code: str | None, entered: int | None = None) -> None:
        sess.stage, sess.passkey, sess.entered = stage, code, entered
        sess.shown = sess.shown or stage in CODE_STAGES
        if code and len(code) >= 6:
            self.app.redactor.add(code)
        self.notify()
        if sess.origin == "assistant" and stage in CODE_STAGES and self.prompt_hook:
            self._spawn(self.prompt_hook(sess))     # the code window, at once

    async def agent_request(self, req: AgentRequest) -> Any:
        """BlueZ asks the agent. Answers only for the pairing RetroBoot started."""
        addr = (req.address or "").upper()
        sess = self._mine(addr) if addr else None
        m = req.method
        if m in ("release",):
            return None
        if m == "cancel":
            s = self.session
            if s and not s.done and s.stage in CODE_STAGES:
                if s.confirm and not s.confirm.done():
                    s.confirm.set_result(False)
                self._show(s, "pairing", None)
            return None
        if m == "authorize_service":
            p = self._raw(addr) or {}
            hid = (req.uuid or "").lower() in (HID_UUID, HOGP_UUID)
            if hid and (sess or p.get("Paired") or p.get("Bonded")):
                return None
            self.app.lifecycle.event("bluetooth_refused", method=m, address=addr or None, uuid=req.uuid)
            raise AgentRejected()
        if sess is None:
            # Someone else's pairing attempt (a device asking to pair with this computer).
            self.app.lifecycle.event("bluetooth_refused", method=m, address=addr or None)
            raise AgentRejected()
        if m == "display_passkey":
            self._show(sess, "passkey", f"{int(req.passkey or 0):06d}", req.entered)
            return None
        if m == "display_pin":
            self._show(sess, "pin", req.pin, None)
            return None
        if m == "request_pin":
            if sess.kind in KEYBOARD_KINDS or (sess.kind == "other" and "keyboard" in sess.name.lower()):
                pin = f"{secrets.randbelow(1_000_000):06d}"
                self._show(sess, "pin", pin)
                return pin
            return "0000"       # legacy mice and controllers: their fixed PIN
        if m == "request_passkey":
            raise AgentRejected()   # a code from the device's own screen: not for input devices
        if m == "request_confirmation":
            self._show(sess, "confirm", f"{int(req.passkey or 0):06d}")
            if sess.origin == "assistant":
                return None         # no keyboard here to answer with; the device confirms
            sess.confirm = asyncio.get_running_loop().create_future()
            try:
                ok = await asyncio.wait_for(sess.confirm, self.CONFIRM_TIMEOUT_S)
            except asyncio.TimeoutError:
                ok = False
            if not ok:
                raise AgentRejected()
            self._show(sess, "pairing", None)
            return None
        if m == "request_authorization":
            return None             # Just Works pairing this computer asked for
        raise AgentRejected()

    def confirm(self, accept: bool) -> bool:
        """The person's answer to "Does the device show this code?"."""
        s = self.session
        if s and not s.done and s.confirm and not s.confirm.done():
            s.confirm.set_result(bool(accept))
            return True
        return False

    # -- pairing --------------------------------------------------------------------------
    def check_ready(self) -> None:
        if not self.capable():
            raise BluetoothError("no_bluetooth")
        if self.rfkill.state()["hard"]:
            raise BluetoothError("blocked")
        if not self.enabled():
            raise BluetoothError("off")

    async def pair(self, address: str, origin: str = "panel", job: "Job | None" = None,
                   owner: str | None = None) -> dict:
        address = norm_address(address)
        self.check_ready()
        if self.session and not self.session.done:
            raise BluetoothError("busy")
        dev = self.device(address)
        if dev is None:
            raise BluetoothError("not_found")
        sess = PairingSession(address, dev["name"], dev["kind"], origin, job_id=job.id if job else None,
                              owner=owner)
        self.session = sess
        name = dev["name"]
        self.app.lifecycle.event("bluetooth_pair_start", address=address, kind=dev["kind"], origin=origin,
                                 le=dev["le"], legacy=dev["legacy"])
        if job:
            job.update(None, f"Pairing with “{name}”…")
        await self._sync_discovery()          # discovery pauses while pairing
        t0 = mono()
        try:
            if not dev["paired"]:
                sess.stage = "pairing"
                self.notify()
                try:
                    await asyncio.wait_for(self.backend.pair(address), self.PAIR_TIMEOUT_S)
                except asyncio.TimeoutError:
                    try:
                        await self.backend.cancel_pairing(address)
                    except BluetoothError:
                        pass
                    raise BluetoothError("timeout")
                except BluetoothError as exc:
                    if exc.code != "already_paired":
                        raise
            self._show(sess, "connecting", None)
            if job:
                job.update(None, f"Connecting to “{name}”…")
            await self.backend.set_trusted(address, True)
            connected = await self._connect_and_wait(address)
            sess.stage = "connected"
            self.counts["paired"] += 1
            if origin == "assistant":
                self._assistant_paired_at = mono()
                if dev["kind"] in POINTER_KINDS:
                    self._assistant_pointer_at = self._assistant_paired_at
            self.app.lifecycle.event("bluetooth_paired", address=address, kind=dev["kind"], origin=origin,
                                     connected=connected, seconds=round(mono() - t0, 1))
            result = {"address": address, "name": name, "kind": dev["kind"], "connected": connected}
            if job:
                job.outcome = (f"“{name}” is connected." if connected else
                               f"“{name}” is paired. It connects when you use it.")
            return result
        except BluetoothError as exc:
            sess.stage = "failed"
            code = exc.code
            if code == "canceled" and not sess.canceled:
                # BlueZ reports a failed LE connection as AuthenticationCanceled:
                # nobody canceled. Before any code it never reached the device.
                code = "auth_failed" if sess.shown else "unreachable"
            sess.code = "canceled" if sess.canceled else code
            sess.error = message_for(sess.code, name)
            self.counts["pair_failed"] += 1
            self.app.lifecycle.event("bluetooth_pair_failed", address=address, origin=origin, code=sess.code,
                                     detail=(exc.detail or "")[:120] or None, seconds=round(mono() - t0, 1))
            raise BluetoothError(sess.code, sess.error)
        finally:
            sess.done = True
            if sess.passkey:
                # the code stays out of logs for the rest of this hostd's life
                sess.passkey = None
            if sess.confirm and not sess.confirm.done():
                sess.confirm.cancel()
            self.notify()
            self._spawn(self._sync_discovery())

    async def _connect_and_wait(self, address: str) -> bool:
        p = self._raw(address) or {}
        if not p.get("Connected"):
            try:
                await asyncio.wait_for(self.backend.connect(address), self.CONNECT_TIMEOUT_S)
            except (BluetoothError, asyncio.TimeoutError) as exc:
                log.info("connect after pairing: %s", getattr(exc, "code", "timeout"))
        deadline = mono() + 5.0
        while mono() < deadline:
            if (self._raw(address) or {}).get("Connected"):
                return True
            await asyncio.sleep(0.1)
        return bool((self._raw(address) or {}).get("Connected"))

    async def cancel_pairing(self) -> bool:
        s = self.session
        if not s or s.done:
            return False
        s.canceled = True
        if s.confirm and not s.confirm.done():
            s.confirm.set_result(False)
        try:
            await self.backend.cancel_pairing(s.address)
        except BluetoothError:
            pass
        self.notify()
        return True

    def dismiss(self) -> None:
        """Forget a finished pairing's result (the web page dismissed it)."""
        if self.session and self.session.done:
            self.session = None
            self.notify()

    async def forget(self, address: str, origin: str = "panel") -> None:
        address = norm_address(address)
        if not self.capable():
            raise BluetoothError("no_bluetooth")
        p = self._raw(address)
        if p is None:
            raise BluetoothError("not_found")
        if self._mine(address):
            await self.cancel_pairing()
        await self.backend.remove(address)
        self._seen.pop(address, None)
        self.assistant["tries"].pop(address, None)
        self._restart_scan = True
        self.app.lifecycle.event("bluetooth_forgotten", address=address, origin=origin)
        self.notify()

    async def connect(self, address: str, job: "Job | None" = None) -> dict:
        address = norm_address(address)
        self.check_ready()
        p = self._raw(address)
        if p is None:
            raise BluetoothError("not_found")
        if not (p.get("Paired") or p.get("Bonded")):
            raise BluetoothError("not_paired")
        name = (self.device(address) or {}).get("name") or address
        if job:
            job.update(None, f"Connecting to “{name}”…")
        try:
            await asyncio.wait_for(self.backend.connect(address), self.CONNECT_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise BluetoothError("connect_failed", message_for("connect_failed", name))
        except BluetoothError as exc:
            # The sentence people see, never BlueZ's ("Input/output error"); the detail
            # goes to the lifecycle log.
            code = exc.code if exc.code in ("off", "blocked", "not_paired", "busy") else "connect_failed"
            self.app.lifecycle.event("bluetooth_connect_failed", address=address, code=exc.code,
                                     detail=(exc.detail or "")[:120] or None)
            raise BluetoothError(code, message_for(code, name))
        if job:
            job.outcome = f"“{name}” is connected."
        return {"address": address, "connected": True}

    async def disconnect(self, address: str) -> None:
        address = norm_address(address)
        if self._raw(address) is None:
            raise BluetoothError("not_found")
        await self.backend.disconnect(address)

    async def reconnect_sweep(self, why: str) -> int:
        """Ask each paired classic input device that is not connected to connect (once).
        LE devices reconnect through BlueZ's own background scan."""
        if self._sweep_lock.locked():
            return 0
        async with self._sweep_lock:
            await asyncio.sleep(0.5)
            n = 0
            for d in self.devices():
                if not (d["paired"] and not d["connected"] and not d["le"] and not d["blocked"]):
                    continue
                if not (self.enabled() and self.powered()) or (self.session and not self.session.done):
                    break
                n += 1
                self.counts["reconnect_attempts"] += 1
                try:
                    await asyncio.wait_for(self.backend.connect(d["address"]), 10.0)
                    self.counts["reconnected"] += 1
                except (BluetoothError, asyncio.TimeoutError):
                    pass
            if n:
                self.app.lifecycle.event("bluetooth_reconnect_sweep", why=why, tried=n)
            return n

    # -- setup assistant -------------------------------------------------------------------------
    # It never covers the Mac OS startup: outside first-time setup and the installer
    # it waits until Mac OS has reached the Finder (or is stopped) and a keyboard is
    # still missing OFFER_DELAY_S later. With a pointer it only offers ("No keyboard
    # is connected. Pair a Bluetooth keyboard?"); the Bluetooth window then scans,
    # and the person picks the keyboard. With neither a keyboard nor a pointer it
    # looks for one and pairs it by itself, as a Mac's Bluetooth Setup Assistant
    # does, still only over the Finder or the panel. A dismissed offer stays away
    # until the next boot (the note is in /run) or a keyboard change.

    def offer_enabled(self) -> bool:
        return bool(self.app.settings.applied.get("bluetooth.offer_keyboard_setup", True))

    def _portable(self) -> bool:
        return (getattr(self.app, "hardware", None) or {}).get("form_factor") == "portable"

    def assistant_need(self, devs: list[dict] | None = None) -> str | None:
        """What is missing: "keyboard" (a pointer is attached), "keyboard-or-mouse"
        (nothing is attached) or None (a keyboard is attached, or the assistant is off)."""
        mode = self.assistant_mode
        if mode == "off" or not self.offer_enabled() or not self.capable() or not self.enabled() \
                or not self.powered():
            return None
        if mode == "always":            # lab: as if nothing were attached
            return "keyboard-or-mouse"
        if mode == "no-keyboard":       # lab: as if only a pointer were attached
            return "keyboard"
        kb, ptr = self.inputs.presence(count_builtin=self._portable(), devs=devs)
        if kb:
            return None
        return "keyboard" if ptr else "keyboard-or-mouse"

    def assistant_context(self) -> str | None:
        """Where the assistant may appear: "setup" (first-time setup or the installer),
        "finder" (Mac OS has reached the Finder), "stopped" (Mac OS isn't running and
        the panel shows) or None (Mac OS is starting, shutting down or not answering)."""
        app = self.app
        panel = getattr(app, "panel", None)
        visible = bool(panel is not None and panel.visible)
        if app.mode == "installer":
            return "setup" if visible else None
        sup = app.supervisor
        if sup.active:
            if sup.state != "running" or sup.paused or sup.responsive is False:
                return None
            settled = getattr(sup, "mac_os_up", sup.finder_seen) or sup.finder_timeout_logged or (
                not app.cfg.finder_probe and sup.content_seen and sup.t_start is not None
                and mono() - sup.t_start > app.cfg.finder_wait_timeout)
            return "finder" if settled else None
        if getattr(sup, "boot_pending", False) or not visible:
            return None
        try:
            has_machine = bool(app.machines.list())
        except OSError:
            has_machine = True
        return "stopped" if has_machine else "setup"

    def assistant_shown(self) -> str | None:
        """The assistant window the panel shows now ("offer", "search", "pairing"), if any."""
        panel = getattr(self.app, "panel", None)
        if panel is None or not panel.visible:
            return None
        return getattr(panel.screen, "assistant_screen", None)

    def _cooldown_file(self) -> Path | None:
        paths = getattr(self.app, "paths", None)
        return paths.run / "bluetooth-assistant.json" if paths is not None else None

    def _load_cooldown(self) -> None:
        f = self._cooldown_file()
        try:
            d = json.loads(f.read_text()) if f else {}
        except (OSError, ValueError):
            return
        if isinstance(d, dict) and d.get("dismissed"):
            self._dismissed = str(d["dismissed"])[:32]
            kb = d.get("keyboards")
            self._kb_sig = tuple(str(k) for k in kb) if isinstance(kb, list) else None

    def _save_cooldown(self) -> None:
        f = self._cooldown_file()
        if f is None:
            return
        try:
            if self._dismissed:
                f.write_text(json.dumps({"dismissed": self._dismissed, "keyboards": list(self._kb_sig or ())}))
            else:
                f.unlink(missing_ok=True)
        except OSError as exc:
            log.info("assistant note not saved: %s", type(exc).__name__)

    def assistant_dismiss(self, why: str = "not-now") -> None:
        """The offer (or the search) goes away until the next boot or a keyboard change:
        "not-now", "accepted" (the person went on in the Bluetooth window) or "timeout"."""
        if self._dismissed:
            return
        self._dismissed = why
        self._offer_now = False
        self.stop_scan("assistant")
        a = self.assistant
        self.app.lifecycle.event("bluetooth_assistant_dismissed", why=why, stage=a["stage"],
                                 context=a["context"])
        a["stage"] = None
        self._save_cooldown()
        self.notify()

    async def _assistant_step(self) -> None:
        now = mono()
        if now - self._last_assistant < self.ASSISTANT_STEP_S:
            return
        self._last_assistant = now
        a = self.assistant
        devs = self.inputs.scan()
        kb = self.inputs.keyboards(devs, count_builtin=self._portable())
        if kb != self._kb_sig:
            if self._dismissed and self._kb_sig is not None:
                self._dismissed = None      # a keyboard came or went: the offer may come back
                self.app.lifecycle.event("bluetooth_assistant_rearmed", why="keyboard-change")
            self._kb_sig = kb
            self._save_cooldown()
        need = self.assistant_need(devs)
        if need != a["looking_for"]:
            self._need_since = now
            if need == "keyboard" and a["looking_for"] == "keyboard-or-mouse" and \
                    now - self._assistant_pointer_at < 60:
                self._offer_now = True      # it just paired a mouse: go on with the keyboard
            a.update(active=bool(need), looking_for=need)
            self.app.lifecycle.event("bluetooth_assistant", looking_for=need)
            self.notify()
        ctx = self.assistant_context() if need else None
        if ctx != a["context"]:
            self._ctx_since = now
            a["context"] = ctx
        stage = None
        if need and not self._dismissed:
            delay = self.SETUP_DELAY_S if ctx == "setup" else self.OFFER_DELAY_S
            if ctx is None or (not self._offer_now and now - max(self._need_since, self._ctx_since) < delay):
                stage = "waiting"
            else:
                stage = "offer" if need == "keyboard" else "search"
        pairing = self.session is not None and not self.session.done
        if stage == "search" and a["stage"] == "search" and ctx == "finder" and not pairing and \
                now - self._search_since > self.SEARCH_LIMIT_S:
            self.assistant_dismiss("timeout")        # nobody came: give Mac OS its screen back
            stage = None
        if stage != a["stage"]:
            if stage == "search":
                self._search_since = now
                self._restart_scan = True
            if stage == "offer":
                self._offer_now = False
            a["stage"] = stage
            self.app.lifecycle.event("bluetooth_assistant_stage", stage=stage, looking_for=need, context=ctx)
            self.notify()
        if self.assistant_hook:
            try:
                await self.assistant_hook(stage, ctx)
            except Exception:
                log.exception("setup assistant window failed")
        shown = self.assistant_shown()
        if stage != "search" or shown not in ("search", "pairing"):
            self.stop_scan("assistant")
            return
        # The search window is up: look, and pair the first keyboard or mouse in pairing
        # mode; after a pairing, give its input device time to appear first.
        self._leases["assistant"] = now + 15.0
        if pairing or shown != "search" or now - self._assistant_paired_at < self.ASSISTANT_SETTLE_S:
            return
        kinds = KEYBOARD_KINDS + POINTER_KINDS
        tries = a["tries"]
        for d in self.devices():
            if d["paired"] or d["kind"] not in kinds or d["rssi"] is None:
                continue
            n, last = tries.get(d["address"], (0, None))
            if n >= self.ASSISTANT_MAX_TRIES or (last is not None and now - last < self.ASSISTANT_RETRY_S):
                continue
            tries[d["address"]] = (n + 1, now)
            starter = getattr(self.app, "bluetooth_job", None)
            if starter is None:
                return
            try:
                starter("pair", origin="assistant", address=d["address"])
            except Exception as exc:     # Busy: another Bluetooth job; try again later
                tries[d["address"]] = (n, None)
                log.info("assistant could not start pairing: %s", type(exc).__name__)
            return
