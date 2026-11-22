"""USB Overdrive payload checks (synthetic payloads; real disks only through env).

    RB_USBO_IMAGE=disk.img RB_USBO_PAYLOAD=payload python3 -m unittest tests/test_usboverdrive.py
"""
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from rbguestprep import install, usboverdrive  # noqa: E402


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_payload(root: str) -> None:
    for folder, (sub, names) in usboverdrive.FOLDERS.items():
        entries = []
        for i, name in enumerate(names):
            data = f"data {folder} {name}".encode()
            rsrc = f"rsrc {name}".encode() if i == 0 else b""
            fi = (b"pref" if folder == "Preferences" else b"USBd") + b"USBd" + bytes(24)
            os.makedirs(os.path.join(root, sub, "data"), exist_ok=True)
            with open(os.path.join(root, sub, "data", name), "wb") as f:
                f.write(data)
            if rsrc:
                os.makedirs(os.path.join(root, sub, "rsrc"), exist_ok=True)
                with open(os.path.join(root, sub, "rsrc", name), "wb") as f:
                    f.write(rsrc)
            entries.append({"path": [name], "kind": "file", "finder_info": fi.hex(),
                            "data": {"size": len(data), "sha256": _sha(data)},
                            "rsrc": {"size": len(rsrc), "sha256": _sha(rsrc)}})
        with open(os.path.join(root, sub, "manifest.json"), "w") as f:
            json.dump({"format": install.TREE_FORMAT, "version": install.TREE_VERSION, "entries": entries}, f)
    with open(os.path.join(root, "payload.json"), "w") as f:
        json.dump({"format": usboverdrive.PAYLOAD_FORMAT, "version": usboverdrive.PAYLOAD_VERSION,
                   "product": "USB Overdrive 1.4", "folders": {}}, f)


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = self.td.name
        make_payload(self.root)

    def test_valid_payload_loads(self):
        p = usboverdrive.load_payload(self.root)
        self.assertEqual(set(p["trees"]), set(usboverdrive.FOLDERS))
        self.assertEqual(sorted(e["path"][0] for e in p["trees"]["Preferences"].entries),
                         sorted(usboverdrive.FOLDERS["Preferences"][1]))

    def test_tampered_fork_is_refused(self):
        with open(os.path.join(self.root, "extensions", "data", "USB Mouse Overdrive"), "ab") as f:
            f.write(b"x")
        with self.assertRaises(install.InstallError) as cm:
            usboverdrive.load_payload(self.root)
        self.assertEqual(cm.exception.code, "invalid-payload")

    def test_missing_item_is_refused(self):
        m = json.load(open(os.path.join(self.root, "preferences", "manifest.json")))
        m["entries"] = m["entries"][:-1]
        json.dump(m, open(os.path.join(self.root, "preferences", "manifest.json"), "w"))
        with self.assertRaises(install.InstallError):
            usboverdrive.load_payload(self.root)

    def test_not_a_payload(self):
        json.dump({"format": "something-else", "version": 1}, open(os.path.join(self.root, "payload.json"), "w"))
        out = io.StringIO()
        with redirect_stdout(out):
            rc = usboverdrive.main(["install", "/nonexistent.img", "--payload", self.root])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out.getvalue())["code"], "invalid-payload")


@unittest.skipUnless(os.environ.get("RB_USBO_IMAGE") and os.environ.get("RB_USBO_PAYLOAD"),
                     "set RB_USBO_IMAGE (a disk with the payload installed) and RB_USBO_PAYLOAD")
class DiskTests(unittest.TestCase):
    def test_status_and_presence(self):
        image, payload = os.environ["RB_USBO_IMAGE"], os.environ["RB_USBO_PAYLOAD"]
        st = usboverdrive.status(image, payload)
        self.assertTrue(st["installed"], st)
        self.assertTrue(usboverdrive.present(image))


if __name__ == "__main__":
    unittest.main()
