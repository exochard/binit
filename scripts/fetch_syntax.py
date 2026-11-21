#!/usr/bin/env python3
"""Fetch the pinned advisory Bash grammar before an offline build."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import urllib.request

MANIFEST = Path(__file__).resolve().parent.parent / 'native/syntax-probe/tree-source.json'
LIMIT = 16 * 1024 * 1024
RUNTIME_COMMIT = '208c6cac1453315e979f05ab34b6d4f7cd0340be'
RUNTIME_SHA256 = '13d2d68e6fb28cf9124fd9fbc672356882c9568aad114246cd3a6fbad589d9ed'
GRAMMAR_FILES = {
    'grammar.js': '3c125330d995968a7e3a4fc71617aa73472449f5a6c254f39da2d8860a47bf65',
    'tree-sitter.json': 'd58a0b1c3a697323c10303a4f7a81782d5bdcfc74c3ed78e2bab4575f531ea3b',
}
PATCHED_GRAMMAR_SHA256 = 'e82404d1aff3d494f0a6de63de82d5255abf6ca7e2a24998c2a81f86aef20d9e'
PATCHED_PARSER_SHA256 = 'a015fb30e94e15431d667e829a846417abcd1e4a48b08bf0f6ab1a9cf12cec6b'


def fetch(destination):
    manifest = json.loads(MANIFEST.read_text())
    destination.mkdir(parents=True, exist_ok=False)
    for name, expected in {**manifest['files'], **GRAMMAR_FILES}.items():
        url = f"https://raw.githubusercontent.com/{manifest['repository']}/{manifest['commit']}/{name}"
        request = urllib.request.Request(url, headers={'User-Agent': 'binit-syntax'})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(LIMIT + 1)
        if len(data) > LIMIT or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f'grammar size or checksum mismatch: {name}')
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    version = subprocess.check_output(['tree-sitter', '--version'], text=True).strip()
    if version != 'tree-sitter 0.26.9':
        raise ValueError('syntax preparation requires tree-sitter CLI 0.26.9')
    path = destination / 'grammar.js'
    grammar = path.read_text()
    changes = [
        ("      optional(prec(1, ';;')),", "      optional(prec(1, choice(';;', ';&', ';;&'))),"),
        ('    command_substitution: $ => choice(\n',
         "    command_substitution: $ => choice(\n      seq(token(/\\$\\{[ \\t\\n]+/), $._statements, '}'),\n      seq('${|', $._statements, '}'),\n"),
    ]
    for old, new in changes:
        if grammar.count(old) != 1:
            raise ValueError('pinned grammar patch context mismatch')
        grammar = grammar.replace(old, new, 1)
    path.write_text(grammar)
    pinned_headers = {name: (destination / name).read_bytes() for name in manifest['files']
                      if name.startswith('src/tree_sitter/')}
    subprocess.run(['tree-sitter', 'generate'], cwd=destination, check=True)
    for name, data in pinned_headers.items():
        (destination / name).write_bytes(data)
    verify(destination)


def verify(destination):
    manifest = json.loads(MANIFEST.read_text())
    files = {**manifest['files'], **GRAMMAR_FILES, 'grammar.js': PATCHED_GRAMMAR_SHA256,
             'src/parser.c': PATCHED_PARSER_SHA256}
    for name, expected in files.items():
        path = destination / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > LIMIT:
            raise ValueError(f'missing regular grammar file: {name}')
        data = path.read_bytes()
        if len(data) > LIMIT or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f'grammar size or checksum mismatch: {name}')


def runtime(prefix):
    prefix.mkdir(parents=True, exist_ok=False)
    url = f'https://codeload.github.com/tree-sitter/tree-sitter/tar.gz/{RUNTIME_COMMIT}'
    request = urllib.request.Request(url, headers={'User-Agent': 'binit-syntax'})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(LIMIT + 1)
    if len(data) > LIMIT or hashlib.sha256(data).hexdigest() != RUNTIME_SHA256:
        raise ValueError('tree-sitter runtime archive checksum mismatch')
    archive = prefix / 'source.tar.gz'
    archive.write_bytes(data)
    expected_root = f'tree-sitter-{RUNTIME_COMMIT}/'
    source = prefix / 'source'
    source.mkdir()
    total = 0
    with tarfile.open(archive, 'r:gz') as tar:
        for member in tar:
            if member.name.rstrip('/') == expected_root.rstrip('/') and member.isdir():
                continue
            if not member.name.startswith(expected_root):
                raise ValueError('unexpected runtime archive path')
            name = member.name[len(expected_root):]
            if name != 'LICENSE' and not name.startswith(('lib/src/', 'lib/include/')):
                continue
            parts = Path(name).parts
            if not parts or any(part in ('.', '..') for part in parts) or not (member.isfile() or member.isdir()):
                raise ValueError(f'unsafe runtime archive entry: {name}')
            total += member.size
            if total > LIMIT:
                raise ValueError('runtime source exceeds size limit')
            target = source.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as data_file, target.open('xb') as output:
                    output.write(data_file.read(LIMIT + 1))
    lib = prefix / 'lib'
    lib.mkdir()
    obj = lib / 'tree-sitter.o'
    subprocess.run([os.environ.get('CC', 'cc'), '-std=c11', '-O2', '-fPIC', '-D_DEFAULT_SOURCE',
                    '-I', str(source / 'lib/include'), '-I', str(source / 'lib/src'),
                    '-c', str(source / 'lib/src/lib.c'), '-o', str(obj)], check=True)
    subprocess.run(['ar', 'rcs', str(lib / 'libtree-sitter.a'), str(obj)], check=True)
    receipt = {'archive_sha256': RUNTIME_SHA256,
               'library_sha256': hashlib.sha256((lib / 'libtree-sitter.a').read_bytes()).hexdigest(),
               'header_sha256': hashlib.sha256((source / 'lib/include/tree_sitter/api.h').read_bytes()).hexdigest()}
    (prefix / 'runtime.json').write_text(json.dumps(receipt, indent=2) + '\n')


def verify_runtime(prefix):
    receipt = json.loads((prefix / 'runtime.json').read_text())
    files = {'archive_sha256': prefix / 'source.tar.gz', 'library_sha256': prefix / 'lib/libtree-sitter.a',
             'header_sha256': prefix / 'source/lib/include/tree_sitter/api.h'}
    if receipt.get('archive_sha256') != RUNTIME_SHA256:
        raise ValueError('runtime archive receipt mismatch')
    for key, path in files.items():
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != receipt.get(key):
            raise ValueError(f'runtime verification failed: {key}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path, nargs='?')
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--runtime-prefix', type=Path)
    args = parser.parse_args()
    if not (args.destination or args.runtime_prefix):
        parser.error('specify a grammar destination or --runtime-prefix')
    if args.destination:
        (verify if args.verify else fetch)(args.destination)
    if args.runtime_prefix:
        (verify_runtime if args.verify else runtime)(args.runtime_prefix)
