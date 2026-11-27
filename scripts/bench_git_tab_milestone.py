#!/usr/bin/env python3
"""Compare first and warm Git branch completion using the installed providers."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

import bench_ecosystem as corpus
from bench_development_milestone import BASELINE, BINIT_HASHES, POLICY, ROOT, ZINIT_HASH
from bench_ecosystem_primed import inventory_tree
from bench_interactive import PROMPT, Shell, digest
from bench_nvm_milestone import accepted, p95
from bench_seven_plugin_startup import PLUGIN_HASHES

METRICS = ('first', 'warm')
PREFIX = 'git switch qualify-can'
LINE = 'git switch qualify-candidate'
HEAD = 'ref: refs/heads/qualify-candidate\n'


def prepare(directory, binary, package, zinit, args, git):
    home = directory / 'home'
    home.mkdir(parents=True)
    environment = corpus.environment(home)
    common = [git, '-c', 'core.hooksPath=/dev/null']
    branches = ('qualify-candidate', *(f'other-{i}' for i in range(10)))
    for command in (['init', '-q', '-b', 'fixture'],
                    ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                     'commit', '--allow-empty', '-q', '-m', 'fixture'],
                    ['tag', 'fixture-release'], *(['branch', name] for name in branches)):
        subprocess.run([*common, *command], cwd=directory, env=environment, check=True, timeout=30)
    if binary is None:
        environment['SHELL'] = str(args.zsh)
        rc = home / '.zshrc'
        rc.write_text(
            "HISTFILE=/dev/null\ntypeset -gA ZINIT\nZINIT[HOME_DIR]=\"$HOME/zinit\"\n"
            f"source {shlex.quote(str(zinit / 'zinit.zsh'))}\n"
            f"fpath=({shlex.quote(str(args.zsh_completion.parent))} $fpath)\n"
            "autoload -Uz compinit\ncompinit -i\n"
            f"bindkey -{'v' if args.editor == 'vi' else 'e'}\nPROMPT='BENCH> '\n")
        return directory, rc, environment, [str(args.zsh), '-d', '-i']
    environment.update(BINIT_BASH=str(args.bash), SHELL=str(args.bash),
                       BINIT_GIT_COMPLETION_FILE=str(args.bash_completion))
    config = directory / 'config.toml'
    config.write_text('schema=1\n[[plugins]]\npath=' + json.dumps(str(package)) + '\n')
    state = directory / 'state'
    subprocess.run([binary, 'prepare', config, '--state', state], env=environment, check=True,
                   stdout=subprocess.DEVNULL, timeout=30)
    init = shlex.join([str(binary), 'init', '--state', str(state)])
    rc = directory / 'rc'
    rc.write_text(f"set -o {args.editor}\nHISTFILE=/dev/null\nPS1='BENCH> '\neval \"$({init})\"\n")
    return directory, rc, environment, None


def observe(bash, arm, failure):
    directory, rc, environment, shell_command = arm
    shell = Shell(str(bash), rc, directory, environment, shell_command)

    def shown(line):
        return (shell.screen.display[shell.screen.cursor.y].rstrip().endswith(PROMPT.decode() + line)
                and shell.screen.cursor.x >= len(PROMPT) + len(line))

    def check(expression, marker):
        accepted(shell, expression + " && printf '\\036GIT:" + marker + "\\037'")
        if b'\x1eGIT:' + marker.encode() + b'\x1f' not in shell.raw:
            raise RuntimeError(f'Git completion failed its {marker} state check')

    try:
        corpus.wait_for_editor(shell)
        if shell_command is None:
            check('[[ ${__binit_state[git-completion]} == pending ]]', 'pending')
        else:
            check("[[ ${functions[_git]} == *'builtin autoload -X'* ]]", 'pending')
        times = {}
        head = directory / '.git/HEAD'
        for metric in METRICS:
            # ZLE writes the prompt before restoring editor input mode.
            corpus.wait_for_editor(shell)
            os.write(shell.fd, PREFIX[:1].encode())
            shell.until(lambda: shown(PREFIX[:1]))
            os.write(shell.fd, PREFIX[1:].encode())
            shell.until(lambda: shown(PREFIX))
            started = time.perf_counter_ns()
            os.write(shell.fd, b'\t')
            shell.until(lambda: shown(LINE))
            times[metric] = (time.perf_counter_ns() - started) / 1e6
            accepted(shell, '')
            if head.read_text() != HEAD:
                raise RuntimeError('accepted completion did not switch to the fixture branch')
            accepted(shell, 'git switch -q fixture')
            if head.read_text() != 'ref: refs/heads/fixture\n':
                raise RuntimeError('failed to reset the fixture branch')
        if shell_command is None:
            check('[[ ${__binit_state[git-completion]} == loaded ]]', 'loaded')
        else:
            check("[[ -n ${functions[_git]} && ${functions[_git]} != *'builtin autoload -X'* ]]", 'loaded')
        return times
    except BaseException:
        failure.write_bytes(shell.raw)
        raise
    finally:
        shell.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--plugin', required=True, type=Path)
    parser.add_argument('--baseline-dir', default=BASELINE, type=Path)
    parser.add_argument('--bash', default=Path('/usr/bin/bash'), type=Path)
    parser.add_argument('--zsh', default=Path('/usr/bin/zsh'), type=Path)
    parser.add_argument('--bash-completion', required=True, type=Path)
    parser.add_argument('--zsh-completion', required=True, type=Path)
    parser.add_argument('--editor', choices=['emacs', 'vi'], required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--pairs', default=20, type=int)
    parser.add_argument('--runs', default=2, type=int)
    args = parser.parse_args()
    for argument in ('candidate', 'plugin', 'baseline_dir', 'bash', 'zsh', 'output',
                     'bash_completion', 'zsh_completion'):
        setattr(args, argument, getattr(args, argument).resolve())
    if not args.output.is_relative_to(ROOT / 'tmp') or args.output.exists() or min(args.pairs, args.runs) < 1:
        parser.error('use a fresh output under tmp/ and positive counts')
    if args.zsh_completion.name != '_git':
        parser.error('zsh-completion must point to the installed _git function')
    abi = subprocess.check_output([args.bash, '-c', 'printf "%s%s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'],
                                  text=True)
    baseline = args.baseline_dir / f'binit-{abi}'
    zinit_archive = args.baseline_dir / 'zinit.tar.gz'
    plugin_archive = args.baseline_dir / 'git-completion.tar.gz'
    if (abi not in BINIT_HASHES or digest(baseline) != BINIT_HASHES[abi] or
            digest(zinit_archive) != ZINIT_HASH or digest(plugin_archive) != PLUGIN_HASHES['git-completion']):
        parser.error('a frozen baseline artifact does not match')
    git = Path(shutil.which('git', path=os.defpath)).resolve()
    inputs = [Path(__file__), POLICY, baseline, args.candidate, zinit_archive, plugin_archive, git, args.bash,
              args.zsh, args.bash_completion, args.zsh_completion, Path(sys.executable),
              *(ROOT / 'scripts' / name for name in ('bench_interactive.py', 'bench_nvm_milestone.py',
                                                     'bench_ecosystem.py', 'bench_ecosystem_primed.py',
                                                     'bench_seven_plugin_startup.py'))]
    identities = {str(path): digest(path) for path in inputs}
    package_identity = inventory_tree(args.plugin)
    result = {'schema': 1, 'abi': abi, 'editor': args.editor, 'input_sha256': identities,
              'candidate_plugin': package_identity, 'runs': [], 'git_tab_gate_pass': False,
              'release_gate_pass': False}
    args.output.mkdir()
    receipt = args.output / 'result.json'
    try:
        with tempfile.TemporaryDirectory(dir=args.output) as scratch:
            work = Path(scratch)
            zinit, before = work / 'zinit-source', work / 'baseline-plugin'
            for archive, destination in ((zinit_archive, zinit), (plugin_archive, before)):
                destination.mkdir()
                with tarfile.open(archive) as bundle:
                    bundle.extractall(destination, filter='data')
            configs = {'baseline': (baseline, before), 'candidate': (args.candidate, args.plugin),
                       'zinit': (None, None), 'baseline-control': (baseline, before)}
            arms = {name: prepare(work / name, *config, zinit, args, git) for name, config in configs.items()}
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
            if any(digest(path) != sha256 for path, sha256 in identities.items()) or inventory_tree(args.plugin) != package_identity:
                raise RuntimeError('benchmark inputs changed')
            result['p95_ms'] = [{name: {metric: p95([pair[name][metric] for pair in run]) for metric in METRICS}
                                 for name in arms} for run in result['runs']]
            result['ratios'] = [{name: {metric: run['candidate'][metric] / run[name][metric] for metric in METRICS}
                                 for name in ('baseline', 'zinit')} for run in result['p95_ms']]
            result['control_ratios'] = [{metric: run['baseline-control'][metric] / run['baseline'][metric]
                                        for metric in METRICS} for run in result['p95_ms']]
            limit = json.loads(POLICY.read_text())['timing']['maximum_win_ratio']
            result['git_tab_gate_pass'] = (args.runs == 2 and args.pairs == 20 and all(
                ratio <= limit for run in result['ratios'] for arm in run.values() for ratio in arm.values()))
    except BaseException as error:
        result['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        receipt.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
