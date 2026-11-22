"""Command line for Software Update (lab bootstrap and diagnostics).

    python3 -m retroboot.update status
    python3 -m retroboot.update verify BUNDLE [--keys DIR]
    python3 -m retroboot.update apply BUNDLE [--keys DIR] [--arm] [--allow-older] [--allow-flavor]
    python3 -m retroboot.update update-disk --target /dev/DISK      # installer: keep Mac OS and settings

``apply`` does what hostd's Software Update does, without hostd: verify, write the
other slot (converting a pre-slot ESP once), and with ``--arm`` copy the host state
again and give the new slot its one try at the next restart.  It is how the lab
updates a device whose hostd predates Software Update (RetroBoot 0.2.x): the lab
tool copies this package and the bundle to the device and runs it as root, then
restarts RetroBoot through its management API (a clean Mac OS shut down).
Progress goes to stdout as JSON lines.  Runs as a copy of this directory under any
package name (relative imports only).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import slotenv
from .bundle import Bundle, BundleError, compatibility, trusted_keys
from .writer import RealOps, SlotWriter, WriteError, release_info

DATA = Path("/var/lib/retroboot")


def emit(**kw) -> None:
    sys.stdout.write(json.dumps(kw) + "\n")
    sys.stdout.flush()


def boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def status(a) -> int:
    lay = None
    try:
        lay = slotenv.layout()
    except slotenv.SlotError as exc:
        emit(layout_error=str(exc))
    env, source = slotenv.read_env(Path(a.esp))
    cmd = slotenv.read_cmdline()
    booted = slotenv.booted_slot(cmd, lay)
    emit(layout=lay, env=env, env_source=source, booted=booted, release=release_info(Path("/")),
         grub_slotted=slotenv.grub_cfg_is_slotted(Path(a.esp)),
         boot=slotenv.boot_state(env, cmd, booted, slotenv.grub_cfg_is_slotted(Path(a.esp))),
         slots={s: slotenv.read_slot_cfg(Path(a.esp), s) for s in slotenv.SLOTS})
    return 0


def verify(a) -> int:
    b = Bundle(Path(a.bundle))
    m = b.verify(trusted_keys(a.keys), lambda f, msg: None)
    emit(ok=True, key_id=b.key_id, version=m["version"], build_id=m["build_id"], flavor=m["flavor"],
         root_bytes=m["root"]["bytes"], installed_bytes=m["root"]["installed_bytes"])
    return 0


def snapshot_settings(build_id: str) -> None:
    dest = DATA / "update" / "settings" / "".join(c if c.isalnum() or c in "._-" else "_" for c in build_id)
    dest.mkdir(parents=True, exist_ok=True)
    for src in (DATA / "config/settings.json", DATA / "config/settings.json.bak", DATA / "state/known-good.json"):
        if src.is_file():
            shutil.copy2(src, dest / src.name)


def write_state(update: dict) -> None:
    path = DATA / "update" / "state.json"
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    state.update(update)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("state.json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def apply(a) -> int:
    esp = Path(a.esp)
    os.listdir(esp)
    lay = slotenv.layout()
    cmd = slotenv.read_cmdline()
    env, _ = slotenv.read_env(esp)
    booted = slotenv.booted_slot(cmd, lay)
    if slotenv.grub_cfg_is_slotted(esp) and booted != env["rb_active"]:
        raise WriteError("RetroBoot isn't running from its committed slot; restart it first.", "not-active")
    lay["current"] = booted
    installed = release_info(Path("/"))
    try:
        schema_now = json.loads((DATA / "config/settings.json").read_text()).get("schema", 1)
    except (OSError, ValueError):
        schema_now = 1
    emit(phase="verify", progress=0.0, message="Checking the update…")
    b = Bundle(Path(a.bundle))
    m = b.verify(trusted_keys(a.keys), lambda f, msg: emit(phase="verify", progress=round(0.15 * f, 3), message=msg))
    compatibility(m, installed, int(schema_now), allow_older=a.allow_older, allow_flavor=a.allow_flavor,
                  allow_same=a.allow_older)
    write_state({"staged": {"version": m["version"], "build_id": m["build_id"], "flavor": m["flavor"],
                            "status": "writing", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "notes": str(m.get("notes") or "")[:2000], "settings_schema": m["settings_schema"]}})
    writer = SlotWriter(esp, lay, Path("/run/retroboot/update"), ops=RealOps(a.bwlimit))
    last = [0.0]

    def prog(f, msg):
        if f is None or time.monotonic() - last[0] > 1 or f >= 1:
            last[0] = time.monotonic()
            emit(phase="write", progress=round(0.15 + 0.85 * (f or 0), 3), message=msg)
    res = writer.write(b, prog)
    from .bundle import describe_version
    staged = {"version": m["version"], "build_id": m["build_id"], "flavor": m["flavor"], "status": "ready",
              "slot": res["slot"], "label": describe_version(m["version"], m["build_id"]),
              "notes": str(m.get("notes") or "")[:2000], "settings_schema": m["settings_schema"],
              "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "converted_grub": res["converted_grub"]}
    write_state({"staged": staged})
    emit(phase="staged", progress=1.0, slot=res["slot"], converted_grub=res["converted_grub"],
         carried=res["carried"])
    if a.arm:
        writer.resync_state(res["slot"])
        snapshot_settings(installed.get("build_id") or "unknown")
        staged["status"] = "restarting"
        write_state({"staged": staged, "armed": {"slot": res["slot"], "boot_id": boot_id(), "via": "cli",
                                                 "from": {"version": installed.get("version"),
                                                          "build_id": installed.get("build_id"),
                                                          "slot": booted}}})
        slotenv.update_env(esp, rb_next=res["slot"], rb_tries="1")
        emit(phase="armed", progress=1.0, slot=res["slot"],
             message="The new version starts at the next restart (once; it falls back unless it confirms).")
    return 0


LIVE_ROOT = "/run/retroboot/live/rootfs"
BOOT_JSON = "/usr/lib/retroboot/boot/boot.json"


def _run(argv: list[str]) -> None:
    import subprocess
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode:
        raise WriteError(f"{os.path.basename(argv[0])} failed: {r.stderr.strip()[-200:]}", "mount-failed")


def _mounted_devices() -> set[str]:
    out = set()
    with open("/proc/self/mountinfo") as f:
        for line in f:
            parts = line.split(" - ")
            if len(parts) == 2:
                out.add(os.path.realpath(parts[1].split()[1]))
    return out


def update_disk(a) -> int:
    """The installer's "Update RetroBoot on this disk": the live system's own root into the
    other slot of an installed disk (no signature: the installer medium is the trust)."""
    name = os.path.basename(os.path.realpath(a.target))
    sysdir = Path("/sys/block") / name
    if not a.target.startswith("/dev/") or not sysdir.is_dir():
        raise WriteError("Choose a whole disk.", "not-a-disk")
    try:
        lay = slotenv.disk_layout(sysdir)
    except slotenv.SlotError:
        raise WriteError("This disk doesn't hold a RetroBoot installation this installer can update. "
                         "Nothing was changed.", "not-retroboot")
    medium = Path("/run/retroboot/live/medium-device")
    if medium.is_file():
        mdev = os.path.basename(os.path.realpath(medium.read_text().strip()))
        if mdev == name or (Path("/sys/class/block") / mdev / "partition").exists() and \
                Path(os.path.realpath(Path("/sys/class/block") / mdev)).parent.name == name:
            raise WriteError("That is the installer's own disk.", "boot-medium")
    busy = _mounted_devices() & {os.path.realpath(lay[r]) for r in ("esp", "a", "b", "data")}
    if busy:
        raise WriteError("This disk is in use.", "busy")
    source = Path(a.source)
    release = release_info(source)
    if not release.get("build_id"):
        raise WriteError("The installer's own system couldn't be found.", "no-source")
    work = Path("/run/retroboot/update-disk")
    esp, old, data = work / "esp", work / "old", work / "data"
    mounts = []
    try:
        for p in (esp, old, data):
            p.mkdir(parents=True, exist_ok=True)
        _run(["mount", "-t", "vfat", "-o", "umask=0077,shortname=mixed", lay["esp"], str(esp)])
        mounts.append(esp)
        if not (esp / "grub" / "grub.cfg").is_file():
            raise WriteError("This disk's startup files are missing, so it can't be updated. Nothing was changed.",
                             "no-grub")
        env, _ = slotenv.read_env(esp)
        slotted = slotenv.grub_cfg_is_slotted(esp)
        active = env["rb_active"] if slotted else "a"
        if not slotted and not slotenv.legacy_cmdline(esp):
            raise WriteError("This disk's startup configuration isn't one this installer recognizes. "
                             "Nothing was changed.", "unknown-grub")
        _run(["mount", "-t", "ext4", "-o", "ro", lay[active], str(old)])
        mounts.append(old)
        installed = release_info(old)
        if installed.get("product") != "RetroBoot":
            raise WriteError("The RetroBoot system on this disk couldn't be read. Nothing was changed.", "no-release")
        _run(["mount", "-t", "xfs", "-o", "noatime", lay["data"], str(data)])
        mounts.append(data)
        if not (data / "machines").is_dir() or not (data / "config").is_dir():
            raise WriteError("This disk's Mac OS and settings partition isn't one RetroBoot recognizes. "
                             "Nothing was changed.", "unknown-data")
        from .bundle import version_key
        if installed.get("build_id") == release["build_id"] and not a.allow_older:
            raise WriteError(f"This disk already has RetroBoot {release.get('version')}.", "same-version")
        if version_key(release.get("version", "0"), release["build_id"]) < \
                version_key(installed.get("version", "0"), installed.get("build_id")) and not a.allow_older:
            raise WriteError(f"This disk has a newer RetroBoot ({installed.get('version')}).", "older")
        emit(phase="verify", progress=0.02, message="Reading the installer's system…",
             installed=installed.get("version"), new=release.get("version"))
        from . import tree as tree_mod
        entries = tree_mod.build(str(source))["entries"]
        size = sum(os.lstat(source / e[0]).st_size for e in entries if e[1] == "f")
        lay["current"] = active
        cmdline = json.loads(Path(a.boot_json).read_text())["kernel_cmdline"]
        writer = SlotWriter(esp, lay, work / "w", ops=RealOps(bwlimit_kbps=None, low_priority=False),
                            running_root=old, data_dir=data)
        last = [0.0]

        def prog(f, msg):
            if f is None or time.monotonic() - last[0] > 1 or f >= 1:
                last[0] = time.monotonic()
                emit(phase="write", progress=round(0.1 + 0.85 * (f or 0), 3), message=msg)
        res = writer.write_tree(source, entries, release, cmdline, size, prog)
        snap = data / "update" / "settings" / "".join(c if c.isalnum() or c in "._-" else "_"
                                                      for c in installed.get("build_id", "unknown"))
        snap.mkdir(parents=True, exist_ok=True)
        for src in (data / "config/settings.json", data / "config/settings.json.bak", data / "state/known-good.json"):
            if src.is_file():
                shutil.copy2(src, snap / src.name)
        from .bundle import describe_version
        state_path = data / "update" / "state.json"
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state = {}
        state["staged"] = {"version": release.get("version"), "build_id": release["build_id"],
                           "flavor": release.get("flavor"), "status": "restarting", "slot": res["slot"],
                           "label": describe_version(release.get("version", ""), release["build_id"]),
                           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "via": "installer"}
        state["armed"] = {"slot": res["slot"], "boot_id": boot_id(), "via": "installer",
                          "from": {"version": installed.get("version"), "build_id": installed.get("build_id"),
                                   "slot": active}}
        tmp = state_path.with_name("state.json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, state_path)
        slotenv.update_env(esp, rb_next=res["slot"], rb_tries="1")
        emit(phase="armed", progress=1.0, slot=res["slot"], converted_grub=res["converted_grub"],
             message=f"RetroBoot {release.get('version')} is installed.")
        return 0
    finally:
        import subprocess
        subprocess.run(["sync"])
        for p in reversed(mounts):
            subprocess.run(["umount", str(p)], capture_output=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="retroboot.update")
    ap.add_argument("--esp", default="/boot/efi")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    v = sub.add_parser("verify")
    v.add_argument("bundle")
    v.add_argument("--keys", default="/usr/lib/retroboot/update/keys")
    p = sub.add_parser("apply")
    p.add_argument("bundle")
    p.add_argument("--keys", default="/usr/lib/retroboot/update/keys")
    p.add_argument("--arm", action="store_true")
    p.add_argument("--allow-older", action="store_true")
    p.add_argument("--allow-flavor", action="store_true")
    p.add_argument("--bwlimit", type=int, default=32768)
    d = sub.add_parser("update-disk")
    d.add_argument("--target", required=True)
    d.add_argument("--source", default=LIVE_ROOT)
    d.add_argument("--boot-json", default=BOOT_JSON)
    d.add_argument("--allow-older", action="store_true")
    a = ap.parse_args(argv)
    try:
        return {"status": status, "verify": verify, "apply": apply, "update-disk": update_disk}[a.cmd](a)
    except (BundleError, WriteError, slotenv.SlotError) as exc:
        emit(phase="error", message=getattr(exc, "message", str(exc)), code=getattr(exc, "code", "error"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
