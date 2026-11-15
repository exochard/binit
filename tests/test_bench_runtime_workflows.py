from pathlib import Path
import json
import math
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import bench_runtime_workflows as workflow


def paired_rows():
    return [{'case': case, 'run': 0, 'pair': 0, 'variant': variant,
             'elapsed_ms': elapsed, 'effects_passed': True, 'comparison_signature': {'fixture': 'same'},
             'commands_ms': [elapsed]}
            for case in workflow.CASES for variant, elapsed in [('candidate', .5), ('reference', 1.)]]


class WorkflowIntegrityTests(unittest.TestCase):
    def test_assessment_uses_each_paired_ratio(self):
        for values in workflow.assess(paired_rows(), 1, 1).values():
            self.assertEqual(values[0]['median_paired_ratio'], .5)

    def test_failed_duplicate_cannot_be_overwritten_by_passing_row(self):
        rows = paired_rows()
        failed = {**rows[0], 'effects_passed': False, 'elapsed_ms': 100.}
        for sequence in ([failed, *rows], [*rows, failed], [rows[0], *rows]):
            with self.subTest(sequence=sequence):
                with self.assertRaisesRegex(RuntimeError, 'duplicate or failed'):
                    workflow.assess(sequence, 1, 1)

    def test_missing_unexpected_and_invalid_elapsed_rows_fail(self):
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            workflow.assess(paired_rows()[:-1], 1, 1)
        for elapsed in (0., -1., math.nan, math.inf, True):
            rows = paired_rows()
            rows[0]['elapsed_ms'] = elapsed
            with self.subTest(elapsed=elapsed):
                with self.assertRaisesRegex(RuntimeError, 'nonpositive or nonfinite'):
                    workflow.assess(rows, 1, 1)
        rows = paired_rows()
        rows[0]['pair'] = 1
        with self.assertRaisesRegex(RuntimeError, 'unexpected'):
            workflow.assess(rows, 1, 1)

    def test_unequal_pair_inputs_fail_even_when_both_effects_pass(self):
        rows = paired_rows()
        rows[0]['comparison_signature'] = {'fixture': 'changed'}
        with self.assertRaisesRegex(RuntimeError, 'fixture inputs differ'):
            workflow.assess(rows, 1, 1)

    def test_watched_content_and_missing_paths_are_checked(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            directory = Path(temporary) / 'fixture'
            workflow.make_fixture(workflow.CASES[1], directory, None)
            initial = workflow.watched_inputs(directory)
            self.assertIsNone(initial['build/toolchain.ready'])
            (directory / 'settings.env').write_text('changed\n')
            with self.assertRaisesRegex(RuntimeError, 'content differs'):
                workflow.watched_inputs(directory)
            (directory / 'settings.env').write_text('mode=development\n')
            (directory / 'build/toolchain.ready').write_text('early\n')
            with self.assertRaisesRegex(RuntimeError, 'identity differs'):
                workflow.watched_inputs(directory)

    def test_failure_retains_partial_receipt_and_driver(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            base = Path(temporary)
            source = base / 'source'
            source.write_bytes(b'wrong pin')
            output = base / 'receipt.json'
            argv = ['driver', '--candidate', str(source), '--reference', str(source),
                    '--git-source', str(source), '--work', str(base / 'work'), '--output', str(output)]
            with patch.object(sys, 'argv', argv):
                with self.assertRaisesRegex(RuntimeError, 'frozen pin'):
                    workflow.main()
            result = json.loads(output.read_text())
            self.assertFalse(result['complete'])
            self.assertFalse(result['qualification'])
            self.assertIn('frozen pin', result['error'])
            self.assertEqual((base / 'work/driver.py').read_bytes(), Path(workflow.__file__).read_bytes())


class WorkflowEffectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = Path(os.environ.get('BINIT_TEST_BINARY', ROOT / 'target/release/binit')).resolve()
        cls.source = Path(os.environ.get('BINIT_WORKFLOW_GIT_SOURCE', ROOT / 'tmp/bash-ecosystem/sources/bash-it' / workflow.GIT_PATH))
        cls.bash = os.environ.get('BINIT_BASH', '/bin/bash')
        if not cls.binary.is_file() or not cls.source.is_file():
            raise unittest.SkipTest('workflow effects require a built Binit and the pinned optional corpus source')

    def test_both_workflows_match_direct_source_and_all_prompt_effects(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            for index, case in enumerate(workflow.CASES):
                with self.subTest(case=case):
                    row = workflow.observe(case, self.binary, self.bash, self.source,
                                           Path(temporary) / str(index))
                    self.assertTrue(row['effects_passed'])
                    self.assertEqual(len(row['commands_ms']), 2 if index == 0 else 20)

    def test_real_negative_controls_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            for fault in workflow.FAULTS:
                case = workflow.CASES[1] if fault in ('no-watch', 'wrong-status') else workflow.CASES[0]
                with self.subTest(fault=fault):
                    with self.assertRaisesRegex(RuntimeError, 'effect failure|prompt activation failure|input changed'):
                        workflow.observe(case, self.binary, self.bash, self.source,
                                         Path(temporary) / fault, fault=fault)


if __name__ == '__main__':
    unittest.main()
