"""Root filesystem file lists: build one (image build) and verify a written slot against one.

Entry: ``[path, type, mode, uid, gid, extra]`` with type ``f`` (extra = SHA-256),
``l`` (link target), ``d``, ``c``/``b`` (extra = "major:minor") or ``p``.  Paths are
relative, ``/``-separated, without a leading slash, sorted.

Standard library only; also runs as a script so the device can verify at idle
CPU/I/O priority in a child process::

    python3 -I tree.py build ROOT OUT.json.gz
    python3 -I tree.py verify LIST.json.gz ROOT      # JSON progress lines, exit 0 when equal
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import stat
import sys

FILES_FORMAT = "retroboot-root-files/1"
# Present on a freshly made ext4 slot but not part of the release.
IGNORE_TOP = {"lost+found"}


def _hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def entry(root: str, rel: str) -> list | None:
    full = os.path.join(root, rel)
    st = os.lstat(full)
    mode = stat.S_IMODE(st.st_mode)
    base = [rel, None, mode, st.st_uid, st.st_gid, ""]
    if stat.S_ISREG(st.st_mode):
        base[1], base[5] = "f", _hash(full)
    elif stat.S_ISLNK(st.st_mode):
        base[1], base[5] = "l", os.readlink(full)
    elif stat.S_ISDIR(st.st_mode):
        base[1] = "d"
    elif stat.S_ISCHR(st.st_mode) or stat.S_ISBLK(st.st_mode):
        base[1] = "c" if stat.S_ISCHR(st.st_mode) else "b"
        base[5] = f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
    elif stat.S_ISFIFO(st.st_mode):
        base[1] = "p"
    else:
        return None   # sockets are runtime objects
    return base


def walk(root: str):
    """Relative paths below root (no crossing into other filesystems)."""
    dev = os.lstat(root).st_dev
    stack = [""]
    while stack:
        rel = stack.pop()
        full = os.path.join(root, rel) if rel else root
        try:
            names = sorted(os.listdir(full))
        except OSError:
            continue
        for n in names:
            r = f"{rel}/{n}" if rel else n
            if not rel and n in IGNORE_TOP:
                continue
            st = os.lstat(os.path.join(root, r))
            yield r
            if stat.S_ISDIR(st.st_mode) and st.st_dev == dev:
                stack.append(r)


def build(root: str) -> dict:
    entries = []
    for rel in walk(root):
        e = entry(root, rel)
        if e is not None:
            entries.append(e)
    entries.sort(key=lambda e: e[0])
    return {"format": FILES_FORMAT, "entries": entries}


def verify(entries: list, root: str, progress=None) -> list[str]:
    """Problems (empty when the tree at root is exactly the listed one)."""
    problems: list[str] = []
    want = {e[0]: e for e in entries}
    seen = set()
    total = len(entries) or 1
    for i, rel in enumerate(walk(root)):
        e = want.get(rel)
        if e is None:
            problems.append(f"unexpected {rel}")
            if len(problems) > 20:
                break
            continue
        seen.add(rel)
        try:
            got = entry(root, rel)
        except OSError as exc:
            problems.append(f"unreadable {rel}: {exc}")
            continue
        if got is None or list(got) != list(e):
            problems.append(f"differs {rel}")
            if len(problems) > 20:
                break
        if progress and i % 500 == 0:
            progress(min(1.0, len(seen) / total))
    missing = [p for p in want if p not in seen]
    problems += [f"missing {p}" for p in missing[:20]]
    if progress:
        progress(1.0)
    return problems


def load(path: str) -> list:
    with open(path, "rb") as f:
        doc = json.loads(gzip.decompress(f.read()))
    if doc.get("format") != FILES_FORMAT:
        raise ValueError("not a RetroBoot file list")
    return doc["entries"]


def main(argv: list[str]) -> int:
    if argv[:1] == ["build"] and len(argv) == 3:
        doc = build(argv[1])
        data = gzip.compress(json.dumps(doc, separators=(",", ":")).encode(), 9)
        with open(argv[2], "wb") as f:
            f.write(data)
        size = sum(os.lstat(os.path.join(argv[1], e[0])).st_size for e in doc["entries"] if e[1] == "f")
        print(json.dumps({"files": len(doc["entries"]), "bytes": size}))
        return 0
    if argv[:1] == ["verify"] and len(argv) == 3:
        entries = load(argv[1])

        def prog(f):
            print(json.dumps({"progress": round(f, 4)}), flush=True)
        problems = verify(entries, argv[2], prog)
        print(json.dumps({"ok": not problems, "problems": problems[:40]}), flush=True)
        return 0 if not problems else 3
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
