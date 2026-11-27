"""Prototype update review: a plugin update reaches new shells only through a passing candidate check."""
import hashlib
import json
import os
import pty
import select
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('BINIT_TEST_BINARY', ROOT / 'target/release/binit'))
SCRIPT = ROOT / 'scripts/update_candidate.py'
BASE = '_tool() { COMPREPLY=(start stop); }\ncomplete -F _tool tool\n'


class UpdateCandidateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='.scratch-update-candidate-', dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'state'
        self.environment = dict(os.environ, HOME=str(self.root), INPUTRC='/dev/null')
        for name in ('BASH_ENV', 'ENV'):
            self.environment.pop(name, None)
        self.source('v1', BASE)
        (self.root / 'config.toml').write_text('schema=1\n[[plugins]]\npath="v1"\n')
        (self.root / 'completion.bash').write_text('[[ $(complete -p tool) == *_tool* ]]\n')
        (self.root / 'checks.toml').write_text(
            'schema=1\n[[checks]]\nid="completion"\nscript="completion.bash"\ntimeout_ms=5000\nmax_elapsed_ms=400\n')
        self.binit('prepare', str(self.root / 'config.toml'))
        self.first = self.current()

    def source(self, name, body):
        directory = self.root / name
        directory.mkdir()
        (directory / 'plugin.toml').write_text("schema=1\nid='tool'\nentry='init.bash'\nload='startup'\n")
        (directory / 'init.bash').write_text(body)
        return directory

    def binit(self, *args, check=True):
        result = subprocess.run([str(BINARY), *args, '--state', str(self.state)], capture_output=True, text=True,
                                env=self.environment, cwd=self.root, timeout=60)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def update(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), '--binit', str(BINARY), '--state', str(self.state), *args],
                              capture_output=True, text=True, env=self.environment, cwd=self.root, timeout=120)

    def stage(self, name):
        result = self.update('stage', str(self.root / 'config.toml'), 'tool', str(self.root / name),
                             str(self.root / 'checks.toml'))
        return result.returncode, json.loads(result.stdout)

    def select(self, report):
        review = report['review']
        return self.update('select', '--base', review['base'], '--generation', review['generation'],
                           '--receipt', review['receipt'] or '-')

    def current(self):
        return (self.state / 'current').read_text()

    def fresh_shell(self):
        """What a new private interactive Bash gets from the selected generation."""
        rc = self.root / 'rc'
        rc.write_text(f'eval "$({shlex.quote(str(BINARY))} init --state {shlex.quote(str(self.state))})"\n')
        script = ('complete -p tool >/dev/null 2>&1 && echo completion=yes || echo completion=no\n'
                  'declare -F tool_version >/dev/null && echo version=$(tool_version) || echo version=none\nexit 0\n')
        environment = dict(self.environment, TERM='dumb', HISTFILE='/dev/null')
        result = subprocess.run([os.environ.get('BINIT_BASH', '/bin/bash'), '--noprofile', '--rcfile', str(rc), '-i'],
                                input=script, capture_output=True, text=True, env=environment, cwd=self.root, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [line for line in result.stdout.splitlines() if '=' in line]

    def assert_selected_generation_usable(self, generation):
        self.assertEqual(self.current(), generation)
        self.binit('status')
        self.assertEqual(self.fresh_shell(), ['completion=yes', 'version=none'])

    def test_clean_update_is_selected_after_its_check(self):
        self.source('v2', BASE + 'tool_version() { echo 2; }\n')
        code, report = self.stage('v2')
        self.assertEqual(code, 0, report)
        self.assertEqual(report['source_files']['changed'], ['init.bash'])
        self.assertEqual(report['regressions'], [])
        self.assertEqual(report['review']['base'], self.first)
        self.assertEqual(self.current(), self.first)

        self.assertEqual(self.select(report).returncode, 0)
        self.assertEqual(self.current(), report['review']['generation'])
        self.assertEqual((self.state / 'previous').read_text(), self.first)
        self.assertEqual(self.fresh_shell(), ['completion=yes', 'version=2'])
        # Rollback returns new shells to the generation the update replaced.
        self.binit('rollback')
        self.assert_selected_generation_usable(self.first)

    def test_history_lists_valid_generations_when_an_unrelated_one_is_corrupt(self):
        source = self.source('v2', BASE + 'tool_version() { echo 2; }\n')
        self.binit('update', '--stage', 'tool', str(source))
        broken = self.state / 'generations' / ('a' * 64)
        broken.mkdir(mode=0o700)
        rows = json.loads(self.binit('candidate', 'history', '--json').stdout)
        by_id = {row['generation']: row for row in rows}
        self.assertFalse(by_id['a' * 64]['verified'])
        self.assertTrue(by_id[self.first]['verified'])
        self.assertNotEqual(self.binit('candidate', 'restore', 'tool', '--from', 'a' * 64,
                                       check=False).returncode, 0)

    def test_restore_picker_stages_on_enter_and_cancels_on_escape(self):
        source = self.source('v2', BASE + 'tool_version() { echo 2; }\n')
        self.binit('update', 'tool', str(source))
        current = self.current()
        previous = (self.state / 'previous').read_bytes()
        init = self.binit('init').stdout
        for key, staged in ((b'\x1b', False), (b'\n', True)):
            master, slave = pty.openpty()
            process = subprocess.Popen([os.environ.get('BINIT_BASH', '/bin/bash'), '--noprofile', '--norc', '-ic',
                                        init + '\nbinit candidate restore tool --pick'],
                                       stdin=slave, stdout=slave, stderr=slave,
                                       env=dict(self.environment, TERM='xterm-256color'), cwd=self.root)
            os.close(slave)
            output = b''
            try:
                while b'1/1' not in output:
                    self.assertTrue(select.select([master], [], [], 10)[0], output)
                    data = os.read(master, 4096)
                    self.assertTrue(data, output)
                    output += data
                os.write(master, key)
                process.wait(timeout=10)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
                os.close(master)
            self.assertEqual(process.returncode, 0 if staged else 1, output)
            self.assertEqual(self.current(), current)
            self.assertEqual((self.state / 'previous').read_bytes(), previous)
            self.assertEqual((self.state / 'candidate.json').exists(), staged)

    def test_manager_stages_a_catalog_update_and_preserves_a_checked_candidate_on_error(self):
        source = self.source('v2', BASE + 'tool_version() { echo 2; }\n')
        self.binit('update', '--stage', 'tool', str(source))
        self.assert_selected_generation_usable(self.first)

        self.assertIn('package contents changed', self.binit('candidate', 'diff').stdout)
        self.assertNotEqual(self.binit('candidate', 'select', check=False).returncode, 0)
        self.binit('candidate', 'check', str(self.root / 'checks.toml'))
        checked = json.loads(self.binit('candidate', 'status', '--json').stdout)
        self.assertEqual(checked['base_generation'], self.first)
        self.assertEqual(checked['state'], 'checked')
        self.assertNotIn('config', checked)
        self.assertIn('unchanged', self.binit('update', '--stage', 'tool', str(source)).stdout)
        self.assertNotEqual(self.binit('update', '--stage', 'tool', str(self.root / 'missing'),
                                       check=False).returncode, 0)
        self.assertIn('no generation changes', self.binit('update', '--stage', 'tool').stdout)
        self.assertEqual(json.loads(self.binit('candidate', 'status', '--json').stdout), checked)
        self.assert_selected_generation_usable(self.first)
        self.binit('candidate', 'select')
        self.assertEqual(self.fresh_shell(), ['completion=yes', 'version=2'])
        self.binit('rollback')
        self.assert_selected_generation_usable(self.first)

    def test_check_progress_is_terminal_only_and_respects_opt_out(self):
        replacement = self.source('v2', BASE + 'tool_version() { echo 2; }\n')
        self.binit('update', '--stage', 'tool', str(replacement))
        (self.root / 'completion.bash').write_text('sleep 0.35; complete -p tool\n')
        (self.root / 'checks.toml').write_text('schema=1\n[[checks]]\nid="completion"\nscript="completion.bash"\n')
        plan = str(self.root / 'checks.toml')
        self.environment['TERM'] = 'xterm-256color'
        result = self.binit('candidate', 'check', plan)
        self.assertNotIn('Running candidate checks', result.stderr)
        for enabled in ('1', '0'):
            master, slave = pty.openpty()
            try:
                result = subprocess.run(
                    [str(BINARY), 'candidate', 'check', plan, '--state', str(self.state)],
                    stdin=subprocess.DEVNULL, stdout=slave, stderr=slave,
                    env={**self.environment, 'BINIT_PROGRESS': enabled}, cwd=self.root, timeout=10)
                output = os.read(master, 4096)
                self.assertEqual(result.returncode, 0, output)
                self.assertIn(b'checks     passed', output)
                self.assertEqual(b'Running candidate checks' in output, enabled == '1')
                if enabled == '1':
                    self.assertRegex(output, rb'Running candidate checks\r\x1b\[2Kcandidate ')
            finally:
                os.close(slave)
                os.close(master)

    def test_generation_history_and_file_digests(self):
        source = self.source('v2', BASE + 'tool_version() { echo 2; }\n')
        self.binit('update', '--stage', 'tool', str(source))
        report = json.loads(self.binit('candidate', 'status', '--json').stdout)
        rows = json.loads(self.binit('candidate', 'history', '--json').stdout)
        by_id = {row['generation']: row for row in rows}
        self.assertTrue(by_id[self.first]['current'])
        self.assertEqual(by_id[report['generation']]['candidate_state'], 'prepared')
        text = self.binit('candidate', 'diff', '--files').stdout
        self.assertIn('file plugins/tool/init.bash', text)
        self.assertIn(hashlib.sha256(BASE.encode()).hexdigest(), text)
        self.assertIn(hashlib.sha256((BASE + 'tool_version() { echo 2; }\n').encode()).hexdigest(), text)
        self.assertEqual(self.current(), self.first)

    def test_single_restore_keeps_newer_plugins_without_reading_sources(self):
        companion = self.root / 'companion'
        companion.mkdir()
        (companion / 'plugin.toml').write_text("schema=1\nid='companion'\nentry='init.bash'\nload='startup'\n")
        (companion / 'init.bash').write_text('COMPANION=1\n')
        self.binit('add', str(companion))
        old = self.current()
        replacement = self.source('v2', BASE + 'tool_version() { echo 2; }\n')
        self.binit('update', 'tool', str(replacement))
        (companion / 'init.bash').write_text('COMPANION=2\n')
        self.binit('update', 'companion')
        current = self.current()
        previous = (self.state / 'previous').read_bytes()
        (self.root / 'v1').rename(self.root / 'v1-unavailable')
        replacement.rename(self.root / 'v2-unavailable')
        companion.rename(self.root / 'companion-unavailable')

        self.binit('candidate', 'restore', 'tool', '--from', old)
        diff = self.binit('candidate', 'diff').stdout
        self.assertIn('~ tool\n', diff)
        self.assertNotIn('companion', diff)
        self.assertEqual(self.current(), current)
        self.assertEqual((self.state / 'previous').read_bytes(), previous)
        self.assertEqual(self.fresh_shell(), ['completion=yes', 'version=2'])
        self.assertNotEqual(self.binit('candidate', 'select', check=False).returncode, 0)

        (self.root / 'completion.bash').write_text(
            '[[ $COMPANION == 2 && $(complete -p tool) == *_tool* ]] && ! declare -F tool_version\n')
        self.binit('candidate', 'check', str(self.root / 'checks.toml'))
        self.binit('candidate', 'select')
        self.assertEqual(self.fresh_shell(), ['completion=yes', 'version=none'])
        self.binit('rollback')
        self.assertEqual(self.current(), current)
        self.assertEqual(self.fresh_shell(), ['completion=yes', 'version=2'])

    def test_single_restore_defaults_to_previous_and_refuses_damaged_source(self):
        replacement = self.source('v2', BASE + 'tool_version() { echo 2; }\n')
        self.binit('update', 'tool', str(replacement))
        current = self.current()
        self.binit('candidate', 'restore', 'tool')
        self.assertEqual(json.loads(self.binit('candidate', 'status', '--json').stdout)['generation'], self.first)

        self.binit('candidate', 'prepare', str(self.root / 'config.toml'))
        record = (self.state / 'candidate.json').read_bytes()
        self.assertIn('no plugin changes', self.binit('candidate', 'restore', 'tool', '--from', current).stdout)
        self.assertEqual((self.state / 'candidate.json').read_bytes(), record)
        old_entry = self.state / 'generations' / self.first / 'plugins/tool/init.bash'
        old_entry.write_text('damaged\n')
        result = self.binit('candidate', 'restore', 'tool', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('content changed: plugins/tool/init.bash', result.stderr)
        self.assertEqual((self.state / 'candidate.json').read_bytes(), record)
        self.assertEqual(self.current(), current)
        self.assertEqual((self.state / 'previous').read_text(), self.first)
        self.assertEqual(self.fresh_shell(), ['completion=yes', 'version=2'])

    def test_single_restore_keeps_a_disabled_plugin_disabled(self):
        replacement = self.source('v2', BASE + 'tool_version() { echo 2; }\n')
        self.binit('update', 'tool', str(replacement))
        self.binit('disable', 'tool')
        self.binit('candidate', 'restore', 'tool', '--from', self.first)
        diff = self.binit('candidate', 'diff').stdout
        self.assertIn('package contents changed', diff)
        self.assertNotIn('enabled:', diff)
        (self.root / 'completion.bash').write_text('! complete -p tool 2>/dev/null\n')
        self.binit('candidate', 'check', str(self.root / 'checks.toml'))
        self.binit('candidate', 'select')
        self.assertEqual(self.fresh_shell(), ['completion=no', 'version=none'])

    def test_single_restore_missing_source_can_be_prepared_again(self):
        replacement = self.source('v2', BASE + 'tool_version() { echo 2; }\n')
        self.binit('update', 'tool', str(replacement))
        current = self.current()
        generation = self.state / 'generations' / self.first
        generation.rename(self.root / 'unavailable-generation')

        result = self.binit('candidate', 'restore', 'tool', '--from', self.first, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(generation.exists(), 'verification recreated the missing generation directory')
        self.assertEqual(self.current(), current)
        self.binit('candidate', 'prepare', str(self.root / 'config.toml'))
        self.assertEqual(json.loads(self.binit('candidate', 'status', '--json').stdout)['generation'], self.first)

    def test_review_replaced_while_staging_reports_no_identities(self):
        self.source('v2', BASE + 'tool_version() { echo 2; }\n')
        self.source('v3', BASE + 'tool_version() { echo 3; }\n')
        (self.root / 'other.toml').write_text('schema=1\n[[plugins]]\npath="v3"\n')
        # Another review prepares its own candidate right after this stage's check.
        wrapper = self.root / 'binit-interleaved'
        wrapper.write_text(f'#!/bin/bash\n{shlex.quote(str(BINARY))} "$@"; rc=$?\n'
                           f'[[ $1 == candidate && $2 == check ]] && {shlex.quote(str(BINARY))} candidate prepare '
                           f'{shlex.quote(str(self.root / "other.toml"))} "${{@:4}}" >/dev/null\nexit $rc\n')
        wrapper.chmod(0o700)
        result = subprocess.run([sys.executable, str(SCRIPT), '--binit', str(wrapper), '--state', str(self.state), 'stage',
                                 str(self.root / 'config.toml'), 'tool', str(self.root / 'v2'),
                                 str(self.root / 'checks.toml')],
                                capture_output=True, text=True, env=self.environment, cwd=self.root, timeout=120)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('changed during this review', result.stderr)
        self.assertEqual(result.stdout, '')
        self.assert_selected_generation_usable(self.first)

    def test_selection_changed_while_staging_reports_no_identities(self):
        self.source('v2', BASE + 'tool_version() { echo 2; }\n')
        self.source('v3', BASE + 'tool_version() { echo 3; }\n')
        (self.root / 'other.toml').write_text('schema=1\n[[plugins]]\npath="v3"\n')
        # Another shell selects a new generation after this review read the base and before it prepares.
        wrapper = self.root / 'binit-reselect'
        wrapper.write_text(f'#!/bin/bash\n[[ $1 == diagnose ]] && {shlex.quote(str(BINARY))} prepare '
                           f'{shlex.quote(str(self.root / "other.toml"))} "${{@: -2}}" >/dev/null\n'
                           f'exec {shlex.quote(str(BINARY))} "$@"\n')
        wrapper.chmod(0o700)
        result = subprocess.run([sys.executable, str(SCRIPT), '--binit', str(wrapper), '--state', str(self.state), 'stage',
                                 str(self.root / 'config.toml'), 'tool', str(self.root / 'v2'),
                                 str(self.root / 'checks.toml')],
                                capture_output=True, text=True, env=self.environment, cwd=self.root, timeout=120)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('changed during this review', result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertNotEqual(self.current(), self.first)

    @unittest.skipIf(os.geteuid() == 0, 'root reads a file whatever its mode, as in CI containers')
    def test_unreadable_new_source_leaves_no_review_configuration(self):
        unreadable = self.source('v2', BASE)
        (unreadable / 'init.bash').chmod(0)
        self.addCleanup((unreadable / 'init.bash').chmod, 0o600)
        result = self.update('stage', str(self.root / 'config.toml'), 'tool', str(unreadable), str(self.root / 'checks.toml'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('PermissionError', result.stderr)
        self.assertEqual(sorted(p.name for p in self.root.glob('.config.update-*')), [])

    def test_failed_preparation_leaves_no_review_configuration(self):
        broken = self.root / 'v2'
        broken.mkdir()
        (broken / 'plugin.toml').write_text("schema=1\nid='tool'\nentry='missing.bash'\nload='startup'\n")
        result = self.update('stage', str(self.root / 'config.toml'), 'tool', str(broken), str(self.root / 'checks.toml'))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sorted(p.name for p in self.root.glob('.config.update-*')), [])
        self.assert_selected_generation_usable(self.first)

    def test_update_staged_after_the_review_is_not_selected_in_its_place(self):
        self.source('v2', BASE + 'tool_version() { echo 2; }\n')
        self.source('v3', BASE + 'tool_version() { echo 3; }\n')
        code, reviewed = self.stage('v2')
        self.assertEqual(code, 0, reviewed)
        code, later = self.stage('v3')
        self.assertEqual(code, 0, later)
        self.assertEqual(later['review']['base'], reviewed['review']['base'])
        self.assertNotEqual(later['review']['generation'], reviewed['review']['generation'])
        # Each review keeps its own private configuration, so the first stays intact.
        configs = [Path(reviewed['proposed_config']), Path(later['proposed_config'])]
        self.assertNotEqual(configs[0], configs[1])
        for config in configs:
            self.assertEqual(config.parent, self.root)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertIn(str(self.root / 'v2'), configs[0].read_text())
        selected = self.select(reviewed)
        self.assertNotEqual(selected.returncode, 0)
        self.assertIn('differs from the reviewed candidate', selected.stderr)
        self.assert_selected_generation_usable(self.first)
        self.assertEqual(self.select(later).returncode, 0)
        self.assertEqual(self.fresh_shell(), ['completion=yes', 'version=3'])

    def test_update_that_drops_completion_is_shown_and_never_selected(self):
        self.source('v2', '_tool() { COMPREPLY=(start stop); }\n')
        code, report = self.stage('v2')
        self.assertNotEqual(code, 0)
        self.assertEqual(report['regressions'], ['completion'])
        self.assertTrue(report['selected']['completion']['passed'])
        self.assertNotEqual(self.select(report).returncode, 0)
        self.assert_selected_generation_usable(self.first)

    def test_update_that_adds_a_slow_load_hook_is_never_selected(self):
        self.source('v2', BASE + 'sleep 0.6\n')
        code, report = self.stage('v2')
        self.assertNotEqual(code, 0)
        self.assertEqual(report['candidate_state'], 'failed')
        self.assertGreater(min(report['candidate']['completion']['elapsed_ms']), 400)
        self.assertNotEqual(self.select(report).returncode, 0)
        self.assert_selected_generation_usable(self.first)

    def test_stale_receipt_or_changed_selection_leaves_the_selected_generation(self):
        self.source('v2', BASE + 'tool_version() { echo 2; }\n')
        code, report = self.stage('v2')
        self.assertEqual(code, 0, report)
        (self.root / 'completion.bash').write_text('true\n')
        self.assertNotEqual(self.select(report).returncode, 0)
        self.assert_selected_generation_usable(self.first)
        (self.root / 'completion.bash').write_text('[[ $(complete -p tool) == *_tool* ]]\n')
        code, report = self.stage('v2')
        self.assertEqual(code, 0, report)
        self.source('v3', BASE + 'other() { :; }\n')
        (self.root / 'other.toml').write_text('schema=1\n[[plugins]]\npath="v3"\n')
        self.binit('prepare', str(self.root / 'other.toml'))
        changed = self.current()
        self.assertNotEqual(self.select(report).returncode, 0)
        self.assert_selected_generation_usable(changed)


if __name__ == '__main__':
    unittest.main()
