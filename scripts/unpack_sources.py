#!/usr/bin/env python3
"""Verify an explicit source bundle and extract it into a new trial directory."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile

MAX_TAR_BYTES = 132 * 1024 * 1024


def regular_file(path, limit):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError('expected a bounded regular source file')


def relative_name(name):
    if not isinstance(name, str):
        raise ValueError('source name must be text')
    parts = PurePosixPath(name).parts
    if (not parts or name != '/'.join(parts) or PurePosixPath(name).is_absolute()
            or any(part in ('.', '..', '.git') or part.startswith('.env') for part in parts)
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ValueError('unsafe source path')
    return parts


def unpack(bundle, destination):
    bundle = Path(bundle).resolve(strict=True)
    manifest_path = bundle / 'sources.json'
    regular_file(manifest_path, 2 * 1024 * 1024)
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('schema') != 1 or manifest.get('complete') is not True:
        raise ValueError('source bundle is incomplete')
    if len(manifest['plugins']) > 64:
        raise ValueError('too many selected plugins')
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    selected = [('core', manifest['core'])]
    identifiers = set()
    for plugin in manifest['plugins']:
        identifier = plugin['id']
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', identifier) or identifier in identifiers:
            raise ValueError('invalid or duplicate plugin identifier')
        identifiers.add(identifier)
        selected.append(('plugins/' + identifier, plugin))
    for subdirectory, receipt in selected:
        archive_name = receipt['artifact']
        if len(relative_name(archive_name)) != 1:
            raise ValueError('archive must be inside the bundle')
        archive_path = bundle / archive_name
        regular_file(archive_path, 128 * 1024 * 1024)
        if hashlib.sha256(archive_path.read_bytes()).hexdigest() != receipt['sha256']:
            raise ValueError('source archive checksum mismatch')
        expected = receipt['files']
        if len(expected) > 2048:
            raise ValueError('too many source files')
        target = destination / subdirectory
        target.mkdir(parents=True)
        seen, total = set(), 0
        # Bound extension headers too: tarfile consumes them before yielding a member.
        with gzip.open(archive_path, 'rb') as compressed:
            tar_bytes = compressed.read(MAX_TAR_BYTES + 1)
        if len(tar_bytes) > MAX_TAR_BYTES:
            raise ValueError('decompressed archive exceeds size limit')
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode='r:') as archive:
            for member in archive:
                parts = relative_name(member.name)
                if (not member.isfile() or member.name in seen or member.name not in expected
                        or member.size > 32 * 1024 * 1024):
                    raise ValueError('unexpected source archive member')
                total += member.size
                if total > 128 * 1024 * 1024:
                    raise ValueError('source archive exceeds size limit')
                item = expected[member.name]
                sha = item['sha256'] if isinstance(item, dict) else item
                mode = item['mode'] if isinstance(item, dict) else receipt['modes'][member.name]
                if mode not in (0o644, 0o755) or member.mode != mode:
                    raise ValueError('source file mode mismatch')
                data = archive.extractfile(member).read(member.size + 1)
                if len(data) != member.size or hashlib.sha256(data).hexdigest() != sha:
                    raise ValueError('source file checksum mismatch')
                path = target.joinpath(*parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('xb') as output:
                    output.write(data)
                path.chmod(mode)
                seen.add(member.name)
        if seen != set(expected):
            raise ValueError('source archive is missing files')
    (destination / 'sources.json').write_bytes(manifest_path.read_bytes())
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    try:
        unpack(args.bundle, args.destination)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        parser.exit(1, f'unpack-sources: {error}\n')
    print(f'Source trial: {args.destination}')
