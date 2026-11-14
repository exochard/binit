"""Opted-in external completion retains Readline's actual interaction path."""
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import test_completion as completion


ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get("BINIT_TEST_BINARY", ROOT / "target/release/binit")).resolve()


@unittest.skipUnless(os.environ.get("BINIT_NATIVE") != "0" and completion.MODULE.is_file(),
                     "matching native completion module required")
class EligibleCompletionTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="eligible-test-", dir=ROOT / "tmp")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.root_patch = patch.object(completion, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.reply = self.root / "reply"

    def session(self, code, extra=""):
        request = shlex.join([str(BINARY), sys.executable, "-c", code])
        setup = f"""{extra}
__eligible() {{
    COMPREPLY=()
    builtin binit_complete request {request}
    printf '%s\\0' "$?" "${{COMPREPLY[@]}}" > {shlex.quote(str(self.reply))}
}}
complete -o filenames -F __eligible probe_cmd
"""
        return completion.Session(extra=setup, values=0)

    def reply_values(self, session):
        session.wait(lambda: self.reply.exists() and self.reply.stat().st_size > 1)
        return self.reply.read_bytes().split(b"\0")[:-1]

    def test_complete_results_use_existing_menu_and_readline_quoting(self):
        with self.session("import os; os.write(1,b'alpha one\\0alpha two\\0')") as session:
            session.open("probe_cmd al")
            self.assertEqual(self.reply_values(session), [b"0", b"alpha one", b"alpha two"])
            session.send(b"\r")
            session.wait(lambda: not session.panel)
            self.assertEqual(shlex.split(session.capture()[1]), ["probe_cmd", "alpha one"])

    def test_typing_cancels_work_without_inserting_late_result(self):
        ready = self.root / "ready"
        code = f"import os,time; open({str(ready)!r},'w').close(); time.sleep(1); os.write(1,b'alpha stale\\0')"
        with self.session(code) as session:
            session.send(b"probe_cmd al\t")
            session.wait(ready.exists)
            session.send(b"xyz")
            self.assertEqual(self.reply_values(session), [b"1"])
            self.assertEqual(session.capture()[1], "probe_cmd alxyz")
            session.pump(.05)
            self.assertFalse(session.panel)

    def test_malformed_failed_and_oversized_results_are_discarded(self):
        cases = ["import os; os.write(1,b'alpha valid\\0truncated')",
                 "import os; os.write(1,b'alpha valid\\0'); raise SystemExit(7)",
                 "import os; os.write(1,b'a'*4097+b'\\0')",
                 "import os; os.write(1,b'a\\0'*4097)",
                 "import os; os.write(1,(b'a'*4095+b'\\0')*257)"]
        for code in cases:
            with self.subTest(code=code), self.session(code) as session:
                self.reply.unlink(missing_ok=True)
                session.send(b"probe_cmd al\t")
                self.assertEqual(self.reply_values(session), [b"1"])
                self.assertEqual(session.capture()[1], "probe_cmd al")

    def test_loader_constructor_is_excluded_before_helper_start(self):
        library = self.root / "constructor.so"
        marker = self.root / "constructor-forked"
        subprocess.run([os.environ.get("CC", "cc"), "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
                        str(ROOT / "tests/fixtures/worker_constructor.c"), "-o", str(library)], check=True)
        environment = dict(os.environ, LD_PRELOAD=str(library), BINIT_WORKER_CONSTRUCTOR_MARKER=str(marker))
        control = subprocess.run([str(BINARY), "__completion-worker", str(os.getpid()), sys.executable,
                                  "-c", "pass"], env=environment, capture_output=True, timeout=5)
        self.assertEqual(control.returncode, 0, control.stderr)
        self.assertTrue(marker.exists(), "control must prove a constructor runs before helper main")
        marker.unlink()
        extra = f"export LD_PRELOAD={shlex.quote(str(library))}\nexport BINIT_WORKER_CONSTRUCTOR_MARKER={shlex.quote(str(marker))}"
        with self.session("import os; os.write(1,b'alpha one\\0alpha two\\0')", extra=extra) as session:
            session.open("probe_cmd al")
            self.assertEqual(self.reply_values(session), [b"0", b"alpha one", b"alpha two"])
            self.assertFalse(marker.exists())
            session.send(b"\x1b")

    def test_timeout_discards_result_and_leaves_line_usable(self):
        with self.session("import time; time.sleep(30)") as session:
            started = time.monotonic()
            session.send(b"probe_cmd al\t")
            session.wait(lambda: self.reply.exists() and self.reply.stat().st_size > 1, seconds=3)
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(self.reply.read_bytes(), b"1\0")
            self.assertEqual(session.capture()[1], "probe_cmd al")

    def test_signal_cancels_before_trap_changes_context_or_disables_menu(self):
        ready = self.root / "ready"
        code = f"import time; open({str(ready)!r},'w').close(); time.sleep(1)"
        with self.session(code, extra="trap '__binit_generation_dir=changed; builtin binit_complete off' USR1") as session:
            session.send(b"probe_cmd al\t")
            session.wait(ready.exists)
            os.kill(session.terminal.pid, signal.SIGUSR1)
            self.assertEqual(self.reply_values(session), [b"1"])
            self.assertEqual(session.capture()[1], "probe_cmd al")

    def test_repeated_requests_release_children_and_descriptors(self):
        with self.session("import os; os.write(1,b'alpha one\\0alpha two\\0')") as session:
            process = Path(f"/proc/{session.terminal.pid}")
            descriptors = len(list((process / "fd").iterdir()))
            children = process / "task" / str(session.terminal.pid) / "children"
            for _ in range(10):
                self.reply.unlink(missing_ok=True)
                session.send(b"\x01\x0b")
                session.open("probe_cmd al")
                self.assertEqual(self.reply_values(session), [b"0", b"alpha one", b"alpha two"])
                session.send(b"\x1b")
                session.wait(lambda: not session.panel)
                self.assertEqual(len(list((process / "fd").iterdir())), descriptors)
                self.assertEqual(children.read_text().strip(), "")
