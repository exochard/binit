#!/usr/bin/env python3
"""Measure the frozen diagnostic observer against the same untraced checks."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import signal
import statistics
import subprocess
import time

from diagnostics_cases import materialize, validate

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def percentile95(values):
    return sorted(values)[math.ceil(len(values) * .95) - 1]


def assess(records, cases, runs, pairs, target):
    if len(records) != runs * pairs * len(cases) * 2 or any(
            not row.get('validated') or not math.isfinite(row['wall_ms']) or row['wall_ms'] <= 0
            or row['case'] not in cases or row['run'] not in range(runs) for row in records):
        raise ValueError('invalid or unvalidated observation set')
    summaries = []
    for run in range(runs):
        for case in cases:
            grouped = {}
            for row in records:
                if row['run'] == run and row['case'] == case:
                    group = grouped.setdefault(row['pair'], {})
                    if row['mode'] in group:
                        raise ValueError('duplicate paired observation')
                    group[row['mode']] = row['wall_ms']
            if set(grouped) != set(range(pairs)) or any(
                    set(group) != {'observed', 'unobserved'} for group in grouped.values()):
                raise ValueError('incomplete paired observations')
            ratios = [grouped[p]['observed'] / grouped[p]['unobserved'] for p in range(pairs)]
            tails = percentile95([g['observed'] for g in grouped.values()]) / percentile95(
                [g['unobserved'] for g in grouped.values()])
            summaries.append(dict(run=run, case=case, pairs=pairs,
                                  median_paired_ratio=statistics.median(ratios),
                                  p95_paired_ratio=percentile95(ratios), p95_elapsed_ratio=tails,
                                  passed=max(percentile95(ratios), tails) <= target))
    return summaries


def timed(command, environment, directory):
    """wait4 reports kernel resource use separately from parent wall time."""
    directory.mkdir()
    def expired(_signal, _frame):
        raise TimeoutError('benchmark command exceeded its 15 second watchdog')
    with (directory / 'stdout').open('wb') as stdout, (directory / 'stderr').open('wb') as stderr:
        started = time.perf_counter_ns()
        child = subprocess.Popen(command, env=environment, stdout=stdout, stderr=stderr)
        old_handler = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, 15)
        try:
            _, status, usage = os.wait4(child.pid, 0)
            child.returncode = os.waitstatus_to_exitcode(status)
            wall_ms = (time.perf_counter_ns() - started) / 1_000_000
        except BaseException:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)
    return dict(code=child.returncode, wall_ms=wall_ms, user_s=usage.ru_utime,
                system_s=usage.ru_stime, maxrss_kib=usage.ru_maxrss,
                minor_faults=usage.ru_minflt, major_faults=usage.ru_majflt,
                voluntary_switches=usage.ru_nvcsw, involuntary_switches=usage.ru_nivcsw)


def check_receipt(receipt, case):
    if not receipt['complete'] or receipt['error'] is not None or len(receipt['samples']) != 1:
        raise ValueError('check receipt incomplete')
    sample = receipt['samples'][0]
    if (sample['code'] != 0 or sample['check_return'] != 0 or sample['phase'] != 'complete'
            or sample['signal'] is not None or sample['timed_out'] or sample['interrupted']
            or not sample['output_complete'] or not sample['generation_unchanged']
            or not case['minimum_window_ms'] <= sample['elapsed_ms'] <= 2500):
        raise ValueError('frozen effect check or timing policy failed')


def run(args):
    corpus_path = ROOT / 'benchmarks/diagnostics.json'
    corpus = json.loads(corpus_path.read_text())
    cases = [case for case in corpus['cases'] if case['primary']]
    binary, bash = args.binary.resolve(), args.bash.resolve()
    work, output = args.work.resolve(), args.output.resolve()
    if work.exists() or output.exists():
        raise ValueError('work and output paths must be new')
    work.mkdir(parents=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    identities = dict(binary_sha256=digest(binary), bash_sha256=digest(bash),
                      corpus_sha256=digest(corpus_path), runner_sha256=digest(__file__),
                      validator_sha256=digest(ROOT / 'scripts/diagnostics_cases.py'))
    result = dict(schema=1, complete=False, qualification_pass=False, identities=identities,
                  runs=args.runs, pairs=args.pairs, seed=args.seed, records=[], summaries=[],
                  method='Whole-command paired elapsed time; same manager, Bash, generation and fixtures. '
                         'Balanced randomized order. Nearest-rank p95 of paired ratios and ratio of p95 elapsed times must both pass.',
                  resources='Linux wait4 rusage for each command and reaped descendants. maxrss is the kernel maximum, not aggregate simultaneous process-tree memory. Peak process and descriptor counts are not measured by this runner.')
    environment = dict(os.environ, BINIT_BASH=str(bash), INPUTRC='/dev/null', HISTFILE='/dev/null')
    for name in ('BASH_ENV', 'ENV'):
        environment.pop(name, None)
    def save():
        output.write_text(json.dumps(result, indent=2) + '\n')
    save()
    try:
        rng = random.Random(args.seed)
        for run_index in range(args.runs):
            for case in cases:
                fixture = work / f'run-{run_index}' / case['id']
                config, plan, state = materialize(fixture, case)
                environment['HOME'] = str(fixture)
                for command in [('prepare', str(config))]:
                    subprocess.run([str(binary), *command, '--state', str(state)], env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=15)
                generation = (state / 'current').read_text().strip()
                orders = [i % 2 for i in range(args.pairs)]
                rng.shuffle(orders)
                frozen_inputs = {p: digest(p) for p in fixture.rglob('*')
                                 if p.is_file() and state not in p.parents}
                binding_identity = None
                for pair, first in enumerate(orders):
                    modes = ('observed', 'unobserved') if first else ('unobserved', 'observed')
                    for position, mode in enumerate(modes):
                        command = ['diagnose', str(plan), '--json']
                        if mode == 'unobserved':
                            command.append('--no-trace')
                        directory = fixture / f'{pair:02d}-{mode}'
                        row = timed([str(binary), *command, '--state', str(state)], environment, directory)
                        row.update(run=run_index, case=case['id'], pair=pair, position=position,
                                   mode=mode, generation=generation, validated=False)
                        result['records'].append(row)
                        save()
                        if row['code'] != 0:
                            raise ValueError(f'{mode} command failed; retained output in work directory')
                        payload = json.loads((directory / 'stdout').read_text())
                        receipt = payload['receipt']
                        if receipt['context']['diagnostic'] != (mode == 'observed'):
                            raise ValueError('wrong observation mode')
                        if mode == 'observed':
                            report = validate(case, payload, row['code'], corpus['target'])
                            phase = next(p for p in report['trace']['phases']
                                         if p['process'] == 0 and p['name'] == case['expected_phase'])
                            row['attribution_fraction'] = phase['attributed_us'] / phase['wall_us']
                            row['trace_records'] = report['trace']['records']
                        elif payload['diagnostics']:
                            raise ValueError('unobserved run contains diagnostic sidecars')
                        check_receipt(receipt, case)
                        binding = hashlib.sha256(json.dumps(receipt['context']['binding'], sort_keys=True).encode()).hexdigest()
                        if binding_identity is not None and binding != binding_identity:
                            raise ValueError('fixture bindings changed between observations')
                        binding_identity = binding
                        if any(digest(path) != value for path, value in frozen_inputs.items()):
                            raise ValueError('frozen fixture inputs changed')
                        if receipt['subject']['generation'] != generation or (state / 'current').read_text().strip() != generation:
                            raise ValueError('selected generation changed')
                        row['receipt_sha256'] = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
                        row['binding_sha256'] = binding
                        row['validated'] = True
                        save()
        if (digest(binary) != identities['binary_sha256'] or digest(bash) != identities['bash_sha256']
                or digest(corpus_path) != identities['corpus_sha256']
                or digest(__file__) != identities['runner_sha256']
                or digest(ROOT / 'scripts/diagnostics_cases.py') != identities['validator_sha256']):
            raise ValueError('measurement identities changed')
        result['summaries'] = assess(result['records'], [c['id'] for c in cases], args.runs,
                                     args.pairs, corpus['target']['observer_maximum_p95_ratio'])
        result['complete'] = True
        result['qualification_pass'] = (args.runs == corpus['target']['runs']
                                        and args.pairs == corpus['target']['pairs_per_run']
                                        and all(s['passed'] for s in result['summaries']))
    except BaseException as error:
        result['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        save()
    print(json.dumps(dict(complete=result['complete'], qualification_pass=result['qualification_pass'],
                          summaries=result['summaries']), indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--bash', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--runs', type=int, default=2)
    parser.add_argument('--pairs', type=int, default=20)
    parser.add_argument('--seed', type=int, default=81273)
    options = parser.parse_args()
    if options.runs < 1 or options.pairs < 1:
        parser.error('runs and pairs must be positive')
    run(options)
