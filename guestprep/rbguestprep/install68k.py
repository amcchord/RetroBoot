"""Copy canonical Mac file trees into a stopped HFS Standard disk.

Bulk copies use pinned hfsutils with isolated state and its HFS overflow
allocator. A Linux hfs fallback creates records and moves temporary resource
allocations only when their three catalog extents cover the whole allocation.
Both forks, all 32 Finder-info bytes and dates are read back before success.

Use a disposable disk copy.  An interrupted/failed install is not rolled
back.  The caller must hold guest ownership for the entire operation; the
process check is an additional guard, not a substitute for that ownership.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tempfile
import uuid

from . import hfs, prep68k
from .install import InstallError, SPACE_MARGIN, TREE_FORMAT, TREE_VERSION

MAC_EPOCH = dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc)
QEMU_NAMES = ("qemu-system-m68k", "qemu-system-ppc")


def _sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _name(name: str) -> None:
    if not isinstance(name, str) or not name or name in (".", "..") or ":" in name or "\0" in name:
        raise InstallError("invalid-tree", f"Invalid Mac name {name!r}.")
    try:
        raw = name.encode("mac_roman")
    except UnicodeEncodeError:
        raw = b""
    if not 0 < len(raw) <= 31:
        raise InstallError("invalid-tree", f"Name {name!r} does not fit HFS Standard.")


def _path(value) -> list[str]:
    parts = value.split(":") if isinstance(value, str) and value else list(value or [])
    for part in parts:
        _name(part)
    return parts


def _key(parts) -> tuple[bytes, ...]:
    return tuple(hfs.fold(p) for p in parts)


def _date(value) -> int | None:
    if value is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        seconds = int((parsed - MAC_EPOCH).total_seconds())
        if 0 <= seconds <= 0xffffffff:
            return seconds
    except (AttributeError, TypeError, ValueError):
        pass
    raise InstallError("invalid-tree", f"Invalid HFS date {value!r}.")


def _date_text(value: int) -> str:
    return (MAC_EPOCH + dt.timedelta(seconds=value)).replace(tzinfo=None).isoformat()


def finder_info(rec: hfs.CatalogRecord) -> bytes:
    if rec.is_file:
        return rec.data[hfs.F_FINFO:hfs.F_FINFO + 16] + rec.data[hfs.F_FXINFO:hfs.F_FXINFO + 16]
    return rec.data[22:54]


class Tree:
    """Fully validate paths and payloads before the first disk mutation."""

    def __init__(self, root):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise InstallError("invalid-tree", "The input tree must not be a symbolic link.")
        try:
            manifest = self._file("manifest.json").read_text(encoding="utf-8")
            self.manifest = json.loads(manifest)
            self.entries = self.manifest["entries"]
        except (OSError, KeyError, ValueError, TypeError) as exc:
            raise InstallError("invalid-tree", f"No valid tree manifest: {exc}") from exc
        if (self.manifest.get("format") != TREE_FORMAT or self.manifest.get("version") != TREE_VERSION
                or not isinstance(self.entries, list) or not self.entries):
            raise InstallError("invalid-tree", "Not a nonempty RetroBoot Mac tree.")
        seen = {}
        for e in self.entries:
            if not isinstance(e, dict) or not isinstance(e.get("path"), list) or not e["path"]:
                raise InstallError("invalid-tree", "Malformed tree entry.")
            _path(e["path"])
            k = _key(e["path"])
            if k in seen or e.get("kind") not in ("file", "folder"):
                raise InstallError("invalid-tree", f"Duplicate or invalid entry {':'.join(e['path'])}.")
            seen[k] = e
            try:
                if len(bytes.fromhex(e["finder_info"])) != 32:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise InstallError("invalid-tree", "Malformed Finder information.") from None
            for field in ("created", "modified"):
                _date(e.get(field))
            if e["kind"] == "file":
                for fork in ("data", "rsrc"):
                    try:
                        facts = e[fork]
                        if type(facts["size"]) is not int or facts["size"] < 0:
                            raise ValueError
                        digest = bytes.fromhex(facts["sha256"])
                        if len(digest) != 32:
                            raise ValueError
                    except (KeyError, TypeError, ValueError):
                        raise InstallError("invalid-tree", "Malformed fork information.") from None
                    blob = self.read(e, fork)
                    if len(blob) != facts["size"] or _sha(blob) != facts["sha256"]:
                        raise InstallError("invalid-tree", f"{':'.join(e['path'])}: {fork} fork differs.")
        for e in self.entries:
            for i in range(1, len(e["path"])):
                parent = seen.get(_key(e["path"][:i]))
                if parent is None or parent["kind"] != "folder":
                    raise InstallError("invalid-tree", "Every parent must be a folder in the tree.")
        self.entries.sort(key=lambda e: (len(e["path"]), e["path"]))
        self.top = sorted({e["path"][0] for e in self.entries})

    def _file(self, *parts) -> Path:
        path = self.root
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise InstallError("invalid-tree", "Tree paths must not contain symbolic links.")
        if not path.is_file():
            raise InstallError("invalid-tree", f"Missing tree file {path}.")
        return path

    def read(self, entry, fork: str) -> bytes:
        if fork == "rsrc" and entry[fork]["size"] == 0:
            return b""
        return self._file(fork, *[p.replace("/", ":") for p in entry["path"]]).read_bytes()

    def needed(self, block: int) -> int:
        return sum(-(-e[f]["size"] // block) * block for e in self.entries if e["kind"] == "file"
                   for f in ("data", "rsrc"))


def assert_offline(image, *, proc_root="/proc") -> None:
    """Refuse an image open in either supported emulator, including hardlinks.

    On Linux compare open descriptors by device/inode, not command-line text.
    A non-Linux read-only audit uses lsof.  Mutations still require Linux hfs.
    """
    image = Path(image)
    if image.is_symlink() or not stat.S_ISREG(image.stat().st_mode):
        raise InstallError("invalid-image", "The disk must be a regular file, not a symbolic link.")
    target = image.stat()
    proc = Path(proc_root)
    if proc.is_dir():
        for process in proc.iterdir():
            if not process.name.isdigit():
                continue
            try:
                argv = (process / "cmdline").read_bytes().split(b"\0")
                exe = os.path.basename(os.fsdecode(argv[0]))
                if not exe.startswith(QEMU_NAMES):
                    continue
                for fd in (process / "fd").iterdir():
                    try:
                        opened = fd.stat()
                    except FileNotFoundError:
                        continue
                    if (opened.st_dev, opened.st_ino) == (target.st_dev, target.st_ino):
                        raise InstallError("disk-in-use", f"The Mac disk is open in {exe} (process {process.name}).")
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError as exc:
                raise InstallError("identity-unverified", "Cannot inspect emulator disk ownership; run as root.") from exc
        return
    try:
        result = subprocess.run(["lsof", "+c", "0", "-w", "-nP", "-Fpc", "--", str(image)],
                                capture_output=True, text=True)
    except OSError as exc:
        raise InstallError("identity-unverified", "Cannot verify that the Mac disk is stopped.") from exc
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise InstallError("identity-unverified", "Cannot verify that the Mac disk is stopped.")
    if any(line[1:].startswith(QEMU_NAMES) for line in result.stdout.splitlines() if line.startswith("c")):
        raise InstallError("disk-in-use", "The Mac disk is open in an emulator.")


# Name used by image composers; retain the descriptive name for audit callers.
assert_stopped = assert_offline


@contextlib.contextmanager
def _exclusive(image):
    # One local installer at a time. Guest ownership must also remain exclusive.
    # Lock the inode itself so two hardlink spellings cannot bypass the lock.
    assert_offline(image)
    with open(image, "rb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallError("disk-in-use", "Another offline install owns this disk.") from exc
        assert_offline(image)
        yield


def _lookup(vol, index, path):
    try:
        return vol.lookup(path, index) if path else None
    except FileNotFoundError:
        return None


def _metadata(vol, rec, entry):
    info = bytes.fromhex(entry["finder_info"])
    if rec.is_file:
        vol.set_finder_info(rec, info[:16])
        vol._write_record_field(rec, hfs.F_FXINFO, info[16:])
    else:
        vol._write_record_field(rec, 22, info)
    offsets = (hfs.F_CRDAT, hfs.F_MDDAT) if rec.is_file else (10, 14)
    for field, offset in zip(("created", "modified"), offsets):
        value = _date(entry.get(field))
        if value is not None:
            vol._write_record_field(rec, offset, struct.pack(">I", value))


def _move_resources(image, dest, temporary, pending):
    """Preflight every allocation before moving the first fork descriptor."""
    with hfs.Volume(str(image), writable=True) as vol:
        index = vol.index()
        moves = []
        for entry, name in pending:
            target = vol.lookup(dest + entry["path"], index)
            temp = vol.lookup([temporary, name], index)
            allocation = temp.fork("data")
            if (allocation.logical_size != entry["rsrc"]["size"]
                    or allocation.covered_blocks() * vol.block_size < allocation.physical_size):
                raise InstallError("fragmented", f"Resource fork needs overflow extents: {':'.join(entry['path'])}.")
            old = target.fork("rsrc")
            if old.physical_size or old.logical_size:
                raise InstallError("internal", "New target unexpectedly has a resource fork.")
            moves.append((target, temp, allocation))
        for target, temp, allocation in moves:
            vol.set_fork_record(target, "rsrc", allocation)
            vol.set_fork_record(temp, "data", hfs.Fork.empty())
        vol.flush()


def verify_tree(image, dest_folder, tree_root) -> dict:
    tree = tree_root if isinstance(tree_root, Tree) else Tree(tree_root)
    dest = _path(dest_folder)
    assert_offline(image)
    files = 0
    with hfs.Volume(str(image)) as vol:
        index = vol.index()
        for entry in tree.entries:
            rec = _lookup(vol, index, dest + entry["path"])
            name = ":".join(dest + entry["path"])
            if rec is None or rec.is_file != (entry["kind"] == "file"):
                raise InstallError("verify-failed", f"Missing item or wrong kind: {name}.")
            if finder_info(rec) != bytes.fromhex(entry["finder_info"]):
                raise InstallError("verify-failed", f"Finder information differs: {name}.")
            offsets = (hfs.F_CRDAT, hfs.F_MDDAT) if rec.is_file else (10, 14)
            for field, offset in zip(("created", "modified"), offsets):
                value = _date(entry.get(field))
                if value is not None and struct.unpack(">I", rec.data[offset:offset + 4])[0] != value:
                    raise InstallError("verify-failed", f"{field} date differs: {name}.")
            if rec.is_file:
                for fork in ("data", "rsrc"):
                    blob = vol.read_fork(rec, fork)
                    facts = entry[fork]
                    if len(blob) != facts["size"] or _sha(blob) != facts["sha256"]:
                        raise InstallError("verify-failed", f"{fork} fork differs: {name}.")
                files += 1
    return {"verified_items": len(tree.entries), "files": files,
            "folders": len(tree.entries) - files, "verified_forks": files * 2}


def _install_linux_tree(image, dest_folder, tree_root, *, replace=False) -> dict:
    """Merge folders; existing files need replace=True. Never replace a folder.

    The HFS Standard startup volume must be stopped and a disposable copy.
    A failed install leaves that copy for diagnosis. Originals stay untouched.
    """
    image = str(Path(image).absolute())
    tree = Tree(tree_root)
    dest = _path(dest_folder)
    with _exclusive(image):
        ok, output = prep68k.fsck(image, repair=True)
        if not ok:
            raise InstallError("disk-damaged", "HFS check failed before installation: " + output[-400:])
        replaced = []
        with hfs.Volume(image) as vol:
            index = vol.index()
            for i in range(1, len(dest) + 1):
                node = _lookup(vol, index, dest[:i])
                if node is not None and not node.is_folder:
                    raise InstallError("invalid-destination", "The destination contains a file.")
            for entry in tree.entries:
                node = _lookup(vol, index, dest + entry["path"])
                if node is None:
                    continue
                if node.is_file != (entry["kind"] == "file"):
                    raise InstallError("already-installed", "An existing item has a different kind.")
                if node.is_file:
                    if not replace:
                        raise InstallError("already-installed", f"File already exists: {':'.join(entry['path'])}.")
                    replaced.append(entry["path"])
            needed = tree.needed(vol.block_size) + SPACE_MARGIN
            if needed > vol.free_blocks * vol.block_size:
                raise InstallError("disk-full", f"The HFS volume needs {needed // 1048576} MB free.")
        temporary = f"rbinstall-{uuid.uuid4().hex[:8]}"
        pending = []
        assert_offline(image)
        with prep68k.mounted(image) as mount:
            base = Path(mount).joinpath(*[p.replace("/", ":") for p in dest])
            base.mkdir(parents=True, exist_ok=True)
            tmp = Path(mount) / temporary
            tmp.mkdir()
            for i, entry in enumerate(tree.entries):
                target = base.joinpath(*[p.replace("/", ":") for p in entry["path"]])
                if entry["kind"] == "folder":
                    target.mkdir(exist_ok=True)
                    continue
                if entry["path"] in replaced:
                    target.unlink()     # driver frees both old forks
                # Recheck the input in case a caller changed it after preflight.
                data, resource = tree.read(entry, "data"), tree.read(entry, "rsrc")
                if _sha(data) != entry["data"]["sha256"] or _sha(resource) != entry["rsrc"]["sha256"]:
                    raise InstallError("invalid-tree", "An input fork changed during installation.")
                with target.open("xb") as file:
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                if resource:
                    name = f"{i:06d}"
                    with (tmp / name).open("xb") as file:
                        file.write(resource)
                        file.flush()
                        os.fsync(file.fileno())
                    pending.append((entry, name))
        assert_offline(image)
        _move_resources(image, dest, temporary, pending)
        with prep68k.mounted(image) as mount:
            shutil.rmtree(Path(mount) / temporary)
        # All driver mounts are finished: directory dates and Finder info now
        # survive unchanged, including FXInfo (aliases, custom icons and labels).
        with hfs.Volume(image, writable=True) as vol:
            index = vol.index()
            for entry in tree.entries:
                _metadata(vol, vol.lookup(dest + entry["path"], index), entry)
            vol.flush()
        facts = verify_tree(image, dest, tree)
        ok, output = prep68k.fsck(image, repair=False)
        if not ok:
            raise InstallError("verify-failed", "HFS check failed after installation: " + output[-400:])
    return {"image": image, "dest": dest, "top": tree.top, "replaced": replaced,
            "resource_forks_moved": len(pending), "bytes": sum(e[f]["size"] for e in tree.entries
            if e["kind"] == "file" for f in ("data", "rsrc")), "fsck_before": "ok", "fsck_after": "ok", **facts}


def _hfsutils_dir() -> Path | None:
    explicit = os.environ.get("RB_HFSUTILS_DIR")
    found = shutil.which("hmount")
    candidates = [Path(explicit)] if explicit else ([Path(found).parent] if found else [])
    for directory in candidates:
        try:
            marker = json.loads((directory / "retroboot-hfsutils.json").read_text())
            if marker.get("state_environment") != "RB_HFS_STATE":
                continue
            if all(os.access(directory / n, os.X_OK) for n in ("hmount", "humount", "hmkdir", "hcopy", "hdel")):
                return directory
        except (OSError, ValueError):
            continue
    if explicit:
        raise InstallError("missing-tools", "The selected hfsutils tools do not have isolated state support.")
    return None


def _macbinary(entry, data: bytes, resource: bytes) -> bytes:
    header = bytearray(128)
    name = entry["path"][-1].encode("mac_roman")
    header[1] = len(name)
    header[2:2 + len(name)] = name
    info = bytes.fromhex(entry["finder_info"])
    header[65:73] = info[:8]
    header[73], header[101] = info[8], info[9]
    header[75:81] = info[10:16]
    struct.pack_into(">II", header, 83, len(data), len(resource))
    for field, offset in (("created", 91), ("modified", 95)):
        struct.pack_into(">I", header, offset, _date(entry.get(field)) or 0)
    header[122] = header[123] = 129
    crc = 0
    for byte in header[:124]:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xffff
    struct.pack_into(">H", header, 124, crc)
    return bytes(header) + data + bytes(-len(data) % 128) + resource + bytes(-len(resource) % 128)


def _install_hfsutils(image, dest, tree: Tree, tools, replace) -> dict:
    image = str(Path(image).absolute())
    with _exclusive(image):
        ok, output = prep68k.fsck(image, repair=True)
        if not ok:
            raise InstallError("disk-damaged", "HFS check failed before installation: " + output[-400:])
        folders, replaced = [], []
        with hfs.Volume(image) as vol:
            index = vol.index()
            for path in [dest[:i] for i in range(1, len(dest) + 1)]:
                node = _lookup(vol, index, path)
                if node is not None and not node.is_folder:
                    raise InstallError("invalid-destination", "The destination contains a file.")
                if node is None:
                    folders.append(path)
            for entry in tree.entries:
                path = dest + entry["path"]
                node = _lookup(vol, index, path)
                if node is not None and node.is_file != (entry["kind"] == "file"):
                    raise InstallError("already-installed", "An existing item has a different kind.")
                if entry["kind"] == "folder":
                    if node is None:
                        folders.append(path)
                elif node is not None:
                    if not replace:
                        raise InstallError("already-installed", f"File already exists: {':'.join(path)}.")
                    replaced.append(entry["path"])
            needed = tree.needed(vol.block_size) + SPACE_MARGIN
            if needed > vol.free_blocks * vol.block_size:
                raise InstallError("disk-full", f"The HFS volume needs {needed // 1048576} MB free.")
        with tempfile.TemporaryDirectory(prefix="rbhfs-") as temporary:
            environment = {**os.environ, "RB_HFS_STATE": temporary}

            def run(name, *args):
                result = subprocess.run([str(tools / name), *args], capture_output=True, text=True,
                                        errors="replace", env=environment)
                if result.returncode:
                    raise InstallError("hfsutils-failed", f"{name}: {(result.stderr or result.stdout)[-500:]}")

            run("hmount", image)
            try:
                for path in folders:
                    run("hmkdir", (":" + ":".join(path)).encode("mac_roman"))
                for entry in tree.entries:
                    if entry["kind"] != "file":
                        continue
                    data, resource = tree.read(entry, "data"), tree.read(entry, "rsrc")
                    if _sha(data) != entry["data"]["sha256"] or _sha(resource) != entry["rsrc"]["sha256"]:
                        raise InstallError("invalid-tree", "An input fork changed during installation.")
                    path = (":" + ":".join(dest + entry["path"])).encode("mac_roman")
                    if entry["path"] in replaced:
                        run("hdel", path)
                    encoded = Path(temporary) / "payload.bin"
                    encoded.write_bytes(_macbinary(entry, data, resource))
                    run("hcopy", "-m", str(encoded), path)
            except BaseException as error:
                try:
                    run("humount")
                except InstallError as cleanup:
                    if hasattr(error, "add_note"):
                        error.add_note(f"hfsutils state cleanup also failed: {cleanup}")
                raise
            else:
                run("humount")
        # hfsutils 3.2.6 leaves reserved catalog fields that hfsprogs repairs.
        # Repair before restoring exact metadata; forks are checked afterwards.
        ok, output = prep68k.fsck(image, repair=True)
        if not ok:
            raise InstallError("verify-failed", "HFS repair failed after copying: " + output[-400:])
        assert_offline(image)
        with hfs.Volume(image, writable=True) as vol:
            index = vol.index()
            for entry in tree.entries:
                _metadata(vol, vol.lookup(dest + entry["path"], index), entry)
            vol.flush()
        facts = verify_tree(image, dest, tree)
        ok, output = prep68k.fsck(image, repair=False)
        if not ok:
            raise InstallError("verify-failed", "Final HFS check failed: " + output[-400:])
    return {"image": image, "dest": dest, "top": tree.top, "replaced": replaced, "backend": "hfsutils",
            "resource_forks_written": sum(bool(e["rsrc"]["size"]) for e in tree.entries if e["kind"] == "file"),
            "bytes": sum(e[f]["size"] for e in tree.entries if e["kind"] == "file" for f in ("data", "rsrc")),
            "fsck_before": "ok", "fsck_after": "ok", **facts}


def install_tree(image, dest_folder, tree_root, *, replace=False, backend="auto") -> dict:
    """Install both forks and metadata, using hfsutils for arbitrary fragmentation.

    Build isolated-state tools with guestprep/tools/build_hfsutils.py and put
    them on PATH or set RB_HFSUTILS_DIR. The Linux fallback refuses resource
    allocations using overflow extents before it moves any fork descriptors.
    Existing folders merge; existing files require explicit replacement.
    """
    tree = Tree(tree_root)
    dest = _path(dest_folder)
    if backend not in ("auto", "hfsutils", "linux"):
        raise InstallError("invalid-backend", "Unknown HFS installer backend.")
    tools = _hfsutils_dir() if backend != "linux" else None
    if backend == "hfsutils" and tools is None:
        raise InstallError("missing-tools", "Isolated-state hfsutils tools are required.")
    assert_offline(image)
    if tools:
        return _install_hfsutils(image, dest, tree, tools, replace)
    report = _install_linux_tree(image, dest, tree_root, replace=replace)
    report["backend"] = "linux"
    return report


def export_tree(image, source_folder, tree_root) -> dict:
    """Export a stopped volume folder's contents as a canonical tree for audits."""
    source = _path(source_folder)
    target = Path(tree_root)
    if target.exists():
        raise InstallError("already-exists", "The export destination already exists.")
    assert_offline(image)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".hfs-export-", dir=target.parent))
    try:
        entries = []
        with hfs.Volume(str(image)) as vol:
            index = vol.index()
            parent = vol.lookup(source, index) if source else None
            if parent is not None and not parent.is_folder:
                raise InstallError("invalid-destination", "The export source is not a folder.")
            root_id = parent.cnid if parent else hfs.ROOT_FOLDER_ID
            children = {}
            for rec in index.values():
                children.setdefault(rec.parent_id, []).append(rec)
            visited = set()

            def walk(cnid, path):
                if cnid in visited:
                    raise InstallError("disk-damaged", "The HFS folder hierarchy contains a cycle.")
                visited.add(cnid)
                for rec in sorted(children.get(cnid, []), key=lambda r: r.name):
                    parts = path + [rec.name]
                    item = {"path": parts, "kind": "file" if rec.is_file else "folder",
                            "finder_info": finder_info(rec).hex(), "flags": rec.finder_flags}
                    offsets = (hfs.F_CRDAT, hfs.F_MDDAT) if rec.is_file else (10, 14)
                    for field, offset in zip(("created", "modified"), offsets):
                        item[field] = _date_text(struct.unpack(">I", rec.data[offset:offset + 4])[0])
                    disk_parts = [p.replace("/", ":") for p in parts]
                    if rec.is_folder:
                        temporary.joinpath("data", *disk_parts).mkdir(parents=True, exist_ok=True)
                    else:
                        item["type"], item["creator"] = (v.decode("mac_roman") for v in rec.type_creator)
                        for fork in ("data", "rsrc"):
                            blob = vol.read_fork(rec, fork)
                            item[fork] = {"size": len(blob), "sha256": _sha(blob)}
                            if blob or fork == "data":
                                output = temporary.joinpath(fork, *disk_parts)
                                output.parent.mkdir(parents=True, exist_ok=True)
                                output.write_bytes(blob)
                    entries.append(item)
                    if rec.is_folder:
                        walk(rec.cnid, parts)
            walk(root_id, [])
        manifest = {"format": TREE_FORMAT, "version": TREE_VERSION,
                    "source": {"kind": "hfs-standard", "folder": source}, "entries": entries,
                    "top": sorted({e["path"][0] for e in entries})}
        (temporary / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")
        # Validate what was written, including all fork hashes and HFS names.
        Tree(temporary)
        os.rename(temporary, target)
        return {"tree": str(target), "items": len(entries)}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("install", "verify", "export"))
    parser.add_argument("image")
    parser.add_argument("tree")
    parser.add_argument("--dest", default="", help="Mac folder path, separated with colons; default volume root")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--backend", choices=("auto", "hfsutils", "linux"), default="auto")
    args = parser.parse_args(argv)
    if args.operation == "install":
        report = install_tree(args.image, args.dest, args.tree, replace=args.replace, backend=args.backend)
    elif args.operation == "verify":
        report = verify_tree(args.image, args.dest, args.tree)
    else:
        report = export_tree(args.image, args.dest, args.tree)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
