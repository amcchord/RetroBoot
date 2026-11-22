#!/usr/bin/env python3
"""Explicit ATI render-node failures reject realization, with no guest media."""
import argparse
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--support", type=Path, required=True)
    args = parser.parse_args()
    command = [str(args.qemu), "-M", "mac99,usb=off,audiodev=snd0",
               "-audiodev", "none,id=snd0", "-nodefaults", "-vga", "none",
               "-device", "ati-vga,model=r100,romfile=", "-display", "none",
               "-S", "-L", str(args.support)]
    checks = {}
    for disabled in (False, True):
        env = {**os.environ, "RETROBOOT_EGL_RENDER_NODE":
               "/dev/dri/retroboot-test-missing-render-node"}
        env.pop("RETROBOOT_ATI_3D", None)
        if disabled:
            env["RETROBOOT_ATI_3D"] = "off"
        result = subprocess.run(command, capture_output=True, text=True,
                                env=env, timeout=15)
        expected = ("explicit render node conflicts with disabled 3D" if disabled else
                    "explicit render node initialization failed")
        assert result.returncode != 0 and expected in result.stderr, (
            result.returncode, result.stdout, result.stderr)
        assert "3D drawing is discarded" not in result.stderr
        checks["conflicting_disable_rejected" if disabled else "missing_node_rejected"] = True
    print(json.dumps(checks))


if __name__ == "__main__":
    main()
