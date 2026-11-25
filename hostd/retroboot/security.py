"""Pairing, sessions, TLS certificate and QR matrix for web management.

* A 6-digit pairing code is generated only for the local classic panel and
  expires after 5 minutes.  Attempts are rate limited per client address and
  globally; too many failures retire the current code.
* Pairing creates a device record holding only SHA-256 hashes of its session
  token; the token itself lives in the browser's HttpOnly cookie.
* Each session has a CSRF token (HMAC of the session token) that clients echo
  in ``X-RetroBoot-CSRF``.
* The TLS certificate is a per-device self-signed EC P-256 certificate created
  at first start under ``state/``; the panel shows its SHA-256 fingerprint.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path

from .util import atomic_write_bytes, atomic_write_json, read_json, wall

log = logging.getLogger("retroboot.security")

CODE_TTL = 300.0
MAX_FAILS_PER_CLIENT = 5
MAX_FAILS_GLOBAL = 20
FAIL_WINDOW = 300.0
MAX_DEVICES = 32


class PairingError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class PairingService:
    def __init__(self, state_dir: Path, clock=time.monotonic):
        self.path = Path(state_dir) / "pairing.json"
        self.clock = clock
        self.code: str | None = None
        self.code_expires: float = 0.0
        self.fails: dict[str, list[float]] = {}
        self.global_fails: list[float] = []
        self.data = self._load()
        self.on_change = []
        self.frozen = False

    def _load(self) -> dict:
        try:
            data = read_json(self.path, 1024 * 1024)
            if isinstance(data, dict) and isinstance(data.get("devices"), list) and data.get("secret"):
                return data
        except (OSError, ValueError):
            pass
        data = {"secret": secrets.token_hex(32), "devices": []}
        self._save(data)
        return data

    def _save(self, data: dict | None = None) -> None:
        atomic_write_json(self.path, data or self.data, 0o600)

    # -- code ----------------------------------------------------------------
    def current_code(self, regenerate: bool = False) -> tuple[str, float]:
        """Return (code, seconds_left), generating a fresh code when needed."""
        now = self.clock()
        if regenerate or not self.code or now >= self.code_expires:
            self.code = f"{secrets.randbelow(1_000_000):06d}"
            self.code_expires = now + CODE_TTL
            for fn in self.on_change:
                fn(self.code)
        return self.code, max(0.0, self.code_expires - now)

    def code_active(self) -> bool:
        return bool(self.code) and self.clock() < self.code_expires

    def retire_code(self) -> None:
        self.code = None
        self.code_expires = 0.0

    def _prune(self, now: float) -> None:
        cutoff = now - FAIL_WINDOW
        self.global_fails = [t for t in self.global_fails if t > cutoff]
        for k in list(self.fails):
            self.fails[k] = [t for t in self.fails[k] if t > cutoff]
            if not self.fails[k]:
                del self.fails[k]

    def pair(self, code: str, device_name: str, client: str) -> tuple[dict, str]:
        """Validate a code and create a device.  Returns (device_public, session_token)."""
        now = self.clock()
        self._prune(now)
        if len(self.fails.get(client, [])) >= MAX_FAILS_PER_CLIENT or len(self.global_fails) >= MAX_FAILS_GLOBAL:
            raise PairingError("rate_limited", "Too many attempts. Wait a few minutes and try again.", 429)
        if not isinstance(device_name, str) or not 1 <= len(device_name.strip()) <= 64 \
                or any(ord(c) < 32 for c in device_name):
            raise PairingError("invalid", "Give this device a name of up to 64 characters.", 422)
        ok = (isinstance(code, str) and len(code) == 6 and code.isdigit() and self.code is not None
              and now < self.code_expires and hmac.compare_digest(code, self.code))
        if not ok:
            self.fails.setdefault(client, []).append(now)
            self.global_fails.append(now)
            if len(self.global_fails) >= MAX_FAILS_GLOBAL:
                self.retire_code()
            raise PairingError("invalid_code", "That code is not valid. Check the code shown on the RetroBoot screen.", 403)
        self.retire_code()
        token = secrets.token_urlsafe(32)
        dev = {"id": secrets.token_hex(6), "name": device_name.strip(), "token_hash": _h(token),
               "created": wall(), "last_seen": wall(), "client": client}
        self.data["devices"].append(dev)
        self.data["devices"] = self.data["devices"][-MAX_DEVICES:]
        self._save()
        return self.public(dev), token

    # -- sessions ------------------------------------------------------------
    def device_for_token(self, token: str | None) -> dict | None:
        if not token or len(token) > 128:
            return None
        th = _h(token)
        for d in self.data["devices"]:
            if hmac.compare_digest(d["token_hash"], th):
                return d
        return None

    def touch(self, dev: dict) -> None:
        if self.frozen:
            return
        # Persist last_seen at most once a minute.
        now = wall()
        if dev.get("last_seen", "")[:16] != now[:16]:
            dev["last_seen"] = now
            self._save()

    def csrf_for(self, token: str) -> str:
        return hmac.new(bytes.fromhex(self.data["secret"]), token.encode(), hashlib.sha256).hexdigest()

    def check_csrf(self, token: str, header: str | None) -> bool:
        return bool(header) and hmac.compare_digest(self.csrf_for(token), header)

    def revoke(self, device_id: str) -> bool:
        before = len(self.data["devices"])
        self.data["devices"] = [d for d in self.data["devices"] if d["id"] != device_id]
        if len(self.data["devices"]) != before:
            self._save()
            return True
        return False

    def devices(self) -> list[dict]:
        return [self.public(d) for d in self.data["devices"]]

    @staticmethod
    def public(d: dict) -> dict:
        return {"id": d["id"], "name": d["name"], "created": d["created"], "last_seen": d.get("last_seen")}


# --- TLS -----------------------------------------------------------------------

def ensure_certificate(state_dir: Path, hostname: str) -> tuple[Path, Path, str]:
    """Create (once) and return (cert_path, key_path, sha256_fingerprint)."""
    state_dir = Path(state_dir)
    cert_path = state_dir / "tls-cert.pem"
    key_path = state_dir / "tls-key.pem"
    if not (cert_path.exists() and key_path.exists()):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{hostname}.local"),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, "RetroBoot")])
        now = _dt.datetime.now(_dt.timezone.utc)
        sans = [x509.DNSName(hostname), x509.DNSName(f"{hostname}.local"), x509.DNSName("localhost")]
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - _dt.timedelta(days=1))
                .not_valid_after(now + _dt.timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName(sans), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        atomic_write_bytes(key_path, key.private_bytes(serialization.Encoding.PEM,
                                                       serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()), 0o600)
        atomic_write_bytes(cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o644)
    return cert_path, key_path, fingerprint(cert_path)


def fingerprint(cert_path: Path) -> str:
    import base64
    pem = Path(cert_path).read_text()
    b64 = "".join(l for l in pem.splitlines() if l and not l.startswith("-----"))
    der = base64.b64decode(b64)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


# --- QR ------------------------------------------------------------------------

def qr_matrix(text: str, border: int = 0) -> list[str] | None:
    # The presenter adds the quiet zone (rbui "quiet", default 4 modules).
    """Rows of '0'/'1' for the UI document ``qr`` item; None if python3-qrcode is missing."""
    try:
        import qrcode
    except ImportError:
        return None
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=border, box_size=1)
    q.add_data(text)
    q.make(fit=True)
    return ["".join("1" if c else "0" for c in row) for row in q.get_matrix()]
