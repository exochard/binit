"""The frozen publication rules must turn paired observations into verdicts."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import scoreboard  # noqa: E402

RULE = json.loads(scoreboard.POLICY.read_text())['rule']
RULE = {**RULE, 'bootstrap_resamples': 400}


def blocks(values):
    return [[value] for value in values]


class ScoreboardRules(unittest.TestCase):
    def verdict(self, subject, competitor, floor=0, name='median'):
        return scoreboard.compare(blocks(subject), blocks(competitor), name, RULE, floor, 'test')['verdict']

    def test_clear_differences_win_and_lose(self):
        competitor = [27 + (i % 5) * .1 for i in range(20)]
        self.assertEqual(self.verdict([v * .8 for v in competitor], competitor), 'win')
        self.assertEqual(self.verdict([v * 1.2 for v in competitor], competitor), 'loss')

    def test_non_inferior_within_five_percent_ties(self):
        competitor = [27 + (i % 5) * .1 for i in range(20)]
        self.assertEqual(self.verdict([v * 1.01 for v in competitor], competitor), 'tie')
        self.assertEqual(self.verdict([v * .97 for v in competitor], competitor), 'tie')

    def test_wide_or_borderline_intervals_are_inconclusive(self):
        competitor = [27.0] * 20
        noisy = [10.0 if i % 2 else 50.0 for i in range(20)]
        self.assertEqual(self.verdict(noisy, competitor), 'inconclusive')
        self.assertEqual(self.verdict([28.0 + (i % 4) * .2 for i in range(20)], competitor), 'inconclusive')

    def test_absolute_floor_ties_only_small_differences(self):
        self.assertEqual(self.verdict([.30] * 20, [.05] * 20, floor=1.0), 'tie')
        self.assertEqual(self.verdict([.05] * 20, [25.0] * 20, floor=1.0), 'win')
        self.assertEqual(self.verdict([30.0] * 20, [25.0] * 20, floor=1.0), 'loss')

    def test_pooled_quantiles_use_whole_blocks(self):
        subject = [[1.0] * 49 + [9.0] for _ in range(20)]
        competitor = [[1.0] * 50 for _ in range(20)]
        result = scoreboard.compare(subject, competitor, 'p99', RULE, 0, 'pool')
        self.assertEqual(result['binit'], 9.0)
        self.assertEqual(result['competitor'], 1.0)

    def test_run_and_aspect_combination(self):
        self.assertEqual(scoreboard.combine_runs(['win', 'win']), 'win')
        self.assertEqual(scoreboard.combine_runs(['win', 'tie']), 'tie')
        self.assertEqual(scoreboard.combine_runs(['win', 'inconclusive']), 'inconclusive')
        self.assertEqual(scoreboard.combine_runs(['tie', 'loss']), 'inconclusive')
        self.assertEqual(scoreboard.combine_aspect(['win', 'tie']), 'tie')
        self.assertEqual(scoreboard.combine_aspect(['win', 'loss']), 'loss')
        self.assertEqual(scoreboard.combine_aspect(['win', 'inconclusive']), 'inconclusive')
        self.assertEqual(scoreboard.combine_aspect([]), 'not measured')

    def test_framework_receipt_scores_wait_row_beside_eager_row(self):
        subjects = {'bash': 16, 'binit': 28, 'binit-wait': 28.2, 'bash-it': 27, 'oh-my-bash': 54, 'zsh': 7, 'zinit': 66}
        runs = []
        for _ in range(2):
            run = []
            for pair in range(20):
                for subject, base in subjects.items():
                    value = base + (pair % 3) * .05
                    run.append({'case': f'{subject}-50', 'pair': pair, 'startup_visible_ms': value - 2,
                                'first_input_ack_ms': 2, 'startup_usable_ms': value,
                                'effect_probe_ms': 5, 'required_effects_ms': value + 5})
            runs.append(run)
        receipt = {'complete_two_run_primed_framework': True, 'runs': runs}
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as directory:
            source = Path(directory) / 'framework.json'
            source.write_text(json.dumps(receipt))
            output = Path(directory) / 'score.json'
            subprocess.run([sys.executable, str(ROOT / 'scripts/scoreboard.py'), '--framework', str(source),
                            '--output', str(output)], check=True, capture_output=True)
            result = json.loads(output.read_text())
        usable = result['aspects']['first-usable-input']
        self.assertEqual(usable['comparisons']['binit vs oh-my-bash']['verdict'], 'win')
        self.assertEqual(usable['comparisons']['binit vs bash-it']['verdict'], 'tie')
        self.assertEqual(usable['verdict'], 'tie')
        self.assertIn('binit-wait', usable['labelled_verdicts'])
        self.assertAlmostEqual(usable['framework_overhead_ms_by_run']['binit'][0], 12.0, places=6)
        self.assertEqual(result['aspects']['keystroke-p99']['verdict'], 'not measured')
        self.assertFalse(result['publication_gate']['passed'])

    def test_incomplete_receipts_are_rejected(self):
        with self.assertRaises(ValueError):
            scoreboard.score_framework(json.loads(scoreboard.POLICY.read_text())['aspects'][0],
                                       {'complete_two_run_primed_framework': False, 'runs': []}, RULE)


if __name__ == '__main__':
    unittest.main()
