"""USB Overdrive 1.4 on a stopped guest disk: right-click and scroll wheel.

RetroBoot gives Mac OS a USB HID mouse for buttons and the wheel (the
pointer position still comes from the virtio tablet; ``hostd`` machines.py).
Mac OS 9 has no wheel or secondary-button support of its own, so the base
disk carries USB Overdrive 1.4 (Alessandro Levi Montalcini), registered with
the developer-provided code and configured like a modern Mac mouse:

* right button: Control-click (contextual menus), the panel's default
  "Pass To Mac OS" with Control;
* wheel: Scroll Up/Down, 3 lines a notch (not "Accelerate", which jumps
  about 16 lines a notch as soon as notches come faster than about 8 a
  second, so trackpads and the web Screen page would overshoot);
* middle button: Command-click (open a link in a new window in the browsers,
  add to a Finder selection);
* no startup reminder (registered).

The files cannot be produced offline from the archive: the installer is an
application with its own compressed payload, and registration is stored by
the control panel in its preferences.  So the result is produced once in a
guest and replayed offline, exactly:

    python3 -m rbguestprep usb-overdrive capture IMAGE --out PAYLOAD [--source NOTE]
    python3 -m rbguestprep usb-overdrive status  IMAGE [--payload PAYLOAD]
    python3 -m rbguestprep usb-overdrive install IMAGE --payload PAYLOAD [--report FILE]

``capture`` (read-only, any host) copies the six files below out of a guest
where USB Overdrive was installed, registered and configured in Mac OS into a
private payload (one canonical tree per System Folder subfolder, plus
``payload.json``).  ``install`` (root, hfsplus module, fsck.hfsplus; rb-dev)
puts them into the blessed System Folder with
``install.install_tree(replace=True)``: idempotent (identical files are left
alone and reported ``already-installed``), copies Extensions Manager moved to
a "(Disabled)" folder are removed, every fork is read back and
``fsck.hfsplus -fn`` must pass.  ``status`` reports, read-only, whether a disk
has exactly the payload.

The payload is derived from the user's licensed media and holds the
registration: it stays private (never in Git or an image built for anyone
else), and nothing here prints the registration name or code.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import time

from . import hfsplus, install, prep

PAYLOAD_FORMAT = "retroboot-usb-overdrive-payload"
PAYLOAD_VERSION = 1
# System Folder subfolder -> payload tree directory and the items it holds
FOLDERS = {
    "Control Panels": ("control-panels", ("USB Overdrive",)),
    "Extensions": ("extensions", ("USB Mouse Overdrive", "USB Joystick Overdrive")),
    "Preferences": ("preferences", ("USB Overdrive Prefs", "USB Overdrive Mouse Prefs",
                                    "USB Overdrive Joystick Prefs")),
}
CREATOR = b"USBd"
FLAG_HAS_BEEN_INITED = 0x0100


class OverdriveError(install.InstallError):
    pass


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _lookup(vol: hfsplus.Volume, path: list[str]):
    try:
        return vol.lookup(path)
    except FileNotFoundError:
        return None


def present(image: str) -> bool:
    """Read-only: USB Overdrive's control panel is active (in the blessed System
    Folder's Control Panels; Extensions Manager moves a disabled one to "Control
    Panels (Disabled)").  hostd adds the USB mouse only then (pointer.py)."""
    with hfsplus.Volume(image) as vol:
        rec = _lookup(vol, prep._system_folder(vol) + ["Control Panels", "USB Overdrive"])
        return rec is not None and rec.is_file and rec.data[52:56] == CREATOR


# -- capture ------------------------------------------------------------------------------
def capture(image: str, out: str, source: str = "") -> dict:
    """Copy USB Overdrive's files out of a guest disk into a payload (read-only)."""
    if os.path.exists(out) and os.listdir(out):
        raise OverdriveError("invalid-payload", f"{out} is not empty")
    files = {}
    with hfsplus.Volume(image) as vol:
        sysf = prep._system_folder(vol)
        for folder, (sub, names) in FOLDERS.items():
            entries = []
            for name in names:
                rec = _lookup(vol, sysf + [folder, name])
                if rec is None or not rec.is_file:
                    raise OverdriveError("not-installed", f"System Folder:{folder}:{name} is missing on {image}")
                if rec.data[52:56] != CREATOR:
                    raise OverdriveError("not-installed", f"{folder}:{name} has creator {rec.data[52:56]!r}")
                data, rsrc = vol.read_fork(rec, "data"), vol.read_fork(rec, "rsrc")
                fi = bytearray(rec.data[48:80])
                flags = struct.unpack(">H", fi[8:10])[0] & ~FLAG_HAS_BEEN_INITED
                fi[8:10] = struct.pack(">H", flags)
                fi[10:14] = bytes(4)          # let the Finder place the icon
                entries.append({"path": [name], "kind": "file", "type": fi[0:4].decode("mac_roman"),
                                "creator": fi[4:8].decode("mac_roman"), "flags": flags,
                                "finder_info": bytes(fi).hex(),
                                "data": {"size": len(data), "sha256": _sha(data)},
                                "rsrc": {"size": len(rsrc), "sha256": _sha(rsrc)}})
                for base, blob in (("data", data), ("rsrc", rsrc)):
                    p = os.path.join(out, sub, base, install.linux_name(name))
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    if base == "data" or blob:
                        with open(p, "wb") as f:
                            f.write(blob)
                files[f"{folder}:{name}"] = {"data_sha256": _sha(data), "rsrc_sha256": _sha(rsrc),
                                             "data_size": len(data), "rsrc_size": len(rsrc)}
            with open(os.path.join(out, sub, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump({"format": install.TREE_FORMAT, "version": install.TREE_VERSION,
                           "source": {"kind": "usb-overdrive-capture", "folder": folder},
                           "entries": entries, "skipped": []}, f, indent=1, ensure_ascii=False)
    meta = {"format": PAYLOAD_FORMAT, "version": PAYLOAD_VERSION, "product": "USB Overdrive 1.4",
            "captured": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "source": source,
            "folders": {k: v[0] for k, v in FOLDERS.items()}, "files": files}
    with open(os.path.join(out, "payload.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1, ensure_ascii=False)
    return {"payload": out, "files": len(files)}


# -- payload ------------------------------------------------------------------------------
def load_payload(payload: str) -> dict:
    try:
        with open(os.path.join(payload, "payload.json"), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError) as exc:
        raise OverdriveError("invalid-payload", f"No readable payload.json in {payload}: {exc}") from exc
    if meta.get("format") != PAYLOAD_FORMAT or meta.get("version") != PAYLOAD_VERSION:
        raise OverdriveError("invalid-payload", "Not a USB Overdrive payload.")
    trees = {}
    for folder, (sub, names) in FOLDERS.items():
        tree = install.Tree(os.path.join(payload, sub))
        if sorted(e["path"][0] for e in tree.entries) != sorted(names):
            raise OverdriveError("invalid-payload", f"The {folder} tree does not hold {', '.join(names)}.")
        for e in tree.entries:
            if install._sha(tree.data(e)) != e["data"]["sha256"] or install._sha(tree.rsrc(e)) != e["rsrc"]["sha256"]:
                raise OverdriveError("invalid-payload", f"{folder}:{e['path'][0]} does not match its manifest.")
        trees[folder] = tree
    return {"meta": meta, "trees": trees}


# -- status / install ---------------------------------------------------------------------
def status(image: str, payload: str | None = None) -> dict:
    """Read-only: each item present / identical to the payload, plus disabled copies."""
    trees = load_payload(payload)["trees"] if payload else {}
    out = {"items": {}, "disabled_copies": []}
    with hfsplus.Volume(image) as vol:
        sysf = prep._system_folder(vol)
        out["system_folder"] = ":".join(sysf)
        for folder, (_sub, names) in FOLDERS.items():
            want = {e["path"][0]: e for e in trees[folder].entries} if folder in trees else {}
            for name in names:
                rec = _lookup(vol, sysf + [folder, name])
                key = f"{folder}:{name}"
                if rec is None or not rec.is_file:
                    out["items"][key] = "missing"
                elif name in want:
                    e = want[name]
                    same = (rec.data[48:56] == bytes.fromhex(e["finder_info"])[:8]
                            and _sha(vol.read_fork(rec, "data")) == e["data"]["sha256"]
                            and _sha(vol.read_fork(rec, "rsrc")) == e["rsrc"]["sha256"])
                    out["items"][key] = "identical" if same else "different"
                else:
                    out["items"][key] = "present"
                if _lookup(vol, sysf + [f"{folder} (Disabled)", name]) is not None:
                    out["disabled_copies"].append(f"{folder} (Disabled):{name}")
    states = set(out["items"].values())
    out["installed"] = states == {"identical"} if payload else states <= {"present", "identical"}
    return out


def install_payload(image: str, payload: str) -> dict:
    """Install the payload into the blessed System Folder (root; guest stopped)."""
    p = load_payload(payload)
    before = status(image, payload)
    report = {"image": os.path.abspath(image), "system_folder": before["system_folder"],
              "before": before["items"], "folders": {}}
    if before["disabled_copies"]:
        sysf = before["system_folder"].split(":")
        by_folder: dict[str, list[str]] = {}
        for item in before["disabled_copies"]:
            folder, name = item.split(":", 1)
            by_folder.setdefault(folder, []).append(name)
        for folder, names in by_folder.items():
            install.remove_installed(image, sysf + [folder], names)
        report["removed_disabled_copies"] = before["disabled_copies"]
    for folder, (sub, names) in FOLDERS.items():
        if all(before["items"][f"{folder}:{n}"] == "identical" for n in names):
            report["folders"][folder] = "already-installed"
            continue
        sysf = before["system_folder"].split(":")
        r = install.install_tree(image, sysf + [folder], os.path.join(payload, sub), replace=True)
        report["folders"][folder] = {"installed": r["top"], "replaced": r["replaced"],
                                     "verified_items": r["verified_items"]}
    after = status(image, payload)
    report["after"] = after["items"]
    if not after["installed"] or after["disabled_copies"]:
        raise OverdriveError("verify-failed", "USB Overdrive is not exactly the payload after installing.")
    ok, out = prep.fsck(image, repair=False)
    report["fsck_after"] = "ok" if ok else out.splitlines()[-1:]
    if not ok:
        raise OverdriveError("verify-failed", "The Mac OS disk check failed after installing USB Overdrive.")
    changed = any(v != "already-installed" for v in report["folders"].values()) or "removed_disabled_copies" in report
    report["result"] = "installed" if changed else "already-installed"
    report["product"] = p["meta"].get("product")
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="rbguestprep usb-overdrive", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("image")
    c.add_argument("--out", required=True)
    c.add_argument("--source", default="")
    s = sub.add_parser("status")
    s.add_argument("image")
    s.add_argument("--payload")
    i = sub.add_parser("install")
    i.add_argument("image")
    i.add_argument("--payload", required=True)
    i.add_argument("--report")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "capture":
            r = capture(a.image, a.out, a.source)
        elif a.cmd == "status":
            r = status(a.image, a.payload)
        else:
            r = install_payload(a.image, a.payload)
            if a.report:
                with open(a.report, "w", encoding="utf-8") as f:
                    json.dump(r, f, indent=1, ensure_ascii=False)
    except install.InstallError as exc:
        json.dump({"ok": False, "code": exc.code, "error": str(exc)}, sys.stdout)
        print()
        return 2
    json.dump(r, sys.stdout, indent=1, ensure_ascii=False)
    print()
    return 0 if a.cmd != "status" or r.get("installed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
