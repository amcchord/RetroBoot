#!/usr/bin/env python3
"""Build project-owned 68k color links; never open or modify a game executable."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'apps'))
sys.path.insert(0, str(ROOT))
from rbapps.resfork import parse_resource_fork
from rbapps.tree import Tree, TreeWriter, check_name, file_finder_info, sha256_file
from research.gfx.guestapps.make_tree import read_macbinary, check_68k_app


def plan(spec: dict) -> list[dict]:
    result, ids, paths = [], set(), set()
    for index, game in enumerate(spec['games']):
        original = game['installed_app']
        if len(original) != 3 or original[0] != 'Games':
            raise ValueError('A game must name its original application inside one Games folder')
        title, folder, name = game['title'], original[1], original[2]
        helper = 'Play ' + title
        for part in (folder, name, helper):
            check_name(part)
            if '\0' in part:
                raise ValueError('A Mac name cannot contain NUL')
        depth = game['required_depth']
        if depth not in (4, 8):
            raise ValueError('Only the qualified 16-color/256-color game modes are supported')
        key = (folder.casefold(), helper.casefold())
        if game['id'] in ids or key in paths or helper.casefold() == name.casefold():
            raise ValueError('Duplicate or conflicting game link')
        if index > 799:
            raise ValueError('The reserved helper creator range is exhausted')
        ids.add(game['id']); paths.add(key)
        result.append({'id': game['id'], 'title': title, 'original': original,
                       'helper': ['Games', folder, helper], 'depth': depth,
                       'creator': f'R{200 + index:03d}'})
    if not result:
        raise ValueError('The game selection is empty')
    return result


def config_bytes(item: dict) -> bytes:
    name = item['original'][-1].encode('mac_roman')
    return bytes([1, item['depth'], len(name)]) + name


def build(spec_path: Path, toolchain: Path, out: Path, build_dir: Path) -> dict:
    items = plan(json.loads(spec_path.read_text()))
    if out.exists() or build_dir.exists():
        raise ValueError('Output and build directories must be new')
    tool = toolchain.resolve() / 'm68k-apple-macos/cmake/retro68.toolchain.cmake'
    if not tool.is_file():
        raise ValueError('The read-only Retro68 toolchain is missing')
    build_dir.mkdir(parents=True)
    cmake = build_dir / 'cmake'
    writer = TreeWriter(out, {'kind': 'retroboot-68k-game-color-links',
                             'roster_sha256': sha256_file(spec_path),
                             'source_sha256': sha256_file(HERE / 'color.c')})
    folders, baseline = set(), None
    for item in items:
        config = config_bytes(item)
        resource = build_dir.resolve() / ('config-' + item['creator'] + '.r')
        resource.write_text('data \'RBcf\' (128) { $"' + config.hex() + '" };\n')
        subprocess.run(['cmake', '-S', str(HERE), '-B', str(cmake),
                        '-DCMAKE_TOOLCHAIN_FILE=' + str(tool), '-DCMAKE_BUILD_TYPE=Release',
                        '-DRB_CREATOR=' + item['creator'], '-DRB_CONFIG_RESOURCE=' + str(resource)],
                       check=True)
        # The source and object directory stay fixed: only project resources and
        # application packaging change for each creator/name configuration.
        subprocess.run(['cmake', '--build', str(cmake)], check=True)
        mb = read_macbinary(cmake / 'RetroBootColor.bin')
        check_68k_app(item['title'], mb)
        if mb['creator'] != item['creator'].encode('ascii'):
            raise ValueError('The application creator did not match its configuration')
        resources = parse_resource_fork(mb['rsrc'])
        configs = [blob for kind, ident, _n, _o, blob in resources if (kind, ident) == ('RBcf', 128)]
        if configs != [config]:
            raise ValueError('The built game configuration differs')
        code = {str(ident): hashlib.sha256(blob).hexdigest()
                for kind, ident, _n, _o, blob in resources if kind == 'CODE'}
        if baseline is None:
            baseline = code
        elif code != baseline:
            raise ValueError('The common launcher CODE unexpectedly changed between configurations')
        path = item['helper'][1:]
        if path[0] not in folders:
            writer.folder(path[:1]); folders.add(path[0])
        flags = mb['flags'] & ~0x0100
        writer.file(path, mb['data'], mb['rsrc'],
                    file_finder_info(b'APPL', mb['creator'], flags), mb['created'], mb['modified'])
    writer.finish()
    if Tree(out).verify():
        raise ValueError('The generated canonical tree failed verification')
    mapping = {'format': 'retroboot-68k-game-color-links', 'version': 1,
               'install_destination': ['Games'], 'roster_sha256': sha256_file(spec_path),
               'tree_manifest_sha256': sha256_file(out / 'manifest.json'),
               'code_resource_sha256': baseline, 'links': items}
    (out / 'launchers.json').write_text(json.dumps(mapping, indent=2, ensure_ascii=False) + '\n')
    return {'tree': str(out), 'files': len(items),
            'manifest_sha256': mapping['tree_manifest_sha256'],
            'mapping_sha256': sha256_file(out / 'launchers.json'),
            'code_resource_sha256': baseline}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--roster', type=Path,
                        default=ROOT / 'apps/manifests/68k-games-utilities-v1.json')
    parser.add_argument('--toolchain', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--build-dir', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.roster, args.toolchain, args.out, args.build_dir), indent=2))
