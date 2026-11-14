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

    def test_check_functions_preserves_definitions_metadata_and_missing_entries(self):
        self.shell(r'''
first() { printf '%s\n' 'literal $(must-not-run)'; }
hello-world() { cat <<'EOF'
}
literal $(must-not-run)
EOF
}
redirected() { printf must-not-run; } > >(printf must-not-run)
foreign() { : untouched; }
declare -A owned=()
for name in first hello-world redirected; do
    owned["sample:$name"]=$(declare -f -- "$name")
done
owned[sample:missing]='old definition'
owned[other:first]='foreign ownership'
declare -r owned
snapshot=$(declare -p owned)
binit_complete effects check-functions sample owned || exit 1
[[ $(declare -p owned) == "$snapshot" ]] || exit 2
for name in first hello-world redirected foreign; do declare -F "$name" >/dev/null || exit 3; done
''')

    def test_check_functions_refuses_changed_and_readonly_without_removal(self):
        self.shell(r'''
first() { : original; }
last() { : original; }
declare -A owned=([sample:first]="$(declare -f first)" [sample:last]="$(declare -f last)")
last() { : replaced; }
if binit_complete effects check-functions sample owned; then exit 1; fi
declare -F first last >/dev/null || exit 2
owned[sample:last]=$(declare -f last)
readonly -f last
if binit_complete effects check-functions sample owned; then exit 3; fi
declare -F first last >/dev/null || exit 4
''')

    def test_check_functions_rejects_unsafe_inputs_without_evaluation(self):
        self.shell(r'''
safe() { :; }
declare -A owned=([sample:safe]="$(declare -f safe)")
declare -n reference=owned
declare -a indexed=(value)
for source in reference indexed BASH_ALIASES; do
    if binit_complete effects check-functions sample "$source"; then exit 1; fi
done
for id in '' 'sample:other' 'bad/id' '1bad'; do
    if binit_complete effects check-functions "$id" owned; then exit 2; fi
done
owned[sample:__binit_hidden]='anything'
if binit_complete effects check-functions sample owned; then exit 3; fi
declare -F safe >/dev/null || exit 4
''')

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

    def test_record_functions_preserves_before_and_prior_ownership(self):
        self.shell(r'''
existing() { : original; }
reintroduced() { : original; }
gone() { : original; }
declare -A before=() owned=()
for name in reintroduced gone; do
    owned["prior:$name"]=$(declare -f -- "$name")
done
owned[prior:empty_owned]=''
old_reintroduced=${owned[prior:reintroduced]}
old_gone=${owned[prior:gone]}
unset -f reintroduced gone
binit_complete effects names function before || exit 1
before[existing]=''
declare -r before

existing() { : changed; }
reintroduced() { : replacement; }
empty_owned() { : replacement; }
quoted() { printf '%s\n' 'quoted $text' "$(printf must-not-run)"; }
document() { cat <<'EOF'
}
literal $(printf must-not-run)
EOF
}
redirected() { printf must-not-run; } > >(printf must-not-run)
__binit_hidden() { :; }
binit() { :; }
binit_on() { :; }
binit_segment() { :; }
function odd.name { :; }

binit_complete effects record-functions plugin-2_9 before owned || exit 2
[[ ${#owned[@]} == 6 ]] || exit 3
[[ ${owned[prior:reintroduced]} == "$old_reintroduced" ]] || exit 4
[[ ${owned[prior:gone]} == "$old_gone" ]] || exit 5
[[ -v owned[prior:empty_owned] && ${owned[prior:empty_owned]} == '' ]] || exit 6
for name in quoted document redirected; do
    expected=$(declare -f -- "$name")
    [[ ${owned["plugin-2_9:$name"]-} == "$expected" ]] || exit 7
done
saved=$(declare -p owned)
binit_complete effects record-functions another before owned || exit 8
[[ $(declare -p owned) == "$saved" ]] || exit 9
''')

    def test_record_functions_matches_fallback_for_owner_keys_without_a_separator(self):
        self.shell(r'''
f() { : replacement; }
declare -A before=() owned=([f]='prior definition')
binit_complete effects record-functions plugin before owned || exit 1
[[ ${#owned[@]} == 1 && ${owned[f]} == 'prior definition' ]] || exit 2
''')

    def test_record_functions_keeps_the_complete_id_and_long_function_name(self):
        self.shell(r'''
printf -v id '%064d' 0
id=p${id:1}
printf -v name '%2048s' ''
name=${name// /f}
eval "$name() { :; }"
declare -A before=() owned=()
binit_complete effects record-functions "$id" before owned || exit 1
[[ ${owned["$id:$name"]-} == "$(declare -f -- "$name")" ]] || exit 2
''')

    def test_record_functions_initializes_output_only_when_it_records_a_function(self):
        self.shell(r'''
f() { :; }
declare -A before=() owners
binit_complete effects record-functions plugin before owners || exit 1
[[ -v 'owners[plugin:f]' ]] || exit 2
binit_complete effects names function before || exit 3
declare -A untouched
saved=$(declare -p untouched)
binit_complete effects record-functions plugin before untouched || exit 4
[[ $(declare -p untouched) == "$saved" ]] || exit 5
''')

    def test_record_functions_validation_is_non_mutating(self):
        self.shell(r'''
candidate() { :; }
declare -A before=([existing]='') owned=([prior:gone]='saved definition')
declare -A invisible_before
declare -Ar fixed=([keep]='unchanged')
declare scalar=unchanged
declare -a indexed=(unchanged)
declare -Ai integer_assoc=([keep]=7)
declare -Au upper_assoc=([keep]=unchanged)
declare -n reference=owned before_alias=owned before_ref=before
declare -n cycle_a=cycle_b cycle_b=cycle_a
saved=$(declare -p before owned fixed scalar indexed integer_assoc upper_assoc reference before_alias before_ref cycle_a cycle_b)

__binit_reject_record() {
    if binit_complete effects record-functions "$@" 2>/dev/null; then exit 1; fi
    [[ $(declare -p before owned fixed scalar indexed integer_assoc upper_assoc reference before_alias before_ref cycle_a cycle_b) == "$saved" ]] || exit 2
}

for target in fixed scalar indexed integer_assoc upper_assoc reference missing RANDOM 'owned[$(printf must-not-run)]' 'bad-name'; do
    __binit_reject_record plugin before "$target"
done
for source in scalar indexed missing cycle_a before_ref invisible_before 'before[$(printf must-not-run)]' 'bad-name'; do
    __binit_reject_record plugin "$source" owned
done
__binit_reject_record plugin owned owned
__binit_reject_record plugin before_alias owned
for id in '' '9bad' '_bad' 'bad.id' 'bad:id' 'bad/id' 'bad id' 'bad$(printf must-not-run)' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; do
    __binit_reject_record "$id" before owned
done
__binit_reject_record plugin before
__binit_reject_record plugin before owned extra

# Rejecting a dynamic output must not even read its dynamic value.
RANDOM=12345
expected=$RANDOM
RANDOM=12345
if binit_complete effects record-functions plugin before RANDOM 2>/dev/null; then exit 3; fi
actual=$RANDOM
[[ $actual == "$expected" ]] || exit 4
[[ $(declare -p before owned fixed scalar indexed integer_assoc upper_assoc reference before_alias before_ref cycle_a cycle_b) == "$saved" ]] || exit 5
''')

    def test_runtime_recording_matches_native_disabled_fallback(self):
        source = (ROOT / 'runtime/core.bash').read_text()
        start = source.index('__binit_record_effects() {')
        end = source.index('\n}', start) + 2
        self.shell(source[start:end] + r'''

# Supply name snapshots independently of the native module so both runs
# exercise the same real runtime recorder with the same inputs.
__binit_capture_names() {
    local -n __binit_test_target=$1
    local __binit_test_name
    __binit_test_target=()
    while IFS= read -r __binit_test_name; do
        [[ $__binit_test_name =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ &&
           $__binit_test_name != __binit_* &&
           $__binit_test_name != binit &&
           $__binit_test_name != binit_on &&
           $__binit_test_name != binit_segment ]] || continue
        __binit_test_target[$__binit_test_name]=1
    done < <(compgen -A "$2")
}
existing() { : original; }
declare -A before_functions=() before_aliases=()
__binit_capture_names before_functions function
__binit_capture_names before_aliases alias
existing() { : changed; }
new_function() { printf '%s\n' 'literal $text' "$(printf must-not-run)"; }
alias new_alias='printf "%s\n" "quoted alias"'
declare -A __binit_owned_functions=([prior:missing]='saved definition')
declare -A __binit_owned_aliases=()

__binit_record_effects plugin before_functions before_aliases || exit 1
[[ ${#__binit_owned_functions[@]} == 2 ]] || exit 2
[[ ${__binit_owned_functions[plugin:new_function]} == "$(declare -f new_function)" ]] || exit 3
[[ ${#__binit_owned_aliases[@]} == 1 ]] || exit 4
[[ ${__binit_owned_aliases[plugin:new_alias]} == "$(alias -p new_alias)" ]] || exit 5
native_functions=$(declare -p __binit_owned_functions)
native_aliases=$(declare -p __binit_owned_aliases)

enable -n binit_complete || exit 6
__binit_owned_functions=([prior:missing]='saved definition')
__binit_owned_aliases=()
__binit_record_effects plugin before_functions before_aliases || exit 7
[[ $(declare -p __binit_owned_functions) == "$native_functions" ]] || exit 8
[[ $(declare -p __binit_owned_aliases) == "$native_aliases" ]] || exit 9
''')
