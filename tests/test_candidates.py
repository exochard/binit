"""Candidate preparation must not select unverified shell changes."""
import json
import fcntl
import hashlib
import os
import shlex
import shutil
import signal
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('BINIT_TEST_BINARY', ROOT / 'target/release/binit'))


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='.scratch-candidates-', dir=ROOT)
        self.root = Path(self.temporary.name)
        self.state = self.root / 'state'
        self.plugin = self.root / 'plugin'
        self.plugin.mkdir()
        (self.plugin / 'plugin.toml').write_text(
            "schema=1\nid='sample'\nentry='init.bash'\nload='startup'\n")
        self.entry = self.plugin / 'init.bash'
        self.entry.write_text('greet() { printf old; }\n')
        self.config = self.root / 'config.toml'
        self.config.write_text('schema=1\n[[plugins]]\npath="plugin"\n')
        self.environment = dict(os.environ, HOME=str(self.root), INPUTRC='/dev/null')
        for name in ('BASH_ENV', 'ENV'):
            self.environment.pop(name, None)

    def tearDown(self):
        self.temporary.cleanup()

    def run_binit(self, *arguments):
        return subprocess.run([str(BINARY), *arguments, '--state', str(self.state)],
                              capture_output=True, text=True, env=self.environment,
                              cwd=self.root, timeout=15)

    def success(self, *arguments):
        result = self.run_binit(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def checks(self, script, *, setup=None, timeout_ms=2000, max_elapsed_ms=1000):
        (self.root / 'check.bash').write_text(script)
        body = ('schema=1\n[[checks]]\nid="greet"\nscript="check.bash"\n'
                f'timeout_ms={timeout_ms}\nmax_elapsed_ms={max_elapsed_ms}\n')
        if setup is not None:
            (self.root / 'setup.bash').write_text(setup)
            body += 'setup="setup.bash"\n'
        plan = self.root / 'checks.toml'
        plan.write_text(body)
        return plan

    def prepared_change(self):
        self.success('prepare', str(self.config))
        before = (self.state / 'current').read_text()
        self.entry.write_text('greet() { printf candidate; }\n')
        self.success('candidate', 'prepare', str(self.config))
        candidate = json.loads(self.success('candidate', 'status', '--json'))['generation']
        return before, candidate

    def test_preparing_candidate_preserves_current_and_previous_and_snapshots_new_code(self):
        self.success('prepare', str(self.config))
        self.entry.write_text('greet() { printf current; }\n')
        self.success('prepare', str(self.config))
        current = (self.state / 'current').read_text()
        previous = (self.state / 'previous').read_bytes() if (self.state / 'previous').exists() else None
        self.entry.write_text('greet() { printf candidate; }\n')
        self.success('candidate', 'prepare', str(self.config))
        status = json.loads(self.success('candidate', 'status', '--json'))
        self.assertEqual(status['base_generation'], current)
        self.assertEqual(status['config_sha256'], hashlib.sha256(self.config.read_bytes()).hexdigest())
        self.assertNotEqual(status['generation'], current)
        self.assertEqual((self.state / 'current').read_text(), current)
        self.assertEqual((self.state / 'previous').read_bytes()
                         if (self.state / 'previous').exists() else None, previous)
        snapshot = self.state / 'generations' / status['generation'] / 'plugins/sample/init.bash'
        self.assertIn('printf candidate', snapshot.read_text())
        self.success('status')

    def test_first_candidate_leaves_selection_absent(self):
        self.success('candidate', 'prepare', str(self.config))
        status = json.loads(self.success('candidate', 'status', '--json'))
        self.assertIsNone(status['base_generation'])
        self.assertFalse((self.state / 'current').exists())
        self.assertFalse((self.state / 'previous').exists())

    def test_failed_preparation_preserves_existing_candidate_and_current(self):
        self.success('prepare', str(self.config))
        current = (self.state / 'current').read_text()
        self.entry.write_text('greet() { printf candidate; }\n')
        self.success('candidate', 'prepare', str(self.config))
        before = json.loads(self.success('candidate', 'status', '--json'))
        (self.plugin / 'plugin.toml').write_text("schema=1\nid='invalid name'\n")
        failed = self.run_binit('candidate', 'prepare', str(self.config))
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(json.loads(self.success('candidate', 'status', '--json')), before)
        self.assertEqual((self.state / 'current').read_text(), current)
        self.success('status')

    def test_candidate_status_rejects_changed_generation_content(self):
        self.success('candidate', 'prepare', str(self.config))
        status = json.loads(self.success('candidate', 'status', '--json'))
        snapshot = self.state / 'generations' / status['generation'] / 'runtime.bash'
        snapshot.write_text(': changed\n')
        failed = self.run_binit('candidate', 'status', '--json')
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('content changed', failed.stderr)

    def test_candidate_status_refuses_symlink_record_without_changing_its_target(self):
        self.success('candidate', 'prepare', str(self.config))
        record = self.state / 'candidate.json'
        saved = self.root / 'saved-candidate.json'
        record.rename(saved)
        before = saved.read_bytes()
        record.symlink_to(saved)
        failed = self.run_binit('candidate', 'status', '--json')
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('private regular file', failed.stderr)
        self.assertEqual(saved.read_bytes(), before)

    def test_candidate_status_refuses_nonprivate_record(self):
        self.success('candidate', 'prepare', str(self.config))
        record = self.state / 'candidate.json'
        before = record.read_bytes()
        record.chmod(0o644)
        failed = self.run_binit('candidate', 'status', '--json')
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('private regular file', failed.stderr)
        self.assertEqual(record.read_bytes(), before)

    def test_prepare_refuses_lock_contention_without_changing_current_or_candidate(self):
        self.success('prepare', str(self.config))
        self.success('candidate', 'prepare', str(self.config))
        before = json.loads(self.success('candidate', 'status', '--json'))
        current = (self.state / 'current').read_text()
        self.entry.write_text('greet() { printf blocked; }\n')
        with (self.state / '.mutation.lock').open('rb') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            failed = self.run_binit('candidate', 'prepare', str(self.config))
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('another Binit change', failed.stderr)
        self.assertEqual(json.loads(self.success('candidate', 'status', '--json')), before)
        self.assertEqual((self.state / 'current').read_text(), current)

    def test_select_requires_successful_checks_before_changing_current(self):
        before, candidate = self.prepared_change()
        rejected = self.run_binit('candidate', 'select')
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual((self.state / 'current').read_text(), before)
        plan = self.checks('[[ $(greet) == candidate ]]\n'
                           '[[ $(binit status) == *sample* ]]\n')
        self.success('candidate', 'check', str(plan))
        checked = json.loads(self.success('candidate', 'status', '--json'))
        self.assertEqual(checked['state'], 'checked')
        self.assertEqual((self.state / 'current').read_text(), before)
        self.success('candidate', 'select')
        self.assertEqual((self.state / 'current').read_text(), candidate)
        self.assertEqual((self.state / 'previous').read_text(), before)
        self.assertEqual(json.loads(self.success('candidate', 'status', '--json'))['state'], 'selected')

    def test_failed_check_preserves_current_and_keeps_failure_evidence(self):
        before, _ = self.prepared_change()
        plan = self.checks('[[ $(greet) == deliberately_wrong ]]\n')
        failed = self.run_binit('candidate', 'check', str(plan))
        self.assertNotEqual(failed.returncode, 0)
        status = json.loads(self.success('candidate', 'status', '--json'))
        self.assertEqual(status['state'], 'failed')
        self.assertIsNotNone(status['check']['receipt_sha256'])
        self.assertNotEqual(self.run_binit('candidate', 'select').returncode, 0)
        self.assertEqual((self.state / 'current').read_text(), before)

    def test_changed_check_script_invalidates_previously_successful_receipt(self):
        before, _ = self.prepared_change()
        plan = self.checks('[[ $(greet) == candidate ]]\n')
        self.success('candidate', 'check', str(plan))
        (self.root / 'check.bash').write_text('exit 99\n')
        rejected = self.run_binit('candidate', 'select')
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('changed', rejected.stderr)
        self.assertEqual((self.state / 'current').read_text(), before)

    def test_new_failed_check_attempt_invalidates_previous_success(self):
        before, _ = self.prepared_change()
        plan = self.checks('[[ $(greet) == candidate ]]\n')
        self.success('candidate', 'check', str(plan))
        plan.write_text('schema=99\n')
        self.assertNotEqual(self.run_binit('candidate', 'check', str(plan)).returncode, 0)
        self.assertNotEqual(self.run_binit('candidate', 'select').returncode, 0)
        self.assertEqual((self.state / 'current').read_text(), before)

    def test_setup_cannot_exit_successfully_before_candidate_and_check_run(self):
        before, _ = self.prepared_change()
        plan = self.checks('exit 1\n', setup='exit 0\n')
        self.assertNotEqual(self.run_binit('candidate', 'check', str(plan)).returncode, 0)
        self.assertEqual(json.loads(self.success('candidate', 'status', '--json'))['state'], 'failed')
        self.assertNotEqual(self.run_binit('candidate', 'select').returncode, 0)
        self.assertEqual((self.state / 'current').read_text(), before)

    def test_elapsed_budget_failure_keeps_current_selected(self):
        before, _ = self.prepared_change()
        plan = self.checks('sleep 0.15\n', timeout_ms=1000, max_elapsed_ms=40)
        self.assertNotEqual(self.run_binit('candidate', 'check', str(plan)).returncode, 0)
        status = json.loads(self.success('candidate', 'status', '--json'))
        self.assertEqual(status['state'], 'failed')
        self.assertNotEqual(self.run_binit('candidate', 'select').returncode, 0)
        self.assertEqual((self.state / 'current').read_text(), before)

    def test_checker_manager_calls_use_private_state_view(self):
        before, _ = self.prepared_change()
        plan = self.checks('binit apply || exit 93\n[[ $(greet) == candidate ]]\n')
        self.success('candidate', 'check', str(plan))
        self.assertEqual((self.state / 'current').read_text(), before)

    def test_changed_selected_reference_rejects_check_receipt(self):
        _, candidate = self.prepared_change()
        plan = self.checks('[[ $(greet) == candidate ]]\n')
        self.success('candidate', 'check', str(plan))
        self.entry.write_text('greet() { printf intervening; }\n')
        self.success('prepare', str(self.config))
        intervening = (self.state / 'current').read_text()
        self.assertNotEqual(intervening, candidate)
        self.assertNotEqual(self.run_binit('candidate', 'select').returncode, 0)
        self.assertEqual((self.state / 'current').read_text(), intervening)

    def checked_change(self, **options):
        before, candidate = self.prepared_change()
        plan = self.checks('[[ $(greet) == candidate ]]\n', **options)
        self.success('candidate', 'check', str(plan))
        return before, candidate, plan

    def test_configuration_change_requires_fresh_preparation(self):
        before, _, _ = self.checked_change()
        self.config.write_text(self.config.read_text() + '\n# changed\n')
        self.assertEqual(json.loads(self.success('candidate', 'status', '--json'))['state'], 'stale')
        self.assertNotEqual(self.run_binit('candidate', 'select').returncode, 0)
        self.assertEqual((self.state / 'current').read_text(), before)

    def test_setup_and_declared_resource_changes_invalidate_acceptance(self):
        before, _ = self.prepared_change()
        resource = self.root / 'fixture.txt'
        resource.write_text('original\n')
        plan = self.checks('[[ $(greet) == candidate ]]\n', setup=': setup\n')
        plan.write_text(plan.read_text().replace('schema=1', 'schema=1\nfiles=["fixture.txt"]'))
        self.success('candidate', 'check', str(plan))
        resource.write_text('changed\n')
        self.assertNotEqual(self.run_binit('candidate', 'select').returncode, 0)
        resource.write_text('original\n')
        (self.root / 'setup.bash').write_text(': changed\n')
        self.assertNotEqual(self.run_binit('candidate', 'select').returncode, 0)
        self.assertEqual((self.state / 'current').read_text(), before)

    def test_elapsed_policy_change_invalidates_acceptance(self):
        before, _, plan = self.checked_change()
        plan.write_text(plan.read_text().replace('max_elapsed_ms=1000', 'max_elapsed_ms=1'))
        self.assertNotEqual(self.run_binit('candidate', 'select').returncode, 0)
        self.assertEqual((self.state / 'current').read_text(), before)

    def test_modified_receipt_is_rejected(self):
        before, _, _ = self.checked_change()
        record = json.loads((self.state / 'candidate.json').read_text())
        receipt = self.state / 'check-runs' / record['check']['id'] / 'receipt.json'
        receipt.write_text(receipt.read_text() + '\n')
        rejected = self.run_binit('candidate', 'select')
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('receipt changed', rejected.stderr)
        self.assertEqual((self.state / 'current').read_text(), before)

    def test_explicit_native_disabled_checks_and_idempotent_selection(self):
        before, candidate = self.prepared_change()
        plan = self.checks('! builtin binit_complete status >/dev/null 2>&1\n')
        plan.write_text(plan.read_text().replace('schema=1', 'schema=1\nnative=false'))
        self.success('candidate', 'check', str(plan))
        self.success('candidate', 'select')
        self.success('candidate', 'select')
        self.assertEqual((self.state / 'current').read_text(), candidate)
        self.assertEqual((self.state / 'previous').read_text(), before)

    def test_input_symlink_is_refused_before_execution(self):
        before, _ = self.prepared_change()
        plan = self.checks('exit 0\n')
        script = self.root / 'check.bash'
        target = self.root / 'actual.bash'
        script.rename(target)
        script.symlink_to(target)
        rejected = self.run_binit('candidate', 'check', str(plan))
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('symlinks', rejected.stderr)
        self.assertNotEqual(self.run_binit('candidate', 'select').returncode, 0)
        self.assertEqual((self.state / 'current').read_text(), before)

    def test_mutated_snapshot_cannot_earn_acceptance(self):
        before, _ = self.prepared_change()
        plan = self.checks('chmod u+w "$BINIT_CHECK_INPUTS/check.bash"\n'
                           'printf "\\n# edited\\n" >> "$BINIT_CHECK_INPUTS/check.bash"\n')
        rejected = self.run_binit('candidate', 'check', str(plan))
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('snapshot changed', rejected.stderr)
        self.assertNotEqual(self.run_binit('candidate', 'select').returncode, 0)
        self.assertEqual((self.state / 'current').read_text(), before)

    def test_mutated_private_generation_never_modifies_real_snapshot(self):
        before, candidate = self.prepared_change()
        original = self.state / 'generations' / candidate / 'runtime.bash'
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        plan = self.checks('printf "\\n# edited\\n" >> "$__binit_generation_dir/runtime.bash"\n')
        self.assertNotEqual(self.run_binit('candidate', 'check', str(plan)).returncode, 0)
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), digest)
        self.assertEqual((self.state / 'current').read_text(), before)

    def test_timeout_stops_ordinary_background_children_and_preserves_current(self):
        before, _ = self.prepared_change()
        marker = self.root / 'late-marker'
        plan = self.checks(f'(sleep 0.35; touch {shlex.quote(str(marker))}) &\nsleep 5\n',
                           timeout_ms=100, max_elapsed_ms=100)
        rejected = self.run_binit('candidate', 'check', str(plan))
        self.assertNotEqual(rejected.returncode, 0)
        time.sleep(0.4)
        self.assertFalse(marker.exists())
        record = json.loads((self.state / 'candidate.json').read_text())
        receipt = json.loads((self.state / 'check-runs' / record['check']['id'] / 'receipt.json').read_text())
        self.assertTrue(receipt['samples'][0]['timed_out'])
        self.assertEqual((self.state / 'current').read_text(), before)

    def test_interrupt_invalidates_earlier_acceptance(self):
        before, _, plan = self.checked_change()
        marker = self.root / 'running'
        (self.root / 'check.bash').write_text(f'touch {shlex.quote(str(marker))}\nsleep 5\n')
        with subprocess.Popen([str(BINARY), 'candidate', 'check', str(plan), '--state', str(self.state)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              env=self.environment, cwd=self.root) as child:
            deadline = time.monotonic() + 5
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            child.send_signal(signal.SIGTERM)
            _, stderr = child.communicate(timeout=5)
            self.assertNotEqual(child.returncode, 0, stderr)
        self.assertNotEqual(self.run_binit('candidate', 'select').returncode, 0)
        self.assertEqual((self.state / 'current').read_text(), before)

    def test_bash_binary_change_invalidates_acceptance(self):
        bash = self.root / 'check bash Ω'
        shutil.copy2(self.environment.get('BINIT_BASH', '/bin/bash'), bash)
        self.environment['BINIT_BASH'] = str(bash)
        before, _, _ = self.checked_change()
        with bash.open('ab') as binary:
            binary.write(b'changed')
        self.assertNotEqual(self.run_binit('candidate', 'select').returncode, 0)
        self.assertEqual((self.state / 'current').read_text(), before)

    def test_plugin_exit_function_cannot_turn_failed_check_into_success(self):
        self.success('prepare', str(self.config))
        before = (self.state / 'current').read_text()
        self.entry.write_text('greet() { printf candidate; }\nexit() { return 0; }\n')
        self.success('candidate', 'prepare', str(self.config))
        plan = self.checks('false\n')
        rejected = self.run_binit('candidate', 'check', str(plan))
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(json.loads(self.success('candidate', 'status', '--json'))['state'], 'failed')
        self.assertNotEqual(self.run_binit('candidate', 'select').returncode, 0)
        self.assertEqual((self.state / 'current').read_text(), before)

    def test_plugin_exit_trap_cannot_mask_failed_or_incomplete_check(self):
        self.success('prepare', str(self.config))
        before = (self.state / 'current').read_text()
        self.entry.write_text('greet() { printf candidate; }\ntrap "exit 0" EXIT\n')
        self.success('candidate', 'prepare', str(self.config))
        for script in ('false\n', 'builtin exit 17\n', 'builtin exit 0\n'):
            with self.subTest(script=script):
                plan = self.checks(script)
                rejected = self.run_binit('candidate', 'check', str(plan))
                self.assertNotEqual(rejected.returncode, 0)
                self.assertNotEqual(self.run_binit('candidate', 'select').returncode, 0)
                self.assertEqual((self.state / 'current').read_text(), before)

    def test_manager_binary_change_invalidates_acceptance(self):
        global BINARY
        original = BINARY
        copied = self.root / 'check binit Ω'
        shutil.copy2(original, copied)
        BINARY = copied
        try:
            before, _, _ = self.checked_change()
            with copied.open('ab') as binary:
                binary.write(b'changed')
            self.assertNotEqual(self.run_binit('candidate', 'select').returncode, 0)
            self.assertEqual((self.state / 'current').read_text(), before)
        finally:
            BINARY = original

    def test_candidate_view_supports_state_paths_with_spaces_and_unicode(self):
        self.state = self.root / 'state with spaces Ω'
        before, candidate, _ = self.checked_change()
        self.success('candidate', 'select')
        self.assertEqual((self.state / 'current').read_text(), candidate)
        self.assertEqual((self.state / 'previous').read_text(), before)

    def test_legacy_candidate_record_can_be_inspected_but_requires_new_preparation(self):
        before, _ = self.prepared_change()
        path = self.state / 'candidate.json'
        record = json.loads(path.read_text())
        del record['prepared_by']
        path.write_text(json.dumps(record))
        self.assertEqual(json.loads(self.success('candidate', 'status', '--json'))['state'], 'prepared')
        plan = self.checks('[[ $(greet) == candidate ]]\n')
        rejected = self.run_binit('candidate', 'check', str(plan))
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('prepare it again', rejected.stderr)
        self.assertEqual((self.state / 'current').read_text(), before)


if __name__ == '__main__':
    unittest.main()
