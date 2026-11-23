"""Terminal tests for load=wait: plugins that load after the first prompt.

The shell comes from a prepared generation of the debug binary, which embeds
the native module built from native/completion.c. Build it first (cargo test
does); BINIT_TEST_BINARY overrides the path.
"""
import codecs
import fcntl
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import tempfile
import termios
import time
import unittest

import pyte

ROOT = Path(__file__).resolve().parent.parent
BINARY = Path(os.environ.get("BINIT_TEST_BINARY", ROOT / "target/debug/binit"))
PROMPT = "READY>"


class Session:
    def __init__(self, wait_body, startup_body="", menu=True, rows=24, columns=80, mode="emacs",
                 *, first_wait_body="", wait_edit=False):
        if not BINARY.is_file():
            raise RuntimeError("binit binary missing; run cargo build first")
        self.temporary = tempfile.TemporaryDirectory(prefix=".scratch-wait-", dir=ROOT)
        self.directory = Path(self.temporary.name)
        self.marks = self.directory / "marks"
        self.marks.mkdir()
        config = "schema=1\n"
        for name, load, body in (("early", "startup", startup_body),
                                 ("first", "wait", first_wait_body), ("slow", "wait", wait_body)):
            if not body:
                continue
            plugin = self.directory / name
            plugin.mkdir()
            (plugin / "plugin.toml").write_text(f"schema=1\nid='{name}'\nentry='plugin.bash'\nload='{load}'\n")
            (plugin / "plugin.bash").write_text(body.replace("MARKS", str(self.marks)))
            config += f"[[plugins]]\npath='{name}'\n"
        (self.directory / "config.toml").write_text(config)
        state = self.directory / "state"
        home = self.directory / "home"
        home.mkdir()
        inputrc = self.directory / "inputrc"
        inputrc.write_text(f"set editing-mode {mode}\n")
        subprocess.run([str(BINARY), "prepare", str(self.directory / "config.toml"), "--state", str(state)],
                       check=True, capture_output=True)
        self.started = time.monotonic()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(self.directory)
            env = dict(os.environ, TERM="xterm-256color", PS1=PROMPT + " ", HOME=str(home), HISTFILE="/dev/null",
                       INPUTRC=str(inputrc), LC_ALL="C.UTF-8", BINIT_SAFE="0", BINIT_COMPLETION_MENU="1" if menu else "0",
                       BINIT_WAIT_EDIT="1" if wait_edit else "0",
                       GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
            env.pop("BASH_ENV", None)
            env.pop("ENV", None)
            os.execvpe(str(BINARY), [str(BINARY), "shell", "--state", str(state)], env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        self.screen = pyte.Screen(columns, rows)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.raw = bytearray()

    @property
    def text(self):
        return "\n".join(self.screen.display)

    @property
    def cursor_line(self):
        return self.screen.display[self.screen.cursor.y].rstrip()

    def pump(self, seconds=.05):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([self.fd], [], [], .01)[0]:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                self.raw.extend(data)
                self.stream.feed(self.decoder.decode(data))

    def wait(self, predicate, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.pump(.02)
            if predicate():
                return time.monotonic() - self.started
        raise AssertionError(f"timed out:\n{self.text}\nraw tail: {bytes(self.raw[-500:])!r}")

    def send(self, data):
        os.write(self.fd, data.encode() if isinstance(data, str) else data)

    def mark(self, name):
        return (self.marks / name).exists()

    def settle(self, seconds=.3):
        self.pump(seconds)

    def close(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except (OSError, ChildProcessError):
            pass
        os.close(self.fd)
        self.temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


SLOW = "touch MARKS/start\nsleep 0.6\nwaited_fn() { echo waited; }\ntouch MARKS/done\n"
FIRST = "touch MARKS/first\nwhile [[ ! -e MARKS/release ]]; do sleep .01; done\n"
TAIL = "touch MARKS/tail\nwhile [[ ! -e MARKS/finish ]]; do sleep .01; done\n"


class WaitTests(unittest.TestCase):
    def test_prompt_precedes_the_load_and_the_function_exists_afterwards(self):
        with Session(SLOW) as session:
            session.wait(lambda: PROMPT in session.text)
            self.assertFalse(session.mark("done"))
            session.wait(lambda: session.mark("done"))
            session.settle()
            self.assertEqual(session.cursor_line, PROMPT)
            session.send("type -t waited_fn\n")
            session.wait(lambda: "function" in session.text)

    def test_keys_typed_during_the_load_are_kept(self):
        with Session(SLOW) as session:
            session.wait(lambda: PROMPT in session.text)
            session.send("echo kept")
            self.assertFalse(session.mark("done"))
            session.wait(lambda: session.mark("done"))
            session.settle()
            self.assertEqual(session.cursor_line, f"{PROMPT} echo kept")
            session.send("\n")
            session.wait(lambda: "\nkept" in session.text)

    def test_output_from_the_plugin_leaves_a_clean_prompt(self):
        body = "sleep 0.3\nprintf 'plugin says hi\\n'\ntouch MARKS/done\n"
        with Session(body) as session:
            session.wait(lambda: session.mark("done"))
            session.settle()
            self.assertIn("plugin says hi", session.text)
            self.assertEqual(session.cursor_line, PROMPT)

    def test_a_nested_read_does_not_dispatch(self):
        startup = "PROMPT_COMMAND+=('if ((BINIT_TEST_N++ == 0)); then read -e -p \"Q> \" answer; fi')\n"
        body = "touch MARKS/start\nprintf x >> MARKS/count\ntouch MARKS/done\n"
        with Session(body, startup) as session:
            session.wait(lambda: "Q> " in session.text)
            session.settle(.8)
            self.assertFalse(session.mark("start"))
            session.send("reply\n")
            session.wait(lambda: session.mark("done"))
            session.settle()
            self.assertEqual((session.marks / "count").read_text(), "x")
            session.send("echo again\n")
            session.wait(lambda: "\nagain" in session.text)
            session.settle()
            self.assertEqual((session.marks / "count").read_text(), "x")

    def test_previous_status_survives_a_later_dispatch(self):
        startup = (
            "arm() { __binit_state[slow]=pending; __binit_wait_pending=1; __binit_wait_queue+=(slow); return 7; }\n"
        )
        body = "printf x >> MARKS/count\ntouch MARKS/done\n"
        with Session(body, startup) as session:
            session.wait(lambda: session.mark("done"))
            session.settle()
            session.send("arm\n")
            session.wait(lambda: (session.marks / "count").read_text() == "xx")
            session.settle()
            session.send("echo st=$?\n")
            session.wait(lambda: "st=7" in session.text)
            self.assertEqual((session.marks / "count").read_text(), "xx")

    def test_last_argument_survives_a_later_dispatch(self):
        startup = "arm() { __binit_state[slow]=pending; __binit_wait_pending=1; __binit_wait_queue+=(slow); return 7; }\n"
        body = "printf x >> MARKS/count\ntrue plugin-argument\ntouch MARKS/done\n"
        with Session(body, startup) as session:
            session.wait(lambda: session.mark("done"))
            session.settle()
            session.send("arm sentinel-argument\n")
            session.wait(lambda: (session.marks / "count").read_text() == "xx")
            session.settle()
            session.send("printf 'observed=%s:%s\\n' \"$_\" \"$?\"\n")
            session.wait(lambda: "observed=sentinel-argument:7" in session.text)

    def test_vi_mode_keeps_keys_typed_during_loading(self):
        with Session(SLOW, mode="vi") as session:
            session.wait(lambda: PROMPT in session.text)
            session.send("echo vi-kept")
            self.assertFalse(session.mark("done"))
            session.wait(lambda: session.mark("done"))
            session.send("\n")
            session.wait(lambda: "\nvi-kept" in session.text)

    def test_an_interrupted_load_is_reported_and_the_shell_continues(self):
        body = "touch MARKS/start\nsleep 3\ntouch MARKS/done\n"
        with Session(body) as session:
            session.wait(lambda: session.mark("start"))
            session.send(b"\x03")
            session.settle()
            session.send("binit profile | grep -o 'load interrupted'\n")
            session.wait(lambda: "\nload interrupted" in session.text)
            self.assertFalse(session.mark("done"))
            session.send("echo still-here\n")
            session.wait(lambda: "\nstill-here" in session.text)

    def test_without_the_module_the_plugin_loads_at_the_first_prompt(self):
        with Session(SLOW, menu=False, wait_edit=True) as session:
            session.wait(lambda: session.mark("done"))
            session.wait(lambda: PROMPT in session.text)
            session.send("type -t waited_fn\n")
            session.wait(lambda: "function" in session.text)

    def test_opt_in_echoes_before_tail_and_checks_alias_at_first_use(self):
        body = TAIL + "waited_fn() { printf 'accepted=%s\\n' \"$1\"; }\nalias waited=waited_fn\ntouch MARKS/done\n"
        for mode in ("emacs", "vi"):
            with self.subTest(mode=mode), Session(
                body, mode=mode, first_wait_body=FIRST, wait_edit=True
            ) as session:
                session.wait(lambda: session.mark("first"))
                session.send("waited 'two words'")
                (session.marks / "release").touch()
                session.wait(lambda: session.mark("tail") and "waited 'two words'" in session.text)
                self.assertFalse(session.mark("done"))
                (session.marks / "finish").touch()
                session.wait(lambda: session.mark("done"))
                session.send("\n")
                session.wait(lambda: "\naccepted=two words" in session.text)

    def test_default_and_native_disabled_apply_key_remap_before_editing(self):
        body = TAIL + "bind -m emacs-standard '\"x\": \"y\"'\ntouch MARKS/done\n"
        for menu in (True, False):
            with self.subTest(menu=menu), Session(
                body, menu=menu, first_wait_body=FIRST, wait_edit=not menu
            ) as session:
                session.wait(lambda: session.mark("first"))
                session.send("x")
                (session.marks / "release").touch()
                session.wait(lambda: session.mark("tail"))
                (session.marks / "finish").touch()
                # The terminal may echo x before the first prompt in the fallback.
                session.wait(lambda: session.cursor_line.endswith(f"{PROMPT} y"))

    def test_opt_in_interrupt_recovers_from_a_tail_after_echo(self):
        with Session(TAIL, first_wait_body=FIRST, wait_edit=True) as session:
            session.wait(lambda: session.mark("first"))
            session.send("echo never-runs")
            (session.marks / "release").touch()
            session.wait(lambda: session.mark("tail") and "echo never-runs" in session.text)
            os.killpg(session.pid, signal.SIGINT)
            session.wait(lambda: session.cursor_line == PROMPT)
            session.send("binit profile | grep -o 'load interrupted'\n")
            session.wait(lambda: "\nload interrupted" in session.text)
            self.assertNotIn("\nnever-runs", session.text)

    def test_opt_in_finishes_the_batch_when_a_binding_disables_native(self):
        body = "alias waited='echo still-loaded'\n"
        for action in ("builtin binit_complete off", "enable -d binit_complete"):
            with self.subTest(action=action), Session(
                body, f"""bind -x '"\\C-g": {action}'\n""",
                first_wait_body=FIRST, wait_edit=True
            ) as session:
                session.wait(lambda: session.mark("first"))
                session.send(b"\x07waited\n")
                (session.marks / "release").touch()
                session.wait(lambda: "\nstill-loaded" in session.text)
                self.assertNotIn("command not found", session.text)

    def test_opt_in_does_not_retry_a_wait_condition_during_the_same_prompt(self):
        startup = (
            "try_first() { printf x >> MARKS/attempts;\n"
            + FIRST
            + "return 1; }\n__binit_when[first]=try_first\n"
        )
        with Session("touch MARKS/done\n", startup, first_wait_body=":", wait_edit=True) as session:
            session.wait(lambda: session.mark("first"))
            session.send("x")
            (session.marks / "release").touch()
            session.wait(lambda: session.mark("done") and session.cursor_line.endswith(f"{PROMPT} x"))
            self.assertEqual((session.marks / "attempts").read_text(), "x")


if __name__ == "__main__":
    unittest.main()
