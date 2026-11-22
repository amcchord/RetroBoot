#!/usr/bin/env python3
"""Write the wave7/gemini-kernel handoff JSON from the build manifests (rb-builder).

Usage: write-handoff.py --status candidate|ready --revision REV [--tests DIR ...]
       [--charger-test TEXT] [--nftables-test TEXT] [--note TEXT ...]
       [--boot-dir DIR] [--candidate NAME] [--superseded SHA256 ...]
       [--out /srv/retroboot/wave7/handoff/gemini-kernel.json]
The boot directory (default $WORK/out/boot/latest) names its kernel and initrd builds.
With --candidate NAME the entry goes under "candidates" of the existing JSON, leaving
the main entry alone.
"""
import argparse
import datetime
import json
import os

WORK = os.environ.get("RB_GEMINI_WORK", "/srv/retroboot/wave7/gemini-kernel")


def load(p):
    with open(p) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", required=True)
    ap.add_argument("--revision", required=True)
    ap.add_argument("--tests", nargs="*", default=[])
    ap.add_argument("--charger-test", default="")
    ap.add_argument("--note", action="append", default=[])
    ap.add_argument("--nftables-test", default="")
    ap.add_argument("--boot-dir", default=f"{WORK}/out/boot/latest")
    ap.add_argument("--candidate", default="")
    ap.add_argument("--superseded", nargs="*", default=[])
    ap.add_argument("--out", default="/srv/retroboot/wave7/handoff/gemini-kernel.json")
    a = ap.parse_args()

    bdir = os.path.realpath(a.boot_dir)
    b = load(f"{bdir}/manifest.json")
    kdir = b["kernel"]["dir"]
    idir = os.path.dirname(b["initrd"]["path"])
    k, i = load(f"{kdir}/manifest.json"), load(f"{idir}/manifest.json")
    chk = load(f"{bdir}/check.json")
    assert b["kernel"]["build_id"] == k["build_id"] and b["initrd"]["build_id"] == i["build_id"]
    kfiles = {n: dict(v, path=f"{kdir}/{n}") for n, v in k["outputs"].items()}
    modtar = k["modules"]["tarball"]
    fwtar = k["upstream"]["firmware_tarball"]
    tests = []
    for t in a.tests:
        s = load(f"{t}/summary.json")
        tests.append({"run": t, "passes": s["passes"], "fails": s["fails"], "failed": s["failed"],
                      "test_kernel": s["test_kernel"], "initrd_sha256": s["initrd_sha256"]})

    h = {
        "status": a.status,
        "workstream": "wave7/gemini-kernel",
        "branch": "wave7/gemini-kernel",
        "revision": a.revision,
        "updated_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "boot_img": {
            "path": b["boot_img"]["path"], "sha256": b["boot_img"]["sha256"], "size": b["boot_img"]["size"],
            "target": "eMMC GPT PARTNAME=boot (p22, 16 MiB); LK boots it unless para says boot-recovery",
            "check": f"{bdir}/check.txt", "check_ok": chk["ok"], "manifest": f"{bdir}/manifest.json",
            "header_cmdline": b["header_cmdline"],
            "geometry": "base 0x40000000, kernel 0x40200000, ramdisk 0x45000000, second 0x40f00000, tags 0x44000000, page 2048",
        },
        "kernel": {
            "build_id": k["build_id"], "dir": kdir, "release": k["kernel_release"],
            "cmdline_compiled_in": k["config"]["cmdline"], "cmdline_force": True,
            "upstream": {"repo": k["upstream"]["repo"], "rev": k["upstream"]["rev"]},
            "linux": k["linux"], "safety_gate": k["config"]["safety_gate"],
            "files": {n: kfiles[n] for n in kfiles if n != modtar and n != fwtar},
            "manifest": f"{kdir}/manifest.json",
        },
        "modules": {
            "tarball": kfiles[modtar]["path"], "sha256": kfiles[modtar]["sha256"],
            "layout": f"usr/lib/modules/{k['kernel_release']} (merged-usr), depmod done, stripped",
            "install": f"tar -C <root> --zstd -xf {kfiles[modtar]['path']}",
            "count": k["modules"]["count"],
            "out_of_tree": k["modules"]["out_of_tree"],
            "notes": ["mt6351-gauge.ko (extra/) is the MT6351 fuel gauge: load with system=1 for a System battery",
                      "geminipda-drm (DRM/KMS on LK's framebuffer, /dev/dri/card0) is a module; panfrost must load only after the GPU power-on sequence (upstream blacklists it)",
                      "sramldo-smc.ko is built but not shipped (A72 bring-up is in-kernel since upstream c028f86)"],
        },
        "firmware": {
            "tarball": kfiles[fwtar]["path"], "sha256": kfiles[fwtar]["sha256"],
            "layout": "usr/lib/firmware: WMT_SOC.cfg, ROMv3_patch_1_{0,1}_hdr.bin, WIFI_RAM_CODE_6797, rtw88/rtw8821c_fw.bin",
            "excluded": "WIFI_factory.bin (upstream's own unit's factory NVRAM record; this Gemini's record is in nvdata)",
            "license": "none upstream; private builder artifact only, never commit",
        },
        "initrd": {"build_id": i["build_id"], "path": f"{idir}/initrd.cpio.gz",
                   "sha256": i["outputs"]["initrd.cpio.gz"]["sha256"], "manifest": f"{idir}/manifest.json",
                   "busybox_static": i["debian_packages"]["busybox-static"]["version"],
                   "e2fsck_static": i["debian_packages"]["e2fsck-static"]["version"]},
        "contract": {
            "root": "the directory /retroboot on GPT PARTNAME=linux (ext4) is bind-mounted rw as /; the outer mount is dropped (lazy umount); /dev /proc /sys /run are moved; switch_root to /sbin/init",
            "counter_file": "/.retroboot-boot-attempts at the root of the linux filesystem (outside /retroboot)",
            "counter_format": "decimal ASCII number plus newline; missing or unparsable = 0",
            "counter_rule": "the initrd increments it before every handover; at 3 (or with no /retroboot/sbin/init, no linux partition, or an unmountable fs) it writes 'boot-recovery' + 19 NULs to para, resets the counter to 0, writes /.retroboot-fallback and reboots into Gemian (p1)",
            "userspace_reset": "once RetroBoot is healthy: mount /dev/disk/by-partlabel/linux (or RB_LINUX_DEV from /run/retroboot-initrd/state) at a private mountpoint (same superblock as /, allowed), write '0\\n' to .retroboot-boot-attempts.new, rename it over .retroboot-boot-attempts, sync, unmount",
            "runtime_state": "/run/retroboot-initrd/state: RB_INITRD_VERSION, RB_BOOT_MODE, RB_LINUX_DEV, RB_PARA_DEV, RB_ROOT_SUBDIR, RB_COUNTER_FILE, RB_BOOT_ATTEMPT, RB_FSCK_RC",
            "logs": "/.retroboot-initrd.log (+ .1): dmesg and e2fsck output of the last boot, readable from Gemian too",
            "fstab": "no / entry needed (systemd-fsck-root skips a rw /); if present use pass 0 and options valid for a remount",
            "silence": "kernel console is ttyS0 only, no fbcon or logo: the panel shows LK's logo until something draws through geminipda-drm",
            "panic": "panic=10: a kernel panic reboots and counts as a failed attempt",
        },
        "tests": {"qemu": tests, "charger_unit": a.charger_test, "nftables_modules": a.nftables_test},
        "notes": a.note,
        "superseded_boot_img_sha256": a.superseded,
        "retroboot_patches": k.get("retroboot_patches", {}),
    }
    if a.candidate:
        with open(a.out) as f:
            full = json.load(f)
        h["status"] = a.status
        full.setdefault("candidates", {})[a.candidate] = h
        h = full
    tmp = a.out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(h, f, indent=2)
    os.replace(tmp, a.out)
    print(a.out)


if __name__ == "__main__":
    main()
