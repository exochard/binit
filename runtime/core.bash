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
declare -ga __binit_line_counts=() __binit_line_args=()
declare -g __binit_loading='' __binit_last_directory='' BINIT_LAST_STATUS=0 BINIT_PROMPT_TEXT=''
declare -g __binit_line_text='' __binit_first_prompt=0 __binit_lines_total=0 __binit_lines_matched=0

__binit_register() {
    local id=$1 entry=$2 kind=$3 requires=$4 depends=$5 completions=$6 commands=${7-} command
    __binit_ids+=("$id")
    __binit_entry[$id]=$entry
    __binit_load_kind[$id]=$kind
    __binit_requires[$id]=$requires
    __binit_depends[$id]=$depends
    __binit_state[$id]=pending
    __binit_load_us[$id]=0
    __binit_hook_us[$id]=0
    __binit_hook_calls[$id]=0
    __binit_commands[$id]=$commands
    __binit_completions[$id]=$completions
    __binit_reason[$id]=''
    local -a completion_names command_names
    IFS=' ' read -r -a completion_names <<< "$completions"
    IFS=' ' read -r -a command_names <<< "$commands"
    for command in "${completion_names[@]}"; do __binit_completion_owner[$command]=$id; done
    for command in "${command_names[@]}"; do __binit_command_owner[$command]=$id; done
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
    local __binit_load_id=$1 __binit_load_requirement __binit_load_dependency
    local __binit_load_before __binit_load_after __binit_load_saved=$__binit_loading
    local -a __binit_load_dependencies __binit_load_requirements
    case ${__binit_state[$__binit_load_id]-missing} in
        loaded) return 0 ;;
        pending) ;;
        *) return 1 ;;
    esac
    __binit_state[$__binit_load_id]=loading
    if ! __binit_drop_commands "$__binit_load_id"; then
        __binit_state[$__binit_load_id]=blocked
        return 1
    fi
    IFS=' ' read -r -a __binit_load_dependencies <<< "${__binit_depends[$__binit_load_id]}"
    IFS=' ' read -r -a __binit_load_requirements <<< "${__binit_requires[$__binit_load_id]}"
    for __binit_load_dependency in "${__binit_load_dependencies[@]}"; do
        if ! __binit_load "$__binit_load_dependency"; then
            __binit_state[$__binit_load_id]=unavailable
            __binit_reason[$__binit_load_id]="dependency unavailable: $__binit_load_dependency"
            return 1
        fi
    done
    for __binit_load_requirement in "${__binit_load_requirements[@]}"; do
        if ! command -v -- "$__binit_load_requirement" >/dev/null; then
            __binit_state[$__binit_load_id]=unavailable
            __binit_reason[$__binit_load_id]="missing command: $__binit_load_requirement"
            return 1
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
    __binit_loading=$__binit_load_saved
    __binit_load_after=${EPOCHREALTIME/./}
    __binit_load_us[$__binit_load_id]=$((__binit_load_after - __binit_load_before))
    [[ ${__binit_state[$__binit_load_id]} == loaded ]]
}

__binit_drop_commands() {
    local __binit_drop_id=$1 __binit_drop_name __binit_drop_definition
    local -a __binit_drop_names __binit_drop_owned=()
    IFS=' ' read -r -a __binit_drop_names <<< "${__binit_commands[$__binit_drop_id]}"
    for __binit_drop_name in "${__binit_drop_names[@]}"; do
        __binit_drop_definition=$( { builtin declare -f -- "$__binit_drop_name" 1>&"$__binit_definition_fd"; } {__binit_definition_fd}>&1 >/dev/null )
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
    local __binit_call_callback=$1 __binit_call_owner=${__binit_hook_owner[$1]} __binit_call_before __binit_call_after
    [[ ${__binit_state[$__binit_call_owner]} == loaded ]] || return 0
    __binit_call_before=${EPOCHREALTIME/./}
    if "$__binit_call_callback" "$BINIT_LAST_STATUS" "$PWD"; then :; else :; fi
    __binit_call_after=${EPOCHREALTIME/./}
    __binit_hook_us[$__binit_call_owner]=$(( ${__binit_hook_us[$__binit_call_owner]} + __binit_call_after - __binit_call_before ))
    __binit_hook_calls[$__binit_call_owner]=$(( ${__binit_hook_calls[$__binit_call_owner]} + 1 ))
}

__binit_prompt_dispatch() {
    local __binit_prompt_dispatch_status=$? __binit_prompt_dispatch_id __binit_prompt_dispatch_callback __binit_prompt_dispatch_value
    BINIT_LAST_STATUS=$__binit_prompt_dispatch_status
    if (( __binit_first_prompt )); then
        __binit_first_prompt=0
        __binit_first_prompt_work
    fi
    for __binit_prompt_dispatch_id in "${__binit_ids[@]}"; do
        if [[ ${__binit_load_kind[$__binit_prompt_dispatch_id]} == prompt && ${__binit_state[$__binit_prompt_dispatch_id]} == pending ]]; then
            if __binit_load "$__binit_prompt_dispatch_id"; then :; fi
        fi
    done
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

__binit_complete() {
    local __binit_complete_name=${COMP_WORDS[0]-} __binit_complete_id
    [[ -n $__binit_complete_name && -v __binit_completion_owner[$__binit_complete_name] ]] || return 1
    __binit_complete_id=${__binit_completion_owner[$__binit_complete_name]}
    if __binit_load "$__binit_complete_id"; then
        # Bash retries completion using the specification installed by the plugin.
        return 124
    fi
    complete -r -- "$__binit_complete_name"
    return 1
}

__binit_has_completion() {
    [[ $( { builtin type -a -t binit_complete 1>&"$__binit_definition_fd" 2>/dev/null; } {__binit_definition_fd}>&1 >/dev/null ) == *builtin* ]]
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
        printf '%-20s %-12s %10s %10s %8s  %s\n' PLUGIN STATE LOAD_US HOOK_US CALLS REASON
        for id in "${__binit_ids[@]}"; do
            printf '%-20s %-12s %10s %10s %8s  %s\n' "$id" "${__binit_state[$id]}" "${__binit_load_us[$id]}" "${__binit_hook_us[$id]}" "${__binit_hook_calls[$id]}" "${__binit_reason[$id]}"
        done
    elif [[ ${1-} == load ]]; then
        if (( $# != 2 )) || [[ ! $2 =~ ^[a-zA-Z][a-zA-Z0-9_-]{0,63}$ || ! -v __binit_state[$2] ]]; then
            printf 'Usage: binit load ID\n' >&2
            return 2
        fi
        if ! __binit_load "$2"; then
            printf 'binit: %s: %s\n' "$2" "${__binit_reason[$2]:-${__binit_state[$2]}}" >&2
            return 1
        fi
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
    elif [[ ${1-} == plugin ]]; then
        __binit_plugin_line "${@:2}"
    elif [[ ${1-} == sync ]]; then
        if (( $# != 1 )); then
            printf 'Usage: binit sync\n' >&2
            return 2
        fi
        __binit_write_declared || return
        command "$__binit_manager" sync --state "$BINIT_STATE_DIR"
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
        printf 'Usage: binit plugin SOURCE [id=ID] [files=A,B] [load=startup|prompt|command|completion]\n' >&2
        printf '                          [commands=A,B] [completions=A,B] [requires=A,B] [depends=A,B]\n                          [ver=TAG|BRANCH|COMMIT]  (Git sources)\n' >&2
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
            id|files|load|commands|completions|requires|depends|ver) ;;
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
        startup|prompt|command|completion) ;;
        *)
            printf 'binit: plugin %s: load must be startup, prompt, command or completion\n' "$1" >&2
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
    printf 'binit: %d plugin line%s changed since the last sync; run binit sync\n' \
        "$__binit_first_changed" "$( ((__binit_first_changed == 1)) || printf s )" >&2
    # An empty list could come from a shell that never reads the startup file.
    if (( __binit_lines_total > 0 )); then __binit_write_declared || :; fi
}

# Activates the given plugins together: conflicts are checked before any
# startup dependency can load, then triggers are installed.
__binit_activate_plugins() {
    local __binit_activate_id __binit_activate_command
    local -a __binit_activate_names
    for __binit_activate_id in "$@"; do
        IFS=' ' read -r -a __binit_activate_names <<< "${__binit_completions[$__binit_activate_id]}"
        for __binit_activate_command in "${__binit_activate_names[@]}"; do
            if complete -p -- "$__binit_activate_command" &>/dev/null; then
                __binit_state[$__binit_activate_id]=blocked
                __binit_reason[$__binit_activate_id]="completion already defined: $__binit_activate_command"
            fi
        done
        IFS=' ' read -r -a __binit_activate_names <<< "${__binit_commands[$__binit_activate_id]}"
        for __binit_activate_command in "${__binit_activate_names[@]}"; do
            if __binit_command_conflict "$__binit_activate_command"; then
                __binit_state[$__binit_activate_id]=blocked
                __binit_reason[$__binit_activate_id]="command already defined: $__binit_activate_command"
            fi
        done
    done
    for __binit_activate_id in "$@"; do
        if [[ ${__binit_load_kind[$__binit_activate_id]} == startup ]]; then
            if __binit_load "$__binit_activate_id"; then :; fi
        fi
    done
    for __binit_activate_id in "$@"; do
        IFS=' ' read -r -a __binit_activate_names <<< "${__binit_completions[$__binit_activate_id]}"
        for __binit_activate_command in "${__binit_activate_names[@]}"; do
            if [[ ${__binit_state[$__binit_activate_id]} == pending ]]; then complete -F __binit_complete -- "$__binit_activate_command"; fi
        done
    done
    # Startup plugins may have supplied a function after the first conflict check.
    for __binit_activate_id in "$@"; do
        IFS=' ' read -r -a __binit_activate_names <<< "${__binit_commands[$__binit_activate_id]}"
        for __binit_activate_command in "${__binit_activate_names[@]}"; do
            if [[ ${__binit_state[$__binit_activate_id]} == pending ]] && __binit_command_conflict "$__binit_activate_command"; then
                __binit_state[$__binit_activate_id]=blocked
                __binit_reason[$__binit_activate_id]="command already defined: $__binit_activate_command"
            fi
        done
    done
    for __binit_activate_id in "$@"; do
        [[ ${__binit_state[$__binit_activate_id]} == pending ]] || continue
        IFS=' ' read -r -a __binit_activate_names <<< "${__binit_commands[$__binit_activate_id]}"
        for __binit_activate_command in "${__binit_activate_names[@]}"; do
            if builtin eval -- "$__binit_activate_command() { __binit_invoke '$__binit_activate_command' \"\$@\"; }"; then
                # Capture only the builtin's output; inherited DEBUG output stays out.
                __binit_stub[$__binit_activate_command]=$( { builtin declare -f -- "$__binit_activate_command" 1>&"$__binit_definition_fd"; } {__binit_definition_fd}>&1 >/dev/null )
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
        fi
    fi
}
