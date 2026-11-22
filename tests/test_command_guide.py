"""Command discovery works through the executable and initialized shells."""
import os
import fcntl
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile
import termios
import unittest

ROOT = Path(__file__).resolve().parent.parent
BINARY = Path(os.environ.get('BINIT_TEST_BINARY', ROOT / 'target/release/binit')).resolve()
BASH = os.environ.get('BINIT_BASH', '/bin/bash')


class CommandGuideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='command-guide-', dir=ROOT / 'tmp')
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.env = dict(os.environ, HOME=str(self.root), BINIT_STATE=str(self.state),
                        TERM='dumb', INPUTRC='/dev/null', HISTFILE='/dev/null')
        self.env.pop('BASH_ENV', None)
        self.env.pop('ENV', None)

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, *args, **env):
        return subprocess.run([str(BINARY), *args], capture_output=True, text=True,
                              env={**self.env, **env}, cwd=self.root, timeout=15)

    def test_bare_guide_is_bounded_and_needs_no_state(self):
        for width in ('80', '44'):
            result = self.command(COLUMNS=width, NO_COLOR='1', BINIT_STATE='relative')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('current shell', result.stdout)
            self.assertNotIn('\x1b', result.stdout)
            self.assertTrue(all(len(line) <= int(width) for line in result.stdout.splitlines()))
            if width == '80':
                self.assertLessEqual(len(result.stdout.splitlines()), 24)
        self.assertFalse(self.state.exists())

    def test_command_help_and_unknown_topic(self):
        left = self.command('help', 'update')
        right = self.command('update', '--help')
        self.assertEqual(left.returncode, 0, left.stderr)
        self.assertEqual(left.stdout, right.stdout)
        self.assertIn('default 4', left.stdout)
        self.assertIn('enable|disable --plugin ID', self.command('help', 'completions').stdout)
        self.assertNotEqual(self.command('help', 'missing-command').returncode, 0)

    def test_guide_uses_terminal_width_and_respects_no_color(self):
        for no_color in ('', '1'):
            with self.subTest(no_color=no_color):
                reader, writer = os.openpty()
                try:
                    fcntl.ioctl(writer, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 44, 0, 0))
                    result = subprocess.run([str(BINARY)], stdout=writer, stderr=subprocess.PIPE,
                                            env={**self.env, 'TERM': 'xterm-256color',
                                                 'COLUMNS': '80', 'NO_COLOR': no_color}, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    output = os.read(reader, 4096).replace(b'\r\n', b'\n')
                    self.assertIn(b'  update --check\n    preview plugin updates\n', output)
                    self.assertEqual(b'\x1b[1mplugins\x1b[0m' in output, not no_color)
                finally:
                    os.close(writer)
                    os.close(reader)

    def test_initialized_shell_help_and_version(self):
        rc = self.root / 'rc'
        inputrc = self.root / 'inputrc'
        rc.write_text(f'eval "$({shlex.quote(str(BINARY))} init)"\n')
        for mode in ('emacs', 'vi'):
            inputrc.write_text(f'set editing-mode {mode}\n')
            result = subprocess.run([BASH, '--noprofile', '--rcfile', str(rc), '-i'],
                                    input='binit\nbinit --version\nbinit report --help\nbinit completions --help\nexit\n',
                                    capture_output=True, text=True, env=dict(self.env, INPUTRC=str(inputrc)),
                                    cwd=self.root, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('current shell', result.stdout)
            self.assertIn('binit 0.', result.stdout)
            self.assertIn('usage: binit report ID', result.stdout)
            self.assertIn('enable|disable --plugin ID', result.stdout)

    def test_run_passes_help_to_script(self):
        script = self.root / 'script.bash'
        script.write_text('printf "argument:%s\\n" "$1"\n')
        result = self.command('run', str(script), '--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'argument:--help\n')
