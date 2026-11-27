#!/usr/bin/env python3
"""Compare history opening and filtering with matched selection and cancel effects."""
import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

import bench_ecosystem as corpus
from bench_development_milestone import BASELINE, BINIT_HASHES, POLICY, ROOT, ZINIT_HASH
from bench_interactive import PROMPT, Shell, digest
from bench_nvm_milestone import p95

NEEDLES = {'old': 'milestone-historic-needle', 'recent': 'milestone-recent-needle'}
DRAFT = 'echo draft'


def command(needle):
    return f'printf {needle} > executed-witness'


def prepare(directory, binary, source, args, history):
    home = directory / 'home'
    home.mkdir(parents=True)
    env = corpus.environment(home)
    path = shlex.quote(str(history))
    if binary is None:
        env['SHELL'] = str(args.zsh)
        rc = home / '.zshrc'
        rc.write_text(
            'typeset -gA ZINIT\nZINIT[HOME_DIR]="$HOME/zinit"\n'
            f"source {shlex.quote(str(source / 'zinit.zsh'))}\n"
            f"PROMPT='BENCH> '\nbindkey -{'v' if args.editor == 'vi' else 'e'}\n"
            "bindkey '^R' history-incremental-search-backward\nbindkey -M isearch '^G' send-break\n"
            "bindkey '^A' beginning-of-line\nbindkey -M isearch '^A' beginning-of-line\n"
            f'HISTSIZE=60000\nSAVEHIST=60000\nHISTFILE=/dev/null\nfc -R {path}\n')
        return rc, env, [str(args.zsh), '-d', '-i']
    env.update(BINIT_BASH=str(args.bash), SHELL=str(args.bash))
    state, config = directory / 'state', directory / 'config.toml'
    config.write_text('schema=1\nplugins=[]\n')
    subprocess.run([binary, 'prepare', config, '--state', state], env=env, check=True,
                   stdout=subprocess.DEVNULL, timeout=30)
    init = shlex.join([str(binary), 'init', '--state', str(state)])
    rc = directory / 'rc'
    rc.write_text(
        f"PS1='BENCH> '\nset -o {args.editor}\nHISTSIZE=60000\nHISTFILE=/dev/null\n"
        f'history -c\nhistory -r {path}\neval "$({init})"\nbuiltin binit_complete editor history on\n')
    return rc, env, None


def observe(bash, config, work, trace, age):
    rc, env, launch = config
    shell = Shell(str(bash), rc, work, env, launch)
    zsh = launch is not None
    menu = 'bck-i-search: ' if zsh else 'history  '
    needle = NEEDLES[age]
    old = age == 'old'

    def line_is(expected):
        row = shell.screen.display[shell.screen.cursor.y].rstrip()
        return row == PROMPT.decode() + expected

    def measure(keys, visible):
        started = time.perf_counter_ns()
        os.write(shell.fd, keys)
        shell.until(visible)
        return (time.perf_counter_ns() - started) / 1e6

    try:
        corpus.wait_for_editor(shell)
        if not old:
            os.write(shell.fd, DRAFT[:1].encode())
            shell.until(lambda: line_is(DRAFT[:1]))
            os.write(shell.fd, DRAFT[1:].encode())
            shell.until(lambda: line_is(DRAFT))
        opened = measure(b'\x12', lambda: any(menu in row for row in shell.screen.display))
        filtered = 0.0
        for index, key in enumerate(needle):
            prefix = needle[:index + 1]
            filtered += measure(key.encode(), lambda: any(menu + prefix in row for row in shell.screen.display)
                                and (prefix != needle or any(command(needle) in row for row in shell.screen.display)))
        key = (b'\x01' if zsh else b'\r') if old else b'\x07'
        os.write(shell.fd, key)
        shell.until(lambda: line_is(command(needle) if old else DRAFT)
                    and not any(menu in row for row in shell.screen.display))
        if (work / 'executed-witness').exists():
            raise RuntimeError('history selection executed a command')
        return {f'open_{age}': opened, f'filter_{age}': filtered}
    except Exception:
        trace.write_bytes(shell.raw)
        raise
    finally:
        # Insertion leaves a valid command at the prompt. Never type into it during teardown.
        try:
            os.kill(shell.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(shell.pid, 0)
        os.close(shell.fd)


def stop_run(signum, _frame):
    raise SystemExit(128 + signum)


def main():
    signal.signal(signal.SIGTERM, stop_run)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--baseline-dir', default=BASELINE, type=Path)
    parser.add_argument('--bash', default=Path('/usr/bin/bash'), type=Path)
    parser.add_argument('--zsh', default=Path('/usr/bin/zsh'), type=Path)
    parser.add_argument('--editor', choices=['emacs', 'vi'], required=True)
    parser.add_argument('--depth', choices=[64, 2048, 50000], type=int, required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--pairs', default=20, type=int)
    parser.add_argument('--runs', default=2, type=int)
    args = parser.parse_args()
    for field in ('candidate', 'baseline_dir', 'bash', 'zsh', 'output'):
        setattr(args, field, getattr(args, field).resolve())
    if not args.output.is_relative_to(ROOT / 'tmp') or args.output.exists() or min(args.pairs, args.runs) < 1:
        parser.error('use a fresh output under tmp/ and positive counts')
    abi = subprocess.check_output([args.bash, '-c', 'printf "%s%s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'],
                                  text=True)
    baseline, archive = args.baseline_dir / f'binit-{abi}', args.baseline_dir / 'zinit.tar.gz'
    if abi not in BINIT_HASHES or digest(baseline) != BINIT_HASHES[abi] or digest(archive) != ZINIT_HASH:
        parser.error('a frozen baseline artifact does not match')
    inputs = [Path(__file__), POLICY, baseline, args.candidate, archive, args.bash, args.zsh,
              Path(sys.executable), *(ROOT / 'scripts' / file for file in (
                  'bench_interactive.py', 'bench_ecosystem.py', 'bench_nvm_milestone.py'))]
    identities = {str(path): digest(path) for path in inputs}
    result = {'schema': 1, 'abi': abi, 'editor': args.editor, 'depth': args.depth,
              'filter_method': 'sum of key-to-visible latencies for the complete query',
              'input_sha256': identities, 'runs': [], 'history_gate_pass': False, 'release_gate_pass': False}
    args.output.mkdir()
    receipt = args.output / 'result.json'
    try:
        with tempfile.TemporaryDirectory(dir=args.output) as temp:
            work = Path(temp)
            source = work / 'zinit'
            source.mkdir()
            with tarfile.open(archive) as bundle:
                bundle.extractall(source, filter='data')
            history = work / 'history.txt'
            history.write_text(command(NEEDLES['old']) + '\n' +
                               ''.join(f'printf fixture-{i:05d}\n' for i in range(1, args.depth - 1)) +
                               command(NEEDLES['recent']) + '\n')
            result['history_sha256'] = digest(history)
            binaries = {'baseline': baseline, 'candidate': args.candidate, 'zinit': None, 'control': baseline}
            configs = {name: prepare(work / name, binary, source, args, history)
                       for name, binary in binaries.items()}
            for run in range(args.runs):
                pairs = []
                result['runs'].append(pairs)
                for index in range(args.pairs):
                    order = list(configs)
                    rotation = (run + index) % len(order)
                    pair = {}
                    for name in order[rotation:] + order[:rotation]:
                        pair[name] = {}
                        for age in NEEDLES:
                            label = f'{run}-{index}-{name}-{age}'
                            with tempfile.TemporaryDirectory(dir=work) as trial:
                                pair[name].update(observe(args.bash, configs[name], Path(trial),
                                                          args.output / f'{label}.trace', age))
                    pairs.append(pair)
                    receipt.write_text(json.dumps(result, indent=2) + '\n')
            if digest(history) != result['history_sha256'] or any(
                    digest(path) != value for path, value in identities.items()):
                raise RuntimeError('benchmark inputs changed')
            metrics = result['runs'][0][0]['candidate']
            result['p95_ms'] = [{name: {key: p95([pair[name][key] for pair in run]) for key in metrics}
                                 for name in configs} for run in result['runs']]
            result['ratios'] = [{name: {key: row['candidate'][key] / row[name][key] for key in metrics}
                                 for name in ('baseline', 'zinit')} for row in result['p95_ms']]
            limit = json.loads(POLICY.read_text())['timing']['maximum_win_ratio']
            result['history_gate_pass'] = (args.runs == 2 and args.pairs == 20 and all(
                ratio <= limit for run in result['ratios'] for arm in run.values() for ratio in arm.values()))
    except BaseException as error:
        result['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        receipt.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
