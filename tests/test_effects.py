"""Native snapshots must match Bash serialization without evaluating definitions."""
import os
from pathlib import Path
import shlex
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = Path(os.environ.get('BINIT_TEST_MODULE', ROOT / 'target/native-check/binit_complete.so')).resolve()
BASH = os.environ.get('BINIT_BASH', '/bin/bash')


class EffectsTests(unittest.TestCase):
    def shell(self, body):
        environment = dict(os.environ)
        environment.pop('BASH_ENV', None)
        environment.pop('ENV', None)
        result = subprocess.run([BASH, '--noprofile', '--norc', '-c',
                                 'enable -f ' + shlex.quote(str(MODULE)) + ' binit_complete\n' + body],
                                env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(result.stdout, '')

    def test_names_match_filtered_bash_compgen_and_clear_previous_snapshot(self):
        self.shell(r'''
hello-world() { :; }
__binit_hidden() { :; }
binit() { :; }
alias example='echo "quoted"'
alias 'odd.name=ignored'
declare -A actual=() expected=()
for kind in function alias; do
    expected=()
    while IFS= read -r name; do
        [[ $name =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ && $name != __binit_* && $name != binit && $name != binit_on && $name != binit_segment ]] || continue
        expected[$name]=1
    done < <(compgen -A "$kind")
    actual=([stale]=1)
    binit_complete effects names "$kind" actual || exit 1
    [[ ${#actual[@]} == ${#expected[@]} ]] || exit 2
    for name in "${!actual[@]}"; do [[ ${expected[$name]-} == 1 ]] || exit 3; done
done
''')

    def test_function_serialization_matches_bash_with_heredocs_and_redirections(self):
        self.shell(r'''
ordinary() { printf '%s\n' 'quoted $text' "$(false)"; }
document() { cat <<'EOF'
}
literal $(touch should-never-run)
EOF
}
redirected() { :; } >/dev/null
declare actual expected
for name in ordinary document redirected; do
    expected=$(declare -f -- "$name")
    binit_complete effects function "$name" actual || exit 1
    [[ $actual == "$expected" ]] || exit 2
done
''')

    def test_invalid_targets_fail_without_changing_them(self):
        self.shell(r'''
example() { :; }
declare -r fixed=unchanged
declare -i number=7
declare -a indexed=(unchanged)
declare -A snapshot=([unchanged]=1)
declare -n reference=snapshot
for target in fixed number indexed snapshot reference RANDOM 'array[$(false)]'; do
    if binit_complete effects function example "$target" 2>/dev/null; then exit 1; fi
done
for target in fixed number indexed reference; do
    if binit_complete effects names function "$target" 2>/dev/null; then exit 2; fi
done
if binit_complete effects names invalid snapshot 2>/dev/null; then exit 3; fi
[[ $fixed == unchanged && $number == 7 && ${indexed[0]} == unchanged && ${snapshot[unchanged]} == 1 ]] || exit 4
''')
