"""Compare deferred acknowledgement with and without yielding before first use."""
import argparse
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from bench_interactive import Shell, digest, summarize

START = b"\x1eFIRST\x1f"
READY = b"\x1eSECOND-READY\x1f"
EFFECT = b"\x1eEFFECT:2:<two words><>\x1f"
STATUS_PREFIX = b"\x1eSTATUS:"
INPUT = b"second_tool 'two words' ''"
COMMAND = INPUT + b"; stat=$?; printf '\\036STATUS:%s\\037' \"$stat\"\n"


def score_run(rows, pairs):
    expected = {(pair, arm) for pair in range(pairs) for arm in ('baseline', 'candidate')}
    observed = [(row['pair'], row['mode']) for row in rows]
    if set(observed) != expected or len(observed) != len(expected) or not all(row['effects_ok'] for row in rows):
        raise RuntimeError('incomplete, duplicate or failed observations')
    old_values = sorted(row['metric_ms']['echo'] for row in rows if row['mode'] == 'baseline')
    new_values = sorted(row['metric_ms']['echo'] for row in rows if row['mode'] == 'candidate')
    if not all(math.isfinite(value) and value > 0 for value in old_values + new_values):
        raise RuntimeError('invalid acknowledgement duration')
    old = old_values[math.ceil(len(old_values) * .95) - 1]
    new = new_values[math.ceil(len(new_values) * .95) - 1]
    return {'baseline_p95_ms': old, 'candidate_p95_ms': new, 'ratio': new / old}


def run(manager, bash, root, receipt, mode, editor, case, editing):
    key = b"\x14" if case == "alternate-accept" else b"\n"
    typed = INPUT.replace(b"second_tool", b"second_short") if case == "alias" else INPUT
    command = COMMAND.replace(INPUT, typed).removesuffix(b"\n")
    release = root / "release"
    configs = ["schema=1"]
    for name, body in (
        ("first", f"""printf '\036FIRST\037'
while [[ ! -e {shlex.quote(str(release))} ]]; do sleep 0.001; done
"""),
        ("second", """sleep 0.075
second_tool() { printf '\036EFFECT:%d:' "$#"; printf '<%s>' "$@"; printf '\037'; return 37; }
printf '\036SECOND-READY\037'
"""),
    ):
        if name == "second" and case == "alias":
            body += "alias second_short=second_tool\n"
        package = root / name
        package.mkdir()
        (package / "plugin.toml").write_text(
            f"schema=1\nid='{name}'\nentry='plugin.bash'\nload='wait'\n"
        )
        (package / "plugin.bash").write_text(body)
        configs.extend(["[[plugins]]", f"path='{name}'"])
    config = root / "config.toml"
    config.write_text("\n".join(configs) + "\n")
    state = root / "state"
    subprocess.run([manager, "prepare", config, "--state", state], check=True, capture_output=True)
    rc = root / "rc"
    rc.write_text(
        f"PS1='BENCH> '\nset -o {editor}\n"
        + ("bind '\"\\C-t\": accept-line'\n" if case == "alternate-accept" else "")
        +
        f"eval \"$({shlex.quote(str(manager))} init --state {shlex.quote(str(state))})\"\n"
        "__binit_runtime_load more || :\n"
    )
    home = root / "home"
    home.mkdir()
    env = {"PATH": os.defpath, "HOME": str(home), "TERM": "xterm-256color",
           "INPUTRC": "/dev/null", "HISTFILE": "/dev/null", "BINIT_COMPLETION_MENU": "1",
           "LC_ALL": "C.UTF-8", "BINIT_WAIT_EDIT": "1" if editing else "0"}
    shell = Shell(str(bash), rc, root, env)
    try:
        shell.until(lambda: START in shell.raw)
        offset = len(shell.raw)
        sent = time.perf_counter_ns()
        os.write(shell.fd, command if case == "pause" else command + key)
        release.touch()
        metrics = {}
        markers = {"echo": typed, "ready": READY, "status": STATUS_PREFIX}
        if case == "pause":
            for name in ("echo", "ready"):
                shell.until(lambda: markers[name] in shell.raw[offset:], seconds=.5)
                metrics[name] = (time.perf_counter_ns() - sent) / 1e6
            os.write(shell.fd, key)
        while len(metrics) != len(markers):
            shell.until(lambda: any(marker in shell.raw[offset:]
                      for name, marker in markers.items() if name not in metrics))
            elapsed = (time.perf_counter_ns() - sent) / 1e6
            for name, marker in markers.items():
                if marker in shell.raw[offset:]:
                    metrics.setdefault(name, elapsed)
        trace = bytes(shell.raw[offset:])
        receipt.write_bytes(trace)
        effects_ok = EFFECT in trace and STATUS_PREFIX + b"37\x1f" in trace
        if not effects_ok:
            raise RuntimeError(f"baseline effects failed: {trace!r}")
        return {"mode": mode, "metric_ms": metrics, "effects_ok": effects_ok}
    finally:
        shell.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--bash", type=Path, default=Path("/bin/bash"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pairs", type=int, default=20)
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--editing", action="store_true")
    parser.add_argument("--editor", choices=["emacs", "vi"], required=True)
    parser.add_argument("--case", choices=["queued", "alias", "alternate-accept", "pause"], required=True)
    args = parser.parse_args()
    if args.runs < 1 or args.pairs < 1:
        parser.error('runs and pairs must be positive')
    if not args.output.resolve().is_relative_to(ROOT / 'tmp'):
        parser.error('output must be under project tmp/')
    args.baseline = args.baseline.resolve(strict=True)
    args.candidate = args.candidate.resolve(strict=True)
    args.bash = args.bash.resolve(strict=True)
    args.output.mkdir()
    rows = []
    arms = {"baseline": args.baseline, "candidate": args.candidate}
    result = {"binary_sha256": {arm: digest(binary) for arm,binary in arms.items()},
              "bash_sha256": digest(args.bash), "harness_sha256": digest(__file__),
              "status": "incomplete", "editor": args.editor, "case": args.case,
              "editing": args.editing, "raw": rows}
    summary = args.output / "result.json"
    try:
        for index in range(args.runs * args.pairs):
            order = list(arms.items())
            if index % 2:
                order.reverse()
            for mode, binary in order:
                if (digest(binary) != result['binary_sha256'][mode] or
                        digest(args.bash) != result['bash_sha256'] or
                        digest(__file__) != result['harness_sha256']):
                    raise RuntimeError('benchmark inputs changed')
                with tempfile.TemporaryDirectory(prefix="trial-", dir=args.output) as work:
                    rows.append({"run": index // args.pairs, "pair": index % args.pairs, **run(binary, args.bash,
                        Path(work), args.output / f"{index}-{mode}.trace", mode,
                        args.editor, args.case, args.editing)})
                summary.write_text(json.dumps(result, indent=2) + "\n")
        result["summary"] = {
            mode: {key: summarize([row["metric_ms"][key] for row in rows if row["mode"] == mode])
                   for key in ("echo", "ready", "status")}
            for mode in arms}
        result["runs"] = []
        for run_index in range(args.runs):
            subset = [row for row in rows if row["run"] == run_index]
            result["runs"].append(score_run(subset, args.pairs))
        result["binit_comparison_pass"] = (args.runs == 2 and args.pairs == 20 and
                                           all(row["ratio"] <= .8 for row in result["runs"]))
        result["release_gate_pass"] = False
        result["pending"] = ["matched Zinit deferred workload"]
        result["status"] = "complete"
    finally:
        summary.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
