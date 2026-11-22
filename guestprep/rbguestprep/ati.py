"""ATI's Mac OS 9 graphics software for RetroBoot's emulated ATI Radeon (ARCHITECTURE 4.9).

Mac OS drives the emulated Radeon R100 with ATI's own software from the
user's Mac OS 9.2.1 CD.  RetroBoot installs the ``radeon-3d`` set: hardware 3D
through ATI's RAVE engine and Apple's OpenGL, QuickDraw drawing into the
card's memory in software (ATI's 2D accelerator is left out on purpose: under
emulation it makes CopyBits from an offscreen GWorld about 40 times slower;
research/gfx/reports/gfx-2026-10-11.md):

* ``ATI Driver Update`` (the CD's System Folder:Extensions; the native display
  driver ``.Display_RADEON`` for ``ATY,Rage6p``);
* ``ATI Radeon 3D Accelerator`` and ``ATI Resource Manager`` (read out of the
  CD's Installation Tome with ``rbapps.tome``);
* Apple's ``OpenGLRendererATI`` moved from Extensions (Disabled) back into
  Extensions (Mac OS Install leaves it there on a Mac without ATI graphics);
* ``RetroBoot Display`` (RetroBoot's own helper, guest/rbdisplay): ATI's driver
  starts every session at 640 x 480, so hostd passes the Mac screen size it
  chose (``-prom-env retroboot-display=WxH``). Its startup extension
  (`` RetroBoot Display Startup``, 68k INIT) sets it during the extension
  parade and saves it, so later startups restore it themselves; the
  background-only app (``RetroBoot Display``) sets it as the Finder starts if
  the screen is not that size yet. The helper tree (guest/rbdisplay/build.sh)
  holds both; an older one (rbdisplay-1) only the app.

Command line (``python3 -m rbguestprep ati ...``)::

    status  IMAGE                                   read-only; exit 1 when the set is incomplete
    tree    --cd ISO OUT [--set radeon-3d]          any host, no root: the set as a canonical tree
    install IMAGE (--cd ISO | --tree TREE) [--helper TREE|none] [--set radeon-3d] [--report FILE]

``install`` needs root, the hfsplus module and fsck.hfsplus (rb-dev or a
RetroBoot device) with the guest stopped; run it on a copy.  It is idempotent:
earlier copies in Extensions or Extensions (Disabled) are replaced, every fork
is read back and ``fsck.hfsplus -fn`` must pass.  The CD may be an ISO
(HFS/HFS+ hybrid) or the CD's HFS image.  ``--helper`` defaults to the
RetroBoot Display tree shipped with the image (``HELPER_TREES``).

Everything this produces from the CD is the user's own Apple/ATI software:
never commit it or put it in an image built for someone else.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from . import hfsplus, install, prep

SETS = {
    # what Mac OS Install puts on a Radeon Mac, without the 2D accelerator
    "radeon-3d": ("ATI Radeon 3D Accelerator", "ATI Resource Manager"),
    "radeon": ("ATI Graphics Accelerator", "ATI Radeon 3D Accelerator", "ATI Resource Manager"),
    "driver": (),
}
DEFAULT_SET = "radeon-3d"
CD_DRIVER = "System Folder:Extensions:ATI Driver Update"
TOME = "Software Installers:System Software:Mac OS 9.2.1:Installation Tome"
DRIVER = "ATI Driver Update"
ACCELERATOR_2D = "ATI Graphics Accelerator"
OPENGL_RENDERER = "OpenGLRendererATI"
HELPER = "RetroBoot Display"
# Its startup extension (guest/rbdisplay/init, 68k INIT): the same during the
# extension parade, saving the size so later startups restore it themselves.
# The leading space loads it right after " EM Extension".
HELPER_STARTUP = " RetroBoot Display Startup"
# Where install() finds RetroBoot Display's canonical tree when --helper is not
# given: $RB_RBDISPLAY_TREE, the image (/usr/lib/retroboot/guest/rbdisplay), the
# source checkout's build output (guest/rbdisplay/build.sh).
HELPER_TREES = (os.environ.get("RB_RBDISPLAY_TREE") or "",
                "/usr/lib/retroboot/guest/rbdisplay",
                str(Path(__file__).resolve().parents[2] / "guest" / "rbdisplay" / "build" / "tree"))


class ATIError(prep.PrepError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _rbapps():
    """apps/rbapps (classic media tools): the CD's volume and its Installation Tome."""
    try:
        from rbapps import tome, volume  # noqa: F401
    except ImportError:
        here = Path(__file__).resolve()
        for p in (os.environ.get("RB_APPS_PATH"), str(here.parents[2] / "apps")):
            if p and os.path.isdir(os.path.join(p, "rbapps")) and p not in sys.path:
                sys.path.insert(0, p)
        from rbapps import tome, volume  # noqa: F401
    return tome, volume


def helper_tree(explicit: str | None = None) -> str | None:
    """RetroBoot Display's tree: ``explicit`` ('none' disables it), else the first of HELPER_TREES."""
    if explicit == "none":
        return None
    for p in ([explicit] if explicit else list(HELPER_TREES)):
        if p and os.path.isfile(os.path.join(p, "manifest.json")):
            return p
    if explicit:
        raise ATIError("helper-missing", f"RetroBoot Display's files are not at {explicit}.")
    return None


# ---------------------------------------------------------------- read-only
def status(image: str) -> dict:
    """What of the set is on the disk (blessed System Folder), read-only.

    ``state``: ``installed`` (driver, the set's 3D pieces and OpenGLRendererATI
    in Extensions), ``partial`` or ``absent``; ``helper`` says whether RetroBoot
    Display is there (without it Mac OS keeps ATI's 640 x 480), ``helper_startup``
    whether its startup extension is (the size during the extension parade)."""
    out: dict = {}
    with hfsplus.Volume(image) as vol:
        sysf = prep._system_folder(vol)

        def where(name):
            for folder in ("Extensions", "Extensions (Disabled)"):
                try:
                    vol.lookup(sysf + [folder, name])
                    return "enabled" if folder == "Extensions" else "disabled"
                except FileNotFoundError:
                    pass
            return "missing"
        for name in (DRIVER,) + SETS["radeon"] + (OPENGL_RENDERER, HELPER, HELPER_STARTUP):
            out[name] = where(name)
    needed = (DRIVER,) + SETS[DEFAULT_SET] + (OPENGL_RENDERER,)
    have = [n for n in needed if out[n] == "enabled"]
    state = "installed" if len(have) == len(needed) else ("absent" if out[DRIVER] != "enabled" else "partial")
    return {"state": state, "helper": out[HELPER] == "enabled",
            "helper_startup": out[HELPER_STARTUP] == "enabled", "items": out}


# ---------------------------------------------------------------- the CD
def build_tree(cd: str, out: str, which: str = DEFAULT_SET) -> dict:
    """The set as an rbapps canonical tree, read from the user's Mac OS 9.2.1 CD."""
    tome_mod, volume = _rbapps()
    from rbapps import tree as rtree
    if which not in SETS:
        raise ATIError("bad-set", f"unknown set {which!r}")
    writer = rtree.TreeWriter(Path(out), {"kind": "mac-os-9.2.1-cd", "path": os.path.basename(cd), "set": which})
    try:
        f = open(cd, "rb")
    except OSError as exc:
        raise ATIError("cd-unreadable", f"The CD image cannot be read: {exc.strerror or exc}.") from exc
    with f:
        try:
            v = volume.open_volume(f, os.path.getsize(cd))
        except Exception as exc:
            raise ATIError("not-mac-cd", "That disc image is not a Mac OS CD.") from exc
        by_path = {":".join(e.path): e for e in v.entries()}
        e = by_path.get(CD_DRIVER)
        if e is None:
            raise ATIError("not-macos-921-cd", "That disc does not have ATI's graphics software. "
                                               "Use the Mac OS 9.2.1 CD (the one RetroBoot installs Mac OS from).")
        fi = bytearray(e.finder_info)
        fi[8:10] = (int.from_bytes(fi[8:10], "big") & ~rtree.FLAG_HAS_BEEN_INITED).to_bytes(2, "big")
        writer.file((e.path[-1],), v.read_fork(e, "data"), v.read_fork(e, "rsrc"), bytes(fi),
                    volume.mac_date(e.created), volume.mac_date(e.modified))
        if SETS[which]:
            t = by_path.get(TOME)
            if t is None:
                raise ATIError("not-macos-921-cd", "That disc has no Mac OS 9.2.1 Installation Tome.")
            book = tome_mod.Tome(v.read_fork(t, "data"))
            for name in SETS[which]:
                try:
                    m = book.find(name)
                except KeyError as exc:
                    raise ATIError("not-macos-921-cd", f"The Installation Tome has no {name}.") from exc
                fi = bytearray(32)
                fi[0:4], fi[4:8] = m.ftype, m.creator
                fi[8:10] = (m.finder_flags & ~rtree.FLAG_HAS_BEEN_INITED).to_bytes(2, "big")
                writer.file((name,), book.fork(m, "data"), book.fork(m, "rsrc"), bytes(fi),
                            volume.mac_date(m.crdate), volume.mac_date(m.mddate))
    return writer.finish()


# ---------------------------------------------------------------- install (root)
def _tops(tree_root: str) -> list[str]:
    return json.loads(Path(tree_root, "manifest.json").read_text())["top"]


def _sweep(image: str, names: list[str], disable_2d: bool) -> dict:
    """Earlier copies of ``names`` (Extensions, Extensions (Disabled)); install_tree's
    replace handles folders only.  Also: the 2D accelerator off (``disable_2d``,
    radeon-3d), and Apple's OpenGLRendererATI back into Extensions."""
    removed, moved = [], {}
    with hfsplus.Volume(image) as vol:
        sysf = prep._system_folder(vol)
    with install._mounted(image) as mnt:
        base = os.path.join(mnt, *[install.linux_name(p) for p in sysf])
        ext = os.path.join(base, "Extensions")
        dis = os.path.join(base, "Extensions (Disabled)")
        for folder in (ext, dis):
            for name in names:
                p = os.path.join(folder, install.linux_name(name))
                if os.path.isfile(p):
                    os.remove(p)
                    removed.append(f"{os.path.basename(folder)}:{name}")
        src = os.path.join(ext, ACCELERATOR_2D)
        if disable_2d and os.path.exists(src):
            os.makedirs(dis, exist_ok=True)
            dst = os.path.join(dis, ACCELERATOR_2D)
            if os.path.exists(dst):
                os.remove(dst)
            os.rename(src, dst)
            moved[ACCELERATOR_2D] = "moved to Extensions (Disabled)"
        src = os.path.join(dis, OPENGL_RENDERER)
        dst = os.path.join(ext, OPENGL_RENDERER)
        if os.path.exists(dst):
            moved[OPENGL_RENDERER] = "already enabled"
        elif os.path.exists(src):
            os.rename(src, dst)
            moved[OPENGL_RENDERER] = "moved to Extensions"
        else:
            moved[OPENGL_RENDERER] = "not present"
    return {"removed": removed, "moved": moved}


def install_set(image: str, *, cd: str | None = None, tree: str | None = None, helper: str | None = None,
                which: str = DEFAULT_SET) -> dict:
    """Install the set (and RetroBoot Display) into the stopped guest's Extensions."""
    if not cd and not tree:
        raise ATIError("no-source", "Choose the Mac OS 9.2.1 CD.")
    helper_root = helper_tree(helper)
    tmp = None
    try:
        if not tree:
            tmp = tempfile.mkdtemp(prefix="rb-ati-")
            tree = os.path.join(tmp, "tree")
            build_tree(cd, tree, which)
        tops = _tops(tree)
        if DRIVER not in tops:
            raise ATIError("bad-tree", "The ATI tree has no ATI Driver Update.")
        trees = [tree] + ([helper_root] if helper_root else [])
        names = [n for t in trees for n in _tops(t)]
        report: dict = {"set": which, "items": names, "helper": bool(helper_root)}
        report.update(_sweep(image, names, disable_2d=which == "radeon-3d"))
        with hfsplus.Volume(image) as vol:
            dest = prep._system_folder(vol) + ["Extensions"]
        report["trees"] = []
        for t in trees:
            r = install.install_tree(image, dest, t, replace=True)
            report["trees"].append({k: r.get(k) for k in ("top", "files", "bytes", "verified_items",
                                                          "fsck_before", "fsck_after")})
        ok, out, _cosmetic = prep.fsck_verify(image)
        report["fsck"] = "ok" if ok else out.strip().splitlines()[-1:]
        if not ok:
            raise ATIError("fsck", "The disk did not check out after installing ATI's software.")
        report["status"] = status(image)
        return report
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="rbguestprep ati", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("image")
    t = sub.add_parser("tree")
    t.add_argument("--cd", required=True)
    t.add_argument("--set", choices=tuple(SETS), default=DEFAULT_SET)
    t.add_argument("out")
    i = sub.add_parser("install")
    i.add_argument("image")
    i.add_argument("--cd")
    i.add_argument("--tree")
    i.add_argument("--helper", help="RetroBoot Display tree, or 'none'")
    i.add_argument("--set", choices=tuple(SETS), default=DEFAULT_SET)
    i.add_argument("--report", type=Path)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            r = status(a.image)
            print(json.dumps(r, indent=1))
            return 0 if r["state"] == "installed" and r["helper"] else 1
        if a.cmd == "tree":
            m = build_tree(a.cd, a.out, a.set)
            print(json.dumps({"top": m.get("top"), "summary": m.get("summary")}, indent=1))
            return 0
        r = install_set(a.image, cd=a.cd, tree=a.tree, helper=a.helper, which=a.set)
    except ATIError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(r, indent=1, default=str))
    if a.report:
        a.report.write_text(json.dumps(r, indent=1, default=str) + "\n")
    return 0
