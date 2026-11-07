#!/usr/bin/env python3
"""Record minimal reproductions of the known shared upstream differences."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import engine  # noqa: E402

CASES = {
    "help-C": ("C", "help"),
    "help-UTF8": ("C.UTF-8", "help"),
    "trap-order": ("C", "trap 'echo USR1' USR1; trap '' TERM; trap -p"),
    "signal-numbers": ("C", "kill -l USR1; kill -l TERM"),
    "printf-inline-precision": ("C", "printf -v v '%.9223372036854775825s\\n' XY; s=$?; printf 'status=%s value=<%s>\\n' \"$s\" \"$v\""),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or args.output.is_symlink():
        raise ValueError("compatibility output must be fresh")
    observations = {}
    with tempfile.TemporaryDirectory(prefix=".scratch-compatibility-", dir=ROOT) as temporary:
        for name in args.name:
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) or name in observations:
                raise ValueError("invalid or duplicate build name")
            binary = engine.CACHE / name / "bash"
            metadata = json.loads((binary.parent / "build.json").read_text())
            if engine.digest(binary) != metadata["binary_sha256"]:
                raise ValueError("compatibility engine digest mismatch")
            cases = {}
            for case, (locale, script) in CASES.items():
                # A common argv[0] makes diagnostics comparable across paths.
                result = subprocess.run(["bash", "--noprofile", "--norc", "-c", script], executable=binary,
                                        env={"PATH": os.defpath, "HOME": temporary, "LC_ALL": locale,
                                             "TERM": "xterm", "COLUMNS": "80", "TZ": "UTC"},
                                        cwd=temporary, capture_output=True, text=True, timeout=10)
                stdout = result.stdout
                if case.startswith("help-"):
                    stdout = "\n".join(line for line in stdout.splitlines() if "coproc [NAME]" in line) + "\n"
                if case == "trap-order":
                    stdout = "\n".join(line for line in stdout.splitlines() if "SIGUSR1" in line or "SIGTERM" in line) + "\n"
                cases[case] = {"status": result.returncode, "stdout": stdout, "stderr": result.stderr}
            observations[name] = {"binary_sha256": metadata["binary_sha256"], "cases": cases}
    reference = observations[args.name[0]]["cases"]
    same = all(row["cases"] == reference for row in observations.values())
    receipt = {"purpose": "known upstream differences; diagnostic only", "upstream_gate_pass": False,
               "matches_reference": same, "engines": observations,
               "source_lock_sha256": engine.digest(engine.LOCK),
               "driver_sha256": engine.digest(Path(__file__)),
               "libc": os.confstr("CS_GNU_LIBC_VERSION")}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"matches_reference": same, "upstream_gate_pass": False, "output": str(output)}))
    if not same:
        raise ValueError("a minimal reproduction differs from reference")


if __name__ == "__main__":
    main()
