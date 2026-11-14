"""Register isolated providers through the initialized shell's public API."""
import os
import hashlib
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_completion as completion


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("BINIT_NATIVE") != "0" and completion.MODULE.is_file(),
                     "matching native completion module required")
class CompletionRegistrationTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="registration-test-", dir=ROOT / "tmp")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        change = patch.object(completion, "ROOT", self.root)
        change.start()
        self.addCleanup(change.stop)
        self.status = self.root / "status"
        self.provider = shlex.join([sys.executable, "-c", "import os,sys; assert sys.stdin.buffer.read().startswith(b'binit-completion-v1\\0'); os.write(1,b'alpha one\\0alpha two\\0')"])

    def command(self, session, text):
        self.status.unlink(missing_ok=True)
        session.send(f"\x01\x0b{text}; builtin printf '%s' \"$?\" > {shlex.quote(str(self.status))}\n")
        session.wait(lambda: self.status.exists() and self.status.stat().st_size)
        return int(self.status.read_text())

    def test_register_complete_disable_enable_and_unregister(self):
        with completion.Session(framework=True, values=0) as session:
            self.assertEqual(self.command(session, f"binit completions register probe_cmd {self.provider}"), 0)
            session.open("probe_cmd al")
            session.send(b"\r")
            session.wait(lambda: not session.panel)
            self.assertEqual(shlex.split(session.capture()[1]), ["probe_cmd", "alpha one"])
            self.assertEqual(self.command(session, "binit completions disable probe_cmd"), 0)
            self.assertEqual(self.command(session, "! complete -p probe_cmd >/dev/null 2>&1"), 0)
            self.assertEqual(self.command(session, "binit completions enable probe_cmd"), 0)
            session.open("probe_cmd al")
            session.send(b"\x1b")
            session.wait(lambda: not session.panel)
            self.assertEqual(self.command(session, "binit completions unregister probe_cmd"), 0)
            self.assertEqual(self.command(session, "! complete -p probe_cmd >/dev/null 2>&1"), 0)

    def test_registration_preserves_existing_stateful_provider(self):
        with completion.Session(framework=True, values=0) as session:
            self.assertEqual(self.command(session, f"binit completions register demo_cmd {self.provider}"), 1)
            session.open("demo_cmd va")
            session.send(b"\r")
            session.wait(lambda: not session.panel)
            self.assertEqual(shlex.split(session.capture()[1]), ["demo_cmd", "value one"])

    def test_registration_works_with_ordinary_readline_completion(self):
        provider = shlex.join([sys.executable, "-c", "import os; os.write(1,b'alpha one\\0')"])
        with completion.Session(framework=True, values=0, menu=False) as session:
            self.assertEqual(self.command(session, f"binit completions register probe_cmd {provider}"), 0)
            session.send(b"probe_cmd al\t")
            session.wait(lambda: session.screen.display[session.screen.cursor.y].startswith("READY> probe_cmd alpha"))
            self.assertEqual(shlex.split(session.capture()[1]), ["probe_cmd", "alpha one"])
            self.assertFalse(session.panel)

    def test_unregister_preserves_a_later_foreign_binding(self):
        with completion.Session(framework=True, values=0) as session:
            self.assertEqual(self.command(session, f"binit completions register probe_cmd {self.provider}"), 0)
            self.assertEqual(self.command(session, "complete -o filenames -F __values probe_cmd"), 0)
            self.assertEqual(self.command(session, "binit completions unregister probe_cmd"), 1)
            session.open("probe_cmd va")
            session.send(b"\r")
            session.wait(lambda: not session.panel)
            self.assertEqual(shlex.split(session.capture()[1]), ["probe_cmd", "value one"])

    def test_declared_plugin_registration_is_removed_on_owned_unload(self):
        with self.plugin_session() as session:
            session.open("probe_cmd al")
            session.send(b"\x1b")
            session.wait(lambda: not session.panel)
            self.assertEqual(self.command(session, "binit unload test"), 0)
            self.assertEqual(self.command(session, "! complete -p probe_cmd >/dev/null 2>&1"), 0)
            self.assertEqual(self.command(session, "[[ ${#__binit_provider_args[@]} == 0 ]]"), 0)

    def test_manual_takeover_with_same_wrapper_blocks_plugin_unload(self):
        with self.plugin_session() as session:
            session.open("probe_cmd al")
            session.send(b"\x1b")
            session.wait(lambda: not session.panel)
            self.assertEqual(self.command(session, "binit completions unregister probe_cmd"), 0)
            self.assertEqual(self.command(session, f"binit completions register probe_cmd {self.provider}"), 0)
            self.assertEqual(self.command(session, "binit unload test"), 1)
            session.open("probe_cmd al")
            session.send(b"\x1b")
            session.wait(lambda: not session.panel)
            self.assertEqual(self.command(session, "[[ ${__binit_provider_count[probe_cmd]} == 3 ]]"), 0)

    def test_fixed_arguments_are_literal_across_disable_and_enable(self):
        marker = self.root / "executed"
        argument = f"$(touch {marker}) ' quote\nsecond line"
        digest = hashlib.sha256(os.fsencode(argument)).hexdigest()
        code = f"import hashlib,os,sys; assert hashlib.sha256(os.fsencode(sys.argv[1])).hexdigest() == {digest!r}; os.write(1,b'alpha one\\0alpha two\\0')"
        registration = shlex.join(["binit", "completions", "register", "probe_cmd", sys.executable, "-c", code, argument])
        with completion.Session(framework=True, values=0) as session:
            self.assertEqual(self.command(session, registration), 0)
            self.assertEqual(self.command(session, "binit completions disable probe_cmd; binit completions enable probe_cmd"), 0)
            session.open("probe_cmd al")
            session.send(b"\x1b")
            session.wait(lambda: not session.panel)
            self.assertFalse(marker.exists())

    def plugin_session(self):
        original = completion.terminal.cli
        def prepare(state, *arguments):
            if arguments[0] == "prepare":
                manifest = Path(arguments[1]).parent / "plugin/plugin.toml"
                manifest.write_text(manifest.read_text().replace("load='startup'", "load='completion'") + "completions=['probe_cmd']\n")
            return original(state, *arguments)
        with patch.object(completion.terminal, "cli", side_effect=prepare):
            return completion.Session(framework=True, values=0,
                                      extra=f"binit completions register probe_cmd {self.provider}\n")


@unittest.skipUnless(os.environ.get("BINIT_NATIVE") == "0", "native-disabled build required")
class CompletionRegistrationFallbackTests(unittest.TestCase):
    def test_unavailable_registration_preserves_state_and_previous_provider(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="registration-fallback-", dir=ROOT / "tmp") as directory:
            root = Path(directory)
            environment = dict(os.environ, HOME=str(root), BINIT_STATE=str(root / "state"), TERM="dumb", HISTFILE="/dev/null", INPUTRC="/dev/null")
            environment.pop("BASH_ENV", None)
            environment.pop("ENV", None)
            binary = os.environ["BINIT_TEST_BINARY"]
            rc = root / "rc"
            rc.write_text(f'eval "$({shlex.quote(binary)} init)"\n')
            script = f"""keep() {{ COMPREPLY=(kept); }}
complete -F keep kept_cmd
before=$(complete -p kept_cmd)
binit completions register kept_cmd {shlex.quote(sys.executable)} -c pass && exit 10
[[ $(complete -p kept_cmd) == "$before" ]] || exit 11
binit completions register new_cmd {shlex.quote(sys.executable)} -c pass && exit 12
[[ ${{#__binit_provider_count[@]}} == 0 && ${{#__binit_provider_args[@]}} == 0 ]] || exit 13
complete -p new_cmd >/dev/null 2>&1 && exit 14
printf 'preserved\\n'
exit 0
"""
            result = subprocess.run([os.environ.get("BINIT_BASH", "/bin/bash"), "--noprofile", "--rcfile", str(rc), "-i"],
                                    input=script, text=True, capture_output=True, env=environment, cwd=root, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("preserved\n", result.stdout)
