#!/usr/bin/env python3
"""Install and check the two published Quadra archives on a Zero 2 W card."""
import argparse
import asyncio
import json
from pathlib import Path

from retroboot.catalog import CatalogClient, ExtractLimits, install_machine, validate_catalog
from retroboot.catalogs import INTERNET, RETROBOOT_CATALOG
from retroboot.machines import QUADRA800, machine_slug, validate_meta
from retroboot.quadra import check_rom
from retroboot.util import atomic_write_json


CATALOG = Path(__file__).resolve().parent.parent / "config/zero2w-machines.json"
MARKER = "state/factory-machines"
SEED = "config/install-seed.json"


def entries():
    machines = validate_catalog(json.loads(CATALOG.read_text()))
    if ([m["osVersion"] for m in machines] != ["7.5.5", "8.1"]
            or any(m["machineFamily"] != QUADRA800 for m in machines)):
        raise ValueError("The factory catalog must list System 7.5.5 and Mac OS 8.1 in startup order.")
    return machines


def source_for(entry):
    return {"type": "catalog", "template_id": entry["id"], "archive_sha256": entry["sha256"],
            "archive_format": "retroboot-thin-1", "catalog_url": RETROBOOT_CATALOG, "catalog": INTERNET}


async def install(data, cache):
    client = CatalogClient(RETROBOOT_CATALOG, cache)
    machines = data / "machines"
    if any(machines.iterdir()):
        raise ValueError("The factory Macs require an empty machine library.")
    ids = []
    for entry in entries():
        archive = await client.download(entry, lambda *_: None)
        mid = await install_machine(
            archive, None, entry["name"], entry["osVersion"], source_for(entry),
            ExtractLimits(entry["installedBytes"], entry["requiredStorageBytes"]),
            lambda *_: None, machines_dir=machines, expected_family=QUADRA800)
        ids.append(mid)
    atomic_write_json(data / SEED, {"settings": {"machine.active": ids[0]}})
    (data / MARKER).write_text("".join(f"{mid}\n" for mid in ids))
    verify(data)


def verify(data):
    machines = data / "machines"
    catalog = entries()
    ids = [machine_slug(entry["name"]) for entry in catalog]
    if (set(ids) != {path.name for path in machines.iterdir()}
            or (data / MARKER).read_text().splitlines() != ids
            or json.loads((data / SEED).read_text())["settings"]["machine.active"] != ids[0]):
        raise ValueError("Factory library or startup Mac does not match the pinned catalog.")
    for mid, entry in zip(ids, catalog):
        path = machines / mid
        meta = json.loads((path / "machine.json").read_text())
        validate_meta(meta, mid)
        if (meta["os_version"] != entry["osVersion"] or meta["family"] != QUADRA800
                or meta["source"] != source_for(entry)
                or (path / meta["disk"]).stat().st_size != entry["diskCapacityBytes"]):
            raise ValueError(f"Invalid factory Mac: {mid}.")
        check_rom(path / meta["rom"])
    print("Factory Macs: " + ", ".join(ids) + f". Startup Mac: {ids[0]}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("--cache", type=Path, help="install verified archives using this cache, then check")
    args = parser.parse_args()
    if args.cache is None:
        verify(args.data)
    else:
        asyncio.run(install(args.data, args.cache))


if __name__ == "__main__":
    main()
