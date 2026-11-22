"""The standard card's untracked-VRAM damage (retroboot-vga-untracked-damage.patch,
emulator/reports/idle-damage-2026-11-12.md) is pinned after the other VGA patches, touches
only the VGA core, and its row comparison reports every change: the routine is
compiled from the patch itself and run against random rows when a C compiler is
available."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
LOCK = json.loads((ROOT / 'emulator/manifests/sources.lock.json').read_text())
PATCH = 'emulator/patches/retroboot-vga-untracked-damage.patch'
AFTER = ['emulator/patches/retroboot-vga-flip-during-snapshot.patch',
         'emulator/patches/retroboot-vga-fast-rows.patch']

HARNESS = r'''
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define likely(x) __builtin_expect(!!(x), 1)
static inline uint64_t ldq_he_p(const void *p) { uint64_t v; memcpy(&v, p, 8); return v; }
%s
int main(void)
{
    srand(2026);
    for (int it = 0; it < 60000; it++) {
        size_t len = (it & 1) ? (size_t)(1 + rand() %% 9000) : ((rand() %% 2) ? 4320 : 2160);
        uint8_t *row = malloc(len), *copy = malloc(len), *was = malloc(len);
        for (size_t i = 0; i < len; i++) row[i] = rand();
        memcpy(copy, row, len);
        for (int m = rand() %% 4; m > 0; m--) {
            size_t at = rand() %% len, n = 1 + rand() %% 64;
            for (size_t i = at; i < at + n && i < len; i++) row[i] = rand();
        }
        memcpy(was, copy, len);
        size_t b0 = 0, b1 = 0;
        bool changed = vga_untracked_row_changed(row, copy, len, &b0, &b1);
        if (changed != (memcmp(row, was, len) != 0)) return 1;
        if (changed) {
            if (!(b0 < b1 && b1 <= len) || row[b0] == was[b0] || row[b1 - 1] == was[b1 - 1]) return 2;
            for (size_t i = 0; i < len; i++) {
                if (row[i] != was[i] && (i < b0 || i >= b1)) return 3;
                if (copy[i] != ((i >= b0 && i < b1) ? row[i] : was[i])) return 4;
            }
        } else if (memcmp(copy, was, len)) {
            return 5;
        }
        free(row); free(copy); free(was);
    }
    puts("ok");
    return 0;
}
'''


def touched(name):
    return [ln.split()[2][2:] for ln in (ROOT / name).read_text().splitlines() if ln.startswith('diff --git')]


def row_routine():
    """vga_untracked_row_changed as the patch adds it."""
    lines, take = [], False
    for ln in (ROOT / PATCH).read_text().splitlines():
        if ln.startswith('+static bool vga_untracked_row_changed'):
            take = True
        if take:
            lines.append(ln[1:])
            if ln == '+}':
                break
    return '\n'.join(lines)


class VgaSources(unittest.TestCase):
    def test_order_hash_files(self):
        paths = [e['path'] for e in LOCK['retroboot_patches']]
        self.assertGreater(paths.index(PATCH), max(paths.index(p) for p in AFTER))
        e = LOCK['retroboot_patches'][paths.index(PATCH)]
        self.assertEqual(e['sha256'], hashlib.sha256((ROOT / PATCH).read_bytes()).hexdigest())
        self.assertEqual(sorted(e['files']), sorted(touched(PATCH)))
        self.assertEqual(set(touched(PATCH)), {'hw/display/vga.c', 'hw/display/vga_int.h'})

    def test_only_untracked_scanout_changes(self):
        text = (ROOT / PATCH).read_text()
        # the comparison runs only for a shared surface over untracked VRAM; tracked
        # VRAM keeps its dirty-page snapshot
        self.assertIn('+        if (s->untracked_vram_active && y < s->untracked_rows) {', text)
        removed = [ln for ln in text.splitlines() if ln.startswith('-') and not ln.startswith('---')]
        self.assertFalse([ln for ln in removed if 'memory_region_snapshot' in ln], removed)
        self.assertNotIn('DEFINE_PROP', text)

    def test_row_comparison(self):
        cc = shutil.which(os.environ.get('CC', 'cc')) or shutil.which('gcc')
        if not cc:
            self.skipTest('no C compiler')
        src = row_routine()
        self.assertTrue(src.startswith('static bool vga_untracked_row_changed'))
        with tempfile.TemporaryDirectory() as tmp:
            c, exe = Path(tmp) / 'rows.c', Path(tmp) / 'rows'
            c.write_text(HARNESS % src)
            subprocess.run([cc, '-O2', '-Wall', '-Werror', '-o', str(exe), str(c)], check=True)
            run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120)
            self.assertEqual((run.returncode, run.stdout.strip()), (0, 'ok'))


if __name__ == '__main__':
    unittest.main()
