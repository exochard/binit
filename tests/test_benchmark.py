import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("benchmark", ROOT / "scripts/benchmark.py")
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.suite, self.lock = benchmark.load_lock()

    def result(self):
        source = ROOT / "engines/bash/source.json"
        provenance = {
            "version": json.loads(source.read_text())["version"],
            "source_lock_sha256": benchmark.file_sha(source),
            "source_tree_sha256": (ROOT / "engines/bash/tree.sha256").read_text().strip(),
            "configure": ["--without-bash-malloc"],
        }
        engines = {
            "reference": {"sha256": "1" * 64, "build": {
                **provenance, "kind": "reference", "cflags": "-O2 -g", "binary_sha256": "1" * 64}},
            "candidate": {"sha256": "2" * 64, "build": {
                **provenance, "kind": "pgo", "binary_sha256": "2" * 64,
                "training_sha256": benchmark.file_sha(ROOT / "engines/bash/train.bash")}},
            "system": {"path": str(Path("/bin/bash").resolve()),
                       "sha256": benchmark.file_sha(Path("/bin/bash"))},
        }
        records = [{"run": run, "sample": sample, "case": case["name"],
                    "seconds": {"reference": 1.0, "system": 1.0, "candidate": 0.7}}
                   for run in range(3) for sample in range(30) for case in self.lock["cases"]]
        return {"suite_lock_sha256": benchmark.file_sha(benchmark.LOCK), "engines": engines,
                "runs": 3, "samples_per_run": 30, "finished": "test", "records": records,
                "reference_exports_preserved": True}

    def test_complete_qualified_result_passes(self):
        self.assertTrue(benchmark.score(self.result())["release_gate_pass"])

    def test_timing_and_sample_requirements_are_hard_gates(self):
        for edit in ("too_slow", "short", "missing", "duplicate", "unfinished"):
            with self.subTest(edit=edit):
                result = self.result()
                if edit == "too_slow":
                    for row in result["records"]:
                        row["seconds"]["candidate"] = 0.8
                elif edit == "short":
                    result["samples_per_run"] = 3
                    result["records"] = [row for row in result["records"] if row["sample"] < 3]
                elif edit == "missing":
                    result["records"].pop()
                elif edit == "duplicate":
                    result["records"].append(copy.deepcopy(result["records"][0]))
                else:
                    del result["finished"]
                self.assertFalse(benchmark.score(result)["release_gate_pass"])

    def test_provenance_mismatches_fail(self):
        for edit in ("binary", "kind", "source", "tree", "flags", "system", "training", "exports"):
            with self.subTest(edit=edit):
                result = self.result()
                reference = result["engines"]["reference"]["build"]
                if edit == "binary":
                    reference["binary_sha256"] = "3" * 64
                elif edit == "kind":
                    reference["kind"] = "pgo"
                elif edit == "source":
                    reference["source_lock_sha256"] = "3" * 64
                elif edit == "tree":
                    reference["source_tree_sha256"] = "3" * 64
                elif edit == "flags":
                    reference["cflags"] = "-O0"
                elif edit == "system":
                    result["engines"]["system"]["path"] = "/custom/bash"
                elif edit == "training":
                    result["engines"]["candidate"]["build"]["training_sha256"] = "3" * 64
                else:
                    result["reference_exports_preserved"] = False
                self.assertFalse(benchmark.score(result)["release_gate_pass"])

    def test_repeatable_family_regression_fails_even_with_large_total_gain(self):
        result = self.result()
        names = {case["name"] for case in self.lock["cases"] if case["family"] == "filesystem"}
        for row in result["records"]:
            row["seconds"]["candidate"] = 1.06 if row["case"] in names else 0.5
        report = benchmark.score(result)
        self.assertFalse(report["release_gate_pass"])
        self.assertEqual(report["baselines"]["reference"]["regressed_families"], ["filesystem"])

    def test_too_short_baseline_fails(self):
        result = self.result()
        for row in result["records"]:
            row["seconds"] = {"reference": 0.1, "system": 0.1, "candidate": 0.05}
        self.assertFalse(benchmark.score(result)["release_gate_pass"])

    def test_invalid_durations_are_rejected(self):
        for value in (0, -1, float("nan"), float("inf"), True):
            result = self.result()
            result["records"][0]["seconds"]["candidate"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                benchmark.score(result)

    def test_directory_modes_are_observable(self):
        with tempfile.TemporaryDirectory(prefix=".scratch-benchmark-test-", dir=ROOT) as name:
            directory = Path(name)
            before = benchmark.tree(directory)
            directory.chmod(0o777)
            self.assertNotEqual(before, benchmark.tree(directory))
            directory.chmod(0o700)
            child = directory / "child"
            child.mkdir()
            before = benchmark.tree(directory)
            child.chmod(0o777)
            self.assertNotEqual(before, benchmark.tree(directory))

    def test_changed_inputs_are_rejected_before_another_engine_runs(self):
        with tempfile.TemporaryDirectory(prefix=".scratch-benchmark-test-", dir=ROOT) as name:
            directory = Path(name)
            data = benchmark.fixture(directory)
            _, expected = benchmark.invoke(Path("/bin/bash"), "arithmetic-sum", 1, directory, data)
            lock = {"cases": [{"name": "arithmetic-sum", "count": 1, "expected": expected}],
                    "fixture_sha256": benchmark.tree(data)}
            wrapper = directory / "wrapper"
            wrapper.write_text('#!/bin/bash\nif [[ $1 == --version ]]; then exec /bin/bash "$@"; fi\n'
                               '/bin/bash "$@"\nstatus=$?\nchmod 0777 "$DATA_DIR/records.txt"\nexit "$status"\n')
            wrapper.chmod(0o700)
            with patch.object(benchmark, "load_lock", return_value=(self.suite, lock)):
                with self.assertRaisesRegex(ValueError, "input fixture was modified"):
                    benchmark.measure({"candidate": wrapper}, directory / "result.json", 1, 1)


if __name__ == "__main__":
    unittest.main()
