#!/usr/bin/env python3
"""Bounded PTY close with retained exit evidence for corpus drivers."""
import json
import os
import select
import signal
import time
from pathlib import Path

from bench_ecosystem import BoundedShell
from bench_interactive import PROMPT


class CheckedShell(BoundedShell):
    def __init__(self, *args, receipt_path=None, close_recover=None, **kwargs):
        self.close_recover = close_recover
        self.receipt_path = Path(receipt_path) if receipt_path else None
        self.fd = None
        self.exit_status = None
        self.forced_exit = False
        self.output_truncated = False
        try:
            super().__init__(*args, **kwargs)
        except BaseException as error:
            if self.fd is None and error.__context__ is not None:
                raise error.__context__ from error
            raise

    def _drain_exited(self):
        deadline = time.monotonic() + .5
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
                self.output_truncated = True
                return
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            self.raw.extend(chunk)
            if len(self.raw) > 8 * 1024 * 1024:
                self.output_truncated = True
                return

    def close(self, *, ready=False):
        # A caller that verified an empty prompt can send exit without a new interrupt.
        if self.fd is None:
            return
        try:
            done, status = os.waitpid(self.pid, os.WNOHANG)
            if not done:
                try:
                    if not ready:
                        offset = len(self.raw)
                        os.write(self.fd, b'\x03')
                        if self.close_recover is not None:
                            self.read(.05)
                            self.close_recover(self)
                        else:
                            try:
                                self.until(lambda: PROMPT in self.raw[offset:], seconds=.5)
                            except (OSError, RuntimeError):
                                pass
                    os.write(self.fd, b'exit 0\n')
                except (OSError, RuntimeError):
                    pass
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    done, status = os.waitpid(self.pid, os.WNOHANG)
                    if done:
                        break
                    if select.select([self.fd], [], [], .05)[0]:
                        try:
                            data = os.read(self.fd, 65536)
                            self.raw.extend(data)
                            if len(self.raw) > 8 * 1024 * 1024:
                                self.output_truncated = True
                                break
                        except OSError:
                            pass
                if not done:
                    self.forced_exit = True
                    os.kill(self.pid, signal.SIGKILL)
                    _, status = os.waitpid(self.pid, 0)
            self.exit_status = os.waitstatus_to_exitcode(status)
            self._drain_exited()
        finally:
            os.close(self.fd)
            self.fd = None
            if self.receipt_path:
                self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
                self.receipt_path.write_bytes(self.raw)
                self.receipt_path.with_suffix(self.receipt_path.suffix + '.close.json').write_text(
                    json.dumps({'exit_status': self.exit_status, 'forced_exit': self.forced_exit,
                                'output_truncated': self.output_truncated}, indent=2) + '\n')
        if self.exit_status != 0 or self.forced_exit or self.output_truncated:
            raise RuntimeError(f'abnormal shell close: status={self.exit_status}, forced={self.forced_exit}, '
                               f'truncated={self.output_truncated}')
