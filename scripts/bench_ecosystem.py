#!/usr/bin/env python3
"""Exercise pinned framework loaders with identical portable plugin effects."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import termios
import time

from bench_interactive import Shell, digest, summarize

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / 'benchmarks/ecosystem.json'
FIXTURE = ROOT / 'benchmarks/fixtures/ecosystem/framework-plugin.sh'


class BoundedShell(Shell):
    def read(self, timeout):
        result = super().read(timeout)
        if len(self.raw) > 8 * 1024 * 1024:
            raise RuntimeError('terminal exceeded frozen 8 MiB output limit')
        return result


def verify_sources(base, spec):
    """Bind every upstream file to the acquired archive, including link targets."""
    receipts = {}
    for name, source in spec['sources'].items():
        archive = base / f'{name}.tar.gz'
        if digest(archive) != source['archive_sha256']:
            raise RuntimeError(f'{name}: archive hash differs from frozen source')
        tree = base / 'sources' / name
        expected = {}
        with tarfile.open(archive) as bundle:
            for member in bundle:
                parts = Path(member.name).parts[1:]
                if not parts or member.isdir():
                    continue
                if member.name.startswith('/') or '..' in parts:
                    raise RuntimeError(f'{name}: invalid archive member')
                path = tree.joinpath(*parts)
                if member.issym():
                    valid = path.is_symlink() and os.readlink(path) == member.linkname
                    record = {'link': member.linkname}
                elif member.isfile():
                    content_hash = hashlib.sha256(bundle.extractfile(member).read()).hexdigest()
                    record = {'sha256': content_hash, 'executable': member.mode & 0o111}
                    valid = (not path.is_symlink() and path.is_file()
                             and digest(path) == content_hash
                             and path.stat().st_mode & 0o111 == member.mode & 0o111)
                else:
                    valid = False
                if not valid:
                    raise RuntimeError(f'{name}: source differs at {"/".join(parts)}')
                expected['/'.join(parts)] = record
        actual = inventory(tree)
        derivatives = {}
        if name == 'ble':
            # Upstream's build consumes the separately pinned contrib submodule.
            contrib = inventory(base / 'sources/ble-contrib')
            expected.update({f'contrib/{p}': value for p, value in contrib.items()})
            derivatives = {p: value for p, value in actual.items() if p.startswith('out/')}
        if actual != {**expected, **derivatives}:
            raise RuntimeError(f'{name}: source inventory differs (extra, missing or changed entries)')
        receipts[name] = {**source, 'verified_files': len(expected), 'build_outputs': derivatives}
    return receipts


def inventory(root):
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in list(dirs):
            if (parent / name).is_symlink():
                dirs.remove(name)
                files.append(name)
        for name in sorted(files):
            path = parent / name
            key = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[key] = {'link': os.readlink(path)}
            elif path.is_file():
                result[key] = {'sha256': digest(path), 'executable': path.stat().st_mode & 0o111}
            else:
                raise RuntimeError(f'non-regular fixture input: {key}')
    return result


def environment(home):
    # Construct the environment rather than inheriting shell hooks or user config.
    return {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'TERM': 'xterm-256color',
            'LC_ALL': 'C.UTF-8', 'INPUTRC': '/dev/null', 'SHELL': '/bin/bash',
            'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_DATA_HOME': str(home / 'data'),
            'XDG_CACHE_HOME': str(home / 'cache'), 'ZDOTDIR': str(home),
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}


def effect_check(count, target):
    order = ''.join(f'p{i:02d};' for i in range(count))
    code = 'corpus_verify() {\n'
    code += f'[[ ${{CORPUS_LOAD_ORDER-}} == {shlex.quote(order)} ]] || return 21\n'
    if count:
        code += 'corpus_origin=$PWD\ncorpus_exports="\n$(command env)\n"\n'
    for i in range(count):
        key = f'p{i:02d}'
        code += (f'typeset -f corpus_fn_{key} >/dev/null || return 22\n'
                 f'eval corpus_alias_{key} || return 23\n'
                 f'[[ $CORPUS_EXPORT_{key} == ready ]] || return 24\n')
        exported = shlex.quote(f'\nCORPUS_EXPORT_{key}=ready\n')
        code += f'[[ $corpus_exports == *{exported}* ]] || return 24\n'
        arguments = ['two words', '', '*', "a'b", '$HOME', 'back\\slash']
        expected = ''.join(f'<{arg}>' for arg in arguments)
        code += (f'CORPUS_TARGET={shlex.quote(str(target))}\n'
                 'cd -- "$corpus_origin" || return 29\n'
                 'CORPUS_CALLS=0\n'
                 f'corpus_fn_{key} ' + ' '.join(map(shlex.quote, arguments)) + '\n'
                 'corpus_rc=$?\n'
                 '[[ $corpus_rc == 37 && $CORPUS_ARGC == 6 && $CORPUS_CALLS == 1 ]] || return 25\n'
                 f'[[ $CORPUS_ARGS == {shlex.quote(expected)} ]] || return 26\n'
                 '[[ $PWD == "$CORPUS_TARGET" ]] || return 27\n'
                 f'corpus_fn_{key} warm\n'
                 'corpus_rc=$?\n'
                 '[[ $corpus_rc == 37 && $CORPUS_ARGC == 1 && $CORPUS_CALLS == 2 && $CORPUS_ARGS == "<warm>" ]] || return 28\n')
    return code + 'return 0\n}\n'


def prompt_hook(zsh):
    code = ('_corpus_prompt() { case $PS1 in *"BENCH> ") ;; *) PS1="${PS1}BENCH> ";; esac; }\n')
    return code + ('precmd_functions+=(_corpus_prompt)\n' if zsh else
                   'PROMPT_COMMAND+=(_corpus_prompt)\n')


def configuration(subject, count, directory, sources, binary, bash):
    directory.mkdir()
    plugins = directory / 'plugins'
    plugins.mkdir()
    target = directory / 'target with spaces'
    target.mkdir()
    template = FIXTURE.read_text()
    entries = []
    for i in range(count):
        key = f'p{i:02d}'
        plugin = plugins / key
        plugin.mkdir()
        body = template.replace('@@ID@@', key)
        # The labelled wait row defers the same plugins until after the first prompt is drawn.
        load = 'wait' if subject == 'binit-wait' else 'startup'
        (plugin / 'plugin.toml').write_text(f'schema=1\nid="{key}"\nentry="{key}.plugin.bash"\nload="{load}"\n')
        entry = plugin / f'{key}.plugin.bash'
        entry.write_text(body)
        # The loaders use different entry-file suffixes; the script bytes match.
        if subject == 'zinit':
            entry.rename(plugin / f'{key}.plugin.zsh')
        entries.append(entry)
    zsh = subject in ('zsh', 'zinit')
    setup = 'HISTFILE=/dev/null\nPS1=""\n' + ('bindkey -e\n' if zsh else 'set -o emacs\n')
    preparation = None
    if subject in ('bash', 'zsh'):
        setup += ''.join(f'source {shlex.quote(str(p))}\n' for p in entries)
    elif subject in ('binit', 'binit-wait'):
        config = directory / 'config.toml'
        config.write_text('schema=1\n' + ('plugins=[]\n' if not count else '') + ''.join(
            f'\n[[plugins]]\npath="plugins/p{i:02d}"\n' for i in range(count)))
        state = directory / 'state'
        home = directory / 'prepare-home'
        home.mkdir()
        started = time.perf_counter_ns()
        try:
            proc = subprocess.run([str(binary), 'prepare', str(config), '--state', str(state)],
                                  env={**environment(home), 'BINIT_BASH': bash}, capture_output=True, timeout=30)
        except subprocess.TimeoutExpired as error:
            (directory / 'preparation.stdout').write_bytes(error.stdout or b'')
            (directory / 'preparation.stderr').write_bytes(error.stderr or b'')
            raise
        (directory / 'preparation.stdout').write_bytes(proc.stdout)
        (directory / 'preparation.stderr').write_bytes(proc.stderr)
        if proc.returncode:
            raise RuntimeError(proc.stderr.decode(errors='replace'))
        preparation = (time.perf_counter_ns() - started) / 1e6
        # `binit init` stamps a generation whose files settled at least 2 s earlier, and later
        # starts compare the stamp instead of hashing. Record it now, outside timing and before the
        # inputs are frozen, so every timed shell starts from the same primed state.
        time.sleep(2.1)
        primed = subprocess.run([str(binary), 'init', '--state', str(state)],
                                env={**environment(home), 'BINIT_BASH': bash}, capture_output=True, timeout=30)
        if primed.returncode:
            raise RuntimeError(primed.stderr.decode(errors='replace'))
        # `init` succeeds without a stamp when it cannot write one, so require the file itself.
        generation = (state / 'current').read_text().strip()
        if not (state / 'generations' / f'{generation}.verified').is_file():
            raise RuntimeError(f'{directory.name}: init stamp was not recorded before timing')
        setup += f'eval "$({shlex.quote(str(binary))} init --state {shlex.quote(str(state))})"\n'
    elif subject == 'bash-it':
        custom = directory / 'custom'
        custom.mkdir()
        for i, entry in enumerate(entries):
            (custom / f'p{i:02d}.bash').symlink_to(entry)
        setup += (f'BASH_IT={shlex.quote(str(sources / "bash-it"))}\n'
                  f'BASH_IT_CUSTOM={shlex.quote(str(custom))}\nBASH_IT_THEME=""\n'
                  'source "$BASH_IT/bash_it.sh"\n')
    elif subject == 'oh-my-bash':
        setup += (f'OSH={shlex.quote(str(sources / "oh-my-bash"))}\n'
                  f'OSH_CUSTOM={shlex.quote(str(directory))}\nOSH_THEME=""\n'
                  'DISABLE_AUTO_UPDATE=true\naliases=()\ncompletions=()\n'
                  'plugins=(' + ' '.join(f'p{i:02d}' for i in range(count)) + ')\n'
                  'source "$OSH/oh-my-bash.sh"\n')
    elif subject == 'zinit':
        setup += ('typeset -gA ZINIT\nZINIT[HOME_DIR]="$HOME/zinit"\n'
                  f'source {shlex.quote(str(sources / "zinit" / "zinit.zsh"))}\n')
        setup += ''.join(f'zinit load {shlex.quote(str(plugins / f"p{i:02d}"))}\n' for i in range(count))
    else:
        raise ValueError(subject)
    setup += effect_check(count, target) + prompt_hook(zsh)
    rc = directory / 'rc'
    rc.write_text(setup)
    return rc, preparation


def resource_sample(pid, proc=Path('/proc')):
    """Read separate, non-atomic observations of our child and its descendants."""
    pending, rows, seen, vanished = [pid], [], set(), []
    page_size = os.sysconf('SC_PAGE_SIZE')
    while pending:
        child = pending.pop()
        if child in seen:
            continue
        seen.add(child)
        base = proc / str(child)
        row = {'pid': child}
        try:
            status = (base / 'status').read_text()
            row['state'] = re.search(r'^State:\s+(\w)', status, re.M)[1]
            fields = (base / 'statm').read_text().split()
            if len(fields) != 7 or not all(value.isascii() and value.isdigit() for value in fields):
                raise RuntimeError(f'malformed statm for owned process {child}')
            row['resident_pages'] = int(fields[1])
            row['rss_kib'] = row['resident_pages'] * page_size // 1024
            row['fds'] = len(list((base / 'fd').iterdir()))
            pending.extend(map(int, (base / f'task/{child}/children').read_text().split()))
            rows.append(row)
        except (FileNotFoundError, ProcessLookupError):
            if child == pid:
                raise RuntimeError('owned shell disappeared during resource observation')
            vanished.append(row)
            continue
    return {'processes': len(rows), 'zombies': sum(r['state'] == 'Z' for r in rows),
            'discovered_processes': len(seen), 'vanished_processes': len(vanished),
            'partial': bool(vanished), 'vanished_samples': vanished,
            'rss_kind': 'approximate kernel statm RSS; shared mappings counted per process',
            'page_size_bytes': page_size,
            'rss_kib_sum': sum(r['rss_kib'] for r in rows),
            'fds_sum': sum(r['fds'] for r in rows), 'samples': rows}


def observe(subject, interpreter, rc, work, home):
    home.mkdir()
    env = environment(home)
    env['BINIT_BASH'] = interpreter
    env['SHELL'] = interpreter
    if subject in ('zsh', 'zinit'):
        shutil.copyfile(rc, home / '.zshrc')
        # -d suppresses global startup files; -f would suppress our own .zshrc.
        command = [interpreter, '-d', '-i']
    else:
        command = None
    started = time.perf_counter_ns()
    shell = BoundedShell(interpreter, rc, work, env, command)
    try:
        startup = shell.startup_ms
        wait_for_editor(shell)
        ack_started = time.perf_counter_ns()
        shell.settled(b':')
        echo_quiet = (time.perf_counter_ns() - ack_started) / 1e6
        # Complete the already-typed ':' and require execution plus a new prompt.
        # A rendered colon alone did not establish accepted input in Zsh probes.
        shell.command('')
        acknowledgment = (time.perf_counter_ns() - ack_started) / 1e6
        usable = (time.perf_counter_ns() - started) / 1e6
        offset = len(shell.raw)
        ready = shell.command('corpus_verify; corpus_status=$?; printf "\\036CORPUS:%s\\036" "$corpus_status"')
        statuses = re.findall(rb'\x1eCORPUS:(\d+)\x1e', shell.raw[offset:])
        if statuses != [b'0']:
            raise RuntimeError(f'{subject}: effects failed: {statuses!r}\n' + '\n'.join(shell.screen.display))
        return {'startup_visible_ms': startup, 'first_input_ack_ms': acknowledgment,
                'input_echo_quiet_ms': echo_quiet,
                'startup_usable_ms': usable, 'effect_probe_ms': ready,
                'required_effects_ms': (time.perf_counter_ns() - started) / 1e6,
                'resources_at_readiness': resource_sample(shell.pid),
                'effect_status': 0, 'terminal_bytes': len(shell.raw)}
    except BaseException as error:
        (home / 'terminal.raw').write_bytes(shell.raw)
        raise RuntimeError(f'{subject}: {error}\n' + '\n'.join(shell.screen.display)) from error
    finally:
        shell.close()


def wait_for_editor(shell, seconds=10):
    # Zsh emits PS1 before entering ZLE: cooked tty echo is not an editor ACK.
    deadline = time.monotonic() + seconds
    while termios.tcgetattr(shell.fd)[3] & termios.ICANON:
        if time.monotonic() >= deadline:
            raise RuntimeError('visible prompt never reached editor input mode')
        shell.read(.001)


def input_identity(args, scenarios):
    files = [SPEC, FIXTURE, Path(__file__), ROOT / 'scripts/bench_interactive.py',
             args.binary, Path(args.bash), Path(args.zsh), Path(sys.executable)]
    result = {'files': {str(path.resolve()): digest(path) for path in files}, 'configurations': {}}
    for key, (_, rc, directory) in scenarios.items():
        entries = {'rc': {'sha256': digest(rc)}}
        entries.update({f'plugins/{p}': value for p, value in inventory(directory / 'plugins').items()})
        if (directory / 'state').exists():
            entries.update({f'state/{p}': value for p, value in inventory(directory / 'state').items()})
            entries['config.toml'] = {'sha256': digest(directory / 'config.toml')}
        if (directory / 'custom').exists():
            entries.update({f'custom/{p}': value for p, value in inventory(directory / 'custom').items()})
        result['configurations'][key] = entries
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, required=True, help='directory holding pinned archives and sources/')
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--bash', default='/bin/bash')
    parser.add_argument('--zsh', default='/bin/zsh')
    parser.add_argument('--work', type=Path, required=True, help='fresh directory under project tmp/')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pairs', type=int, default=20)
    parser.add_argument('--runs', type=int, default=2)
    parser.add_argument('--subjects', nargs='+', default=None)
    parser.add_argument('--counts', nargs='+', type=int, default=None)
    args = parser.parse_args()
    if args.output.exists() or args.work.exists():
        parser.error('work and output must be fresh; preserve earlier experiments')
    if args.pairs < 1 or args.runs < 1:
        parser.error('runs and pairs must be positive')
    if not args.work.resolve().is_relative_to(ROOT / 'tmp'):
        parser.error('work must be under project tmp/')
    spec = json.loads(SPEC.read_text())
    subjects = args.subjects or spec['framework']['subjects']
    counts = args.counts or spec['framework']['plugin_counts']
    if any(s not in spec['framework']['subjects'] for s in subjects) or any(n not in spec['framework']['plugin_counts'] for n in counts):
        parser.error('subjects and counts must come from the frozen corpus')
    sources = args.sources.resolve()
    result = {'schema': 1, 'phase': 'framework', 'spec_sha256': digest(SPEC),
              'fixture_sha256': digest(FIXTURE), 'driver_sha256': digest(Path(__file__)),
              'terminal_driver_sha256': digest(ROOT / 'scripts/bench_interactive.py'),
              'binit_sha256': digest(args.binary), 'bash_sha256': digest(args.bash),
              'zsh_sha256': digest(args.zsh), 'observations': spec['observation'],
              'runs': [], 'complete_two_run_framework_corpus': False}
    args.work.mkdir(parents=True)
    binary = args.binary.resolve()
    scenarios = {}
    try:
        result['sources'] = verify_sources(sources, spec)
        for count in counts:
            for subject in subjects:
                key = f'{subject}-{count}'
                result['active_case'] = key
                directory = args.work.resolve() / key
                rc, preparation = configuration(subject, count, directory, sources / 'sources', binary, args.bash)
                scenarios[key] = (subject, rc, directory)
                result.setdefault('configurations', {})[key] = {
                    'rc_sha256': digest(rc), 'plugin_count': count, 'subject': subject,
                    'preparation_ms': preparation,
                    'shell': 'zsh' if subject in ('zsh', 'zinit') else 'bash'}
        result['inputs'] = input_identity(args, scenarios)
        result['interpreter_versions'] = {
            name: subprocess.run([path, '--version'], capture_output=True, text=True, check=True).stdout.splitlines()[0]
            for name, path in [('bash', args.bash), ('zsh', args.zsh)]}
        for run in range(args.runs):
            samples = []
            result['runs'].append(samples)
            for pair in range(args.pairs):
                if input_identity(args, scenarios) != result['inputs']:
                    raise RuntimeError('frozen benchmark inputs changed before pair')
                keys = list(scenarios)[::1 if (run + pair) % 2 == 0 else -1]
                for key in keys:
                    result['active_case'] = key
                    subject, rc, directory = scenarios[key]
                    interpreter = args.zsh if subject in ('zsh', 'zinit') else args.bash
                    observation = observe(subject, interpreter, rc, directory,
                                          directory / f'home-{run}-{pair}')
                    samples.append({'case': key, 'pair': pair, **observation})
                print(f'run {run + 1} pair {pair + 1}/{args.pairs}: effects passed', flush=True)
            if input_identity(args, scenarios) != result['inputs']:
                raise RuntimeError('frozen benchmark inputs changed during run')
        result['sources_after'] = verify_sources(sources, spec)
        if result['sources_after'] != result['sources']:
            raise RuntimeError('upstream build outputs changed during run')
        result['complete_two_run_framework_corpus'] = (
            args.runs == 2 and args.pairs == 20
            and subjects == spec['framework']['subjects']
            and counts == spec['framework']['plugin_counts'])
        result['summary_by_run'] = [{key: {
            metric: summarize([row[metric] for row in run if row['case'] == key])
            for metric in ('startup_visible_ms', 'startup_usable_ms', 'effect_probe_ms', 'required_effects_ms')
        } for key in scenarios} for run in result['runs']]
        result.pop('active_case', None)
    except BaseException as error:
        result['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
