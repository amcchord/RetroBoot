#!/usr/bin/env python3
"""Make disk variants that each keep only one part of the added theme files and
sound sets (for finding an item that crashes the Appearance control panel).

    theme_bisect.py FULL.img MERGED_DIR OUTDIR PARTS [--only NAMES.json]

FULL.img has every merged item installed (themes.py + offline_install.py);
each OUTDIR/part-K.img is a copy with all added items removed except part K
of the candidates (candidates = every added item, or the names in --only).
Writes OUTDIR/parts.json.  Needs root + hfsplus (rb-dev).
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    full, merged, out, parts = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4])
    only = None
    if '--only' in sys.argv:
        only = set(json.loads(Path(sys.argv[sys.argv.index('--only') + 1]).read_text()))
    items = []
    for sub, folder in (('theme-files', 'Theme Files'), ('sound-sets', 'Sound Sets')):
        m = json.loads((merged / sub / 'manifest.json').read_text())
        items += [f'System Folder:Appearance:{folder}:{e["path"][-1]}' for e in m['entries']]
    cands = [i for i in items if only is None or i in only]
    out.mkdir(parents=True, exist_ok=True)
    k = max(1, -(-len(cands) // parts))
    chunks = [cands[i:i + k] for i in range(0, len(cands), k)]
    for n, keep in enumerate(chunks):
        img = out / f'part-{n}.img'
        subprocess.run(['cp', '--sparse=always', full, str(img)], check=True)
        args = [sys.executable, str(HERE / 'offline_install.py'), str(img)]
        for i in items:
            if i not in keep:
                args += ['--remove', i]
        subprocess.run(args, check=True, stdout=subprocess.DEVNULL)
    (out / 'parts.json').write_text(json.dumps({f'part-{n}': c for n, c in enumerate(chunks)}, indent=1,
                                               ensure_ascii=False))
    print({f'part-{n}': len(c) for n, c in enumerate(chunks)})


if __name__ == '__main__':
    main()
