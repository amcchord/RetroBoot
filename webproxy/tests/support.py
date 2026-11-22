"""Test support: local upstream web servers (http and TLS), a policy that lets the
helper reach them by *.test names, and a running helper with an old-browser client.

The real Policy refuses loopback, so tests that need a reachable upstream use
TestPolicy: ``<name>.test`` resolves to 127.0.0.1 and 127.0.0.1 is allowed only
on the upstream servers' own ports.  test_security.py checks the real policy.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import os
import socket
import ssl
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rbwebproxy.config import Config  # noqa: E402
from rbwebproxy.proxy import App, SafeResolver  # noqa: E402
from rbwebproxy.security import Policy, Refused  # noqa: E402
from rbwebproxy.server import Server  # noqa: E402


class TestPolicy(Policy):
    """Real checks, except *.test names map to 127.0.0.1 on allowed ports."""

    def __init__(self, ports: set[int], **kw):
        super().__init__(**kw)
        self.ports = ports
        self.local_provider = lambda: {ipaddress.ip_address("192.0.2.200")}

    async def resolve(self, loop, host, port):
        if host.endswith(".test"):
            self.check_port(port)
            if port not in self.ports:
                raise Refused("host", "addresses on this computer")
            return [{"hostname": host, "host": "127.0.0.1", "port": port, "family": socket.AF_INET,
                     "proto": 0, "flags": socket.AI_NUMERICHOST}]
        return await super().resolve(loop, host, port)

    def check_name(self, host):
        if host.endswith(".test"):
            return
        super().check_name(host)


def make_ca(tmp: str, name: str = "RetroBoot Test CA"):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subj).issuer_name(subj).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    path = os.path.join(tmp, name.replace(" ", "_") + ".pem")
    with open(path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return key, cert, path


def make_leaf(tmp: str, ca_key, ca_cert, hostname: str, expired: bool = False):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    start, end = (now - dt.timedelta(days=10), now - dt.timedelta(days=1)) if expired else \
        (now - dt.timedelta(days=1), now + dt.timedelta(days=10))
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
            .issuer_name(ca_cert.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(start).not_valid_after(end)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    cpath = os.path.join(tmp, f"{hostname}-{'expired' if expired else 'ok'}.crt")
    kpath = cpath[:-4] + ".key"
    with open(cpath, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kpath, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return cpath, kpath


class Upstreams:
    """aiohttp test sites; handlers are registered per test."""

    def __init__(self):
        from aiohttp import web
        self.web = web
        self.app = web.Application()
        self.routes: dict[str, object] = {}
        self.app.router.add_route("*", "/{tail:.*}", self._dispatch)
        self.runner = None
        self.http_port = 0
        self.tls_port = 0
        self.bad_tls_port = 0
        self.expired_tls_port = 0
        self.seen: list = []

    async def _dispatch(self, request):
        self.seen.append(request)
        h = self.routes.get(request.path) or self.routes.get("*")
        if h is None:
            return self.web.Response(status=404, text="<html><body><p>Not here</p></body></html>",
                                     content_type="text/html")
        return await h(request)

    async def start(self, tmp: str):
        web = self.web
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.http_port = site._server.sockets[0].getsockname()[1]
        ca_key, ca_cert, self.ca_path = make_ca(tmp)
        bad_key, bad_cert, _ = make_ca(tmp, "Untrusted CA")
        for attr, (k, c, exp) in {"tls_port": (ca_key, ca_cert, False), "bad_tls_port": (bad_key, bad_cert, False),
                                  "expired_tls_port": (ca_key, ca_cert, True)}.items():
            cpath, kpath = make_leaf(tmp, k, c, "secure.test", expired=exp)
            sctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            sctx.load_cert_chain(cpath, kpath)
            s = web.TCPSite(self.runner, "127.0.0.1", 0, ssl_context=sctx)
            await s.start()
            setattr(self, attr, s._server.sockets[0].getsockname()[1])

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()


class Helper:
    def __init__(self, app: App, server: Server, port: int):
        self.app = app
        self.server = server
        self.port = port

    async def raw(self, data: bytes, read_timeout: float = 15.0) -> bytes:
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        w.write(data)
        await w.drain()
        out = b""
        try:
            while True:
                chunk = await asyncio.wait_for(r.read(65536), read_timeout)
                if not chunk:
                    break
                out += chunk
        finally:
            w.close()
        return out

    async def get(self, url_or_path: str, host: str | None = None, headers: dict | None = None,
                  method: str = "GET", body: bytes = b"", version: str = "HTTP/1.0") -> tuple[int, dict, bytes]:
        lines = [f"{method} {url_or_path} {version}"]
        if host:
            lines.append(f"Host: {host}")
        lines.append("User-Agent: Mozilla/4.0 (compatible; MSIE 5.0; Mac_PowerPC)")
        for k, v in (headers or {}).items():
            lines.append(f"{k}: {v}")
        if body:
            lines.append(f"Content-Length: {len(body)}")
        if version == "HTTP/1.1":
            lines.append("Connection: close")
        raw = await self.raw(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
        return parse_response(raw)


def parse_response(raw: bytes) -> tuple[int, dict, bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    hdrs: dict = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        hdrs.setdefault(k.strip().lower(), []).append(v.strip())
    return status, {k: (v[0] if len(v) == 1 else v) for k, v in hdrs.items()}, body


async def start_helper(upstreams: "Upstreams", **cfg_overrides) -> "Helper":
    data = {"port": 0, "dns": False, "ca_file": upstreams.ca_path}
    data.update(cfg_overrides)
    cfg = Config.from_dict(data)
    app = App(cfg)
    app.policy = TestPolicy({upstreams.http_port, upstreams.tls_port, upstreams.bad_tls_port,
                             upstreams.expired_tls_port}, allow_lan=cfg.allow_lan)
    await app.start()
    app.session.connector._resolver = SafeResolver(app.policy)
    server = Server(app.handle, lambda s, m: app.error_response(s, m), max_body=cfg.limits.upload_bytes,
                    allow_peer=app.allow_peer, refused=app.refused_response)
    await server.start(["127.0.0.1"], 0)
    port = server.servers[0].sockets[0].getsockname()[1]
    return Helper(app, server, port)


class HelperCase(unittest.IsolatedAsyncioTestCase):
    """A running helper (self.h) in front of local upstream sites (self.up)."""

    helper_config: dict = {}

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.up = Upstreams()
        await self.up.start(self._tmp.name)
        self.h = await start_helper(self.up, **self.helper_config)

    async def asyncTearDown(self):
        await self.h.server.close()
        await self.h.app.close()
        await self.up.stop()
        self._tmp.cleanup()

    def site(self, path: str = "/") -> str:
        return f"site.test:{self.up.http_port}"
