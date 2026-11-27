import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from terminal import Terminal


class TerminalCaptureTests(unittest.TestCase):
    def session(self, directory):
        rc = directory / 'rc.bash'
        rc.write_text("PS1='READY> '\ntrap 'printf \"EXIT-RECEIPT\\n\" >&2' EXIT\n")
        return Terminal(rc, directory)

    def test_prompt_after_line_clear_requires_the_command_marker(self):
        terminal = Terminal.__new__(Terminal)
        terminal.fd = 42
        terminal.transcript = bytearray()
        frames = [b'\rREADY> ', b'\x1eBINIT-CLEARED\x1f\r\nREADY> ']
        with (patch('terminal.os.write'),
              patch('terminal.select.select', return_value=([terminal.fd], [], [])),
              patch('terminal.os.read', side_effect=frames) as read):
            terminal.prompt = b'READY> '
            terminal.clear_line()
        self.assertEqual(read.call_count, 2)
        self.assertEqual(terminal.transcript, b''.join(frames))

    def test_close_preserves_exit_diagnostics_and_status(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-terminal-', dir=ROOT) as directory:
            with patch.dict(os.environ, INPUTRC='/dev/null', HISTFILE='/dev/null'):
                terminal = self.session(Path(directory))
                terminal.close()
                self.assertIn(b'EXIT-RECEIPT', terminal.transcript)
                self.assertEqual(terminal.exit_status, 0)
                self.assertFalse(terminal.forced_exit)
                self.assertFalse(terminal.output_truncated)

    def test_delayed_descendant_diagnostics_are_drained(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-terminal-', dir=ROOT) as directory:
            with patch.dict(os.environ, INPUTRC='/dev/null', HISTFILE='/dev/null'):
                terminal = self.session(Path(directory))
                try:
                    os.write(terminal.fd, b"(trap '' HUP; sleep .05; printf 'LATE-DIAGNOSTIC\\n' >&2) & disown; exit 7\n")
                    os.waitid(os.P_PID, terminal.pid, os.WEXITED | os.WNOWAIT)
                finally:
                    terminal.close()
                time.sleep(.1)
                self.assertIn(b'LATE-DIAGNOSTIC\r\n', terminal.transcript)
                self.assertEqual(terminal.exit_status, 7)
                self.assertFalse(terminal.output_truncated)

    def test_already_exited_child_is_drained_and_keeps_its_status(self):
        with tempfile.TemporaryDirectory(prefix='.scratch-terminal-', dir=ROOT) as directory:
            with patch.dict(os.environ, INPUTRC='/dev/null', HISTFILE='/dev/null'):
                terminal = self.session(Path(directory))
                try:
                    os.write(terminal.fd, b'exit 7\n')
                    os.waitid(os.P_PID, terminal.pid, os.WEXITED | os.WNOWAIT)
                finally:
                    terminal.close()
                self.assertIn(b'EXIT-RECEIPT', terminal.transcript)
                self.assertEqual(terminal.exit_status, 7)
                self.assertFalse(terminal.forced_exit)
                self.assertFalse(terminal.output_truncated)


if __name__ == '__main__':
    unittest.main()
