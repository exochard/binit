#!/usr/bin/env python3
"""Validate and measure the two frozen current-shell runtime workflows."""
import argparse
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import time

from bench_ecosystem import BoundedShell, environment, inventory, resource_sample
from bench_interactive import digest, summarize
from bench_lifecycle import FRAME, prepare

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / 'benchmarks/runtime-workflows.json'
GIT_PATH = 'aliases/available/git.aliases.bash'
CASES = ('git-shortcuts-first-use', 'toolchain-readiness-during-work')
FAULTS = ('skip-load', 'wrong-definition', 'double-source', 'no-watch', 'wrong-status', 'changed-identity')
HELPERS = '''about-alias() { :; }
url() { :; }
_bash-it-component-item-is-enabled() { return 1; }
_log_warning() { printf '%s\\n' "$*" >&2; }
_command_exists() { command -v "$1" >/dev/null 2>&1; }
wf_unrelated() { printf '%s' kept; }
alias wf_unrelated_alias=': unrelated'
export WF_UNRELATED='literal $value'
'''
COMMON = '''wf_state() {
    printf '%s\\n' "$-" "$IFS"
    declare -p WF_UNRELATED
    set +o; shopt -p; umask; trap -p; bind -p
    declare -f about-alias url _bash-it-component-item-is-enabled _log_warning _command_exists wf_unrelated
    alias wf_unrelated_alias
    complete -p wf_unrelated
    return 0
}
wf_preserved() {
    wf_state > "$WF_ROOT/state-after"
    cmp -s -- "$WF_ROOT/state-before" "$WF_ROOT/state-after"
}
wf_git_check() {
    [[ $WF_SOURCES == 1 && $WF_EXPORT == ready ]] || return 31
    alias -p > "$WF_ROOT/aliases-actual"
    declare -f gdv get_default_branch > "$WF_ROOT/functions-actual"
    cmp -s -- "$WF_ROOT/aliases-golden" "$WF_ROOT/aliases-actual" || return 32
    cmp -s -- "$WF_ROOT/functions-golden" "$WF_ROOT/functions-actual" || return 33
    [[ $(eval 'g status --porcelain=v1') == '?? "untracked file"' ]] || return 34
    [[ $(get_default_branch) == main ]] || return 35
    [[ $(command printenv WF_EXPORT) == ready ]] || return 36
    wf_preserved || return 38
}
wf_ready() { [[ -f $WF_ROOT/build/toolchain.ready ]]; }
wf_prompt() { printf '\\036WF-PROMPT:%s:%s\\037' "$WF_COMMANDS" "${WF_SOURCES:-0}"; }
wf_watch_effects() {
    declare -p WF_COMMANDS WF_SOURCES WF_EXPORT WF_ARGC WF_ARGS WF_CALLS
    declare -f wf_tool
}
wf_command() {
    local number=$1 rc
    [[ $WF_COMMANDS == $((number - 1)) ]] || return 41
    WF_COMMANDS=$number
    if ((number <= 11)); then [[ ${WF_SOURCES:-0} == 0 ]] || return 42; fi
    if ((number <= 11)); then [[ ! -e $WF_ROOT/build/toolchain.ready ]] || return 50; fi
    if ((number >= 12)); then [[ $(< "$WF_ROOT/build/toolchain.ready") == ready ]] || return 51; fi
    if ((number <= 13)); then [[ $(< "$WF_ROOT/settings.env") == mode=development ]] || return 52; fi
    if ((number >= 14)); then [[ $(< "$WF_ROOT/settings.env") == mode=ready ]] || return 53; fi
    case $number in
        11) printf 'ready\\n' > "$WF_ROOT/build/toolchain.ready" ;;
        12)
            [[ ${WF_SOURCES:-0} == 1 ]] || return 43
            wf_tool 'two words' '' '*' "a'b" '$HOME' 'back\\slash'; rc=$?
            [[ $rc == 37 && $WF_ARGC == 6 && $WF_CALLS == 1 ]] || return 44
            [[ $WF_ARGS == "<two words><><*><a'b><\\$HOME><back\\\\slash>" ]] || return 45
            [[ $PWD == "$WF_ROOT/target with spaces" && $WF_EXPORT == ready ]] || return 46
            [[ $(command printenv WF_EXPORT) == ready ]] || return 47 ;;
        13) printf 'mode=ready\\n' > "$WF_ROOT/settings.env" ;;
    esac
    if ((number >= 12)); then [[ $WF_SOURCES == 1 ]] || return 48; fi
    [[ $- == "$WF_FLAGS" && $WF_UNRELATED == 'literal $value' ]] || return 49
    return 0
}
'''
WATCH_BODY = '''WF_SOURCES=$(( ${WF_SOURCES:-0} + 1 ))
export WF_EXPORT=ready
wf_tool() {
    WF_ARGC=$#; WF_CALLS=$(( ${WF_CALLS:-0} + 1 ))
    printf -v WF_ARGS '<%s>' "$@"
    cd -- "$WF_ROOT/target with spaces" || return 98
    return 37
}
'''


def action(shell, code, prompt=None):
    offset = len(shell.raw)
    elapsed = shell.command('{ ' + code + '; }; wf_status=$?; '
                            '[[ $# == 4 && $1 == "one value" && -z $2 && $3 == \'$HOME\' && $4 == \'back\\slash\' ]] || wf_status=54; '
                            'printf "\\036BINIT-STATUS:%s\\037" "$wf_status"')
    statuses = FRAME.findall(shell.raw[offset:])
    if statuses != [b'0']:
        raise RuntimeError(f'workflow effect failure: {statuses!r}')
    if prompt is not None:
        frames = re.findall(rb'\x1eWF-PROMPT:(\d+):(\d+)\x1f', shell.raw[offset:])
        if frames != [(str(prompt).encode(), b'0' if prompt < 11 else b'1')]:
            raise RuntimeError(f'normal prompt activation failure at command {prompt}: {frames!r}')
    return elapsed


def git(directory, *arguments):
    return subprocess.run(['/usr/bin/git', '-C', str(directory), *arguments],
                          env={**environment(directory.parent), 'GIT_AUTHOR_DATE': '2000-01-01T00:00:00+0000',
                               'GIT_COMMITTER_DATE': '2000-01-01T00:00:00+0000'}, capture_output=True,
                          check=True, timeout=10).stdout


def make_fixture(case, directory, source, fault=None):
    directory.mkdir(parents=True)
    (directory / 'home').mkdir()
    (directory / 'target with spaces').mkdir()
    plugin = directory / 'plugin'
    plugin.mkdir()
    if case == CASES[0]:
        repo = directory / 'repository'
        repo.mkdir()
        git(repo, 'init', '-b', 'main')
        git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            'commit', '--allow-empty', '-m', 'fixture')
        git(repo, 'remote', 'add', 'origin', str(repo))
        git(repo, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        git(repo, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
        (repo / 'untracked file').write_text('fixture\n')
        (plugin / 'git.aliases.bash').write_bytes(source.read_bytes())
        body = ('WF_SOURCES=$(( ${WF_SOURCES:-0} + 1 ))\nexport WF_EXPORT=ready\n'
                'source "${BASH_SOURCE[0]%/*}/git.aliases.bash"\n')
        if fault == 'wrong-definition':
            body += "alias ga=': corrupted'\n"
        manifest = 'load="event"\nevent="shortcuts"\n'
    else:
        for item in json.loads(SPEC.read_text())['cases'][1]['watched_files']:
            path = directory / item['path']
            path.parent.mkdir(parents=True, exist_ok=True)
            if item['initial'] is not None:
                path.write_text(item['initial'])
        paths = [str(directory / item['path']) for item in json.loads(SPEC.read_text())['cases'][1]['watched_files']]
        body = WATCH_BODY.replace('return 37', 'return 36') if fault == 'wrong-status' else WATCH_BODY
        manifest = 'load="watch"\nwhen="wf_ready"\nwatch=' + json.dumps(paths) + '\n'
    if fault == 'double-source':
        body += 'WF_SOURCES=$((WF_SOURCES + 1))\n'
    (plugin / 'plugin.bash').write_text(body)
    (plugin / 'plugin.toml').write_text('schema=1\nid="workflow"\nentry="plugin.bash"\n' + manifest)
    config = directory / 'config.toml'
    config.write_text('schema=1\n[[plugins]]\npath="plugin"\n')
    rc = directory / 'rc'
    rc.write_text("PS1='BENCH> '\nPS2=''\nHISTFILE=/dev/null\nset -o emacs\n" + HELPERS + COMMON
                  + 'complete -W kept wf_unrelated\n'
                  + "set -- 'one value' '' '$HOME' 'back\\slash'\n"
                  + f'WF_ROOT={shlex.quote(str(directory))}\nWF_COMMANDS=0\n')
    return config, rc


def golden_git(directory, rc, source, bash):
    """Source the unmodified pin in an independent same-interpreter shell."""
    env = {**environment(directory / 'home'), 'BINIT_BASH': bash}
    shell = BoundedShell(bash, rc, directory, env)
    try:
        action(shell, 'source ' + shlex.quote(str(source)))
        action(shell, 'alias -p > "$WF_ROOT/aliases-golden"; '
                      'declare -f gdv get_default_branch > "$WF_ROOT/functions-golden"')
    finally:
        (directory / 'golden-terminal.raw').write_bytes(shell.raw)
        shell.close()
    aliases = re.findall(r'^alias ([^=]+)=', (directory / 'aliases-golden').read_text(), re.M)
    report = ['Plugin: workflow', 'State: loaded', 'function gdv', 'function get_default_branch']
    report += ['alias ' + name for name in aliases if re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_-]*', name)
               and name != 'wf_unrelated_alias']
    (directory / 'report-golden').write_text(''.join(line + '\n' for line in sorted(report)))


def golden_watch(directory, source, bash):
    control = directory / 'direct-source'
    _, rc = make_fixture(CASES[1], control, source)
    rc.write_text(rc.read_text() + 'wf_direct_dispatch() { if [[ ${WF_SOURCES:-0} == 0 ]] && wf_ready; then '
                  'source "$WF_ROOT/plugin/plugin.bash"; fi; }\n'
                  'PROMPT_COMMAND=(wf_direct_dispatch wf_prompt)\n')
    shell = BoundedShell(bash, rc, control, environment(control / 'home'))
    try:
        action(shell, 'WF_FLAGS=$-; wf_state > "$WF_ROOT/state-before"')
        for number in range(1, 21):
            action(shell, f'wf_command {number}', prompt=number)
        action(shell, 'wf_preserved && wf_watch_effects > "$WF_ROOT/effects-golden"')
        golden = (control / 'effects-golden').read_bytes()
        (directory / 'effects-golden').write_bytes(golden)
    finally:
        (control / 'terminal.raw').write_bytes(shell.raw)
        shell.close()


def watched_inputs(directory, final=False):
    result = {}
    for item in json.loads(SPEC.read_text())['cases'][1]['watched_files']:
        name = item['path']
        expected = item['initial']
        if final and name == 'settings.env':
            expected = 'mode=ready\n'
        if final and name == 'build/toolchain.ready':
            expected = 'ready\n'
        path = directory / name
        if path.is_symlink() or (expected is None and path.exists()):
            raise RuntimeError(f'watched fixture identity differs: {name}')
        if expected is not None and (not path.is_file() or path.read_text() != expected):
            raise RuntimeError(f'watched fixture content differs: {name}')
        result[name] = None if expected is None else {'sha256': digest(path), 'mode': path.stat().st_mode & 0o777}
    return result


def input_identity(binary, bash, source, directory):
    files = [SPEC, Path(__file__), ROOT / 'scripts/bench_ecosystem.py',
             ROOT / 'scripts/bench_interactive.py', ROOT / 'scripts/bench_lifecycle.py',
             Path(sys.executable), Path(bash), binary, source, Path('/usr/bin/git')]
    for name in ('gh', 'cmp', 'sort', 'grep', 'head', 'printenv', 'stat'):
        resolved = shutil.which(name, path='/usr/bin:/bin')
        if resolved:
            files.append(Path(resolved))
    result = {'files': {str(path.resolve()): digest(path) for path in files}, 'fixture': {}}
    result['gh_available'] = shutil.which('gh', path='/usr/bin:/bin') is not None
    for name in ('plugin', 'state', 'repository'):
        if (directory / name).exists():
            result['fixture'][name] = inventory(directory / name)
    for name in ('config.toml', 'rc', 'aliases-golden', 'functions-golden', 'report-golden', 'effects-golden'):
        if (directory / name).exists():
            result['fixture'][name] = digest(directory / name)
    return result


def comparison_signature(case, binary, bash, source, directory):
    """Permit variant roots and Binit implementation differences, nothing else."""
    common = input_identity(binary, bash, source, directory)
    common['files'].pop(str(binary.resolve()))
    files = {}
    paths = [directory / 'config.toml', directory / 'rc']
    paths.extend(path for path in (directory / 'plugin').rglob('*') if path.is_file())
    paths.extend(directory / name for name in ('aliases-golden', 'functions-golden', 'report-golden', 'effects-golden')
                 if (directory / name).exists())
    if (directory / 'repository').exists():
        paths.extend(path for path in (directory / 'repository').rglob('*') if path.is_file())
    for path in sorted(paths):
        files[str(path.relative_to(directory))] = {
            'sha256': hashlib.sha256(path.read_bytes().replace(str(directory).encode(), b'<fixture>')).hexdigest(),
            'mode': path.stat().st_mode & 0o777}
    return {'case': case, 'common_files': common['files'], 'gh_available': common['gh_available'],
            'native_module_present': any((directory / 'state').rglob('completion.so')),
            'configuration_and_golden': files,
            'watched_initial': watched_inputs(directory) if case == CASES[1] else None}


def verify_retained_inputs(rows):
    for row in rows:
        context = row['context']
        directory = Path(context['directory'])
        current = input_identity(Path(context['binary']), context['bash'], Path(context['source']), directory)
        if current != row['inputs']:
            raise RuntimeError('retained workflow inputs changed at run boundary')
        if row['case'] == CASES[1] and watched_inputs(directory, final=True) != row['final_watched_inputs']:
            raise RuntimeError('retained watched fixture changed at run boundary')


def observe(case, binary, bash, source, directory, fault=None, resources=False):
    config, rc = make_fixture(case, directory, source, fault)
    env = {**environment(directory / 'home'), 'BINIT_BASH': bash}
    prepare(binary, config, directory / 'state', env)
    if case == CASES[0]:
        golden_git(directory, rc, source, bash)
    else:
        golden_watch(directory, source, bash)
        initial_watched = watched_inputs(directory)
    identity = input_identity(binary, bash, source, directory)
    shell = BoundedShell(bash, rc, directory, env)
    row = {'case': case, 'inputs': identity, 'commands_ms': [], 'effects_passed': False,
           'context': {'directory': str(directory), 'binary': str(binary), 'bash': bash, 'source': str(source)},
           'comparison_signature': comparison_signature(case, binary, bash, source, directory)}
    automatic_gc = gc.isenabled()
    gc.disable()
    try:
        native_expected = row['comparison_signature']['native_module_present']
        native_probe = ('[[ $(builtin binit_complete status 2>/dev/null) == "Completion menu: on" ]]'
                        if native_expected else '! builtin binit_complete status >/dev/null 2>&1')
        action(shell, f'eval "$({shlex.quote(str(binary))} init --state {shlex.quote(str(directory / "state"))})" && {native_probe}')
        row['native_active'] = native_expected
        action(shell, 'WF_FLAGS=$-; wf_state > "$WF_ROOT/state-before"')
        if case == CASES[1]:
            action(shell, 'PROMPT_COMMAND+=(wf_prompt)')
            row['initial_watched_inputs'] = initial_watched
        if fault == 'changed-identity':
            rc.write_text(rc.read_text() + '# changed input\n')
        if input_identity(binary, bash, source, directory) != identity:
            raise RuntimeError('frozen workflow input changed before timing')
        if case == CASES[1] and watched_inputs(directory) != initial_watched:
            raise RuntimeError('watched inputs changed during initialization')
        if fault == 'no-watch':
            action(shell, '__binit_dispatch() { :; }')
        started = time.perf_counter_ns()
        if case == CASES[0]:
            row['commands_ms'].append(action(shell, ':' if fault == 'skip-load' else 'binit load workflow'))
            # This must be a later accepted command: Bash expands aliases at parse time.
            row['commands_ms'].append(action(shell, 'cd -- "$WF_ROOT/repository" && wf_git_check && '
                'binit report workflow | LC_ALL=C sort > "$WF_ROOT/report-actual" && '
                'cmp -s -- "$WF_ROOT/report-golden" "$WF_ROOT/report-actual"'))
        else:
            for number in range(1, 21):
                row['commands_ms'].append(action(shell, f'wf_command {number}', prompt=number))
        row['elapsed_ms'] = (time.perf_counter_ns() - started) / 1e6
        if resources:
            row['resources_at_readiness'] = resource_sample(shell.pid)
        if case == CASES[0]:
            action(shell, 'wf_git_check')
            # Changed owned definitions block unload; untracked punctuation aliases survive.
            action(shell, "alias g=': replacement'; if binit unload workflow; then false; else true; fi")
            action(shell, "[[ $(alias g) == \"alias g=': replacement'\" ]] && "
                          "alias g=git && binit unload workflow && ! declare -F gdv && "
                          "! alias g >/dev/null 2>&1 && alias 'gc!' >/dev/null && wf_preserved")
        else:
            action(shell, '[[ $WF_COMMANDS == 20 && $WF_SOURCES == 1 && $WF_CALLS == 1 ]] && wf_preserved && '
                   'wf_watch_effects > "$WF_ROOT/effects-actual" && '
                   'cmp -s -- "$WF_ROOT/effects-golden" "$WF_ROOT/effects-actual"')
            row['final_watched_inputs'] = watched_inputs(directory, final=True)
        row['effects_passed'] = True
        if input_identity(binary, bash, source, directory) != identity:
            raise RuntimeError('frozen workflow input changed after timing')
        return row
    finally:
        try:
            shell.close()
        finally:
            (directory / 'terminal.raw').write_bytes(shell.raw)
            (directory / 'observation.json').write_text(json.dumps(row, indent=2) + '\n')
            gc.collect()
            if automatic_gc:
                gc.enable()


def assess(rows, runs, pairs):
    expected = {(case, run, pair, variant) for case in CASES for run in range(runs)
                for pair in range(pairs) for variant in ('candidate', 'reference')}
    seen = set()
    for row in rows:
        key = (row['case'], row['run'], row['pair'], row['variant'])
        elapsed = row['elapsed_ms']
        if key not in expected or key in seen or not row['effects_passed']:
            raise RuntimeError('unexpected, duplicate or failed effect row')
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed <= 0:
            raise RuntimeError('nonpositive or nonfinite elapsed time')
        seen.add(key)
    if seen != expected:
        raise RuntimeError('incomplete paired effect evidence')
    output = {}
    for case in CASES:
        output[case] = []
        for run in range(runs):
            subset = [row for row in rows if row['run'] == run and row['case'] == case]
            ratios = []
            for pair in range(pairs):
                values = {row['variant']: row for row in subset if row['pair'] == pair}
                if set(values) != {'candidate', 'reference'} or not all(r['effects_passed'] for r in values.values()):
                    raise RuntimeError('incomplete paired effect evidence')
                if values['candidate']['comparison_signature'] != values['reference']['comparison_signature']:
                    raise RuntimeError('paired workflow fixture inputs differ')
                ratios.append(values['candidate']['elapsed_ms'] / values['reference']['elapsed_ms'])
            median = statistics.median(ratios)
            output[case].append({'paired_ratios': ratios, 'median_paired_ratio': median,
                                 'target_pass': median <= .75,
                                 **{v: summarize([row['elapsed_ms'] for row in subset if row['variant'] == v])
                                    for v in ('candidate', 'reference')},
                                 'commands_by_index': {v: [summarize([row['commands_ms'][index] for row in subset
                                    if row['variant'] == v]) for index in range(len(subset[0]['commands_ms']))]
                                    for v in ('candidate', 'reference')}})
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--bash', default='/bin/bash')
    parser.add_argument('--git-source', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--runs', type=int, default=2)
    parser.add_argument('--pairs', type=int, default=20)
    parser.add_argument('--negative-control', choices=FAULTS)
    parser.add_argument('--resources', action='store_true', help='separate diagnostic observations; never qualification')
    args = parser.parse_args()
    args.work = args.work.resolve()
    args.output = args.output.resolve()
    if not args.work.is_relative_to(ROOT / 'tmp') or not args.output.is_relative_to(ROOT / 'tmp'):
        parser.error('work and raw receipts must be under project tmp/')
    if args.work.exists() or args.output.exists() or args.runs < 1 or args.pairs < 1:
        parser.error('use fresh work/output and positive runs/pairs; retain all experiments')
    args.work.mkdir(parents=True)
    (args.work / 'driver.py').write_bytes(Path(__file__).read_bytes())
    result = {'schema': 1, 'rows': [], 'complete': False, 'qualification': False,
              'negative_control': args.negative_control, 'resources_separate': args.resources}
    try:
        spec = json.loads(SPEC.read_text())
        if digest(args.git_source) != spec['cases'][0]['source']['sha256']:
            raise RuntimeError('Git source differs from frozen pin')
        binaries = {'candidate': args.candidate.resolve(), 'reference': args.reference.resolve()}
        result['invocation'] = vars(args) | {'bash_version': subprocess.check_output([args.bash, '--version'], text=True).splitlines()[0]}
        result['initial_files'] = input_identity(binaries['candidate'], args.bash, args.git_source, args.work)['files']
        result['initial_files'][str(binaries['reference'])] = digest(binaries['reference'])
        for run in range(args.runs):
            for pair in range(args.pairs):
                for case in CASES:
                    for variant in (('reference', 'candidate') if (run + pair) % 2 else ('candidate', 'reference')):
                        result['active_case'] = [run, pair, case, variant]
                        directory = args.work / f'{run}-{pair}-{case}-{variant}'
                        row = observe(case, binaries[variant], args.bash, args.git_source.resolve(), directory,
                                      args.negative_control if variant == 'candidate' else None, args.resources)
                        result['rows'].append({'run': run, 'pair': pair, 'variant': variant, **row})
                    same_pair = [row for row in result['rows'] if row['run'] == run and row['pair'] == pair and row['case'] == case]
                    if same_pair[0]['comparison_signature'] != same_pair[1]['comparison_signature']:
                        raise RuntimeError('paired workflow fixture inputs differ')
                for path, expected in result['initial_files'].items():
                    if digest(Path(path)) != expected:
                        raise RuntimeError('frozen workflow input changed between pairs')
                print(f'run {run + 1} pair {pair + 1}/{args.pairs}: all workflow effects passed', flush=True)
            verify_retained_inputs(result['rows'])
        result['assessment'] = assess(result['rows'], args.runs, args.pairs)
        result['complete'] = True
        result['primary_environment_eligible'] = ('version 5.3.' in result['invocation']['bash_version']
                                                 and all(row['native_active'] for row in result['rows']))
        result['workload_target_pass'] = (args.runs == 2 and args.pairs == 20 and not args.negative_control
            and not args.resources and result['primary_environment_eligible']
            and digest(args.reference) == spec['reference_binary_sha256']
            and all(item['target_pass'] for values in result['assessment'].values() for item in values))
        result.pop('active_case', None)
    except BaseException as error:
        result['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x') as stream:
            json.dump(result, stream, indent=2, default=str)
            stream.write('\n')


if __name__ == '__main__':
    main()
