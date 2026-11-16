"""Default history interaction: completion owns Tab; search owns Ctrl-R."""
import os
import unittest
from unittest.mock import patch

from test_completion import Session


class HistoryContractTests(unittest.TestCase):
    def setUp(self):
        mode = patch.dict(os.environ)
        mode.start()
        os.environ.pop('BINIT_HISTORY_MODE', None)
        self.addCleanup(mode.stop)

    def test_matching_history_does_not_take_tab_from_completion(self):
        extra = "history -s 'demo_cmd value history'\nbinit_complete editor history on\n"
        for mode in ('vi', 'emacs'):
            with self.subTest(mode=mode), Session(extra=extra, mode=mode, framework=True) as session:
                session.send('demo_cmd va')
                session.pump(.15)
                self.assertNotIn('History', session.text)
                session.send(b'\t')
                session.wait(lambda: 'Completions' in session.text)
                session.send(b'\x1b')
                session.pump(.1)
                self.assertEqual(session.capture()[1], 'demo_cmd va')

    def test_history_search_inserts_without_executing_and_keeps_tab_available(self):
        extra = "history -s 'demo_cmd value history'\nbinit_complete editor history on\n"
        for mode in ('vi', 'emacs'):
            with self.subTest(mode=mode), Session(extra=extra, mode=mode) as session:
                session.send(b'\x12value history\r')
                session.pump(.2)
                self.assertEqual(session.capture()[1], 'demo_cmd value history')
                session.send(b'\x03')
                session.pump(.1)
                session.open('demo_cmd va')
                session.send(b'\x1b')
                session.pump(.1)
                self.assertEqual(session.capture()[1], 'demo_cmd va')

    def test_editor_status_reports_actual_flags(self):
        with Session(framework=True) as session:
            session.send('binit editor\n')
            session.wait(lambda: 'Suggestions: off' in session.text)
            self.assertIn('History: off', session.text)
            session.send('binit_complete editor history on; binit editor\n')
            session.wait(lambda: 'History: search' in session.text)
            self.assertIn('Suggestions: on', session.text)
            session.send('binit_complete editor history off; binit editor\n')
            session.wait(lambda: session.text.count('History: off') == 2)

    def test_completion_menu_does_not_display_a_history_suggestion_as_input(self):
        extra = "history -s 'demo_cmd value history'\nbinit_complete editor history on\n"
        with Session(extra=extra) as session:
            session.send('demo_cmd va')
            session.wait(lambda: 'demo_cmd value history' in session.text)
            session.send(b'\t')
            session.wait(lambda: 'Completions' in session.text)
            self.assertNotIn('demo_cmd value history', session.text)
            session.send(b'\x1b')
            session.pump(.1)
            self.assertEqual(session.capture()[1], 'demo_cmd va')

    def test_mode_change_restores_completion_and_preserves_custom_keys(self):
        extra = ("history -s 'demo_cmd value history'\n"
                 "binit_complete editor history live\n"
                 "binit_complete editor history on\n")
        with Session(extra=extra) as session:
            session.open('demo_cmd va')
            self.assertIn('Completions', session.text)
        extra += ("__custom() { READLINE_LINE=custom; READLINE_POINT=6; }\n"
                  "bind -m emacs-standard -x '\"\\t\": __custom'\n"
                  "bind -m emacs-standard -x '\"\\C-r\": __custom'\n"
                  "binit_complete editor history live\n"
                  "binit_complete editor history on\n")
        for key in (b'\t', b'\x12'):
            with self.subTest(key=key), Session(extra=extra) as session:
                session.send(key)
                session.pump(.1)
                self.assertEqual(session.capture()[1], 'custom')

    def test_invalid_mode_leaves_active_history_unchanged(self):
        extra = ("binit_complete editor history on\n"
                 "BINIT_HISTORY_MODE=invalid\n"
                 "binit_complete editor history on\n")
        with Session(extra=extra, framework=True) as session:
            session.send('binit editor\n')
            session.wait(lambda: 'History: search' in session.text)
            self.assertIn('Suggestions: on', session.text)

    def test_editor_status_reports_fallback_without_loading_a_module(self):
        with Session(framework=True) as session:
            session.send('enable -n binit_complete; binit editor; printf "STATUS:%s\\n" "$?"\n')
            session.wait(lambda: 'Native editor: unavailable in this shell' in session.text)
            self.assertIn('STATUS:1', session.text)

    def test_editor_status_only_advertises_owned_accept_and_search_keys(self):
        extra = ("binit_complete editor history on\n"
                 "__custom() { :; }\n"
                 "bind -m emacs-standard -x '\"\\t\": __custom'\n"
                 "bind -m emacs-standard -x '\"\\C-r\": __custom'\n"
                 "bind -m emacs-standard -x '\"\\C-f\": __custom'\n"
                 "bind -m emacs-standard -x '\"\\e[C\": __custom'\n")
        with Session(extra=extra, framework=True) as session:
            session.send('binit editor\n')
            session.wait(lambda: 'Tab: custom binding' in session.text)
            self.assertNotIn('Right: accept suggestion', session.text)
            self.assertNotIn('Ctrl-F: accept suggestion', session.text)
            self.assertNotIn('Ctrl-R: history search', session.text)


if __name__ == '__main__':
    unittest.main()
