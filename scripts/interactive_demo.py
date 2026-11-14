#!/usr/bin/env python3
"""Prepare and open the explicitly selected first-party development demo."""
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys

from fetch_syntax import fetch, runtime, verify, verify_runtime

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ('git-prompt', 'git-completion', 'project-context', 'directory-jump',
           'history-search', 'syntax-highlighting', 'autosuggestions')


def make_value(path):
    return str(path).replace('$', '$$').replace('#', '\\#')


def prepare():
    demo = Path(os.environ.get('BINIT_DEMO_ROOT', ROOT / '.local-demo-interactive')).resolve()
    plugins = Path(os.environ.get('BINIT_PLUGIN_ROOT', ROOT.parent / 'binit-plugins')).resolve()
    missing = [name for name in PLUGINS if not (plugins / name / 'plugin.toml').is_file()]
    if missing:
        raise ValueError('missing independent plugin sources: ' + ', '.join(missing)
                         + '; set BINIT_PLUGIN_ROOT to their parent directory')
    demo.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = demo.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('demo directory must be owned by you with mode 700')
    supplied = os.environ.get('BINIT_TEST_BINARY')
    target = Path(os.environ.get('CARGO_TARGET_DIR', ROOT / 'target'))
    if not target.is_absolute():
        target = ROOT / target
    binary = Path(supplied).resolve() if supplied else target / 'release/binit'
    if not supplied:
        source = Path(os.environ.get('BINIT_SYNTAX_SOURCE', ROOT / '.cache/demo-syntax/grammar')).resolve()
        engine = Path(os.environ.get('BINIT_SYNTAX_RUNTIME', ROOT / '.cache/demo-syntax/runtime')).resolve()
        source.parent.mkdir(parents=True, exist_ok=True)
        engine.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            try:
                version = subprocess.check_output(['tree-sitter', '--version'], text=True).strip()
            except OSError as error:
                raise ValueError('install tree-sitter CLI 0.26.9 before the first demo build') from error
            if version != 'tree-sitter 0.26.9':
                raise ValueError('the demo grammar requires tree-sitter CLI 0.26.9')
            fetch(source)
        if not engine.exists():
            runtime(engine)
        verify(source)
        verify_runtime(engine)
        (demo / 'build.mk').write_text('export BINIT_SYNTAX_SOURCE := ' + make_value(source)
                                      + '\nexport BINIT_SYNTAX_RUNTIME := ' + make_value(engine) + '\n')
        subprocess.run(['make', '--no-print-directory', 'build',
                        'BINIT_SYNTAX_SOURCE=' + str(source),
                        'BINIT_SYNTAX_RUNTIME=' + str(engine)], cwd=ROOT, check=True)
    if not binary.is_file():
        raise ValueError('Binit binary unavailable; run make build')
    config = demo / 'plugins.toml'
    config.write_text('schema=1\n' + ''.join('\n[[plugins]]\npath=' + json.dumps(str(plugins / name))
                                            + '\n' for name in PLUGINS))
    state = demo / 'state'
    subprocess.run([str(binary), 'prepare', str(config), '--state', str(state)],
                   capture_output=True, text=True, check=True)
    history = state / 'history'
    try:
        history_info = history.lstat()
    except FileNotFoundError:
        descriptor = os.open(history, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(b'git status --short\nprintf "hello from binit\\n"\n')
    else:
        if not stat.S_ISREG(history_info.st_mode) or history_info.st_nlink != 1:
            raise ValueError('demo history must be a regular file with one link')
    mode = os.environ.get('BINIT_DEMO_EDITING', 'vi')
    if mode not in ('vi', 'emacs'):
        raise ValueError('BINIT_DEMO_EDITING must be vi or emacs')
    rc = demo / 'bashrc'
    rc.write_text('# Generated development demo; state and history stay local.\n'
                  + 'HISTFILE=' + shlex.quote(str(history)) + '\nHISTSIZE=1000\nHISTFILESIZE=1000\n'
                  + 'set -o ' + mode + '\nPS1=\'binit \\w \\$ \'\n'
                  + 'PATH=' + shlex.quote(str(binary.parent)) + ':"$PATH"\n'
                  + 'eval "$(' + shlex.quote(str(binary)) + ' init --state '
                  + shlex.quote(str(state)) + ')"\n')
    (demo / 'launch.bash').write_text('#!/usr/bin/env bash\nexec bash '
                                    + shlex.quote(str(ROOT / 'scripts/demo.bash')) + '\n')
    bash = os.environ.get('BINIT_BASH', '/bin/bash')
    if not Path(bash).is_absolute():
        raise ValueError('BINIT_BASH must be an absolute path')
    environment = dict(os.environ, BINIT_COMPLETION_MENU='1')
    for name in ('BASH_ENV', 'ENV', 'BINIT_SAFE'):
        environment.pop(name, None)
    return bash, rc, environment


if __name__ == '__main__':
    try:
        interpreter, startup, environment = prepare()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        detail = getattr(error, 'stderr', '') or str(error)
        raise SystemExit('binit demo: ' + detail)
    print('Binit demo. Ctrl-R history. Exit returns.', flush=True)
    os.execvpe(interpreter, [interpreter, '--noprofile', '--rcfile', str(startup), '-i'], environment)
