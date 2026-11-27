#!/usr/bin/env python3
"""Measure usable startup with all seven first-party plugins and verify their readiness."""
import argparse
import json
import math
from pathlib import Path
import shlex
import statistics
import subprocess
import sys
import tarfile
import tempfile

import bench_ecosystem as corpus
from bench_development_milestone import BASELINE, BINIT_HASHES, POLICY, ROOT
from bench_ecosystem_primed import inventory_tree, observe
from bench_interactive import PLUGINS, digest

PLUGIN_HASHES = {
    'autosuggestions': '2664b78faf87049bcdbf0af1f87343971893c430777e65f6a75bfd588e76e813',
    'directory-jump': 'c28e10793051608e60556d0bf42883e423ab272f015c4eab89993fd90afb2a99',
    'git-completion': 'ef1cf5f4b87d08b4f6343572008d5d640e408d74d7a080a2ed959d7d8b75c076',
    'git-prompt': 'e17318ec1cef2e6e26de4928eb7df3a8a76f1e7d5d95be0c29871d85f75d26aa',
    'history-search': '4568c1171e37284f738b088f6fa572283e06b6288d151cced17c925e0fe6b03b',
    'project-context': '6292f756e8a087daa3f12ed5b3a0a2ce2665cd58b824292ce9e80ee26f504f91',
    'syntax-highlighting': 'fc7407bab9d6a8e4b5ece6e0672fdd7c25804bcf81703339cd1ad6fc993f4dbb',
}


def prepare(directory, binary, packages, bash, editor):
    directory.mkdir()
    home = directory / 'home'
    home.mkdir()
    environment = corpus.environment(home) | {'BINIT_BASH': str(bash)}
    subprocess.run(['git', 'init', '-q', '-b', 'bench-branch', directory], env=environment, check=True)
    (directory / '.binit-project.toml').write_text('[env]\nBENCH_PROJECT = "ready"\n')
    history = directory / 'history'
    history.write_text(''.join(f'git status --short # {i:04d}\n' for i in range(1000)))
    state = directory / 'state'
    config = directory / 'config.toml'
    config.write_text('schema=1\n' + ''.join(
        '\n[[plugins]]\npath=' + json.dumps(str(packages / name)) + '\n' for name in PLUGINS))
    for command in (['prepare', config], ['trust', directory]):
        subprocess.run([binary, *command, '--state', state], env=environment, check=True,
                       stdout=subprocess.DEVNULL)
    init = shlex.join([str(binary), 'init', '--state', str(state)])
    rc = directory / 'rc'
    ready = '\n'.join(
        f'    [[ ${{__binit_state[{plugin}]-}} == {status} ]] || return 21'
        for plugin, status in ((name, 'pending' if name == 'git-completion' else 'loaded') for name in PLUGINS))
    rc.write_text(
        f"set -o {editor}\nPS1='BENCH> '\nHISTFILE=/dev/null\nHISTSIZE=1000\n"
        f"history -r {shlex.quote(str(history))}\n"
        f'eval "$({init})"\n'
        'corpus_verify() {\n'
        f'{ready}\n'
        '    [[ ${BENCH_PROJECT-} == ready && ${__binit_segment_value[git]-} == bench-branch ]] || return 22\n'
        '    [[ ${__binit_jump_scores[$PWD]-0} -ge 1 && $(type -t hs) == function ]] || return 23\n'
        '    local editor_state\n'
        '    editor_state=$(builtin binit_complete editor status) || return 24\n'
        '    [[ $editor_state =~ suggestions[[:blank:]]+on ]] || return 25\n'
        '    [[ $editor_state =~ history[[:blank:]]+search ]] || return 26\n'
        '    [[ $editor_state =~ highlighting[[:blank:]]+on ]] || return 27\n'
        '}\n')
    return directory, rc, home


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--candidate-plugins', required=True, type=Path)
    parser.add_argument('--baseline-dir', default=BASELINE, type=Path)
    parser.add_argument('--bash', default=Path('/usr/bin/bash'), type=Path)
    parser.add_argument('--editor', choices=['emacs', 'vi'], required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--pairs', default=20, type=int)
    parser.add_argument('--runs', default=2, type=int)
    args = parser.parse_args()
    for argument in ('candidate', 'candidate_plugins', 'baseline_dir', 'bash', 'output'):
        setattr(args, argument, getattr(args, argument).resolve())
    if not args.output.is_relative_to(ROOT / 'tmp') or args.output.exists() or min(args.pairs, args.runs) < 1:
        parser.error('use a fresh output under tmp/ and positive counts')
    abi = subprocess.check_output([args.bash, '-c', 'printf "%s%s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'],
                                  text=True)
    baseline = args.baseline_dir / f'binit-{abi}'
    if abi not in BINIT_HASHES or digest(baseline) != BINIT_HASHES[abi]:
        parser.error('use a frozen baseline with matching Bash 5.2 or 5.3')
    policy = json.loads(POLICY.read_text())
    archives = {name: args.baseline_dir / f'{name}.tar.gz' for name in PLUGINS}
    for name, archive in archives.items():
        if digest(archive) != PLUGIN_HASHES[name]:
            raise RuntimeError(f'{name}: frozen plugin archive changed')
    candidate_packages = {name: inventory_tree(args.candidate_plugins / name) for name in PLUGINS}
    inputs = [POLICY, Path(__file__), args.bash, baseline, args.candidate, Path(sys.executable),
              *archives.values(), *(ROOT / 'scripts' / f'{name}.py' for name in (
                  'bench_interactive', 'bench_ecosystem', 'bench_ecosystem_primed', 'bench_corpus_terminal'))]
    identities = {str(path): digest(path) for path in inputs}
    result = {'schema': 1, 'abi': abi, 'editor': args.editor, 'input_sha256': identities,
              'candidate_plugins': candidate_packages, 'runs': [], 'release_gate_pass': False,
              'pending': ['equivalent Zinit plugin workload']}
    args.output.mkdir()
    receipt = args.output / 'result.json'
    try:
        with tempfile.TemporaryDirectory(dir=args.output) as scratch:
            work = Path(scratch)
            baseline_packages = work / 'baseline-packages'
            for name, archive in archives.items():
                destination = baseline_packages / name
                destination.mkdir(parents=True)
                with tarfile.open(archive) as bundle:
                    bundle.extractall(destination, filter='data')
            arms = {
                'baseline': prepare(work / 'baseline', baseline, baseline_packages, args.bash, args.editor),
                'candidate': prepare(work / 'candidate', args.candidate, args.candidate_plugins, args.bash, args.editor),
            }
            for name, (directory, rc, home) in arms.items():
                observe('binit', str(args.bash), rc, directory, home, args.output / f'prime-{name}.trace')
            homes = {name: inventory_tree(home) for name, (_, _, home) in arms.items()}
            fixtures = [path for directory, rc, _ in arms.values()
                        for path in (rc, directory / 'config.toml', directory / 'history',
                                     directory / '.binit-project.toml', directory / 'state/current')]
            result['fixture_sha256'] = {str(path.relative_to(work)): digest(path) for path in fixtures}
            for run in range(args.runs):
                pairs = []
                result['runs'].append(pairs)
                for index in range(args.pairs):
                    pair = {}
                    for name in list(arms)[::1 if (run + index) % 2 == 0 else -1]:
                        directory, rc, home = arms[name]
                        pair[name] = observe('binit', str(args.bash), rc, directory, home,
                                             args.output / f'{run}-{index}-{name}.trace')
                        if inventory_tree(home) != homes[name]:
                            raise RuntimeError(f'{name}: benchmark home changed')
                    pairs.append(pair)
                    receipt.write_text(json.dumps(result, indent=2) + '\n')
            if any(digest(work / name) != sha256 for name, sha256 in result['fixture_sha256'].items()):
                raise RuntimeError('prepared benchmark fixture changed')
            if any(digest(path) != sha256 for path, sha256 in identities.items()) or any(
                    inventory_tree(args.candidate_plugins / name) != files for name, files in candidate_packages.items()):
                raise RuntimeError('benchmark inputs changed')
            result['ratios'] = [statistics.median(pair['candidate']['startup_usable_ms'] /
                                                  pair['baseline']['startup_usable_ms'] for pair in run)
                                for run in result['runs']]
            threshold = policy['workloads'][0]['seven_plugin_ratio_max']
            result['binit_comparison_pass'] = (args.runs == 2 and args.pairs == 20 and
                                               all(math.isfinite(r) and 0 < r <= threshold for r in result['ratios']))
    except BaseException as error:
        result['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        receipt.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
