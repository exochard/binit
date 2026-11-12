import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('binit_installer', ROOT / 'scripts/install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='.scratch-installer-', dir=ROOT)
        self.root = Path(self.tmp.name)
        self.prefix = self.root / 'prefix with space'
        self.source = self.root / 'built-binit'
        self.source.write_bytes(b'first binary')
        self.source.chmod(0o755)
        self.rc = self.root / 'bashrc'
        self.rc.write_bytes(b'# user settings without newline')
        self.binary = self.prefix / 'bin/binit'
        self.receipt = self.prefix / 'share/binit/install.json'

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/install.py'), '--binary', str(self.source),
                               '--prefix', str(self.prefix), *map(str, args)], capture_output=True, text=True)

    def test_install_upgrade_and_uninstall_preserve_user_bytes(self):
        first = self.cli('--rc-file', self.rc)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn(' init)', first.stdout)
        self.assertEqual(self.binary.read_bytes(), b'first binary')
        self.assertEqual(self.binary.stat().st_mode & 0o777, 0o755)
        installed_rc = self.rc.read_bytes()
        self.assertTrue(installed_rc.startswith(b'# user settings without newline\n'))
        self.assertEqual(installed_rc.count(installer.START), 1)
        self.assertEqual(self.cli('--rc-file', self.rc).returncode, 0)
        self.assertEqual(self.rc.read_bytes(), installed_rc)
        self.assertEqual(len(list(self.root.glob('bashrc.binit-*.bak'))), 1)
        self.source.write_bytes(b'new binary')
        self.assertEqual(self.cli().returncode, 0)
        self.assertEqual(self.binary.read_bytes(), b'new binary')
        self.assertEqual([p.read_bytes() for p in self.binary.parent.glob('binit.binit-*.bak')], [b'first binary'])
        self.rc.write_bytes(self.rc.read_bytes() + b'export USER_SETTING=1\n')
        removed = self.cli('--uninstall')
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(self.binary.exists())
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.rc.read_bytes(), b'# user settings without newline\nexport USER_SETTING=1\n')
        self.assertEqual(len(list(self.root.glob('bashrc.binit-*.bak'))), 1)

    def test_default_install_never_touches_startup_file(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.rc.read_bytes(), b'# user settings without newline')
        self.assertIsNone(json.loads(self.receipt.read_text())['rc_file'])
        state = self.prefix / 'share/binit/state'
        state.mkdir()
        (state / 'catalog').write_text('preserved')
        self.assertEqual(self.cli('--uninstall').returncode, 0)
        self.assertEqual((state / 'catalog').read_text(), 'preserved')

    def test_installed_binary_initializes_a_real_private_shell(self):
        built = Path(os.environ.get('BINIT_TEST_BINARY', ROOT / 'target/release/binit'))
        self.assertTrue(built.is_file(), 'release binary required before installer smoke test')
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/install.py'), '--binary', str(built),
                                 '--prefix', str(self.prefix), '--rc-file', str(self.rc)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        home = self.root / 'home'
        home.mkdir()
        env = dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(home / 'data'), TERM='dumb', HISTFILE='/dev/null')
        for key in ('BASH_ENV', 'ENV', 'BINIT_STATE', 'BINIT_SAFE'):
            env.pop(key, None)
        bash = env.get('BINIT_BASH', '/bin/bash')
        shell = subprocess.run([bash, '--noprofile', '--rcfile', str(self.rc), '-ic',
                                'type -t binit; binit profile; printf "state=%s\\n" "$BINIT_STATE_DIR"'],
                               env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(shell.returncode, 0, shell.stderr)
        self.assertIn('function\n', shell.stdout)
        self.assertIn('PLUGIN', shell.stdout)
        self.assertIn(f'state={home}/data/binit', shell.stdout)

    def test_nonexecutable_source_and_changed_installed_mode_are_rejected(self):
        self.source.chmod(0o644)
        self.assertNotEqual(self.cli().returncode, 0)
        self.assertFalse(self.binary.exists())
        self.source.chmod(0o755)
        self.assertEqual(self.cli().returncode, 0)
        self.binary.chmod(0o700)
        self.assertNotEqual(self.cli('--uninstall').returncode, 0)
        self.assertEqual(self.binary.read_bytes(), b'first binary')
        self.assertTrue(self.receipt.exists())

    def test_foreign_or_modified_executable_is_never_replaced_or_removed(self):
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(b'unrelated')
        self.assertNotEqual(self.cli().returncode, 0)
        self.assertEqual(self.binary.read_bytes(), b'unrelated')
        self.binary.unlink()
        self.assertEqual(self.cli().returncode, 0)
        self.binary.write_bytes(b'user change')
        self.assertNotEqual(self.cli().returncode, 0)
        self.assertNotEqual(self.cli('--uninstall').returncode, 0)
        self.assertEqual(self.binary.read_bytes(), b'user change')
        self.assertTrue(self.receipt.exists())

    def test_modified_block_refuses_all_uninstall_changes(self):
        self.assertEqual(self.cli('--rc-file', self.rc).returncode, 0)
        self.rc.write_bytes(self.rc.read_bytes().replace(b' init)', b' changed)'))
        modified = self.rc.read_bytes()
        self.assertNotEqual(self.cli('--uninstall').returncode, 0)
        self.assertEqual(self.rc.read_bytes(), modified)
        self.assertEqual(self.binary.read_bytes(), b'first binary')

    def test_malformed_receipt_is_a_clean_error_and_preserves_files(self):
        self.assertEqual(self.cli('--rc-file', self.rc).returncode, 0)
        original_rc = self.rc.read_bytes()
        for value in ('[]', '{}', '{"schema": 1}'):
            self.receipt.write_text(value)
            result = self.cli('--uninstall')
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual(self.rc.read_bytes(), original_rc)
            self.assertEqual(self.binary.read_bytes(), b'first binary')

    def test_symlinks_and_unrecorded_blocks_are_rejected(self):
        self.prefix.symlink_to(self.root, target_is_directory=True)
        self.assertNotEqual(self.cli().returncode, 0)
        self.prefix.unlink()
        self.rc.write_bytes(installer.START + b'user\n' + installer.END)
        self.assertNotEqual(self.cli('--rc-file', self.rc).returncode, 0)
        self.assertFalse(self.binary.exists())

    def test_startup_symlink_and_conflicting_paths_are_rejected(self):
        target = self.root / 'real-rc'
        target.write_text('untouched\n')
        self.rc.unlink()
        self.rc.symlink_to(target)
        self.assertNotEqual(self.cli('--rc-file', self.rc).returncode, 0)
        self.assertEqual(target.read_text(), 'untouched\n')
        self.assertNotEqual(self.cli('--rc-file', self.source).returncode, 0)
        self.assertEqual(self.source.read_bytes(), b'first binary')
        self.assertFalse(self.binary.exists())

    def test_failed_startup_write_rolls_back_executable_and_receipt(self):
        installer.install(self.source, self.prefix, self.rc)
        previous_rc = self.rc.read_bytes()
        previous_receipt = self.receipt.read_bytes()
        self.source.write_bytes(b'upgrade')
        # Change only the owned block to force a startup rewrite while keeping
        # its receipt consistent, then inject the filesystem failure.
        receipt = json.loads(previous_receipt)
        block = installer.decode_block(receipt)
        older_block = block.replace(b' init)', b' --state old init)')
        receipt['rc_block'] = __import__('base64').b64encode(older_block).decode()
        self.receipt.write_text(json.dumps(receipt))
        self.rc.write_bytes(previous_rc.replace(block, older_block))
        previous_rc = self.rc.read_bytes()
        previous_receipt = self.receipt.read_bytes()
        original_atomic = installer.atomic
        def fail_rc(path, data, mode):
            if path == self.rc:
                raise OSError('fixture write failed')
            return original_atomic(path, data, mode)
        with patch.object(installer, 'atomic', side_effect=fail_rc):
            with self.assertRaisesRegex(OSError, 'fixture write failed'):
                installer.install(self.source, self.prefix, self.rc)
        self.assertEqual(self.binary.read_bytes(), b'first binary')
        self.assertEqual(self.rc.read_bytes(), previous_rc)
        self.assertEqual(self.receipt.read_bytes(), previous_receipt)

    def test_failed_receipt_write_rolls_back_first_install(self):
        original_rc = self.rc.read_bytes()
        original_atomic = installer.atomic
        def fail_receipt(path, data, mode):
            if path == self.receipt:
                raise OSError('fixture receipt failure')
            return original_atomic(path, data, mode)
        with patch.object(installer, 'atomic', side_effect=fail_receipt):
            with self.assertRaisesRegex(OSError, 'fixture receipt failure'):
                installer.install(self.source, self.prefix, self.rc)
        self.assertEqual(self.rc.read_bytes(), original_rc)
        self.assertFalse(self.binary.exists())
        self.assertFalse(self.receipt.exists())


if __name__ == '__main__':
    unittest.main()
