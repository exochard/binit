#!/usr/bin/env python3
"""Exercise common editor traces against Readline, Binit and pinned ble.sh."""
import argparse
import fcntl
import gc
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import statistics
import struct
import subprocess
import sys
import termios
import time

import bench_ecosystem as corpus
from bench_corpus_terminal import CheckedShell
from bench_interactive import PROMPT, digest
from bench_lifecycle import prepare

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / 'benchmarks/ecosystem-editor.json'
DONE = b'\x1eEDITOR-DONE\x1f'
STATUS = re.compile(rb'\x1eEDITOR-STATUS:([0-9]+)\x1f')
EQUIVALENT_FRAMEWORKS = {
    'equiv-binit': 'binit',
    'equiv-blesh-control': 'bash',
    'equiv-bashit-control': 'bash-it',
    'equiv-omb-control': 'oh-my-bash',
    'equiv-bashit-blesh': 'bash-it',
    'equiv-omb-blesh': 'oh-my-bash',
}
NATIVE_SUBJECTS = {'binit', 'binit-blesh', 'equiv-binit'}
BLE_SUBJECTS = {'blesh', 'binit-blesh', 'equiv-blesh-control', 'equiv-bashit-blesh', 'equiv-omb-blesh'}
READLINE_SUBJECTS = {'readline', 'equiv-bashit-control', 'equiv-omb-control'}


class EditorBehaviorMismatch(RuntimeError):
    """An executed interaction produced an observed wrong effect."""


ISOLATED = '''#!/usr/bin/python3
import os, sys, time
sys.stdin.buffer.read()
temporary = sys.argv[1] + '.tmp'
with open(temporary, 'w') as marker:
    marker.write(str(os.getpid()) + '\\n')
os.replace(temporary, sys.argv[1])
time.sleep(.2)
sys.stdout.buffer.write(b'alpha beta\\0')
'''
FUNCTIONS = r'''
wf_capture() {
    printf '%s\0' "$@" > "$WF_DIRECTORY/$WF_TAG"
    printf '\036EDITOR-DONE\037'
    return 37
}
wf_one() { wf_capture "$@"; }
wf_many() { wf_capture "$@"; }
wf_iso() { wf_capture "$@"; }
wf_slow() { wf_capture "$@"; }
WF_CALLS=0
WF_MANY_CALLS=0
wf_complete() {
    WF_CALLS=$((WF_CALLS + 1))
    WF_COMP_LINE=$COMP_LINE
    WF_COMP_CWORD=$COMP_CWORD
    WF_COMP_WORD0=${COMP_WORDS[0]}
    WF_COMP_WORD1=${COMP_WORDS[1]}
    COMPREPLY=('alpha beta')
}
wf_many_complete() {
    local index
    WF_MANY_CALLS=$((WF_MANY_CALLS + 1))
    COMPREPLY=()
    for ((index=0; index<1024; index++)); do printf -v COMPREPLY[index] 'pre%04d' "$index"; done
}
wf_isolated_complete() { mapfile -d '' -t COMPREPLY < <("$WF_PROVIDER" "$WF_STARTED" </dev/null); }
wf_slow_complete() {
    WF_SLOW_CALLS=$(( ${WF_SLOW_CALLS:-0} + 1 ))
    WF_SLOW_LINE=$COMP_LINE
    WF_SLOW_CWORD=$COMP_CWORD
    WF_SLOW_WORD0=${COMP_WORDS[0]}
    WF_SLOW_WORD1=${COMP_WORDS[1]}
    sleep .2
    COMPREPLY=('alpha beta')
}
complete -o filenames -F wf_complete wf_one
complete -F wf_many_complete wf_many
complete -o filenames -F wf_slow_complete wf_slow
'''


def history_text(count):
    if not count:
        return ''
    rows = [f"printf 'history-entry-{index:04d}\\n'\n" for index in range(count)]
    if count == 2048:
        rows[0] = "printf 'wf-history-old:editor-historic-needle\\n'\n"
        rows[-1] = "printf 'wf-history-recent:editor-recent-needle\\n'\n"
    else:
        rows[-1] = "printf 'wf-history-recent:editor-recent-needle\\n'\n"
    return ''.join(rows)


def copy_ble_build(source, destination):
    def exclude_runtime_cache(path, names):
        return {'cache.d'} if Path(path) == source and 'cache.d' in names else set()
    original = corpus.inventory(source)
    expected = {key: value for key, value in original.items()
                if key != 'cache.d' and not key.startswith('cache.d/')}
    shutil.copytree(source, destination, symlinks=True, ignore=exclude_runtime_cache)
    if corpus.inventory(destination) != expected:
        raise RuntimeError('private ble.sh build differs from pinned source')
    return expected


def state_directory(subject, directory):
    return directory / ('framework/state' if subject == 'equiv-binit' else 'state')


def case_identity(subject, directory):
    result = {'rc': digest(directory / 'rc'), 'history': digest(directory / 'history.txt'),
              'provider': digest(directory / 'provider.py'),
              'pair_probe': digest(directory / 'alpha_pair_probe') if subject == 'binit-blesh' else None,
              'config': digest(directory / 'config.toml') if subject in ('binit', 'binit-blesh') else None,
              'state': corpus.inventory(state_directory(subject, directory)) if subject in NATIVE_SUBJECTS else None}
    if subject in EQUIVALENT_FRAMEWORKS:
        result['framework'] = corpus.inventory(directory / 'framework')
    if subject in BLE_SUBJECTS:
        inventory = corpus.inventory(directory / 'home/ble/out')
        result['ble_build'] = {key: value for key, value in inventory.items()
                               if key != 'cache.d' and not key.startswith('cache.d/')}
        result['ble_copy_receipt'] = digest(directory / 'ble-copy.json')
    return result


def configuration(subject, mode, history, directory, binary, bash, source):
    directory.mkdir(parents=True)
    home = directory / 'home'
    home.mkdir()
    if subject == 'binit-blesh':
        (directory / 'alpha_pair_probe').write_text('pairing probe\n')
    marker_dir = directory / 'effects'
    marker_dir.mkdir()
    provider = directory / 'provider.py'
    provider.write_text(ISOLATED)
    provider.chmod(0o700)
    history_path = directory / 'history.txt'
    history_path.write_text(history_text(history['entries']))
    runtime = directory / 'runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    env = {**corpus.environment(home), 'BINIT_BASH': bash, 'XDG_RUNTIME_DIR': str(runtime)}
    code = (f"PS1='BENCH> '\nPS2='CONT> '\nset -o {mode}\nbind 'set enable-bracketed-paste on'\nHISTCONTROL=ignorespace\n"
            'HISTSIZE=4096\nHISTFILESIZE=4096\nHISTFILE=/dev/null\nhistory -c\n'
            f'history -r {shlex.quote(str(history_path))}\n'
            f'WF_DIRECTORY={shlex.quote(str(marker_dir))}\n'
            f'WF_PROVIDER={shlex.quote(str(provider))}\n'
            f'WF_STARTED={shlex.quote(str(directory / "provider-started"))}\n' + FUNCTIONS)
    if subject in EQUIVALENT_FRAMEWORKS:
        source_name = EQUIVALENT_FRAMEWORKS[subject]
        framework_rc, _ = corpus.configuration(source_name, 10, directory / 'framework', source, binary, bash)
        code += f'source {shlex.quote(str(framework_rc))}\nset -o {mode}\nPS1="BENCH> "\n'
    if subject in NATIVE_SUBJECTS:
        if subject != 'equiv-binit':
            config = directory / 'config.toml'
            config.write_text('schema=1\nplugins=[]\n')
            state = state_directory(subject, directory)
            prepare(binary, config, state, env)
            code += f'eval "$({shlex.quote(str(binary))} init --state {shlex.quote(str(state))})"\n'
        code += ('binit_complete on; WF_MENU_STATUS=$?\n'
                 'binit_complete editor history on; WF_HISTORY_STATUS=$?\n'
                 'binit completions register wf_iso "$WF_PROVIDER" "$WF_STARTED"; WF_PROVIDER_STATUS=$?\n')
    else:
        code += 'complete -o filenames -F wf_isolated_complete wf_iso\n'
    if subject in BLE_SUBJECTS:
        private_out = home / 'ble/out'
        private_out.parent.mkdir()
        copied = copy_ble_build(source / 'ble/out', private_out)
        (directory / 'ble-copy.json').write_text(json.dumps(copied, indent=2) + '\n')
        code += f'source {shlex.quote(str(private_out / "ble.sh"))}\n'
        if subject in EQUIVALENT_FRAMEWORKS:
            code += ('bleopt highlight_syntax= highlight_filename= highlight_variable= '
                     'complete_auto_complete_opts=syntax-disabled; WF_BLE_OPTIONS_STATUS=$?\n')
    code += corpus.prompt_hook(False)
    rc = directory / 'rc'
    rc.write_text(code)
    return rc, home


def frame(shell, code):
    offset = len(shell.raw)
    shell.command(' ' + code + '; wf_probe_status=$?; printf "\\036EDITOR-PROBE:%s\\037" "$wf_probe_status"')
    matches = re.findall(rb'\x1eEDITOR-PROBE:(\d+)\x1f', shell.raw[offset:])
    if matches != [b'0']:
        raise RuntimeError(f'editor probe failed: {matches!r}')


def accepted(shell, payload, with_endpoint=False):
    offset = len(shell.raw)
    started = time.perf_counter_ns()
    os.write(shell.fd, payload + b'\n')
    shell.until(lambda: DONE in shell.raw[offset:] and shell.raw[offset:].rfind(PROMPT) > shell.raw[offset:].find(DONE))
    endpoint = time.perf_counter_ns()
    elapsed = (endpoint - started) / 1e6
    offset = len(shell.raw)
    shell.command(' printf "\\036EDITOR-STATUS:%s\\037" "$?"')
    statuses = STATUS.findall(shell.raw[offset:])
    if statuses != [b'37']:
        raise EditorBehaviorMismatch(f'accepted command status differs: {statuses!r}')
    return (elapsed, endpoint) if with_endpoint else elapsed


def tag(shell, directory, name):
    path = directory / 'effects' / name
    if path.exists():
        raise RuntimeError(f'editor effect already exists: {name}')
    frame(shell, 'WF_TAG=' + shlex.quote(name))
    nonce = secrets.token_hex(16)
    offset = len(shell.raw)
    shell.command(' printf "\\036WF-TAG:' + nonce + ':%s\\037" "${WF_TAG-}"')
    pattern = rb'\x1eWF-TAG:' + nonce.encode('ascii') + rb':([^\x1f]*)\x1f'
    observed = re.findall(pattern, shell.raw[offset:])
    if observed != [name.encode()]:
        raise RuntimeError(f'editor tag confirmation failed: {observed!r}')
    return path


def effect(path, expected):
    if not path.is_file() or path.read_bytes() != expected:
        actual = path.read_bytes() if path.is_file() else None
        raise EditorBehaviorMismatch(f'editor argument effect differs: {actual!r}')


def isolated_effect(subject, actual, response_ms):
    if actual is None:
        raise RuntimeError('accepted isolated trace did not retain its argument capture')
    if subject in NATIVE_SUBJECTS:
        if actual != b'alx\0':
            raise EditorBehaviorMismatch(f'Binit isolated input or stale result differs: {actual!r}')
        if response_ms is None:
            raise EditorBehaviorMismatch('Binit did not acknowledge typed cancellation input')
    return {'observed_argument_hex': actual.hex(), 'input_preserved': actual.endswith(b'x\0'),
            'candidate_inserted': b'alpha beta' in actual,
            'scope': ('eligible cancellable isolated provider' if subject in NATIVE_SUBJECTS
                      else 'synchronous Bash completion wrapper; observed behavior, not an isolated cancellation guarantee')}


def resize(shell, columns):
    fcntl.ioctl(shell.fd, termios.TIOCSWINSZ, struct.pack('HHHH', 32, columns, 0, 0))
    shell.screen.resize(lines=32, columns=columns)
    os.kill(shell.pid, signal.SIGWINCH)


def screen_tail(shell):
    rows = shell.screen.display
    positions = [index for index, row in enumerate(rows) if 'BENCH> ' in row]
    if not positions:
        raise RuntimeError('prompt not visible on terminal screen')
    return '\n'.join(rows[positions[-1]:shell.screen.cursor.y + 1])


def typing_trace(shell, events):
    samples = {'insert': [], 'delete': []}
    for _ in range(events):
        samples['insert'].append(shell.settled(b'@'))
        if 'BENCH> @' not in screen_tail(shell):
            raise EditorBehaviorMismatch('printable input was not acknowledged on screen')
        samples['delete'].append(shell.settled(b'\x7f'))
        if 'BENCH> @' in screen_tail(shell):
            raise EditorBehaviorMismatch('deletion was not acknowledged on screen')
    result = {'samples_ms': samples, 'per_key': {}}
    for key, values in samples.items():
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise RuntimeError('invalid typing event duration')
        result['per_key'][key] = {'events': len(values), 'median_ms': statistics.median(values),
            'p95_ms': sorted(values)[math.ceil(len(values) * .95) - 1],
            'p99_ms': sorted(values)[math.ceil(len(values) * .99) - 1] if len(values) >= 1000 else None}
    return result


def history_query_acknowledged(shell, subject, needle):
    for row in shell.screen.display:
        if subject in BLE_SUBJECTS:
            if re.search(r'^\s*\(\d+:.*\)\s*`' + re.escape(needle) + r"'\s*$", row):
                return True
        elif subject in NATIVE_SUBJECTS:
            if f'History SEARCH /{needle}' in row:
                return True
        elif re.search(r'\((?:failed )?reverse-i-search\)`' + re.escape(needle) + r"':", row):
            return True
    return False


def ble_empty_prompt(shell, mode):
    cursor = shell.screen.cursor
    rows = shell.screen.display
    if not (0 <= cursor.y < len(rows)) or cursor.x != len(PROMPT):
        return False
    if rows[cursor.y].strip() != 'BENCH>':
        return False
    return mode != 'vi' or (cursor.y + 1 < len(rows) and '-- INSERT --' in rows[cursor.y + 1])


def history_trace(shell, subject, history, mode='emacs'):
    if not history['entries']:
        return {'range': 'empty', 'attempted': False}
    checks = [('recent', 'editor-recent-needle', 'wf-history-recent', True)]
    if history['entries'] == 2048:
        checks.append(('old', 'editor-historic-needle', 'wf-history-old', subject not in NATIVE_SUBJECTS))
    result = {}
    for name, needle, marker, expected in checks:
        shell.settled(b'\x12')
        shell.settled(needle.encode())
        shell.until(lambda: history_query_acknowledged(shell, subject, needle), seconds=3)
        if expected:
            shell.until(lambda: marker in '\n'.join(shell.screen.display), seconds=3)
        else:
            deadline = time.monotonic() + 3
            quiet_since = time.monotonic()
            while time.monotonic() < deadline and time.monotonic() - quiet_since < .05:
                if shell.read(.01):
                    quiet_since = time.monotonic()
            if time.monotonic() - quiet_since < .05:
                raise RuntimeError('history search did not reach a quiet state')
        visible = marker in '\n'.join(shell.screen.display)
        result[name] = {'visible': visible, 'expected': expected}
        if subject in BLE_SUBJECTS:
            shell.settled(b'\x07')
            # Without evidence that a search fiber remains active, do not retry cancellation.
            shell.until(lambda: ble_empty_prompt(shell, mode), seconds=3)
        else:
            shell.cancel()
        if visible != expected:
            raise EditorBehaviorMismatch(f'history {name} visibility differs: {visible}, expected {expected}')
    return result


def readline_candidate_confirmation(data):
    return bool(re.search(rb'Display all [0-9]+ possibilities\? \(y or n\)', data))


def readline_tab_dialog(shell, offset):
    """Wait for a complete dialog or a quiet response after Tab."""
    deadline = time.monotonic() + .5
    shell.read(.1)
    while time.monotonic() < deadline:
        data = bytes(shell.raw[offset:])
        if readline_candidate_confirmation(data):
            return True
        prefix = b'Display all '
        partial = prefix in data or any(data.endswith(prefix[:length]) for length in range(3, len(prefix)))
        if not shell.read(min(.02, max(0, deadline - time.monotonic()))) and not partial:
            break
    data = bytes(shell.raw[offset:])
    prefix = b'Display all '
    if prefix in data or any(data.endswith(prefix[:length]) for length in range(3, len(prefix))):
        raise RuntimeError('incomplete Readline candidate confirmation')
    return False


def cancellation_elapsed(start_ns, endpoint_ns):
    if endpoint_ns < start_ns:
        raise RuntimeError('invalid cancellation timing boundary')
    return (endpoint_ns - start_ns) / 1e6


def paired_provider_is_live(sample, shell_pid, provider_pid):
    return (not sample['partial'] and provider_pid != shell_pid
            and any(row['pid'] == provider_pid and row['state'] != 'Z' for row in sample['samples']))


def paired_provider_mismatch(provider_started, rendered):
    return not provider_started and 'BENCH>  wf_iso alpha_pair_probe' in rendered


def cleanup_observation(pid):
    """Retain non-atomic resource and owned-shell descriptor observations."""
    resources = corpus.resource_sample(pid)
    targets = {}
    partial = resources['partial']
    try:
        entries = list((Path('/proc') / str(pid) / 'fd').iterdir())
        for entry in entries:
            try:
                targets[entry.name] = os.readlink(entry)
            except FileNotFoundError:
                targets[entry.name] = None
                partial = True
    except FileNotFoundError:
        partial = True
    if resources['processes'] == 1 and len(targets) != resources['fds_sum']:
        partial = True
    return {'resources': resources, 'fd_targets': targets, 'partial': partial}


def cleanup_complete(observation, bound):
    resources = observation['resources']
    return (not observation['partial'] and not resources['partial']
            and resources['processes'] == 1 and resources['discovered_processes'] == 1
            and resources['zombies'] == 0 and (bound is None or resources['fds_sum'] <= bound))


def cleanup_quiescence(pid, bound, samples):
    """Keep the immediate sample and wait at most 100 ms for cleanup."""
    deadline = time.monotonic() + .1
    for index in range(21):
        observation = cleanup_observation(pid)
        samples.append(observation)
        observed_at = time.monotonic()
        if observed_at <= deadline and cleanup_complete(observation, bound):
            return True
        remaining = deadline - observed_at
        if index == 20 or remaining <= 0:
            break
        time.sleep(min(.005, remaining))
    return False


def run_trace(subject, mode, history, binary, bash, source, directory, events):
    rc, home = configuration(subject, mode, history, directory, binary, bash, source)
    runtime = directory / 'runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    env = {**corpus.environment(home), 'BINIT_BASH': bash, 'XDG_RUNTIME_DIR': str(runtime)}
    initial = case_identity(subject, directory)
    def close_recover(shell):
        if subject in BLE_SUBJECTS and mode == 'vi':
            shell.settled(b'ddi')
            shell.until(lambda: ble_empty_prompt(shell, 'vi'), seconds=3)
    shell = CheckedShell(bash, rc, directory, env, receipt_path=directory / 'terminal.raw',
                         close_recover=close_recover if subject in BLE_SUBJECTS and mode == 'vi' else None)
    row = {'subject': subject, 'mode': mode, 'history': history['id'], 'inputs': initial, 'traces': {}}
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        if subject in EQUIVALENT_FRAMEWORKS:
            frame(shell, 'wf_original_dir=$PWD; corpus_verify; wf_corpus_status=$?; '
                         'cd -- "$wf_original_dir"; [[ $wf_corpus_status == 0 ]]')
            if EQUIVALENT_FRAMEWORKS[subject] == 'bash-it':
                frame(shell, '[[ $(type -t bash-it) == function && -n ${BASH_IT-} ]]')
            if EQUIVALENT_FRAMEWORKS[subject] == 'oh-my-bash':
                frame(shell, '[[ $(type -t omb) == function && -n ${OMB_VERSION-} ]]')
        if subject in NATIVE_SUBJECTS:
            state_path = state_directory(subject, directory)
            selected = (state_path / 'current').read_text().strip()
            state = shlex.quote(str(state_path))
            generation = shlex.quote(str(state_path / 'generations' / selected))
            frame(shell, f'[[ $(type -t binit) == function && ${{BINIT_STATE_DIR-}} == {state} '
                         f'&& ${{__binit_generation_dir-}} == {generation} ]] && binit profile >/dev/null')
            frame(shell, '[[ $(type -t binit_complete) == builtin && $WF_MENU_STATUS == 0 '
                         '&& $WF_HISTORY_STATUS == 0 && $WF_PROVIDER_STATUS == 0 ]]')
        if subject in BLE_SUBJECTS:
            frame(shell, '[[ -n ${BLE_VERSION-} && -n ${_ble_attached-} ]] && declare -F ble-attach >/dev/null')
            if subject in EQUIVALENT_FRAMEWORKS:
                frame(shell, '[[ $WF_BLE_OPTIONS_STATUS == 0 && -z ${bleopt_highlight_syntax-} '
                             '&& -z ${bleopt_highlight_filename-} && -z ${bleopt_highlight_variable-} '
                             '&& :${bleopt_complete_auto_complete_opts-}: == *:syntax-disabled:* '
                             '&& :${bleopt_complete_auto_complete_opts-}: != *:history-disabled:* ]]')
        frame(shell, f'[[ $(history | wc -l) == {history["entries"]} ]]')
        row['traces']['typing'] = typing_trace(shell, events)
        row['traces']['history-range'] = history_trace(shell, subject, history, mode)
        path = tag(shell, directory, 'unicode')
        row['traces']['unicode'] = {'elapsed_ms': accepted(shell, " wf_capture 'caffè 雪'".encode())}
        effect(path, 'caffè 雪\0'.encode())
        path = tag(shell, directory, 'multiline-paste')
        offset = len(shell.raw)
        os.write(shell.fd, b" wf_capture '\x1b[200~first\nsecond\x1b[201~'")
        shell.until(lambda: len(shell.raw) > offset)
        if path.exists():
            raise EditorBehaviorMismatch('bracketed paste executed before acceptance')
        row['traces']['multiline-paste'] = {'elapsed_ms': accepted(shell, b'')}
        effect(path, b'first\nsecond\0')
        resize(shell, 40)
        frame(shell, "PS1='long-editor-prompt-for-wrap-0123456789 BENCH> '")
        path = tag(shell, directory, 'wrapped')
        value = 'wrapped-' + 'a' * 64
        row['traces']['wrapped-prompt'] = {'elapsed_ms': accepted(shell, (' wf_capture ' + value).encode())}
        effect(path, (value + '\0').encode())
        resize(shell, 120)
        frame(shell, "PS1='BENCH> '")
        for index, phase in enumerate(('first', 'warm'), 1):
            path = tag(shell, directory, 'completion-' + phase)
            shell.settled(b' wf_one al')
            completion_ms = shell.settled(b'\t')
            accepted_ms = accepted(shell, b'')
            effect(path, b'alpha beta\0')
            frame(shell, f'[[ $WF_CALLS == {index} && $WF_COMP_CWORD == 1 && $WF_COMP_LINE == "wf_one al" '
                         '&& $WF_COMP_WORD0 == wf_one && $WF_COMP_WORD1 == al ]]')
            row['traces'].setdefault('completion-first-warm', {})[phase] = {
                'completion_response_ms': completion_ms, 'accepted_ms': accepted_ms}
        path = tag(shell, directory, 'large-cancel')
        shell.settled(b' wf_many pre')
        if subject in READLINE_SUBJECTS:
            offset = len(shell.raw)
            os.write(shell.fd, b'\t')
            confirmation = readline_tab_dialog(shell, offset)
            cancel_started = time.perf_counter_ns()
            if confirmation:
                os.write(shell.fd, b'n')
                shell.read(.1)
            shell.settled(b'\x07' if mode == 'emacs' else b'\x1bi')
        else:
            shell.settled(b'\t')
            cancel_started = time.perf_counter_ns()
            shell.settled(b'\x07' if subject in BLE_SUBJECTS else b'\x1b')
        accept_ms, endpoint = accepted(shell, b'', with_endpoint=True)
        row['traces']['completion-large-cancel'] = {
            'elapsed_ms': cancellation_elapsed(cancel_started, endpoint), 'accepted_ms': accept_ms}
        effect(path, b'pre\0')
        offset = len(shell.raw)
        shell.command(' printf "\\036MANY-CALLS:%s\\037" "${WF_MANY_CALLS:-0}"')
        counts = re.findall(rb'\x1eMANY-CALLS:(\d+)\x1f', shell.raw[offset:])
        if len(counts) != 1 or int(counts[0]) < 1:
            raise EditorBehaviorMismatch(f'large completion provider was not observed: {counts!r}')
        row['traces']['completion-large-cancel']['provider_calls'] = int(counts[0])
        if subject == 'binit-blesh':
            frame(shell, 'complete -p wf_iso > pair-completion.txt && '
                         'binit completions list > pair-registry.txt && ble-bind --print > pair-bindings.txt')
            registration = (directory / 'pair-completion.txt').read_text().strip()
            registry = (directory / 'pair-registry.txt').read_text().splitlines()
            bindings = (directory / 'pair-bindings.txt').read_text()
            if (registration != 'complete -o filenames -F __binit_provider_complete wf_iso'
                    or 'wf_iso - enabled' not in registry
                    or not re.search(r"ble-bind -m '[^']+' -f (?:TAB|C-i) complete(?:\n|$)", bindings)):
                raise RuntimeError('paired provider or ble Tab binding was not established')
        path = tag(shell, directory, 'isolated-cancel')
        baseline_samples = []
        row['traces']['isolated-baseline-observations'] = baseline_samples
        if not cleanup_quiescence(shell.pid, None, baseline_samples):
            raise RuntimeError('isolated provider baseline is not a complete single-shell observation')
        baseline = baseline_samples[-1]
        resources_before = baseline['resources']
        del row['traces']['isolated-baseline-observations']
        started = directory / 'provider-started'
        if started.exists():
            raise RuntimeError('provider marker exists before isolated input')
        shell.settled(b' wf_iso al')
        started_before_tab = started.is_file()
        if subject == 'binit-blesh':
            shell.settled(b'\t')
            deadline = time.monotonic() + .3
            while time.monotonic() < deadline and not started.is_file():
                shell.read(.01)
            rendered = screen_tail(shell)
            row['paired_provider_probe'] = {'provider_started': started.is_file(), 'rendered': rendered,
                                            'registration': registration, 'bindings_sha256': digest(directory / 'pair-bindings.txt')}
            if paired_provider_mismatch(started.is_file(), rendered):
                raise EditorBehaviorMismatch('paired isolated provider did not start; Tab completed the probe filename')
        else:
            os.write(shell.fd, b'\t')
        shell.until(started.is_file)
        marker = started.read_text().strip()
        if not re.fullmatch(r'[1-9][0-9]*', marker):
            raise RuntimeError('provider start marker has invalid process identity')
        provider_pid = int(marker)
        in_flight = corpus.resource_sample(shell.pid)
        provider_live = paired_provider_is_live(in_flight, shell.pid, provider_pid)
        if subject in NATIVE_SUBJECTS and not provider_live:
            raise RuntimeError('native provider liveness at input was not established')
        before = time.perf_counter_ns()
        os.write(shell.fd, b'x')
        deadline = time.monotonic() + 1
        response_ms = None
        while time.monotonic() < deadline:
            if 'x' in screen_tail(shell):
                response_ms = (time.perf_counter_ns() - before) / 1e6
                break
            shell.read(.01)
        row['traces']['isolated-slow-cancel'] = {'elapsed_ms': accepted(shell, b'')}
        row['traces']['isolated-slow-cancel']['input_response_ms'] = response_ms
        row['traces']['isolated-slow-cancel']['provider_start_phase'] = ('typing' if started_before_tab else 'tab')
        row['traces']['isolated-slow-cancel']['provider_pid'] = provider_pid
        row['traces']['isolated-slow-cancel']['provider_live_before_input'] = provider_live
        row['traces']['isolated-slow-cancel']['in_flight_resources'] = in_flight
        row['traces']['isolated-slow-cancel']['resources_before'] = resources_before
        row['traces']['isolated-slow-cancel']['baseline_observations'] = baseline_samples
        actual = path.read_bytes() if path.is_file() else None
        row['traces']['isolated-slow-cancel'].update(isolated_effect(subject, actual, response_ms))
        cleanup_samples = []
        row['traces']['isolated-slow-cancel']['cleanup_observations'] = cleanup_samples
        if not cleanup_quiescence(shell.pid, resources_before['fds_sum'], cleanup_samples):
            raise RuntimeError('isolated provider owned child cleanup was not established')
        row['traces']['isolated-slow-cancel']['resources_after'] = cleanup_samples[-1]['resources']
        path = tag(shell, directory, 'stateful-slow')
        shell.settled(b' wf_slow al')
        completion_ms = shell.settled(b'\t')
        row['traces']['stateful-slow-provider'] = {'completion_elapsed_ms': completion_ms,
                                                   'accepted_elapsed_ms': accepted(shell, b'')}
        effect(path, b'alpha beta\0')
        frame(shell, '[[ $WF_SLOW_CALLS == 1 && $WF_SLOW_CWORD == 1 && $WF_SLOW_LINE == "wf_slow al" '
                     '&& $WF_SLOW_WORD0 == wf_slow && $WF_SLOW_WORD1 == al ]]')
        path = tag(shell, directory, 'interrupted')
        shell.settled(b' wf_capture interrupted')
        shell.cancel()
        if path.exists():
            raise EditorBehaviorMismatch('interrupted input executed')
        recovery_effect = 'interrupt-cleared'
        if subject in BLE_SUBJECTS and mode == 'vi':
            shell.until(lambda: 'wf_capture interrupted' in screen_tail(shell), seconds=3)
            recovery_effect = 'vi-normal-mode-line-retained; dd then i'
            shell.settled(b'ddi')
            shell.until(lambda: ble_empty_prompt(shell, 'vi'), seconds=3)
        path = tag(shell, directory, 'recovery')
        row['traces']['recovery'] = {'elapsed_ms': accepted(shell, b' wf_capture recovered'),
                                     'ctrl_c_effect': recovery_effect}
        effect(path, b'recovered\0')
        row['resources_at_end'] = corpus.resource_sample(shell.pid)
        if case_identity(subject, directory) != initial:
            raise RuntimeError('editor input changed during trace')
        if subject in BLE_SUBJECTS:
            row['ble_private_runtime_cache'] = corpus.inventory(directory / 'home/ble/out/cache.d')
        row['passed'] = True
        return row
    except BaseException as error:
        row['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        try:
            shell.close()
        except BaseException as error:
            row['passed'] = False
            row['close_error'] = f'{type(error).__name__}: {error}'
            raise
        finally:
            (directory / 'observation.json').write_text(json.dumps(row, indent=2) + '\n')
            gc.collect()
            if was_enabled:
                gc.enable()


def identity(args, source):
    files = [POLICY, corpus.SPEC, corpus.FIXTURE, Path(__file__), ROOT / 'scripts/bench_ecosystem.py', ROOT / 'scripts/bench_corpus_terminal.py',
             ROOT / 'scripts/bench_interactive.py', ROOT / 'scripts/bench_lifecycle.py',
             Path(args.bash), Path(sys.executable), Path('/usr/bin/python3'), Path('/usr/bin/sleep'),
             Path('/usr/bin/wc'), args.binary, source / 'ble/out/ble.sh']
    dependencies = subprocess.check_output(['/usr/bin/ldd', args.bash], text=True)
    for line in dependencies.splitlines():
        match = re.search(r'=>\s+(/\S+)', line)
        if match:
            files.append(Path(match.group(1)))
    return {str(path.resolve()): digest(path) for path in files}


def run_summary(rows, subjects, modes, histories, pairs, events):
    expected = {(pair, subject, mode, history) for pair in range(pairs) for subject in subjects
                for mode in modes for history in histories}
    seen = set()
    required = {'typing', 'history-range', 'unicode', 'multiline-paste', 'wrapped-prompt',
                'completion-first-warm', 'completion-large-cancel', 'isolated-slow-cancel',
                'stateful-slow-provider', 'recovery'}
    timed = {'unicode': ('unicode', 'elapsed_ms'), 'paste': ('multiline-paste', 'elapsed_ms'),
             'wrap': ('wrapped-prompt', 'elapsed_ms'), 'completion_first': ('completion-first-warm', 'first', 'completion_response_ms'),
             'completion_warm': ('completion-first-warm', 'warm', 'completion_response_ms'),
             'completion_first_accept': ('completion-first-warm', 'first', 'accepted_ms'),
             'completion_warm_accept': ('completion-first-warm', 'warm', 'accepted_ms'),
             'large_cancel': ('completion-large-cancel', 'elapsed_ms'),
             'isolated_response': ('isolated-slow-cancel', 'input_response_ms'),
             'isolated_accept': ('isolated-slow-cancel', 'elapsed_ms'),
             'stateful_completion': ('stateful-slow-provider', 'completion_elapsed_ms'),
             'stateful_accept': ('stateful-slow-provider', 'accepted_elapsed_ms'),
             'recovery': ('recovery', 'elapsed_ms')}
    for row in rows:
        key = (row['pair'], row['subject'], row['mode'], row['history'])
        if key not in expected or key in seen or not row.get('passed') or set(row['traces']) != required:
            raise RuntimeError('unexpected, duplicate or failed editor trace')
        for values in row['traces']['typing']['samples_ms'].values():
            if len(values) != events or any(isinstance(v, bool) or not isinstance(v, (int, float))
                                            or not math.isfinite(v) or v <= 0 for v in values):
                raise RuntimeError('invalid or incomplete editor input events')
        if set(row['traces']['typing']['samples_ms']) != {'insert', 'delete'}:
            raise RuntimeError('missing editor key category')
        calls = row['traces']['completion-large-cancel'].get('provider_calls')
        if isinstance(calls, bool) or not isinstance(calls, int) or calls < 1:
            raise RuntimeError('missing large completion provider observations')
        for name, path in timed.items():
            value = row['traces']
            for field in path:
                value = value.get(field) if isinstance(value, dict) else None
            if name == 'isolated_response' and value is None and row['subject'] not in NATIVE_SUBJECTS:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise RuntimeError(f'invalid editor trace duration: {name}')
        seen.add(key)
    if seen != expected:
        raise RuntimeError('incomplete editor trace observations')
    summary = {}
    for subject in subjects:
        for mode in modes:
            for history in histories:
                key = f'{subject}/{mode}/{history}'
                group = [row for row in rows if (row['subject'], row['mode'], row['history']) == (subject, mode, history)]
                summary[key] = {}
                for event in ('insert', 'delete'):
                    values = [value for row in group for value in row['traces']['typing']['samples_ms'][event]]
                    ordered = sorted(values)
                    summary[key][event] = {'events': len(values), 'median_ms': statistics.median(values),
                        'p95_ms': ordered[math.ceil(len(values) * .95) - 1],
                        'p99_ms': ordered[math.ceil(len(values) * .99) - 1] if len(values) >= 1000 else None}
                for name, path in timed.items():
                    values = []
                    for row in group:
                        value = row['traces']
                        for field in path:
                            value = value[field]
                        if value is not None:
                            values.append(value)
                    values.sort()
                    summary[key][name] = {'samples': len(values), 'missing': len(group) - len(values),
                        'median_ms': statistics.median(values) if values else None,
                        'p95_ms': values[math.ceil(len(values) * .95) - 1] if values else None}
    return summary


def pairing_preflight(policy, args, source, history):
    """Classify a pairing mismatch only after both same-trace controls pass."""
    results = {}
    for mode in policy['modes']:
        controls = {}
        for subject in ('binit', 'blesh'):
            directory = args.work / f'pairing-control-{mode}-{subject}'
            control = run_trace(subject, mode, history, args.binary, args.bash,
                                source, directory, min(args.events, 3))
            if not control.get('passed'):
                raise RuntimeError(f'{subject} {mode}: incomplete standalone compatibility control')
            controls[subject] = {'passed': True, 'evidence': str(directory)}
        directory = args.work / f'pairing-preflight-{mode}'
        try:
            paired = run_trace('binit-blesh', mode, history, args.binary, args.bash,
                               source, directory, min(args.events, 3))
            if not paired.get('passed'):
                raise RuntimeError(f'{mode}: incomplete paired compatibility observation')
            results[mode] = {'passed': True, 'controls': controls, 'observation': paired}
        except EditorBehaviorMismatch as error:
            results[mode] = {'passed': False, 'classification': 'observed_behavior_mismatch',
                             'controls': controls, 'error': f'{type(error).__name__}: {error}',
                             'evidence': str(directory)}
    return {'attempted': True, 'passed': all(item['passed'] for item in results.values()), 'modes': results}


def framework_pairing_preflight(policy, args, source, history):
    """Check common fixture controls before attempting each framework/editor pair."""
    spec = policy['framework_editor_track']
    pair_controls = {'equiv-bashit-blesh': 'equiv-bashit-control',
                     'equiv-omb-blesh': 'equiv-omb-control'}
    controls = {}
    pairs = {pair: {'passed': True, 'modes': {}} for pair in spec['conditional_pairings']}
    for mode in policy['modes']:
        controls[mode] = {}
        for subject in (spec['measured_baseline'], 'equiv-blesh-control', *pair_controls.values()):
            directory = args.work / f'framework-control-{mode}-{subject}'
            row = run_trace(subject, mode, history, args.binary, args.bash, source, directory, min(args.events, 3))
            if not row.get('passed'):
                raise RuntimeError(f'{subject} {mode}: incomplete framework/editor control')
            controls[mode][subject] = {'passed': True, 'evidence': str(directory)}
        for pair in spec['conditional_pairings']:
            directory = args.work / f'framework-pair-{mode}-{pair}'
            try:
                row = run_trace(pair, mode, history, args.binary, args.bash, source, directory, min(args.events, 3))
                if not row.get('passed'):
                    raise RuntimeError(f'{pair} {mode}: incomplete framework/editor observation')
                pairs[pair]['modes'][mode] = {'passed': True, 'evidence': str(directory)}
            except EditorBehaviorMismatch as error:
                pairs[pair]['passed'] = False
                pairs[pair]['modes'][mode] = {'passed': False, 'classification': 'observed_behavior_mismatch',
                    'error': f'{type(error).__name__}: {error}', 'evidence': str(directory)}
    return {'attempted': True, 'controls': controls, 'pairs': pairs}


def save(output, result):
    temporary = output.with_name(output.name + '.writing')
    with temporary.open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    os.replace(temporary, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--bash', default='/bin/bash')
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--runs', type=int, default=2)
    parser.add_argument('--pairs', type=int, default=20)
    parser.add_argument('--events', type=int, default=50, help='per key and pair; full policy uses 50')
    parser.add_argument('--subjects', nargs='+')
    parser.add_argument('--modes', nargs='+')
    parser.add_argument('--histories', nargs='+')
    parser.add_argument('--skip-pairing', action='store_true', help='diagnostic only; does not qualify the corpus')
    args = parser.parse_args()
    args.work, args.output, args.sources, args.binary = (args.work.resolve(), args.output.resolve(),
                                                        args.sources.resolve(), args.binary.resolve())
    if not args.work.is_relative_to(ROOT / 'tmp') or not args.output.is_relative_to(ROOT / 'tmp'):
        parser.error('work and output must be under project tmp/')
    if args.work.exists() or args.output.exists() or min(args.runs, args.pairs, args.events) < 1:
        parser.error('use fresh paths and positive run, pair and event counts')
    policy = json.loads(POLICY.read_text())
    subjects = args.subjects or [row['id'] for row in policy['subjects']]
    modes = args.modes or policy['modes']
    histories = args.histories or [row['id'] for row in policy['histories']]
    allowed = ([row['id'] for row in policy['subjects']], policy['modes'], [row['id'] for row in policy['histories']])
    selections = (allowed[0] + list(EQUIVALENT_FRAMEWORKS), allowed[1], allowed[2])
    for chosen, options in zip((subjects, modes, histories), selections):
        if len(set(chosen)) != len(chosen) or any(value not in options for value in chosen):
            parser.error('use distinct subjects, modes and histories from the frozen policy')
    args.work.mkdir(parents=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as stream:
        stream.write('{}\n')
    (args.work / 'driver.py').write_bytes(Path(__file__).read_bytes())
    result = {'schema': 1, 'phase': 'editor-common-traces', 'runs': [], 'complete_two_run_editor': False,
              'comparability': policy['comparability']}
    try:
        source_lock = json.loads(corpus.SPEC.read_text())
        result['source_receipts'] = corpus.verify_sources(args.sources, source_lock)
        source = args.sources / 'sources'
        if digest(source / policy['sources']['ble_entry']) != policy['sources']['ble_entry_sha256']:
            raise RuntimeError('ble.sh build output differs from frozen editor policy')
        result['inputs'] = identity(args, source)
        result['bash_version'] = subprocess.check_output([args.bash, '--version'], text=True).splitlines()[0]
        by_history = {row['id']: row for row in policy['histories']}
        core_subjects = list(subjects)
        if args.skip_pairing:
            result['pairing_compatibility'] = {'attempted': False, 'passed': False}
        else:
            result['active_case'] = ['pairing-preflight', 'standalone-controls-and-pair', 'both-keymaps', 'small']
            result['pairing_compatibility'] = pairing_preflight(policy, args, source, by_history['small'])
            if result['pairing_compatibility']['passed']:
                subjects.append('binit-blesh')
            save(args.output, result)
            result['active_case'] = ['framework-pairing-preflight', 'controls-and-pairs', 'both-keymaps', 'small']
            result['framework_editor_compatibility'] = framework_pairing_preflight(policy, args, source, by_history['small'])
            baseline = policy['framework_editor_track']['measured_baseline']
            if baseline not in subjects:
                subjects.append(baseline)
            for pair, decision in result['framework_editor_compatibility']['pairs'].items():
                if decision['passed'] and pair not in subjects:
                    subjects.append(pair)
            save(args.output, result)
        for run in range(args.runs):
            samples = []
            result['runs'].append(samples)
            for pair in range(args.pairs):
                if identity(args, source) != result['inputs']:
                    raise RuntimeError('editor inputs changed before pair')
                order = subjects[::1 if (run + pair) % 2 == 0 else -1]
                for mode in modes:
                    for history in histories:
                        for subject in order:
                            result['active_case'] = [run, pair, subject, mode, history]
                            directory = args.work / f'{run}-{pair}-{subject}-{mode}-{history}'
                            row = run_trace(subject, mode, by_history[history], args.binary, args.bash,
                                            source, directory, args.events)
                            samples.append({'pair': pair, **row})
                            save(args.output, result)
                print(f'run {run + 1} pair {pair + 1}/{args.pairs}: editor effects passed', flush=True)
            for row in samples:
                directory = args.work / f"{run}-{row['pair']}-{row['subject']}-{row['mode']}-{row['history']}"
                if case_identity(row['subject'], directory) != row['inputs']:
                    raise RuntimeError(f'editor case inputs changed during run: {directory}')
            if identity(args, source) != result['inputs']:
                raise RuntimeError('editor inputs changed during run')
            if corpus.verify_sources(args.sources, source_lock) != result['source_receipts']:
                raise RuntimeError('pinned editor sources changed during run')
        result['summary_by_run'] = [run_summary(run, subjects, modes, histories, args.pairs, args.events)
                                    for run in result['runs']]
        result['complete_two_run_editor'] = (args.runs == policy['measurement']['runs']
            and args.pairs == policy['measurement']['pairs_per_run']
            and args.events == next(trace['events_per_key_per_pair'] for trace in policy['trace'] if trace['id'] == 'typing')
            and not args.skip_pairing and (core_subjects, modes, histories) == tuple(allowed))
        result.pop('active_case', None)
    except BaseException as error:
        result['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        save(args.output, result)


if __name__ == '__main__':
    main()
