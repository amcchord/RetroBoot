"""The canonical intermediate: a directory tree of classic Mac files.

    TREE/manifest.json      format, source, every folder and file (parents first)
    TREE/data/<path>        data forks (folders are directories)
    TREE/rsrc/<path>        resource forks (only for files that have one)

``<path>`` is the Mac path with ':' standing for '/' inside names, the same
mapping the Linux hfsplus driver uses, so a tree path is also the Linux path
of the file on a mounted guest volume.  Each manifest entry::

    {"path": ["SimCity", "SimCity"], "kind": "file",
     "type": "APPL", "creator": "MCRP", "flags": 8448,
     "finder_info": "<64 hex digits: FInfo+FXInfo, or DInfo+DXInfo for folders>",
     "created": "1996-03-01T20:25:14", "modified": "...",
     "data": {"size": n, "sha256": "..."}, "rsrc": {"size": n, "sha256": "..."}}

Names are Unicode strings that must be representable in Mac OS Roman and be
1-31 bytes long; type and creator are Mac OS Roman four-character codes.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path
from typing import Iterable

FORMAT = "retroboot-mac-tree"
VERSION = 1
FLAG_HAS_BEEN_INITED = 0x0100
FLAG_HAS_BUNDLE = 0x2000
FLAG_INVISIBLE = 0x4000
MAX_NAME = 31


class TreeError(Exception):
    pass


def linux_name(mac_name: str) -> str:
    """Mac '/' is legal in names; Linux hfsplus (and this tree) shows it as ':'."""
    return mac_name.replace("/", ":")


def check_name(name: str) -> None:
    if not name or name in (".", ".."):
        raise TreeError(f"invalid Mac name {name!r}")
    if ":" in name:
        raise TreeError(f"Mac names cannot contain ':' ({name!r})")
    try:
        raw = name.encode("mac_roman")
    except UnicodeEncodeError as exc:
        raise TreeError(f"name {name!r} is not representable in Mac OS Roman") from exc
    if len(raw) > MAX_NAME:
        raise TreeError(f"name {name!r} is longer than {MAX_NAME} bytes")


def code(raw: bytes) -> str:
    return raw.decode("mac_roman")


def code_bytes(s: str) -> bytes:
    b = s.encode("mac_roman")
    if len(b) != 4:
        raise TreeError(f"four-character code expected, got {s!r}")
    return b


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_finder_info(type_: bytes, creator: bytes, flags: int, rest: bytes = b"") -> bytes:
    """32 bytes of Finder info for a file: FInfo (type, creator, flags, location, folder) + FXInfo."""
    base = type_ + creator + struct.pack(">H", flags & 0xFFFF)
    rest = (rest or bytes(22))[:22]
    return base + rest + bytes(22 - len(rest))


class TreeWriter:
    """Build a canonical tree.  Add entries parents-first (or any order; the
    manifest is sorted when finished)."""

    def __init__(self, root: Path, source: dict):
        self.root = Path(root)
        if self.root.exists() and any(self.root.iterdir()):
            raise TreeError(f"{self.root} is not empty")
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        (self.root / "rsrc").mkdir(exist_ok=True)
        self.source = source
        self.entries: list[dict] = []
        self.skipped: list[dict] = []
        self._paths: set[tuple[str, ...]] = set()

    def _fs(self, base: str, path: tuple[str, ...]) -> Path:
        return self.root.joinpath(base, *[linux_name(p) for p in path])

    def _register(self, path: tuple[str, ...]) -> None:
        for p in path:
            check_name(p)
        key = tuple(p.casefold() for p in path)
        if key in self._paths:
            raise TreeError(f"duplicate entry {':'.join(path)}")
        self._paths.add(key)

    def folder(self, path: Iterable[str], finder_info: bytes = bytes(32), created: str | None = None,
               modified: str | None = None) -> dict:
        path = tuple(path)
        self._register(path)
        self._fs("data", path).mkdir(parents=True, exist_ok=True)
        e = {"path": list(path), "kind": "folder", "flags": struct.unpack(">H", finder_info[8:10])[0],
             "finder_info": finder_info.hex(), "created": created, "modified": modified}
        self.entries.append(e)
        return e

    def file(self, path: Iterable[str], data: bytes, rsrc: bytes, finder_info: bytes,
             created: str | None = None, modified: str | None = None) -> dict:
        path = tuple(path)
        self._register(path)
        if len(finder_info) != 32:
            raise TreeError("Finder info must be 32 bytes")
        dpath = self._fs("data", path)
        dpath.parent.mkdir(parents=True, exist_ok=True)
        dpath.write_bytes(data)
        e = {"path": list(path), "kind": "file", "type": code(finder_info[0:4]),
             "creator": code(finder_info[4:8]), "flags": struct.unpack(">H", finder_info[8:10])[0],
             "finder_info": finder_info.hex(), "created": created, "modified": modified,
             "data": {"size": len(data), "sha256": sha256(data)}}
        if rsrc:
            rpath = self._fs("rsrc", path)
            rpath.parent.mkdir(parents=True, exist_ok=True)
            rpath.write_bytes(rsrc)
        e["rsrc"] = {"size": len(rsrc), "sha256": sha256(rsrc)}
        self.entries.append(e)
        return e

    def skip(self, path: Iterable[str], reason: str) -> None:
        self.skipped.append({"path": list(path), "reason": reason})

    def finish(self) -> dict:
        # every parent folder must be an entry of its own
        have = {tuple(e["path"]) for e in self.entries if e["kind"] == "folder"}
        for e in list(self.entries):
            for i in range(1, len(e["path"])):
                parent = tuple(e["path"][:i])
                if parent not in have:
                    self.folder(parent)
                    have.add(parent)
        self.entries.sort(key=lambda e: (len(e["path"]), e["path"]))
        files = [e for e in self.entries if e["kind"] == "file"]
        manifest = {"format": FORMAT, "version": VERSION, "source": self.source,
                    "summary": {"folders": len(self.entries) - len(files), "files": len(files),
                                "data_bytes": sum(e["data"]["size"] for e in files),
                                "rsrc_bytes": sum(e["rsrc"]["size"] for e in files)},
                    "top": sorted({e["path"][0] for e in self.entries}),
                    "entries": self.entries, "skipped": self.skipped}
        tmp = self.root / "manifest.json.tmp"
        tmp.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.root / "manifest.json")
        return manifest


class Tree:
    """A finished canonical tree (read side)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        try:
            self.manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TreeError(f"no readable manifest in {self.root}: {exc}") from exc
        if self.manifest.get("format") != FORMAT or self.manifest.get("version") != VERSION:
            raise TreeError("not a RetroBoot Mac tree (format/version)")
        self.entries = self.manifest["entries"]
        for e in self.entries:
            for p in e["path"]:
                check_name(p)
            if e["kind"] not in ("file", "folder"):
                raise TreeError(f"unknown entry kind {e['kind']!r}")
            if len(bytes.fromhex(e["finder_info"])) != 32:
                raise TreeError("Finder info must be 32 bytes")

    def data_path(self, e: dict) -> Path:
        return self.root.joinpath("data", *[linux_name(p) for p in e["path"]])

    def rsrc_path(self, e: dict) -> Path:
        return self.root.joinpath("rsrc", *[linux_name(p) for p in e["path"]])

    def read_data(self, e: dict) -> bytes:
        return self.data_path(e).read_bytes()

    def read_rsrc(self, e: dict) -> bytes:
        if not e.get("rsrc", {}).get("size"):
            return b""
        return self.rsrc_path(e).read_bytes()

    def finder_info(self, e: dict) -> bytes:
        return bytes.fromhex(e["finder_info"])

    @property
    def top(self) -> list[str]:
        return sorted({e["path"][0] for e in self.entries})

    def total_bytes(self) -> int:
        return sum(e["data"]["size"] + e["rsrc"]["size"] for e in self.entries if e["kind"] == "file")

    def verify(self) -> list[str]:
        """Re-hash every fork against the manifest; returns problems (empty = OK)."""
        problems = []
        for e in self.entries:
            if e["kind"] == "folder":
                if not self.data_path(e).is_dir():
                    problems.append(f"missing folder {':'.join(e['path'])}")
                continue
            for which, reader in (("data", self.read_data), ("rsrc", self.read_rsrc)):
                try:
                    blob = reader(e)
                except OSError as exc:
                    problems.append(f"{':'.join(e['path'])} {which}: {exc}")
                    continue
                want = e[which]
                if len(blob) != want["size"] or sha256(blob) != want["sha256"]:
                    problems.append(f"{':'.join(e['path'])} {which} fork differs from the manifest")
        return problems
