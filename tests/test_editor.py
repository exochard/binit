import fcntl
import os
import re
import signal
import struct
import termios
import unittest

from test_completion import MODULE, Session

PROMPT = 'READY> '
WRAPPED = 'echo ' + 'a' * 28 + '; ls -l | cat'


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


def foreground(session, offset, prompt_cells=len(PROMPT)):
    """Foreground colour of the input byte at offset, following the 40-column wrap."""
    position = prompt_cells + offset
    cell = session.screen.buffer[prompt_row(session) + position // 40][position % 40]
    return cell.fg


class EditorTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('BINIT_EXPECT_HIGHLIGHT') == '1', 'grammar-enabled native build required')
    def test_highlighting_never_executes_input_and_preserves_line(self):
        with Session(extra='binit_complete editor highlighting on\n') as session:
            marker = session.directory / 'highlight-executed'
            line = 'echo $(touch highlight-executed)'
            session.send(line)
            session.wait(lambda: '\x1b[36m' in bytes(session.transcript).decode(errors='replace'))
            self.assertEqual(session.capture()[1], line)
            self.assertFalse(marker.exists())

    @unittest.skipUnless(os.environ.get('BINIT_EXPECT_HIGHLIGHT') == '1', 'grammar-enabled native build required')
    def test_alias_command_is_left_uncolored(self):
        with Session(extra="alias magic='if true; then'\nbinit_complete editor highlighting on\n") as session:
            session.send('magi')
            session.pump(.2)
            session.transcript.clear()
            session.send('c')
            session.pump(.2)
            self.assertEqual(session.capture()[1], 'magic')
            self.assertNotIn('\x1b[36m', bytes(session.transcript).decode(errors='replace'))

    @unittest.skipUnless(os.environ.get('BINIT_EXPECT_HIGHLIGHT') == '1', 'grammar-enabled native build required')
    def test_suggestions_continue_after_highlighting_is_disabled(self):
        with Session(extra="history -s 'echo sample-suggestion'\nbinit_complete editor highlighting on\nbinit_complete editor suggestions on\nbinit_complete editor highlighting off\n") as session:
            session.send('echo sam')
            session.wait(lambda: 'echo sample-suggestion' in session.text)
            session.send(b'\x06')
            self.assertEqual(session.capture()[1], 'echo sample-suggestion')

    def test_history_suggestion_is_visible_and_accepted(self):
        with Session(extra="history -s 'echo sample-suggestion'\nbinit_complete editor suggestions on\n") as session:
            session.send('echo sam')
            session.wait(lambda: 'echo sample-suggestion' in session.text)
            session.send(b'\x06')
            self.assertEqual(session.capture()[1], 'echo sample-suggestion')

    def test_history_suggestion_can_be_disabled(self):
        with Session(extra="history -s 'echo sample-suggestion'\nbinit_complete editor suggestions on\nbinit_complete editor suggestions off\n") as session:
            session.send('echo sam')
            session.pump(.2)
            self.assertNotIn('echo sample-suggestion', session.text)
            self.assertEqual(session.capture()[1], 'echo sam')

    def test_changing_to_an_unmatched_prefix_clears_the_suggestion(self):
        with Session(extra="history -s 'echo sample-suggestion'\nbinit_complete editor suggestions on\n") as session:
            session.send('echo sam')
            session.wait(lambda: 'echo sample-suggestion' in session.text)
            session.send('z')
            session.wait(lambda: 'echo samz' in session.text)
            self.assertNotIn('sample-suggestion', session.text)
            self.assertEqual(session.capture()[1], 'echo samz')

    def test_control_characters_in_history_are_not_rendered(self):
        with Session(extra="history -s $'echo sam\\033[31mple'\nbinit_complete editor suggestions on\n") as session:
            session.send('echo sam')
            session.pump(.2)
            self.assertNotIn('\x1b[31m', bytes(session.transcript).decode(errors='replace'))
            self.assertEqual(session.capture()[1], 'echo sam')

    def test_moving_cursor_hides_the_suggestion(self):
        with Session(extra="history -s 'echo sample-suggestion'\nbinit_complete editor suggestions on\n") as session:
            session.send('echo sam')
            session.wait(lambda: 'echo sample-suggestion' in session.text)
            session.send(b'\x02')
            session.wait(lambda: 'sample-suggestion' not in session.text)
            self.assertEqual(session.capture(), (7, 'echo sam'))

    def test_unicode_history_suggestion_preserves_input(self):
        with Session(extra="history -s 'echo caffè'\nbinit_complete editor suggestions on\n") as session:
            session.send('echo caf')
            session.wait(lambda: 'echo caffè' in session.text)
            session.send(b'\x06')
            self.assertEqual(session.capture()[1], 'echo caffè')

    def test_read_e_does_not_display_shell_history(self):
        with Session(extra="history -s 'echo sample-suggestion'\nbinit_complete editor suggestions on\n") as session:
            session.send("read -e -p 'SECRET> ' reply\n")
            session.wait(lambda: 'SECRET> ' in session.text)
            session.send('echo sam')
            session.pump(.2)
            self.assertNotIn('sample-suggestion', session.text)
            session.send(b'\x06\n')
            session.wait(lambda: 'READY> ' in session.text.split('SECRET>')[-1])
            session.send("printf 'READ_RESULT:%s\\n' \"$reply\"\n")
            session.wait(lambda: 'READ_RESULT:echo sam' in session.text)
            self.assertNotIn('READ_RESULT:echo sample-suggestion', session.text)

    def test_colored_prompt_shows_unicode_suggestion(self):
        with Session(extra="history -s 'echo café-suggestion'\nbinit_complete editor suggestions on\n") as session:
            session.send("PS1=$'\\[\\e[31m\\]READY\\[\\e[0m\\]> '\n")
            session.pump(.2)
            session.send('echo café')
            session.wait(lambda: 'echo café-suggestion' in session.text)
            session.send(b'\x06')
            self.assertEqual(session.capture()[1], 'echo café-suggestion')

    def test_foreign_hook_chaining_old_callback_does_not_recurse(self):
        fixture = MODULE.parent / 'binit_redisplay_test.so'
        if not fixture.is_file():
            self.skipTest('native redisplay fixture not built')
        extra = (f"enable -f '{fixture}' binit_redisplay_test\n"
                 "history -s 'echo sample-suggestion'\n"
                 "binit_complete editor suggestions on\n"
                 "binit_redisplay_test on\n"
                 "binit_complete editor suggestions off\n"
                 "binit_complete editor suggestions on\n")
        with Session(extra=extra) as session:
            session.send('echo sam')
            session.wait(lambda: 'echo sample-suggestion' in session.text)
            session.send(b'\x06')
            self.assertEqual(session.capture()[1], 'echo sample-suggestion')

    def test_idle_poll_waits_until_input_is_empty(self):
        extra = ("__binit_dispatch_pending=0\n"
                 "__binit_event_dispatch() { : > .poll-marker; __binit_dispatch_pending=0; __binit_event_changed=1; }\n"
                 "bind -x '\"\\C-x\\C-p\": __binit_dispatch_pending=1'\n"
                 "binit_complete poll\n")
        with Session(extra=extra) as session:
            marker = session.directory / '.poll-marker'
            session.send(b'echo holding\x18\x10')
            session.pump(.4)
            self.assertFalse(marker.exists())
            session.send(b'\x15')
            session.wait(marker.exists)

    def test_idle_poll_does_not_run_during_read_e(self):
        extra = ("__binit_dispatch_pending=0\n"
                 "__binit_event_dispatch() { : > .poll-marker; __binit_dispatch_pending=0; __binit_event_changed=1; }\n"
                 "binit_complete poll\n")
        with Session(extra=extra) as session:
            marker = session.directory / '.poll-marker'
            session.send("__binit_dispatch_pending=1; read -e -p 'POLL_READ> ' reply\n")
            session.wait(lambda: 'POLL_READ> ' in session.text)
            session.pump(.4)
            self.assertFalse(marker.exists())
            session.send('done\n')
            session.wait(marker.exists)

    def test_idle_poll_chains_the_previous_event_hook(self):
        fixture = MODULE.parent / 'binit_redisplay_test.so'
        if not fixture.is_file():
            self.skipTest('native redisplay fixture not built')
        extra = f"enable -f '{fixture}' binit_redisplay_test\nbinit_redisplay_test event\nbinit_complete poll\n"
        with Session(extra=extra) as session:
            session.pump(.4)
            session.send('binit_redisplay_test count\n')
            session.wait(lambda: bool(re.search(r'EVENT_COUNT:[1-9][0-9]*', session.text)))

    def test_idle_poll_is_rate_limited(self):
        extra = ("__binit_dispatch_pending=1\n"
                 "__binit_event_dispatch() { printf x >> .poll-count; __binit_event_changed=0; }\n"
                 "binit_complete poll\n")
        with Session(extra=extra) as session:
            session.pump(1.1)
            calls = (session.directory / '.poll-count').read_text()
            self.assertGreaterEqual(len(calls), 1)
            self.assertLessEqual(len(calls), 5)

    def test_idle_poll_waits_for_secondary_prompt_to_finish(self):
        extra = ("__binit_dispatch_pending=0\n"
                 "__binit_event_dispatch() { : > .poll-marker; __binit_dispatch_pending=0; __binit_event_changed=1; }\n"
                 "bind -x '\"\\C-x\\C-p\": __binit_dispatch_pending=1'\n"
                 "binit_complete poll\n")
        with Session(extra=extra) as session:
            marker = session.directory / '.poll-marker'
            session.send('if true; then\n')
            session.pump(.3)
            session.send(b'\x18\x10')
            session.pump(.4)
            self.assertFalse(marker.exists())
            session.send(':\nfi\n')
            session.wait(marker.exists)

    def test_wait_hook_avoids_secondary_prompt(self):
        extra = ("__binit_wait_pending=0\n"
                 "__binit_wait_dispatch() { : > .wait-marker; __binit_wait_pending=0; }\n"
                 "bind -x '\"\\C-x\\C-p\": __binit_wait_pending=1'\n"
                 "binit_complete wait\n")
        with Session(extra=extra) as session:
            marker = session.directory / '.wait-marker'
            session.send('if true; then\n')
            session.pump(.3)
            session.send(b'\x18\x10')
            session.send(':\n')
            session.pump(.4)
            self.assertFalse(marker.exists())
            session.send('fi\n')
            session.wait(marker.exists)

    @unittest.skipUnless(os.environ.get('BINIT_EXPECT_HIGHLIGHT') == '1', 'grammar-enabled native build required')
    def test_colored_prompt_highlights_unicode_input(self):
        with Session(extra='binit_complete editor highlighting on\n') as session:
            session.send("PS1=$'\\[\\e[31m\\]READY\\[\\e[0m\\]> '\n")
            session.pump(.2)
            session.transcript.clear()
            session.send('echo café')
            session.wait(lambda: '\x1b[36m' in bytes(session.transcript).decode(errors='replace'))
            self.assertEqual(session.capture()[1], 'echo café')

    def test_wrapped_input_keeps_a_truncated_suggestion_and_accepts_the_full_text(self):
        entry = 'echo ' + 'x' * 30 + '-suggestion-' + 'y' * 60
        with Session(extra=f"history -s '{entry}'\nbinit_complete editor suggestions on\n", columns=40) as session:
            settle(session)
            session.send('echo ' + 'x' * 30)
            session.wait(lambda: '-suggestion-' in session.text)
            row = session.screen.display[session.screen.cursor.y]
            self.assertEqual(row[session.screen.cursor.x:].rstrip(), entry[35:35 + 36] + '…')
            session.send(b'\x06')
            self.assertEqual(session.capture()[1], entry)

    def test_cursor_and_suggestion_stay_right_across_the_right_margin(self):
        entry = 'abcdefghij' * 8
        with Session(extra=f"history -s '{entry}'\nbinit_complete editor suggestions on\n", columns=40) as session:
            settle(session)
            start = prompt_row(session)
            sent = 0
            for length in range(30, 37):
                session.send(entry[sent:length])
                sent = length
                position = len(PROMPT) + length
                row, column = divmod(position, 40)
                suffix, available = entry[length:], 40 - column - 1
                ghost = '' if available < 1 else suffix if len(suffix) <= available else suffix[:available - 1] + '…'
                session.wait(lambda: (session.screen.cursor.y, session.screen.cursor.x) == (start + row, column) and
                             session.screen.display[start + row][column:].rstrip() == ghost)
                self.assertEqual(typed(session), entry[:length])

    def test_multiline_prompt_shows_the_suggestion(self):
        with Session(extra="history -s 'echo sample-suggestion'\nbinit_complete editor suggestions on\n") as session:
            settle(session)
            session.send("PS1=$'top line\\nREADY> '\n")
            session.pump(.2)
            session.send('echo sam')
            session.wait(lambda: 'echo sample-suggestion' in session.text)
            session.send(b'\x06')
            self.assertEqual(session.capture()[1], 'echo sample-suggestion')

    def test_marked_title_in_prompt_shows_the_suggestion(self):
        with Session(extra="history -s 'echo sample-suggestion'\nbinit_complete editor suggestions on\n") as session:
            settle(session)
            session.send("PS1='\\[\\e]0;t\\a\\]READY> '\n")
            session.pump(.2)
            session.send('echo sam')
            session.wait(lambda: 'echo sample-suggestion' in session.text)
            session.send(b'\x06')
            self.assertEqual(session.capture()[1], 'echo sample-suggestion')

    @unittest.skipUnless(os.environ.get('BINIT_EXPECT_HIGHLIGHT') == '1', 'grammar-enabled native build required')
    def test_wrapped_input_is_colored_on_every_row(self):
        for mode in ('vi', 'emacs'):
            with self.subTest(mode=mode), Session(extra='binit_complete editor highlighting on\n', mode=mode, columns=40) as session:
                settle(session)
                start = prompt_row(session)
                session.send(WRAPPED)
                session.wait(lambda: foreground(session, WRAPPED.index('cat')) != 'default')
                command = foreground(session, 0)
                self.assertNotEqual(command, 'default')
                self.assertEqual(foreground(session, WRAPPED.index('ls')), command)
                self.assertEqual(foreground(session, WRAPPED.index('cat')), command)
                self.assertEqual(typed(session), WRAPPED)
                self.assertEqual((session.screen.cursor.y, session.screen.cursor.x), (start + 1, 13))
                # The repaint is one run the terminal wraps itself; cursor moves between rows would
                # end the terminal's soft wrap and copying the command would insert a line break.
                plain = re.sub(r'\x1b\[[0-9;]*m', '', bytes(session.transcript).decode(errors='replace'))
                self.assertTrue(plain[plain.rindex('echo '):].startswith(WRAPPED), plain[-200:])

    @unittest.skipUnless(os.environ.get('BINIT_EXPECT_HIGHLIGHT') == '1', 'grammar-enabled native build required')
    def test_multiline_prompt_is_highlighted(self):
        with Session(extra='binit_complete editor highlighting on\n') as session:
            settle(session)
            session.send("PS1=$'top line\\nREADY> '\n")
            session.pump(.2)
            session.send('echo $(true)')
            session.wait(lambda: foreground(session, 0) != 'default')
            self.assertEqual(typed(session), 'echo $(true)')

    @unittest.skipUnless(os.environ.get('BINIT_EXPECT_HIGHLIGHT') == '1', 'grammar-enabled native build required')
    def test_mode_string_in_prompt_keeps_colors_aligned(self):
        for mode, label in (('vi', '(ins)'), ('emacs', '@')):
            with self.subTest(mode=mode), Session(extra="bind 'set show-mode-in-prompt on'\nbinit_complete editor highlighting on\n",
                                                  mode=mode, columns=40) as session:
                settle(session)
                cells = len(label + PROMPT)
                session.send(WRAPPED)
                session.wait(lambda: foreground(session, WRAPPED.index('cat'), cells) != 'default')
                command = foreground(session, 0, cells)
                self.assertNotEqual(command, 'default')
                self.assertEqual(foreground(session, WRAPPED.index('ls'), cells), command)
                self.assertEqual(foreground(session, WRAPPED.index('cat'), cells), command)
                self.assertEqual(typed(session), WRAPPED)

    @unittest.skipUnless(os.environ.get('BINIT_EXPECT_HIGHLIGHT') == '1', 'grammar-enabled native build required')
    def test_input_taller_than_the_screen_is_plain(self):
        with Session(extra='binit_complete editor highlighting on\n', rows=6, columns=40) as session:
            settle(session)
            session.send('echo hi ')
            session.wait(lambda: foreground(session, 0) != 'default')
            line = 'echo hi ' + 'a' * 192
            session.send(line[8:])
            session.pump(.4)
            colors = {cell.fg for row in session.screen.buffer.values() for cell in row.values()}
            self.assertEqual(colors, {'default'})
            self.assertEqual(session.capture()[1], line)

SUGGEST = "history -s 'echo sample-suggestion'\n"
GHOST = 'sample-suggestion'


def input_row(session):
    return next(row for row in session.screen.display if row.startswith(PROMPT + 'echo sam'))


class SuggestionLifecycleTests(unittest.TestCase):
    CONFIGS = (('suggestions', 'binit_complete editor suggestions on\n'),
               ('history', 'binit_complete editor history on\n'))

    def test_enter_erases_the_ghost(self):
        for mode in ('vi', 'emacs'):
            for name, setup in self.CONFIGS:
                with self.subTest(mode=mode, config=name), Session(extra=SUGGEST + setup, mode=mode) as session:
                    session.send('echo sam')
                    session.wait(lambda: GHOST in session.text)
                    session.send(b'\r')
                    session.pump(.3)
                    self.assertNotIn(GHOST, session.text)
                    self.assertEqual(session.screen.display[session.screen.cursor.y].rstrip(), PROMPT.rstrip())

    def test_enter_erases_the_ghost_on_a_blank_wrap_row(self):
        entry = ': ' + 'x' * 31 + '-suggestion-tail'
        for mode in ('vi', 'emacs'):
            for name, setup in self.CONFIGS:
                with self.subTest(mode=mode, config=name), Session(
                        extra=f"history -s '{entry}'\n" + setup, mode=mode, columns=40) as session:
                    settle(session)
                    session.send(': ' + 'x' * 31)
                    session.wait(lambda: '-suggestion-' in session.text)
                    session.send(b'\r')
                    session.pump(.3)
                    # The next prompt may overwrite the first cells of a leftover; the tail still shows.
                    self.assertNotIn('tail', session.text)

    def test_ctrl_j_erases_the_ghost(self):
        with Session(extra=SUGGEST + 'binit_complete editor suggestions on\n') as session:
            session.send('echo sam')
            session.wait(lambda: GHOST in session.text)
            session.send(b'\n')
            session.pump(.3)
            self.assertNotIn(GHOST, session.text)

    def test_interrupt_leaves_only_the_caret_echo(self):
        for mode in ('vi', 'emacs'):
            for name, setup in self.CONFIGS:
                with self.subTest(mode=mode, config=name), Session(extra=SUGGEST + setup, mode=mode) as session:
                    session.send('echo sam')
                    session.wait(lambda: GHOST in session.text)
                    session.send(b'\x03')
                    session.pump(.3)
                    self.assertEqual(input_row(session).rstrip(), PROMPT + 'echo sam^C')
                    self.assertNotIn(GHOST, session.text)
                    self.assertNotIn('History', session.text)

    def test_interrupt_in_the_middle_keeps_the_text_after_the_cursor(self):
        for mode in ('vi', 'emacs'):
            with self.subTest(mode=mode), Session(extra=SUGGEST + 'binit_complete editor history on\n', mode=mode) as session:
                session.send('echo sample')
                session.wait(lambda: 'History' in session.text)
                session.send(b'\x1b[D' * 3)
                session.pump(.2)
                session.send(b'\x03')
                session.pump(.3)
                self.assertEqual(input_row(session).rstrip(), PROMPT + 'echo sam^Ce')
                self.assertNotIn('History', session.text)
                self.assertNotIn(GHOST, session.text)


class AcceptKeyTests(unittest.TestCase):
    KEYS = (b'\x06', b'\x1b[C', b'\x1bOC', b'\x1b[F', b'\x1bOF')

    def test_every_accept_key_inserts_the_suggestion(self):
        for mode in ('vi', 'emacs'):
            keys = self.KEYS + ((b'\x05',) if mode == 'emacs' else ())
            for key in keys:
                with self.subTest(mode=mode, key=key), Session(extra=SUGGEST + 'binit_complete editor suggestions on\n', mode=mode) as session:
                    session.send('echo sam')
                    session.wait(lambda: GHOST in session.text)
                    session.send(key)
                    self.assertEqual(session.capture()[1], 'echo sample-suggestion')

    def test_keys_keep_their_meaning_without_a_suggestion(self):
        for mode in ('vi', 'emacs'):
            with self.subTest(mode=mode), Session(extra=SUGGEST + 'binit_complete editor suggestions on\n', mode=mode) as session:
                session.send('abc\x1b[D\x1b[D\x1b[C')
                self.assertEqual(session.capture(), (2, 'abc'))
        with Session(extra=SUGGEST + 'binit_complete editor suggestions on\n', mode='vi') as session:
            session.send(b'abc\x06')
            self.assertEqual(session.capture(), (4, 'abc\x06'))

    def test_disable_restores_every_binding(self):
        extra = (SUGGEST + "binit_complete editor suggestions on\nbinit_complete editor suggestions off\n"
                 "bind -m vi-insert -p > .binds-vi\nbind -m emacs-standard -p > .binds-emacs\n")
        with Session(extra=extra) as session:
            session.pump(.3)
            vi = (session.directory / '.binds-vi').read_text()
            emacs = (session.directory / '.binds-emacs').read_text()
        self.assertIn('"\\C-f": self-insert', vi)
        self.assertIn('"\\C-f": forward-char', emacs)
        self.assertIn('"\\e[C": forward-char', emacs)
        self.assertIn('"\\C-e": end-of-line', emacs)
        for text in (vi, emacs):
            self.assertIn('"\\e[C": forward-char', text)
            self.assertRegex(text, r'"\\e\[F": end-of-line')

    def test_history_owned_suggestions_restore_bindings_on_disable(self):
        extra = (SUGGEST + "binit_complete editor history on\nbinit_complete editor history off\n"
                 "bind -m vi-insert -p > .binds-vi\n")
        with Session(extra=extra) as session:
            session.pump(.3)
            self.assertIn('"\\C-f": self-insert', (session.directory / '.binds-vi').read_text())
            session.send('echo sam')
            session.pump(.2)
            self.assertNotIn(GHOST, session.text)

    def test_unload_restores_bindings(self):
        extra = (SUGGEST + "binit_complete editor suggestions on\nenable -d binit_complete\n"
                 "bind -m vi-insert -p > .binds-vi\n")
        with Session(extra=extra) as session:
            session.pump(.3)
            self.assertIn('"\\C-f": self-insert', (session.directory / '.binds-vi').read_text())

    def test_user_binding_made_before_enable_is_kept(self):
        extra = (SUGGEST + "bind '\"\\e[C\": backward-char'\nbinit_complete editor suggestions on\n")
        for mode in ('vi', 'emacs'):
            with self.subTest(mode=mode), Session(extra=extra, mode=mode) as session:
                session.send('echo sam')
                session.wait(lambda: GHOST in session.text)
                session.send(b'\x1b[C')
                self.assertEqual(session.capture(), (7, 'echo sam'))
                session.send(b'\x15binit_complete editor suggestions off\n')
                session.wait(lambda: session.text.count(PROMPT) >= 2)
                session.send(b'abc\x1b[C')
                self.assertEqual(session.capture(), (2, 'abc'))

    def test_user_binding_made_after_enable_is_kept(self):
        extra = (SUGGEST + "binit_complete editor suggestions on\nbind '\"\\e[C\": backward-char'\n"
                 "binit_complete editor suggestions off\n")
        for mode in ('vi', 'emacs'):
            with self.subTest(mode=mode), Session(extra=extra, mode=mode) as session:
                session.send(b'abc\x1b[C')
                self.assertEqual(session.capture(), (2, 'abc'))

    def resize(self, session, rows, columns):
        session.screen.resize(rows, columns)
        fcntl.ioctl(session.terminal.fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
        os.kill(session.terminal.pid, signal.SIGWINCH)
        session.pump(.3)

    def test_resizing_keeps_a_single_prompt(self):
        extra = SUGGEST + "binit_complete editor suggestions on\n"
        for line in ('', 'echo sam'):
            with self.subTest(line=line), Session(extra=extra) as session:
                settle(session)
                session.send(line)
                session.wait(lambda: typed(session) == line)
                for columns in (60, 70):
                    self.resize(session, 24, columns)
                    row = session.screen.display[prompt_row(session)]
                    self.assertEqual(row.count(PROMPT), 1, row)
                    self.assertEqual(typed(session), line)

    @unittest.skipUnless(os.environ.get('BINIT_EXPECT_HIGHLIGHT') == '1', 'grammar-enabled native build required')
    def test_term_assignment_keeps_wrapped_input_aligned(self):
        extra = 'binit_complete editor highlighting on\nbinit_complete editor suggestions on\n'
        with Session(extra=extra, columns=40) as session:
            settle(session)
            session.send('TERM=xterm-256color\n')
            session.pump(.3)
            start = prompt_row(session)
            session.send(WRAPPED)
            session.wait(lambda: typed(session) == WRAPPED)
            session.send(b'\x01X')
            session.pump(.3)
            rows = session.screen.display
            self.assertEqual(rows[start], (PROMPT + 'X' + WRAPPED)[:40])
            self.assertEqual(rows[start + 1].rstrip(), (PROMPT + 'X' + WRAPPED)[40:])
            self.assertEqual(session.capture(), (1, 'X' + WRAPPED))

    @unittest.skipUnless(os.environ.get('BINIT_EXPECT_HIGHLIGHT') == '1', 'grammar-enabled native build required')
    def test_horizontal_scroll_mode_leaves_long_lines_to_readline(self):
        extra = "bind 'set horizontal-scroll-mode on'\nbinit_complete editor highlighting on\n"
        with Session(extra=extra, columns=40) as session:
            settle(session)
            start = prompt_row(session)
            session.send(WRAPPED)
            session.pump(.3)
            session.send(b'\x01')
            session.pump(.3)
            rows = session.screen.display
            self.assertEqual(rows[start + 1].strip(), '')
            self.assertEqual(session.screen.cursor.y, start)
            self.assertEqual(session.capture(), (0, WRAPPED))

    @unittest.skipUnless(os.environ.get('BINIT_EXPECT_HIGHLIGHT') == '1', 'grammar-enabled native build required')
    def test_edited_history_line_keeps_its_mark_and_alignment(self):
        extra = "bind 'set mark-modified-lines on'\nhistory -s 'echo ab'\nbinit_complete editor highlighting on\n"
        with Session(extra=extra) as session:
            session.send(b'\x1b[A')
            session.wait(lambda: 'echo ab' in session.text)
            session.send('cdefg')
            session.wait(lambda: 'abcdefg' in session.text)
            session.pump(.2)
            row = session.screen.display[session.screen.cursor.y]
            self.assertEqual(row.rstrip(), '*READY> echo abcdefg')
            self.assertEqual(session.screen.cursor.x, len('*READY> echo abcdefg'))
