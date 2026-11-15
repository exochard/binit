"""Exercise the private worker through its real executable boundary."""
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get("BINIT_TEST_BINARY", ROOT / "target/release/binit")).resolve()


@unittest.skipUnless(sys.platform == "linux", "eligible workers require Linux")
class CompletionWorkerTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="worker-test-", dir=ROOT / "tmp")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def worker(self, program, *arguments, parent=None, **kwargs):
        kwargs.setdefault("input", b"")
        return subprocess.run([str(BINARY), "__completion-worker", str(parent or os.getpid()),
                               str(program), *arguments], capture_output=True, timeout=8, **kwargs)

    def test_context_stdin_is_complete_and_sealed(self):
        payload = b"private context\0" * 2000
        result = self.python("""import errno,os,sys
data=sys.stdin.buffer.read()
try: os.write(0,b'x')
except OSError as e: assert e.errno == errno.EPERM, e
else: raise AssertionError('context remained writable')
sys.stdout.buffer.write(data)
""", input=payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, payload)

    def test_closed_inherited_stdin_becomes_sealed_empty_input(self):
        result = self.python("import fcntl,sys; assert fcntl.fcntl(0,fcntl.F_GET_SEALS) == 15; assert sys.stdin.buffer.read() == b''",
                             preexec_fn=lambda: os.close(0))
        self.assertEqual(result.returncode, 0, result.stderr)

    def python(self, code, **kwargs):
        return self.worker(sys.executable, "-c", code, **kwargs)

    def test_exec_preserves_arguments_and_cwd_without_interpreting_options(self):
        result = self.worker(sys.executable, "-c", "import os,sys; print(repr(sys.argv[1:])); print(os.getcwd())",
                             "--help", "two words", "Ω", cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode().splitlines(), ["['--help', 'two words', 'Ω']", str(self.root)])

    def test_child_limits_do_not_change_parent_limits(self):
        before = [resource.getrlimit(item) for item in (resource.RLIMIT_AS, resource.RLIMIT_CPU, resource.RLIMIT_NOFILE)]
        result = self.python("""import json,resource
print(json.dumps([resource.getrlimit(x) for x in (resource.RLIMIT_AS,resource.RLIMIT_CPU,resource.RLIMIT_NOFILE,resource.RLIMIT_CORE)]))
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [[268435456, 268435456], [2, 2], [32, 32], [0, 0]])
        self.assertEqual(before, [resource.getrlimit(item) for item in (resource.RLIMIT_AS, resource.RLIMIT_CPU, resource.RLIMIT_NOFILE)])

    def test_worker_cannot_fork_or_start_threads(self):
        result = self.python("""import errno,os,threading
try:
 p=os.fork()
except OSError as e:
 assert e.errno == errno.EPERM, e
else:
 if not p: os._exit(0)
 os.waitpid(p,0)
 raise AssertionError('fork allowed')
try:
 t=threading.Thread(target=lambda: None); t.start()
except RuntimeError:
 pass
else:
 t.join(); raise AssertionError('thread allowed')
print('denied')
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"denied\n")

    def test_worker_cannot_raise_or_replace_resource_limits(self):
        result = self.python("""import errno,resource
for operation in (lambda: resource.setrlimit(resource.RLIMIT_NOFILE,(32,32)),
                  lambda: resource.prlimit(0,resource.RLIMIT_CPU,(2,2))):
 try: operation()
 except (OSError,ValueError): pass
 else: raise AssertionError('resource mutation allowed')
print('denied')
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"denied\n")

    def test_inherited_descriptor_is_closed(self):
        with (self.root / "private").open("w") as stream:
            fd = fcntl.fcntl(stream.fileno(), fcntl.F_DUPFD_CLOEXEC, 128)
            try:
                result = self.python(f"""import os,errno
try: os.fstat({fd})
except OSError as e: assert e.errno == errno.EBADF
else: raise AssertionError('inherited descriptor remains open')
""", pass_fds=(fd,))
            finally:
                os.close(fd)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_memory_and_descriptor_limits_stop_real_allocations(self):
        result = self.python("""import errno,os
try: large=bytearray(512*1024*1024)
except MemoryError: pass
else: raise AssertionError('memory limit not enforced')
opened=[]
try:
 for _ in range(40): opened.append(os.open('/dev/null',os.O_RDONLY))
except OSError as e:
 assert e.errno == errno.EMFILE, e
else: raise AssertionError('descriptor limit not enforced')
assert len(opened) <= 29
print('bounded')
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"bounded\n")

    def test_cpu_limit_terminates_busy_worker(self):
        result = self.python("while True: pass")
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)

    def test_stricter_inherited_limits_remain_stricter(self):
        result = self.python("import resource; print(resource.getrlimit(resource.RLIMIT_NOFILE))",
                             preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"(16, 16)\n")

    def test_raw_process_syscalls_and_abi_switch_are_rejected(self):
        fixture = self.root / "syscalls"
        subprocess.run([os.environ.get("CC", "cc"), "-std=c17", "-O2", "-Wall", "-Wextra", "-Werror",
                        str(ROOT / "tests/fixtures/worker_syscalls.c"), "-o", str(fixture)], check=True)
        result = self.worker(fixture)
        self.assertEqual(result.returncode, 0, result.stderr)
        if os.uname().machine == "x86_64":
            compat = self.worker(fixture, "compat")
            self.assertEqual(compat.returncode, -signal.SIGSYS, compat.stderr)

    def test_mismatched_parent_and_oversized_context_fail_before_exec(self):
        marker = self.root / "executed"
        code = f"from pathlib import Path; Path({str(marker)!r}).touch()"
        mismatch = self.worker(sys.executable, "-c", code, parent=os.getpid() + 1)
        oversized = self.worker(sys.executable, "-c", code, "x" * 65536)
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertNotEqual(oversized.returncode, 0)
        self.assertFalse(marker.exists())

    def test_parent_exit_kills_worker_and_reaps_owned_orphan(self):
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(previous), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        parent = None
        worker_pid = None
        try:
            ready = self.root / "ready"
            child = f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(30)"
            supervisor = "import os,subprocess,sys,time; p=subprocess.Popen([sys.argv[1],'__completion-worker',str(os.getpid()),sys.executable,'-c',sys.argv[2]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid,flush=True); time.sleep(30)"
            parent = subprocess.Popen([sys.executable, "-c", supervisor, str(BINARY), child],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
            worker_pid = int(parent.stdout.readline())
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(ready.exists(), "worker failed to reach its executed body")
            parent.terminate()
            parent.wait(timeout=2)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                pid, status = os.waitpid(worker_pid, os.WNOHANG)
                if pid:
                    worker_pid = None
                    self.assertTrue(os.WIFSIGNALED(status))
                    self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
                    break
                time.sleep(.005)
            self.assertIsNone(worker_pid, "worker outlived its parent")
        finally:
            if parent is not None:
                if parent.poll() is None:
                    parent.terminate()
                parent.wait(timeout=2)
                parent.stdout.close()
            if worker_pid is not None:
                try:
                    os.kill(worker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(worker_pid, 0)
            libc.prctl(36, previous.value, 0, 0, 0)
