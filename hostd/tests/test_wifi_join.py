"""Wi-Fi join failure wording (retroboot/network.py), against a scripted nmcli.

On the first hardware tested (Intel AX200, WPA2/WPA3 transition network) a wrong
password made NetworkManager fail with reason 'ssid-not-found' after SAE
authentication was rejected, and the panel said only "Could not join the network."
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from retroboot.network import NetworkAdapter, NetworkError


class FakeNmcli:
    def __init__(self, up_err):
        self.up_err = up_err
        self.calls = []

    async def __call__(self, argv, timeout=None, **kw):
        self.calls.append(argv)
        if argv[:5] == ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"]:
            return 0, "wlp98s0:wifi:disconnected\neno1:ethernet:connected\n", ""
        if argv[:3] == ["nmcli", "connection", "load"]:
            return 0, "", ""
        if "up" in argv:
            return 4, "", self.up_err
        return 0, "", ""


class WifiJoinTests(unittest.TestCase):
    def join(self, err, scanned, psk="not-the-password"):
        with tempfile.TemporaryDirectory() as td:
            fake = FakeNmcli(err)
            net = NetworkAdapter(runner=fake, nm_connections_dir=Path(td))
            net.last_scan = [{"ssid": "SvensHaus"}] if scanned else []
            with self.assertRaises(NetworkError) as cm:
                asyncio.run(net.connect("SvensHaus", psk))
            self.assertEqual(list(Path(td).iterdir()), [])          # no broken profile left behind
            self.assertTrue(any("delete" in c for c in fake.calls))
            return str(cm.exception)

    def test_wrong_password_reported_as_not_found(self):
        msg = self.join("Error: Connection activation failed: The Wi-Fi network could not be found", True)
        self.assertEqual(msg, "The password was not accepted. Check it and try again.")

    def test_secrets_error(self):
        msg = self.join("Error: Connection activation failed: Secrets were required, but not provided", False)
        self.assertIn("password was not accepted", msg)

    def test_network_gone(self):
        msg = self.join("Error: Connection activation failed: The Wi-Fi network could not be found", False)
        self.assertEqual(msg, "RetroBoot could not connect to this network. Move closer to it or try again.")


if __name__ == "__main__":
    unittest.main()
