"""Controlled diagnostic runs preserve selected state and expose real causes."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from diagnostics_cases import materialize, validate

BINARY = Path(os.environ.get('BINIT_TEST_BINARY', ROOT / 'target/release/binit'))
CORPUS = json.loads((ROOT / 'benchmarks/diagnostics.json').read_text())


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        parent = Path(os.environ.get('BINIT_TEST_TMP', ROOT / 'tmp/diagnostics-tests'))
        parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='case-', dir=parent)
        self.root = Path(self.temporary.name)
        self.environment = dict(os.environ, HOME=str(self.root), INPUTRC='/dev/null')
        self.environment.pop('BASH_ENV', None)
        self.environment.pop('ENV', None)

    def tearDown(self):
        self.temporary.cleanup()

    def command(self, state, *args):
        return subprocess.run([str(BINARY), *args, '--state', str(state)],
                              cwd=self.root, env=self.environment, capture_output=True,
                              text=True, timeout=15)

    def prepare(self, case_id='nested-load', directory=None):
        case = next(case for case in CORPUS['cases'] if case['id'] == case_id)
        config, plan, state = materialize(directory or self.root, case)
        result = self.command(state, 'prepare', str(config))
        self.assertEqual(result.returncode, 0, result.stderr)
        return case, config, plan, state

    def preserved(self, state):
        return {name: (state / name).read_bytes() if (state / name).exists() else None
                for name in ('current', 'previous', 'candidate.json')}

    def exercise(self, case_id):
        case, config, plan, state = self.prepare(case_id)
        candidate = self.command(state, 'candidate', 'prepare', str(config))
        self.assertEqual(candidate.returncode, 0, candidate.stderr)
        before = self.preserved(state)
        result = self.command(state, 'diagnose', str(plan), '--json')
        payload = json.loads(result.stdout)
        report = validate(case, payload, result.returncode, CORPUS['target'])
        self.assertEqual(self.preserved(state), before)
        return report

    def test_nested_load_attributes_exclusive_and_inclusive_time(self):
        report = self.exercise('nested-load')
        self.assertTrue(any(cause['inclusive_us'] > cause['exclusive_us']
                            for cause in report['trace']['causes']))

    def test_hook_and_real_bash_function_completion_keep_effects(self):
        self.exercise('hook-provider')

    def test_blocked_command_identifies_both_plugins_and_retains_failure(self):
        self.exercise('blocked-command')

    def test_subshell_observations_are_separate_from_root_wall_time(self):
        self.exercise('subshell-overlap')

    def test_human_report_exposes_costs_and_artifact_identifier(self):
        _, _, plan, state = self.prepare()
        result = self.command(state, 'diagnose', str(plan))
        self.assertEqual(result.returncode, 0, result.stderr)
        for label in ('SELF_US', 'TOTAL_US', 'fixture_child', 'Run:', 'instrumented'):
            self.assertIn(label, result.stdout)

    def test_spaces_unicode_and_changed_ifs_do_not_corrupt_metadata(self):
        case, _, plan, state = self.prepare(directory=self.root / 'workspace with spaces Ω')
        script = plan.parent / 'check.bash'
        script.write_text('IFS=:\n' + script.read_text())
        result = self.command(state, 'diagnose', str(plan), '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        validate(case, json.loads(result.stdout), result.returncode, CORPUS['target'])

    def test_argument_payloads_and_multiline_text_are_never_retained(self):
        _, _, plan, state = self.prepare()
        marker = 'private_argument_79a_never_retain'
        (self.root / 'check.bash').write_text(f': "{marker}\nsecond line"\n')
        result = self.command(state, 'diagnose', str(plan), '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertNotIn(marker, json.dumps(payload['diagnostics']))
        for artifact in (state / 'diagnoses' / payload['run']).rglob('*'):
            if artifact.is_file() and artifact.name in ('diagnostic.json', 'namespace.bin'):
                self.assertNotIn(marker.encode(), artifact.read_bytes())

    def test_overflow_fails_with_partial_diagnostic_evidence(self):
        _, _, plan, state = self.prepare()
        (self.root / 'check.bash').write_text('for ((n=0;n<12000;n++)); do : discarded_argument; done\n')
        result = self.command(state, 'diagnose', str(plan), '--json')
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload['passed'])
        self.assertTrue(payload['diagnostics'][0]['trace']['limit_reached'])
        self.assertFalse(payload['diagnostics'][0]['complete'])

    def test_redirected_trace_arguments_do_not_enter_output_previews(self):
        _, _, plan, state = self.prepare()
        marker = 'private_redirected_trace_argument'
        for redirect in ('unset BASH_XTRACEFD', 'BASH_XTRACEFD=1'):
            with self.subTest(redirect=redirect):
                (self.root / 'check.bash').write_text(
                    f"PS4='+ '\n{redirect}\n: {marker}\n")
                result = self.command(state, 'diagnose', str(plan), '--json')
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(marker, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                for artifact in (state / 'diagnoses' / payload['run']).rglob('*'):
                    if artifact.is_file() and artifact.name in (
                            'result.json', 'progress.json', 'receipt.json',
                            'diagnostic.json', 'namespace.bin'):
                        self.assertNotIn(marker.encode(), artifact.read_bytes())

    def test_namespace_oversized_fields_are_rejected_before_writing(self):
        _, _, plan, state = self.prepare()
        (self.root / 'check.bash').write_text(
            "printf -v '__binit_reason[parent]' '%2048s' ''\n")
        result = self.command(state, 'diagnose', str(plan), '--json')
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload['diagnostics'][0]['namespace']['complete'])
        for artifact in (state / 'diagnoses' / payload['run']).rglob('namespace.bin'):
            self.assertLess(artifact.stat().st_size, 1024)

    def test_candidate_checks_keep_their_output_previews(self):
        _, config, plan, state = self.prepare()
        (self.root / 'check.bash').write_text(
            "printf public-stdout\nprintf public-stderr >&2\n")
        prepared = self.command(state, 'candidate', 'prepare', str(config))
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        checked = self.command(state, 'candidate', 'check', str(plan))
        self.assertEqual(checked.returncode, 0, checked.stderr)
        candidate = json.loads((state / 'candidate.json').read_text())
        receipt = json.loads((state / 'check-runs' / candidate['check']['id'] /
                              'receipt.json').read_text())
        self.assertIn('public-stdout', receipt['samples'][0]['stdout_preview'])
        self.assertIn('public-stderr', receipt['samples'][0]['stderr_preview'])

    def test_untraced_diagnosis_runs_identical_effect_checks_without_sidecars(self):
        _, _, plan, state = self.prepare()
        before = self.preserved(state)
        result = self.command(state, 'diagnose', str(plan), '--json', '--no-trace')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload['passed'])
        self.assertEqual(payload['diagnostics'], [])
        self.assertFalse(payload['receipt']['context']['diagnostic'])
        sample = payload['receipt']['samples'][0]
        self.assertNotIn('diagnostic', sample)
        self.assertEqual(sample['check_return'], 0)
        self.assertGreaterEqual(sample['elapsed_ms'], 200)
        self.assertEqual(self.preserved(state), before)
        run = state / 'diagnoses' / payload['run']
        self.assertEqual(list(run.rglob('namespace.bin')), [])
        (self.root / 'check.bash').write_text('return 23\n')
        failed = self.command(state, 'diagnose', str(plan), '--no-trace')
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('Tracing: off', failed.stdout)

    def test_sample_receipts_account_for_both_sidecars(self):
        _, _, plan, state = self.prepare()
        plan.write_text(plan.read_text() + 'samples=2\n')
        result = self.command(state, 'diagnose', str(plan), '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        samples = payload['receipt']['samples']
        self.assertEqual(len(samples), 2)
        for index, sample in enumerate(samples):
            directory = state / 'diagnoses' / payload['run'] / 'samples' / f'{index:04d}'
            self.assertEqual(sample['diagnostic']['bytes'],
                             (directory / 'diagnostic.json').stat().st_size)
            self.assertEqual(sample['diagnostic']['namespace_bytes'],
                             (directory / 'namespace.bin').stat().st_size)
            self.assertGreater(sample['diagnostic']['namespace_bytes'], 0)

    def test_real_pty_completion_is_attributed_without_losing_shell_effects(self):
        _, _, plan, state = self.prepare()
        script = r'''export BINIT_PTY_PS4="$PS4" BINIT_PTY_BASH="$BASH"
python3 - <<'PY'
import os, pathlib, select, subprocess, time
work = pathlib.Path(os.environ['BINIT_CHECK_WORK'])
rc = work / 'pty.bash'
rc.write_text("""PS1='DIAG_READY> '
PROMPT_COMMAND=()
HISTFILE=/dev/null
fixture_pty_provider() { sleep .25; DIAG_PTY=changed; cd "$BINIT_CHECK_WORK"; COMPREPLY=(finished); }
complete -F fixture_pty_provider diagcmd
BASH_XTRACEFD=$__binit_trace_fd
PS4=$BINIT_PTY_PS4
__binit_check_phase=check
set -x
""")
master, slave = os.openpty()
child = subprocess.Popen([os.environ['BINIT_PTY_BASH'], '--noprofile',
                          '--rcfile', str(rc), '-i'], stdin=slave, stdout=slave,
                         stderr=slave, pass_fds=(int(os.environ['__binit_trace_fd']),),
                         env=dict(os.environ, INPUTRC='/dev/null', TERM='dumb'))
os.close(slave)
pending = bytearray()
def expect(value):
    end = time.monotonic() + 2
    while value not in pending:
        assert time.monotonic() < end, 'PTY did not reach the expected state'
        if select.select([master], [], [], .02)[0]:
            pending.extend(os.read(master, 8192))
    del pending[:pending.index(value) + len(value)]
try:
    expect(b'DIAG_READY> ')
    os.write(master, b'diagcmd \t')
    expect(b'finished')
    os.write(master, b'\x15printf "EFFECT:%s:%s\\n" "$DIAG_PTY" "${PWD##*/}"\n')
    expect(b'EFFECT:changed:work')
    expect(b'DIAG_READY> ')
    os.write(master, b'exit\n')
    assert child.wait(timeout=2) == 0
finally:
    if child.poll() is None:
        child.terminate()
        child.wait(timeout=2)
    os.close(master)
PY
'''
        (self.root / 'check.bash').write_text(script)
        result = self.command(state, 'diagnose', str(plan), '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)['diagnostics'][0]
        self.assertTrue(report['complete'])
        causes = [cause for cause in report['trace']['causes']
                  if cause['function'] == 'fixture_pty_provider' and cause['process'] > 0]
        self.assertGreaterEqual(sum(cause['exclusive_us'] for cause in causes), 240000)

    def test_fixture_disabling_trace_is_reported_incomplete(self):
        _, _, plan, state = self.prepare()
        (self.root / 'check.bash').write_text('set +x\n: silent\n')
        result = self.command(state, 'diagnose', str(plan), '--json')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)['diagnostics'][0]['complete'])

    def test_fixture_restoring_trace_does_not_hide_unobserved_work(self):
        _, _, plan, state = self.prepare()
        for disable, restore in [('set +x', 'set -x'), ('set -', 'set -x'),
                                 ('declare PS4=x', 'PS4=$saved'),
                                 ('printf -v PS4 x', 'PS4=$saved')]:
            with self.subTest(disable=disable):
                (self.root / 'check.bash').write_text(
                    f'saved=$PS4\n{disable}\nsleep .25\n{restore}\n: restored\n')
                result = self.command(state, 'diagnose', str(plan), '--json')
                self.assertNotEqual(result.returncode, 0)
                report = json.loads(result.stdout)['diagnostics'][0]
                self.assertFalse(report['complete'])
                phase = next(p for p in report['trace']['phases']
                             if p['process'] == 0 and p['name'] == 'check')
                self.assertGreaterEqual(phase['unattributed_us'], 200000)

    def test_timeout_stops_owned_background_work_and_preserves_selection(self):
        _, _, plan, state = self.prepare()
        marker = self.root / 'late'
        (self.root / 'check.bash').write_text(f'(sleep .35; touch {shlex.quote(str(marker))}) &\nsleep 2\n')
        plan.write_text('schema=1\n[[checks]]\nid="timeout"\nscript="check.bash"\ntimeout_ms=100\n')
        before = self.preserved(state)
        result = self.command(state, 'diagnose', str(plan), '--json')
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(payload['receipt']['samples'][0]['timed_out'])
        time.sleep(.4)
        self.assertFalse(marker.exists())
        self.assertEqual(self.preserved(state), before)

    def test_interrupted_diagnostic_keeps_selected_generation(self):
        _, _, plan, state = self.prepare()
        marker = self.root / 'ready'
        (self.root / 'check.bash').write_text(f'touch {shlex.quote(str(marker))}\nsleep 5\n')
        before = self.preserved(state)
        with subprocess.Popen([str(BINARY), 'diagnose', str(plan), '--json', '--state', str(state)],
                              cwd=self.root, env=self.environment, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as child:
            deadline = time.monotonic() + 5
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists())
            child.send_signal(signal.SIGTERM)
            stdout, _ = child.communicate(timeout=5)
            self.assertNotEqual(child.returncode, 0)
        self.assertFalse(json.loads(stdout)['passed'])
        self.assertEqual(self.preserved(state), before)


if __name__ == '__main__':
    unittest.main()
