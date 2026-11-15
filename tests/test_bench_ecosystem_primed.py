import json
import math
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import bench_ecosystem_primed as primed


class PrimedFrameworkTests(unittest.TestCase):
    def test_framework_zero_effects_reject_missing_loader_or_wrong_source(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            directory = Path(temporary)
            rc = directory / 'rc'
            for subject, variable in [('bash-it', 'BASH_IT'), ('oh-my-bash', 'OSH')]:
                rc.write_text(f'{variable}={shlex.quote(str(directory))}\n')
                probe = primed.loader_readiness(subject, directory, rc)
                initialized = (f'{variable}={shlex.quote(str(directory))}; '
                    + ('bash-it() { :; }; ' if subject == 'bash-it' else
                       'OMB_VERSION=1; omb() { :; }; _omb_module_require() { :; }; '))
                for code, succeeds in [('', False), (initialized, True), (initialized + f'{variable}=/wrong; ', False)]:
                    with self.subTest(subject=subject, code=code):
                        result = subprocess.run(['/bin/bash', '-c', code + probe], capture_output=True)
                        self.assertEqual(result.returncode == 0, succeeds)
            zinit_home = directory / 'zinit'
            zinit_home.mkdir()
            probe = primed.loader_readiness('zinit', directory, rc)
            initialized = 'typeset -gA ZINIT; ZINIT[HOME_DIR]="$HOME/zinit"; zinit() { :; }; '
            for code, succeeds in [('', False), (initialized, True), (initialized + 'ZINIT[HOME_DIR]=/wrong; ', False)]:
                with self.subTest(subject='zinit', code=code):
                    result = subprocess.run(['/bin/zsh', '-c', code + probe],
                                            env={'HOME': str(directory), 'PATH': '/usr/bin:/bin'}, capture_output=True)
                    self.assertEqual(result.returncode == 0, succeeds)

    def test_binit_zero_effects_reject_empty_failed_and_wrong_generation_init(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            directory = Path(temporary)
            state = directory / 'state'
            state.mkdir()
            selected = 'a' * 64
            (state / 'current').write_text(selected)
            probe = primed.binit_readiness(directory)
            setup = (f'BINIT_STATE_DIR={shlex.quote(str(state))}; '
                     f'__binit_generation_dir={shlex.quote(str(state / "generations" / selected))}; ')
            cases = [('', False), (setup + 'binit() { return 1; }; ', False),
                     (setup + '__binit_generation_dir=wrong; binit() { :; }; ', False),
                     (setup + 'binit() { :; }; ', True)]
            for code, succeeds in cases:
                with self.subTest(code=code):
                    result = subprocess.run(['/bin/bash', '-c', code + probe], capture_output=True)
                    self.assertEqual(result.returncode == 0, succeeds)

    def test_summary_rejects_duplicate_missing_failed_and_invalid_rows(self):
        row = {'case': 'bash-0', 'pair': 0, 'effect_status': 0, 'private_state_unchanged': True}
        row.update({metric: 1. for metric in primed.METRICS})
        self.assertEqual(primed.summarize_run([row], ['bash-0'], 1)['bash-0']['startup_visible_ms']['median'], 1.)
        for rows in ([row, row], [], [{**row, 'effect_status': 9}],
                     [{**row, 'private_state_unchanged': False}], [{**row, 'pair': 1}]):
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                primed.summarize_run(rows, ['bash-0'], 1)
        for invalid in (0, -1, math.nan, math.inf, True):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(RuntimeError, 'invalid primed elapsed'):
                primed.summarize_run([{**row, 'startup_visible_ms': invalid}], ['bash-0'], 1)

    def test_empty_directories_modes_and_symlink_targets_are_inputs(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            directory = Path(temporary)
            (directory / 'empty').mkdir()
            (directory / 'fixture').write_text('fixture\n')
            (directory / 'link').symlink_to('fixture')
            first = primed.inventory_tree(directory)
            self.assertIn('empty', first)
            self.assertEqual(first['link'], {'link': 'fixture'})
            (directory / 'empty').chmod(0o755)
            self.assertNotEqual(first, primed.inventory_tree(directory))
            (directory / 'fixture').write_text('changed\n')
            self.assertNotEqual(first['fixture'], primed.inventory_tree(directory)['fixture'])

    def test_state_drift_before_launch_is_rejected_without_running(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            home = Path(temporary)
            before = primed.inventory_tree(home)
            (home / 'added').write_text('new')
            with patch.object(primed, 'observe') as observe:
                with self.assertRaisesRegex(RuntimeError, 'before launch'):
                    primed.verified_observe('bash', '/bin/bash', home / 'rc', home, home, before,
                                            home / 'failure.raw')
                observe.assert_not_called()

    def test_drift_during_launch_is_retained_with_the_observation(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            home = Path(temporary)
            before = primed.inventory_tree(home)
            def change(*_):
                (home / 'added').write_text('new')
                return {'effect_status': 0, 'startup_visible_ms': 7}
            with patch.object(primed, 'observe', side_effect=change):
                row = primed.verified_observe('bash', '/bin/bash', home / 'rc', home, home, before,
                                              home / 'failure.raw')
            self.assertFalse(row['private_state_unchanged'])
            self.assertEqual(row['startup_visible_ms'], 7)
            self.assertIn('added', row['changed_private_state'])

    def test_changed_primed_state_stops_run_with_partial_receipt(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            base = Path(temporary)
            binary = base / 'binary'
            binary.write_bytes(b'binary')
            output = base / 'receipt.json'
            calls = 0
            def changed(_subject, _interpreter, _rc, _work, home, _failure):
                nonlocal calls
                calls += 1
                if calls == 2:
                    (home / 'changed').write_text('new')
                return {'effect_status': 0, 'startup_visible_ms': 7}
            argv = ['primed', '--sources', str(base), '--binary', str(binary),
                    '--work', str(base / 'work'), '--output', str(output),
                    '--subjects', 'bash', '--counts', '0', '--runs', '1', '--pairs', '1']
            with patch.object(sys, 'argv', argv), patch.object(primed.corpus, 'verify_sources', return_value={}), \
                    patch.object(primed, 'observe', side_effect=changed):
                with self.assertRaisesRegex(RuntimeError, 'during launch'):
                    primed.main()
            receipt = json.loads(output.read_text())
            self.assertFalse(receipt['complete_two_run_primed_framework'])
            self.assertEqual(len(receipt['runs'][0]), 1)
            self.assertFalse(receipt['runs'][0][0]['private_state_unchanged'])
            self.assertEqual(receipt['active_case'], ['measure', 0, 0, 'bash-0'])
            self.assertIn('during launch', receipt['error'])

    def test_initialization_primes_only_selected_private_home(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            directory = Path(temporary)
            rc = directory / 'rc'
            rc.write_text('PS1="BENCH> "\n')
            bash_home = directory / 'bash-home'
            zsh_home = directory / 'zsh-home'
            primed.initialize_home('bash', rc, bash_home)
            primed.initialize_home('zinit', rc, zsh_home)
            self.assertEqual(primed.inventory_tree(bash_home), {'.': {'directory_mode': bash_home.stat().st_mode & 0o777}})
            self.assertEqual((zsh_home / '.zshrc').read_bytes(), rc.read_bytes())


if __name__ == '__main__':
    unittest.main()
