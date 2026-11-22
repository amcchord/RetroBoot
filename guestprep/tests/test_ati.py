"""rbguestprep.ati: the radeon-3d set, RetroBoot Display's tree and the CLI
(synthetic; a real disk and CD only through env).

    RB_ATI_IMAGE=copy.img RB_ATI_CD=macos_921_ppc.iso python3 -m unittest tests/test_ati.py   (root, rb-dev)
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from rbguestprep import ati  # noqa: E402


class SetTest(unittest.TestCase):
    def test_radeon_3d_leaves_the_2d_accelerator_out(self):
        self.assertEqual(ati.SETS["radeon-3d"], ("ATI Radeon 3D Accelerator", "ATI Resource Manager"))
        self.assertNotIn(ati.ACCELERATOR_2D, ati.SETS["radeon-3d"])
        self.assertIn(ati.ACCELERATOR_2D, ati.SETS["radeon"])
        self.assertEqual(ati.DEFAULT_SET, "radeon-3d")

    def test_helper_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ati.helper_tree("none"))
            with self.assertRaises(ati.ATIError) as cm:
                ati.helper_tree(tmp)
            self.assertEqual(cm.exception.code, "helper-missing")
            with open(os.path.join(tmp, "manifest.json"), "w") as f:
                json.dump({"top": [ati.HELPER]}, f)
            self.assertEqual(ati.helper_tree(tmp), tmp)

    def test_install_needs_a_source(self):
        with self.assertRaises(ati.ATIError) as cm:
            ati.install_set("/nonexistent.img", helper="none")
        self.assertEqual(cm.exception.code, "no-source")

    def test_cli_reports_errors_as_json(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = ati.main(["install", "/nonexistent.img", "--helper", "none"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(err.getvalue())["error"], "no-source")

    def test_a_cd_that_is_not_a_mac_cd(self):
        with tempfile.TemporaryDirectory() as tmp:
            cd = os.path.join(tmp, "x.iso")
            with open(cd, "wb") as f:
                f.write(b"\0" * 65536)
            with self.assertRaises(ati.ATIError) as cm:
                ati.build_tree(cd, os.path.join(tmp, "tree"))
            self.assertIn(cm.exception.code, ("not-mac-cd", "not-macos-921-cd"))


@unittest.skipUnless(os.environ.get("RB_ATI_IMAGE") and os.environ.get("RB_ATI_CD") and os.geteuid() == 0,
                     "needs RB_ATI_IMAGE (a disk copy), RB_ATI_CD and root")
class RealDiskTest(unittest.TestCase):
    def test_install_twice_and_status(self):
        with tempfile.TemporaryDirectory(dir=os.path.dirname(os.environ["RB_ATI_IMAGE"])) as tmp:
            img = os.path.join(tmp, "disk.img")
            subprocess.run(["cp", "--sparse=always", os.environ["RB_ATI_IMAGE"], img], check=True)
            for _ in range(2):        # idempotent
                r = ati.install_set(img, cd=os.environ["RB_ATI_CD"])
                self.assertEqual(r["fsck"], "ok")
                self.assertEqual(r["status"]["state"], "installed")
            st = ati.status(img)
            self.assertEqual(st["items"]["ATI Driver Update"], "enabled")
            self.assertEqual(st["items"]["OpenGLRendererATI"], "enabled")
            self.assertNotEqual(st["items"][ati.ACCELERATOR_2D], "enabled")
            helper = ati.helper_tree()
            if helper:
                with open(os.path.join(helper, "manifest.json")) as f:
                    top = json.load(f).get("top", [])
                self.assertEqual(st["helper"], ati.HELPER in top)
                self.assertEqual(st["helper_startup"], ati.HELPER_STARTUP in top)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
