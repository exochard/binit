#!/usr/bin/env python3
"""Attribute fixed-suite costs without changing validation or claiming qualification."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import benchmark  # noqa: E402
import engine  # noqa: E402


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def run_case(binary, case, data, output, prefix=(), timeout=180):
    with tempfile.TemporaryDirectory(prefix=".scratch-cost-", dir=ROOT) as temporary:
        work = Path(temporary)
        env = {"PATH": os.defpath, "HOME": str(work), "LC_ALL": "C", "TZ": "UTC",
               "TERM": "dumb", "BENCH_COUNT": str(case["count"]), "DATA_DIR": str(data)}
        command = [*map(str, prefix), str(binary), "--noprofile", "--norc",
                   str(ROOT / "benchmarks/scripts" / (case["name"] + ".bash"))]
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        started = time.perf_counter_ns()
        child = subprocess.Popen(command, cwd=work, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
            raise ValueError(f"diagnostic timeout: {case['name']}") from None
        elapsed = (time.perf_counter_ns() - started) / 1e9
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        observed = {"stdout": benchmark.sha(stdout), "stderr": benchmark.sha(stderr),
                    "status": child.returncode, "files": benchmark.tree(work)}
        result = {"pid": child.pid, "command": command, "elapsed_seconds": elapsed,
                  "tree_user_seconds": after.ru_utime - before.ru_utime,
                  "tree_system_seconds": after.ru_stime - before.ru_stime,
                  "observation": observed, "behavior_matches": observed == case["expected"]}
        write(output, result)
        if not result["behavior_matches"]:
            (output.with_suffix(".stderr")).write_bytes(stderr)
            raise ValueError(f"diagnostic behavior mismatch: {case['name']}")
        return result


def taskclock(path):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.startswith("{")]
    rows = [row for row in rows if row.get("event") == "task-clock:u"]
    if len(rows) != 1 or rows[0].get("unit") != "msec":
        raise ValueError("missing root task-clock observation")
    running = float(rows[0]["pcnt-running"])
    value = float(rows[0]["counter-value"])
    if not math.isfinite(running) or not 99 <= running <= 100:
        raise ValueError("root task-clock counter was multiplexed")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("invalid root task-clock duration")
    return value / 1000


def native(binary, cases, data, stage, pairs):
    records = []
    rng = random.Random(20261107)
    for sample in range(pairs):
        order = cases.copy()
        rng.shuffle(order)
        for case in order:
            modes = ["clean", "root-clock"]
            rng.shuffle(modes)
            pair = {"case": case["name"], "sample": sample}
            for mode in modes:
                path = stage / f"{case['name']}-{sample}-{mode}.json"
                prefix = []
                counter = stage / f"{case['name']}-{sample}.counter"
                if mode == "root-clock":
                    prefix = ["perf", "stat", "--no-inherit", "-e", "task-clock:u",
                              "--json-output", "-o", counter]
                result = run_case(binary, case, data, path, prefix)
                if mode == "root-clock":
                    result["root_task_clock_seconds"] = taskclock(counter)
                    write(path, result)
                pair[mode] = result
            records.append(pair)
            write(stage / "records.json", records)
        print(f"native pair {sample + 1}/{pairs}", flush=True)


def stacks(binary, cases, data, stage, repetitions, event, period):
    for case in cases:
        directory = stage / case["name"]
        directory.mkdir()
        sampling = ["-c", str(period)] if event == "cycles:u" else ["-F", "499"]
        profiles = []
        with (directory / "stacks.txt").open("w") as dest:
            for number in range(repetitions):
                profile = directory / f"cpu-{number}.perf"
                prefix = ["perf", "record", "-q", "--no-buildid-cache", "-m", "128", "-e", event,
                          *sampling, "--call-graph", "dwarf,8192",
                          "--user-callchains", "--all-user", "-o", str(profile), "--"]
                run_case(binary, case, data, directory / f"invocation-{number}.json", prefix)
                stack = directory / f"stacks-{number}.txt"
                unwind = directory / f"unwind-{number}.log"
                with stack.open("w") as stream, unwind.open("w") as log:
                    subprocess.run(["perf", "script", "-i", profile, "-F", "comm,pid,period,event,ip,sym,dso",
                                    "--max-stack", "128"], stdout=stream, stderr=log, check=True, timeout=60)
                accepted = "lost" not in unwind.read_text()
                profiles.append({"invocation": number, "accepted": accepted,
                                 "reason": "no lost-record warning" if accepted else "lost profiling records"})
                if accepted:
                    dest.write(stack.read_text())
        write(directory / "profiles.json", profiles)
        print("stacks", case["name"], flush=True)


def heap(binary, cases, data, stage):
    for case in cases:
        with tempfile.TemporaryDirectory(prefix=".scratch-heap-", dir=ROOT) as temporary:
            scratch = Path(temporary)
            result = run_case(binary, case, data, stage / (case["name"] + ".run.json"),
                              ["valgrind", "--tool=dhat", "--trace-children=no",
                               "--child-silent-after-fork=yes", "--num-callers=32",
                               f"--dhat-out-file={scratch}/heap.%p.json",
                               f"--log-file={scratch}/heap.%p.log"], timeout=300)
            source = scratch / f"heap.{result['pid']}.json"
            doc = json.loads(source.read_text())
            if doc["pid"] != result["pid"]:
                raise ValueError("DHAT root PID mismatch")
            shutil.move(source, stage / (case["name"] + ".heap.json"))
            shutil.move(scratch / f"heap.{result['pid']}.log", stage / (case["name"] + ".heap.log"))
            result["discarded_child_profiles"] = len(list(scratch.glob("heap.*.json")))
            result["scope"] = "root-process only; child copies excluded"
            write(stage / (case["name"] + ".run.json"), result)
        print("heap", case["name"], flush=True)


def trace(binary, cases, data, stage):
    for case in cases:
        run_case(binary, case, data, stage / (case["name"] + ".run.json"),
                 ["strace", "-f", "-qq", "-v", "-e", "trace=process", "-o",
                  stage / (case["name"] + ".trace")], timeout=180)
        print("trace", case["name"], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-name", default="arithmetic-decimal-pgo")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--pairs", type=int, default=5)
    parser.add_argument("--repetitions", type=int, default=8)
    parser.add_argument("--event", choices=("cpu-clock:u", "cycles:u"), default="cpu-clock:u")
    parser.add_argument("--period", type=int, default=1000000)
    parser.add_argument("mode", choices=("native", "stacks", "heap", "trace"))
    args = parser.parse_args()
    if not args.engine_name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in args.engine_name):
        raise ValueError("invalid engine name")
    if not 1 <= args.pairs <= 30 or not 1 <= args.repetitions <= 30 or args.period < 100000:
        raise ValueError("diagnostic repetition bound exceeded")
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    binary = engine.CACHE / args.engine_name / "bash"
    metadata = json.loads((binary.parent / "build.json").read_text())
    if engine.digest(binary) != metadata["binary_sha256"]:
        raise ValueError("engine binary metadata mismatch")
    _, lock = benchmark.load_lock()
    cases = [c for c in lock["cases"] if not args.case or c["name"] in args.case]
    if not cases or set(args.case) - {c["name"] for c in cases}:
        raise ValueError("unknown fixed-suite case")
    data = root / "fixture" / "data"
    if not data.exists():
        (root / "fixture").mkdir()
        data = benchmark.fixture(root / "fixture")
    if benchmark.tree(data) != lock["fixture_sha256"]:
        raise ValueError("fixed fixture mismatch")
    inputs = {"suite_lock_sha256": engine.digest(benchmark.LOCK),
              "source_lock_sha256": engine.digest(engine.LOCK),
              "training_sha256": engine.digest(ROOT / "engines/bash/train.bash"), "build": metadata}
    contract = root / "inputs.json"
    if contract.exists():
        if json.loads(contract.read_text()) != inputs:
            raise ValueError("cost-budget inputs changed between stages")
    else:
        write(contract, inputs)
    stage = root / args.mode
    stage.mkdir(exist_ok=False)
    driver = Path(__file__).resolve()
    driver_hash = engine.digest(driver)
    shutil.copyfile(driver, stage / "driver.py")
    functions = {"native": lambda: native(binary, cases, data, stage, args.pairs),
                 "stacks": lambda: stacks(binary, cases, data, stage, args.repetitions, args.event, args.period),
                 "heap": lambda: heap(binary, cases, data, stage),
                 "trace": lambda: trace(binary, cases, data, stage)}
    functions[args.mode]()
    if engine.digest(driver) != driver_hash or engine.digest(binary) != metadata["binary_sha256"]:
        raise ValueError("cost-budget producer changed during collection")
    if benchmark.tree(data) != lock["fixture_sha256"]:
        raise ValueError("profiling modified fixed fixture inputs")
    write(stage / "receipt.json", {"status": "diagnostic-only", "release_gate_pass": False,
                                   "driver_sha256": driver_hash, "inputs": inputs,
                                   "case_counts": {c["name"]: c["count"] for c in cases},
                                   "pairs": args.pairs, "stack_repetitions": args.repetitions,
                                   "stack_event": args.event, "stack_period": args.period,
                                   "finished": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    main()
