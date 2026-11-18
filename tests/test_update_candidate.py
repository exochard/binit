"""Prototype update review: a plugin update reaches new shells only through a passing candidate check."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('BINIT_TEST_BINARY', ROOT / 'target/release/binit'))
SCRIPT = ROOT / 'scripts/update_candidate.py'
BASE = '_tool() { COMPREPLY=(start stop); }\ncomplete -F _tool tool\n'


class UpdateCandidateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='.scratch-update-candidate-', dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'state'
        self.environment = dict(os.environ, HOME=str(self.root), INPUTRC='/dev/null')
        for name in ('BASH_ENV', 'ENV'):
            self.environment.pop(name, None)
        self.source('v1', BASE)
        (self.root / 'config.toml').write_text('schema=1\n[[plugins]]\npath="v1"\n')
        (self.root / 'completion.bash').write_text('[[ $(complete -p tool) == *_tool* ]]\n')
        (self.root / 'checks.toml').write_text(
            'schema=1\n[[checks]]\nid="completion"\nscript="completion.bash"\ntimeout_ms=5000\nmax_elapsed_ms=400\n')
        self.binit('prepare', str(self.root / 'config.toml'))
        self.first = self.current()

    def source(self, name, body):
        directory = self.root / name
        directory.mkdir()
        (directory / 'plugin.toml').write_text("schema=1\nid='tool'\nentry='init.bash'\nload='startup'\n")
        (directory / 'init.bash').write_text(body)
        return directory

    def binit(self, *args, check=True):
        result = subprocess.run([str(BINARY), *args, '--state', str(self.state)], capture_output=True, text=True,
                                env=self.environment, cwd=self.root, timeout=60)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def update(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), '--binit', str(BINARY), '--state', str(self.state), *args],
                              capture_output=True, text=True, env=self.environment, cwd=self.root, timeout=120)

    def stage(self, name):
        result = self.update('stage', str(self.root / 'config.toml'), 'tool', str(self.root / name),
                             str(self.root / 'checks.toml'))
        return result.returncode, json.loads(result.stdout)

    def current(self):
        return (self.state / 'current').read_text()

    def assert_selected_generation_usable(self, generation):
        self.assertEqual(self.current(), generation)
        self.binit('status')

    def test_clean_update_is_selected_after_its_check(self):
        self.source('v2', BASE + 'tool_version() { echo 2; }\n')
        code, report = self.stage('v2')
        self.assertEqual(code, 0, report)
        self.assertEqual(report['source_files']['changed'], ['init.bash'])
        self.assertEqual(report['regressions'], [])
        self.assertEqual(self.current(), self.first)
        self.assertEqual(self.update('select').returncode, 0)
        self.assertNotEqual(self.current(), self.first)
        self.assertEqual((self.state / 'previous').read_text(), self.first)

    def test_update_that_drops_completion_is_shown_and_never_selected(self):
        self.source('v2', '_tool() { COMPREPLY=(start stop); }\n')
        code, report = self.stage('v2')
        self.assertNotEqual(code, 0)
        self.assertEqual(report['regressions'], ['completion'])
        self.assertTrue(report['selected']['completion']['passed'])
        self.assertNotEqual(self.update('select').returncode, 0)
        self.assert_selected_generation_usable(self.first)

    def test_update_that_adds_a_slow_load_hook_is_never_selected(self):
        self.source('v2', BASE + 'sleep 0.6\n')
        code, report = self.stage('v2')
        self.assertNotEqual(code, 0)
        self.assertEqual(report['candidate_state'], 'failed')
        self.assertGreater(min(report['candidate']['completion']['elapsed_ms']), 400)
        self.assertNotEqual(self.update('select').returncode, 0)
        self.assert_selected_generation_usable(self.first)

    def test_stale_receipt_or_changed_selection_leaves_the_selected_generation(self):
        self.source('v2', BASE + 'tool_version() { echo 2; }\n')
        code, report = self.stage('v2')
        self.assertEqual(code, 0, report)
        (self.root / 'completion.bash').write_text('true\n')
        self.assertNotEqual(self.update('select').returncode, 0)
        self.assert_selected_generation_usable(self.first)
        (self.root / 'completion.bash').write_text('[[ $(complete -p tool) == *_tool* ]]\n')
        code, report = self.stage('v2')
        self.assertEqual(code, 0, report)
        self.source('v3', BASE + 'other() { :; }\n')
        (self.root / 'other.toml').write_text('schema=1\n[[plugins]]\npath="v3"\n')
        self.binit('prepare', str(self.root / 'other.toml'))
        changed = self.current()
        self.assertNotEqual(self.update('select').returncode, 0)
        self.assert_selected_generation_usable(changed)


if __name__ == '__main__':
    unittest.main()
