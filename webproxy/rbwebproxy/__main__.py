"""retroboot-webproxy: run the Modern Web Helper.

    python3 -m rbwebproxy --config /run/retroboot/webproxy.json

SIGHUP re-reads the configuration (view, redirects, search, LAN access);
listening addresses and ports need a restart.  Readiness is reported to
systemd (Type=notify) once the web and DNS listeners are open.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import sys

from . import __version__
from .config import Config
from .dns import Forwarder
from .proxy import App
from .redirects import hosts as redirect_hosts
from .server import Server

log = logging.getLogger("rbwebproxy")


def sd_notify(msg: str) -> None:
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    if path.startswith("@"):
        path = "\0" + path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(path)
            s.send(msg.encode())
    except OSError:
        pass


async def run(config_path: str | None, cfg: Config) -> int:
    app = App(cfg)
    await app.start()
    server = Server(app.handle, lambda status, msg: app.error_response(status, msg),
                    max_body=cfg.limits.upload_bytes, allow_peer=app.allow_peer, refused=app.refused_response)
    dns = None
    try:
        await server.start(cfg.listen, cfg.port)
        if cfg.dns:
            dns = Forwarder(lambda: redirect_hosts(app.cfg.redirects) if app.cfg.homepages else set(),
                            lambda: app.cfg.redirect_address)
            await dns.start(cfg.dns_listen, cfg.dns_port)
    except OSError as e:
        log.error("cannot listen: %s", e)
        await server.close()
        await app.close()
        return 1
    log.info("Modern Web Helper %s listening on %s:%s (DNS %s), view %s", __version__, ",".join(cfg.listen),
             cfg.port, cfg.dns_port if dns else "off", cfg.view)
    app.write_status()
    sd_notify("READY=1")
    loop = asyncio.get_running_loop()
    watchdog = None
    usec = os.environ.get("WATCHDOG_USEC")
    if usec and usec.isdigit() and int(usec) > 0:
        # A helper whose event loop stalls stops answering systemd's watchdog; systemd then
        # restarts it, and its ExecStopPost removes the redirect meanwhile.
        interval = int(usec) / 1e6 / 3

        async def ping() -> None:
            while True:
                sd_notify("WATCHDOG=1")
                await asyncio.sleep(interval)
        watchdog = loop.create_task(ping())
    stop = asyncio.Event()

    def reload() -> None:
        try:
            new = Config.load(config_path)
        except (OSError, ValueError) as e:
            log.error("configuration not reloaded: %s", e)
            return
        app.reconfigure(new)
        logging.getLogger().setLevel(new.log_level)
        log.info("configuration reloaded: view %s, %d redirects, search %s, LAN %s", new.view,
                 len(new.redirects), new.search, "on" if new.allow_lan else "off")
        sd_notify("READY=1")

    loop.add_signal_handler(signal.SIGHUP, reload)
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    loop.add_signal_handler(signal.SIGINT, stop.set)
    await stop.wait()
    sd_notify("STOPPING=1")
    if watchdog:
        watchdog.cancel()
    await server.close()
    if dns:
        await dns.close()
    await app.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rbwebproxy", description="RetroBoot Modern Web Helper")
    ap.add_argument("--config", default=os.environ.get("RBWEBPROXY_CONFIG"))
    ap.add_argument("--port", type=int)
    args = ap.parse_args(argv)
    cfg = Config.load(args.config)
    if args.port:
        cfg.port = args.port
    logging.basicConfig(level=cfg.log_level, format="%(name)s: %(message)s", stream=sys.stdout)
    return asyncio.run(run(args.config, cfg))


if __name__ == "__main__":
    sys.exit(main())
