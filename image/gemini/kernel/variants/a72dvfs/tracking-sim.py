#!/usr/bin/env python3
"""Simulate every A72 OPP transition of the a72dvfs kernel and check the vendor rules.

Mirrors, step for step:
  * mediatek-cpufreq mtk_cpufreq_set_target(): voltage up to max(target,
    intermediate) first, then MAINPLL -> PLL -> back, then voltage down;
  * mtk_cpufreq_voltage_tracking() with the MT6797 platform data
    (min_volt_shift 100 mV, max_volt_shift 275 mV, VSRAM 1.0..1.2 V, VPROC <= 1.2 V);
  * the regulator core's selector choice: the lowest grid voltage within
    [min, max] (DA9214 BUCKB 300 mV + n x 10 mV; VSRAM_B 0.9 V + n x 25 mV);
    an unchanged selector is not written.
Checks after every single regulator write (the vendor set_cur_volt_extbuck()
BUG_ON conditions): VSRAM >= VPROC, VSRAM - VPROC <= 300 mV, VPROC <= 1.2 V,
1.0 <= VSRAM <= 1.2 V. It also checks that the CPU runs on MAINPLL only at
>= 1.0 V and at each OPP's own frequency only at >= its table voltage, and
counts the SMC (VSRAM_B) and I2C (VPROC2) writes.

Usage: tracking-sim.py [--bin sb|tt|other]   (default: all three)
"""
import itertools
import sys

OPPS = {  # MHz: (uV, opp-supported-hw)
    845: (1000000, 0x7), 1001: (1000000, 0x7), 1131: (1000000, 0x7),
    1378: (1000000, 0x7), 1495: (1000000, 0x7),
    1677: (1040000, 0x3), 1885: (1080000, 0x3), 2093: (1110000, 0x3),
    2158: (1130000, 0x3), 2223: (1140000, 0x3), 2262: (1150000, 0x3),
    2327: (1160000, 0x3), 2392: (1170000, 0x1), 2522: (1200000, 0x1),
    2431: (1180000, 0x2), 2587: (1200000, 0x2),
}
BINS = {"sb": 0x1, "tt": 0x2, "other": 0x4}
MIN_SHIFT, MAX_SHIFT = 100000, 275000
SRAM_MIN, SRAM_MAX, PROC_MAX = 1000000, 1200000, 1200000
INTER_MHZ, INTER_FLOOR = 1092, 1000000
BOOT = (1000000, 1100000)   # cl2-power: VPROC2 boot selector 0x46, VSRAM_B 1.1 V


def grid_up(lo, hi, base, step):
    v = base + -(-(lo - base) // step) * step
    if v > hi:
        raise AssertionError(f"no grid point in [{lo}, {hi}]")
    return v


class Rails:
    def __init__(self):
        self.vproc, self.vsram = BOOT
        self.writes = {"vproc2": 0, "vsram_b": 0}
        self.max_seen = max(BOOT)

    def check(self, what):
        p, s = self.vproc, self.vsram
        assert s >= p, f"{what}: VSRAM {s} < VPROC {p}"
        assert s - p <= 300000, f"{what}: VSRAM - VPROC = {s - p}"
        assert p <= PROC_MAX and SRAM_MIN <= s <= SRAM_MAX, f"{what}: {p} / {s}"
        self.max_seen = max(self.max_seen, p, s)

    def set_proc(self, lo, hi):
        v = grid_up(max(lo, 800000), min(hi, 1200000), 300000, 10000)
        if v != self.vproc:
            self.vproc = v
            self.writes["vproc2"] += 1
            self.check("vproc2 write")

    def set_sram(self, lo, hi):
        v = grid_up(max(lo, 1000000), min(hi, 1200000), 900000, 25000)
        if v != self.vsram:
            self.vsram = v
            self.writes["vsram_b"] += 1
            self.check("vsram_b write")


def tracking(r, new_vproc):
    """mtk_cpufreq_voltage_tracking(), regulator values read back each pass."""
    pre_vproc, pre_vsram = r.vproc, r.vsram
    new_vsram = min(max(new_vproc + MIN_SHIFT, SRAM_MIN), SRAM_MAX)
    for _ in range(3 * -(-max(SRAM_MAX, PROC_MAX) // MIN_SHIFT)):
        if pre_vproc <= new_vproc:
            vsram = min(max(pre_vproc + MAX_SHIFT, SRAM_MIN), new_vsram)
            r.set_sram(vsram, SRAM_MAX)
            vproc = new_vproc if (vsram == SRAM_MAX or new_vsram == SRAM_MIN) else vsram - MIN_SHIFT
            r.set_proc(vproc, PROC_MAX)
        else:
            vproc = max(new_vproc, pre_vsram - MAX_SHIFT)
            r.set_proc(vproc, PROC_MAX)
            vsram = new_vsram if vproc == new_vproc else max(new_vsram, vproc + MIN_SHIFT)
            r.set_sram(vsram, SRAM_MAX)
        pre_vproc, pre_vsram = vproc, vsram
        if vproc == new_vproc and vsram == new_vsram:
            return
    raise AssertionError("over loop count")


def set_target(r, table, cur_mhz, new_mhz):
    vproc = table[new_mhz]
    inter = max(table[min(m for m in table if m >= INTER_MHZ)], INTER_FLOOR)
    pre = r.vproc
    target = max(inter, vproc)
    if pre <= target:
        tracking(r, target)
    # on MAINPLL now: needs >= the 1.0 V floor
    assert r.vproc >= INTER_FLOOR, f"MAINPLL at {r.vproc}"
    assert r.vproc >= vproc, f"{new_mhz} MHz at {r.vproc} < {vproc}"
    if vproc < inter or vproc < pre:
        tracking(r, vproc)
    assert r.vproc >= vproc, f"{new_mhz} MHz settles at {r.vproc} < {vproc}"


def run(bin_name):
    hw = BINS[bin_name]
    table = {m: uv for m, (uv, h) in OPPS.items() if h & hw}
    total = {"vproc2": 0, "vsram_b": 0}
    worst = 0
    pairs = 0
    for a, b in itertools.permutations(sorted(table), 2):
        r = Rails()
        set_target(r, table, 750, a)      # first transition from ATF's 750 MHz
        r.writes = {"vproc2": 0, "vsram_b": 0}
        set_target(r, table, a, b)
        for k in total:
            total[k] += r.writes[k]
        worst = max(worst, r.max_seen)
        pairs += 1
    # boot cap: every transition among the <= 1495 MHz OPPs writes nothing
    r = Rails()
    for a, b in itertools.permutations([m for m in table if m <= 1495], 2):
        set_target(r, table, a, b)
    capped = dict(r.writes)
    print(f"{bin_name:5s}: {len(table)} OPPs (top {max(table)} MHz at {table[max(table)] / 1000:.0f} mV), "
          f"{pairs} transitions ok, max rail {worst / 1000:.0f} mV, writes over all pairs "
          f"vproc2 {total['vproc2']} vsram_b {total['vsram_b']}; within the 1495 MHz boot cap: "
          f"vproc2 {capped['vproc2']} vsram_b {capped['vsram_b']} writes")
    assert capped == {"vproc2": 0, "vsram_b": 0}, "boot-cap range must not write any voltage"
    assert worst <= 1200000


def main():
    bins = [sys.argv[2]] if len(sys.argv) > 2 and sys.argv[1] == "--bin" else list(BINS)
    for b in bins:
        run(b)
    print("tracking simulation: pass")


if __name__ == "__main__":
    main()
