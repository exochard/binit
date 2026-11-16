#!/usr/bin/env python3
"""Score Binit against its competitors under the frozen publication rules.

Reads a primed framework receipt and, when given, an editor corpus receipt, then
applies benchmarks/publication-scoreboard.json without changing it.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import statistics

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / 'benchmarks/publication-scoreboard.json'


def statistic(values, name):
    if not values:
        raise ValueError('no observations')
    if name == 'median':
        return statistics.median(values)
    quantile = {'p95': .95, 'p99': .99}[name]
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * quantile) - 1]


def interval(estimates, confidence=.95):
    ordered = sorted(estimates)
    tail = (1 - confidence) / 2
    low = ordered[max(0, math.floor(tail * len(ordered)))]
    high = ordered[min(len(ordered) - 1, math.ceil((1 - tail) * len(ordered)) - 1)]
    return low, high


def compare(subject_blocks, competitor_blocks, name, rule, absolute_tie_ms, seed_key):
    """Paired block bootstrap of one run; each block is a list of observations."""
    if len(subject_blocks) != len(competitor_blocks) or not subject_blocks:
        raise ValueError('paired runs need the same nonzero number of blocks')
    pool = lambda blocks: [value for block in blocks for value in block]
    point_s, point_c = statistic(pool(subject_blocks), name), statistic(pool(competitor_blocks), name)
    seed = int(hashlib.sha256(f"{rule['seed']}:{seed_key}".encode()).hexdigest()[:16], 16)
    generator = random.Random(seed)
    count = len(subject_blocks)
    ratios, differences = [], []
    for _ in range(rule['bootstrap_resamples']):
        chosen = [generator.randrange(count) for _ in range(count)]
        s = statistic([v for i in chosen for v in subject_blocks[i]], name)
        c = statistic([v for i in chosen for v in competitor_blocks[i]], name)
        ratios.append(s / c)
        differences.append(s - c)
    ratio_low, ratio_high = interval(ratios)
    diff_low, diff_high = interval(differences)
    within_floor = absolute_tie_ms > 0 and -absolute_tie_ms <= diff_low and diff_high <= absolute_tie_ms
    if ratio_high - ratio_low > rule['max_interval_width']:
        verdict = 'tie' if within_floor else 'inconclusive'
    elif ratio_high < rule['win_upper']:
        verdict = 'win'
    elif within_floor:
        verdict = 'tie'
    elif ratio_low > rule['loss_lower'] and diff_low > absolute_tie_ms:
        verdict = 'loss'
    elif ratio_high <= rule['tie_upper']:
        verdict = 'tie'
    else:
        verdict = 'inconclusive'
    return {'binit': point_s, 'competitor': point_c, 'ratio': point_s / point_c,
            'ratio_interval': [ratio_low, ratio_high], 'difference_interval_ms': [diff_low, diff_high],
            'verdict': verdict}


def combine_runs(verdicts):
    if len(set(verdicts)) == 1:
        return verdicts[0]
    if set(verdicts) == {'win', 'tie'}:
        return 'tie'
    return 'inconclusive'


def combine_aspect(verdicts):
    if not verdicts:
        return 'not measured'
    if 'loss' in verdicts:
        return 'loss'
    if all(v == 'win' for v in verdicts):
        return 'win'
    if all(v in ('win', 'tie') for v in verdicts):
        return 'tie'
    return 'inconclusive'


def framework_blocks(run, case, metric):
    rows = sorted((row for row in run if row['case'] == case), key=lambda row: row['pair'])
    if not rows or [row['pair'] for row in rows] != list(range(len(rows))):
        raise ValueError(f'missing or duplicate framework observations for {case}')
    return [[row[metric]] for row in rows]


def score_framework(aspect, receipt, rule):
    if not receipt.get('complete_two_run_primed_framework'):
        raise ValueError('framework receipt is not a complete two-run corpus')
    runs = receipt['runs']
    if len(runs) != rule['runs_required']:
        raise ValueError('framework receipt has the wrong number of runs')
    count = aspect['plugins']
    rows = {}
    for subject in [aspect['subject'], *aspect['labelled_rows']]:
        for competitor in aspect['competitors']:
            per_run = [compare(framework_blocks(run, f'{subject}-{count}', aspect['metric']),
                               framework_blocks(run, f'{competitor}-{count}', aspect['metric']),
                               aspect['statistic'], rule, aspect['absolute_tie_ms'],
                               f"{aspect['id']}:{subject}:{competitor}:{index}")
                       for index, run in enumerate(runs)]
            rows[f'{subject} vs {competitor}'] = {'runs': per_run,
                                                  'verdict': combine_runs([r['verdict'] for r in per_run])}
    reported = {}
    for subject in ['bash', aspect['subject'], *aspect['labelled_rows'], *aspect['competitors'], 'zsh', 'zinit']:
        reported[subject] = {metric: [statistic([b[0] for b in framework_blocks(run, f'{subject}-{count}', metric)], 'median')
                                      for run in runs]
                             for metric in [aspect['metric'], *aspect['reported']]}
    overhead = {name: [statistic([b[0] for b in framework_blocks(run, f'{name}-{count}', aspect['metric'])], 'median')
                       - statistic([b[0] for b in framework_blocks(run, f'{control}-{count}', aspect['metric'])], 'median')
                       for run in runs]
                for name, control in (('binit', 'bash'), ('zinit', 'zsh'))}
    scored = [rows[f"{aspect['subject']} vs {c}"]['verdict'] for c in aspect['competitors']]
    labelled = {name: combine_aspect([rows[f'{name} vs {c}']['verdict'] for c in aspect['competitors']])
                for name in aspect['labelled_rows']}
    return {'verdict': combine_aspect(scored), 'labelled_verdicts': labelled, 'comparisons': rows,
            'medians_ms_by_run': reported, 'framework_overhead_ms_by_run': overhead}


def editor_blocks(run, subject, mode, history, metric):
    rows = sorted((row for row in run if (row['subject'], row['mode'], row['history']) == (subject, mode, history)),
                  key=lambda row: row['pair'])
    if not rows or [row['pair'] for row in rows] != list(range(len(rows))):
        raise ValueError(f'missing or duplicate editor observations for {subject}/{mode}/{history}')
    if 'event' in metric:
        return [list(row['traces'][metric['trace']]['samples_ms'][metric['event']]) for row in rows]
    path = EDITOR_TIMED[metric['name']]
    blocks = []
    for row in rows:
        value = row['traces']
        for field in path:
            value = value[field]
        if value is None:
            raise ValueError(f'missing {metric["name"]} for {subject}/{mode}/{history}')
        blocks.append([value])
    return blocks


EDITOR_TIMED = {
    'isolated_response': ('isolated-slow-cancel', 'input_response_ms'),
    'isolated_accept': ('isolated-slow-cancel', 'elapsed_ms'),
    'large_cancel': ('completion-large-cancel', 'elapsed_ms'),
    'stateful_completion': ('stateful-slow-provider', 'completion_elapsed_ms'),
    'stateful_accept': ('stateful-slow-provider', 'accepted_elapsed_ms'),
}


def metric_label(metric):
    return f"{metric.get('event') or metric['name']} {metric['statistic']}"


def score_editor(aspect, receipt, rule):
    if not receipt.get('complete_two_run_editor'):
        raise ValueError('editor receipt is not a complete two-run corpus')
    runs = receipt['runs']
    if len(runs) != rule['runs_required']:
        raise ValueError('editor receipt has the wrong number of runs')
    cells = sorted({(row['mode'], row['history']) for row in runs[0]})
    rows, scored = {}, []
    for mode, history in cells:
        for metric in aspect['metrics']:
            for competitor in aspect['competitors']:
                key = f"{mode}/{history} {metric_label(metric)} vs {competitor}"
                per_run = [compare(editor_blocks(run, aspect['subject'], mode, history, metric),
                                   editor_blocks(run, competitor, mode, history, metric),
                                   metric['statistic'], rule, aspect['absolute_tie_ms'],
                                   f"{aspect['id']}:{key}:{index}")
                           for index, run in enumerate(runs)]
                rows[key] = {'runs': per_run, 'verdict': combine_runs([r['verdict'] for r in per_run])}
                scored.append(rows[key]['verdict'])
    reported = {}
    extra = [{'name': name, 'statistic': 'median'} for name in aspect.get('stateful_controls', [])]
    for subject in [aspect['subject'], *aspect['competitors'], *aspect['controls']]:
        for mode, history in cells:
            for metric in [*aspect['metrics'], *extra]:
                reported[f"{subject} {mode}/{history} {metric_label(metric)}"] = [
                    statistic([v for block in editor_blocks(run, subject, mode, history, metric) for v in block],
                              metric['statistic'])
                    for run in runs]
    return {'verdict': combine_aspect(scored), 'comparisons': rows, 'values_ms_by_run': reported}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--framework', type=Path, required=True)
    parser.add_argument('--editor', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output exists')
    policy = json.loads(POLICY.read_text())
    rule = policy['rule']
    framework = json.loads(args.framework.read_text())
    editor = json.loads(args.editor.read_text()) if args.editor else None
    result = {'policy_sha256': hashlib.sha256(POLICY.read_bytes()).hexdigest(),
              'framework_sha256': hashlib.sha256(args.framework.read_bytes()).hexdigest(),
              'editor_sha256': hashlib.sha256(args.editor.read_bytes()).hexdigest() if args.editor else None,
              # The measured binary and driver identities, so a verdict names what it was computed for.
              'framework_inputs': framework.get('inputs', {}).get('files'),
              'editor_inputs': editor.get('inputs') if editor else None,
              'aspects': {}}
    for aspect in policy['aspects']:
        if aspect['id'] in ('first-usable-input', 'all-effects-ready'):
            result['aspects'][aspect['id']] = score_framework(aspect, framework, rule)
        elif 'metrics' in aspect and editor is not None:
            result['aspects'][aspect['id']] = score_editor(aspect, editor, rule)
        else:
            result['aspects'][aspect['id']] = {'verdict': 'not measured', 'reason': aspect.get('status', 'no receipt given')}
    verdicts = [row['verdict'] for row in result['aspects'].values()]
    measured = [v for v in verdicts if v != 'not measured']
    result['publication_gate'] = {
        'measured': len(measured), 'win_or_tie': sum(v in ('win', 'tie') for v in measured),
        'passed': len(measured) >= 4 and all(v in ('win', 'tie') for v in measured)}
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=1)
        stream.write('\n')
    for name, row in result['aspects'].items():
        print(f'{name}: {row["verdict"]}')
    print('publication gate:', 'passed' if result['publication_gate']['passed'] else 'not passed')


if __name__ == '__main__':
    main()
