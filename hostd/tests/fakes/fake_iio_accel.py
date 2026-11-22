#!/usr/bin/env python3
"""A fake IIO accelerometer tree for hostd's orientation.py (tests and lab).

Writes ROOT/iio:device0/{name,in_accel_x_raw,in_accel_y_raw,in_accel_z_raw,
in_accel_scale[,in_accel_mount_matrix]} the way a kernel driver presents a
3-axis accelerometer, holding the device in a given orientation (iio-sensor-
proxy's convention after the mount matrix: normal upright reads y = -1 g).
Point hostd's ``iio_root`` at ROOT.

    fake_iio_accel.py ROOT normal|left-up|right-up|bottom-up|flat [--matrix "0, 1, 0; -1, 0, 0; 0, 0, 1"]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

G = 9.80665
SCALE = 0.009806650   # m/s^2 per raw unit: 1 g = 1000 raw
# (x, y, z) in g for each pose, in the device frame (after the mount matrix)
POSES = {"normal": (0.0, -1.0, 0.0), "bottom-up": (0.0, 1.0, 0.0), "left-up": (1.0, 0.0, 0.0),
         "right-up": (-1.0, 0.0, 0.0), "flat": (0.0, 0.0, -1.0), "tilted-normal": (0.17, -0.7, -0.69)}


def unapply(matrix: str | None, v):
    """The raw sensor reading that the mount matrix turns into v (matrices
    in the hwdb are rotations: the inverse is the transpose)."""
    if not matrix:
        return v
    m = [[float(x) for x in row.split(",")] for row in matrix.split(";")]
    return tuple(sum(m[r][c] * v[r] for r in range(3)) for c in range(3))


def write(root: str | Path, pose: str, matrix: str | None = None, device: str = "iio:device0",
          name: str = "accel_3d") -> Path:
    d = Path(root) / device
    d.mkdir(parents=True, exist_ok=True)
    (d / "name").write_text(name + "\n")
    (d / "in_accel_scale").write_text(f"{SCALE:.9f}\n")
    if matrix:
        (d / "in_accel_mount_matrix").write_text(matrix + "\n")
    raw = unapply(matrix, POSES[pose])
    for axis, g in zip("xyz", raw):
        tmp = d / f".in_accel_{axis}_raw"
        tmp.write_text(f"{round(g * G / SCALE)}\n")
        tmp.replace(d / f"in_accel_{axis}_raw")
    return d


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("root")
    p.add_argument("pose", choices=sorted(POSES))
    p.add_argument("--matrix")
    a = p.parse_args()
    print(write(a.root, a.pose, a.matrix))
    return 0


if __name__ == "__main__":
    sys.exit(main())
