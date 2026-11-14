"""Source bundles preserve committed inputs and explicit plugin selection."""
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from package_sources import package


class SourcePackageTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / 'tmp'
        scratch.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='package-sources-test-', dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.core = self.repository('core', {
            'Cargo.toml': '[package]\nname="fixture"\n', 'Cargo.lock': '# fixture\n',
            'LICENSE': 'MIT\n', 'scripts/demo.bash': '#!/bin/bash\nprintf fixture\n'})
        self.plugin = self.repository('plugin', {
            'plugin.toml': 'schema=1\nid="fixture"\nentry="plugin.bash"\nload="startup"\n',
            'plugin.bash': ':\n', 'LICENSE': 'MIT\n'})

    def git(self, repository, *args):
        return subprocess.run(['git', '-C', str(repository), *args], check=True, capture_output=True)

    def commit(self, repository):
        self.git(repository, 'add', '.')
        self.git(repository, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')

    def repository(self, name, files):
        repository = self.work / name
        repository.mkdir()
        self.git(repository, 'init', '-q')
        for name, content in files.items():
            path = repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            if name.endswith('.bash'):
                path.chmod(0o755)
        self.commit(repository)
        return repository

    def test_reproducible_core_and_explicit_plugins_preserve_commits_and_modes(self):
        (self.core / 'untracked-personal-file').write_text('not exported\n')
        first = package(self.core, [self.plugin], self.work / 'one')
        second = package(self.core, [self.plugin], self.work / 'two')
        self.assertEqual(first, second)
        self.assertTrue(first['complete'])
        self.assertEqual([p['id'] for p in first['plugins']], ['fixture'])
        subprocess.run(['sha256sum', '-c', 'SHA256SUMS'], cwd=self.work / 'one',
                       check=True, capture_output=True)
        self.assertEqual(first['core']['commit'], self.git(self.core, 'rev-parse', 'HEAD').stdout.decode().strip())
        with tarfile.open(self.work / 'one' / first['core']['artifact']) as archive:
            self.assertEqual(set(archive.getnames()), {'Cargo.toml', 'Cargo.lock', 'LICENSE', 'scripts/demo.bash'})
            self.assertEqual(archive.getmember('scripts/demo.bash').mode, 0o755)
            self.assertEqual(archive.extractfile('scripts/demo.bash').read(), b'#!/bin/bash\nprintf fixture\n')
        with tarfile.open(self.work / 'one' / first['plugins'][0]['artifact']) as archive:
            self.assertEqual(archive.getmember('plugin.bash').mode, 0o755)
        core_only = package(self.core, [], self.work / 'core-only')
        self.assertEqual(core_only['plugins'], [])

    def test_worktree_override_cannot_replace_committed_source(self):
        self.git(self.core, 'update-index', '--assume-unchanged', 'scripts/demo.bash')
        (self.core / 'scripts/demo.bash').write_text('PRIVATE_OVERRIDE\n')
        result = package(self.core, [], self.work / 'out')
        with tarfile.open(self.work / 'out' / result['core']['artifact']) as archive:
            self.assertNotIn(b'PRIVATE_OVERRIDE', archive.extractfile('scripts/demo.bash').read())

    def test_dirty_core_retains_incomplete_receipt_and_existing_output_is_refused(self):
        (self.core / 'LICENSE').write_text('changed\n')
        output = self.work / 'out'
        with self.assertRaisesRegex(ValueError, 'uncommitted tracked'):
            package(self.core, [], output)
        data = (output / 'sources.json').read_bytes()
        receipt = json.loads(data)
        self.assertFalse(receipt['complete'])
        self.assertIn('uncommitted', receipt['error'])
        with self.assertRaises(FileExistsError):
            package(self.core, [], output)
        self.assertEqual((output / 'sources.json').read_bytes(), data)

    def test_symlinks_and_secret_files_are_rejected(self):
        for name in ('.env', 'link'):
            repository = self.repository('unsafe-' + name, {'LICENSE': 'MIT\n'})
            if name == 'link':
                (repository / name).symlink_to('LICENSE')
            else:
                (repository / name).write_text('FIXTURE=example\n')
            self.commit(repository)
            with self.assertRaisesRegex(ValueError, 'unsafe core source'):
                package(repository, [], self.work / ('out-' + name))

    def test_failed_or_duplicate_plugin_selection_never_completes_bundle(self):
        output = self.work / 'duplicate'
        other = self.repository('other', {
            'plugin.toml': 'schema=1\nid="fixture"\nentry="plugin.bash"\nload="startup"\n',
            'plugin.bash': 'true\n', 'LICENSE': 'MIT\n'})
        with self.assertRaisesRegex(ValueError, 'duplicate selected plugin'):
            package(self.core, [self.plugin, other], output)
        self.assertFalse(json.loads((output / 'sources.json').read_text())['complete'])
        (self.plugin / 'plugin.bash').write_text('dirty\n')
        with self.assertRaisesRegex(ValueError, 'uncommitted'):
            package(self.core, [self.plugin], self.work / 'dirty-plugin')
        self.assertFalse(json.loads((self.work / 'dirty-plugin/sources.json').read_text())['complete'])

    def test_nested_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'repository root'):
            package(self.core / 'scripts', [], self.work / 'out')
