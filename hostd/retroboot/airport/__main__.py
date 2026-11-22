"""Run the AirPort bridge on its own (lab and development; hostd embeds it normally).

    python3 -m retroboot.airport --socket RUN/airport.sock --backend virtual [--uplink IF] [--ssid NAME]
    python3 -m retroboot.airport --socket RUN/airport.sock --backend fixture --fixture NETS.json
    python3 -m retroboot.airport --socket RUN/airport.sock --backend networkmanager

``--control CTL.sock`` accepts newline-delimited JSON commands standing in for
the classic panel: ``{"cmd":"status"}``, ``{"cmd":"prompt"}`` (the pending
password request, if any), ``{"cmd":"answer","secret":"..."}``,
``{"cmd":"cancel"}``, ``{"cmd":"prompts","available":false}``.  Events go to
``--events FILE`` as JSON lines (never secrets).  ``--startup-s S`` stands in
for hostd's "Mac OS is starting up" (launched, no Finder yet): joins within S
seconds of the card's hello are AirPort's startup rejoin (service.py).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import time
from pathlib import Path

from . import make_backend
from .names import NameTable
from .service import AirPortService

log = logging.getLogger("retroboot.airport")


class ControlPrompter:
    def __init__(self):
        self.enabled = True
        self.pending: dict | None = None
        self.future: asyncio.Future | None = None
        self.history: list[dict] = []

    def available(self) -> bool:
        return self.enabled

    async def ask(self, name, security, error, hidden):
        self.future = asyncio.get_running_loop().create_future()
        self.pending = {"name": name, "security": security, "error": error, "hidden": hidden, "since": time.time()}
        try:
            return await asyncio.wait_for(self.future, 300)
        except asyncio.TimeoutError:
            return None
        finally:
            self.pending = None

    async def working(self, name):
        self.history.append({"working": name})

    async def finish(self, ok, message):
        self.history.append({"finish": ok, "message": message})

    def answer(self, secret):
        if self.future and not self.future.done():
            self.future.set_result(secret)
            return True
        return False


async def control_server(path: Path, svc: AirPortService, prompter: ControlPrompter):
    async def client(reader, writer):
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                req = json.loads(line)
                cmd = req.get("cmd")
                if cmd == "status":
                    res = {"ok": True, "status": svc.public(), "prompt": prompter.pending}
                elif cmd == "prompt":
                    res = {"ok": True, "prompt": prompter.pending}
                elif cmd == "answer":
                    res = {"ok": prompter.answer(str(req.get("secret", "")))}
                elif cmd == "cancel":
                    res = {"ok": prompter.answer(None)}
                elif cmd == "prompts":
                    prompter.enabled = bool(req.get("available", True))
                    res = {"ok": True}
                else:
                    res = {"ok": False, "error": "unknown command"}
            except (ValueError, AttributeError) as exc:
                res = {"ok": False, "error": str(exc)}
            writer.write(json.dumps(res).encode() + b"\n")
            await writer.drain()
        writer.close()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return await asyncio.start_unix_server(client, path=str(path))


async def amain(a) -> int:
    events = open(a.events, "a", buffering=1) if a.events else None

    def event(name, **kw):
        rec = {"t": round(time.time(), 3), "mono": round(time.monotonic(), 3), "event": name, **kw}
        if events:
            events.write(json.dumps(rec) + "\n")
        log.info("%s %s", name, kw)

    adapter = None
    if a.backend == "networkmanager":
        from ..network import NetworkAdapter
        adapter = NetworkAdapter(nm_connections_dir=Path(a.nm_connections))
    backend = make_backend(a.backend, wifi=True, adapter=adapter, fixture=a.fixture, ssid=a.ssid, uplink=a.uplink)
    prompter = ControlPrompter()
    names = NameTable.load(Path(a.state) / "airport-names.json" if a.state else None)
    svc = AirPortService(backend, Path(a.socket), names, prompter, event=event)
    if a.startup_s > 0:
        svc.startup = lambda: svc.card_hello_at is not None and time.monotonic() - svc.card_hello_at < a.startup_s
    await svc.start()
    ctl = await control_server(Path(a.control), svc, prompter) if a.control else None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    if ctl:
        ctl.close()
    await svc.stop()
    event("airport_bridge_stopped")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="python3 -m retroboot.airport", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--socket", required=True)
    ap.add_argument("--backend", choices=("virtual", "fixture", "networkmanager"), default="virtual")
    ap.add_argument("--fixture")
    ap.add_argument("--ssid", default="RetroBoot")
    ap.add_argument("--uplink")
    ap.add_argument("--state", help="directory for airport-names.json")
    ap.add_argument("--control")
    ap.add_argument("--events")
    ap.add_argument("--nm-connections", default="/var/lib/retroboot/network/connections")
    ap.add_argument("--startup-s", type=float, default=0.0,
                    help="joins this long after the card's hello count as Mac OS's startup rejoin (0: never)")
    ap.add_argument("--log-level", default="INFO")
    a = ap.parse_args()
    logging.basicConfig(level=getattr(logging, a.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(amain(a))


if __name__ == "__main__":
    raise SystemExit(main())
