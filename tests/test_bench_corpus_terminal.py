import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from bench_corpus_terminal import CheckedShell
from bench_ecosystem import environment


class CheckedShellTests(unittest.TestCase):
    def test_pty_allocation_error_is_preserved(self):
        with patch('bench_interactive.pty.fork', side_effect=OSError('PTY exhausted')):
            with self.assertRaisesRegex(OSError, 'PTY exhausted'):
                CheckedShell('/bin/bash', 'unused', ROOT, {})

    def test_normal_close_retains_bytes_and_status(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            root = Path(temporary)
            rc = root / 'rc'
            rc.write_text("PS1='BENCH> '\ntrap 'printf closing-marker' EXIT\n")
            receipt = root / 'terminal.raw'
            shell = CheckedShell('/bin/bash', rc, root, environment(root), receipt_path=receipt)
            shell.close()
            self.assertIn(b'closing-marker', receipt.read_bytes())
            self.assertEqual(json.loads((root / 'terminal.raw.close.json').read_text()),
                             {'exit_status': 0, 'forced_exit': False, 'output_truncated': False})

    def test_constructor_failure_retains_raw_and_abnormal_status(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            root = Path(temporary)
            rc = root / 'rc'
            rc.write_text("printf constructor-marker; exit 37\n")
            receipt = root / 'terminal.raw'
            with self.assertRaises(Exception):
                CheckedShell('/bin/bash', rc, root, environment(root), receipt_path=receipt)
            self.assertIn(b'constructor-marker', receipt.read_bytes())
            self.assertEqual(json.loads((root / 'terminal.raw.close.json').read_text())['exit_status'], 37)

    def test_missing_interrupt_prompt_still_attempts_graceful_exit(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            root = Path(temporary)
            rc = root / 'rc'
            rc.write_text("PS1='BENCH> '\n")
            shell = CheckedShell('/bin/bash', rc, root, environment(root))

            # Like the real wait, give up only after the timeout; an instant failure would type
            # `exit` while Bash is still handling the interrupt, and Bash 5.3 discards it.
            def no_interrupt_prompt(predicate, seconds=10):
                time.sleep(seconds)
                raise RuntimeError('no interrupt prompt')

            with patch.object(shell, 'until', side_effect=no_interrupt_prompt):
                shell.close()
            self.assertEqual(shell.exit_status, 0)
            self.assertFalse(shell.forced_exit)

    def test_verified_empty_prompt_does_not_receive_an_interrupt(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            root = Path(temporary)
            rc = root / 'rc'
            rc.write_text("PS1='BENCH> '\ntrap 'printf unexpected-interrupt' INT\n")
            shell = CheckedShell('/bin/bash', rc, root, environment(root))
            shell.close(ready=True)
            self.assertEqual(shell.exit_status, 0)
            self.assertNotIn(b'unexpected-interrupt', shell.raw)

    def test_abnormal_close_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            root = Path(temporary)
            rc = root / 'rc'
            rc.write_text("PS1='BENCH> '\n")
            receipt = root / 'terminal.raw'
            shell = CheckedShell('/bin/bash', rc, root, environment(root), receipt_path=receipt)
            os.write(shell.fd, b'exit 37\n')
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    shell.read(.01)
                except OSError:
                    break
            with self.assertRaisesRegex(RuntimeError, 'abnormal shell close'):
                shell.close()
            self.assertEqual(json.loads((root / 'terminal.raw.close.json').read_text())['exit_status'], 37)
