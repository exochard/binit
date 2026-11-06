# shellcheck shell=bash
__binit_git_completion_load() {
    local file saved_gitk result
    saved_gitk=$(builtin complete -p gitk 2>/dev/null) || saved_gitk=''
    for file in /usr/share/git/completion/git-completion.bash /usr/share/bash-completion/completions/git /etc/bash_completion.d/git; do
        [[ -r $file ]] || continue
        result=0
        # shellcheck source=/dev/null
        source "$file" || result=$?
        # complete -p emits a reusable quoted specification, not project data.
        if [[ -n $saved_gitk ]]; then eval "$saved_gitk"; fi
        (( result == 0 )) || return "$result"
        builtin complete -p git >/dev/null
        return "$?"
    done
    printf 'binit: Git Bash completion unavailable; install the Git completion package\n' >&2
    return 1
}
__binit_git_completion_load
