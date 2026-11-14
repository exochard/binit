#!/usr/bin/env python3
"""Measure an experimental native wait boundary in real Bash PTYs."""
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import select
import shlex
import subprocess
import time


ROOT = Path(__file__).resolve().parent.parent


def identity(path):
    path = Path(path).resolve()
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "name": path.name}


def wait_file(session, path, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size:
            return
        if select.select([session.terminal.fd], [], [], .0005)[0]:
            data = os.read(session.terminal.fd, 65536)
            session.transcript.extend(data)
            session.stream.feed(session.decoder.decode(data))
    raise AssertionError(f"missing {path.name}; terminal: {session.text!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bash", type=Path, required=True)
    parser.add_argument("--menu-module", type=Path, required=True)
    parser.add_argument("--probe-module", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=40)
    args = parser.parse_args()
    if not 1 <= args.samples <= 1000:
        parser.error("--samples must be between 1 and 1000")
    work = args.work.resolve()
    if not work.is_relative_to(ROOT / "tmp"):
        parser.error("--work must be inside the project's tmp directory")
    work.mkdir(parents=True, exist_ok=True)
    os.environ["BINIT_BASH"] = str(args.bash.resolve())
    os.environ["BINIT_TEST_MODULE"] = str(args.menu_module.resolve())
    spec = importlib.util.spec_from_file_location("completion_probe_session", ROOT / "tests/test_completion.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Session's scratch directory is redirected; its terminal helper was loaded from source.
    module.ROOT = work
    probe = shlex.quote(str(args.probe_module.resolve()))
    worker = shlex.quote(str(args.worker.resolve()))
    cases = {
        "typed suffix": (b"xyz", "probe_cmd pxyz", 14),
        "bracketed paste": (b"\x1b[200~a b\x1b[201~", "probe_cmd pa b", 14),
        "cursor movement": (b"\x02Z", "probe_cmd Zp", 11),
        "coalesced Tab and suffix": (b"xyz", "probe_cmd pxyz", 14),
    }
    rows = []
    result = {"schema": 1, "status": "incomplete", "scope": "feasibility only; synthetic delay, no product qualification",
              "bash_version": subprocess.check_output([str(args.bash.resolve()), "--version"], text=True).splitlines()[0],
              "identities": {"bash": identity(args.bash), "menu": identity(args.menu_module),
                             "probe": identity(args.probe_module), "worker": identity(args.worker),
                             "harness": identity(__file__),
                             "probe_source": identity(ROOT / "benchmarks/probes/eligible_wait.c"),
                             "contract": identity(ROOT / "benchmarks/eligible-completion.json")},
              "rows": rows}
    with args.output.open("x") as output:
        json.dump(result, output, indent=2)

    def save():
        temporary = args.output.with_suffix(".pending")
        with temporary.open("x") as output:
            output.write(json.dumps(result, indent=2) + "\n")
        temporary.replace(args.output)

    session = None
    try:
        for treatment in ("synchronous-control", "input-poll-probe"):
            ready = work / "ready"
            receipt = work / "worker-receipt"
            status_path = work / "provider-status"
            operation = f"{worker} .2" if treatment == "synchronous-control" else f"builtin binit_wait_probe {shlex.quote(str(receipt))} {worker}"
            extra = f"""enable -f {probe} binit_wait_probe
bind 'set enable-bracketed-paste on'
__probe_provider() {{
    printf ready > {shlex.quote(str(ready))}
    COMPREPLY=()
    {operation}
    printf '%s' "$?" > {shlex.quote(str(status_path))}
}}
complete -F __probe_provider probe_cmd
"""
            with module.Session(extra=extra, values=0) as session:
                for case, (payload, expected, point) in cases.items():
                    for sample in range(args.samples):
                        ready.unlink(missing_ok=True)
                        receipt.unlink(missing_ok=True)
                        status_path.unlink(missing_ok=True)
                        session.capture_path.unlink(missing_ok=True)
                        session.send(b"\x01\x0bprobe_cmd p")
                        if case == "coalesced Tab and suffix":
                            started = time.perf_counter_ns()
                            session.send(b"\t" + payload + b"\x18\x07")
                        else:
                            session.send(b"\t")
                            wait_file(session, ready if treatment == "synchronous-control" else receipt)
                            # Allow the known 200 ms worker to enter its wait.
                            time.sleep(.005)
                            started = time.perf_counter_ns()
                            session.send(payload + b"\x18\x07")
                        wait_file(session, session.capture_path)
                        elapsed = (time.perf_counter_ns() - started) / 1e6
                        actual_point, actual_line, _ = session.capture_path.read_bytes().split(b"\0")
                        worker_receipt = receipt.read_text() if receipt.exists() else ""
                        parts = [line.split() for line in worker_receipt.splitlines()]
                        cancelled = (len(parts) == 2 and len(parts[0]) == 2 and parts[0][0] == "spawned" and
                                     len(parts[1]) == 5 and parts[1][0] == "cancelled" and
                                     int(parts[0][1]) > 0 and parts[0][1] == parts[1][1] == parts[1][3] and
                                     parts[1][2] == "0" and parts[1][4] == "9")
                        provider_status = status_path.read_text() if status_path.exists() else None
                        rows.append({"treatment": treatment, "case": case, "sample": sample,
                                     "elapsed_ms": elapsed, "line": actual_line.decode(),
                                     "point": int(actual_point),
                                     "worker_receipt": worker_receipt, "worker_cancelled_and_reaped": cancelled,
                                     "provider_status": provider_status,
                                     "input_preserved": actual_line.decode() == expected and int(actual_point) == point})
                        save()
    except BaseException as error:
        result["error"] = f"{type(error).__name__}: {error}"
        if session is not None:
            result["terminal_tail"] = bytes(session.transcript[-4096:]).decode(errors="replace")
        save()
        raise
    summaries = []
    for treatment in ("synchronous-control", "input-poll-probe"):
        for case in cases:
            selected = [row for row in rows if row["treatment"] == treatment and row["case"] == case]
            times = sorted(row["elapsed_ms"] for row in selected)
            summaries.append({"treatment": treatment, "case": case, "samples": len(times),
                              "p95_ms": times[math.ceil(.95 * len(times)) - 1],
                              "worker_failures": sum(not row["worker_cancelled_and_reaped"] or row["provider_status"] != "1"
                                                     for row in selected) if treatment == "input-poll-probe" else
                                                 sum(row["provider_status"] != "0" for row in selected),
                              "input_failures": sum(not row["input_preserved"] for row in selected)})
    failed = any(row["input_failures"] or row["worker_failures"] or
                 (row["p95_ms"] > 16 if row["treatment"] == "input-poll-probe" else row["p95_ms"] < 150)
                 for row in summaries)
    result.update(status="failed" if failed else "passed", summaries=summaries)
    save()
    print(json.dumps(summaries, indent=2))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
