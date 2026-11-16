#!/usr/bin/env python3
"""Measure the pinned framework corpus after private state has been primed."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

import bench_ecosystem as corpus
from bench_corpus_terminal import CheckedShell
from bench_interactive import digest, summarize

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / 'docs/ecosystem-primed.md'
METRICS = ('startup_visible_ms', 'startup_usable_ms', 'effect_probe_ms', 'required_effects_ms')


def inventory_tree(root):
    """Include empty directories: their existence can change initialization."""
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError(f'missing or invalid private tree: {root}')
    result = {'.': {'directory_mode': root.stat().st_mode & 0o777}}
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in list(dirs):
            path = parent / name
            if path.is_symlink():
                dirs.remove(name)
                files.append(name)
            else:
                result[path.relative_to(root).as_posix()] = {'directory_mode': path.stat().st_mode & 0o777}
        for name in files:
            path = parent / name
            key = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[key] = {'link': os.readlink(path)}
            elif path.is_file():
                result[key] = {'sha256': digest(path), 'mode': path.stat().st_mode & 0o777}
            else:
                raise RuntimeError(f'non-regular primed input: {path}')
    return result


def identity(args, scenarios):
    common = corpus.input_identity(args, scenarios)
    common['files'][str(Path(__file__).resolve())] = digest(Path(__file__))
    common['files'][str((ROOT / 'scripts/bench_corpus_terminal.py').resolve())] = digest(ROOT / 'scripts/bench_corpus_terminal.py')
    common['files'][str(POLICY.resolve())] = digest(POLICY)
    common['primed_homes'] = {key: inventory_tree(directory / 'home')
                              for key, (_, _, directory) in scenarios.items()}
    common['fixture_directories'] = {key: inventory_tree(directory / 'plugins')
                                     for key, (_, _, directory) in scenarios.items()}
    return common


def initialize_home(subject, rc, home):
    home.mkdir()
    if subject in ('zsh', 'zinit'):
        shutil.copyfile(rc, home / '.zshrc')


def binit_readiness(directory):
    state = directory / 'state'
    selected = (state / 'current').read_text().strip()
    if not re.fullmatch(r'[0-9a-f]{64}', selected):
        raise RuntimeError('invalid selected Binit generation identity')
    generation = state / 'generations' / selected
    return ('[[ $(type -t binit) == function ]] && '
            f'[[ ${{BINIT_STATE_DIR-}} == {shlex.quote(str(state))} ]] && '
            f'[[ ${{__binit_generation_dir-}} == {shlex.quote(str(generation))} ]] && '
            'binit profile >/dev/null')


def loader_readiness(subject, work, rc):
    if subject in ('binit', 'binit-wait'):
        return binit_readiness(work)
    if subject in ('bash-it', 'oh-my-bash'):
        variable = 'BASH_IT' if subject == 'bash-it' else 'OSH'
        match = re.search(rf'^{variable}=(.*)$', rc.read_text(), re.M)
        if not match or len(shlex.split(match.group(1))) != 1:
            raise RuntimeError(f'missing {variable} loader configuration')
        expected = shlex.quote(shlex.split(match.group(1))[0])
        if subject == 'bash-it':
            return f'[[ ${{BASH_IT-}} == {expected} ]] && [[ $(type -t bash-it) == function ]]'
        return (f'[[ ${{OSH-}} == {expected} && -n ${{OMB_VERSION-}} ]] && '
                '[[ $(type -t omb) == function && $(type -t _omb_module_require) == function ]]')
    if subject == 'zinit':
        return ('typeset -f zinit >/dev/null && '
                '[[ ${ZINIT[HOME_DIR]-} == "$HOME/zinit" && -d ${ZINIT[HOME_DIR]} ]]')
    if subject in ('bash', 'zsh'):
        return ':'
    raise RuntimeError(f'unknown framework subject: {subject}')


def observe(subject, interpreter, rc, work, home, failure_path):
    """Run the same accepted-input and effect probe with an existing private home."""
    if not home.is_dir():
        raise RuntimeError('private home must exist before each launch')
    env = corpus.environment(home)
    env['BINIT_BASH'] = interpreter
    env['SHELL'] = interpreter
    runtime = work / 'runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    env['XDG_RUNTIME_DIR'] = str(runtime)
    command = [interpreter, '-d', '-i'] if subject in ('zsh', 'zinit') else None
    if command and digest(home / '.zshrc') != digest(rc):
        raise RuntimeError('primed Zsh startup configuration changed')
    started = time.perf_counter_ns()
    shell = CheckedShell(interpreter, rc, work, env, command, receipt_path=failure_path)
    close_ready = False
    try:
        startup = shell.startup_ms
        corpus.wait_for_editor(shell)
        ack_started = time.perf_counter_ns()
        shell.settled(b':')
        echo_quiet = (time.perf_counter_ns() - ack_started) / 1e6
        shell.command('')
        acknowledgment = (time.perf_counter_ns() - ack_started) / 1e6
        usable = (time.perf_counter_ns() - started) / 1e6
        offset = len(shell.raw)
        check = 'corpus_verify && ' + loader_readiness(subject, work, rc)
        effect_ms = shell.command(check + '; corpus_status=$?; printf "\\036CORPUS:%s\\036" "$corpus_status"')
        statuses = re.findall(rb'\x1eCORPUS:(\d+)\x1e', shell.raw[offset:])
        if statuses != [b'0']:
            raise RuntimeError(f'{subject}: effects failed: {statuses!r}')
        result = {'startup_visible_ms': startup, 'first_input_ack_ms': acknowledgment,
                'input_echo_quiet_ms': echo_quiet, 'startup_usable_ms': usable,
                'effect_probe_ms': effect_ms, 'required_effects_ms': (time.perf_counter_ns() - started) / 1e6,
                'resources_at_readiness': corpus.resource_sample(shell.pid),
                'effect_status': 0, 'terminal_bytes': len(shell.raw)}
        close_ready = True
        return result
    except BaseException as error:
        failure_path.write_bytes(shell.raw)
        raise RuntimeError(f'{subject}: {error}\n' + '\n'.join(shell.screen.display)) from error
    finally:
        shell.close(ready=close_ready)


def verified_observe(subject, interpreter, rc, directory, home, expected_home, failure_path):
    if inventory_tree(home) != expected_home:
        raise RuntimeError(f'{subject}: primed private state changed before launch')
    row = observe(subject, interpreter, rc, directory, home, failure_path)
    if row.get('effect_status') != 0:
        raise RuntimeError(f'{subject}: required effects did not pass')
    after = inventory_tree(home)
    row['private_state_unchanged'] = after == expected_home
    if not row['private_state_unchanged']:
        row['changed_private_state'] = after
    return row


def summarize_run(samples, keys, pairs):
    expected = {(key, pair) for key in keys for pair in range(pairs)}
    seen = set()
    for row in samples:
        key = (row['case'], row['pair'])
        if (key not in expected or key in seen or row.get('effect_status') != 0
                or not row.get('private_state_unchanged')):
            raise RuntimeError('unexpected, duplicate or failed primed observation')
        for metric in METRICS:
            value = row[metric]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise RuntimeError(f'invalid primed elapsed time: {metric}')
        seen.add(key)
    if seen != expected:
        raise RuntimeError('incomplete primed framework observations')
    result = {}
    for key in keys:
        matched = [row for row in samples if row['case'] == key]
        if not matched:
            raise RuntimeError(f'missing observations for {key}')
        result[key] = {metric: summarize([row[metric] for row in matched]) for metric in METRICS}
    return result


def save(output, result):
    temporary = output.with_name(output.name + '.writing')
    with temporary.open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    os.replace(temporary, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--bash', default='/bin/bash')
    parser.add_argument('--zsh', default='/bin/zsh')
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pairs', type=int, default=20)
    parser.add_argument('--runs', type=int, default=2)
    parser.add_argument('--subjects', nargs='+')
    parser.add_argument('--counts', nargs='+', type=int)
    args = parser.parse_args()
    args.work = args.work.resolve()
    args.output = args.output.resolve()
    args.binary = args.binary.resolve()
    args.sources = args.sources.resolve()
    if not args.work.is_relative_to(ROOT / 'tmp') or not args.output.is_relative_to(ROOT / 'tmp'):
        parser.error('work and output must be under project tmp/')
    if args.output.exists() or args.work.exists() or args.runs < 1 or args.pairs < 1:
        parser.error('use fresh paths and positive run and pair counts')
    spec = json.loads(corpus.SPEC.read_text())
    subjects = args.subjects or spec['framework']['subjects']
    counts = args.counts or spec['framework']['plugin_counts']
    if (len(set(subjects)) != len(subjects) or len(set(counts)) != len(counts)
            or any(s not in spec['framework']['subjects'] for s in subjects)
            or any(n not in spec['framework']['plugin_counts'] for n in counts)):
        parser.error('use distinct subjects and counts from the frozen framework corpus')
    args.work.mkdir(parents=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as stream:
        stream.write('{}\n')
    result = {'schema': 1, 'phase': 'primed-private-framework', 'runs': [], 'priming': {},
              'complete_two_run_primed_framework': False,
              'scope': {'private_state': 'one primed private home per configuration; reused unchanged',
                        'comparisons': 'Bash and Zsh use separate same-interpreter controls',
                        'unsupported': ['editor equivalence', 'documented defaults', 'overall resource ranking'],
                        'resource_observations': spec['observation']['resource_scope']}}
    scenarios = {}
    try:
        result['sources'] = corpus.verify_sources(args.sources, spec)
        for count in counts:
            for subject in subjects:
                key = f'{subject}-{count}'
                result['active_case'] = ['prepare', key]
                directory = args.work / key
                rc, preparation = corpus.configuration(subject, count, directory, args.sources / 'sources',
                                                       args.binary, args.bash)
                initialize_home(subject, rc, directory / 'home')
                scenarios[key] = (subject, rc, directory)
                result.setdefault('configurations', {})[key] = {
                    'plugin_count': count, 'subject': subject, 'preparation_ms': preparation,
                    'rc_sha256': digest(rc), 'shell': 'zsh' if subject in ('zsh', 'zinit') else 'bash'}
                if subject in ('binit', 'binit-wait'):
                    result['configurations'][key]['selected_generation'] = (directory / 'state/current').read_text().strip()
                interpreter = args.zsh if subject in ('zsh', 'zinit') else args.bash
                result['active_case'] = ['prime', key]
                result['priming'][key] = observe(subject, interpreter, rc, directory, directory / 'home',
                                                 directory / 'prime-failure.raw')
                if result['priming'][key].get('effect_status') != 0:
                    raise RuntimeError(f'{key}: priming effects failed')
                result['priming'][key]['private_state'] = inventory_tree(directory / 'home')
                save(args.output, result)
        result['inputs'] = identity(args, scenarios)
        result['interpreter_versions'] = {
            name: subprocess.run([path, '--version'], capture_output=True, text=True, check=True).stdout.splitlines()[0]
            for name, path in [('bash', args.bash), ('zsh', args.zsh)]}
        for run in range(args.runs):
            samples = []
            result['runs'].append(samples)
            for pair in range(args.pairs):
                if identity(args, scenarios) != result['inputs']:
                    raise RuntimeError('primed framework inputs changed before pair')
                keys = list(scenarios)[::1 if (run + pair) % 2 == 0 else -1]
                for key in keys:
                    result['active_case'] = ['measure', run, pair, key]
                    subject, rc, directory = scenarios[key]
                    interpreter = args.zsh if subject in ('zsh', 'zinit') else args.bash
                    observation = verified_observe(subject, interpreter, rc, directory, directory / 'home',
                        result['inputs']['primed_homes'][key], directory / f'failure-{run}-{pair}.raw')
                    samples.append({'case': key, 'pair': pair, **observation})
                    save(args.output, result)
                    if not observation['private_state_unchanged']:
                        raise RuntimeError(f'{subject}: primed private state changed during launch')
                print(f'run {run + 1} pair {pair + 1}/{args.pairs}: primed effects passed', flush=True)
            if identity(args, scenarios) != result['inputs']:
                raise RuntimeError('primed framework inputs changed during run')
            sources_after_run = corpus.verify_sources(args.sources, spec)
            if sources_after_run != result['sources']:
                raise RuntimeError('pinned upstream inputs changed during run')
        result['sources_after'] = corpus.verify_sources(args.sources, spec)
        if result['sources_after'] != result['sources']:
            raise RuntimeError('pinned upstream inputs changed after measurement')
        result['summary_by_run'] = [summarize_run(run, scenarios, args.pairs) for run in result['runs']]
        result['complete_two_run_primed_framework'] = (
            args.runs == spec['framework']['runs'] and args.pairs == spec['framework']['pairs_per_run']
            and subjects == spec['framework']['subjects'] and counts == spec['framework']['plugin_counts']
            and all(len(run) == len(scenarios) * args.pairs for run in result['runs']))
        result.pop('active_case', None)
    except BaseException as error:
        result['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        save(args.output, result)


if __name__ == '__main__':
    main()
