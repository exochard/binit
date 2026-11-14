"""Materialize the frozen diagnostic cases and check observable results."""
import json
from pathlib import Path


def materialize(root, case):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    plugin_paths = []
    for plugin in case['plugins']:
        directory = root / plugin['id']
        directory.mkdir()
        lines = ['schema=1', f'id={json.dumps(plugin["id"])}', 'entry="init.bash"']
        for key in ('load', 'event', 'depends', 'commands', 'completions'):
            if key in plugin:
                lines.append(f'{key}={json.dumps(plugin[key])}')
        (directory / 'plugin.toml').write_text('\n'.join(lines) + '\n')
        (directory / 'init.bash').write_text(plugin['entry'])
        plugin_paths.append(plugin['id'])
    config = root / 'config.toml'
    config.write_text(('schema=1\n' if plugin_paths else 'schema=1\nplugins=[]\n') + ''.join(
        f'[[plugins]]\npath={json.dumps(name)}\n' for name in plugin_paths))
    (root / 'check.bash').write_text(case['script'])
    body = 'schema=1\n[[checks]]\nid="fixture"\nscript="check.bash"\ntimeout_ms=3000\nmax_elapsed_ms=2500\n'
    if case['setup']:
        (root / 'setup.bash').write_text(case['setup'])
        body += 'setup="setup.bash"\n'
    plan = root / 'checks.toml'
    plan.write_text(body)
    return config, plan, root / 'state'


def validate(case, payload, exit_code, targets):
    expected = case['expected_success']
    if (exit_code == 0) != expected or payload['passed'] != expected:
        raise AssertionError(f'{case["id"]}: diagnostic outcome differs from the frozen effect check')
    if len(payload['diagnostics']) != 1:
        raise AssertionError('one diagnostic report is required')
    report = payload['diagnostics'][0]
    if not report['complete'] or not report['trace']['complete'] or not report['namespace']['complete']:
        raise AssertionError(f'{case["id"]}: diagnostic evidence is incomplete')
    phase = next(p for p in report['trace']['phases']
                 if p['process'] == 0 and p['name'] == case['expected_phase'])
    if phase['wall_us'] < case['minimum_window_ms'] * 1000:
        raise AssertionError('controlled interval is too short')
    causes = report['trace']['causes']
    for item in report['trace']['phases']:
        exclusive = sum(row['exclusive_us'] for row in causes
                        if row['process'] == item['process'] and row['phase'] == item['name'])
        if abs(exclusive - item['attributed_us']) > targets['maximum_exclusive_overcount_us']:
            raise AssertionError('exclusive attribution does not match the attributed interval')
        if exclusive > item['wall_us'] + targets['maximum_exclusive_overcount_us']:
            raise AssertionError('exclusive attribution double counts wall time')
    if case['primary']:
        if phase['attributed_us'] / phase['wall_us'] < targets['minimum_attribution_fraction']:
            raise AssertionError('controlled root interval is less than 95% attributed')
    for expected_cause in case['expected_causes']:
        matching = [row for row in causes if row['phase'] == case['expected_phase']
                    and row['function'] == expected_cause['function']
                    and row['plugin'] == expected_cause['plugin']
                    and (row['process'] > 0) == expected_cause.get('child_process', False)]
        exclusive = sum(row['exclusive_us'] for row in matching)
        if exclusive < expected_cause['minimum_exclusive_ms'] * 1000:
            raise AssertionError(f'missing planted cause: {expected_cause["function"]}, {exclusive} us')
    if case.get('require_opaque_child_time') and phase['unattributed_us'] < 200000:
        raise AssertionError('child expansion must remain opaque in the root interval')
    if 'expected_conflict' in case:
        conflict = case['expected_conflict']
        if not any(all(row.get(key) == value for key, value in conflict.items())
                   for row in report['namespace']['conflicts']):
            raise AssertionError('missing planted conflict and its known owner')
    if 'diagnostic_argument_marker_must_not_be_retained' in json.dumps(report):
        raise AssertionError('diagnostic retained command arguments')
    return report
