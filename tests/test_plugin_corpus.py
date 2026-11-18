"""The plugin corpus runner applies the frozen passing-set rule and covers exactly the frozen manifest."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_plugin_corpus", ROOT / "scripts/check_plugin_corpus.py")
corpus = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(corpus)
FRAMEWORKS = ["binit", "bash-it", "oh-my-bash"]


def results(sets):
    return [{"framework": framework, "plugin": plugin, "passed": plugin in passing}
            for framework, passing in sets.items() for plugin in ("a", "b", "c")]


class PluginCorpusTests(unittest.TestCase):
    def test_runner_covers_exactly_the_frozen_manifest(self):
        manifest = json.loads((ROOT / "benchmarks/plugin-corpus.json").read_text())
        self.assertEqual(set(corpus.EFFECTS), set(manifest["plugins"]))
        self.assertEqual(set(corpus.INSTALL), set(manifest["frameworks"]))

    def test_passing_set_rule(self):
        cases = [
            ({"binit": "ab", "bash-it": "ab", "oh-my-bash": "ab"}, "tie"),
            ({"binit": "abc", "bash-it": "ab", "oh-my-bash": "a"}, "win"),
            ({"binit": "abc", "bash-it": "abc", "oh-my-bash": "a"}, "inconclusive"),
            ({"binit": "ab", "bash-it": "c", "oh-my-bash": ""}, "loss"),
            ({"binit": "abc", "bash-it": "", "oh-my-bash": "abc"}, "inconclusive"),
        ]
        for sets, outcome in cases:
            with self.subTest(sets=sets):
                self.assertEqual(corpus.verdict(results(sets), FRAMEWORKS)["outcome"], outcome)


if __name__ == "__main__":
    unittest.main()
