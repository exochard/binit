import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("engine", ROOT / "scripts/engine.py")
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=".scratch-engine-test-", dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.source = self.cache / "source-5.3.20"
        self.source.mkdir()
        (self.source / "example.c").write_text("original\n")
        locks = self.root / "engines/bash"
        locks.mkdir(parents=True)
        lock = locks / "source.json"
        lock.write_text(json.dumps({"version": "5.3.20", "files": [], "signing_fingerprints": []}))
        (locks / "tree.sha256").write_text(engine.source_digest(self.source) + "\n")
        for name, value in (("ROOT", self.root), ("CACHE", self.cache), ("LOCK", lock)):
            patcher = patch.object(engine, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_unchanged_cached_source_is_accepted(self):
        self.assertEqual(engine.source()[0], self.source)

    def test_changed_cached_source_is_rejected(self):
        (self.source / "example.c").write_text("changed\n")
        with self.assertRaisesRegex(ValueError, "cached source tree differs"):
            engine.source()

    def test_invalid_variant_options_do_not_create_builds(self):
        for name in ("../outside", "/absolute", "reference/child", ".hidden", ""):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "invalid build name"):
                engine.build("pgo", name=name)
            with self.subTest(test_name=name), self.assertRaisesRegex(ValueError, "invalid build name"):
                engine.test(name)
        for kind in ("reference", "profile"):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "fixed compiler flags"):
                engine.build(kind, name="altered", optimization="O3")
        with self.assertRaisesRegex(ValueError, "optimization must"):
            engine.build("pgo", name="altered", optimization="Ofast")
        self.assertEqual(list(self.cache.iterdir()), [self.source])

    def test_existing_partial_builds_and_directory_symlinks_are_preserved(self):
        partial = self.cache / "partial"
        partial.mkdir()
        sentinel = partial / "unfinished.o"
        sentinel.write_bytes(b"previous build")
        outside = self.root / "outside"
        outside.mkdir()
        (self.cache / "linked").symlink_to(outside, target_is_directory=True)
        for name in ("partial", "linked"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "fresh build name"):
                engine.build("pgo", name=name, optimization="O2")
        self.assertEqual(sentinel.read_bytes(), b"previous build")
        self.assertEqual(list(outside.iterdir()), [])

    def test_cli_rejects_an_explicitly_empty_test_variant(self):
        with patch("sys.argv", ["engine.py", "test", "--name", ""]):
            with self.assertRaisesRegex(ValueError, "invalid build name"):
                engine.main()

    def test_upstream_zero_exit_with_diff_is_failure(self):
        directory = self.cache / "reference"
        directory.mkdir()
        (directory / "build.json").write_text('{"cflags":"-O2 -g"}')
        locales = self.cache / "locales"
        locales.mkdir()
        result = subprocess.CompletedProcess([], 0, stdout="run-example\n1c1\n< actual\n---\n> expected\n", stderr="")
        with patch.object(engine, "run", return_value=result), patch("builtins.print"), patch.object(
                engine, "prepare_locales", return_value=locales):
            with self.assertRaisesRegex(ValueError, "upstream tests reported differences"):
                engine.test("reference")


if __name__ == "__main__":
    unittest.main()
