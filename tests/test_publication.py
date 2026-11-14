"""A failed generation write must leave the selected generation usable."""
import json
import os
from pathlib import Path
import resource
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('BINIT_TEST_BINARY', ROOT / 'target/release/binit'))


def limited_file_size():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16384, 16384))


class PublicationTests(unittest.TestCase):
    def test_failed_stage_write_keeps_previous_generation_selected_and_verified(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-publication-', dir=ROOT) as temporary:
            root = Path(temporary)
            plugin = root / 'plugin'
            plugin.mkdir()
            (plugin / 'plugin.toml').write_text("schema=1\nid='sample'\nentry='init.bash'\nload='startup'\n")
            entry = plugin / 'init.bash'
            entry.write_text('greet() { :; }\n')
            config = root / 'config.toml'
            config.write_text('schema=1\n[[plugins]]\npath=' + json.dumps(str(plugin)) + '\n')
            state = root / 'state'
            command = [str(BINARY), 'prepare', str(config), '--state', str(state)]
            first = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertEqual(first.returncode, 0, first.stderr)
            before = (state / 'current').read_text()
            entry.write_text('greet() { : changed; }\n')
            failed = subprocess.run(command, capture_output=True, text=True, timeout=15,
                                    preexec_fn=limited_file_size)
            self.assertNotEqual(failed.returncode, 0)
            self.assertTrue(list((state / 'generations').glob('.staging-*')))
            self.assertEqual((state / 'current').read_text(), before)
            status = subprocess.run([str(BINARY), 'status', '--state', str(state)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn(before.strip(), status.stdout)


if __name__ == '__main__':
    unittest.main()
