#!/usr/bin/env python3
"""Summarize diagnostic CPU stacks, process activity and root-process heap data."""

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import re
import statistics
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import benchmark  # noqa: E402
import engine  # noqa: E402

CATEGORIES = [
    ("descriptor bookkeeping", r"^(new_fd_bitmap|dispose_fd_bitmap|close_fd_bitmap)$"),
    ("unwind bookkeeping", r"unwind"),
    ("command rendering", r"^(cprintf|make_command_string.*|print_arith_command|print_simple_command|_?print_word_list|the_printed_command_resize)$"),
    ("environment export", r"^(maybe_make_export_env|make_.*export.*|make_env_array|mk_env_string)$"),
    ("arithmetic evaluation", r"^(evalexp|eval_arith_for_expr|subexpr|readtok|expr_.*|arith_decimal|expand_arith_string|exp(comma|assign|cond|lor|land|bor|bxor|band|eq|compare|shift|addsub|muldiv|power|unary|0))$"),
    ("lookup and binding", r"^(find_.*variable.*|bind_.*variable.*|make_variable_value|hash_search|hash_string|find_shell_builtin|builtin_address_internal)$"),
    ("word objects and strings", r"^(alloc_word_desc|make_.*word.*|copy_word.*|dispose_word.*|dispose_words|strvec_.*|list_string|sub_append_string|substring)$"),
    ("expansion and quoting", r"^(expand_.*|.*_expand.*|dequote.*|quote_.*|.*quote_removal.*|extract_.*|skip_.*)$"),
    ("pattern matching", r"^(.*gmatch.*|.*smatch.*|.*strmatch.*|sh_regmatch|regcomp|regexec|glob_.*)$"),
    ("shell parsing", r"^(yyparse|yylex|read_token.*|parse_matched_pair|parse_comsub|parse_command)$"),
    ("process control", r"^(make_child|execute_disk_command|shell_execve|wait_for.*|waitchld|execute_pipeline|do_piping)$"),
    ("builtin execution", r".*_builtin$"),
    ("command framework", r"^execute_.*"),
]
ALLOCATOR = re.compile(r"^(malloc|cfree|free|calloc|realloc|reallocarray|_int_.*|__libc_.*alloc|xmalloc|xrealloc|xreallocarray)$")


def normalize(name):
    return re.split(r"\.(?:constprop|isra|lto_priv|part|cold)(?:\.|$)", name)[0]


def category(name):
    for label, pattern in CATEGORIES:
        if re.search(pattern, name):
            return label
    return None


def parse_stacks(text, binary):
    total = 0
    bash_samples = 0
    unresolved = 0
    rooted = 0
    groups = Counter()
    inclusive = Counter()
    self_cost = Counter()
    allocator_owners = Counter()
    allocator_weight = 0
    comms = Counter()
    pipelines = Counter()
    unknown_leaf = 0
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if not lines:
            continue
        header = re.match(r"^(.*?)\s+(\d+)\s+(\d+)\s+(?:cpu-clock|cycles):u:\s*$", lines[0])
        if not header:
            raise ValueError(f"unknown perf sample header: {lines[0][:120]}")
        comm, _, weight = header.groups()
        comm = comm.strip()
        weight = int(weight)
        comms[comm] += weight
        if comm != "bash":
            continue
        total += weight
        bash_samples += 1
        frames = []
        for line in lines[1:]:
            match = re.match(r"\s*[0-9a-f]+\s+(.*?)\s+\((.*)\)\s*$", line)
            if match:
                frames.append([normalize(match[1]), match[2]])
        module = None
        for frame in reversed(frames):
            if frame[1] == "inlined":
                frame[1] = module
            else:
                module = frame[1]
        owned = [name for name, obj in frames if obj == str(binary)]
        if not owned:
            groups["unresolved Bash stack"] += weight
            unresolved += weight
            continue
        if "main" in owned or "_start" in owned:
            rooted += weight
        for name in set(owned):
            inclusive[name] += weight
        names = set(owned)
        arithmetic = bool(names & {"eval_arith_for_expr", "execute_arith_command", "evalexp"})
        if arithmetic:
            pipelines["arithmetic execution including expansion"] += weight
        if "evalexp" in names:
            pipelines["arithmetic evaluator including dynamic lookups"] += weight
        if names & {"expand_word_internal", "expand_word_list_internal", "call_expand_word_internal"}:
            pipelines["word expansion including nested evaluation"] += weight
        if arithmetic and "evalexp" not in names:
            pipelines["arithmetic outside evaluator"] += weight
            # This union includes necessary expansion and error handling: it is
            # an optimistic cost envelope, not a removable-cost estimate.
            if not any(category(name) == "command rendering" for name in owned) and not names & {"run_debug_trap", "run_trap_internal", "xtrace_print_arith_cmd"}:
                pipelines["arithmetic preparation envelope"] += weight
        if frames:
            self_cost[frames[0][0]] += weight
            if frames[0][0] == "[unknown]":
                unknown_leaf += weight
        label = next((category(name) for name in owned if category(name)), "other Bash/library work")
        groups[label] += weight
        if any(ALLOCATOR.match(name) for name, _ in frames):
            allocator_weight += weight
            owner = next((name for name in owned if not ALLOCATOR.match(name)), "unresolved")
            allocator_owners[owner] += weight
    if not total:
        return {"bash_samples": 0, "sample_period_sum": 0,
                "coverage_warning": "no Bash user-space samples; do not infer zero cost",
                "exclusive_categories_percent": {}, "inclusive_functions": [], "pipeline_percent": {},
                "self_functions": [], "allocator_owners": [],
                "allocator_stack_percent": None, "unresolved_percent": None,
                "rooted_percent": None, "unknown_leaf_percent": None, "all_sampled_comms": dict(comms)}
    percent = lambda count: 100 * count / total
    top = lambda counter: [{"name": name, "percent": percent(weight)} for name, weight in counter.most_common(25)]
    return {"bash_samples": bash_samples, "sample_period_sum": total,
            "unresolved_percent": percent(unresolved), "rooted_percent": percent(rooted),
            "unknown_leaf_percent": percent(unknown_leaf),
            "exclusive_categories_percent": {k: percent(v) for k, v in groups.items()},
            "inclusive_functions": top(inclusive), "self_functions": top(self_cost),
            "pipeline_percent": {k: percent(v) for k, v in pipelines.items()},
            "coverage_warning": "fewer than 200 Bash samples; short-child bias can dominate" if bash_samples < 200 else None,
            "allocator_stack_percent": percent(allocator_weight), "allocator_owners": top(allocator_owners),
            "all_sampled_comms": dict(comms)}


def source_index(source):
    result = defaultdict(list)
    for path in source.rglob("*.c"):
        lines = path.read_text(errors="replace").splitlines()
        functions = []
        for i, line in enumerate(lines[:-1]):
            match = re.match(r"^([A-Za-z_][A-Za-z_0-9]*)\s*\(", line)
            if match:
                following = "\n".join(lines[i:min(i+12, len(lines))])
                if re.match(r"^[^;{]+\)\s*\n\{", following):
                    functions.append((i+1, match[1]))
        result[path.name].append(functions)
    return result


def resolve_frame(frame, sources):
    match = re.match(r"0x[0-9A-Fa-f]+:\s+(.*?)\s+\((.*)\)$", frame)
    if not match:
        return None
    name, location = match.groups()
    if name != "UnknownInlinedFun":
        return normalize(name)
    at = re.match(r"(?:.*/)?([^/:]+\.c):(\d+)", location)
    if not at or len(sources[at[1]]) != 1:
        return None
    candidates = [name for line, name in sources[at[1]][0] if line <= int(at[2])]
    return candidates[-1] if candidates else None


def parse_heap(doc, sources):
    blocks = sum(point["tbk"] for point in doc["pps"])
    allocated = sum(point["tb"] for point in doc["pps"])
    owners = defaultdict(lambda: {"blocks": 0, "bytes": 0, "lifetime_instructions": 0})
    points = []
    for point in doc["pps"]:
        stack = [doc["ftbl"][i] for i in point["fs"]]
        names = [resolve_frame(frame, sources) for frame in stack]
        owner = next((name for name in names if name and not ALLOCATOR.match(name) and name != "[root]"), "unresolved")
        owners[owner]["blocks"] += point["tbk"]
        owners[owner]["bytes"] += point["tb"]
        owners[owner]["lifetime_instructions"] += point.get("tl", 0)
        points.append({"owner": owner, "blocks": point["tbk"], "bytes": point["tb"],
                       "average_lifetime_instructions": point.get("tl", 0) / point["tbk"] if point["tbk"] else None,
                       "stack": stack[:12]})
    for value in owners.values():
        value["blocks_percent"] = 100 * value["blocks"] / blocks
        value["bytes_percent"] = 100 * value["bytes"] / allocated
    return {"scope": "root process only", "total_blocks": blocks, "total_allocated_bytes": allocated,
            "owner_limit": "UnknownInlinedFun owners inferred from enclosing source definitions are approximate",
            "lifetime_units": "guest instructions, not elapsed time", "program_instructions": doc["te"],
            "owners": dict(sorted(owners.items(), key=lambda p: p[1]["blocks"], reverse=True)[:25]),
            "largest_allocation_points": sorted(points, key=lambda p: p["blocks"], reverse=True)[:15]}


def process_counts(text, binary):
    complete = []
    pending = {}
    for line in text.splitlines():
        row = re.match(r"^(\d+)\s+(.*)$", line)
        if not row:
            continue
        pid, body = row.groups()
        if "<unfinished ...>" in body:
            if pid in pending:
                raise ValueError("duplicate unfinished process syscall")
            pending[pid] = body.split("<unfinished ...>", 1)[0]
            continue
        resumed = re.match(r"<\.\.\. (\w+) resumed>(.*)$", body)
        if resumed:
            if pid not in pending:
                raise ValueError("resumed process syscall has no matching entry")
            if not pending[pid].startswith(resumed[1] + "("):
                raise ValueError("resumed process syscall name mismatch")
            body = pending.pop(pid) + resumed[2]
        complete.append(body)
    if pending:
        raise ValueError("unfinished process syscalls remain in completed trace")
    joined = "\n".join(complete)
    executions = re.findall(r'execve\("([^"\n]+)".*?\)\s+=\s+0', joined)
    forks = re.findall(r'(?:clone3?|fork|vfork)\(.*?\)\s+=\s+([1-9][0-9]*)', joined)
    external = Counter(path for path in executions if path != str(binary))
    return {"successful_external_execs": dict(external), "external_exec_count": sum(external.values()),
            "fork_count": len(forks), "cpu_partition_warning": "descendants include Bash subshells and external programs"}


def summarize(root, stack_root=None):
    inputs = json.loads((root / "inputs.json").read_text())
    _, lock = benchmark.load_lock()
    if inputs["suite_lock_sha256"] != engine.digest(benchmark.LOCK):
        raise ValueError("diagnostic suite lock mismatch")
    if inputs["source_lock_sha256"] != engine.digest(engine.LOCK) or inputs["training_sha256"] != engine.digest(ROOT / "engines/bash/train.bash"):
        raise ValueError("diagnostic source or training lock mismatch")
    receipt = json.loads((root / "native/receipt.json").read_text())
    expected = receipt["case_counts"]
    selected = [case for case in lock["cases"] if case["name"] in expected]
    if {case["name"]: case["count"] for case in selected} != expected or receipt["inputs"] != inputs:
        raise ValueError("native diagnostic contract mismatch")
    records = json.loads((root / "native/records.json").read_text())
    if not records or {row["case"] for row in records} != set(expected):
        raise ValueError("native diagnostic cases mismatch")
    binary = Path(records[0]["clean"]["command"][0])
    if engine.digest(binary) != inputs["build"]["binary_sha256"]:
        raise ValueError("diagnostic binary mismatch")
    source = binary.parent.with_name(binary.parent.name + "-source")
    if not source.is_dir() and "version" in inputs["build"]:
        source = engine.CACHE / ("source-" + inputs["build"]["version"])
    if not source.is_dir() or engine.source_digest(source) != inputs["build"]["source_tree_sha256"]:
        raise ValueError("diagnostic source tree mismatch")
    sources = source_index(source)
    selected_stacks = stack_root or root
    if json.loads((selected_stacks / "inputs.json").read_text()) != inputs:
        raise ValueError("stack and native diagnostic inputs differ")
    stages = {"native": root / "native", "stacks": selected_stacks / "stacks",
              "heap": root / "heap", "trace": root / "trace"}
    provenance = {}
    for name, stage in stages.items():
        if not stage.exists():
            continue
        stage_receipt = json.loads((stage / "receipt.json").read_text())
        if stage_receipt["inputs"] != inputs or stage_receipt["case_counts"] != expected:
            raise ValueError("diagnostic stage inputs differ")
        provenance[name] = {"receipt": stage_receipt, "files_sha256": {
            str(path.relative_to(stage)): engine.digest(path)
            for path in sorted(stage.rglob("*")) if path.is_file()}}
    cases = {}
    for case in selected:
        name = case["name"]
        rows = [row for row in records if row["case"] == name]
        if len(rows) != receipt["pairs"] or {row["sample"] for row in rows} != set(range(receipt["pairs"])):
            raise ValueError("incomplete native diagnostic pairs")
        for row in rows:
            if row["clean"]["command"][0] != str(binary) or not all(row[mode]["behavior_matches"] and row[mode]["observation"] == case["expected"] for mode in ("clean", "root-clock")):
                raise ValueError("native diagnostic behavior mismatch")
            values = [row["clean"][key] for key in ("elapsed_seconds", "tree_user_seconds", "tree_system_seconds")]
            values.append(row["root-clock"]["root_task_clock_seconds"])
            if not all(type(value) in (int, float) and math.isfinite(value) and value >= 0 for value in values) or values[0] == 0 or values[-1] == 0:
                raise ValueError("invalid diagnostic duration")
        clean = lambda key: statistics.median(row["clean"][key] for row in rows)
        user, system = clean("tree_user_seconds"), clean("tree_system_seconds")
        root_clock = statistics.median(row["root-clock"]["root_task_clock_seconds"] for row in rows)
        native = {"elapsed_seconds": clean("elapsed_seconds"), "tree_user_cpu_seconds": user,
                  "tree_system_cpu_seconds": system, "root_task_clock_seconds": root_clock,
                  "root_to_tree_cpu_ratio": root_clock/(user+system),
                  "raw_pairs": [{"sample": row["sample"], "elapsed_seconds": row["clean"]["elapsed_seconds"],
                                 "tree_user_cpu_seconds": row["clean"]["tree_user_seconds"],
                                 "tree_system_cpu_seconds": row["clean"]["tree_system_seconds"],
                                 "root_task_clock_seconds": row["root-clock"]["root_task_clock_seconds"]} for row in rows],
                  "warning": "root and whole-tree CPU come from separate paired invocations; ratio may slightly exceed 1"}
        stack = selected_stacks / "stacks" / name / "stacks.txt"
        trace = root / "trace" / (name + ".trace")
        heap = root / "heap" / (name + ".heap.json")
        cases[name] = {"family": case["family"], "count": case["count"], "native": native}
        if stack.exists():
            cases[name]["stacks"] = parse_stacks(stack.read_text(), binary)
            profiles = stack.parent / "profiles.json"
            if profiles.exists():
                quality = json.loads(profiles.read_text())
                accepted = [row for row in quality if row["accepted"]]
                for row in accepted:
                    if "lost" in (stack.parent / f"unwind-{row['invocation']}.log").read_text():
                        raise ValueError("accepted stack profile has lost records")
                aggregate = "".join((stack.parent / f"stacks-{row['invocation']}.txt").read_text() for row in accepted)
                if aggregate != stack.read_text():
                    raise ValueError("stack aggregate differs from accepted profiles")
                cases[name]["stacks"]["profiles"] = quality
                cases[name]["stacks"]["accepted_profile_count"] = len(accepted)
        if trace.exists():
            cases[name]["processes"] = process_counts(trace.read_text(), binary)
        if heap.exists():
            cases[name]["heap"] = parse_heap(json.loads(heap.read_text()), sources)
    models = {}
    for target in ("arithmetic preparation envelope", "arithmetic evaluator including dynamic lookups",
                   "word expansion including nested evaluation"):
        values = {}
        for removed in (.5, 1.0):
            ratios = []
            for case in cases.values():
                share = 0
                if case.get("processes", {}).get("fork_count") == 0:
                    share = case.get("stacks", {}).get("pipeline_percent", {}).get(target, 0) / 100
                native = case["native"]
                ratio = 1 - removed * share * native["tree_user_cpu_seconds"] / native["elapsed_seconds"]
                if not 0 < ratio <= 1:
                    raise ValueError("serial model exceeds measured elapsed time")
                ratios.append(ratio)
            values[str(removed)] = math.exp(statistics.mean(math.log(ratio) for ratio in ratios))
        models[target] = values
    return {"status": "diagnostic-only", "release_gate_pass": False, "inputs": inputs,
            "provenance": provenance, "cases": cases,
            "serial_cost_models": {"assumptions": "Bash user cycle shares scale with user CPU; removing attributed work directly subtracts elapsed time in cases with no forks; all fork cases unchanged. Equal case weights. Cache, scheduling and profiler effects excluded. Illustrations, not bounds or forecasts.",
                                   "ratios_by_fraction_of_attributed_work_removed": models},
            "method_limits": ["Stacks sample user-space cycles or CPU-clock; short descendant processes can be undersampled.",
                              "Bash stacks include Bash descendants; heap profiles cover only the root process.",
                              "Inclusive function percentages overlap; exclusive categories use the nearest matching Bash frame.",
                              "DHAT allocation totals cover only the root process, excluding inherited child profiles.",
                              "Instruction counts, allocated bytes and CPU shares are not elapsed-time speedup predictions."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stack-root", type=Path)
    args = parser.parse_args()
    result = summarize(args.root.resolve(), args.stack_root.resolve() if args.stack_root else None)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
