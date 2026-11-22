"""Exercise OS X EGL rejection at Launcher.run's actual pre-Popen boundary."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, mock

import helpers
from retroboot import osx, osx_graphics
from retroboot.launcher import Launcher


class OSXLauncherBoundary(IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp, self.root = helpers.scratch()
        self.addCleanup(self.temp.cleanup)
        self.pkg = self.root / 'package'
        self.pkg.mkdir()
        self.features = {osx.FEATURE, osx.QEMU_FEATURE, osx.NVRAM_FEATURE,
                         osx.FIRMWARE_FEATURE, 'display-retroboot',
                         osx_graphics.FEATURE, osx_graphics.EGL_FEATURE,
                         osx_graphics.COHERENT_FEATURE, osx_graphics.I8_FEATURE} | osx.SLEEP_FEATURES
        self.manifest = {'features': sorted(self.features), 'sources': {'qemu': osx.QEMU_SOURCE}}
        self.write_manifest(self.manifest)
        self.spec = {'session': 'test-osx', 'status': str(self.root/'status.json'),
                     'serial_log': str(self.root/'serial.log'), 'stderr_log': str(self.root/'stderr.log'),
                     'package': {'root': str(self.pkg), 'verify': []}, 'run_as': None,
                     'guest_os': 'macosx-10.3', 'os_build': '7W98',
                     'egl_render_node': '/dev/dri/renderD128',
                     'argv': ['/owned/qemu', '-vga', 'none', '-device',
                              'ati-vga,model=r100,x-agp=on,x-coherent-vram=on,x-i8-byte-order=on',
                              '-global', 'via-pmu.osx-sleep=on', '-global',
                              'uni-north-pci-pcihost.x-agp-compat=on', '-boot', 'c']}

    def write_manifest(self, value):
        (self.pkg/'manifest.json').write_text(json.dumps(value))

    async def boundary(self, spec, accepted):
        launcher = Launcher(spec, SimpleNamespace(event=mock.Mock()), verify=False)
        # Deliberately stop at Popen: positive tests verify the exact environment
        # passed to the real call; negative tests must never reach it.
        with mock.patch('retroboot.launcher.asyncio.create_subprocess_exec',
                        new=mock.AsyncMock(side_effect=OSError('test stopped at Popen'))) as spawn:
            self.assertEqual(await launcher.run(), 1)
            self.assertEqual(spawn.await_count, int(accepted))
            if accepted:
                self.assertEqual(spawn.call_args.kwargs['env']['RETROBOOT_EGL_RENDER_NODE'],
                                 '/dev/dri/renderD128')
            else:
                status = json.loads(Path(spec['status']).read_text())
                self.assertEqual(status['exit']['detail'], 'Invalid Mac OS X graphics device selection.')

    async def test_qualified_cache_reaches_the_actual_child_environment(self):
        cwd = self.root/'machines'/'tiger'
        cwd.mkdir(parents=True)
        cache = self.root/'cache'/'mesa-osx'
        spec = {**self.spec, 'cwd': str(cwd), 'shader_cache': str(cache)}
        launcher = Launcher(spec, SimpleNamespace(event=mock.Mock()), verify=False)
        with mock.patch('retroboot.launcher.asyncio.create_subprocess_exec',
                        new=mock.AsyncMock(side_effect=OSError('test stopped at Popen'))) as spawn:
            self.assertEqual(await launcher.run(), 1)
            self.assertEqual(spawn.await_count, 1)
            self.assertEqual(spawn.call_args.kwargs['env']['MESA_SHADER_CACHE_DIR'], str(cache))
            self.assertEqual(spawn.call_args.kwargs['env']['MESA_SHADER_CACHE_MAX_SIZE'], '256M')
        self.assertTrue(cache.is_dir())

    async def test_only_opted_in_tiger_requests_qemu_worker_names(self):
        for profile, build, enabled, named in [('macosx-10.4', '8S165', True, True),
                                                ('macosx-10.4', '8S165', False, False),
                                                ('macosx-10.3', '7W98', True, False)]:
            with self.subTest(profile=profile, enabled=enabled):
                name = 'Mac OS X 10.4.11 Deluxe,, Lab'
                original = [*self.spec['argv'], '-name', name]
                spec = {**self.spec, 'argv': original, 'guest_os': profile, 'os_build': build,
                        'osx_fast_cores': enabled}
                launcher = Launcher(spec, SimpleNamespace(event=mock.Mock()), verify=False)
                with mock.patch('retroboot.launcher.asyncio.create_subprocess_exec',
                                new=mock.AsyncMock(side_effect=OSError('stop at Popen'))) as spawn:
                    self.assertEqual(await launcher.run(), 1)
                actual = list(spawn.call_args.args)
                self.assertEqual(actual.count('-name'), 1)
                self.assertEqual(actual, [*original[:-1], name + ',debug-threads=on' if named else name])
                self.assertEqual(spec['argv'][-1], name)

    async def test_exact_panther_and_tiger_reach_popen(self):
        for profile, build in [('macosx-10.3', '7W98'), ('macosx-10.4', '8S165')]:
            with self.subTest(profile=profile):
                await self.boundary({**self.spec, 'guest_os': profile, 'os_build': build}, True)

    async def test_every_required_capability_and_dedicated_source(self):
        for feature in self.features:
            with self.subTest(missing=feature):
                self.write_manifest({**self.manifest, 'features': sorted(self.features-{feature})})
                await self.boundary(self.spec, False)
        bad = copy.deepcopy(self.manifest)
        bad['sources']['qemu'] = {**osx.QEMU_SOURCE, 'version': '11.0.2'}
        self.write_manifest(bad)
        await self.boundary(self.spec, False)

    async def test_unknown_classic_and_boot_cd_fail_before_popen(self):
        for profile, build in [('macosx-10.3', None), ('macosx-10.3', '7W98x'),
                               ('macosx-10.4', '8I128'), ('macos-9', '7W98')]:
            await self.boundary({**self.spec, 'guest_os': profile, 'os_build': build}, False)
        spec = copy.deepcopy(self.spec)
        spec['argv'][-1] = 'd'
        await self.boundary(spec, False)

    async def test_device_node_and_actual_repair_flags_are_required(self):
        for node in ['/dev/dri/card0', 'renderD128', '/dev/dri/renderD128/../card0']:
            await self.boundary({**self.spec, 'egl_render_node': node}, False)
        for old, new in [('x-coherent-vram=on', 'x-coherent-vram=off'),
                         ('x-i8-byte-order=on', 'x-i8-byte-order=off'),
                         ('via-pmu.osx-sleep=on', 'via-pmu.osx-sleep=off'),
                         ('ati-vga,', 'VGA,'), ('x-agp=on', 'x-agp=off')]:
            spec = copy.deepcopy(self.spec)
            spec['argv'] = [a.replace(old, new) for a in spec['argv']]
            await self.boundary(spec, False)

    async def test_ram_source_sync_is_requalified_at_process_boundary(self):
        spec = copy.deepcopy(self.spec)
        spec.update(guest_os='macosx-10.4', os_build='8S165')
        spec['argv'] = [a + ',x-depth-memory=on,x-selective-2d-sync=on,x-selective-2d-ram=on'
                        if a.startswith('ati-vga,') else a for a in spec['argv']]
        required = {osx_graphics.SELECTIVE_2D_FEATURE, osx_graphics.DEPTH_MEMORY_FEATURE,
                    osx_graphics.RAM_SOURCE_FEATURE, osx_graphics.TEXTURE_PAGE_FEATURE}
        features = self.features | required
        self.write_manifest({**self.manifest, 'features': sorted(features)})
        await self.boundary(spec, True)
        for feature in required | {osx_graphics.COHERENT_FEATURE}:
            self.write_manifest({**self.manifest, 'features': sorted(features-{feature})})
            await self.boundary(spec, False)
        self.write_manifest({**self.manifest, 'features': sorted(features)})
        await self.boundary({**spec, 'guest_os': 'macosx-10.3', 'os_build': '7W98'}, False)
        for old, new in [('x-selective-2d-ram=on', 'x-selective-2d-ram=off'),
                         ('x-selective-2d-ram=on', 'x-selective-2d-ram=on,x-selective-2d-ram=on'),
                         ('x-selective-2d-sync=on,', ''),
                         ('x-depth-memory=on,', '')]:
            changed = {**spec, 'argv': [a.replace(old, new) for a in spec['argv']]}
            await self.boundary(changed, False)

    async def test_yuv_ram_order_is_requalified_before_the_actual_process(self):
        spec = copy.deepcopy(self.spec)
        spec.update(guest_os='macosx-10.4', os_build='8S165')
        spec['argv'] = [a + ',x-yuv16-aperture-view=on,x-yuv-ram-byte-order=on'
                        if a.startswith('ati-vga,') else a for a in spec['argv']]
        required = {osx_graphics.YUV_RAM_FEATURE, osx_graphics.YUV_VIEW_FEATURE,
                    osx_graphics.TEXTURE_PAGE_FEATURE}
        features = self.features | required
        self.write_manifest({**self.manifest, 'features': sorted(features)})
        await self.boundary(spec, True)
        for feature in features:
            with self.subTest(missing=feature):
                self.write_manifest({**self.manifest, 'features': sorted(features-{feature})})
                await self.boundary(spec, False)
        self.write_manifest({**self.manifest, 'features': sorted(features)})
        for profile, build in [('macosx-10.3', '7W98'), ('macosx-10.4', '8S164'),
                               ('macosx-10.4', '7W98'), ('macosx-10.2', '6R73')]:
            await self.boundary({**spec, 'guest_os': profile, 'os_build': build}, False)
        for old, new in [('x-yuv-ram-byte-order=on', 'x-yuv-ram-byte-order=off'),
                         ('x-yuv-ram-byte-order=on',
                          'x-yuv-ram-byte-order=on,x-yuv-ram-byte-order=on'),
                         ('x-yuv16-aperture-view=on,', ''),
                         ('x-yuv16-aperture-view=on', 'x-yuv16-aperture-view=off'),
                         ('x-yuv16-aperture-view=on',
                          'x-yuv16-aperture-view=on,x-yuv16-aperture-view=on')]:
            with self.subTest(replacement=new):
                changed = {**spec, 'argv': [a.replace(old, new) for a in spec['argv']]}
                await self.boundary(changed, False)

    async def test_selective_sync_is_requalified_at_actual_process_boundary(self):
        spec = copy.deepcopy(self.spec)
        spec.update(guest_os='macosx-10.4', os_build='8S165')
        spec['argv'] = [a + ',x-depth-memory=on,x-selective-2d-sync=on'
                        if a.startswith('ati-vga,') else a for a in spec['argv']]
        features = self.features | {osx_graphics.SELECTIVE_2D_FEATURE, osx_graphics.DEPTH_MEMORY_FEATURE}
        self.write_manifest({**self.manifest, 'features': sorted(features)})
        await self.boundary(spec, True)
        for feature in (osx_graphics.SELECTIVE_2D_FEATURE, osx_graphics.DEPTH_MEMORY_FEATURE):
            self.write_manifest({**self.manifest, 'features': sorted(features-{feature})})
            await self.boundary(spec, False)
        self.write_manifest({**self.manifest, 'features': sorted(features)})
        await self.boundary({**spec, 'guest_os': 'macosx-10.3', 'os_build': '7W98'}, False)
        for old, new in [('x-depth-memory=on', 'x-depth-memory=off'),
                         ('x-selective-2d-sync=on', 'x-selective-2d-sync=off'),
                         ('x-selective-2d-sync=on', 'x-selective-2d-sync=on,x-selective-2d-sync=on')]:
            changed = {**spec, 'argv': [a.replace(old,new) for a in spec['argv']]}
            await self.boundary(changed, False)


class OSXShaderCache(IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp, self.root = helpers.scratch()
        self.addCleanup(self.temp.cleanup)
        self.cwd = self.root / 'machines' / 'tiger'
        self.cwd.mkdir(parents=True)
        self.cache = self.root / 'cache' / 'mesa-osx'
        self.events = mock.Mock()
        self.spec = {'session': 'cache-test', 'status': str(self.root/'status.json'),
                     'cwd': str(self.cwd), 'shader_cache': str(self.cache)}

    def launcher(self, **overrides):
        return Launcher({**self.spec, **overrides}, SimpleNamespace(event=self.events), verify=False)

    def test_cache_is_writable_private_and_reused_without_deleting_contents(self):
        env = self.launcher()._shader_cache_environment(None)
        self.assertEqual(env, {'MESA_SHADER_CACHE_DIR': str(self.cache),
                               'MESA_SHADER_CACHE_MAX_SIZE': '256M'})
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o700)
        (self.cache/'retained').write_bytes(b'cache sentinel')
        self.assertEqual(self.launcher()._shader_cache_environment(None), env)
        self.assertEqual((self.cache/'retained').read_bytes(), b'cache sentinel')

    def test_missing_cache_keeps_old_spec_compatible(self):
        self.assertEqual(self.launcher(shader_cache=None)._shader_cache_environment(None), {})
        self.assertFalse(self.cache.exists())

    def test_wrong_location_and_symlink_leave_other_files_untouched(self):
        outside = self.root/'other'
        outside.mkdir(mode=0o750)
        (outside/'sentinel').write_text('unchanged')
        self.assertEqual(self.launcher(shader_cache=str(outside))._shader_cache_environment(None), {})
        self.cache.parent.mkdir()
        self.cache.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.launcher()._shader_cache_environment(None), {})
        self.assertEqual(outside.stat().st_mode & 0o777, 0o750)
        self.assertEqual((outside/'sentinel').read_text(), 'unchanged')
        self.assertTrue(self.events.called)

    def test_parent_symlink_and_group_writable_parent_do_not_gain_ownership_changes(self):
        outside = self.root/'outside'
        outside.mkdir(mode=0o750)
        self.cache.parent.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.launcher()._shader_cache_environment(None), {})
        self.assertEqual(outside.stat().st_mode & 0o777, 0o750)
        self.assertFalse((outside/'mesa-osx').exists())
        self.cache.parent.unlink()
        self.cache.parent.mkdir(mode=0o770)
        self.cache.parent.chmod(0o770)
        self.assertEqual(self.launcher()._shader_cache_environment(None), {})
        self.assertFalse(self.cache.exists())
