import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import bench_ecosystem_defaults as defaults


class DefaultsIntegrityTests(unittest.TestCase):
    def test_close_ready_only_after_effect_checks_and_close_failures_propagate(self):
        class Shell:
            startup_ms = 1.0
            pid = 1
            declined = False
            screen = type('Screen', (), {'display': []})()
            def __init__(self, status, close_error=False):
                self.status = status
                self.close_error = close_error
                self.raw = bytearray()
                self.closed = []
            def settled(self, data):
                return 1.0
            def command(self, code):
                if code:
                    self.raw.extend(b'\x1eCORPUS:' + self.status + b'\x1e')
                return 1.0
            def close(self, *, ready=False):
                self.closed.append(ready)
                if self.close_error:
                    raise RuntimeError('abnormal shell close')
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            root = Path(temporary)
            (root / '.osh-update').write_text('invalid timestamp')
            for case_id, status, close_error, expected in [('bash-control', b'0', False, True),
                    ('bash-control', b'1', False, False), ('bash-control', b'0', True, True),
                    ('omb-template-not-due', b'0', False, False)]:
                shell = Shell(status, close_error)
                with self.subTest(case_id=case_id, status=status, close_error=close_error), \
                        patch.object(defaults, 'DefaultShell', return_value=shell), \
                        patch.object(defaults.corpus, 'wait_for_editor'), \
                        patch.object(defaults.corpus, 'resource_sample', return_value={}), \
                        patch.object(defaults, 'default_probe', return_value=':'):
                    if status != b'0' or close_error or case_id.startswith('omb-template-'):
                        with self.assertRaises(RuntimeError):
                            defaults.observe({'id': case_id, 'shell': 'bash'}, '/bin/bash', root / 'rc', root, root, root / 'terminal.raw')
                    else:
                        defaults.observe({'id': case_id, 'shell': 'bash'}, '/bin/bash', root / 'rc', root, root, root / 'terminal.raw')
                    self.assertEqual(shell.closed, [expected])

    def test_doc_git_completion_glob_is_in_identity(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            root = Path(temporary)
            completion = root / 'git-completion.bash'
            completion.write_text('first')
            pattern = '/usr/share/doc/git-*/contrib/completion/git-completion.bash'
            real_glob = defaults.glob.glob
            def matches(value):
                return [str(completion)] if value == pattern else real_glob(value)
            args = SimpleNamespace(bash='/bin/bash', zsh='/bin/bash')
            with patch.object(defaults.glob, 'glob', side_effect=matches):
                before = defaults.inputs(args, {})['system_completion']
                completion.write_text('changed')
                after = defaults.inputs(args, {})['system_completion']
            self.assertNotEqual(before[str(completion)], after[str(completion)])

    def test_ble_runtime_cache_is_private_without_hiding_source_changes(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            root = Path(temporary)
            source = root / 'source'
            (source / 'cache.d/1000').mkdir(parents=True)
            (source / 'ble.sh').write_text('pinned build\n')
            (source / 'cache.d/1000/existing').write_text('prior run\n')
            before = defaults.corpus.inventory(source)
            receipt = defaults.copy_ble_build(source, root / 'private')
            self.assertEqual(defaults.corpus.inventory(source), before)
            self.assertIn('cache.d/1000/existing', receipt['source_runtime_cache'])
            self.assertFalse((root / 'private/cache.d').exists())
            (root / 'private/cache.d/1000').mkdir(parents=True)
            (root / 'private/cache.d/1000/new').write_text('private only\n')
            self.assertEqual(defaults.corpus.inventory(source), before)
            (source / 'cache.d/1000/existing').write_text('changed\n')
            self.assertNotEqual(defaults.corpus.inventory(source), before)

    def test_default_effect_probe_rejects_changed_alias_behavior(self):
        cases = {case['id']: case for case in json.loads(defaults.POLICY.read_text())['cases']}
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            home = Path(temporary)
            (home / 'enabled').mkdir()
            bash_it_setup = (f'BASH_IT={home}; BASH_IT_THEME=bobby; prompt_command() {{ :; }}; ips() {{ :; }}; '
                "complete -F ips bash-it; alias edit='${EDITOR:-${ALTERNATE_EDITOR:-nano}}'; "
                "alias ll='ls -Al' shit='bash-it' cd..='cd ..'; ")
            omb_setup = ('OSH_THEME=font; _omb_theme_PROMPT_COMMAND() { :; }; bm() { :; }; '
                'complete -F bm composer ssh; alias ga="command git add" g="bm -g" ll="ls -lAFh"; '
                "_omb_module_loaded=' completion:git completion:composer completion:ssh alias:general "
                "plugin:git plugin:bashmarks'; ")
            for key, setup, broken in [('bash-it-template', bash_it_setup, "alias edit='false'; "),
                                       ('omb-template-not-due', omb_setup, "alias ga='false'; ")]:
                for mutation, expected in [('', 0), (broken, 1)]:
                    with self.subTest(case=key, broken=bool(mutation)):
                        result = subprocess.run(['/bin/bash', '-c', setup + mutation + defaults.default_probe(cases[key])],
                                                capture_output=True)
                        self.assertEqual(result.returncode, expected, result.stderr)

    def test_contract_keeps_update_behaviors_and_unequal_roles_separate(self):
        policy = json.loads(defaults.POLICY.read_text())
        cases = {case['id']: case for case in policy['cases']}
        self.assertEqual(cases['omb-template-due-decline']['initial_update_timestamp'], 'LAST_EPOCH=1\n')
        self.assertIn('not due', cases['omb-template-not-due']['role'])
        self.assertIn('editor', cases['blesh-trial']['role'])
        self.assertTrue(any('Binit has no universal installed default' in item for item in policy['unsupported']))
        for name in ('completion:git', 'completion:composer', 'completion:ssh', 'plugin:git', 'plugin:bashmarks'):
            self.assertIn(name, cases['omb-template-not-due']['components'])

    def test_template_pin_rejects_mutation(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            base = Path(temporary)
            file = base / 'sources/example/template'
            file.parent.mkdir(parents=True)
            file.write_text('original')
            policy = {'cases': [{'id': 'example', 'template': 'example/template',
                                  'template_sha256': defaults.digest(file)}]}
            self.assertEqual(defaults.source_pins(base, policy), {'example/template': defaults.digest(file)})
            file.write_text('changed')
            with self.assertRaisesRegex(RuntimeError, 'pinned template differs'):
                defaults.source_pins(base, policy)

    def test_private_identity_sees_directory_and_link_changes(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            root = Path(temporary)
            (root / 'empty').mkdir()
            # A fixed starting mode keeps the later chmod a change under any umask.
            (root / 'empty').chmod(0o700)
            (root / 'target').write_text('kept')
            (root / 'link').symlink_to('target')
            before = defaults.tree_identity(root)
            self.assertIn('empty', before)
            self.assertEqual(before['link'], {'link': 'target'})
            (root / 'empty').chmod(0o755)
            self.assertNotEqual(before, defaults.tree_identity(root))

    def test_due_history_reset_stays_inside_private_home(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            home = Path(temporary)
            policy = json.loads(defaults.POLICY.read_text())
            case = next(case for case in policy['cases'] if case['id'] == 'omb-template-due-decline')
            (home / '.bash_history').write_text('synthetic prior commands\n')
            (home / '.osh-update').write_text('LAST_EPOCH=20000\n')
            defaults.reset_inputs(case, home)
            self.assertEqual((home / '.bash_history').read_text(), '')
            self.assertEqual((home / '.osh-update').read_text(), 'LAST_EPOCH=1\n')

    def test_summary_rejects_duplicate_failed_and_invalid_rows(self):
        row = {'case': 'bash-bare', 'pair': 0, 'effect_status': 0, 'state_unchanged': True}
        row.update({metric: 1. for metric in defaults.METRICS})
        self.assertEqual(defaults.run_summary([row], ['bash-bare'], 1)['bash-bare']['startup_visible_ms']['median'], 1.)
        for rows in ([row, row], [], [{**row, 'effect_status': 1}], [{**row, 'state_unchanged': False}]):
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                defaults.run_summary(rows, ['bash-bare'], 1)
        for invalid in (True, 0, -1, math.nan, math.inf):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(RuntimeError, 'invalid default elapsed'):
                defaults.run_summary([{**row, 'startup_visible_ms': invalid}], ['bash-bare'], 1)

    def test_due_prompt_is_declined_only_in_declared_case(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            root = Path(temporary)
            rc = root / 'rc'
            rc.write_text("read -rp '[Oh My Bash] Would you like to check for updates? [Y/n]: ' answer\n"
                          "PS1='BENCH> '\n")
            shell = defaults.DefaultShell('/bin/bash', rc, root, defaults.corpus.environment(root), decline_due=True)
            try:
                self.assertTrue(shell.declined)
                offset = len(shell.raw)
                shell.command('printf "\\036ANSWER:%s\\036" "$answer"')
                self.assertIn(b'\x1eANSWER:n\x1e', shell.raw[offset:])
            finally:
                shell.close()
            with self.assertRaisesRegex(RuntimeError, 'unexpected due update prompt'):
                defaults.DefaultShell('/bin/bash', rc, root, defaults.corpus.environment(root))


if __name__ == '__main__':
    unittest.main()
