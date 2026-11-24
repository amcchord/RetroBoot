"""Unit tests for the image workstream's pure-Python pieces (no root, no network).

    python3 -m unittest discover -s image/tests -v
    (the classifier test needs numpy + Pillow: run it with the lab venv)
"""
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

IMAGE = pathlib.Path(__file__).resolve().parents[1]


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def png_chunks(data):
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    pos, chunks = 8, {}
    while pos < len(data):
        length, kind = struct.unpack('>I4s', data[pos:pos + 8])
        chunks.setdefault(kind, b'')
        chunks[kind] += data[pos + 8:pos + 8 + length]
        pos += 12 + length
    return chunks


class CheckerboardTest(unittest.TestCase):
    def test_osx_fallback_is_the_measured_bootx_gray(self):
        tool = load(IMAGE / 'rootfs/usr/lib/retroboot/image/rb-checkerboard', 'osx_gray')
        chunks = png_chunks(tool.png(33, 21, 2, 'macosx'))
        self.assertEqual(chunks[b'PLTE'], bytes([192] * 6))
        self.assertEqual(zlib.decompress(chunks[b'IDAT']), bytes(34 * 21))

    def test_pattern_and_grub_compatible_format(self):
        tool = IMAGE / 'rootfs/usr/lib/retroboot/image/rb-checkerboard'
        for width, height, scale in ((17, 9, 1), (40, 30, 2), (33, 21, 3)):
            with tempfile.TemporaryDirectory() as tmp:
                out = pathlib.Path(tmp, 'c.png')
                subprocess.run([sys.executable, str(tool), str(width), str(height), str(scale), str(out)], check=True)
                chunks = png_chunks(out.read_bytes())
            w, h, depth, ctype, _, _, interlace = struct.unpack('>IIBBBBB', chunks[b'IHDR'])
            self.assertEqual((w, h, depth, ctype, interlace), (width, height, 8, 3, 0))  # GRUB: palette, 8 bit
            self.assertEqual(chunks[b'PLTE'], bytes([0, 0, 0, 255, 255, 255]))
            raw = zlib.decompress(chunks[b'IDAT'])
            for y in range(height):
                row = raw[y * (width + 1):(y + 1) * (width + 1)]
                self.assertEqual(row[0], 0)  # filter none
                for x in range(width):
                    white = (x // scale + y // scale) % 2 == 1   # ARCHITECTURE 4.4: black at the anchor
                    self.assertEqual(row[1 + x], 1 if white else 0, (x, y, scale))


class BlankFontTest(unittest.TestCase):
    def test_every_glyph_is_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp, 'blank.pf2')
            subprocess.run([sys.executable, str(IMAGE / 'scripts/make-blank-font.py'), str(out)], check=True)
            data = out.read_bytes()
        pos, sections = 0, {}
        while pos < len(data):
            name, length = data[pos:pos + 4], struct.unpack('>I', data[pos + 4:pos + 8])[0]
            if name == b'DATA':
                sections[name] = pos + 8
                break
            sections[name] = data[pos + 8:pos + 8 + length]
            pos += 8 + length
        self.assertEqual(sections[b'FILE'], b'PFF2')
        chix = sections[b'CHIX']
        entries = [struct.unpack('>IBI', chix[i:i + 9]) for i in range(0, len(chix), 9)]
        codes = [e[0] for e in entries]
        self.assertEqual(codes, sorted(codes))
        for needed in (ord(' '), ord('e'), ord("'"), 0x2018, 0xFFFD):
            self.assertIn(needed, codes)
        for _, _, offset in entries[:50]:
            w, h, _, _, advance = struct.unpack('>HHhhh', data[offset:offset + 10])
            bitmap = data[offset + 10:offset + 10 + (w * h + 7) // 8]
            self.assertEqual(bitmap, b'\x00' * len(bitmap))
            self.assertEqual(advance, 8)


class BootDisplayTest(unittest.TestCase):
    def setUp(self):
        self.mod = load(IMAGE / 'rootfs/usr/lib/retroboot/image/rb-bootdisplay', 'rb_bootdisplay')

    def test_osx_style_is_selected_per_mac(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            settings = root / 'settings.json'
            machines = root / 'machines'
            (machines / 'tiger').mkdir(parents=True)
            settings.write_text(json.dumps({'applied': {'machine.active': 'tiger'}}))
            meta = {'schema_version': 3, 'family': 'powermac', 'guest_os': 'macosx-10.4',
                    'template': {'machineFamily': 'powerMacOSX', 'guestOS': 'macosx-10.4'}}
            path = machines / 'tiger/machine.json'
            path.write_text(json.dumps(meta))
            self.assertEqual(self.mod.configured_style(settings, machines), 'macosx')
            # Next-start choice is independent of the Mac that is still running.
            (machines / 'classic').mkdir()
            (machines / 'classic/machine.json').write_text('{"schema_version":1}')
            settings.write_text(json.dumps({'applied': {'machine.active': 'tiger'},
                                             'desired': {'machine.active': 'classic'}}))
            self.assertEqual(self.mod.configured_style(settings, machines), 'classic')
            settings.write_text(json.dumps({'applied': {'machine.active': 'absent'},
                                             'desired': {'machine.active': 'tiger'}}))
            self.assertEqual(self.mod.configured_style(settings, machines), 'macosx')
            meta['schema_version'] = 1
            path.write_text(json.dumps(meta))
            self.assertEqual(self.mod.configured_style(settings, machines), 'classic')
            settings.write_text(json.dumps({'applied': {'machine.active': '../tiger'}}))
            self.assertEqual(self.mod.configured_style(settings, machines), 'classic')

    def test_first_raw_boot_style_needs_no_builder_framebuffer(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            machines = root / 'machines'
            (machines / 'tiger').mkdir(parents=True)
            (machines / 'tiger/machine.json').write_text(json.dumps({
                'schema_version': 3, 'family': 'powermac', 'guest_os': 'macosx-10.4',
                'template': {'machineFamily': 'powerMacOSX', 'guestOS': 'macosx-10.4'}}))
            seed = root / 'seed.json'
            seed.write_text('{"settings":{"machine.active":"tiger"}}')
            esp = root / 'esp'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.mod.main(['--initial', '--esp', str(esp), '--settings', str(seed),
                                                 '--machines', str(machines), '--boot-params', '/absent']), 0)
            rbdir = esp / 'grub/retroboot'
            self.assertIn('set rb_startup_style=macosx\n', (rbdir / 'display.cfg').read_text())
            self.assertIn('set rb_progress=off\n', (rbdir / 'display.cfg').read_text())
            self.assertEqual(png_chunks((rbdir / 'checker-default.png').read_bytes())[b'PLTE'], bytes([192] * 6))
            self.mod.initial_background(str(esp), 'classic')
            self.assertIn('set rb_startup_style=classic\n', (rbdir / 'display.cfg').read_text())
            self.assertNotEqual(png_chunks((rbdir / 'checker-default.png').read_bytes())[b'PLTE'], bytes([192] * 6))

    def test_suggested_scale_matches_architecture(self):
        cases = {(1024, 768): 1, (1366, 768): 1, (1280, 800): 1, (1920, 1080): 2, (2560, 1600): 2, (3840, 2160): 4}
        for (w, h), s in cases.items():
            self.assertEqual(self.mod.suggested_scale(w, h), s, (w, h))

    def test_png_format_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            for ctype, ok in ((0, False), (2, True), (3, True), (4, False), (6, True)):
                ihdr = struct.pack('>IIBBBBB', 4, 4, 8, ctype, 0, 0, 0)
                path = pathlib.Path(tmp, f'{ctype}.png')
                path.write_bytes(b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + ihdr + b'\0\0\0\0')
                self.assertEqual(self.mod.grub_readable_png(str(path)), ok, ctype)

    def test_boot_params_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = bytearray(0x40)
            raw[0x0f] = 0x70
            struct.pack_into('<HHH', raw, 0x12, 1280, 800, 32)
            path = pathlib.Path(tmp, 'bp')
            path.write_bytes(bytes(raw))
            self.assertEqual(self.mod.boot_framebuffer(str(path)),
                             {'type': 'efi', 'width': 1280, 'height': 800, 'depth': 32})
            raw[0x0f] = 0x01   # text mode
            path.write_bytes(bytes(raw))
            self.assertIsNone(self.mod.boot_framebuffer(str(path)))


class PixelScaleBootTest(unittest.TestCase):
    """display.pixel_scale persists through boot: rb-bootdisplay draws GRUB's
    checkerboard at the configured scale and GRUB passes it to Linux (4.4/4.5)."""

    def setUp(self):
        self.mod = load(IMAGE / 'rootfs/usr/lib/retroboot/image/rb-bootdisplay', 'rb_bootdisplay_ps')
        self.mod.PRESENTER = '/nonexistent/rb-presenter'   # use the image's own writer
        self.mod.FALLBACK = str(IMAGE / 'rootfs/usr/lib/retroboot/image/rb-checkerboard')

    def test_effective_scale_matches_presenter(self):
        # (mode, configured) -> scale in effect; same table as presenter/tests/test_scale.c
        cases = {((1920, 1080), 'auto'): 2, ((1920, 1080), '1'): 1, ((1920, 1080), '4'): 2,
                 ((2560, 1440), '3'): 3, ((2560, 1440), '4'): 3, ((3840, 2160), '1'): 1,
                 ((3840, 2160), 'auto'): 4, ((1280, 800), '4'): 1, ((5120, 2880), '1'): 2}
        for ((w, h), ps), want in cases.items():
            self.assertEqual(self.mod.effective_scale(w, h, ps), want, (w, h, ps))

    def test_configured_pixel_scale_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = pathlib.Path(tmp, 'settings.json')
            store.write_text('{"schema": 1, "applied": {"display.pixel_scale": "3"}, "desired": {"display.pixel_scale": "1"}}')
            self.assertEqual(self.mod.configured_pixel_scale(str(store)), '3')   # applied, not desired
            seed = pathlib.Path(tmp, 'install-seed.json')
            seed.write_text('{"settings": {"display.pixel_scale": "2"}}')
            self.assertEqual(self.mod.configured_pixel_scale(str(seed)), '2')
            store.write_text('{"applied": {"display.pixel_scale": "9"}}')
            self.assertEqual(self.mod.configured_pixel_scale(str(store)), 'auto')
            store.write_text('not json')
            self.assertEqual(self.mod.configured_pixel_scale(str(store)), 'auto')
            self.assertEqual(self.mod.configured_pixel_scale(str(pathlib.Path(tmp, 'missing'))), 'auto')

    def test_ati_radeon_starts_at_640x480(self):
        """hostd's state/graphics.json: with the ATI Radeon Mac OS starts at ATI's own
        640x480, so GRUB's startup indicator and anchor use that guest layout, but only
        while the applied settings still ask for the card (ARCHITECTURE 4.9)."""
        with tempfile.TemporaryDirectory() as tmp:
            hint = pathlib.Path(tmp, 'graphics.json')
            store = pathlib.Path(tmp, 'settings.json')
            store.write_text('{"applied": {"display.graphics": "ati-radeon", "display.guest_size": "auto"}}')
            self.assertIsNone(self.mod.startup_card_size(str(hint), str(store)))       # no hint
            hint.write_text('{"card": "ati-radeon", "machine": "m", "startup_guest_size": "640x480"}')
            self.assertEqual(self.mod.startup_card_size(str(hint), str(store)), '640x480')
            store.write_text('{"applied": {"display.graphics": "standard"}}')
            self.assertIsNone(self.mod.startup_card_size(str(hint), str(store)))       # turned off since
            store.write_text('{"applied": {"display.graphics": "ati-radeon"}}')
            hint.write_text('{"card": "standard", "startup_guest_size": null}')
            self.assertIsNone(self.mod.startup_card_size(str(hint), str(store)))       # fell back to std-VGA
            hint.write_text('{"card": "ati-radeon", "startup_guest_size": "9x9"}')
            self.assertIsNone(self.mod.startup_card_size(str(hint), str(store)))       # not a guest size
            hint.write_text('not json')
            self.assertIsNone(self.mod.startup_card_size(str(hint), str(store)))

    def run_tool(self, tmp, width, height, settings_value):
        raw = bytearray(0x40)
        raw[0x0f] = 0x23   # VESA (BIOS): rb_gfxmode recorded too
        struct.pack_into('<HHH', raw, 0x12, width, height, 32)
        bp = pathlib.Path(tmp, 'bp')
        bp.write_bytes(bytes(raw))
        store = pathlib.Path(tmp, 'settings.json')
        store.write_text('{"applied": {"display.pixel_scale": "%s"}}' % settings_value)
        esp = pathlib.Path(tmp, 'esp')
        rc = self.mod.main(['--esp', str(esp), '--boot-params', str(bp), '--settings', str(store)])
        self.assertEqual(rc, 0)
        return esp / 'grub/retroboot'

    def test_display_cfg_and_checkerboard_follow_the_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.run_tool(tmp, 96, 64, 'auto')
            cfg = (out / 'display.cfg').read_text()
            self.assertIn('set rb_scale=1\n', cfg)
            self.assertIn('set rb_pixel_scale=auto\n', cfg)
            self.assertIn('set rb_gfxmode=96x64\n', cfg)
            # A real mode: 1920x1080 at 1x (auto would be 2x), then 2x; the old PNG goes.
            for ps, scale in (('1', 1), ('2', 2), ('4', 2)):
                out = self.run_tool(tmp, 1920, 1080, ps)
                cfg = (out / 'display.cfg').read_text()
                self.assertIn(f'set rb_scale={scale}\n', cfg)
                self.assertIn(f'set rb_pixel_scale={ps}\n', cfg)
                name = f'checker-1920x1080-s{scale}.png'
                self.assertIn(f'set rb_checker=/grub/retroboot/{name}\n', cfg)
                self.assertEqual(sorted(p.name for p in out.glob('checker-*.png')), [name])
                chunks = png_chunks((out / name).read_bytes())
                raw = zlib.decompress(chunks[b'IDAT'])
                for y in (0, scale - 1, scale, 1079):
                    row = raw[y * 1921 + 1:(y + 1) * 1921]
                    for x in (0, scale - 1, scale, 1919):
                        # black at the anchor (4.4; the fallback anchors at the origin)
                        self.assertEqual(row[x], 0 if (x // scale + y // scale) % 2 == 0 else 1, (ps, x, y))

    def test_grub_passes_the_pixel_scale_to_linux(self):
        cfg = (IMAGE / 'grub/grub.cfg.in').read_text()
        default = cfg.index('set rb_pixel_scale=auto')
        source = cfg.index('source /grub/retroboot/display.cfg')
        self.assertLess(default, source)
        # every kernel line (the A/B slot entry and its fallback, ARCHITECTURE 13)
        linux = [l for l in cfg.splitlines() if l.strip().startswith(('linux ', 'if linux '))]
        self.assertGreaterEqual(len(linux), 1)
        for line in linux:
            self.assertIn(' retroboot.pixel_scale=$rb_pixel_scale ', line + ' ')
            self.assertIn('$rb_startup_args', line)
        # ... and the startup indicator's parameters (4.4.1): defaults before display.cfg,
        # gathered above the first repaint (entries below it only reference them)
        args = cfg.index('set rb_startup_args=')
        self.assertLess(source, args)
        self.assertLess(args, cfg.index('echo -n " "'))
        for var, param in (('rb_progress', 'progress'), ('rb_guest_size', 'guest_size'),
                           ('rb_checker_scale', 'checker_scale'), ('rb_startup_ms', 'startup_ms'),
                           ('rb_rotation', 'rotation')):
            self.assertLess(cfg.index(f'set {var}='), source, var)
            self.assertIn(f'retroboot.{param}=${var}', cfg[args:cfg.index('\n', args)])
        self.assertIn('set rb_progress=off', cfg)   # no indicator without rb-bootdisplay's background
        # the main initrd always; the art cpio only when the file exists
        initrds = [l.strip() for l in cfg.splitlines() if l.strip().startswith(('initrd ', 'if initrd '))]
        self.assertTrue(initrds)
        for line in initrds:
            self.assertRegex(line, r'initrd \$rb_ucode \(\$rb_disk,\$rb_(other_)?part\)/boot/initrd\.img \$rb_initrd_extra')
        # early microcode (hostboot): the slot's own set, chosen right before each initrd
        lines = [l.strip() for l in cfg.splitlines()]
        for i, line in enumerate(lines):
            if line.startswith(('initrd ', 'if initrd ')):
                part = '$rb_other_part' if '$rb_other_part' in line else '$rb_part'
                self.assertEqual(lines[i - 1], f'rb_ucode_of {part}')
        self.assertIn('if [ -f "$rb_startup_initrd" ]; then set rb_initrd_extra="$rb_startup_initrd"; fi', cfg)


class StartupIndicatorBootTest(unittest.TestCase):
    """The startup indicator in GRUB's background (ARCHITECTURE 4.4.1): settings,
    the presenter's display.json, the learned cut-over time with hysteresis,
    the extra initrd with the Platinum art, and a read-only ESP."""

    def setUp(self):
        self.mod = load(IMAGE / 'rootfs/usr/lib/retroboot/image/rb-bootdisplay', 'rb_bootdisplay_si')
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        # A stand-in for rb-presenter --write-checkerboard: a valid PNG (the image's
        # own writer) and the JSON report the real one prints.
        stub = self.dir / 'rb-presenter'
        stub.write_text(
            '#!/usr/bin/env python3\n'
            'import json, subprocess, sys\n'
            'a = sys.argv\n'
            'w, h, s, out = a[2], a[3], a[4], a[5]\n'
            'opts = dict(zip(a[6::2], a[7::2]))\n'
            f'subprocess.run([{str(IMAGE / "rootfs/usr/lib/retroboot/image/rb-checkerboard")!r}, w, h, s, out], check=True)\n'
            'open(sys.argv[0] + ".log", "a").write(json.dumps(a[6:]) + "\\n")\n'
            'cs = 3 * int(s) if opts.get("--mm") == "133x75" else int(s)\n'
            'print(json.dumps({"png": out, "checker_scale": cs, "progress": opts.get("--progress", "off"),\n'
            '                  "art": "macos" if "--art" in opts else "builtin", "indicator": [1, 2, 3]}))\n')
        stub.chmod(0o755)
        self.mod.PRESENTER = str(stub)
        self.mod.FALLBACK = str(IMAGE / 'rootfs/usr/lib/retroboot/image/rb-checkerboard')
        self.stub_log = pathlib.Path(str(stub) + '.log')

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, w=1280, h=720, settings=None, display=None, timing=None, art='none', esp=None):
        raw = bytearray(0x40)
        raw[0x0f] = 0x70
        struct.pack_into('<HHH', raw, 0x12, w, h, 32)
        bp = self.dir / 'bp'
        bp.write_bytes(bytes(raw))
        store = self.dir / 'settings.json'
        store.write_text(json.dumps({'applied': settings or {}}))
        disp = self.dir / 'display.json'
        disp.write_text(json.dumps(display or {}))
        tim = self.dir / 'timing.json'
        tim.write_text(json.dumps(timing or {}))
        esp = esp or self.dir / 'esp'
        rc = self.mod.main(['--esp', str(esp), '--boot-params', str(bp), '--settings', str(store),
                            '--display', str(disp), '--timing', str(tim), '--art', str(art)])
        self.assertEqual(rc, 0)
        out = esp / 'grub/retroboot'
        return out, (out / 'display.cfg').read_text() if (out / 'display.cfg').exists() else ''

    def test_settings_and_defaults(self):
        store = self.dir / 's.json'
        store.write_text('{"applied": {"startup.progress_indicator": false, "display.guest_size": "800x600"}}')
        self.assertEqual(self.mod.configured_startup(str(store)), (False, '800x600'))
        store.write_text('{"settings": {"display.guest_size": "13x7"}}')
        self.assertEqual(self.mod.configured_startup(str(store)), (True, 'auto'))
        self.assertEqual(self.mod.configured_startup(str(self.dir / 'missing')), (True, 'auto'))

    def test_indicator_dpi_and_rotation_reach_grub(self):
        display = {'scanout': [1280, 720], 'physical_mm': [133, 75], 'physical_mm_source': 'quirk', 'rotation': 0}
        out, cfg = self.run_tool(display=display)
        self.assertIn('set rb_progress=on\n', cfg)
        self.assertIn('set rb_checker_scale=3\n', cfg)
        self.assertIn('set rb_guest_size=auto\n', cfg)
        self.assertIn('set rb_startup_ms=0\n', cfg)
        self.assertNotIn('rb_startup_initrd', cfg)
        args = json.loads(self.stub_log.read_text().splitlines()[-1])
        self.assertEqual(dict(zip(args[::2], args[1::2]))['--mm'], '133x75')
        # unchanged inputs: nothing rewritten
        mtime = (out / 'display.cfg').stat().st_mtime_ns
        self.run_tool(display=display)
        self.assertEqual((out / 'display.cfg').stat().st_mtime_ns, mtime)
        self.assertEqual(len(self.stub_log.read_text().splitlines()), 1)
        # a display.json for another scanout is ignored (DPI unknown)
        _, cfg = self.run_tool(display={'scanout': [1920, 1080], 'physical_mm': [133, 75],
                                        'physical_mm_source': 'drm'})
        self.assertIn('set rb_checker_scale=1\n', cfg)
        # sideways panel: the scale belongs to the rotated frame, the rotation reaches rb-presenter
        self.run_tool(w=720, h=1280, display={'scanout': [720, 1280], 'physical_mm': [75, 133],
                                              'physical_mm_source': 'quirk', 'rotation': 90})
        args = json.loads(self.stub_log.read_text().splitlines()[-1])
        self.assertEqual(dict(zip(args[::2], args[1::2]))['--rotation'], '90')
        # off: no indicator
        _, cfg = self.run_tool(settings={'startup.progress_indicator': False})
        self.assertIn('set rb_progress=off\n', cfg)

    def test_user_rotation_reaches_grub(self):
        """display.rotation (or auto's orientation; ARCHITECTURE 4.5): the background is
        drawn turned (rotation = panel orientation + user rotation) and GRUB passes
        the user rotation, so the splash and the presenter start turned."""
        _, cfg = self.run_tool(display={'scanout': [1280, 720], 'rotation': 0})
        self.assertIn('set rb_rotation=0\n', cfg)
        out, cfg = self.run_tool(display={'scanout': [1280, 720], 'rotation': 90, 'user_rotation': 90})
        self.assertIn('set rb_rotation=90\n', cfg)
        args = dict(zip(*[iter(json.loads(self.stub_log.read_text().splitlines()[-1]))] * 2))
        self.assertEqual(args['--rotation'], '90')
        # the GPD MicroPC (panel right side up) turned to portrait: 90 + 90
        _, cfg = self.run_tool(w=720, h=1280, display={'scanout': [720, 1280], 'rotation': 180,
                                                       'user_rotation': 90})
        self.assertIn('set rb_rotation=90\n', cfg)
        args = dict(zip(*[iter(json.loads(self.stub_log.read_text().splitlines()[-1]))] * 2))
        self.assertEqual(args['--rotation'], '180')
        # nonsense is ignored
        _, cfg = self.run_tool(display={'scanout': [1280, 720], 'rotation': 90, 'user_rotation': 45})
        self.assertIn('set rb_rotation=0\n', cfg)

    def test_portrait_scale_rules_match_presenter(self):
        # presenter/tests/test_touch.c test_portrait_rules
        self.assertEqual(self.mod.suggested_scale(1080, 1920), 2)
        self.assertEqual(self.mod.suggested_scale(720, 1280), 1)
        self.assertTrue(self.mod.pixel_scale_valid(1080, 1920, 2))
        self.assertFalse(self.mod.pixel_scale_valid(1080, 1920, 3))
        self.assertFalse(self.mod.pixel_scale_valid(960, 1280, 2))
        self.assertEqual(self.mod.effective_scale(1080, 1920, 'auto'), 2)

    def test_new_pattern_anchoring_regenerates_an_installed_background(self):
        # An ESP written before the pattern was anchored at Mac OS's picture
        # (ARCHITECTURE 4.4) records no pattern version: the next run redraws it.
        out, cfg = self.run_tool()
        self.assertIn('"pattern": %d' % self.mod.PATTERN_VERSION, cfg)
        old = '\n'.join(l.replace(', "pattern": %d' % self.mod.PATTERN_VERSION, '') if l.startswith('# inputs') else l
                        for l in cfg.split('\n'))
        (out / 'display.cfg').write_text(old)
        runs = len(self.stub_log.read_text().splitlines())
        _, cfg = self.run_tool()
        self.assertEqual(len(self.stub_log.read_text().splitlines()), runs + 1)
        self.assertIn('"pattern": %d' % self.mod.PATTERN_VERSION, cfg)
        # and then it stays
        self.run_tool()
        self.assertEqual(len(self.stub_log.read_text().splitlines()), runs + 1)

    def test_learned_time_with_hysteresis(self):
        f = self.dir / 't.json'
        f.write_text('{"cold_ms": 41120.7}')
        self.assertEqual(self.mod.learned_startup_ms(str(f), 0), 41000)
        self.assertEqual(self.mod.learned_startup_ms(str(f), 40000), 40000)    # within 1.5 s
        f.write_text('{"cold_ms": 52000}')
        self.assertEqual(self.mod.learned_startup_ms(str(f), 41000), 52000)    # moved > 15 %
        f.write_text('{"cold_ms": 99999999}')
        self.assertEqual(self.mod.learned_startup_ms(str(f), 41000), 41000)
        self.assertEqual(self.mod.learned_startup_ms('none', 7), 7)
        _, cfg = self.run_tool(timing={'cold_ms': 13870})
        self.assertIn('set rb_startup_ms=13750\n', cfg)
        _, cfg = self.run_tool(timing={'cold_ms': 14300})   # small change: kept
        self.assertIn('set rb_startup_ms=13750\n', cfg)

    def test_bundle_art_travels_as_an_initrd(self):
        art = self.dir / 'overview-32.rbi'
        art.write_bytes(b'RBI1' + bytes([32, 0, 32, 0]) + bytes(4096))
        out, cfg = self.run_tool(art=art)
        line = [l for l in cfg.splitlines() if l.startswith('set rb_startup_initrd=')]
        self.assertEqual(len(line), 1)
        cpio = out / line[0].split('/')[-1]
        data = cpio.read_bytes()
        self.assertEqual(len(data) % 4, 0)
        # parse the newc archive back
        names, files, off = [], {}, 0
        while True:
            hdr = data[off:off + 110]
            self.assertEqual(hdr[:6], b'070701')
            f = [int(hdr[6 + i * 8:14 + i * 8], 16) for i in range(13)]
            size, namesize = f[6], f[11]
            name = data[off + 110:off + 110 + namesize - 1].decode()
            off = off + 110 + namesize
            off += -off % 4
            files[name] = data[off:off + size]
            off += size
            off += -off % 4
            names.append(name)
            if name == 'TRAILER!!!':
                break
        self.assertEqual(names, ['retroboot', 'retroboot/startup-icon.rbi', 'TRAILER!!!'])
        self.assertEqual(files['retroboot/startup-icon.rbi'], art.read_bytes())
        # without the art the cpio goes away
        out, cfg = self.run_tool()
        self.assertNotIn('rb_startup_initrd', cfg)
        self.assertEqual(list(out.glob('startup-*.cpio')), [])

    def test_read_only_esp_never_fails(self):
        esp = self.dir / 'ro-esp'
        (esp / 'grub').mkdir(parents=True)
        (esp / 'grub').chmod(0o555)
        try:
            if os.access(esp / 'grub', os.W_OK):
                self.skipTest('running as root: permissions are not enforced')
            _, cfg = self.run_tool(esp=esp)
            self.assertEqual(cfg, '')
        finally:
            (esp / 'grub').chmod(0o755)


try:
    import numpy as np  # noqa: F401
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False


@unittest.skipUnless(HAVE_NUMPY, 'needs numpy/Pillow (lab venv)')
class ClassifierTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(IMAGE / 'lab'))
        sys.path.insert(0, str(IMAGE.parent))
        import boot_capture
        self.classify = boot_capture.classify

    def checker(self, w=320, h=200, s=1):
        import numpy as np
        y, x = np.mgrid[0:h, 0:w]
        white = ((x // s + y // s) % 2 == 0)
        return np.repeat((white * 255).astype(np.uint8)[:, :, None], 3, axis=2)

    def test_startup_indicator_is_not_a_stray(self):
        # The Happy Mac (25 x 32 Mac pixels) where Mac OS draws it: the indicator.
        for k in (1, 2):
            a = self.checker(1280, 800, 1).copy()
            x0, y0 = 624 + 3 * k - (k - 1) * 16, 384 - (k - 1) * 16
            a[y0:y0 + 32 * k, x0:x0 + 25 * k] = (204, 204, 204)
            info = self.classify(a)
            self.assertEqual(info['kind'], 'checkerboard')
            self.assertEqual(info['anomaly_pixels'], 0)
            self.assertIn('indicator_bbox', info)
        # the same block in a corner is a stray
        a = self.checker(1280, 800, 1).copy()
        a[10:42, 10:35] = (204, 204, 204)
        info = self.classify(a)
        self.assertGreater(info['anomaly_pixels'], 0)
        self.assertNotIn('indicator_bbox', info)

    def test_checkerboard_black_panel_and_cursor(self):
        import numpy as np
        board = self.checker()
        self.assertEqual(self.classify(board)['kind'], 'checkerboard')
        self.assertEqual(self.classify(self.checker(s=2))['checker_scale'], 2)
        # anchored at Mac OS's picture (4.4): any phase and cell offset
        shifted = self.checker(323, 200, 2)[:, 3:]
        info = self.classify(shifted)
        self.assertEqual((info['kind'], info['checker_scale'], info['anomaly_pixels']), ('checkerboard', 2, 0))
        self.assertEqual(self.classify(255 - board)['kind'], 'checkerboard')
        self.assertEqual(self.classify(np.zeros_like(board))['kind'], 'black')
        cursor = board.copy()
        cursor[10:20, 10:16] = 0          # a small cursor-sized blot stays a flagged anomaly
        info = self.classify(cursor)
        self.assertEqual(info['kind'], 'checkerboard')
        self.assertGreater(info['anomaly_pixels'], 0)
        panel = board.copy()
        panel[60:140, 80:240] = 220       # a dialog over the checkerboard
        self.assertEqual(self.classify(panel)['kind'], 'panel')
        gray = np.full_like(board, 136)
        self.assertEqual(self.classify(gray)['kind'], 'flat-gray')


class MachineJsonTest(unittest.TestCase):
    """make-machine-json.py: a registered guest fixture names the machine record."""

    def run_tool(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = pathlib.Path(tmp.name, 'os921-full')
        d.mkdir()
        for name, text in files.items():
            (d / name).write_text(text)
        subprocess.run([sys.executable, str(IMAGE / 'scripts/make-machine-json.py'), str(d), 'os921-full',
                        str(IMAGE.parent / 'hostd')], check=True, capture_output=True)
        import json
        return json.loads((d / 'machine.json').read_text())

    def test_fixture_json_supplies_name_version_and_source(self):
        cfg = '{"name": "Mac OS 9", "width": 1024, "height": 768, "machineFamily": "powerMacG4", "ramMB": 512}'
        fx = ('{"id": "os921-full", "name": "Mac OS 9.2.1", "os_version": "9.2.1", '
              '"disk_sha256": "' + 'ab' * 32 + '"}')
        meta = self.run_tool({'config.json': cfg, 'fixture.json': fx})
        self.assertEqual((meta['id'], meta['name'], meta['os_version']), ('os921-full', 'Mac OS 9.2.1', '9.2.1'))
        self.assertEqual(meta['source'], {'kind': 'image-build', 'fixture': 'os921-full', 'disk_sha256': 'ab' * 32})
        self.assertEqual(meta['template']['ramMB'], 512)
        self.assertIsNone(meta['prep'])

    def test_without_fixture_json_keeps_the_old_record(self):
        meta = self.run_tool({'config.json': '{"name": "Mac OS 9", "machineFamily": "powerMacG4"}'})
        self.assertEqual((meta['name'], meta['os_version']), ('Mac OS 9', None))
        self.assertEqual(meta['source'], {'kind': 'image-build', 'fixture': 'os921-full'})

    def test_bad_version_is_dropped(self):
        meta = self.run_tool({'config.json': '{}', 'fixture.json': '{"os_version": "9.2.1; rm -rf /"}'})
        self.assertIsNone(meta['os_version'])


class InstallerBluetoothTest(unittest.TestCase):
    """install-to-disk --bluetooth: BlueZ's pairing store reaches RBDATA/bluetooth, private."""

    def test_copy_bluetooth(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = pathlib.Path(tmp.name)
        itd = load(IMAGE / 'rootfs/usr/lib/retroboot/installer/install-to-disk', 'install_to_disk_bt')
        src, data = d / 'var-lib-bluetooth', d / 'rbdata'
        dev = src / '00:1A:7D:DA:71:13' / 'C8:2B:96:1A:4E:01'
        dev.mkdir(parents=True)
        (dev / 'info').write_text('[General]\nName=K380\n[LinkKey]\nKey=00112233445566778899AABBCCDDEEFF\n')
        (dev / 'attributes').write_text('[0x0001]\n')
        (src / '00:1A:7D:DA:71:13' / 'settings').write_text('[General]\nDiscoverable=false\n')
        (src / '00:1A:7D:DA:71:13' / 'cache').mkdir()
        (src / '00:1A:7D:DA:71:13' / 'cache' / 'C8:2B:96:1A:4E:01').write_text('[General]\nName=K380\n')
        (src / 'stray.txt').write_text('not copied')
        (src / 'not-an-adapter').mkdir()
        (src / 'not-an-adapter' / 'x').write_text('not copied')
        os.symlink('/etc/passwd', dev / 'link')
        data.mkdir()
        self.assertEqual(itd.copy_bluetooth(str(src), str(data)), 1)
        out = data / 'bluetooth'
        got = sorted(str(p.relative_to(out)) for p in out.rglob('*') if p.is_file())
        self.assertEqual(got, ['00:1A:7D:DA:71:13/C8:2B:96:1A:4E:01/attributes', '00:1A:7D:DA:71:13/C8:2B:96:1A:4E:01/info',
                               '00:1A:7D:DA:71:13/cache/C8:2B:96:1A:4E:01', '00:1A:7D:DA:71:13/settings'])
        self.assertEqual((out / '00:1A:7D:DA:71:13/C8:2B:96:1A:4E:01/info').read_text(), (dev / 'info').read_text())
        for p in [out, *out.rglob('*')]:
            self.assertEqual(p.stat().st_mode & 0o777, 0o700 if p.is_dir() else 0o600, p)


class InstallerCopyTest(unittest.TestCase):
    """install-to-disk sparse_copy and make-extents.py: only the disk's data is read and written."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = pathlib.Path(tmp.name)
        self.itd = load(IMAGE / 'rootfs/usr/lib/retroboot/installer/install-to-disk', 'install_to_disk')
        self.src = self.d / 'disk.img'
        with open(self.src, 'wb') as f:
            f.truncate(64 << 20)
            for off, data in ((0, b'LK' + os.urandom(3000)), (8192, bytes(4096) + b'x'), (5 << 20, os.urandom(70000)),
                              ((64 << 20) - 512, b'END')):
                f.seek(off)
                f.write(data)
        self.want = self.src.read_bytes()

    def copy(self, name):
        dst = self.d / name
        how, wrote = self.itd.sparse_copy(str(self.src), str(dst), lambda d, s: None)
        self.assertEqual(dst.read_bytes(), self.want)
        self.assertLessEqual(wrote, 4096 * 23)                 # whole 4 KiB data blocks only
        return how

    def test_scan_without_map(self):
        self.assertEqual(self.copy('a.img'), 'scan')

    def test_build_map_is_used_and_checked(self):
        subprocess.run([sys.executable, str(IMAGE / 'scripts/make-extents.py'), str(self.src),
                        str(IMAGE.parent / 'hostd')], check=True, capture_output=True)
        side = pathlib.Path(str(self.src) + '.extents')
        doc = json.loads(side.read_text())
        self.assertEqual((doc['format'], doc['size']), ('retroboot-extents-1', 64 << 20))
        self.assertEqual(self.copy('b.img'), 'extent map')
        good = side.read_text()
        bad = [dict(doc, size=doc['size'] - 512), dict(doc, mtime=doc['mtime'] - 1),
               dict(doc, extents=[[4096, 4096], [0, 4096]]), dict(doc, extents=[[0, 4096], [2048, 4096]]),
               dict(doc, extents=[[100, 512]]), dict(doc, extents=[[(64 << 20) - 512, 1024]]),
               dict(doc, extents=[[0, 0]]), dict(doc, format='other'), dict(doc, extents='0-4096')]
        for i, d in enumerate(bad):
            with self.subTest(case=i):
                side.write_text(json.dumps(d))
                self.assertIsNone(self.itd.load_extents(str(self.src)))
                self.assertEqual(self.copy(f'c{i}.img'), 'scan')
        side.write_text('not json')
        self.assertIsNone(self.itd.load_extents(str(self.src)))
        side.write_text(good)
        self.assertIsNotNone(self.itd.load_extents(str(self.src)))


def _gnu_cp():
    """stage-embedded.sh copies sparsely with GNU cp (the builder; macOS cp lacks --sparse)."""
    with tempfile.TemporaryDirectory() as t:
        src = pathlib.Path(t) / 'a'
        src.write_bytes(b'x')
        return subprocess.run(['cp', '--sparse=always', '--reflink=never', str(src), str(src) + '.b'],
                              capture_output=True).returncode == 0


def _machine_dir(parent, name, mid=None, config_name=None, data=b'MAC'):
    d = parent / name
    d.mkdir(parents=True)
    with open(d / 'disk.img', 'wb') as f:
        f.truncate(8 << 20)
        f.seek(1 << 20)
        f.write(data)
    if mid:
        (d / 'machine.json').write_text(json.dumps({'schema_version': 1, 'id': mid, 'name': f'Mac {mid}',
                                                    'disk': 'disk.img'}))
    (d / 'config.json').write_text(json.dumps({'name': config_name or f'Mac {name}', 'machineFamily': 'powerMacG4'}))
    return d


class InstallerMachinesTest(unittest.TestCase):
    """install-to-disk with several --machine-dir: unique ids, one seed, no data maps or stale seeds."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = pathlib.Path(tmp.name)
        self.itd = load(IMAGE / 'rootfs/usr/lib/retroboot/installer/install-to-disk', 'install_to_disk_many')
        self.lines = []
        self.report = lambda phase, frac, msg, **extra: self.lines.append(dict(extra, phase=phase, message=msg))

    def test_a_68k_mac_keeps_its_rom_and_family(self):
        """A 68k Mac directory with only config.json: machine.json names its family, ROM and PRAM, and
        every file of the directory (the ROM included) is installed."""
        q = self.d / 'src/q'
        q.mkdir(parents=True)
        with open(q / 'disk.img', 'wb') as f:
            f.truncate(1 << 20)
        (q / 'config.json').write_text(json.dumps({'name': 'Mac OS 8.1', 'machineFamily': 'quadra800'}))
        (q / 'Quadra800.rom').write_bytes(b'stand-in ROM bytes (not Apple code)')
        data = self.d / 'rbdata'
        done = self.itd.install_machines([str(q)], str(data), None, None, report=self.report)
        self.assertEqual(done, [(str(q), 'mac-os-8-1')])
        meta = json.loads((data / 'machines/mac-os-8-1/machine.json').read_text())
        self.assertEqual((meta['family'], meta['rom'], meta['pram'], meta['schema_version']),
                         ('quadra800', 'Quadra800.rom', 'pram.img', 2))
        self.assertEqual((data / 'machines/mac-os-8-1/Quadra800.rom').read_bytes(), b'stand-in ROM bytes (not Apple code)')

    def test_osx_machine_format_and_first_boot_library(self):
        from unittest import mock
        source = _machine_dir(self.d, 'tiger')
        (source / 'config.json').write_text(json.dumps({'name': 'Tiger', 'machineFamily': 'powerMacOSX',
                                                       'guestOS': 'macosx-10.4'}))
        (source / 'nvram.img').write_bytes(b'private test startup-memory placeholder')
        data = self.d / 'rbdata'
        seed = self.d / 'seed.json'
        seed.write_text(json.dumps({'settings': {'machine.active': 'tiger'}}))
        done = self.itd.install_machines([str(source)], str(data), None, str(seed), report=self.report)
        self.assertEqual(done, [(str(source), 'tiger')])
        meta = json.loads((data / 'machines/tiger/machine.json').read_text())
        self.assertEqual((meta['schema_version'], meta['guest_os'], meta['family']), (3, 'macosx-10.4', 'powermac'))
        with mock.patch.object(self.itd, 'run') as run:
            self.itd.record_installed_boot_display('/test-esp', str(data), str(seed))
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index('--machines') + 1], str(data / 'machines'))
        self.assertEqual(argv[argv.index('--settings') + 1], str(data / 'config/install-seed.json'))
        (source / 'config.json').write_text('{"machineFamily":"powerMacG4","guestOS":"macosx-10.4"}')
        with self.assertRaises(self.itd.InstallError):
            self.itd.machine_meta(str(source), None)

    def test_several_machines_and_seed(self):
        a = _machine_dir(self.d / 'src', 'a', mid='os922-deluxe-v4')
        b = _machine_dir(self.d / 'src', 'b', config_name='Mac OS 9')          # config.json only
        c = _machine_dir(self.d / 'src', 'c', mid='mac-os-9')                  # the same id as b's
        (a / 'disk.img.extents').write_text('{}')                              # build-time map: not installed
        (a / 'install-seed.json').write_text('{"settings": {"machine.active": "stale"}}')
        seed = self.d / 'seed.json'
        seed.write_text(json.dumps({'settings': {'machine.active': 'os922-deluxe-v4', 'system.timezone': 'UTC'}}))
        data = self.d / 'rbdata'
        done = self.itd.install_machines([str(a), str(b), str(c)], str(data), None, str(seed), report=self.report)
        self.assertEqual(done, [(str(a), 'os922-deluxe-v4'), (str(b), 'mac-os-9'), (str(c), 'mac-os-9-2')])
        self.assertEqual(sorted(os.listdir(data / 'machines')), ['mac-os-9', 'mac-os-9-2', 'os922-deluxe-v4'])
        self.assertEqual(sorted(os.listdir(data / 'machines/os922-deluxe-v4')),
                         ['config.json', 'disk.img', 'machine.json', 'state.json'])
        self.assertEqual(json.loads((data / 'machines/mac-os-9-2/machine.json').read_text())['id'], 'mac-os-9-2')
        self.assertEqual(json.loads((data / 'machines/mac-os-9/machine.json').read_text())['name'], 'Mac OS 9')
        self.assertEqual((data / 'machines/mac-os-9-2/disk.img').read_bytes(), (c / 'disk.img').read_bytes())
        self.assertEqual(json.loads((data / 'config/install-seed.json').read_text())['settings'],
                         {'machine.active': 'os922-deluxe-v4', 'system.timezone': 'UTC'})
        finished = [l for l in self.lines if 'machine_id' in l]
        self.assertEqual([(l['machine_dir'], l['machine_id']) for l in finished],
                         [(str(a), 'os922-deluxe-v4'), (str(b), 'mac-os-9'), (str(c), 'mac-os-9-2')])
        self.assertIn('(2 of 3)', ' '.join(l['message'] for l in self.lines))

    def test_seed_follows_a_renamed_startup_mac(self):
        a = _machine_dir(self.d / 'src', 'a', mid='mac-os-9')
        data = self.d / 'rbdata'
        (data / 'machines/mac-os-9').mkdir(parents=True)                         # already on the disk
        seed = self.d / 'seed.json'
        seed.write_text(json.dumps({'settings': {'machine.active': 'mac-os-9'}}))
        self.assertEqual(self.itd.install_machines([str(a)], str(data), None, str(seed), report=self.report),
                         [(str(a), 'mac-os-9-2')])
        self.assertEqual(json.loads((data / 'config/install-seed.json').read_text())['settings']['machine.active'],
                         'mac-os-9-2')

    def test_without_seed_the_machine_keeps_its_own(self):
        a = _machine_dir(self.d / 'src', 'a', mid='mac-os-9')
        (a / 'install-seed.json').write_text('{"settings": {}}')
        data = self.d / 'rbdata'
        self.itd.install_machines([str(a)], str(data), None, None, report=self.report)
        self.assertTrue((data / 'machines/mac-os-9/install-seed.json').exists())
        self.assertFalse((data / 'config/install-seed.json').exists())

    def test_installed_bytes_follow_the_data_map(self):
        a = _machine_dir(self.d / 'src', 'a', mid='mac-os-9')
        subprocess.run([sys.executable, str(IMAGE / 'scripts/make-extents.py'), str(a / 'disk.img'),
                        str(IMAGE.parent / 'hostd')], check=True, capture_output=True)
        small = sum((a / f).stat().st_blocks * 512 for f in ('config.json', 'machine.json'))
        self.assertEqual(self.itd.installed_bytes(str(a)), 4096 + small)
        (a / 'disk.img.extents').write_text('{}')                     # no valid map: allocated blocks
        self.assertEqual(self.itd.installed_bytes(str(a)), (a / 'disk.img').stat().st_blocks * 512 + small)

    def test_arguments(self):
        out = subprocess.run([sys.executable, '-I', str(IMAGE / 'rootfs/usr/lib/retroboot/installer/install-to-disk'),
                              '--help'], capture_output=True, text=True)
        for flag in ('--machine-dir', '--seed', '--mount-data', '--unmount-data'):
            self.assertIn(flag, out.stdout)


@unittest.skipUnless(_gnu_cp(), 'needs GNU cp (the builder)')
class StageEmbeddedTest(unittest.TestCase):
    """make-iso.sh's embedded machines: machines/<id>/ in build order, each with its data map."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = pathlib.Path(tmp.name)

    def stage(self, *pairs):
        return subprocess.run([str(IMAGE / 'scripts/stage-embedded.sh'), str(self.d / 'stage'),
                               str(IMAGE.parent / 'hostd'), *map(str, pairs)], capture_output=True, text=True)

    def test_two_machines(self):
        a = _machine_dir(self.d / 'src', 'os922-deluxe-v4', data=b'DELUXE')
        (a / 'fixture.json').write_text(json.dumps({'id': 'os922-deluxe-v4', 'name': 'Mac OS 9.2.2 Deluxe v4',
                                                    'os_version': '9.2.2'}))
        b = _machine_dir(self.d / 'src', 'os922-full-v5', data=b'STANDARD')
        r = self.stage(a, 'os922-deluxe-v4', b, 'os922-full-v5')
        self.assertEqual(r.returncode, 0, r.stderr)
        emb = self.d / 'stage/usr/lib/retroboot/installer/embedded/machines'
        self.assertEqual((emb / 'order').read_text().split(), ['os922-deluxe-v4', 'os922-full-v5'])
        meta = json.loads((emb / 'os922-deluxe-v4/machine.json').read_text())
        self.assertEqual((meta['id'], meta['name'], meta['os_version']),
                         ('os922-deluxe-v4', 'Mac OS 9.2.2 Deluxe v4', '9.2.2'))
        for mid, src in (('os922-deluxe-v4', a), ('os922-full-v5', b)):
            disk = emb / mid / 'disk.img'
            self.assertEqual(disk.read_bytes(), (src / 'disk.img').read_bytes())
            self.assertLess(disk.stat().st_blocks * 512, 1 << 20)                # sparse copy
            ext = json.loads((emb / mid / 'disk.img.extents').read_text())
            self.assertEqual(ext['extents'], [[1 << 20, 4096]])
        self.assertEqual(oct(os.stat(emb / 'order').st_mode & 0o777), '0o644')
        # install-to-disk copies the staged layout by its maps
        itd = load(IMAGE / 'rootfs/usr/lib/retroboot/installer/install-to-disk', 'install_to_disk_stage')
        done = itd.install_machines([str(emb / 'os922-deluxe-v4'), str(emb / 'os922-full-v5')], str(self.d / 'data'),
                                    report=lambda *a, **k: None)
        self.assertEqual([m for _, m in done], ['os922-deluxe-v4', 'os922-full-v5'])
        self.assertFalse((self.d / 'data/machines/os922-full-v5/disk.img.extents').exists())

    def test_refusals(self):
        a = _machine_dir(self.d / 'src', 'a')
        self.assertNotEqual(self.stage(a, 'same', a, 'same').returncode, 0)          # an id twice
        self.assertNotEqual(self.stage(a, 'Bad_Id').returncode, 0)
        self.assertNotEqual(self.stage(a).returncode, 0)                              # not a pair
        self.assertNotEqual(self.stage(self.d / 'missing', 'x').returncode, 0)


class IsoLabAuthTest(unittest.TestCase):
    """make-iso.sh's RB_ISO_LAB_AUTH / RB_ISO_LAB_URL (write-lab-auth.sh): only the lab keys, 0600."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = pathlib.Path(tmp.name)
        self.out = self.d / 'lab-auth.env'

    def run_tool(self, **env):
        return subprocess.run([str(IMAGE / 'scripts/write-lab-auth.sh'), str(self.out)], capture_output=True,
                              text=True, env={**{k: v for k, v in os.environ.items() if not k.startswith('RB_ISO_')},
                                              **env})

    def test_sign_in_file_keeps_only_lab_keys(self):
        src = self.d / 'lab.env'
        src.write_text("RB_LAB_HTTP_USER=zq-user\nRB_LAB_HTTP_PASS='zq pass'\nexport RB_LAB_BASE_URL=https://f.example/lab/\n"
                       "GEMINI_PASS=zq-other\nLAB_WIFI_PASS=zq-wifi\n# RB_LAB_HTTP_PASS=commented\n")
        r = self.run_tool(RB_ISO_LAB_AUTH=str(src))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('zq', r.stdout + r.stderr)                                   # never printed
        text = self.out.read_text()
        self.assertEqual(text.splitlines(), ["RB_LAB_HTTP_USER=zq-user", "RB_LAB_HTTP_PASS='zq pass'",
                                             "export RB_LAB_BASE_URL=https://f.example/lab/"])
        self.assertEqual(oct(self.out.stat().st_mode & 0o777), '0o600')
        sys.path.insert(0, str(IMAGE.parent / 'hostd'))
        from retroboot import labauth
        m = labauth.parse_media(text)
        self.assertEqual((m.base_url, m.auth is not None), ('https://f.example/lab/', True))

    def test_incomplete_and_url_only(self):
        src = self.d / 'lab.env'
        src.write_text('RB_LAB_HTTP_USER=u\nRB_LAB_BASE_URL=https://f.example/lab/\n')
        self.assertNotEqual(self.run_tool(RB_ISO_LAB_AUTH=str(src)).returncode, 0)
        self.assertFalse(self.out.exists())
        self.assertNotEqual(self.run_tool(RB_ISO_LAB_URL='http://f.example/lab/').returncode, 0)
        r = self.run_tool(RB_ISO_LAB_URL='https://f.example/lab/')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.out.read_text(), 'RB_LAB_BASE_URL=https://f.example/lab/\n')


class UpdateAuditTest(unittest.TestCase):
    """make-update.py refuses a root that carries a machine archive anywhere."""

    def test_machine_archive_refused(self):
        mu = load(IMAGE / 'scripts/make-update.py', 'make_update')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / 'usr/share/doc').mkdir(parents=True)
        import gzip
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w') as tf:
            for n, data in (('config.json', b'{}'), ('disk.img', bytes(512))):
                ti = tarfile.TarInfo(n)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        (root / 'usr/share/doc/notes.tar.gz').write_bytes(gzip.compress(buf.getvalue()))
        with self.assertRaises(SystemExit) as cm:
            mu.audit(str(root))
        self.assertIn('machine archive', str(cm.exception))
        (root / 'usr/share/doc/notes.tar.gz').write_bytes(gzip.compress(b'plain text, not a tar'))
        mu.audit(str(root))

    def test_download_sign_in_refused(self):
        """A RetroBoot downloads sign-in never travels in an update (it stays on RBDATA)."""
        mu = load(IMAGE / 'scripts/make-update.py', 'make_update_lab')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / 'var/lib/retroboot/config').mkdir(parents=True)
        mu.audit(str(root))                                    # the empty mount-point directory is fine
        for path in ('var/lib/retroboot/config/hostd.local.json', 'etc/retroboot/lab-auth.env'):
            with self.subTest(path=path):
                f = root / path
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text('RB_LAB_BASE_URL=https://f.example/lab/\n')
                with self.assertRaises(SystemExit):
                    mu.audit(str(root))
                f.unlink()
        mu.audit(str(root))


class UpdateRomPolicyTest(unittest.TestCase):
    """make-update.py and the 68k emulator package (D044): its two declaration ROMs by exact path,
    no other ROM-like file there, and no Apple ROM anywhere whatever its name."""

    def test_m68k_package(self):
        import hashlib
        mu = load(IMAGE / 'scripts/make-update.py', 'make_update_m68k')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        share = root / 'usr/lib/retroboot/qemu-m68k/share/qemu'
        share.mkdir(parents=True)
        (root / 'usr/lib/retroboot/qemu/share/qemu').mkdir(parents=True)
        (root / 'usr/lib/retroboot/qemu/share/qemu/vgabios.rom').write_bytes(b'option ROM')
        (share / 'mac_qfb.rom').write_bytes(b'declaration ROM')
        (share / 'classicvirtio-68k.rom').write_bytes(b'declaration ROM')
        (share / 'q800-pram-seed.img').write_bytes(bytes(256))
        mu.audit(str(root))
        for name in ('Quadra800.rom', 'other.rom'):
            with self.subTest(name=name):
                (share / name).write_bytes(b'x')
                with self.assertRaises(SystemExit):
                    mu.audit(str(root))
                (share / name).unlink()
        # A file with an Apple ROM's hash is refused under any name (a stand-in hash here: no
        # Apple ROM is in this tree).
        fake = (b'not Apple code ' * 70000)[:1024 * 1024]
        old = mu.APPLE_ROMS
        mu.APPLE_ROMS = {hashlib.sha256(fake).hexdigest()}
        try:
            (root / 'usr/share/doc').mkdir(parents=True)
            (root / 'usr/share/doc/manual.pdf').write_bytes(fake)
            with self.assertRaises(SystemExit) as cm:
                mu.audit(str(root))
            self.assertIn('Apple ROM', str(cm.exception))
        finally:
            mu.APPLE_ROMS = old
        (root / 'usr/share/doc/manual.pdf').unlink()
        (root / 'var/lib/retroboot/roms').mkdir(parents=True)
        mu.audit(str(root))                                    # the empty directory is fine
        (root / 'var/lib/retroboot/roms/x.rom').write_bytes(b'x')
        with self.assertRaises(SystemExit):
            mu.audit(str(root))


class QuadraMachineJsonTest(unittest.TestCase):
    """make-machine-json.py and install-to-disk register a 68k Mac (family quadra800) like hostd."""

    def test_machine_json(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = pathlib.Path(tmp.name, 'os81')
        d.mkdir()
        (d / 'config.json').write_text('{"name": "Mac OS 8.1", "machineFamily": "quadra800", "ramMB": 128}')
        subprocess.run([sys.executable, str(IMAGE / 'scripts/make-machine-json.py'), str(d), 'os81',
                        str(IMAGE.parent / 'hostd')], check=True, capture_output=True)
        meta = json.loads((d / 'machine.json').read_text())
        self.assertEqual((meta['family'], meta['rom'], meta['pram']), ('quadra800', 'Quadra800.rom', 'pram.img'))
        self.assertEqual(meta['schema_version'], 2)          # a RetroBoot before 68k Macs leaves it out
        self.assertEqual(meta['template']['machineFamily'], 'quadra800')
        # without hostd beside it (the plain parsed template), the same fields
        e = pathlib.Path(tmp.name, 'os755')
        e.mkdir()
        (e / 'config.json').write_text('{"name": "Mac OS 7.5.5", "machineFamily": "quadra800"}')
        subprocess.run([sys.executable, str(IMAGE / 'scripts/make-machine-json.py'), str(e), 'os755',
                        '/nonexistent'], check=True, capture_output=True)
        self.assertEqual(json.loads((e / 'machine.json').read_text())['family'], 'quadra800')
        self.assertEqual(json.loads((e / 'machine.json').read_text())['schema_version'], 2)
        f = pathlib.Path(tmp.name, 'os922')
        f.mkdir()
        (f / 'config.json').write_text('{"name": "Mac OS 9", "machineFamily": "powerMacG4"}')
        subprocess.run([sys.executable, str(IMAGE / 'scripts/make-machine-json.py'), str(f), 'os922',
                        str(IMAGE.parent / 'hostd')], check=True, capture_output=True)
        self.assertNotIn('family', json.loads((f / 'machine.json').read_text()))     # a Power Mac as before


class GuestConfigTest(unittest.TestCase):
    """image.conf: the default guest is registered with a SHA-256, the old fixture stays selectable."""

    def cfg(self, key):
        out = subprocess.run(['bash', '-c', f'. "{IMAGE}/scripts/lib.sh"; cfg "{key}"'], capture_output=True,
                             text=True, check=True).stdout.strip()
        return out

    def test_default_and_regression_guests(self):
        import re
        default = self.cfg('guest_default')
        self.assertEqual(default, 'os922-full-v5')
        for gid in (default, 'os921-full-v3', 'os922-full-v2', 'os922-full-v1', 'os921-full-v2', 'os921-full', 'classicmac-os9-921-v2'):
            self.assertTrue(self.cfg(f'guest_{gid}_dir').startswith('/srv/retroboot/'))
            self.assertRegex(self.cfg(f'guest_{gid}_disk_sha256'), re.compile(r'^[0-9a-f]{64}$'))


if __name__ == '__main__':
    unittest.main()


class MicrocodeTest(unittest.TestCase):
    """rb-microcode (hostboot): this CPU's early microcode on the ESP for GRUB."""
    tool = IMAGE / 'rootfs/usr/lib/retroboot/image/rb-microcode'
    N4100 = 'processor\t: 0\nvendor_id\t: GenuineIntel\ncpu family\t: 6\nmodel\t\t: 122\nstepping\t: 8\n\nprocessor\t: 1\n'
    RYZEN = 'processor\t: 0\nvendor_id\t: AuthenticAMD\ncpu family\t: 26\nmodel\t\t: 36\nstepping\t: 0\n'

    def setUp(self):
        self.m = load(self.tool, 'rb_microcode')

    @staticmethod
    def members(data):
        out, pos = {}, 0
        while pos < len(data):
            assert data[pos:pos + 6] == b'070701', pos
            size, namesize = int(data[pos + 54:pos + 62], 16), int(data[pos + 94:pos + 102], 16)
            name = data[pos + 110:pos + 110 + namesize - 1].decode()
            pos += 110 + namesize
            pos += -pos % 4
            if name == 'TRAILER!!!':
                break
            out[name] = data[pos:pos + size]
            pos += size
            pos += -pos % 4
        return out

    def firmware(self, tmp):
        fw = pathlib.Path(tmp, 'fw')
        (fw / 'intel-ucode').mkdir(parents=True)
        (fw / 'amd-ucode').mkdir()
        (fw / 'intel-ucode/06-7a-08').write_bytes(b'GLK' * 100)
        (fw / 'intel-ucode/06-9c-00').write_bytes(b'JSL' * 100)
        (fw / 'amd-ucode/microcode_amd_fam1ah.bin').write_bytes(b'STRIX')
        (fw / 'amd-ucode/microcode_amd_fam19h.bin').write_bytes(b'ZEN3')
        return str(fw)

    def no_iucode(self, *a, **kw):
        raise OSError('no iucode_tool here')

    def test_signatures(self):
        self.assertEqual(self.m.cpu_identity(self.N4100), ('GenuineIntel', 6, 122, 8))
        self.assertEqual(self.m.intel_signature(6, 122, 8), 0x706a8)    # Gemini Lake
        self.assertEqual(self.m.intel_signature(6, 156, 0), 0x906c0)    # Jasper Lake
        self.assertEqual(self.m.intel_signature(15, 4, 1), 0xf41)

    def test_only_this_cpus_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            fw = self.firmware(tmp)
            data, info = self.m.archive_for(self.N4100, fw, run=self.no_iucode)
            self.assertEqual(self.members(data)['kernel/x86/microcode/GenuineIntel.bin'], b'GLK' * 100)
            data, info = self.m.archive_for(self.RYZEN, fw)
            self.assertEqual(self.members(data)['kernel/x86/microcode/AuthenticAMD.bin'], b'STRIX')
            # no updates for this CPU: an empty archive, so GRUB doesn't load the full set
            data, info = self.m.archive_for(self.N4100.replace('122', '55'), fw, run=self.no_iucode)
            self.assertEqual(self.members(data), {})
            self.assertTrue(data.startswith(b'070701'))

    def test_esp_file_named_by_the_slot_set_and_pruned(self):
        with tempfile.TemporaryDirectory() as tmp:
            fw = self.firmware(tmp)
            boot, esp = pathlib.Path(tmp, 'boot'), pathlib.Path(tmp, 'esp')
            boot.mkdir()
            esp.mkdir()
            cpuinfo = pathlib.Path(tmp, 'cpuinfo')
            cpuinfo.write_text(self.N4100)
            args = ['--esp', str(esp), '--boot', str(boot), '--cpuinfo', str(cpuinfo), '--firmware', fw,
                    '--state', str(pathlib.Path(tmp, 'state.json'))]
            self.assertEqual(self.m.main(args), 0)          # no microcode.cfg: nothing to do
            self.assertEqual(list(esp.iterdir()), [])
            for n, ident in enumerate(('aaaa0001aaaa0001', 'bbbb0002bbbb0002', 'cccc0003cccc0003', 'dddd0004dddd0004')):
                (boot / 'microcode.cfg').write_text(f'set rb_ucode_id={ident}\n')
                self.assertEqual(self.m.main(args), 0)
                path = esp / 'grub/retroboot' / f'ucode-{ident}.cpio'
                self.assertTrue(path.exists())
                os.utime(path, (1000 + n, 1000 + n))
            names = sorted(p.name for p in (esp / 'grub/retroboot').iterdir())
            self.assertEqual(names, ['ucode-bbbb0002bbbb0002.cpio', 'ucode-cccc0003cccc0003.cpio',
                                     'ucode-dddd0004dddd0004.cpio'])
            before = (esp / 'grub/retroboot/ucode-dddd0004dddd0004.cpio').stat().st_mtime_ns
            calls = []
            real = self.m.archive_for
            self.m.archive_for = lambda *a, **kw: calls.append(1) or real(*a, **kw)
            try:
                self.assertEqual(self.m.main(args), 0)      # same set, same CPU: nothing scanned or rewritten
                self.assertEqual(calls, [])
                self.assertEqual((esp / 'grub/retroboot/ucode-dddd0004dddd0004.cpio').stat().st_mtime_ns, before)
                (esp / 'grub/retroboot/ucode-dddd0004dddd0004.cpio').unlink()
                self.assertEqual(self.m.main(args), 0)      # the file went away: made again
                self.assertEqual(calls, [1])
                self.assertTrue((esp / 'grub/retroboot/ucode-dddd0004dddd0004.cpio').exists())
            finally:
                self.m.archive_for = real

    def test_read_only_esp_never_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            fw = self.firmware(tmp)
            boot = pathlib.Path(tmp, 'boot')
            boot.mkdir()
            (boot / 'microcode.cfg').write_text('set rb_ucode_id=abcdef0123456789\n')
            cpuinfo = pathlib.Path(tmp, 'cpuinfo')
            cpuinfo.write_text(self.N4100)
            esp = pathlib.Path(tmp, 'esp')
            esp.write_text('not a directory')
            self.assertEqual(self.m.main(['--esp', str(esp), '--boot', str(boot), '--cpuinfo', str(cpuinfo),
                                          '--firmware', fw, '--state', str(pathlib.Path(tmp, 's.json'))]), 0)
            self.assertFalse(pathlib.Path(tmp, 's.json').exists())   # not recorded: retried next boot


class ListDisksBitLockerTest(unittest.TestCase):
    """list-disks recognizes Windows BitLocker and Device Encryption drives (#4)."""
    tool = IMAGE / 'rootfs/usr/lib/retroboot/installer/list-disks'

    def setUp(self):
        self.m = load(self.tool, 'rb_list_disks')

    def test_bitlocker_and_device_encryption_detected(self):
        esp = {'type': 'part', 'parttype': self.m.ESP_TYPE, 'fstype': 'vfat', 'label': 'SYSTEM', 'partlabel': 'EFI system partition'}
        msr = {'type': 'part', 'parttype': self.m.MSR_TYPE, 'fstype': None, 'label': None, 'partlabel': 'Microsoft reserved partition'}
        winre = {'type': 'part', 'parttype': self.m.WIN_RE_TYPE, 'fstype': 'ntfs', 'label': 'Recovery', 'partlabel': ''}
        # Standard Windows 11 with BitLocker + WinRE NTFS + MSR + ESP
        dev = {'children': [esp, msr, {'type': 'part', 'parttype': self.m.BASIC_DATA_TYPE, 'fstype': 'BitLocker', 'label': None}, winre]}
        c = self.m.contents(dev)
        self.assertEqual(c['os'], 'Windows (encrypted)')
        self.assertEqual(c['summary'], 'Windows (encrypted)')
        # BitLocker-To-Go / standalone encrypted volume (no ESP)
        c_togo = self.m.contents({'children': [{'type': 'part', 'parttype': self.m.BASIC_DATA_TYPE, 'fstype': 'BitLocker', 'label': 'Work'}]})
        self.assertEqual(c_togo['os'], 'Windows (encrypted)')
        # Windows Device Encryption where blkid leaves Basic Data fstype blank alongside ESP + MSR
        c_blank = self.m.contents({'children': [esp, msr, {'type': 'part', 'parttype': self.m.BASIC_DATA_TYPE, 'fstype': '', 'label': None}]})
        self.assertEqual(c_blank['os'], 'Windows (encrypted)')
        # Standard unencrypted Windows still identifies as Windows
        c_win = self.m.contents({'children': [esp, msr, {'type': 'part', 'parttype': self.m.BASIC_DATA_TYPE, 'fstype': 'ntfs', 'label': 'OS'}]})
        self.assertEqual(c_win['os'], 'Windows')
        # Plain exFAT/FAT32 USB drive is not misclassified as Windows
        c_usb = self.m.contents({'children': [{'type': 'part', 'parttype': self.m.BASIC_DATA_TYPE, 'fstype': 'exfat', 'label': 'THUMB'}]})
        self.assertIsNone(c_usb['os'])
        self.assertEqual(c_usb['summary'], 'THUMB')
        # Standard Linux, Mac OS, and RetroBoot remain accurate
        self.assertEqual(self.m.contents({'children': [esp, {'type': 'part', 'fstype': 'ext4', 'parttype': '0fc63daf-8483-4772-8e79-3d69d8477de4'}]})['os'], 'Linux')
        self.assertEqual(self.m.contents({'children': [{'type': 'part', 'fstype': 'apfs'}]})['os'], 'Mac OS')
        self.assertEqual(self.m.contents({'children': [{'type': 'part', 'partlabel': 'RBROOT_A', 'fstype': 'ext4'}]})['os'], 'RetroBoot')


class Boot4KAndIsoContinuityTest(unittest.TestCase):
    """4K checker-default, 1366x768 GRUB fallback, ISO ESP notes, chimes, and arm64 OTA build (#5, #12, #13, #55)."""

    def test_initial_background_covers_4k_and_low_res_variants(self):
        mod = load(IMAGE / 'rootfs/usr/lib/retroboot/image/rb_bootdisplay' if False else IMAGE / 'rootfs/usr/lib/retroboot/image/rb-bootdisplay', 'rb_bootdisplay_4k')
        import io, contextlib
        with tempfile.TemporaryDirectory() as tmp:
            esp = pathlib.Path(tmp, 'esp')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mod.main(['--initial', '--esp', str(esp), '--settings', str(pathlib.Path(tmp, 'none.json'))]), 0)
            rbdir = esp / 'grub/retroboot'
            for fname in ('checker-default.png', 'checker-default-s1.png'):
                ihdr = png_chunks((rbdir / fname).read_bytes())[b'IHDR']
                w, h = struct.unpack('>II', ihdr[:8])
                self.assertEqual((w, h), (3840, 2160), fname)
        for tmpl in ('grub/grub.cfg.in', 'grub/grub-iso.cfg.in'):
            txt = (IMAGE / tmpl).read_text()
            self.assertIn('checker-default-s1.png', txt, tmpl)
            self.assertIn('1366', txt, tmpl)

    def test_make_iso_esp_includes_wifi_example_and_setup(self):
        src = (IMAGE / 'scripts/make-iso.sh').read_text()
        self.assertIn('::/retroboot/network.conf.example', src)
        self.assertIn('::/SETUP.txt', src)

    def test_stage_startup_chimes_handles_unset_and_wav_dir(self):
        lib = IMAGE / 'scripts/lib.sh'
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp, 'root')
            chimes = pathlib.Path(tmp, 'chimes')
            root.mkdir()
            chimes.mkdir()
            (chimes / 'os9.wav').write_bytes(b'RIFFdemo')
            (chimes / 'README.txt').write_text('ignore non-wav')
            env_unset = {k: v for k, v in os.environ.items() if k != 'RB_CHIMES_DIR'}
            r1 = subprocess.run(['bash', '-c', f'. "{lib}" && stage_startup_chimes "{root}"'], env=env_unset, capture_output=True, text=True, check=True)
            self.assertIn('skipping', r1.stderr)
            self.assertFalse((root / 'usr/share/retroboot/chimes').exists())
            env_set = dict(env_unset, RB_CHIMES_DIR=str(chimes))
            subprocess.run(['bash', '-c', f'. "{lib}" && stage_startup_chimes "{root}"'], env=env_set, capture_output=True, text=True, check=True)
            self.assertTrue((root / 'usr/share/retroboot/chimes/os9.wav').is_file())
            self.assertFalse((root / 'usr/share/retroboot/chimes/README.txt').exists())

    def test_make_update_target_arch_avoids_x86_bcj_on_arm64(self):
        mu = load(IMAGE / 'scripts/make-update.py', 'rb_make_update')
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp, 'root')
            (root / 'usr/lib/retroboot').mkdir(parents=True)
            self.assertEqual(mu.target_arch('auto', str(root), {}), 'x86_64')
            self.assertEqual(mu.target_arch('auto', str(root), {'board': 'rpi', 'arch': 'arm64'}), 'arm64')
            (root / 'usr/lib/retroboot/rpi-profile.json').write_text('{}')
            self.assertEqual(mu.target_arch('auto', str(root), {}), 'arm64')
            self.assertEqual(mu.target_arch('x86_64', str(root), {}), 'x86_64')
