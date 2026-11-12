#!/usr/bin/env python3
"""Acquire checksum-pinned ecosystem fixtures before offline shell checks."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request

MANIFEST = Path(__file__).with_name('plugin-fixtures.json')
LIMIT = 2 * 1024 * 1024


def fetch(destination):
    destination.mkdir(parents=True, exist_ok=False)
    manifest = json.loads(MANIFEST.read_text())
    for item in manifest['files']:
        url = f"https://raw.githubusercontent.com/{item['repository']}/{item['commit']}/{item['path']}"
        request = urllib.request.Request(url, headers={'User-Agent': 'binit-compatibility'})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(LIMIT + 1)
        if len(data) > LIMIT or hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError(f"fixture size or checksum mismatch: {item['destination']}")
        path = destination / item['destination']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (destination / 'sources.json').write_text(json.dumps(manifest, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    fetch(parser.parse_args().destination)
