"""Calling-shell lifecycle and scheduling behavior."""
import json
import os
import pty
import select
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
BINARY = Path(os.environ.get('BINIT_TEST_BINARY', ROOT / 'target/release/binit'))
BASH = os.environ.get('BINIT_BASH', '/bin/bash')


class RuntimeFeatures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='.scratch-runtime-', dir=ROOT)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.state = self.root / 'state'
        self.env = dict(os.environ, HOME=str(self.home), BINIT_STATE=str(self.state), TERM='dumb',
                        HISTFILE='/dev/null', INPUTRC='/dev/null')
        for name in ('BASH_ENV', 'ENV'):
            self.env.pop(name, None)
        self.rc = self.root / 'rc'
        self.lines = []

    def tearDown(self):
        self.tmp.cleanup()

    def plugin(self, name, body, *keys):
        directory = self.root / name
        directory.mkdir()
        (directory / 'init.bash').write_text(body)
        self.lines.append(shlex.join(['binit', 'plugin', str(directory), f'id={name}', 'files=init.bash', *keys]))

    def shell(self, script):
        self.rc.write_text(f'eval "$({shlex.quote(str(BINARY))} init)"\n' + '\n'.join(self.lines) + '\n')
        return subprocess.run([BASH, '--noprofile', '--rcfile', str(self.rc), '-i'], input=script + '\nexit\n',
                              text=True, capture_output=True, env=self.env, cwd=self.root, timeout=15)

    def sync(self):
        result = self.shell('binit sync')
        self.assertEqual(result.returncode, 0, result.stderr)
        # One line when the generation changed, nothing when new shells keep theirs.
        self.assertRegex(result.stdout, r'\A(synced \d+ plugin lines? into [0-9a-f]{12}; new shells use it\n)?\Z')

    def test_before_after_callbacks_run_in_order_in_calling_shell(self):
        self.plugin('sample', "printf 'plugin\\n'\ngreet() { printf 'greet:%s\\n' \"$1\"; }\n", 'load=command', 'commands=greet')
        self.sync()
        result = self.shell("before() { printf 'before:%s\\n' \"$1\"; }; after() { printf 'after:%s:%s\\n' \"$1\" \"$2\"; }; binit on before-load sample before; binit on after-load sample after; greet ok")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['before:sample', 'plugin', 'after:sample:loaded', 'greet:ok'])

    @unittest.skipIf(os.environ.get('BINIT_NATIVE') == '0', 'native module required')
    def test_prompt_load_uses_direct_path_and_preserves_requirement_fallback(self):
        for name, body, requires in (
            ('ready', 'seen_group=${__binit_runtime_loaded[load]-deferred}\nready_function() { :; }\n',
             ['printf', 'true']),
            ('zz-missing', "printf 'unexpected-source\\n'\n", ['absent_binit_prompt_requirement']),
        ):
            directory = self.root / name
            directory.mkdir()
            (directory / 'init.bash').write_text(body)
            (directory / 'plugin.toml').write_text(
                f"schema=1\nid='{name}'\nentry='init.bash'\nload='prompt'\nrequires={json.dumps(requires)}\n")
        config = self.root / 'config.toml'
        config.write_text("schema=1\n[[plugins]]\npath='ready'\n[[plugins]]\npath='zz-missing'\n")
        subprocess.run([BINARY, 'prepare', config, '--state', self.state], env=self.env, check=True,
                       capture_output=True)
        script = ('printf "group=%s\\n" "$seen_group"; binit profile; '
                  'binit unload ready && ! declare -F ready_function >/dev/null && printf "unloaded\\n"')
        direct = self.shell(script)
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertIn('group=deferred\n', direct.stdout)
        self.assertRegex(direct.stdout, r'zz-missing\s+unavailable[^\n]*missing command: absent_binit_prompt_requirement')
        self.assertTrue(direct.stdout.endswith('unloaded\n'))
        self.assertNotIn('unexpected-source', direct.stdout)

        self.lines.insert(0, 'command() { builtin command "$@"; }')
        fallback = self.shell(script)
        self.assertEqual(fallback.returncode, 0, fallback.stderr)
        self.assertIn('group=1\n', fallback.stdout)
        self.assertRegex(fallback.stdout, r'zz-missing\s+unavailable[^\n]*missing command: absent_binit_prompt_requirement')
        self.assertTrue(fallback.stdout.endswith('unloaded\n'))

    def test_lifecycle_logs_are_opt_in_and_use_fixed_reason_codes(self):
        self.plugin('precursor', 'SHOULD_NOT_APPEAR=private_value\n')
        self.plugin('later', 'return 17\n', 'load=event', 'event=demo')
        self.sync()
        self.assertEqual(json.loads(self.shell('binit logs --json').stdout)['events'], [])
        self.env['BINIT_LOGS'] = '1'
        result = self.shell('binit emit demo; binit profile --timeline --json')
        receipt = json.loads(result.stdout)
        self.assertIn(receipt['clock'], ('native', 'uptime'))
        events = receipt['events']
        self.assertEqual([row['stage'] for row in events], ['load', 'prompt-dispatch', 'load'])
        self.assertEqual([row['offset_us'] for row in events], sorted(row['offset_us'] for row in events))
        self.assertEqual((events[-1]['outcome'], events[-1]['reason']), ('failed', 'entry'))
        self.assertNotIn('private_value', result.stdout)

    def test_lifecycle_log_retains_a_bounded_ordered_window(self):
        self.plugin('pending', ':\n', 'load=event', 'event=demo', 'when=no_such_condition')
        self.sync()
        self.env.update(BINIT_LOGS='1', BINIT_COMPLETION_MENU='0')
        result = self.shell('for ((i=0; i<270; ++i)); do binit emit demo; done; binit logs --json')
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt['clock'], 'uptime')
        self.assertEqual(receipt['dropped'], 15)
        self.assertEqual(len(receipt['events']), 256)
        self.assertEqual([row['sequence'] for row in receipt['events']], list(range(15, 271)))
        self.assertTrue(all(row['reason'] == 'condition' for row in receipt['events']))

    @unittest.skipIf(os.environ.get('BINIT_NATIVE') == '0', 'native module required')
    def test_completion_status_stays_in_the_calling_shell_with_a_shadowing_function(self):
        self.sync()
        pids = self.root / 'completion-pids'
        script = (f'pids={shlex.quote(str(pids))}\n'
                  'parent_pid=$BASHPID\n'
                  'binit_complete() { printf shadowed; return 99; }\n'
                  'set -T\n'
                  "trap 'printf \"%s\\n\" \"$BASHPID\" >> \"$pids\"' DEBUG\n"
                  'binit completion status\n'
                  'trap - DEBUG\n'
                  "printf 'root:%s\\n' \"$parent_pid\"\n")
        result = self.shell(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('shadowed', result.stdout)
        parent = next(line.removeprefix('root:') for line in result.stdout.splitlines() if line.startswith('root:'))
        self.assertEqual(set(pids.read_text().splitlines()), {parent})

    def test_callback_arguments_match_phase(self):
        self.plugin('sample', 'greet() { :; }\n', 'load=command', 'commands=greet')
        self.sync()
        result = self.shell("before() { printf 'before:%s\\n' \"$#\"; }; after() { printf 'after:%s\\n' \"$#\"; }; binit on before-load sample before; binit on after-load sample after; greet")
        self.assertEqual(result.stdout.splitlines(), ['before:1', 'after:2'])

    def test_debug_trap_does_not_corrupt_owned_definitions(self):
        self.plugin('sample', 'greet() { :; }\n', 'load=command', 'commands=greet')
        self.sync()
        result = self.shell("set -T; trap 'printf \"TRACE:%s\\n\" \"$BASH_COMMAND\"' DEBUG; greet; binit unload sample && printf 'UNLOADED\\n'; trap - DEBUG")
        self.assertIn('UNLOADED\n', result.stdout, result.stderr)

    def test_nested_load_does_not_transfer_child_functions(self):
        self.plugin('child', 'child_function() { :; }\n', 'load=event', 'event=child')
        self.plugin('parent', 'binit load child\nparent_function() { :; }\n', 'load=event', 'event=parent')
        self.sync()
        result = self.shell("binit emit parent; binit unload parent; declare -F child_function >/dev/null && printf 'CHILD-REMAINS\\n'; declare -F parent_function >/dev/null || printf 'PARENT-REMOVED\\n'")
        self.assertIn('CHILD-REMAINS\n', result.stdout, result.stderr)
        self.assertIn('PARENT-REMOVED\n', result.stdout, result.stderr)

    def test_event_and_condition_keep_plugin_pending_until_both_hold(self):
        self.plugin('sample', "printf 'loaded-event\\n'\n", 'load=event', 'event=go', 'when=allowed')
        self.sync()
        result = self.shell("allowed() { [[ ${ALLOW-no} == yes ]]; }; binit emit wrong; binit emit go || :; printf 'before:%s\\n' \"${__binit_state[sample]}\"; ALLOW=yes; binit emit go; printf 'after:%s\\n' \"${__binit_state[sample]}\"")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['before:pending', 'loaded-event', 'after:loaded'])

    def test_timed_loading_runs_on_dispatch_after_deadline(self):
        self.plugin('sample', "printf 'loaded-timed\\n'\n", 'load=timed', 'delay_ms=200')
        self.sync()
        result = self.shell("binit dispatch; printf 'before:%s\\n' \"${__binit_state[sample]}\"; sleep .3; binit dispatch; printf 'after:%s\\n' \"${__binit_state[sample]}\"")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['before:pending', 'loaded-timed', 'after:loaded'])

    def test_conditional_wait_retries_after_a_later_prompt(self):
        self.plugin('sample', "printf 'loaded-wait\\n'\n", 'load=wait', 'when=allowed')
        self.sync()
        result = self.shell("allowed() { [[ ${ALLOW-no} == yes ]]; }\nprintf 'before:%s\\n' \"${__binit_state[sample]}\"\nALLOW=yes\nprintf 'after:%s\\n' \"${__binit_state[sample]}\"")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['before:pending', 'loaded-wait', 'after:loaded'])

    def test_interrupt_before_load_callback_recovers_at_prompt(self):
        import test_wait
        test_wait.BINARY = BINARY
        startup = "before() { touch MARKS/callback; sleep 30; }; binit on before-load slow before\n"
        with test_wait.Session("touch MARKS/sourced\n", startup_body=startup) as session:
            session.wait(lambda: session.mark('callback'))
            session.interrupt_to_prompt()
            session.send("binit profile | grep -o 'load interrupted'\n")
            session.wait(lambda: '\nload interrupted' in session.text)
            self.assertFalse(session.mark('sourced'))

    @unittest.skipIf(os.environ.get("BINIT_NATIVE") == "0", "native entry snapshots require the module")
    def test_interrupted_load_releases_native_snapshots(self):
        import test_wait
        test_wait.BINARY = BINARY
        with test_wait.Session("sh -c ': > \"$1\"; exec sleep 30' sh MARKS/started\n") as session:
            session.wait(lambda: session.mark('started'))
            session.interrupt_to_prompt()
            session.send("binit profile | grep -o 'load interrupted'; t=; binit_complete effects mark t; echo \"snapshot-depth-$t\"\n")
            session.wait(lambda: '\nload interrupted' in session.text and 'snapshot-depth-1' in session.text)

    def test_git_prompt_interrupt_reaps_the_provider(self):
        tools = self.root / 'tools'
        tools.mkdir()
        marker = self.root / 'git-started'
        git = tools / 'git'
        git.write_text('#!/bin/sh\nprintf "%s\\n" "$$" > '
                       + shlex.quote(str(marker)) + '\nexec sleep 2\n')
        git.chmod(0o700)
        env = {**self.env, 'PATH': f'{tools}{os.pathsep}{self.env["PATH"]}'}
        with subprocess.Popen([BINARY, '__git-prompt-branch', self.root], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) as worker:
            deadline = time.monotonic() + 3
            while not marker.exists() or marker.stat().st_size == 0:
                self.assertIsNone(worker.poll())
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            provider = int(marker.read_text())
            worker.send_signal(signal.SIGINT)
            stdout, stderr = worker.communicate(timeout=3)
            self.assertEqual(worker.returncode, 1, stderr)
            self.assertEqual(stdout, b'')
            self.assertFalse(Path(f'/proc/{provider}').exists())

    @unittest.skipIf(os.environ.get("BINIT_NATIVE") == "0", "idle dispatch requires the native module")
    def test_timed_plugin_runs_at_idle_without_enter(self):
        self.plugin('sample', 'touch "$HOME/idle-loaded"\n', 'load=timed', 'delay_ms=700')
        self.sync()
        self.rc.write_text(f'eval "$({shlex.quote(str(BINARY))} init)"\n' + '\n'.join(self.lines) + '\n')
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.root)
            env = dict(self.env, TERM='xterm-256color', PS1='READY> ', BINIT_COMPLETION_MENU='1')
            os.execve(BASH, [BASH, '--noprofile', '--rcfile', str(self.rc), '-i'], env)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (self.home / 'idle-loaded').exists():
                if select.select([fd], [], [], .05)[0]:
                    os.read(fd, 65536)
            self.assertTrue((self.home / 'idle-loaded').exists(), 'timed plugin did not load at idle')
        finally:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            os.close(fd)

    @unittest.skipIf(os.environ.get("BINIT_NATIVE") == "0", "idle dispatch requires the native module")
    def test_idle_condition_retries_after_it_becomes_true(self):
        self.plugin('sample', 'touch "$HOME/idle-loaded"\n', 'load=timed', 'delay_ms=200', 'when=allowed')
        self.sync()
        self.rc.write_text(f'eval "$({shlex.quote(str(BINARY))} init)"\nallowed() {{ [[ -f $HOME/allow ]]; }}\n' + '\n'.join(self.lines) + '\n')
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.root)
            os.execve(BASH, [BASH, '--noprofile', '--rcfile', str(self.rc), '-i'],
                      dict(self.env, TERM='xterm-256color', PS1='READY> ', BINIT_COMPLETION_MENU='1'))
        try:
            time.sleep(.7)
            self.assertFalse((self.home / 'idle-loaded').exists())
            (self.home / 'allow').touch()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (self.home / 'idle-loaded').exists():
                if select.select([fd], [], [], .05)[0]:
                    os.read(fd, 65536)
            self.assertTrue((self.home / 'idle-loaded').exists())
        finally:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            os.close(fd)

    @unittest.skipIf(os.environ.get("BINIT_NATIVE") == "0", "idle dispatch requires the native module")
    def test_interrupt_during_idle_load_recovers_at_prompt(self):
        worker = ('sh -c \'printf "%s\\n" "$$" > "$HOME/idle-start"; exec sleep 30\'\n'
                  'touch "$HOME/idle-done"\n')
        self.plugin('sample', worker, 'load=timed', 'delay_ms=200')
        self.sync()
        self.rc.write_text(f'eval "$({shlex.quote(str(BINARY))} init)"\n' + '\n'.join(self.lines) + '\n')
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.root)
            os.execve(BASH, [BASH, '--noprofile', '--rcfile', str(self.rc), '-i'],
                      dict(self.env, TERM='xterm-256color', PS1='READY> ', BINIT_COMPLETION_MENU='1'))
        startup = bytearray()
        output = bytearray()
        foreground = []
        prompt = b'READY> '
        started = self.home / 'idle-start'

        def context():
            return (f'shell pid={pid}, foreground groups at Ctrl-C and profile probe={foreground}\n'
                    f'startup terminal tail: {bytes(startup[-1024:])!r}\n'
                    f'response terminal tail: {bytes(output[-1024:])!r}')

        def foreground_group():
            try:
                return os.tcgetpgrp(fd)
            except OSError:
                return None

        def idle_worker_is_foreground():
            try:
                witness = started.read_text()
                if not witness.endswith('\n') or not witness.strip().isdigit():
                    return False
                return os.getpgid(int(witness)) == foreground_group()
            except (FileNotFoundError, ProcessLookupError):
                return False

        def read_terminal(destination):
            data = os.read(fd, 65536)
            if not data:
                self.fail(f'idle interruption PTY reached EOF\n{context()}')
            destination.extend(data)

        def wait_for(predicate, destination, deadline, description):
            while time.monotonic() < deadline and not predicate():
                if select.select([fd], [], [], .05)[0]:
                    read_terminal(destination)
            self.assertTrue(predicate(), f'{description}\n{context()}')

        try:
            wait_for(lambda: prompt in startup and idle_worker_is_foreground(), startup, time.monotonic() + 5,
                     'The idle worker did not enter the foreground after the initial prompt')
            foreground.append(foreground_group())
            os.write(fd, b'\x03')
            deadline = time.monotonic() + 5
            wait_for(lambda: prompt in output, output, deadline,
                     'Bash did not return to a fresh prompt after Ctrl-C')
            foreground.append(foreground_group())
            os.write(fd, b"binit profile | grep -o 'load interrupted'\n")
            wait_for(lambda: b'\rload interrupted\r\n' in output, output, deadline,
                     'The profile did not report an interrupted load')
            self.assertFalse((self.home / 'idle-done').exists(), context())
        except OSError as error:
            self.fail(f'idle interruption PTY error: {error}\n{context()}')
        finally:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            os.close(fd)

    def test_watched_file_change_dispatches_once(self):
        watched = self.root / 'watched file'
        watched.write_text('old')
        self.plugin('sample', "printf 'loaded-watch\\n'\n", 'load=watch', f'watch={watched}')
        self.sync()
        result = self.shell(f"printf 'changed-content' > {shlex.quote(str(watched))}; binit dispatch; binit dispatch; printf 'state:%s\\n' \"${{__binit_state[sample]}}\"")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['loaded-watch', 'state:loaded'])

    def test_watch_change_stays_pending_until_condition_succeeds(self):
        watched = self.root / 'watched'
        watched.write_text('old')
        self.plugin('sample', "printf 'loaded-watch\\n'\n", 'load=watch', f'watch={watched}', 'when=allowed')
        self.sync()
        result = self.shell(f"allowed() {{ [[ ${{ALLOW-no}} == yes ]]; }}; printf new-content > {shlex.quote(str(watched))}; binit dispatch; printf 'before:%s\\n' \"${{__binit_state[sample]}}\"; ALLOW=yes; binit dispatch; printf 'after:%s\\n' \"${{__binit_state[sample]}}\"")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['before:pending', 'loaded-watch', 'after:loaded'])

    def test_failing_before_callback_prevents_sourcing_and_reports_failure(self):
        self.plugin('sample', "printf 'should-not-load\\n'\ngreet() { :; }\n", 'load=command', 'commands=greet')
        self.sync()
        result = self.shell("before() { return 7; }; after() { printf 'after:%s:%s\\n' \"$1\" \"$2\"; }; binit on before-load sample before; binit on after-load sample after; greet || :; binit profile")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('after:sample:failed\n', result.stdout)
        self.assertIn('before-load callback failed', result.stdout)
        self.assertNotIn('should-not-load', result.stdout)

    def test_unload_removes_new_functions_and_aliases_but_preserves_user_changes(self):
        self.plugin('sample', "greet() { :; }\nother_new() { :; }\nalias sample_alias='printf sample'\n", 'load=command', 'commands=greet')
        self.sync()
        result = self.shell("greet; binit report sample; greet() { printf user; }; binit unload sample || printf 'conflict\\n'; type -t other_new; greet; printf '\\n'; unalias sample_alias; binit report sample")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('conflict', result.stdout)
        self.assertIn('function\nuser\n', result.stdout)
        self.assertIn('other_new', result.stdout)

    def test_unload_removes_owned_definitions_when_unchanged(self):
        self.plugin('sample', "greet() { :; }\nother_new() { :; }\nalias sample_alias='printf sample'\n", 'load=command', 'commands=greet')
        self.sync()
        result = self.shell("greet; binit unload sample; declare -F greet >/dev/null || printf 'greet-gone\\n'; declare -F other_new >/dev/null || printf 'other-gone\\n'; alias sample_alias 2>/dev/null || printf 'alias-gone\\n'")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['greet-gone', 'other-gone', 'alias-gone'])

    @unittest.skipIf(os.environ.get('BINIT_NATIVE') == '0', 'native module required')
    def test_native_unload_preflight_stays_in_the_calling_shell(self):
        self.plugin('sample', '\n'.join(f'owned_{number}() {{ :; }}' for number in range(32)), 'load=startup')
        self.sync()
        pids = self.root / 'unload-pids'
        script = (f'pids={shlex.quote(str(pids))}\n'
                  'parent_pid=$BASHPID\n'
                  'binit_complete() { printf shadowed; return 99; }\n'
                  'set -T\n'
                  "trap 'printf \"%s\\n\" \"$BASHPID\" >> \"$pids\"' DEBUG\n"
                  'binit unload sample || exit 41\n'
                  'trap - DEBUG\n'
                  "printf 'root:%s\\n' \"$parent_pid\"\n"
                  'if declare -F owned_0 owned_31 >/dev/null; then exit 42; fi\n')
        result = self.shell(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('shadowed', result.stdout)
        parent = next(line.removeprefix('root:') for line in result.stdout.splitlines() if line.startswith('root:'))
        self.assertEqual(set(pids.read_text().splitlines()), {parent})

    def test_readonly_unload_refusal_preserves_all_owned_effects(self):
        self.plugin('sample', "greet() { :; }\nother_new() { :; }\nalias sample_alias='printf sample'\n", 'load=command', 'commands=greet')
        self.sync()
        result = self.shell("greet; readonly -f other_new; if binit unload sample; then exit 41; fi; "
                            "declare -F greet other_new >/dev/null || exit 42; "
                            "alias sample_alias >/dev/null || exit 43; "
                            "[[ ${__binit_state[sample]} == loaded ]] || exit 44")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('readonly function: other_new', result.stderr)

    def test_alias_unload_refusal_preserves_validated_functions(self):
        self.plugin('sample', "greet() { :; }\nother_new() { :; }\nalias sample_alias='printf sample'\n", 'load=command', 'commands=greet')
        self.sync()
        result = self.shell("greet; alias sample_alias='printf user'; if binit unload sample; then exit 41; fi; "
                            "declare -F greet other_new >/dev/null || exit 42; "
                            "[[ ${__binit_state[sample]} == loaded ]] || exit 43")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('alias changed: sample_alias', result.stderr)

    def test_alias_unload_ignores_aliases_added_later_by_other_plugins(self):
        self.plugin('first', "alias first_alias='printf first'\nalias empty_alias=''\n", 'load=startup')
        self.plugin('second', "alias second_alias='printf second'\n", 'load=startup')
        self.sync()
        result = self.shell("alias caller_alias=':'; binit unload first || exit 41; "
                            "if alias first_alias empty_alias >/dev/null 2>&1; then exit 42; fi; "
                            "alias second_alias caller_alias >/dev/null || exit 43; "
                            "binit unload second || exit 44; "
                            "if alias second_alias >/dev/null 2>&1; then exit 45; fi")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_startup_plugins_keep_arguments_and_ownership_when_one_leaves_a_trap(self):
        # The first plugin takes the direct startup path and leaves a DEBUG trap; the rest take the general load.
        self.plugin('first', "first_fn() { :; }\nalias first_alias=:\ntrap ':' DEBUG\nprintf 'first:%s:%s\\n' \"$#\" \"$1\"\n", 'load=startup')
        self.plugin('second', "second_fn() { :; }\nprintf 'second:%s:%s\\n' \"$#\" \"$1\"\n", 'load=startup')
        self.plugin('third', "third_fn() { :; }\nprintf 'third:%s:%s\\n' \"$#\" \"$1\"\n", 'load=startup')
        self.sync()
        result = self.shell("trap - DEBUG; for id in first second third; do [[ ${__binit_state[$id]} == loaded ]] || exit 41; done; "
                            "[[ -v __binit_owned_functions[first:first_fn] && -v __binit_owned_aliases[first:first_alias] ]] || exit 42; "
                            "[[ -v __binit_owned_functions[second:second_fn] && -v __binit_owned_functions[third:third_fn] ]] || exit 43; "
                            "binit unload second || exit 44; if declare -F second_fn >/dev/null; then exit 45; fi; "
                            "declare -F first_fn third_fn >/dev/null || exit 46")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([line for line in result.stdout.splitlines() if ':' in line],
                         ['first:1:first', 'second:1:second', 'third:1:third'])

    def test_startup_plugin_declarations_stay_local_to_each_load(self):
        # A prepared generation activates all startup plugins in one call. Under __binit_load a top-level
        # declare or local in a plugin is local to its own load; the direct path must agree.
        bodies = {'first': "declare -r PLUGIN_SCOPE=first\nlocal plugin_local=first\nfirst_fn() { :; }\n",
                  'second': "declare -r PLUGIN_SCOPE=second\nlocal plugin_local=second\n"
                            "printf 'second:%s:%s\\n' \"$PLUGIN_SCOPE\" \"$plugin_local\"\n",
                  'failing': "failing_fn() { :; }\nreturn 3\n"}
        config = 'schema=1\n'
        for name, body in bodies.items():
            directory = self.root / name
            directory.mkdir()
            (directory / 'plugin.toml').write_text(f"schema=1\nid='{name}'\nentry='plugin.bash'\nload='startup'\n")
            (directory / 'plugin.bash').write_text(body)
            config += f"[[plugins]]\npath='{name}'\n"
        (self.root / 'config.toml').write_text(config)
        state = self.root / 'prepared'
        prepared = subprocess.run([str(BINARY), 'prepare', str(self.root / 'config.toml'), '--state', str(state)],
                                  env=self.env, capture_output=True, text=True)
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.rc.write_text(f'eval "$({shlex.quote(str(BINARY))} init --state {shlex.quote(str(state))})"\n')
        script = ("[[ ${__binit_state[first]} == loaded && ${__binit_state[second]} == loaded ]] || exit 41; "
                  "[[ ${__binit_state[failing]} == failed && ${__binit_reason[failing]} == 'entry returned 3' ]] || exit 42; "
                  "[[ -v __binit_owned_functions[failing:failing_fn] && -v __binit_owned_functions[first:first_fn] ]] || exit 43; "
                  "if [[ -v PLUGIN_SCOPE || -v plugin_local ]]; then exit 44; fi")
        result = subprocess.run([BASH, '--noprofile', '--rcfile', str(self.rc), '-i'], input=script + '\nexit\n',
                                text=True, capture_output=True, env=self.env, cwd=self.root, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('second:second:second', result.stdout.splitlines())
        self.assertNotIn('readonly', result.stderr)

    def test_traced_nested_load_keeps_inner_function_after_outer_unload(self):
        self.plugin('outer', "shared_command() { echo outer; }\n", 'load=event', 'event=start_outer')
        self.plugin('inner', "shared_command() { echo inner; }\n", 'load=event', 'event=start_inner')
        self.env['BINIT_COMPLETION_MENU'] = '0'
        self.sync()
        result = self.shell(r"""
set +T
declare -ft binit __binit_load __binit_record_effects
# Transfer the function while the outer loader is recording its effects.
replace_from_inner() {
    if [[ ${__binit_record_id-} == outer && ${__binit_record_name-} == shared_command && $replaced == 0 ]]; then
        replaced=1
        unset -f shared_command
        binit load inner || exit 41
    fi
}
replaced=0
trap 'replace_from_inner' DEBUG
binit load outer || exit 42
trap - DEBUG
[[ $replaced == 1 ]] || exit 43
binit unload outer || exit 44
shared_command || exit 45
binit unload inner || exit 46
if declare -F shared_command >/dev/null; then exit 47; fi
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['inner'])

    def test_unload_dependency_refusal_preserves_both_plugins(self):
        self.plugin('parent', 'parent_function() { :; }\n', 'load=startup')
        self.plugin('child', 'child_function() { :; }\n', 'load=startup', 'depends=parent')
        self.sync()
        result = self.shell("if binit unload parent; then exit 41; fi; "
                            "declare -F parent_function child_function >/dev/null || exit 42; "
                            "[[ ${__binit_state[parent]} == loaded && ${__binit_state[child]} == loaded ]] || exit 43")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('required by child', result.stderr)

    @unittest.skipIf(os.environ.get('BINIT_NATIVE') == '0', 'native module required')
    def test_unload_falls_back_after_native_module_is_removed(self):
        self.plugin('sample', 'greet() { :; }\nother_new() { :; }\n', 'load=command', 'commands=greet')
        self.sync()
        result = self.shell("greet; builtin enable -d binit_complete || exit 41; "
                            "binit unload sample || exit 42; "
                            "if declare -F greet other_new >/dev/null; then exit 43; fi")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unload_keeps_preexisting_definition_that_plugin_replaced(self):
        self.plugin('sample', "greet() { :; }\nexisting() { printf replaced; }\nnew_function() { :; }\n", 'load=command', 'commands=greet')
        self.sync()
        result = self.shell("existing() { printf old; }; greet; binit unload sample; existing; printf '\\n'; declare -F new_function >/dev/null || printf 'new-gone\\n'")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['replaced', 'new-gone'])

    def test_completion_admin_never_overwrites_user_replacement(self):
        self.plugin('sample', "_sample_complete() { COMPREPLY=(one); }\ncomplete -F _sample_complete demo\n", 'load=completion', 'completions=demo')
        self.sync()
        result = self.shell("binit completions disable demo; complete -p demo >/dev/null 2>&1 || printf 'disabled\\n'; binit completions enable demo; complete -p demo; binit load sample; binit completions disable demo; complete -W user demo; binit completions enable demo || printf 'protected\\n'; complete -p demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('disabled\n', result.stdout)
        self.assertIn('protected\n', result.stdout)
        self.assertIn("complete -W 'user' demo", result.stdout)

    def test_completion_admin_changes_owned_group_in_this_shell(self):
        self.plugin('sample', 'complete -W owned sample pick\n', 'load=completion', 'completions=sample,pick')
        self.plugin('separate', 'complete -W spare spare\n', 'load=completion', 'completions=spare')
        self.sync()
        script = '''
            binit completions list --plugin sample
            binit completions disable --plugin sample || exit 10
            for name in sample pick; do complete -p "$name" >/dev/null 2>&1 && exit 11; done
            complete -p spare || exit 12
            binit completions enable --plugin sample || exit 13
            complete -p sample pick || exit 14
            binit completions disable --plugin sample || exit 15
            binit load sample || exit 16
            for name in sample pick; do complete -p "$name" >/dev/null 2>&1 && exit 17; done
            binit completions list --plugin sample
            binit completions enable --plugin sample || exit 18
            complete -p sample pick || exit 19
            complete -W foreign pick
            if binit completions disable --plugin sample; then exit 20; fi
            complete -p sample pick || exit 21
            binit completions list --plugin sample
            binit completions disable sample || exit 22
            complete -p sample >/dev/null 2>&1 && exit 23
            complete -p pick || exit 24
        '''
        inputrc = self.root / 'inputrc'
        self.env['INPUTRC'] = str(inputrc)
        for mode in ('emacs', 'vi'):
            with self.subTest(mode=mode):
                inputrc.write_text(f'set editing-mode {mode}\n')
                result = self.shell(script)
                self.assertEqual(result.returncode, 0, result.stderr)
                output = result.stdout
                self.assertTrue(output.startswith('pick sample enabled\nsample sample enabled\n'))
                self.assertIn('pick sample disabled\nsample sample disabled\n', output)
                self.assertIn('complete -F __binit_complete sample\n', output)
                self.assertEqual(output.count("complete -W 'owned' sample\n"), 2)
                self.assertIn("complete -W 'foreign' pick\npick sample enabled\nsample sample enabled\n", output)
                self.assertIn('binit: completion changed: pick', result.stderr)
                fresh = self.shell('binit completions list --plugin sample')
                self.assertEqual(fresh.stdout, 'pick sample enabled\nsample sample enabled\n')

    def test_completion_admin_restores_bindings_if_a_bulk_write_fails(self):
        self.plugin('sample', 'complete -W owned sample pick\n', 'load=completion', 'completions=sample,pick')
        self.sync()
        result = self.shell('''
            binit completions list --plugin sample >/dev/null
            original=$(declare -f __binit_completion_write)
            eval "${original/__binit_completion_write /real_completion_write }"
            calls=0
            __binit_completion_write() {
                calls=$((calls + 1))
                (( calls != 2 )) || return 1
                real_completion_write "$@"
            }
            if binit completions disable --plugin sample; then exit 10; fi
            [[ $calls == 3 ]] || exit 11
            complete -p sample pick || exit 12
            binit completions list --plugin sample
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('complete -F __binit_complete sample\n', result.stdout)
        self.assertIn('complete -F __binit_complete pick\n', result.stdout)
        self.assertTrue(result.stdout.endswith('pick sample enabled\nsample sample enabled\n'))

    @unittest.skipIf(os.environ.get('BINIT_NATIVE') == '0', 'native module required')
    def test_completion_admin_preserves_disabled_isolated_provider_on_refresh(self):
        provider = self.home / 'provider'
        provider.write_text('#!/bin/bash\nprintf "%s\\0" "$1"\n')
        provider.chmod(0o700)
        self.plugin('sample', '''
            binit completions register sample "$HOME/provider" initial
            refresh_sample() {
                [[ ${refresh_completion-0} == 1 ]] || return 0
                refresh_completion=0
                binit completions register sample "$HOME/provider" refreshed && printf 'refreshed\\n'
            }
            binit_on prompt refresh_sample
        ''', 'load=completion', 'completions=sample')
        self.sync()
        script = '''
            binit load sample || exit 10
            binit completions disable --plugin sample || exit 11
            refresh_completion=1
            complete -p sample >/dev/null 2>&1 && exit 13
            binit completions list --plugin sample
            binit completions enable --plugin sample || exit 14
            complete -p sample || exit 15
        '''
        inputrc = self.root / 'inputrc'
        self.env['INPUTRC'] = str(inputrc)
        for mode in ('emacs', 'vi'):
            with self.subTest(mode=mode):
                inputrc.write_text(f'set editing-mode {mode}\n')
                result = self.shell(script)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('refreshed\nsample sample disabled\n', result.stdout)
                self.assertIn('complete -o filenames -F __binit_provider_complete sample\n', result.stdout)

    def test_owned_prompt_segment_is_reported_and_removed_on_unload(self):
        self.plugin('sample', "refresh_sample() { binit_segment sample READY; }\nbinit_on prompt refresh_sample\n", 'load=startup')
        self.sync()
        result = self.shell('binit report sample; binit unload sample; binit report sample')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines().count('segment sample'), 1)


if __name__ == '__main__':
    unittest.main()
