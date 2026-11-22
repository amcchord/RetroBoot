#!/usr/bin/env python3
"""Minimal QR Code encoder for example documents (byte mode, level M, v1-6).

hostd encodes its own QR matrices; this tool only produces the '0'/'1' rows
used by examples/web-pairing.json and the tests, without third-party
packages. Usage: qrgen.py TEXT  -> JSON array of row strings on stdout.
"""
import json
import sys

# (data codewords per block, number of blocks, EC codewords per block) for level M
LEVEL_M = {1: (16, 1, 10), 2: (28, 1, 16), 3: (44, 1, 26), 4: (32, 2, 18), 5: (43, 2, 24), 6: (27, 4, 16)}
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}

EXP = [0] * 512
LOG = [0] * 256
v = 1
for i in range(255):
    EXP[i] = v
    LOG[v] = i
    v <<= 1
    if v & 0x100:
        v ^= 0x11D
for i in range(255, 512):
    EXP[i] = EXP[i - 255]


def gmul(a, b):
    return 0 if a == 0 or b == 0 else EXP[LOG[a] + LOG[b]]


def rs_ec(data, n):
    gen = [1]
    for i in range(n):
        nxt = [0] * (len(gen) + 1)
        for j, g in enumerate(gen):
            nxt[j] ^= g
            nxt[j + 1] ^= gmul(g, EXP[i])
        gen = nxt
    rem = list(data) + [0] * n
    for i in range(len(data)):
        c = rem[i]
        if c:
            for j in range(1, len(gen)):
                rem[i + j] ^= gmul(gen[j], c)
    return rem[len(data):]


def encode(text):
    raw = text.encode('utf-8')
    for ver in range(1, 7):
        dpb, nb, ecb = LEVEL_M[ver]
        cap = dpb * nb
        if 4 + 8 + 8 * len(raw) <= cap * 8:
            break
    else:
        raise SystemExit('text too long for version 6-M')
    bits = [0, 1, 0, 0] + [int(b) for b in format(len(raw), '08b')]
    for byte in raw:
        bits += [int(b) for b in format(byte, '08b')]
    bits += [0] * min(4, cap * 8 - len(bits))
    while len(bits) % 8:
        bits.append(0)
    data = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = [0xEC, 0x11]
    while len(data) < cap:
        data.append(pad[(len(data) - len(bits) // 8) % 2])
    blocks = [data[i * dpb:(i + 1) * dpb] for i in range(nb)]
    ecs = [rs_ec(b, ecb) for b in blocks]
    final = []
    for i in range(dpb):
        for b in blocks:
            final.append(b[i])
    for i in range(ecb):
        for e in ecs:
            final.append(e[i])
    stream = []
    for c in final:
        stream += [int(b) for b in format(c, '08b')]

    size = 17 + 4 * ver
    m = [[None] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def setf(x, y, val):
        m[y][x] = val
        fn[y][x] = True

    def finder(cx, cy):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    d = max(abs(dx), abs(dy))
                    setf(x, y, 1 if d in (0, 1, 3) else 0)

    finder(3, 3)
    finder(size - 4, 3)
    finder(3, size - 4)
    for i in range(8, size - 8):
        setf(i, 6, 1 if i % 2 == 0 else 0)
        setf(6, i, 1 if i % 2 == 0 else 0)
    pos = ALIGN[ver]
    for ay in pos:
        for ax in pos:
            if fn[ay][ax]:
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setf(ax + dx, ay + dy, 1 if max(abs(dx), abs(dy)) != 1 else 0)
    # Reserve format areas and the dark module.
    for i in range(9):
        if not fn[8][i]:
            setf(i, 8, 0)
        if not fn[i][8]:
            setf(8, i, 0)
    for i in range(8):
        setf(size - 1 - i, 8, 0)
        setf(8, size - 1 - i, 0)
    setf(8, size - 8, 1)

    # Zigzag data placement.
    idx = 0
    x = size - 1
    upward = True
    while x > 0:
        if x == 6:
            x -= 1
        for k in range(size):
            y = size - 1 - k if upward else k
            for xx in (x, x - 1):
                if not fn[y][xx]:
                    m[y][xx] = stream[idx] if idx < len(stream) else 0
                    idx += 1
        upward = not upward
        x -= 2

    masks = [
        lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
        lambda x, y: (x + y) % 3 == 0, lambda x, y: (y // 2 + x // 3) % 2 == 0,
        lambda x, y: (x * y) % 2 + (x * y) % 3 == 0, lambda x, y: ((x * y) % 2 + (x * y) % 3) % 2 == 0,
        lambda x, y: ((x + y) % 2 + (x * y) % 3) % 2 == 0,
    ]

    def format_bits(mask):
        d = (0b00 << 3) | mask          # level M = 00
        rem = d << 10
        for i in range(14, 9, -1):
            if rem & (1 << i):
                rem ^= 0x537 << (i - 10)
        return ((d << 10) | rem) ^ 0x5412

    def apply(mask):
        g = [row[:] for row in m]
        for y in range(size):
            for x in range(size):
                if not fn[y][x] and masks[mask](x, y):
                    g[y][x] ^= 1
        f = format_bits(mask)
        fb = [(f >> i) & 1 for i in range(15)]
        # Around the top-left finder.
        coords = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8), (7, 8),
                  (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
        for i, (xx, yy) in enumerate(coords):
            g[yy][xx] = fb[i]
        # Split copy beside the other two finders.
        for i in range(8):
            g[8][size - 1 - i] = fb[i]
        for i in range(8, 15):
            g[size - 15 + i][8] = fb[i]
        g[size - 8][8] = 1
        return g

    def penalty(g):
        p = 0
        for rows in (g, [list(c) for c in zip(*g)]):
            for row in rows:
                run, prev = 0, None
                for c in row:
                    if c == prev:
                        run += 1
                    else:
                        if run >= 5:
                            p += run - 2
                        run, prev = 1, c
                if run >= 5:
                    p += run - 2
                s = ''.join(map(str, row))
                p += 40 * (s.count('10111010000') + s.count('00001011101'))
        for y in range(size - 1):
            for x in range(size - 1):
                if g[y][x] == g[y][x + 1] == g[y + 1][x] == g[y + 1][x + 1]:
                    p += 3
        dark = sum(map(sum, g))
        p += abs(dark * 20 // (size * size) - 10) * 10
        return p

    best = min(range(8), key=lambda k: penalty(apply(k)))
    g = apply(best)
    return [''.join(str(c) for c in row) for row in g]


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    print(json.dumps(encode(sys.argv[1])))
