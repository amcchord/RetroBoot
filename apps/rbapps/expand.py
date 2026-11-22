"""Expand classic Mac software into a canonical tree (``tree.py``).

Archives (StuffIt 1.5-5, StuffIt X, Compact Pro, BinHex, MacBinary, zip...)
go through The Unarchiver's ``lsar``/``unar`` (Debian package ``unar``).  On
Linux ``unar`` writes each resource fork and the Finder info as an
AppleDouble sidecar ``<name>.rsrc``; this module joins data forks, sidecars
and the ``lsar -j`` listing, and verifies every fork size and every type,
creator and Finder flag against that listing.

Disk images found inside an archive (Disk Copy 4.2, NDIF, raw HFS) and disk
images given directly are opened with ``volume.py`` and their files become
part of the tree.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import struct
import subprocess
import tempfile
from pathlib import Path

from . import diskimage, resfork, tree, volume

# Files that carry no meaning on Mac OS 9 (Mac OS X Finder metadata, volume
# bookkeeping) and are left out of a tree; recorded in manifest "skipped".
SKIP_NAMES = {".DS_Store": "mac-os-x-metadata", "__MACOSX": "mac-os-x-metadata",
              ".fseventsd": "mac-os-x-metadata", ".Spotlight-V100": "mac-os-x-metadata",
              ".Trashes": "mac-os-x-metadata", ".TemporaryItems": "mac-os-x-metadata"}
VOLUME_ROOT_SKIP = {"Desktop DB", "Desktop DF", "Desktop", "Trash", "TheVolumeSettingsFolder",
                    "TheFindByContentFolder", "Temporary Items", "Network Trash Folder",
                    "Cleanup At Startup", "Shutdown Check", "VM Storage", "AppleShare PDS",
                    ".HFS+ Private Directory Data\r", "\x00\x00\x00\x00HFS+ Private Data",
                    ".metadata_never_index", ".journal", ".journal_info_block", "DesktopPrinters DB"}


class ExpandError(Exception):
    pass


def _run(cmd: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ExpandError(f"{cmd[0]} is not installed (Debian package 'unar')") from exc
    except subprocess.TimeoutExpired as exc:
        raise ExpandError(f"{cmd[0]} timed out") from exc


def _skip_reason(name: str) -> str | None:
    if name in SKIP_NAMES:
        return SKIP_NAMES[name]
    if name.startswith("._"):
        return "appledouble-sidecar"
    return None


def _code(v) -> bytes:
    return struct.pack(">I", int(v or 0) & 0xFFFFFFFF)


def list_archive(path: Path) -> dict:
    res = _run(["lsar", "-j", "-jss", str(path)], timeout=600)
    try:
        listing = json.loads(res.stdout.decode("utf-8"))
    except ValueError:
        listing = None
    if res.returncode != 0 or not listing or "lsarContents" not in listing:
        msg = (res.stderr or res.stdout).decode("utf-8", "replace").strip()[-300:]
        raise ExpandError(f"not a supported archive: {msg or 'lsar failed'}")
    if listing.get("lsarError"):
        raise ExpandError(f"archive is damaged: {listing['lsarError']}")
    return listing


def _safe_tree(root: Path) -> None:
    """unar output must be plain files and directories under root."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for n in dirnames + filenames:
            p = Path(dirpath, n)
            st = p.lstat()
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                raise ExpandError(f"archive produced a non-regular file: {n!r}")


def _parse_lsar(listing: dict) -> dict[tuple[str, ...], dict]:
    items: dict[tuple[str, ...], dict] = {}
    for e in listing["lsarContents"]:
        if e.get("XADIsEncrypted"):
            raise ExpandError("archive is password protected")
        if e.get("XADIsLink") or e.get("XADIsHardLink"):
            raise ExpandError("archive contains links")
        name = e["XADFileName"]
        # XADMaster shows a Mac '/' inside a name as ':' (Mac names cannot contain ':').
        parts = tuple(p.replace(":", "/") for p in name.split("/") if p not in ("", "."))
        if any(p == ".." for p in parts) or not parts:
            raise ExpandError(f"unsafe path in archive: {name!r}")
        it = items.setdefault(parts, {"dir": False, "data": 0, "rsrc": 0, "type": None, "creator": None,
                                      "flags": None, "created": None, "modified": None})
        if e.get("XADIsDirectory"):
            it["dir"] = True
        elif e.get("XADIsResourceFork"):
            it["rsrc"] = int(e.get("XADFileSize", 0))
        else:
            it["data"] = int(e.get("XADFileSize", 0))
        if "XADFileType" in e:
            it["type"] = _code(e["XADFileType"])
        if "XADFileCreator" in e:
            it["creator"] = _code(e["XADFileCreator"])
        if "XADFinderFlags" in e:
            it["flags"] = int(e["XADFinderFlags"]) & 0xFFFF
        it["created"] = e.get("XADCreationDate") or it["created"]
        it["modified"] = e.get("XADLastModificationDate") or it["modified"]
        # implied parent folders
        for i in range(1, len(parts)):
            items.setdefault(parts[:i], {"dir": True, "data": 0, "rsrc": 0, "type": None, "creator": None,
                                         "flags": None, "created": None, "modified": None})["dir"] = True
    return items


def _installer_aliases(listing: dict) -> tuple[dict, list[dict]]:
    """Separate StuffIt Installer placement directives from actual files.

    STal/STin zero-length records tell the original installer where to put
    another item. They can have the same name as a real folder or control
    panel; extracting them would overwrite that item or change its metadata.
    They are never guest files. Keep every other archive record unchanged.
    """
    aliases = []
    contents = []
    for e in listing["lsarContents"]:
        if (e.get("XADFileType") == int.from_bytes(b"STal", "big")
                and e.get("XADFileCreator") == int.from_bytes(b"STin", "big")
                and e.get("XADFileSize") == 0
                and not e.get("XADIsDirectory") and not e.get("XADIsResourceFork")):
            aliases.append(e)
        else:
            contents.append(e)
    return {**listing, "lsarContents": contents}, aliases


def _prepare_finder_info(fi: bytes, kind: str) -> bytes:
    """Make an item new to the guest Finder: clear kHasBeenInited so it is
    placed in its window and its bundle (icons, document types) registered."""
    fi = bytearray(fi.ljust(32, b"\0")[:32])
    flags = struct.unpack(">H", fi[8:10])[0] & ~tree.FLAG_HAS_BEEN_INITED
    fi[8:10] = struct.pack(">H", flags)
    return bytes(fi)


def add_volume(writer: tree.TreeWriter, vol: volume._BTreeVolume, prefix: tuple[str, ...],
               wrap: str = "auto") -> dict:
    """Copy every visible file of a volume into the tree below ``prefix``."""
    entries = vol.entries()          # parents before children
    kept, skipped = [], []
    for e in entries:
        if any(e.path[:len(sp)] == sp for sp in skipped):
            continue
        if len(e.path) == 1 and e.path[0] in VOLUME_ROOT_SKIP:
            reason = "volume-bookkeeping"
        else:
            reason = _skip_reason(e.path[-1])
        if reason:
            writer.skip(prefix + e.path, reason)
            skipped.append(e.path)
            continue
        kept.append(e)
    roots = [e for e in kept if len(e.path) == 1]
    if wrap == "auto":
        wrap = "none" if len(roots) == 1 and roots[0].kind == "folder" else "volume"
    base = prefix + ((vol.name or "Untitled"),) if wrap == "volume" else prefix
    if wrap == "volume":
        writer.folder(base)
    for e in kept:
        path = base + e.path
        created, modified = volume.mac_date(e.created), volume.mac_date(e.modified)
        if e.kind == "folder":
            writer.folder(path, _prepare_finder_info(e.finder_info, "folder"), created, modified)
        else:
            writer.file(path, vol.read_fork(e, "data"), vol.read_fork(e, "rsrc"),
                        _prepare_finder_info(e.finder_info, "file"), created, modified)
    return {"volume_name": vol.name, "volume_format": vol.kind, "wrapped": wrap == "volume",
            "files": sum(1 for e in kept if e.kind == "file")}


def _open_image_bytes(raw: bytes):
    import io
    f = io.BytesIO(raw)
    return volume.open_volume(f, len(raw))


def expand(source: Path, out: Path, images: str = "extract", work_dir: Path | None = None) -> dict:
    """Expand an archive or disk image into a canonical tree at ``out``."""
    source = Path(source)
    if not source.is_file():
        raise ExpandError(f"{source} is not a file")
    src_info = {"name": source.name, "bytes": source.stat().st_size, "sha256": tree.sha256_file(source)}
    with open(source, "rb") as f:
        head = f.read(4096)
    # A disk image given directly (data fork only)
    size = src_info["bytes"]
    try:
        if len(head) >= 84 and head[82:84] == b"\x01\x00" and 0 < head[0] <= 63 and \
                84 + sum(struct.unpack(">II", head[64:72])) == size:
            raw, info = diskimage.unwrap_dc42(source.read_bytes())
            writer = tree.TreeWriter(out, {"kind": "disk-image", **src_info, **info})
            writer.source.update(add_volume(writer, _open_image_bytes(raw), ()))
            return writer.finish()
        if len(head) >= 1026 and (head[1024:1026] in (b"BD", b"H+", b"HX") or head[:2] == b"ER"):
            with open(source, "rb") as f:
                writer = tree.TreeWriter(out, {"kind": "disk-image", "container": "raw", **src_info})
                writer.source.update(add_volume(writer, volume.open_volume(f, size), ()))
            return writer.finish()
    except (diskimage.ImageError, volume.VolumeError) as exc:
        raise ExpandError(f"disk image could not be read: {exc}") from exc
    listing, installer_aliases = _installer_aliases(list_archive(source))
    items = _parse_lsar(listing)
    tmp_parent = work_dir or out.parent
    with tempfile.TemporaryDirectory(prefix=".rbexpand-", dir=tmp_parent) as tmp:
        work = Path(tmp) / "x"
        command = ["unar", "-q", "-nr", "-D", "-f", "-forks", "visible", "-o", str(work)]
        if installer_aliases:
            indexes = [e.get("XADIndex") for e in listing["lsarContents"]]
            if not indexes or any(type(i) is not int or i < 0 for i in indexes):
                raise ExpandError("installer archive lacks usable entry indexes")
            command += ["-i", str(source), *map(str, indexes)]
        else:
            command.append(str(source))
        res = _run(command)
        if res.returncode != 0:
            raise ExpandError("unar failed: " + (res.stderr or res.stdout).decode("utf-8", "replace").strip()[-300:])
        _safe_tree(work)
        writer = tree.TreeWriter(out, {"kind": "archive", "format": listing.get("lsarFormatName"), **src_info})
        for e in installer_aliases:
            writer.skip(tuple(e["XADFileName"].split("/")), "stuffit-installer-placement-directive")
        images_opened = []
        problems = []
        skipped_prefixes = []
        for parts in sorted(items, key=lambda p: (len(p), p)):
            it = items[parts]
            reason = next((_skip_reason(p) for p in parts if _skip_reason(p)), None)
            if reason or any(parts[:len(s)] == s for s in skipped_prefixes):
                if reason and not any(parts[:len(s)] == s for s in skipped_prefixes):
                    writer.skip(parts, reason)
                    skipped_prefixes.append(parts)
                continue
            # lsar shows a Mac '/' as ':' and unar writes it as '_'
            fs = work.joinpath(*(p.replace("/", "_") for p in parts))
            side = fs.with_name(fs.name + ".rsrc")
            if parts[:-1] + (parts[-1] + ".rsrc",) in items and (it["rsrc"] or it["type"]):
                raise ExpandError(f"ambiguous names: the archive has both {parts[-1]!r} and {parts[-1] + '.rsrc'!r}")
            ad = {}
            if not it["dir"] and side.is_file():
                try:
                    ad = resfork.parse_appledouble(side.read_bytes())
                except resfork.ForkError as exc:
                    problems.append(f"{'/'.join(parts)}: {exc}")
            fi = ad.get(9, b"")
            if len(fi) < 32:
                fi = tree.file_finder_info(it["type"] or bytes(4), it["creator"] or bytes(4), it["flags"] or 0) \
                    if not it["dir"] else struct.pack(">8xH", it["flags"] or 0).ljust(32, b"\0")
            fi = fi[:32]
            if it["dir"]:
                if not fs.is_dir():
                    problems.append(f"{'/'.join(parts)}: folder missing from unar output")
                    continue
                writer.folder(parts, _prepare_finder_info(fi, "folder"), it["created"], it["modified"])
                continue
            data = fs.read_bytes() if fs.is_file() else b""
            rsrc = ad.get(2, b"")
            if len(data) != it["data"]:
                problems.append(f"{'/'.join(parts)}: data fork {len(data)} bytes, archive lists {it['data']}")
            if len(rsrc) != it["rsrc"]:
                problems.append(f"{'/'.join(parts)}: resource fork {len(rsrc)} bytes, archive lists {it['rsrc']}")
            if it["type"] is not None and fi[0:4] != it["type"]:
                problems.append(f"{'/'.join(parts)}: type {fi[0:4]!r}, archive lists {it['type']!r}")
            if it["creator"] is not None and fi[4:8] != it["creator"]:
                problems.append(f"{'/'.join(parts)}: creator {fi[4:8]!r}, archive lists {it['creator']!r}")
            if it["flags"] is not None and struct.unpack(">H", fi[8:10])[0] != it["flags"]:
                problems.append(f"{'/'.join(parts)}: Finder flags differ from the archive listing")
            if images == "extract" and diskimage.looks_like_image((fi[0:4], fi[4:8]), parts[-1], data[:4096]):
                try:
                    raw, info = diskimage.unwrap(data, rsrc, (fi[0:4], fi[4:8]))
                    vinfo = add_volume(writer, _open_image_bytes(raw), parts[:-1], wrap="volume")
                    images_opened.append({"path": list(parts), **info, **vinfo})
                    continue
                except (diskimage.ImageError, volume.VolumeError) as exc:
                    problems.append(f"{'/'.join(parts)}: disk image could not be opened: {exc}")
                    continue
            writer.file(parts, data, rsrc, _prepare_finder_info(fi, "file"), it["created"], it["modified"])
        if problems:
            raise ExpandError("archive contents did not verify: " + "; ".join(problems[:8]))
        writer.source["images"] = images_opened
        writer.source["verified_against"] = "lsar listing (fork sizes, type, creator, Finder flags)"
        return writer.finish()


def identify(path: Path) -> dict:
    """Classify a media file for the media library (content sniffing, no extraction)."""
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(36 * 1024)
        info: dict = {"bytes": size}
        try:
            vols = volume.find_volumes(f, size)
        except (OSError, struct.error):
            vols = []
        if head[:2] == b"ER" or (len(head) > 1026 and head[1024:1026] in (b"BD", b"H+", b"HX")):
            name = None
            if vols:
                try:
                    off, length, kind = vols[0]
                    v = volume.HFSPlusVolume(f, off, length) if kind == "hfs+" else volume.HFSVolume(f, off, length)
                    name = v.name
                except (volume.VolumeError, struct.error, UnicodeDecodeError):
                    name = None
            info.update({"kind": "cd-image" if size > 4 * 1024 * 1024 else "disk-image",
                         "format": "apple-partition-map" if head[:2] == b"ER" else (vols[0][2] if vols else "hfs"),
                         "volume_name": name})
            return info
        if len(head) >= 0x8006 and head[0x8001:0x8006] == b"CD001":
            vid = head[0x8028:0x8048].decode("ascii", "replace").strip()
            return {**info, "kind": "cd-image", "format": "iso9660", "volume_name": vid}
        if diskimage.is_dc42(head[:84]) or (len(head) >= 84 and head[82:84] == b"\x01\x00" and 0 < head[0] <= 63):
            return {**info, "kind": "disk-image", "format": "dc42",
                    "volume_name": head[1:1 + head[0]].decode("mac_roman")}
    magic = head[:8]
    if magic[:4] in (b"SIT!", b"ST46", b"ST50", b"ST60", b"ST65", b"STin", b"STi2", b"STi3", b"STi4") \
            or head[:16].startswith(b"StuffIt (c)1997") or b"StuffIt" in head[:128]:
        return {**info, "kind": "archive", "format": "stuffit"}
    if head.lstrip()[:45].startswith(b"(This file must be converted with BinHex"):
        return {**info, "kind": "archive", "format": "binhex"}
    if head[:4] == b"PK\x03\x04":
        return {**info, "kind": "archive", "format": "zip"}
    if len(head) >= 128 and head[0] == 0 and 0 < head[1] <= 63 and head[74] == 0 and head[82] == 0:
        return {**info, "kind": "archive", "format": "macbinary"}
    return {**info, "kind": "unknown"}
