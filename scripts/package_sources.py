#!/usr/bin/env python3
"""Export committed core sources and explicitly selected independent plugins."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

from package_plugin import git_read, package as package_plugin


def package_core(source, output):
    source = Path(source).resolve(strict=True)
    top = Path(git_read(source, ['rev-parse', '--show-toplevel'], 16384).decode().strip()).resolve()
    if top != source:
        raise ValueError('core source must be a repository root')
    if git_read(source, ['status', '--porcelain', '--untracked-files=no'], 1024 * 1024):
        raise ValueError('core has uncommitted tracked changes')
    commit = git_read(source, ['rev-parse', 'HEAD'], 128).decode().strip()
    entries = git_read(source, ['ls-tree', '-rz', '--full-tree', commit], 1024 * 1024).split(b'\0')
    files = {}
    total = 0
    for raw in entries:
        if not raw:
            continue
        metadata, raw_name = raw.split(b'\t', 1)
        mode, kind, object_id = metadata.decode().split()
        name = raw_name.decode('utf-8')
        parts = Path(name).parts
        if (mode not in ('100644', '100755') or kind != 'blob' or not parts
                or Path(name).is_absolute() or len(parts) > 16
                or any(part in ('.', '..', '.git') or part.startswith('.env') for part in parts)
                or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise ValueError(f'unsafe core source file: {name}')
        size = int(git_read(source, ['cat-file', '-s', object_id], 32))
        total += size
        if len(files) >= 2048 or size > 32 * 1024 * 1024 or total > 128 * 1024 * 1024:
            raise ValueError('core exceeds the source package limits')
        files[name] = (git_read(source, ['cat-file', 'blob', object_id], size), int(mode, 8) & 0o777)
    if not {'Cargo.toml', 'Cargo.lock', 'LICENSE', 'scripts/demo.bash'} <= files.keys():
        raise ValueError('core source is missing build, license or demo files')
    artifact = Path(output) / f'binit-{commit[:12]}.tar.gz'
    with artifact.open('xb') as stream:
        with gzip.GzipFile(fileobj=stream, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as archive:
                for name, (data, mode) in sorted(files.items()):
                    item = tarfile.TarInfo(name)
                    item.size, item.mode, item.mtime = len(data), mode, 0
                    archive.addfile(item, io.BytesIO(data))
    return dict(commit=commit, artifact=artifact.name, sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
                files={name: dict(sha256=hashlib.sha256(data).hexdigest(), mode=mode)
                       for name, (data, mode) in sorted(files.items())})


def package(source, plugins, output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = dict(schema=1, complete=False, core=None, plugins=[])
    receipt = output / 'sources.json'

    def save():
        receipt.write_text(json.dumps(manifest, indent=2) + '\n')

    save()
    try:
        manifest['core'] = package_core(source, output)
        identifiers = set()
        for plugin in plugins:
            result = package_plugin(plugin, output)
            if result['id'] in identifiers:
                raise ValueError(f'duplicate selected plugin: {result["id"]}')
            identifiers.add(result['id'])
            manifest['plugins'].append(result)
        with (output / 'SHA256SUMS').open('x') as checksums:
            for item in [manifest['core'], *manifest['plugins']]:
                checksums.write(f'{item["sha256"]}  {item["artifact"]}\n')
        manifest['complete'] = True
        save()
    except (OSError, ValueError, KeyError, UnicodeError, tarfile.TarError) as error:
        manifest['error'] = str(error)
        save()
        raise
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--plugin', type=Path, action='append', default=[])
    args = parser.parse_args()
    try:
        result = package(args.source, args.plugin, args.output)
    except (OSError, ValueError, KeyError, UnicodeError, tarfile.TarError) as error:
        parser.exit(1, f'package-sources: {error}\n')
    print(f'Source bundle: {args.output / "sources.json"}')
