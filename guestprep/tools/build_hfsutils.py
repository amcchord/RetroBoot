#!/usr/bin/env python3
"""Build pinned hfsutils with per-install state, outside a user home directory.

    python3 guestprep/tools/build_hfsutils.py WORK --out TOOLS

Requires a C compiler and make. Set RB_HFSUTILS_DIR=TOOLS for install68k, or
put TOOLS on PATH. WORK and TOOLS must not already exist. The downloaded
source is retained in WORK and its license is copied beside the binaries.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

URL = "https://deb.debian.org/debian/pool/main/h/hfsutils/hfsutils_3.2.6.orig.tar.gz"
SHA256 = "bc9d22d6d252b920ec9cdf18e00b7655a6189b3f34f42e58d5bb152957289840"
COMMANDS = ("hmount", "humount", "hmkdir", "hcopy", "hdel", "hformat", "hls")
OLD_STATE = 'home = getenv("HOME");'
NEW_STATE = 'home = getenv("RB_HFS_STATE");\n  if (!home) home = getenv("HOME");'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    work, output = args.work.absolute(), args.out.absolute()
    if work.exists() or output.exists():
        parser.error("WORK and TOOLS must not already exist")
    work.mkdir(parents=True)
    archive = work / "hfsutils-3.2.6.tar.gz"
    with urllib.request.urlopen(URL, timeout=60) as response:
        blob = response.read()
    if hashlib.sha256(blob).hexdigest() != SHA256:
        raise RuntimeError("Downloaded hfsutils source has the wrong SHA-256")
    archive.write_bytes(blob)
    with tarfile.open(archive) as source:
        source.extractall(work, filter="data")
    source = work / "hfsutils-3.2.6"
    state = source / "hcwd.c"
    original = state.read_text()
    if original.count(OLD_STATE) != 1:
        raise RuntimeError("Unexpected hfsutils state-path source")
    state.write_text(original.replace(OLD_STATE, NEW_STATE))
    # This source predates C99; current GCC defaults otherwise reject its
    # configure probe's implicit-int main before compilation even begins.
    environment = {**os.environ, "CFLAGS": "-O2 -std=gnu89"}
    with (work / "build.log").open("w") as log:
        for command in (["./configure", "--without-tcl", "--without-tk"], ["make", "-j2"]):
            subprocess.run(command, cwd=source, env=environment, stdout=log,
                           stderr=subprocess.STDOUT, check=True)
    output.mkdir(parents=True)
    binary = output / "hfsutil"
    shutil.copy2(source / "hfsutil", binary)
    for command in COMMANDS:
        (output / command).symlink_to("hfsutil")
    shutil.copy2(source / "COPYING", output / "COPYING")
    marker = {"version": "3.2.6", "source_url": URL, "source_sha256": SHA256,
              "state_environment": "RB_HFS_STATE", "source_patch": {"before": OLD_STATE, "after": NEW_STATE},
              "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
    (output / "retroboot-hfsutils.json").write_text(json.dumps(marker, indent=2) + "\n")
    print(json.dumps({"tools": str(output), **marker}, indent=2))


if __name__ == "__main__":
    main()
