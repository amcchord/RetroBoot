#!/usr/bin/env python3
"""Prepare the dedicated Debian QEMU10 OS X tree without resetting sources."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request
from prepare_sources import clone

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "emulator/manifests/osx.lock.json"

# One reviewed upstream correctness fix; this is not a target/ allowlist.
PPC_FEX_FIX = {
    "path": "target/ppc/fpu_helper.c",
    "patch_path": "emulator/osx/ppc-fp-fex.patch",
    "patch_sha256": "67043accb2bb3d488c2fa4f32ab8689a256b28b8aca75478d19a0be94662eedf",
    "before_sha256": "5364f28390699607f81a7ca1ccb796f4839a0729e92b767ec73244f4b3574326",
    "after_sha256": "6edce85bc843c8107e834102ecb160bc1ee88c3bffc2026c063e9bbdf71eba2e",
}


# Exact MPC7400 cache-flush completion exception, not a CPU optimization.
PPC_7400_DL1HWF_FIX = {'patch_path': 'emulator/osx/ppc-7400-dl1hwf.patch', 'patch_sha256': 'a751a50f4e40f9f744cb510356411bf81e238d5b94e49d2670b7628901392ae3', 'files': {'target/ppc/cpu_init.c': {'before': 'c6767b9494c48b29b8aa1f466c37ad2b976dfb3688b0815d5145960403ff516d', 'after': 'fe4d0830cec6883a4d59dc4b9986d51092924f19a01ef96dcf8a05a61f50b021'}, 'target/ppc/spr_common.h': {'before': '238d8400d7fc1024bc80e8ffad342b8cab00c7094b494f34119ad2b36b36e41f', 'after': '4152204793ace891852cecd0e2f84b4ce2b29f81c1e4f6af566c460cbabae712'}, 'target/ppc/translate.c': {'before': 'a26645144f097651ac5eb54f3dee960abdc2e54e024fb433805933b0b60a3068', 'after': '1a01b84e4ae3de1570edc53a16c31f044ecf84376a2ea3ca8f130ef2dce3bcc8'}}}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*args):
    subprocess.run(args, check=True)


def changes(tree):
    result = subprocess.check_output(["git", "-C", str(tree), "diff", "--name-only"], text=True).splitlines()
    result += subprocess.check_output(["git", "-C", str(tree), "ls-files", "--others", "--exclude-standard"], text=True).splitlines()
    return sorted(set(result))


def validate_scope(tree, names, lock):
    fix = lock.get("ppc_fex_fix")
    allowed = {PPC_FEX_FIX["path"]} if fix == PPC_FEX_FIX else set()
    cache_fix = lock.get("ppc_7400_dl1hwf_fix")
    if cache_fix == PPC_7400_DL1HWF_FIX:
        allowed.update(cache_fix["files"])
    forbidden = [name for name in names
                 if name.startswith(("target/", "tcg/", "accel/", "fpu/", "hw/ide/"))
                 and name not in allowed]
    if names != sorted(lock["port"]["files"]) or forbidden:
        raise SystemExit("OS X patch scope differs from the reviewed inventory")
    if fix is not None:
        if fix != PPC_FEX_FIX or fix["path"] not in names:
            raise SystemExit("OS X FP semantic fix differs from its exact reviewed selection")
        original = subprocess.check_output([
            "git", "-C", str(tree), "show", "HEAD:" + fix["path"]])
        if (hashlib.sha256(original).hexdigest() != fix["before_sha256"] or
                sha(tree / fix["path"]) != fix["after_sha256"] or
                sha(ROOT / fix["patch_path"]) != fix["patch_sha256"]):
            raise SystemExit("OS X FP semantic fix differs from its exact reviewed source/hunk")


    if cache_fix is not None:
        if cache_fix != PPC_7400_DL1HWF_FIX:
            raise SystemExit("OS X MPC7400 completion fix differs from exact reviewed selection")
        if sha(ROOT / cache_fix["patch_path"]) != cache_fix["patch_sha256"]:
            raise SystemExit("OS X MPC7400 completion hunk differs from review")
        for name, hashes in cache_fix["files"].items():
            if name not in names:
                raise SystemExit("OS X MPC7400 completion file missing")
            original = subprocess.check_output(["git", "-C", str(tree), "show", "HEAD:" + name])
            if (hashlib.sha256(original).hexdigest() != hashes["before"] or
                    sha(tree / name) != hashes["after"]):
                raise SystemExit("OS X MPC7400 completion source differs from exact review")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "build/osx-source-cache")
    args = parser.parse_args()
    work, cache = args.work_dir.resolve(), args.cache_dir.resolve()
    if not work.is_relative_to(ROOT / "build") or not cache.is_relative_to(ROOT):
        raise SystemExit("Use task-owned source/build/cache directories under this checkout")
    work.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    lock = json.loads(LOCK.read_text())
    patch = ROOT / lock["port"]["path"]
    if sha(patch) != lock["port"]["sha256"]:
        raise SystemExit("OS X port patch differs from its lock")
    identity = sha(LOCK)
    tree, stamp = work / "qemu", work / "prepared.json"
    if stamp.exists():
        state = json.loads(stamp.read_text())
        if state["identity"] != identity or changes(tree) != sorted(state["files"]):
            raise SystemExit("Prepared OS X source selection changed; use a fresh build directory")
        if any(not (tree / name).is_file() or sha(tree / name) != digest for name, digest in state["files"].items()):
            raise SystemExit("Prepared OS X source files changed; no reset is performed")
        print(tree)
        return
    if tree.exists():
        raise SystemExit("Refusing to patch an existing OS X tree without its preparation stamp")
    for archive in lock["qemu"]["archives"]:
        target = cache / archive["file"]
        if not target.exists():
            with urllib.request.urlopen(archive["url"]) as response:
                target.write_bytes(response.read())
        if sha(target) != archive["sha256"] or target.stat().st_size != archive["bytes"]:
            raise SystemExit(f"Source archive checksum/size mismatch: {target.name}")
    dsc = cache / lock["qemu"]["archives"][0]["file"]
    run("dpkg-source", "-x", str(dsc), str(tree))
    if (tree / "VERSION").read_text().strip() != lock["qemu"]["version"]:
        raise SystemExit("Debian source has an unexpected upstream version")
    run("git", "-C", str(tree), "init", "-q")
    run("git", "-C", str(tree), "add", ".")
    run("git", "-C", str(tree), "-c", "user.name=RetroBoot", "-c", "user.email=retroboot@invalid", "commit", "-qm", "Pinned Debian QEMU source")
    run("git", "-C", str(tree), "apply", "--check", str(patch))
    run("git", "-C", str(tree), "apply", str(patch))
    names = changes(tree)
    validate_scope(tree, names, lock)
    clone(lock["classicmac"], work / "classicmac")
    for asset in lock["assets"]:
        path = work / "classicmac" / asset["path"]
        if sha(path) != asset["sha256"] or path.stat().st_size != asset["bytes"]:
            raise SystemExit(f"Pinned private asset differs: {asset['path']}")
    state = {"identity": identity, "flavor": lock["flavor"], "qemu": lock["qemu"],
             "files": {name: sha(tree / name) for name in names}}
    stamp.write_text(json.dumps(state, indent=2) + "\n")
    print(tree)


if __name__ == "__main__":
    main()
