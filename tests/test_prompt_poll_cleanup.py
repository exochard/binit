"""Exercise watched loading and interrupt recovery through the public shell."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from bench_corpus_terminal import CheckedShell
from bench_ecosystem import environment

BINARY = Path(os.environ.get('BINIT_TEST_BINARY', ROOT / 'target/debug/binit')).resolve()
BASH = os.environ.get('BINIT_BASH', '/bin/bash')
PROMPT = b'BENCH> '


class PromptPollCleanupTests(unittest.TestCase):
    @contextmanager
    def shell(self, second=False):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            directory = Path(temporary)
            home = directory / 'home'
            home.mkdir()
            config = 'schema=1\n'
            setup = "PS1='BENCH> '\nPS2=''\nHISTFILE=/dev/null\nset -o emacs\n"
            for name in (['first', 'second'] if second else ['first']):
                plugin = directory / name
                plugin.mkdir()
                marker = directory / f'{name}.ready'
                (plugin / 'plugin.toml').write_text(
                    f'schema=1\nid="{name}"\nentry="plugin.bash"\nload="watch"\n'
                    f'when="ready_{name}"\nwatch=[{json.dumps(str(marker))}]\n')
                (plugin / 'plugin.bash').write_text(
                    f'WF_{name.upper()}=$(( ${{WF_{name.upper()}:-0}} + 1 ))\n'
                    f'printf "\\036LOADED-{name}\\037"\n')
                setup += f'ready_{name}() {{ [[ -f {shlex.quote(str(marker))} ]]; }}\n'
                config += f'[[plugins]]\npath="{name}"\n'
            path = directory / 'config.toml'
            path.write_text(config)
            state = directory / 'state'
            env = {**environment(home), 'BINIT_BASH': BASH}
            subprocess.run([str(BINARY), 'prepare', str(path), '--state', str(state)],
                           env=env, check=True, capture_output=True)
            rc = directory / 'rc'
            rc.write_text(setup + f'eval "$({shlex.quote(str(BINARY))} init --state {shlex.quote(str(state))})"\n')
            shell = CheckedShell(BASH, rc, directory, env)
            succeeded = False
            try:
                yield shell, directory, any(state.glob('generations/*/completion.so'))
                succeeded = True
            finally:
                try:
                    shell.close(ready=succeeded)
                except RuntimeError:
                    if succeeded:
                        raise

    def interrupt_and_use(self, shell):
        start = len(shell.raw)
        os.write(shell.fd, b'\x03')
        shell.until(lambda: PROMPT in shell.raw[start:], seconds=1)
        start = len(shell.raw)
        shell.command('printf "\\036INTACT-INPUT\\037"')
        self.assertIn(b'\x1eINTACT-INPUT\x1f', shell.raw[start:])

    def activate_first(self, shell, directory):
        (directory / 'first.ready').touch()
        start = len(shell.raw)
        shell.command(':')
        self.assertIn(b'\x1eLOADED-first\x1f', shell.raw[start:])

    def test_prompt_activation_releases_polling_before_interrupt(self):
        with self.shell() as (shell, directory, _native):
            self.activate_first(shell, directory)
            self.interrupt_and_use(shell)

    def test_pending_watch_survives_interrupt_and_still_loads_at_idle(self):
        with self.shell(second=True) as (shell, directory, native):
            if not native:
                self.skipTest('idle watched loading requires native completion')
            self.activate_first(shell, directory)
            self.interrupt_and_use(shell)
            start = len(shell.raw)
            (directory / 'second.ready').touch()
            shell.until(lambda: b'\x1eLOADED-second\x1f' in shell.raw[start:], seconds=2)
            shell.until(lambda: PROMPT in shell.raw[start:], seconds=2)
            self.interrupt_and_use(shell)
