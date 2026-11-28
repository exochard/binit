"""Command discovery works through the executable and initialized shells."""
import os
import fcntl
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile
import termios
import time
import unittest

from test_wait import PROMPT, Session

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

    def test_tab_completes_commands_and_the_right_plugin_catalogs(self):
        for mode in ('emacs', 'vi'):
            for text, expected in (
                ('binit cand', 'binit candidate'),
                ('binit candidate di', 'binit candidate diff'),
                ('binit candidate res', 'binit candidate restore'),
                ('binit candidate restore slo', 'binit candidate restore slow'),
                ('binit candidate restore slow --fr', 'binit candidate restore slow --from'),
                ('binit report slo', 'binit report slow'),
                ('binit inspect slo', 'binit inspect slow'),
                ('binit plugins --sess', 'binit plugins --session'),
            ):
                with self.subTest(mode=mode, text=text), Session(':\n', menu=False, mode=mode) as shell:
                    shell.wait(lambda: shell.cursor_line == PROMPT)
                    shell.send(text + '\t')
                    shell.wait(lambda: shell.cursor_line == f'{PROMPT} {expected}')

    def test_plugin_session_separates_live_and_next_generation(self):
        result = self.command('plugins', '--session')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires an initialized shell', result.stderr)
        with Session(':\n', ':\n', menu=False) as shell:
            shell.wait(lambda: shell.cursor_line == PROMPT)
            plugin = shell.directory / 'next-plugin'
            plugin.mkdir()
            (plugin / 'plugin.toml').write_text("schema=1\nid='next-plugin'\nentry='plugin.bash'\nload='startup'\n")
            (plugin / 'plugin.bash').write_text(':\n')
            shell.send(f'binit disable early && binit remove slow && binit add {shlex.quote(str(plugin))} && '
                       'COLUMNS=80 binit plugins --session > marks/view\n')
            shell.wait(lambda: shell.mark('view') and shell.cursor_line == PROMPT)
            view = (shell.marks / 'view').read_text()
            self.assertIn('different generations', view)
            self.assertRegex(view, r'(?m)^early\s+Loaded\s+Disabled\s+Startup$')
            self.assertRegex(view, r'(?m)^slow\s+Loaded\s+Removed\s+After prompt$')
            self.assertRegex(view, r'(?m)^next-plugin\s+Absent\s+Enabled\s+Startup$')
            self.assertNotIn('\x1b', view)

    def test_plugin_session_rejects_an_invalid_current_pointer(self):
        self.state.mkdir()
        (self.state / 'current').symlink_to(self.state / 'missing-pointer-target')
        generation = 'a' * 64
        result = subprocess.run([str(BINARY), 'plugins', '--session', '--stdin'],
                                input=f'{generation}\0', capture_output=True, text=True,
                                env=self.env, cwd=self.root, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('expected a regular file', result.stderr)
        self.assertEqual(result.stdout, '')

    def test_tab_preserves_the_callers_completion_specification(self):
        with Session('', 'complete -W custom-result binit\n', menu=False) as shell:
            shell.wait(lambda: shell.cursor_line == PROMPT)
            shell.send('binit cus\t')
            shell.wait(lambda: shell.cursor_line == f'{PROMPT} binit custom-result')

    def test_opt_in_report_and_unload_preserve_later_user_edits(self):
        setup = r"""BINIT_REPORT_EFFECTS=1
before_audit_fixture() {
    shopt -s extglob
    eval 'restore_me() { case a in @(a|b)) echo old;; esac; }'
    shopt -u extglob
    keep_me() { echo old; }
    bring_back() { echo old; }
    AUDITED=old
    complete -W 'old choice' audit_cmd
    bind -m emacs-standard '"\C-xq": beginning-of-line'
    bind -m vi-insert '"\C-xq": beginning-of-line'
}
binit on before-load slow before_audit_fixture
"""
        change = r"""restore_me() { echo new; }
keep_me() { echo new; }
unset -f bring_back
export AUDITED=fixture-value-must-stay-private
complete -W 'new choice' audit_cmd
bind -m emacs-standard '"\C-xq": end-of-line'
bind -m vi-insert '"\C-xq": end-of-line'
return 9
"""
        with Session(change, setup, menu=False) as shell:
            # This case takes full Bash namespace snapshots on the fallback path.
            deadline = time.monotonic() + 60

            def wait_for_audit(predicate):
                shell.wait(predicate, seconds=deadline - time.monotonic())

            wait_for_audit(lambda: shell.cursor_line == PROMPT)
            shell.send('keep_me() { echo user; }; binit report slow > marks/report\n')
            wait_for_audit(lambda: shell.mark('report') and shell.cursor_line == PROMPT)
            report = (shell.marks / 'report').read_text()
            self.assertIn('state failed', report)
            self.assertIn('effect changed function restore_me current', report)
            self.assertIn('effect changed function keep_me modified', report)
            self.assertIn('effect removed function bring_back current', report)
            self.assertIn('effect changed variable AUDITED current  -- -> -x', report)
            self.assertIn('effect changed completion audit_cmd current', report)
            self.assertIn('effect changed binding emacs-', report)
            self.assertIn('effect changed binding vi-insert/', report)
            self.assertNotIn(b'invalid keymap name', shell.raw)
            self.assertNotIn('fixture-value-must-stay-private', report)
            shell.send('binit unload slow && ! shopt -q extglob && '
                       '(shopt -s extglob; restore_me; keep_me; bring_back) > marks/result\n')
            wait_for_audit(lambda: shell.mark('result') and shell.cursor_line == PROMPT)
            self.assertEqual((shell.marks / 'result').read_text(), 'old\nuser\nold\n')
