#!/usr/bin/env python3
"""Measure the first and warm NVM command in private Bash and Zsh sessions."""
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
import tarfile
import tempfile
import time
import time

import bench_ecosystem as corpus
from bench_development_milestone import BASELINE, BINIT_HASHES, POLICY, ROOT, ZINIT_HASH
from bench_interactive import PROMPT, Shell, digest

NVM_HASH = '390260ab9eb1da20e8bc0ebea2ee90f528d53e5e9f6e13b16717db4af454df9d'
METRICS = ('first', 'warm')
NVM_COMMAND = """nvm --version; rc=$?; if type nvm_echo >/dev/null 2>&1; then
printf '\\036NVM:%s:loaded\\037' "$rc"; fi""".replace('\n', ' ')


def prepare(directory, binary, nvm, zinit, editor, bash, zsh):
    home = directory / 'home'
    home.mkdir(parents=True)
    (home / 'nvm').mkdir()
    env = corpus.environment(home) | {'NVM_DIR': str(home / 'nvm')}
    if binary is None:
        env['SHELL'] = str(zsh)
        package = home / 'zinit/plugins/_local---nvm'
        package.mkdir(parents=True)
        rc = home / '.zshrc'
        rc.write_text(
            f"bindkey -{'v' if editor == 'vi' else 'e'}\n"
            "HISTFILE=/dev/null\ntypeset -gA ZINIT\nZINIT[HOME_DIR]=\"$HOME/zinit\"\n"
            f"source {shlex.quote(str(zinit / 'zinit.zsh'))}\n"
            "zinit ice trigger-load'!nvm' pick'nvm.sh'\nzinit light _local/nvm\nPROMPT='BENCH> '\n")
        command = [str(zsh), '-d', '-i']
    else:
        package = directory / 'plugin'
        package.mkdir()
        rc = directory / 'rc'
        command = None
    entry = package / 'nvm.sh'
    shutil.copyfile(nvm, entry)
    if binary is not None:
        state = directory / 'state'
        config = directory / 'config.toml'
        config.write_text('schema=1\n[[plugins]]\npath="nvm.toml"\n')
        (directory / 'nvm.toml').write_text(
            'schema=1\nid="nvm"\npath="plugin"\nfiles=["nvm.sh"]\nload="command"\ncommands=["nvm"]\n')
        env.update(BINIT_BASH=str(bash), SHELL=str(bash))
        subprocess.run([binary, 'prepare', config, '--state', state], env=env, check=True,
                       stdout=subprocess.DEVNULL, timeout=30)
        init = shlex.join([str(binary), 'init', '--state', str(state)])
        rc.write_text(f"set -o {editor}\nHISTFILE=/dev/null\nPS1='BENCH> '\neval \"$({init})\"\n")
    return directory, rc, env, command, entry


def accepted(shell, code):
    marker = b'\x1eDONE\x1e'
    start = len(shell.raw)
    started = time.perf_counter_ns()
    # A newline after the marker makes ZLE print a fresh prompt instead of recycling its row.
    os.write(shell.fd, code.encode() + b"; printf '\\036DONE\\036\\n'\n")
    shell.until(lambda: marker in shell.raw[start:] and
                shell.raw.rfind(PROMPT) > shell.raw.index(marker, start))
    return (time.perf_counter_ns() - started) / 1e6


def observe(bash, arm, failure):
    directory, rc, env, command, entry = arm
    shell = Shell(str(bash), rc, directory, env, command)
    try:
        corpus.wait_for_editor(shell)
        accepted(shell, "if type nvm >/dev/null 2>&1 && ! type nvm_echo >/dev/null 2>&1; "
                 "then printf '\\036NVM:PENDING\\037'; fi")
        if b'\x1eNVM:PENDING\x1f' not in shell.raw:
            raise RuntimeError('NVM was not pending before first use')
        times = {}
        for metric in METRICS:
            start = len(shell.raw)
            times[metric] = accepted(shell, NVM_COMMAND)
            output = shell.raw[start:]
            if not re.search(rb'[\r\n]0\.40\.3\r*\n', output) or b'\x1eNVM:0:loaded\x1f' not in output:
                raise RuntimeError(f'{metric} NVM invocation failed the version or status check')
        if digest(entry) != NVM_HASH:
            raise RuntimeError('NVM source changed during launch')
        if any(Path(env['NVM_DIR']).iterdir()):
            raise RuntimeError('NVM wrote to its private installation directory')
        return times
    except BaseException:
        failure.write_bytes(shell.raw)
        raise
    finally:
        shell.close()


def p95(timings):
    if not all(math.isfinite(value) and value > 0 for value in timings):
        raise ValueError('invalid timing')
    return sorted(timings)[math.ceil(.95 * len(timings)) - 1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--nvm', required=True, type=Path)
    parser.add_argument('--baseline-dir', default=BASELINE, type=Path)
    parser.add_argument('--bash', default=Path('/usr/bin/bash'), type=Path)
    parser.add_argument('--zsh', default=Path('/usr/bin/zsh'), type=Path)
    parser.add_argument('--editor', choices=['emacs', 'vi'], required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--pairs', default=20, type=int)
    parser.add_argument('--runs', default=2, type=int)
    args = parser.parse_args()
    for argument in ('candidate', 'nvm', 'baseline_dir', 'bash', 'zsh', 'output'):
        setattr(args, argument, getattr(args, argument).resolve())
    if not args.output.is_relative_to(ROOT / 'tmp') or args.output.exists() or min(args.pairs, args.runs) < 1:
        parser.error('use a fresh output under tmp/ and positive counts')
    abi = subprocess.check_output([args.bash, '-c', 'printf "%s%s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'],
                                  text=True)
    baseline = args.baseline_dir / f'binit-{abi}'
    zinit_archive = args.baseline_dir / 'zinit.tar.gz'
    if (abi not in BINIT_HASHES or digest(baseline) != BINIT_HASHES[abi] or
            digest(zinit_archive) != ZINIT_HASH or digest(args.nvm) != NVM_HASH):
        parser.error('a frozen baseline artifact or NVM source does not match')
    binaries = {'baseline': baseline, 'candidate': args.candidate, 'zinit': None, 'baseline-control': baseline}
    inputs = [Path(__file__), POLICY, baseline, args.candidate, zinit_archive, args.nvm, args.bash, args.zsh,
              Path(sys.executable), ROOT / 'scripts/bench_interactive.py', ROOT / 'scripts/bench_ecosystem.py']
    identities = {str(path): digest(path) for path in inputs}
    result = {'schema': 1, 'abi': abi, 'editor': args.editor, 'input_sha256': identities, 'runs': [],
              'command_gate_pass': False, 'release_gate_pass': False}
    args.output.mkdir()
    receipt = args.output / 'result.json'
    try:
        with tempfile.TemporaryDirectory(dir=args.output) as scratch:
            work = Path(scratch)
            zinit = work / 'zinit'
            zinit.mkdir()
            with tarfile.open(zinit_archive) as bundle:
                bundle.extractall(zinit, filter='data')
            arms = {name: prepare(work / name, binary, args.nvm, zinit, args.editor, args.bash, args.zsh)
                    for name, binary in binaries.items()}
            for name, arm in arms.items():
                observe(args.bash, arm, args.output / f'prime-{name}.trace')
            for run in range(args.runs):
                pairs = []
                result['runs'].append(pairs)
                for index in range(args.pairs):
                    order = list(arms)
                    offset = (run + index) % len(order)
                    pairs.append({name: observe(args.bash, arms[name], args.output / f'{run}-{index}-{name}.trace')
                                  for name in order[offset:] + order[:offset]})
                    receipt.write_text(json.dumps(result, indent=2) + '\n')
            if any(digest(path) != sha256 for path, sha256 in identities.items()):
                raise RuntimeError('benchmark inputs changed')
            result['p95_ms'] = [{name: {metric: p95([pair[name][metric] for pair in run]) for metric in METRICS}
                                for name in arms} for run in result['runs']]
            result['ratios'] = [{name: {metric: run['candidate'][metric] / run[name][metric] for metric in METRICS}
                                for name in ('baseline', 'zinit')} for run in result['p95_ms']]
            result['control_ratios'] = [{metric: run['baseline-control'][metric] / run['baseline'][metric]
                                         for metric in METRICS} for run in result['p95_ms']]
            limit = json.loads(POLICY.read_text())['timing']['maximum_win_ratio']
            result['command_gate_pass'] = (args.runs == 2 and args.pairs == 20 and all(
                ratio <= limit for run in result['ratios'] for arm in run.values() for ratio in arm.values()))
    except BaseException as error:
        result['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        receipt.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
