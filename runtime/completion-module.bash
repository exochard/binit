# shellcheck shell=bash
__binit_enable_completion() {
    case $2:${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]} in
        dual:5.2) set -- "$1/completion-5.2.so" ;;
        dual:5.3|single:*) set -- "$1/completion.so" ;;
        *) return 1 ;;
    esac
    [[ -f $1 ]] || return 1
    bind -q complete >/dev/null 2>&1 || :
    builtin enable -f "$1" binit_complete
}
