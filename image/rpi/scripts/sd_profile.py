#!/usr/bin/env python3
"""Board policy and the Zero 2 W's firmware-compatible SD partition table.

This writes only a new image file. It never accepts a block device.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import stat
import struct


MODELS = {
    "pi4-5": {"name": "Raspberry Pi 4 / 5", "partition_table": "gpt",
              "machine_families": ["powermac", "quadra800"]},
    "pi4": {"name": "Raspberry Pi 4", "partition_table": "gpt",
            "machine_families": ["powermac", "quadra800"]},
    "pi5": {"name": "Raspberry Pi 5", "partition_table": "gpt",
            "machine_families": ["powermac", "quadra800"]},
    "zero2w": {"name": "Raspberry Pi Zero 2 W", "partition_table": "dos",
               "machine_families": ["quadra800"], "quadra_ram_mb": 64,
               "tcg_tb_size_mb": 64},
}


def profile(model):
    return {"schema_version": 1, "model": model, **MODELS[model]}


def validate_machine(model, machine):
    """Reject a PPC private base before building the Zero image, without changing it."""
    if model != "zero2w":
        return
    machine = Path(machine)
    config = json.loads((machine / "config.json").read_text())
    if not isinstance(config, dict) or config.get("machineFamily") != "quadra800":
        raise ValueError("The Zero 2 W image accepts only a 68040 Quadra 800 Mac (Mac OS 7.5.5 or 8.1).")
    if (machine / "machine.json").exists():
        meta = json.loads((machine / "machine.json").read_text())
        if not isinstance(meta, dict) or meta.get("family") != "quadra800":
            raise ValueError("The embedded machine record is not a Quadra 800.")


def mbr_partitions(boot_mib, root_mib, data_mib):
    """Build 4 primary MBR partitions: FAT32 boot (p1), RBROOT_A (p2), RBROOT_B (p3), RBDATA (p4)."""
    sizes = (boot_mib, root_mib, root_mib, data_mib)
    if any(type(n) is not int or n < 1 for n in sizes):
        raise ValueError("Partition sizes must be positive MiB integers.")
    start = 2048
    parts = []
    for number, size in enumerate(sizes, 1):
        count = size * 2048
        if start + count > 0xFFFFFFFF:
            raise ValueError("The Zero 2 W MBR image must be smaller than 2 TiB.")
        parts.append({"number": number, "type": 0x0C if number == 1 else 0x83,
                      "start_sector": start, "sectors": count})
        start += count
    return parts


def write_mbr(path, boot_mib, root_mib, data_mib, disk_id=None):
    """Initialize a newly created sparse regular file with four aligned A/B partitions."""
    parts = mbr_partitions(boot_mib, root_mib, data_mib)
    disk_id = secrets.randbelow(0xFFFFFFFF) + 1 if disk_id is None else disk_id
    if type(disk_id) is not int or not 0 < disk_id <= 0xFFFFFFFF:
        raise ValueError("Invalid MBR disk identifier.")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("An SD image must be a new regular file.")
        mbr = bytearray(512)
        struct.pack_into("<I", mbr, 440, disk_id)
        for p in parts:
            # LBA addressing; saturated CHS fields are deliberately not used for boot.
            struct.pack_into("<B3sB3sII", mbr, 446 + (p["number"] - 1) * 16,
                             0x80 if p["number"] == 1 else 0,
                             b"\xfe\xff\xff", p["type"], b"\xfe\xff\xff",
                             p["start_sector"], p["sectors"])
        mbr[510:512] = b"\x55\xaa"
        os.write(fd, mbr)
        os.ftruncate(fd, (parts[-1]["start_sector"] + parts[-1]["sectors"]) * 512)
    finally:
        os.close(fd)
    return {"disk_id": f"{disk_id:08x}", "partitions": parts}


def update_fstab(root, boot_uuid, root_uuid, data_uuid):
    path = Path(root) / "etc/fstab"
    text = path.read_text()
    for label, uuid in (("RBEFI", boot_uuid), ("RBROOT_A", root_uuid), ("RBDATA", data_uuid)):
        old = "PARTLABEL=" + label
        if text.count(old) != 1:
            raise ValueError(f"fstab must contain exactly one {old} entry")
        text = text.replace(old, "PARTUUID=" + uuid)
    path.write_text(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("profile")
    p.add_argument("model", choices=MODELS)
    p.add_argument("--machine")
    p.add_argument("--out")
    p = sub.add_parser("mbr")
    p.add_argument("image")
    p.add_argument("--boot-mib", type=int, required=True)
    p.add_argument("--root-mib", type=int, required=True)
    p.add_argument("--data-mib", type=int, required=True)
    p = sub.add_parser("fstab")
    p.add_argument("root")
    p.add_argument("boot_uuid")
    p.add_argument("root_uuid")
    p.add_argument("data_uuid")
    args = parser.parse_args()
    try:
        if args.command == "profile":
            if args.machine:
                validate_machine(args.model, args.machine)
            text = json.dumps(profile(args.model), indent=2) + "\n"
            if args.out:
                Path(args.out).write_text(text)
            else:
                print(text, end="")
        elif args.command == "mbr":
            print(json.dumps(write_mbr(args.image, args.boot_mib, args.root_mib, args.data_mib)))
        else:
            update_fstab(args.root, args.boot_uuid, args.root_uuid, args.data_uuid)
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
