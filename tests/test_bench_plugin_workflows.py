"""Only complete effects-checked subjects get a timing summary."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bench_plugin_workflows import EFFECTS, METRICS, fzf_query_ready, results_by_run


def bash_row(pair, passed=True):
    return {
        "run": 0, "pair": pair, "subject": "bash", "passed": passed,
        "timings_ms": dict.fromkeys(METRICS, float(pair + 1)),
        "effects": list(EFFECTS if passed else EFFECTS[:2]),
    }


class WorkflowSummaryTests(unittest.TestCase):
    def test_fzf_query_is_not_ready_until_the_unique_result_is_drawn(self):
        query = b"workflow-history-target"
        prompt = b"\x1b[1mWorkflow history>\x1b[0m " + query
        line = b"printf 'workflow-history-target\\n'"
        self.assertFalse(fzf_query_ready(prompt + b"\r\n2/2 (0)\r\n" + line, query))
        self.assertFalse(fzf_query_ready(prompt + b"\r\n1/4 (0)\r\n" + line, query))
        self.assertTrue(fzf_query_ready(prompt + b"\r\n1/2 (0)\r\n" + line, query))

    def test_complete_subject_reports_each_metric(self):
        result = results_by_run([bash_row(0), bash_row(1)], runs=1, pairs=2)[0]
        self.assertEqual(result["passed"], 2)
        self.assertEqual(result["effect_counts"], dict.fromkeys(EFFECTS, 2))
        self.assertEqual(result["timings_ms"]["git_tab"], {"median": 1.5, "p95": 2.0})

    def test_failed_and_missing_runs_keep_coverage_without_a_timing_summary(self):
        for rows in ([bash_row(0)], [bash_row(0), bash_row(1, passed=False)]):
            with self.subTest(rows=rows):
                result = results_by_run(rows, runs=1, pairs=2)[0]
                self.assertEqual(result["expected"], 2)
                self.assertEqual(result["passed"], 1)
                self.assertEqual(result["effect_counts"]["fzf-history"], 1)
                self.assertNotIn("timings_ms", result)
