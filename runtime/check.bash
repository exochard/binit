# The manager supplies only private paths and a reviewed fixture plan.
umask 077
declare -r __binit_check_setup=$1 __binit_check_init=$2 __binit_check_script=$3
declare -r __binit_check_phase_file=$4 __binit_check_view=$5 __binit_check_generation=$6
declare -r __binit_check_native=$7
builtin printf 'setup\n' > "$__binit_check_phase_file"
if [[ -n $__binit_check_setup ]]; then
    builtin source "$__binit_check_setup" || builtin exit "$?"
fi
export __binit_check_state=$__binit_check_view
builtin printf 'init\n' > "$__binit_check_phase_file"
builtin source "$__binit_check_init" || builtin exit "$?"
[[ ${__binit_active-0} == 1 && ${BINIT_STATE_DIR-} == "$__binit_check_view" &&
   ${__binit_generation_dir-} == "$__binit_check_generation" ]] || builtin exit 125
# Candidate initialization supplies these runtime arrays.
# shellcheck disable=SC2154
for __binit_check_plugin in "${__binit_ids[@]}"; do
    # shellcheck disable=SC2154
    case ${__binit_state[$__binit_check_plugin]} in
        failed|blocked|loading|checking) builtin exit 125 ;;
    esac
done
if [[ $__binit_check_native == 1 ]]; then
    builtin binit_complete status >/dev/null 2>&1 || builtin exit 125
elif builtin binit_complete status >/dev/null 2>&1; then
    builtin exit 125
fi
builtin printf 'check\n' > "$__binit_check_phase_file"
builtin source "$__binit_check_script"
__binit_check_result=$?
builtin printf 'complete:%s\n' "$__binit_check_result" > "$__binit_check_phase_file"
builtin exit "$__binit_check_result"
