"""A visible suggestion must belong to the current logical input."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from check_source_trial import input_display
import bench_interactive as terminal


class SourceTrialObserverTests(unittest.TestCase):
    def test_previous_commands_and_history_rows_cannot_supply_a_suggestion(self):
        screen = SimpleNamespace(display=[
            'binit /source $ history -s "printf source-trial-suggestion"',
            'binit /source $ printf source-trial-s',
            'history',
            '  printf source-trial-suggestion',
        ], cursor=SimpleNamespace(y=1))
        self.assertNotIn('printf source-trial-suggestion', input_display(screen))

    def test_wrapped_current_input_can_contain_a_visible_suggestion(self):
        screen = SimpleNamespace(display=[
            'binit /a/long/source/path $ printf source-trial-',
            'suggestion                         ',
            'history',
        ], cursor=SimpleNamespace(y=1))
        self.assertIn('printf source-trial-suggestion', input_display(screen))

    def test_a_wrapped_prompt_cannot_borrow_an_earlier_command(self):
        directory = '[trial-branch] binit /' + 'a' * 104 + '/core '
        screen = SimpleNamespace(display=[
            directory, '$ binit profile; printf "\\036DONE\\036"',
            directory, '$ history -s "printf source-trial-suggestion"',
            directory, '$ printf source-trial-s',
        ], cursor=SimpleNamespace(y=5))
        self.assertEqual(input_display(screen), 'printf source-trial-s')

    def test_a_prompt_marker_split_across_rows_is_found(self):
        screen = SimpleNamespace(display=[
            'binit /source $ history -s "printf source-trial-suggestion"',
            'binit /' + 'a' * 111 + ' $',
            ' printf source-trial-suggestion',
        ], cursor=SimpleNamespace(y=2))
        self.assertEqual(input_display(screen), 'printf source-trial-suggestion')


class ChromeTests(unittest.TestCase):
    """The harness must recognize the frozen reference's chrome and the candidate's."""

    def test_each_predicate_accepts_both_presentations_of_its_state_only(self):
        cases = {
            terminal.live_history_shown: (
                ['History  Tab focus  Ctrl-R search', 'history'],
                ['history  1/2   ?', 'history  al\u258f  1/37   ?', 'sta  1/3   ?']),
            terminal.history_search_shown: (
                ['History SEARCH /al  1/37', 'history  al\u258f  1/37   ?', 'history  \u258f  3/3   ?'],
                ['History NAVIGATE 1/37', 'history  1/37   ?', 'history', 'sta /co  1/2   ?']),
            lambda display: terminal.completion_shown(display, 'sta'): (
                ['Completions 1/3', 'sta  1/3   ?', 'sta /co  1/2'],
                ['history  1/37   ?', 'history  sta\u258f  1/1   ?', 'History NAVIGATE 1/37', 'stash  1/3   ?']),
        }
        for predicate, (shown, other) in cases.items():
            for row in shown:
                self.assertTrue(predicate(['BENCH> git sta', row]), row)
            for row in other:
                self.assertFalse(predicate(['BENCH> git sta', row]), row)
