#!/usr/bin/env bash
# Diagnostic workload; timings from an instrumented binary are not release evidence.
set -euo pipefail
declare -A counts=()
items=()
checksum=1
record() {
    local index=$1 path name key
    path="/srv/packages/group_$((index % 89))/item_$index.txt"
    name=${path##*/}
    name=${name%.txt}
    key=${name//_/-}
    counts[group_$((index % 89))]=$(( ${counts[group_$((index % 89))]:-0} + 1 ))
    items[index % 128]=$key
    if [[ $key =~ ^item-([0-9]+)$ ]]; then
        checksum=$(((checksum + BASH_REMATCH[1] + ${#path}) % 65521))
    fi
}
for ((i=0; i<150000; i++)); do record "$i"; done
printf '%s %s %s\n' "$checksum" "${#counts[@]}" "${#items[@]}"
