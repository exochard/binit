"""Keep observer cleanup outside timed work and reject incomplete controls."""
import copy
import gc
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bench_interactive_observer as observer


class ObserverStateTests(unittest.TestCase):
    def test_collection_follows_close_and_restores_caller_state(self):
        events = []

        class Shell:
            def settled(self):
                events.append(('timed', gc.isenabled()))

            def close(self):
                events.append(('closed', gc.isenabled()))

        module = SimpleNamespace(Shell=Shell)
        previous = gc.isenabled()
        try:
            for enabled in (False, True):
                gc.enable() if enabled else gc.disable()
                for failure in (False, True):
                    events.clear()
                    cleanup = []
                    with patch.object(observer.gc, 'collect', side_effect=lambda: events.append(('gc', gc.isenabled())) or 3):
                        try:
                            with observer.controlled_observer(module, cleanup):
                                self.assertIs(module.Shell.settled, Shell.settled)
                                shell = module.Shell()
                                shell.settled()
                                shell.close()
                                if failure:
                                    raise RuntimeError('workload failed')
                        except RuntimeError as error:
                            self.assertEqual(str(error), 'workload failed')
                    self.assertIs(module.Shell, Shell)
                    self.assertEqual(gc.isenabled(), enabled)
                    self.assertEqual(events, [('timed', False), ('closed', False), ('gc', False), ('gc', False)])
                    self.assertEqual([row['event'] for row in cleanup], ['closed-shell', 'teardown'])
                    self.assertTrue(all(row['collected'] == 3 and row['milliseconds'] >= 0 for row in cleanup))
        finally:
            gc.enable() if previous else gc.disable()

    def test_close_and_collector_errors_restore_class_and_gc(self):
        class Shell:
            def close(self):
                raise RuntimeError('close failed')

        module = SimpleNamespace(Shell=Shell)
        previous = gc.isenabled()
        try:
            for enabled in (False, True):
                for collector_fails in (False, True):
                    gc.enable() if enabled else gc.disable()
                    with patch.object(observer.gc, 'collect', side_effect=RuntimeError('collect failed') if collector_fails else None, return_value=0):
                        with self.assertRaisesRegex(RuntimeError, 'collect failed' if collector_fails else 'close failed'):
                            with observer.controlled_observer(module, []):
                                module.Shell().close()
                    self.assertIs(module.Shell, Shell)
                    self.assertEqual(gc.isenabled(), enabled)
        finally:
            gc.enable() if previous else gc.disable()


class ObserverEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.policy = json.loads(observer.POLICY.read_text())

    def run_fixture(self):
        rounds = self.policy['rounds_per_run']
        raw = {}
        profiles = []
        for scenario in ('bare_bash', 'binit_empty', 'binit_seven_plugins', 'binit_seven_before'):
            metrics = observer.METRICS if scenario.startswith('binit_seven_') else observer.METRICS[:-2]
            raw[scenario] = {metric: [1.0] * (rounds * (11 if metric == 'typing' else 16 if metric == 'typing_miss' else 1))
                             for metric in metrics}
            if scenario.startswith('binit_seven_'):
                profile = dict(scenario=scenario,
                               before_git_completion={p: dict(state='pending' if p == 'git-completion' else 'loaded') for p in observer.workload.PLUGINS},
                               after_git_completion={p: dict(state='loaded') for p in observer.workload.PLUGINS})
                profiles.extend(copy.deepcopy(profile) for _ in range(rounds))
        identities = {key: 'a' * 64 for key in ('binary', 'reference', 'bash', 'python', 'workload', 'observer', 'policy')}
        identities.update({f'plugin/{plugin}/{name}': 'a' * 64 for plugin in observer.workload.PLUGINS
                           for name in ('plugin.toml', 'plugin.bash')})
        return dict(complete=True, pairs=rounds, raw=raw, profiles=profiles,
                    input_sha256=identities, identities_unchanged=True)

    def test_runs_with_different_or_unverified_inputs_are_rejected(self):
        for key in ('binary', 'reference', 'workload', 'observer', 'policy'):
            runs = [self.run_fixture(), self.run_fixture()]
            runs[1]['input_sha256'][key] = 'b' * 64
            with self.assertRaisesRegex(ValueError, 'identities differ'):
                observer.assess_runs(runs, self.policy)
        runs = [self.run_fixture(), self.run_fixture()]
        runs[1]['identities_unchanged'] = False
        with self.assertRaisesRegex(ValueError, 'not verified'):
            observer.assess_runs(runs, self.policy)
        for run in runs:
            del run['input_sha256']['bash']
        with self.assertRaisesRegex(ValueError, 'complete input identities'):
            observer.assess_runs(runs, self.policy)

    def test_repeated_tail_regression_fails_and_single_crossing_stays_visible(self):
        runs = [self.run_fixture(), self.run_fixture()]
        self.assertTrue(observer.assess_runs(runs, self.policy)['passed'])
        runs[0]['raw']['binit_seven_plugins']['first_history'][-6:] = [2.0] * 6
        assessment = observer.assess_runs(runs, self.policy)
        self.assertTrue(assessment['passed'])
        self.assertEqual(len(assessment['single_run_crossings']), 1)
        runs[1]['raw']['binit_seven_plugins']['first_history'][-6:] = [2.0] * 6
        self.assertFalse(observer.assess_runs(runs, self.policy)['passed'])

    def test_typing_absolute_limit_and_p99_are_checked(self):
        for metric in ('typing', 'typing_miss'):
            runs = [self.run_fixture(), self.run_fixture()]
            for run in runs:
                values = run['raw']['binit_seven_plugins'][metric]
                values[-int(len(values) * .02):] = [2.0] * int(len(values) * .02)
            result = observer.assess_runs(runs, self.policy)
            self.assertFalse(result['passed'])
            self.assertEqual(result['repeated_greater_than_5_percent'][0][0]['statistic'], 'p99')
            for run in runs:
                for scenario in ('binit_seven_plugins', 'binit_seven_before'):
                    values = run['raw'][scenario][metric]
                    values[:] = [9.0] * len(values)
            self.assertTrue(observer.assess_runs(runs, self.policy)['typing_limit_failures'])

    def test_missing_samples_scenarios_readiness_and_nonfinite_values_fail(self):
        def missing_sample(run):
            run['raw']['binit_seven_plugins']['typing'].pop()

        def missing_scenario(run):
            del run['raw']['bare_bash']

        def wrong_readiness(run):
            run['profiles'][0]['before_git_completion']['git-completion']['state'] = 'loaded'

        def missing_profile(run):
            run['profiles'].pop()

        mutations = [missing_sample, missing_scenario, wrong_readiness, missing_profile,
                     lambda run: run.update(complete=False)]
        for value in (float('nan'), float('inf'), -1, 0, True):
            mutations.append(lambda run, value=value: run['raw']['binit_seven_plugins']['typing'].__setitem__(0, value))
        for mutate in mutations:
            runs = [self.run_fixture(), self.run_fixture()]
            mutate(runs[0])
            with self.assertRaises(ValueError):
                observer.assess_runs(runs, self.policy)

    def test_controls_bind_both_binaries_observer_and_complete_evidence(self):
        scratch = observer.ROOT / 'tmp'
        scratch.mkdir(exist_ok=True)
        current = dict(self.run_fixture()['input_sha256'], reference='b' * 64)
        with tempfile.TemporaryDirectory(dir=scratch, prefix='observer-test-') as directory:
            paths = [Path(directory) / f'{i}.json' for i in range(2)]
            runs = [self.run_fixture(), self.run_fixture()]
            for i, run in enumerate(runs):
                run.update(input_sha256=dict(current, reference=current['binary']), identities_unchanged=True, distinct_run=i)

            def save():
                for path, run in zip(paths, runs):
                    path.write_text(json.dumps(run))

            save()
            self.assertTrue(observer.validate_controls(paths, current, self.policy)['assessment']['passed'])
            for field, value in (('binary', 'old'), ('reference', 'different'), ('observer', 'stale'), ('extra', 'input')):
                altered = copy.deepcopy(runs[0])
                runs[0]['input_sha256'][field] = value
                save()
                with self.assertRaises(ValueError):
                    observer.validate_controls(paths, current, self.policy)
                runs[0] = altered
            save()
            with self.assertRaises(ValueError):
                observer.validate_controls([paths[0], paths[0]], current, self.policy)
            runs[0]['identities_unchanged'] = False
            save()
            with self.assertRaises(ValueError):
                observer.validate_controls(paths, current, self.policy)

    def test_cli_refuses_overwrite_and_records_preexecution_failure(self):
        scratch = observer.ROOT / 'tmp'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch, prefix='observer-cli-test-') as directory:
            output = Path(directory) / 'receipt.json'
            argv = ['observer', '--plugin-root', directory, '--binary', 'candidate',
                    '--compare-binary', 'reference', '--output', str(output)]
            identity = dict(binary='candidate', reference='reference', workload=self.policy['workload']['sha256'])
            with patch.object(sys, 'argv', argv), patch.object(observer, 'inputs', return_value=identity), patch.object(observer.workload, 'benchmark') as benchmark:
                with self.assertRaisesRegex(ValueError, 'two distinct control receipts'):
                    observer.main()
                benchmark.assert_not_called()
                receipt = json.loads(output.read_text())
                self.assertFalse(receipt['complete'])
                self.assertIn('ValueError', receipt['error'])
                original = output.read_bytes()
                with self.assertRaises(FileExistsError):
                    observer.main()
                self.assertEqual(output.read_bytes(), original)

    def test_cli_retains_raw_evidence_when_post_run_identity_changes(self):
        scratch = observer.ROOT / 'tmp'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch, prefix='observer-drift-test-') as directory:
            output = Path(directory) / 'receipt.json'
            argv = ['observer', '--plugin-root', directory, '--binary', 'candidate',
                    '--compare-binary', 'candidate', '--output', str(output)]
            identity = dict(binary='candidate', reference='candidate', workload=self.policy['workload']['sha256'])
            original_shell = observer.workload.Shell
            was_enabled = gc.isenabled()
            with patch.object(sys, 'argv', argv), patch.object(observer, 'inputs', side_effect=[identity, dict(identity, binary='changed')]), patch.object(observer.workload, 'benchmark', return_value=dict(raw={'retained': [1.0]})):
                with self.assertRaisesRegex(ValueError, 'inputs changed during'):
                    observer.main()
            receipt = json.loads(output.read_text())
            self.assertFalse(receipt['complete'])
            self.assertFalse(receipt.get('identities_unchanged', False))
            self.assertEqual(receipt['raw'], {'retained': [1.0]})
            self.assertEqual(receipt['observer_cleanup'][0]['event'], 'teardown')
            self.assertIs(observer.workload.Shell, original_shell)
            self.assertEqual(gc.isenabled(), was_enabled)
