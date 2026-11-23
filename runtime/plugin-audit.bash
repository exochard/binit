# shellcheck shell=bash
# shellcheck disable=SC2154 # The runtime defines the audit state.

__binit_audit_snapshot() {
    local -n __binit_audit_result=$1
    local __binit_audit_key __binit_audit_record
    while IFS= read -r -d '' __binit_audit_key && IFS= read -r -d '' __binit_audit_record; do
        __binit_audit_result[$__binit_audit_key]=$__binit_audit_record
    done < <(__binit_audit_entries)
}

__binit_audit_entries() {
    local __binit_audit_line __binit_audit_name __binit_audit_map __binit_audit_value
    local __binit_audit_variable='^declare (-[^ ]+) ([a-zA-Z_][a-zA-Z0-9_]*)(=|$)'
    local __binit_audit_spec='^complete .* ([-a-zA-Z0-9_+.]+)$'
    local __binit_audit_key='^("([^"\\]|\\.)*"):'
    while IFS= read -r __binit_audit_name; do
        [[ $__binit_audit_name =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ &&
            $__binit_audit_name != __binit_* && $__binit_audit_name != binit &&
            $__binit_audit_name != binit_on && $__binit_audit_name != binit_segment ]] || continue
        __binit_audit_value=$(builtin declare -pf -- "$__binit_audit_name" 2>/dev/null) || continue
        printf '%s\0%s\0' "function:$__binit_audit_name" "$__binit_audit_value"
    done < <(builtin compgen -A function)
    while IFS= read -r __binit_audit_line; do
        [[ $__binit_audit_line =~ $__binit_audit_variable ]] || continue
        __binit_audit_name=${BASH_REMATCH[2]}
        case $__binit_audit_name in
            __binit_*|BASH*|EPOCH*|FUNCNAME|LINENO|RANDOM|SECONDS|SRANDOM|PIPESTATUS|_|COMP_*|READLINE_*) continue ;;
        esac
        printf '%s\0%s\0' "variable:$__binit_audit_name" "$__binit_audit_line"
    done < <(builtin declare -p)
    while IFS= read -r __binit_audit_line; do
        [[ $__binit_audit_line =~ $__binit_audit_spec ]] || continue
        printf '%s\0%s\0' "completion:${BASH_REMATCH[1]}" "$__binit_audit_line"
    done < <(builtin complete -p)
    [[ $- == *i* ]] || return 0
    for __binit_audit_map in emacs-standard emacs-meta emacs-ctlx vi-command vi-insertion; do
        while IFS= read -r __binit_audit_line; do
            [[ $__binit_audit_line =~ $__binit_audit_key ]] || continue
            printf '%s\0%s\0' "binding:$__binit_audit_map/${BASH_REMATCH[1]}" "$__binit_audit_line"
        done < <(
            builtin bind -m "$__binit_audit_map" -p
            builtin bind -m "$__binit_audit_map" -s
            builtin bind -m "$__binit_audit_map" -X
        )
    done
}

__binit_audit_clear() {
    local __binit_audit_id=$1 __binit_audit_index
    for __binit_audit_index in "${!__binit_audit_before[@]}"; do
        [[ $__binit_audit_index == "$__binit_audit_id:"* ]] || continue
        unset '__binit_audit_before[$__binit_audit_index]' '__binit_audit_after[$__binit_audit_index]' '__binit_audit_attributes[$__binit_audit_index]'
    done
    unset '__binit_audit_status[$__binit_audit_id]' '__binit_audit_nested[$__binit_audit_id]'
}

__binit_audit_begin() {
    local __binit_audit_id=$1
    __binit_audit_nested[$__binit_audit_id]=0
    if [[ $- == *x* || $- == *T* || -n $(trap -p DEBUG RETURN) ]]; then
        __binit_audit_status[$__binit_audit_id]='skipped (shell tracing)'
        return 0
    fi
    if [[ -n $__binit_loading ]]; then
        __binit_audit_nested[$__binit_loading]=1
        __binit_audit_nested[$__binit_audit_id]=1
    fi
    __binit_audit_status[$__binit_audit_id]=capturing
    __binit_audit_snapshot "$2"
}

__binit_audit_value() {
    if [[ $1 == variable:* && -n $2 ]]; then
        printf '%s' "$2" | command sha256sum
    else
        printf '%s' "$2"
    fi
}

__binit_audit_end() {
    local __binit_audit_id=$1 __binit_audit_symbol __binit_audit_index
    local -n __binit_audit_initial=$2
    local -A __binit_audit_final=() __binit_audit_seen=()
    local __binit_audit_old __binit_audit_new __binit_audit_flags
    if [[ $- == *x* || $- == *T* || -n $(trap -p DEBUG RETURN) ]]; then
        __binit_audit_status[$__binit_audit_id]='skipped (shell tracing)'
        return 0
    fi
    __binit_audit_snapshot __binit_audit_final
    for __binit_audit_symbol in "${!__binit_audit_initial[@]}" "${!__binit_audit_final[@]}"; do
        [[ ! -v __binit_audit_seen[$__binit_audit_symbol] ]] || continue
        __binit_audit_seen[$__binit_audit_symbol]=1
        __binit_audit_old=${__binit_audit_initial[$__binit_audit_symbol]-}
        __binit_audit_new=${__binit_audit_final[$__binit_audit_symbol]-}
        [[ $__binit_audit_old != "$__binit_audit_new" ]] || continue
        __binit_audit_index=$__binit_audit_id:$__binit_audit_symbol
        if [[ $__binit_audit_symbol == variable:* ]]; then
            __binit_audit_flags=${__binit_audit_old#declare }
            __binit_audit_attributes[$__binit_audit_index]=${__binit_audit_flags%% *}
            __binit_audit_flags=${__binit_audit_new#declare }
            __binit_audit_attributes[$__binit_audit_index]+=" -> ${__binit_audit_flags%% *}"
        fi
        __binit_audit_before[$__binit_audit_index]=$(__binit_audit_value "$__binit_audit_symbol" "$__binit_audit_old") || { __binit_audit_status[$__binit_audit_id]=incomplete; return 0; }
        __binit_audit_after[$__binit_audit_index]=$(__binit_audit_value "$__binit_audit_symbol" "$__binit_audit_new") || { __binit_audit_status[$__binit_audit_id]=incomplete; return 0; }
    done
    __binit_audit_status[$__binit_audit_id]=captured
}

__binit_audit_report() {
    local __binit_audit_id=$1 __binit_audit_index __binit_audit_symbol __binit_audit_change
    local __binit_audit_now __binit_audit_state
    local -A __binit_audit_live=()
    printf 'effects %s\n' "${__binit_audit_status[$__binit_audit_id]}"
    [[ ${__binit_audit_status[$__binit_audit_id]} == captured && $- != *x* && $- != *T* ]] || return 0
    if [[ ${__binit_audit_nested[$__binit_audit_id]} == 1 ]]; then
        printf 'effects include nested loads\n'
    fi
    __binit_audit_snapshot __binit_audit_live
    for __binit_audit_index in "${!__binit_audit_before[@]}"; do
        [[ $__binit_audit_index == "$__binit_audit_id:"* ]] || continue
        __binit_audit_symbol=${__binit_audit_index#*:}
        __binit_audit_change=changed
        [[ -n ${__binit_audit_before[$__binit_audit_index]} ]] || __binit_audit_change=added
        [[ -n ${__binit_audit_after[$__binit_audit_index]} ]] || __binit_audit_change=removed
        __binit_audit_now=$(__binit_audit_value "$__binit_audit_symbol" "${__binit_audit_live[$__binit_audit_symbol]-}") || return 1
        __binit_audit_state=current
        [[ $__binit_audit_now == "${__binit_audit_after[$__binit_audit_index]}" ]] || __binit_audit_state=modified
        printf 'effect %s %s %q %s' "$__binit_audit_change" "${__binit_audit_symbol%%:*}" "${__binit_audit_symbol#*:}" "$__binit_audit_state"
        [[ ! -v __binit_audit_attributes[$__binit_audit_index] ]] || printf '  %s' "${__binit_audit_attributes[$__binit_audit_index]}"
        printf '\n'
    done | LC_ALL=C command sort
}

# Restore plain functions only when a single plugin changed the definition.
__binit_audit_restore_functions() {
    local __binit_audit_id=$1 __binit_audit_index __binit_audit_other __binit_audit_name
    local __binit_audit_old __binit_audit_expected __binit_audit_now __binit_audit_overlap
    local -a __binit_audit_restore=()
    [[ ${__binit_audit_nested[$__binit_audit_id]} == 0 && $- != *x* && $- != *T* &&
        -z $(trap -p DEBUG RETURN) ]] || return 0
    for __binit_audit_index in "${!__binit_audit_before[@]}"; do
        [[ $__binit_audit_index == "$__binit_audit_id:function:"* ]] || continue
        __binit_audit_old=${__binit_audit_before[$__binit_audit_index]}
        __binit_audit_expected=${__binit_audit_after[$__binit_audit_index]}
        [[ $__binit_audit_old == *$'\n}' && ( -z $__binit_audit_expected || $__binit_audit_expected == *$'\n}' ) ]] || continue
        __binit_audit_name=${__binit_audit_index#*:function:}
        __binit_audit_overlap=0
        for __binit_audit_other in "${__binit_ids[@]}"; do
            [[ $__binit_audit_other != "$__binit_audit_id" ]] || continue
            if [[ -v __binit_audit_before[$__binit_audit_other:function:$__binit_audit_name] ||
                -v __binit_owned_functions[$__binit_audit_other:$__binit_audit_name] ]]; then
                __binit_audit_overlap=1
                break
            fi
        done
        (( ! __binit_audit_overlap )) || continue
        __binit_audit_now=$(builtin declare -pf -- "$__binit_audit_name" 2>/dev/null) || __binit_audit_now=''
        [[ $__binit_audit_now == "$__binit_audit_expected" ]] || continue
        __binit_audit_restore+=("$__binit_audit_old")
    done
    (( ${#__binit_audit_restore[@]} )) || return 0
    if ! (
        builtin shopt -u expand_aliases
        builtin shopt -s extglob
        for __binit_audit_old in "${__binit_audit_restore[@]}"; do
            builtin eval -- "$__binit_audit_old" || exit 1
        done
    ); then
        printf 'binit: %s: could not replay recorded function definitions\n' "$__binit_audit_id" >&2
        return 1
    fi
    local __binit_audit_aliases=0 __binit_audit_extglob=0 __binit_audit_status_code=0
    if builtin shopt -q expand_aliases; then
        __binit_audit_aliases=1
        builtin shopt -u expand_aliases
    fi
    if ! builtin shopt -q extglob; then
        __binit_audit_extglob=1
        builtin shopt -s extglob
    fi
    for __binit_audit_old in "${__binit_audit_restore[@]}"; do
        if ! builtin eval -- "$__binit_audit_old"; then
            __binit_audit_status_code=1
            break
        fi
    done
    if (( __binit_audit_aliases )); then builtin shopt -s expand_aliases; fi
    if (( __binit_audit_extglob )); then builtin shopt -u extglob; fi
    return "$__binit_audit_status_code"
}
