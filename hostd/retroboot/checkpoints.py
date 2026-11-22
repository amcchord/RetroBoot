"""Clean-shutdown checkpoints: /var/lib/retroboot/checkpoints/<machine>/<ts>/.

A checkpoint captures the guest disk (``cp --reflink=always``, falling back to
a sparse copy), machine.json, the applied settings and the emulator package
hashes together, so a restore returns to the recorded combination.  Callers
must hold the ``guest`` job resource and have verified a clean shutdown.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
import ctypes
import errno
import hashlib
import tempfile
from pathlib import Path

from .machines import Machine
from .util import atomic_write_json, read_json, run as run_cmd, wall

TS_RE = re.compile(r"^\d{8}T\d{6}Z(-\d+)?$")


class CheckpointError(Exception):
    def __init__(self, message: str, code: str = "checkpoint_failed"):
        super().__init__(message)
        self.message = message
        self.code = code


def exchange_directories(first: Path, second: Path) -> None:
    """One Linux rename exchanges both complete directories, including disk + NVRAM.

    No two-rename fallback: a power loss between two renames would split the pair.
    Hosts/filesystems without exchange support fail before either directory changes.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renameat2
    except (OSError, AttributeError) as exc:
        raise OSError(errno.ENOTSUP, "atomic checkpoint restore is unavailable") from exc
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(first), -100, os.fsencode(second), 2) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


async def copy_disk(src: Path, dst: Path) -> str:
    """Reflink when the filesystem supports it, else a sparse copy.  Returns the method."""
    rc, _, _ = await run_cmd(["cp", "--reflink=always", str(src), str(dst)], timeout=3600)
    if rc == 0:
        return "reflink"
    try:
        dst.unlink()
    except FileNotFoundError:
        pass
    rc, _, err = await run_cmd(["cp", "--sparse=always", str(src), str(dst)], timeout=6 * 3600)
    if rc != 0:
        raise CheckpointError(f"Copying the disk failed: {err.strip()[:200]}")
    return "sparse"


class Checkpoints:
    def __init__(self, root: Path):
        self.root = Path(root)

    def dir_for(self, machine_id: str) -> Path:
        return self.root / machine_id

    def list(self, machine_id: str) -> list[dict]:
        d = self.dir_for(machine_id)
        out = []
        if not d.is_dir():
            return out
        for c in sorted(d.iterdir(), reverse=True):
            if not TS_RE.fullmatch(c.name):
                continue
            try:
                meta = read_json(c / "checkpoint.json", 1024 * 1024)
            except (OSError, ValueError):
                continue
            out.append({k: meta.get(k) for k in ("id", "label", "created", "machine", "disk_bytes",
                                                 "allocated_bytes", "method", "package", "settings_revision")})
        return out

    def get(self, machine_id: str, cp_id: str) -> Path | None:
        if not TS_RE.fullmatch(cp_id or ""):
            return None
        p = self.dir_for(machine_id) / cp_id
        return p if (p / "checkpoint.json").is_file() and (p / "disk.img").is_file() else None

    async def create(self, machine: Machine, label: str, settings: dict, settings_revision: int,
                     package: dict | None) -> dict:
        st = machine.state()
        if not st.get("clean", False):
            raise CheckpointError("Checkpoints need Mac OS to have shut down cleanly first.", "not_clean")
        base = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        d = self.dir_for(machine.id)
        d.mkdir(parents=True, exist_ok=True)
        cp_id, n = base, 1
        while (d / cp_id).exists():
            n += 1
            cp_id = f"{base}-{n}"
        staging = d / f".{cp_id}.tmp"
        staging.mkdir()
        try:
            nvram = None
            if machine.osx:
                from .osx import read_nvram
                try:
                    nvram = read_nvram(machine.nvram)
                except ValueError as exc:
                    raise CheckpointError(str(exc), "invalid_nvram") from exc
            method = await copy_disk(machine.disk, staging / "disk.img")
            shutil.copy2(machine.dir / "machine.json", staging / "machine.json")
            stat = (staging / "disk.img").stat()
            meta = {"schema": 1, "id": cp_id, "label": (label or "Checkpoint")[:80], "created": wall(),
                    "machine": machine.id, "disk_bytes": stat.st_size, "allocated_bytes": stat.st_blocks * 512,
                    "method": method, "settings": settings, "settings_revision": settings_revision,
                    "package": package, "last_exit": st.get("last_exit")}
            if nvram is not None:
                from .quadra import write_new, open_regular
                write_new(staging, staging / "nvram.img", nvram, 0o660)
                atomic_write_json(staging / "machine.json", machine.meta)
                fd = open_regular(staging / "disk.img")
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                meta.update(schema=2, guest_os=machine.guest_os, nvram_sha256=hashlib.sha256(nvram).hexdigest())
            atomic_write_json(staging / "checkpoint.json", meta)
            os.rename(staging, d / cp_id)
            if nvram is not None:
                fsync_directory(d)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return {k: meta[k] for k in ("id", "label", "created", "machine", "disk_bytes", "allocated_bytes", "method")}

    async def restore(self, machine: Machine, cp_id: str) -> dict:
        cp = self.get(machine.id, cp_id)
        if cp is None:
            raise CheckpointError("That checkpoint does not exist.", "not_found")
        if not machine.state().get("clean", False):
            # A dirty disk is about to be replaced; still require that no guest runs (caller holds the lock).
            pass
        if machine.osx:
            return await self._restore_osx(machine, cp, cp_id)
        tmp = machine.dir / ".disk.img.restore"
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        method = await copy_disk(cp / "disk.img", tmp)
        os.chmod(tmp, 0o660)
        os.replace(tmp, machine.disk)
        machine.set_state(clean=True, restored_from=cp_id, restored_at=wall())
        return {"checkpoint": cp_id, "method": method}

    async def _restore_osx(self, machine: Machine, cp: Path, cp_id: str) -> dict:
        """Stage a whole stopped Mac, then atomically exchange its directory.

        A crash before exchange leaves the original pair; a crash after it leaves
        the checkpoint pair. Dot staging directories never appear in the registry.
        Current machine identity/name/configuration are preserved, and the checkpoint
        must belong to this exact OS profile. Caller holds the guest lifecycle lock.
        """
        from .osx import read_nvram
        from .quadra import write_new
        try:
            meta = read_json(cp / "checkpoint.json", 1024 * 1024)
            if meta.get("schema") != 2 or meta.get("guest_os") != machine.guest_os or meta.get("machine") != machine.id:
                raise CheckpointError("This checkpoint does not contain a matching OS X disk and startup memory.",
                                      "incompatible_checkpoint")
            from .machines import validate_meta
            saved_machine = validate_meta(read_json(cp / "machine.json", 1024 * 1024), machine.id)
            if saved_machine.get("guest_os") != machine.guest_os:
                raise ValueError("The checkpoint's operating-system identity does not match this Mac.")
            restored_meta = {k: v for k, v in machine.meta.items() if k not in ("os_version", "os_build")}
            restored_meta.update({k: saved_machine[k] for k in ("os_version", "os_build") if k in saved_machine})
            nvram = read_nvram(cp / "nvram.img")
            if hashlib.sha256(nvram).hexdigest() != meta.get("nvram_sha256"):
                raise CheckpointError("The checkpoint's OS X startup memory does not match its record.", "invalid_nvram")
        except (OSError, ValueError) as exc:
            raise CheckpointError(str(exc), "invalid_checkpoint") from exc
        staging = Path(tempfile.mkdtemp(prefix=f".restore-{machine.id}-", dir=machine.dir.parent))
        exchanged = committed = False
        try:
            # Preserve the Mac's other files without following guest-owned links. Disk
            # and NVRAM are replaced explicitly from the checkpoint, never hard-linked.
            await asyncio.to_thread(shutil.copytree, machine.dir, staging, symlinks=True, dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns(machine.disk.name, "nvram.img"))
            method = await copy_disk(cp / "disk.img", staging / machine.disk.name)
            os.chmod(staging / machine.disk.name, 0o660)
            write_new(staging, staging / "nvram.img", nvram, 0o660)
            atomic_write_json(staging / "state.json", {**machine.state(), "clean": True,
                                                       "restored_from": cp_id, "restored_at": wall(),
                                                       "running_session": None, "dirty_since": None})
            atomic_write_json(staging / "machine.json", restored_meta)
            from .quadra import open_regular
            for directory, _dirs, files in os.walk(staging, followlinks=False):
                for filename in files:
                    path = Path(directory) / filename
                    if path.is_symlink():
                        continue
                    fd = open_regular(path)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                fsync_directory(Path(directory))
            fsync_directory(staging)
            try:
                exchange_directories(staging, machine.dir)
            except OSError as exc:
                raise CheckpointError("This disk cannot restore OS X disk and startup memory together. "
                                      "The current Mac is unchanged.", "atomic_restore_unavailable") from exc
            exchanged = True
            try:
                fsync_directory(machine.dir.parent)
            except OSError as exc:
                error = CheckpointError("The checkpoint is active, but its final save could not be confirmed. "
                                        "The previous Mac was kept for recovery.", "restore_not_synced")
                error.recovery_copy = str(staging)
                raise error from exc
            committed = True
            machine.meta = restored_meta
            return {"checkpoint": cp_id, "method": method, "atomic_pair": True}
        finally:
            # Before exchange this is the incomplete new copy; afterwards it is the
            # old stopped Mac. The registry always names one complete directory.
            if staging.exists() and (not exchanged or committed):
                await asyncio.to_thread(shutil.rmtree, staging)

    async def delete(self, machine_id: str, cp_id: str) -> bool:
        cp = self.get(machine_id, cp_id)
        if cp is None:
            return False
        await asyncio.to_thread(shutil.rmtree, cp)
        return True
