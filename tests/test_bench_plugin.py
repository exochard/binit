"""Prompt detection in the per-plugin timing driver."""
from pathlib import Path
import sys
import unittest

import pyte

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from bench_plugin import at_empty_prompt  # noqa: E402


def screen(text):
    display = pyte.Screen(40, 4)
    pyte.Stream(display).feed(text)
    return display


class EmptyPromptTests(unittest.TestCase):
    def test_accepts_the_bare_prompt(self):
        self.assertTrue(at_empty_prompt(screen('BENCH> ')))

    def test_accepts_a_prompt_after_plugin_segments(self):
        self.assertTrue(at_empty_prompt(screen('[project] BENCH> ')))
        self.assertTrue(at_empty_prompt(screen('[main] BENCH> ')))

    def test_accepts_the_new_prompt_after_an_interrupted_line(self):
        self.assertTrue(at_empty_prompt(screen('[project] BENCH> :^C\r\n[project] BENCH> ')))

    def test_rejects_pending_input(self):
        self.assertFalse(at_empty_prompt(screen('BENCH> git')))
        self.assertFalse(at_empty_prompt(screen('[project] BENCH> :^C')))
        self.assertFalse(at_empty_prompt(screen('BENCH> x\x1b[D')))


if __name__ == '__main__':
    unittest.main()
