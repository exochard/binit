"""Guard the experiment's acceptance bar against false qualification."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
with patch.object(sys, 'path', [str(ROOT / 'scripts'), *sys.path]):
    SPEC = importlib.util.spec_from_file_location('syntax_probe', ROOT / 'scripts/syntax_probe.py')
    probe = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(probe)


class ReceiptTests(unittest.TestCase):
    def receipt(self, *, overflow=False, observer=True, plain_state='unchanged', interrupted=True):
        with tempfile.TemporaryDirectory(prefix='.scratch-syntax-test-', dir=ROOT) as temporary:
            root = Path(temporary)
            corpus = root / 'corpus.json'
            corpus.write_text(json.dumps({'cases': [{'id': 'case', 'text': 'printf x', 'parsed': True, 'spans': ['printf']}]}))
            binary = root / 'binary'
            binary.write_text('fixture')
            output = root / 'report.json'
            def invoke(_bash, _module, _case, _directory, mode, debug=True):
                state = plain_state if not debug and mode == 'parse' else 'unchanged'
                return {'returncode': 0, 'state': state + '\nSTATE survived',
                        'side_effect_file': False, 'observation': {'parsed': True, 'observer': observer,
                            'overflow': overflow, 'tokens': [[281, 1, 0, 1, 6]]}}
            with patch.object(probe, 'ROOT', root), patch.object(probe, 'CORPUS', corpus):
                with patch.object(probe, 'invoke', side_effect=invoke), patch.object(probe, 'interrupt_check',
                     return_value={'recovered': True, 'parser_phase_confirmed': interrupted}):
                    with contextlib.redirect_stdout(io.StringIO()):
                        probe.check(binary, binary, output)
            return json.loads(output.read_text())

    def test_complete_observation_can_meet_the_frozen_bar(self):
        self.assertTrue(self.receipt()['editor_ready'])

    def test_truncated_or_absent_observer_cannot_qualify(self):
        self.assertFalse(self.receipt(overflow=True)['editor_ready'])
        self.assertFalse(self.receipt(observer=False)['editor_ready'])

    def test_trap_free_state_and_controlled_interruption_are_required(self):
        self.assertFalse(self.receipt(plain_state='clobbered')['editor_ready'])
        self.assertFalse(self.receipt(interrupted=False)['editor_ready'])

    def test_cross_line_out_of_range_coordinates_are_rejected(self):
        case = {'text': 'echo x\nprintf y', 'spans': ['printf']}
        observation = {'tokens': [[281, 1, 7, 1, 13]]}
        self.assertEqual(probe.span_matches(case, observation), {'printf': False})


if __name__ == '__main__':
    unittest.main()
