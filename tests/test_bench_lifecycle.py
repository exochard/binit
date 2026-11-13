"""Public benchmark integrity: real lifecycle behavior precedes timing acceptance."""
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('BINIT_TEST_BINARY', ROOT / 'target/release/binit'))


class LifecycleBenchmarkTests(unittest.TestCase):
    def run_benchmark(self, directory, *extra):
        output = directory / 'result.json'
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/bench_lifecycle.py'),
            '--binary', str(BINARY), '--samples', '1', '--runs', '1', '--output', str(output),
            *extra], capture_output=True, text=True, timeout=40)
        return result, output

    def test_actual_lifecycle_has_checked_pairs_and_frozen_identities(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-bench-lifecycle-', dir=ROOT) as temporary:
            result, output = self.run_benchmark(Path(temporary), '--compare-binary', str(BINARY),
                                                 '--cases', 'eager-0', 'eager-10', 'lazy-32')
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(output.read_text())
            self.assertTrue(receipt['complete'])
            self.assertFalse(receipt['release_qualified'])
            self.assertEqual(receipt['binary_sha256'], receipt['compare_binary_sha256'])
            self.assertEqual(len(receipt['comparison']), 10)
            self.assertEqual({r['variant'] for r in receipt['records']}, {'direct_bash', 'binit', 'binit_before'})
            self.assertTrue(all(r['milliseconds'] > 0 for r in receipt['records']))
            self.assertEqual(len(receipt['fixtures']['eager-10']), 21)
            self.assertNotIn(str(ROOT), output.read_text())

    def test_a_fast_noop_init_cannot_produce_an_accepted_receipt(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-bench-lifecycle-', dir=ROOT) as temporary:
            directory = Path(temporary)
            wrapper = directory / 'noop-manager'
            wrapper.write_text('#!/usr/bin/env bash\nif [[ $1 == init ]]; then printf ":\\n"; exit; fi\n'
                               'exec ' + shlex.quote(str(BINARY)) + ' "$@"\n')
            wrapper.chmod(0o700)
            result, output = self.run_benchmark(directory, '--binary', str(wrapper), '--cases', 'eager-10')
            self.assertNotEqual(result.returncode, 0)
            receipt = json.loads(output.read_text())
            self.assertFalse(receipt['complete'])
            self.assertFalse(receipt['release_qualified'])
            self.assertFalse(any(r['variant'] == 'binit' for r in receipt['records']))

    def test_existing_receipt_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-bench-lifecycle-', dir=ROOT) as temporary:
            directory = Path(temporary)
            output = directory / 'result.json'
            output.write_text('retained evidence\n')
            result, _ = self.run_benchmark(directory, '--cases', 'eager-0')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_text(), 'retained evidence\n')

    def bad_manager(self, directory, suffix):
        wrapper = directory / 'bad-manager'
        wrapper.write_text('#!/usr/bin/env bash\nif [[ $1 == init ]]; then\n' +
            shlex.quote(str(BINARY)) + ' "$@"\n' + suffix + '\nexit; fi\nexec ' +
            shlex.quote(str(BINARY)) + ' "$@"\n')
        wrapper.chmod(0o700)
        return wrapper

    def test_successful_init_output_does_not_mask_failure_status(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-bench-lifecycle-', dir=ROOT) as temporary:
            directory = Path(temporary)
            wrapper = self.bad_manager(directory, 'exit 37')
            result, output = self.run_benchmark(directory, '--binary', str(wrapper), '--cases', 'eager-0')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(json.loads(output.read_text())['complete'])

    def test_inherited_private_state_does_not_satisfy_empty_activation(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-bench-lifecycle-', dir=ROOT) as temporary:
            directory = Path(temporary)
            wrapper = directory / 'noop-manager'
            wrapper.write_text('#!/usr/bin/env bash\nif [[ $1 == init ]]; then printf ":\\n"; exit; fi\n'
                               'exec ' + shlex.quote(str(BINARY)) + ' "$@"\n')
            wrapper.chmod(0o700)
            with patch.dict(os.environ, {'__binit_active': '1'}):
                result, output = self.run_benchmark(directory, '--binary', str(wrapper), '--cases', 'eager-0')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(json.loads(output.read_text())['complete'])

    def test_unexported_value_does_not_satisfy_export_preservation(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-bench-lifecycle-', dir=ROOT) as temporary:
            directory = Path(temporary)
            extra = ('printf "%s\\n" "__bench_unexport() { export -n BINIT_BENCH_EXPORT; }" '
                     '"binit on after-load fixture-0 __bench_unexport"')
            wrapper = self.bad_manager(directory, extra)
            result, output = self.run_benchmark(directory, '--binary', str(wrapper), '--cases', 'lazy-32')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(json.loads(output.read_text())['complete'])

    def test_unload_must_preserve_a_foreign_function(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-bench-lifecycle-', dir=ROOT) as temporary:
            directory = Path(temporary)
            lines = ['bench_unrelated() { : sentinel; }',
                     '__bench_body=$(declare -f binit)', 'eval "__bench_real_${__bench_body}"',
                     'binit() { if [[ $1 == unload ]]; then unset -f bench_unrelated; fi; __bench_real_binit "$@"; }']
            extra = "printf '%s\\n' " + ' '.join(shlex.quote(line) for line in lines)
            wrapper = self.bad_manager(directory, extra)
            result, output = self.run_benchmark(directory, '--binary', str(wrapper), '--cases', 'eager-10')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(json.loads(output.read_text())['complete'])

    def test_prepare_timeout_stops_its_process_group(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        try:
            import bench_lifecycle
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory(prefix='.scratch-bench-lifecycle-', dir=ROOT) as temporary:
            directory = Path(temporary)
            marker = directory / 'child-survived'
            wrapper = directory / 'slow-manager'
            wrapper.write_text('#!/usr/bin/env bash\n(sleep .4; : > ' + shlex.quote(str(marker)) + ') &\nwait\n')
            wrapper.chmod(0o700)
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                bench_lifecycle.prepare(wrapper, directory / 'config', directory / 'state', dict(os.environ), timeout=.05)
            self.assertLess(time.monotonic() - started, .3)
            time.sleep(.45)
            self.assertFalse(marker.exists())

    def test_interrupted_checkpoint_preserves_previous_valid_json(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        try:
            import bench_lifecycle
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory(prefix='.scratch-bench-lifecycle-', dir=ROOT) as temporary:
            output = Path(temporary) / 'result.json'
            bench_lifecycle.persist(output, {'complete': False, 'records': [1]}, exclusive=True)
            with patch.object(bench_lifecycle.os, 'replace', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    bench_lifecycle.persist(output, {'complete': True, 'records': [1, 2]})
            self.assertEqual(json.loads(output.read_text()), {'complete': False, 'records': [1]})
            with self.assertRaises(FileExistsError):
                bench_lifecycle.persist(output, {'complete': True}, exclusive=True)

    def test_directory_sync_failure_cannot_publish_a_complete_receipt(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        try:
            import bench_lifecycle
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory(prefix='.scratch-bench-lifecycle-', dir=ROOT) as temporary:
            output = Path(temporary) / 'result.json'
            bench_lifecycle.persist(output, {'complete': False}, exclusive=True)
            real_fsync = os.fsync
            def fail_directory(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError('directory synchronization failed')
                return real_fsync(fd)
            with patch.object(bench_lifecycle.os, 'fsync', side_effect=fail_directory):
                with self.assertRaises(OSError):
                    bench_lifecycle.persist(output, {'complete': True})
            self.assertFalse(json.loads(output.read_text())['complete'])

    def test_every_warm_command_must_preserve_its_status(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-bench-lifecycle-', dir=ROOT) as temporary:
            directory = Path(temporary)
            lines = ['__bench_poison_warm() {',
                     '__bench_saved=$(declare -f bench_command)',
                     'eval "__bench_real_${__bench_saved}"', '__bench_calls=0',
                     'bench_command() { ((__bench_calls++)); '
                     'if (( __bench_calls == 1 || __bench_calls == 1001 )); then '
                     'if (( __bench_calls == 1001 )); then eval "$__bench_saved"; fi; '
                     '__bench_real_bench_command "$@"; else return 0; fi; }', '}',
                     'binit on after-load fixture-0 __bench_poison_warm']
            extra = "printf '%s\\n' " + ' '.join(shlex.quote(line) for line in lines)
            wrapper = self.bad_manager(directory, extra)
            result, output = self.run_benchmark(directory, '--binary', str(wrapper), '--cases', 'lazy-32')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(json.loads(output.read_text())['complete'])

    def test_changed_executable_identity_invalidates_the_run(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-bench-lifecycle-', dir=ROOT) as temporary:
            directory = Path(temporary)
            wrapper = directory / 'changing-manager'
            wrapper.write_text('#!/usr/bin/env bash\nif [[ $1 == prepare ]]; then printf "# changed\\n" >> "$0"; fi\n'
                               'exec ' + shlex.quote(str(BINARY)) + ' "$@"\n')
            wrapper.chmod(0o700)
            result, output = self.run_benchmark(directory, '--binary', str(wrapper), '--cases', 'eager-0')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(json.loads(output.read_text())['complete'])

    def test_missing_binary_still_leaves_incomplete_evidence(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-bench-lifecycle-', dir=ROOT) as temporary:
            directory = Path(temporary)
            result, output = self.run_benchmark(directory, '--binary', str(directory / 'missing'), '--cases', 'eager-0')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(json.loads(output.read_text())['complete'])
