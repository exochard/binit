import fcntl
import os
import re
import shlex
import signal
import struct
import sys
import termios
import textwrap
import time
import unittest

from test_completion import MODULE, Session, terminal

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
    def test_word_and_character_suggestions_keep_end_for_full_acceptance(self):
        for mode, unit, expected in (('emacs', 'word', 'echo naïve'), ('vi', 'character', 'echo n')):
            extra = (f"BINIT_SUGGESTION_ACCEPT={unit}\nhistory -s 'echo naïve example'\n"
                     "binit_complete editor suggestions on\n")
            with self.subTest(unit=unit), Session(extra=extra, mode=mode) as session:
                session.send('echo ')
                session.wait(lambda: 'naïve example' in session.text)
                session.send(b'\x06')
                self.assertEqual(session.capture()[1], expected)
                session.send(b'\x1b[F')
                self.assertEqual(session.capture()[1], 'echo naïve example')

    def test_previous_command_strategy_prefers_the_matching_predecessor(self):
        extra = ("BINIT_SUGGESTION_STRATEGIES=previous,history\n"
                 "history -s 'echo context'\nhistory -s 'git cherry-pick'\n"
                 "history -s 'echo unrelated'\nhistory -s 'git checkout'\n"
                 "history -s 'echo context'\nbinit_complete editor suggestions on\n")
        with Session(extra=extra) as session:
            session.send('git ch')
            session.wait(lambda: 'git cherry-pick' in session.text)
            session.send(b'\x06')
            self.assertEqual(session.capture()[1], 'git cherry-pick')

    def test_completion_suggestion_uses_only_an_isolated_registered_provider(self):
        python = shlex.quote(sys.executable)
        provider = shlex.quote("import sys; sys.stdout.buffer.write(b'answer\\0')")
        extra = "BINIT_SUGGESTION_STRATEGIES=completion\nbinit_complete editor suggestions on\n"
        with Session(extra=extra, framework=True) as session:
            session.send(f"binit completions register suggest_demo {python} -c {provider}\n")
            session.pump(.15)
            session.send('suggest_demo an')
            session.wait(lambda: 'suggest_demo answer' in session.text)
            session.send(b'\x06')
            self.assertEqual(session.capture()[1], 'suggest_demo answer')

    def test_suggestion_ignore_and_style_apply_under_the_input_limit(self):
        extra = ("BINIT_SUGGESTION_LIMIT=6\nBINIT_SUGGESTION_IGNORE='echo secret*'\n"
                 "BINIT_SUGGESTION_STYLE=underline\nhistory -s 'echo safe'\n"
                 "history -s 'echo secret'\nbinit_complete editor suggestions on\n")
        with Session(extra=extra) as session:
            session.send('echo ')
            session.wait(lambda: 'echo safe' in session.text)
            self.assertIn(b'\x1b[4m', session.transcript)
            session.send('sec')
            session.pump(.12)
            self.assertNotIn('echo secret', session.text)

    def test_completion_suggestion_is_cancelled_by_typing(self):
        python = shlex.quote(sys.executable)
        provider = shlex.quote(
            "import sys, time\nfrom pathlib import Path\n"
            "if sys.stdin.buffer.read().split(b'\\0')[1].endswith(b' an'):\n"
            "    Path('preview-started').touch()\n    time.sleep(.6)\n"
            "    Path('preview-finished').touch()\n    sys.stdout.buffer.write(b'answer\\0')\n"
        )
        extra = "BINIT_SUGGESTION_STRATEGIES=completion\nbinit_complete editor suggestions on\n"
        with Session(extra=extra, framework=True) as session:
            session.send(f"binit completions register suggest_demo {python} -c {provider}\n")
            session.pump(.15)
            session.send('suggest_demo an')
            session.wait(lambda: (session.directory / 'preview-started').exists())
            session.send('x')
            self.assertEqual(session.capture()[1], 'suggest_demo anx')
            session.pump(.65)
            self.assertFalse((session.directory / 'preview-finished').exists())

    def test_prompt_worker_has_no_terminal_and_results_go_only_to_binit_names(self):
        manager = shlex.quote(str(terminal.BINARY.resolve()))
        start = f"binit_complete prompt-worker start {manager} /bin/sh -c"
        probe = shlex.quote("if (: > /dev/tty) 2>/dev/null; then printf tty; else printf none; fi")
        with Session() as session:
            # A worker cannot reach the user's terminal, so it cannot write escapes to it or inject keys.
            session.send(f"{start} {probe}; binit_complete prompt-worker wait 500; __binit_r=; "
                         "binit_complete prompt-worker take __binit_r; printf 'TTY:%s\\n' \"$__binit_r\"\n")
            session.wait(lambda: b"\rTTY:" in session.transcript)
            self.assertIn(b"\rTTY:none", session.transcript)
            # A result never lands in a variable the shell interprets, and the refused take frees the slot.
            session.send(f"saved=$PS1; {start} 'printf x'; binit_complete prompt-worker wait 500; "
                         "binit_complete prompt-worker take PS1; s=$?; [[ $PS1 == \"$saved\" ]] && same=1; "
                         f"{start} 'printf y'; t=$?; printf 'NAME:%s:%s:%s\\n' \"$s\" \"$same\" \"$t\"\n")
            session.wait(lambda: b"\rNAME:" in session.transcript)
            self.assertIn(b"\rNAME:2:1:0", session.transcript)

    def test_prompt_worker_cannot_open_a_network_socket(self):
        executable = shlex.quote(sys.executable)
        manager = shlex.quote(str(terminal.BINARY.resolve()))
        program = shlex.quote(
            "import socket\n"
            "try:\n"
            "    sock = socket.socket()\n"
            "except PermissionError:\n"
            "    print('offline', end='')\n"
            "else:\n"
            "    sock.close()\n"
            "    print('online', end='')\n"
        )
        with Session(extra=f"binit_complete prompt-worker start {manager} {executable} -c {program}\n") as session:
            session.pump(.3)
            session.send("__binit_result=; binit_complete prompt-worker take __binit_result; result=$__binit_result; printf 'WORKER:%s\\n' \"$result\"\n")
            session.wait(lambda: b"\rWORKER:" in session.transcript)
            self.assertIn(b"\rWORKER:offline", session.transcript)

    def test_prompt_worker_is_cancelled_by_input_in_both_editing_modes(self):
        manager = shlex.quote(str(terminal.BINARY.resolve()))
        worker = "touch started; (sleep .6; touch late) & wait"
        command = f"binit_complete prompt-worker start {manager} /bin/bash -c {shlex.quote(worker)}"
        for mode in ("emacs", "vi"):
            with self.subTest(mode=mode), Session(extra=command + "\n", mode=mode) as session:
                session.wait(lambda: (session.directory / "started").exists(), seconds=.5)
                session.send("witness")
                self.assertEqual(session.capture()[1], "witness")
                session.pump(.7)
                self.assertFalse((session.directory / "late").exists())

    def test_prompt_worker_wait_is_bounded_and_keeps_a_slow_worker(self):
        manager = shlex.quote(str(terminal.BINARY.resolve()))
        start = f"binit_complete prompt-worker start {manager} /bin/bash -c"
        with Session() as session:
            session.send(f"{start} 'printf fast'; binit_complete prompt-worker wait 500; s=$?; __binit_r=; "
                         "binit_complete prompt-worker take __binit_r; r=$__binit_r; printf 'FAST:%s:%s\\n' \"$s\" \"$r\"\n")
            session.wait(lambda: b"\rFAST:" in session.transcript)
            self.assertIn(b"\rFAST:0:fast", session.transcript)
            # A timed-out wait leaves the worker running for a later prompt.
            session.send(f"{start} 'sleep .4; printf slow'; binit_complete prompt-worker wait 50; printf 'SLOW:%s\\n' $?\n")
            session.wait(lambda: b"\rSLOW:" in session.transcript)
            self.assertIn(b"\rSLOW:1", session.transcript)
            session.pump(.6)
            session.send("__binit_r=; binit_complete prompt-worker take __binit_r; r=$__binit_r; printf 'LATE:%s\\n' \"$r\"\n")
            session.wait(lambda: b"\rLATE:" in session.transcript)
            self.assertIn(b"\rLATE:slow", session.transcript)
            for argument in ("abc", "1001", "", "5x"):
                session.send(f"binit_complete prompt-worker wait '{argument}'; printf 'USAGE:%s\\n' $?\n")
                session.wait(lambda: b"\rUSAGE:" in session.transcript)
                self.assertIn(b"\rUSAGE:2", session.transcript)
                session.transcript.clear()
            # A request cancelled just before the next start, as when a command is typed quickly, still
            # lets that start run once its group has stopped.
            session.send(f"{start} 'sleep .4; printf old'; __binit_r=; binit_complete prompt-worker take __binit_r; "
                         f"{start} 'printf new'; s=$?; binit_complete prompt-worker wait 500; "
                         "binit_complete prompt-worker take __binit_r; printf 'NEXT:%s:%s\\n' \"$s\" \"$__binit_r\"\n")
            session.wait(lambda: b"\rNEXT:" in session.transcript)
            self.assertIn(b"\rNEXT:0:new", session.transcript)
            # The terminal settings a command sees are the same after a wait.
            session.send(f"a=$(stty -g); {start} 'sleep .3'; binit_complete prompt-worker wait 100; "
                         "b=$(stty -g); [[ $a == \"$b\" ]] && printf 'TTY:same\\n' || printf 'TTY:changed\\n'\n")
            session.wait(lambda: b"\rTTY:" in session.transcript)
            self.assertIn(b"\rTTY:same", session.transcript)
        # A key typed during a prompt hook's wait ends it and reaches Readline unchanged, including keys the
        # terminal would otherwise consume while it is still in canonical mode.
        hook = (f"__hook() {{ {start} 'sleep 2'; binit_complete prompt-worker wait 1000; }}\n"
                "history -s 'echo typed-ahead-witness'\n")
        for mode in ("emacs", "vi"):
            for key, shown in ((b"x", "READY> x"), (b"\x12", "(reverse-i-search)")):
                with self.subTest(mode=mode, key=key), Session(extra=hook, mode=mode) as session:
                    settle(session)
                    session.send("PROMPT_COMMAND=__hook\n")
                    session.pump(.3)
                    started = time.monotonic()
                    session.send(key)
                    session.wait(lambda: shown in session.text, seconds=3)
                    self.assertLess(time.monotonic() - started, .6)
                    self.assertNotIn("^R", session.text)
        # Return typed during the wait reaches Readline as Ctrl-M, as it does at an idle prompt.
        distinct = "bind -m emacs '\"\\C-j\": \"ctrl-j\"'; bind -m vi-insert '\"\\C-j\": \"ctrl-j\"'\n"
        for mode in ("emacs", "vi"):
            with self.subTest(mode=mode, key=b"\r"), Session(extra=hook + distinct, mode=mode) as session:
                settle(session)
                session.send("PROMPT_COMMAND=__hook\r")
                session.pump(.3)
                prompts = session.text.count("READY>")
                session.send(b"\r")
                session.wait(lambda: session.text.count("READY>") > prompts, seconds=3)
                self.assertNotIn("ctrl-j", session.text)
        # Typed input ends the wait early.
        with Session() as session:
            session.send(f"{start} 'sleep 2'; s=$EPOCHREALTIME; binit_complete prompt-worker wait 1000; "
                         "e=$EPOCHREALTIME; printf 'WAITED:%s\\n' $(( (${e/./} - ${s/./}) / 1000 ))\nx")
            session.wait(lambda: b"\rWAITED:" in session.transcript)
            waited = int(bytes(session.transcript).split(b"\rWAITED:")[1].split(b"\r")[0])
            self.assertLess(waited, 500)

    def test_prompt_worker_rejects_output_over_its_limit(self):
        executable = shlex.quote(sys.executable)
        manager = shlex.quote(str(terminal.BINARY.resolve()))
        program = shlex.quote("print('x' * 513, end='')")
        with Session(extra=f"binit_complete prompt-worker start {manager} {executable} -c {program}\n") as session:
            session.pump(.3)
            session.send("__binit_result=empty; binit_complete prompt-worker take __binit_result; result=$__binit_result; printf 'WORKER:%s\\n' \"$result\"\n")
            session.wait(lambda: b"\rWORKER:" in session.transcript)
            self.assertIn(b"\rWORKER:empty", session.transcript)

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
    def test_extended_highlighting_styles_shell_tokens_and_git_arguments(self):
        extra = (
            "BINIT_HIGHLIGHT_EXTENDED=1\n"
            "BINIT_HIGHLIGHT_STYLES='alias=91,function=92,builtin=93,option=94,"
            "git=95,redirection=96,substitution=97,expansion=33;4'\n"
            "alias entry='if true; then'\n"
            "operation() { :; }\n"
            "binit_complete editor highlighting on\n"
        )
        with Session(extra=extra) as session:
            for line, painted in (
                ('entry', b'\x1b[91mentry'),
                ('operation', b'\x1b[92moperation'),
                ('echo', b'\x1b[93mecho'),
                ('git -C . status --short', b'\x1b[95mstatus'),
                ('printf foo > ./missing', b'\x1b[96m>'),
                ('echo "$(echo hi) ${TERM:-none}"', b'\x1b[97m$('),
            ):
                with self.subTest(line=line):
                    session.transcript.clear()
                    session.send(line)
                    session.wait(lambda: painted in session.transcript)
                    self.assertEqual(session.capture()[1], line)
                    session.send(b'\x03')
                    session.pump(.06)

    @unittest.skipUnless(os.environ.get('BINIT_EXPECT_HIGHLIGHT') == '1', 'grammar-enabled native build required')
    def test_extended_highlighting_resolves_external_commands_and_literal_paths_on_idle(self):
        extra = (
            "BINIT_HIGHLIGHT_EXTENDED=1\n"
            "BINIT_HIGHLIGHT_STYLES='external=92,missing=91,path=94,missing-path=95'\n"
            "binit_complete editor highlighting on\n"
        )
        for mode in ('emacs', 'vi'):
            for line, painted in (
                    ('ls', b'\x1b[92mls'),
                    ('binit-no-such-command-781', b'\x1b[91mbinit-no-such-command-781'),
                    ('echo ./present ./absent', b'\x1b[95m./absent')):
                with self.subTest(line=line, mode=mode), Session(extra=extra, framework=True, mode=mode) as session:
                    (session.directory / 'present').touch()
                    session.send(line)
                    session.wait(lambda: painted in session.transcript)
                    if 'present' in line:
                        self.assertIn(b'\x1b[94m./present', session.transcript)
                    self.assertEqual(session.capture()[1], line)
                    session.send(b'\x03')
                    session.wait(lambda: session.screen.cursor.x == len(PROMPT) and
                                 session.screen.display[session.screen.cursor.y].rstrip() == PROMPT.rstrip())

    @unittest.skipUnless(os.environ.get('BINIT_EXPECT_HIGHLIGHT') == '1', 'grammar-enabled native build required')
    def test_extended_highlighting_keeps_a_word_color_while_it_is_typed(self):
        # Each key repaints a word with its last resolved color until its own lookup answers, so a
        # missing command stays red as it grows and a word that becomes valid changes color once.
        extra = (
            "BINIT_HIGHLIGHT_EXTENDED=1\n"
            "BINIT_HIGHLIGHT_STYLES='command=96,external=92,missing=91'\n"
            "binit_complete editor highlighting on\n"
        )
        for mode in ('emacs', 'vi'):
            for typed, more, painted in (('binit-no-such', '-command-783', b'\x1b[91mbinit-no-such-command-783'),
                                         ('sha256su', 'm', b'\x1b[92msha256sum')):
                with self.subTest(mode=mode, word=typed + more), Session(extra=extra, framework=True, mode=mode) as session:
                    session.send(typed)
                    session.wait(lambda: b'\x1b[91m' + typed.encode() in session.transcript)
                    session.transcript.clear()
                    for key in more:
                        session.send(key)
                        session.pump(.03)
                    session.wait(lambda: painted in session.transcript)
                    self.assertNotIn(b'\x1b[96m', session.transcript)

    @unittest.skipUnless(os.environ.get('BINIT_EXPECT_HIGHLIGHT') == '1', 'grammar-enabled native build required')
    def test_extended_highlighting_starts_a_different_word_unresolved(self):
        # A word that replaces another at the same place, as after recalling history, must not borrow
        # the old word's color.
        extra = ("BINIT_HIGHLIGHT_EXTENDED=1\nBINIT_HIGHLIGHT_STYLES='external=92,missing=91'\n"
                 "history -s 'ls'\nbinit_complete editor highlighting on\n")
        for mode, recall in (('emacs', b'\x10'), ('vi', b'\x1b[A'), ('emacs', b'\x15ls'), ('vi', b'\x15ls')):
            with self.subTest(mode=mode, recall=recall), Session(extra=extra, framework=True, mode=mode) as session:
                session.send('binit-no-such-cmd')
                session.wait(lambda: b'\x1b[91mbinit-no-such-cmd' in session.transcript)
                session.transcript.clear()
                session.send(recall)
                session.wait(lambda: b'\x1b[92mls' in session.transcript)
                self.assertNotIn(b'\x1b[91ml', session.transcript)

    @unittest.skipUnless(os.environ.get('BINIT_EXPECT_HIGHLIGHT') == '1', 'grammar-enabled native build required')
    def test_interrupt_right_after_a_lookup_answer_keeps_the_shell(self):
        # Ctrl-C runs Readline's signal handling inside its key loop; a hook removed there would be called
        # as a null pointer when the loop resumes.
        extra = ("BINIT_HIGHLIGHT_EXTENDED=1\nBINIT_HIGHLIGHT_STYLES='missing=91,missing-path=95'\n"
                 "binit_complete editor highlighting on\n")
        for mode in ('emacs', 'vi'):
            for line, painted in (('binit-no-such-cmd', b'\x1b[91mbinit-no-such-cmd'),
                                  ('echo ./absent', b'\x1b[95m./absent')):
                with self.subTest(mode=mode, line=line), Session(extra=extra, framework=True, mode=mode) as session:
                    settle(session)
                    session.send(line)
                    session.wait(lambda: painted in session.transcript)
                    session.send(b'\x03')
                    session.pump(.2)
                    session.send("echo still-$((40 + 2))\n")
                    session.wait(lambda: 'still-42' in session.text)

    @unittest.skipUnless(os.environ.get('BINIT_EXPECT_HIGHLIGHT') == '1', 'grammar-enabled native build required')
    def test_extended_highlighting_answers_within_a_few_frames(self):
        # Readline's idle hook runs every 0.1 s; a pending lookup polls faster, so the provisional
        # color of a new word is replaced almost at once.
        extra = ("BINIT_HIGHLIGHT_EXTENDED=1\nBINIT_HIGHLIGHT_STYLES='missing=91'\n"
                 "binit_complete editor highlighting on\n")
        for mode in ('emacs', 'vi'):
            with self.subTest(mode=mode), Session(extra=extra, framework=True, mode=mode) as session:
                settle(session)
                delays = []
                for attempt in range(5):
                    word = f'binit-no-such-{attempt}'
                    session.transcript.clear()
                    started = time.monotonic()
                    session.send(word)
                    # A tight loop: Session.wait checks every 20 ms and keeps reading after a match.
                    while b'\x1b[91m' + word.encode() not in session.transcript and time.monotonic() - started < 2:
                        session.pump(.002)
                    delays.append(time.monotonic() - started)
                    session.send(b'\x03')
                    session.wait(lambda: session.screen.cursor.x == len(PROMPT) and
                                 session.screen.display[session.screen.cursor.y].rstrip() == PROMPT.rstrip())
                self.assertLess(sorted(delays)[2], .06, delays)

    @unittest.skipUnless(os.environ.get('BINIT_EXPECT_HIGHLIGHT') == '1', 'grammar-enabled native build required')
    def test_extended_highlighting_cancels_path_lookup_on_typing(self):
        fixture = textwrap.dedent("""
            from pathlib import Path
            import socket
            connection = socket.socket(fileno=0)
            connection.settimeout(4)
            source = connection.makefile("rb")
            size = int.from_bytes(source.read(2), "little")
            source.read(size)
            Path("lookup-started").touch()
            if not source.read(1):
                Path("lookup-cancelled").touch()
        """)
        extra = ('__binit_manager="$PWD/path-worker"\nBINIT_HIGHLIGHT_EXTENDED=1\n'
                 'binit_complete editor highlighting on\n')
        for mode in ('emacs', 'vi'):
            for key, expected in ((b'd', 'echo ./food'), (b'\x03', '')):
                with self.subTest(mode=mode, key=key), Session(extra=extra, mode=mode) as session:
                    worker = session.directory / 'path-worker'
                    worker.write_text(f'#!{sys.executable}\n' + fixture)
                    worker.chmod(0o700)
                    session.send('echo ./foo')
                    session.wait(lambda: (session.directory / 'lookup-started').exists())
                    session.send(key)
                    session.wait(lambda: (session.directory / 'lookup-cancelled').exists())
                    self.assertEqual(session.capture()[1], expected)

    @unittest.skipUnless(os.environ.get('BINIT_EXPECT_HIGHLIGHT') == '1', 'grammar-enabled native build required')
    def test_extended_highlighting_matches_brackets_and_preserves_long_lines(self):
        extra = "BINIT_HIGHLIGHT_EXTENDED=1\nBINIT_HIGHLIGHT_STYLES='bracket=7;95'\nbinit_complete editor highlighting on\n"
        for mode in ('emacs', 'vi'):
            with self.subTest(mode=mode), Session(extra=extra, mode=mode, rows=60, columns=100) as session:
                settle(session)
                line = "echo '" + "x" * 3960 + "]' && [[ -n x ]]"
                session.send(line)
                session.wait(lambda: b'\x1b[7;95m[[' in session.transcript)
                self.assertIn(b'\x1b[7;95m]]', session.transcript)
                self.assertEqual(session.capture()[1], line)

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

    def test_suggestion_keys_keep_their_action_when_the_margin_hides_the_ghost(self):
        prefix = 'echo ' + 'x' * 27
        extra = f"history -s '{prefix} hidden'\nbinit_complete editor suggestions on\n"
        for mode in ('emacs', 'vi'):
            for key in (b'\x1b[C', b'\x1b[F'):
                with self.subTest(mode=mode, key=key), Session(extra=extra, mode=mode, columns=40) as session:
                    settle(session)
                    session.send(prefix)
                    session.wait(lambda: typed(session) == prefix)
                    self.assertEqual(session.screen.display[session.screen.cursor.y][39:], ' ')
                    session.send(key)
                    self.assertEqual(session.capture(), (len(prefix), prefix))

    def test_suggestion_keys_keep_their_action_when_the_input_exceeds_the_screen(self):
        prefix = 'echo ' + 'x' * 100
        extra = f"history -s '{prefix} hidden'\nbinit_complete editor suggestions on\n"
        for mode in ('emacs', 'vi'):
            with self.subTest(mode=mode), Session(extra=extra, mode=mode, rows=3, columns=40) as session:
                settle(session)
                session.send(prefix)
                session.pump(.2)
                self.assertNotIn('hidden', session.text)
                session.send(b'\x1b[C')
                self.assertEqual(session.capture(), (len(prefix), prefix))

    def test_repeated_term_assignment_keeps_suggestions_visible(self):
        extra = "history -s 'echo sample-suggestion'\nbinit_complete editor suggestions on\n"
        with Session(extra=extra) as session:
            settle(session)
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    session.send('TERM=xterm-256color\n')
                    session.pump(.2)
                    session.send('echo sam')
                    session.wait(lambda: 'echo sample-suggestion' in session.screen.display[prompt_row(session)])
                    session.send(b'\x03')
                    session.pump(.2)

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
    def test_pasted_lines_are_colored_where_readline_draws_them(self):
        # Readline starts a row at each newline and pads a tab with spaces to the next multiple of eight,
        # continuing on the next row when the tab crosses the margin.
        full = 'echo ' + 'c' * 34 + ';'
        cases = (
            (40, '\n'.join(('echo one', '\tls | cat', full, 'echo four')),
             ['READY> echo one', '        ls | cat', full, '', 'echo four'],
             [(0, 7), (1, 8), (1, 13), (2, 0), (4, 0)], (4, 9)),
            (42, 'true\n' + full + '\tls',
             ['READY> true', full, '      ls'], [(0, 7), (1, 0), (2, 6)], (2, 8)),
        )
        for mode in ('emacs', 'vi'):
            for columns, pasted, rows, commands, cursor in cases:
                with self.subTest(mode=mode, columns=columns), Session(
                        extra='binit_complete editor highlighting on\n', mode=mode, columns=columns) as session:
                    settle(session)
                    start = prompt_row(session)
                    session.send(b'\x1b[200~' + pasted.encode() + b'\x1b[201~')
                    row, column = commands[-1]
                    session.wait(lambda: session.screen.buffer[start + row][column].fg != 'default')
                    color = session.screen.buffer[start][7].fg
                    self.assertNotEqual(color, 'default')
                    for row, column in commands:
                        self.assertEqual(session.screen.buffer[start + row][column].fg, color, (row, column))
                    shown = [line.rstrip() for line in session.screen.display[start:start + len(rows)]]
                    self.assertEqual(shown, rows)
                    self.assertEqual((session.screen.cursor.y - start, session.screen.cursor.x), cursor)
                    self.assertEqual(session.capture()[1], pasted)

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
                    self.assertNotIn('history', session.text)

    def test_interrupt_in_the_middle_keeps_the_text_after_the_cursor(self):
        for mode in ('vi', 'emacs'):
            with self.subTest(mode=mode), Session(extra=SUGGEST + 'binit_complete editor history live\n', mode=mode) as session:
                session.send('echo sample')
                session.wait(lambda: 'history' in session.text)
                session.send(b'\x1b[D' * 3)
                session.pump(.2)
                session.send(b'\x03')
                session.pump(.3)
                self.assertEqual(input_row(session).rstrip(), PROMPT + 'echo sam^Ce')
                self.assertNotIn('history', session.text)
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

    def test_resize_with_a_cleared_signal_state_returns_to_the_prompt_start(self):
        # A second SIGWINCH during a resize's forced update runs Readline's asynchronous handler, which clears
        # RL_STATE_SIGHANDLER; the fixture replays that update deterministically on a 60-column screen.
        fixture = MODULE.parent / 'binit_redisplay_test.so'
        if not fixture.is_file():
            self.skipTest('native redisplay fixture not built')
        extra = SUGGEST + f"binit_complete editor suggestions on\nenable -f '{fixture}' binit_redisplay_test\n"
        with Session(extra=extra) as session:
            settle(session)
            session.screen.resize(24, 60)
            session.send('binit_redisplay_test resize-clobbered\n')
            session.wait(lambda: 'RESIZE_ARMED' in session.text or 'NO_MARKER' in session.text)
            if 'NO_MARKER' in session.text:
                self.skipTest('this Readline has no _rl_handling_signal marker')
            session.pump(.6)
            row = session.screen.display[prompt_row(session)]
            self.assertEqual(row.count(PROMPT), 1, row)
            self.assertEqual(typed(session), '')

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


KEYS_ON = 'binit_complete editor keys on\n'
CTRL_BACKSPACE, CTRL_DELETE, ALT_BACKSPACE = b'\x08', b'\x1b[3;5~', b'\x1b\x7f'


def status(session, command='binit_complete editor status'):
    """Runs the status command and waits for a sentinel printed after its last line."""
    session.send(b'\x15' + command.encode() + b'; echo STATUS-$((1))-DONE\n')
    session.wait(lambda: 'STATUS-1-DONE' in session.text)
    return session.text


def edited(session, line, left, *keys):
    """Types line, moves the cursor left `left` characters, sends each key and returns (point, line)."""
    session.send(line.encode() + b'\x1b[D' * left)
    session.pump(.1)
    for key in keys:
        session.send(key)
        session.pump(.05)
    return session.capture()


class WordKeyTests(unittest.TestCase):
    # (line, characters left of the end, key, point, result); words are Readline's letters and digits.
    CASES = (
        ('echo one two three', 0, CTRL_BACKSPACE, 13, 'echo one two '),
        ('echo one two three', 3, CTRL_BACKSPACE, 13, 'echo one two ree'),
        ('echo one two three', 6, CTRL_BACKSPACE, 9, 'echo one  three'),
        ('git commit -m fix-bug_x', 0, CTRL_BACKSPACE, 22, 'git commit -m fix-bug_'),
        ('cd /usr/local/share/', 0, CTRL_BACKSPACE, 14, 'cd /usr/local/'),
        ('echo caffè 雪', 0, CTRL_BACKSPACE, 11, 'echo caffè '),
        ('echo one two three', 9, CTRL_DELETE, 9, 'echo one  three'),
        ('echo one two three', 7, CTRL_DELETE, 11, 'echo one tw three'),
        ('echo one two three', 0, b'\x7f', 17, 'echo one two thre'),
    )

    def test_word_boundaries_in_both_modes(self):
        for mode in ('vi', 'emacs'):
            for line, left, key, point, result in self.CASES:
                with self.subTest(mode=mode, line=line, left=left, key=key), \
                        Session(extra=KEYS_ON, mode=mode) as session:
                    self.assertEqual(edited(session, line, left, key), (point, result))

    def test_ctrl_backspace_matches_alt_backspace_and_undoes_in_one_step(self):
        with Session(extra=KEYS_ON) as session:
            expected = edited(session, 'echo caffè 雪 x-y', 2, ALT_BACKSPACE)
        with Session(extra=KEYS_ON) as session:
            self.assertEqual(edited(session, 'echo caffè 雪 x-y', 2, CTRL_BACKSPACE), expected)
            session.send(b'\x1f')
            self.assertEqual(session.capture()[1], 'echo caffè 雪 x-y')
            session.send(b'\x05\x19')
            self.assertEqual(session.capture()[1], 'echo caffè 雪 x-yx')

    def test_home_end_and_delete_keep_readline_meaning_with_every_feature(self):
        extra = SUGGEST + KEYS_ON + 'binit_complete editor history on\n'
        for mode in ('vi', 'emacs'):
            for home in (b'\x1b[H', b'\x1bOH'):
                with self.subTest(mode=mode, home=home), Session(extra=extra, mode=mode) as session:
                    self.assertEqual(edited(session, 'echo one two', 4, home), (0, 'echo one two'))
                    session.send(b'\x1b[3~')
                    self.assertEqual(session.capture(), (0, 'cho one two'))
                    session.send(b'\x1b[F')
                    self.assertEqual(session.capture(), (11, 'cho one two'))

    def test_vi_command_mode_keeps_home_end_and_ctrl_delete(self):
        with Session(extra=KEYS_ON, mode='vi') as session:
            session.send(b'echo one two\x1b')
            session.pump(.1)
            session.send(b'\x1b[Hi')
            self.assertEqual(session.capture(), (0, 'echo one two'))
            session.send(b'\x1b')
            session.pump(.1)
            session.send(b'\x1b[3;5~i')
            self.assertEqual(session.capture(), (0, ' one two'))
            session.send(b'\x1b')
            session.pump(.1)
            session.send(b'\x1b[FA')
            self.assertEqual(session.capture(), (8, ' one two'))

    def test_wrapped_line_and_first_input_after_interrupt(self):
        for mode in ('vi', 'emacs'):
            with self.subTest(mode=mode), Session(extra=KEYS_ON, mode=mode, columns=40) as session:
                settle(session)
                session.send(WRAPPED + ' tail')
                session.pump(.15)
                session.send(CTRL_BACKSPACE)
                session.pump(.15)
                self.assertEqual(typed(session), WRAPPED + ' ')
                self.assertEqual(session.capture(), (len(WRAPPED) + 1, WRAPPED + ' '))
                session.send(b'\x1b[H')
                session.pump(.15)
                self.assertEqual((session.screen.cursor.y, session.screen.cursor.x), (prompt_row(session), len(PROMPT)))
                session.send(b'\x03')
                session.pump(.2)
                session.send(CTRL_BACKSPACE + b'echo ok one' + CTRL_BACKSPACE)
                self.assertEqual(session.capture(), (8, 'echo ok '))

    def test_unknown_or_ctrl_h_erase_key_is_left_alone(self):
        # Readline repairs the erase key in the active keymap at each prompt, so the other keymap shows the guard.
        dump = "bind -m vi-insert -p > .binds-vi\n"
        for name, setup in (('ctrl-h erase', "stty erase '^H'\n" + KEYS_ON),
                            ('unreadable terminal', "stty erase '^H'\nbinit_complete editor keys on </dev/null\n"),
                            ('stdin redirected, erase DEL', "binit_complete editor keys on </dev/null\n")):
            with self.subTest(case=name), Session(extra=setup + dump) as session:
                session.pump(.3)
                self.assertIn('"\\C-h": backward-delete-char', (session.directory / '.binds-vi').read_text())
                self.assertEqual(edited(session, 'echo one two', 0, CTRL_BACKSPACE), (11, 'echo one tw'))
                self.assertNotIn('Ctrl-Backspace', status(session))

    def test_erase_key_set_after_enable_takes_effect_at_the_next_prompt(self):
        for mode in ('vi', 'emacs'):
            with self.subTest(mode=mode), Session(extra=KEYS_ON, mode=mode) as session:
                session.send(b"stty erase '^H'\n")
                session.wait(lambda: session.text.count(PROMPT) >= 2)
                self.assertEqual(edited(session, 'echo one two', 0, CTRL_BACKSPACE), (11, 'echo one tw'))
                self.assertNotIn('Ctrl-Backspace', status(session))

    def test_custom_binding_is_kept_and_status_lists_only_owned_keys(self):
        extra = "bind -m vi-insert '\"\\C-h\": unix-word-rubout'\n" + KEYS_ON
        for mode in ('vi', 'emacs'):
            with self.subTest(mode=mode), Session(extra=extra, mode=mode, framework=True) as session:
                result = edited(session, 'echo a/b-c', 0, CTRL_BACKSPACE)
                self.assertEqual(result, (5, 'echo ') if mode == 'vi' else (9, 'echo a/b-'))
                text = status(session, 'binit editor')
                self.assertIn('tab complete', text)
                self.assertEqual('ctrl-backspace word' in text, mode == 'emacs')

    def test_framework_activation_takes_the_key_and_off_and_unload_restore_it(self):
        dump = "bind -m vi-insert -p > .binds-vi\nbind -m emacs-standard -p > .binds-emacs\n"
        with Session(framework=True) as session:
            self.assertEqual(edited(session, 'echo one two', 0, CTRL_BACKSPACE), (9, 'echo one '))
            text = status(session, 'binit editor')
            self.assertIn('ctrl-backspace word', text)
            self.assertIn('tab complete', text)
        for release in ('binit_complete editor keys off\n', 'enable -d binit_complete\n'):
            with self.subTest(release=release), Session(extra=KEYS_ON + release + dump) as session:
                session.pump(.3)
                for name in ('.binds-vi', '.binds-emacs'):
                    self.assertIn('"\\C-h": backward-delete-char', (session.directory / name).read_text())
                if release.startswith('binit_complete'):
                    self.assertEqual(edited(session, 'echo one two', 0, CTRL_BACKSPACE), (11, 'echo one tw'))

    def test_disabled_native_menu_leaves_the_key_alone(self):
        with Session(framework=True, menu=False) as session:
            self.assertEqual(edited(session, 'echo one two', 0, CTRL_BACKSPACE), (11, 'echo one tw'))
