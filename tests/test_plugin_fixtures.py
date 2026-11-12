"""Exercise the integrity boundary before upstream plugin code is sourced."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('fetch_plugins', ROOT / 'scripts/fetch_plugins.py')
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)


class FixtureTests(unittest.TestCase):
    def test_checker_quotes_selected_file_arguments(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-fixtures-', dir=ROOT) as temporary:
            root = Path(temporary)
            plugin = root / 'bash completion;false'
            plugin.write_text('BASH_COMPLETION_VERSINFO=(2 18 0)\n_init_completion() { :; }\n')
            receipt = root / 'receipt.json'
            binary = os.environ.get('BINIT_TEST_BINARY', str(ROOT / 'target/release/binit'))
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/check_plugins.py'),
                                     '--bash-completion', str(plugin), '--output', str(receipt)],
                                    env=dict(os.environ, BINIT_TEST_BINARY=binary),
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(plugin.name, json.loads(receipt.read_text())['checks'][0]['files'])

    def test_checker_opens_the_synced_generation_after_first_shell(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-fixtures-', dir=ROOT) as temporary:
            root = Path(temporary)
            plugin = root / 'nvm.sh'
            plugin.write_text("nvm() { printf '%s\\n' fixture-version; }\n")
            receipt = root / 'receipt.json'
            binary = os.environ.get('BINIT_TEST_BINARY', str(ROOT / 'target/release/binit'))
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/check_plugins.py'),
                                     '--nvm', str(plugin), '--output', str(receipt)],
                                    env=dict(os.environ, BINIT_TEST_BINARY=binary),
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            check = json.loads(receipt.read_text())['checks'][0]
            self.assertEqual(check['result'], 'pass')
            self.assertEqual(check['files']['nvm.sh'], hashlib.sha256(plugin.read_bytes()).hexdigest())

    def test_only_verified_bytes_are_written_and_existing_targets_are_refused(self):
        data = b'printf plugin\n'
        manifest = {'schema': 1, 'files': [{'repository': 'owner/repo', 'commit': 'a' * 40,
                    'path': 'init.bash', 'destination': 'plugin/init.bash',
                    'sha256': hashlib.sha256(data).hexdigest()}]}
        with tempfile.TemporaryDirectory(prefix='.scratch-fixtures-', dir=ROOT) as temporary:
            root = Path(temporary)
            source = root / 'manifest.json'
            source.write_text(json.dumps(manifest))
            with patch.object(fixtures, 'MANIFEST', source):
                with patch.object(fixtures.urllib.request, 'urlopen', return_value=io.BytesIO(data)):
                    fixtures.fetch(root / 'valid')
                self.assertEqual((root / 'valid/plugin/init.bash').read_bytes(), data)
                with self.assertRaises(FileExistsError):
                    fixtures.fetch(root / 'valid')
                for payload in [b'tampered', data + b'oversize']:
                    target = root / hashlib.sha256(payload).hexdigest()
                    with patch.object(fixtures, 'LIMIT', len(data)):
                        with patch.object(fixtures.urllib.request, 'urlopen', return_value=io.BytesIO(payload)):
                            with self.assertRaisesRegex(ValueError, 'size or checksum mismatch'):
                                fixtures.fetch(target)
                    self.assertFalse((target / 'plugin/init.bash').exists())
                    self.assertFalse((target / 'sources.json').exists())


if __name__ == '__main__':
    unittest.main()
