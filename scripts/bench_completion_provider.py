#!/usr/bin/env python3
"""Measure public-provider input response and cleanup in paired real PTYs."""
import argparse
from contextlib import ExitStack
import importlib.util
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from probe_eligible_completion import identity, wait_file

ROOT = Path(__file__).resolve().parent.parent
CASES = {
    "typed suffix": (b"xyz", "probe_cmd pxyz", 14),
    "bracketed paste": (b"\x1b[200~a b\x1b[201~", "probe_cmd pa b", 14),
    "cursor movement": (b"\x02Z", "probe_cmd Zp", 11),
    "coalesced Tab and suffix": (b"xyz", "probe_cmd pxyz", 14),
}


def children(pid):
    return Path(f"/proc/{pid}/task/{pid}/children").read_text().split()


def descriptors(pid):
    return len(list(Path(f"/proc/{pid}/fd").iterdir()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    if not __debug__:
        parser.error("optimized Python disables qualification assertions")
    for name in ("bash", "binary", "module", "work", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--pairs", type=int, default=20)
    parser.add_argument("--runs", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.pairs <= 100 or not 1 <= args.runs <= 10:
        parser.error("pairs must be 1..100 and runs 1..10")
    work = args.work.resolve()
    if not work.is_relative_to(ROOT / "tmp"):
        parser.error("work must be inside the project's tmp directory")
    work.mkdir(parents=True, exist_ok=True)
    os.environ.update(BINIT_BASH=str(args.bash.resolve()), BINIT_TEST_BINARY=str(args.binary.resolve()),
                      BINIT_TEST_MODULE=str(args.module.resolve()), BINIT_NATIVE="1")
    spec = importlib.util.spec_from_file_location("completion_session", ROOT / "tests/test_completion.py")
    completion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(completion)
    completion.ROOT = work
    provider = ROOT / "benchmarks/probes/completion_provider.py"
    inputs = {"bash": args.bash, "binary": args.binary, "test_fixture_module": args.module,
              "python": sys.executable, "provider": provider, "harness": __file__,
              "session": ROOT / "tests/test_completion.py", "terminal": ROOT / "scripts/terminal.py",
              "wait_helper": ROOT / "scripts/probe_eligible_completion.py",
              "contract": ROOT / "benchmarks/eligible-completion.json"}
    identities = {name: identity(path) for name, path in inputs.items()}
    rows = []
    result = {"schema": 1, "status": "incomplete", "runs": args.runs, "pairs": args.pairs,
              "scope": "Public API cancellation and cleanup; synthetic delay is not a workload improvement",
              "measurement": "Input write through Readline capture; process checks follow outside the timed interval",
              "coalesced_scope": "May cancel before worker acknowledgment; does not establish running-worker termination",
              "bash_version": subprocess.check_output([str(args.bash.resolve()), "--version"], text=True).splitlines()[0],
              "identities": identities, "identity_checks": [],
              "module_scope": "Framework sessions load the binary's embedded module; test_fixture_module is required by Session construction",
              "loaded_runtimes": [], "rows": rows, "successful_public_requests": []}
    with args.output.open("x") as output:
        json.dump(result, output, indent=2)

    def save():
        temporary = args.output.with_suffix(".pending")
        with temporary.open("x") as output:
            output.write(json.dumps(result, indent=2) + "\n")
        temporary.replace(args.output)

    session = None
    try:
        for run in range(args.runs):
            assert all(identity(path) == identities[name] for name, path in inputs.items()), "input changed before run"
            with ExitStack() as stack:
                sessions = {}
                loaded = []
                for mode in ("synchronous-control", "isolated"):
                    ready = work / f"ready-{run}-{mode}"
                    ready.unlink(missing_ok=True)
                    command = shlex.join([sys.executable, str(provider), mode, str(ready)])
                    extra = "bind 'set enable-bracketed-paste on'\n"
                    if mode == "synchronous-control":
                        extra += f"__control() {{ COMPREPLY=(); {command} >/dev/null; }}\n"
                        registration = "complete -F __control probe_cmd"
                    else:
                        registration = "binit completions register probe_cmd " + command
                    session = stack.enter_context(completion.Session(extra=extra, values=0, framework=True))
                    status = work / f"registration-{run}-{mode}"
                    status.unlink(missing_ok=True)
                    session.send(f"{registration}; printf '%s' \"$?\" > {shlex.quote(str(status))}\n")
                    session.wait(lambda: status.exists() and status.stat().st_size)
                    assert status.read_text() == "0", "registration failed"
                    modules = list(session.directory.glob("state/generations/*/completion.so"))
                    assert len(modules) == 1, "ambiguous prepared module"
                    runtime = modules[0].with_name("runtime.bash")
                    loaded.append((modules[0], identity(modules[0]), runtime, identity(runtime)))
                    result["loaded_runtimes"].append({"run": run, "treatment": mode,
                                                      "module": loaded[-1][1], "runtime": loaded[-1][3]})
                    if mode == "isolated":
                        session.open("probe_cmd p")
                        assert "p stale one" in session.text and "p stale two" in session.text
                        session.send(b"\x1b")
                        session.wait(lambda: not session.panel)
                        result["successful_public_requests"].append({"run": run, "receipt": json.loads(ready.read_text())})
                    pid = session.terminal.pid
                    assert children(pid) == [], "unexpected idle child"
                    sessions[mode] = (session, ready, descriptors(pid))
                for case, (payload, expected, point) in CASES.items():
                    for pair in range(args.pairs):
                        order = ("synchronous-control", "isolated") if pair % 2 == 0 else ("isolated", "synchronous-control")
                        for mode in order:
                            session, ready, baseline_fds = sessions[mode]
                            pid = session.terminal.pid
                            ready.unlink(missing_ok=True)
                            session.capture_path.unlink(missing_ok=True)
                            session.send(b"\x01\x0bprobe_cmd p")
                            acknowledged = None
                            active_children = None
                            active_fds = None
                            if case == "coalesced Tab and suffix":
                                started = time.perf_counter_ns()
                                session.send(b"\t" + payload + b"\x18\x07")
                            else:
                                session.send(b"\t")
                                wait_file(session, ready)
                                acknowledged = json.loads(ready.read_text())
                                active_children = children(pid)
                                active_fds = descriptors(pid)
                                assert acknowledged["parent"] == pid and active_children == [str(acknowledged["pid"])], "worker not active"
                                started = time.perf_counter_ns()
                                session.send(payload + b"\x18\x07")
                            wait_file(session, session.capture_path)
                            elapsed = (time.perf_counter_ns() - started) / 1e6
                            actual_point, actual_line, _ = session.capture_path.read_bytes().split(b"\0")
                            session.pump(.003)
                            receipt = json.loads(ready.read_text()) if ready.exists() else None
                            idle_children = children(pid)
                            idle_fds = descriptors(pid)
                            reaped = receipt is None or not Path(f"/proc/{receipt['pid']}").exists()
                            row = {"run": run, "pair": pair, "case": case, "treatment": mode,
                                   "elapsed_ms": elapsed, "line": actual_line.decode(), "point": int(actual_point),
                                   "input_preserved": actual_line.decode() == expected and int(actual_point) == point,
                                   "receipt": receipt, "acknowledged_before_input": acknowledged is not None,
                                   "active_children": active_children, "active_parent_fds": active_fds,
                                   "baseline_parent_fds": baseline_fds, "idle_parent_fds": idle_fds,
                                   "idle_children": idle_children, "worker_reaped": reaped,
                                   "cleanup_ok": reaped and not idle_children and idle_fds == baseline_fds}
                            if mode == "isolated" and acknowledged:
                                row["bounds_ok"] = (active_fds <= baseline_fds + 2 and
                                    acknowledged["limits"]["address_space"][1] <= 268435456 and
                                    acknowledged["limits"]["cpu"][1] <= 2 and
                                    acknowledged["limits"]["descriptors"][1] <= 32 and
                                    acknowledged["descriptors"] <= 32)
                            else:
                                row["bounds_ok"] = True
                            rows.append(row)
                            save()
                assert all(identity(module) == module_id and identity(runtime) == runtime_id
                           for module, module_id, runtime, runtime_id in loaded), "prepared runtime changed during run"
            assert all(identity(path) == identities[name] for name, path in inputs.items()), "input changed during run"
            result["identity_checks"].append({"run": run, "unchanged": True})
            save()
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
        if session is not None:
            result["terminal_tail"] = bytes(session.transcript[-4096:]).decode(errors="replace")
        save()
        raise
    summaries = []
    for run in range(args.runs):
        for case in CASES:
            for mode in ("synchronous-control", "isolated"):
                selected = [row for row in rows if row["run"] == run and row["case"] == case and row["treatment"] == mode]
                times = sorted(row["elapsed_ms"] for row in selected)
                summaries.append({"run": run, "case": case, "treatment": mode, "pairs": len(times),
                                  "p95_ms": times[math.ceil(.95 * len(times)) - 1],
                                  "failures": sum(not all(row[key] for key in ("input_preserved", "cleanup_ok", "bounds_ok")) for row in selected)})
    passed = all(not row["failures"] and (row["p95_ms"] <= 16 if row["treatment"] == "isolated" else row["p95_ms"] >= 150)
                 for row in summaries)
    result.update(status="passed" if passed else "failed", qualified_sample_count=args.runs == 2 and args.pairs == 20,
                  summaries=summaries)
    save()
    print(json.dumps(summaries, indent=2))
    return int(not passed)


if __name__ == "__main__":
    raise SystemExit(main())
