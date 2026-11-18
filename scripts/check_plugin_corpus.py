#!/usr/bin/env python3
"""Run the frozen cross-framework plugin corpus inside its pinned image (benchmarks/plugin-corpus.json).

Each framework and plugin gets a private HOME. A setup shell installs the plugin through the framework's
documented mechanism; a fresh interactive shell under a real PTY then checks the plugin's declared effect.
Binit's update, failed update and rollback sequence follows. The receipt records identities and every
transcript, and applies the frozen passing-set rule. Not release evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import shutil
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
SOURCES = Path("/opt/corpus")
STATUS = re.compile(rb"__CORPUS_STATUS:(\d+)__")

EFFECTS = {
    "git-completion": r'''
spec=$(complete -p git 2>/dev/null) || {
  loader=$(complete -p -D 2>/dev/null) && loader=${loader#*-F } && loader=${loader%% *}
  [[ -n $loader ]] && "$loader" git ch git
  spec=$(complete -p git 2>/dev/null)
} && fn=${spec#*-F } && fn=${fn%% *} &&
COMP_WORDS=(git ch) COMP_CWORD=1 COMP_LINE='git ch' COMP_POINT=6 && COMPREPLY=() &&
"$fn" git ch git; words=" ${COMPREPLY[*]} "; printf 'candidates:%s\n' "$words";
[[ $words == *" checkout "* && $words == *" cherry-pick "* ]]''',
    "bash-completion": r'''
[[ ${BASH_COMPLETION_VERSINFO[0]-0} -ge 2 ]] &&
{ declare -F _comp_initialize >/dev/null || declare -F _init_completion >/dev/null; } &&
loader=$(complete -p -D 2>/dev/null) && loader=${loader#*-F } && loader=${loader%% *} && [[ -n $loader ]] &&
{ "$loader" make '' make; complete -p make; }''',
    "fzf": r'''
declare -F fzf-file-widget >/dev/null && declare -F __fzf_history__ >/dev/null &&
bind -X | grep -q fzf-file-widget && bind -X | grep -q __fzf_history__ &&
[[ $(printf 'alpha\nbeta\n' | fzf --filter beta) == beta ]]''',
    "nvm": r'''
[[ $(nvm --version) == "$CORPUS_NVM_VERSION" ]] &&
[[ -z $(find "$NVM_DIR" -mindepth 1 ! -name nvm.sh -print -quit) ]]''',
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Shell:
    """An interactive Bash on a PTY that reads only HOME/.bashrc; commands end with a printed status."""

    def __init__(self, home, extra_env=None):
        self.transcript = bytearray()
        env = {"HOME": str(home), "PATH": "/usr/local/bin:/usr/bin:/bin", "TERM": "xterm-256color",
               "LC_ALL": "C.UTF-8", "INPUTRC": "/dev/null", "HISTFILE": str(home / ".bash_history"),
               "USER": os.environ.get("USER", "corpus"), "SHELL": "/bin/bash", **(extra_env or {})}
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(home)
            os.execve("/bin/bash", ["/bin/bash", "--noprofile", "--rcfile", str(home / ".bashrc"), "-i"], env)
        self.run("true", 60)

    def run(self, command, seconds=60):
        start = len(self.transcript)
        os.write(self.fd, (command + "\nprintf '__CORPUS_%s__\\n' \"STATUS:$?\"\n").encode())
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([self.fd], [], [], .2)[0]:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                self.transcript.extend(chunk)
                match = STATUS.search(self.transcript, start)
                if match:
                    return int(match.group(1)), self.transcript[start:match.start()].decode(errors="replace")
        raise TimeoutError(f"no status within {seconds}s:\n{self.transcript[start:].decode(errors='replace')}")

    def close(self):
        try:
            os.write(self.fd, b"exit\n")
            for _ in range(50):
                if os.waitpid(self.pid, os.WNOHANG)[0]:
                    break
                time.sleep(.05)
            else:
                os.killpg(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
        except (OSError, ChildProcessError):
            pass
        os.close(self.fd)


def fresh_home(work, name):
    home = work / name
    home.mkdir(parents=True)
    os.chmod(home, 0o700)
    (home / ".nvm").mkdir()
    shutil.copyfile(SOURCES / "nvm-0.40.3" / "nvm.sh", home / ".nvm" / "nvm.sh")
    return home


NVM_LINE = 'export NVM_DIR="$HOME/.nvm"\n'


def install_binit(home, plugin):
    lines = {"git-completion": "binit plugin /usr/share/bash-completion/completions id=git-completion files=git",
             "bash-completion": "binit plugin /usr/share/bash-completion id=bash-completion files=bash_completion",
             "fzf": "binit plugin /opt/fzf/shell id=fzf files=completion.bash,key-bindings.bash",
             "nvm": "binit plugin $HOME/.nvm id=nvm files=nvm.sh load=command commands=nvm"}
    (home / ".bashrc").write_text(NVM_LINE + 'eval "$(binit init)"\n' + lines[plugin] + "\n")
    shell = Shell(home)
    try:
        return [shell.run("binit sync", 300)]
    finally:
        shell.close()


def install_bash_it(home, plugin):
    shutil.copytree(SOURCES / "bash-it", home / ".bash_it", symlinks=True)
    log = subprocess.run(["bash", str(home / ".bash_it/install.sh"), "--silent"], env={
        "HOME": str(home), "PATH": "/usr/bin:/bin", "TERM": "dumb"}, cwd=home, capture_output=True, text=True)
    steps = [(log.returncode, log.stdout + log.stderr)]
    if plugin == "fzf":
        (home / ".fzf.bash").write_text('eval "$(fzf --bash)"\n')
    text = (home / ".bashrc").read_text()
    (home / ".bashrc").write_text(NVM_LINE + text)
    command = {"git-completion": "bash-it enable completion git", "bash-completion": "bash-it enable completion system",
               "fzf": "bash-it enable plugin fzf", "nvm": "bash-it enable plugin nvm"}[plugin]
    shell = Shell(home)
    try:
        steps.append(shell.run(command, 120))
    finally:
        shell.close()
    return steps


def install_oh_my_bash(home, plugin):
    log = subprocess.run(["bash", str(SOURCES / "oh-my-bash/tools/install.sh"), "--unattended"], env={
        "HOME": str(home), "PATH": "/usr/bin:/bin", "TERM": "dumb", "OSH_REPOSITORY": str(SOURCES / "oh-my-bash")},
        cwd=home, capture_output=True, text=True)
    steps = [(log.returncode, log.stdout + log.stderr)]
    text = (home / ".bashrc").read_text()
    array, value = {"git-completion": ("completions", "git"), "bash-completion": ("completions", "system"),
                    "fzf": ("plugins", "fzf"), "nvm": ("plugins", "nvm")}[plugin]
    block = re.search(rf"^{array}=\(\n(.*?)^\)", text, re.S | re.M)
    if block is None:
        raise ValueError(f"template has no {array} array")
    if not re.search(rf"^\s*{value}\s*$", block.group(1), re.M):
        text = text[:block.end(1)] + f"  {value}\n" + text[block.end(1):]
    (home / ".bashrc").write_text(NVM_LINE + text)
    return steps


INSTALL = {"binit": install_binit, "bash-it": install_bash_it, "oh-my-bash": install_oh_my_bash}


def check(work, framework, plugin):
    home = fresh_home(work, f"{framework}-{plugin}")
    result = {"framework": framework, "plugin": plugin}
    try:
        result["setup"] = [{"status": s, "output": o[-4000:]} for s, o in INSTALL[framework](home, plugin)]
        shell = Shell(home, {"CORPUS_NVM_VERSION": "0.40.3"})
        try:
            status, output = shell.run(EFFECTS[plugin], 60)
        finally:
            shell.close()
        result.update(passed=status == 0, status=status, output=output[-4000:],
                      transcript_sha256=hashlib.sha256(bytes(shell.transcript)).hexdigest())
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as error:
        result.update(passed=False, error=f"{type(error).__name__}: {error}"[-4000:])
    return result


def lifecycle(work):
    home = fresh_home(work, "binit-lifecycle")
    for version in ("0.40.2", "0.40.3"):
        (home / f"src/nvm-{version}").mkdir(parents=True)
        shutil.copyfile(SOURCES / f"nvm-{version}" / "nvm.sh", home / f"src/nvm-{version}/nvm.sh")
    (home / "src/nvm-missing").mkdir()
    state = home / ".local/share/binit"
    steps = []

    def rc(source):
        (home / ".bashrc").write_text(NVM_LINE + 'eval "$(binit init)"\n'
                                      f"binit plugin $HOME/src/{source} id=nvm files=nvm.sh load=command commands=nvm\n"
                                      "corpus_user() { :; }\n")

    def current():
        return (state / "current").read_text().strip()

    def sync_step(name, source, expect_success):
        rc(source)
        shell = Shell(home)
        try:
            status, output = shell.run("binit sync", 300)
            kept, _ = shell.run("declare -F corpus_user >/dev/null", 10)
        finally:
            shell.close()
        steps.append({"step": name, "sync_status": status, "user_definition_kept": kept == 0,
                      "generation": current(), "output": output[-2000:],
                      "passed": (status == 0) == expect_success and kept == 0})

    def fresh(name, version):
        shell = Shell(home, {"CORPUS_NVM_VERSION": version})
        try:
            status, output = shell.run(EFFECTS["nvm"] + " && declare -F corpus_user >/dev/null", 60)
        finally:
            shell.close()
        steps.append({"step": name, "status": status, "generation": current(), "output": output[-2000:],
                      "passed": status == 0})

    sync_step("initial", "nvm-0.40.2", True)
    first = current()
    fresh("initial-fresh-shell", "0.40.2")
    sync_step("pinned-update", "nvm-0.40.3", True)
    second = current()
    steps[-1]["passed"] &= second != first
    fresh("updated-fresh-shell", "0.40.3")
    sync_step("failed-update", "nvm-missing", False)
    steps[-1]["passed"] &= current() == second
    rc("nvm-0.40.3")
    fresh("after-failed-update-fresh-shell", "0.40.3")
    rollback = subprocess.run(["binit", "rollback", "--state", str(state)], capture_output=True, text=True,
                              env={"HOME": str(home), "PATH": "/usr/local/bin:/usr/bin:/bin"})
    steps.append({"step": "rollback", "status": rollback.returncode, "generation": current(),
                  "output": (rollback.stdout + rollback.stderr)[-2000:],
                  "passed": rollback.returncode == 0 and current() == first})
    rc("nvm-0.40.2")
    fresh("rolled-back-fresh-shell", "0.40.2")
    return {"generations": {"first": first, "second": second}, "steps": steps,
            "passed": all(step["passed"] for step in steps)}


def verdict(results, frameworks):
    passing = {f: sorted(r["plugin"] for r in results if r["framework"] == f and r["passed"]) for f in frameworks}
    binit = set(passing["binit"])
    others = [set(passing[f]) for f in frameworks if f != "binit"]
    if any(other - binit for other in others):
        outcome = "loss"
    elif all(other == binit for other in others):
        outcome = "tie"
    elif all(other < binit for other in others):
        outcome = "win"
    else:
        outcome = "inconclusive"
    return {"passing_sets": passing, "outcome": outcome}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest_path = ROOT / "benchmarks/plugin-corpus.json"
    manifest = json.loads(manifest_path.read_text())
    args.work.mkdir(parents=True)
    frameworks = list(manifest["frameworks"])
    results = [check(args.work, framework, plugin) for framework in frameworks for plugin in manifest["plugins"]]
    identities = {
        "manifest_sha256": digest(manifest_path), "runner_sha256": digest(__file__),
        "bash": subprocess.check_output(["/bin/bash", "--version"], text=True).splitlines()[0],
        "bash_sha256": digest("/bin/bash"), "binit_sha256": digest(shutil.which("binit")),
        "binit_commit": (SOURCES / "binit-commit").read_text().strip(),
        "inputs": json.loads((SOURCES / "inputs.json").read_text()),
        "packages": subprocess.check_output(["dpkg-query", "-W", "bash", "bash-completion", "git", "ca-certificates"], text=True).split("\n"),
        "loaded_files": {path: digest(path) for path in ("/usr/share/bash-completion/completions/git",
                                                         "/usr/share/bash-completion/bash_completion",
                                                         "/opt/fzf/shell/completion.bash", "/opt/fzf/shell/key-bindings.bash",
                                                         "/usr/local/bin/fzf")},
    }
    receipt = {"identities": identities, "results": results, "rule": verdict(results, frameworks),
               "binit_lifecycle": lifecycle(args.work), "release_evidence": False}
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"rule": receipt["rule"], "lifecycle_passed": receipt["binit_lifecycle"]["passed"]}, indent=2))


if __name__ == "__main__":
    main()
