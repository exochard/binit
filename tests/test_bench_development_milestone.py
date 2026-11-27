import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from bench_development_milestone import grade, grade_runs, summarize_receipts  # noqa: E402
from bench_deferred_milestone import score_run  # noqa: E402


class MilestoneGradeTests(unittest.TestCase):
    def test_requires_every_pair_with_positive_effect_checked_observation(self):
        rows = [dict(case=case, pair=pair, effect_status=0, private_state_unchanged=True,
                     startup_usable_ms=value)
                for pair in range(2)
                for case, value in [('candidate', 8), ('baseline', 10), ('zinit', 12),
                                    ('bash', 4), ('zsh', 4)]]
        self.assertEqual(grade(rows, 2), {'baseline': .8, 'zinit': 2 / 3})
        for invalid in (rows[:-1], rows + rows[:1], [*rows[:-1], {**rows[-1], 'effect_status': 1}],
                        [*rows[:-1], {**rows[-1], 'startup_usable_ms': float('nan')}]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                grade(invalid, 2)

    def test_complete_runs_must_beat_both_baselines_in_both_runs(self):
        def run(candidate):
            return [dict(case=case, pair=pair, effect_status=0, private_state_unchanged=True,
                         startup_usable_ms=value)
                    for pair in range(20)
                    for case, value in [('candidate', candidate), ('baseline', 100), ('zinit', 100),
                                        ('bash', 50), ('zsh', 50)]]
        self.assertTrue(grade_runs([run(97), run(98)], 20, .98)['pass'])
        self.assertFalse(grade_runs([run(97), run(99)], 20, .98)['pass'])
        self.assertFalse(grade_runs([run(97)], 20, .98)['pass'])

    def test_deferred_p95_requires_complete_valid_effects(self):
        rows = [dict(pair=pair, mode=mode, effects_ok=True, metric_ms={'echo': value})
                for pair in range(20) for mode, value in [('baseline', 10.0), ('candidate', 8.0)]]
        self.assertEqual(score_run(rows, 20)['ratio'], .8)
        for invalid in (rows[:-1], rows + rows[:1], [*rows[:-1], {**rows[-1], 'effects_ok': False}],
                        [*rows[:-1], {**rows[-1], 'metric_ms': {'echo': float('nan')}}]):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                score_run(invalid, 20)

    def test_summary_never_converts_incomplete_or_missing_cells_to_wins(self):
        summary = summarize_receipts([{'abi': '53', 'count': 10, 'complete': False}])
        self.assertEqual(summary['startup_framework_cells']['53/10'], 'incomplete')
        self.assertEqual(summary['startup_framework_cells']['52/50'], 'pending')
        self.assertFalse(summary['release_gate_pass'])
        with self.assertRaises(ValueError):
            summarize_receipts([{'abi': '53', 'count': 10}, {'abi': '53', 'count': 10}])
