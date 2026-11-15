#!/usr/bin/env python3
"""Observe pinned upstream template selections and standalone editor startup."""
import argparse
import glob
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
POLICY = ROOT / 'benchmarks/ecosystem-defaults.json'
PROMPT = b'[Oh My Bash] Would you like to check for updates? [Y/n]: '
METRICS = ('startup_visible_ms', 'startup_usable_ms', 'effect_probe_ms', 'required_effects_ms')
EXPECTED_LINKS = {
    '250---base.plugin.bash': '../plugins/available/base.plugin.bash',
    '800---aliases.completion.bash': '../completion/available/aliases.completion.bash',
    '350---bash-it.completion.bash': '../completion/available/bash-it.completion.bash',
    '325---system.completion.bash': '../completion/available/system.completion.bash',
    '750---general.aliases.bash': '../aliases/available/general.aliases.bash',
    '750---bash-it.aliases.bash': '../aliases/available/bash-it.aliases.bash',
    '750---directory.aliases.bash': '../aliases/available/directory.aliases.bash',
    '750---editor.aliases.bash': '../aliases/available/editor.aliases.bash',
}


class DefaultShell(CheckedShell):
    def __init__(self, *args, decline_due=False, **kwargs):
        self.decline_due = decline_due
        self.declined = False
        super().__init__(*args, **kwargs)

    def until(self, predicate, seconds=10):
        deadline = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() >= deadline:
                raise RuntimeError('terminal timed out:\n' + '\n'.join(self.screen.display))
            self.read(.01)
            if PROMPT in self.raw and not self.declined:
                if not self.decline_due:
                    raise RuntimeError('unexpected due update prompt')
                os.write(self.fd, b'n\n')
                self.declined = True


def tree_identity(root, ignored=()):
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError(f'invalid case tree: {root}')
    ignored = set(ignored)
    result = {'.': {'directory_mode': root.stat().st_mode & 0o777}}
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in list(dirs):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if relative in ignored:
                dirs.remove(name)
            elif path.is_symlink():
                dirs.remove(name)
                files.append(name)
            else:
                result[relative] = {'directory_mode': path.stat().st_mode & 0o777}
        for name in files:
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if relative in ignored:
                continue
            if path.is_symlink():
                result[relative] = {'link': os.readlink(path)}
            elif path.is_file():
                result[relative] = {'sha256': digest(path), 'mode': path.stat().st_mode & 0o777}
            else:
                raise RuntimeError(f'non-regular case input: {path}')
    return result


def source_pins(source, policy):
    checked = {}
    for case in policy['cases']:
        for field in ('template', 'profile', 'updater', 'entry'):
            if field in case:
                path = source / 'sources' / case[field]
                value = digest(path)
                if value != case[field + '_sha256']:
                    raise RuntimeError(f'pinned {field} differs for {case["id"]}')
                checked[case[field]] = value
    return checked


def default_probe(case):
    identifier = case['id']
    if identifier == 'bash-it-template':
        return ('[[ ${BASH_IT_THEME-} == bobby ]] && declare -F prompt_command >/dev/null && '
                'declare -F ips >/dev/null && complete -p bash-it >/dev/null && '
                "[[ ${BASH_ALIASES[edit]-} == '${EDITOR:-${ALTERNATE_EDITOR:-nano}}' && "
                "${BASH_ALIASES[ll]-} == 'ls -Al' && ${BASH_ALIASES[shit]-} == bash-it && "
                "${BASH_ALIASES[cd..]-} == 'cd ..' ]] && [[ -d \"$BASH_IT/enabled\" ]]")
    if identifier.startswith('omb-template-'):
        required = case.get('components') or [
            'completion:git', 'completion:composer', 'completion:ssh', 'alias:general',
            'plugin:git', 'plugin:bashmarks', 'theme:font']
        modules = [value for value in required if not value.startswith('theme:')]
        checks = ' && '.join(f'[[ " ${{_omb_module_loaded-}} " == *{shlex.quote(" " + value + " ")}* ]]'
                             for value in modules)
        return ('[[ ${OSH_THEME-} == font && ${DISABLE_AUTO_UPDATE-} != true ]] && '
                'declare -F _omb_theme_PROMPT_COMMAND >/dev/null && declare -F bm >/dev/null && '
                "[[ ${BASH_ALIASES[ga]-} == 'command git add' && ${BASH_ALIASES[g]-} == 'bm -g' "
                "&& ${BASH_ALIASES[ll]-} == 'ls -lAFh' ]] && complete -p composer ssh >/dev/null && " + checks)
    if identifier == 'blesh-trial':
        return '[[ -n ${BLE_VERSION-} && -n ${_ble_attached-} ]] && declare -F ble-attach >/dev/null'
    if identifier == 'zinit-loader':
        return 'typeset -f zinit >/dev/null && [[ ${ZINIT[HOME_DIR]-} == "$HOME/zinit" && -d ${ZINIT[HOME_DIR]} ]]'
    if identifier in ('bash-bare', 'zsh-bare'):
        return ':'
    raise RuntimeError(f'unknown default case: {identifier}')


def copy_ble_build(source, destination):
    """Copy the pinned build; initialize runtime caches only inside the private case."""
    def exclude_runtime_cache(path, names):
        return {'cache.d'} if Path(path) == source and 'cache.d' in names else set()
    source_inventory = corpus.inventory(source)
    expected = {key: value for key, value in source_inventory.items()
                if key != 'cache.d' and not key.startswith('cache.d/')}
    shutil.copytree(source, destination, symlinks=True, ignore=exclude_runtime_cache)
    if corpus.inventory(destination) != expected:
        raise RuntimeError('private ble.sh build copy differs from pinned source')
    return {'copied_files': expected, 'source_runtime_cache': {
        key: value for key, value in source_inventory.items() if key == 'cache.d' or key.startswith('cache.d/')}}


def configure(case, directory, sources, bash):
    directory.mkdir()
    home = directory / 'home'
    home.mkdir()
    cwd = directory / 'cwd'
    cwd.mkdir()
    identifier = case['id']
    setup = 'PS1=""\n'
    if identifier == 'bash-it-template':
        source = sources / 'bash-it'
        installed = home / '.bash_it'
        shutil.copytree(source, installed, symlinks=True)
        if corpus.inventory(installed) != corpus.inventory(source):
            raise RuntimeError('private Bash-it copy differs from pinned source')
        env = corpus.environment(home)
        process = subprocess.run([bash, str(installed / 'install.sh'), '--silent'], cwd=cwd,
                                 env=env, capture_output=True, timeout=30)
        (directory / 'installer.stdout').write_bytes(process.stdout)
        (directory / 'installer.stderr').write_bytes(process.stderr)
        if process.returncode:
            raise RuntimeError(f'private Bash-it default preparation failed: {process.returncode}')
        actual = {item.name: os.readlink(item) for item in (installed / 'enabled').iterdir() if item.is_symlink()}
        if actual != EXPECTED_LINKS or len(list((installed / 'enabled').iterdir())) != len(EXPECTED_LINKS):
            raise RuntimeError(f'Bash-it installer did not select the declared default profile: {actual!r}')
        expected_rc = (source / 'template/bashrc.template.bash').read_text().replace('{{BASH_IT}}', str(installed))
        if (home / '.bashrc').read_text() != expected_rc:
            raise RuntimeError('Bash-it installer did not produce the pinned template')
        setup += 'source "$HOME/.bashrc"\n'
    elif identifier.startswith('omb-template-'):
        source = sources / 'oh-my-bash'
        installed = home / '.oh-my-bash'
        shutil.copytree(source, installed, symlinks=True)
        if corpus.inventory(installed) != corpus.inventory(source):
            raise RuntimeError('private Oh My Bash copy differs from pinned source')
        shutil.copyfile(source / 'templates/bashrc.osh-template', home / '.bashrc')
        setup += 'source "$HOME/.bashrc"\n'
    elif identifier == 'blesh-trial':
        private_out = home / 'ble/out'
        private_out.parent.mkdir()
        copied = copy_ble_build(sources / 'ble/out', private_out)
        (directory / 'ble-copy.json').write_text(json.dumps(copied, indent=2) + '\n')
        setup += f'source {shlex.quote(str(private_out / "ble.sh"))}\n'
    elif identifier == 'zinit-loader':
        setup += ('typeset -gA ZINIT\nZINIT[HOME_DIR]="$HOME/zinit"\n'
                  f'source {shlex.quote(str(sources / "zinit/zinit.zsh"))}\n')
    zsh = case['shell'] == 'zsh'
    setup += corpus.prompt_hook(zsh)
    rc = directory / 'observer.rc'
    rc.write_text(setup)
    if zsh:
        shutil.copyfile(rc, home / '.zshrc')
    return rc, home, cwd


def reset_inputs(case, home):
    history = home / '.bash_history'
    if case['shell'] == 'bash':
        history.write_text('')
    if case['id'] == 'omb-template-due-decline':
        (home / '.osh-update').write_text(case['initial_update_timestamp'])


def ignored_inputs(case):
    ignored = ['.bash_history'] if case['shell'] == 'bash' else []
    if case['id'] == 'omb-template-due-decline':
        ignored.append('.osh-update')
    return ignored


def observe(case, interpreter, rc, home, cwd, failure_path):
    env = corpus.environment(home)
    env['BINIT_BASH'] = interpreter
    env['SHELL'] = interpreter
    runtime = cwd.parent / 'runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    env['XDG_RUNTIME_DIR'] = str(runtime)
    env['GIT_CEILING_DIRECTORIES'] = str(cwd.parent)
    command = [interpreter, '-d', '-i'] if case['shell'] == 'zsh' else None
    due = case['id'] == 'omb-template-due-decline'
    started = time.perf_counter_ns()
    shell = DefaultShell(interpreter, rc, cwd, env, command, decline_due=due, receipt_path=failure_path)
    try:
        visible = shell.startup_ms
        if due and not shell.declined:
            raise RuntimeError('expected due update prompt was not observed and declined')
        corpus.wait_for_editor(shell)
        ack_started = time.perf_counter_ns()
        shell.settled(b':')
        quiet = (time.perf_counter_ns() - ack_started) / 1e6
        shell.command('')
        ack = (time.perf_counter_ns() - ack_started) / 1e6
        usable = (time.perf_counter_ns() - started) / 1e6
        offset = len(shell.raw)
        probe = default_probe(case)
        effect_ms = shell.command('{ ' + probe + '; }; corpus_status=$?; '
                                  'printf "\\036CORPUS:%s\\036" "$corpus_status"')
        statuses = re.findall(rb'\x1eCORPUS:(\d+)\x1e', shell.raw[offset:])
        if statuses != [b'0']:
            raise RuntimeError(f'{case["id"]}: default effect failure: {statuses!r}')
        if case['id'].startswith('omb-template-'):
            stamp = (home / '.osh-update').read_text().strip()
            match = re.fullmatch(r'LAST_EPOCH=(\d+)', stamp)
            if not match or not (int(time.time() // 86400) - 1 <= int(match[1]) <= int(time.time() // 86400) + 1):
                raise RuntimeError('default updater did not preserve a current private timestamp')
        else:
            stamp = None
        return {'startup_visible_ms': visible, 'first_input_ack_ms': ack,
                'input_echo_quiet_ms': quiet, 'startup_usable_ms': usable,
                'effect_probe_ms': effect_ms, 'required_effects_ms': (time.perf_counter_ns() - started) / 1e6,
                'update_prompt_declined': shell.declined, 'update_timestamp': stamp,
                'resources_at_readiness': corpus.resource_sample(shell.pid),
                'effect_status': 0, 'terminal_bytes': len(shell.raw)}
    except BaseException as error:
        failure_path.write_bytes(shell.raw)
        raise RuntimeError(f'{case["id"]}: {error}\n' + '\n'.join(shell.screen.display)) from error
    finally:
        shell.close()


def run_summary(rows, case_ids, pairs):
    expected = {(case, pair) for case in case_ids for pair in range(pairs)}
    seen = set()
    for row in rows:
        key = (row['case'], row['pair'])
        if key not in expected or key in seen or row.get('effect_status') != 0 or not row.get('state_unchanged'):
            raise RuntimeError('unexpected, duplicate or failed default observation')
        for metric in METRICS:
            value = row[metric]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise RuntimeError(f'invalid default elapsed time: {metric}')
        seen.add(key)
    if seen != expected:
        raise RuntimeError('incomplete default observations')
    return {case: {metric: summarize([row[metric] for row in rows if row['case'] == case])
                   for metric in METRICS} for case in case_ids}


def inputs(args, scenarios):
    paths = [POLICY, corpus.SPEC, corpus.FIXTURE, Path(__file__), ROOT / 'scripts/bench_ecosystem.py', ROOT / 'scripts/bench_corpus_terminal.py',
             ROOT / 'scripts/bench_interactive.py', Path(sys.executable), Path(args.bash), Path(args.zsh),
             Path('/usr/bin/git')]
    for name in ('tput', 'uname', 'date', 'sed', 'awk', 'ls', 'sort', 'mkdir', 'rmdir', 'stty'):
        resolved = shutil.which(name, path='/usr/bin:/bin')
        if resolved:
            paths.append(Path(resolved))
    system_completion = ['/etc/bash_completion', '/etc/profile.d/bash_completion.sh',
                         '/usr/share/bash-completion/bash_completion', '/usr/share/bash-completion/completions/git']
    system_completion += sorted(glob.glob('/usr/share/git*/contrib/completion/git-completion.bash'))
    system_completion += sorted(glob.glob('/usr/share/doc/git-*/contrib/completion/git-completion.bash'))
    external = {path: digest(Path(path)) if Path(path).is_file() else None for path in system_completion}
    return {'files': {str(path.resolve()): digest(path) for path in paths},
            'system_completion': external,
            'cases': {key: {'rc': digest(rc), 'home': tree_identity(home, ignored_inputs(case)),
                            'cwd': tree_identity(cwd),
                            'ble_copy_receipt': (digest(home.parent / 'ble-copy.json')
                                                 if case['id'] == 'blesh-trial' else None)}
                      for key, (case, rc, home, cwd) in scenarios.items()}}


def save(output, result):
    temporary = output.with_name(output.name + '.writing')
    with temporary.open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    os.replace(temporary, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--bash', default='/bin/bash')
    parser.add_argument('--zsh', default='/bin/zsh')
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pairs', type=int, default=20)
    parser.add_argument('--runs', type=int, default=2)
    parser.add_argument('--cases', nargs='+')
    args = parser.parse_args()
    args.work, args.output, args.sources = args.work.resolve(), args.output.resolve(), args.sources.resolve()
    if not args.work.is_relative_to(ROOT / 'tmp') or not args.output.is_relative_to(ROOT / 'tmp'):
        parser.error('work and output must be under project tmp/')
    if args.output.exists() or args.work.exists() or args.runs < 1 or args.pairs < 1:
        parser.error('use fresh paths and positive run and pair counts')
    policy = json.loads(POLICY.read_text())
    all_cases = {case['id']: case for case in policy['cases']}
    selected = args.cases or list(all_cases)
    if len(set(selected)) != len(selected) or any(case not in all_cases for case in selected):
        parser.error('cases must be distinct and listed in the frozen policy')
    args.work.mkdir(parents=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as stream:
        stream.write('{}\n')
    result = {'schema': 1, 'phase': 'documented-template-and-roles', 'runs': [], 'priming': {},
              'complete_two_run_defaults': False, 'unsupported': policy['unsupported']}
    scenarios = {}
    try:
        source_lock = json.loads(corpus.SPEC.read_text())
        result['source_receipts'] = corpus.verify_sources(args.sources, source_lock)
        result['source_pins'] = source_pins(args.sources, policy)
        for key in selected:
            case = all_cases[key]
            result['active_case'] = ['prepare', key]
            directory = args.work / key
            rc, home, cwd = configure(case, directory, args.sources / 'sources', args.bash)
            scenarios[key] = (case, rc, home, cwd)
            reset_inputs(case, home)
            interpreter = args.zsh if case['shell'] == 'zsh' else args.bash
            result['active_case'] = ['prime', key]
            result['priming'][key] = observe(case, interpreter, rc, home, cwd, directory / 'prime-failure.raw')
            reset_inputs(case, home)
            result['priming'][key]['private_state'] = tree_identity(home, ignored_inputs(case))
            save(args.output, result)
        result['inputs'] = inputs(args, scenarios)
        result['interpreter_versions'] = {name: subprocess.run([path, '--version'], capture_output=True,
            text=True, check=True).stdout.splitlines()[0] for name, path in [('bash', args.bash), ('zsh', args.zsh)]}
        for run in range(args.runs):
            samples = []
            result['runs'].append(samples)
            for pair in range(args.pairs):
                for case, _, home, _ in scenarios.values():
                    reset_inputs(case, home)
                if inputs(args, scenarios) != result['inputs']:
                    raise RuntimeError('default inputs changed before pair')
                for key in list(scenarios)[::1 if (run + pair) % 2 == 0 else -1]:
                    case, rc, home, cwd = scenarios[key]
                    result['active_case'] = ['measure', run, pair, key]
                    interpreter = args.zsh if case['shell'] == 'zsh' else args.bash
                    before = tree_identity(home, ignored_inputs(case))
                    observation = observe(case, interpreter, rc, home, cwd,
                                          args.work / key / f'failure-{run}-{pair}.raw')
                    after = tree_identity(home, ignored_inputs(case))
                    observation['state_unchanged'] = before == after and tree_identity(cwd) == result['inputs']['cases'][key]['cwd']
                    history = home / '.bash_history'
                    observation['private_mutable_after'] = {
                        'history_sha256': digest(history) if history.is_file() else None,
                        'update_timestamp': observation['update_timestamp']}
                    if before != after:
                        observation['changed_private_state'] = after
                    samples.append({'case': key, 'pair': pair, **observation})
                    save(args.output, result)
                    if not observation['state_unchanged']:
                        raise RuntimeError(f'{key}: undeclared private state changed')
                print(f'run {run + 1} pair {pair + 1}/{args.pairs}: default effects passed', flush=True)
            for case, _, home, _ in scenarios.values():
                reset_inputs(case, home)
            if inputs(args, scenarios) != result['inputs']:
                raise RuntimeError('default inputs changed during run')
            after_sources = corpus.verify_sources(args.sources, source_lock)
            result.setdefault('sources_after_runs', []).append(after_sources)
            if after_sources != result['source_receipts']:
                raise RuntimeError('pinned sources changed during run')
            after_pins = source_pins(args.sources, policy)
            result.setdefault('source_pins_after_runs', []).append(after_pins)
            if after_pins != result['source_pins']:
                raise RuntimeError('pinned default inputs changed during run')
        result['summary_by_run'] = [run_summary(rows, selected, args.pairs) for rows in result['runs']]
        result['complete_two_run_defaults'] = (args.runs == policy['observation']['runs']
            and args.pairs == policy['observation']['pairs_per_run'] and selected == list(all_cases))
        result.pop('active_case', None)
    except BaseException as error:
        result['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        save(args.output, result)


if __name__ == '__main__':
    main()
