#!/usr/bin/env python3
"""Stage 6: the Software Update bundle from the same root filesystem as the image (ARCHITECTURE 13).

    make-update.py build ROOTDIR BOOT_JSON OUTDIR --build-id ID --flavor F --version V
                   [--min-version 0.2.0] [--source-revision REV] [--notes TEXT]
    make-update.py assemble OUTDIR OUT.rbu        # after manifest.sig exists (signed on the Mac)
    make-update.py derive SRC_OUTDIR OUTDIR --break kernel|initrd|hostd|none [--build-id ID]
                                                  # LAB ONLY: a deliberately broken (or rebuilt) payload

``build`` writes OUTDIR/{manifest.json, root-files.json.gz, root.sqfs}; the private
signing key never comes to the builder: ``image/lab/sign_update.py`` reads
manifest.json, signs it on the Mac (private/update-keys/) and writes manifest.sig,
then runs ``assemble``, which writes the tar in the exact member order devices accept.

Only the root filesystem goes in: it carries the kernel, initramfs and GRUB/ESP
payload. Guest machines, ROMs, Apple-derived assets and credentials never do (the
root has none; ``build`` refuses a root that looks otherwise, including a machine
archive anywhere: a thin one is small enough to pass the 512 MiB file limit).
"""
import argparse
import gzip
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import time

MEMBERS = ("manifest.json", "manifest.sig", "root-files.json.gz", "root.sqfs")
FORBIDDEN_DIRS = ("usr/lib/retroboot/installer/embedded", "var/lib/retroboot/machines",
                  "var/lib/retroboot/assets", "var/lib/retroboot/state", "var/lib/retroboot/config",
                  "var/lib/retroboot/roms")
# lab-auth.env: a RetroBoot downloads sign-in (RBDATA/config on a device, an installer medium's
# retroboot/ folder); it lives on RBDATA, which an update never writes, and never in a bundle.
FORBIDDEN_NAMES = ("disk.img", "pairing.json", "tls-key.pem", "known-good.json", "Mac OS ROM", "lab-auth.env")
FORBIDDEN_SUFFIX = (".rom", ".hfv", ".dsk", ".toast", ".dmg", ".iso")
# QEMU's own firmware (open-source option ROMs, OpenBIOS, the video driver) lives in the
# emulator package; any other ROM-like file could be a guest ROM and is refused.
FIRMWARE_DIRS = ("usr/lib/retroboot/qemu/share/qemu",)
# The 68k emulator package's two NuBus declaration ROMs (D044): original code built by
# ClassicMac and RetroBoot, allowed by exact path; nothing else ROM-like in qemu-m68k.
FIRMWARE_FILES = ("usr/lib/retroboot/qemu-m68k/share/qemu/mac_qfb.rom",
                  "usr/lib/retroboot/qemu-m68k/share/qemu/classicvirtio-68k.rom")
# Apple ROMs by SHA-256: refused anywhere, whatever their name (the Quadra 800 ROM travels
# with each 68k Mac on RBDATA, never in an update).
APPLE_ROMS = {"05ad753fb594e656cf078023ec189e09e2a7655a780de993b75b8c51ed6b09ca"}   # Quadra 610/650/800
APPLE_ROM_SIZES = (512 * 1024, 1024 * 1024, 2 * 1024 * 1024, 4 * 1024 * 1024)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(4 << 20), b""):
            h.update(b)
    return h.hexdigest()


def machine_archive(path):
    """True when a gzip file holds a tar whose first members include disk.img (a machine archive)."""
    import gzip
    try:
        with gzip.open(path, 'rb') as gz:
            head = gz.read(256 * 1024)
    except (OSError, EOFError, ValueError):
        return False
    pos = 0
    while pos + 512 <= len(head):
        h = head[pos:pos + 512]
        if h[257:262] != b'ustar':
            return False
        name = h[:100].split(b'\0', 1)[0]
        if name.endswith(b'disk.img') or b'GNUSparseFile' in name:
            return True
        try:
            size = int(h[124:136].split(b'\0', 1)[0].strip() or b'0', 8)
        except ValueError:
            return False
        pos += 512 + (size + 511) // 512 * 512
    return False


def audit(root):
    """Refuse a root that carries guest data, private keys or credentials."""
    problems = []
    for d in FORBIDDEN_DIRS:
        p = os.path.join(root, d)
        if os.path.isdir(p) and os.listdir(p):
            problems.append(f"{d}/ is not empty")
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel.startswith(("proc", "sys", "dev")):
            continue
        for n in filenames:
            p = os.path.join(dirpath, n)
            low = n.lower()
            relp = os.path.relpath(p, root)
            if n in FORBIDDEN_NAMES or (low.endswith(FORBIDDEN_SUFFIX) and not rel.startswith(FIRMWARE_DIRS)
                                        and relp not in FIRMWARE_FILES):
                problems.append(relp)
            elif not os.path.islink(p) and os.path.isfile(p) and os.path.getsize(p) in APPLE_ROM_SIZES \
                    and sha256(p) in APPLE_ROMS:
                problems.append(f"{relp} is an Apple ROM")
            elif n.startswith("ssh_host_") and rel == "etc/ssh":
                problems.append(os.path.relpath(p, root))
            elif os.path.islink(p):
                continue
            elif low.endswith(('.tar.gz', '.tgz')) and machine_archive(p):
                problems.append(f"{os.path.relpath(p, root)} is a machine archive")
            elif os.path.getsize(p) > 512 * 1024 * 1024:
                problems.append(f"{os.path.relpath(p, root)} is larger than 512 MiB")
    if problems:
        sys.exit("make-update: refusing root filesystem:\n  " + "\n  ".join(problems[:30]))


def target_arch(arch, root, boot):
    if arch and arch != "auto":
        return arch
    if boot.get("arch") in ("x86_64", "arm64"):
        return boot["arch"]
    if boot.get("board") == "rpi" or os.path.isfile(os.path.join(root, "usr/lib/retroboot/rpi-profile.json")):
        return "arm64"
    return "x86_64"


def target_model(model, root, boot):
    if model:
        return model
    prof_model = (boot.get("profile") or {}).get("model")
    if prof_model:
        return prof_model
    rpi_prof = os.path.join(root, "usr/lib/retroboot/rpi-profile.json")
    if os.path.isfile(rpi_prof):
        try:
            return json.load(open(rpi_prof)).get("model") or ""
        except (OSError, ValueError):
            pass
    return ""


def build(a):
    root, out = a.root, a.out
    os.makedirs(out, exist_ok=True)
    for n in MEMBERS:
        if os.path.exists(os.path.join(out, n)):
            os.unlink(os.path.join(out, n))
    audit(root)
    rel = json.load(open(os.path.join(root, "usr/lib/retroboot/release.json")))
    boot = json.load(open(a.boot_json))
    tree = os.path.join(root, "usr/lib/retroboot/hostd/retroboot/update/tree.py")
    if not os.path.isfile(tree):
        sys.exit("make-update: the root has no Software Update support (hostd without retroboot/update)")
    if not any(n.endswith(".pub") for n in os.listdir(os.path.join(root, "usr/lib/retroboot/update/keys"))):
        sys.exit("make-update: the root trusts no update key; stage private/update-keys first")
    # Device nodes that are stand-ins (customize-rootfs.sh on a builder that may not create
    # them, bind mounts of the builder's own): the root is done with them, so they are
    # released and removed, and the base's records go into the file list and, as mksquashfs
    # pseudo files, into the squashfs (a pseudo file only applies where the source has none).
    devnodes = []
    if os.path.isfile(root + ".devnodes.json"):
        devnodes = json.load(open(root + ".devnodes.json"))
        for n in devnodes:
            p = os.path.join(root, n["name"].removeprefix("./"))
            subprocess.run(["umount", "-l", p], stderr=subprocess.DEVNULL)
            if os.path.lexists(p):
                os.unlink(p)
    t0 = time.time()
    res = subprocess.run([sys.executable, "-I", tree, "build", root, os.path.join(out, "root-files.json.gz")],
                         capture_output=True, text=True, check=True)
    stats = json.loads(res.stdout)
    pseudo = []
    if devnodes:
        fl = os.path.join(out, "root-files.json.gz")
        with open(fl, "rb") as f:
            doc = json.loads(gzip.decompress(f.read()))
        have = {e[0] for e in doc["entries"]}
        for n in devnodes:
            node = n["name"].removeprefix("./")
            if node in have:
                sys.exit(f"make-update: stand-in {node} still in the root")
            kind = "c" if n["type"] == "chr" else "b"
            doc["entries"].append([node, kind, n["mode"] & 0o7777, n["uid"], n["gid"], f"{n['major']}:{n['minor']}"])
            pseudo += ["-p", f"{node} {kind} {n['mode'] & 0o7777:o} {n['uid']} {n['gid']} {n['major']} {n['minor']}"]
        doc["entries"].sort(key=lambda e: e[0])
        with open(fl, "wb") as f:
            f.write(gzip.compress(json.dumps(doc, separators=(",", ":")).encode(), 9, mtime=0))
        stats["files"] = len(doc["entries"])
        print(f"device nodes: {len(devnodes)} from the base's records", file=sys.stderr)
    print(f"file list: {stats['files']} entries, {stats['bytes']} bytes ({time.time() - t0:.0f} s)", file=sys.stderr)
    t0 = time.time()
    arch = target_arch(getattr(a, "arch", "auto"), root, boot)
    bcj = ["-Xbcj", "x86"] if a.compression == "xz" and arch == "x86_64" else []
    subprocess.run(["mksquashfs", root, os.path.join(out, "root.sqfs"), "-comp", a.compression, "-b", "1M",
                    "-noappend", "-xattrs", "-quiet", "-no-progress"]
                   + (bcj if a.compression == "xz" else
                      ["-Xcompression-level", "19"] if a.compression == "zstd" else []) + pseudo, check=True)
    print(f"root.sqfs: {os.path.getsize(os.path.join(out, 'root.sqfs'))} bytes ({time.time() - t0:.0f} s)",
          file=sys.stderr)
    board = boot.get("board", "pc")
    model = target_model(getattr(a, "model", ""), root, boot) if board == "rpi" else ""
    if board == "rpi":
        if not model:
            model = "pi4-5"
        boot_base = os.path.join(root, "usr/lib/retroboot/boot")
        missing = [
            f for f in ("kernel8.img", "initramfs8", "config.txt")
            if not (os.path.isfile(os.path.join(boot_base, "bootfs", f)) or os.path.isfile(os.path.join(boot_base, f)))
        ]
        if missing:
            sys.exit("make-update: Pi root is missing staged boot files under usr/lib/retroboot/boot/bootfs/: "
                     + ", ".join(missing))
    kernels = sorted(os.listdir(os.path.join(root, "lib/modules")))
    schema = int(rel.get("settings_schema") or 1)
    manifest = {
        "format": "retroboot-update/1",
        "product": "RetroBoot",
        "arch": arch,
        "board": board,
        **({"model": model} if model else {}),
        "version": a.version,
        "build_id": a.build_id,
        "flavor": a.flavor,
        "source_revision": a.source_revision,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "min_version": a.min_version,
        "settings_schema": schema,
        "settings_schema_min": min(schema, a.settings_schema_min),
        "kernel": {"version": kernels[-1] if kernels else None, "cmdline": boot["kernel_cmdline"]},
        "boot": {"files_sha256": boot.get("files_sha256"), "grub": boot.get("grub"),
                 **({"bootfs_files_sha256": boot["bootfs_files_sha256"]} if boot.get("bootfs_files_sha256") else {})},
        "root": {"member": "root.sqfs", "format": "squashfs", "compression": a.compression,
                 "bytes": os.path.getsize(os.path.join(out, "root.sqfs")),
                 "sha256": sha256(os.path.join(out, "root.sqfs")),
                 "installed_bytes": stats["bytes"], "files": stats["files"]},
        "files": {"member": "root-files.json.gz", "bytes": os.path.getsize(os.path.join(out, "root-files.json.gz")),
                  "sha256": sha256(os.path.join(out, "root-files.json.gz"))},
        "notes": a.notes or "",
    }
    try:
        manifest["components"] = json.load(open(os.path.join(root, "usr/lib/retroboot/components.json")))
    except (OSError, ValueError):
        pass
    if rel.get("version") != a.version or rel.get("build_id") != a.build_id:
        sys.exit("make-update: release.json does not match this build")
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
        f.write("\n")
    print(json.dumps({"out": out, "root_bytes": manifest["root"]["bytes"], "files": stats["files"]}))


def assemble(a):
    for n in MEMBERS:
        if not os.path.isfile(os.path.join(a.out, n)):
            sys.exit(f"make-update: {n} is missing (sign the manifest first)")
    tmp = a.rbu + ".tmp"
    with tarfile.open(tmp, "w", format=tarfile.USTAR_FORMAT) as tf:
        for n in MEMBERS:
            ti = tf.gettarinfo(os.path.join(a.out, n), arcname=n)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.mode = 0o644
            ti.mtime = int(time.time())
            with open(os.path.join(a.out, n), "rb") as f:
                tf.addfile(ti, f)
    os.replace(tmp, a.rbu)
    print(json.dumps({"rbu": a.rbu, "bytes": os.path.getsize(a.rbu), "sha256": sha256(a.rbu)}))


def derive(a):
    """LAB ONLY: unpack a built payload, break one part on purpose, rebuild list and manifest
    (unsigned; sign it like any build). For the fallback tests in image/reports/update-*.md."""
    import shutil
    src = json.load(open(os.path.join(a.src, "manifest.json")))
    tmp = os.path.join(a.out, "root")
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(a.out, exist_ok=True)
    subprocess.run(["unsquashfs", "-q", "-n", "-d", tmp, os.path.join(a.src, "root.sqfs")], check=True)
    boot = os.path.join(tmp, "boot")
    if a.break_ == "kernel":        # GRUB cannot load it: the fallback entry boots the old slot at once
        target = os.path.join(boot, os.readlink(os.path.join(boot, "vmlinuz")))
        open(target, "wb").write(b"not a kernel\n" * 64)
    elif a.break_ == "initrd":      # the kernel panics (no root): panic=10 restarts into the old slot
        target = os.path.join(boot, os.readlink(os.path.join(boot, "initrd.img")))
        open(target, "wb").write(b"\0" * 4096)
    elif a.break_ == "hostd":       # hostd crash-loops: the slot guard restarts into the old slot
        p = os.path.join(tmp, "usr/lib/retroboot/hostd/retroboot/hostd.py")
        text = open(p).read().replace("\nlog = logging.getLogger", "\nraise RuntimeError('LAB TEST: hostd broken on purpose')\nlog = logging.getLogger", 1)
        open(p, "w").write(text)
    build_id = a.build_id or "%s-%s-%s" % (src["version"], time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
                                           "lab" + a.break_)
    rel = os.path.join(tmp, "usr/lib/retroboot/release.json")
    r = json.load(open(rel))
    r["build_id"] = build_id
    json.dump(r, open(rel, "w"))
    boot_json = os.path.join(tmp, "usr/lib/retroboot/boot/boot.json")
    ns = argparse.Namespace(root=tmp, boot_json=boot_json, out=a.out, build_id=build_id, flavor=src["flavor"],
                            version=src["version"], min_version=src["min_version"],
                            settings_schema_min=src["settings_schema_min"], source_revision=src["source_revision"],
                            compression=src["root"]["compression"], arch=src.get("arch", "auto"),
                            model=src.get("model", ""),
                            notes=f"LAB TEST build derived from {src['build_id']} (break: {a.break_}).")
    build(ns)
    shutil.rmtree(tmp)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("root")
    b.add_argument("boot_json")
    b.add_argument("out")
    b.add_argument("--build-id", required=True)
    b.add_argument("--flavor", required=True, choices=("debug", "product"))
    b.add_argument("--version", required=True)
    b.add_argument("--min-version", default="0.2.0")
    b.add_argument("--settings-schema-min", type=int, default=1)
    b.add_argument("--source-revision", default="unknown")
    b.add_argument("--arch", default="auto", choices=("auto", "x86_64", "arm64"))
    b.add_argument("--model", default="", help="Raspberry Pi model (e.g. pi4-5, zero2w)")
    b.add_argument("--compression", default="xz", choices=("xz", "zstd"))
    b.add_argument("--notes", default="")
    s = sub.add_parser("assemble")
    s.add_argument("out")
    s.add_argument("rbu")
    d = sub.add_parser("derive")
    d.add_argument("src")
    d.add_argument("out")
    d.add_argument("--break", dest="break_", required=True, choices=("kernel", "initrd", "hostd", "none"))
    d.add_argument("--build-id")
    a = ap.parse_args()
    {"build": build, "assemble": assemble, "derive": derive}[a.cmd](a)


if __name__ == "__main__":
    main()
