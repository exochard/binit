"""Public make-demo entry point; fixture plugins test catalog selection."""
import os
from pathlib import Path
import pty
import re
import select
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('BINIT_TEST_BINARY', ROOT / 'target/release/binit')).resolve()
PLUGINS = ('git-prompt', 'git-completion', 'project-context', 'directory-jump',
           'history-search', 'syntax-highlighting', 'autosuggestions')


class DemoEntry(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.scratch-demo-entry-', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.inputrc = self.work / 'inputrc'
        self.inputrc.write_text('set completion-ignore-case on\n')
        self.environment = dict(os.environ, HOME=str(self.work), BINIT_TEST_BINARY=str(BINARY),
                                BINIT_PLUGIN_ROOT=str(self.work / 'plugins'),
                                BINIT_DEMO_ROOT=str(self.work / 'demo'),
                                INPUTRC=str(self.inputrc), TERM='xterm-256color')
        for key in ('BINIT_DEMO_STATE', 'BINIT_SAFE', 'BASH_ENV', 'ENV'):
            self.environment.pop(key, None)

    def test_missing_plugins_fail_without_opening_the_legacy_demo(self):
        result = subprocess.run(['make', '--no-print-directory', 'demo'], cwd=ROOT,
                                env=self.environment, input='', capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('missing independent plugin sources', result.stderr)
        self.assertNotIn('Binit shell.', result.stdout)

    def test_make_demo_selects_all_plugins_and_preserves_native_vi_settings(self):
        for name in PLUGINS:
            plugin = self.work / 'plugins' / name
            plugin.mkdir(parents=True)
            (plugin / 'plugin.toml').write_text(f'schema=1\nid="{name}"\nentry="plugin.bash"\nload="startup"\n')
            (plugin / 'plugin.bash').write_text(':\n')
        pid, descriptor = pty.fork()
        if pid == 0:
            os.chdir(ROOT)
            os.execvpe('make', ['make', '--no-print-directory', 'demo'], self.environment)
        data = bytearray()
        reaped = False

        def until(marker):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if marker in data:
                    return
                if select.select([descriptor], [], [], .05)[0]:
                    try:
                        part = os.read(descriptor, 65536)
                    except OSError:
                        break
                    if not part:
                        break
                    data.extend(part)
            self.fail(f'demo did not reach {marker!r}: {data.decode(errors="replace")}')

        try:
            until(b'$ ')
            os.write(descriptor, b'binit profile; [[ -o vi ]] && printf "\\036VI\\036"; bind -v; printf "\\036DONE\\036"\n')
            until(b'\x1eDONE\x1e')
            text = data.decode(errors='replace').replace('\r', '')
            for name in PLUGINS:
                self.assertRegex(text, re.compile(r'^' + name + r'\s+loaded\b', re.M))
            self.assertIn('\x1eVI\x1e', text)
            self.assertIn('set completion-ignore-case on', text)
            os.write(descriptor, b'exit\n')
            _, status = os.waitpid(pid, 0)
            reaped = True
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        finally:
            os.close(descriptor)
            if not reaped:
                for _ in range(50):
                    ended, _ = os.waitpid(pid, os.WNOHANG)
                    if ended:
                        break
                    time.sleep(.02)
                else:
                    os.kill(pid, signal.SIGTERM)
                    os.waitpid(pid, 0)
