#!/usr/bin/env python3
"""Exercise the documented full source trial in a private real terminal."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import bench_interactive as terminal


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def input_display(screen):
    rows, cursor = screen.display, screen.cursor
    start = max((index for index in range(cursor.y + 1)
                 if 'binit ' in rows[index] and '$ ' in rows[index]), default=cursor.y)
    return ''.join(row.rstrip() for row in rows[start:cursor.y + 1])


def check(trial):
    core, plugins = trial / 'core', trial / 'plugins'
    private = core / 'tmp/source-trial-check'
    private.mkdir(parents=True, exist_ok=False)
    home = private / 'home'
    home.mkdir()
    personal = {'.bash_history': '# PRIVATE_HISTORY_FIXTURE\n',
                '.bashrc': 'printf PERSONAL_RC_MUST_NOT_RUN\n',
                '.bash_profile': '# private profile fixture\n',
                '.inputrc': 'set completion-ignore-case on\n'}
    for name, text in personal.items():
        (home / name).write_text(text)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('BINIT_', 'BASH_FUNC_')) and key not in ('BASH_ENV', 'ENV')}
    environment.update(HOME=str(home), BINIT_PLUGIN_ROOT=str(plugins),
                       BINIT_DEMO_ROOT=str(private / 'demo'), CARGO_TARGET_DIR=str(core / 'tmp/build'),
                       INPUTRC=str(home / '.inputrc'), TERM='xterm-256color', LC_ALL='C.UTF-8',
                       GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                       XDG_DATA_HOME=str(home / '.local/share'), XDG_CONFIG_HOME=str(home / '.config'))
    terminal.PROMPT = b'$ '

    class TrialShell(terminal.Shell):
        def until(self, predicate, seconds=900):
            return super().until(predicate, seconds)

        def read(self, timeout):
            try:
                return super().read(timeout)
            except OSError as error:
                raise RuntimeError('trial terminal closed:\n' + self.raw.decode(errors='replace')) from error

    results, frames = [], []
    for launch in (1, 2):
        shell = TrialShell('/bin/bash', None, core, environment, command=['make', '--no-print-directory', 'demo'])
        try:
            def require(code, label):
                start = len(shell.raw)
                shell.command('if { ' + code + '; }; then printf "\\036PASS\\036"; else printf "\\036FAIL\\036"; fi')
                output = shell.raw[start:]
                if b'\x1ePASS\x1e' not in output or b'\x1eFAIL\x1e' in output:
                    raise RuntimeError(label + ': ' + output.decode(errors='replace'))
                results.append(dict(launch=launch, check=label, passed=True))

            require('[[ -o vi ]] && bind -v | grep -q "set completion-ignore-case on"', 'vi mode and personal Readline settings')
            profile = terminal.profile(shell)
            if set(profile) != set(terminal.PLUGINS):
                raise RuntimeError('selected plugin set differs')
            for plugin in terminal.PLUGINS:
                expected = 'pending' if plugin == 'git-completion' else 'loaded'
                if profile[plugin]['state'] != expected:
                    raise RuntimeError('plugin not ready: ' + plugin)
            results.append(dict(launch=launch, check='seven selected plugins and lazy Git readiness', passed=True))
            shell.settled(b'\x12')
            shell.until(lambda: any('History SEARCH' in row for row in shell.screen.display), seconds=5)
            frames.append(dict(launch=launch, state='history search', lines=shell.screen.display))
            shell.settled(b'\x1b')
            shell.settled(b'\x1b')
            shell.cancel()
            require('hs "hello from binit" | grep -Fx ' + shlex.quote('printf "hello from binit\\n"'), 'seeded history search')
            shell.settled(b'git sta')
            shell.settled(b'\t')
            shell.until(lambda: any('Completions' in row for row in shell.screen.display), seconds=5)
            frames.append(dict(launch=launch, state='Git completion', lines=shell.screen.display))
            shell.settled(b'\x1b')
            shell.cancel()
            if terminal.profile(shell)['git-completion']['state'] != 'loaded':
                raise RuntimeError('Git completion did not become loaded')
            results.append(dict(launch=launch, check='first-use Git completion menu', passed=True))
            require('binit_complete editor highlighting on && binit_complete editor suggestions on', 'native highlighting and suggestions available')
            require('history -s "printf source-trial-suggestion"', 'suggestion history entry')
            capture = private / 'input.txt'
            require('__source_trial_capture() { printf "%s" "$READLINE_LINE" > ' + shlex.quote(str(capture)) +
                    '; }; bind -m vi-insert -x ' + shlex.quote('"\\C-x\\C-g":__source_trial_capture'), 'input capture binding')
            shell.settled(b'printf source-trial-s')
            shell.until(lambda: 'printf source-trial-suggestion' in input_display(shell.screen), seconds=5)
            frames.append(dict(launch=launch, state='history suggestion', lines=shell.screen.display))
            shell.settled(b'\x18\x07')
            if capture.read_text() != 'printf source-trial-s':
                raise RuntimeError('unaccepted suggestion changed the input')
            shell.settled(b'\x06')
            shell.settled(b'\x18\x07')
            if capture.read_text() != 'printf source-trial-suggestion':
                raise RuntimeError('visible suggestion could not be accepted')
            results.append(dict(launch=launch, check='visible suggestion preserves input until accepted', passed=True))
            shell.cancel()
            require('binit trust examples/project && cd examples/project', 'enter trusted project')
            require('[[ $PROJECT_MODE == development ]] && '
                    '[[ $PROJECT_MESSAGE == ' + shlex.quote('Literal data: $(printf this-is-not-executed)') + ' ]]', 'trusted project variables remain literal')
            require('cd ../..', 'leave trusted project')
            require('[[ ! ${PROJECT_MODE+x} ]]', 'project variables restored')
            require('j amples/project && [[ $PWD == */examples/project ]] && cd ../..', 'ranked directory jump')
            require('git init -q --initial-branch=trial-branch tmp/source-trial-check/repo && cd tmp/source-trial-check/repo', 'Git prompt fixture')
            for _ in range(4):
                shell.command('sleep .6')
            if not any('trial-branch' in row for row in shell.screen.display[-4:]):
                raise RuntimeError('Git branch prompt did not appear')
            frames.append(dict(launch=launch, state='Git branch prompt', lines=shell.screen.display))
            require('cd ' + shlex.quote(str(core)), 'return to core source')
            if b'PERSONAL_RC_MUST_NOT_RUN' in shell.raw or b'PRIVATE_HISTORY_FIXTURE' in shell.raw:
                raise RuntimeError('personal fixture was imported')
        finally:
            shell.close()
        for name, text in personal.items():
            if (home / name).read_text() != text:
                raise RuntimeError('personal file changed: ' + name)
        history = private / 'demo/state/history'
        if 'PRIVATE_HISTORY_FIXTURE' in history.read_text():
            raise RuntimeError('personal history was copied')
    binary = core / 'tmp/build/release/binit'
    return dict(complete=True, checks=results, frames=frames, binary_sha256=digest(binary),
                manifest_sha256=digest(trial / 'sources.json'),
                personal_files_unchanged=True, personal_history_imported=False,
                bash_version=subprocess.check_output(['/bin/bash', '--version'], text=True).splitlines()[0])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trial', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with args.output.open('x') as stream:
        stream.write(json.dumps(dict(complete=False)) + '\n')
    try:
        result = check(args.trial.resolve())
    except BaseException as error:
        args.output.write_text(json.dumps(dict(complete=False, error=str(error))) + '\n')
        raise
    args.output.write_text(json.dumps(result, indent=2).replace(str(args.trial.resolve()), '$TRIAL') + '\n')
    print(f'Source trial: {len(result["checks"])} checks passed across two launches')
