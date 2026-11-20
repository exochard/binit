#!/usr/bin/env python3
"""Measure one plugin against its pinned baseline through the interactive PTY."""

import argparse
from itertools import permutations
import json
import math
import os
from pathlib import Path
import random
import shlex
import statistics
import subprocess
import tempfile
import time

from bench_interactive import PLUGINS, PROMPT, Shell, digest, profile


def prepare(directory, binary, source, home, mode, environment, project=None, long_line=False):
    directory.mkdir()
    state = directory / "state"
    config = directory / "plugins.toml"
    config.write_text("schema=1\n[[plugins]]\npath=" + json.dumps(str(source)) + "\n")
    subprocess.run(
        [binary, "prepare", config, "--state", state],
        env=environment, check=True, stdout=subprocess.DEVNULL,
    )
    if project is not None:
        subprocess.run(
            [binary, "trust", project, "--state", state],
            env=environment, check=True, stdout=subprocess.DEVNULL,
        )
    rc = directory / "rc"
    init = shlex.join([str(binary), "init", "--state", str(state)])
    paste = "bind 'set enable-bracketed-paste on'\n" if long_line else ""
    rc.write_text(
        f"set -o {mode}\nPS1='BENCH> '\nHISTSIZE=1000\nHISTFILE=/dev/null\n"
        f"history -r {shlex.quote(str(home / 'history'))}\n"
        f'{paste}eval "$({init})"\n'
    )
    return rc


def percentile(values, fraction):
    return sorted(values)[math.ceil(len(values) * fraction) - 1]


def flatten_typing(rows, field="typing"):
    return [duration for row in rows for duration in row[field]]


def summary(rows):
    result = {
        "startup_visible_median": statistics.median(row["startup_visible"] for row in rows),
        "startup_usable_median": statistics.median(row["startup_usable"] for row in rows),
        "typing_p99": percentile(flatten_typing(rows), .99),
        "typing_samples": len(flatten_typing(rows)),
    }
    if rows[0]["long_typing"]:
        samples = flatten_typing(rows, "long_typing")
        result.update(long_typing_p99=percentile(samples, .99), long_typing_samples=len(samples))
    return result


def differences(a, b):
    result = {
        "startup_visible_median": statistics.median(
            after["startup_visible"] - before["startup_visible"]
            for before, after in zip(a, b, strict=True)
        ),
        "startup_usable_median": statistics.median(
            after["startup_usable"] - before["startup_usable"]
            for before, after in zip(a, b, strict=True)
        ),
        "typing_p99": percentile(flatten_typing(b), .99) - percentile(flatten_typing(a), .99),
    }
    if a[0]["long_typing"]:
        result["long_typing_p99"] = (percentile(flatten_typing(b, "long_typing"), .99)
                                     - percentile(flatten_typing(a, "long_typing"), .99))
    return result


def paired(a, b):
    rng = random.Random(0)
    simulations = []
    for _ in range(1000):
        indices = rng.choices(range(len(a)), k=len(a))
        simulations.append(differences([a[i] for i in indices], [b[i] for i in indices]))
    return {
        key: {
            "delta": value,
            "interval_95": [
                percentile([sample[key] for sample in simulations], fraction)
                for fraction in (.025, .975)
            ],
        }
        for key, value in differences(a, b).items()
    }


def measure(terminal, identifier, expected, long_line):
    def clear_line():
        terminal.cancel()
        terminal.until(lambda: terminal.screen.cursor.x == len(PROMPT) and
                       terminal.screen.display[terminal.screen.cursor.y].rstrip() == PROMPT.decode().rstrip())

    startup = terminal.startup_ms
    started = time.perf_counter_ns()
    os.write(terminal.fd, b":")
    marker = PROMPT.decode() + ":"
    terminal.until(lambda: any(marker in row for row in terminal.screen.display))
    first_input = (time.perf_counter_ns() - started) / 1e6
    clear_line()
    typing = [terminal.settled(bytes([byte])) for byte in b"git status --short"]
    clear_line()
    typing.extend(terminal.settled(bytes([byte])) for byte in b"nonmatching-fragment")
    clear_line()
    long_typing = []
    if long_line:
        terminal.settled(b"\x1b[200~echo '" + b"x" * 3400 + b"' && git -C . status --\x1b[201~")
        long_typing = [terminal.settled(bytes([byte])) for byte in b"short && echo ./fixture"]
        clear_line()
    plugins = profile(terminal)
    if plugins.get(identifier, {}).get("state") != expected:
        raise RuntimeError(f"{identifier} expected {expected}, saw {plugins}")
    return {
        "startup_visible": startup,
        "startup_usable": startup + first_input,
        "typing": typing,
        "long_typing": long_typing,
        "profile": plugins[identifier],
    }


def benchmark(args, work):
    home = work / "home"
    home.mkdir()
    history = home / "history"
    history.write_text("".join(f"git status --short # sample-{i:04d}\n" for i in range(1000)))
    repo = work / "repo"
    repo.mkdir()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("BASH_ENV", "ENV") and not key.startswith("BINIT_")
    }
    environment.update(
        HOME=str(home), INPUTRC="/dev/null", TERM="xterm-256color", LC_ALL="C.UTF-8",
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", BINIT_COMPLETION_MENU="1",
        BINIT_GIT_PROMPT_MODE=args.git_prompt_mode,
    )
    subprocess.run(["git", "init", "-q", "-b", "main", repo], env=environment, check=True)
    for i in range(args.git_files):
        (repo / f"fixture-{i:06d}").touch()
    if args.project_profile:
        (repo / ".binit-project.toml").write_text('[env]\nPROJECT_MODE = "development"\n')
    cases = {
        "before": (args.before_binary, args.before_plugin),
        "after": (args.after_binary, args.after_plugin),
        "control": (args.before_binary, args.before_plugin),
    }
    rcfiles = {
        name: prepare(work / name, *case, home, args.mode, environment,
                      project=repo if args.project_profile else None, long_line=args.long_line)
        for name, case in cases.items()
    }
    raw = {name: [] for name in cases}
    orders = tuple(permutations(cases))
    default_state = "pending" if args.plugin == "git-completion" else "loaded"
    for round_index in range(args.samples):
        for name in orders[round_index % len(orders)]:
            case_environment = environment | (dict(args.after_setting) if name == "after" else {})
            terminal = Shell(str(args.bash), rcfiles[name], repo, case_environment)
            try:
                expected = args.after_state if name == "after" and args.after_state else default_state
                raw[name].append(measure(terminal, args.plugin, expected, args.long_line))
            finally:
                terminal.close()
    return {
        "schema": 1,
        "units": "milliseconds",
        "rounds": args.samples,
        "mode": args.mode,
        "plugin": args.plugin,
        "git_prompt_mode": args.git_prompt_mode,
        "after_state": args.after_state or default_state,
        "after_settings": dict(args.after_setting),
        "git_files": args.git_files,
        "project_profile": args.project_profile,
        "long_line": args.long_line,
        "bash_version": subprocess.check_output([args.bash, "--version"], text=True).splitlines()[0],
        "bash_sha256": digest(args.bash),
        "history_sha256": digest(history),
        "workload_sha256": digest(__file__),
        "driver_sha256": digest(Path(__file__).with_name("bench_interactive.py")),
        "inputs": {
            name: {
                file.name: digest(file)
                for file in (binary, *package_files(source))
            }
            for name, (binary, source) in cases.items()
        },
        "summary": {name: summary(samples) for name, samples in raw.items()},
        "paired_after": paired(raw["before"], raw["after"]),
        "paired_control": paired(raw["before"], raw["control"]),
        "raw": raw,
    }


def package_files(source):
    directory = source.parent if source.is_file() else source
    return [*sorted(directory.glob("*.toml")), *sorted(directory.glob("*.bash"))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", choices=PLUGINS, required=True)
    parser.add_argument("--before-binary", type=Path, required=True)
    parser.add_argument("--after-binary", type=Path, required=True)
    parser.add_argument("--before-plugin", type=Path, required=True)
    parser.add_argument("--after-plugin", type=Path, required=True)
    parser.add_argument("--after-state", choices=("pending", "loaded"))
    parser.add_argument("--after-setting", action="append", default=[], metavar="BINIT_NAME=VALUE")
    parser.add_argument("--bash", type=Path, default=Path("/bin/bash"))
    parser.add_argument("--mode", choices=("emacs", "vi"), default="vi")
    parser.add_argument("--git-prompt-mode", choices=("branch", "status"), default="branch")
    parser.add_argument("--git-files", type=int, default=30)
    parser.add_argument("--project-profile", action="store_true",
                        help="start in a directory with a trusted environment profile")
    parser.add_argument("--long-line", action="store_true",
                        help="also sample edits near the end of a wrapped 3,400-byte line")
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 1 or args.git_files < 0:
        parser.error("samples must be positive and git-files must be nonnegative")
    args.after_setting = [setting.split("=", 1) for setting in args.after_setting]
    if any(len(setting) != 2 or not setting[0].startswith("BINIT_") for setting in args.after_setting):
        parser.error("after-setting requires BINIT_NAME=VALUE")
    for name in ("before_binary", "after_binary", "before_plugin", "after_plugin", "bash", "output"):
        path = getattr(args, name)
        setattr(args, name, path.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="plugin-benchmark-", dir=args.output.parent) as directory:
        result = benchmark(args, Path(directory))
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
