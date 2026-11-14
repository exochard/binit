"""Candidate preparation must not select unverified shell changes."""
import json
import fcntl
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('BINIT_TEST_BINARY', ROOT / 'target/release/binit'))


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='.scratch-candidates-', dir=ROOT)
        self.root = Path(self.temporary.name)
        self.state = self.root / 'state'
        self.plugin = self.root / 'plugin'
        self.plugin.mkdir()
        (self.plugin / 'plugin.toml').write_text(
            "schema=1\nid='sample'\nentry='init.bash'\nload='startup'\n")
        self.entry = self.plugin / 'init.bash'
        self.entry.write_text('greet() { printf old; }\n')
        self.config = self.root / 'config.toml'
        self.config.write_text('schema=1\n[[plugins]]\npath="plugin"\n')
        self.environment = dict(os.environ, HOME=str(self.root), INPUTRC='/dev/null')
        for name in ('BASH_ENV', 'ENV'):
            self.environment.pop(name, None)

    def tearDown(self):
        self.temporary.cleanup()

    def run_binit(self, *arguments):
        return subprocess.run([str(BINARY), *arguments, '--state', str(self.state)],
                              capture_output=True, text=True, env=self.environment,
                              cwd=self.root, timeout=15)

    def success(self, *arguments):
        result = self.run_binit(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_preparing_candidate_preserves_current_and_previous_and_snapshots_new_code(self):
        self.success('prepare', str(self.config))
        self.entry.write_text('greet() { printf current; }\n')
        self.success('prepare', str(self.config))
        current = (self.state / 'current').read_text()
        previous = (self.state / 'previous').read_bytes() if (self.state / 'previous').exists() else None
        self.entry.write_text('greet() { printf candidate; }\n')
        self.success('candidate', 'prepare', str(self.config))
        status = json.loads(self.success('candidate', 'status', '--json'))
        self.assertEqual(status['base_generation'], current)
        self.assertEqual(status['config_sha256'], hashlib.sha256(self.config.read_bytes()).hexdigest())
        self.assertNotEqual(status['generation'], current)
        self.assertEqual((self.state / 'current').read_text(), current)
        self.assertEqual((self.state / 'previous').read_bytes()
                         if (self.state / 'previous').exists() else None, previous)
        snapshot = self.state / 'generations' / status['generation'] / 'plugins/sample/init.bash'
        self.assertIn('printf candidate', snapshot.read_text())
        self.success('status')

    def test_first_candidate_leaves_selection_absent(self):
        self.success('candidate', 'prepare', str(self.config))
        status = json.loads(self.success('candidate', 'status', '--json'))
        self.assertIsNone(status['base_generation'])
        self.assertFalse((self.state / 'current').exists())
        self.assertFalse((self.state / 'previous').exists())

    def test_failed_preparation_preserves_existing_candidate_and_current(self):
        self.success('prepare', str(self.config))
        current = (self.state / 'current').read_text()
        self.entry.write_text('greet() { printf candidate; }\n')
        self.success('candidate', 'prepare', str(self.config))
        before = json.loads(self.success('candidate', 'status', '--json'))
        (self.plugin / 'plugin.toml').write_text("schema=1\nid='invalid name'\n")
        failed = self.run_binit('candidate', 'prepare', str(self.config))
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(json.loads(self.success('candidate', 'status', '--json')), before)
        self.assertEqual((self.state / 'current').read_text(), current)
        self.success('status')

    def test_candidate_status_rejects_changed_generation_content(self):
        self.success('candidate', 'prepare', str(self.config))
        status = json.loads(self.success('candidate', 'status', '--json'))
        snapshot = self.state / 'generations' / status['generation'] / 'runtime.bash'
        snapshot.write_text(': changed\n')
        failed = self.run_binit('candidate', 'status', '--json')
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('content changed', failed.stderr)

    def test_candidate_status_refuses_symlink_record_without_changing_its_target(self):
        self.success('candidate', 'prepare', str(self.config))
        record = self.state / 'candidate.json'
        saved = self.root / 'saved-candidate.json'
        record.rename(saved)
        before = saved.read_bytes()
        record.symlink_to(saved)
        failed = self.run_binit('candidate', 'status', '--json')
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('private regular file', failed.stderr)
        self.assertEqual(saved.read_bytes(), before)

    def test_candidate_status_refuses_nonprivate_record(self):
        self.success('candidate', 'prepare', str(self.config))
        record = self.state / 'candidate.json'
        before = record.read_bytes()
        record.chmod(0o644)
        failed = self.run_binit('candidate', 'status', '--json')
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('private regular file', failed.stderr)
        self.assertEqual(record.read_bytes(), before)

    def test_prepare_refuses_lock_contention_without_changing_current_or_candidate(self):
        self.success('prepare', str(self.config))
        self.success('candidate', 'prepare', str(self.config))
        before = json.loads(self.success('candidate', 'status', '--json'))
        current = (self.state / 'current').read_text()
        self.entry.write_text('greet() { printf blocked; }\n')
        with (self.state / '.mutation.lock').open('rb') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            failed = self.run_binit('candidate', 'prepare', str(self.config))
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('another Binit change', failed.stderr)
        self.assertEqual(json.loads(self.success('candidate', 'status', '--json')), before)
        self.assertEqual((self.state / 'current').read_text(), current)


if __name__ == '__main__':
    unittest.main()
