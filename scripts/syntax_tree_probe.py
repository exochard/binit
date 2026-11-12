#!/usr/bin/env python3
"""Compare advisory parser spans and incremental edits on the frozen corpus."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess

from syntax_probe import CORPUS, PROBE, digest
from fetch_syntax import verify as verify_editor_source


def build(destination, source):
    manifest = json.loads((PROBE / 'tree-source.json').read_text())
    if digest(source / 'src/parser.c') == manifest['files']['src/parser.c']:
        for name, expected in manifest['files'].items():
            if digest(source / name) != expected:
                raise ValueError(f'incremental grammar source mismatch: {name}')
    else:
        verify_editor_source(source)
    if destination.exists():
        raise ValueError('incremental experiment requires a fresh destination')
    configure = ['cmake', '-S', str(PROBE), '-B', str(destination), f'-DTREE_SITTER_GRAMMAR={source}']
    for variable, option in [('BINIT_BASH_INCLUDE', 'BASH_INCLUDE_DIR'),
                             ('BINIT_READLINE_INCLUDE', 'READLINE_INCLUDE_DIR')]:
        if os.environ.get(variable):
            configure.append(f'-D{option}={os.environ[variable]}')
    subprocess.run(configure, check=True)
    subprocess.run(['cmake', '--build', str(destination), '--parallel', '2'], check=True)
    runtime = os.environ.get('BINIT_SYNTAX_RUNTIME')
    runtime_version = '0.25.10 (pinned static)' if runtime else subprocess.check_output(['pkg-config', '--modversion', 'tree-sitter'], text=True).strip()
    metadata = {'source': manifest, 'source_parser_sha256': digest(source / 'src/parser.c'),
                'probe_sha256': digest(PROBE / 'tree_probe.c'),
                'driver_sha256': digest(Path(__file__)), 'corpus_sha256': digest(CORPUS),
                'binary_sha256': digest(destination / 'binit_tree_probe'),
                'grammar_sha256': digest(destination / 'libbash_grammar.so'),
                'tree_sitter_version': runtime_version,
                'release_qualified': False}
    (destination / 'build.json').write_text(json.dumps(metadata, indent=2) + '\n')


def check(binary, grammar, output, bash):
    rows, edits, times, sequences = [], [], [], []
    for case in json.loads(CORPUS.read_text())['cases']:
        text = case['text']
        def parse(old, new):
            # Match the controlled alias configured by syntax_probe.SCRIPT.
            result = subprocess.run([str(binary), old, new, 'magic=if true; then'], check=True,
                                    capture_output=True, text=True, timeout=5)
            value = json.loads(result.stdout)
            times.append(value['incremental_ns'])
            return value
        parsed = parse('', text)
        spans = set(map(tuple, parsed['nodes']))
        expected = {}
        for fragment in case['spans']:
            raw = fragment.encode()
            start = text.encode().index(raw)
            expected[fragment] = (start, start + len(raw)) in spans
        rows.append({'id': case['id'], 'classification_matches': parsed['parsed'] == case['parsed'],
                     'spans': expected, 'parse': parsed})
        for i in range(len(text)):
            for old, new in [(text[:i], text[:i + 1]), (text[:i + 1], text[:i])]:
                result = parse(old, new)
                edits.append({'id': case['id'], 'old_sha256': hashlib.sha256(old.encode()).hexdigest(),
                              'new_sha256': hashlib.sha256(new.encode()).hexdigest(),
                              'matches': result['incremental_matches_fresh']})
        middle = len(text) // 2
        result = parse(text, text[:middle] + 'x\n' + text[middle:])
        edits.append({'id': case['id'], 'operation': 'middle-insertion', 'matches': result['incremental_matches_fresh']})
        sequence = subprocess.run([str(binary), '--sequence', text], check=True,
                                  capture_output=True, text=True, timeout=5)
        sequences.append({'id': case['id'], **json.loads(sequence.stdout)})
    dialects = []
    dialect_cases = json.loads((PROBE / 'dialects.json').read_text())['cases']
    dialect_cases += json.loads((PROBE / 'dialects-extra.json').read_text())['cases']
    for case in dialect_cases:
        reference = subprocess.run([str(bash), '--noprofile', '--norc', '-n', '-c', case['text']],
                                   capture_output=True, text=True, timeout=5)
        candidate = json.loads(subprocess.check_output([str(binary), '', case['text']], text=True, timeout=5))
        dialects.append({'id': case['id'], 'bash_accepts': reference.returncode == 0, 'tree_accepts': candidate['parsed']})
    report = {'schema': 1, 'binary_sha256': digest(binary), 'grammar_sha256': digest(grammar),
              'corpus_sha256': digest(CORPUS), 'driver_sha256': digest(Path(__file__)),
              'cases': rows, 'edits': edits, 'sequences': sequences,
              'dialects': dialects, 'dialects_sha256': digest(PROBE / 'dialects.json'), 'bash_sha256': digest(bash),
              'extra_dialects_sha256': digest(PROBE / 'dialects-extra.json'),
              'incremental_median_ns': statistics.median(times),
              'incremental_max_ns': max(times), 'release_qualified': False}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'cases': len(rows), 'edits': len(edits),
                      'edit_failures': sum(not r['matches'] for r in edits),
                      'sequence_failures': sum(not r['sequence_matches'] for r in sequences),
                      'sequence_steps': sum(r['steps'] for r in sequences),
                      'dialect_differences': [r['id'] for r in dialects if r['bash_accepts'] != r['tree_accepts']],
                      'classification_failures': [r['id'] for r in rows if not r['classification_matches']],
                      'span_failures': [r['id'] for r in rows if not all(r['spans'].values())],
                      'median_incremental_us': statistics.median(times) / 1000,
                      'max_incremental_us': max(times) / 1000}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    builder = commands.add_parser('build')
    builder.add_argument('destination', type=Path)
    builder.add_argument('--source', required=True, type=Path)
    checker = commands.add_parser('check')
    checker.add_argument('--binary', required=True, type=Path)
    checker.add_argument('--grammar', required=True, type=Path)
    checker.add_argument('--output', required=True, type=Path)
    checker.add_argument('--bash', required=True, type=Path)
    args = parser.parse_args()
    if args.command == 'build':
        build(args.destination.resolve(), args.source.resolve())
    else:
        check(args.binary.resolve(), args.grammar.resolve(), args.output.resolve(), args.bash.resolve())
