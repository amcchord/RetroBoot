"""Stdlib-only unit tests for Clean Update (runs without cryptography or aiohttp)."""
import asyncio
import gzip
import io
import json
import os
import shutil
import sys
import tarfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
from retroboot import security
from retroboot.hostd import Hostd
from retroboot.panel.update import ConfirmRestartUpdate, SoftwareUpdate
from retroboot.update import bundle as B, slotenv, tree
from retroboot.update.writer import carry_state, clean_reset_data
from retroboot.util import wall

NEW_GRUB = (Path(__file__).resolve().parents[2] / "image/grub/grub.cfg.in").read_text().replace("@CMDLINE@", "quiet")
NEW_CMD = "quiet panic=0"


def make_root(path: Path, version="3.2.0", build_id="3.2.0-20261124T201102Z-6c89b03d", flavor="debug",
              keyfile_path="/var/lib/retroboot/network/connections"):
    (path / "usr/lib/retroboot/boot/esp/grub/fonts").mkdir(parents=True, exist_ok=True)
    (path / "usr/lib/retroboot/boot/esp/EFI/BOOT").mkdir(parents=True, exist_ok=True)
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
    (path / "usr/lib/retroboot/hostd/retroboot/update").mkdir(parents=True, exist_ok=True)
    if keyfile_path:
        (path / "etc/NetworkManager/conf.d").mkdir(parents=True, exist_ok=True)
        (path / "etc/NetworkManager/conf.d/50-retroboot-keyfile.conf").write_text(f"[keyfile]\npath={keyfile_path}\n")
    return path


def make_unsigned_bundle(out: Path, root: Path, *, version="3.2.0",
                         build_id="3.2.0-20261124T201102Z-6c89b03d", flavor="debug") -> Path:
    files = gzip.compress(json.dumps(tree.build(str(root))).encode())
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        tf.add(str(root), arcname=".")
    payload = buf.getvalue()
    import hashlib
    manifest = {"format": B.FORMAT, "product": "RetroBoot", "version": version, "build_id": build_id,
                "flavor": flavor, "min_version": "0.2.0", "settings_schema": 1, "settings_schema_min": 1,
                "kernel": {"version": "6.12", "cmdline": NEW_CMD},
                "root": {"member": "root.sqfs", "format": "squashfs", "compression": "tar", "bytes": len(payload),
                         "sha256": hashlib.sha256(payload).hexdigest(), "installed_bytes": 4096, "files": 10},
                "files": {"member": "root-files.json.gz", "bytes": len(files),
                          "sha256": hashlib.sha256(files).hexdigest()},
                "notes": "Clean test update."}
    mbytes = json.dumps(manifest).encode()
    sig = json.dumps({"format": B.SIG_FORMAT, "alg": "ed25519", "key_id": "testkid",
                      "signature": "AAAA"}).encode()
    members = {"manifest.json": mbytes, "manifest.sig": sig, "root-files.json.gz": files, "root.sqfs": payload}
    with tarfile.open(out, "w", format=tarfile.USTAR_FORMAT) as tf:
        for n in B.MEMBERS:
            ti = tarfile.TarInfo(n)
            ti.size = len(members[n])
            tf.addfile(ti, io.BytesIO(members[n]))
    return out


class FakeOps:
    def mkfs(self, dev, label):
        shutil.rmtree(dev, ignore_errors=True)
        os.makedirs(dev)

    def mount(self, dev, path, ro=False):
        if os.path.islink(path):
            os.unlink(path)
        elif os.path.isdir(path):
            os.rmdir(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(dev, path)

    def umount(self, path):
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
        progress(1.0)

    def verify_tree(self, list_file, root, progress):
        return tree.verify(tree.load(list_file), root, progress)

    def sync(self):
        pass

    def size_bytes(self, dev):
        return 3 * 1024 ** 3


class CleanUpdateStdlibTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)

    def test_compatibility_clean_flags(self):
        manifest = {"format": B.FORMAT, "product": "RetroBoot", "version": "3.2.0",
                    "build_id": "3.2.0-20261124T201102Z-6c89b03d", "flavor": "debug", "min_version": "0.2.0",
                    "settings_schema": 5, "settings_schema_min": 5, "arch": "amd64", "board": "pc"}
        inst = {"version": "3.2.0", "build_id": "3.2.0-20261124T201102Z-6c89b03d", "flavor": "debug",
                "arch": "amd64", "board": "pc"}
        with self.assertRaises(B.BundleError) as e:
            B.compatibility(manifest, inst, 1)
        self.assertEqual(e.exception.code, "same-version")
        B.compatibility(manifest, inst, 1, clean=True)
        with self.assertRaises(B.BundleError) as e2:
            B.compatibility(manifest, {**inst, "flavor": "product"}, 1, clean=True)
        self.assertEqual(e2.exception.code, "wrong-flavor")

    def test_carry_state_clean_resets_user_state_and_keeps_wifi(self):
        src = self.root / "src"
        dst = self.root / "dst"
        data = self.root / "data"
        for d in (src / "etc/ssh", src / "var/lib/alsa", src / "var/lib/systemd/backlight",
                  src / "var/lib/systemd/rfkill", src / "var/lib/NetworkManager",
                  src / "etc/NetworkManager/system-connections", src / "root/.ssh",
                  dst / "etc/NetworkManager/conf.d", dst / "var/lib/alsa",
                  dst / "var/lib/systemd/backlight", dst / "root/.ssh", data):
            d.mkdir(parents=True, exist_ok=True)
        (src / "etc/hostname").write_text("custom-host\n")
        (src / "etc/hosts").write_text("127.0.1.1 custom-host\n")
        (src / "etc/machine-info").write_text("PRETTY_HOSTNAME=Custom\n")
        (src / "etc/machine-id").write_text("abc123\n")
        (src / "etc/timezone").write_text("America/New_York\n")
        os.symlink("/usr/share/zoneinfo/America/New_York", src / "etc/localtime")
        (src / "etc/ssh/ssh_host_ed25519_key").write_text("SSHKEY")
        (src / "var/lib/alsa/asound.state").write_text("ALSA")
        (src / "var/lib/systemd/backlight/b").write_text("1")
        (src / "var/lib/systemd/rfkill/r").write_text("0")
        (src / "etc/NetworkManager/system-connections/Home.nmconnection").write_text("ssid=Home\n")
        (src / "root/.ssh/authorized_keys").write_text("ssh-ed25519 AAAAuser\n")
        (dst / "etc/machine-info").write_text("old\n")
        (dst / "var/lib/alsa/asound.state").write_text("old\n")
        (dst / "var/lib/systemd/backlight/old").write_text("old\n")
        (dst / "root/.ssh/authorized_keys").write_text("ssh-ed25519 AAAAlab\n")
        (dst / "etc/NetworkManager/conf.d/50.conf").write_text("[keyfile]\npath=/var/lib/retroboot/network/connections\n")

        carry_state(src, dst, data, clean=True)
        self.assertEqual((dst / "etc/hostname").read_text(), "retroboot\n")
        self.assertIn("127.0.1.1\tretroboot", (dst / "etc/hosts").read_text())
        self.assertFalse((dst / "etc/machine-info").exists())
        self.assertEqual((dst / "etc/machine-id").read_text(), "abc123\n")
        self.assertEqual((dst / "etc/timezone").read_text(), "Etc/UTC\n")
        self.assertEqual(os.readlink(dst / "etc/localtime"), "/usr/share/zoneinfo/Etc/UTC")
        self.assertFalse((dst / "var/lib/alsa/asound.state").exists())
        self.assertFalse((dst / "var/lib/systemd/backlight").exists())
        self.assertFalse((dst / "var/lib/systemd/rfkill").exists())
        self.assertEqual((dst / "etc/ssh/ssh_host_ed25519_key").read_text(), "SSHKEY")
        self.assertTrue((data / "network/connections/Home.nmconnection").is_file())
        self.assertEqual((dst / "root/.ssh/authorized_keys").read_text().splitlines(),
                         ["ssh-ed25519 AAAAuser", "ssh-ed25519 AAAAlab"])

    def test_clean_reset_data_and_frozen_pairing(self):
        data = self.root / "rbdata"
        esp = self.root / "esp"
        for d in ("machines/mac1", "checkpoints/mac1/c1", "share", "media", "roms", "downloads",
                  "assets/platinum", "cache/mesa-osx", "bluetooth/11:22", "logs/evidence/ev1",
                  "logs/bundles", "logs/journal", "update/settings/b1", "update/incoming",
                  "network/connections", "config", "state"):
            (data / d).mkdir(parents=True, exist_ok=True)
        (data / "machines/mac1/disk.img").write_bytes(b"disk")
        (data / "network/connections/Office.nmconnection").write_text("ssid=Office\n")
        (data / "config/settings.json").write_text("{}")
        (data / "config/settings.json.bak").write_text("{}")
        (data / "config/install-seed.json").write_text("{}")
        (data / "config/lab-auth.env").write_text("RB_LAB_HTTP_USER=u\n")
        (data / "config/hostd.local.json").write_text("{}")
        (data / "config/hostd.local.migrated").write_text("h\n")
        (data / "state/pairing.json").write_text('{"secret": "00", "devices": []}')
        (data / "state/tls-cert.pem").write_text("cert")
        (data / "state/known-good.json").write_text("{}")
        (data / "state/growdata.done").write_text("1")
        (data / "state/microcode.json").write_text("{}")
        (data / "state/boot-network.json").write_text("{}")
        (data / "update/state.json").write_text('{"armed": true}')
        (data / "logs/lifecycle.jsonl").write_text("{}\n")
        (data / "logs/journal/j").write_text("j")

        rb = esp / "grub/retroboot"
        rb.mkdir(parents=True)
        (rb / "display.cfg").write_text("set rb_scale=2\n")
        (rb / "checker-default.png").write_bytes(b"d")
        (rb / "checker-default-s1.png").write_bytes(b"d1")
        (rb / "checker-1920x1080-s2.png").write_bytes(b"g")
        (rb / "startup-x.cpio").write_bytes(b"c")
        (rb / "startup-icon.rbi").write_bytes(b"r")
        (rb / "slots.env").write_bytes(b"e")
        (rb / "slot-a.cfg").write_text("a")

        ps = security.PairingService(data / "state")
        _, tok = ps.pair(ps.current_code()[0], "mac", "127.0.0.1")
        dev = ps.device_for_token(tok)
        dev["last_seen"] = "2000-01-01T00:00:00Z"

        clean_reset_data(data, esp)
        ps.frozen = True
        ps.touch(dev)

        self.assertFalse((data / "machines/mac1").exists())
        self.assertTrue((data / "machines").is_dir())
        self.assertTrue((data / "bluetooth").is_dir())
        self.assertFalse((data / "bluetooth/11:22").exists())
        self.assertTrue((data / "network/connections/Office.nmconnection").is_file())
        self.assertFalse((data / "config/settings.json").exists())
        self.assertFalse((data / "config/install-seed.json").exists())
        self.assertTrue((data / "config/lab-auth.env").is_file())
        self.assertTrue((data / "config/hostd.local.json").is_file())
        self.assertFalse((data / "state/pairing.json").exists())
        self.assertFalse((data / "state/tls-cert.pem").exists())
        self.assertTrue((data / "state/growdata.done").is_file())
        self.assertTrue((data / "state/microcode.json").is_file())
        self.assertTrue((data / "state/boot-network.json").is_file())
        self.assertTrue((data / "update/state.json").is_file())
        self.assertTrue((data / "logs/journal/j").is_file())
        self.assertFalse((rb / "display.cfg").exists())
        self.assertFalse((rb / "checker-1920x1080-s2.png").exists())
        self.assertFalse((rb / "startup-x.cpio").exists())
        self.assertFalse((rb / "startup-icon.rbi").exists())
        self.assertTrue((rb / "checker-default.png").is_file())
        self.assertTrue((rb / "checker-default-s1.png").is_file())
        self.assertTrue((rb / "slots.env").is_file())
        self.assertTrue((rb / "slot-a.cfg").is_file())

    async def test_manager_and_panel_same_version_clean_update(self):
        esp = self.root / "boot/efi"
        (esp / "grub/retroboot").mkdir(parents=True)
        (esp / "grub/grub.cfg").write_text(NEW_GRUB)
        make_root(self.root)
        slots = {s: self.root / "slots" / s for s in "ab"}
        for d in slots.values():
            d.mkdir(parents=True)
        make_root(slots["a"])
        cmdline = self.root / "cmdline"
        cmdline.write_text("quiet retroboot.slot=a\n")
        new_root = make_root(self.root / "newroot")
        bundle_path = make_unsigned_bundle(self.root / "same.rbu", new_root)

        async def make_app(current="a"):
            helpers.make_package(self.root)
            cfg = helpers.make_config(self.root)
            cfg.update_esp = str(esp)
            cfg.update_keys_dir = str(self.root / "keys")
            cfg.update_layout = {"current": current, "a": str(slots["a"]), "b": str(slots["b"]),
                                 "esp": "x", "data": "y"}
            cfg.cmdline_path = str(cmdline)
            cfg.update_panel_health_s = 0.2
            for attr, fn in (("sched_setaffinity", lambda *a: None), ("sched_getaffinity", lambda *a: set())):
                if not hasattr(os, attr):
                    p = mock.patch.object(os, attr, fn, create=True)
                    p.start()
                    self.addCleanup(p.stop)
            app = Hostd(cfg)
            app.loop = asyncio.get_running_loop()
            app.settings.store.load()
            app.updates.ops = FakeOps()
            await app.updates.start()
            self.addAsyncCleanup(app.updates.stop)
            return app

        app = await make_app()
        same_entry = {"version": "3.2.0", "build_id": "3.2.0-20261124T201102Z-6c89b03d", "flavor": "debug",
                      "url": "https://updates.example/u.rbu", "sha256": "0" * 64, "bytes": 1000}
        feed = {"format": "retroboot-feed/1", "updates": [same_entry]}
        self.assertIsNone(app.updates.pick_from_feed(feed))
        self.assertEqual(app.updates.pick_latest_from_feed(feed)["build_id"], same_entry["build_id"])

        app.settings.applied["update.url"] = "https://updates.example/feed.json"
        app.updates.state["check"] = {"at": wall(), "url": "https://updates.example/feed.json",
                                      "found": None, "latest": app.updates.pick_latest_from_feed(feed)}
        app.updates.save()
        scr = SoftwareUpdate(app.panel)
        scr.scanning = False
        self.assertIn("Clean Update (3.2.0)", json.dumps(scr.doc()))

        (app.paths.machines / "m1").mkdir(parents=True)
        (app.paths.machines / "m1/disk.img").write_bytes(b"mac")
        (app.paths.nm_connections / "Home.nmconnection").write_text("ssid=Home\n")
        app.pairing.pair(app.pairing.current_code()[0], "browser", "127.0.0.1")

        def fake_verify(self_b, keys, progress=None):
            self_b.key_id = "testkid"
            self_b.manifest = json.loads(self_b.manifest_bytes)
            return self_b.manifest

        with mock.patch.object(B.Bundle, "verify", fake_verify):
            incoming = app.updates.new_incoming()
            shutil.copy(bundle_path, incoming)
            job = app.updates.install_job({"kind": "file", "path": str(incoming)}, clean=True)
            await job.done.wait()
        self.assertEqual(job.state, "succeeded", job.error)
        self.assertTrue(app.updates.public()["staged"]["clean"])
        self.assertIn("Clean Update", app.updates.notices()[0]["message"])

        # Panel staged view lets user toggle clean mode and triggers caution dialog
        scr2 = SoftwareUpdate(app.panel)
        scr2.scanning = False
        self.assertIn("Restart for Clean Update", json.dumps(scr2.doc()))
        await scr2.change("clean", False)
        self.assertIn("Restart to Update", json.dumps(scr2.doc()))
        await scr2.change("clean", True)
        self.assertIn("Restart for Clean Update", json.dumps(scr2.doc()))

        cdoc = ConfirmRestartUpdate(app.panel, "3.2.0", clean=True).doc()
        self.assertEqual(cdoc["alert_icon"], "caution")
        self.assertTrue(next(b for b in cdoc["items"][-1]["items"] if b["id"] == "cancel").get("default"))

        rjob = app.updates.restart_job(origin="panel")
        await rjob.done.wait()
        self.assertEqual(rjob.state, "succeeded", rjob.error)
        self.assertFalse((app.paths.machines / "m1").exists())
        self.assertFalse((app.paths.state / "pairing.json").exists())
        self.assertTrue((app.paths.nm_connections / "Home.nmconnection").is_file())
        self.assertTrue(app.pairing.frozen)

        slotenv.update_env(esp, rb_tries="0")
        cmdline.write_text("quiet retroboot.slot=b retroboot.trial=1\n")
        app2 = await make_app(current="b")
        app2.supervisor.state = "stopped"
        app2.panel.visible = True
        app2.presenter.connected.set()
        await helpers.wait_for(lambda: app2.updates.committed, 5, 0.05, "clean commit")
        self.assertIn("clean-updated", app2.updates.state["last_result"]["message"])
        self.assertTrue(app2.updates.state["last_result"]["clean"])


if __name__ == "__main__":
    unittest.main()
