# shellcheck shell=bash
declare -g __binit_git_fd='' __binit_git_requested='' __binit_git_last_directory=''
declare -g __binit_git_last_refresh=0

__binit_git_prompt() {
    local branch=''
    if [[ -n $__binit_git_fd ]] && IFS= read -r -t 0 -u "$__binit_git_fd"; then
        if IFS= read -r -t 0.001 -u "$__binit_git_fd" branch; then
            if [[ $__binit_git_requested == "$PWD" ]]; then binit_segment git "$branch"; fi
        fi
        exec {__binit_git_fd}<&-
        __binit_git_fd=''
    fi
    if [[ $__binit_git_last_directory != "$PWD" ]]; then
        binit_segment git ''
        __binit_git_last_refresh=0
        __binit_git_last_directory=$PWD
    fi
    if [[ -z $__binit_git_fd ]] && (( EPOCHSECONDS - __binit_git_last_refresh >= 2 )); then
        __binit_git_last_refresh=$EPOCHSECONDS
        __binit_git_requested=$PWD
        # A bounded worker reports data only. Nothing it prints is evaluated.
        exec {__binit_git_fd}< <(
            branch=$(timeout 1s git --no-optional-locks -C "$PWD" symbolic-ref --quiet --short HEAD 2>/dev/null) || branch=''
            if [[ -z $branch ]]; then
                branch=$(timeout 1s git --no-optional-locks -C "$PWD" rev-parse --short HEAD 2>/dev/null) || branch=''
            fi
            printf '%s\n' "$branch"
        )
    fi
}
binit_on prompt __binit_git_prompt
