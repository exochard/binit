"""Everyday shell use with the development plugins loaded together.

Each case drives one interactive Bash the way a person does: entering and leaving repositories, pasting
blocks, mistyping commands, typing ahead of the prompt, recalling history and interrupting. The plugins
come from BINIT_PLUGIN_ROOT (the directory holding the plugin repositories); without it the cases skip.
"""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time
import unittest
from unittest.mock import patch

from test_completion import ROOT, Session

PLUGIN_ROOT = Path(os.environ.get("BINIT_PLUGIN_ROOT", "/nonexistent"))
PLUGINS = ("git-prompt", "history-search", "autosuggestions", "project-context", "directory-jump",
           "syntax-highlighting")
PROMPT = "READY> "
# The settings an owner trial uses, with distinct colors so each state can be told apart. They come
# through the environment so that every plugin sees them when it loads.
SETTINGS = {
    "BINIT_GIT_PROMPT_MODE": "status",
    "BINIT_HISTORY_MATCH": "words",
    "BINIT_HISTORY_CONTEXT": "1",
    "BINIT_HISTORY_DELETE": "1",
    "BINIT_SUGGESTION_STRATEGIES": "history,previous",
    "BINIT_PROJECT_SCOPE": "ancestors",
    "BINIT_JUMP_RANK": "frecency",
    "BINIT_HIGHLIGHT_EXTENDED": "1",
    "BINIT_HIGHLIGHT_STYLES": "command=96,external=92,missing=91",
}
MISSING = b"\x1b[91m"
UNRESOLVED = b"\x1b[96m"


def available():
    return (os.environ.get("BINIT_EXPECT_HIGHLIGHT") == "1" and shutil.which("git") is not None and
            all((PLUGIN_ROOT / name / "plugin.toml").is_file() for name in PLUGINS))


@unittest.skipUnless(available(), "BINIT_PLUGIN_ROOT with the development plugins and a grammar build required")
class DailySessionTests(unittest.TestCase):
    def session(self, mode, extra=""):
        # The checkout encloses the session directory; the ceiling keeps it out of the Git prompt.
        with patch.dict(os.environ, SETTINGS, GIT_CEILING_DIRECTORIES=str(ROOT)):
            session = Session(extra=extra, values=0, mode=mode, framework=True,
                              plugins=[PLUGIN_ROOT / name for name in PLUGINS])
        self.addCleanup(session.close)
        self.ready(session)
        return session

    def repository(self, session, name="repo"):
        repo = session.directory / name
        repo.mkdir()
        environment = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
        for command in (["init", "-q", "-b", "trunk"], ["-c", "user.name=T", "-c", "user.email=t@example.invalid",
                                                        "commit", "-q", "--allow-empty", "-m", "start"]):
            subprocess.run(["git", *command], cwd=repo, env=environment, check=True)
        return repo

    @staticmethod
    def at_prompt(session):
        row = session.screen.display[session.screen.cursor.y].rstrip()
        return row.endswith(PROMPT.rstrip()) and session.screen.cursor.x == len(row) + 1

    def ready(self, session, seconds=3):
        session.wait(lambda: self.at_prompt(session), seconds)

    def prompt_row(self, session):
        return session.screen.display[session.screen.cursor.y].rstrip()

    def run_line(self, session, line):
        session.send(line + "\n")
        session.pump(.05)
        self.ready(session)

    def alive(self, session):
        session.transcript.clear()
        session.send("echo alive-$((6 * 7))\n")
        session.wait(lambda: "alive-42" in session.text)
        self.ready(session)

    def same_terminal(self, session, before):
        session.send(f"stty -g > {shlex.quote(str(session.directory / 'tty-after'))}\n")
        self.ready(session)
        after = (session.directory / "tty-after").read_text()
        self.assertEqual(before, after)

    def record_terminal(self, session):
        session.send(f"stty -g > {shlex.quote(str(session.directory / 'tty-before'))}\n")
        self.ready(session)
        return (session.directory / "tty-before").read_text()

    def test_git_segment_follows_cd_and_each_command(self):
        for mode in ("emacs", "vi"):
            with self.subTest(mode=mode):
                session = self.session(mode)
                self.repository(session)
                self.assertNotIn("[", self.prompt_row(session))
                self.run_line(session, "cd repo")
                self.assertEqual(self.prompt_row(session), "[trunk] " + PROMPT.rstrip())
                self.run_line(session, "touch fresh")
                self.assertEqual(self.prompt_row(session), "[trunk ?1] " + PROMPT.rstrip())
                self.run_line(session, "cd ..")
                self.assertEqual(self.prompt_row(session), PROMPT.rstrip())
                # Several commands arriving at once, as from a paste ending in newlines.
                session.send("cd repo\ncd ..\ncd repo\n")
                session.wait(lambda: self.prompt_row(session) == "[trunk ?1] " + PROMPT.rstrip() and
                             self.at_prompt(session))

    def test_pasted_block_is_colored_and_interrupt_keeps_the_shell(self):
        block = "echo one\n\tls -l | cat\ngit status --short\nprintf '%s\\n' \"$(true)\""
        for mode in ("emacs", "vi"):
            with self.subTest(mode=mode):
                session = self.session(mode)
                before = self.record_terminal(session)
                start = session.screen.cursor.y
                session.send(b"\x1b[200~" + block.encode() + b"\x1b[201~")
                cells = ((0, len(PROMPT)), (1, 8), (1, 16), (2, 0), (3, 0))
                session.wait(lambda: all(session.screen.buffer[start + row][column].fg != "default"
                                         for row, column in cells))
                self.assertEqual([session.screen.display[start + row].rstrip() for row in range(4)],
                                 [PROMPT + "echo one", "        ls -l | cat", "git status --short",
                                  "printf '%s\\n' \"$(true)\""])
                session.send(b"\x03")
                self.ready(session)
                self.alive(session)
                self.same_terminal(session, before)

    def test_mistyped_command_is_red_while_typed_and_interrupt_is_safe(self):
        for mode in ("emacs", "vi"):
            with self.subTest(mode=mode):
                session = self.session(mode)
                session.send("s")
                session.wait(lambda: MISSING + b"s" in session.transcript)
                session.transcript.clear()
                for key in "asafjlsadkf":
                    session.send(key)
                    session.pump(.03)
                session.wait(lambda: MISSING + b"sasafjlsadkf" in session.transcript)
                self.assertNotIn(UNRESOLVED, session.transcript)
                session.send(b"\x03")
                self.ready(session)
                self.alive(session)
                session.send("sasafjlsadkf\n")
                session.wait(lambda: "sasafjlsadkf: command not found" in session.text)
                self.ready(session)

    def test_keys_typed_ahead_of_a_slow_prompt_reach_the_editor(self):
        for mode in ("emacs", "vi"):
            with self.subTest(mode=mode):
                session = self.session(mode)
                repo = self.repository(session)
                fake = session.directory / "bin"
                fake.mkdir()
                (fake / "git").write_text(f"#!/bin/sh\nsleep .2\nexec {shlex.quote(shutil.which('git'))} \"$@\"\n")
                (fake / "git").chmod(0o700)
                before = self.record_terminal(session)
                self.run_line(session, f"history -s 'echo typed-ahead-witness'; PATH={shlex.quote(str(fake))}:$PATH")
                self.run_line(session, f"cd {shlex.quote(str(repo))}")
                # Each prompt now waits for a slow scan; keys typed meanwhile must not be lost or altered.
                session.transcript.clear()
                session.send("true\necho keys-ahead")
                session.wait(lambda: self.prompt_row(session).endswith(PROMPT + "echo keys-ahead"))
                session.send(" done\n")
                session.wait(lambda: "keys-ahead done" in session.text)
                self.ready(session)
                session.send(b"true\n\x12")
                # The history search panel: its state row starts with "history" and shows the query caret.
                session.wait(lambda: any(row.startswith("history  ") and "\u258f" in row
                                         for row in session.screen.display))
                session.send(b"\x1b")
                session.pump(.1)
                session.send(b"\x03")
                self.ready(session)
                # Ctrl-C while a prompt waits for its scan.
                session.send(b"true\n")
                session.pump(.02)
                session.send(b"\x03")
                self.ready(session)
                self.alive(session)
                self.same_terminal(session, before)

    def test_suggestion_history_and_jump_work_together(self):
        for mode in ("emacs", "vi"):
            with self.subTest(mode=mode):
                session = self.session(mode)
                for name in ("alpha", "beta"):
                    (session.directory / name).mkdir()
                self.run_line(session, "echo sample-witness-1")
                session.send("echo sam")
                session.wait(lambda: "echo sample-witness-1" in self.prompt_row(session) or
                             "sample-witness-1" in session.text.split(PROMPT)[-1])
                session.send(b"\x1b[C")
                session.pump(.1)
                session.send("\n")
                session.wait(lambda: session.text.count("sample-witness-1") >= 4)
                self.ready(session)
                self.run_line(session, "cd alpha")
                self.run_line(session, "cd ../beta")
                self.run_line(session, "j alpha; pwd")
                self.assertTrue([row.rstrip() for row in session.text.rstrip().splitlines()][-2].endswith("/alpha"), session.text)
                self.alive(session)


if __name__ == "__main__":
    unittest.main()
