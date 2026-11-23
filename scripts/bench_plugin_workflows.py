#!/usr/bin/env python3
"""Time real Bash plugin workflows and retain the outcomes for each shell."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time

from bench_corpus_terminal import CheckedShell
from bench_ecosystem import SPEC, environment, inventory, prompt_hook, verify_sources
from bench_interactive import PROMPT, at_empty_prompt, digest, summarize


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_LOCK = ROOT / "benchmarks/plugin-corpus.json"
SUBJECTS = ("bash", "binit", "binit-lazy", "bash-it", "oh-my-bash")
PLUGINS = ("system-completion", "git-completion", "fuzzy-history", "nvm")
HISTORY_LINE = "printf 'workflow-history-target\\n'"
FZF_PROMPT = "Workflow history> "
METRICS = ("visible_prompt", "usable_startup", "startup_effects", "git_tab",
           "fzf_open", "fzf_query", "fzf_select", "nvm_first", "nvm_warm")
EFFECTS = ("startup-registrations", "git-tab", "fzf-history", "nvm-first", "nvm-warm")
SGR = re.compile(rb"\x1b\[[0-9;:]*m")
ONE_FZF_MATCH = re.compile(rb"(?<![0-9])1/[0-9]+ \([0-9]+\)")


def elapsed(started):
    return (time.perf_counter_ns() - started) / 1e6


def stage_plugins(args, lock):
    root = args.work / "packages"
    records = {}
    for name in PLUGINS:
        directory = root / name
        directory.mkdir(parents=True)
    fzf = lock["plugins"]["fzf"]
    nvm = lock["plugins"]["nvm"]
    inputs = {
        args.bash_completion: (PLUGINS[0], "bash_completion", None),
        args.git_completion: (PLUGINS[1], "git.bash", None),
        args.plugin_sources / "fzf-completion.bash": (PLUGINS[2], "completion.bash",
                                                      fzf["shell_files_sha256"]["shell/completion.bash"]),
        args.plugin_sources / "fzf-key-bindings.bash": (PLUGINS[2], "key-bindings.bash",
                                                        fzf["shell_files_sha256"]["shell/key-bindings.bash"]),
        args.plugin_sources / "nvm.sh": (PLUGINS[3], "nvm.sh",
                                        nvm["versions"][nvm["compared_version"]]["nvm_sh_sha256"]),
    }
    for source, (plugin, filename, expected) in inputs.items():
        actual = digest(source)
        if expected and actual != expected:
            raise RuntimeError(f"{source}: differs from the frozen plugin input")
        destination = root / plugin / filename
        shutil.copyfile(source, destination)
        if digest(destination) != actual:
            raise RuntimeError(f"{source}: changed while staging")
        with (destination.parent / "entry.bash").open("a") as entry:
            entry.write(f'source "${{BASH_SOURCE[0]%/*}}/{filename}"\n')
        records[str(source)] = actual
    for name in PLUGINS:
        directory = root / name
        (directory / "plugin.toml").write_text(
            f'schema=1\nid="{name}"\nentry="entry.bash"\nload="startup"\n')
    archive = args.plugin_sources / fzf["binary_archive"]
    records[str(archive)] = digest(archive)
    if records[str(archive)] != fzf["binary_archive_sha256"]:
        raise RuntimeError("fzf archive differs from the frozen plugin input")
    bin_dir = args.work / "bin"
    bin_dir.mkdir()
    with tarfile.open(archive) as bundle, (bin_dir / "fzf").open("wb") as target:
        member = bundle.getmember("fzf")
        if not member.isfile() or member.size > 16 * 1024 * 1024:
            raise RuntimeError("invalid fzf executable in archive")
        with bundle.extractfile(member) as source:
            shutil.copyfileobj(source, target)
    (bin_dir / "fzf").chmod(0o700)
    lazy = root / "nvm-lazy"
    shutil.copytree(root / PLUGINS[3], lazy)
    manifest = lazy / "plugin.toml"
    manifest.write_text(manifest.read_text().replace('load="startup"', 'load="command"\ncommands=["nvm"]'))
    return records


def prepare(args, subject, directory, env):
    home = Path(env["HOME"])
    entries = [args.work / "packages" / name for name in PLUGINS]
    setup = 'PS1=""\nset -o emacs\n'
    if subject == "bash":
        setup += "".join(f"source {shlex.quote(str(entry / 'entry.bash'))}\n" for entry in entries)
    elif subject.startswith("binit"):
        if subject == "binit-lazy":
            entries[-1] = entries[-1].with_name("nvm-lazy")
        config = directory / "config.toml"
        config.write_text("schema=1\n" + "".join(
            f"\n[[plugins]]\npath={json.dumps(str(entry), ensure_ascii=False)}\n" for entry in entries))
        state = home / "state"
        preparation = subprocess.run([args.binary, "prepare", config, "--state", state],
                                     env=env, capture_output=True, timeout=30)
        (directory / "prepare.stdout").write_bytes(preparation.stdout)
        (directory / "prepare.stderr").write_bytes(preparation.stderr)
        if preparation.returncode:
            raise RuntimeError("Binit generation preparation failed")
        setup += f'eval "$({shlex.quote(str(args.binary))} init --state {shlex.quote(str(state))})"\n'
    else:
        upstream = shlex.quote(str(args.sources / "sources" / subject))
        custom = directory / "custom"
        for index, entry in enumerate(entries):
            name = f"workflow{index}"
            wrapper = (custom / f"{name}.bash" if subject == "bash-it" else
                       custom / "plugins" / name / f"{name}.plugin.sh")
            wrapper.parent.mkdir(parents=True, exist_ok=True)
            wrapper.write_text(f"source {shlex.quote(str(entry / 'entry.bash'))}\n")
        custom_path = shlex.quote(str(custom))
        if subject == "bash-it":
            setup += (f'BASH_IT={upstream}\nBASH_IT_CUSTOM={custom_path}\nBASH_IT_THEME=""\n'
                      'source "$BASH_IT/bash_it.sh"\n')
        else:
            setup += (f'OSH={upstream}\nOSH_CUSTOM={custom_path}\nOSH_THEME=""\n'
                      'DISABLE_AUTO_UPDATE=true\naliases=()\ncompletions=()\n'
                      'plugins=(workflow0 workflow1 workflow2 workflow3)\nsource "$OSH/oh-my-bash.sh"\n')
    rc = directory / "rc"
    rc.write_text(setup + 'bind "set show-all-if-ambiguous on"\n' + prompt_hook(False))
    return rc


def visible(shell, value):
    return any(value in row for row in shell.screen.display)


def input_to_effect(shell, keys, observed):
    started = time.perf_counter_ns()
    os.write(shell.fd, keys)
    shell.until(observed, seconds=5)
    return elapsed(started)


def accepted(shell, command, marker):
    """Require fresh command output followed by the next prompt."""
    offset = len(shell.raw)
    return input_to_effect(shell, command, lambda: (
        marker in shell.raw[offset:]
        and shell.raw[offset:].rfind(PROMPT) > shell.raw[offset:].find(marker)
        and at_empty_prompt(shell.screen)
    ))


def nvm_state(shell, expected):
    offset = len(shell.raw)
    shell.command("binit profile")
    if not re.search(rb"(?:\r|\n)nvm +"
                     + expected.encode() + rb" +", shell.raw[offset:]):
        raise RuntimeError(f"expected Binit to report NVM {expected}")


def fzf_query_ready(output, query):
    clean = SGR.sub(b"", output)
    return (FZF_PROMPT.encode() + query in clean
            and ONE_FZF_MATCH.search(clean) is not None
            and HISTORY_LINE.encode() in clean)


def exercise(shell, result, nvm_version, started):
    timings, effects = result["timings_ms"], result["effects"]
    accepted(shell, b"printf '\\036ready\\036'\n", b"\x1eready\x1e")
    timings["usable_startup"] = elapsed(started)
    accepted(
        shell, (b'[[ ${BASH_COMPLETION_VERSINFO[0]-0} -ge 2 ]] && complete -p git >/dev/null && '
                b'declare -F fzf-file-widget __fzf_history__ >/dev/null && '
                b'[[ $(type -t nvm) == function ]] && printf "\\036effects\\036"\n'),
        b"\x1eeffects\x1e")
    timings["startup_effects"] = elapsed(started)
    effects.append("startup-registrations")
    if result["subject"].startswith("binit"):
        nvm_state(shell, "pending" if result["subject"] == "binit-lazy" else "loaded")
    input_to_effect(shell, b"git ch", lambda: visible(shell, "BENCH> git ch"))
    timings["git_tab"] = input_to_effect(shell, b"\t\t", lambda: (
        visible(shell, "checkout") and visible(shell, "cherry-pick")))
    effects.append("git-tab")
    shell.until(lambda: "BENCH> git " in shell.screen.display[shell.screen.cursor.y])
    input_to_effect(shell, b"\x15", lambda: at_empty_prompt(shell.screen))
    timings["fzf_open"] = input_to_effect(shell, b"\x12", lambda: visible(shell, FZF_PROMPT.rstrip()))
    offset = len(shell.raw)
    query = b"workflow-history-target"
    # The query can be painted before fzf finishes matching. Wait for the
    # matching result before sending Enter.
    timings["fzf_query"] = input_to_effect(shell, query, lambda: fzf_query_ready(shell.raw[offset:], query))
    timings["fzf_select"] = input_to_effect(shell, b"\r", lambda: (
        shell.screen.display[shell.screen.cursor.y].rstrip().endswith("BENCH> " + HISTORY_LINE)))
    accepted(shell, b"\n", b"workflow-history-target\r\n")
    effects.append("fzf-history")
    command = b"nvm --version\n"
    version_output = b"\r" + nvm_version.encode() + b"\r\n"
    timings["nvm_first"] = accepted(shell, command, version_output)
    effects.append("nvm-first")
    timings["nvm_warm"] = accepted(shell, command, version_output)
    effects.append("nvm-warm")
    if result["subject"].startswith("binit"):
        nvm_state(shell, "loaded")


def sample(args, subject, run, pair, nvm_version):
    directory = args.work / f"{run}-{pair}-{subject}"
    home = directory / "home"
    home.mkdir(parents=True, mode=0o700)
    (home / "nvm").mkdir()
    history = home / "history"
    history.write_text("printf 'another entry\\n'\n" + HISTORY_LINE + "\n")
    env = {
        **environment(home), "BINIT_BASH": str(args.bash), "BINIT_COMPLETION_MENU": "0",
        "NVM_DIR": str(home / "nvm"), "HISTFILE": str(history), "HISTSIZE": "1000",
        "PATH": f"{args.work / 'bin'}:/usr/bin:/bin",
        "FZF_CTRL_R_OPTS": f"--height=12 --prompt={shlex.quote(FZF_PROMPT)}",
    }
    result = {"run": run, "pair": pair, "subject": subject, "passed": False, "timings_ms": {}, "effects": []}
    try:
        rc = prepare(args, subject, directory, env)
        start = time.perf_counter_ns()
        shell = CheckedShell(str(args.bash), rc, home, env, receipt_path=directory / "terminal.log")
        try:
            result["timings_ms"]["visible_prompt"] = shell.startup_ms
            exercise(shell, result, nvm_version, start)
        finally:
            shell.close(ready=at_empty_prompt(shell.screen))
        result["passed"] = True
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
    return result


def results_by_run(rows, runs, pairs):
    result = []
    for run in range(runs):
        for subject in SUBJECTS:
            group = [row for row in rows if row["run"] == run and row["subject"] == subject]
            passed = sum(row["passed"] for row in group)
            summary = {
                "run": run, "subject": subject, "passed": passed, "expected": pairs,
                "effect_counts": {effect: sum(effect in row["effects"] for row in group) for effect in EFFECTS},
            }
            if passed == len(group) == pairs:
                summary["timings_ms"] = {metric: summarize([row["timings_ms"][metric] for row in group])
                                         for metric in METRICS}
            result.append(summary)
    return result


def save(path, receipt):
    pending = path.with_name(path.name + ".pending")
    with pending.open("x") as output:
        output.write(json.dumps(receipt, indent=2) + "\n")
    pending.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("sources", "plugin-sources", "binary", "work", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--bash", type=Path, default=Path("/bin/bash"))
    parser.add_argument("--bash-completion", type=Path, default=Path("/usr/share/bash-completion/bash_completion"))
    parser.add_argument("--git-completion", type=Path, default=Path("/usr/share/bash-completion/completions/git"))
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--pairs", type=int, default=20)
    args = parser.parse_args()
    for name, value in vars(args).items():
        if isinstance(value, Path):
            setattr(args, name, value.resolve())
    if (min(args.runs, args.pairs) < 1 or args.work.exists() or args.output.exists()
            or not all(path.is_relative_to(ROOT / "tmp") for path in (args.work, args.output))
            or args.output.is_relative_to(args.work)):
        parser.error("use positive counts and fresh work and receipt paths under tmp/")
    args.work.mkdir(parents=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema": 1, "profile": "real-plugins-readline", "runs": args.runs, "pairs": args.pairs,
        "host": {key: getattr(os.uname(), key) for key in ("sysname", "release", "machine")},
        "start_load": os.getloadavg(),
        "complete_profile": False, "rows": [],
    }
    rows = receipt["rows"]
    with args.output.open("x") as output:
        output.write(json.dumps(receipt, indent=2) + "\n")
    try:
        spec = json.loads(SPEC.read_text())
        spec["sources"] = {name: spec["sources"][name] for name in ("bash-it", "oh-my-bash")}
        lock = json.loads(PLUGIN_LOCK.read_text())
        receipt["sources"] = verify_sources(args.sources, spec)
        files = stage_plugins(args, lock)
        for path in (args.binary, args.bash, Path(sys.executable), Path(__file__), SPEC, PLUGIN_LOCK,
                     ROOT / "scripts/bench_corpus_terminal.py", ROOT / "scripts/bench_ecosystem.py",
                     ROOT / "scripts/bench_interactive.py"):
            files[str(path.resolve())] = digest(path)
        receipt["identities"] = files
        staged = receipt["staged"] = inventory(args.work)
        receipt["bash_version"] = subprocess.check_output([args.bash, "--version"], text=True).splitlines()[0]
        version = lock["plugins"]["nvm"]["compared_version"]
        for run in range(args.runs):
            for pair in range(args.pairs):
                for subject in SUBJECTS[::1 if (run + pair) % 2 == 0 else -1]:
                    rows.append(sample(args, subject, run, pair, version))
                save(args.output, receipt)
                print(f"run {run + 1} pair {pair + 1}/{args.pairs}", flush=True)
        if ({name: digest(name) for name in files} != files or verify_sources(args.sources, spec) != receipt["sources"]
                or any(digest(args.work / name) != record["sha256"] for name, record in staged.items())):
            raise RuntimeError("benchmark inputs changed during measurement")
        receipt["summary"] = results_by_run(rows, args.runs, args.pairs)
        receipt["effects_passed"] = all(row["passed"] for row in rows)
        receipt["complete_profile"] = receipt["effects_passed"] and args.runs == 2 and args.pairs == 20
    except BaseException as error:
        receipt["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        receipt["end_load"] = os.getloadavg()
        if not args.output.with_name(args.output.name + ".pending").exists():
            save(args.output, receipt)
    return int(not receipt["effects_passed"])


if __name__ == "__main__":
    sys.exit(main())
