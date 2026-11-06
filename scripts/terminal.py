#!/usr/bin/env python3
"""Exercise the environment in a real PTY and measure prompt readiness."""
import errno
import json
import math
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
BINARY = ROOT / "target/release/binit"
if not BINARY.exists():
    BINARY = ROOT / "target/debug/binit"
PROMPT = b"READY> "


def cli(state, *args):
    return subprocess.run([str(BINARY), *map(str, args), "--state", str(state)],
                          check=True, capture_output=True, text=True).stdout


class Terminal:
    def __init__(self, rcfile, directory, safe=False, state=None, demo=False):
        started = time.perf_counter_ns()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(directory)
            env = dict(os.environ, TERM="dumb", PS1="READY> ", BINIT_SAFE="1" if safe else "0",
                       GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
            env.pop("BASH_ENV", None)
            env.pop("ENV", None)
            if demo:
                env['BINIT_DEMO_STATE'] = str(state)
                os.execvpe("bash", ["bash", str(ROOT / 'scripts/demo.bash')], env)
            if state is not None:
                os.execvpe(str(BINARY), [str(BINARY), 'shell', '--state', str(state)], env)
            os.execvpe("bash", ["bash", "--noprofile", "--rcfile", str(rcfile), "-i"], env)
        self.transcript = bytearray()
        self.prompt = (b'# ' if os.geteuid() == 0 else b'$ ') if demo else PROMPT
        try:
            self.read_until(self.prompt)
        except BaseException:
            self.close()
            raise
        self.startup_ms = (time.perf_counter_ns() - started) / 1e6

    def read_until(self, suffix, seconds=5):
        deadline = time.monotonic() + seconds
        data = bytearray()
        while time.monotonic() < deadline:
            if select.select([self.fd], [], [], 0.1)[0]:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                data.extend(chunk)
                self.transcript.extend(chunk)
                if data.endswith(suffix):
                    return data.decode(errors="replace")
        raise AssertionError(f"terminal did not reach {suffix!r}: {data!r}")

    def command(self, command):
        os.write(self.fd, command.encode() + b"\n")
        return self.read_until(self.prompt)

    def close(self):
        try:
            os.write(self.fd, b"\x15exit\n")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                done, _ = os.waitpid(self.pid, os.WNOHANG)
                if done:
                    return
                select.select([self.fd], [], [], 0.05)
            # Only this helper's own forked child may be signalled.
            os.kill(self.pid, signal.SIGTERM)
            os.waitpid(self.pid, 0)
        finally:
            os.close(self.fd)


def fixture(directory):
    state = directory / "state"
    cli(state, "prepare", ROOT / "config/demo.toml")
    generation = (state / "current").read_text()
    init = state / "generations" / generation / "init.bash"
    baseline = directory / "baseline.bash"
    baseline.write_text("PS1='READY> '\n")
    configured = directory / "configured.bash"
    # Use the same literal-source quoting as the manager emits.
    source = str(init).replace("'", "'\\''")
    configured.write_text(f"PS1='READY> '\nsource '{source}'\n")
    return state, baseline, configured


def smoke(directory):
    state, _, rcfile = fixture(directory)
    project = directory / "project"
    project.mkdir()
    shutil.copyfile(ROOT / "examples/project/.binit-project.toml", project / ".binit-project.toml")
    git_env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
    repository = directory / 'repo'
    def git(*args):
        subprocess.run(['git', '-C', str(repository), *args], env=git_env, check=True, capture_output=True)
    repository.mkdir()
    git('init', '--quiet', '--initial-branch=main')
    git('-c', 'user.name=BinitTest', '-c', 'user.email=fixtures@localhost', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', 'commit', '--quiet', '--allow-empty', '-m', 'fixture')
    git('branch', 'feature/binit')
    (directory / 'run.bash').write_text('printf "run-args=%s:%s:%s\\n" "$#" "$1" "$2"\nexit 37\n')
    terminal = Terminal(rcfile, directory, state=state)
    try:
        assert "pending" in terminal.command("binit profile")
        output = terminal.command('binit run --engine /bin/bash run.bash --help --state; printf "run-status=%s\\n" "$?"')
        assert 'run-args=2:--help:--state' in output and 'run-status=37' in output
        # Exercise actual Readline TAB dispatch, rather than invoking its callback.
        os.write(terminal.fd, b"git stat\t")
        terminal.read_until(b"git status ")
        os.write(terminal.fd, b"\x15\n")
        terminal.read_until(PROMPT)
        output = terminal.command("binit profile")
        assert any("git-completion" in row and "loaded" in row for row in output.splitlines()), terminal.transcript.decode(errors="replace")
        terminal.command("false")
        assert "saved-status=1" in terminal.command("printf 'saved-status=%s\\n' \"$?\"")
        terminal.command("cd project")
        assert "project:untrusted" in terminal.command("printf 'profile=%s\\n' \"${PROJECT_MODE-unset}\"")
        # Trusting the current directory must take effect at the next prompt.
        terminal.command("binit trust .")
        assert "mode=development" in terminal.command("printf 'mode=%s\\n' \"$PROJECT_MODE\"")
        literal = terminal.command("printf '%s\\n' \"$PROJECT_MESSAGE\"")
        assert "$(printf this-is-not-executed)" in literal
        terminal.command("cd ..")
        assert "mode=unset" in terminal.command("printf 'mode=%s\\n' \"${PROJECT_MODE-unset}\"")
        (project / ".binit-project.toml").write_text("label='changed'\n[env]\nPROJECT_MODE='changed'\n")
        terminal.command("cd project")
        assert "mode=unset" in terminal.command("printf 'mode=%s\\n' \"${PROJECT_MODE-unset}\"")
        assert "project:untrusted" in terminal.command(":")
        terminal.command('cd ../repo')
        os.write(terminal.fd, b'git switch feature/\t')
        terminal.read_until(b'git switch feature/binit ')
        os.write(terminal.fd, b'\x15\n')
        terminal.read_until(PROMPT)
        os.write(terminal.fd, b'git log --one\t')
        terminal.read_until(b'git log --oneline ')
        os.write(terminal.fd, b'\x15\n')
        terminal.read_until(PROMPT)
        transcript = terminal.transcript.decode(errors="replace")
    finally:
        terminal.close()
    recovery = Terminal(rcfile, directory, safe=True)
    try:
        assert "recovery=clean" in recovery.command("declare -F binit >/dev/null || printf 'recovery=clean\\n'")
    finally:
        recovery.close()

    source = directory / 'custom'
    shutil.copytree(ROOT / 'examples/plugins/workspace', source)
    (directory / 'Cargo.toml').write_text('[workspace]\n')
    terminal = Terminal(rcfile, directory, state=state)
    try:
        assert 'Added: workspace' in terminal.command(f"binit add '{source}'")
        assert 'Entry SHA256:' in terminal.command('binit inspect workspace')
    finally:
        terminal.close()
    terminal = Terminal(rcfile, directory, state=state)
    try:
        assert '[Rust]' in terminal.command(':')
        (source / 'plugin.bash').write_text("workspace_directory() { binit_segment workspace Updated; }\nbinit_on directory workspace_directory\n")
        assert '[Rust]' in terminal.command(':')
        assert 'Updated: workspace' in terminal.command('binit update workspace')
    finally:
        terminal.close()
    terminal = Terminal(rcfile, directory, state=state)
    try:
        assert '[Updated]' in terminal.command(':')
        assert 'Disabled: workspace' in terminal.command('binit disable workspace')
    finally:
        terminal.close()
    terminal = Terminal(rcfile, directory, state=state)
    try:
        assert '[Updated]' not in terminal.command(':')
        assert 'Enabled: workspace' in terminal.command('binit enable workspace')
    finally:
        terminal.close()
    # Exercise the actual demo entrypoint with isolated state; it must not reset it.
    terminal = Terminal(rcfile, directory, state=state, demo=True)
    try:
        assert '[Updated]' in terminal.command(':')
        assert 'workspace' in terminal.command('binit plugins')
        assert 'Removed: workspace' in terminal.command('binit remove workspace')
    finally:
        terminal.close()
    terminal = Terminal(rcfile, directory, state=state)
    try:
        assert '[Updated]' not in terminal.command(':')
        assert 'workspace' not in terminal.command('binit plugins')
    finally:
        terminal.close()
    return {"result": "PASS", "checks": ["real TAB completion", "script argument and status forwarding", "last exit status", "untrusted profile",
            "trust current directory", "literal project data", "environment restoration",
            "changed profile rejection", "recovery shell", "native branch completion", "native option completion",
            "plugin install and inspection", "explicit source update", "plugin disable and enable",
            "demo registry persistence", "plugin removal"], "transcript": transcript}


def benchmark(directory):
    state, baseline, configured = fixture(directory)
    result = {"samples": 20, "units": "milliseconds", "measurement": "PTY readiness, includes manager verification for Binit", "scenarios": {}}
    scenarios = [("clean_bash", baseline, None), ("framework_three_plugins", configured, state)]
    samples = {name: {"startup": [], "first_command_roundtrip": []} for name, _, _ in scenarios}
    execution = {
        'builtin_loop': 'for ((_binit_bench_i=0; _binit_bench_i<10000; _binit_bench_i++)); do :; done',
        'parameter_expansion': "_binit_bench_path=/a/b/c/file.txt; for ((_binit_bench_i=0; _binit_bench_i<5000; _binit_bench_i++)); do _binit_bench_value=${_binit_bench_path##*/}; done",
        'external_commands': 'for ((_binit_bench_i=0; _binit_bench_i<20; _binit_bench_i++)); do /usr/bin/true; done',
        'command_substitution': 'for ((_binit_bench_i=0; _binit_bench_i<20; _binit_bench_i++)); do _binit_bench_value=$(printf x); done',
    }
    for timings in samples.values():
        timings.update({kind: [] for kind in execution})
    # Alternate order within each pair to reduce cache and scheduler bias.
    for index in range(result["samples"]):
        for name, rcfile, shell_state in scenarios[::1 if index % 2 == 0 else -1]:
            terminal = Terminal(rcfile, directory, state=shell_state)
            try:
                samples[name]["startup"].append(terminal.startup_ms)
                started = time.perf_counter_ns()
                terminal.command(":")
                samples[name]["first_command_roundtrip"].append((time.perf_counter_ns() - started) / 1e6)
                for kind, code in execution.items():
                    started = time.perf_counter_ns()
                    terminal.command(code)
                    samples[name][kind].append((time.perf_counter_ns() - started) / 1e6)
            finally:
                terminal.close()
    def summarize(values):
        return {"median": round(statistics.median(values), 3),
                "p95": round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 3)}
    for name, timings in samples.items():
        result["scenarios"][name] = {kind: summarize(values) for kind, values in timings.items()}
    result["paired_overhead"] = {
        kind: summarize([framework - clean for framework, clean in zip(
            samples["framework_three_plugins"][kind], samples["clean_bash"][kind])])
        for kind in samples["clean_bash"]
    }
    result['execution_cases'] = execution
    return result


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    scratch = ROOT / f".scratch-{mode}"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as directory:
        result = smoke(Path(directory)) if mode == "smoke" else benchmark(Path(directory))
    destination = ROOT / "evidence"
    destination.mkdir(exist_ok=True)
    (destination / f"{mode}.json").write_text(json.dumps(result, indent=2) + "\n")
    if mode == "smoke":
        print(f"PTY smoke: PASS ({len(result['checks'])} checks)")
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
