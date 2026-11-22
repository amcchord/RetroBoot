"""Stopped OS X identity is bounded, read-only, and distinct from boot readiness."""
import json
import subprocess
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from retroboot import osx_identity, power_osx
from retroboot.machines import MachineUnavailable, MachineError, validate_meta


class Identity(unittest.TestCase):
    def test_profile_version_and_build_must_agree(self):
        for profile, version, build in (("macosx-10.2", "10.2.8", "6R73"),
                                        ("macosx-10.3", "10.3.9", "7W98"),
                                        ("macosx-10.4", "10.4.11", "8S165")):
            osx_identity.validate(profile, version, build)
        osx_identity.validate("macosx-10.4", None, None)
        for version, build in (("10.3.9", "7W98"), ("10.5.0", "9A581"),
                               (None, "8I128"), (True, None), ("10.4.6", "../../x")):
            with self.assertRaises(ValueError):
                osx_identity.validate("macosx-10.4", version, build)

    def test_json_identity_validation_covers_imported_and_checkpoint_records(self):
        meta = {"id": "test", "schema_version": 3, "guest_os": "macosx-10.4",
                "template": {"machineFamily": "powerMacOSX", "guestOS": "macosx-10.4"}}
        self.assertEqual(validate_meta(meta, "test"), meta)
        with self.assertRaises(MachineError):
            validate_meta({**meta, "os_version": "10.3.9", "os_build": "7W98"}, "test")

    def test_blank_disk_learning_and_mismatch_refusal(self):
        machine = SimpleNamespace(disk=Path('/disk'), guest_os="macosx-10.4",
                                  update_meta=Mock(), drop_meta=Mock())
        with patch.object(osx_identity, 'read', return_value={"os_version": "10.4.6", "os_build": "8I128"}):
            osx_identity.refresh(machine, Path('/apps'))
        machine.update_meta.assert_called_once_with(os_version="10.4.6", os_build="8I128")
        with patch.object(osx_identity, 'read', return_value={"os_version": "10.3.9", "os_build": "7W98"}):
            with self.assertRaises(MachineUnavailable):
                osx_identity.refresh(machine, Path('/apps'))
        with patch.object(osx_identity, 'read', return_value=None):
            self.assertIsNone(osx_identity.refresh(machine, Path('/apps')))
        machine.drop_meta.assert_called_once_with('os_version', 'os_build')

    def test_bounded_child_failure_does_not_claim_identity(self):
        for output in (b'null', b'[]', b'bad json', b'x' * 1025):
            with patch.object(subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=output)):
                self.assertIsNone(osx_identity.read(Path('/disk'), Path('/apps')))
        with patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('read', 10)):
            self.assertIsNone(osx_identity.read(Path('/disk'), Path('/apps')))

    def test_read_budget_rejects_unbounded_or_oversized_forks(self):
        f = Mock()
        reader = osx_identity._BoundedReader(f)
        for count in (-1, 64 * 1024 * 1024 + 1):
            with self.assertRaises(ValueError):
                reader.read(count)
        f.read.assert_not_called()

    def test_power_controls_require_the_exact_measured_native_build(self):
        self.assertTrue(power_osx.supported('macosx-10.4', '10.4.6', '8I128'))
        for build in (None, '8I127', '8S165', 'unknown'):
            self.assertFalse(power_osx.supported('macosx-10.4', '10.4.6', build))
