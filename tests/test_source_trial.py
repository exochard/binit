"""The public source trial keeps personal history and startup files private."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('BINIT_TEST_BINARY', ROOT / 'target/release/binit')).resolve()
PLUGINS = ('git-prompt', 'git-completion', 'project-context', 'directory-jump',
           'history-search', 'syntax-highlighting', 'autosuggestions')


class SourceTrialPrivacyTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / 'tmp'
        scratch.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='source-trial-test-', dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.home = self.work / 'home'
        self.home.mkdir()
        self.personal = {}
        for name in ('.bash_history', '.bashrc', '.bash_profile', '.inputrc'):
            path = self.home / name
            data = b'# PERSONAL_FIXTURE_DO_NOT_IMPORT\n'
            path.write_bytes(data)
            self.personal[path] = data
        self.demo = self.work / 'demo'
        plugins = self.work / 'plugins'
        for identifier in PLUGINS:
            source = plugins / identifier
            source.mkdir(parents=True)
            (source / 'plugin.toml').write_text(f'schema=1\nid="{identifier}"\nentry="plugin.bash"\nload="startup"\n')
            (source / 'plugin.bash').write_text(':\n')
        self.environment = dict(os.environ, HOME=str(self.home), BINIT_TEST_BINARY=str(BINARY),
                                BINIT_PLUGIN_ROOT=str(plugins), BINIT_DEMO_ROOT=str(self.demo),
                                TERM='xterm-256color')
        for key in ('BINIT_SAFE', 'BINIT_DEMO_STATE', 'BASH_ENV', 'ENV'):
            self.environment.pop(key, None)

    def launch(self):
        result = subprocess.run(['make', '--no-print-directory', 'demo'], cwd=ROOT,
                                env=self.environment, input='exit\n', text=True,
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        for path, expected in self.personal.items():
            self.assertEqual(path.read_bytes(), expected)
        return (self.demo / 'state/history').read_text()

    def test_first_trial_seeds_history_without_importing_personal_commands(self):
        history = self.launch()
        self.assertNotIn('PERSONAL_FIXTURE_DO_NOT_IMPORT', history)
        self.assertIn('git status --short', history)
        self.assertIn('hello from binit', history)

    def test_existing_trial_history_survives_another_launch(self):
        self.demo.mkdir(mode=0o700)
        state = self.demo / 'state'
        state.mkdir(mode=0o700)
        (state / 'history').write_text('printf existing-trial-command\n')
        history = self.launch()
        self.assertIn('existing-trial-command', history)
        self.assertNotIn('PERSONAL_FIXTURE_DO_NOT_IMPORT', history)

    def test_history_aliases_cannot_import_or_write_personal_files(self):
        for kind in ('symlink', 'dangling', 'hardlink'):
            with self.subTest(kind=kind):
                demo = self.work / kind
                demo.mkdir(mode=0o700)
                state = demo / 'state'
                state.mkdir(mode=0o700)
                history = state / 'history'
                personal = self.home / '.bash_history'
                missing = self.home / 'absent-history'
                if kind == 'hardlink':
                    os.link(personal, history)
                else:
                    history.symlink_to(missing if kind == 'dangling' else personal)
                result = subprocess.run(['make', '--no-print-directory', 'demo'], cwd=ROOT,
                                        env=dict(self.environment, BINIT_DEMO_ROOT=str(demo)),
                                        input='echo MUST_NOT_RUN\nexit\n', text=True,
                                        capture_output=True, timeout=20)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('history must be a regular file with one link', result.stderr)
                self.assertEqual(personal.read_bytes(), self.personal[personal])
                self.assertFalse(missing.exists())
