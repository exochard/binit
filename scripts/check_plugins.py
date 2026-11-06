#!/usr/bin/env python3
"""Check existing Bash plugins through user-owned declarations in a real PTY."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import tempfile
from unittest.mock import patch

from terminal import ROOT, Terminal, cli


def check(directory, name, root, files, load, commands, body):
    declaration = directory / f"{name}.toml"
    declaration.write_text(
        f"schema=1\nid={json.dumps(name)}\npath={json.dumps(str(root))}\n"
        f"files={json.dumps(files)}\nload={json.dumps(load)}\ncommands={json.dumps(commands)}\n"
    )
    state = directory / f"state-{name}"
    cli(state, "add", declaration)
    generation = state / "generations" / (state / "current").read_text()
    rc = directory / "rc"
    rc.write_text("PS1='READY> '\nPROMPT_COMMAND=()\n" + cli(state, "init") + "\n")
    shell = Terminal(rc, directory, term="xterm-256color")
    try:
        result = shell.command(body + "; printf '\\ncheck-status=%s\\n' \"$?\"")
        if "check-status=0\r\n" not in result:
            raise AssertionError(f"{name}: {result}")
    finally:
        shell.close()
    return {
        "plugin": name,
        "load": load,
        "files": {file: hashlib.sha256((generation / "plugins" / name / "files" / file).read_bytes()).hexdigest()
                  for file in files},
        "result": "pass",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fzf", type=Path, help="directory containing fzf's key-bindings.bash and completion.bash")
    parser.add_argument("--bash-completion", type=Path, help="bash_completion script")
    parser.add_argument("--nvm", type=Path, help="reviewed, pinned nvm.sh; no Node installation is performed")
    args = parser.parse_args()
    if not any((args.fzf, args.bash_completion, args.nvm)):
        parser.error("select at least one plugin")
    scratch = ROOT / ".scratch-plugin-check"
    scratch.mkdir(exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(dir=scratch) as temporary:
        directory = Path(temporary)
        home = directory / "home"
        home.mkdir()
        nvm = directory / "node"
        nvm.mkdir()
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("NVM_", "FZF_", "BASH_COMPLETION_"))}
        environment.update(HOME=str(home), HISTFILE="/dev/null", INPUTRC="/dev/null", NVM_DIR=str(nvm),
                           PS1="READY> ", PROMPT_COMMAND="", LC_ALL="C.UTF-8")
        # The terminal helper passes only this private environment to child Bash.
        with patch.dict(os.environ, environment, clear=True):
            if args.fzf:
                results.append(check(directory, "fzf", args.fzf.resolve(),
                                     ["completion.bash", "key-bindings.bash"], "command", ["fzf"],
                                     "[[ ${__binit_state[fzf]} == pending ]] && fzf --version && "
                                     "[[ ${__binit_state[fzf]} == loaded ]] && declare -F fzf-file-widget >/dev/null"))
            if args.bash_completion:
                source = args.bash_completion.resolve()
                results.append(check(directory, "bash-completion", source.parent, [source.name], "startup", [],
                                     "[[ ${__binit_state[bash-completion]} == loaded ]] && "
                                     "declare -F _init_completion >/dev/null && "
                                     "[[ ${BASH_COMPLETION_VERSINFO[0]} -ge 2 ]]"))
            if args.nvm:
                source = args.nvm.resolve()
                results.append(check(directory, "nvm", source.parent, [source.name], "command", ["nvm"],
                                     "[[ ${__binit_state[nvm]} == pending ]] && nvm --version && "
                                     "[[ ${__binit_state[nvm]} == loaded && $NVM_DIR == " + shlex.quote(str(nvm)) + " ]]"))
        if any(nvm.iterdir()):
            raise AssertionError("NVM check changed the empty Node directory")
    destination = ROOT / "evidence/plugin-compatibility.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps({"checks": results}, indent=2) + "\n")
    print(json.dumps({"checks": results}, indent=2))


if __name__ == "__main__":
    main()
