#!/usr/bin/env python3
"""Prototype: review a local plugin update through the existing candidate flow before any shell uses it.

stage: write a proposed configuration that points PLUGIN's entry at NEW_SOURCE, show which of the
plugin's files change, prepare it as a candidate (nothing is selected), run the reviewed check plan on the
selected generation (binit diagnose) and on the candidate (binit candidate check), and print both side by
side. select: select the candidate only through `binit candidate select`, which refuses a stale receipt,
a failed check or a changed selection. No new manager command or format is introduced."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib


def binit(binary, state, *args, check=True):
    result = subprocess.run([binary, *args, "--state", str(state)], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"binit {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result


def files(root):
    root = Path(root)
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def plugin_id(source):
    return tomllib.loads((Path(source) / "plugin.toml").read_text())["id"]


def proposed_config(config, plugin, new_source):
    config = Path(config).resolve()
    text = config.read_text()
    entries = tomllib.loads(text)["plugins"]
    paths = [e["path"] for e in entries if plugin_id(config.parent / e["path"]) == plugin]
    if len(paths) != 1:
        raise SystemExit(f"{plugin}: expected one configuration entry, found {len(paths)}")
    old = paths[0]
    pattern = re.compile(r'^(\s*path\s*=\s*)(["\'])' + re.escape(old) + r'\2\s*$', re.M)
    if len(pattern.findall(text)) != 1:
        raise SystemExit(f"{plugin}: path {old!r} is not a single plain line")
    proposed = pattern.sub(lambda m: m.group(1) + json.dumps(str(Path(new_source).resolve())), text)
    target = config.with_name(f".{config.stem}.update-{plugin}.toml")
    target.write_text(proposed)
    return config.parent / old, target


def summary(samples):
    by_check = {}
    for sample in samples:
        entry = by_check.setdefault(sample["check"], {"passed": True, "elapsed_ms": []})
        entry["passed"] &= sample.get("check_return") == 0 and sample.get("phase") == "complete" and not sample.get("timed_out")
        entry["elapsed_ms"].append(sample.get("elapsed_ms"))
    return by_check


def stage(args):
    old_source, proposed = proposed_config(args.config, args.plugin, args.new_source)
    before, after = files(old_source), files(args.new_source)
    changed = {"added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after)),
               "changed": sorted(k for k in before.keys() & after.keys() if before[k] != after[k])}
    selected = json.loads(binit(args.binit, args.state, "diagnose", str(args.checks), "--json", check=False).stdout)
    binit(args.binit, args.state, "candidate", "prepare", str(proposed))
    checked = binit(args.binit, args.state, "candidate", "check", str(args.checks), check=False)
    status = json.loads(binit(args.binit, args.state, "candidate", "status", "--json").stdout)
    receipt = {}
    run = status.get("check") or {}
    if run.get("receipt_sha256"):
        data = (args.state / "check-runs" / run["id"] / "receipt.json").read_bytes()
        if hashlib.sha256(data).hexdigest() != run["receipt_sha256"]:
            raise SystemExit("candidate check receipt changed")
        receipt = json.loads(data)
    report = {
        "plugin": args.plugin, "proposed_config": str(proposed), "source_files": changed,
        "selected": summary(selected.get("receipt", {}).get("samples", [])),
        "selected_functions": {fn: owner for d in selected.get("diagnostics", [])
                               for fn, owner in d.get("namespace", {}).get("functions", {}).items() if owner == args.plugin},
        "candidate": summary(receipt.get("samples", [])),
        "candidate_state": status.get("state"), "check_exit": checked.returncode,
        "not_shown": "the candidate's owned functions and trace: diagnose runs only on the selected generation",
    }
    report["regressions"] = sorted(c for c, r in report["selected"].items()
                                   if r["passed"] and not report["candidate"].get(c, {}).get("passed"))
    print(json.dumps(report, indent=2))
    return 0 if report["candidate_state"] == "checked" and not report["regressions"] else 1


def select(args):
    result = binit(args.binit, args.state, "candidate", "select", check=False)
    sys.stderr.write(result.stderr)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binit", default="binit")
    parser.add_argument("--state", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    staged = commands.add_parser("stage")
    staged.add_argument("config", type=Path)
    staged.add_argument("plugin")
    staged.add_argument("new_source", type=Path)
    staged.add_argument("checks", type=Path)
    commands.add_parser("select")
    args = parser.parse_args()
    return stage(args) if args.command == "stage" else select(args)


if __name__ == "__main__":
    sys.exit(main())
