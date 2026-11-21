#!/usr/bin/env python3
"""Collect the license files for the release's locked dependencies."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil


def collect(archive, name, license_id, files):
    if not files:
        raise ValueError(f'no license files found for {name}')
    notices = {}
    for source in sorted(files):
        destination = archive / 'LICENSES' / name / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        notices[destination.relative_to(archive).as_posix()] = hashlib.sha256(
            destination.read_bytes()
        ).hexdigest()
    return {'package': name, 'license': license_id, 'notices': notices}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('metadata', 'archive', 'grammar', 'tree_sitter', 'rust_sysroot'):
        parser.add_argument(arg, type=Path)
    args = parser.parse_args()
    entries = []
    metadata = json.loads(args.metadata.read_bytes())
    for package in sorted(metadata['packages'], key=lambda p: (p['name'], p['version'])):
        if package['source'] is None:  # The archive carries Binit's own license at its root.
            continue
        directory = Path(package['manifest_path']).parent
        files = {
            path for path in directory.iterdir()
            if path.is_file() and path.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE'))
        }
        if package['license_file']:
            files.add(directory / package['license_file'])
        entries.append(collect(args.archive, f"{package['name']}-{package['version']}",
                               package['license'], files))
    for name, source in [('tree-sitter-bash', args.grammar), ('tree-sitter', args.tree_sitter)]:
        entries.append(collect(args.archive, name, 'MIT', {source / 'LICENSE'}))
    docs = args.rust_sysroot / 'share/doc/rust'
    stdlib = {docs / 'COPYRIGHT-library.html'}
    stdlib.update((docs / 'html/static.files').glob('LICENSE-*.txt'))
    entries.append(collect(args.archive, 'rust-stdlib', 'MIT OR Apache-2.0', stdlib))
    (args.archive / 'dependency-notices.json').write_text(json.dumps(entries, indent=2) + '\n')


if __name__ == '__main__':
    main()
