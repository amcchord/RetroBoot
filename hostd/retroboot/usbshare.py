"""Auto-mount removable USB flash drives into ``RetroBoot Share/USB Drives/`` (#53).

Safely mounts FAT32, exFAT, HFS+, and NTFS removable partitions with
``nosuid,nodev,noexec`` while strictly refusing system, boot, data, installer,
or Mac OS VM disk partitions. Provides list and safe-eject controls for Web
and the local Platinum panel.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Callable

from .util import run as run_cmd

log = logging.getLogger("retroboot.usbshare")

ALLOWED_FS = frozenset({"vfat", "fat32", "msdos", "exfat", "hfsplus", "hfs", "ntfs", "ntfs3"})
PROTECTED_LABELS = frozenset({"RBEFI", "BIOSBOOT", "RBROOT_A", "RBROOT_B", "RBDATA", "RBINSTEFI", "RETROBOOT"})
PROTECTED_MOUNTS = frozenset({"/", "/boot", "/boot/efi", "/boot/firmware", "/var/lib/retroboot", "/usr", "/etc"})
DEV_PATH_RE = re.compile(r"^/dev/[A-Za-z0-9_/-]+$")


class UsbDriveError(Exception):
    def __init__(self, message: str, code: str = "usb_error"):
        super().__init__(message)
        self.message = message
        self.code = code


def _safe_dir_name(raw: str | None, fallback: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9 ._()-]+", " ", (raw or "").strip())
    clean = " ".join(clean.split()).strip(" .")
    return (clean[:24] or fallback)[:24]


class UsbShare:
    """Removable USB flash drive watcher and mount manager under ``Share/USB Drives``."""

    def __init__(self, share_root: Path, *, enabled: Callable[[], bool] | bool = True,
                 runner=None, poll_s: float = 5.0,
                 mount_fn=None, umount_fn=None, statvfs_fn=None):
        self.share_root = Path(share_root)
        self._enabled_fn = enabled if callable(enabled) else (lambda: bool(enabled))
        self.run = runner or run_cmd
        self.poll_s = poll_s
        self._mount_fn = mount_fn
        self._umount_fn = umount_fn
        self._statvfs_fn = statvfs_fn or os.statvfs
        self._lock = asyncio.Lock()
        self._mounted: dict[str, dict] = {}
        self._ejected: set[str] = set()
        self._task: asyncio.Task | None = None
        self._running = False

    def is_enabled(self) -> bool:
        try:
            return bool(self._enabled_fn())
        except Exception:
            return False

    @property
    def base_dir(self) -> Path:
        return self.share_root / "USB Drives"

    def list_drives(self) -> list[dict]:
        return [{k: v for k, v in info.items() if not k.startswith("_")}
                for info in self._mounted.values()]

    async def start(self) -> None:
        self._running = True
        await self.sync()
        if self._task is None and self.poll_s > 0:
            self._task = asyncio.create_task(self._poll_loop(), name="usb-share-watch")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        await self.unmount_all()

    async def _poll_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.poll_s)
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("USB share poll error", exc_info=True)

    async def sync(self) -> list[dict]:
        if not self.is_enabled():
            await self.unmount_all()
            return []
        return await self.refresh()

    async def _scan_lsblk(self) -> list[dict]:
        rc, out, _ = await self.run(
            ["lsblk", "-J", "-b", "-o", "NAME,PATH,RM,HOTPLUG,TRAN,FSTYPE,LABEL,PARTLABEL,MOUNTPOINT,SIZE,TYPE,PKNAME"],
            timeout=10)
        if rc != 0 or not out:
            return []
        try:
            data = json.loads(out)
        except ValueError:
            return []

        protected_disks: set[str] = set()
        all_nodes: list[dict] = []

        def collect(devs, parent_rem=False, root_disk=None):
            for d in devs or []:
                name = d.get("name") or ""
                dtype = d.get("type") or ""
                disk_name = name if dtype == "disk" else (d.get("pkname") or root_disk)
                rem = parent_rem or bool(d.get("rm")) or bool(d.get("hotplug")) or d.get("tran") == "usb"
                mp = d.get("mountpoint") or ""
                lbl = (d.get("label") or "").upper()
                plbl = (d.get("partlabel") or "").upper()
                if (mp in PROTECTED_MOUNTS or mp.startswith("/var/lib/retroboot")
                        or lbl in PROTECTED_LABELS or plbl in PROTECTED_LABELS):
                    if disk_name:
                        protected_disks.add(disk_name)
                all_nodes.append({**d, "_rem": rem, "_disk": disk_name})
                collect(d.get("children"), rem, disk_name)

        collect(data.get("blockdevices"))
        candidates = []
        seen_devs = set()
        for d in all_nodes:
            path = d.get("path") or ""
            fs = str(d.get("fstype") or "").lower()
            mp = d.get("mountpoint") or ""
            disk_name = d.get("_disk") or ""
            if not path or not DEV_PATH_RE.fullmatch(path):
                continue
            if fs not in ALLOWED_FS or not d.get("_rem"):
                continue
            if disk_name in protected_disks:
                continue
            if (d.get("label") or "").upper() in PROTECTED_LABELS or (d.get("partlabel") or "").upper() in PROTECTED_LABELS:
                continue
            # Ignore partitions already mounted outside our share USB tree
            base_str = str(self.base_dir)
            if mp and not (mp == base_str or mp.startswith(base_str + "/")):
                continue
            seen_devs.add(path)
            candidates.append(d)

        # Un-ignore previously ejected devices once physically unplugged
        self._ejected = {dev for dev in self._ejected if dev in seen_devs}
        return [c for c in candidates if c.get("path") not in self._ejected]

    async def _do_mount(self, dev: str, target: Path, fstype: str) -> tuple[bool, bool]:
        if self._mount_fn is not None:
            return await self._mount_fn(dev, target, fstype)
        target.mkdir(parents=True, exist_ok=True)
        uid, gid = os.getuid(), os.getgid()
        opts = "nosuid,nodev,noexec"
        if fstype in ("vfat", "fat32", "msdos", "exfat", "ntfs", "ntfs3"):
            opts += f",uid={uid},gid={gid}"
        rc, _, _ = await self.run(["mount", "-o", opts, dev, str(target)], timeout=15)
        if rc == 0:
            return True, False
        rc, _, err = await self.run(["mount", "-o", f"ro,{opts}", dev, str(target)], timeout=15)
        if rc == 0:
            return True, True
        log.debug("failed to mount USB drive %s (%s): %s", dev, fstype, err.strip())
        return False, False

    async def _do_umount(self, target: Path, disk_dev: str | None = None) -> bool:
        if self._umount_fn is not None:
            ok = await self._umount_fn(target, disk_dev)
            if ok and target.exists():
                try:
                    target.rmdir()
                except OSError:
                    pass
            return ok
        await self.run(["sync"], timeout=10)
        rc, _, err = await self.run(["umount", str(target)], timeout=15)
        if rc != 0:
            log.warning("umount %s failed: %s", target, err.strip())
            return False
        try:
            target.rmdir()
        except OSError:
            pass
        if disk_dev and shutil.which("eject"):
            await self.run(["eject", disk_dev], timeout=10)
        return True

    def _space_of(self, target: Path, size_fallback: int | None) -> tuple[int, int]:
        try:
            st = self._statvfs_fn(target)
            total = int(st.f_blocks * st.f_frsize)
            free = int(st.f_bavail * st.f_frsize)
            return total or int(size_fallback or 0), free
        except OSError:
            return int(size_fallback or 0), 0

    async def refresh(self) -> list[dict]:
        async with self._lock:
            if not self.is_enabled():
                await self._unmount_all_locked()
                return []
            cands = await self._scan_lsblk()
            present = {c["path"] for c in cands}
            for gone in [dev for dev in self._mounted if dev not in present]:
                info = self._mounted.pop(gone)
                await self._do_umount(Path(info["mountpoint"]))

            used_names = {Path(info["mountpoint"]).name for info in self._mounted.values()}
            for c in cands:
                dev = c["path"]
                if dev in self._mounted:
                    info = self._mounted[dev]
                    total, free = self._space_of(Path(info["mountpoint"]), c.get("size"))
                    info["bytes"], info["free_bytes"] = total, free
                    continue
                fstype = str(c.get("fstype") or "").lower()
                raw_label = c.get("label") or c.get("name") or Path(dev).name
                dname = _safe_dir_name(raw_label, Path(dev).name)
                cand_name, idx = dname, 1
                while cand_name in used_names:
                    idx += 1
                    cand_name = f"{dname[:20]} {idx}"
                target = self.base_dir / cand_name
                mp_existing = c.get("mountpoint")
                ro = False
                if not mp_existing:
                    ok, ro = await self._do_mount(dev, target, fstype)
                    if not ok:
                        try:
                            target.rmdir()
                        except OSError:
                            pass
                        continue
                else:
                    target = Path(mp_existing)
                used_names.add(target.name)
                total, free = self._space_of(target, c.get("size"))
                pkname = c.get("pkname")
                disk_dev = f"/dev/{pkname}" if pkname and DEV_PATH_RE.fullmatch(f"/dev/{pkname}") else None
                self._mounted[dev] = {
                    "device": dev,
                    "name": c.get("name") or Path(dev).name,
                    "label": str(c.get("label") or cand_name),
                    "fstype": fstype,
                    "mountpoint": str(target),
                    "share_path": f"USB Drives/{target.name}",
                    "bytes": total,
                    "free_bytes": free,
                    "read_only": ro,
                    "_disk_dev": disk_dev,
                }
            return [{k: v for k, v in info.items() if not k.startswith("_")}
                    for info in self._mounted.values()]

    async def unmount_all(self) -> None:
        async with self._lock:
            await self._unmount_all_locked()

    async def _unmount_all_locked(self) -> None:
        for dev, info in list(self._mounted.items()):
            await self._do_umount(Path(info["mountpoint"]))
            self._mounted.pop(dev, None)

    async def eject(self, device: str) -> dict:
        if not isinstance(device, str) or not device.strip():
            raise UsbDriveError("Specify a USB drive to eject.", "invalid")
        key = device.strip()
        async with self._lock:
            hit = None
            for dev, info in self._mounted.items():
                if key in (dev, info["name"], info["share_path"], Path(info["mountpoint"]).name):
                    hit = (dev, info)
                    break
            if hit is None:
                raise UsbDriveError("That USB drive is not connected.", "not_found")
            dev, info = hit
            ok = await self._do_umount(Path(info["mountpoint"]), info.get("_disk_dev"))
            if not ok:
                raise UsbDriveError(
                    f"{info['label']} is in use right now. Close open files first and try again.", "busy")
            self._mounted.pop(dev, None)
            self._ejected.add(dev)
            return {"ok": True, "ejected": dev, "label": info["label"]}
