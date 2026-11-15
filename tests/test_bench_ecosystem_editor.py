import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import bench_ecosystem_editor as editor


def valid_row():
    trace_names = ('history-range', 'unicode', 'multiline-paste', 'wrapped-prompt',
                   'completion-first-warm', 'completion-large-cancel', 'isolated-slow-cancel',
                   'stateful-slow-provider', 'recovery')
    traces = {name: {} for name in trace_names}
    traces['typing'] = {'samples_ms': {'insert': [1.], 'delete': [2.]}}
    for name in ('unicode', 'multiline-paste', 'wrapped-prompt', 'completion-large-cancel', 'recovery'):
        traces[name]['elapsed_ms'] = 1.
    traces['completion-large-cancel']['provider_calls'] = 1
    traces['completion-first-warm'] = {'first': {'completion_response_ms': 1., 'accepted_ms': 10.},
                                       'warm': {'completion_response_ms': 2., 'accepted_ms': 11.}}
    traces['isolated-slow-cancel'] = {'elapsed_ms': 1., 'input_response_ms': 1.}
    traces['stateful-slow-provider'] = {'completion_elapsed_ms': 1., 'accepted_elapsed_ms': 1.}
    return {'pair': 0, 'subject': 'binit', 'mode': 'vi', 'history': 'small',
            'passed': True, 'traces': traces}


class EditorCorpusTests(unittest.TestCase):
    def test_readline_dialog_accepts_fragmented_prompt_and_rejects_incomplete(self):
        class FakeShell:
            def __init__(self, chunks):
                self.raw = bytearray()
                self.chunks = iter(chunks)
            def read(self, timeout):
                chunk = next(self.chunks, b'')
                self.raw.extend(chunk)
                return bool(chunk)
        shell = FakeShell([b'Display all 1024 ', b'possibilities? (y or n)'])
        self.assertTrue(editor.readline_tab_dialog(shell, 0))
        shell = FakeShell([b'Display all 1024 '])
        with patch.object(editor.time, 'monotonic', side_effect=[0., 0., .1, .2, .3, .51, .51]):
            with self.assertRaisesRegex(RuntimeError, 'incomplete Readline'):
                editor.readline_tab_dialog(shell, 0)

    def test_large_cancel_timing_uses_first_cancel_input_and_endpoint(self):
        self.assertEqual(editor.cancellation_elapsed(1000000, 7500000), 6.5)
        with self.assertRaisesRegex(RuntimeError, 'invalid cancellation'):
            editor.cancellation_elapsed(2, 1)

    def test_readline_candidate_confirmation_requires_actual_prompt(self):
        self.assertTrue(editor.readline_candidate_confirmation(b'Display all 1024 possibilities? (y or n)'))
        self.assertFalse(editor.readline_candidate_confirmation(b'Display all candidates'))
        self.assertFalse(editor.readline_candidate_confirmation(b'ordinary completion output'))

    def test_paired_provider_liveness_requires_exact_live_owned_pid(self):
        sample = {'partial': False, 'samples': [{'pid': 10, 'state': 'S'}, {'pid': 11, 'state': 'S'}]}
        self.assertTrue(editor.paired_provider_is_live(sample, 10, 11))
        self.assertFalse(editor.paired_provider_is_live(sample, 10, 12))
        self.assertFalse(editor.paired_provider_is_live(sample, 10, 10))
        self.assertFalse(editor.paired_provider_is_live({**sample, 'partial': True}, 10, 11))
        zombie = {'partial': False, 'samples': [{'pid': 10, 'state': 'S'}, {'pid': 11, 'state': 'Z'},
                                               {'pid': 12, 'state': 'S'}]}
        self.assertFalse(editor.paired_provider_is_live(zombie, 10, 11))

    def test_pair_mismatch_requires_completed_filename_and_absent_provider(self):
        line = 'BENCH>  wf_iso alpha_pair_probe '
        self.assertTrue(editor.paired_provider_mismatch(False, line))
        self.assertFalse(editor.paired_provider_mismatch(True, line))
        self.assertFalse(editor.paired_provider_mismatch(False, 'BENCH>  wf_iso al'))

    def test_cleanup_waits_for_complete_sample_and_retains_all_observations(self):
        def observation(count, partial=False, processes=1):
            return {'partial': partial, 'fd_targets': {}, 'resources': {
                'partial': False, 'processes': processes, 'discovered_processes': processes,
                'zombies': 0, 'fds_sum': count}}
        transient = [observation(13, True), observation(13), observation(12)]
        samples = []
        with patch.object(editor, 'cleanup_observation', side_effect=transient), patch.object(editor.time, 'sleep'):
            self.assertTrue(editor.cleanup_quiescence(1, 12, samples))
        self.assertEqual(samples, transient)
        baseline = [observation(13, True), observation(12)]
        samples = []
        with patch.object(editor, 'cleanup_observation', side_effect=baseline), patch.object(editor.time, 'sleep'):
            self.assertTrue(editor.cleanup_quiescence(1, None, samples))
        self.assertEqual(samples, baseline)
        self.assertEqual(samples[-1]['resources']['fds_sum'], 12)
        samples = []
        with patch.object(editor, 'cleanup_observation', return_value=observation(12, True)), patch.object(editor.time, 'sleep'):
            self.assertFalse(editor.cleanup_quiescence(1, None, samples))
        self.assertEqual(len(samples), 21)
        persistent = observation(13)
        samples = []
        with patch.object(editor, 'cleanup_observation', return_value=persistent), patch.object(editor.time, 'sleep'):
            self.assertFalse(editor.cleanup_quiescence(1, 12, samples))
        self.assertEqual(len(samples), 21)
        self.assertTrue(all(item == persistent for item in samples))
        self.assertFalse(editor.cleanup_complete(observation(12, processes=2), 12))
        samples = []
        with patch.object(editor, 'cleanup_observation', return_value=observation(12)), \
                patch.object(editor.time, 'monotonic', side_effect=[0.0, .101]):
            self.assertFalse(editor.cleanup_quiescence(1, 12, samples))
        self.assertEqual(len(samples), 1)
        samples = []
        with patch.object(editor, 'cleanup_observation', return_value=observation(12)), \
                patch.object(editor.time, 'monotonic', side_effect=[0.0, .101]):
            self.assertFalse(editor.cleanup_quiescence(1, None, samples))
        self.assertEqual(len(samples), 1)

    def test_cleanup_marks_vanished_fd_partial(self):
        resources = {'partial': False, 'processes': 1, 'discovered_processes': 1,
                     'zombies': 0, 'fds_sum': 1}
        with patch.object(editor.corpus, 'resource_sample', return_value=resources), \
                patch.object(Path, 'iterdir', return_value=iter([Path('/proc/1/fd/10')])), \
                patch.object(editor.os, 'readlink', side_effect=FileNotFoundError):
            observed = editor.cleanup_observation(1)
        self.assertTrue(observed['partial'])
        self.assertEqual(observed['fd_targets'], {'10': None})
        self.assertFalse(editor.cleanup_complete(observed, 1))

    def test_blesh_vi_ctrl_c_retains_line_then_dd_i_recovers(self):
        source = Path(os.environ.get('BINIT_ECOSYSTEM_SOURCE', ROOT / 'tmp/bash-ecosystem/sources'))
        if not (source / 'ble/out/ble.sh').is_file():
            self.skipTest('pinned optional ble.sh corpus source not present')
        bash = os.environ.get('BINIT_BASH', '/bin/bash')
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            work = Path(temporary) / 'case'
            rc, home = editor.configuration('blesh', 'vi', {'id': 'empty', 'entries': 0},
                                            work, ROOT / 'unused', bash, source)
            shell = editor.corpus.BoundedShell(bash, rc, work, editor.corpus.environment(home))
            try:
                path = editor.tag(shell, work, 'interrupted')
                shell.settled(b' wf_capture interrupted')
                shell.cancel()
                shell.until(lambda: 'wf_capture interrupted' in editor.screen_tail(shell), seconds=3)
                self.assertFalse(path.exists())
                shell.settled(b'ddi')
                shell.until(lambda: editor.ble_empty_prompt(shell, 'vi'), seconds=3)
                recovered = editor.tag(shell, work, 'recovered')
                editor.accepted(shell, b' wf_capture recovered')
                editor.effect(recovered, b'recovered\0')
            finally:
                shell.close()

    def test_query_acknowledgment_requires_the_search_status_not_the_candidate(self):
        needle = 'editor-recent-needle'
        def shell(rows):
            return SimpleNamespace(screen=SimpleNamespace(display=rows))
        self.assertFalse(editor.history_query_acknowledged(shell([
            "BENCH> printf 'wf-history-recent:editor-recent-needle'", "(1: << !64 ) `e'"]), 'blesh', needle))
        self.assertTrue(editor.history_query_acknowledged(shell([
            "BENCH> printf 'wf-history-recent:editor-recent-needle'", f"(20: << !64 ) `{needle}'"]), 'blesh', needle))
        self.assertFalse(editor.history_query_acknowledged(shell([
            'History SEARCH /e 1/1', 'wf-history-recent:editor-recent-needle']), 'binit', needle))
        self.assertTrue(editor.history_query_acknowledged(shell([f'History SEARCH /{needle} 1/1']), 'binit', needle))
        self.assertFalse(editor.history_query_acknowledged(shell([
            "(reverse-i-search)`e': wf-history-recent:editor-recent-needle"]), 'readline', needle))
        self.assertTrue(editor.history_query_acknowledged(shell([
            f"(reverse-i-search)`{needle}': wf-history-recent:editor-recent-needle"]), 'readline', needle))

    def test_tag_rejects_occupied_line_even_if_generic_frame_claims_success(self):
        class OccupiedShell:
            raw = bytearray()
            def command(self, code):
                match = editor.re.search(r'WF-TAG:([0-9a-f]+):', code)
                self.raw.extend(b'\x1eWF-TAG:' + match[1].encode() + b':\x1f')
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            directory = Path(temporary)
            (directory / 'effects').mkdir()
            with patch.object(editor, 'frame', return_value=None):
                with self.assertRaisesRegex(RuntimeError, 'tag confirmation failed'):
                    editor.tag(OccupiedShell(), directory, 'unique-value')

    def test_ble_vi_recovery_requires_empty_prompt_cursor_and_insert_status(self):
        cursor = SimpleNamespace(x=7, y=0)
        shell = SimpleNamespace(screen=SimpleNamespace(cursor=cursor, display=['BENCH> ', '-- INSERT --']))
        self.assertTrue(editor.ble_empty_prompt(shell, 'vi'))
        shell.screen.display[0] = 'BENCH> occupied'
        self.assertFalse(editor.ble_empty_prompt(shell, 'vi'))
        shell.screen.display[0] = 'BENCH> '
        shell.screen.display[1] = '-- NORMAL --'
        self.assertFalse(editor.ble_empty_prompt(shell, 'vi'))
        shell.screen.display[1] = '-- INSERT --'
        cursor.x = 8
        self.assertFalse(editor.ble_empty_prompt(shell, 'vi'))

    def test_blesh_vi_history_recovery_and_occupied_search_tag(self):
        source = Path(os.environ.get('BINIT_ECOSYSTEM_SOURCE', ROOT / 'tmp/bash-ecosystem/sources'))
        if not (source / 'ble/out/ble.sh').is_file():
            self.skipTest('pinned optional ble.sh corpus source not present')
        bash = os.environ.get('BINIT_BASH', '/bin/bash')
        for occupied in (False, True):
            with self.subTest(occupied=occupied), tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
                work = Path(temporary) / 'case'
                history = {'id': 'small', 'entries': 64}
                rc, home = editor.configuration('blesh', 'vi', history, work, ROOT / 'unused', bash, source)
                shell = editor.corpus.BoundedShell(bash, rc, work, editor.corpus.environment(home))
                try:
                    if occupied:
                        shell.settled(b'\x12')
                        shell.settled(b'editor-recent-needle')
                        shell.until(lambda: editor.history_query_acknowledged(shell, 'blesh', 'editor-recent-needle'), seconds=3)
                        original_until = shell.until
                        with patch.object(shell, 'until', side_effect=lambda predicate, seconds=10:
                                          original_until(predicate, seconds=min(seconds, 1))):
                            with self.assertRaises(RuntimeError):
                                editor.tag(shell, work, 'occupied-search')
                        self.assertFalse((work / 'effects/occupied-search').exists())
                    else:
                        result = editor.history_trace(shell, 'blesh', history, 'vi')
                        self.assertTrue(result['recent']['visible'])
                        self.assertTrue(editor.ble_empty_prompt(shell, 'vi'))
                        path = editor.tag(shell, work, 'recovered')
                        editor.accepted(shell, " wf_capture 'caffè 雪'".encode())
                        editor.effect(path, 'caffè 雪\0'.encode())
                finally:
                    shell.close()

    def test_framework_blesh_pairings_load_shared_effects_and_selected_options(self):
        source = Path(os.environ.get('BINIT_ECOSYSTEM_SOURCE', ROOT / 'tmp/bash-ecosystem/sources'))
        if not (source / 'ble/out/ble.sh').is_file() or not (source / 'bash-it/bash_it.sh').is_file() \
                or not (source / 'oh-my-bash/oh-my-bash.sh').is_file():
            self.skipTest('pinned optional framework and ble.sh corpus sources not present')
        bash = os.environ.get('BINIT_BASH', '/bin/bash')
        for subject in ('equiv-bashit-blesh', 'equiv-omb-blesh'):
            with self.subTest(subject=subject), tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
                work = Path(temporary) / 'case'
                rc, home = editor.configuration(subject, 'emacs', {'id': 'empty', 'entries': 0},
                                                work, ROOT / 'unused', bash, source)
                shell = editor.corpus.BoundedShell(bash, rc, work, editor.corpus.environment(home))
                try:
                    editor.frame(shell, 'wf_original_dir=$PWD; corpus_verify; wf_corpus_status=$?; '
                                        'cd -- "$wf_original_dir"; [[ $wf_corpus_status == 0 ]]')
                    editor.frame(shell, '[[ $WF_BLE_OPTIONS_STATUS == 0 && -n ${_ble_attached-} '
                                        '&& -z ${bleopt_highlight_syntax-} && -z ${bleopt_highlight_filename-} '
                                        '&& -z ${bleopt_highlight_variable-} '
                                        '&& :${bleopt_complete_auto_complete_opts-}: == *:syntax-disabled:* ]]')
                finally:
                    shell.close()

    def test_framework_pairing_requires_passing_controls_and_propagates_infrastructure_errors(self):
        args = SimpleNamespace(work=ROOT / 'tmp', binary=Path('/bin/true'), bash='/bin/bash', events=1)
        policy = json.loads(editor.POLICY.read_text())
        policy['modes'] = ['emacs']
        history = {'id': 'small', 'entries': 64}
        calls = []
        def observed(subject, *_args):
            calls.append(subject)
            if subject == 'equiv-omb-blesh':
                raise editor.EditorBehaviorMismatch('observed paired input mismatch')
            return {'passed': True}
        with patch.object(editor, 'run_trace', side_effect=observed):
            result = editor.framework_pairing_preflight(policy, args, ROOT, history)
        self.assertTrue(result['pairs']['equiv-bashit-blesh']['passed'])
        self.assertFalse(result['pairs']['equiv-omb-blesh']['passed'])
        self.assertEqual(result['pairs']['equiv-omb-blesh']['modes']['emacs']['classification'],
                         'observed_behavior_mismatch')
        self.assertEqual(calls[:4], ['equiv-binit', 'equiv-blesh-control',
                                     'equiv-bashit-control', 'equiv-omb-control'])
        def infrastructure(subject, *_args):
            if subject == 'equiv-bashit-blesh':
                raise RuntimeError('source identity unavailable')
            return {'passed': True}
        with patch.object(editor, 'run_trace', side_effect=infrastructure):
            with self.assertRaisesRegex(RuntimeError, 'source identity unavailable'):
                editor.framework_pairing_preflight(policy, args, ROOT, history)

    @unittest.skipUnless(os.environ.get('BINIT_TEST_BINARY') and os.environ.get('BINIT_NATIVE', '1') == '1',
                         'matching native Binit artifact required')
    def test_binit_equivalent_framework_fixture_has_all_effects(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            work = Path(temporary) / 'case'
            binary = Path(os.environ['BINIT_TEST_BINARY']).resolve()
            bash = os.environ.get('BINIT_BASH', '/bin/bash')
            source = Path(os.environ.get('BINIT_ECOSYSTEM_SOURCE', ROOT / 'tmp/bash-ecosystem/sources'))
            rc, home = editor.configuration('equiv-binit', 'emacs', {'id': 'empty', 'entries': 0},
                                            work, binary, bash, source)
            shell = editor.corpus.BoundedShell(bash, rc, work, {**editor.corpus.environment(home), 'BINIT_BASH': bash})
            try:
                editor.frame(shell, 'wf_original_dir=$PWD; corpus_verify; wf_corpus_status=$?; '
                                    'cd -- "$wf_original_dir"; [[ $wf_corpus_status == 0 ]]')
                state = editor.state_directory('equiv-binit', work)
                self.assertTrue((state / 'current').is_file())
                editor.frame(shell, '[[ $(type -t binit) == function && $WF_MENU_STATUS == 0 '
                                    '&& $WF_HISTORY_STATUS == 0 && $WF_PROVIDER_STATUS == 0 ]]')
            finally:
                shell.close()

    def test_blesh_equivalent_configuration_enables_declared_features(self):
        source = Path(os.environ.get('BINIT_ECOSYSTEM_SOURCE', ROOT / 'tmp/bash-ecosystem/sources'))
        if not (source / 'ble/out/ble.sh').is_file():
            self.skipTest('pinned optional ble.sh corpus source not present')
        bash = os.environ.get('BINIT_BASH', '/bin/bash')
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            work = Path(temporary) / 'case'
            rc, home = editor.configuration('equiv-blesh-control', 'emacs', {'id': 'empty', 'entries': 0},
                                            work, ROOT / 'unused', bash, source)
            shell = editor.corpus.BoundedShell(bash, rc, work, editor.corpus.environment(home))
            try:
                editor.frame(shell, 'wf_original_dir=$PWD; corpus_verify; wf_corpus_status=$?; '
                                    'cd -- "$wf_original_dir"; [[ $wf_corpus_status == 0 ]]')
                editor.frame(shell, '[[ $WF_BLE_OPTIONS_STATUS == 0 && -n ${_ble_attached-} '
                                    '&& -z ${bleopt_highlight_syntax-} && -z ${bleopt_highlight_filename-} '
                                    '&& -z ${bleopt_highlight_variable-} '
                                    '&& :${bleopt_complete_auto_complete_opts-}: == *:syntax-disabled:* '
                                    '&& :${bleopt_complete_auto_complete_opts-}: != *:history-disabled:* ]]')
            finally:
                shell.close()

    def test_pinned_blesh_history_waits_for_the_rendered_match(self):
        source = Path(os.environ.get('BINIT_ECOSYSTEM_SOURCE', ROOT / 'tmp/bash-ecosystem/sources'))
        if not (source / 'ble/out/ble.sh').is_file():
            self.skipTest('pinned optional ble.sh corpus source not present')
        bash = os.environ.get('BINIT_BASH', '/bin/bash')
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            work = Path(temporary) / 'case'
            history = {'id': 'small', 'entries': 64}
            rc, home = editor.configuration('blesh', 'emacs', history, work, ROOT / 'unused', bash, source)
            shell = editor.corpus.BoundedShell(bash, rc, work, editor.corpus.environment(home))
            try:
                editor.frame(shell, '[[ $(history | wc -l) == 64 ]]')
                result = editor.history_trace(shell, 'blesh', history)
                self.assertEqual(result['recent'], {'visible': True, 'expected': True})
            finally:
                shell.close()

    def test_pinned_blesh_large_completion_ctrl_g_keeps_the_input(self):
        source = Path(os.environ.get('BINIT_ECOSYSTEM_SOURCE', ROOT / 'tmp/bash-ecosystem/sources'))
        if not (source / 'ble/out/ble.sh').is_file():
            self.skipTest('pinned optional ble.sh corpus source not present')
        bash = os.environ.get('BINIT_BASH', '/bin/bash')
        for mode in ('emacs', 'vi'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
                work = Path(temporary) / 'case'
                rc, home = editor.configuration('blesh', mode, {'id': 'empty', 'entries': 0},
                                                work, ROOT / 'unused', bash, source)
                shell = editor.corpus.BoundedShell(bash, rc, work, editor.corpus.environment(home))
                try:
                    editor.frame(shell, '[[ -n ${BLE_VERSION-} && -n ${_ble_attached-} ]]')
                    path = editor.tag(shell, work, 'large')
                    shell.settled(b' wf_many pre')
                    shell.settled(b'\t')
                    shell.settled(b'\x07')
                    editor.accepted(shell, b'')
                    editor.effect(path, b'pre\0')
                    editor.frame(shell, '[[ $WF_MANY_CALLS == 1 ]]')
                finally:
                    shell.close()

    def test_readline_large_completion_mode_keys_keep_the_input(self):
        bash = os.environ.get('BINIT_BASH', '/bin/bash')
        for mode in ('emacs', 'vi'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
                work = Path(temporary) / 'case'
                rc, home = editor.configuration('readline', mode, {'id': 'empty', 'entries': 0},
                                                work, ROOT / 'unused', bash, ROOT / 'unused')
                shell = editor.corpus.BoundedShell(bash, rc, work, editor.corpus.environment(home))
                try:
                    path = editor.tag(shell, work, 'large')
                    shell.settled(b' wf_many pre')
                    os.write(shell.fd, b'\t')
                    shell.read(.1)
                    shell.settled(b'\x07' if mode == 'emacs' else b'\x1bi')
                    editor.accepted(shell, b'')
                    editor.effect(path, b'pre\0')
                    editor.frame(shell, '[[ $WF_MANY_CALLS == 1 ]]')
                finally:
                    shell.close()

    @unittest.skipUnless(os.environ.get('BINIT_TEST_BINARY') and os.environ.get('BINIT_NATIVE', '1') == '1',
                         'matching native Binit artifact required')
    def test_real_binit_isolated_provider_preserves_suffix_without_stale_candidate(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            work = Path(temporary) / 'case'
            binary = Path(os.environ['BINIT_TEST_BINARY']).resolve()
            bash = os.environ.get('BINIT_BASH', '/bin/bash')
            rc, home = editor.configuration('binit', 'emacs', {'id': 'empty', 'entries': 0},
                                            work, binary, bash, ROOT / 'unused')
            shell = editor.corpus.BoundedShell(bash, rc, work,
                                               {**editor.corpus.environment(home), 'BINIT_BASH': bash})
            try:
                editor.frame(shell, '[[ $(type -t binit_complete) == builtin && $WF_MENU_STATUS == 0 '
                                    '&& $WF_HISTORY_STATUS == 0 && $WF_PROVIDER_STATUS == 0 ]]')
                path = editor.tag(shell, work, 'isolated')
                shell.settled(b' wf_iso al')
                os.write(shell.fd, b'\t')
                shell.until((work / 'provider-started').is_file)
                os.write(shell.fd, b'x')
                shell.until(lambda: 'x' in editor.screen_tail(shell))
                editor.accepted(shell, b'')
                self.assertEqual(path.read_bytes(), b'alx\0')
                observation = editor.isolated_effect('binit', path.read_bytes(), 1.)
                self.assertTrue(observation['input_preserved'])
                self.assertFalse(observation['candidate_inserted'])
            finally:
                shell.close()

    def test_real_readline_synchronous_provider_records_completion_and_typed_suffix(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            work = Path(temporary) / 'case'
            bash = os.environ.get('BINIT_BASH', '/bin/bash')
            rc, home = editor.configuration('readline', 'emacs', {'id': 'empty', 'entries': 0},
                                            work, ROOT / 'unused', bash, ROOT / 'unused')
            shell = editor.corpus.BoundedShell(bash, rc, work, editor.corpus.environment(home))
            try:
                path = editor.tag(shell, work, 'isolated')
                shell.settled(b' wf_iso al')
                os.write(shell.fd, b'\t')
                shell.until((work / 'provider-started').is_file)
                os.write(shell.fd, b'x')
                deadline = time.monotonic() + 1
                while 'x' not in editor.screen_tail(shell) and time.monotonic() < deadline:
                    shell.read(.01)
                editor.accepted(shell, b'')
                actual = path.read_bytes()
                self.assertEqual(actual, b'alpha beta\0x\0')
                observation = editor.isolated_effect('readline', actual, 1.)
                self.assertTrue(observation['input_preserved'])
                self.assertTrue(observation['candidate_inserted'])
            finally:
                shell.close()

    def test_real_readline_completion_context_with_history_isolation(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            work = Path(temporary) / 'case'
            bash = os.environ.get('BINIT_BASH', '/bin/bash')
            rc, home = editor.configuration('readline', 'emacs', {'id': 'empty', 'entries': 0},
                                            work, ROOT / 'unused', bash, ROOT / 'unused')
            shell = editor.corpus.BoundedShell(bash, rc, work, editor.corpus.environment(home))
            try:
                path = editor.tag(shell, work, 'completion')
                shell.settled(b' wf_one al')
                shell.settled(b'\t')
                editor.accepted(shell, b'')
                editor.effect(path, b'alpha beta\0')
                editor.frame(shell, '[[ $WF_CALLS == 1 && $WF_COMP_CWORD == 1 && '
                    '$WF_COMP_LINE == "wf_one al" && $WF_COMP_WORD0 == wf_one && $WF_COMP_WORD1 == al ]]')
                with self.assertRaisesRegex(RuntimeError, 'editor probe failed'):
                    editor.frame(shell, '[[ $WF_COMP_LINE == " wf_one al" ]]')
            finally:
                shell.close()

    def test_binit_isolated_input_stays_strict_while_synchronous_results_remain_visible(self):
        result = editor.isolated_effect('binit', b'alx\0', .2)
        self.assertTrue(result['input_preserved'])
        self.assertFalse(result['candidate_inserted'])
        for actual, response in [(b'alpha betax\0', .2), (b'al\0', .2), (b'alx\0', None)]:
            with self.subTest(actual=actual, response=response):
                with self.assertRaises(editor.EditorBehaviorMismatch):
                    editor.isolated_effect('binit', actual, response)
        synchronous = editor.isolated_effect('readline', b'alpha betax\0', 220.)
        self.assertEqual(bytes.fromhex(synchronous['observed_argument_hex']), b'alpha betax\0')
        self.assertTrue(synchronous['candidate_inserted'])
        self.assertTrue(synchronous['input_preserved'])
        lost = editor.isolated_effect('blesh', b'alpha beta\0', None)
        self.assertFalse(lost['input_preserved'])
        with self.assertRaisesRegex(RuntimeError, 'did not retain'):
            editor.isolated_effect('readline', None, None)

    def test_preflight_infrastructure_error_rejects_the_whole_receipt(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            base = Path(temporary)
            output = base / 'receipt.json'
            policy = json.loads(editor.POLICY.read_text())
            argv = ['editor', '--sources', str(base), '--binary', '/bin/true', '--work', str(base / 'work'),
                    '--output', str(output), '--runs', '1', '--pairs', '1', '--events', '1']
            with patch.object(sys, 'argv', argv), patch.object(editor.corpus, 'verify_sources', return_value={}), \
                    patch.object(editor, 'digest', return_value=policy['sources']['ble_entry_sha256']), \
                    patch.object(editor, 'identity', return_value={}), \
                    patch.object(editor.subprocess, 'check_output', return_value='Bash fixture\n'), \
                    patch.object(editor, 'pairing_preflight', side_effect=RuntimeError('fixture unavailable')):
                with self.assertRaisesRegex(RuntimeError, 'fixture unavailable'):
                    editor.main()
            receipt = json.loads(output.read_text())
            self.assertFalse(receipt['complete_two_run_editor'])
            self.assertEqual(receipt['runs'], [])
            self.assertIn('fixture unavailable', receipt['error'])
            self.assertNotIn('pairing_compatibility', receipt)

    def test_ble_private_copy_excludes_runtime_cache_but_source_is_still_hashed(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            directory = Path(temporary)
            source = directory / 'source'
            (source / 'cache.d/1000').mkdir(parents=True)
            (source / 'ble.sh').write_text('pinned\n')
            (source / 'cache.d/1000/old').write_text('old cache\n')
            before = editor.corpus.inventory(source)
            copied = editor.copy_ble_build(source, directory / 'private')
            self.assertEqual(copied, {'ble.sh': before['ble.sh']})
            self.assertFalse((directory / 'private/cache.d').exists())
            (directory / 'private/cache.d').mkdir()
            (directory / 'private/cache.d/new').write_text('new cache\n')
            self.assertEqual(editor.corpus.inventory(source), before)
            (source / 'cache.d/1000/old').write_text('changed\n')
            self.assertNotEqual(editor.corpus.inventory(source), before)

    def test_pairing_preflight_does_not_mask_infrastructure_or_control_failures(self):
        args = SimpleNamespace(work=ROOT / 'tmp', binary=Path('/bin/true'), bash='/bin/bash', events=1)
        policy = {'modes': ['emacs']}
        history = {'id': 'small', 'entries': 64}
        def infrastructure(subject, *_args):
            if subject == 'binit-blesh':
                raise RuntimeError('prepare or harness failure')
            return {'passed': True}
        with patch.object(editor, 'run_trace', side_effect=infrastructure):
            with self.assertRaisesRegex(RuntimeError, 'prepare or harness'):
                editor.pairing_preflight(policy, args, ROOT, history)
        def bad_control(subject, *_args):
            if subject == 'binit':
                raise editor.EditorBehaviorMismatch('standalone failed')
            return {'passed': True}
        with patch.object(editor, 'run_trace', side_effect=bad_control):
            with self.assertRaisesRegex(editor.EditorBehaviorMismatch, 'standalone failed'):
                editor.pairing_preflight(policy, args, ROOT, history)
        def observed(subject, *_args):
            if subject == 'binit-blesh':
                raise editor.EditorBehaviorMismatch('accepted argument differs')
            return {'passed': True}
        with patch.object(editor, 'run_trace', side_effect=observed):
            result = editor.pairing_preflight(policy, args, ROOT, history)
        self.assertFalse(result['passed'])
        self.assertEqual(result['modes']['emacs']['classification'], 'observed_behavior_mismatch')
        self.assertEqual(set(result['modes']['emacs']['controls']), {'binit', 'blesh'})

    def test_histories_have_declared_size_and_old_marker_outside_window(self):
        self.assertEqual(editor.history_text(0), '')
        self.assertEqual(len(editor.history_text(64).splitlines()), 64)
        lines = editor.history_text(2048).splitlines()
        self.assertEqual(len(lines), 2048)
        self.assertIn('editor-historic-needle', lines[0])
        self.assertIn('editor-recent-needle', lines[-1])
        self.assertNotIn('editor-historic-needle', '\n'.join(lines[-1000:]))

    def test_typing_p99_needs_1000_acknowledged_events_per_key(self):
        class FakeShell:
            active = False
            def settled(self, data):
                self.active = data == b'@'
                return 1. if self.active else 2.
        shell = FakeShell()
        def screen(_):
            return 'BENCH> @' if shell.active else 'BENCH> '
        with patch.object(editor, 'screen_tail', side_effect=screen):
            short = editor.typing_trace(shell, 3)
            full = editor.typing_trace(shell, 1000)
        self.assertIsNone(short['per_key']['insert']['p99_ms'])
        self.assertEqual(full['per_key']['insert']['p99_ms'], 1.)
        self.assertEqual(full['per_key']['delete']['events'], 1000)

    def test_unacknowledged_key_fails_instead_of_recording_a_sample(self):
        class FakeShell:
            def settled(self, _):
                return 1.
        with patch.object(editor, 'screen_tail', return_value='BENCH> '):
            with self.assertRaisesRegex(RuntimeError, 'not acknowledged'):
                editor.typing_trace(FakeShell(), 1)

    def test_exact_utf8_and_nul_termination_are_checked(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            path = Path(temporary) / 'effect'
            path.write_bytes('caffè 雪\0'.encode())
            editor.effect(path, 'caffè 雪\0'.encode())
            with self.assertRaisesRegex(RuntimeError, 'argument effect differs'):
                editor.effect(path, 'caffè 雪'.encode())

    def test_summary_rejects_missing_duplicate_failed_and_bad_events(self):
        row = valid_row()
        result = editor.run_summary([row], ['binit'], ['vi'], ['small'], 1, 1)
        self.assertIsNone(result['binit/vi/small']['insert']['p99_ms'])
        self.assertEqual(result['binit/vi/small']['completion_first']['median_ms'], 1.)
        self.assertEqual(result['binit/vi/small']['completion_first_accept']['median_ms'], 10.)
        for rows in ([], [row, row], [{**row, 'passed': False}], [{**row, 'pair': 1}]):
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                editor.run_summary(rows, ['binit'], ['vi'], ['small'], 1, 1)
        broken = valid_row()
        broken['traces']['typing']['samples_ms']['insert'] = [math.nan]
        with self.assertRaisesRegex(RuntimeError, 'invalid or incomplete'):
            editor.run_summary([broken], ['binit'], ['vi'], ['small'], 1, 1)

    def test_policy_keeps_different_editor_and_provider_roles(self):
        policy = json.loads(editor.POLICY.read_text())
        self.assertEqual({row['id'] for row in policy['subjects']}, {'readline', 'binit', 'blesh'})
        traces = {row['id']: row for row in policy['trace']}
        self.assertEqual(traces['typing']['events_per_key_per_pair'], 50)
        self.assertEqual(traces['typing']['events_per_key_per_case_per_run'], 1000)
        self.assertEqual(traces['completion-large-cancel']['candidates'], 1024)
        self.assertIn('isolated-slow-cancel', traces)
        self.assertIn('stateful-slow-provider', traces)
        self.assertTrue(any('newest 1000' in item for item in policy['comparability']))

    def test_p99_uses_at_least_1000_events_in_each_run_and_key(self):
        rows = []
        for pair in range(20):
            row = valid_row()
            row['pair'] = pair
            row['traces']['typing']['samples_ms'] = {'insert': [1.] * 50, 'delete': [2.] * 50}
            rows.append(row)
        result = editor.run_summary(rows, ['binit'], ['vi'], ['small'], 20, 50)['binit/vi/small']
        self.assertEqual(result['insert']['events'], 1000)
        self.assertEqual(result['delete']['p99_ms'], 2.)
        short = editor.run_summary(rows[:-1], ['binit'], ['vi'], ['small'], 19, 50)['binit/vi/small']
        self.assertIsNone(short['insert']['p99_ms'])


if __name__ == '__main__':
    unittest.main()
