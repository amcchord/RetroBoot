#!/usr/bin/env python3
"""Manifest for the dedicated OS X runtime; build checks are not native QA."""
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
package, work = map(Path, sys.argv[1:])
lock = json.loads((root / "emulator/manifests/osx.lock.json").read_text())
files = ["bin/qemu-system-ppc", "bin/qemu-img", "share/qemu/openbios-ppc-osx",
         "share/qemu/openbios-ppc", "share/qemu/qemu_vga.ndrv", "share/qemu/macosx-flash-seed.img"]
manifest = {"schema_version": 1, "host": {"system": platform.system(), "machine": platform.machine()},
            "sources": lock, "prepared": json.loads((work / "prepared.json").read_text()),
            "compiler": subprocess.check_output(["cc", "--version"], text=True).splitlines()[0],
            "build": {"extra_cflags": "-O2", "display_backend": "retroboot", "openbios": "source",
                      "profile": "macosx-qemu10", "classic_cpu_optimizations": False},
            "features": lock["features"],
            "files": {name: {"sha256": hashlib.sha256((package / name).read_bytes()).hexdigest(),
                             "bytes": (package / name).stat().st_size} for name in files},
            "dependencies": subprocess.check_output(["pkg-config", "--modversion", "glib-2.0", "pixman-1", "slirp", "sdl2", "alsa"], text=True).splitlines(),
            "runtime_qualification": "not run by build; see native lab evidence"}
(package / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(json.dumps({"package": str(package), "profile": manifest["build"]["profile"],
                  "features": manifest["features"]}, indent=2))
