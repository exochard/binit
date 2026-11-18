"""Private candidate fixtures must not inherit the caller's controlling terminal."""
import os
import json
from pathlib import Path
import pty
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('BINIT_TEST_BINARY', ROOT / 'target/release/binit')).resolve()


class CheckTerminalTests(unittest.TestCase):
    def test_checks_and_diagnostics_run_from_a_controlling_terminal(self):
        scratch = ROOT / 'tmp'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='check-terminal-', dir=scratch) as directory:
            work = Path(directory)
            state = work / 'state'
            config = work / 'plugins.toml'
            config.write_text('schema=1\nplugins=[]\n')
            marker = work / 'fixture-ran'
            inspect_terminal = ('import os\ntry: fd=os.open("/dev/tty",os.O_RDWR)\n'
                                'except OSError: pass\nelse: os.close(fd); raise SystemExit(1)\n')
            (work / 'check.bash').write_text(
                '[[ ! -t 0 ]] || return 1\n'
                + shlex.quote(sys.executable) + ' -c ' + shlex.quote(inspect_terminal) + ' || return 1\n'
                'printf fixture > ' + shlex.quote(str(marker)) + '\n')
            plan = work / 'checks.toml'
            plan.write_text('schema=1\n[[checks]]\nid="terminal"\nscript="check.bash"\ntimeout_ms=700\nmax_elapsed_ms=600\n')
            environment = dict(os.environ, HOME=str(work), INPUTRC='/dev/null')
            for name in ('BASH_ENV', 'ENV'):
                environment.pop(name, None)
            for arguments in (['prepare', str(config)], ['candidate', 'prepare', str(config)]):
                subprocess.run([str(BINARY), *arguments, '--state', str(state)], env=environment,
                               check=True, capture_output=True, timeout=10)
            for arguments, interrupt in ((['candidate', 'check', str(plan)], False),
                                         (['diagnose', str(plan), '--json'], False),
                                         (['candidate', 'check', str(plan)], True)):
                with self.subTest(command=arguments[0], interrupt=interrupt):
                    running, late = work / 'running', work / 'late'
                    before = (state / 'current').read_bytes()
                    if interrupt:
                        (work / 'check.bash').write_text(
                            '(sleep .3; printf leaked > ' + shlex.quote(str(late)) + ') &\n'
                            'printf running > ' + shlex.quote(str(running)) + '\nsleep 30\n')
                        plan.write_text(plan.read_text().replace('timeout_ms=700', 'timeout_ms=3000'))
                    pid, descriptor = pty.fork()
                    if pid == 0:
                        os.execve(str(BINARY), [str(BINARY), *arguments, '--state', str(state)], environment)
                    output, states, status = bytearray(), set(), None
                    sent_interrupt = False
                    try:
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            done, current = os.waitpid(pid, os.WNOHANG)
                            if done:
                                status = current
                                break
                            if interrupt and running.exists() and not sent_interrupt:
                                self.assertEqual(os.tcgetpgrp(descriptor), pid)
                                os.write(descriptor, b'\x03')
                                sent_interrupt = True
                            children = Path(f'/proc/{pid}/task/{pid}/children')
                            try:
                                for child in children.read_text().split():
                                    text = Path(f'/proc/{child}/stat').read_text()
                                    states.add(text.rsplit(')', 1)[1].split()[0])
                            except (FileNotFoundError, ProcessLookupError):
                                pass
                            if select.select([descriptor], [], [], .01)[0]:
                                try:
                                    data = os.read(descriptor, 65536)
                                except OSError:
                                    data = b''
                                output.extend(data)
                                if len(output) > 128 * 1024:
                                    self.fail('check output exceeded fixture budget')
                        self.assertIsNotNone(status, 'manager did not finish')
                        drain_deadline = time.monotonic() + .5
                        while time.monotonic() < drain_deadline and select.select([descriptor], [], [], .01)[0]:
                            try:
                                data = os.read(descriptor, 65536)
                            except OSError:
                                break
                            if not data:
                                break
                            output.extend(data)
                            if len(output) > 128 * 1024:
                                self.fail('check output exceeded fixture budget')
                        text = output.decode(errors='replace').replace('\r', '')
                        details = text[-1000:]
                        if arguments[0] == 'diagnose' and text.lstrip().startswith('{'):
                            try:
                                report, _ = json.JSONDecoder().raw_decode(text.lstrip())
                                details = str([dict(complete=item['complete'], issues=item['trace']['issues'])
                                               for item in report['diagnostics']])
                            except (ValueError, KeyError):
                                pass
                        if interrupt:
                            self.assertTrue(sent_interrupt)
                            self.assertNotEqual(os.waitstatus_to_exitcode(status), 0)
                            self.assertIn('interrupted by signal 2', text)
                            self.assertEqual((state / 'current').read_bytes(), before)
                            time.sleep(.4)
                            self.assertFalse(late.exists(), 'owned descendant survived interruption')
                        else:
                            self.assertEqual(os.waitstatus_to_exitcode(status), 0,
                                             f'child states={sorted(states)}; {details}')
                            self.assertEqual(marker.read_text(), 'fixture')
                    finally:
                        if status is None:
                            os.kill(pid, signal.SIGKILL)
                            os.waitpid(pid, 0)
                        os.close(descriptor)
