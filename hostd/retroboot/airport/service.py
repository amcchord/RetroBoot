"""AirPort bridge policy: one host connection authority, reflected to the card.

* The card's association is the host's: while the guest has AirPort on (BSS
  mode) the card is "associated" exactly when the host is connected, with the
  host network's guest name, BSSID, channel and signal.  A change made outside
  the guest (web page, panel, roaming, a lost link) reaches the guest.
* A guest join asks the host to connect.  Networks with a host profile, open
  networks and the virtual network join directly.  A WPA/WEP network without a
  profile opens RetroBoot's own password panel over the guest; the password
  goes to NetworkManager's profile and never into the emulated card.  Cancel
  reports a failed join and suppresses another prompt for that network for a
  minute (the driver may retry on its own).
* Guest "Turn AirPort Off" (port disabled) only takes the guest off the
  network.  It never turns the host radio off: shut down/restart also disable
  the card, and the host radio carries web management.  A guest join turns a
  disabled host radio back on.
* Computer-to-computer (IBSS) networks stay inside the card.
* Mac OS's own rejoin while it starts up follows the host.  AirPort 2.0.4
  joins its remembered network at every start (hostd names the host's
  network there before each launch, ``AirPort.prepare_launch``); that join
  never changes the host's Wi-Fi and never opens a password window.  With the
  host connected the card gets the host's association whatever name the
  guest remembered; with the host not connected yet (a cold boot racing
  NetworkManager) the guest gets a provisional association and then the
  host's real state, as for any join.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .backends import Backend, HostLink, JoinError, VirtualBackend
from .bridge import PROTOCOL_VERSION, BridgeServer, Connection
from .names import (PSK, SECURITY_LABELS, GuestNetwork, HostNetwork, NameTable, guest_name,
                    host_ssid_from_guest)

log = logging.getLogger("retroboot.airport")

NOISE_DBM = -95
SIGNAL_STEP_DB = 4
SCAN_REPLY_TIMEOUT = 2.0
SCAN_MAX_AGE = 15.0
PROMPT_SUPPRESS_S = 60.0
PROMPT_ATTEMPTS = 3
LAUNCH_LINK_TIMEOUT = 2.0     # asking the backend before the first poll finished
# The host link is polled at the backend's pace (NetworkManager: every 2 s, three nmcli
# runs) only while an AirPort card is connected or a join runs: that is when the card
# mirrors it.  Otherwise it only feeds status pages and the network AirPort joins at the
# next launch, and every IDLE_POLL_S is enough; a card that connects wakes the poll at once.
IDLE_POLL_S = 30.0


class Prompter(Protocol):
    def available(self) -> bool: ...
    async def ask(self, name: str, security: str, error: str | None, hidden: bool) -> str | None: ...
    async def working(self, name: str) -> None: ...
    async def finish(self, ok: bool, message: str | None) -> None: ...


@dataclass
class GuestState:
    connected: bool = False
    enabled: bool = False
    mode: str = "bss"
    desired: bytes = b""
    mac: str | None = None
    firmware: str | None = None
    # A network whose password window the user cancelled: the card's driver keeps it in
    # its desired-SSID register (and in its state messages), but Mac OS shows the network
    # the host is really on, so status reports that one instead (see _declined).
    declined: bytes | None = None


class AirPortService:
    def __init__(self, backend: Backend, socket_path, names: NameTable | None = None,
                 prompter: Prompter | None = None, event: Callable[..., None] | None = None,
                 group_gid: int | None = None, redact: Callable[[str], None] | None = None,
                 startup: Callable[[], bool] | None = None):
        self.backend = backend
        self.names = names or NameTable()
        self.prompter = prompter
        # True while Mac OS is starting up (hostd: launched, Finder not seen yet).
        self.startup = startup or (lambda: False)
        self.event = event or (lambda name, **kw: log.info("%s %s", name, kw))
        self.redact = redact or (lambda secret: None)
        self.bridge = BridgeServer(socket_path, self._on_message, self._on_connect, self._on_disconnect, group_gid)
        self.guest = GuestState()
        self.host = HostLink()
        self.networks: list[GuestNetwork] = []
        self.networks_at = 0.0
        self.last_link: dict | None = None
        self.join_task: asyncio.Task | None = None
        self.joining = False
        self.suppress: dict[bytes, float] = {}
        self._poll: asyncio.Task | None = None
        self._poll_wake = asyncio.Event()
        self._rescan: asyncio.Task | None = None
        self._fresh_at = 0.0
        self.polled = False
        self.card_hello_at: float | None = None
        self.stats = {"scans": 0, "joins": 0, "prompts": 0, "links_sent": 0}

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        await self.backend.start()
        await self.bridge.start()
        self._poll = asyncio.get_running_loop().create_task(self._poll_loop())
        self.event("airport_bridge_listening", backend=self.backend.name, socket=str(self.bridge.path))

    async def stop(self) -> None:
        for t in (self._poll, self._rescan, self.join_task):
            if t and not t.done():
                t.cancel()
        await self.bridge.stop()
        await self.backend.stop()

    def public(self) -> dict:
        """State for status pages and evidence (no secrets)."""
        link = self.host.connected
        return {"backend": self.backend.describe(), "guest": {
                    "connected": self.guest.connected, "radio": "on" if self.guest.enabled else "off",
                    "mode": self.guest.mode, "desired": self.guest.desired.decode("mac_roman"),
                    # what the card reports to Mac OS: only while its port is enabled
                    "associated": bool(self.guest.enabled and self.last_link
                                       and self.last_link.get("state") == "associated"),
                    "mac": self.guest.mac},
                "host": {"radio": self.host.radio,
                         "network": None if link is None else {"ssid": link.ssid.decode("utf-8", "replace"),
                                                               "bssid": link.bssid, "signal_dbm": link.signal,
                                                               "security": SECURITY_LABELS.get(link.security)}},
                "networks": [n.public() for n in self.networks], "joining": self.joining,
                "connections": self.bridge.connections, "stats": dict(self.stats)}

    # -- bridge events -------------------------------------------------------
    async def _on_connect(self, conn: Connection) -> None:
        self.guest = GuestState(connected=True)
        self.last_link = None
        self._poll_wake.set()                        # back to the backend's pace at once
        conn.send({"type": "hello", "version": PROTOCOL_VERSION, "backend": self.backend.name})
        self.event("airport_bridge_connected", connection=conn.serial)
        nets = await self._guest_networks(fresh=False)
        conn.send({"type": "networks", "networks": [n.wire(NOISE_DBM) for n in nets]})

    async def _on_disconnect(self, conn: Connection) -> None:
        self.guest.connected = False
        if self.join_task and not self.join_task.done():
            self.join_task.cancel()
        self.event("airport_bridge_disconnected", connection=conn.serial)

    async def _on_message(self, conn: Connection, msg: dict) -> None:
        t = msg["type"]
        if t == "hello":
            self.guest.mac, self.guest.firmware = msg["mac"], msg["firmware"]
            self.card_hello_at = time.monotonic()
            self.event("airport_card_hello", version=msg["version"], mac=msg["mac"], firmware=msg["firmware"])
        elif t == "state":
            was = (self.guest.enabled, self.guest.mode)
            self.guest.enabled, self.guest.mode = msg["enabled"], msg["mode"]
            if not (self.guest.declined and msg["desired"] == self.guest.declined):
                self.guest.desired = msg["desired"]
            if (self.guest.enabled, self.guest.mode) != was:
                self.event("airport_guest_radio", radio="on" if msg["enabled"] else "off", mode=msg["mode"])
            if not self.guest.enabled:
                self.last_link = None
        elif t == "scan":
            self.stats["scans"] += 1
            await self._scan(conn, msg["seq"])
        elif t == "join":
            self.stats["joins"] += 1
            if msg["ssid"] != self.guest.declined:
                self.guest.declined = None
            self.guest.desired = msg["ssid"]
            if self.join_task and not self.join_task.done():
                self.join_task.cancel()
            self.join_task = asyncio.get_running_loop().create_task(self._join(conn, msg["seq"], msg["ssid"]))
        elif t == "leave":
            self.last_link = None
            self.event("airport_guest_left")

    # -- networks ------------------------------------------------------------
    async def _guest_networks(self, fresh: bool) -> list[GuestNetwork]:
        try:
            aps = await asyncio.wait_for(self.backend.scan(fresh), SCAN_REPLY_TIMEOUT if not fresh else 30)
        except (asyncio.TimeoutError, OSError) as exc:
            log.warning("airport scan failed: %s", exc)
            return self.networks
        self.networks = self.names.guest_view(aps)
        self.networks_at = time.monotonic()
        return self.networks

    async def _scan(self, conn: Connection, seq: int) -> None:
        nets = await self._guest_networks(fresh=False)
        conn.send({"type": "networks", "seq": seq, "networks": [n.wire(NOISE_DBM) for n in nets]})
        if (self._rescan is None or self._rescan.done()) and time.monotonic() - self._fresh_at > SCAN_MAX_AGE:
            self._rescan = asyncio.get_running_loop().create_task(self._background_rescan())

    async def _background_rescan(self) -> None:
        self._fresh_at = time.monotonic()
        nets = await self._guest_networks(fresh=True)
        self.bridge.send({"type": "networks", "networks": [n.wire(NOISE_DBM) for n in nets]})

    # -- link reflection -----------------------------------------------------
    def _link_message(self, link: HostLink, seq: int | None = None, failed: str | None = None) -> dict:
        if failed is not None:
            msg = {"type": "link", "state": "failed", "reason": failed}
        elif link.connected is None:
            # "lost" (out of range) until the host is back, so the card can
            # report "back in range" rather than a new association.
            was = self.last_link and self.last_link.get("state") in ("associated", "lost")
            msg = {"type": "link", "state": "lost" if was else "searching"}
        else:
            ap = link.connected
            name = self.names.assign(ap.id, guest_name(ap.ssid))
            msg = {"type": "link", "state": "associated", "ssid": name.hex(), "bssid": ap.bssid,
                   "channel": max(1, min(14, ap.channel or 1)), "signal": max(-110, min(-10, ap.signal)),
                   "noise": NOISE_DBM}
        msg["radio"] = "on" if link.radio else "off"
        if seq is not None:
            msg["seq"] = seq
        return msg

    def _send_link(self, msg: dict, force: bool = False) -> None:
        last = self.last_link
        if not force and last is not None and last.get("state") == msg["state"] and \
                all(last.get(k) == msg.get(k) for k in ("ssid", "bssid", "channel", "radio")) and \
                abs(int(last.get("signal", 0)) - int(msg.get("signal", 0))) < SIGNAL_STEP_DB:
            return
        if self.bridge.send(msg):
            self.stats["links_sent"] += 1
            # A reply that arrives after the guest disabled its port (AirPort probes the
            # card with ENABLE/DISABLE pairs at every start) is ignored by the card.
            self.last_link = {k: v for k, v in msg.items() if k != "seq"} if self.guest.enabled else None

    def _note_host(self, ap) -> bytes:
        """The guest name of the host's network; remember it as the host's last network."""
        name = self.names.assign(ap.id, guest_name(ap.ssid))
        if not isinstance(self.backend, VirtualBackend):
            self.names.remember_host(self.backend.name, ap.id)
        return name

    async def startup_network(self) -> dict | None:
        """The network AirPort should join when Mac OS next starts: the host's, else the
        one the host was last on, else NetworkManager's most recently used saved profile,
        else the virtual network.  {"name": guest bytes, "network": id, "source": host |
        last-host | profile | virtual} or None (nothing known)."""
        link: HostLink | None = self.host if self.polled else None
        if link is None:
            try:
                link = await asyncio.wait_for(self.backend.link(), LAUNCH_LINK_TIMEOUT)
            except Exception as exc:                 # never hold up a launch
                log.warning("airport host link unknown at launch: %s", exc)
                link = None
        if link is not None and link.connected is not None:
            return {"name": self._note_host(link.connected), "network": link.connected.id, "source": "host"}
        last = self.names.last_host_name(self.backend.name)
        if last is not None:
            return {"name": last[1], "network": last[0], "source": "last-host"}
        recent = getattr(self.backend, "recent_profile", None)
        if recent is not None:                       # a first boot: NetworkManager's last profile
            try:
                net = await asyncio.wait_for(recent(), LAUNCH_LINK_TIMEOUT)
            except Exception as exc:
                log.warning("airport saved profiles unknown at launch: %s", exc)
                net = None
            if net is not None:
                return {"name": self.names.assign(net.id, guest_name(net.ssid)), "network": net.id,
                        "source": "profile"}
        if isinstance(self.backend, VirtualBackend):
            net = self.backend.network()
            return {"name": self.names.assign(net.id, guest_name(net.ssid)), "network": net.id, "source": "virtual"}
        return None

    async def _poll_loop(self) -> None:
        while True:
            try:
                link = await asyncio.wait_for(self.backend.link(), 20)
                changed = (link.radio, link.connected and (link.connected.id, link.connected.bssid)) != \
                          (self.host.radio, self.host.connected and (self.host.connected.id, self.host.connected.bssid))
                self.host = link
                self.polled = True
                if changed:
                    self.event("airport_host_link", radio=link.radio,
                               network=link.connected.id if link.connected else None)
                    if link.connected is not None:
                        self._note_host(link.connected)
                if self.guest.connected and self.guest.enabled and self.guest.mode == "bss" and not self.joining:
                    self._send_link(self._link_message(link))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("airport host poll failed")
            await self._poll_pause()

    def poll_delay(self) -> float:
        if self.guest.connected or self.joining:
            return self.backend.poll_interval
        return max(self.backend.poll_interval, IDLE_POLL_S)

    async def _poll_pause(self) -> None:
        self._poll_wake.clear()
        try:
            await asyncio.wait_for(self._poll_wake.wait(), self.poll_delay())
        except asyncio.TimeoutError:
            pass

    # -- joins ---------------------------------------------------------------
    async def _join(self, conn: Connection, seq: int, name: bytes) -> None:
        self.joining = True
        try:
            result = await self._join_inner(seq, name)
        except asyncio.CancelledError:
            self.joining = False
            raise
        except Exception as exc:
            log.exception("airport join failed")
            result = self._link_message(self.host, seq, failed=str(exc)[:120])
        self.joining = False
        self._send_link(result, force=True)

    async def _join_inner(self, seq: int, name: bytes) -> dict:
        link = await self.backend.link()
        self.host = link
        starting = self.startup()
        if not name:                                   # "any network": whatever the host has
            self.event("airport_join", network="any", result="host-link", **({"startup": True} if starting else {}))
            return self._link_message(link, seq)
        if starting:
            return self._startup_join(seq, name, link)
        nets = await self._guest_networks(fresh=False)
        target = next((n for n in nets if n.name == name), None)
        if target is None and self.names.lookup(name):
            nets = await self._guest_networks(fresh=True)
            target = next((n for n in nets if n.name == name), None)
        if link.connected is not None and target is not None and link.connected.id == target.id:
            self.event("airport_join", network=target.id, result="already-connected")
            return self._link_message(link, seq)
        if target is not None:
            host_net = HostNetwork(target.ssid, target.bssid, target.channel, target.signal, target.security,
                                   known=target.known)
            label, security, hidden = target.name.decode("mac_roman"), target.security, False
        else:
            # Not in range or typed with "Other…": a hidden network by its typed name.
            host_net = HostNetwork(host_ssid_from_guest(name), "", 0, -60, PSK, known=False)
            label, security, hidden = name.decode("mac_roman"), None, True
        self.event("airport_join", network=host_net.id if not hidden else "hidden", result="requested",
                   guest_name=label)
        # AirPort 2.0.4 gives up on an association after about two seconds,
        # while a host join (RetroBoot's password panel, NetworkManager's
        # activation and DHCP) takes longer.  Answer the guest at once with
        # the network it chose; the outcome follows (failure -> disconnected,
        # and the poll loop then shows whatever the host really has).
        self._send_link(self._provisional(name, target, seq), force=True)
        secret: str | None = None
        error: str | None = None
        for attempt in range(PROMPT_ATTEMPTS + 1):
            try:
                if not await self._host_radio_on():
                    return self._link_message(await self.backend.link(), seq, failed="radio-off")
                await self.backend.join(host_net, secret, hidden=hidden)
                link = await self.backend.link()
                self.host = link
                if self.prompter and secret is not None:
                    await self.prompter.finish(True, None)
                self.event("airport_join", network=host_net.id if not hidden else "hidden", result="connected",
                           with_password=secret is not None)
                return self._link_message(link, seq) if link.connected else \
                    self._link_message(link, seq, failed="not-connected")
            except JoinError as exc:
                if exc.code not in ("needs-secret", "wrong-password") or attempt == PROMPT_ATTEMPTS:
                    if self.prompter and secret is not None:
                        await self.prompter.finish(False, str(exc))
                    self.event("airport_join", network=host_net.id if not hidden else "hidden",
                               result="failed", code=exc.code)
                    return self._link_message(self.host, seq, failed=exc.code)
                error = None if exc.code == "needs-secret" else str(exc)
            secret, why = await self._ask(name, label, security, error, hidden)
            if secret is None:
                if why in ("cancelled", "suppressed"):
                    self._declined(name)
                return self._link_message(self.host, seq, failed=why)
        return self._link_message(self.host, seq, failed="failed")

    def _startup_join(self, seq: int, name: bytes, link: HostLink) -> dict:
        """AirPort's rejoin of its remembered network while Mac OS starts: follow the host.
        A Mac OS start never changes the host's Wi-Fi or asks for a password (a remembered
        neighbour's open network, or one the host has no profile for, stays a guest-side
        memory); the guest shows the network the host is on."""
        wanted = self.names.lookup(name)
        if link.connected is not None:
            ap = link.connected
            self.event("airport_join", network=ap.id, startup=True,
                       result="already-connected" if wanted == ap.id else "follows-host")
            return self._link_message(link, seq)
        # The host is not connected yet (a cold boot racing NetworkManager, or no Wi-Fi in
        # range): answer at once so AirPort does not give up after two seconds; the poll
        # loop then reports the host's real state (out of range until it connects).
        self.event("airport_join", network=wanted or "unknown", startup=True, result="waiting-for-host")
        target = next((n for n in self.networks if n.name == name), None)
        return self._provisional(name, target, seq)

    def _declined(self, name: bytes) -> None:
        """After a cancelled password window the guest stays on (or returns to) the host's
        network: report that as desired, not the declined one, until the guest picks
        another network."""
        self.guest.declined = bytes(name)
        ap = self.host.connected
        self.guest.desired = self.names.assign(ap.id, guest_name(ap.ssid)) if ap is not None else b""

    def _provisional(self, name: bytes, target: GuestNetwork | None, seq: int) -> dict:
        if target is not None:
            bssid, channel, signal = target.bssid, target.channel, target.signal
        else:
            h = hashlib.sha256(b"hidden:" + bytes(name)).digest()
            bssid, channel, signal = "02:" + ":".join(f"{b:02x}" for b in h[:5]), 6, -60
        return {"type": "link", "state": "associated", "ssid": bytes(name).hex(), "bssid": bssid,
                "channel": max(1, min(14, channel or 1)), "signal": max(-110, min(-10, signal)),
                "noise": NOISE_DBM, "radio": "on" if self.host.radio else "off", "seq": seq, "provisional": True}

    async def _ask(self, name: bytes, label: str, security: str | None, error: str | None,
                   hidden: bool) -> tuple[str | None, str]:
        """(secret, "") or (None, reason): cancelled | suppressed | no-prompt."""
        now = time.monotonic()
        if self.suppress.get(name, 0) > now:
            self.event("airport_prompt", result="suppressed", guest_name=label)
            return None, "suppressed"
        if self.prompter is None or not self.prompter.available():
            self.event("airport_prompt", result="unavailable", guest_name=label)
            return None, "no-prompt"
        self.stats["prompts"] += 1
        self.event("airport_prompt", result="shown", guest_name=label, retry=error is not None)
        secret = await self.prompter.ask(label, SECURITY_LABELS.get(security, "") if security else "",
                                         error, hidden)
        if secret is None:
            self.suppress[name] = time.monotonic() + PROMPT_SUPPRESS_S
            self.event("airport_prompt", result="cancelled", guest_name=label)
            return None, "cancelled"
        self.redact(secret)
        await self.prompter.working(label)
        return secret, ""

    async def _host_radio_on(self) -> bool:
        link = await self.backend.link()
        if link.radio:
            return True
        enable = getattr(self.backend, "radio_on", None)
        if enable is None:
            return False
        self.event("airport_host_radio", action="on", reason="guest join")
        return await enable()
