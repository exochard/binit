#!/usr/bin/env python3
"""Compare same-interpreter Bash and Binit in an emulated interactive terminal."""
import argparse
import codecs
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import pty
import re
import select
import shlex
import signal
import statistics
import struct
import subprocess
import tempfile
import termios
import time

import pyte

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ('git-prompt', 'git-completion', 'project-context', 'directory-jump',
           'history-search', 'syntax-highlighting', 'autosuggestions')
PROMPT = b'BENCH> '

# A comparison runs the candidate and the frozen reference, which draw different chrome for the
# same state; each predicate accepts exactly the two presentations of its own state.
CARET = '\u258f'


def live_history_shown(display):
    """The live history list under the input, before it takes focus."""
    return any('History  Tab focus' in row or row.rstrip() == 'history' for row in display)


def history_search_shown(display):
    """The history panel with a search query being typed."""
    return any('History SEARCH' in row or (row.startswith('history  ') and CARET in row)
               for row in display)


def completion_shown(display, word):
    """The completion menu for `word`, never the history panel."""
    state = re.compile(re.escape(word) + r'(?: /\S*)?  \d+/\d+(?:   \?)?')
    return any('Completions' in row or state.fullmatch(row.rstrip()) for row in display)


class Shell:
    def __init__(self, bash, rc, work, environment, command=None):
        self.screen = pyte.HistoryScreen(120, 32, history=200)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.raw = bytearray()
        started = time.perf_counter_ns()
        self.pid, self.fd = pty.fork()
        if not self.pid:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 120, 0, 0))
            os.chdir(work)
            os.execvpe(command[0] if command else bash, command or
                       [bash, '--noprofile', '--rcfile', str(rc), '-i'], environment)
        self.screen.write_process_input = lambda data: os.write(self.fd, data.encode())
        try:
            self.until(lambda: PROMPT in self.raw)
        except BaseException:
            self.close()
            raise
        self.startup_ms = (time.perf_counter_ns() - started) / 1e6

    def read(self, timeout):
        if not select.select([self.fd], [], [], timeout)[0]:
            return False
        data = os.read(self.fd, 65536)
        if not data:
            raise RuntimeError('shell closed before completing the benchmark')
        self.raw.extend(data)
        self.stream.feed(self.decoder.decode(data))
        return True

    def until(self, predicate, seconds=10):
        deadline = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() >= deadline:
                raise RuntimeError('terminal timed out:\n' + '\n'.join(self.screen.display))
            self.read(.01)

    def settled(self, data):
        """Input to last terminal output before a 2 ms quiet interval."""
        started = time.perf_counter_ns()
        os.write(self.fd, data)
        if not self.read(2):
            raise RuntimeError('input produced no terminal response')
        last = time.perf_counter_ns()
        while self.read(.002):
            last = time.perf_counter_ns()
            if last - started > 2_000_000_000:
                raise RuntimeError('terminal did not settle')
        return (last - started) / 1e6

    def command(self, code):
        start = len(self.raw)
        started = time.perf_counter_ns()
        os.write(self.fd, code.encode() + b'; printf "\\036DONE\\036"\n')
        self.until(lambda: b'\x1eDONE\x1e' in self.raw[start:]
                   and self.raw[start:].rfind(PROMPT) > self.raw[start:].find(b'\x1eDONE\x1e'))
        return (time.perf_counter_ns() - started) / 1e6

    def cancel(self):
        self.settled(b'\x03')

    def close(self):
        try:
            try:
                os.write(self.fd, b'\x03')
                self.read(.05)
                os.write(self.fd, b'exit 0\n')
            except (OSError, RuntimeError):
                pass
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                done, _ = os.waitpid(self.pid, os.WNOHANG)
                if done:
                    return
                try:
                    self.read(.01)
                except (OSError, RuntimeError):
                    pass
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        finally:
            os.close(self.fd)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def summarize(values):
    return {'median': round(statistics.median(values), 3),
            'p95': round(sorted(values)[math.ceil(len(values) * .95) - 1], 3)}


def profile(shell):
    start = len(shell.raw)
    shell.command('binit profile')
    text = bytes(shell.raw[start:]).decode(errors='replace').replace('\r', '')
    rows = re.findall(r'^([a-z-]+)\s+(\w+)\s+(\d+)\s+(\d+)\s+(\d+)\s', text, re.M)
    return {name: {'state': state, 'load_us': int(load), 'hook_us': int(hook), 'calls': int(calls)}
            for name, state, load, hook, calls in rows}


def benchmark(args, work):
    home = work / 'home'
    home.mkdir()
    history = ''.join(f'git status --short # fixture-{i:04d}\n' for i in range(1000))
    (home / '.bash_history').write_text(history)
    repo = work / 'repo'
    repo.mkdir()
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('BASH_ENV', 'ENV', 'BINIT_SAFE', 'BINIT_TEST_BINARY')}
    environment.update(HOME=str(home), INPUTRC='/dev/null', TERM='xterm-256color',
                       LC_ALL='C.UTF-8', BINIT_COMPLETION_MENU='1',
                       GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
    subprocess.run(['git', 'init', '-q', '--initial-branch=main', str(repo)],
                   env=environment, check=True)
    (repo / 'Cargo.toml').write_text('[package]\nname="fixture"\nversion="0.1.0"\n')
    for i in range(30):
        (repo / f'alpha-{i:02d}.txt').touch()
    binary = args.binary.resolve()
    scenarios = {}
    plugin_receipts = {}
    configurations = [('bare_bash', (), binary), ('binit_empty', (), binary),
                      ('binit_seven_plugins', PLUGINS, binary)]
    if args.compare_binary:
        configurations.append(('binit_seven_before', PLUGINS, args.compare_binary.resolve()))
    for name, ids, manager in configurations:
        rc = work / f'{name}.bash'
        setup = "HISTSIZE=1000\nHISTFILE=/dev/null\nset -o vi\nPS1='BENCH> '\n"
        setup += 'history -r ' + shlex.quote(str(home / '.bash_history')) + '\n'
        if name != 'bare_bash':
            config = work / f'{name}.toml'
            config.write_text('schema=1\n' + ('plugins=[]\n' if not ids else '') + ''.join('\n[[plugins]]\npath=' + json.dumps(
                str(args.plugin_root.resolve() / identifier)) + '\n' for identifier in ids))
            state = work / name
            prepared = subprocess.run([str(manager), 'prepare', str(config), '--state', str(state)],
                                      capture_output=True, text=True, env=environment)
            if prepared.returncode:
                raise RuntimeError(prepared.stderr)
            setup += 'eval "$(' + shlex.quote(str(manager)) + ' init --state ' + shlex.quote(str(state)) + ')"\n'
        rc.write_text(setup)
        scenarios[name] = rc
    for identifier in PLUGINS:
        source = args.plugin_root / identifier
        plugin_receipts[identifier] = {name: digest(source / name) for name in ('plugin.toml', 'plugin.bash')}
    samples = {name: {metric: [] for metric in (
        'startup_visible', 'startup_usable', 'typing', 'typing_miss', 'first_history', 'builtin_loop',
        'external_commands', 'first_file_completion')} for name in scenarios}
    samples['binit_seven_plugins']['first_git_completion'] = []
    samples['binit_seven_plugins']['warm_git_completion'] = []
    if args.compare_binary:
        samples['binit_seven_before']['first_git_completion'] = []
        samples['binit_seven_before']['warm_git_completion'] = []
    profiles = []
    for index in range(args.samples):
        for name in list(scenarios)[::1 if index % 2 == 0 else -1]:
            seven = name.startswith('binit_seven_')
            shell = Shell(args.bash, scenarios[name], repo, environment)
            timings = samples[name]
            try:
                timings['startup_visible'].append(shell.startup_ms)
                timings['startup_usable'].append(shell.startup_ms + shell.settled(b':'))
                shell.cancel()
                for character in b'git status ':
                    timings['typing'].append(shell.settled(bytes([character])))
                if seven and not live_history_shown(shell.screen.display):
                    raise RuntimeError('live history was not active during typing')
                shell.cancel()
                for character in b'no-history-match':
                    timings['typing_miss'].append(shell.settled(bytes([character])))
                shell.cancel()
                timings['first_history'].append(shell.settled(b'\x12'))
                if seven:
                    shell.until(lambda: history_search_shown(shell.screen.display))
                    shell.settled(b'\x1b')
                    shell.settled(b'\x1b')
                shell.cancel()
                timings['builtin_loop'].append(shell.command('for ((i=0;i<10000;i++)); do :; done'))
                timings['external_commands'].append(shell.command('for ((i=0;i<20;i++)); do /usr/bin/true; done'))
                if seven:
                    shell.command('binit_complete editor history off; binit_complete on')
                shell.settled(b"printf '%s' ./alpha-00.t")
                timings['first_file_completion'].append(shell.settled(b'\t'))
                shell.cancel()
                if seven:
                    before = profile(shell)
                    for identifier in PLUGINS:
                        expected = 'pending' if identifier == 'git-completion' else 'loaded'
                        if before.get(identifier, {}).get('state') != expected:
                            raise RuntimeError('plugin readiness mismatch: ' + str(before))
                    shell.settled(b'git sta')
                    timings['first_git_completion'].append(shell.settled(b'\t'))
                    shell.until(lambda: completion_shown(shell.screen.display, 'sta'))
                    shell.settled(b'\x1b')
                    shell.cancel()
                    shell.settled(b'git sta')
                    timings['warm_git_completion'].append(shell.settled(b'\t'))
                    shell.until(lambda: completion_shown(shell.screen.display, 'sta'))
                    shell.settled(b'\x1b')
                    shell.cancel()
                    after = profile(shell)
                    for identifier in PLUGINS:
                        if after.get(identifier, {}).get('state') != 'loaded':
                            raise RuntimeError('plugin readiness mismatch: ' + str(after))
                    profiles.append({'scenario': name, 'before_git_completion': before, 'after_git_completion': after})
            finally:
                shell.close()
    return {
        'schema': 1, 'units': 'milliseconds', 'pairs': args.samples,
        'method': 'Alternating scenario order; same Bash, vi mode, 120x32 PTY, synthetic 1000-entry history. '
                  'Typing measures input to last terminal byte before 2 ms quiet; quiet interval excluded. '
                  'Usable startup adds first character response. File completion inserts one unique filename; excludes typing. '
                  'History panel disabled for completion. Git completion measured only with seven plugins. Warm filesystem caches; shared host.',
        'bash_version': subprocess.check_output([args.bash, '--version'], text=True).splitlines()[0],
        'bash_sha256': digest(args.bash), 'binary_sha256': digest(binary),
        'compare_binary_sha256': digest(args.compare_binary) if args.compare_binary else None,
        'workload_sha256': digest(__file__), 'history_sha256': hashlib.sha256(history.encode()).hexdigest(),
        'plugins': plugin_receipts, 'profiles': profiles, 'raw': samples,
        'summary': {name: {metric: summarize(values) for metric, values in timings.items()}
                    for name, timings in samples.items()},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin-root', type=Path, required=True)
    parser.add_argument('--binary', type=Path, default=ROOT / 'target/release/binit')
    parser.add_argument('--compare-binary', type=Path, help='include a previous seven-plugin binary in each alternating round')
    parser.add_argument('--bash', default='/bin/bash')
    parser.add_argument('--samples', type=int, default=20)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 1 or not Path(args.bash).is_absolute():
        parser.error('samples must be positive and bash must be an absolute path')
    with tempfile.TemporaryDirectory(prefix='.scratch-interactive-', dir=ROOT) as temporary:
        result = benchmark(args, Path(temporary))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result['summary'], indent=2))


if __name__ == '__main__':
    main()
