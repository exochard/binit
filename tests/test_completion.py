import re
import codecs
import importlib.util
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
import fcntl
import termios

import pyte


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("terminal", ROOT / "scripts/terminal.py")
terminal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(terminal)
MODULE = Path(os.environ.get("BINIT_TEST_MODULE", ROOT / "target/native-check/binit_complete.so")).resolve()


class Session:
    def __init__(self, extra="", values=40, mode="emacs", rows=24, columns=80, framework=False, menu=True, replies=True,
                 plugins=()):
        if not MODULE.is_file():
            raise RuntimeError("native test module missing; run bash scripts/verify.bash")
        self.temporary = tempfile.TemporaryDirectory(prefix=".scratch-menu-", dir=ROOT)
        self.directory = Path(self.temporary.name)
        self.capture_path = self.directory / "capture"
        for i in range(values):
            (self.directory / f"alpha {i:02}.txt").touch()
        capture = shlex.quote(str(self.capture_path))
        rc = self.directory / "rc"
        setup = f'''PS1='READY> '
PROMPT_COMMAND=()
set -o {mode}
bind -q complete >/dev/null
__capture() {{ printf '%s\\0%s\\0' "$READLINE_POINT" "$READLINE_LINE" > {capture}; }}
bind -m emacs-standard -x '"\\C-x\\C-g": __capture'
bind -m vi-insert -x '"\\C-x\\C-g": __capture'
__values() {{ COMPREPLY=('value one' 'value two'); }}
complete -o filenames -F __values demo_cmd
__lazy() {{ complete -o filenames -F __values lazy_cmd; return 124; }}
complete -F __lazy lazy_cmd
'''
        state = None
        if framework:
            plugin = self.directory / "plugin"
            plugin.mkdir()
            (plugin / "plugin.toml").write_text("schema=1\nid='test'\nentry='plugin.bash'\nload='startup'\n")
            (plugin / "plugin.bash").write_text(setup + extra + "\n")
            config = self.directory / "config.toml"
            # Further plugins load after the setup plugin, so its settings are already in place.
            config.write_text("schema=1\n[[plugins]]\npath='plugin'\n" +
                              "".join(f"[[plugins]]\npath={json.dumps(str(path))}\n" for path in plugins))
            state = self.directory / "state"
            terminal.cli(state, "prepare", config)
        else:
            rc.write_text(setup + f"enable -f {shlex.quote(str(MODULE))} binit_complete\nbinit_complete on\n" + extra)
        with patch.dict(os.environ, HISTFILE="/dev/null", INPUTRC="/dev/null", LC_ALL="C.UTF-8",
                        BINIT_COMPLETION_MENU="1" if menu else "0"):
            self.terminal = terminal.Terminal(rc, self.directory, term="xterm-256color", state=state)
        self.screen = pyte.HistoryScreen(columns, rows, history=1000)
        self.screen.write_process_input = lambda text: os.write(self.terminal.fd, text.encode()) if replies else None
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.transcript = bytearray(self.terminal.transcript)
        self.stream.feed(self.decoder.decode(self.terminal.transcript))
        self.initial_title = self.screen.title
        fcntl.ioctl(self.terminal.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    @property
    def text(self):
        return "\n".join(self.screen.display)

    @property
    def panel(self):
        # The panel ends with its state line: the position, then `?` unless a query is typed.
        return any(re.search(r"(?:^|  )\d+/\d+(?:   \?)?$", row.rstrip()) for row in self.screen.display)

    def pump(self, seconds=.08):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([self.terminal.fd], [], [], .01)[0]:
                data = os.read(self.terminal.fd, 65536)
                if not data:
                    break
                self.transcript.extend(data)
                self.stream.feed(self.decoder.decode(data))

    def wait(self, predicate, seconds=2):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.pump(.02)
            if predicate():
                self.pump(.02)
                return
        raise AssertionError(f"terminal state timed out:\n{self.text}\nraw tail: {bytes(self.transcript[-700:])!r}")

    def send(self, data):
        os.write(self.terminal.fd, data.encode() if isinstance(data, str) else data)

    def open(self, line='printf "%s" al', suffix=""):
        self.send((line + suffix).encode() + b"\x02" * len(suffix) + b"\t")
        self.wait(lambda: self.panel)

    def capture(self):
        if self.capture_path.exists():
            self.capture_path.unlink()
        self.send(b"\x18\x07")
        self.wait(lambda: self.capture_path.exists() and self.capture_path.stat().st_size > 1)
        point, line, _ = self.capture_path.read_bytes().split(b"\0")
        return int(point), line.decode()

    def close(self):
        self.terminal.close()
        self.temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class CompletionTests(unittest.TestCase):
    def test_cursor_reply_within_probe_deadline_opens_menu_without_leaking_input(self):
        with Session(replies=False) as session:
            def delayed_reply(text):
                time.sleep(.08)
                os.write(session.terminal.fd, text.encode())
            session.screen.write_process_input = delayed_reply
            session.open('demo_cmd v')
            session.send(b'\x1b')
            session.wait(lambda: not session.panel)
            self.assertEqual(session.capture(), (len('demo_cmd v'), 'demo_cmd v'))

    def test_debug_stdout_does_not_hide_the_native_builtin(self):
        with Session(framework=True, extra="set -T\ntrap 'printf \"TRACE:%s\\n\" \"$BASH_COMMAND\"' DEBUG\n") as session:
            session.send("binit completion\n")
            session.wait(lambda: "menu          on" in session.text)
            session.open()

    def test_missing_cursor_reply_preserves_input_received_during_probe(self):
        with Session(extra="binit_complete off\n") as baseline:
            baseline.send(b'printf "%s" al\t[xyz')
            baseline.pump(.2)
            expected = baseline.capture()
        with Session(replies=False) as session:
            session.send(b'printf "%s" al\t[xyz')
            session.pump(.2)
            self.assertFalse(session.panel)
            self.assertEqual(session.capture(), expected)

    def test_narrow_terminal_uses_readline_without_selecting_a_candidate(self):
        with Session(extra="binit_complete off\n", columns=18) as baseline:
            baseline.send(b'printf "%s" al\t')
            baseline.pump()
            expected = baseline.capture()
        with Session(columns=18) as session:
            session.send(b'printf "%s" al\t')
            session.pump()
            self.assertFalse(session.panel)
            self.assertEqual(session.capture(), expected)

    def test_verified_generation_enables_menu_and_current_shell_controls(self):
        with Session(framework=True) as session:
            session.send("binit completion\n")
            session.wait(lambda: "menu          on" in session.text)
            session.open()
            session.send(b"\r")
            session.wait(lambda: not session.panel)
            self.assertEqual(shlex.split(session.capture()[1])[-1], "alpha 00.txt")
            session.send(b"\x01\x0bbinit completion off\n")
            session.wait(lambda: "menu          off" in session.text)
            session.send(b'printf "%s" al\t')
            session.pump()
            self.assertFalse(session.panel)

    def test_disabled_startup_can_enable_the_menu_in_the_current_shell(self):
        with Session(framework=True, menu=False) as session:
            session.send("binit completion on\n")
            session.wait(lambda: "menu          on" in session.text)
            session.open()

    def test_recovery_skips_a_corrupted_native_generation(self):
        with Session(framework=True) as session:
            state = session.directory / "state"
            broken = session.directory / "broken-state"
            shutil.copytree(state, broken)
            generation = (broken / "current").read_text()
            (broken / "generations" / generation / "completion.so").write_bytes(b"corrupted")
            with patch.dict(os.environ, HISTFILE="/dev/null", INPUTRC="/dev/null"):
                recovery = terminal.Terminal(session.directory / "rc", session.directory, safe=True, state=broken)
            try:
                output = recovery.command("declare -F binit >/dev/null || printf 'recovery=clean\\n'")
                self.assertIn("recovery=clean", output)
            finally:
                recovery.close()

    def test_grid_moves_in_place_without_transcript_growth(self):
        with Session() as session:
            session.open()
            history = len(session.screen.history.top)
            for i in range(1, 30):
                session.send(b"\t")
                session.wait(lambda: f" {i + 1}/40   ?" in session.text)
            self.assertEqual(len(session.screen.history.top), history)
            self.assertEqual(sum(bool(re.search(r"\d+/\d+   \?$", row.rstrip())) for row in session.screen.display), 1)
            session.send(b"\x1b")
            session.wait(lambda: not session.panel)
            self.assertEqual(session.capture()[1], 'printf "%s" al')

    def test_arrows_hjkl_and_shift_tab(self):
        with Session() as session:
            session.open()
            for keys, index in ((b"\x1b[C", 2), (b"\x1b[D", 1), (b"\x1b[B", 5),
                                (b"\x1b[A", 1), (b"l", 2), (b"h", 1), (b"j", 5),
                                (b"k", 1), (b"\x1b[Z", 40)):
                session.send(keys)
                session.wait(lambda: f" {index}/40   ?" in session.text)

    def test_state_line_names_the_typed_word_and_shows_help_only_where_it_works(self):
        with Session() as session:
            # Readline's common prefix is `value `; the state line names what was typed.
            session.open('demo_cmd v')
            session.wait(lambda: any(row.rstrip() == "v  1/2   ?" for row in session.screen.display))
            session.send("/tw")
            session.wait(lambda: any(row.rstrip() == "v /tw  1/1" for row in session.screen.display))

    def test_filtering_and_empty_results(self):
        with Session() as session:
            session.open()
            session.send("/39")
            session.wait(lambda: "/39  1/1" in session.text)
            self.assertIn("alpha 39.txt", session.text)
            session.send(b"\x15xyz")
            session.wait(lambda: "/xyz  0/0" in session.text)
            session.send(b"\r")
            session.pump()
            self.assertTrue(session.panel)
            session.send(b"\x15\x7f39\r")
            session.wait(lambda: not session.panel)
            self.assertEqual(shlex.split(session.capture()[1])[-1], "alpha 39.txt")

    def test_enter_inserts_without_executing(self):
        with Session() as session:
            session.open("touch executed al")
            session.send(b"\t\r")
            session.wait(lambda: not session.panel)
            self.assertFalse((session.directory / "executed").exists())
            self.assertEqual(shlex.split(session.capture()[1]), ["touch", "executed", "alpha 01.txt"])

    def test_quotes_unicode_providers_and_middle_of_line(self):
        cases = [("plain", 'printf "%s" al', "", "alpha 01.txt"),
                 ("double", 'printf "%s" "al', "", "alpha 01.txt"),
                 ("single", "printf '%s' 'al", "", "alpha 01.txt"),
                 ("unicode", 'printf "%s" caff', "", "caffè uno.txt"),
                 ("provider", "demo_cmd va", "", "value two"),
                 ("lazy", "lazy_cmd va", "", "value two"),
                 ("middle", "demo_cmd va", " --keep", "value two")]
        for name, prefix, suffix, expected in cases:
            with self.subTest(name=name), Session() as session:
                for file in ("caffè due.txt", "caffè uno.txt"):
                    (session.directory / file).touch()
                session.open(prefix, suffix)
                session.send(b"\t\r")
                session.wait(lambda: not session.panel)
                words = shlex.split(session.capture()[1])
                self.assertEqual(words[-2] if suffix else words[-1], expected)
                if suffix:
                    self.assertEqual(words[-1], "--keep")

    def test_cancellation_restores_line_and_point(self):
        with Session() as session:
            session.send(b"demo_cmd va --keep\x02\x02\x02\x02\x02\x02\x02")
            session.pump()
            before = session.capture()
            session.send(b"\t")
            session.wait(lambda: session.panel)
            session.send(b"\t\t\x1b")
            session.wait(lambda: not session.panel)
            self.assertEqual(session.capture(), before)

    def test_accept_is_one_undo_and_cancel_preserves_prior_undo(self):
        with Session() as session:
            session.open()
            session.send(b"\t\r")
            session.wait(lambda: not session.panel)
            session.send(b"\x1f")
            session.pump()
            self.assertEqual(session.capture()[1], 'printf "%s" al')
        with Session(extra="binit_complete off") as baseline:
            baseline.send('printf "%s" al')
            baseline.send(b"\x1f")
            baseline.pump()
            expected = baseline.capture()
        with Session() as session:
            session.open()
            session.send(b"\x1b")
            session.wait(lambda: not session.panel)
            session.send(b"\x1f")
            session.pump()
            self.assertEqual(session.capture(), expected)

    def test_vi_insert_mode(self):
        with Session(mode="vi") as session:
            session.open("demo_cmd va")
            session.send(b"\t\r")
            session.wait(lambda: not session.panel)
            self.assertEqual(shlex.split(session.capture()[1])[-1], "value two")

    def test_interrupt_cleans_up_and_menu_can_reopen(self):
        with Session() as session:
            session.open()
            session.send(b"\x03")
            session.wait(lambda: not session.panel)
            session.send("printf 'status=%s\\n' \"$?\"\n")
            session.wait(lambda: "status=130" in session.text)
            session.open()
            session.send(b"\r")
            session.wait(lambda: not session.panel)
            self.assertIn("alpha", session.capture()[1])

    def test_resize_closes_menu_without_losing_input(self):
        for mode in ("emacs", "vi"):
            with self.subTest(mode=mode), Session(mode=mode) as session:
                session.open()
                session.screen.resize(20, 60)
                fcntl.ioctl(session.terminal.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 20, 60, 0, 0))
                os.kill(session.terminal.pid, signal.SIGWINCH)
                session.wait(lambda: not session.panel)
                self.assertEqual(session.capture(), (len('printf "%s" al'), 'printf "%s" al'))
                session.send(b"\t")
                session.wait(lambda: session.panel)

    def test_bracketed_paste_returns_to_readline_without_execution(self):
        with Session() as session:
            session.open()
            session.send(b"\x1b[200~\n touch pasted\n\x1b[201~")
            session.wait(lambda: not session.panel)
            self.assertFalse((session.directory / "pasted").exists())
            self.assertIn("touch pasted", session.capture()[1])

    def test_fragmented_paste_header_cannot_submit_pasted_commands(self):
        with Session() as session:
            session.open()
            session.send(b"\x1b")
            session.wait(lambda: not session.panel)
            session.pump(.2)
            session.send(b"[200~\n touch pasted\n\x1b[201~")
            session.pump(.2)
            self.assertFalse((session.directory / "pasted").exists())
            self.assertIn("touch pasted", session.capture()[1])

    def test_escape_wait_follows_binit_escape_ms(self):
        with Session(extra="BINIT_ESCAPE_MS=400\n") as session:
            session.open()
            # An arrow split inside the configured wait is still an arrow.
            session.send(b"\x1b")
            time.sleep(.1)
            session.send(b"[C")
            session.wait(lambda: " 2/40   ?" in session.text)
            # A bare Esc holds the menu for the configured wait, then closes it.
            session.send(b"\x1b")
            session.pump(.15)
            self.assertTrue(session.panel)
            session.wait(lambda: not session.panel)
            self.assertEqual(session.capture()[1], 'printf "%s" al')

    def test_invalid_escape_ms_keeps_the_short_default(self):
        for value in ("", "abc", "-5", "5000"):
            with self.subTest(value=value), Session(extra=f"BINIT_ESCAPE_MS={value}\n") as session:
                session.open()
                start = time.monotonic()
                session.send(b"\x1b")
                session.wait(lambda: not session.panel)
                # The default is 10 ms; an accepted 5000 or the old 400 test value would hold far longer.
                self.assertLess(time.monotonic() - start, .3)

    def test_alt_key_in_one_write_reaches_readline(self):
        with Session() as session:
            session.open()
            session.send(b"\x1bb")
            session.wait(lambda: not session.panel)
            point, line = session.capture()
            self.assertEqual(line, 'printf "%s" al')
            self.assertEqual(point, line.rindex("al"))

    def test_one_key_after_escape_appears_without_another_key(self):
        # A '[' could start a late paste header, but it must not wait for a key that may never come.
        for mode in ("emacs", "vi"):
            for key in ("[", "x"):
                with self.subTest(mode=mode, key=key), Session(mode=mode) as session:
                    session.open()
                    session.send(b"\x1b")
                    session.wait(lambda: not session.panel)
                    session.send(key)
                    # Watch the screen only: capture() sends keys, which would release a held byte.
                    session.wait(lambda: 'al' + key in session.text, seconds=1)
                    expected = 'printf "%s" al' + key
                    self.assertEqual(session.capture(), (len(expected), expected))

    def test_interrupt_while_a_bracket_waits_cancels_the_line(self):
        for mode in ("emacs", "vi"):
            with self.subTest(mode=mode), Session(mode=mode) as session:
                session.open()
                session.send(b"\x1b")
                session.wait(lambda: not session.panel)
                session.send(b"[")
                # Interrupt while the held '[' is drawn, so the unwind must remove it.
                session.wait(lambda: 'al[' in session.text, seconds=1)
                session.send(b"\x03")
                session.wait(lambda: session.text.count("READY>") >= 2)
                # Idle past the header wait, so a reader left behind would now fail the next read.
                session.pump(.3)
                session.send("printf 'ok-%s\\n' $((1+1))\r")
                session.wait(lambda: "ok-2" in session.text)
                self.assertNotIn("[printf", session.text)
                self.assertEqual(session.capture(), (0, ""))

    def test_late_paste_after_escape_matches_readline_in_both_keymaps(self):
        paste = b"\x1b[200~\n touch pasted\n\x1b[201~"
        for mode in ("emacs", "vi"):
            with Session(mode=mode, extra="binit_complete off\n") as baseline:
                baseline.send(b'printf "%s" al' + paste)
                baseline.pump(.2)
                expected = baseline.capture()
            self.assertEqual(expected[1].count("touch pasted"), 1)
            # The header arrives after the Esc closed the menu: whole, split after its bracket, or split
            # with gaps far longer than any key-sequence wait.
            for parts, gap in (((paste[1:],), 0), ((paste[1:2], paste[2:]), .005),
                               ((paste[1:2], paste[2:]), .3), ((paste[1:2], paste[2:4], paste[4:]), .3)):
                with self.subTest(mode=mode, parts=len(parts), gap=gap), Session(mode=mode) as session:
                    session.open()
                    session.send(b"\x1b")
                    session.wait(lambda: not session.panel)
                    session.pump(.2)
                    for part in parts:
                        session.send(part)
                        time.sleep(gap)
                    session.pump(.2)
                    self.assertFalse((session.directory / "pasted").exists())
                    self.assertEqual(session.text.count("READY>"), 1)
                    self.assertEqual(session.capture(), expected)

    def test_undo_after_a_held_bracket_matches_readline(self):
        for mode in ("emacs", "vi"):
            for typed in (b"[x\x1f", b"[\x1f", b"[2\x1f"):
                with self.subTest(mode=mode, typed=typed):
                    with Session(mode=mode, extra="binit_complete off\n") as baseline:
                        baseline.send(b'printf "%s" al' + typed)
                        baseline.pump()
                        expected = baseline.capture()
                    with Session(mode=mode) as session:
                        session.open()
                        session.send(b"\x1b")
                        session.wait(lambda: not session.panel)
                        session.send(typed)
                        session.pump()
                        self.assertEqual(session.capture(), expected)

    def test_ordinary_brackets_after_escape_are_preserved(self):
        for text in ("[xyz", "[20x", "[200x"):
            with self.subTest(text=text), Session() as session:
                session.open()
                session.send(b"\x1b")
                session.wait(lambda: not session.panel)
                session.send(text)
                session.pump()
                self.assertEqual(session.capture()[1], 'printf "%s" al' + text)

    def test_brackets_after_escape_keep_bindings_and_overwrite_mode(self):
        cases = [
            ("emacs", "bind '\"[\":\"ZZ\"'\n", b"", 'printf "%s" al', "", b"[xyz"),
            ("emacs", "bind '\"[\":\"ZZ\"'\n", b"", 'printf "%s" al', "", b"["),
            ("vi", "bind '\"[\":\"ZZ\"'\n", b"", 'printf "%s" al', "", b"[xyz"),
            ("vi", "bind '\"[\":\"ZZ\"'\n", b"", 'printf "%s" al', "", b"["),
            ("emacs", "bind '\"\\C-o\":overwrite-mode'\n", b"\x0f", "demo_cmd va", " KEEP", b"[xyz"),
        ]
        for keymap, extra, mode, line, suffix, typed in cases:
            with self.subTest(keymap=keymap, extra=extra, typed=typed):
                with Session(mode=keymap, extra=extra + "binit_complete off\n") as baseline:
                    baseline.send(mode + (line + suffix).encode() + b"\x02" * len(suffix) + typed)
                    baseline.pump()
                    expected = baseline.capture()
                with Session(mode=keymap, extra=extra) as session:
                    session.send(mode)
                    session.open(line, suffix)
                    session.send(b"\x1b")
                    session.wait(lambda: not session.panel)
                    session.send(typed)
                    # The screen must settle before capture() sends its keys. A lone rebound '[' is not
                    # drawn while a late paste header may follow; as in Readline after Esc, the next key
                    # (here capture()'s) resolves it.
                    session.wait(lambda: line + typed.decode().replace("[", "ZZ") in session.text
                                 if "ZZ" in extra and typed != b"[" else True)
                    session.pump()
                    self.assertEqual(session.capture(), expected)

    def test_terminal_controls_in_candidates_are_displayed_as_text(self):
        with Session() as session:
            (session.directory / "alpha \x1b]0;PWN\x07.txt").touch()
            session.open()
            self.assertEqual(session.screen.title, session.initial_title)
            self.assertNotIn(b"\x1b]0;PWN", session.transcript)
            self.assertIn(r"\e]0;PWN", session.text)

    def test_disable_and_unload_leave_user_created_bindings_safe(self):
        with Session() as session:
            session.send('bind \'"\\C-xm": binit-complete\'; binit_complete off; enable -d binit_complete\n')
            session.wait(lambda: session.text.count("READY>") >= 2)
            session.send(b'demo_cmd va\x18m')
            session.pump()
            self.assertFalse(session.panel)
            self.assertIn("value", session.capture()[1])

    def test_slashes_shorten_only_filename_candidates_as_in_readline(self):
        extra = "__refs() { COMPREPLY=('feature/history-panel' 'release/0.1'); }\ncomplete -F __refs refs_cmd\n"
        with Session(extra=extra) as session:
            session.open('refs_cmd ')
            rows = "\n".join(session.screen.display)
            self.assertIn("feature/history-panel", rows)
            self.assertIn("release/0.1", rows)
        with Session() as session:
            (session.directory / "sub").mkdir()
            for name in ("one.txt", "two.txt"):
                (session.directory / "sub" / name).touch()
            session.open('cat sub/')
            row = next(row for row in session.screen.display if "one.txt" in row)
            self.assertNotIn("sub/one.txt", row)

    def test_few_candidates_keep_compact_cells_on_a_wide_terminal(self):
        with Session(columns=200) as session:
            session.open('demo_cmd va')
            row = next(row for row in session.screen.display if 'value one' in row and 'value two' in row)
            self.assertLess(row.index('value two'), 30)


if __name__ == "__main__":
    unittest.main()
