"""Write a new system into the inactive slot, carry host state between slots, keep the ESP current.

Synchronous (hostd runs it in a worker thread; the lab bootstrap runs it directly).
Self-contained: standard library plus the sibling modules of this package.

Order of a slot write (power may fail at any point; GRUB never boots a slot that
is not finished, because ``rb_next`` is armed only by "Restart to Update"):

1. environment: ``rb_next`` cleared and ``rb_other_ok=0`` (the target no longer holds
   a working system) before the target partition is touched;
2. ``mkfs.ext4`` the target, copy the verified root squashfs into it at idle I/O and
   CPU priority with a bandwidth limit (Mac OS keeps running);
3. verify every file of the target against the signed file list;
4. device changes: its own fstab root line, then host state copied forward from
   the running system (``CARRY_*``), Wi-Fi profiles merged;
5. unmount (clean ext4: GRUB reads it without a journal replay), then the slot's
   kernel arguments on the ESP (``slot-<x>.cfg``);
6. a device still on the pre-slot ``grub.cfg`` (RetroBoot 0.2.x) is converted once:
   ``slot-a.cfg`` with its current kernel arguments, the environment, then the new
   ``grub.cfg`` from the verified slot.  With ``rb_active=a`` it boots exactly as before.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from . import slotenv
from .bundle import Bundle, BundleError

Progress = Callable[[float | None, str], None]

# Host state on the root filesystem that belongs to this computer, not to a release.
CARRY_FILES = ("etc/hostname", "etc/hosts", "etc/machine-info", "etc/machine-id", "etc/localtime",
               "etc/timezone", "etc/adjtime", "var/lib/systemd/random-seed", "var/lib/systemd/timesync/clock",
               "var/lib/alsa/asound.state")
CARRY_GLOBS = ("etc/ssh/ssh_host_*",)
CARRY_DIRS = ("var/lib/NetworkManager", "var/lib/systemd/backlight", "var/lib/systemd/rfkill", "root/.ssh")
# Copied only when the target slot has none (an image that ships its own keeps it).  hostd moves
# per-device keys into its RBDATA overlay (config.py), so this is belt and braces for tools that
# read /etc/retroboot/hostd.json directly.
CARRY_IF_ABSENT = ("etc/retroboot/hostd.json",)
NM_LEGACY_DIR = "etc/NetworkManager/system-connections"
NM_CONF_DIR = "etc/NetworkManager/conf.d"
DATA_MOUNT = "/var/lib/retroboot"


class WriteError(Exception):
    def __init__(self, message: str, code: str = "write-failed"):
        super().__init__(message)
        self.message = message
        self.code = code


def _log(msg: str) -> None:
    sys.stderr.write(f"rb-update: {msg}\n")
    sys.stderr.flush()


class RealOps:
    """Block devices, mounts and copies on a real RetroBoot (root)."""

    def __init__(self, bwlimit_kbps: int | None = 32768, low_priority: bool = True):
        self.bwlimit = bwlimit_kbps
        self.low = low_priority

    def _prio(self) -> list[str]:
        return (["ionice", "-c", "3", "nice", "-n", "19"] if self.low and shutil.which("ionice") else [])

    def run(self, argv: list[str], timeout: float = 600) -> str:
        _log("$ " + " ".join(argv))
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise WriteError(f"{os.path.basename(argv[0])} failed: {r.stderr.strip()[-300:]}")
        return r.stdout

    def mounted(self, dev: str) -> bool:
        real = os.path.realpath(dev)
        with open("/proc/self/mountinfo") as f:
            for line in f:
                parts = line.split(" - ")
                if len(parts) == 2 and os.path.realpath(parts[1].split()[1]) == real:
                    return True
        return False

    def mkfs(self, dev: str, label: str) -> None:
        if self.mounted(dev):
            raise WriteError("The other system slot is in use.", "slot-busy")
        self.run(["wipefs", "-a", "-q", dev])
        self.run(["mkfs.ext4", "-F", "-q", "-L", label, "-E", "lazy_itable_init=0,lazy_journal_init=0", dev])

    def mount(self, dev: str, path: str, ro: bool = False) -> None:
        os.makedirs(path, exist_ok=True)
        self.run(["mount", "-t", "ext4", "-o", ("ro,noload" if ro else "rw,noatime"), dev, path])

    def umount(self, path: str) -> None:
        subprocess.run(["sync", "-f", path], capture_output=True)
        self.run(["umount", path])

    def mount_squashfs(self, file: str, offset: int, size: int, path: str) -> None:
        os.makedirs(path, exist_ok=True)
        self.run(["mount", "-t", "squashfs", "-o", f"ro,loop,offset={offset},sizelimit={size}", file, path])

    def copy_tree(self, src: str, dst: str, progress: Callable[[float], None]) -> None:
        argv = self._prio() + ["rsync", "-aHAXx", "--numeric-ids", "--no-inc-recursive", "--info=progress2"]
        if self.bwlimit:
            argv.append(f"--bwlimit={int(self.bwlimit)}")
        argv += [src.rstrip("/") + "/", dst.rstrip("/") + "/"]
        _log("$ " + " ".join(argv))
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        buf = b""
        last = 0.0
        while True:
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            buf += chunk
            *lines, buf = re.split(rb"[\r\n]", buf)
            for line in lines:
                m = re.search(rb"\s(\d+)%", line)
                if m and time.monotonic() - last > 0.5:
                    last = time.monotonic()
                    progress(int(m.group(1)) / 100)
        err = proc.stderr.read().decode(errors="replace")
        if proc.wait() != 0:
            raise WriteError(f"Copying the new system failed: {err.strip()[-300:]}", "copy-failed")

    def verify_tree(self, list_file: str, root: str, progress: Callable[[float], None]) -> list[str]:
        script = str(Path(__file__).with_name("tree.py"))
        argv = self._prio() + [sys.executable, "-I", script, "verify", list_file, root]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        result = None
        for line in proc.stdout:
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if "progress" in doc:
                progress(doc["progress"])
            else:
                result = doc
        proc.wait()
        if result is None:
            return [f"verification did not run: {proc.stderr.read()[-300:]}"]
        return list(result.get("problems") or [])

    def sync(self) -> None:
        subprocess.run(["sync"], capture_output=True)

    def size_bytes(self, dev: str) -> int | None:
        try:
            return int(Path(f"/sys/class/block/{os.path.basename(os.path.realpath(dev))}/size").read_text()) * 512
        except (OSError, ValueError):
            return None


# --- host state carried between slots ------------------------------------------------------

def _copy_entry(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)
    if src.is_symlink():
        os.symlink(os.readlink(src), dst)
    elif src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)
    try:
        st = src.lstat()
        os.lchown(dst, st.st_uid, st.st_gid)
    except OSError:
        pass


def keyfile_dir(root: Path) -> str:
    """NetworkManager's keyfile directory for a root (``[keyfile] path=`` in conf.d)."""
    path = "/" + NM_LEGACY_DIR
    conf = Path(root) / NM_CONF_DIR
    for f in sorted(conf.glob("*.conf")) if conf.is_dir() else []:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        section = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("["):
                section = line.strip("[]").strip()
            elif section == "keyfile" and line.startswith("path") and "=" in line:
                path = line.split("=", 1)[1].strip()
    return path


def _resolve(root: Path, path: str, data_dir: Path) -> Path:
    """A path of that root's view: RBDATA is shared (the running system's mount at data_dir)."""
    if path == DATA_MOUNT or path.startswith(DATA_MOUNT + "/"):
        return Path(data_dir) / path[len(DATA_MOUNT):].lstrip("/")
    return Path(root) / path.lstrip("/")


def _authorized_keys(ssh_dir: Path) -> list[str]:
    try:
        return [l for l in (ssh_dir / "authorized_keys").read_text().splitlines() if l.strip()]
    except OSError:
        return []


def _merge_authorized_keys(ssh_dir: Path, shipped: list[str]) -> None:
    """Keys the new release ships (a debug image's lab key) stay next to the carried ones:
    an update from a device whose /root/.ssh had none (a product slot) used to remove them."""
    f = ssh_dir / "authorized_keys"
    have = _authorized_keys(ssh_dir)
    add = [l for l in shipped if l not in have]
    if not add:
        return
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    f.write_text("\n".join(have + add) + "\n")
    os.chmod(f, 0o600)
    try:
        os.chown(f, 0, 0)
    except OSError:
        pass


def carry_state(src_root: Path, dst_root: Path, data_dir: Path = Path(DATA_MOUNT)) -> list[str]:
    """Copy this computer's host state from src_root into dst_root.  Returns what was copied."""
    src_root, dst_root = Path(src_root), Path(dst_root)
    done = []
    for rel in CARRY_FILES:
        s = src_root / rel
        if s.is_symlink() or s.is_file():
            _copy_entry(s, dst_root / rel)
            done.append(rel)
    for pattern in CARRY_GLOBS:
        d = os.path.dirname(pattern)
        sdir = src_root / d
        if not sdir.is_dir():
            continue
        for old in (dst_root / d).glob(os.path.basename(pattern)) if (dst_root / d).is_dir() else []:
            old.unlink()
        for s in sorted(sdir.iterdir()):
            if fnmatch.fnmatch(s.name, os.path.basename(pattern)):
                _copy_entry(s, dst_root / d / s.name)
                done.append(f"{d}/{s.name}")
    for rel in CARRY_DIRS:
        s = src_root / rel
        if s.is_dir() and not s.is_symlink():
            shipped = _authorized_keys(dst_root / rel) if rel == "root/.ssh" else []
            _copy_entry(s, dst_root / rel)
            if shipped:
                _merge_authorized_keys(dst_root / rel, shipped)
            done.append(rel + "/")
    for rel in CARRY_IF_ABSENT:
        s, d = src_root / rel, dst_root / rel
        if s.is_file() and not s.is_symlink() and not (d.exists() or d.is_symlink()):
            _copy_entry(s, d)
            done.append(rel)
    done += merge_wifi(src_root, dst_root, data_dir)
    return done


def merge_wifi(src_root: Path, dst_root: Path, data_dir: Path = Path(DATA_MOUNT)) -> list[str]:
    """Wi-Fi profiles: from wherever the running system keeps them to wherever the
    target reads them (RBDATA /var/lib/retroboot/network/connections or the slot's
    /etc).  Profiles already present at the destination are kept."""
    src = _resolve(src_root, keyfile_dir(src_root), data_dir)
    dst = _resolve(dst_root, keyfile_dir(dst_root), data_dir)
    copied = []
    if not src.is_dir():
        return copied
    if dst.exists() and src.resolve() == dst.resolve():
        return copied        # both systems read the same (shared RBDATA) directory
    dst.mkdir(parents=True, exist_ok=True)
    os.chmod(dst, 0o700)
    for f in sorted(src.iterdir()):
        if not f.is_file() or f.name.startswith("."):
            continue
        target = dst / f.name
        if target.exists():
            continue
        shutil.copy2(f, target)
        os.chmod(target, 0o600)
        try:
            os.chown(target, 0, 0)
        except OSError:
            pass
        copied.append(f"wifi:{f.name}")
    return copied


def set_fstab_root(root: Path, label: str) -> None:
    p = Path(root) / "etc" / "fstab"
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return
    out = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and not line.lstrip().startswith("#") and parts[1] == "/":
            line = re.sub(r"^\s*\S+", f"PARTLABEL={label}", line, count=1)
        out.append(line)
    p.write_text("\n".join(out) + "\n")


# --- ESP payload ---------------------------------------------------------------------------

def release_info(root: Path) -> dict:
    try:
        return json.loads((Path(root) / "usr/lib/retroboot/release.json").read_text())
    except (OSError, ValueError):
        return {}


def boot_payload(root: Path) -> tuple[Path, dict]:
    base = Path(root) / "usr/lib/retroboot/boot"
    try:
        info = json.loads((base / "boot.json").read_text())
    except (OSError, ValueError):
        info = {}
    return base, info


def sync_esp(esp: Path, root: Path, *, include_grub_cfg: bool = True) -> list[str]:
    """Bring the ESP's release files (GRUB binaries, grub.cfg, font, default checkerboard)
    to the versions shipped in root.  Device files (display.cfg, checkerboards made
    for this display, slots.env, slot-*.cfg) are never touched.  Returns changed paths."""
    base, info = boot_payload(root)
    changed = []
    for rel, digest in sorted((info.get("files_sha256") or {}).items()):
        if not rel.startswith("esp/"):
            continue
        name = rel[4:]
        if name == "grub/grub.cfg" and not include_grub_cfg:
            continue
        src = base / rel
        dst = Path(esp) / name
        try:
            data = src.read_bytes()
        except OSError:
            continue
        import hashlib
        if hashlib.sha256(data).hexdigest() != digest:
            raise WriteError(f"The startup file {name} doesn't match its checksum.", "boot-payload")
        try:
            if dst.read_bytes() == data:
                continue
        except OSError:
            pass
        if name == "grub/grub.cfg" and dst.exists():
            shutil.copy2(dst, dst.with_name("grub.cfg.prev"))
        slotenv.write_file_atomic(dst, data)
        changed.append(name)
    return changed


def install_grub_cfg(esp: Path, root: Path) -> None:
    base, info = boot_payload(root)
    data = (base / "esp/grub/grub.cfg").read_bytes()
    import hashlib
    if hashlib.sha256(data).hexdigest() != (info.get("files_sha256") or {}).get("esp/grub/grub.cfg"):
        raise WriteError("The new startup configuration doesn't match its checksum.", "boot-payload")
    if slotenv.GRUB_CFG_MARKER.encode() not in data:
        raise WriteError("The new system can't start from either slot.", "boot-payload")
    cfg = Path(esp) / "grub" / "grub.cfg"
    if cfg.exists() and not (cfg.with_name("grub.cfg.legacy")).exists():
        shutil.copy2(cfg, cfg.with_name("grub.cfg.legacy"))
    slotenv.write_file_atomic(cfg, data)


def convert_legacy(esp: Path, running_root: Path, new_root: Path) -> bool:
    """One-time switch of a pre-slot ESP to slot selection.  True if it converted."""
    if slotenv.grub_cfg_is_slotted(esp):
        return False
    cmd = slotenv.legacy_cmdline(esp)
    if not cmd:
        raise WriteError("This computer's startup configuration isn't one RetroBoot recognizes.", "unknown-grub")
    rel = release_info(running_root)
    slotenv.write_slot_cfg(esp, "a", cmd, rel.get("version", ""), rel.get("build_id", ""))
    values, source = slotenv.read_env(esp)
    if source == "none":
        slotenv.write_env(esp, slotenv.DEFAULTS)
    install_grub_cfg(esp, new_root)
    return True


# --- the writer -------------------------------------------------------------------------------

class SlotWriter:
    def __init__(self, esp: Path, lay: dict, work: Path, ops=None, running_root: Path = Path("/"),
                 data_dir: Path = Path(DATA_MOUNT)):
        self.esp = Path(esp)
        self.lay = lay
        self.work = Path(work)
        self.ops = ops or RealOps()
        self.running_root = Path(running_root)
        self.data_dir = Path(data_dir)

    def target(self) -> str:
        cur = self.lay.get("current")
        if cur not in slotenv.SLOTS:
            raise WriteError("RetroBoot isn't running from one of its system slots.", "no-slot")
        return slotenv.other(cur)

    def write(self, bundle: Bundle, progress: Progress) -> dict:
        """The verified bundle's root into the other slot."""
        m = bundle.manifest
        if not m:
            raise WriteError("The update was not checked.", "unverified")
        off, sz = bundle.member_range("root.sqfs")

        def mount_source(path):
            self.ops.mount_squashfs(str(bundle.path), off, sz, path)
            return path
        return self._write(mount_source, True, bundle.file_list(), m["version"], m["build_id"], m["flavor"],
                           m["kernel"]["cmdline"], m["root"]["installed_bytes"], progress)

    def write_tree(self, src: Path, entries: list, release: dict, cmdline: str, installed_bytes: int,
                   progress: Progress) -> dict:
        """A root directory (the installer's own live root) into the other slot."""
        return self._write(lambda path: str(src), False, entries, release.get("version", ""),
                           release.get("build_id", ""), release.get("flavor", ""), cmdline, installed_bytes, progress)

    def _write(self, mount_source, unmount_source: bool, entries: list, version: str, build_id: str, flavor: str,
               cmdline: str, installed_bytes: int, progress: Progress) -> dict:
        target = self.target()
        dev = self.lay[target]
        size = self.ops.size_bytes(dev)
        if size and installed_bytes > size * 0.9:
            raise WriteError("The new system is too large for this computer's system slot.", "too-large")
        self.work.mkdir(parents=True, exist_ok=True)
        list_file = self.work / "root-files.json.gz"
        import gzip
        list_file.write_bytes(gzip.compress(json.dumps({"format": "retroboot-root-files/1",
                                                        "entries": entries}).encode(), 6))
        # 1. The target stops being a bootable choice before it is touched.
        slotenv.update_env(self.esp, rb_next="", rb_tries="0", rb_other_ok="0")
        payload, slot_mnt = str(self.work / "payload"), str(self.work / "slot")
        mounted = []
        try:
            progress(0.02, "Preparing…")
            self.ops.mkfs(dev, slotenv.SLOT_LABEL[target])
            self.ops.mount(dev, slot_mnt)
            mounted.append(slot_mnt)
            source = mount_source(payload)
            if unmount_source:
                mounted.append(payload)
            self.ops.copy_tree(source, slot_mnt, lambda f: progress(0.05 + 0.6 * f, f"Installing RetroBoot {version}…"))
            if unmount_source:
                self.ops.umount(payload)
                mounted.remove(payload)
            problems = self.ops.verify_tree(str(list_file), slot_mnt,
                                            lambda f: progress(0.66 + 0.24 * f, "Verifying…"))
            if problems:
                _log("verification problems: " + "; ".join(problems[:10]))
                raise WriteError("The new system didn't verify after it was written.", "verify-failed")
            progress(0.92, "Finishing…")
            set_fstab_root(Path(slot_mnt), slotenv.SLOT_LABEL[target])
            carried = carry_state(self.running_root, Path(slot_mnt), self.data_dir)
            new_root_boot = self.work / "boot"
            if new_root_boot.exists():
                shutil.rmtree(new_root_boot)
            shutil.copytree(Path(slot_mnt) / "usr/lib/retroboot/boot", new_root_boot / "usr/lib/retroboot/boot")
            shutil.copy2(Path(slot_mnt) / "usr/lib/retroboot/release.json",
                         new_root_boot / "usr/lib/retroboot/release.json")
            self.ops.umount(slot_mnt)
            mounted.remove(slot_mnt)
            self.ops.sync()
        finally:
            for p in reversed(mounted):
                try:
                    self.ops.umount(p)
                except Exception as exc:  # noqa: BLE001 - best effort cleanup
                    _log(f"cleanup umount {p}: {exc}")
        slotenv.write_slot_cfg(self.esp, target, cmdline, version, build_id)
        converted = convert_legacy(self.esp, self.running_root, self.work / "boot")
        progress(1.0, f"RetroBoot {version} is ready to install.")
        return {"slot": target, "carried": carried, "converted_grub": converted,
                "version": version, "build_id": build_id, "flavor": flavor}

    def resync_state(self, slot: str) -> list[str]:
        """Copy host state into ``slot`` again (right before switching to it)."""
        if slot == self.lay.get("current"):
            return []
        mnt = str(self.work / "slot")
        self.ops.mount(self.lay[slot], mnt)
        try:
            set_fstab_root(Path(mnt), slotenv.SLOT_LABEL[slot])
            return carry_state(self.running_root, Path(mnt), self.data_dir)
        finally:
            self.ops.umount(mnt)

    def slot_release(self, slot: str) -> dict:
        """release.json of a slot (mounted read-only briefly); {} if unreadable."""
        if slot == self.lay.get("current"):
            return release_info(self.running_root)
        mnt = str(self.work / "peek")
        try:
            self.ops.mount(self.lay[slot], mnt, ro=True)
        except WriteError:
            return {}
        try:
            info = release_info(Path(mnt))
            boot = boot_payload(Path(mnt))[1]
            info["kernel_cmdline"] = boot.get("kernel_cmdline")
            info["update_capable"] = (Path(mnt) / "usr/lib/retroboot/hostd/retroboot/update").is_dir()
            return info
        finally:
            try:
                self.ops.umount(mnt)
            except WriteError:
                pass
