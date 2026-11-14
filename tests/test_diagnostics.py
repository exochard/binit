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

    def test_fixture_disabling_trace_is_reported_incomplete(self):
        _, _, plan, state = self.prepare()
        (self.root / 'check.bash').write_text('set +x\n: silent\n')
        result = self.command(state, 'diagnose', str(plan), '--json')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)['diagnostics'][0]['complete'])

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
