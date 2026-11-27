# shellcheck shell=bash
# shellcheck disable=SC2154 # The generated runtime provides plugin state.

__binit_log_clock() {
    local -n __binit_log_clock_result=$1
    local __binit_log_time='' __binit_log_seconds __binit_log_fraction __binit_log_unused
    if [[ $__binit_log_source == native ]]; then
        builtin binit_complete clock 2>/dev/null || return 1
        __binit_log_clock_result=$__binit_log_time
    else
        IFS=' .' builtin read -r __binit_log_seconds __binit_log_fraction __binit_log_unused < /proc/uptime || return 1
        __binit_log_clock_result=$((10#$__binit_log_seconds * 1000000 + 10#$__binit_log_fraction * 10000))
    fi
}

__binit_log_start() {
    declare -ga __binit_log_ring=()
    declare -gi __binit_log_next=0 __binit_log_origin=0
    declare -g __binit_log_source=native
    if ! __binit_log_clock __binit_log_origin; then
        __binit_log_source=uptime
        __binit_log_clock __binit_log_origin || __binit_log_source=''
    fi
}

__binit_log_add() {
    local __binit_log_now __binit_log_duration=-1
    __binit_log_clock __binit_log_now || return 0
    if (( ${6:--1} >= 0 )); then __binit_log_duration=$((__binit_log_now - $6)); fi
    # Only internal labels and validated plugin IDs enter the ring.
    __binit_log_ring[$((__binit_log_next % 256))]="$((__binit_log_now - __binit_log_origin)) $__binit_log_duration $1 $2 $3 $4 $5"
    __binit_log_next=$((__binit_log_next + 1))
}

__binit_log_load() {
    local __binit_log_id=$1 __binit_log_reason __binit_log_trigger=${__binit_log_manual:-${__binit_load_kind[$1]}}
    if (( $3 )); then __binit_log_trigger=nested; fi
    case ${__binit_reason[$1]} in
        'condition not met') __binit_log_reason=condition ;;
        'command already defined:'*) __binit_log_reason=command-conflict ;;
        'dependency unavailable:'*) __binit_log_reason=dependency ;;
        'missing command:'*) __binit_log_reason=requirement ;;
        'before-load callback failed') __binit_log_reason=before-callback ;;
        'after-load callback failed') __binit_log_reason=after-callback ;;
        'entry returned '*) __binit_log_reason=entry ;;
        *) __binit_log_reason=none ;;
    esac
    if [[ ${__binit_state[$1]} == loaded ]]; then __binit_log_reason=none; fi
    __binit_log_add load "$__binit_log_id" "$__binit_log_trigger" "${__binit_state[$1]}" "$__binit_log_reason" "$2"
}

__binit_log_show() {
    if (( $# > 1 )) || [[ $# == 1 && $1 != --json ]]; then
        printf 'usage: binit logs [--json]\n' >&2
        return 2
    fi
    if [[ -z ${__binit_log_source-} ]]; then
        if [[ $# == 1 ]]; then printf '{"schema":1,"clock":null,"dropped":0,"events":[]}\n'
        else printf 'Logs disabled. Start a shell with BINIT_LOGS=1.\n'; fi
        return 0
    fi
    local __binit_log_first=0 __binit_log_index __binit_log_offset __binit_log_duration
    local __binit_log_stage __binit_log_id __binit_log_trigger __binit_log_outcome __binit_log_reason
    if (( __binit_log_next > 256 )); then __binit_log_first=$((__binit_log_next - 256)); fi
    if [[ $# == 1 ]]; then
        printf '{"schema":1,"clock":"%s","dropped":%d,"events":[' "$__binit_log_source" "$__binit_log_first"
    else
        printf '%10s %10s  %-12s %-16s %-12s %-12s %s\n' OFFSET_US ELAPSED_US STAGE PLUGIN TRIGGER OUTCOME REASON
        if (( __binit_log_first )); then printf '%s earlier events omitted\n' "$__binit_log_first"; fi
    fi
    for ((__binit_log_index=__binit_log_first; __binit_log_index<__binit_log_next; ++__binit_log_index)); do
        IFS=' ' builtin read -r __binit_log_offset __binit_log_duration __binit_log_stage __binit_log_id \
            __binit_log_trigger __binit_log_outcome __binit_log_reason <<< "${__binit_log_ring[$((__binit_log_index % 256))]}"
        if [[ $# == 1 ]]; then
            if (( __binit_log_index > __binit_log_first )); then printf ','; fi
            if (( __binit_log_duration < 0 )); then __binit_log_duration=null; fi
            printf '{"sequence":%d,"offset_us":%d,"duration_us":%s,"stage":"%s","plugin":"%s","trigger":"%s","outcome":"%s","reason":"%s"}' \
                "$__binit_log_index" "$__binit_log_offset" "$__binit_log_duration" "$__binit_log_stage" \
                "$__binit_log_id" "$__binit_log_trigger" "$__binit_log_outcome" "$__binit_log_reason"
        else
            if (( __binit_log_duration < 0 )); then __binit_log_duration=-; fi
            printf '%10s %10s  %-12s %-16s %-12s %-12s %s\n' "$__binit_log_offset" "$__binit_log_duration" \
                "$__binit_log_stage" "$__binit_log_id" "$__binit_log_trigger" "$__binit_log_outcome" "$__binit_log_reason"
        fi
    done
    if [[ $# == 1 ]]; then printf ']}\n'; fi
}
