"""The OS X runtime keeps exact source pins and narrowly reviewed scope."""
import hashlib
import json
from pathlib import Path
import unittest
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[2]
LOCK = json.loads((ROOT / 'emulator/manifests/osx.lock.json').read_text())
sys.path.insert(0, str(ROOT / 'emulator/scripts'))
spec = importlib.util.spec_from_file_location('prepare_osx_sources', ROOT / 'emulator/scripts/prepare_osx_sources.py')
PREPARE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PREPARE)


class OSXSourceContract(unittest.TestCase):
    def test_port_hash_and_scope(self):
        entry = LOCK['port']
        data = (ROOT / entry['path']).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), entry['sha256'])
        files = [x.split()[2][2:] for x in data.decode().splitlines() if x.startswith('diff --git')]
        self.assertEqual(files, entry['files'])
        restricted = [x for x in files if x.startswith(('target/', 'tcg/', 'accel/', 'fpu/', 'hw/ide/'))]
        self.assertEqual(restricted, sorted(['target/ppc/fpu_helper.c', *PREPARE.PPC_7400_DL1HWF_FIX['files']]))
        self.assertEqual(LOCK['ppc_7400_dl1hwf_fix'], PREPARE.PPC_7400_DL1HWF_FIX)
        cache = LOCK['ppc_7400_dl1hwf_fix']
        self.assertEqual(hashlib.sha256((ROOT / cache['patch_path']).read_bytes()).hexdigest(), cache['patch_sha256'])
        self.assertEqual(LOCK['ppc_fex_fix'], PREPARE.PPC_FEX_FIX)
        fix = LOCK['ppc_fex_fix']
        self.assertEqual(hashlib.sha256((ROOT / fix['patch_path']).read_bytes()).hexdigest(), fix['patch_sha256'])
        self.assertEqual((ROOT / fix['patch_path']).read_text().count('\n@@ '), 1)

    def test_other_cpu_and_ide_paths_remain_forbidden(self):
        for path in ('target/ppc/cpu.c', 'target/ppc/helper_regs.c', 'tcg/tcg.c',
                     'accel/tcg/cpu-exec.c', 'fpu/softfloat.c', 'hw/ide/core.c'):
            lock = json.loads(json.dumps(LOCK))
            lock['port']['files'] = sorted(lock['port']['files'] + [path])
            with self.assertRaises(SystemExit):
                PREPARE.validate_scope(ROOT, lock['port']['files'], lock)
        lock = json.loads(json.dumps(LOCK))
        del lock['ppc_fex_fix']
        with self.assertRaises(SystemExit):
            PREPARE.validate_scope(ROOT, lock['port']['files'], lock)

    def test_cache_completion_exception_is_exact(self):
        lock = json.loads(json.dumps(LOCK))
        lock['ppc_7400_dl1hwf_fix']['files']['target/ppc/translate.c']['after'] = '0' * 64
        with self.assertRaises(SystemExit):
            PREPARE.validate_scope(ROOT, sorted(lock['port']['files']), lock)
        lock = json.loads(json.dumps(LOCK))
        del lock['ppc_7400_dl1hwf_fix']
        with self.assertRaises(SystemExit):
            PREPARE.validate_scope(ROOT, sorted(lock['port']['files']), lock)

    def test_exact_debian_inputs_and_distinct_guards(self):
        qemu = LOCK['qemu']
        self.assertEqual(qemu['version'], '10.0.13')
        self.assertEqual(qemu['debian_version'], '1:10.0.13+ds-0+deb13u1')
        self.assertEqual(qemu['debian_orig_sha256'], qemu['archives'][1]['sha256'])
        self.assertEqual(qemu['debian_patch_sha256'], qemu['archives'][2]['sha256'])
        self.assertEqual(qemu['debian_dsc_sha256'], qemu['archives'][0]['sha256'])
        self.assertTrue(all(x['bytes'] > 0 and x['url'].startswith('https://deb.debian.org/debian/pool/main/q/qemu/') for x in qemu['archives']))
        self.assertTrue({'macosx-runtime-1', 'macosx-qemu-10.0-1', 'macosx-nvram-flash-1'} <= set(LOCK['features']))
        classic = (ROOT / 'emulator/scripts/package_manifest.py').read_text()
        self.assertNotIn('macosx-runtime-1', classic)
        shared = json.loads((ROOT / 'emulator/manifests/sources.lock.json').read_text())
        self.assertEqual(LOCK['firmware']['sha256'], shared['firmware_build']['openbios']['osx']['staged_sha256'])


if __name__ == '__main__':
    unittest.main()
