"""Display presets: real FAT-file writes, reboot journals and authenticated APIs.

No physical display, GPIO bus, touch controller or firmware is simulated here.
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import helpers
from retroboot import rpidisplay as rd
from retroboot.platform import Platform

BOOT_IDS = ('11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222',
            '33333333-3333-3333-3333-333333333333', '44444444-4444-4444-4444-444444444444')


def files(root, model='pi4-5'):
    boot = root / 'boot/firmware'
    overlays = boot / 'overlays'
    overlays.mkdir(parents=True)
    for p in rd.PROFILES:
        if p.overlay:
            (overlays / (p.overlay + '.dtbo')).write_bytes(b'Test fixture: no hardware driver')
    (boot / 'config.txt').write_text('[all]\n' + rd.INCLUDE + '\n')
    rd.stage(boot, model, overlays)
    proc = root / 'proc'
    (proc / 'sys/kernel/random').mkdir(parents=True, exist_ok=True)
    (proc / 'sys/kernel/random/boot_id').write_text(BOOT_IDS[0])
    return boot, proc


class Presets(unittest.TestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.boot, self.proc = files(self.root)
        self.app = SimpleNamespace(cfg=SimpleNamespace(pi_boot_dir=str(self.boot), proc_path=str(self.proc)),
                                   platform=Platform(family='raspberrypi', board='pi4'),
                                   paths=SimpleNamespace(state=self.root / 'state'),
                                   lifecycle=SimpleNamespace(event=lambda *a, **k: None))
        self.manager = rd.PiDisplay(self.app)

    def reboot(self, index):
        (self.proc / 'sys/kernel/random/boot_id').write_text(BOOT_IDS[index])
        self.manager = rd.PiDisplay(self.app)
        return self.manager.public()

    def choose(self, profile):
        return self.manager.select(profile, self.manager.public()['revision'])

    def test_canonical_profiles_and_windows_line_endings(self):
        for p in rd.PROFILES:
            self.assertEqual(rd.identify(rd.render(p.id).replace('\n', '\r\n')), p.id)
            self.assertTrue(all(len(l) <= 98 for l in rd.render(p.id).splitlines()))
        self.assertIsNone(rd.identify(rd.render('hdmi') + 'custom=value\n'))
        for invalid in ('../../x', 'hdmi\ndtoverlay=evil', True, [], None):
            with self.assertRaises(rd.DisplayError):
                rd.render(invalid)

    def test_hyperpixel_resets_overlay_scope_before_base_audio(self):
        lines = rd.render('hyperpixel4').splitlines()
        self.assertLess(lines.index('dtoverlay='), lines.index('dtparam=audio=off'))
        self.assertLess(lines.index('dtparam=audio=off'), lines.index('dtoverlay=vc4-kms-dpi-hyperpixel4,rotate=90'))
        self.assertIn('enable_uart=0', lines)
        self.assertIn('dtparam=i2c_arm=off', lines)
        self.assertIn('dtparam=spi=off', lines)

    def test_zero_has_gpio_presets_and_no_dsi(self):
        self.app.platform = Platform(family='raspberrypi', board='zero2w')
        ids = {p['id'] for p in self.manager.public()['profiles']}
        self.assertEqual(ids, {'auto', 'hdmi', 'hyperpixel4', 'hyperpixel4-square'})
        with self.assertRaises(rd.DisplayError):
            self.choose('dsi-touch')

    def test_image_profile_on_arm_virt(self):
        self.app.platform = SimpleNamespace(family='other', is_raspberry_pi=False, board=None, image_model='zero2w')
        self.assertTrue(self.manager.public()['available'])
        self.assertNotIn('dsi-touch', {p['id'] for p in self.manager.public()['profiles']})

    def test_pc_has_no_picker_and_never_writes(self):
        self.app.platform = Platform()
        self.assertEqual(self.manager.public(), {'available': False})
        before = self.manager.file.read_bytes()
        with self.assertRaises(rd.DisplayError):
            self.manager.select('hdmi', 'unused')
        self.assertEqual(self.manager.file.read_bytes(), before)

    def test_one_start_trial_kept_and_service_restart(self):
        self.assertEqual(self.manager.public()['active'], 'auto')
        result = self.choose('hyperpixel4')
        self.assertEqual(result['pending'], 'hyperpixel4')
        self.assertTrue(result['restart_required'])
        result = self.reboot(1)
        self.assertEqual((result['active'], result['trial'], result['profile']), ('hyperpixel4', 'hyperpixel4', 'auto'))
        self.manager = rd.PiDisplay(self.app)  # service restart in the same kernel boot
        self.assertEqual(self.manager.public()['trial'], 'hyperpixel4')
        result = self.manager.confirm(True, self.manager.public()['revision'])
        self.assertEqual(result['profile'], 'hyperpixel4')
        self.assertFalse(result['restart_required'])
        result = self.reboot(2)
        self.assertEqual((result['active'], result['profile'], result['trial']), ('hyperpixel4', 'hyperpixel4', None))

    def test_no_confirmation_recovers_previous_on_next_boot(self):
        self.choose('hyperpixel4-square')
        self.assertEqual(self.reboot(1)['trial'], 'hyperpixel4-square')
        result = self.reboot(2)
        self.assertEqual((result['active'], result['profile'], result['trial']), ('auto', 'auto', None))

    def test_queued_confirmation_from_an_earlier_trial_is_rejected(self):
        self.choose('hyperpixel4')
        old = self.reboot(1)
        self.assertEqual(old['profile'], 'auto')
        self.reboot(2)  # no confirmation: back to auto
        self.choose('dsi-touch')
        current = self.reboot(3)
        self.assertEqual(current['profile'], 'auto')
        self.assertNotEqual(old['revision'], current['revision'])
        with self.assertRaisesRegex(rd.DisplayError, 'changed elsewhere'):
            self.manager.confirm(True, old['revision'])
        self.assertEqual(self.manager.public()['trial'], 'dsi-touch')

    def test_declining_trial_keeps_previous_for_next_start(self):
        self.choose('dsi-touch')
        result = self.reboot(1)
        result = self.manager.confirm(False, result['revision'])
        self.assertEqual(result['profile'], 'auto')
        self.assertTrue(result['restart_required'])
        self.assertIsNone(result['trial'])

    def test_stale_invalid_and_custom_changes_do_not_write(self):
        revision = self.manager.public()['revision']
        self.choose('hdmi')
        before = self.manager.file.read_bytes()
        with self.assertRaisesRegex(rd.DisplayError, 'changed elsewhere'):
            self.manager.select('hyperpixel4', revision)
        self.assertEqual(self.manager.file.read_bytes(), before)
        for invalid in (None, {}, [], 'not-a-profile'):
            with self.assertRaises(rd.DisplayError):
                self.manager.select(invalid, self.manager.public()['revision'])
        self.manager.file.write_text(rd.render('hdmi') + 'my_custom_setting=1\n')
        before = self.manager.file.read_bytes()
        with self.assertRaisesRegex(rd.DisplayError, 'custom display settings'):
            self.choose('auto')
        self.assertEqual(self.manager.file.read_bytes(), before)

    def test_missing_overlay_is_not_offered(self):
        (self.boot / 'overlays/vc4-kms-dpi-hyperpixel4.dtbo').unlink()
        self.assertNotIn('hyperpixel4', {p['id'] for p in self.manager.public()['profiles']})
        with self.assertRaises(rd.DisplayError):
            self.choose('hyperpixel4')

    def test_symlink_and_missing_include_refused(self):
        self.manager.file.unlink()
        target = self.root / 'protected'
        target.write_text(rd.render('auto'))
        self.manager.file.symlink_to(target)
        self.assertFalse(self.manager.public()['available'])
        self.assertEqual(target.read_text(), rd.render('auto'))
        self.manager.file.unlink()
        self.manager.file.write_text(rd.render('auto'))
        (self.boot / 'config.txt').write_text('kernel=kernel8.img\n')
        self.manager = rd.PiDisplay(self.app)
        self.assertFalse(self.manager.public()['available'])

    def test_readonly_failure_keeps_previous_setting(self):
        self.manager.public()
        before = self.manager.file.read_bytes()
        with mock.patch.object(self.manager, '_write', side_effect=OSError('read only')):
            with self.assertRaises(OSError):
                self.choose('hdmi')
        self.assertEqual(self.manager.file.read_bytes(), before)
        self.assertIsNone(self.manager.public()['pending'])

    def test_stage_validates_before_writing_and_zero_excludes_dsi_files(self):
        stage_root = self.root / 'stage'
        with self.assertRaises(rd.DisplayError):
            rd.stage(stage_root, 'pi4-5', self.root / 'missing')
        self.assertFalse(stage_root.exists())
        rd.stage(stage_root, 'zero2w', self.boot / 'overlays')
        self.assertEqual({p.stem for p in (stage_root / 'retroboot/displays').glob('*.txt')},
                         {'auto', 'hdmi', 'hyperpixel4', 'hyperpixel4-square'})
        self.assertIn('no DSI connector', (stage_root / 'DISPLAY-SETUP.txt').read_text())


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import test_web_api
        self.base = test_web_api.ApiTests
        await self.base.asyncSetUp(self)
        boot, proc = files(self.root)
        self.app.cfg.pi_boot_dir, self.app.cfg.proc_path = str(boot), str(proc)
        self.app.platform = Platform(family='raspberrypi', board='pi4')
        self.app.rpi_display = rd.PiDisplay(self.app)
        # The API object was registered during Hostd construction; register uses
        # the live authority through the hostd property.

    def h(self, *a, **k):
        return self.base.h(self, *a, **k)

    async def pair(self):
        return await self.base.pair(self)

    async def test_auth_csrf_stale_revision_and_shared_panel_authority(self):
        r = await self.client.get('/api/v1/pi-display')
        self.assertEqual(r.status, 401)
        await self.pair()
        response = await self.client.get('/api/v1/pi-display', headers=self.h())
        status = await response.json()
        self.assertTrue(status['available'], status)
        body = {'profile': 'hyperpixel4', 'revision': status['revision']}
        response = await self.client.post('/api/v1/pi-display', json=body, headers=self.h(csrf=False))
        self.assertEqual(response.status, 403)
        response = await self.client.post('/api/v1/pi-display', json=body, headers=self.h())
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.app.rpi_display.public()['pending'], 'hyperpixel4')
        response = await self.client.post('/api/v1/pi-display', json={**body, 'profile': 'hdmi'}, headers=self.h())
        self.assertEqual(response.status, 409)
        response = await self.client.post('/api/v1/pi-display', json={'path': '/tmp/x'}, headers=self.h())
        self.assertEqual(response.status, 422)
        response = await self.client.post('/api/v1/pi-display/confirm', json={'keep': 'yes'}, headers=self.h())
        self.assertEqual(response.status, 422)

    async def test_local_panel_choice_writes_same_authority(self):
        from retroboot.panel.controller import DisplaySound
        from test_lifecycle import find_item
        panel = DisplaySound(self.app.panel, page='pi-display')
        popup = find_item(panel.doc(), 'pi-display.profile')
        self.assertIsNotNone(popup)
        self.assertIn('hyperpixel4-square', {p['id'] for p in popup['options']})
        with mock.patch.object(self.app.panel, 'refresh', new=mock.AsyncMock()):
            await panel.action('pi-display.try', {'pi-display.profile': 'hyperpixel4-square'})
        self.assertEqual(self.app.rpi_display.public()['pending'], 'hyperpixel4-square')


if __name__ == '__main__':
    unittest.main()
