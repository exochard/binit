import unittest
import os
import re

from test_completion import MODULE, Session


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
