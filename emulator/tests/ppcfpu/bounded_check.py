#!/usr/bin/env python3
"""Exact-rational check of the single-precision bounded fmadds rule
(fpu_fast.c.inc, ff_fma_single_bounded) on vectors aimed at single-precision
midpoints and grid points; "wide" adds heavy cancellation and wide offsets.

    bounded_check.py SEED COUNT [wide]
"""
import random, struct, sys
from fractions import Fraction as F

def u(d): return struct.unpack('>Q', struct.pack('>d', d))[0]
def dd(x): return struct.unpack('>d', struct.pack('>Q', x))[0]
def to_f32(d): return struct.unpack('>f', struct.pack('>f', d))[0]
def exp(x): return (x >> 52) & 0x7ff
def normal(x): return 1 <= exp(x) <= 0x7fe

def rn24(x):
    """exact round-to-nearest-even of a positive or negative rational to 24 bits"""
    if x == 0: return F(0)
    s = -1 if x < 0 else 1
    x = abs(x)
    e = 0
    while x >= 2: x /= 2; e += 1
    while x < 1: x *= 2; e -= 1
    m = x * 2**23
    fl = m.numerator // m.denominator
    rem = m - fl
    if rem > F(1, 2) or (rem == F(1, 2) and fl % 2 == 1): fl += 1
    return s * F(fl, 2**23) * F(2)**e

def bounded(a, c, b):
    p = a * c
    rp = u(p)
    if not (2 <= exp(rp) <= 0x7fd): return None
    rb = u(b)
    if not (exp(rb) <= 0x7fc or (rb & 0x7fffffffffffffff) == 0):
        return None
    if exp(rb) == 0 and (rb & 0x7fffffffffffffff): return None
    s = p + b
    rs = u(s)
    if not normal(rs): return None
    d = exp(rp) - exp(rs)
    if d > 20: return None
    need = 2 if d <= 0 else (1 << (d - 1)) + 1
    low = rs & 0x1fffffff
    mid = abs(low - 0x10000000)
    grid = low if low < 0x10000000 else 0x20000000 - low
    if mid < need or grid < need: return None
    y = to_f32(s)
    ay = abs(y)
    if not (ay > 2**-126 and ay <= 3.4028234663852886e38): return None
    return y

random.seed(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
WIDE = len(sys.argv) > 3 and sys.argv[3] == "wide"
n = ok = taken = near = 0
for i in range(int(sys.argv[2]) if len(sys.argv) > 2 else 200000):
    a = random.uniform(0.5, 2) * 2.0 ** random.randint(-20, 20)
    c = random.uniform(0.5, 2) * 2.0 ** random.randint(-20, 20)
    if random.random() < 0.5:
        a = to_f32(a)       # one single, one double: product not exact
    p = a * c
    # target: a single-precision midpoint or grid point near |p| times a factor
    t = to_f32(p * (random.uniform(0.5, 2) * 2.0 ** random.randint(-22, 3) if WIDE else random.uniform(0.25, 4)))
    ulp24 = 2.0 ** (((u(t) >> 52) & 0x7ff) - 1023 - 23)
    t = t + (ulp24 / 2 if random.random() < 0.5 else 0)
    r = (1 << random.randint(0, 22)) if WIDE else 40
    k = random.randint(-r, r) * ulp24 * 2.0 ** -29
    b = (t + k) - p
    if random.random() < 0.5: b = -b; a = -a
    n += 1
    y = bounded(a, c, b)
    if y is None: continue
    taken += 1
    x = F(a) * F(c) + F(b)
    if abs(F(u(p + b) & 0x1fffffff) - 0x10000000) < 2**10: near += 1
    if F(y) != rn24(x) or x == F(y):
        print('WRONG', a.hex(), c.hex(), b.hex(), y, float(rn24(x)))
        sys.exit(1)
    ok += 1
print(f'vectors {n} fast {taken} correct {ok} near-midpoint fast {near}')
