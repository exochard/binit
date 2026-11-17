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
declare -gA __binit_commands=() __binit_command_owner=() __binit_stub=() __binit_reason=()
declare -gA __binit_completions=() __binit_line_key=() __binit_line_seen=()
declare -gA __binit_delay_ms=() __binit_deadline=() __binit_event=() __binit_when=() __binit_watch=()
declare -gA __binit_watch_value=() __binit_watch_dirty=() __binit_before_callbacks=() __binit_after_callbacks=()
declare -gA __binit_owned_functions=() __binit_owned_aliases=() __binit_completion_spec=() __binit_completion_disabled=()
declare -gA __binit_provider_count=() __binit_provider_args=() __binit_provider_owner=() __binit_provider_spec=()
declare -gA __binit_segment_owner=() __binit_owned_segment_value=()
declare -ga __binit_line_counts=() __binit_line_args=()
declare -g __binit_loading='' __binit_callback_owner='' __binit_last_directory='' BINIT_LAST_STATUS=0 BINIT_PROMPT_TEXT=''
declare -g __binit_line_text='' __binit_first_prompt=0 __binit_lines_total=0 __binit_lines_matched=0
declare -gi __binit_wait_pending=0 __binit_wait_armed=0 __binit_dispatch_pending=0 __binit_load_active=0 __binit_event_changed=0
declare -ga __binit_wait_queue=()

# Collect only simple shell names; each definition is checked again on unload.
__binit_capture_names() {
    local -n __binit_capture_result=$1
    local __binit_capture_kind=$2 __binit_capture_name
    __binit_capture_result=()
    if builtin binit_complete effects names "$__binit_capture_kind" "$1" 2>/dev/null; then return 0; fi
    while IFS= read -r __binit_capture_name; do
        [[ $__binit_capture_name =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ && $__binit_capture_name != __binit_* && $__binit_capture_name != binit && $__binit_capture_name != binit_on && $__binit_capture_name != binit_segment ]] || continue
        __binit_capture_result[$__binit_capture_name]=1
    done < <( { builtin compgen -A "$__binit_capture_kind" 1>&"$__binit_definition_fd" || :; } {__binit_definition_fd}>&1 >/dev/null )
}

__binit_capture_plugin_completions() {
    local __binit_comp_id=$1 __binit_comp_name __binit_comp_current
    local -a __binit_comp_names=()
    if [[ -n "${__binit_completions[$__binit_comp_id]}" ]]; then IFS=' ' read -r -a __binit_comp_names <<< "${__binit_completions[$__binit_comp_id]}"; else __binit_comp_names=(); fi
    for __binit_comp_name in "${__binit_comp_names[@]}"; do
        [[ ${__binit_pre_completion[$__binit_comp_name]-safe} == safe ]] || continue
        __binit_comp_current=$( { builtin complete -p -- "$__binit_comp_name" 1>&"$__binit_definition_fd"; } {__binit_definition_fd}>&1 >/dev/null ) || __binit_comp_current=''
        [[ -n $__binit_comp_current ]] || continue
        __binit_completion_spec[$__binit_comp_name]=$__binit_comp_current
        if [[ ${__binit_completion_disabled[$__binit_comp_name]-0} == 1 ]]; then
            builtin complete -r -- "$__binit_comp_name" || :
        fi
    done
}

__binit_record_effects() {
    local __binit_record_id=$1 __binit_record_name __binit_record_def __binit_record_key __binit_record_owned __binit_record_native_aliases=0
    local __binit_record_mark=${4-}
    local -n __binit_record_before_functions=$2 __binit_record_before_aliases=$3
    local -A __binit_record_functions=() __binit_record_aliases=()
    if [[ -n $__binit_record_mark ]]; then
        # The native record needs a plain context. When the plugin left a trap, the shell path below records
        # from the entry snapshot copied into the caller's arrays.
        if ! builtin binit_complete effects names-since "$__binit_record_mark" function "$2" 2>/dev/null ||
           ! builtin binit_complete effects names-since "$__binit_record_mark" alias "$3" 2>/dev/null; then
            builtin binit_complete effects release "$__binit_record_mark" 2>/dev/null || :
            return 1
        fi
        builtin binit_complete effects release "$__binit_record_mark" 2>/dev/null || :
    fi
    # A trap can remove itself after this snapshot, so retain entry eligibility.
    if [[ $- != *T* ]] && builtin binit_complete effects plain 2>/dev/null; then __binit_record_native_aliases=1; fi
    __binit_capture_names __binit_record_aliases alias
    if ! builtin binit_complete effects record-functions "$__binit_record_id" "$2" __binit_owned_functions 2>/dev/null; then
        __binit_capture_names __binit_record_functions function
        for __binit_record_name in "${!__binit_record_functions[@]}"; do
            [[ -v __binit_record_before_functions[$__binit_record_name] ]] && continue
            __binit_record_owned=0
            for __binit_record_key in "${!__binit_owned_functions[@]}"; do
                [[ ${__binit_record_key#*:} != "$__binit_record_name" ]] || __binit_record_owned=1
            done
            (( __binit_record_owned )) && continue
            if ! builtin binit_complete effects function "$__binit_record_name" __binit_record_def 2>/dev/null; then
                __binit_record_def=$( { builtin declare -f -- "$__binit_record_name" 1>&"$__binit_definition_fd"; } {__binit_definition_fd}>&1 >/dev/null )
            fi
            [[ -n $__binit_record_def ]] && __binit_owned_functions["$__binit_record_id:$__binit_record_name"]=$__binit_record_def
        done
    fi
    # An empty post-load snapshot has no new alias to record on either path.
    (( ${#__binit_record_aliases[@]} )) || return 0
    if (( __binit_record_native_aliases )) && builtin binit_complete effects record-aliases "$__binit_record_id" "$3" __binit_owned_aliases 2>/dev/null; then return 0; fi
    for __binit_record_name in "${!__binit_record_aliases[@]}"; do
        [[ -v __binit_record_before_aliases[$__binit_record_name] ]] && continue
        __binit_record_owned=0
        for __binit_record_key in "${!__binit_owned_aliases[@]}"; do
            [[ ${__binit_record_key#*:} != "$__binit_record_name" ]] || __binit_record_owned=1
        done
        (( __binit_record_owned )) && continue
        [[ -v BASH_ALIASES[$__binit_record_name] ]] &&
            __binit_owned_aliases["$__binit_record_id:$__binit_record_name"]=${BASH_ALIASES[$__binit_record_name]}
    done
    return 0
}

__binit_notify() {
    local __binit_notify_phase=$1 __binit_notify_id=$2 __binit_notify_result=${3-} __binit_notify_name
    local -a __binit_notify_names=()
    if [[ $__binit_notify_phase == before-load ]]; then
        if [[ -n "${__binit_before_callbacks[$__binit_notify_id]-}" ]]; then IFS=',' read -r -a __binit_notify_names <<< "${__binit_before_callbacks[$__binit_notify_id]-}"; else __binit_notify_names=(); fi
    else
        if [[ -n "${__binit_after_callbacks[$__binit_notify_id]-}" ]]; then IFS=',' read -r -a __binit_notify_names <<< "${__binit_after_callbacks[$__binit_notify_id]-}"; else __binit_notify_names=(); fi
    fi
    for __binit_notify_name in "${__binit_notify_names[@]}"; do
        [[ -n $__binit_notify_name ]] || continue
        if ! declare -F -- "$__binit_notify_name" >/dev/null; then
            return 1
        fi
        if [[ $__binit_notify_phase == before-load ]]; then
            "$__binit_notify_name" "$__binit_notify_id" || return 1
        else
            "$__binit_notify_name" "$__binit_notify_id" "$__binit_notify_result" || return 1
        fi
    done
}

__binit_watch_snapshot() {
    local __binit_watch_id=$1 __binit_watch_path __binit_watch_key __binit_watch_new __binit_watch_index=0 __binit_watch_native=0 __binit_watch_command
    local -a __binit_watch_paths=()
    if [[ -n "${__binit_watch[$__binit_watch_id]}" ]]; then IFS=',' read -r -a __binit_watch_paths <<< "${__binit_watch[$__binit_watch_id]}"; else __binit_watch_paths=(); fi
    if [[ $- != *T* ]] && builtin binit_complete effects plain >/dev/null 2>&1; then
        __binit_watch_command=$(builtin type -t stat; builtin type -p stat; builtin type -t command) 2>/dev/null
        case $__binit_watch_command in
            $'file\n/usr/bin/stat\nbuiltin'|$'file\n/bin/stat\nbuiltin') __binit_watch_native=1 ;;
        esac
    fi
    for __binit_watch_path in "${__binit_watch_paths[@]}"; do
        __binit_watch_key="$__binit_watch_id:$__binit_watch_index"
        __binit_watch_index=$((__binit_watch_index + 1))
        if (( __binit_watch_native )) && builtin binit_complete effects watch "$__binit_watch_path" __binit_watch_new 2>/dev/null; then
            :
        else
            __binit_watch_new=$( { { LC_ALL=C command stat -Lc '%d:%i:%s:%y:%z' -- "$__binit_watch_path" 2>/dev/null || printf 'missing'; } 1>&"$__binit_definition_fd"; } {__binit_definition_fd}>&1 >/dev/null )
        fi
        if [[ -v __binit_watch_value[$__binit_watch_key] && ${__binit_watch_value[$__binit_watch_key]} != "$__binit_watch_new" ]]; then
            __binit_watch_dirty[$__binit_watch_id]=1
        fi
        __binit_watch_value[$__binit_watch_key]=$__binit_watch_new
    done
}

__binit_dispatch() {
    local __binit_dispatch_id __binit_dispatch_now=${EPOCHREALTIME/./} __binit_dispatch_due
    __binit_dispatch_pending=0
    for __binit_dispatch_id in "${__binit_ids[@]}"; do
        [[ ${__binit_state[$__binit_dispatch_id]} == pending ]] || continue
        __binit_dispatch_due=0
        case ${__binit_load_kind[$__binit_dispatch_id]} in
            timed) (( __binit_dispatch_now >= ${__binit_deadline[$__binit_dispatch_id]:-9223372036854775807} )) && __binit_dispatch_due=1 ;;
            watch)
                __binit_watch_snapshot "$__binit_dispatch_id"
                [[ ${__binit_watch_dirty[$__binit_dispatch_id]-0} == 1 ]] && __binit_dispatch_due=1
                ;;
            startup) [[ -n ${__binit_when[$__binit_dispatch_id]-} ]] && __binit_dispatch_due=1 ;;
        esac
        if (( __binit_dispatch_due )); then
            __binit_event_changed=1
            if __binit_load "$__binit_dispatch_id"; then
                __binit_watch_dirty[$__binit_dispatch_id]=0
            elif [[ ${__binit_state[$__binit_dispatch_id]} == failed ]]; then
                printf 'binit: %s: %s\n' "$__binit_dispatch_id" "${__binit_reason[$__binit_dispatch_id]}" >&2
            fi
        fi
        if [[ ${__binit_state[$__binit_dispatch_id]} == pending ]]; then
            case ${__binit_load_kind[$__binit_dispatch_id]} in
                timed|watch) __binit_dispatch_pending=1 ;;
                startup) [[ -z ${__binit_when[$__binit_dispatch_id]-} ]] || __binit_dispatch_pending=1 ;;
            esac
        fi
    done
    return 0
}

__binit_event_dispatch() {
    local __binit_event_id __binit_event_pending=0
    __binit_event_changed=0
    __binit_dispatch
    for __binit_event_id in "${__binit_ids[@]}"; do
        if [[ ${__binit_state[$__binit_event_id]} == pending && ${__binit_load_kind[$__binit_event_id]} =~ ^(timed|watch)$ ]]; then
            __binit_event_pending=1
            break
        fi
    done
    if (( ! __binit_event_pending )); then builtin binit_complete poll-off 2>/dev/null || :; fi
}

__binit_unload() {
    local __binit_unload_id=$1 __binit_unload_key __binit_unload_name __binit_unload_current __binit_unload_attrs __binit_unload_other __binit_unload_dep
    local __binit_unload_readonly_re=$'\n''declare -[^ ]*r'
    local -a __binit_unload_dependencies=() __binit_unload_keep=() __binit_unload_names=()
    [[ ${__binit_state[$__binit_unload_id]-} == loaded || ${__binit_state[$__binit_unload_id]-} == failed ]] || return 1
    for __binit_unload_other in "${__binit_ids[@]}"; do
        [[ $__binit_unload_other != "$__binit_unload_id" && ${__binit_state[$__binit_unload_other]} == loaded ]] || continue
        if [[ -n "${__binit_depends[$__binit_unload_other]}" ]]; then IFS=' ' read -r -a __binit_unload_dependencies <<< "${__binit_depends[$__binit_unload_other]}"; else __binit_unload_dependencies=(); fi
        for __binit_unload_dep in "${__binit_unload_dependencies[@]}"; do
            [[ $__binit_unload_dep != "$__binit_unload_id" ]] || { printf 'binit: %s: required by %s\n' "$__binit_unload_id" "$__binit_unload_other" >&2; return 1; }
        done
    done
    if ! builtin binit_complete effects check-functions "$__binit_unload_id" __binit_owned_functions 2>/dev/null; then
        for __binit_unload_key in "${!__binit_owned_functions[@]}"; do
            [[ $__binit_unload_key == "$__binit_unload_id:"* ]] || continue
            __binit_unload_name=${__binit_unload_key#*:}
            __binit_unload_current=$( { builtin declare -f -- "$__binit_unload_name" 1>&"$__binit_definition_fd"; } {__binit_definition_fd}>&1 >/dev/null ) || __binit_unload_current=''
            [[ -z $__binit_unload_current || $__binit_unload_current == "${__binit_owned_functions[$__binit_unload_key]}" ]] || { printf 'binit: %s: function changed: %s\n' "$__binit_unload_id" "$__binit_unload_name" >&2; return 1; }
            __binit_unload_attrs=$( { builtin declare -pf -- "$__binit_unload_name" 1>&"$__binit_definition_fd" 2>/dev/null; } {__binit_definition_fd}>&1 >/dev/null ) || :
            [[ ! $__binit_unload_attrs =~ $__binit_unload_readonly_re ]] || { printf 'binit: %s: readonly function: %s\n' "$__binit_unload_id" "$__binit_unload_name" >&2; return 1; }
        done
    fi
    for __binit_unload_key in "${!__binit_owned_aliases[@]}"; do
        [[ $__binit_unload_key == "$__binit_unload_id:"* ]] || continue
        __binit_unload_name=${__binit_unload_key#*:}
        # A removed alias needs no restore; a changed value is someone else's and blocks unload.
        if [[ -v BASH_ALIASES[$__binit_unload_name] && ${BASH_ALIASES[$__binit_unload_name]} != "${__binit_owned_aliases[$__binit_unload_key]}" ]]; then
            printf 'binit: %s: alias changed: %s\n' "$__binit_unload_id" "$__binit_unload_name" >&2
            return 1
        fi
    done
    for __binit_unload_key in "${!__binit_segment_owner[@]}"; do
        [[ ${__binit_segment_owner[$__binit_unload_key]} == "$__binit_unload_id" ]] || continue
        [[ ${__binit_segment_value[$__binit_unload_key]-} == "${__binit_owned_segment_value[$__binit_unload_key]}" ]] || { printf 'binit: %s: segment changed: %s\n' "$__binit_unload_id" "$__binit_unload_key" >&2; return 1; }
    done
    if [[ -n "${__binit_completions[$__binit_unload_id]}" ]]; then IFS=' ' read -r -a __binit_unload_names <<< "${__binit_completions[$__binit_unload_id]}"; else __binit_unload_names=(); fi
    for __binit_unload_name in "${__binit_unload_names[@]}"; do
        if [[ -v __binit_provider_count[$__binit_unload_name] && ${__binit_provider_owner[$__binit_unload_name]} != "$__binit_unload_id" ]]; then
            printf 'binit: %s: completion changed: %s\n' "$__binit_unload_id" "$__binit_unload_name" >&2
            return 1
        fi
        __binit_unload_current=$( { builtin complete -p -- "$__binit_unload_name" 1>&"$__binit_definition_fd"; } {__binit_definition_fd}>&1 >/dev/null ) || __binit_unload_current=''
        [[ -z $__binit_unload_current || $__binit_unload_current == "${__binit_completion_spec[$__binit_unload_name]-}" ]] || { printf 'binit: %s: completion changed: %s\n' "$__binit_unload_id" "$__binit_unload_name" >&2; return 1; }
    done
    __binit_unload_keep=()
    for __binit_unload_name in "${__binit_segments[@]}"; do
        if [[ ${__binit_segment_owner[$__binit_unload_name]-} == "$__binit_unload_id" ]]; then
            unset '__binit_segment_value[$__binit_unload_name]' '__binit_segment_owner[$__binit_unload_name]' '__binit_owned_segment_value[$__binit_unload_name]'
        else
            __binit_unload_keep+=("$__binit_unload_name")
        fi
    done
    __binit_segments=("${__binit_unload_keep[@]}")
    __binit_unload_keep=()
    for __binit_unload_key in "${!__binit_owned_functions[@]}"; do
        [[ $__binit_unload_key == "$__binit_unload_id:"* ]] || continue
        unset -f -- "${__binit_unload_key#*:}" || return 1
        unset '__binit_owned_functions[$__binit_unload_key]'
    done
    for __binit_unload_key in "${!__binit_owned_aliases[@]}"; do
        [[ $__binit_unload_key == "$__binit_unload_id:"* ]] || continue
        builtin unalias -- "${__binit_unload_key#*:}" 2>/dev/null || :
        unset '__binit_owned_aliases[$__binit_unload_key]'
    done
    for __binit_unload_name in "${__binit_unload_names[@]}"; do
        builtin complete -r -- "$__binit_unload_name" 2>/dev/null || :
        __binit_provider_forget "$__binit_unload_name"
        unset '__binit_completion_spec[$__binit_unload_name]' '__binit_completion_disabled[$__binit_unload_name]'
    done
    for __binit_unload_name in "${__binit_prompt_hooks[@]}"; do
        [[ ${__binit_hook_owner[$__binit_unload_name]-} == "$__binit_unload_id" ]] || __binit_unload_keep+=("$__binit_unload_name")
    done
    __binit_prompt_hooks=("${__binit_unload_keep[@]}")
    __binit_unload_keep=()
    for __binit_unload_name in "${__binit_directory_hooks[@]}"; do
        [[ ${__binit_hook_owner[$__binit_unload_name]-} == "$__binit_unload_id" ]] || __binit_unload_keep+=("$__binit_unload_name")
    done
    __binit_directory_hooks=("${__binit_unload_keep[@]}")
    for __binit_unload_key in "${!__binit_hook_owner[@]}"; do
        [[ ${__binit_hook_owner[$__binit_unload_key]} == "$__binit_unload_id" ]] && unset '__binit_hook_owner[$__binit_unload_key]'
    done
    __binit_state[$__binit_unload_id]=unloaded
    __binit_reason[$__binit_unload_id]='explicitly unloaded; use binit load to reload'
    return 0
}

# A plugin built from a `binit plugin` line stays undeclared until a line in
# this shell has the same canonical declaration.
__binit_declare_line() {
    __binit_line_key[$1]=$2
    __binit_state[$2]=undeclared
    __binit_reason[$2]='no matching binit plugin line in this shell'
}

binit_on() {
    local event=${1-} callback=${2-}
    [[ -n $__binit_loading && -n $callback ]] || return 1
    declare -F -- "$callback" >/dev/null || return 1
    [[ ! -v __binit_hook_owner[$callback] || ${__binit_hook_owner[$callback]} == "$__binit_loading" ]] || return 1
    case $event in
        prompt) __binit_prompt_hooks+=("$callback") ;;
        directory) __binit_directory_hooks+=("$callback") ;;
        *) return 1 ;;
    esac
    __binit_hook_owner[$callback]=$__binit_loading
}

binit_segment() {
    local id=${1-} value=${2-} LC_ALL=C owner=${__binit_loading:-$__binit_callback_owner}
    [[ -n $id ]] || return 1
    if [[ ! -v __binit_segment_value[$id] ]]; then
        __binit_segments+=("$id")
        [[ -z $owner ]] || __binit_segment_owner[$id]=$owner
    fi
    value=${value//[$'\001'-$'\037'$'\177']/}
    __binit_segment_value[$id]=${value:0:80}
    if [[ -n $owner && ${__binit_segment_owner[$id]-} == "$owner" ]]; then
        __binit_owned_segment_value[$id]=${__binit_segment_value[$id]}
    fi
}

__binit_load() {
    local __binit_load_id=$1 __binit_load_requirement __binit_load_dependency
    local __binit_load_before __binit_load_after __binit_load_saved=$__binit_loading __binit_load_active_saved=$__binit_load_active
    local -a __binit_load_dependencies __binit_load_requirements
    local -A __binit_load_functions=() __binit_load_aliases=() __binit_pre_completion=()
    local __binit_load_comp __binit_load_comp_before __binit_load_mark=''
    local -a __binit_load_completions=()
    case ${__binit_state[$__binit_load_id]-missing} in
        loaded) return 0 ;;
        pending) ;;
        *) return 1 ;;
    esac
    __binit_load_active=1
    if [[ -n ${__binit_when[$__binit_load_id]-} ]]; then
        __binit_state[$__binit_load_id]=checking
        if ! declare -F -- "${__binit_when[$__binit_load_id]}" >/dev/null || ! "${__binit_when[$__binit_load_id]}" "$__binit_load_id"; then
            __binit_state[$__binit_load_id]=pending
            __binit_reason[$__binit_load_id]='condition not met'
            __binit_load_active=$__binit_load_active_saved
            return 1
        fi
    fi
    __binit_state[$__binit_load_id]=loading
    if [[ -n ${__binit_commands[$__binit_load_id]} ]] && ! __binit_drop_commands "$__binit_load_id"; then
        __binit_state[$__binit_load_id]=blocked
        __binit_load_active=$__binit_load_active_saved
        return 1
    fi
    if [[ -n "${__binit_depends[$__binit_load_id]}" ]]; then IFS=' ' read -r -a __binit_load_dependencies <<< "${__binit_depends[$__binit_load_id]}"; else __binit_load_dependencies=(); fi
    if [[ -n "${__binit_requires[$__binit_load_id]}" ]]; then IFS=' ' read -r -a __binit_load_requirements <<< "${__binit_requires[$__binit_load_id]}"; else __binit_load_requirements=(); fi
    for __binit_load_dependency in "${__binit_load_dependencies[@]}"; do
        if ! __binit_load "$__binit_load_dependency"; then
            __binit_state[$__binit_load_id]=unavailable
            __binit_reason[$__binit_load_id]="dependency unavailable: $__binit_load_dependency"
            __binit_load_active=$__binit_load_active_saved
            return 1
        fi
    done
    for __binit_load_requirement in "${__binit_load_requirements[@]}"; do
        if ! command -v -- "$__binit_load_requirement" >/dev/null; then
            __binit_state[$__binit_load_id]=unavailable
            __binit_reason[$__binit_load_id]="missing command: $__binit_load_requirement"
            __binit_load_active=$__binit_load_active_saved
            return 1
        fi
    done
    if [[ -n ${__binit_before_callbacks[$__binit_load_id]-} ]] && ! __binit_notify before-load "$__binit_load_id"; then
        __binit_state[$__binit_load_id]=failed
        __binit_reason[$__binit_load_id]='before-load callback failed'
        __binit_notify after-load "$__binit_load_id" failed || :
        __binit_load_active=$__binit_load_active_saved
        return 1
    fi
    # A native entry snapshot replaces the two name arrays when no trap or function tracing can observe it.
    if [[ $- == *T* ]] || ! builtin binit_complete effects mark __binit_load_mark 2>/dev/null; then
        __binit_load_mark=''
        __binit_capture_names __binit_load_functions function
        __binit_capture_names __binit_load_aliases alias
    fi
    if [[ -n "${__binit_completions[$__binit_load_id]}" ]]; then IFS=' ' read -r -a __binit_load_completions <<< "${__binit_completions[$__binit_load_id]}"; else __binit_load_completions=(); fi
    for __binit_load_comp in "${__binit_load_completions[@]}"; do
        __binit_load_comp_before=$( { builtin complete -p -- "$__binit_load_comp" 1>&"$__binit_definition_fd"; } {__binit_definition_fd}>&1 >/dev/null ) || __binit_load_comp_before=''
        if [[ -n $__binit_load_comp_before && $__binit_load_comp_before != "${__binit_completion_spec[$__binit_load_comp]-}" ]]; then
            __binit_pre_completion[$__binit_load_comp]=foreign
        else
            __binit_pre_completion[$__binit_load_comp]=safe
        fi
    done
    __binit_load_before=${EPOCHREALTIME/./}
    __binit_loading=$__binit_load_id
    # Sourced plugins are trusted code. A failed load can have partial effects.
    # shellcheck source=/dev/null
    if source "${__binit_entry[$__binit_load_id]}"; then __binit_state[$__binit_load_id]=loaded
    else
        __binit_reason[$__binit_load_id]="entry returned $?"
        __binit_state[$__binit_load_id]=failed
    fi
    # A successful native record also releases the entry snapshot.
    if [[ -z $__binit_load_mark ]] ||
       ! builtin binit_complete effects record-since "$__binit_load_mark" "$__binit_load_id" __binit_owned_functions __binit_owned_aliases 2>/dev/null; then
        __binit_record_effects "$__binit_load_id" __binit_load_functions __binit_load_aliases "$__binit_load_mark"
    fi
    [[ -z ${__binit_completions[$__binit_load_id]} ]] || __binit_capture_plugin_completions "$__binit_load_id"
    __binit_loading=$__binit_load_saved
    if [[ -n ${__binit_after_callbacks[$__binit_load_id]-} ]] && ! __binit_notify after-load "$__binit_load_id" "${__binit_state[$__binit_load_id]}"; then
        __binit_state[$__binit_load_id]=failed
        __binit_reason[$__binit_load_id]='after-load callback failed'
    fi
    __binit_load_after=${EPOCHREALTIME/./}
    __binit_load_us[$__binit_load_id]=$((__binit_load_after - __binit_load_before))
    __binit_load_active=$__binit_load_active_saved
    [[ ${__binit_state[$__binit_load_id]} == loaded ]]
}

__binit_drop_commands() {
    local __binit_drop_id=$1 __binit_drop_name __binit_drop_definition
    local -a __binit_drop_names __binit_drop_owned=()
    if [[ -n "${__binit_commands[$__binit_drop_id]}" ]]; then IFS=' ' read -r -a __binit_drop_names <<< "${__binit_commands[$__binit_drop_id]}"; else __binit_drop_names=(); fi
    for __binit_drop_name in "${__binit_drop_names[@]}"; do
        # Without traps or function tracing no trap code can observe the serializer, so the
        # calling-shell copy replaces the subshell; any other context keeps the subshell.
        if [[ $- == *T* ]] || ! builtin binit_complete effects plain 2>/dev/null ||
           ! builtin binit_complete effects function "$__binit_drop_name" __binit_drop_definition 2>/dev/null; then
            __binit_drop_definition=$( { builtin declare -f -- "$__binit_drop_name" 1>&"$__binit_definition_fd"; } {__binit_definition_fd}>&1 >/dev/null )
        fi
        if builtin alias -- "$__binit_drop_name" &>/dev/null ||
           [[ -n $__binit_drop_definition && $__binit_drop_definition != "${__binit_stub[$__binit_drop_name]-}" ]]; then
            __binit_reason[$__binit_drop_id]="command already defined: $__binit_drop_name"
            return 1
        fi
        if [[ -n $__binit_drop_definition && -v __binit_stub[$__binit_drop_name] ]]; then
            __binit_drop_owned+=("$__binit_drop_name")
        fi
    done
    for __binit_drop_name in "${__binit_drop_owned[@]}"; do
        if ! unset -f -- "$__binit_drop_name"; then
            __binit_reason[$__binit_drop_id]="readonly command: $__binit_drop_name"
            return 1
        fi
    done
    for __binit_drop_name in "${__binit_drop_names[@]}"; do
        unset '__binit_stub[$__binit_drop_name]'
    done
}

__binit_invoke() {
    local __binit_invoke_name=$1 __binit_invoke_id=${__binit_command_owner[$1]}
    shift
    if ! __binit_load "$__binit_invoke_id"; then
        printf 'binit: %s: %s\n' "$__binit_invoke_id" "${__binit_reason[$__binit_invoke_id]:-${__binit_state[$__binit_invoke_id]}}" >&2
        return 1
    fi
    "$__binit_invoke_name" "$@"
}

__binit_command_conflict() {
    builtin alias -- "$1" &>/dev/null && return 0
    declare -F -- "$1" >/dev/null && return 0
    case $( { builtin type -t -- "$1" 1>&"$__binit_definition_fd"; } {__binit_definition_fd}>&1 >/dev/null ) in builtin|keyword) return 0 ;; esac
    return 1
}

__binit_call() {
    local __binit_call_callback=$1 __binit_call_owner=${__binit_hook_owner[$1]} __binit_call_before __binit_call_after __binit_call_saved=$__binit_callback_owner
    [[ ${__binit_state[$__binit_call_owner]} == loaded ]] || return 0
    __binit_call_before=${EPOCHREALTIME/./}
    __binit_callback_owner=$__binit_call_owner
    if "$__binit_call_callback" "$BINIT_LAST_STATUS" "$PWD"; then :; else :; fi
    __binit_callback_owner=$__binit_call_saved
    __binit_call_after=${EPOCHREALTIME/./}
    __binit_hook_us[$__binit_call_owner]=$(( ${__binit_hook_us[$__binit_call_owner]} + __binit_call_after - __binit_call_before ))
    __binit_hook_calls[$__binit_call_owner]=$(( ${__binit_hook_calls[$__binit_call_owner]} + 1 ))
}

__binit_prompt_dispatch() {
    local __binit_prompt_dispatch_status=$? __binit_prompt_dispatch_id __binit_prompt_dispatch_callback __binit_prompt_dispatch_value
    BINIT_LAST_STATUS=$__binit_prompt_dispatch_status
    # No load runs while the prompt is prepared, so a load still marked here
    # was interrupted (for example by Ctrl-C) and may have partial effects.
    if (( __binit_load_active )); then
        for __binit_prompt_dispatch_id in "${__binit_ids[@]}"; do
            if [[ ${__binit_state[$__binit_prompt_dispatch_id]} == loading || ${__binit_state[$__binit_prompt_dispatch_id]} == checking ]]; then
                __binit_state[$__binit_prompt_dispatch_id]=failed
                __binit_reason[$__binit_prompt_dispatch_id]='load interrupted'
            fi
        done
        __binit_loading=''
        __binit_load_active=0
        # No load is running at the prompt, so every native entry snapshot left by the interrupted one is stale.
        builtin binit_complete effects release 1 2>/dev/null || :
    fi
    if (( __binit_first_prompt )); then
        __binit_first_prompt=0
        __binit_first_prompt_work
    fi
    if (( __binit_dispatch_pending )); then __binit_event_dispatch; fi
    for __binit_prompt_dispatch_id in "${__binit_ids[@]}"; do
        if [[ ${__binit_load_kind[$__binit_prompt_dispatch_id]} == prompt && ${__binit_state[$__binit_prompt_dispatch_id]} == pending ]]; then
            if __binit_load "$__binit_prompt_dispatch_id"; then :; fi
        fi
    done
    __binit_wait_arm
    if [[ $__binit_last_directory != "$PWD" ]]; then
        __binit_last_directory=$PWD
        for __binit_prompt_dispatch_callback in "${__binit_directory_hooks[@]}"; do __binit_call "$__binit_prompt_dispatch_callback"; done
    fi
    for __binit_prompt_dispatch_callback in "${__binit_prompt_hooks[@]}"; do __binit_call "$__binit_prompt_dispatch_callback"; done
    BINIT_PROMPT_TEXT=''
    for __binit_prompt_dispatch_id in "${__binit_segments[@]}"; do
        __binit_prompt_dispatch_value=${__binit_segment_value[$__binit_prompt_dispatch_id]}
        if [[ -n $__binit_prompt_dispatch_value ]]; then BINIT_PROMPT_TEXT+="[$__binit_prompt_dispatch_value] "; fi
    done
    return "$__binit_prompt_dispatch_status"
}

# Loads every pending wait plugin in line order. The Readline hook of the
# native module runs it once after the prompt is drawn.
__binit_wait_dispatch() {
    local __binit_wait_id
    local -a __binit_wait_run=("${__binit_wait_queue[@]}")
    __binit_wait_pending=0
    __binit_wait_queue=()
    for __binit_wait_id in "${__binit_wait_run[@]}"; do
        if [[ ${__binit_state[$__binit_wait_id]} == pending ]]; then
            if __binit_load "$__binit_wait_id"; then :; fi
            if [[ ${__binit_state[$__binit_wait_id]} == pending && -n ${__binit_when[$__binit_wait_id]-} ]]; then
                __binit_wait_pending=1
                __binit_wait_queue+=("$__binit_wait_id")
            fi
        fi
    done
}

# Leaves pending wait plugins to the hook for one prompt. If the hook is
# unavailable, or did not run for the previous prompt, they load now.
__binit_wait_arm() {
    if (( ! __binit_wait_pending )); then
        __binit_wait_armed=0
    elif (( __binit_wait_armed )) || ! __binit_has_completion || ! builtin binit_complete wait 2>/dev/null; then
        __binit_wait_armed=0
        __binit_wait_dispatch
    else
        __binit_wait_armed=1
    fi
}

__binit_complete() {
    local __binit_complete_name=${COMP_WORDS[0]-} __binit_complete_id
    [[ -n $__binit_complete_name && -v __binit_completion_owner[$__binit_complete_name] ]] || return 1
    __binit_complete_id=${__binit_completion_owner[$__binit_complete_name]}
    if __binit_load "$__binit_complete_id"; then
        # Bash retries completion using the specification installed by the plugin.
        return 124
    fi
    [[ ${__binit_state[$__binit_complete_id]} == pending ]] || complete -r -- "$__binit_complete_name"
    return 1
}

__binit_has_completion() {
    builtin binit_complete status >/dev/null 2>&1
}

__binit_load_completion() {
    if __binit_has_completion; then return 0; fi
    [[ -f $__binit_generation_dir/completion.so ]] || return 1
    bind -q complete >/dev/null 2>&1 || :
    builtin enable -f "$__binit_generation_dir/completion.so" binit_complete
}

__binit_provider_forget() {
    local __binit_provider_name=$1 __binit_provider_index
    for ((__binit_provider_index=0; __binit_provider_index<${__binit_provider_count[$__binit_provider_name]:-0}; __binit_provider_index++)); do
        unset '__binit_provider_args[$__binit_provider_name:$__binit_provider_index]'
    done
    unset '__binit_provider_count[$__binit_provider_name]' '__binit_provider_owner[$__binit_provider_name]' '__binit_provider_spec[$__binit_provider_name]'
}

__binit_provider_complete() {
    local __binit_provider_name=${1##*/} __binit_provider_index
    local -a __binit_provider_argv=()
    COMPREPLY=()
    [[ $__binit_provider_name =~ ^[a-zA-Z][a-zA-Z0-9_-]{0,63}$ && -v __binit_provider_count[$__binit_provider_name] ]] || return 1
    for ((__binit_provider_index=0; __binit_provider_index<${__binit_provider_count[$__binit_provider_name]}; __binit_provider_index++)); do
        __binit_provider_argv+=("${__binit_provider_args[$__binit_provider_name:$__binit_provider_index]}")
    done
    builtin binit_complete request "$__binit_manager" "${__binit_provider_argv[@]}"
}

__binit_provider_register() {
    if (( $# < 2 || $# > 4096 )) || [[ ! $1 =~ ^[a-zA-Z][a-zA-Z0-9_-]{0,63}$ ]]; then
        printf 'usage: binit completions register NAME PROGRAM [ARG...]\n' >&2
        return 2
    fi
    local __binit_provider_name=$1 __binit_provider_current __binit_provider_index=0 __binit_provider_bytes=0 __binit_provider_value
    local __binit_provider_plugin=${__binit_loading:-$__binit_callback_owner} LC_ALL=C
    shift
    local -a __binit_provider_argv=("$@")
    if [[ ${__binit_provider_argv[0]} != /* ]]; then
        __binit_provider_argv[0]=$( { builtin type -P -- "${__binit_provider_argv[0]}" 1>&"$__binit_definition_fd"; } {__binit_definition_fd}>&1 >/dev/null ) || return 1
        [[ ${__binit_provider_argv[0]} == /* ]] || __binit_provider_argv[0]=$PWD/${__binit_provider_argv[0]}
    fi
    if [[ ${__binit_provider_argv[0]} != /* || ! -f ${__binit_provider_argv[0]} || ! -x ${__binit_provider_argv[0]} ]]; then
        printf 'binit: completion program unavailable\n' >&2
        return 1
    fi
    for __binit_provider_value in "${__binit_provider_argv[@]}"; do
        __binit_provider_bytes=$((__binit_provider_bytes + ${#__binit_provider_value} + 1))
        if (( __binit_provider_bytes > 65536 )); then printf 'binit: completion arguments exceed 64 KiB\n' >&2; return 2; fi
    done
    if [[ -n $__binit_provider_plugin && ${__binit_completion_owner[$__binit_provider_name]-} != "$__binit_provider_plugin" ]]; then
        printf 'binit: declare completion in plugin manifest: %s\n' "$__binit_provider_name" >&2
        return 1
    fi
    __binit_provider_current=$( { builtin complete -p -- "$__binit_provider_name" 1>&"$__binit_definition_fd"; } {__binit_definition_fd}>&1 >/dev/null ) || __binit_provider_current=''
    if [[ -v __binit_provider_count[$__binit_provider_name] ]]; then
        if [[ ${__binit_provider_owner[$__binit_provider_name]} != "$__binit_provider_plugin" || ( -n $__binit_provider_current && $__binit_provider_current != "${__binit_provider_spec[$__binit_provider_name]}" ) ]]; then
            printf 'binit: completion changed: %s\n' "$__binit_provider_name" >&2
            return 1
        fi
    elif [[ -n $__binit_provider_current && ( -z $__binit_provider_plugin || $__binit_provider_current != "${__binit_completion_spec[$__binit_provider_name]-}" ) ]]; then
        printf 'binit: completion already defined: %s\n' "$__binit_provider_name" >&2
        return 1
    fi
    if ! __binit_load_completion; then printf 'binit: isolated completion unavailable in this shell\n' >&2; return 1; fi
    builtin complete -o filenames -F __binit_provider_complete -- "$__binit_provider_name" || return 1
    __binit_provider_forget "$__binit_provider_name"
    __binit_provider_count[$__binit_provider_name]=${#__binit_provider_argv[@]}
    __binit_provider_owner[$__binit_provider_name]=$__binit_provider_plugin
    __binit_provider_spec[$__binit_provider_name]=$( { builtin complete -p -- "$__binit_provider_name" 1>&"$__binit_definition_fd"; } {__binit_definition_fd}>&1 >/dev/null )
    for __binit_provider_value in "${__binit_provider_argv[@]}"; do
        __binit_provider_args[$__binit_provider_name:$__binit_provider_index]=$__binit_provider_value
        __binit_provider_index=$((__binit_provider_index + 1))
    done
    if [[ -n $__binit_provider_plugin ]]; then
        __binit_completion_spec[$__binit_provider_name]=${__binit_provider_spec[$__binit_provider_name]}
    fi
    __binit_completion_disabled[$__binit_provider_name]=0
}

__binit_provider_unregister() {
    if (( $# != 1 )) || [[ ! $1 =~ ^[a-zA-Z][a-zA-Z0-9_-]{0,63}$ || ! -v __binit_provider_count[$1] ]]; then
        printf 'usage: binit completions unregister NAME\n' >&2
        return 2
    fi
    local __binit_provider_name=$1 __binit_provider_current
    __binit_provider_current=$( { builtin complete -p -- "$__binit_provider_name" 1>&"$__binit_definition_fd"; } {__binit_definition_fd}>&1 >/dev/null ) || __binit_provider_current=''
    if [[ -n $__binit_provider_current && $__binit_provider_current != "${__binit_provider_spec[$__binit_provider_name]}" ]]; then
        printf 'binit: completion changed: %s\n' "$__binit_provider_name" >&2
        return 1
    fi
    [[ -z $__binit_provider_current ]] || builtin complete -r -- "$__binit_provider_name" || return 1
    if [[ -n ${__binit_provider_owner[$__binit_provider_name]} ]]; then unset '__binit_completion_spec[$__binit_provider_name]'; fi
    __binit_provider_forget "$__binit_provider_name"
    unset '__binit_completion_disabled[$__binit_provider_name]'
}

binit() {
    if [[ ${1-} == profile ]]; then
        local id
        printf '%-20s %-12s %10s %10s %8s  %s\n' PLUGIN STATE LOAD_US HOOK_US CALLS REASON
        for id in "${__binit_ids[@]}"; do
            printf '%-20s %-12s %10s %10s %8s  %s\n' "$id" "${__binit_state[$id]}" "${__binit_load_us[$id]}" "${__binit_hook_us[$id]}" "${__binit_hook_calls[$id]}" "${__binit_reason[$id]}"
        done
    elif [[ ${1-} == on || ${1-} == off ]]; then
        if (( $# != 4 )) || [[ ! $2 =~ ^(before-load|after-load)$ || ! $3 =~ ^[a-zA-Z][a-zA-Z0-9_-]{0,63}$ || ! -v __binit_state[$3] || ! $4 =~ ^[a-zA-Z_][a-zA-Z0-9_-]{0,63}$ ]] || ! declare -F -- "$4" >/dev/null; then
            printf 'usage: binit on|off before-load|after-load ID FUNCTION\n' >&2
            return 2
        fi
        local __binit_callbacks_array=__binit_before_callbacks
        [[ $2 == after-load ]] && __binit_callbacks_array=__binit_after_callbacks
        local -n __binit_callbacks=$__binit_callbacks_array
        local __binit_callbacks_old=${__binit_callbacks[$3]-} __binit_callback_name
        local -a __binit_callback_names=() __binit_callback_keep=()
        if [[ -n "$__binit_callbacks_old" ]]; then IFS=',' read -r -a __binit_callback_names <<< "$__binit_callbacks_old"; else __binit_callback_names=(); fi
        for __binit_callback_name in "${__binit_callback_names[@]}"; do
            [[ -n $__binit_callback_name && $__binit_callback_name != "$4" ]] && __binit_callback_keep+=("$__binit_callback_name")
        done
        [[ $1 == off ]] || __binit_callback_keep+=("$4")
        local IFS=,
        __binit_callbacks[$3]=${__binit_callback_keep[*]}
    elif [[ ${1-} == emit ]]; then
        if (( $# != 2 )) || [[ ! $2 =~ ^[a-zA-Z][a-zA-Z0-9_-]{0,63}$ ]]; then printf 'usage: binit emit EVENT\n' >&2; return 2; fi
        local __binit_emit_id
        for __binit_emit_id in "${__binit_ids[@]}"; do
            if [[ ${__binit_state[$__binit_emit_id]} == pending && ${__binit_load_kind[$__binit_emit_id]} == event && ${__binit_event[$__binit_emit_id]} == "$2" ]]; then
                __binit_load "$__binit_emit_id" || :
            fi
        done
    elif [[ ${1-} == dispatch ]]; then
        (( $# == 1 )) || { printf 'usage: binit dispatch\n' >&2; return 2; }
        __binit_dispatch
    elif [[ ${1-} == report ]]; then
        if (( $# != 2 )) || [[ ! $2 =~ ^[a-zA-Z][a-zA-Z0-9_-]{0,63}$ || ! -v __binit_state[$2] ]]; then printf 'usage: binit report ID\n' >&2; return 2; fi
        local __binit_report_key
        printf 'plugin %s\nstate %s\n' "$2" "${__binit_state[$2]}"
        for __binit_report_key in "${!__binit_owned_functions[@]}"; do [[ $__binit_report_key == "$2:"* ]] && printf 'function %s\n' "${__binit_report_key#*:}"; done
        for __binit_report_key in "${!__binit_owned_aliases[@]}"; do [[ $__binit_report_key == "$2:"* ]] && printf 'alias %s\n' "${__binit_report_key#*:}"; done
        for __binit_report_key in "${!__binit_segment_owner[@]}"; do [[ ${__binit_segment_owner[$__binit_report_key]} == "$2" ]] && printf 'segment %s\n' "$__binit_report_key"; done
        for __binit_report_key in "${!__binit_hook_owner[@]}"; do [[ ${__binit_hook_owner[$__binit_report_key]} == "$2" ]] && printf 'hook %s\n' "$__binit_report_key"; done
        for __binit_report_key in "${!__binit_completion_owner[@]}"; do [[ ${__binit_completion_owner[$__binit_report_key]} == "$2" && -v __binit_completion_spec[$__binit_report_key] ]] && printf 'completion %s\n' "$__binit_report_key"; done
        return 0
    elif [[ ${1-} == unload ]]; then
        if (( $# != 2 )) || [[ ! $2 =~ ^[a-zA-Z][a-zA-Z0-9_-]{0,63}$ || ! -v __binit_state[$2] ]]; then printf 'usage: binit unload ID\n' >&2; return 2; fi
        __binit_unload "$2"
    elif [[ ${1-} == completions ]]; then
        local __binit_admin_name __binit_admin_current __binit_admin_spec __binit_admin_action=${2-list}
        if [[ $__binit_admin_action == register ]]; then __binit_provider_register "${@:3}"; return "$?"; fi
        if [[ $__binit_admin_action == unregister ]]; then __binit_provider_unregister "${@:3}"; return "$?"; fi
        if [[ $__binit_admin_action == list && $# -le 2 ]]; then
            for __binit_admin_name in "${!__binit_completion_owner[@]}"; do
                printf '%s %s %s\n' "$__binit_admin_name" "${__binit_completion_owner[$__binit_admin_name]}" "$([[ ${__binit_completion_disabled[$__binit_admin_name]-0} == 1 ]] && printf disabled || printf enabled)"
            done
            for __binit_admin_name in "${!__binit_provider_count[@]}"; do
                [[ -v __binit_completion_owner[$__binit_admin_name] ]] && continue
                printf '%s - %s\n' "$__binit_admin_name" "$([[ ${__binit_completion_disabled[$__binit_admin_name]-0} == 1 ]] && printf disabled || printf enabled)"
            done
            return 0
        fi
        if (( $# != 3 )) || [[ ! $__binit_admin_action =~ ^(enable|disable)$ || ! $3 =~ ^[a-zA-Z][a-zA-Z0-9_-]{0,63}$ || ( ! -v __binit_completion_owner[$3] && ! -v __binit_provider_count[$3] ) ]]; then printf 'usage: binit completions list|enable|disable|register|unregister [NAME] [PROGRAM ARG...]\n' >&2; return 2; fi
        __binit_admin_name=$3
        __binit_admin_spec=${__binit_provider_spec[$__binit_admin_name]-${__binit_completion_spec[$__binit_admin_name]-}}
        __binit_admin_current=$( { builtin complete -p -- "$__binit_admin_name" 1>&"$__binit_definition_fd"; } {__binit_definition_fd}>&1 >/dev/null ) || __binit_admin_current=''
        if [[ $__binit_admin_action == disable ]]; then
            if [[ -n $__binit_admin_current && $__binit_admin_current != "$__binit_admin_spec" ]]; then printf 'binit: completion changed: %s\n' "$__binit_admin_name" >&2; return 1; fi
            [[ -z $__binit_admin_current ]] || builtin complete -r -- "$__binit_admin_name" || return 1
            __binit_completion_disabled[$__binit_admin_name]=1
        else
            if [[ -n $__binit_admin_current ]]; then
                [[ $__binit_admin_current == "$__binit_admin_spec" ]] || { printf 'binit: completion changed: %s\n' "$__binit_admin_name" >&2; return 1; }
            elif [[ -n $__binit_admin_spec ]]; then
                builtin eval -- "builtin $__binit_admin_spec" || return 1
            else
                printf 'binit: no owned completion: %s\n' "$__binit_admin_name" >&2; return 1
            fi
            __binit_completion_disabled[$__binit_admin_name]=0
        fi
    elif [[ ${1-} == load ]]; then
        if (( $# != 2 )) || [[ ! $2 =~ ^[a-zA-Z][a-zA-Z0-9_-]{0,63}$ || ! -v __binit_state[$2] ]]; then
            printf 'usage: binit load ID\n' >&2
            return 2
        fi
        if [[ ${__binit_state[$2]} == unloaded ]]; then __binit_state[$2]=pending; fi
        if ! __binit_load "$2"; then
            printf 'binit: %s: %s\n' "$2" "${__binit_reason[$2]:-${__binit_state[$2]}}" >&2
            return 1
        fi
    elif [[ ${1-} == editor ]]; then
        if (( $# > 2 )) || [[ ${2-status} != status ]]; then
            printf 'usage: binit editor [status]\n' >&2
            return 2
        fi
        if __binit_has_completion; then
            builtin binit_complete editor status
        else
            printf 'binit: native editor unavailable in this shell\n' >&2
            return 1
        fi
    elif [[ ${1-} == completion ]]; then
        if (( $# > 2 )) || [[ ! ${2-status} =~ ^(on|off|status)$ ]]; then
            printf 'usage: binit completion [on|off|status]\n' >&2
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
            printf 'binit: completion menu unavailable in this shell\n' >&2
            return 1
        fi
    elif [[ ${1-} == run ]]; then
        command "$__binit_manager" "$@"
    elif [[ ${1-} == plugin ]]; then
        __binit_plugin_line "${@:2}"
    elif [[ ${1-} == sync ]]; then
        if (( $# != 1 )) && { (( $# != 3 )) || [[ $2 != --jobs ]]; }; then
            printf 'usage: binit sync [--jobs N]\n' >&2
            return 2
        fi
        __binit_write_declared || return
        command "$__binit_manager" sync "${@:2}" --state "$BINIT_STATE_DIR"
    else
        if command "$__binit_manager" "$@" --state "$BINIT_STATE_DIR"; then
            if [[ ${1-} == trust ]]; then __binit_last_directory=''; fi
        else
            return "$?"
        fi
    fi
}

# Records this shell's plugin lines as records of an argument count and that
# many arguments, each ended by NUL.
__binit_declared_stream() {
    local __binit_stream_count __binit_stream_index=0 __binit_stream_n
    for __binit_stream_count in "${__binit_line_counts[@]}"; do
        printf '%s\0' "$__binit_stream_count"
        for (( __binit_stream_n = 0; __binit_stream_n < __binit_stream_count; __binit_stream_n++ )); do
            printf '%s\0' "${__binit_line_args[__binit_stream_index++]}"
        done
    done
}

__binit_write_declared() {
    command "$__binit_manager" declare --state "$BINIT_STATE_DIR" < <(__binit_declared_stream)
}

# Sets __binit_line_text to the arguments prefixed with their byte lengths.
__binit_line_text_of() {
    local LC_ALL=C __binit_text_arg
    __binit_line_text=''
    for __binit_text_arg in "$@"; do __binit_line_text+="${#__binit_text_arg}:$__binit_text_arg"; done
}

# True for an absolute path or a Git source. The rules match parse_source in
# src/git.rs; the manager repeats the check at sync.
__binit_source_ok() {
    local LC_ALL=C
    local source=$1 scheme rest authority host userinfo left path user owner repo
    local word_re='^[A-Za-z0-9_.-]+$' user_re='^[a-z_][a-z0-9_-]{0,31}$'
    [[ $source == /* ]] && return 0
    [[ $source != -* && $source != *::* && $source != *[!'!'-'~']* ]] || return 1
    if [[ $source == release:* ]]; then
        rest=${source#release:}
        owner=${rest%%/*}; repo=${rest#*/}
        [[ $rest == */* && $repo != */* && $owner =~ $word_re && $repo =~ $word_re && $owner != . && $owner != .. && $repo != . && $repo != .. ]] && return 0
        return 1
    fi
    if [[ $source == snippet:https://* ]]; then
        rest=${source#snippet:https://}
        authority=${rest%%[/?#]*}
        [[ -n $authority && $authority != *@* && $authority != -* && $rest != *'#'* && $rest != *'\'* ]] && return 0
        return 1
    fi
    if [[ $source == *://* ]]; then
        scheme=${source%%://*}
        rest=${source#*://}
        authority=${rest%%/*}
        case $scheme in
            file) [[ -z $authority && ${#rest} -gt 1 ]] && return 0 ;;
            https|ssh)
                host=${authority##*@}
                if [[ $authority == *@* ]]; then
                    userinfo=${authority%@*}
                    [[ $userinfo != *:* && $scheme == ssh && $userinfo =~ $user_re ]] || return 1
                fi
                [[ -n $host && $host != -* && ${#rest} -gt $((${#authority} + 1)) ]] && return 0
                ;;
        esac
        return 1
    fi
    if [[ $source == *:* ]]; then
        left=${source%%:*}
        path=${source#*:}
        if [[ $left == *@* ]]; then
            user=${left%%@*}
            host=${left#*@}
            [[ $user =~ $user_re && $host =~ $word_re && $host != [.-]* && -n $path ]] && return 0
        fi
    fi
    if [[ $source == */* ]]; then
        owner=${source%%/*}
        repo=${source#*/}
        [[ $owner =~ $word_re && $owner != [.-]* && $repo =~ $word_re && $repo != . && $repo != .. ]] && return 0
    fi
    return 1
}

# True for a rejected source that may hold a secret: an @ before its first /
# (anywhere without a /), also after a scheme: and slashes. Matches src/git.rs.
__binit_source_secret() {
    local LC_ALL=C source=$1 rest
    [[ ${source%%/*} == *@* ]] && return 0
    if [[ $source =~ ^[A-Za-z][A-Za-z0-9+.-]*: ]]; then
        rest=${source#*:}
        while [[ $rest == /* ]]; do rest=${rest#/}; done
        [[ ${rest%%/*} == *@* ]] && return 0
    fi
    return 1
}

# binit plugin SOURCE [KEY=VALUE...]: declare a local directory or Git plugin.
__binit_plugin_line() {
    local __binit_plugin_arg __binit_plugin_key __binit_plugin_id
    local -A __binit_plugin_keys=()
    if (( $# == 0 )); then
        printf 'usage: binit plugin SOURCE [id=ID] [files=A,B] [load=startup|prompt|wait|command|completion]\n' >&2
        printf '                          [commands=A,B] [completions=A,B] [requires=A,B] [depends=A,B]\n                          [ver=TAG|BRANCH|COMMIT] [asset=NAME] [sha256=HEX] [format=raw|tar|tar.gz|zip]\n                          [atclone=SCRIPT] [atpull=SCRIPT]\n' >&2
        return 2
    fi
    if ! __binit_source_ok "$1"; then
        if __binit_source_secret "$1"; then
            printf 'binit: plugin line rejected: a source with credentials is not allowed; use a Git credential helper or an SSH key\n' >&2
            return 2
        fi
        printf 'binit: plugin %s: the source must be an absolute path, user/repo, https://, ssh://, file:// or user@host:path\n' "$1" >&2
        return 2
    fi
    for __binit_plugin_arg in "${@:2}"; do
        case ${__binit_plugin_arg%%=*} in
            id|files|bin|load|commands|completions|requires|depends|ver|sha256|asset|format|atclone|atpull|delay_ms|event|when|watch) ;;
            *)
                if [[ $__binit_plugin_arg == *=* ]]; then
                    printf 'binit: plugin %s: unknown key: %s\n' "$1" "${__binit_plugin_arg%%=*}" >&2
                else
                    printf 'binit: plugin %s: expected key=value, found: %s\n' "$1" "$__binit_plugin_arg" >&2
                fi
                return 2
                ;;
        esac
        if [[ $__binit_plugin_arg != *=* ]]; then
            printf 'binit: plugin %s: expected key=value, found: %s\n' "$1" "$__binit_plugin_arg" >&2
            return 2
        fi
        if [[ -n ${__binit_plugin_keys[${__binit_plugin_arg%%=*}]+set} ]]; then
            printf 'binit: plugin %s: duplicate key: %s\n' "$1" "${__binit_plugin_arg%%=*}" >&2
            return 2
        fi
        __binit_plugin_keys[${__binit_plugin_arg%%=*}]=${__binit_plugin_arg#*=}
    done
    if [[ $1 == /* && -n ${__binit_plugin_keys[ver]+set} ]]; then
        printf 'binit: plugin %s: ver applies to Git sources only\n' "$1" >&2
        return 2
    fi
    case ${__binit_plugin_keys[load]-startup} in
        startup|prompt|wait|command|completion|timed|event|watch) ;;
        *)
            printf 'binit: plugin %s: load must be startup, prompt, wait, command, completion, timed, event or watch\n' "$1" >&2
            return 2
            ;;
    esac
    __binit_line_text_of "$@"
    __binit_plugin_key=$__binit_line_text
    # Sourcing the same startup file again repeats the same lines.
    [[ -z ${__binit_line_seen[$__binit_plugin_key]+set} ]] || return 0
    __binit_line_seen[$__binit_plugin_key]=1
    __binit_line_counts+=("$#")
    __binit_line_args+=("$@")
    __binit_lines_total=$((__binit_lines_total + 1))
    [[ -n ${__binit_line_key[$__binit_plugin_key]+set} ]] || return 0
    __binit_plugin_id=${__binit_line_key[$__binit_plugin_key]}
    __binit_lines_matched=$((__binit_lines_matched + 1))
    __binit_state[$__binit_plugin_id]=pending
    __binit_reason[$__binit_plugin_id]=''
    __binit_activate_plugins "$__binit_plugin_id"
}

# Runs once at the first prompt, after the startup file finished.
__binit_first_prompt_work() {
    local __binit_first_id __binit_first_changed=$((__binit_lines_total - __binit_lines_matched)) __binit_first_missing=0
    # Expand segment data through a variable, never as executable prompt source.
    if shopt -q promptvars; then PS1='${BINIT_PROMPT_TEXT}'"${PS1-\\s-\\v\\$ }"; fi
    for __binit_first_id in "${__binit_line_key[@]}"; do
        if [[ ${__binit_state[$__binit_first_id]} == undeclared ]]; then
            __binit_first_missing=$((__binit_first_missing + 1))
        fi
    done
    # An edited line is one change: it leaves one line and one plugin unmatched.
    if (( __binit_first_missing > __binit_first_changed )); then __binit_first_changed=$__binit_first_missing; fi
    (( __binit_first_changed > 0 )) || return 0
    printf 'binit: %d plugin line%s changed since the last sync\n' \
        "$__binit_first_changed" "$( ((__binit_first_changed == 1)) || printf s )" >&2
    if [[ ${__binit_has_bin-0} == 1 ]] && (( __binit_first_missing > 0 )); then
        printf 'binit: commands of a removed line stay on PATH until binit sync\n' >&2
    fi
    # An empty list could come from a shell that never reads the startup file.
    if (( __binit_lines_total > 0 )); then __binit_write_declared || :; fi
}

# Activates the given plugins together: conflicts are checked before any
# startup dependency can load, then triggers are installed.
# Loads a pending startup plugin that has no condition, command, dependency, requirement, completion or
# callback, with only its source and the native effect record. Each plugin gets this function's frame, as
# under __binit_load, so a top-level declare or local in the plugin stays local to its load; the locals
# keep __binit_load's names. Returns 2, having changed nothing, when the general load must run instead:
# the plugin needs more, or a trap or function tracing can observe the shell (mark then fails).
__binit_load_direct() {
    # Each simple command costs about as much as the bookkeeping it does, so assignments share commands.
    local __binit_load_id=$1 __binit_load_mark __binit_load_before __binit_load_saved=$__binit_loading __binit_load_active_saved=$__binit_load_active
    [[ $- != *T* && ${__binit_state[$__binit_load_id]} == pending &&
       -z ${__binit_when[$__binit_load_id]-}${__binit_commands[$__binit_load_id]}${__binit_depends[$__binit_load_id]}${__binit_requires[$__binit_load_id]}${__binit_completions[$__binit_load_id]}${__binit_before_callbacks[$__binit_load_id]-}${__binit_after_callbacks[$__binit_load_id]-} ]] &&
        builtin binit_complete effects mark __binit_load_mark || return 2
    __binit_load_active=1 __binit_state[$__binit_load_id]=loading __binit_loading=$__binit_load_id __binit_load_before=${EPOCHREALTIME/./}
    # Sourced plugins are trusted code. A failed load can have partial effects. The plugin sees the same
    # positional parameters as under __binit_load: its identifier alone.
    # shellcheck source=/dev/null
    if source "${__binit_entry[$__binit_load_id]}" "$__binit_load_id"; then __binit_state[$__binit_load_id]=loaded
    else
        __binit_reason[$__binit_load_id]="entry returned $?"
        __binit_state[$__binit_load_id]=failed
    fi
    # The plugin may have left a trap; the shell recorder then uses the entry snapshot.
    if ! builtin binit_complete effects record-since "$__binit_load_mark" "$__binit_load_id" __binit_owned_functions __binit_owned_aliases 2>/dev/null; then
        local -A __binit_load_functions=() __binit_load_aliases=()
        __binit_record_effects "$__binit_load_id" __binit_load_functions __binit_load_aliases "$__binit_load_mark"
    fi
    __binit_loading=$__binit_load_saved __binit_load_us[$__binit_load_id]=$(( ${EPOCHREALTIME/./} - __binit_load_before )) __binit_load_active=$__binit_load_active_saved
    [[ ${__binit_state[$__binit_load_id]} == loaded ]]
}

__binit_activate_plugins() {
    local __binit_activate_id __binit_activate_command __binit_activate_definition
    local -a __binit_activate_names __binit_activate_general=()
    # A startup plugin with no condition, command or completion needs only the load pass; the other
    # passes act on the rest, so a long plugin list skips most of them.
    for __binit_activate_id in "$@"; do
        [[ ${__binit_load_kind[$__binit_activate_id]} == startup && -z ${__binit_when[$__binit_activate_id]-}${__binit_completions[$__binit_activate_id]}${__binit_commands[$__binit_activate_id]} ]] ||
            __binit_activate_general+=("$__binit_activate_id")
    done
    for __binit_activate_id in "${__binit_activate_general[@]}"; do
        if [[ ${__binit_load_kind[$__binit_activate_id]} == timed ]]; then
            __binit_dispatch_pending=1
            __binit_deadline[$__binit_activate_id]=$(( ${EPOCHREALTIME/./} + ${__binit_delay_ms[$__binit_activate_id]} * 1000 ))
        elif [[ ${__binit_load_kind[$__binit_activate_id]} == watch ]]; then
            __binit_dispatch_pending=1
            __binit_watch_snapshot "$__binit_activate_id"
        elif [[ ${__binit_load_kind[$__binit_activate_id]} == startup && -n ${__binit_when[$__binit_activate_id]-} ]]; then
            __binit_dispatch_pending=1
        fi
        [[ -n ${__binit_completions[$__binit_activate_id]}${__binit_commands[$__binit_activate_id]} ]] || continue
        if [[ -n "${__binit_completions[$__binit_activate_id]}" ]]; then IFS=' ' read -r -a __binit_activate_names <<< "${__binit_completions[$__binit_activate_id]}"; else __binit_activate_names=(); fi
        for __binit_activate_command in "${__binit_activate_names[@]}"; do
            if complete -p -- "$__binit_activate_command" &>/dev/null; then
                __binit_state[$__binit_activate_id]=blocked
                __binit_reason[$__binit_activate_id]="completion already defined: $__binit_activate_command"
            fi
        done
        if [[ -n "${__binit_commands[$__binit_activate_id]}" ]]; then IFS=' ' read -r -a __binit_activate_names <<< "${__binit_commands[$__binit_activate_id]}"; else __binit_activate_names=(); fi
        for __binit_activate_command in "${__binit_activate_names[@]}"; do
            if __binit_command_conflict "$__binit_activate_command"; then
                __binit_state[$__binit_activate_id]=blocked
                __binit_reason[$__binit_activate_id]="command already defined: $__binit_activate_command"
            fi
        done
    done
    local __binit_activate_native=0
    builtin binit_complete effects plain 2>/dev/null && __binit_activate_native=1
    for __binit_activate_id in "$@"; do
        [[ ${__binit_load_kind[$__binit_activate_id]} == startup ]] || continue
        if (( __binit_activate_native )); then
            # Status 2 means the direct load declined; errexit never sees a failed plugin here.
            __binit_load_direct "$__binit_activate_id" && continue
            (( $? != 2 )) && continue
        fi
        if __binit_load "$__binit_activate_id"; then :; fi
    done
    for __binit_activate_id in "${__binit_activate_general[@]}"; do
        if [[ ${__binit_state[$__binit_activate_id]} == pending && ${__binit_load_kind[$__binit_activate_id]} =~ ^(timed|watch)$ ]] && __binit_has_completion; then
            builtin binit_complete poll 2>/dev/null || :
            break
        fi
    done
    for __binit_activate_id in "${__binit_activate_general[@]}"; do
        [[ ${__binit_state[$__binit_activate_id]} == pending ]] || continue
        if [[ ${__binit_load_kind[$__binit_activate_id]} == wait ]]; then
            __binit_wait_pending=1
            __binit_wait_queue+=("$__binit_activate_id")
        fi
        if [[ -n "${__binit_completions[$__binit_activate_id]}" ]]; then IFS=' ' read -r -a __binit_activate_names <<< "${__binit_completions[$__binit_activate_id]}"; else __binit_activate_names=(); fi
        for __binit_activate_command in "${__binit_activate_names[@]}"; do
            if [[ ${__binit_state[$__binit_activate_id]} == pending ]]; then
                complete -F __binit_complete -- "$__binit_activate_command"
                __binit_completion_spec[$__binit_activate_command]=$( { builtin complete -p -- "$__binit_activate_command" 1>&"$__binit_definition_fd"; } {__binit_definition_fd}>&1 >/dev/null )
            fi
        done
    done
    # Startup plugins may have supplied a function after the first conflict check.
    for __binit_activate_id in "${__binit_activate_general[@]}"; do
        [[ ${__binit_state[$__binit_activate_id]} == pending ]] || continue
        if [[ -n "${__binit_commands[$__binit_activate_id]}" ]]; then IFS=' ' read -r -a __binit_activate_names <<< "${__binit_commands[$__binit_activate_id]}"; else __binit_activate_names=(); fi
        for __binit_activate_command in "${__binit_activate_names[@]}"; do
            if [[ ${__binit_state[$__binit_activate_id]} == pending ]] && __binit_command_conflict "$__binit_activate_command"; then
                __binit_state[$__binit_activate_id]=blocked
                __binit_reason[$__binit_activate_id]="command already defined: $__binit_activate_command"
            fi
        done
    done
    for __binit_activate_id in "${__binit_activate_general[@]}"; do
        [[ ${__binit_state[$__binit_activate_id]} == pending ]] || continue
        if [[ -n "${__binit_commands[$__binit_activate_id]}" ]]; then IFS=' ' read -r -a __binit_activate_names <<< "${__binit_commands[$__binit_activate_id]}"; else __binit_activate_names=(); fi
        for __binit_activate_command in "${__binit_activate_names[@]}"; do
            if builtin eval -- "$__binit_activate_command() { __binit_invoke '$__binit_activate_command' \"\$@\"; }"; then
                # Same serializer choice as __binit_drop_commands. The subshell captures only the
                # builtin's output; inherited DEBUG output stays out.
                if [[ $- != *T* ]] && builtin binit_complete effects plain 2>/dev/null &&
                   builtin binit_complete effects function "$__binit_activate_command" __binit_activate_definition 2>/dev/null; then
                    __binit_stub[$__binit_activate_command]=$__binit_activate_definition
                else
                    __binit_stub[$__binit_activate_command]=$( { builtin declare -f -- "$__binit_activate_command" 1>&"$__binit_definition_fd"; } {__binit_definition_fd}>&1 >/dev/null )
                fi
            else
                __binit_state[$__binit_activate_id]=blocked
                __binit_reason[$__binit_activate_id]="cannot define command: $__binit_activate_command"
                __binit_drop_commands "$__binit_activate_id" || :
            fi
        done
    done
}

# Runs once per shell, after the catalog plugins and before any plugin line.
__binit_activate_shell() {
    # Returning the saved status lets the existing first prompt hook see it.
    PROMPT_COMMAND=(__binit_prompt_dispatch "${PROMPT_COMMAND[@]}")
    __binit_first_prompt=1
    __binit_active=1
    if [[ ${BINIT_COMPLETION_MENU-1} == 1 && -f $__binit_generation_dir/completion.so ]]; then
        if __binit_load_completion >/dev/null 2>&1; then
            builtin binit_complete on >/dev/null 2>&1 || :
            builtin binit_complete editor keys on >/dev/null 2>&1 || :
        fi
    fi
}
