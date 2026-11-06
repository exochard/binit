#!/usr/bin/env python3
"""Freeze and compare unchanged Bash scripts with paired elapsed-time samples."""

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import signal
import statistics
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
SUITE = ROOT / "benchmarks/suite.json"
LOCK = ROOT / "benchmarks/suite.lock.json"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def geomean(values):
    return math.exp(statistics.mean(math.log(value) for value in values))


def exports(engine):
    output = subprocess.check_output(["nm", "--dynamic", "--defined-only", "--format=posix", engine], text=True)
    return {line.split()[0] for line in output.splitlines()}


def fixture(directory):
    data = directory / "data"
    data.mkdir(mode=0o755)
    for i in range(64):
        group = data / f"group_{i // 16}"
        group.mkdir(mode=0o755, exist_ok=True)
        file = group / f"file_{i}.txt"
        file.write_text(f"record_{i}:{i}:ready\n")
        file.chmod(0o644)
    (data / "records.txt").write_text("".join(
        f"record_{i}:{i}:{'skip' if i % 3 == 0 else 'ready'}\n" for i in range(128)))
    (data / "records.txt").chmod(0o644)
    for child in [data, *data.glob("group_*")]:
        child.chmod(0o755)
    return data


def tree(path):
    result = [[".", "directory", path.stat().st_mode & 0o777]]
    for child in sorted(path.rglob("*")):
        name = child.relative_to(path).as_posix()
        if child.is_symlink():
            result.append([name, "symlink", os.readlink(child)])
        elif child.is_file():
            result.append([name, "file", child.stat().st_mode & 0o777, file_sha(child)])
        else:
            result.append([name, "directory", child.stat().st_mode & 0o777])
    return sha(json.dumps(result, separators=(",", ":")).encode())


def invoke(engine, case, count, directory, data):
    with tempfile.TemporaryDirectory(prefix="work-", dir=directory) as temporary:
        work = Path(temporary)
        env = {"PATH": os.defpath, "HOME": str(work), "LC_ALL": "C", "TZ": "UTC",
               "TERM": "dumb", "BENCH_COUNT": str(count), "DATA_DIR": str(data)}
        command = [str(engine), "--noprofile", "--norc",
                   str(ROOT / "benchmarks/scripts" / f"{case}.bash")]
        started = time.perf_counter_ns()
        child = subprocess.Popen(command, cwd=work, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = child.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
            raise ValueError(f"timeout: {case} with {engine}") from None
        elapsed = (time.perf_counter_ns() - started) / 1e9
        observation = {"stdout": sha(stdout), "stderr": sha(stderr),
                       "status": child.returncode, "files": tree(work)}
        return elapsed, observation


def freeze(engine):
    if LOCK.exists():
        raise ValueError("suite is already frozen; review and rename the old lock before recalibration")
    suite = json.loads(SUITE.read_text())
    records = []
    with tempfile.TemporaryDirectory(prefix=".scratch-benchmark-", dir=ROOT) as temporary:
        directory = Path(temporary)
        data = fixture(directory)
        for case in suite["cases"]:
            count = 1
            for _ in range(10):
                elapsed, expected = invoke(engine, case["name"], count, directory, data)
                if expected["status"] != 0 or expected["stderr"] != sha(b""):
                    raise ValueError(f"failed calibration: {case['name']}: {expected}")
                if elapsed >= 0.3:
                    checks = [invoke(engine, case["name"], count, directory, data) for _ in range(2)]
                    if all(result == expected and duration >= suite["minimum_seconds"]
                           for duration, result in checks):
                        break
                count = max(count + 1, math.ceil(count * max(1.2, 0.33 / elapsed)))
                if count > 100_000_000:
                    raise ValueError(f"calibration count too large: {case['name']}")
            else:
                raise ValueError(f"unstable calibration: {case['name']}")
            records.append({**case, "count": count, "expected": expected,
                            "script_sha256": file_sha(ROOT / "benchmarks/scripts" / f"{case['name']}.bash")})
            print(f"{case['name']}: {count} iterations, {elapsed:.3f}s", flush=True)
        lock = {"schema": 1, "suite_sha256": file_sha(SUITE), "fixture_sha256": tree(data),
                "cases": records}
        LOCK.write_text(json.dumps(lock, indent=2) + "\n")


def load_lock():
    suite = json.loads(SUITE.read_text())
    lock = json.loads(LOCK.read_text())
    if lock["suite_sha256"] != file_sha(SUITE):
        raise ValueError("suite changed after calibration")
    if [(c["name"], c["family"]) for c in suite["cases"]] != [
            (c["name"], c["family"]) for c in lock["cases"]]:
        raise ValueError("lock cases differ from suite")
    for case in lock["cases"]:
        path = ROOT / "benchmarks/scripts" / f"{case['name']}.bash"
        if file_sha(path) != case["script_sha256"]:
            raise ValueError(f"script changed after calibration: {case['name']}")
    return suite, lock


def measure(engines, output, samples, runs):
    _, lock = load_lock()
    if output.exists():
        raise ValueError(f"result already exists: {output}")
    metadata = {}
    for label, engine in engines.items():
        metadata[label] = {"path": str(engine), "sha256": file_sha(engine),
                           "version": subprocess.check_output([engine, "--version"], text=True).splitlines()[0]}
        build_info = engine.parent / "build.json"
        if build_info.exists():
            metadata[label]["build"] = json.loads(build_info.read_text())
            if metadata[label]["sha256"] != metadata[label]["build"]["binary_sha256"]:
                raise ValueError(f"binary differs from build metadata: {label}")
    result = {"schema": 1, "suite_lock_sha256": file_sha(LOCK), "started": datetime.now(timezone.utc).isoformat(),
              "platform": {"machine": platform.machine(), "kernel": platform.release(),
                           "libc": platform.libc_ver()},
              "engines": metadata, "samples_per_run": samples, "runs": runs, "records": []}
    if {"reference", "candidate"} <= engines.keys():
        missing = exports(engines["reference"]) - exports(engines["candidate"])
        if missing:
            raise ValueError(f"candidate lost reference exports: {', '.join(sorted(missing))}")
        result["reference_exports_preserved"] = True
    with tempfile.TemporaryDirectory(prefix=".scratch-benchmark-", dir=ROOT) as temporary:
        directory = Path(temporary)
        data = fixture(directory)
        if tree(data) != lock["fixture_sha256"]:
            raise ValueError("fixture changed after calibration")
        for run in range(runs):
            rng = random.Random(5917 + run)
            result.setdefault("run_started", []).append(datetime.now(timezone.utc).isoformat())
            for sample in range(samples):
                cases = lock["cases"].copy()
                rng.shuffle(cases)
                for case in cases:
                    labels = list(engines)
                    rng.shuffle(labels)
                    times = {}
                    for label in labels:
                        elapsed, observed = invoke(engines[label], case["name"], case["count"], directory, data)
                        if observed != case["expected"]:
                            raise ValueError(f"behavior differs: {label}/{case['name']}: {observed}")
                        if tree(data) != lock["fixture_sha256"]:
                            raise ValueError(f"input fixture was modified: {label}/{case['name']}")
                        times[label] = elapsed
                    result["records"].append({"run": run, "sample": sample, "case": case["name"], "seconds": times})
                print(f"run {run + 1}/{runs}, pair {sample + 1}/{samples}", flush=True)
                # A partial file remains useful evidence if the scope expires.
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(result, indent=2) + "\n")
        if tree(data) != lock["fixture_sha256"]:
            raise ValueError("input fixture was modified")
    result["finished"] = datetime.now(timezone.utc).isoformat()
    output.write_text(json.dumps(result, indent=2) + "\n")
    return result


def score(result):
    suite, lock = load_lock()
    if result["suite_lock_sha256"] != file_sha(LOCK):
        raise ValueError("results belong to a different suite lock")
    if not {"reference", "candidate"} <= result["engines"].keys():
        raise ValueError("comparison needs reference and candidate labels")
    labels = set(result["engines"])
    for record in result["records"]:
        if set(record["seconds"]) != labels or any(
                not isinstance(value, (int, float)) or isinstance(value, bool) or
                not math.isfinite(value) or value <= 0 for value in record["seconds"].values()):
            raise ValueError("every record needs a finite positive duration for every engine")
    expected_pairs = {(r, s, c["name"]) for r in range(result["runs"])
                      for s in range(result["samples_per_run"]) for c in lock["cases"]}
    actual_pairs = [(r["run"], r["sample"], r["case"]) for r in result["records"]]
    complete = set(actual_pairs) == expected_pairs and len(actual_pairs) == len(expected_pairs) and "finished" in result
    report = {"complete": complete, "baselines": {}}
    qualified = complete and result["runs"] >= suite["runs"] and result["samples_per_run"] >= suite["samples_per_run"]
    qualified &= result.get("reference_exports_preserved") is True
    system = result["engines"].get("system", {})
    installed = Path("/bin/bash").resolve()
    if system.get("path") != str(installed) or system.get("sha256") != file_sha(installed):
        qualified = False
    build = result["engines"]["reference"].get("build", {})
    candidate = result["engines"]["candidate"].get("build", {})
    source_lock = ROOT / "engines/bash/source.json"
    source_version = json.loads(source_lock.read_text())["version"]
    source_tree = (ROOT / "engines/bash/tree.sha256").read_text().strip()
    for label, provenance in (("reference", build), ("candidate", candidate)):
        if (provenance.get("binary_sha256") != result["engines"][label].get("sha256") or
                provenance.get("source_lock_sha256") != file_sha(source_lock) or
                provenance.get("source_tree_sha256") != source_tree or
                provenance.get("version") != source_version or
                provenance.get("configure") != ["--without-bash-malloc"]):
            qualified = False
    if build.get("kind") != "reference" or build.get("cflags") != "-O2 -g":
        qualified = False
    if candidate.get("kind") not in ("pgo", "lto"):
        qualified = False
    if candidate.get("kind") == "pgo" and candidate.get("training_sha256") != file_sha(
            ROOT / "engines/bash/train.bash"):
        qualified = False
    if result["engines"]["reference"].get("sha256") == result["engines"]["candidate"].get("sha256"):
        qualified = False
    for baseline in ("reference", "system"):
        if baseline not in result["engines"]:
            continue
        run_scores, families_by_run, cases_report = [], [], []
        baseline_long_enough = True
        for run in range(result["runs"]):
            family_ratios = defaultdict(list)
            case_ratios = []
            for case in lock["cases"]:
                rows = [row for row in result["records"] if row["run"] == run and row["case"] == case["name"]]
                if not rows:
                    continue
                ratio = statistics.median(row["seconds"]["candidate"] / row["seconds"][baseline] for row in rows)
                case_ratios.append(ratio)
                family_ratios[case["family"]].append(ratio)
                baseline_median = statistics.median(row["seconds"][baseline] for row in rows)
                baseline_long_enough &= baseline_median >= suite["minimum_seconds"]
                cases_report.append({"run": run, "case": case["name"], "ratio": ratio,
                                     "baseline_seconds": baseline_median})
            run_scores.append(geomean(case_ratios) if case_ratios else None)
            families_by_run.append({family: geomean(values) for family, values in family_ratios.items()})
        families = sorted({case["family"] for case in lock["cases"]})
        improvements = [family for family in families if all(run.get(family, 2) < 1 for run in families_by_run)]
        regressions = [family for family in families if sum(
            run.get(family, 2) > suite["maximum_family_regression"] for run in families_by_run) >= 2]
        passes = (all(value is not None and value <= suite["maximum_ratio"] for value in run_scores)
                  and len(improvements) >= suite["minimum_improved_families"] and not regressions and baseline_long_enough)
        report["baselines"][baseline] = {"run_ratios": run_scores, "families": families_by_run,
                                         "improved_families": improvements, "regressed_families": regressions,
                                         "baseline_long_enough": baseline_long_enough,
                                         "performance_pass": passes, "cases": cases_report}
        qualified &= passes
    report["release_gate_pass"] = bool(qualified)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    calibration = commands.add_parser("freeze", help="calibrate once, before optimization")
    calibration.add_argument("--engine", type=Path, default=Path("/bin/bash"))
    measurement = commands.add_parser("run")
    measurement.add_argument("--engine", action="append", required=True, metavar="LABEL=PATH")
    measurement.add_argument("--output", type=Path, required=True)
    measurement.add_argument("--samples", type=int, default=30)
    measurement.add_argument("--runs", type=int, default=3)
    comparison = commands.add_parser("compare")
    comparison.add_argument("result", type=Path)
    args = parser.parse_args()
    if args.command == "freeze":
        freeze(args.engine.resolve(strict=True))
    elif args.command == "run":
        engines = {}
        for item in args.engine:
            label, path = item.split("=", 1)
            if label in engines or not label:
                raise ValueError("engine labels must be unique and nonempty")
            engines[label] = Path(path).resolve(strict=True)
        if not 1 <= args.samples <= 1000 or not 1 <= args.runs <= 10:
            raise ValueError("samples must be 1..1000 and runs 1..10")
        measure(engines, args.output, args.samples, args.runs)
    else:
        report = score(json.loads(args.result.read_text()))
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report["release_gate_pass"] else 1)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
