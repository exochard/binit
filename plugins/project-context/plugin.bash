# shellcheck shell=bash
# shellcheck disable=SC2154 # The generated plan supplies the manager path.
declare -gA __binit_project_previous=() __binit_project_set=() __binit_project_exported=()

__binit_project_directory() {
    local key value definition fd
    for key in "${!__binit_project_set[@]}"; do
        definition=$(declare -p "$key" 2>/dev/null) || definition=''
        if [[ $definition =~ ^declare\ -[^\ ]*[arAni] ]]; then
            printf 'binit: project variable changed type; leaving it intact: %s\n' "$key" >&2
            continue
        fi
        if [[ ${__binit_project_set[$key]} == 1 ]]; then
            printf -v "$key" '%s' "${__binit_project_previous[$key]}"
            if [[ ${__binit_project_exported[$key]} == 1 ]]; then export "${key?}"; else export -n "${key?}"; fi
        elif [[ ${__binit_project_set[$key]} == 2 ]]; then
            unset -v "$key"
            if [[ ${__binit_project_exported[$key]} == 1 ]]; then export "${key?}"
            else declare -g -- "$key"
            fi
        else
            unset -v "$key"
        fi
    done
    __binit_project_previous=() __binit_project_set=() __binit_project_exported=()
    binit_segment project ''
    [[ -f $PWD/.binit-project.toml ]] || return 0
    binit_segment project 'project:untrusted'
    exec {fd}< <("$__binit_manager" project "$PWD" --state "$BINIT_STATE_DIR" 2>/dev/null)
    while IFS= read -r -d '' -u "$fd" key && IFS= read -r -d '' -u "$fd" value; do
        if [[ $key == :label ]]; then
            binit_segment project "${value:-project}"
            continue
        fi
        definition=$(declare -p "$key" 2>/dev/null) || definition=''
        # Keep arrays and readonly variables intact, including on directory exit.
        if [[ $definition =~ ^declare\ -[^\ ]*[arAni] ]]; then
            printf 'binit: project variable is not a writable scalar: %s\n' "$key" >&2
            continue
        fi
        if [[ -v $key ]]; then
            __binit_project_previous[$key]=${!key}
            __binit_project_set[$key]=1
            if [[ $definition =~ ^declare\ -[^\ ]*x ]]; then __binit_project_exported[$key]=1
            else __binit_project_exported[$key]=0
            fi
        else
            __binit_project_previous[$key]=''
            if [[ -n $definition ]]; then __binit_project_set[$key]=2
            else __binit_project_set[$key]=0
            fi
        fi
        if [[ $definition =~ ^declare\ -[^\ ]*x ]]; then __binit_project_exported[$key]=1
        else __binit_project_exported[$key]=0
        fi
        export "$key=$value"
    done
    exec {fd}<&-
}
binit_on directory __binit_project_directory
