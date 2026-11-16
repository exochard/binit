"""Native snapshots must match Bash serialization without evaluating definitions."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
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

    def test_record_aliases_keeps_each_alias_value_and_prior_ownership(self):
        self.shell(r'''
alias existing=': original'
declare -A before=() owned=([prior:reintroduced]='prior definition')
binit_complete effects names alias before || exit 1
alias existing=': changed'
alias reintroduced=': replacement'
alias quoted='printf "%s" '\''literal $(must-not-run)'\'''
alias multiline=$'first\nsecond\n'
alias empty=''
alias hello-world=': hyphen'
alias 'odd.name=: untracked'
alias -- '-dash=: untracked'
snapshot=$(declare -p before)
binit_complete effects record-aliases plugin before owned || exit 2
[[ $(declare -p before) == "$snapshot" ]] || exit 3
[[ ${#owned[@]} == 5 && ${owned[prior:reintroduced]} == 'prior definition' ]] || exit 4
for name in quoted multiline empty hello-world; do
    [[ ${owned["plugin:$name"]+set} && ${owned["plugin:$name"]} == "${BASH_ALIASES[$name]}" ]] || exit 5
done
saved=$(declare -p owned)
binit_complete effects record-aliases other before owned || exit 6
[[ $(declare -p owned) == "$saved" ]] || exit 7
''')

    def test_record_aliases_rejects_unsafe_arrays_without_mutation(self):
        self.shell(r'''
alias candidate=': safe'
declare -A before=() owners=([prior:kept]=definition)
declare -Ar fixed=([keep]=unchanged)
declare -Ai numbers=([keep]=7)
declare -Au upper=([keep]=unchanged)
declare -a indexed=(unchanged)
declare scalar=unchanged
declare -n reference=owners
saved=$(declare -p before owners fixed numbers upper indexed scalar reference)
for target in fixed numbers upper indexed scalar reference RANDOM missing 'owners[$(printf must-not-run)]'; do
    if binit_complete effects record-aliases plugin before "$target" 2>/dev/null; then exit 1; fi
done
for source in indexed scalar reference RANDOM missing; do
    if binit_complete effects record-aliases plugin "$source" owners 2>/dev/null; then exit 2; fi
done
if binit_complete effects record-aliases plugin owners owners; then exit 3; fi
[[ $(declare -p before owners fixed numbers upper indexed scalar reference) == "$saved" ]] || exit 4
''')

    def test_watch_metadata_matches_stat_for_links_missing_paths_and_timestamps(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            directory = Path(temporary)
            file = directory / "literal 'quote\\ and\nnewline"
            file.write_text('data')
            os.utime(file, ns=(1_234_567_890_123_456_789, 1_234_567_890_987_654_321))
            (directory / 'link').symlink_to(file)
            (directory / 'broken').symlink_to(directory / 'absent')
            (directory / 'loop').symlink_to('loop')
            os.mkfifo(directory / 'fifo')
            for name, seconds in [('ancient', -62_009_366_400), ('future', 253_402_300_800)]:
                dated = directory / name
                dated.write_text('dated')
                os.utime(dated, ns=(seconds * 1_000_000_000, seconds * 1_000_000_000))
            paths = [file, directory, directory / 'link', directory / 'broken',
                     directory / 'loop', directory / 'missing', directory / 'fifo',
                     directory / 'ancient', directory / 'future']
            self.shell('paths=(' + ' '.join(shlex.quote(str(path)) for path in paths) + r''')
declare actual=unchanged
saved_flags=$-
saved_ifs=$IFS
for zone in UTC0 EST5EDT Asia/Kolkata Europe/Rome; do
    export TZ=$zone
    for path in "${paths[@]}"; do
        expected=$(LC_ALL=C command stat -Lc '%d:%i:%s:%y:%z' -- "$path" 2>/dev/null || printf missing)
        binit_complete effects watch "$path" actual || exit 1
        [[ $actual == "$expected" ]] || { printf 'metadata differs: <%s> <%s>\n' "$actual" "$expected" >&2; exit 2; }
    done
done
[[ $- == "$saved_flags" && $IFS == "$saved_ifs" ]] || exit 3
''')

    def test_watch_metadata_rejects_unsafe_outputs_without_evaluating_them(self):
        self.shell(r'''
declare scalar=unchanged
declare -r fixed=unchanged
declare -i number=7
declare -a indexed=(unchanged)
declare -A associative=([keep]=unchanged)
declare -n reference=scalar
saved=$(declare -p scalar fixed number indexed associative reference)
for output in fixed number indexed associative reference RANDOM missing 'associative[$(printf must-not-run)]'; do
    if binit_complete effects watch / "$output" 2>/dev/null; then exit 1; fi
done
[[ $(declare -p scalar fixed number indexed associative reference) == "$saved" ]] || exit 2
''')

    def test_watch_runtime_matches_fallback_for_missing_and_overridden_commands(self):
        source = (ROOT / 'runtime/core.bash').read_text()
        start = source.index('__binit_watch_snapshot() {')
        end = source.index('\n}', start) + 2
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            wrapper = Path(temporary) / 'stat'
            wrapper.write_text('#!/bin/bash\nprintf "wrapped:%s" "$*"\n')
            wrapper.chmod(0o700)
            self.shell(source[start:end] + '\n' + 'wrapper=' + shlex.quote(temporary) + r'''
declare -A __binit_watch=([fixture]=/) __binit_watch_value=() __binit_watch_dirty=()
saved_path=$PATH
exec >/dev/null
for mode in ordinary wrapper missing command_function stat_function functrace traced_function loader; do
    PATH=$saved_path
    case $mode in
        wrapper) PATH=$wrapper ;;
        missing) PATH=/nonexistent ;;
        command_function) command() { printf 'intercepted:%s' "$*"; } ;;
        stat_function) stat() { printf must-not-run; } ;;
        functrace) set -T; trap 'printf "DEBUG:%s\n" "$BASH_COMMAND"' DEBUG ;;
        traced_function) declare -ft __binit_watch_snapshot; trap 'printf "DEBUG:%s\n" "$BASH_COMMAND"' DEBUG ;;
        loader) export LD_PRELOAD=/nonexistent-binit-metadata-test ;;
    esac
    __binit_watch_value=()
    __binit_watch_snapshot fixture || exit 1
    actual=${__binit_watch_value[fixture:0]}
    enable -n binit_complete
    __binit_watch_value=()
    __binit_watch_snapshot fixture || exit 2
    expected=${__binit_watch_value[fixture:0]}
    enable binit_complete
    [[ $actual == "$expected" ]] || { printf 'resolution differs in %s\n' "$mode" >&2; exit 3; }
    trap - DEBUG
    set +T
    declare +t -f __binit_watch_snapshot
    unset LD_PRELOAD
    unset -f command stat
done
''')

    def test_traced_alias_recorder_does_not_claim_late_caller_alias(self):
        source = (ROOT / 'runtime/core.bash').read_text()
        functions = []
        for name in ('__binit_capture_names', '__binit_record_effects'):
            start = source.index(name + '() {')
            end = source.index('\n}', start) + 2
            functions.append(source[start:end])
        self.shell('\n'.join(functions) + r'''
set +T
set -- 'caller value' '' '$literal'
caller_pwd=$PWD
caller_ifs=$IFS
declare -A before_functions=() before_aliases=() __binit_owned_functions=() __binit_owned_aliases=()
__binit_capture_names before_functions function
__binit_capture_names before_aliases alias
before_snapshot=$(declare -p before_functions before_aliases)
plugin_effect() { printf '%s\n' 'literal $value'; }
alias early_plugin_alias='printf "%s" "literal $value"'
run_recorder() {
    builtin unalias late_caller_alias 2>/dev/null || :
    __binit_owned_functions=()
    __binit_owned_aliases=()
    trap_hits=0
    declare -ft __binit_record_effects
    trap 'case $BASH_COMMAND in *"effects record-functions"*) trap_hits=$((trap_hits+1)); builtin alias late_caller_alias="printf late-caller"; if [[ $trace_mode == once ]]; then trap - DEBUG; fi ;; esac' DEBUG
    __binit_record_effects sample before_functions before_aliases || exit 1
    trap - DEBUG
    (( trap_hits > 0 )) || exit 2
    [[ $- != *T* && $PWD == "$caller_pwd" && $IFS == "$caller_ifs" ]] || exit 3
    [[ $# == 0 && ${1-unset} == unset ]] || exit 4
    [[ $(builtin alias -- late_caller_alias) == "alias late_caller_alias='printf late-caller'" ]] || exit 5
    [[ ${__binit_owned_aliases[sample:early_plugin_alias]-} == "${BASH_ALIASES[early_plugin_alias]}" ]] || exit 6
    [[ ! -v __binit_owned_aliases[sample:late_caller_alias] ]] || exit 7
    [[ $(declare -p before_functions before_aliases) == "$before_snapshot" ]] || exit 8
}
for trace_mode in persistent once; do
    run_recorder
    native_aliases=$(declare -p __binit_owned_aliases)
    native_functions=$(declare -p __binit_owned_functions)
    builtin enable -n binit_complete || exit 9
    run_recorder
    [[ $(declare -p __binit_owned_aliases) == "$native_aliases" ]] || exit 10
    [[ $(declare -p __binit_owned_functions) == "$native_functions" ]] || exit 11
    builtin enable binit_complete || exit 12
done
[[ $# == 3 && $1 == 'caller value' && $2 == '' && $3 == '$literal' ]] || exit 13
''')

    def test_traced_watch_snapshot_preserves_debug_trap_file_mutation(self):
        source = (ROOT / 'runtime/core.bash').read_text()
        start = source.index('__binit_watch_snapshot() {')
        end = source.index('\n}', start) + 2
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
            watched = Path(temporary) / 'watched'
            self.shell(source[start:end] + '\nwatched=' + shlex.quote(str(watched)) + r'''
set +T
set -- 'caller value' '' '$literal'
caller_pwd=$PWD
caller_ifs=$IFS
declare -A __binit_watch=([fixture]="$watched") __binit_watch_value=() __binit_watch_dirty=()
run_watch() {
    printf 'initial' > "$watched"
    __binit_watch_value=()
    __binit_watch_dirty=()
    trap_hits=0
    declare -ft __binit_watch_snapshot
    trap 'case $BASH_COMMAND in __binit_watch_new=*) trap_hits=$((trap_hits+1)); printf "trap-mutated-content" > "$watched" ;; esac' DEBUG
    __binit_watch_snapshot fixture || exit 1
    trap - DEBUG
    (( trap_hits > 0 )) || exit 2
    [[ $- != *T* && $PWD == "$caller_pwd" && $IFS == "$caller_ifs" ]] || exit 3
    [[ $(<"$watched") == trap-mutated-content ]] || exit 4
    expected=$(LC_ALL=C command stat -Lc '%d:%i:%s:%y:%z' -- "$watched") || exit 5
    [[ ${__binit_watch_value[fixture:0]-} == "$expected" ]] || exit 6
}
run_watch
native_value=${__binit_watch_value[fixture:0]}
builtin enable -n binit_complete || exit 7
run_watch
[[ ${__binit_watch_value[fixture:0]} == "$expected" ]] || exit 8
[[ $# == 3 && $1 == 'caller value' && $2 == '' && $3 == '$literal' ]] || exit 9
''')

    def test_traced_alias_recorder_matches_shell_capture(self):
        source = (ROOT / 'runtime/core.bash').read_text()
        start = source.index('__binit_record_effects() {')
        end = source.index('\n}', start) + 2
        self.shell(source[start:end] + r'''
__binit_capture_names() {
    local -n target=$1
    target=()
    [[ $2 != alias ]] || target[new_alias]=1
}
declare -A before_functions=() before_aliases=() __binit_owned_functions=() __binit_owned_aliases=()
binit_complete effects names function before_functions
alias new_alias='printf "%s" "literal $value"'
exec >/dev/null
declare -ft __binit_record_effects
trap 'printf "DEBUG\n"' DEBUG
__binit_record_effects sample before_functions before_aliases || exit 1
actual=${__binit_owned_aliases[sample:new_alias]}
enable -n binit_complete
__binit_owned_functions=()
__binit_owned_aliases=()
__binit_record_effects sample before_functions before_aliases || exit 2
expected=${__binit_owned_aliases[sample:new_alias]}
trap - DEBUG
[[ $actual == "$expected" ]] || exit 3
''')

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

    def test_entry_snapshot_records_what_the_name_arrays_record(self):
        self.shell(r"""
existing() { : original; }
alias kept=': original'
declare -A before_f=() before_a=() old_f=([prior:gone]=saved) old_a=() new_f=([prior:gone]=saved) new_a=()
token=
binit_complete effects names function before_f || exit 1
binit_complete effects names alias before_a || exit 2
binit_complete effects mark token || exit 3
[[ $token == 1 ]] || exit 4
existing() { : changed; }
alias kept=': changed'
fresh() { printf '%s\n' 'literal $text' "$(printf must-not-run)"; }
other-name() { :; }
alias fresh_alias='printf "%s\n" "quoted alias"'
alias 'odd.name=: untracked'
binit_complete effects record-functions plugin before_f old_f || exit 5
binit_complete effects record-aliases plugin before_a old_a || exit 6
binit_complete effects record-since "$token" plugin new_f new_a || exit 7
[[ $(declare -p new_f) == "$(declare -p old_f | sed 's/old_f/new_f/')" ]] || exit 8
[[ $(declare -p new_a) == "$(declare -p old_a | sed 's/old_a/new_a/')" ]] || exit 9
[[ ${#new_f[@]} == 3 && ${#new_a[@]} == 1 ]] || exit 10
! binit_complete effects release "$token" || exit 11
! binit_complete effects record-since "$token" plugin new_f new_a || exit 12
""")

    def test_nested_snapshots_attribute_effects_to_the_inner_load(self):
        self.shell(r"""
declare -A owned_f=() owned_a=()
outer= inner= again=
binit_complete effects mark outer || exit 1
outer_fn() { :; }
binit_complete effects mark inner || exit 2
[[ $outer == 1 && $inner == 2 ]] || exit 3
inner_fn() { :; }
alias inner_alias=':'
binit_complete effects record-since "$inner" child owned_f owned_a || exit 4
! binit_complete effects release "$inner" || exit 5
binit_complete effects record-since "$outer" parent owned_f owned_a || exit 6
! binit_complete effects release "$outer" || exit 7
[[ ${owned_f[child:inner_fn]+set} && ${owned_f[parent:outer_fn]+set} && ${owned_a[child:inner_alias]+set} ]] || exit 8
[[ ${#owned_f[@]} == 2 && ${#owned_a[@]} == 1 ]] || exit 9
binit_complete effects mark again || exit 10
[[ $again == 1 ]] || exit 11
""")

    def test_entry_snapshot_falls_back_outside_a_plain_context(self):
        self.shell(r"""
declare -A owned_f=() owned_a=() names_f=() names_a=() direct_f=() direct_a=()
token=
early() { :; }
alias early_alias=':'
binit_complete effects mark token || exit 1
binit_complete effects names function direct_f || exit 2
binit_complete effects names alias direct_a || exit 3
late() { :; }
trap ':' DEBUG
! binit_complete effects record-since "$token" plugin owned_f owned_a || exit 4
other=
! binit_complete effects mark other || exit 4
trap - DEBUG
[[ -z $other ]] || exit 4
[[ ${#owned_f[@]} == 0 && ${#owned_a[@]} == 0 ]] || exit 5
binit_complete effects names-since "$token" function names_f || exit 6
binit_complete effects names-since "$token" alias names_a || exit 7
binit_complete effects release "$token" || exit 7
[[ $(declare -p names_f | sed 's/names_f/x/') == "$(declare -p direct_f | sed 's/direct_f/x/')" ]] || exit 8
[[ $(declare -p names_a | sed 's/names_a/x/') == "$(declare -p direct_a | sed 's/direct_a/x/')" ]] || exit 9
[[ ! ${names_f[late]+set} ]] || exit 10
""")

    def test_snapshot_tokens_and_outputs_are_validated_without_mutation(self):
        self.shell(r"""
declare -A owned_f=([keep]=1) owned_a=()
declare -Ar fixed=([keep]=1)
declare -a indexed=(unchanged)
declare -r constant=unchanged
token= extra=
! binit_complete effects mark constant || exit 1
! binit_complete effects mark missing_variable || exit 1
! binit_complete effects mark indexed || exit 2
[[ $constant == unchanged && ${indexed[*]} == unchanged ]] || exit 3
binit_complete effects mark token || exit 4
for bad in 0 2 -1 x 1x ''; do
    ! binit_complete effects record-since "$bad" plugin owned_f owned_a || exit 5
    ! binit_complete effects release "$bad" || exit 6
done
! binit_complete effects record-since "$token" plugin owned_f owned_f || exit 7
! binit_complete effects record-since "$token" plugin fixed owned_a || exit 8
! binit_complete effects record-since "$token" 'bad id' owned_f owned_a || exit 9
! binit_complete effects names-since "$token" variable owned_a || exit 10
[[ $(declare -p owned_f) == 'declare -A owned_f=([keep]="1" )' ]] || exit 11
binit_complete effects release "$token" || exit 12
for ((i = 1; i <= 64; i++)); do binit_complete effects mark token || exit 13; done
[[ $token == 64 ]] || exit 14
! binit_complete effects mark extra || exit 15
binit_complete effects release 1 || exit 16
binit_complete effects mark token && [[ $token == 1 ]] || exit 17
""")

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
[[ ${__binit_owned_aliases[plugin:new_alias]} == "${BASH_ALIASES[new_alias]}" ]] || exit 5
native_functions=$(declare -p __binit_owned_functions)
native_aliases=$(declare -p __binit_owned_aliases)

enable -n binit_complete || exit 6
__binit_owned_functions=([prior:missing]='saved definition')
__binit_owned_aliases=()
__binit_record_effects plugin before_functions before_aliases || exit 7
[[ $(declare -p __binit_owned_functions) == "$native_functions" ]] || exit 8
[[ $(declare -p __binit_owned_aliases) == "$native_aliases" ]] || exit 9
''')
