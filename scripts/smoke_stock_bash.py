#!/usr/bin/env python3
"""Exercise a Binit binary against a cached distribution image's stock Bash."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from bench_corpus_terminal import CheckedShell
from bench_interactive import at_empty_prompt, digest


ROOT = Path(__file__).resolve().parents[1]
DOCKER = ["docker", "--context", "default"]
ENTRY = """set -euo pipefail
export BINIT_COMPLETION_MENU=$2 BINIT_BASH=/bin/bash
export HOME=/tmp/test-home HISTFILE=/dev/null INPUTRC=/tmp/inputrc
export TERM=xterm-256color LC_ALL=C.UTF-8
mkdir -m 700 -- "$HOME"
printf 'set editing-mode %s\\n' "$1" > "$INPUTRC"
/bin/bash --version | sed -n '1p'
getconf GNU_LIBC_VERSION
/binit prepare /fixture/config.toml --state /tmp/state
exec /binit shell --state /tmp/state
"""


def fixtures(directory):
    directory.mkdir()
    (directory / "entry.bash").write_text(ENTRY)
    config = "schema=1\n"
    for name, load, body in (
        ("prompt", "startup", "PS1='BENCH> '\n"),
        ("deferred", "wait", """sleep .15
stockcmd() { printf '<WAIT:%s>\\n' "$1"; }
complete -W omega stockcmd
"""),
    ):
        plugin = directory / name
        plugin.mkdir()
        (plugin / "plugin.toml").write_text(f'schema=1\nid="{name}"\nentry="plugin.bash"\nload="{load}"\n')
        (plugin / "plugin.bash").write_text(body)
        config += f'\n[[plugins]]\npath="{name}"\n'
    (directory / "config.toml").write_text(config)


def check(shell, command, marker):
    offset = len(shell.raw)
    shell.command(command)
    if marker not in shell.raw[offset:]:
        raise RuntimeError(f"missing {marker.decode()} after {command}")


def sample(image, binary, work, mode, native):
    name = f"{mode}-{'native' if native else 'fallback'}"
    # The generated shared object needs an executable mapping on the private mount.
    command = [*DOCKER, "run", "--rm", "--pull", "never", "--interactive", "--tty",
               "--network", "none", "--read-only", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
               "--tmpfs", "/tmp:rw,exec,nosuid,nodev,mode=1777,size=32m",
               "--mount", f"type=bind,source={binary},target=/binit,readonly",
               "--mount", f"type=bind,source={work / 'fixture'},target=/fixture,readonly",
               image, "/bin/bash", "/fixture/entry.bash", mode, "1" if native else "0"]
    shell = CheckedShell("/bin/bash", None, work, os.environ.copy(), command,
                         receipt_path=work / f"{name}.log")
    try:
        bash = re.search(rb"GNU bash, version [^\r\n]+", shell.raw)
        glibc = re.search(rb"glibc [0-9.]+", shell.raw)
        if not bash or not glibc:
            raise RuntimeError("missing stock Bash or glibc identity")
        check(shell, "stockcmd queued", b"<WAIT:queued>")
        shell.settled(b"stockcmd om\t")
        shell.until(lambda: "BENCH> stockcmd omega" in shell.screen.display[shell.screen.cursor.y])
        check(shell, "", b"<WAIT:omega>")
        check(shell, """printf '<MODULE:%s>\\n' "$(type -t binit_complete || :)" """,
              b"<MODULE:builtin>" if native else b"<MODULE:>")
        check(shell, """bind -v | grep '^set editing-mode '""", f"set editing-mode {mode}".encode())
    finally:
        shell.close(ready=at_empty_prompt(shell.screen))
    return {"bash": bash[0].decode(), "glibc": glibc[0].decode()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    work, binary = args.work.resolve(), args.binary.resolve()
    if work.exists() or not work.is_relative_to(ROOT / "tmp") or not binary.is_file():
        parser.error("use a readable Binit binary and a fresh work directory under tmp/")
    image = subprocess.check_output([*DOCKER, "image", "inspect", args.image, "--format", "{{.Id}}"],
                                    timeout=10, text=True).strip()
    work.mkdir(parents=True)
    fixtures(work / "fixture")
    receipt = {"image": image, "binit_sha256": digest(binary), "cases": []}
    for mode in ("emacs", "vi"):
        for native in (True, False):
            case = {"mode": mode, "native": native, "passed": False}
            try:
                case.update(sample(image, binary, work, mode, native))
                case["passed"] = True
            except (OSError, RuntimeError) as error:
                case["error"] = str(error)
            receipt["cases"].append(case)
            print(json.dumps(case), flush=True)
    (work / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return int(not all(case["passed"] for case in receipt["cases"]))


if __name__ == "__main__":
    sys.exit(main())
