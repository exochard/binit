"""A visible suggestion must belong to the current logical input."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from check_source_trial import input_display


class SourceTrialObserverTests(unittest.TestCase):
    def test_previous_commands_and_history_rows_cannot_supply_a_suggestion(self):
        screen = SimpleNamespace(display=[
            'binit /source $ history -s "printf source-trial-suggestion"',
            'binit /source $ printf source-trial-s',
            'History  Tab focus  Ctrl-R search',
            '  printf source-trial-suggestion',
        ], cursor=SimpleNamespace(y=1))
        self.assertNotIn('printf source-trial-suggestion', input_display(screen))

    def test_wrapped_current_input_can_contain_a_visible_suggestion(self):
        screen = SimpleNamespace(display=[
            'binit /a/long/source/path $ printf source-trial-',
            'suggestion                         ',
            'History  Tab focus  Ctrl-R search',
        ], cursor=SimpleNamespace(y=1))
        self.assertIn('printf source-trial-suggestion', input_display(screen))
