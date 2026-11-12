import importlib.util
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("package_plugin", ROOT / "scripts/package_plugin.py")
package_plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package_plugin)


class PackagePluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".scratch-package-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.repo = self.path / "plugin"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "plugin.toml").write_text('schema=1\nid="example"\nentry="plugin.bash"\nload="startup"\n')
        (self.repo / "plugin.bash").write_text(':\n')
        (self.repo / "LICENSE").write_text('MIT\n')
        self.commit()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")

    def test_reproducible_archive_binds_commit_and_files(self):
        a = package_plugin.package(self.repo, self.path / "one")
        b = package_plugin.package(self.repo, self.path / "two")
        self.assertEqual(a, b)
        with tarfile.open(self.path / "one" / a["artifact"]) as archive:
            self.assertEqual(archive.getnames(), ["LICENSE", "plugin.bash", "plugin.toml"])
            self.assertEqual(archive.extractfile("plugin.bash").read(), b':\n')

    def test_dirty_repository_is_refused(self):
        (self.repo / "plugin.bash").write_text('changed\n')
        with self.assertRaisesRegex(ValueError, "uncommitted"):
            package_plugin.package(self.repo, self.path / "output")

    def test_tracked_symlink_is_refused(self):
        (self.repo / "link").symlink_to("plugin.bash")
        self.commit()
        with self.assertRaisesRegex(ValueError, "unsafe"):
            package_plugin.package(self.repo, self.path / "output")

    def test_secret_file_is_refused(self):
        (self.repo / ".env").write_text('FIXTURE=example\n')
        self.commit()
        with self.assertRaisesRegex(ValueError, "unsafe"):
            package_plugin.package(self.repo, self.path / "output")

    def test_assume_unchanged_worktree_bytes_are_not_packaged(self):
        self.git("update-index", "--assume-unchanged", "plugin.bash")
        (self.repo / "plugin.bash").write_text("LOCAL_OVERRIDE=private\n")
        result = package_plugin.package(self.repo, self.path / "output")
        with tarfile.open(self.path / "output" / result["artifact"]) as archive:
            self.assertEqual(archive.extractfile("plugin.bash").read(), b':\n')

    def test_oversized_blob_is_refused_before_reading(self):
        (self.repo / "large").write_bytes(b'x' * (2 * 1024 * 1024 + 1))
        self.commit()
        with self.assertRaisesRegex(ValueError, "limits"):
            package_plugin.package(self.repo, self.path / "output")

    def test_subdirectory_cannot_package_an_enclosing_repository(self):
        nested = self.repo / "nested"
        nested.mkdir()
        (nested / "placeholder").write_text("fixture\n")
        self.commit()
        with self.assertRaisesRegex(ValueError, "independent repository root"):
            package_plugin.package(nested, self.path / "output")

    def test_output_must_be_outside_source(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            package_plugin.package(self.repo, self.repo / "output")
