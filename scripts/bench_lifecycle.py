#!/usr/bin/env python3
"""Measure local preparation and calling-shell plugin lifecycle costs."""
import argparse
import hashlib
import json
import math
import os
import platform
from pathlib import Path
import re
import shlex
import signal
import statistics
import subprocess
import tempfile
import time

from bench_interactive import Shell as TerminalShell

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / 'benchmarks/lifecycle.json'
FRAME = re.compile(rb'\x1eBINIT-STATUS:([0-9]+)\x1f')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def persist(path, result, exclusive=False):
    with tempfile.TemporaryDirectory(prefix='.scratch-lifecycle-receipt-', dir=path.parent) as temporary:
        pending = Path(temporary) / 'receipt.json'
        with pending.open('w') as stream:
            json.dump(result, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        # Synchronization failures must precede publication of a complete result.
        if exclusive:
            os.link(pending, path)
        else:
            os.replace(pending, path)


class Shell:
    def __init__(self, bash, environment, directory):
        rc = directory / 'control.bash'
        rc.write_text("PS1='BENCH> '\nPS2=''\nset -o vi\nHISTFILE=/dev/null\n")
        self.terminal = TerminalShell(str(bash), rc, directory, environment)

    def action(self, code, expected=0):
        start = len(self.terminal.raw)
        elapsed = self.terminal.command('{ ' + code + '; }; __bench_status=$?; '
            'printf "\\036BINIT-STATUS:%s\\037" "$__bench_status"')
        statuses = FRAME.findall(self.terminal.raw[start:])
        if len(statuses) != 1 or (expected is not None and statuses != [str(expected).encode()]):
            raise RuntimeError(f'phase behavior failed: statuses={statuses!r}, expected={expected}')
        self.last_status = int(statuses[0])
        return elapsed

    def close(self):
        self.terminal.close()


def fixture(case, directory):
    directory.mkdir()
    config = directory / 'config.toml'
    entries, names, files = [], [], {}
    for index in range(case['plugins']):
        plugin = directory / f'plugin-{index}'
        plugin.mkdir()
        manifest = f'schema=1\nid="fixture-{index}"\nentry="plugin.bash"\nload="{case["load"]}"\n'
        if case['load'] == 'command':
            manifest += 'commands=["bench_command"]\n'
        body = 'BINIT_BENCH_LOADS=$(( ${BINIT_BENCH_LOADS:-0} + 1 ))\n'
        for number in range(case['functions_per_plugin'] - (case['load'] == 'command')):
            name = f'bench_p{index}_f{number}'
            names.append(name)
            body += f'{name}() {{ :; }}\n'
        if case['load'] == 'command':
            names.append('bench_command')
            body += ('export BINIT_BENCH_EXPORT=loaded\ncd -- "$BINIT_BENCH_TARGET"\n'
                     'bench_command() { BINIT_BENCH_ARGC=$#; '
                     'BINIT_BENCH_CALLS=$(( ${BINIT_BENCH_CALLS:-0} + 1 )); '
                     'printf -v BINIT_BENCH_ARGS "<%s>" "$@"; return 37; }\n')
        for name, content in [('plugin.toml', manifest), ('plugin.bash', body)]:
            path = plugin / name
            path.write_text(content)
            files[f'{plugin.name}/{name}'] = digest(path)
        entries.append(plugin / 'plugin.bash')
    config.write_text('schema=1\n' + ('' if entries else 'plugins=[]\n') + ''.join(
        f'\n[[plugins]]\npath="plugin-{index}"\n' for index in range(case['plugins'])))
    files['config.toml'] = digest(config)
    return config, entries, names, files


def prepare(binary, config, state, environment, timeout=30):
    started = time.perf_counter_ns()
    process = subprocess.Popen([str(binary), 'prepare', str(config), '--state', str(state)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, start_new_session=True)
    try:
        _, stderr = process.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            process.stdout.close()
            process.stderr.close()
            process.wait(timeout=1)
        raise
    elapsed = (time.perf_counter_ns() - started) / 1e6
    if process.returncode:
        raise RuntimeError(f'generation preparation failed: {stderr[:200]!r}')
    return elapsed


def record(result, case, variant, run, sample, metric, elapsed):
    if not math.isfinite(elapsed) or elapsed <= 0:
        raise ValueError('invalid phase duration')
    result['records'].append(dict(case=case['id'], variant=variant, run=run,
                                 sample=sample, metric=metric, milliseconds=elapsed))


def lifecycle(case, variant, binary, state, entries, names, environment, work, repetitions):
    shell = Shell(Path(environment['BINIT_BENCH_BASH']), environment, work)
    timings = {}
    try:
        shell.action('bench_unrelated() { : sentinel; }; __bench_unrelated=$(declare -f bench_unrelated)')
        source = '; '.join('source ' + shlex.quote(str(path)) for path in entries) or ':'
        if binary:
            activate = f'__bench_init=$( {shlex.quote(str(binary))} init --state {shlex.quote(str(state))} ) && eval "$__bench_init"'
        else:
            activate = source if case['load'] == 'startup' else ':'
        timings['activate'] = shell.action(activate)
        if binary:
            shell.action('[[ ${__binit_active:-0} == 1 ]] && declare -F binit >/dev/null && binit profile >/dev/null')
        shell.action('builtin binit_complete status >/dev/null 2>&1', expected=None)
        native = shell.last_status == 0
        expected_loads = case['plugins'] if case['load'] == 'startup' else 0
        shell.action(f'[[ ${{BINIT_BENCH_LOADS:-0}} == {expected_loads} ]]')
        if case['load'] == 'command':
            command = 'bench_command "one two" "" "λ"'
            timings['first_command'] = shell.action(('' if binary else source + '; ') + command, expected=37)
            shell.action('[[ $BINIT_BENCH_LOADS == 1 && $BINIT_BENCH_EXPORT == loaded && '
                         '$PWD == "$BINIT_BENCH_TARGET" && $BINIT_BENCH_ARGC == 3 && '
                         '$BINIT_BENCH_ARGS == "<one two><><λ>" ]]')
            shell.action(shlex.quote(environment['BINIT_BENCH_BASH']) +
                         " --noprofile --norc -c '[[ ${BINIT_BENCH_EXPORT-} == loaded ]]'")
            timings['warm_commands'] = shell.action(
                f'for ((__bench_i=0; __bench_i<{repetitions}; __bench_i++)); do '
                f'{command}; [[ $? == 37 ]] || exit 94; done')
            shell.action(f'[[ $BINIT_BENCH_LOADS == 1 && $BINIT_BENCH_CALLS == {repetitions + 1} ]]')
        if names:
            shell.action('for __bench_name in ' + ' '.join(names) + '; do '
                         'declare -F -- "$__bench_name" >/dev/null || exit 93; done')
        if case['plugins']:
            if binary:
                unload = '; '.join(f'binit unload fixture-{index} || exit 91' for index in reversed(range(case['plugins'])))
            else:
                unload = 'unset -f -- ' + ' '.join(names)
            timings['unload'] = shell.action(unload)
            shell.action('for __bench_name in ' + ' '.join(names) + '; do '
                         'if declare -F -- "$__bench_name" >/dev/null; then exit 92; fi; done')
            shell.action('[[ $(declare -f bench_unrelated) == "$__bench_unrelated" ]]')
        return timings, native
    finally:
        shell.close()


def summarize(result):
    grouped = {}
    for row in result['records']:
        key = (row['run'], row['case'], row['variant'], row['metric'])
        grouped.setdefault(key, []).append(row['milliseconds'])
    summary = []
    for (run, case, variant, metric), values in sorted(grouped.items()):
        summary.append(dict(run=run, case=case, variant=variant, metric=metric, count=len(values),
            median=statistics.median(values), p95=sorted(values)[math.ceil(len(values) * .95) - 1]))
    return summary


def paired_comparison(result):
    if not result['compare_binary_sha256']:
        return []
    groups = {}
    for row in result['records']:
        if row['variant'] == 'direct_bash':
            continue
        key = row['run'], row['case'], row['metric']
        groups.setdefault(key, {}).setdefault(row['sample'], {})[row['variant']] = row['milliseconds']
    comparisons = []
    for (run, case, metric), pairs in sorted(groups.items()):
        if len(pairs) != result['samples_per_run'] or any(set(p) != {'binit', 'binit_before'} for p in pairs.values()):
            raise ValueError('incomplete comparison pairs')
        ratios = [p['binit'] / p['binit_before'] for p in pairs.values()]
        comparisons.append(dict(run=run, case=case, metric=metric,
                                median_paired_ratio=statistics.median(ratios)))
    return comparisons


def verify_inputs(args, work, result):
    identities = [(args.binary, 'binary_sha256'), (args.bash, 'bash_sha256'),
                  (PLAN, 'plan_sha256'), (Path(__file__), 'runner_sha256'),
                  (ROOT / 'scripts/bench_interactive.py', 'terminal_driver_sha256')]
    if args.compare_binary:
        identities.append((args.compare_binary, 'compare_binary_sha256'))
    for path, key in identities:
        if digest(path) != result[key]:
            raise ValueError(f'benchmark input changed: {key}')
    for case, files in result['fixtures'].items():
        for name, expected in files.items():
            if digest(work / case / name) != expected:
                raise ValueError('benchmark fixture changed')


def measure(args, work, result):
    home = work / 'home'
    home.mkdir()
    environment = {k: v for k, v in os.environ.items() if k not in ('BASH_ENV', 'ENV', 'SHELLOPTS', 'BASHOPTS')
                   and not k.startswith(('BINIT_', 'BASH_FUNC_', '__binit_'))}
    environment.update(HOME=str(home), XDG_CONFIG_HOME=str(home / 'config'),
        XDG_DATA_HOME=str(home / 'data'), XDG_CACHE_HOME=str(home / 'cache'),
        LC_ALL='C.UTF-8', TERM='xterm-256color', INPUTRC='/dev/null', BINIT_COMPLETION_MENU='1',
        BINIT_BENCH_BASH=str(args.bash), BINIT_BENCH_TARGET=str(home))
    variants = [('direct_bash', None), ('binit', args.binary)]
    if args.compare_binary:
        variants.append(('binit_before', args.compare_binary))
    fixtures = {}
    for case in result['cases']:
        fixtures[case['id']] = fixture(case, work / case['id'])
        result['fixtures'][case['id']] = fixtures[case['id']][3]
    for run in range(args.runs):
        for sample in range(args.samples):
            for case in result['cases'][::1 if (run + sample) % 2 == 0 else -1]:
                config, entries, names, _ = fixtures[case['id']]
                for variant, binary in variants[::1 if (run + sample) % 2 == 0 else -1]:
                    state = work / f'state-{run}-{sample}-{case["id"]}-{variant}'
                    preparation = prepare(binary, config, state, environment) if binary else None
                    timings, native = lifecycle(case, variant, binary, state, entries, names,
                                                environment, work, result['command_repetitions'])
                    if result['native_module_available'].setdefault(variant, native) != native:
                        raise ValueError('native module availability changed during the run')
                    if preparation is not None:
                        timings['prepare'] = preparation
                    for metric, elapsed in timings.items():
                        record(result, case, variant, run, sample, metric, elapsed)
                persist(args.output, result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=Path(os.environ.get('BINIT_TEST_BINARY', ROOT / 'target/release/binit')))
    parser.add_argument('--compare-binary', type=Path)
    parser.add_argument('--bash', type=Path, default=Path(os.environ.get('BINIT_BASH', '/bin/bash')))
    parser.add_argument('--samples', type=int, default=20)
    parser.add_argument('--runs', type=int, default=2)
    parser.add_argument('--cases', nargs='+', help='subset of frozen lifecycle case IDs')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(PLAN.read_text())
    selected = set(args.cases) if args.cases else {case['id'] for case in plan['cases']}
    if args.samples < 1 or args.runs < 1 or not selected <= {case['id'] for case in plan['cases']}:
        parser.error('positive samples/runs and known case IDs are required')
    if args.output.exists() or args.output.is_symlink():
        parser.error('output must be new; retain earlier receipts')
    result = dict(schema=1, complete=False, release_qualified=False, units='milliseconds',
        method='Interactive vi-mode Bash PTY command round trips, including prompt dispatch. Activation starts in an already-running shell. Preparation includes process launch and verified generation publication. Warm filesystem caches.',
        samples_per_run=args.samples, runs=args.runs, plan_sha256=digest(PLAN),
        architecture=platform.machine(), python_version=platform.python_version(),
        runner_sha256=digest(Path(__file__)),
        terminal_driver_sha256=digest(ROOT / 'scripts/bench_interactive.py'),
        command_repetitions=plan['command_repetitions'],
        cases=[case for case in plan['cases'] if case['id'] in selected],
        fixtures={}, native_module_available={}, records=[])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        persist(args.output, result, exclusive=True)
    except FileExistsError:
        parser.error('output must be new; retain earlier receipts')
    try:
        args.binary = args.binary.resolve(strict=True)
        args.bash = args.bash.resolve(strict=True)
        if args.compare_binary:
            args.compare_binary = args.compare_binary.resolve(strict=True)
        result.update(bash_sha256=digest(args.bash), binary_sha256=digest(args.binary),
            bash_version=subprocess.check_output([str(args.bash), '--version'], text=True).splitlines()[0],
            compare_binary_sha256=digest(args.compare_binary) if args.compare_binary else None)
        with tempfile.TemporaryDirectory(prefix='.scratch-lifecycle-', dir=ROOT) as temporary:
            measure(args, Path(temporary), result)
            verify_inputs(args, Path(temporary), result)
        if args.compare_binary and result['native_module_available']['binit'] != result['native_module_available']['binit_before']:
            raise ValueError('compared binaries must have the same native module availability')
        result['summary'] = summarize(result)
        result['comparison'] = paired_comparison(result)
        result['complete'] = True
    finally:
        persist(args.output, result)
    print(json.dumps(result['summary'], indent=2))


if __name__ == '__main__':
    main()
