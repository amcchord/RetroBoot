"""The PowerPC FPU fast-path series (emulator/reports/ppcfpu-20261012.md) and
its r17 follow-ups (emulator/reports/ppcfpu2-20261012.md: leaner FP and AltiVec
helpers, branch caches) are pinned in the lock with their hashes in order
before the ATI patch, touch only what they should, have their off switches,
and the package check and manifest features know them."""
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
LOCK = json.loads((ROOT / 'emulator/manifests/sources.lock.json').read_text())
FAST = 'emulator/patches/retroboot-ppc-fpu-fast.patch'
VMX = 'emulator/patches/retroboot-ppc-fpu-vmx.patch'
R17 = 'emulator/patches/retroboot-ppc-fpu-r17.patch'
BRANCH = 'emulator/patches/retroboot-ppc-branch-cache.patch'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def added(name):
    return [ln[1:] for ln in (ROOT / name).read_text().splitlines()
            if ln.startswith('+') and not ln.startswith('+++')]


def touched(name):
    return [ln.split()[2][2:] for ln in (ROOT / name).read_text().splitlines()
            if ln.startswith('diff --git')]


class PPCFPUSources(unittest.TestCase):
    def test_pinned_with_hashes(self):
        paths = [e['path'] for e in LOCK['retroboot_patches']]
        i = paths.index(FAST)
        # r18 (reports/viacal-20261013.md) adds the VIA timer-2 patch between
        # the branch caches and the ATI card, which comes after them.
        self.assertEqual(paths[i + 1:i + 4], [VMX, R17, BRANCH])
        self.assertGreater(paths.index('emulator/patches/retroboot-ati-r100.patch'), i)   # the ATI card after
        for e in LOCK['retroboot_patches'][i:i + 4]:
            self.assertEqual(e['sha256'], sha(ROOT / e['path']))
            self.assertEqual(sorted(e['files']), sorted(touched(e['path'])))

    def test_only_target_ppc(self):
        for name in (FAST, VMX, R17):
            for f in touched(name):
                self.assertTrue(f.startswith('target/ppc/'), f)
        # the branch caches: TCG's lookup and invalidation paths and the PPC translator
        for f in touched(BRANCH):
            self.assertTrue(f.startswith(('target/ppc/', 'accel/tcg/', 'tcg/tcg-op.c',
                                          'include/exec/tb-slot.h', 'include/qemu/rb-stats.h',
                                          'include/tcg/tcg-op-common.h')), f)

    def test_r17_switches(self):
        r17 = '\n'.join(added(R17))
        self.assertIn('#define PPC_FAST_FPU_ALL    0x7f', r17)
        for bit in ('PPC_FAST_FPU_CALLS  0x10', 'PPC_FAST_FPU_SIMD   0x20',
                    'PPC_FAST_FPU_VLDST  0x40'):
            self.assertIn('#define ' + bit, r17)
        # NO_RWG helpers only where no FP exception can trap
        self.assertIn('ctx->ff_norwg = (ctx->fast_fpu & PPC_FAST_FPU_CALLS) &&', r17)
        self.assertIn('if (env->fast_fpexc && (msr & ((1ull << MSR_FE0) | (1ull << MSR_FE1)))) {', r17)
        self.assertIn('if (env->fast_vmx_simd && (simd)) {', r17)
        self.assertIn('if (ctx->fast_fpu & PPC_FAST_FPU_VLDST) {', r17)
        br = '\n'.join(added(BRANCH))
        self.assertIn('DEFINE_PROP_BOOL("fast-branch", PowerPCCPU, fast_branch, true)', br)
        self.assertIn('#define PPC_FAST_BRANCH_ALL    0x7', br)
        # one vCPU only, ordinary cflags only
        self.assertIn('if (CPU_NEXT(first_cpu) || cs->singlestep_enabled ||', br)
        # every jump cache invalidation advances the generation
        self.assertGreaterEqual(br.count('tcg_tb_gen_bump();'), 4)
        self.assertIn('tcg_tb_slot_reset();', br)

    def test_switches(self):
        code = '\n'.join(added(FAST))
        self.assertIn('DEFINE_PROP_BOOL("fast-fpu", PowerPCCPU, fast_fpu, true)', code)
        self.assertIn('DEFINE_PROP_UINT32("x-fast-fpu-mask", PowerPCCPU, fast_fpu_mask,', code)
        self.assertIn('ctx->fast_fpu = POWERPC_CPU(cs)->fast_fpu ?', code)
        vmx = '\n'.join(added(VMX))
        self.assertIn('#define PPC_FAST_FPU_ALL    0xf', vmx)
        self.assertIn('env->fast_vmx = cpu->fast_fpu && (cpu->fast_fpu_mask & PPC_FAST_FPU_VMX);', vmx)
        # every AltiVec fast lane is guarded by the switch
        helpers = (ROOT / VMX).read_text()
        self.assertEqual(helpers.count('fv_madd(a->u32[i]'), 1)
        self.assertGreaterEqual(helpers.count('env->fast_vmx'), 8)

    def test_fallback_keeps_original_sequence(self):
        code = '\n'.join(added(FAST))
        # the fallback writes the FPR before float_check_status can raise
        self.assertIn('ff_set_fpr(env, frt, r);\n    helper_compute_fprf_float64(env, r);\n'
                      '    do_float_check_status(env, true, ra);', code)
        # fast path only with XE clear, round to nearest, no pending FP exception
        self.assertIn('!(env->fpscr & FP_XE)', code)
        self.assertIn('float_round_nearest_even', code)
        self.assertIn('exception_index != POWERPC_EXCP_PROGRAM', code)

    def test_package_feature_and_verification(self):
        manifest = (ROOT / 'emulator/scripts/package_manifest.py').read_text()
        self.assertIn("('ppc-fast-fpu','retroboot-ppc-fpu-fast.patch')", manifest)
        self.assertIn("('ppc-fast-fpu-vmx','retroboot-ppc-fpu-vmx.patch')", manifest)
        self.assertIn("('ppc-fast-fpu-r17','retroboot-ppc-fpu-r17.patch')", manifest)
        self.assertIn("('ppc-branch-cache','retroboot-ppc-branch-cache.patch')", manifest)
        verify = (ROOT / 'emulator/scripts/verify_package.py').read_text()
        self.assertIn("fast-fpu=<bool>.*default: on", verify)
        self.assertIn("x-fast-fpu-mask=<uint32>.*default: 127", verify)
        self.assertIn("fast-branch=<bool>.*default: on", verify)
        self.assertIn("x-fast-branch-mask=<uint32>.*default: 7", verify)

    def test_conformance_ops_cover_the_7400(self):
        sys.path.insert(0, str(ROOT / 'emulator/tests/ppcfpu'))
        import gen_ops
        names = {n for n, _, _ in gen_ops.ops()}
        for op in ('fadd', 'fadds', 'fsub', 'fsubs', 'fmul', 'fmuls', 'fdiv', 'fdivs',
                   'fmadd', 'fmadds', 'fmsub', 'fmsubs', 'fnmadd', 'fnmadds', 'fnmsub',
                   'fnmsubs', 'fsel', 'fres', 'frsqrte', 'fsqrt', 'fsqrts', 'frsp', 'fctiw',
                   'fctiwz', 'fmr', 'fneg', 'fabs', 'fnabs', 'mffs', 'fcmpu_cr0', 'fcmpo_cr0',
                   'lfs', 'lfsx', 'lfsu', 'lfsux', 'lfd', 'stfs', 'stfd', 'stfiwx',
                   'vaddfp', 'vsubfp', 'vmaddfp', 'vnmsubfp', 'vmaxfp', 'vminfp', 'vrefp',
                   'vrsqrtefp', 'vexptefp', 'vlogefp', 'vcmpbfp', 'vcmpeqfp_rc', 'vctsxs_0',
                   'vctuxs_31', 'vcfsx_16', 'vcfux_1', 'vperm', 'lvebx', 'lvehx', 'lvewx',
                   'stvebx', 'stvehx', 'stvewx', 'lvewx_le', 'stvewx_le', 'stvebx_le'):
            self.assertIn(op, names)
        for prefix in ('mtfsf_', 'mtfsfi_', 'mtfsb0_', 'mtfsb1_', 'mcrfs_'):
            self.assertTrue(any(n.startswith(prefix) for n in names), prefix)
        for n in ('fadd', 'fmadds', 'fctiwz', 'fsel', 'frsp'):
            self.assertIn(n + '_rc', names)


if __name__ == '__main__':
    unittest.main()
