"""Checks for the source boundary of the CI Bash builder."""

import errno
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
import urllib.error
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("ci_bash", ROOT / "scripts/ci_bash.py")
ci_bash = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_bash)


class DownloadTests(unittest.TestCase):
    def test_matching_archive_is_written(self):
        body = b"known archive bytes"
        digest = "4071644df698372efb48caf3c44ad19684bbaeda8a375673d414d4dfb84ab0fa"
        with tempfile.TemporaryDirectory(prefix=".scratch-ci-", dir=ROOT) as temporary:
            target = Path(temporary) / "source.tar.gz"
            with patch.dict(ci_bash.SOURCES, {"test": ("test", digest)}):
                with patch.object(ci_bash.urllib.request, "urlopen", return_value=io.BytesIO(body)):
                    url, actual = ci_bash.download("test", target)
            self.assertEqual(target.read_bytes(), body)
            self.assertEqual(actual, digest)
            self.assertEqual(url, "https://ftp.gnu.org/gnu/bash/bash-test.tar.gz")

    def test_unreachable_primary_uses_a_checksum_pinned_mirror(self):
        body = b"known archive bytes"
        checksum = "4071644df698372efb48caf3c44ad19684bbaeda8a375673d414d4dfb84ab0fa"
        offline = urllib.error.URLError(OSError(errno.ENETUNREACH, "Network is unreachable"))
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as directory:
            target = Path(directory) / "source.tar.gz"
            with patch.dict(ci_bash.SOURCES, {"test": ("test", checksum)}), patch.object(
                ci_bash.urllib.request, "urlopen", side_effect=[offline, io.BytesIO(body)]
            ) as fetch:
                url, actual = ci_bash.download("test", target)
            self.assertEqual(target.read_bytes(), body)
            self.assertEqual(actual, checksum)
            self.assertEqual(url, "https://mirrors.kernel.org/gnu/bash/bash-test.tar.gz")
            self.assertEqual(fetch.call_args_list[1].args[0].full_url, url)

    def test_bad_checksum_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory(prefix=".scratch-ci-", dir=ROOT) as temporary:
            target = Path(temporary) / "source.tar.gz"
            with patch.object(ci_bash.urllib.request, "urlopen", return_value=io.BytesIO(b"bad")):
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    ci_bash.download("5.2", target)
            self.assertFalse(target.exists())

    def test_large_archive_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory(prefix=".scratch-ci-", dir=ROOT) as temporary:
            target = Path(temporary) / "source.tar.gz"
            with patch.object(ci_bash, "MAX_ARCHIVE", 2):
                with patch.object(ci_bash.urllib.request, "urlopen", return_value=io.BytesIO(b"large")):
                    with self.assertRaisesRegex(ValueError, "exceeds"):
                        ci_bash.download("5.2", target)
            self.assertFalse(target.exists())

    def test_archive_cannot_escape_destination(self):
        with tempfile.TemporaryDirectory(prefix=".scratch-ci-", dir=ROOT) as temporary:
            directory = Path(temporary)
            def fake_download(_version, path):
                with tarfile.open(path, "w:gz") as archive:
                    member = tarfile.TarInfo("../../escape")
                    member.size = 1
                    archive.addfile(member, io.BytesIO(b"x"))
                return "fixture", "fixture"
            with patch.object(ci_bash, "download", side_effect=fake_download):
                with self.assertRaises(tarfile.FilterError):
                    ci_bash.build("5.2", directory / "build")
            self.assertFalse((directory.parent / "escape").exists())


if __name__ == "__main__":
    unittest.main()
