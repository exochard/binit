#!/usr/bin/env python3
"""Compare one development milestone typing or isolated-provider cell."""
import argparse
import gc
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile

import bench_ecosystem as corpus
import bench_ecosystem_editor as editor
from bench_corpus_terminal import CheckedShell
from bench_development_milestone import BASELINE, BINIT_HASHES, POLICY, ROOT, ZINIT_HASH
from bench_history_milestone import prepare as prepare_history, stop_run
from bench_interactive import digest
from bench_nvm_milestone import p95


def case_identity(label, directory):
    if label == 'zinit':
        return {name: digest(directory / name) for name in ('home/.zshrc', 'history.txt')}
    result = editor.case_identity('binit', directory)
    # Prime and timed starts may write a verification cache for the same locked files.
    result['state'] = {path: value for path, value in result['state'].items() if not path.endswith('.verified')}
    return result


def configure(label, directory, binary, args, work, source, history):
    if label == 'zinit':
        directory.mkdir()
        path = directory / 'history.txt'
        path.write_text(editor.history_text(history['entries']))
        rc, env, launch = prepare_history(directory, None, source, args, path)
        # This probe must neither add a history entry nor race ZLE's first key.
        with rc.open('a') as config:
            config.write(
                'setopt HIST_IGNORE_SPACE\n'
                f'if (( $+functions[zinit] && ${{#history}} == {history["entries"]} )); then '
                'printf "\\036EDITOR-READY\\037"; fi\n')
        return rc, env, launch
    bash = str(args.bash)
    rc, home = editor.configuration('binit', args.editor, history, directory, binary, bash, work)
    return rc, corpus.environment(home) | {'BINIT_BASH': bash, 'XDG_RUNTIME_DIR': str(directory / 'runtime')}, None


def observe(label, directory, scenario, args, history, trace, row):
    rc, env, launch = scenario
    shell = CheckedShell(str(args.bash), rc, directory, env, launch, receipt_path=trace)
    ready = False
    try:
        corpus.wait_for_editor(shell)
        if label == 'zinit':
            shell.until(lambda: b'\x1eEDITOR-READY\x1f' in shell.raw)
        else:
            editor.frame(shell, '[[ $(type -t binit) == function && $(type -t binit_complete) == builtin '
                         '&& $WF_MENU_STATUS == 0 && $WF_HISTORY_STATUS == 0 && $WF_PROVIDER_STATUS == 0 ]]'
                         f' && [[ $(history | wc -l) == {history["entries"]} ]]')
        if args.workload == 'typing':
            row['typing'] = editor.typing_trace(shell, args.events)
            editor.frame(shell, 'true')
        else:
            (directory / 'provider-started').unlink(missing_ok=True)
            (directory / 'effects' / 'isolated-cancel').unlink(missing_ok=True)
            editor.isolated_cancellation(shell, 'binit', directory, row)
        ready = True
    finally:
        shell.close(ready=ready)


def grade(args, policy, runs, labels, failures):
    endpoint = ('insert', 'delete') if args.workload == 'typing' else ('input_response',)
    threshold = policy['timing']['maximum_warm_typing_p95_ms' if args.workload == 'typing'
                                 else 'maximum_isolated_provider_response_ms']
    volume = (args.runs == policy['timing']['runs'] and args.pairs == policy['timing']['pairs_per_run']
              and (args.workload != 'typing' or args.events == 50))
    summaries = []
    for run in runs:
        by_label = {label: [row for row in run if row['case'] == label and row.get('effects_passed')]
                    for label in labels}
        values = {}
        for label, rows in by_label.items():
            if len(rows) != args.pairs:
                continue
            if args.workload == 'typing':
                values[label] = {key: p95([value for row in rows for value in row['typing']['samples_ms'][key]])
                                 for key in endpoint}
            else:
                values[label] = {'input_response': p95([
                    row['traces']['isolated-slow-cancel']['input_response_ms'] for row in rows])}
        candidate = values.get('candidate', {})
        ratios = {label: {key: candidate[key] / value for key, value in metrics.items()}
                  for label, metrics in values.items() if label != 'candidate' and candidate}
        summaries.append({
            'p95_ms': values, 'ratios': ratios,
            'absolute_pass': bool(candidate) and all(value <= threshold for value in candidate.values()),
            'relative_pass': len(ratios) == len(labels) - 1 and all(
                value <= policy['timing']['maximum_win_ratio'] for metrics in ratios.values()
                for value in metrics.values()),
        })
    return {'sample_count_qualified': volume, 'runs': summaries,
            'pass': volume and not failures and all(row['absolute_pass'] and row['relative_pass'] for row in summaries)}


def main():
    signal.signal(signal.SIGTERM, stop_run)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--baseline-dir', default=BASELINE, type=Path)
    parser.add_argument('--bash', default=Path('/usr/bin/bash'), type=Path)
    parser.add_argument('--zsh', default=Path('/usr/bin/zsh'), type=Path)
    parser.add_argument('--editor', required=True, choices=('emacs', 'vi'))
    parser.add_argument('--history', default='small', choices=('empty', 'small', 'large'))
    parser.add_argument('--workload', required=True, choices=('typing', 'slow-provider'))
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--runs', type=int, default=2)
    parser.add_argument('--pairs', type=int, default=20)
    parser.add_argument('--events', type=int, default=50)
    args = parser.parse_args()
    for name in ('candidate', 'baseline_dir', 'bash', 'zsh', 'output'):
        setattr(args, name, getattr(args, name).resolve())
    if not args.output.is_relative_to(ROOT / 'tmp') or args.output.exists() or min(args.runs, args.pairs, args.events) < 1:
        parser.error('use a fresh output under tmp/ and positive counts')
    if args.workload == 'slow-provider' and args.history != 'small':
        parser.error('the isolated provider fixture uses the 64-entry history')

    policy = json.loads(POLICY.read_text())
    editor_policy = json.loads(editor.POLICY.read_text())
    history = next(row for row in editor_policy['histories'] if row['id'] == args.history)
    abi = subprocess.check_output([args.bash, '-c', 'printf "%s%s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'],
                                  text=True)
    baseline, archive = args.baseline_dir / f'binit-{abi}', args.baseline_dir / 'zinit.tar.gz'
    typing = args.workload == 'typing'
    if abi not in BINIT_HASHES or digest(baseline) != BINIT_HASHES[abi] or (typing and digest(archive) != ZINIT_HASH):
        parser.error('a frozen baseline artifact does not match')
    labels = {'candidate': args.candidate, 'baseline': baseline}
    if typing:
        labels['zinit'] = None
    inputs = [Path(__file__), POLICY, editor.POLICY, baseline, args.candidate, args.bash, Path(sys.executable),
              *(ROOT / 'scripts' / filename for filename in (
                  'bench_ecosystem_editor.py', 'bench_interactive.py', 'bench_corpus_terminal.py',
                  'bench_ecosystem.py', 'bench_history_milestone.py', 'bench_nvm_milestone.py'))]
    if typing:
        inputs.extend((archive, args.zsh))
    hashes = {str(path): digest(path) for path in inputs}
    result = {'schema': 1, 'abi': abi, 'editor': args.editor, 'history': history, 'workload': args.workload,
              'input_sha256': hashes, 'runs': [], 'failures': {}, 'complete': False,
              'comparability': {'zinit': 'same insert/delete and history effects' if typing else
                               'isolated and cancellable Bash completion provider has no Zinit endpoint'}}
    args.output.mkdir(parents=True)
    work, receipt = args.output / 'work', args.output / 'result.json'
    work.mkdir()
    source = work / 'zinit-source'
    failed = result['failures']
    try:
        if typing:
            source.mkdir()
            with tarfile.open(archive) as bundle:
                bundle.extractall(source, filter='data')
        source_inventory = corpus.inventory(source) if typing else None
        scenarios = {label: configure(label, work / label, binary, args, work, source, history)
                     for label, binary in labels.items()}
        result['scenarios'] = {label: case_identity(label, work / label) for label in labels}
        gc.disable()
        for run in range(args.runs):
            rows = []
            result['runs'].append(rows)
            for pair in range(args.pairs):
                for label in list(labels)[::1 if (run + pair) % 2 == 0 else -1]:
                    if label in failed:
                        continue
                    trace = args.output / 'terminals' / f'{run}-{pair}-{label}.raw'
                    row = {'case': label, 'pair': pair, 'terminal': str(trace.relative_to(args.output)), 'traces': {}}
                    rows.append(row)
                    try:
                        observe(label, work / label, scenarios[label], args, history, trace, row)
                        row['effects_passed'] = True
                    except (RuntimeError, OSError) as error:
                        row['error'] = failed[label] = f'{type(error).__name__}: {error}'
                editor.save(receipt, result)
            if (any(digest(path) != previous for path, previous in hashes.items())
                    or any(case_identity(label, work / label) != previous
                           for label, previous in result['scenarios'].items())
                    or (typing and corpus.inventory(source) != source_inventory)):
                raise RuntimeError('editor benchmark inputs changed')
        result['grade'] = grade(args, policy, result['runs'], labels, failed)
        result['complete'] = not failed
    except BaseException as error:
        result['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        editor.save(receipt, result)
        shutil.rmtree(work)
    print(json.dumps({'grade': result['grade'], 'failures': failed}), flush=True)
    return int(bool(failed))


if __name__ == '__main__':
    raise SystemExit(main())
