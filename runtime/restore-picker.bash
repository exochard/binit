# shellcheck shell=bash
# shellcheck disable=SC2154 # Initialized by Binit's runtime.

__binit_restore_picker() {
    if [[ ! -t 0 || ! -t 2 || ${TERM:-dumb} == dumb ]]; then
        printf 'binit: snapshot selection requires a terminal\n' >&2
        return 2
    fi
    local id=$1 output key next index=0 generation
    local -a generations=()
    output=$(command "$__binit_manager" candidate __restore-list "$id" --state "$BINIT_STATE_DIR") || return
    [[ -n $output ]] || { printf 'binit: no eligible snapshots for %s\n' "$id" >&2; return 1; }
    mapfile -t generations <<< "$output"
    printf 'j/k move  h/l first/last  enter stage  esc cancel\n' >&2
    while :; do
        generation=${generations[index]}
        printf '\r\033[2K%.12s  %d/%d' "$generation" "$((index + 1))" "${#generations[@]}" >&2
        if ! IFS= read -rsn1 key; then printf '\r\033[2K\n' >&2; return 1; fi
        case $key in
            '') printf '\r\033[2K\n' >&2
                command "$__binit_manager" candidate restore "$id" --from "$generation" --state "$BINIT_STATE_DIR"
                return $? ;;
            $'\e')
                if IFS= read -rsn1 -t 0.05 next && [[ $next == '[' ]] && IFS= read -rsn1 -t 0.05 next; then
                    case $next in A) key=k ;; B) key=j ;; C) key=l ;; D) key=h ;; esac
                else
                    printf '\r\033[2K\n' >&2
                    return 1
                fi ;;
            q) printf '\r\033[2K\n' >&2; return 1 ;;
        esac
        case $key in
            j) if ((index + 1 < ${#generations[@]})); then index=$((index + 1)); fi ;;
            k) if ((index > 0)); then index=$((index - 1)); fi ;;
            h) index=0 ;;
            l) index=$((${#generations[@]} - 1)) ;;
        esac
    done
}
