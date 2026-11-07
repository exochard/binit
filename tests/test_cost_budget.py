import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "engines/bash/diagnostics" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


summary = module("summarize_cost")
collector = module("cost_budget")


class CostBudgetTests(unittest.TestCase):
    def sample(self, comm, weight, frames):
        return f"{comm} 123 {weight} cycles:u:\n" + "\n".join(
            f"  abc {name} ({obj})" for name, obj in frames) + "\n\n"

    def test_stack_weights_inline_frames_and_recursion(self):
        binary = Path("/engine/bash")
        text = self.sample("bash", 3, [("malloc", "/libc"), ("make_word.constprop.1", "inlined"),
                                      ("eval_arith_for_expr", str(binary)), ("main", str(binary))])
        text += self.sample("bash", 7, [("readtok", str(binary)), ("evalexp", str(binary)),
                                       ("evalexp", str(binary)), ("main", str(binary))])
        text += self.sample("cat", 90, [("main", "/bin/cat")])
        result = summary.parse_stacks(text, binary)
        self.assertEqual(result["bash_samples"], 2)
        self.assertEqual(result["sample_period_sum"], 10)
        self.assertEqual(result["allocator_stack_percent"], 30)
        self.assertEqual(result["allocator_owners"], [{"name": "make_word", "percent": 30}])
        functions = {row["name"]: row["percent"] for row in result["inclusive_functions"]}
        self.assertEqual(functions["evalexp"], 70)
        self.assertEqual(result["pipeline_percent"]["arithmetic execution including expansion"], 100)
        self.assertEqual(result["pipeline_percent"]["arithmetic preparation envelope"], 30)
        self.assertAlmostEqual(sum(result["exclusive_categories_percent"].values()), 100)

    def test_absent_and_unresolved_stacks_are_explicit(self):
        text = self.sample("cat", 2, [("main", "/bin/cat")])
        result = summary.parse_stacks(text, Path("/engine/bash"))
        self.assertIsNone(result["allocator_stack_percent"])
        self.assertIn("no Bash", result["coverage_warning"])
        text = self.sample("bash", 1, [("unknown", "[unknown]")])
        self.assertEqual(summary.parse_stacks(text, Path("/engine/bash"))["unresolved_percent"], 100)

    def test_process_interleaving_rejoins_before_counting(self):
        text = '''1 execve("/engine/bash", [], []) = 0
1 clone(child_stack=NULL, flags=SIGCHLD <unfinished ...>
2 execve("/bin/cat", [], [] <unfinished ...>
1 <... clone resumed>) = 2
2 <... execve resumed>) = 0
1 fork() = 3
3 execve("/missing", [], []) = -1 ENOENT (No such file or directory)
3 execve("/bin/tail", [], []) = 0
'''
        result = summary.process_counts(text, Path("/engine/bash"))
        self.assertEqual(result["fork_count"], 2)
        self.assertEqual(result["external_exec_count"], 2)
        self.assertEqual(result["successful_external_execs"], {"/bin/cat": 1, "/bin/tail": 1})

    def test_incomplete_or_mismatched_process_traces_fail(self):
        for text in ('1 fork( <unfinished ...>\n', '1 <... fork resumed>) = 2\n',
                     '1 fork( <unfinished ...>\n1 <... execve resumed>) = 0\n',
                     '1 fork( <unfinished ...>\n1 clone( <unfinished ...>\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                summary.process_counts(text, Path("/engine/bash"))

    def test_heap_lifetime_and_inferred_inline_owner(self):
        doc = {"te": 1000, "ftbl": ["[root]", "0x123: malloc (malloc.c:1)",
                "0x456: UnknownInlinedFun (subst.c:25)"],
               "pps": [{"tbk": 2, "tb": 32, "tl": 100, "fs": [1, 2, 0]}]}
        from collections import defaultdict
        sources = defaultdict(list, {"subst.c": [[(10, "previous"), (20, "expand_word_internal")]]})
        result = summary.parse_heap(doc, sources)
        self.assertEqual(result["owners"]["expand_word_internal"]["blocks"], 2)
        self.assertEqual(result["largest_allocation_points"][0]["average_lifetime_instructions"], 50)
        self.assertIn("not elapsed", result["lifetime_units"])
        self.assertIn("approximate", result["owner_limit"])

    def test_task_clock_requires_valid_unmultiplexed_counter(self):
        with tempfile.TemporaryDirectory(prefix=".scratch-cost-test-", dir=ROOT) as temporary:
            path = Path(temporary) / "counter"
            row = {"event": "task-clock:u", "unit": "msec", "pcnt-running": "100", "counter-value": "25"}
            path.write_text(json.dumps(row))
            self.assertEqual(collector.taskclock(path), .025)
            for key, value in (("unit", "seconds"), ("pcnt-running", "80"),
                               ("pcnt-running", "nan"), ("counter-value", "nan"), ("counter-value", "-1")):
                path.write_text(json.dumps({**row, key: value}))
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    collector.taskclock(path)

    def test_selected_case_pair_contract_and_incomplete_rejection(self):
        with tempfile.TemporaryDirectory(prefix=".scratch-cost-test-", dir=ROOT) as temporary:
            root = Path(temporary)
            binary = root / "engine/bash"
            binary.parent.mkdir()
            binary.write_text("fixture")
            source = root / "engine-source"
            source.mkdir()
            inputs = {"suite_lock_sha256": summary.engine.digest(summary.benchmark.LOCK),
                      "source_lock_sha256": summary.engine.digest(summary.engine.LOCK),
                      "training_sha256": summary.engine.digest(ROOT / "engines/bash/train.bash"),
                      "build": {"binary_sha256": summary.engine.digest(binary),
                                "source_tree_sha256": summary.engine.source_digest(source)}}
            (root / "inputs.json").write_text(json.dumps(inputs))
            native = root / "native"
            native.mkdir()
            case = summary.benchmark.load_lock()[1]["cases"][0]
            receipt = {"inputs": inputs, "pairs": 2, "case_counts": {case["name"]: case["count"]}}
            (native / "receipt.json").write_text(json.dumps(receipt))
            result = {"command": [str(binary)], "behavior_matches": True, "observation": case["expected"],
                      "elapsed_seconds": .03, "tree_user_seconds": .02, "tree_system_seconds": .01,
                      "root_task_clock_seconds": .031}
            rows = [{"case": case["name"], "sample": sample, "clean": result, "root-clock": result}
                    for sample in range(2)]
            records = native / "records.json"
            records.write_text(json.dumps(rows))
            report = summary.summarize(root)
            self.assertEqual(list(report["cases"]), [case["name"]])
            self.assertFalse(report["release_gate_pass"])
            self.assertGreater(report["cases"][case["name"]]["native"]["root_to_tree_cpu_ratio"], 1)
            records.write_text(json.dumps([rows[0], rows[0]]))
            with self.assertRaises(ValueError):
                summary.summarize(root)
            records.write_text(json.dumps(rows))
            for value in (True, float("nan"), -1):
                rows[0]["clean"]["elapsed_seconds"] = value
                records.write_text(json.dumps(rows))
                with self.subTest(value=value), self.assertRaises(ValueError):
                    summary.summarize(root)
            rows[0]["clean"]["elapsed_seconds"] = .03
            records.write_text(json.dumps(rows))
            stage = root / "stacks"
            directory = stage / case["name"]
            directory.mkdir(parents=True)
            (stage / "receipt.json").write_text(json.dumps(receipt))
            stack = self.sample("bash", 1, [("evalexp", str(binary))])
            (directory / "stacks.txt").write_text(stack)
            (directory / "stacks-0.txt").write_text(stack)
            (directory / "profiles.json").write_text(json.dumps([{"invocation": 0, "accepted": True}]))
            (directory / "unwind-0.log").write_text("Warning: lost 1 chunks!")
            with self.assertRaisesRegex(ValueError, "lost records"):
                summary.summarize(root)
            (directory / "unwind-0.log").write_text("")
            (directory / "stacks.txt").write_text(stack + stack)
            with self.assertRaisesRegex(ValueError, "aggregate"):
                summary.summarize(root)
            (directory / "stacks.txt").write_text(stack)
            self.assertEqual(summary.summarize(root)["cases"][case["name"]]["stacks"]["accepted_profile_count"], 1)


if __name__ == "__main__":
    unittest.main()
