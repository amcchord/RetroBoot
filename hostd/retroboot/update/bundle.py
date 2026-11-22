"""RetroBoot update bundles (``retroboot-update-<version>-<flavor>.rbu``; ARCHITECTURE section 13).

A bundle is an uncompressed POSIX tar whose members come in this order:

``manifest.json``       what the bundle is (version, build, flavor, sizes, hashes,
                        oldest version it updates, settings schema, kernel arguments)
``manifest.sig``        ``{"alg": "ed25519", "key_id", "signature"}`` over the exact
                        bytes of manifest.json
``root-files.json.gz``  every path of the new root filesystem with type, mode,
                        owner and SHA-256 (or link target)
``root.sqfs``           the root filesystem (squashfs, xz), which also carries the
                        boot payload (kernel, initramfs in /boot and the ESP/BIOS
                        GRUB files in /usr/lib/retroboot/boot)

Nothing else is accepted.  A device trusts a manifest only when it is signed by a
key in ``/usr/lib/retroboot/update/keys/*.pub`` that is not listed in ``revoked``;
the payload hashes in a trusted manifest then cover everything else.

Self-contained (standard library + python3-cryptography) so the lab bootstrap can
run it on a device whose hostd predates updates.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
import tarfile
from pathlib import Path
from typing import Callable

FORMAT = "retroboot-update/1"
SIG_FORMAT = "retroboot-update-sig/1"
FILES_FORMAT = "retroboot-root-files/1"
MEMBERS = ("manifest.json", "manifest.sig", "root-files.json.gz", "root.sqfs")
MAX_MANIFEST = 1024 * 1024
MAX_SIG = 4096
MAX_FILES = 64 * 1024 * 1024
MAX_ROOT = 3 * 1024 ** 3
KEYS_DIR = "/usr/lib/retroboot/update/keys"
VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
BUILD_RE = re.compile(r"^(\d{1,4}\.\d{1,4}\.\d{1,4})-(\d{8}T\d{6}Z)-([0-9a-z-]{1,40})$")
FLAVORS = ("debug", "product")


class BundleError(Exception):
    """``message`` is user-facing (classic wording); ``code`` is for the API."""

    def __init__(self, message: str, code: str = "invalid-bundle"):
        super().__init__(message)
        self.message = message
        self.code = code


def sha256_file(path: Path, offset: int = 0, size: int | None = None,
                progress: Callable[[int], None] | None = None, chunk: int = 4 << 20) -> str:
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        f.seek(offset)
        while size is None or done < size:
            want = chunk if size is None else min(chunk, size - done)
            data = f.read(want)
            if not data:
                break
            h.update(data)
            done += len(data)
            if progress:
                progress(done)
    if size is not None and done != size:
        raise BundleError("The update file is incomplete.", "incomplete")
    return h.hexdigest()


# --- versions --------------------------------------------------------------------------

def version_key(version: str, build_id: str | None = None) -> tuple:
    try:
        v = tuple(int(x) for x in str(version).split("."))
    except ValueError:
        v = (0,)
    m = BUILD_RE.match(build_id or "")
    return v + ((m.group(2) if m else ""),)


def describe_version(version: str, build_id: str | None) -> str:
    """``0.3.0 (build 2026-10-12 01:02)`` for people."""
    m = BUILD_RE.match(build_id or "")
    if not m:
        return str(version)
    t = m.group(2)
    return f"{version} (build {t[0:4]}-{t[4:6]}-{t[6:8]} {t[9:11]}:{t[11:13]})"


# --- keys and signatures ---------------------------------------------------------------

def key_id(raw_public: bytes) -> str:
    return hashlib.sha256(raw_public).hexdigest()[:16]


def _load_public(pem: bytes):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("not an Ed25519 key")
    return key


def raw_public(key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def trusted_keys(keys_dir: str | Path = KEYS_DIR) -> dict:
    """{key_id: public key} for every *.pub in keys_dir, minus the ids listed in ``revoked``."""
    d = Path(keys_dir)
    revoked = set()
    try:
        revoked = {line.split("#")[0].strip() for line in (d / "revoked").read_text().splitlines()}
    except OSError:
        pass
    keys = {}
    for p in sorted(d.glob("*.pub")) if d.is_dir() else []:
        try:
            k = _load_public(p.read_bytes())
        except Exception:
            continue
        kid = key_id(raw_public(k))
        if kid not in revoked:
            keys[kid] = k
    return keys


def verify_signature(manifest_bytes: bytes, sig_bytes: bytes, keys: dict) -> str:
    """Return the key id that signed the manifest; raise BundleError otherwise."""
    try:
        sig = json.loads(sig_bytes)
        if sig.get("format") != SIG_FORMAT or sig.get("alg") != "ed25519":
            raise ValueError
        kid = str(sig["key_id"])
        signature = base64.b64decode(sig["signature"], validate=True)
    except Exception:
        raise BundleError("The update's signature is damaged.", "bad-signature")
    key = keys.get(kid)
    if key is None:
        raise BundleError("This update wasn't signed by RetroBoot, so it can't be installed.", "untrusted-key")
    from cryptography.exceptions import InvalidSignature
    try:
        key.verify(signature, manifest_bytes)
    except InvalidSignature:
        raise BundleError("This update's signature doesn't match its contents, so it can't be installed.",
                          "bad-signature")
    return kid


def signature_document(kid: str, signature: bytes) -> bytes:
    return json.dumps({"format": SIG_FORMAT, "alg": "ed25519", "key_id": kid,
                       "signature": base64.b64encode(signature).decode()}, indent=1).encode() + b"\n"


# --- manifest --------------------------------------------------------------------------

def check_manifest(m: dict) -> dict:
    """Structural validation of a (signed) manifest."""
    def need(cond, what):
        if not cond:
            raise BundleError(f"The update's description is invalid ({what}).", "invalid-manifest")
    need(isinstance(m, dict) and m.get("format") == FORMAT, "format")
    need(m.get("product") == "RetroBoot", "product")
    need(VERSION_RE.match(str(m.get("version", ""))), "version")
    need(BUILD_RE.match(str(m.get("build_id", ""))), "build")
    need(m.get("flavor") in FLAVORS, "flavor")
    need(VERSION_RE.match(str(m.get("min_version", ""))), "min_version")
    need(type(m.get("settings_schema")) is int and type(m.get("settings_schema_min")) is int, "settings schema")
    root, files = m.get("root") or {}, m.get("files") or {}
    need(root.get("member") == "root.sqfs" and root.get("format") == "squashfs", "root")
    need(type(root.get("bytes")) is int and 0 < root["bytes"] <= MAX_ROOT, "root size")
    need(type(root.get("installed_bytes")) is int and root["installed_bytes"] > 0, "installed size")
    need(re.fullmatch(r"[0-9a-f]{64}", str(root.get("sha256", ""))), "root hash")
    need(files.get("member") == "root-files.json.gz", "file list")
    need(type(files.get("bytes")) is int and 0 < files["bytes"] <= MAX_FILES, "file list size")
    need(re.fullmatch(r"[0-9a-f]{64}", str(files.get("sha256", ""))), "file list hash")
    kernel = m.get("kernel") or {}
    need(isinstance(kernel.get("cmdline"), str) and kernel["cmdline"], "kernel arguments")
    return m


class Bundle:
    """An update bundle file on local storage (random access through tar offsets)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.members: dict[str, tuple[int, int]] = {}
        self.manifest: dict = {}
        self.manifest_bytes = b""
        self.sig_bytes = b""
        self.key_id: str | None = None
        self._index()

    def _index(self) -> None:
        try:
            with tarfile.open(self.path, "r:") as tf:
                names = []
                for ti in tf:
                    if not ti.isreg():
                        raise BundleError("The update file contains something unexpected.")
                    names.append(ti.name)
                    self.members[ti.name] = (ti.offset_data, ti.size)
        except (tarfile.TarError, OSError):
            raise BundleError("This isn't a RetroBoot update file, or it is damaged.", "not-a-bundle")
        if tuple(names) != MEMBERS:
            raise BundleError("This isn't a RetroBoot update file, or it is damaged.", "not-a-bundle")
        mo, ms = self.members["manifest.json"]
        so, ss = self.members["manifest.sig"]
        if ms > MAX_MANIFEST or ss > MAX_SIG:
            raise BundleError("The update's description is too large.", "invalid-manifest")
        with open(self.path, "rb") as f:
            f.seek(mo)
            self.manifest_bytes = f.read(ms)
            f.seek(so)
            self.sig_bytes = f.read(ss)

    def verify(self, keys: dict, progress: Callable[[float, str], None] | None = None) -> dict:
        """Signature first, then every payload hash.  Returns the trusted manifest."""
        self.key_id = verify_signature(self.manifest_bytes, self.sig_bytes, keys)
        try:
            manifest = json.loads(self.manifest_bytes)
        except ValueError:
            raise BundleError("The update's description is invalid.", "invalid-manifest")
        check_manifest(manifest)
        total = manifest["root"]["bytes"] + manifest["files"]["bytes"]
        done = 0
        for part in ("files", "root"):
            spec = manifest[part]
            off, size = self.members[spec["member"]]
            if size != spec["bytes"]:
                raise BundleError("The update file is incomplete or damaged.", "payload-mismatch")
            base = done

            def prog(n, base=base):
                if progress:
                    progress((base + n) / total, "Checking the update…")
            if sha256_file(self.path, off, size, prog) != spec["sha256"]:
                raise BundleError("The update file is damaged (a checksum doesn't match).", "payload-mismatch")
            done += size
        self.manifest = manifest
        return manifest

    def member_range(self, name: str) -> tuple[int, int]:
        return self.members[name]

    def file_list(self) -> list:
        off, size = self.members["root-files.json.gz"]
        with open(self.path, "rb") as f:
            f.seek(off)
            data = f.read(size)
        if hashlib.sha256(data).hexdigest() != self.manifest["files"]["sha256"]:
            raise BundleError("The update file is damaged (a checksum doesn't match).", "payload-mismatch")
        doc = json.loads(gzip.decompress(data))
        if doc.get("format") != FILES_FORMAT or not isinstance(doc.get("entries"), list):
            raise BundleError("The update's file list is invalid.", "invalid-manifest")
        return doc["entries"]


def compatibility(manifest: dict, installed: dict, settings_schema: int, *, allow_older: bool = False,
                  allow_flavor: bool = False, allow_same: bool = False) -> None:
    """Raise BundleError when this device should not install the manifest.

    installed: the running release (``/usr/lib/retroboot/release.json``)."""
    cur_v, cur_b = installed.get("version", "0.0.0"), installed.get("build_id")
    if version_key(cur_v) < version_key(manifest["min_version"]):
        raise BundleError(f"This update needs RetroBoot {manifest['min_version']} or later first.", "too-old")
    if manifest["flavor"] != installed.get("flavor") and not allow_flavor:
        raise BundleError("This update is for a different kind of RetroBoot "
                          f"({manifest['flavor']}, this one is {installed.get('flavor')}).", "wrong-flavor")
    new, cur = version_key(manifest["version"], manifest["build_id"]), version_key(cur_v, cur_b)
    if manifest["build_id"] == cur_b and not allow_same:
        raise BundleError(f"RetroBoot {describe_version(cur_v, cur_b)} is already installed.", "same-version")
    if new < cur and not allow_older:
        raise BundleError("This update is older than the RetroBoot that is installed. "
                          "To go back, use “Go Back to the Previous Version”.", "older")
    if settings_schema < manifest["settings_schema_min"]:
        raise BundleError("This update can't read this RetroBoot's settings. Install the updates in between first.",
                          "settings-too-old")
