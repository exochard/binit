import re
import unittest
import fcntl
import os
import signal
import struct
import termios
from unittest.mock import patch

from test_completion import MODULE, Session


@patch.dict(os.environ, BINIT_HISTORY_MODE='live')
class LiveHistoryTests(unittest.TestCase):
    def test_startup_history_keeps_completion_available_after_dismissal(self):
        extra = "history -s 'demo_cmd value one'\nbinit_complete editor history on\n"
        for mode in ('vi', 'emacs'):
            with self.subTest(mode=mode), Session(extra=extra, mode=mode, framework=True) as session:
                session.send('demo_cmd va')
                session.wait(lambda: any(row.rstrip() == 'history' for row in session.screen.display))
                session.send(b'\t')
                session.wait(lambda: re.search(r'history  \d+/\d+', session.text))
                session.send(b'\x1b')
                session.pump(.1)
                session.send(b'\t')
                session.wait(lambda: session.panel)
                session.send(b'\x1b')
                session.pump(.1)
                self.assertEqual(session.capture()[1], 'demo_cmd va')

    def test_cancel_keeps_a_single_prompt_on_the_input_row(self):
        with Session(extra="history -s 'echo git-status'\nbinit_complete editor history on\n") as session:
            session.send('echo git')
            session.wait(lambda: any(row.rstrip() == 'history' for row in session.screen.display))
            session.send(b'\t')
            session.wait(lambda: re.search(r'history  \d+/\d+', session.text))
            session.send(b'\x1b')
            session.pump(.1)
            row = session.screen.display[session.screen.cursor.y]
            self.assertEqual(row.count('READY>'), 1, row)
            self.assertEqual(session.capture()[1], 'echo git')

    def test_matches_appear_then_navigation_inserts_without_executing(self):
        extra = ("history -s 'echo git-jump'\n"
                 "history -s 'echo git-status'\n"
                 "binit_complete editor history on\n")
        with Session(extra=extra) as session:
            session.send('echo git')
            session.wait(lambda: 'echo git-status' in session.text and 'echo git-jump' in session.text)
            session.send(b'\t')
            session.wait(lambda: 'history  1/2' in session.text)
            session.send('j\n')
            self.assertEqual(session.capture()[1], 'echo git-jump')

    def test_search_accepts_j_and_k_as_text(self):
        extra = "history -s 'echo apple-jk'\nhistory -s 'echo apple-xyz'\nbinit_complete editor history on\n"
        with Session(extra=extra) as session:
            session.send('echo apple')
            session.wait(lambda: 'echo apple-jk' in session.text)
            session.send(b'\t/jk\n')
            self.assertEqual(session.capture()[1], 'echo apple-jk')

    def test_search_escape_returns_to_navigation_then_closes(self):
        extra = "history -s 'echo unique-jk'\nbinit_complete editor history on\n"
        with Session(extra=extra) as session:
            session.send('echo uni')
            session.wait(lambda: 'echo unique-jk' in session.text)
            session.send(b'\t/jk')
            session.wait(lambda: 'history  jk▏' in session.text)
            session.send(b'\x1b')
            # Past the Esc wait, so j is not read as Alt-j.
            session.pump(.1)
            # Browsing hides the query; j now moves instead of extending it.
            session.send('j')
            session.pump(.2)
            self.assertIn('history  1/1', session.text)
            self.assertNotIn('jkj▏', session.text)
            session.send(b'\x1b')
            session.pump(.2)
            self.assertEqual(session.capture()[1], 'echo uni')

    def test_control_r_search_works_with_empty_input(self):
        extra = "history -s 'echo unique-jk'\nbinit_complete editor history on\n"
        with Session(extra=extra) as session:
            session.send(b'\x12')
            session.wait(lambda: '▏' in session.text)
            session.send(b'unique-jk\n')
            self.assertEqual(session.capture()[1], 'echo unique-jk')

    def test_control_r_search_works_in_vi_insert_mode(self):
        extra = "history -s 'echo unique-jk'\nbinit_complete editor history on\n"
        with Session(extra=extra, mode='vi') as session:
            session.send(b'\x12')
            session.wait(lambda: '▏' in session.text)
            session.send(b'unique-jk\n')
            self.assertEqual(session.capture()[1], 'echo unique-jk')

    def test_no_matches_preserves_normal_completion(self):
        with Session(extra="history -s 'echo other'\nbinit_complete editor history on\n") as session:
            session.open('demo_cmd va')
            self.assertTrue(session.panel)

    def test_disable_restores_normal_completion(self):
        extra = "history -s 'demo_cmd value one'\nbinit_complete editor history on\nbinit_complete editor history off\n"
        with Session(extra=extra) as session:
            session.open('demo_cmd va')
            self.assertTrue(session.panel)

    def test_dismissed_history_allows_normal_completion(self):
        extra = "history -s 'demo_cmd value one'\nbinit_complete editor history on\n"
        with Session(extra=extra) as session:
            session.send('demo_cmd va')
            session.wait(lambda: 'demo_cmd value one' in session.text)
            session.send(b'\t')
            session.wait(lambda: re.search(r'history  \d+/\d+', session.text))
            session.send(b'\x1b')
            session.pump(.2)
            session.send(b'\t')
            session.wait(lambda: session.panel)

    def test_candidates_are_deduplicated_and_case_insensitive(self):
        extra = "history -s 'echo HELLO'\nhistory -s 'echo HELLO'\nbinit_complete editor history on\n"
        with Session(extra=extra) as session:
            session.send('hello')
            session.wait(lambda: 'echo HELLO' in session.text)
            self.assertEqual(session.text.count('echo HELLO'), 1)

    def test_history_never_renders_terminal_controls(self):
        extra = "history -s $'echo x\\033[999mY'\nbinit_complete editor history on\n"
        with Session(extra=extra) as session:
            session.send('echo x')
            session.pump(.2)
            self.assertNotIn(b'\x1b[999m', session.transcript)

    def test_history_never_renders_bidirectional_controls(self):
        extra = "history -s $'echo safe\\u202eEVIL'\nbinit_complete editor history on\n"
        with Session(extra=extra) as session:
            session.send('echo safe')
            session.pump(.2)
            self.assertNotIn('\u202e'.encode(), session.transcript)

    def test_vi_insert_can_focus_and_cancel(self):
        extra = "history -s 'echo git-status'\nbinit_complete editor history on\n"
        with Session(extra=extra, mode='vi') as session:
            session.send('echo git')
            session.wait(lambda: 'echo git-status' in session.text)
            session.send(b'\t')
            session.wait(lambda: 'history  1/1' in session.text)
            session.send(b'\x1b')
            session.pump(.2)
            self.assertEqual(session.capture()[1], 'echo git')

    def test_cancel_restores_original_cursor(self):
        extra = "history -s 'echo git-status'\nbinit_complete editor history on\n"
        with Session(extra=extra) as session:
            session.send('echo git')
            session.wait(lambda: 'echo git-status' in session.text)
            session.send(b'\x02\t')
            session.wait(lambda: re.search(r'history  \d+/\d+', session.text))
            session.send(b'\x1b')
            session.pump(.2)
            self.assertEqual(session.capture(), (7, 'echo git'))

    def test_selection_does_not_execute_command(self):
        extra = "history -s 'touch .history-executed'\nbinit_complete editor history on\n"
        with Session(extra=extra) as session:
            session.send('touch .history')
            session.wait(lambda: 'touch .history-executed' in session.text)
            session.send(b'\t\n')
            self.assertEqual(session.capture()[1], 'touch .history-executed')
            self.assertFalse((session.directory / '.history-executed').exists())

    def test_resize_closes_focused_panel_without_losing_input(self):
        extra = "history -s 'echo git-status'\nbinit_complete editor history on\n"
        with Session(extra=extra) as session:
            session.send('echo git')
            session.wait(lambda: 'echo git-status' in session.text)
            session.send(b'\t')
            session.wait(lambda: 'history  1/1' in session.text)
            fcntl.ioctl(session.terminal.fd, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 100, 0, 0))
            os.kill(session.terminal.pid, signal.SIGWINCH)
            session.pump(.3)
            self.assertEqual(session.capture()[1], 'echo git')

    def test_custom_tab_binding_is_preserved(self):
        extra = ("history -s 'echo unique-entry'\n"
                 "__tab_test() { READLINE_LINE=custom-tab; READLINE_POINT=10; }\n"
                 "bind -m emacs-standard -x '\"\\t\": __tab_test'\n"
                 "binit_complete editor history on\n")
        with Session(extra=extra) as session:
            session.send(b'echo unique\t')
            session.pump(.3)
            self.assertEqual(session.capture()[1], 'custom-tab')

    def test_read_e_does_not_display_history_panel(self):
        extra = "history -s 'echo unique-entry'\nbinit_complete editor history on\n"
        with Session(extra=extra) as session:
            session.send("read -e -p 'READ> ' reply\n")
            session.wait(lambda: 'READ> ' in session.text)
            session.send('echo unique')
            session.pump(.3)
            self.assertNotIn('echo unique-entry', session.text)
            session.send('\n')

    def test_secondary_prompt_does_not_display_history_panel(self):
        extra = "history -s 'echo unique-entry'\nbinit_complete editor history on\n"
        with Session(extra=extra) as session:
            session.send('if true; then\n')
            session.pump(.2)
            session.send('echo unique')
            session.pump(.3)
            self.assertNotIn('echo unique-entry', session.text)
            session.send(b'\x03')

    def test_custom_control_r_binding_is_preserved(self):
        extra = ("__search_test() { READLINE_LINE=custom-search; READLINE_POINT=13; }\n"
                 "bind -m emacs-standard -x '\"\\C-r\": __search_test'\n"
                 "history -s 'echo unique-entry'\nbinit_complete editor history on\n")
        with Session(extra=extra) as session:
            session.send(b'\x12')
            session.pump(.2)
            self.assertEqual(session.capture()[1], 'custom-search')

    def test_missing_cursor_reply_does_not_lose_typeahead(self):
        extra = "history -s 'echo unique-entry'\nbinit_complete editor history on\n"
        with Session(extra=extra, replies=False) as session:
            session.send('echo unique')
            session.pump(.5)
            self.assertEqual(session.capture()[1], 'echo unique')

    def test_interrupt_restores_prompt_and_hides_panel(self):
        extra = "history -s 'echo unique-entry'\nbinit_complete editor history on\n"
        with Session(extra=extra) as session:
            session.send('echo unique')
            session.wait(lambda: 'echo unique-entry' in session.text)
            session.send(b'\x03')
            session.pump(.3)
            session.send("printf '%s%s' RECO VERED\n")
            session.wait(lambda: 'RECOVERED' in session.text)
            self.assertNotIn('echo unique-entry', session.text)

    def test_operate_and_get_next_preserves_command_output_and_prompt(self):
        extra = "history -s 'echo live-history'\nbinit_complete editor history on\n"
        with Session(extra=extra) as session:
            session.send('echo live')
            session.wait(lambda: 'echo live-history' in session.text)
            session.send(b'\x0f')
            session.wait(lambda: any(row.strip() == 'live' for row in session.screen.display))
            self.assertGreaterEqual(session.text.count('READY>'), 2)

    def test_foreign_deprep_chain_remains_usable_after_reenable(self):
        fixture = MODULE.parent / 'binit_redisplay_test.so'
        if not fixture.is_file():
            self.skipTest('native redisplay fixture not built')
        extra = (f"enable -f '{fixture}' binit_redisplay_test\n"
                 "history -s 'echo live-history'\n"
                 "binit_complete editor history on\n"
                 "binit_redisplay_test deprep\n"
                 "binit_complete editor history off\n"
                 "binit_complete editor history on\n")
        with Session(extra=extra) as session:
            session.send('echo live')
            session.wait(lambda: 'echo live-history' in session.text)
            session.send(b'\x0f')
            session.wait(lambda: any(row.strip() == 'live' for row in session.screen.display))
            self.assertGreaterEqual(session.text.count('READY>'), 2)

    def test_foreign_deprep_chain_with_initial_null_survives_reenable(self):
        fixture = MODULE.parent / 'binit_redisplay_test.so'
        if not fixture.is_file():
            self.skipTest('native redisplay fixture not built')
        extra = (f"enable -f '{fixture}' binit_redisplay_test\n"
                 "history -s 'echo live-history'\n"
                 "binit_redisplay_test null-deprep\n"
                 "binit_complete editor history on\n"
                 "binit_redisplay_test deprep\n"
                 "binit_complete editor history off\n"
                 "binit_complete editor history on\n")
        with Session(extra=extra) as session:
            session.send('echo live')
            session.wait(lambda: 'echo live-history' in session.text)
            session.send(b'\x0f')
            session.wait(lambda: any(row.strip() == 'live' for row in session.screen.display))
            self.assertGreaterEqual(session.text.count('READY>'), 2)

    def test_custom_enter_binding_is_preserved(self):
        extra = ("__enter_test() { READLINE_LINE=custom-enter; READLINE_POINT=12; }\n"
                 "bind -m emacs-standard -x '\"\\C-m\": __enter_test'\n"
                 "history -s 'echo unique-entry'\nbinit_complete editor history on\n")
        with Session(extra=extra) as session:
            session.send(b'echo unique\r')
            session.pump(.2)
            self.assertEqual(session.capture()[1], 'custom-enter')


PROMPT = 'READY> '
ENTRY = 'echo ' + 'x' * 70


def prompt_row(session):
    return max(y for y, row in enumerate(session.screen.display) if PROMPT in row)


def settle(session):
    """The harness resize leaves a doubled prompt under a custom redisplay; start a fresh prompt."""
    session.pump(.3)
    session.send(b'\x03')
    session.pump(.3)


def typed(session):
    """The input as it sits on screen, from the last prompt row up to the cursor."""
    rows, cursor = session.screen.display, session.screen.cursor
    start = prompt_row(session)
    return (''.join(rows[start:cursor.y]) + rows[cursor.y][:cursor.x]).split(PROMPT, 1)[-1]


def headers(session):
    return [y for y, row in enumerate(session.screen.display) if row.rstrip() == 'history']


@patch.dict(os.environ, BINIT_HISTORY_MODE='live')
class LivePanelLayoutTests(unittest.TestCase):
    extra = f"history -s '{ENTRY}'\nbinit_complete editor history on\n"

    def test_pasting_across_the_margin_keeps_the_wrapped_rows(self):
        with Session(extra=self.extra, columns=40) as session:
            settle(session)
            session.send('echo x')
            session.wait(lambda: headers(session))
            line = 'echo ' + 'x' * 55
            session.send(line[6:])
            session.wait(lambda: typed(session) == line)
            start = prompt_row(session)
            session.wait(lambda: headers(session) == [start + 2])
            self.assertEqual(typed(session), line)

    def test_header_stays_directly_below_the_last_input_row(self):
        for mode in ('vi', 'emacs'):
            with self.subTest(mode=mode), Session(extra=self.extra, mode=mode, columns=40) as session:
                settle(session)
                start = prompt_row(session)
                session.send('echo ' + 'x' * 20)
                session.wait(lambda: headers(session) == [start + 1])
                session.send('x' * 10)
                session.wait(lambda: headers(session) == [start + 2])
                self.assertEqual(typed(session), 'echo ' + 'x' * 30)
                session.send(b'\x7f' * 10)
                session.wait(lambda: headers(session) == [start + 1])
                self.assertEqual(typed(session), 'echo ' + 'x' * 20)

    def test_panel_scrolls_into_view_on_the_bottom_row(self):
        with Session(extra=self.extra, rows=8) as session:
            settle(session)
            session.send('seq 1 12\n')
            session.wait(lambda: PROMPT in session.screen.display[-1])
            session.send('echo x')
            session.wait(lambda: headers(session))
            rows = session.screen.display
            self.assertEqual(headers(session), [6])
            self.assertIn('echo xxxx', rows[7])
            self.assertEqual((session.screen.cursor.y, session.screen.cursor.x), (5, len(PROMPT) + 6))
            self.assertEqual(typed(session), 'echo x')

    def test_resize_keeps_the_input_rows(self):
        with Session(extra=self.extra, columns=80) as session:
            settle(session)
            session.send('echo ' + 'x' * 10)
            session.wait(lambda: headers(session))
            session.screen.resize(24, 40)
            fcntl.ioctl(session.terminal.fd, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 40, 0, 0))
            os.kill(session.terminal.pid, signal.SIGWINCH)
            session.pump(.3)
            session.send(b'\x0c')  # the test screen cannot reflow, so start from a cleared one
            session.wait(lambda: typed(session) == 'echo ' + 'x' * 10 and headers(session))
            session.send('x' * 30)
            session.wait(lambda: typed(session) == 'echo ' + 'x' * 40)
            session.wait(lambda: headers(session) == [prompt_row(session) + 2])

    def test_multiline_prompt_shows_the_panel(self):
        with Session(extra=self.extra) as session:
            settle(session)
            session.send("PS1=$'top line\\nREADY> '\n")
            session.pump(.2)
            session.send('echo x')
            session.wait(lambda: headers(session) == [prompt_row(session) + 1])
            self.assertEqual(typed(session), 'echo x')

    def test_narrow_terminal_keeps_the_cursor_on_the_input(self):
        extra = "history -s 'echo hello-world'\nbinit_complete editor history on\n"
        for columns in (20, 24, 32):
            with self.subTest(columns=columns), Session(extra=extra, columns=columns) as session:
                settle(session)
                session.send('echo hel')
                session.wait(lambda: headers_prefix(session))
                session.send('lo\x7f')
                session.wait(lambda: typed(session) == 'echo hell')
                start = prompt_row(session)
                self.assertEqual(session.screen.cursor.y, start)
                self.assertEqual(headers_prefix(session), [start + 1])

    def test_reenabled_history_restores_the_terminal_after_a_foreign_hook_unwinds(self):
        fixture = MODULE.parent / 'binit_redisplay_test.so'
        if not fixture.is_file():
            self.skipTest('native redisplay fixture not built')
        extra = (f"enable -f '{fixture}' binit_redisplay_test\n"
                 "binit_complete editor history on\n"
                 "binit_redisplay_test deprep\n"
                 "binit_complete editor history off\n"
                 "binit_redisplay_test deprep-off\n"
                 "binit_complete editor history on\n")
        with Session(extra=extra) as session:
            session.send("stty -a | grep -o -- '-*icanon' | sed 's/^/MODE:/'\n")
            session.wait(lambda: 'MODE:' in session.text.replace("'MODE:", ''))
            modes = [row.strip() for row in session.screen.display if row.strip().startswith('MODE:')]
            self.assertEqual(modes, ['MODE:icanon'])


def headers_prefix(session):
    return [y for y, row in enumerate(session.screen.display) if row.startswith('history')]
