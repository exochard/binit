"""Reject incomplete or unequal diagnostic benchmark evidence."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from bench_diagnostics import assess, check_receipt


class DiagnosticsBenchmarkTests(unittest.TestCase):
    def observations(self):
        return [dict(run=run, case='fixture', pair=pair, mode=mode, validated=True,
                     wall_ms=300 if mode == 'unobserved' else 330)
                for run in range(2) for pair in range(20)
                for mode in ('unobserved', 'observed')]

    def test_complete_pairs_pass_and_slow_observer_fails(self):
        rows = self.observations()
        self.assertTrue(all(s['passed'] for s in assess(rows, ['fixture'], 2, 20, 1.2)))
        for row in rows:
            if row['mode'] == 'observed':
                row['wall_ms'] = 380
        self.assertFalse(any(s['passed'] for s in assess(rows, ['fixture'], 2, 20, 1.2)))

    def test_missing_duplicate_unvalidated_and_nonfinite_data_are_rejected(self):
        rows = self.observations()
        variants = [rows[:-1], rows + [rows[0]], rows[:-1] + [rows[0]]]
        for field, value in [('validated', False), ('wall_ms', float('nan')),
                             ('wall_ms', 0), ('case', 'unequal'), ('run', 3)]:
            altered = copy.deepcopy(rows)
            altered[0][field] = value
            variants.append(altered)
        for variant in variants:
            with self.assertRaises(ValueError):
                assess(variant, ['fixture'], 2, 20, 1.2)

    def test_success_exit_cannot_hide_failed_effect_or_incomplete_output(self):
        sample = dict(code=0, check_return=0, phase='complete', signal=None,
                      timed_out=False, interrupted=0, output_complete=True,
                      generation_unchanged=True, elapsed_ms=300)
        receipt = dict(complete=True, error=None, samples=[sample])
        check_receipt(receipt, dict(minimum_window_ms=200))
        for key, value in [('check_return', 1), ('output_complete', False),
                           ('generation_unchanged', False), ('elapsed_ms', 150)]:
            altered = copy.deepcopy(receipt)
            altered['samples'][0][key] = value
            with self.assertRaises(ValueError):
                check_receipt(altered, dict(minimum_window_ms=200))
