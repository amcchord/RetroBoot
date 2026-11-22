#!/usr/bin/env python3
"""Merge Appearance theme collections into two installable trees.

    themes.py --out DIR [--exclude guest/sets/themes-exclude.json] TREE [TREE ...]

Each TREE is a canonical tree from ``python3 -m rbapps expand`` of a theme
collection archive.  Writes DIR/theme-files (every 'thme' file, for
``System Folder:Appearance:Theme Files``), DIR/sound-sets (every 'tsnd' file,
for ``System Folder:Appearance:Sound Sets``) and DIR/themes-report.json.

Rules: only files of type 'thme' / 'tsnd' with a resource fork of at least
1 KiB are taken (the collections also hold read-mes, icons, pictures, aliases
and stray empty forks); identical files (same data and resource fork hashes)
are kept once; two different files with the same name keep the newer one
(modification date, then size) and the other is listed under ``replaced``;
names that collide with ``--reserve`` names (Mac OS's own theme files) are
skipped, and so are theme files named in ``--exclude`` (a JSON file whose
"exclude" maps a reason to a list of names; the Appearance control panel
quits once it lists too many themes).  Folder structure of the collections is flattened, the way the
Appearance control panel expects.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'apps'))
from rbapps import tree as rtree  # noqa: E402

KINDS = {'thme': 'theme-files', 'tsnd': 'sound-sets'}
MIN_RSRC = 1024


def merge(trees: list[Path], out: Path, reserve: set[str], exclude: set[str] = frozenset()) -> dict:
    chosen: dict[tuple[str, str], tuple] = {}
    report = {'sources': [], 'replaced': [], 'duplicates': 0, 'skipped': []}
    for root in trees:
        t = rtree.Tree(root)
        report['sources'].append({'tree': str(root), 'archive': t.manifest.get('source', {}).get('name'),
                                  'sha256': t.manifest.get('source', {}).get('sha256')})
        for e in t.entries:
            if e['kind'] != 'file' or e.get('type') not in KINDS:
                continue
            name = e['path'][-1]
            where = ':'.join(e['path'])
            if e['rsrc']['size'] < MIN_RSRC:
                report['skipped'].append({'path': where, 'reason': 'no-resource-fork'})
                continue
            if name.casefold() in reserve:
                report['skipped'].append({'path': where, 'reason': 'mac-os-name'})
                continue
            if e['type'] == 'thme' and name in exclude:
                report['skipped'].append({'path': where, 'reason': 'excluded'})
                continue
            key = (e['type'], name.casefold())
            ident = (e['data']['sha256'], e['rsrc']['sha256'])
            cand = (e.get('modified') or '', e['data']['size'] + e['rsrc']['size'], ident, t, e, where)
            have = chosen.get(key)
            if have is None:
                chosen[key] = cand
            elif have[2] == ident:
                report['duplicates'] += 1
            else:
                keep, drop = (cand, have) if cand[:2] > have[:2] else (have, cand)
                chosen[key] = keep
                report['replaced'].append({'kept': keep[5], 'kept_modified': keep[0],
                                           'dropped': drop[5], 'dropped_modified': drop[0]})
    writers = {k: rtree.TreeWriter(out / v, {'kind': 'retroboot-baseos-themes', 'collection': v})
               for k, v in KINDS.items()}
    counts = {v: {'files': 0, 'bytes': 0} for v in KINDS.values()}
    for (typ, _), (_, size, _, t, e, where) in sorted(chosen.items(), key=lambda kv: kv[0]):
        fi = bytearray(t.finder_info(e))
        fi[10:14] = bytes(4)                     # no stale window position: Finder places it
        writers[typ].file([e['path'][-1]], t.read_data(e), t.read_rsrc(e), bytes(fi), e.get('created'), e.get('modified'))
        counts[KINDS[typ]]['files'] += 1
        counts[KINDS[typ]]['bytes'] += size
    for w in writers.values():
        w.finish()
    report['installed'] = counts
    report['names'] = {KINDS[k]: sorted(c[4]['path'][-1] for (kk, _), c in chosen.items() if kk == k) for k in KINDS}
    (out / 'themes-report.json').write_text(json.dumps(report, indent=1, ensure_ascii=False) + '\n')
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--reserve', action='append', default=[], help='a theme name Mac OS already has')
    ap.add_argument('--exclude', type=Path, help='JSON file: {"exclude": {reason: [theme names]}}')
    ap.add_argument('trees', nargs='+', type=Path)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    exclude = set()
    if a.exclude:
        exclude = {n for v in json.loads(a.exclude.read_text())['exclude'].values() for n in v}
    r = merge(a.trees, a.out, {x.casefold() for x in a.reserve}, exclude)
    missing = sorted(exclude - {n for n in r['names']['theme-files']} - {
        s['path'].split(':')[-1] for s in r['skipped'] if s['reason'] == 'excluded'})
    if missing:
        raise SystemExit(f'--exclude names not in the collections: {missing}')
    print(json.dumps({k: r[k] for k in ('installed', 'duplicates')} | {'replaced': len(r['replaced']),
                     'skipped': len(r['skipped'])}, indent=1))


if __name__ == '__main__':
    main()
