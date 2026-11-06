# shellcheck shell=bash
# shellcheck disable=SC2154 # The generated initialization plan supplies manager paths.
if (( BASH_VERSINFO[0] < 5 || (BASH_VERSINFO[0] == 5 && BASH_VERSINFO[1] < 2) )); then
    printf 'binit: Bash 5.2 or newer is required\n' >&2
    return 1
fi
if [[ ${PROMPT_COMMAND@a} == *r* || ${PROMPT_COMMAND@a} == *A* ]] || declare -F binit >/dev/null; then
    printf 'binit: existing shell configuration prevents initialization\n' >&2
    return 1
fi

declare -ga __binit_ids=() __binit_prompt_hooks=() __binit_directory_hooks=() __binit_segments=()
declare -gA __binit_entry=() __binit_load_kind=() __binit_requires=() __binit_depends=()
declare -gA __binit_state=() __binit_load_us=() __binit_hook_us=() __binit_hook_calls=()
declare -gA __binit_hook_owner=() __binit_segment_value=() __binit_completion_owner=()
declare -g __binit_loading='' __binit_last_directory='' BINIT_LAST_STATUS=0 BINIT_PROMPT_TEXT=''

__binit_register() {
    local id=$1 entry=$2 kind=$3 requires=$4 depends=$5 completions=$6 command
    __binit_ids+=("$id")
    __binit_entry[$id]=$entry
    __binit_load_kind[$id]=$kind
    __binit_requires[$id]=$requires
    __binit_depends[$id]=$depends
    __binit_state[$id]=pending
    __binit_load_us[$id]=0
    __binit_hook_us[$id]=0
    __binit_hook_calls[$id]=0
    for command in $completions; do __binit_completion_owner[$command]=$id; done
}

binit_on() {
    local event=${1-} callback=${2-}
    [[ -n $__binit_loading && -n $callback ]] || return 1
    declare -F -- "$callback" >/dev/null || return 1
    case $event in
        prompt) __binit_prompt_hooks+=("$callback") ;;
        directory) __binit_directory_hooks+=("$callback") ;;
        *) return 1 ;;
    esac
    __binit_hook_owner[$callback]=$__binit_loading
}

binit_segment() {
    local id=${1-} value=${2-} LC_ALL=C
    [[ -n $id ]] || return 1
    if [[ ! -v __binit_segment_value[$id] ]]; then __binit_segments+=("$id"); fi
    value=${value//[$'\001'-$'\037'$'\177']/}
    __binit_segment_value[$id]=${value:0:80}
}

__binit_load() {
    local id=$1 requirement dependency before after saved_loading=$__binit_loading
    case ${__binit_state[$id]-missing} in
        loaded) return 0 ;;
        pending) ;;
        *) return 1 ;;
    esac
    for dependency in ${__binit_depends[$id]}; do
        if ! __binit_load "$dependency"; then __binit_state[$id]=unavailable; return 1; fi
    done
    for requirement in ${__binit_requires[$id]}; do
        if ! command -v -- "$requirement" >/dev/null; then
            __binit_state[$id]=unavailable
            return 1
        fi
    done
    before=${EPOCHREALTIME/./}
    __binit_loading=$id
    # Sourced plugins are trusted code. A failed load can have partial effects.
    # shellcheck source=/dev/null
    if source "${__binit_entry[$id]}"; then __binit_state[$id]=loaded
    else __binit_state[$id]=failed
    fi
    __binit_loading=$saved_loading
    after=${EPOCHREALTIME/./}
    __binit_load_us[$id]=$((after - before))
    [[ ${__binit_state[$id]} == loaded ]]
}

__binit_call() {
    local callback=$1 owner=${__binit_hook_owner[$1]} before after
    [[ ${__binit_state[$owner]} == loaded ]] || return 0
    before=${EPOCHREALTIME/./}
    if "$callback" "$BINIT_LAST_STATUS" "$PWD"; then :; else :; fi
    after=${EPOCHREALTIME/./}
    __binit_hook_us[$owner]=$(( ${__binit_hook_us[$owner]} + after - before ))
    __binit_hook_calls[$owner]=$(( ${__binit_hook_calls[$owner]} + 1 ))
}

__binit_prompt_dispatch() {
    local status=$? id callback value
    BINIT_LAST_STATUS=$status
    for id in "${__binit_ids[@]}"; do
        if [[ ${__binit_load_kind[$id]} == prompt && ${__binit_state[$id]} == pending ]]; then
            if __binit_load "$id"; then :; fi
        fi
    done
    if [[ $__binit_last_directory != "$PWD" ]]; then
        __binit_last_directory=$PWD
        for callback in "${__binit_directory_hooks[@]}"; do __binit_call "$callback"; done
    fi
    for callback in "${__binit_prompt_hooks[@]}"; do __binit_call "$callback"; done
    BINIT_PROMPT_TEXT=''
    for id in "${__binit_segments[@]}"; do
        value=${__binit_segment_value[$id]}
        if [[ -n $value ]]; then BINIT_PROMPT_TEXT+="[$value] "; fi
    done
    return "$status"
}

__binit_complete() {
    local name=${COMP_WORDS[0]-} id
    [[ -n $name && -v __binit_completion_owner[$name] ]] || return 1
    id=${__binit_completion_owner[$name]}
    if __binit_load "$id"; then
        # Bash retries completion using the specification installed by the plugin.
        return 124
    fi
    complete -r -- "$name"
    return 1
}

__binit_has_completion() {
    [[ $(builtin type -a -t binit_complete 2>/dev/null) == *builtin* ]]
}

__binit_load_completion() {
    if __binit_has_completion; then return 0; fi
    [[ -f $__binit_generation_dir/completion.so ]] || return 1
    bind -q complete >/dev/null 2>&1 || :
    builtin enable -f "$__binit_generation_dir/completion.so" binit_complete
}

binit() {
    if [[ ${1-} == profile ]]; then
        local id
        printf '%-20s %-12s %10s %10s %8s\n' PLUGIN STATE LOAD_US HOOK_US CALLS
        for id in "${__binit_ids[@]}"; do
            printf '%-20s %-12s %10s %10s %8s\n' "$id" "${__binit_state[$id]}" "${__binit_load_us[$id]}" "${__binit_hook_us[$id]}" "${__binit_hook_calls[$id]}"
        done
    elif [[ ${1-} == completion ]]; then
        if (( $# > 2 )) || [[ ! ${2-status} =~ ^(on|off|status)$ ]]; then
            printf 'Usage: binit completion [on|off|status]\n' >&2
            return 2
        fi
        if [[ ${2-status} == on ]]; then __binit_load_completion || return 1; fi
        if __binit_has_completion; then
            if builtin binit_complete "${2-status}"; then
                if [[ ${2-status} != status ]]; then builtin binit_complete status; fi
            else
                return "$?"
            fi
        else
            printf 'Completion menu: unavailable in this shell\n' >&2
            return 1
        fi
    elif [[ ${1-} == run ]]; then
        command "$__binit_manager" "$@"
    else
        if command "$__binit_manager" "$@" --state "$BINIT_STATE_DIR"; then
            if [[ ${1-} == trust ]]; then __binit_last_directory=''; fi
        else
            return "$?"
        fi
    fi
}

__binit_activate() {
    local id command
    # Check all completion conflicts before a startup dependency can load one.
    for command in "${!__binit_completion_owner[@]}"; do
        if complete -p -- "$command" &>/dev/null; then
            id=${__binit_completion_owner[$command]}
            __binit_state[$id]=blocked
        fi
    done
    for id in "${__binit_ids[@]}"; do
        if [[ ${__binit_load_kind[$id]} == startup ]]; then
            if __binit_load "$id"; then :; fi
        fi
    done
    for command in "${!__binit_completion_owner[@]}"; do
        id=${__binit_completion_owner[$command]}
        if [[ ${__binit_state[$id]} == pending ]]; then complete -F __binit_complete -- "$command"; fi
    done
    # Returning the saved status lets the existing first prompt hook see it.
    PROMPT_COMMAND=(__binit_prompt_dispatch "${PROMPT_COMMAND[@]}")
    # Expand segment data through a variable, never as executable prompt source.
    if shopt -q promptvars; then PS1='${BINIT_PROMPT_TEXT}'"${PS1-\\s-\\v\\$ }"; fi
    __binit_active=1
    if [[ ${BINIT_COMPLETION_MENU-1} == 1 && -f $__binit_generation_dir/completion.so ]]; then
        if __binit_load_completion >/dev/null 2>&1; then
            builtin binit_complete on >/dev/null 2>&1 || :
        fi
    fi
}
