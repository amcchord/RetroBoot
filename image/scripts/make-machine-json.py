#!/usr/bin/env python3
"""Register a copied guest machine directory the way hostd expects.

    make-machine-json.py MACHINE_COPY_DIR MACHINE_ID HOSTD_LIB_DIR

Writes machine.json (schema 1, hostd machines.py) and state.json next to the
copied disk.img/config.json when the source had no machine.json. The ClassicMac
config.json is reduced with hostd's own sanitize_template_config when hostd is
part of the image, so the record matches a catalog import. A registered guest
fixture also carries fixture.json (id, name, os_version; lab/fixture-manifest.json
has the same entry), which supplies the machine's name, os_version and source.
Guest preparation (rbguestprep) is left to hostd's first launch: the builder's
kernel has no hfsplus.
"""
import datetime
import json
import os
import pathlib
import re
import sys


def main(mdir, mid, hostd_lib):
    mdir = pathlib.Path(mdir)
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', mid):
        sys.exit(f'invalid machine id {mid!r}')
    if (mdir / 'machine.json').exists():
        return
    raw = (mdir / 'config.json').read_bytes()
    template = None
    family = None
    if os.path.isdir(hostd_lib):
        sys.path.insert(0, hostd_lib)
        try:
            from retroboot.catalog import sanitize_template_config
            template = sanitize_template_config(raw)
        except Exception as error:
            if getattr(error, 'code', None) == 'unsupported':
                sys.exit(f'make-machine-json: {mid}: {error} (machineFamily)')
            print(f'make-machine-json: hostd sanitiser unavailable ({error}); using parsed config', file=sys.stderr)
        try:
            from retroboot.machines import family_fields
            family = family_fields(template if template is not None else json.loads(raw))
        except Exception:
            family = None
    if template is None:
        template = json.loads(raw)
    if family is None:
        # A 68k Mac (hostd machines.family_fields): its family, ROM and PRAM file names.
        family = ({'schema_version': 2, 'family': 'quadra800', 'rom': 'Quadra800.rom', 'pram': 'pram.img'}
                  if isinstance(template, dict) and template.get('machineFamily') == 'quadra800' else {})
        profile = template.get('guestOS', 'classic') if isinstance(template, dict) else 'classic'
        if profile != 'classic' or (isinstance(template, dict) and template.get('machineFamily') == 'powerMacOSX'):
            if profile not in ('macosx-10.2', 'macosx-10.3', 'macosx-10.4') or \
                    template.get('machineFamily') != 'powerMacOSX':
                sys.exit(f'make-machine-json: {mid}: unsupported guestOS')
            family = {'schema_version': 3, 'family': 'powermac', 'guest_os': profile, 'nvram': 'nvram.img'}
    fixture = {}
    if (mdir / 'fixture.json').is_file():
        fixture = json.loads((mdir / 'fixture.json').read_text())
    os_version = fixture.get('os_version')
    if not (isinstance(os_version, str) and re.fullmatch(r'[0-9][0-9.]{0,15}', os_version)):
        os_version = None
    name = fixture.get('name') if isinstance(fixture.get('name'), str) else None
    source = {'kind': 'image-build', 'fixture': fixture.get('id') if isinstance(fixture.get('id'), str) else mid}
    if isinstance(fixture.get('disk_sha256'), str):
        source['disk_sha256'] = fixture['disk_sha256']
    meta = {'schema_version': 1, 'id': mid, 'name': str(name or template.get('name') or 'Mac OS')[:100],
            'os_version': os_version, 'disk': 'disk.img',
            'created': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'source': source, 'template': template, 'prep': None, **family}
    (mdir / 'machine.json').write_text(json.dumps(meta, indent=2) + '\n')
    if not (mdir / 'state.json').exists():
        (mdir / 'state.json').write_text(json.dumps({'clean': True, 'last_exit': None}) + '\n')


if __name__ == '__main__':
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
