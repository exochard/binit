#!/usr/bin/env python3
"""Run the frozen interactive corpus with observer cleanup outside timed fields."""
import argparse
from contextlib import contextmanager
import gc
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

import bench_interactive as workload


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "benchmarks/interactive-observer.json"
METRICS = ("startup_visible", "startup_usable", "typing", "typing_miss", "first_history",
           "builtin_loop", "external_commands", "first_file_completion", "first_git_completion",
           "warm_git_completion")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def controlled_observer(module, cleanup):
    original = module.Shell
    was_enabled = gc.isenabled()

    def collect(event):
        started = time.perf_counter_ns()
        count = gc.collect()
        cleanup.append({"event": event, "collected": count,
                        "milliseconds": (time.perf_counter_ns() - started) / 1e6})

    class ControlledShell(original):
        def close(self):
            try:
                super().close()
            finally:
                collect("closed-shell")

    gc.disable()
    module.Shell = ControlledShell
    try:
        yield
    finally:
        module.Shell = original
        try:
            collect("teardown")
        finally:
            if was_enabled:
                gc.enable()
            else:
                gc.disable()


def assess_runs(runs, policy):
    """Recompute preservation from raw samples; stored summaries are not trusted."""
    if len(runs) != policy["runs"]:
        raise ValueError("two complete runs are required")
    rounds = policy["rounds_per_run"]
    ratios = []
    expected_inputs = {"binary", "reference", "bash", "python", "workload", "observer", "policy"}
    expected_inputs.update(f"plugin/{plugin}/{name}" for plugin in workload.PLUGINS
                           for name in ("plugin.toml", "plugin.bash"))
    common_inputs = runs[0].get("input_sha256", {})
    if (set(common_inputs) != expected_inputs or
            any(not isinstance(value, str) or len(value) != 64 or
                any(character not in "0123456789abcdef" for character in value)
                for value in common_inputs.values())):
        raise ValueError("complete input identities are required")
    for index, run in enumerate(runs):
        if run.get("identities_unchanged") is not True or run.get("input_sha256") != common_inputs:
            raise ValueError("run input identities differ or were not verified")
        if not run.get("complete") or run.get("pairs") != rounds:
            raise ValueError("incomplete run or wrong sample count")
        raw = run["raw"]
        if set(raw) != {"bare_bash", "binit_empty", "binit_seven_plugins", "binit_seven_before"}:
            raise ValueError("scenario set changed")
        for variant, metrics in raw.items():
            expected = METRICS if variant.startswith("binit_seven_") else METRICS[:-2]
            if set(metrics) != set(expected):
                raise ValueError("metric set changed")
            for metric, values in metrics.items():
                count = rounds * (11 if metric == "typing" else 16 if metric == "typing_miss" else 1)
                if len(values) != count or any(type(x) not in (int, float) or not math.isfinite(x) or x <= 0 for x in values):
                    raise ValueError("invalid raw sample count or timing")
        profiles = run["profiles"]
        if len(profiles) != 2 * rounds:
            raise ValueError("readiness profile count changed")
        for variant in ("binit_seven_plugins", "binit_seven_before"):
            selected = [p for p in profiles if p["scenario"] == variant]
            if len(selected) != rounds:
                raise ValueError("readiness evidence is incomplete")
            for row in selected:
                for plugin in workload.PLUGINS:
                    before = "pending" if plugin == "git-completion" else "loaded"
                    if (row["before_git_completion"][plugin]["state"] != before or
                            row["after_git_completion"][plugin]["state"] != "loaded"):
                        raise ValueError("plugin readiness effect changed")
        for metric in METRICS:
            candidate = sorted(raw["binit_seven_plugins"][metric])
            reference = sorted(raw["binit_seven_before"][metric])
            statistics_to_check = [("median", statistics.median)]
            statistics_to_check.append(("p95", lambda values: values[math.ceil(len(values) * .95) - 1]))
            if metric in ("typing", "typing_miss"):
                statistics_to_check.append(("p99", lambda values: values[math.ceil(len(values) * .99) - 1]))
            for name, calculate in statistics_to_check:
                current, previous = calculate(candidate), calculate(reference)
                ratios.append({"run": index, "metric": metric, "statistic": name,
                               "samples": len(candidate), "candidate": current,
                               "reference": previous, "ratio": current / previous})
    crossings = {}
    for row in ratios:
        if row["ratio"] > policy["preservation"]["repeated_ratio_max"]:
            crossings.setdefault((row["metric"], row["statistic"]), []).append(row)
    repeated = [rows for rows in crossings.values() if len(rows) == len(runs)]
    typing_failed = [r for r in ratios if r["metric"] in ("typing", "typing_miss") and
                     r["statistic"] == "p95" and r["candidate"] > policy["preservation"]["typing_p95_ms_max"]]
    return {"passed": not repeated and not typing_failed, "ratios": ratios,
            "repeated_greater_than_5_percent": repeated,
            "single_run_crossings": [rows[0] for rows in crossings.values() if len(rows) == 1],
            "typing_limit_failures": typing_failed}


def inputs(args):
    paths = {"binary": args.binary, "reference": args.compare_binary, "bash": args.bash,
             "python": sys.executable, "workload": workload.__file__, "observer": __file__, "policy": args.policy}
    for plugin in workload.PLUGINS:
        for name in ("plugin.toml", "plugin.bash"):
            paths[f"plugin/{plugin}/{name}"] = args.plugin_root / plugin / name
    return {name: digest(path) for name, path in paths.items()}


def validate_controls(paths, current, policy):
    if len(paths) != 2 or len({digest(path) for path in paths}) != 2:
        raise ValueError("two distinct control receipts are required")
    runs = [json.loads(path.read_text()) for path in paths]
    for run in runs:
        recorded = run["input_sha256"]
        if recorded["binary"] != recorded["reference"] or recorded["binary"] != current["binary"]:
            raise ValueError("controls must use the current candidate on both labels")
        if set(recorded) != set(current) or any(recorded[key] != value for key, value in current.items() if key != "reference"):
            raise ValueError("control inputs or observer have changed")
        if not run.get("identities_unchanged"):
            raise ValueError("control identity verification is missing")
    assessment = assess_runs(runs, policy)
    if not assessment["passed"]:
        raise ValueError("identical-binary controls fail preservation; actual comparison is held")
    return {"receipts": [{"name": path.name, "sha256": digest(path)} for path in paths],
            "assessment": assessment}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--compare-binary", type=Path, required=True)
    parser.add_argument("--bash", default="/bin/bash")
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--policy", type=Path, default=POLICY)
    parser.add_argument("--controls", type=Path, nargs=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.samples <= 1000 or not Path(args.bash).is_absolute():
        parser.error("samples must be 1..1000 and bash must be absolute")
    cleanup = []
    result = {"schema": 1, "complete": False, "release_qualified": False,
              "python_version": platform.python_version(), "observer_cleanup": cleanup,
              "observer_policy": "automatic GC disabled; explicit collection after closed shells and teardown"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output:
        output.write(json.dumps(result, indent=2) + "\n")

    def save():
        temporary = args.output.with_suffix(args.output.suffix + ".pending")
        with temporary.open("x") as output:
            output.write(json.dumps(result, indent=2) + "\n")
        temporary.replace(args.output)

    try:
        policy = json.loads(args.policy.read_text())
        before = inputs(args)
        result["input_sha256"] = before
        if before["workload"] != policy["workload"]["sha256"]:
            raise ValueError("frozen workload changed")
        if before["binary"] != before["reference"]:
            result["controls"] = validate_controls(args.controls or [], before, policy)
        result["comparison_kind"] = "identical-binary-control" if before["binary"] == before["reference"] else "candidate-reference"
        save()
        scratch = ROOT / "tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="interactive-observer-", dir=scratch) as directory:
            with controlled_observer(workload, cleanup):
                measured = workload.benchmark(args, Path(directory))
        result.update(measured)
        if inputs(args) != before:
            raise ValueError("measurement inputs changed during the run")
        result.update(complete=True, identities_unchanged=True,
                      qualifying_sample_count=args.samples == policy["rounds_per_run"])
        save()
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}".replace(str(ROOT), "$PROJECT").replace(str(args.plugin_root.resolve()), "$PLUGINS")
        save()
        raise
    print(json.dumps({"complete": True, "kind": result["comparison_kind"], "rounds": args.samples,
                      "explicit_collections": len(cleanup),
                      "cleanup_ms": sum(row["milliseconds"] for row in cleanup)}))


if __name__ == "__main__":
    main()
