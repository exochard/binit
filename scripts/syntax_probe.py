#!/usr/bin/env python3
"""Build and measure an isolated, non-production Bash syntax experiment."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tarfile
import tempfile
import time
from unittest.mock import patch

import ci_bash
import terminal

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / 'native/syntax-probe'
CORPUS = PROBE / 'corpus.json'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(destination, archive):
    destination = destination.resolve()
    if destination.exists():
        raise ValueError('syntax experiment requires a fresh destination')
    if digest(archive) != ci_bash.SOURCES['5.3'][1]:
        raise ValueError('Bash source archive checksum mismatch')
    destination.mkdir(parents=True)
    with tarfile.open(archive) as source:
        source.extractall(destination, filter='data')
    source = destination / 'bash-5.3'
    def run(args):
        subprocess.run(args, cwd=source, check=True,
                       env=dict(os.environ, CFLAGS='-O2 -g -std=gnu17'))
    run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(PROBE / 'observer.patch')])
    run(['bison', '-y', '-d', '-o', 'y.tab.c', 'parse.y'])
    prefix = destination / 'install'
    run(['./configure', f'--prefix={prefix}', '--without-bash-malloc'])
    run(['make', '-j2'])
    run(['make', 'install-headers'])
    (prefix / 'bin').mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / 'bash', prefix / 'bin/bash')
    subprocess.run(['cmake', '-S', str(PROBE), '-B', str(destination / 'module'),
                    f'-DBASH_INCLUDE_DIR={prefix}/include/bash', f'-DREADLINE_INCLUDE_DIR={source}/lib'], check=True)
    subprocess.run(['cmake', '--build', str(destination / 'module'), '--parallel', '2'], check=True)
    info = {'source_sha256': digest(archive), 'patch_sha256': digest(PROBE / 'observer.patch'),
            'corpus_sha256': digest(CORPUS), 'driver_sha256': digest(Path(__file__)),
            'module_source_sha256': digest(PROBE / 'probe.c'),
            'bash_sha256': digest(prefix / 'bin/bash'),
            'module_sha256': digest(destination / 'module/binit_syntax_probe.so'),
            'cflags': '-O2 -g -std=gnu17', 'release_qualified': False}
    (destination / 'build.json').write_text(json.dumps(info, indent=2) + '\n')


SCRIPT = r'''
enable -f "$BINIT_SYNTAX_MODULE" binit_syntax_probe || exit 90
export SAMPLE='two words'
sample_array=('one' 'two words')
side_effect=0
IFS=$' :\t'
alias magic='if true; then'
trap ':' DEBUG RETURN USR1
set -o pipefail
shopt -s extglob nullglob
set -- 'two words' '' '*.literal'
false | (exit 7)
if [[ $BINIT_SYNTAX_MODE == noop ]]; then
    false | (exit 7)
    builtin binit_syntax_probe --noop "$BINIT_SYNTAX_TEXT"
else
    false | (exit 7)
    builtin binit_syntax_probe "$BINIT_SYNTAX_TEXT"
fi
printf 'STATE status=%s last=%s pipe=%s\n' "$?" "$_" "${PIPESTATUS[*]}"
printf 'STATE cwd=%s args=' "$PWD"; printf '<%s>' "$@"; printf '\n'
declare -p IFS SAMPLE sample_array side_effect
alias magic
trap -p DEBUG RETURN USR1
set +o
shopt -p extglob nullglob
printf 'STATE survived\n'
'''


def invoke(bash, module, case, directory, mode, debug=True):
    marker = directory / 'must-not-exist'
    env = {key: value for key, value in os.environ.items()
           if key not in ('BASH_ENV', 'ENV', 'SHELLOPTS', 'BASHOPTS') and not key.startswith('BASH_FUNC_')}
    env.update(HOME=str(directory), HISTFILE='/dev/null', INPUTRC='/dev/null', TERM='dumb', LC_ALL='C.UTF-8',
               BINIT_SYNTAX_MODULE=str(module), BINIT_SYNTAX_TEXT=case['text'], BINIT_SYNTAX_MODE=mode,
               BINIT_SYNTAX_MARKER=str(marker), PS1='', PS2='')
    started = time.perf_counter_ns()
    script = SCRIPT if debug else SCRIPT.replace("trap ':' DEBUG RETURN USR1", "trap ':' RETURN USR1")
    result = subprocess.run([str(bash), '--noprofile', '--norc', '-ic', script], cwd=directory,
                            env=env, capture_output=True, text=True, timeout=5)
    elapsed = (time.perf_counter_ns() - started) / 1e6
    lines = result.stdout.splitlines()
    observation = next((json.loads(line) for line in lines if line.startswith('{')), None)
    trailer = '\n'.join(line for line in lines if not line.startswith('{'))
    return {'returncode': result.returncode, 'observation': observation, 'state': trailer,
            'stderr': result.stderr, 'side_effect_file': marker.exists(), 'elapsed_ms': elapsed}


def byte_offset(text, line, column):
    if line < 1:
        return None
    lines = text.splitlines(keepends=True)
    if line > len(lines):
        return None
    if column > len(lines[line - 1]):
        return None
    return sum(len(part) for part in lines[:line - 1]) + column


def span_matches(case, observation):
    data = case['text'].encode()
    actual = set()
    if observation:
        for _, start_line, start, end_line, end in observation['tokens']:
            a, b = byte_offset(data, start_line, start), byte_offset(data, end_line, end)
            if a is not None and b is not None and 0 <= a <= b <= len(data):
                actual.add((a, b))
    result = {}
    for fragment in case['spans']:
        raw = fragment.encode()
        start = data.index(raw)
        result[fragment] = (start, start + len(raw)) in actual
    return result


def check(bash, module, output):
    rows = []
    scratch = ROOT / '.scratch-syntax'
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as temporary:
        directory = Path(temporary)
        for case in json.loads(CORPUS.read_text())['cases']:
            case_directory = directory / case['id']
            case_directory.mkdir()
            baseline = invoke(bash, module, case, case_directory, 'noop')
            try:
                parsed = invoke(bash, module, case, case_directory, 'parse')
                plain_baseline = invoke(bash, module, case, case_directory, 'noop', debug=False)
                plain_parsed = invoke(bash, module, case, case_directory, 'parse', debug=False)
            except subprocess.TimeoutExpired:
                rows.append({'id': case['id'], 'timeout': True, 'state_preserved': False})
                continue
            observation = parsed['observation']
            matches = span_matches(case, observation)
            rows.append({'id': case['id'], 'timeout': False,
                         'state_preserved': all(run['returncode'] == 0 and 'STATE survived' in run['state']
                             for run in (parsed, baseline, plain_parsed, plain_baseline))
                             and parsed['state'] == baseline['state'] and parsed['returncode'] == baseline['returncode']
                             and plain_parsed['state'] == plain_baseline['state'] and plain_parsed['returncode'] == plain_baseline['returncode'],
                         'no_execution': not parsed['side_effect_file'] and not plain_parsed['side_effect_file'],
                         'complete_observation': bool(observation) and observation['observer'] and not observation['overflow'],
                         'classification_matches': bool(observation) and observation['parsed'] == case['parsed'],
                         'spans': matches, 'parse': parsed, 'baseline': baseline,
                         'without_debug': {'parse': plain_parsed, 'baseline': plain_baseline}})
        interruption = interrupt_check(bash, module, directory)
    report = {'schema': 1, 'bash': str(bash), 'bash_sha256': digest(bash), 'module_sha256': digest(module),
              'corpus_sha256': digest(CORPUS), 'driver_sha256': digest(Path(__file__)),
              'interruption': interruption,
              'cases': rows, 'editor_ready': interruption['recovered'] and interruption.get('parser_phase_confirmed', False) and all(row.get('state_preserved') and row.get('no_execution') and row.get('complete_observation')
                  and row.get('classification_matches') and all(row.get('spans', {}).values()) for row in rows),
              'release_qualified': False}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'cases': len(rows), 'editor_ready': report['editor_ready'], 'interruption': interruption,
                      'state_failures': [r['id'] for r in rows if not r['state_preserved']],
                      'classification_failures': [r['id'] for r in rows if not r.get('classification_matches')],
                      'span_failures': [r['id'] for r in rows if not all(r.get('spans', {}).values())]}, indent=2))


def interrupt_check(bash, module, directory):
    rc = directory / 'interrupt-rc'
    rc.write_text(f"PS1='READY> '\nPS2=''\nPROMPT_COMMAND=()\nenable -f {shlex.quote(str(module))} binit_syntax_probe\n")
    try:
        with patch.object(terminal, 'BASH', str(bash)), patch.dict(os.environ, HOME=str(directory), HISTFILE='/dev/null', INPUTRC='/dev/null'):
            shell = terminal.Terminal(rc, directory)
            try:
                capability = shell.command(":; binit_syntax_probe 'printf check'")
                controlled = '"observer":true' in capability
                os.write(shell.fd, b"binit_syntax_probe --stress 'printf safe'\n")
                shell.read_until(b'SYNTAX parser suspended\r\n' if controlled else b'SYNTAX stress entered\r\n')
                os.write(shell.fd, b'\x03')
                shell.read_until(terminal.PROMPT)
                response = shell.command(":; binit_syntax_probe 'printf recovered'; printf 'recovered=%s\\n' \"$?\"")
                clear = '"observer_was_active":false' in response if controlled else None
                return {'recovered': 'recovered=0' in response and clear is not False, 'observer_cleared': clear,
                        'parser_phase_confirmed': controlled,
                        'transcript': shell.transcript.decode(errors='replace')}
            finally:
                shell.close()
    except (AssertionError, OSError) as error:
        return {'recovered': False, 'error': str(error)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    builder = commands.add_parser('build')
    builder.add_argument('destination', type=Path)
    builder.add_argument('--archive', required=True, type=Path)
    checker = commands.add_parser('check')
    checker.add_argument('--bash', required=True, type=Path)
    checker.add_argument('--module', required=True, type=Path)
    checker.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.command == 'build':
        build(args.destination, args.archive.resolve())
    else:
        check(args.bash.resolve(), args.module.resolve(), args.output.resolve())
