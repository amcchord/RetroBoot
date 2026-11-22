"""Read SystemVersion.plist from a stopped OS X disk without mounting or writing it.

The existing read-only HFS reader runs in a bounded child. This is identity
metadata, not disk repair, a full filesystem check, or a readiness signal.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys

VERSION = re.compile(r"10\.[234](?:\.[0-9]{1,2})?\Z")
BUILD = re.compile(r"[0-9]{1,2}[A-Z][0-9]{1,5}[a-z]?\Z")
PRODUCT_VERSION = re.compile(r"[0-9]{1,2}(?:\.[0-9]{1,2}){1,2}\Z")
PLIST_PATH = ("System", "Library", "CoreServices", "SystemVersion.plist")


def validate(profile: str, version, build) -> None:
    if version is not None and (not isinstance(version, str) or not VERSION.fullmatch(version)
                                or not (version == profile[7:] or version.startswith(profile[7:] + "."))):
        raise ValueError("The installed Mac OS X version does not match this Mac's OS profile.")
    if build is not None and (version is None or not isinstance(build, str) or not BUILD.fullmatch(build)):
        raise ValueError("The Mac OS X build record is invalid.")


class _BoundedReader:
    def __init__(self, f):
        self.f, self.remaining = f, 192 * 1024 * 1024

    def seek(self, *args):
        return self.f.seek(*args)

    def read(self, size=-1):
        if not 0 <= size <= min(self.remaining, 64 * 1024 * 1024):
            raise ValueError("OS X identity read exceeds its limit")
        self.remaining -= size
        return self.f.read(size)


def _lookup(volume, path: tuple[str, ...]):
    # Scan read-only leaf records once. Keep only this fixed path's candidate
    # components, rather than building every catalog path in a large guest.
    candidates = []
    for node, offset in volume._tree_leaf_records("catalog"):
        parsed = volume._parse_catalog(node, offset)
        if parsed is None or parsed[1] not in path:
            continue
        candidates.append(parsed)
        if len(candidates) > 1024:
            return None
    parent = 2  # HFS/HFS+ volume root
    for index, name in enumerate(path):
        found = [entry for owner, candidate, entry in candidates if owner == parent and candidate == name]
        if len(found) != 1:
            return None
        entry = found[0]
        if index < len(path) - 1 and entry.kind != "folder":
            return None
        parent = entry.cnid
    return entry


def _read(disk: Path, apps: Path) -> dict | None:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024, 384 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    sys.path.insert(0, str(apps))
    from rbapps.volume import open_volume, find_volumes
    fd = os.open(disk, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            return None
        reader = _BoundedReader(f)
        if len(find_volumes(reader, st.st_size)) != 1:
            return None  # the selected startup volume is ambiguous on a multi-volume disk
        volume = open_volume(reader, st.st_size)
        entry = _lookup(volume, PLIST_PATH)
        if entry is not None and entry.kind == "file":
            if not 0 < entry.data_size <= 65536:
                return None
            data = plistlib.loads(volume.read_fork(entry, "data"))
            if not isinstance(data, dict) or data.get("ProductName") != "Mac OS X":
                return None
            version, build = data.get("ProductVersion"), data.get("ProductBuildVersion")
            if not isinstance(version, str) or not PRODUCT_VERSION.fullmatch(version) \
                    or not isinstance(build, str) or not BUILD.fullmatch(build):
                return None
            return {"os_version": version, "os_build": build}
    return None


def read(disk: Path, apps: Path) -> dict | None:
    try:
        p = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--read", str(disk), str(apps)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
        if p.returncode or len(p.stdout) > 1024:
            return None
        value = json.loads(p.stdout)
        if not isinstance(value, dict) or set(value) != {"os_version", "os_build"}:
            return None
        if not isinstance(value["os_version"], str) or not PRODUCT_VERSION.fullmatch(value["os_version"]) \
                or not isinstance(value["os_build"], str) or not BUILD.fullmatch(value["os_build"]):
            return None
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def refresh(machine, apps: Path) -> dict | None:
    """Caller holds lifecycle ownership; QEMU must not have opened the disk yet."""
    from .machines import MachineUnavailable
    identity = read(machine.disk, apps)
    if identity is None:
        # An unreadable/blank disk cannot authorize controls from stale metadata.
        machine.drop_meta("os_version", "os_build")
        return None
    try:
        validate(machine.guest_os, identity["os_version"], identity["os_build"])
    except ValueError as exc:
        raise MachineUnavailable(str(exc)) from None
    machine.update_meta(**identity)
    return identity


if __name__ == "__main__":
    try:
        value = _read(Path(sys.argv[2]), Path(sys.argv[3])) if sys.argv[1] == "--read" else None
    except Exception:
        value = None
    print(json.dumps(value))
