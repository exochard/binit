import base64
import hashlib
import json
import os
import shlex
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
BINARY = Path(os.environ.get('BINIT_TEST_BINARY', ROOT / 'target/release/binit')).resolve()
START = b'# >>> binit >>>\n'
END = b'# <<< binit <<<\n'


class InstallerTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'tmp').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='installer-', dir=ROOT / 'tmp')
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
        if args == ('--uninstall',):
            command = [str(BINARY), 'uninstall', '--prefix', str(self.prefix)]
        else:
            command = [str(BINARY), 'install', '--prefix', str(self.prefix), '--binary', str(self.source), *map(str, args)]
        return subprocess.run(command, capture_output=True, text=True, timeout=10)

    def test_install_upgrade_and_uninstall_preserve_user_bytes(self):
        first = self.cli('--rc-file', self.rc)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn(' init)', first.stdout)
        self.assertEqual(self.binary.read_bytes(), b'first binary')
        self.assertEqual(self.binary.stat().st_mode & 0o777, 0o755)
        installed_rc = self.rc.read_bytes()
        self.assertTrue(installed_rc.startswith(b'# user settings without newline\n'))
        self.assertEqual(installed_rc.count(START), 1)
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
        built = BINARY
        self.assertTrue(built.is_file(), 'release binary required before installer smoke test')
        home = self.root / 'home'
        home.mkdir()
        env = dict(os.environ, HOME=str(home), TERM='dumb', HISTFILE='/dev/null')
        for key in ('BASH_ENV', 'ENV', 'BINIT_STATE', 'BINIT_SAFE', 'XDG_DATA_HOME'):
            env.pop(key, None)
        result = subprocess.run([str(built), 'install', '--rc-file', str(self.rc)],
                                env=dict(env, PATH=str(home / 'empty-path')), umask=0o022,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        bash = env.get('BINIT_BASH', '/bin/bash')
        shell = subprocess.run([bash, '--noprofile', '--rcfile', str(self.rc), '-ic',
                                'type -t binit; binit profile; printf "state=%s\\n" "$BINIT_STATE_DIR"'],
                               env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(shell.returncode, 0, shell.stderr)
        self.assertIn('function\n', shell.stdout, shell.stderr)
        self.assertIn('PLUGIN', shell.stdout)
        state = home / '.local/share/binit'
        self.assertIn(f'state={state}', shell.stdout)
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)
        uninstalled = subprocess.run([bash, '--noprofile', '--rcfile', str(self.rc), '-ic', 'binit uninstall'],
                                     env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
        self.assertFalse((home / '.local/bin/binit').exists())
        self.assertTrue(state.is_dir())

    def test_reinstallation_repairs_legacy_receipt_directory_mode(self):
        self.assertEqual(self.cli().returncode, 0)
        self.receipt.parent.chmod(0o755)
        state = self.receipt.parent / 'state-fixture'
        state.write_bytes(b'preserved')
        original_receipt = self.receipt.read_bytes()

        self.assertEqual(self.cli().returncode, 0)
        self.assertEqual(self.receipt.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.receipt.read_bytes(), original_receipt)
        self.assertEqual(state.read_bytes(), b'preserved')

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
        self.rc.write_bytes(START + b'user\n' + END)
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

    def test_upgrades_schema_one_python_receipt(self):
        original = self.rc.read_bytes()
        old = b'binit 0.1.1 binary'
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(old)
        self.binary.chmod(0o755)
        self.receipt.parent.mkdir(parents=True)
        startup = f'eval "$({shlex.quote(str(self.binary))} init)"'
        block = b'\n' + START + startup.encode() + b'\n' + END
        self.rc.write_bytes(original + block)
        self.receipt.write_text(json.dumps({
            'schema': 1, 'binary': str(self.binary), 'sha256': hashlib.sha256(old).hexdigest(),
            'mode': 0o755, 'rc_file': str(self.rc), 'rc_block': base64.b64encode(block).decode(),
        }))
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.binary.read_bytes(), self.source.read_bytes())
        self.assertTrue(self.rc.read_bytes().startswith(original + b'\n'))
        self.assertEqual(self.cli('--uninstall').returncode, 0)
        self.assertEqual(self.rc.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
