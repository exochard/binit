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
                self.assertFalse(any(row.rstrip() == 'history' for row in session.screen.display))
                session.send(b'\t')
                session.wait(lambda: session.panel)
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

    def test_word_and_fuzzy_modes_find_noncontiguous_history(self):
        for editing, method, query in (('emacs', 'words', 'needle printf'), ('vi', 'fuzzy', 'prtf ndle')):
            extra = (f"BINIT_HISTORY_MATCH={method}\nhistory -s 'printf missed'\n"
                     "history -s 'printf needle'\nbinit_complete editor history on\n")
            with self.subTest(method=method), Session(extra=extra, mode=editing) as session:
                session.send(b'\x12' + query.encode() + b'\r')
                session.pump(.2)
                self.assertEqual(session.capture()[1], 'printf needle')

    def test_ctrl_r_finds_older_commands_without_executing_them(self):
        entry = 'touch deep-witness # old needle'
        for mode, method, query in (('emacs', 'literal', 'old needle'),
                                    ('vi', 'words', 'needle old'), ('emacs', 'fuzzy', 'olndl')):
            extra = (f"HISTSIZE=8192; history -c; BINIT_HISTORY_MATCH={method}\n"
                     f"history -s '{entry}'; history -s '{entry}'\n"
                     "for ((i=0; i<2048; i++)); do history -s \"printf recent-$i\"; done\n"
                     "binit_complete editor history on\n")
            with self.subTest(mode=mode, method=method), Session(extra=extra, mode=mode) as session:
                session.send(b'\x12' + query.encode())
                session.wait(lambda: entry in session.text and '1/1 ' in session.text)
                session.send(b'Z')
                session.wait(lambda: '0/0' in session.text)
                session.send(b'\x7f\r')
                session.wait(lambda: not session.panel)
                self.assertEqual(session.capture()[1], entry)
                self.assertFalse((session.directory / 'deep-witness').exists())

    def test_deleting_an_older_match_preserves_recent_context(self):
        extra = ("HISTSIZE=8192; HISTCONTROL=ignorespace; BINIT_HISTORY_CONTEXT=1; "
                 "BINIT_HISTORY_DELETE=1; binit_complete editor history on\n")
        with Session(extra=extra) as session:
            (session.directory / 'seed').write_text(
                "history -c; history -s 'printf old-delete-entry'\n"
                "binit_complete history record 0 \"$PWD\"\n"
                "for ((i=0; i<1100; i++)); do history -s \"printf recent-$i\"; "
                "binit_complete history record 37 \"$PWD\"; done\n")
            session.send(" source ./seed; binit_complete history list recent-1099 session > before\n")
            before = session.directory / 'before'
            session.wait(lambda: before.exists() and '\t37\t' in before.read_text())
            session.send(b'\x12old-delete-entry\x1b')
            session.wait(lambda: '1/1' in session.text and '▏' not in session.text)
            session.send(b'x')
            session.wait(lambda: 'delete entry?' in session.text)
            session.send(b'y')
            session.wait(lambda: not session.panel)
            self.assertEqual(session.capture()[1], '')
            session.send(" binit_complete history list recent-1099 session > after; history -w saved-history\n")
            session.wait(lambda: (session.directory / 'saved-history').exists())
            self.assertEqual((session.directory / 'after').read_text(), before.read_text())
            self.assertNotIn('printf old-delete-entry',
                             (session.directory / 'saved-history').read_text().splitlines())

    def test_ctrl_r_reaches_50000_entry_history_and_cancel_keeps_input(self):
        extra = ("HISTSIZE=60000; history -c; history -s 'printf ancient-needle'\n"
                 "for ((i=0; i<50000; i++)); do history -s \"printf recent-$i\"; done\n"
                 "binit_complete editor history on\n")
        for mode in ('emacs', 'vi'):
            with self.subTest(mode=mode), Session(extra=extra, mode=mode) as session:
                session.send(b'echo draft\x12ancient-needle')
                session.wait(lambda: 'printf ancient-needle' in session.text, seconds=10)
                session.send(b'\x07')
                self.assertEqual(session.capture()[1], 'echo draft')

    def test_native_all_history_list_finds_old_word_and_fuzzy_matches(self):
        for method, query in (('words', 'needle printf'), ('fuzzy', 'prtf ndle')):
            extra = (f"HISTSIZE=4096; BINIT_HISTORY_MATCH={method}; "
                     "history -c; history -s 'printf needle-old'\n"
                     "for ((i=0; i<1100; i++)); do history -s \"printf recent-$i\"; done\n"
                     "binit_complete editor history on\n")
            with self.subTest(method=method), Session(extra=extra) as session:
                session.send(f"binit_complete history list '{query}' all > result\n")
                result = session.directory / 'result'
                session.wait(lambda: result.exists() and 'printf needle-old' in result.read_text())

    def test_confirmed_history_deletion_closes_without_inserting_or_executing(self):
        extra = ("BINIT_HISTORY_DELETE=1\nhistory -s 'touch delete-witness'\n"
                 "binit_complete editor history on\n")
        with Session(extra=extra) as session:
            session.send(b'\x12delete-witness\x1b')
            session.pump(.12)
            session.send(b'x')
            session.wait(lambda: 'delete entry?' in session.text)
            session.send(b'y')
            session.pump(.12)
            self.assertEqual(session.capture()[1], '')
            self.assertFalse((session.directory / 'delete-witness').exists())
            session.send("history | grep -c '^ *[0-9]*  touch delete-witness$'\n")
            session.wait(lambda: '\n0' in session.text)

    def test_context_records_status_before_reading_the_next_command(self):
        extra = "BINIT_HISTORY_CONTEXT=1\nbinit_complete editor history on\n"
        with Session(extra=extra, framework=True) as session:
            session.send("false # context-witness\n")
            session.pump(.15)
            session.send("binit_complete history list 'false # context-witness' session\n")
            session.wait(lambda: b'\t1\t' in session.transcript)

    def test_control_t_cycles_history_scope_with_the_current_query(self):
        extra = ("HISTCONTROL=ignorespace; BINIT_HISTORY_CONTEXT=1; BINIT_HISTORY_SCOPE=directory\n"
                 "binit_complete editor history on\n")
        for mode in ('emacs', 'vi'):
            with self.subTest(mode=mode), Session(extra=extra, mode=mode) as session:
                (session.directory / 'seed').write_text(
                    "history -c; history -s 'printf untracked-scope'\n"
                    "binit_complete history record 0 /\n"
                    "history -s 'printf elsewhere-scope'; binit_complete history record 2 \"$PWD\"\n"
                    "history -s 'printf here-scope'; binit_complete history record 0 \"$PWD\"\n")
                session.send(" source ./seed; printf 'scope seeded\\n'\n")
                session.wait(lambda: '\nscope seeded' in session.text)
                session.send(b'printf draft\x12scope')
                session.wait(lambda: 'history  directory  scope▏  1/1' in session.text)
                self.assertNotIn('printf elsewhere-scope', session.text)
                session.send(b'\x14')
                session.wait(lambda: 'history  session  scope▏  1/2' in session.text)
                self.assertIn('printf elsewhere-scope', session.text)
                session.send(b'\x14')
                session.wait(lambda: 'history  all  scope▏  1/3' in session.text)
                self.assertIn('printf untracked-scope', session.text)
                session.send(b'\x07')
                self.assertEqual(session.capture()[1], 'printf draft')

    def test_control_t_recovers_from_an_empty_scope_in_a_narrow_terminal(self):
        extra = ("HISTCONTROL=ignorespace; BINIT_HISTORY_CONTEXT=1; BINIT_HISTORY_SCOPE=directory\n"
                 "history -c; history -s 'printf kept'\n"
                 "binit_complete editor history on\n")
        with Session(extra=extra, columns=28) as session:
            session.send(b'\x12kept')
            session.wait(lambda: 'No matches' in session.text)
            session.send(b'\x14\x14')
            session.wait(lambda: 'all  kept▏  1/1' in session.text)
            session.send(b'\r')
            self.assertEqual(session.capture()[1], 'printf kept')

    def test_editor_status_reports_actual_flags(self):
        with Session(framework=True) as session:
            session.send('binit editor\n')
            session.wait(lambda: 'suggestions   off' in session.text)
            self.assertIn('history       off', session.text)
            session.send('binit_complete editor history on; binit editor\n')
            session.wait(lambda: 'history       search' in session.text)
            self.assertIn('suggestions   on', session.text)
            session.send('binit_complete editor history off; binit editor\n')
            session.wait(lambda: session.text.count('history       off') == 2)

    def test_completion_menu_does_not_display_a_history_suggestion_as_input(self):
        extra = "history -s 'demo_cmd value history'\nbinit_complete editor history on\n"
        with Session(extra=extra) as session:
            session.send('demo_cmd va')
            session.wait(lambda: 'demo_cmd value history' in session.text)
            session.send(b'\t')
            session.wait(lambda: session.panel)
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
            self.assertTrue(session.panel)
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
            session.wait(lambda: 'history       search' in session.text)
            self.assertIn('suggestions   on', session.text)

    def test_editor_status_reports_fallback_without_loading_a_module(self):
        with Session(framework=True) as session:
            session.send('enable -n binit_complete; binit editor; printf "STATUS:%s\\n" "$?"\n')
            session.wait(lambda: 'binit: native editor unavailable in this shell' in session.text)
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
            session.wait(lambda: 'tab custom' in session.text)
            self.assertNotIn('right accept', session.text)
            self.assertNotIn('ctrl-f accept', session.text)
            self.assertNotIn('ctrl-r history', session.text)


if __name__ == '__main__':
    unittest.main()
