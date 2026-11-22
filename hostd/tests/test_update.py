"""Software Update (retroboot/update, ARCHITECTURE 13): environment, bundles, slot writer, manager, API.

The slot writer runs against FakeOps (directories stand in for partitions, the root
payload is a tar instead of squashfs); the real block-device path is exercised on
the lab VM (image/reports/update-*.md)."""
import asyncio
import base64
import gzip
import io
import json
import os
import shutil
import tarfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import helpers
from retroboot import schema, settings as settings_mod
from retroboot.hostd import Hostd
from retroboot.jobs import JobFailed
from retroboot.update import bundle as B, slotenv, tree
from retroboot.update.writer import SlotWriter, WriteError, carry_state, set_fstab_root
from test_lifecycle import Base

LEGACY_CMD = ("quiet loglevel=0 console=ttyS0,115200 video=efifb:off fbcon=map:9 "
              "drm_kms_helper.fbdev_emulation=0 sysctl.kernel.sysrq=0 fsck.repair=yes")
LEGACY_GRUB = f'''set timeout=0
echo -n " "
set rb_cmdline="{LEGACY_CMD}"
menuentry RetroBoot --id retroboot {{
	linux ($rb_disk,gpt3)/boot/vmlinuz root=PARTLABEL=RBROOT_A ro $rb_cmdline retroboot.pixel_scale=$rb_pixel_scale
	initrd ($rb_disk,gpt3)/boot/initrd.img
}}
'''
NEW_GRUB = (Path(__file__).resolve().parents[2] / "image/grub/grub.cfg.in").read_text().replace("@CMDLINE@", LEGACY_CMD)
NEW_CMD = LEGACY_CMD + " panic=0"


def keypair():
    k = Ed25519PrivateKey.generate()
    pem = k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return k, pem, B.key_id(B.raw_public(k.public_key()))


def make_root(path: Path, version="0.3.0", build_id="0.3.0-20261012T010203Z-abcdef12", flavor="debug",
              keyfile_path=None, hostd_update=True):
    """A miniature release root with the files the writer and manager read."""
    (path / "usr/lib/retroboot/boot/esp/grub/fonts").mkdir(parents=True)
    (path / "usr/lib/retroboot/boot/esp/EFI/BOOT").mkdir(parents=True)
    (path / "etc").mkdir(parents=True, exist_ok=True)
    (path / "boot").mkdir(exist_ok=True)
    (path / "boot/vmlinuz").write_bytes(b"kernel " + version.encode())
    (path / "etc/hostname").write_text("retroboot\n")
    (path / "etc/machine-id").write_text("")
    (path / "etc/fstab").write_text("PARTLABEL=RBROOT_A / ext4 rw 0 1\nPARTLABEL=RBDATA /var/lib/retroboot xfs nofail 0 0\n")
    (path / "usr/lib/retroboot/release.json").write_text(json.dumps(
        {"product": "RetroBoot", "version": version, "flavor": flavor, "build_id": build_id, "settings_schema": 1}))
    esp = path / "usr/lib/retroboot/boot/esp"
    (esp / "grub/grub.cfg").write_text(NEW_GRUB)
    (esp / "grub/fonts/blank.pf2").write_bytes(b"font")
    (esp / "EFI/BOOT/BOOTX64.EFI").write_bytes(b"grub efi " + version.encode())
    import hashlib
    files = {f"esp/{p.relative_to(esp)}": hashlib.sha256(p.read_bytes()).hexdigest()
             for p in esp.rglob("*") if p.is_file()}
    (path / "usr/lib/retroboot/boot/boot.json").write_text(json.dumps({"kernel_cmdline": NEW_CMD,
                                                                        "files_sha256": files}))
    if hostd_update:
        (path / "usr/lib/retroboot/hostd/retroboot/update").mkdir(parents=True)
    if keyfile_path:
        (path / "etc/NetworkManager/conf.d").mkdir(parents=True)
        (path / "etc/NetworkManager/conf.d/50-retroboot-keyfile.conf").write_text(f"[keyfile]\npath={keyfile_path}\n")
    return path


def make_bundle(out: Path, root: Path, key, kid, *, version="0.3.0", build_id="0.3.0-20261012T010203Z-abcdef12",
                flavor="debug", min_version="0.2.0", tamper=None, order=None) -> Path:
    files = gzip.compress(json.dumps(tree.build(str(root))).encode())
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        tf.add(str(root), arcname=".")
    payload = buf.getvalue()
    import hashlib
    manifest = {"format": B.FORMAT, "product": "RetroBoot", "version": version, "build_id": build_id,
                "flavor": flavor, "min_version": min_version, "settings_schema": 1, "settings_schema_min": 1,
                "kernel": {"version": "6.12", "cmdline": NEW_CMD},
                "root": {"member": "root.sqfs", "format": "squashfs", "compression": "tar", "bytes": len(payload),
                         "sha256": hashlib.sha256(payload).hexdigest(), "installed_bytes": 4096, "files": 10},
                "files": {"member": "root-files.json.gz", "bytes": len(files), "sha256": hashlib.sha256(files).hexdigest()},
                "notes": "Test update."}
    mbytes = json.dumps(manifest).encode()
    sig = B.signature_document(kid, key.sign(mbytes))
    if tamper == "manifest":
        mbytes = mbytes.replace(b"Test update.", b"Evil update.")
    if tamper == "payload":
        payload = payload[:-1] + bytes([payload[-1] ^ 1])
    members = {"manifest.json": mbytes, "manifest.sig": sig, "root-files.json.gz": files, "root.sqfs": payload}
    with tarfile.open(out, "w", format=tarfile.USTAR_FORMAT) as tf:
        for n in order or B.MEMBERS:
            ti = tarfile.TarInfo(n)
            ti.size = len(members[n])
            tf.addfile(ti, io.BytesIO(members[n]))
    return out


class FakeOps:
    """Directories stand in for partitions; the root payload is a tar."""

    def __init__(self, corrupt: bool = False):
        self.corrupt = corrupt
        self.calls = []

    def mkfs(self, dev, label):
        self.calls.append(("mkfs", dev, label))
        shutil.rmtree(dev, ignore_errors=True)
        os.makedirs(dev)

    def mount(self, dev, path, ro=False):
        self.calls.append(("mount", dev, path))
        if os.path.islink(path):
            os.unlink(path)
        elif os.path.isdir(path):
            os.rmdir(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(dev, path)

    def umount(self, path):
        self.calls.append(("umount", path))
        if os.path.islink(path):
            os.unlink(path)
        else:
            shutil.rmtree(path)

    def mount_squashfs(self, file, offset, size, path):
        with open(file, "rb") as f:
            f.seek(offset)
            data = f.read(size)
        os.makedirs(path, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            tf.extractall(path, filter="tar")

    def copy_tree(self, src, dst, progress):
        shutil.copytree(src, dst + "/", symlinks=True, dirs_exist_ok=True)
        if self.corrupt:
            Path(dst, "boot/vmlinuz").write_bytes(b"bit rot")
        progress(1.0)

    def verify_tree(self, list_file, root, progress):
        return tree.verify(tree.load(list_file), root, progress)

    def sync(self):
        pass

    def size_bytes(self, dev):
        return 3 * 1024 ** 3


class SlotEnvTests(unittest.TestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.esp = self.root / "esp"

    def test_env_roundtrip_and_torn_write(self):
        v = slotenv.write_env(self.esp, {"rb_active": "b", "rb_next": "a", "rb_tries": "1", "rb_other_ok": "1"})
        data = (self.esp / slotenv.ENV_NAME).read_bytes()
        self.assertEqual(len(data), 1024)
        self.assertEqual(slotenv.read_env(self.esp), (v, "primary"))
        # Torn write: first sector new, second sector junk -> still a valid block.
        (self.esp / slotenv.ENV_NAME).write_bytes(data[:512] + b"\x00" * 512)
        self.assertEqual(slotenv.read_env(self.esp)[0]["rb_next"], "a")
        # Damaged primary -> the copy.
        (self.esp / slotenv.ENV_NAME).write_bytes(b"garbage")
        self.assertEqual(slotenv.read_env(self.esp)[1], "backup")
        with self.assertRaises(slotenv.SlotError):
            slotenv.format_env({"rb_next": "a\nrb_active=b"})
        self.assertEqual(slotenv.normalize({"rb_active": "x", "rb_next": "a", "rb_tries": "7"}),
                         {"rb_active": "a", "rb_next": "", "rb_tries": "0", "rb_other_ok": "0"})

    def test_boot_states(self):
        env = {"rb_active": "a", "rb_next": "b", "rb_tries": "0", "rb_other_ok": "0"}
        st = slotenv.boot_state
        self.assertEqual(st(env, {"retroboot.trial": "1", "retroboot.slot": "b"}, "b", True)["kind"], "trial")
        self.assertEqual(st(env, {"retroboot.slot": "a"}, "a", True)["kind"], "fell-back")
        self.assertEqual(st(dict(env, rb_tries="1"), {}, "a", True)["kind"], "not-tried")
        self.assertEqual(st(dict(env, rb_next=""), {}, "a", True)["kind"], "normal")
        self.assertEqual(st(dict(env, rb_next=""), {"retroboot.fallback": "1"}, "b", True)["kind"], "other")
        self.assertTrue(st(env, {}, "a", False)["legacy_grub"])

    def test_slot_cfg_and_legacy_grub(self):
        (self.esp / "grub").mkdir(parents=True)
        (self.esp / "grub/grub.cfg").write_text(LEGACY_GRUB)
        self.assertFalse(slotenv.grub_cfg_is_slotted(self.esp))
        self.assertEqual(slotenv.legacy_cmdline(self.esp), LEGACY_CMD)
        slotenv.write_slot_cfg(self.esp, "b", NEW_CMD, "0.3.0", "0.3.0-20261012T010203Z-abcdef12")
        self.assertEqual(slotenv.read_slot_cfg(self.esp, "b")["cmdline"], NEW_CMD)
        for bad in ('quiet"; linux /evil', "a $x", "root=/dev/sda1 quiet", "x\nset y=1"):
            with self.assertRaises(slotenv.SlotError, msg=bad):
                slotenv.write_slot_cfg(self.esp, "a", bad)
        self.assertIn(slotenv.GRUB_CFG_MARKER, NEW_GRUB)

    def test_layout_from_sysfs(self):
        sysfs = self.root / "sys"
        disk = sysfs / "block/sda"
        for num, name in ((1, "RBEFI"), (2, "BIOSBOOT"), (3, "RBROOT_A"), (4, "RBROOT_B"), (5, "RBDATA")):
            p = disk / f"sda{num}"
            p.mkdir(parents=True)
            (p / "partition").write_text(f"{num}\n")
            (p / "uevent").write_text(f"MAJOR=8\nMINOR={num}\nDEVNAME=sda{num}\nPARTN={num}\nPARTNAME={name}\n")
        (sysfs / "dev/block").mkdir(parents=True)
        os.symlink(disk / "sda4", sysfs / "dev/block/8:4")
        lay = slotenv.layout(str(sysfs), (8, 4))
        self.assertEqual((lay["current"], lay["a"], lay["b"], lay["esp"], lay["disk"]),
                         ("b", "/dev/sda3", "/dev/sda4", "/dev/sda1", "/dev/sda"))
        (disk / "sda4/uevent").write_text("PARTNAME=Other\n")
        with self.assertRaises(slotenv.SlotError):
            slotenv.layout(str(sysfs), (8, 4))


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.key, self.pem, self.kid = keypair()
        self.keys = self.root / "keys"
        self.keys.mkdir()
        (self.keys / "dev.pub").write_bytes(self.pem)
        self.newroot = make_root(self.root / "new")

    def bundle(self, name="u.rbu", **kw):
        return make_bundle(self.root / name, self.newroot, self.key, self.kid, **kw)

    def test_good_bundle_verifies(self):
        b = B.Bundle(self.bundle())
        m = b.verify(B.trusted_keys(self.keys))
        self.assertEqual((m["version"], b.key_id), ("0.3.0", self.kid))
        self.assertTrue(any(e[0] == "boot/vmlinuz" for e in b.file_list()))

    def test_rejections(self):
        keys = B.trusted_keys(self.keys)
        with self.assertRaises(B.BundleError) as e:
            B.Bundle(self.bundle("m.rbu", tamper="manifest")).verify(keys)
        self.assertEqual(e.exception.code, "bad-signature")
        with self.assertRaises(B.BundleError) as e:
            B.Bundle(self.bundle("p.rbu", tamper="payload")).verify(keys)
        self.assertEqual(e.exception.code, "payload-mismatch")
        other, _, okid = keypair()
        with self.assertRaises(B.BundleError) as e:
            B.Bundle(make_bundle(self.root / "o.rbu", self.newroot, other, okid)).verify(keys)
        self.assertEqual(e.exception.code, "untrusted-key")
        with self.assertRaises(B.BundleError) as e:
            B.Bundle(self.bundle("r.rbu", order=("manifest.sig", "manifest.json", "root-files.json.gz", "root.sqfs")))
        self.assertEqual(e.exception.code, "not-a-bundle")
        (self.root / "junk.rbu").write_bytes(b"\x00" * 4096)
        with self.assertRaises(B.BundleError):
            B.Bundle(self.root / "junk.rbu")
        (self.keys / "revoked").write_text(f"{self.kid}  # rotated\n")
        self.assertEqual(B.trusted_keys(self.keys), {})

    def test_compatibility(self):
        m = B.Bundle(self.bundle()).verify(B.trusted_keys(self.keys))
        legacy = {"version": "0.2.0", "flavor": "debug", "build_id": "0.2.0-20261011T153043Z-593ad875"}
        B.compatibility(m, legacy, 1)
        for inst, code in (({**legacy, "flavor": "product"}, "wrong-flavor"),
                           ({**legacy, "version": "0.1.0"}, "too-old"),
                           ({**legacy, "version": "0.3.0", "build_id": m["build_id"]}, "same-version"),
                           ({**legacy, "version": "0.4.0", "build_id": "0.4.0-20261101T000000Z-1"}, "older")):
            with self.assertRaises(B.BundleError) as e:
                B.compatibility(m, inst, 1)
            self.assertEqual(e.exception.code, code)
        B.compatibility(m, {**legacy, "version": "0.4.0"}, 1, allow_older=True)
        self.assertEqual(B.describe_version("0.3.0", m["build_id"]), "0.3.0 (build 2026-10-12 01:02)")


class TreeTests(unittest.TestCase):
    def test_verify_detects_changes(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        r = make_root(root / "r")
        os.symlink("vmlinuz", r / "boot/vmlinuz.link")
        entries = tree.build(str(r))["entries"]
        self.assertEqual(tree.verify(entries, str(r)), [])
        (r / "lost+found").mkdir()
        self.assertEqual(tree.verify(entries, str(r)), [])
        (r / "boot/vmlinuz").write_bytes(b"changed")
        (r / "extra").write_text("x")
        os.unlink(r / "etc/hostname")
        probs = tree.verify(entries, str(r))
        self.assertIn("differs boot/vmlinuz", probs)
        self.assertIn("unexpected extra", probs)
        self.assertIn("missing etc/hostname", probs)


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.key, self.pem, self.kid = keypair()
        (self.root / "keys").mkdir()
        (self.root / "keys/dev.pub").write_bytes(self.pem)
        self.esp = self.root / "esp"
        (self.esp / "grub/retroboot").mkdir(parents=True)
        (self.esp / "grub/grub.cfg").write_text(LEGACY_GRUB)
        (self.esp / "grub/retroboot/display.cfg").write_text("set rb_scale=1\n")
        # The running (legacy 0.2.0) system with this computer's identity.
        self.running = make_root(self.root / "running", "0.2.0", "0.2.0-20261011T153043Z-593ad875", hostd_update=False)
        (self.running / "etc/hostname").write_text("mini\n")
        (self.running / "etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n")
        (self.running / "etc/ssh").mkdir()
        (self.running / "etc/ssh/ssh_host_ed25519_key").write_text("PRIVATE")
        (self.running / "etc/NetworkManager/system-connections").mkdir(parents=True)
        (self.running / "etc/NetworkManager/system-connections/Home.nmconnection").write_text("[wifi]\nssid=Home\n")
        self.data = self.root / "data"
        self.data.mkdir()
        self.slots = {s: self.root / "slots" / s for s in "ab"}
        for d in self.slots.values():
            d.mkdir(parents=True)
        self.lay = {"current": "a", "a": str(self.slots["a"]), "b": str(self.slots["b"]), "esp": "x", "data": "y"}
        self.new = make_root(self.root / "new", keyfile_path="/var/lib/retroboot/network/connections")

    def writer(self, ops=None):
        return SlotWriter(self.esp, self.lay, self.root / "work", ops=ops or FakeOps(), running_root=self.running,
                          data_dir=self.data)

    def verified(self):
        b = B.Bundle(make_bundle(self.root / "u.rbu", self.new, self.key, self.kid))
        b.verify(B.trusted_keys(self.root / "keys"))
        return b

    def test_legacy_device_first_update(self):
        steps = []
        res = self.writer().write(self.verified(), lambda f, m: steps.append(m))
        self.assertEqual((res["slot"], res["converted_grub"]), ("b", True))
        slot = self.slots["b"]
        self.assertEqual((slot / "etc/hostname").read_text(), "mini\n")
        self.assertEqual((slot / "etc/machine-id").read_text(), "0123456789abcdef0123456789abcdef\n")
        self.assertEqual((slot / "etc/ssh/ssh_host_ed25519_key").read_text(), "PRIVATE")
        self.assertTrue((slot / "etc/fstab").read_text().startswith("PARTLABEL=RBROOT_B / ext4"))
        # Wi-Fi profiles move to the shared RBDATA directory the new release reads.
        self.assertTrue((self.data / "network/connections/Home.nmconnection").is_file())
        # ESP: slot a keeps its exact kernel arguments, slot b gets the new ones, a not armed.
        self.assertEqual(slotenv.read_slot_cfg(self.esp, "a")["cmdline"], LEGACY_CMD)
        self.assertEqual(slotenv.read_slot_cfg(self.esp, "b")["cmdline"], NEW_CMD)
        self.assertTrue(slotenv.grub_cfg_is_slotted(self.esp))
        self.assertEqual((self.esp / "grub/grub.cfg.legacy").read_text(), LEGACY_GRUB)
        self.assertEqual((self.esp / "grub/retroboot/display.cfg").read_text(), "set rb_scale=1\n")
        env, _ = slotenv.read_env(self.esp)
        self.assertEqual(env, {"rb_active": "a", "rb_next": "", "rb_tries": "0", "rb_other_ok": "0"})
        self.assertIn("RetroBoot 0.3.0 is ready to install.", steps)

    def test_installer_writes_its_own_root(self):
        # The installer's "Update RetroBoot on this disk": a directory source, no bundle.
        entries = tree.build(str(self.new))["entries"]
        rel = json.loads((self.new / "usr/lib/retroboot/release.json").read_text())
        res = self.writer().write_tree(self.new, entries, rel, NEW_CMD, 4096, lambda f, m: None)
        self.assertEqual((res["slot"], res["converted_grub"], res["version"]), ("b", True, "0.3.0"))
        self.assertEqual((self.slots["b"] / "etc/hostname").read_text(), "mini\n")
        self.assertEqual(slotenv.read_slot_cfg(self.esp, "a")["cmdline"], LEGACY_CMD)

    def test_corrupted_copy_is_not_staged(self):
        slotenv.write_env(self.esp, {"rb_active": "a", "rb_other_ok": "1"})
        (self.esp / "grub/grub.cfg").write_text(NEW_GRUB)
        with self.assertRaises(WriteError) as e:
            self.writer(FakeOps(corrupt=True)).write(self.verified(), lambda f, m: None)
        self.assertEqual(e.exception.code, "verify-failed")
        env, _ = slotenv.read_env(self.esp)
        self.assertEqual(env["rb_other_ok"], "0")        # the old previous version is gone; never offered
        self.assertIsNone(slotenv.read_slot_cfg(self.esp, "b"))

    def test_rollback_copies_wifi_back_to_a_legacy_slot(self):
        (self.data / "network/connections").mkdir(parents=True)
        (self.data / "network/connections/Cafe.nmconnection").write_text("[wifi]\nssid=Cafe\n")
        new_running = make_root(self.root / "running2", keyfile_path="/var/lib/retroboot/network/connections")
        legacy = self.root / "legacy"
        legacy.mkdir()
        carried = carry_state(new_running, legacy, self.data)
        self.assertIn("wifi:Cafe.nmconnection", carried)
        self.assertTrue((legacy / "etc/NetworkManager/system-connections/Cafe.nmconnection").is_file())

    def test_shipped_authorized_keys_survive_the_carry(self):
        src = self.root / "src-ssh"
        (src / "root/.ssh").mkdir(parents=True)
        (src / "root/.ssh/known_hosts").write_text("host key\n")
        (src / "root/.ssh/authorized_keys").write_text("ssh-ed25519 AAAAdevice owner\n")
        dst = self.root / "dst-ssh"
        (dst / "root/.ssh").mkdir(parents=True)
        (dst / "root/.ssh/authorized_keys").write_text("ssh-ed25519 AAAAlab retroboot-lab\n")
        carry_state(src, dst, self.data)
        keys = (dst / "root/.ssh/authorized_keys").read_text().splitlines()
        self.assertEqual(keys, ["ssh-ed25519 AAAAdevice owner", "ssh-ed25519 AAAAlab retroboot-lab"])
        self.assertEqual((dst / "root/.ssh/known_hosts").read_text(), "host key\n")
        # A product slot's /root/.ssh without keys: the debug image's key stays.
        (src / "root/.ssh/authorized_keys").unlink()
        dst2 = self.root / "dst2-ssh"
        (dst2 / "root/.ssh").mkdir(parents=True)
        (dst2 / "root/.ssh/authorized_keys").write_text("ssh-ed25519 AAAAlab retroboot-lab\n")
        carry_state(src, dst2, self.data)
        self.assertEqual((dst2 / "root/.ssh/authorized_keys").read_text(), "ssh-ed25519 AAAAlab retroboot-lab\n")

    def test_hostd_json_carried_only_when_the_target_has_none(self):
        src = self.root / "src-root"
        (src / "etc/retroboot").mkdir(parents=True)
        (src / "etc/retroboot/hostd.json").write_text('{"lab_qmp": true}\n')
        fresh = self.root / "fresh"
        fresh.mkdir()
        self.assertIn("etc/retroboot/hostd.json", carry_state(src, fresh, self.data))
        self.assertEqual((fresh / "etc/retroboot/hostd.json").read_text(), '{"lab_qmp": true}\n')
        shipped = self.root / "shipped"
        (shipped / "etc/retroboot").mkdir(parents=True)
        (shipped / "etc/retroboot/hostd.json").write_text('{"form_factor": "portable"}\n')
        self.assertNotIn("etc/retroboot/hostd.json", carry_state(src, shipped, self.data))
        self.assertEqual((shipped / "etc/retroboot/hostd.json").read_text(), '{"form_factor": "portable"}\n')

    def test_fstab_rewrite(self):
        r = self.root / "f"
        (r / "etc").mkdir(parents=True)
        (r / "etc/fstab").write_text("# c\nPARTLABEL=RBROOT_A  /  ext4  rw 0 1\nPARTLABEL=RBEFI /boot/efi vfat x 0 0\n")
        set_fstab_root(r, "RBROOT_B")
        self.assertEqual((r / "etc/fstab").read_text().splitlines()[1], "PARTLABEL=RBROOT_B  /  ext4  rw 0 1")


class SettingsMigrationTests(unittest.TestCase):
    def test_up_down_and_hold(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        store = settings_mod.SettingsStore(root / "settings.json", root / "kg.json")
        store.load()
        old_ver, ups, downs = schema.SCHEMA_VERSION, dict(settings_mod.MIGRATIONS_UP), dict(settings_mod.MIGRATIONS_DOWN)
        self.addCleanup(lambda: (setattr(schema, "SCHEMA_VERSION", old_ver), settings_mod.MIGRATIONS_UP.clear(),
                                 settings_mod.MIGRATIONS_UP.update(ups), settings_mod.MIGRATIONS_DOWN.clear(),
                                 settings_mod.MIGRATIONS_DOWN.update(downs)))
        settings_mod.MIGRATIONS_UP[1] = lambda d: dict(d, upgraded=True)
        settings_mod.MIGRATIONS_DOWN[2] = lambda d: {k: v for k, v in d.items() if k != "upgraded"}
        schema.SCHEMA_VERSION = 2
        s2 = settings_mod.SettingsStore(root / "settings.json", root / "kg.json")
        s2.load()                                  # schema-1 file read by a schema-2 build
        self.assertEqual(s2.state["schema"], 2)
        s2.hold_schema = 1                         # trial boot: keep the old format on disk
        s2.save()
        self.assertEqual(json.loads((root / "settings.json").read_text())["schema"], 1)
        s2.hold_schema = None
        s2.save()
        self.assertEqual(json.loads((root / "settings.json").read_text())["schema"], 2)
        self.assertTrue(s2.write_schema_version(1))
        self.assertEqual(json.loads((root / "settings.json").read_text())["schema"], 1)
        settings_mod.MIGRATIONS_DOWN.clear()
        self.assertFalse(s2.write_schema_version(1))


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.key, self.pem, self.kid = keypair()
        (self.root / "keys").mkdir()
        (self.root / "keys/dev.pub").write_bytes(self.pem)
        self.esp = self.root / "boot/efi"
        (self.esp / "grub/retroboot").mkdir(parents=True)
        (self.esp / "grub/grub.cfg").write_text(LEGACY_GRUB)
        make_root(self.root, "0.2.0", "0.2.0-20261011T153043Z-593ad875")
        (self.root / "etc/hostname").write_text("mini\n")
        self.slots = {s: self.root / "slots" / s for s in "ab"}
        for d in self.slots.values():
            d.mkdir(parents=True)
        # Slot a holds the running legacy release (on a device it *is* the running root).
        make_root(self.slots["a"], "0.2.0", "0.2.0-20261011T153043Z-593ad875", hostd_update=False)
        self.cmdline = self.root / "cmdline"
        self.cmdline.write_text("quiet root=PARTLABEL=RBROOT_A\n")
        self.new = make_root(self.root / "newroot")
        self.bundle = make_bundle(self.root / "u.rbu", self.new, self.key, self.kid)

    async def make_app(self, current="a"):
        helpers.make_package(self.root)
        cfg = helpers.make_config(self.root)
        cfg.update_esp = str(self.esp)
        cfg.update_keys_dir = str(self.root / "keys")
        cfg.update_layout = {"current": current, "a": str(self.slots["a"]), "b": str(self.slots["b"]),
                             "esp": "x", "data": "y"}
        cfg.cmdline_path = str(self.cmdline)
        cfg.update_panel_health_s = 1.0
        app = Hostd(cfg)
        app.loop = asyncio.get_running_loop()
        app.settings.store.load()
        app.updates.ops = FakeOps()
        await app.updates.start()
        self.addAsyncCleanup(app.updates.stop)
        return app

    async def install(self, app):
        incoming = app.updates.new_incoming()
        shutil.copy(self.bundle, incoming)
        job = app.updates.install_job({"kind": "file", "path": str(incoming)})
        await job.done.wait()
        return job

    async def test_update_trial_commit_and_rollback(self):
        app = await self.make_app()
        self.assertEqual((app.updates.boot["kind"], app.updates.committed), ("normal", True))
        job = await self.install(app)
        self.assertEqual(job.state, "succeeded", job.error)
        self.assertFalse(list(app.updates.incoming.glob("*.rbu")))       # bundle removed after use
        self.assertEqual(app.updates.public()["staged"]["status"], "ready")
        job = app.updates.restart_job()
        await job.done.wait()
        self.assertEqual(job.state, "succeeded", job.error)
        env, _ = slotenv.read_env(self.esp)
        self.assertEqual((env["rb_next"], env["rb_tries"]), ("b", "1"))
        self.assertTrue(helpers.events(app, "host_reboot"))
        # GRUB: try b once (sets rb_tries=0), boot it with retroboot.trial=1.
        slotenv.update_env(self.esp, rb_tries="0")
        self.cmdline.write_text("quiet retroboot.slot=b retroboot.trial=1\n")
        shutil.copy(self.new / "usr/lib/retroboot/release.json", self.root / "usr/lib/retroboot/release.json")
        shutil.copytree(self.new / "usr/lib/retroboot/boot", self.root / "usr/lib/retroboot/boot", dirs_exist_ok=True)
        app2 = await self.make_app(current="b")
        self.assertEqual(app2.updates.boot["kind"], "trial")
        self.assertFalse(app2.updates.committed)
        sup = app2.supervisor
        sup.content_seen, sup.state, sup.session = True, "running", "s1"
        await helpers.wait_for(lambda: app2.updates.committed, 5, 0.1, "commit")
        env, _ = slotenv.read_env(self.esp)
        self.assertEqual(env, {"rb_active": "b", "rb_next": "", "rb_tries": "0", "rb_other_ok": "1"})
        self.assertTrue(slotenv.health_flag(str(app2.paths.run)).exists())
        self.assertEqual(app2.updates.state["last_result"]["result"], "installed")
        self.assertEqual((self.esp / "EFI/BOOT/BOOTX64.EFI").read_bytes(), b"grub efi 0.3.0")   # ESP after commit
        prev = app2.updates.public()["previous"]
        self.assertEqual(prev["version"], "0.2.0")
        sup.state = "stopped"          # no emulator in this test: nothing to shut down first
        job = app2.updates.rollback_job()
        await job.done.wait()
        self.assertEqual(job.state, "succeeded", job.error)
        env, _ = slotenv.read_env(self.esp)
        self.assertEqual((env["rb_active"], env["rb_other_ok"], env["rb_next"]), ("a", "1", ""))

    async def test_failed_trial_falls_back(self):
        app = await self.make_app()
        await self.install(app)
        job = app.updates.restart_job()
        await job.done.wait()
        # Power cut during the trial boot: GRUB spent the try, next boot is slot a again.
        slotenv.update_env(self.esp, rb_tries="0")
        self.cmdline.write_text("quiet retroboot.slot=a\n")
        app.updates.state["armed"]["boot_id"] = "an-earlier-boot"
        app.updates.save()
        app2 = await self.make_app()
        self.assertEqual(app2.updates.boot["kind"], "fell-back")
        env, _ = slotenv.read_env(self.esp)
        self.assertEqual(env["rb_next"], "")
        last = app2.updates.state["last_result"]
        self.assertEqual(last["result"], "failed")
        self.assertIn("went back to RetroBoot 0.2.0", last["message"])
        self.assertEqual(app2.updates.public()["staged"]["status"], "failed")
        self.assertTrue(any(n["id"] == "update-failed" for n in app2.notices()))
        self.assertTrue(app2.updates.committed)        # the old version may install again at once

    async def test_rejections_without_a_staged_update(self):
        app = await self.make_app()
        bad = make_bundle(self.root / "bad.rbu", self.new, *keypair()[::2])
        incoming = app.updates.new_incoming()
        shutil.copy(bad, incoming)
        job = app.updates.install_job({"kind": "file", "path": str(incoming)})
        await job.done.wait()
        self.assertEqual((job.state, job.error["code"]), ("failed", "untrusted-key"))
        self.assertIsNone(app.updates.staged())
        job = app.updates.restart_job()
        await job.done.wait()
        self.assertEqual(job.error["code"], "nothing-staged")
        job = app.updates.rollback_job()
        await job.done.wait()
        self.assertEqual(job.error["code"], "no-previous")

    async def test_feed_selection(self):
        app = await self.make_app()
        u = {"version": "0.3.0", "build_id": "0.3.0-20261012T010203Z-abcdef12", "flavor": "debug",
             "url": "https://updates.example/retroboot-update-0.3.0-debug.rbu", "sha256": "0" * 64, "bytes": 1000}
        feed = {"format": "retroboot-feed/1", "updates": [u, dict(u, flavor="product"),
                                                          dict(u, url="http://insecure/x.rbu", build_id="0.3.0-20261013T000000Z-1")]}
        self.assertEqual(app.updates.pick_from_feed(feed)["build_id"], u["build_id"])
        self.assertIsNone(app.updates.pick_from_feed({"format": "retroboot-feed/1", "updates": [dict(u, version="0.1.0")]}))


    async def test_feed_check_reads_fragmented_response_and_bounds_size(self):
        """An early network fragment is not the whole update list (as for catalogs); keep the bound."""
        import ssl
        from unittest.mock import patch
        from aiohttp import web
        from retroboot.update import manager as manager_mod
        from test_units import lab_ca_and_server_cert
        ca_pem, cert_pem, key_pem = lab_ca_and_server_cert("localhost")
        for name, data in (("ca.pem", ca_pem), ("srv.pem", cert_pem), ("srv.key", key_pem)):
            (self.root / name).write_bytes(data)
        app = await self.make_app()
        u = {"version": "9.0.0", "build_id": "9.0.0-20261017T010203Z-abcdef12", "flavor": app.updates.release["flavor"],
             "url": "https://updates.example/retroboot-update-9.0.0.rbu", "sha256": "0" * 64, "bytes": 1000,
             "notes": "x" * 2000}
        body = json.dumps({"format": "retroboot-feed/1", "updates": [u]}).encode()

        async def listing(request):
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            await response.write(body[:19])
            await asyncio.sleep(0.03)
            await response.write(body[19:])
            await response.write_eof()
            return response
        wa = web.Application()
        wa.router.add_get("/feed.json", listing)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.root / "srv.pem", self.root / "srv.key")
        runner = web.AppRunner(wa)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ctx)
        await site.start()
        app.cfg.update_ca_file = str(self.root / "ca.pem")
        app.settings.applied["update.url"] = f"https://localhost:{site._server.sockets[0].getsockname()[1]}/feed.json"
        job = app.updates.check_job()
        await job.done.wait()
        self.assertEqual((job.state, app.updates.state["check"]["found"]["build_id"]), ("succeeded", u["build_id"]))
        with patch.object(manager_mod, "MAX_FEED", len(body) - 1):
            job = app.updates.check_job()
            await job.done.wait()
        self.assertEqual((job.state, job.error["code"]), ("failed", "invalid-feed"))

    async def test_panel_checks_for_updates_when_opened(self):
        """Software Update on the panel checks the update server when it opens (not again within
        ten minutes) and offers Download and Install as its default button."""
        import ssl
        from aiohttp import web
        from retroboot.panel.update import SoftwareUpdate
        from test_units import lab_ca_and_server_cert
        ca_pem, cert_pem, key_pem = lab_ca_and_server_cert("localhost")
        for name, data in (("ca.pem", ca_pem), ("srv.pem", cert_pem), ("srv.key", key_pem)):
            (self.root / name).write_bytes(data)
        app = await self.make_app()
        u = {"version": "9.0.0", "build_id": "9.0.0-20261017T010203Z-abcdef12", "flavor": app.updates.release["flavor"],
             "url": "https://updates.example/retroboot-update-9.0.0.rbu", "sha256": "0" * 64, "bytes": 1000,
             "notes": "Nine."}
        hits = []

        async def listing(request):
            hits.append(1)
            return web.json_response({"format": "retroboot-feed/1", "updates": [u]})
        wa = web.Application()
        wa.router.add_get("/feed.json", listing)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.root / "srv.pem", self.root / "srv.key")
        runner = web.AppRunner(wa)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ctx)
        await site.start()
        app.cfg.update_ca_file = str(self.root / "ca.pem")
        app.settings.applied["update.url"] = f"https://localhost:{site._server.sockets[0].getsockname()[1]}/feed.json"
        scr = SoftwareUpdate(app.panel)
        self.assertIsNotNone(scr.check)
        self.assertIn("Checking for updates", json.dumps(scr.doc()))
        await scr.check.done.wait()
        doc = scr.doc()
        self.assertIn("RetroBoot 9.0.0 is available.", json.dumps(doc))
        main = doc["items"][-1]["items"][-1]
        self.assertEqual((main["id"], main.get("default")), ("download", True))
        again = SoftwareUpdate(app.panel)
        self.assertIsNone(again.check)                  # checked a moment ago
        self.assertEqual(len(hits), 1)
        # Once that version runs here, the stored check no longer offers it.
        app.updates.release = dict(app.updates.release, version="9.0.0", build_id=u["build_id"])
        self.assertIsNone(app.updates.public()["check"]["found"])
        self.assertNotIn("is available", json.dumps(SoftwareUpdate(app.panel).doc()))

    async def test_lab_http_auth_on_update_server(self):
        """hostd.json lab_http_auth_file (LAB ONLY): feed and bundle fetches under the lab base send Basic auth."""
        import base64
        import ssl
        from aiohttp import web
        from retroboot import labauth
        from test_units import lab_ca_and_server_cert
        ca_pem, cert_pem, key_pem = lab_ca_and_server_cert("localhost")
        for name, data in (("ca.pem", ca_pem), ("srv.pem", cert_pem), ("srv.key", key_pem)):
            (self.root / name).write_bytes(data)
        seen = []

        async def handler(request):
            seen.append((request.path, request.headers.get("Authorization")))
            if request.path.startswith("/lab/") and request.headers.get("Authorization") != \
                    "Basic " + base64.b64encode(b"u:p").decode():
                return web.Response(status=401)
            return web.Response(text='{"format": "retroboot-feed/1", "updates": []}')
        wa = web.Application()
        wa.router.add_get("/{tail:.*}", handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.root / "srv.pem", self.root / "srv.key")
        runner = web.AppRunner(wa)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ctx)
        await site.start()
        self.addAsyncCleanup(runner.cleanup)
        base = f"https://localhost:{site._server.sockets[0].getsockname()[1]}"
        app = await self.make_app()
        app.cfg.update_ca_file = str(self.root / "ca.pem")
        with self.assertRaises(JobFailed):                        # no auth file: 401
            await app.updates._get(f"{base}/lab/feed.json", 1000)
        app.lab_auth = labauth.LabAuth(f"{base}/lab/", "u", "p")
        session, resp = await app.updates._get(f"{base}/lab/feed.json", 1000)
        resp.release()
        await session.close()
        session, resp = await app.updates._get(f"{base}/elsewhere/feed.json", 1000)
        resp.release()
        await session.close()
        self.assertEqual([a is not None for _, a in seen], [False, True, False])
        # Signed in to RetroBoot downloads with no update.url: Software Update uses the lab's list.
        self.assertEqual(app.updates.public()["url"], f"{base}/lab/feed.json")
        job = app.updates.check_job()
        await job.done.wait()
        self.assertEqual((job.state, seen[-1][0], seen[-1][1] is not None), ("succeeded", "/lab/feed.json", True))
        app.settings.applied["update.url"] = f"{base}/elsewhere/feed.json"   # the setting wins
        self.assertEqual(app.updates.public()["url"], f"{base}/elsewhere/feed.json")
        job = app.updates.check_job()
        await job.done.wait()
        self.assertEqual((job.state, seen[-1][0], seen[-1][1]), ("succeeded", "/elsewhere/feed.json", None))
        app.settings.applied["update.url"] = ""
        app.lab_auth = None                                       # not signed in: no update server
        self.assertEqual(app.updates.public()["url"], "")
        job = app.updates.check_job()
        await job.done.wait()
        self.assertEqual(job.error["code"], "no-url")


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_endpoints(self):
        from aiohttp.test_utils import TestClient, TestServer
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        helpers.make_package(root)
        app = Hostd(helpers.make_config(root))
        app.loop = asyncio.get_running_loop()
        app.settings.store.load()
        app.web.tls()
        await app.updates.start()
        client = TestClient(TestServer(app.web.web_app, host="127.0.0.1"))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        code, _ = app.pairing.current_code()
        r = await client.post("/api/v1/pair", json={"code": code, "device_name": "t"})
        cookie = r.headers.getall("Set-Cookie")[0].split("rb_session=", 1)[1].split(";", 1)[0]
        csrf = (await r.json())["csrf"]
        hd = {"Cookie": f"rb_session={cookie}", "X-RetroBoot-CSRF": csrf}
        r = await client.get("/api/v1/update", headers=hd)
        body = await r.json()
        self.assertFalse(body["available"])               # a scratch root is not a slotted disk
        self.assertIn("reason", body)
        r = await client.post("/api/v1/update/bundle", data=b"x" * 20000, headers=hd)
        self.assertEqual(r.status, 409)
        r = await client.get("/api/v1/status", headers=hd)
        self.assertIn("update", await r.json())
        r = await client.post("/api/v1/update/restart", headers=hd)
        self.assertEqual(r.status, 409)
        settings = await (await client.get("/api/v1/settings", headers=hd)).json()
        url = next(s for s in settings["schema"] if s["key"] == "update.url")
        self.assertEqual((url["min_length"], url["max_length"], url["default"]), (0, 2048, ""))
        r = await client.patch("/api/v1/settings", json={"revision": settings["revision"],
                                                         "changes": {"update.url": "http://plain/feed.json"}},
                               headers=hd)
        self.assertEqual(r.status, 422)



class RestartToUpdateFinishedLate(Base):
    """Restart to Update with Mac OS running (fake_qemu): an application holds Mac OS's Shut Down
    past clean_stop_timeout (VM169, October 24 2026: Setup Assistant's "Are you sure you want to
    quit?"), so the job fails and the update stays staged.  When Mac OS finishes the Shut Down,
    Restart to Update goes on: the update is armed and the computer restarts.  It used to power
    off instead."""

    def prepare_root(self):
        self.key, self.pem, self.kid = keypair()
        (self.root / "keys").mkdir()
        (self.root / "keys/dev.pub").write_bytes(self.pem)
        self.esp = self.root / "boot/efi"
        (self.esp / "grub/retroboot").mkdir(parents=True)
        (self.esp / "grub/grub.cfg").write_text(LEGACY_GRUB)
        make_root(self.root, "0.2.0", "0.2.0-20261011T153043Z-593ad875")
        slots = {s: self.root / "slots" / s for s in "ab"}
        for d in slots.values():
            d.mkdir(parents=True)
        make_root(slots["a"], "0.2.0", "0.2.0-20261011T153043Z-593ad875", hostd_update=False)
        cmdline = self.root / "cmdline"
        cmdline.write_text("quiet root=PARTLABEL=RBROOT_A\n")
        self.bundle = make_bundle(self.root / "u.rbu", make_root(self.root / "newroot"), self.key, self.kid)
        self.cfg_overrides = {"clean_stop_timeout": 1.5, "update_esp": str(self.esp),
                              "update_keys_dir": str(self.root / "keys"), "cmdline_path": str(cmdline),
                              "update_panel_health_s": 1.0,
                              "update_layout": {"current": "a", "a": str(slots["a"]), "b": str(slots["b"]),
                                                "esp": "x", "data": "y"}}

    def extra_conf(self):
        return {"shutdown_delay_s": 4.0}

    def before_start(self):
        self.app.updates.ops = FakeOps()

    async def test_update_goes_on_when_mac_os_finishes(self):
        await self.until_event("finder_detected")
        updates = self.app.updates
        incoming = updates.new_incoming()
        shutil.copy(self.bundle, incoming)
        job = await self.job(updates.install_job({"kind": "file", "path": str(incoming)}))
        self.assertEqual(job.state, "succeeded", job.error)
        job = await self.job(updates.restart_job(origin="test"), 60)
        self.assertEqual(job.state, "failed", job.public())
        self.assertIn("When Mac OS finishes, RetroBoot restarts.", json.dumps(job.public()))
        self.assertEqual(updates.public()["staged"]["status"], "ready")
        self.assertEqual(slotenv.read_env(self.esp)[0].get("rb_next", ""), "")
        await self.until_event("host_reboot")
        env, _ = slotenv.read_env(self.esp)
        self.assertEqual((env["rb_next"], env["rb_tries"]), ("b", "1"))
        self.assertTrue(helpers.events(self.app, "update_armed"))
        self.assertEqual(self.ev("late_stop_finished")[0]["purpose"], "host-reboot")
        self.assertFalse(self.ev("poweroff"))
        self.assertFalse(self.app.powered_off)


if __name__ == "__main__":
    unittest.main()
