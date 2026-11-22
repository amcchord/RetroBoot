#!/usr/bin/env python3
"""Fetch a container image by digest from a public OCI registry and unpack it.

Used only to obtain the exact OpenBIOS builder toolchain that produced
ClassicMac's pinned ``openbios-ppc`` (ghcr.io/openbios/openbios-builder).
Every byte is content-addressed: the manifest must hash to the requested
digest, every blob to its manifest digest, and every uncompressed layer to the
config's ``rootfs.diff_ids``.  Layers are applied in order with OCI whiteouts.

usage: oci_rootfs.py --image ghcr.io/openbios/openbios-builder \
                     --digest sha256:... --dest DIR
Result: DIR/rootfs (the filesystem) and DIR/blobs/ (verified cache).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

ACCEPT = ", ".join([
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def http(url: str, token: str | None, accept: str | None = None) -> bytes:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if accept:
        req.add_header("Accept", accept)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def download(url: str, token: str, dest: Path, digest: str) -> None:
    if dest.exists() and sha256_file(dest) == digest.split(":", 1)[1]:
        return
    tmp = dest.with_suffix(".part")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=600) as r, tmp.open("wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    if sha256_file(tmp) != digest.split(":", 1)[1]:
        tmp.unlink()
        raise SystemExit(f"blob {digest} failed verification")
    tmp.rename(dest)


def sha256_file(path: Path, gunzip: bool = False) -> str:
    h = hashlib.sha256()
    opener = gzip.open if gunzip else open
    with opener(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def apply_layer(rootfs: Path, layer: Path) -> None:
    """Apply OCI whiteouts, then extract the layer with tar (owners/modes kept)."""
    with tarfile.open(layer, "r:gz") as tf:
        names = tf.getnames()
    for name in names:
        base = os.path.basename(name)
        parent = rootfs / os.path.dirname(name)
        if base == ".wh..wh..opq":
            if parent.is_dir():
                for child in parent.iterdir():
                    shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
        elif base.startswith(".wh."):
            target = parent / base[4:]
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
    subprocess.run(["tar", "-xzf", str(layer), "-C", str(rootfs), "--numeric-owner",
                    "--exclude=.wh.*", "--exclude=*/.wh.*"], check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", required=True, help="registry/repository, e.g. ghcr.io/openbios/openbios-builder")
    ap.add_argument("--digest", required=True, help="manifest digest sha256:...")
    ap.add_argument("--dest", required=True, type=Path)
    a = ap.parse_args()
    registry, repo = a.image.split("/", 1)
    dest = a.dest.resolve()
    stamp = dest / "rootfs.digest"
    if stamp.is_file() and stamp.read_text().strip() == a.digest and (dest / "rootfs").is_dir():
        print(dest / "rootfs")
        return 0
    blobs = dest / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    token = json.loads(http(f"https://{registry}/token?scope=repository:{repo}:pull", None))["token"]
    raw = http(f"https://{registry}/v2/{repo}/manifests/{a.digest}", token, ACCEPT)
    if "sha256:" + hashlib.sha256(raw).hexdigest() != a.digest:
        raise SystemExit("manifest does not match the requested digest")
    manifest = json.loads(raw)
    cfg_digest = manifest["config"]["digest"]
    download(f"https://{registry}/v2/{repo}/blobs/{cfg_digest}", token, blobs / cfg_digest.split(":")[1], cfg_digest)
    config = json.loads((blobs / cfg_digest.split(":")[1]).read_text())
    diff_ids = config["rootfs"]["diff_ids"]
    layers = manifest["layers"]
    if len(layers) != len(diff_ids):
        raise SystemExit("layer count does not match the image config")
    rootfs = dest / "rootfs"
    if rootfs.exists():
        shutil.rmtree(rootfs)
    rootfs.mkdir()
    for layer, diff_id in zip(layers, diff_ids):
        path = blobs / layer["digest"].split(":")[1]
        download(f"https://{registry}/v2/{repo}/blobs/{layer['digest']}", token, path, layer["digest"])
        if "sha256:" + sha256_file(path, gunzip=True) != diff_id:
            raise SystemExit(f"layer {layer['digest']} does not match diff_id {diff_id}")
        apply_layer(rootfs, path)
    stamp.write_text(a.digest + "\n")
    print(rootfs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
