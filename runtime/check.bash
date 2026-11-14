# The manager supplies only private paths and a reviewed fixture plan.
declare -r __binit_diag_expected_ps4=$'+__BINIT_DIAG__\t${EPOCHREALTIME/./}\t${BASHPID}\t${BASH_SUBSHELL}\t${#FUNCNAME[@]}\t${FUNCNAME[0]-}\t${BASH_SOURCE[0]-}\t${LINENO}\t${__binit_check_phase-setup}\t'
if [[ -n ${__binit_trace_fd-} ]]; then
    BASH_XTRACEFD=$__binit_trace_fd
    PS4=$__binit_diag_expected_ps4
    __binit_check_phase=setup
    set -x
fi
umask 077
declare -r __binit_check_setup=$1 __binit_check_init=$2 __binit_check_script=$3
declare -r __binit_check_phase_file=$4 __binit_check_view=$5 __binit_check_generation=$6
declare -r __binit_check_native=$7
declare -r __binit_diag_metadata=${8-}
# Runtime arrays are supplied by the verified initialization plan.
# shellcheck disable=SC2154
__binit_diag_emit() {
    [[ -n $__binit_diag_metadata ]] || return 0
    local __binit_diag_valid=1 __binit_diag_id __binit_diag_key __binit_diag_name __binit_diag_definition
    local __binit_diag_count=0 __binit_diag_overflow=0
    [[ $- == *x* && ${BASH_XTRACEFD-} == "${__binit_trace_fd-}" && $PS4 == "$__binit_diag_expected_ps4" ]] || __binit_diag_valid=0
    __binit_check_phase=metadata
    set +x
    {
        builtin printf '%s\0' BINIT_DIAG_META 1 "$__binit_diag_valid" ''
        if builtin declare -p __binit_ids __binit_state __binit_owned_functions >/dev/null 2>&1; then
            # shellcheck disable=SC2154
            for __binit_diag_id in "${__binit_ids[@]}"; do
                if (( __binit_diag_count >= 4096 )); then __binit_diag_overflow=1; break; fi
                builtin printf '%s\0' state "$__binit_diag_id" "${__binit_state[$__binit_diag_id]-unknown}" "${__binit_reason[$__binit_diag_id]-}"
                __binit_diag_count=$((__binit_diag_count + 1))
            done
            for __binit_diag_key in "${!__binit_owned_functions[@]}"; do
                if (( __binit_diag_count >= 4096 )); then __binit_diag_overflow=1; break; fi
                __binit_diag_name=${__binit_diag_key#*:}
                if ! builtin binit_complete effects function "$__binit_diag_name" __binit_diag_definition 2>/dev/null; then
                    __binit_diag_definition=$(builtin declare -f -- "$__binit_diag_name") || continue
                fi
                # A later definition must not be blamed on an earlier owner.
                [[ $__binit_diag_definition == "${__binit_owned_functions[$__binit_diag_key]}" ]] || continue
                builtin printf '%s\0' function "${__binit_diag_key%%:*}" "$__binit_diag_name" ''
                __binit_diag_count=$((__binit_diag_count + 1))
            done
        else
            __binit_diag_valid=0
        fi
        builtin printf '%s\0' end "$__binit_diag_count" "$__binit_diag_overflow" "$__binit_diag_valid"
    } > "$__binit_diag_metadata"
}
builtin printf 'setup\n' > "$__binit_check_phase_file"
if [[ -n $__binit_check_setup ]]; then
    builtin source "$__binit_check_setup" || { __binit_check_result=$?; __binit_diag_emit; builtin exit "$__binit_check_result"; }
fi
export __binit_check_state=$__binit_check_view
__binit_check_phase=init
builtin printf 'init\n' > "$__binit_check_phase_file"
builtin source "$__binit_check_init" || { __binit_check_result=$?; __binit_diag_emit; builtin exit "$__binit_check_result"; }
[[ ${__binit_active-0} == 1 && ${BINIT_STATE_DIR-} == "$__binit_check_view" &&
   ${__binit_generation_dir-} == "$__binit_check_generation" ]] || { __binit_diag_emit; builtin exit 125; }
# Candidate initialization supplies these runtime arrays.
# shellcheck disable=SC2154
for __binit_check_plugin in "${__binit_ids[@]}"; do
    # shellcheck disable=SC2154
    case ${__binit_state[$__binit_check_plugin]} in
        failed|blocked|loading|checking) __binit_diag_emit; builtin exit 125 ;;
    esac
done
if [[ $__binit_check_native == 1 ]]; then
    builtin binit_complete status >/dev/null 2>&1 || { __binit_diag_emit; builtin exit 125; }
elif builtin binit_complete status >/dev/null 2>&1; then
    __binit_diag_emit; builtin exit 125
fi
__binit_check_phase=check
builtin printf 'check\n' > "$__binit_check_phase_file"
builtin source "$__binit_check_script"
__binit_check_result=$?
builtin printf 'complete:%s\n' "$__binit_check_result" > "$__binit_check_phase_file"
__binit_diag_emit
builtin exit "$__binit_check_result"
