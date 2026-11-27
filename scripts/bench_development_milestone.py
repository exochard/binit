#!/usr/bin/env python3
"""Measure primed framework startup against the frozen development inputs."""
import argparse
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import tarfile

import bench_ecosystem as corpus
import bench_ecosystem_primed as primed
from bench_interactive import digest

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / 'benchmarks/development-milestone.json'
BASELINE = ROOT / '.handoff/milestone-2026-11-27/baseline'
ZINIT_HASH = 'c5deae6bed31265dff54a131144d69cff1ed47df34929c8567700c5d99c8d784'
BINIT_HASHES = {
    '52': 'c9de8b9233629a0a1d27f910a300598a3b8d43ff1c8618bd54fe9300ec694138',
    '53': '83ea4d630349bd1f46d78410f5ded06dce539f0dec1222366a04ab7fbf35e945',
}


def grade(rows, pairs):
    expected = {(case, pair) for case in ('candidate', 'baseline', 'zinit', 'bash', 'zsh') for pair in range(pairs)}
    values = {}
    for row in rows:
        key = row['case'], row['pair']
        if key not in expected or key in values or row.get('effect_status') != 0 or not row.get('private_state_unchanged'):
            raise ValueError('unexpected, duplicate or failed observation')
        value = row['startup_usable_ms']
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError('invalid startup duration')
        values[key] = value
    if set(values) != expected:
        raise ValueError('incomplete observations')
    return {name: statistics.median(values['candidate', i] / values[name, i] for i in range(pairs))
            for name in ('baseline', 'zinit')}


def grade_runs(runs, pairs, maximum_ratio):
    ratios = [grade(rows, pairs) for rows in runs]
    return {'ratios': ratios, 'pass': len(runs) == 2 and pairs == 20 and all(
        all(value <= maximum_ratio for value in run.values()) for run in ratios)}


def summarize_receipts(receipts):
    cells = {f'{abi}/{count}': 'pending' for abi in ('52', '53') for count in (0, 10, 50)}
    seen = set()
    candidates = {}
    for receipt in receipts:
        key = f'{receipt.get("abi")}/{receipt.get("count")}'
        if key not in cells or key in seen:
            raise ValueError(f'unexpected or duplicate startup cell: {key}')
        seen.add(key)
        if not receipt.get('complete') or receipt.get('error'):
            cells[key] = 'incomplete'
            continue
        abi = receipt['abi']
        if (receipt.get('policy') != digest(POLICY) or receipt.get('baseline') != BINIT_HASHES[abi]
                or receipt.get('zinit') != ZINIT_HASH or not receipt.get('input_sha256')):
            cells[key] = 'unverified identity'
            continue
        if abi in candidates and candidates[abi] != receipt.get('candidate'):
            raise ValueError(f'candidate differs between Bash {abi} cells')
        candidates[abi] = receipt.get('candidate')
        outcome = grade_runs(receipt.get('runs', []), 20, .98)
        cells[key] = 'passed' if outcome['pass'] else 'failed'
    return {'startup_framework_cells': cells,
            'pending': ['seven-plugin startup', 'deferred', 'command', 'git-tab', 'history', 'typing', 'slow-provider'],
            'release_gate_pass': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--summarize', type=Path, nargs='+', help='summarize existing startup receipts')
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--bash', type=Path, default=Path('/usr/bin/bash'))
    parser.add_argument('--zsh', type=Path, default=Path('/usr/bin/zsh'))
    parser.add_argument('--baseline-dir', type=Path, default=BASELINE)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--pairs', type=int, default=20)
    parser.add_argument('--runs', type=int, default=2)
    parser.add_argument('--count', type=int, choices=(0, 10, 50), default=10)
    args = parser.parse_args()
    if args.summarize:
        if args.candidate or args.work or args.output:
            parser.error('--summarize cannot be combined with measurement paths')
        print(json.dumps(summarize_receipts([json.loads(path.read_text()) for path in args.summarize]), indent=2))
        return
    if not args.candidate or not args.work or not args.output:
        parser.error('--candidate, --work and --output are required for measurement')
    args.work, args.output, args.candidate = args.work.resolve(), args.output.resolve(), args.candidate.resolve()
    args.bash = args.bash.resolve(strict=True)
    args.zsh = args.zsh.resolve(strict=True)
    args.baseline_dir = args.baseline_dir.resolve(strict=True)
    if not args.work.is_relative_to(ROOT / 'tmp') or not args.output.is_relative_to(ROOT / 'tmp'):
        parser.error('work and output must be under project tmp/')
    if args.work.exists() or args.output.exists() or args.pairs < 1 or args.runs < 1:
        parser.error('use fresh paths and positive counts')
    bash_version = subprocess.check_output([args.bash, '-c', 'printf "%s.%s" "$BASH_VERSINFO" "${BASH_VERSINFO[1]}"'], text=True)
    abi = {'5.2': '52', '5.3': '53'}.get(bash_version)
    if abi is None:
        parser.error('bash must be version 5.2 or 5.3')
    archive = args.baseline_dir / 'zinit.tar.gz'
    baseline = args.baseline_dir / f'binit-{abi}'
    if digest(archive) != ZINIT_HASH or digest(baseline) != BINIT_HASHES[abi]:
        raise RuntimeError('frozen baseline changed')
    inputs = [POLICY, Path(__file__), Path(corpus.__file__), Path(primed.__file__),
              ROOT / 'scripts/bench_interactive.py', ROOT / 'scripts/bench_corpus_terminal.py',
              corpus.FIXTURE, args.candidate, baseline, archive, args.bash, args.zsh, Path(sys.executable)]
    identities = {str(path.resolve()): digest(path) for path in inputs}
    args.work.mkdir(parents=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = {'schema': 1, 'policy': digest(POLICY), 'candidate': digest(args.candidate),
              'baseline': digest(baseline), 'zinit': digest(archive), 'count': args.count,
              'abi': abi, 'input_sha256': identities,
              'bash_version': bash_version,
              'zsh_version': subprocess.check_output([args.zsh, '--version'], text=True).strip(),
              'runs': [], 'complete': False, 'release_gate_pass': False,
              'pending': ['seven-plugin startup', 'deferred', 'command', 'git-tab', 'history', 'typing', 'slow-provider']}
    try:
        sources = args.work / 'sources'
        (sources / 'zinit').mkdir(parents=True)
        with tarfile.open(archive) as bundle:
            bundle.extractall(sources / 'zinit', filter='data')
        source_identity = corpus.inventory(sources / 'zinit')
        scenarios = {}
        for label, subject, binary in [('candidate', 'binit', args.candidate), ('baseline', 'binit', baseline),
                                       ('zinit', 'zinit', baseline), ('bash', 'bash', baseline), ('zsh', 'zsh', baseline)]:
            directory = args.work / label
            rc, _ = corpus.configuration(subject, args.count, directory, sources, binary, str(args.bash))
            primed.initialize_home(subject, rc, directory / 'home')
            shell = str(args.zsh if subject in ('zsh', 'zinit') else args.bash)
            primed.observe(subject, shell, rc, directory, directory / 'home', directory / 'prime-failure.raw')
            scenarios[label] = (subject, rc, directory, shell, primed.inventory_tree(directory / 'home'))
        scenario_identities = {label: (digest(rc), corpus.inventory(directory / 'plugins'),
                                       corpus.inventory(directory / 'state') if (directory / 'state').exists() else None)
                               for label, (_, rc, directory, _, _) in scenarios.items()}
        for run in range(args.runs):
            rows = []
            result['runs'].append(rows)
            for pair in range(args.pairs):
                order = list(scenarios)[::1 if (run + pair) % 2 == 0 else -1]
                for label in order:
                    subject, rc, directory, shell, home = scenarios[label]
                    current = (digest(rc), corpus.inventory(directory / 'plugins'),
                               corpus.inventory(directory / 'state') if (directory / 'state').exists() else None)
                    if (current != scenario_identities[label] or corpus.inventory(sources / 'zinit') != source_identity
                            or any(digest(path) != sha for path, sha in identities.items())):
                        raise RuntimeError('benchmark input changed')
                    observation = primed.verified_observe(subject, shell, rc, directory, directory / 'home', home,
                                                          directory / f'failure-{run}-{pair}.raw')
                    rows.append({'case': label, 'pair': pair, **observation})
                    primed.save(args.output, result)
            grade(rows, args.pairs)
        result['startup'] = grade_runs(result['runs'], args.pairs, .98)
        result['complete'] = args.pairs == 20 and args.runs == 2
    except BaseException as error:
        result['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        primed.save(args.output, result)


if __name__ == '__main__':
    main()
