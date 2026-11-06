#!/usr/bin/env bash
# PGO training is separate from the fixed validation scripts in benchmarks/.
set -euo pipefail
engine=$1
"$engine" --noprofile --norc -c '
set -eu
declare -A words=()
declare -a records=()
total=1
for ((round=0; round<20; round++)); do
    for ((i=0; i<12000; i++)); do
        key="entry_$((i % 173))"
        words[$key]=$(( ${words[$key]:-0} + 1 ))
        value="/var/lib/example/$key.data"
        records[i % 128]=${value##*/}
        (( total += (i * 7 + round) % 311 ))
        [[ $value =~ /([a-z_]+[0-9]+)\.data$ ]] && : "${BASH_REMATCH[1]}"
        case $key in entry_1*) : prefix ;; *) : other ;; esac
    done
done
format() { local entry=${1^^}; printf "%s:%s\n" "$entry" "${entry//_/-}"; }
for ((i=0; i<2500; i++)); do
    result=$(format "key_$i")
    [[ -n $result ]] || exit 1
done
printf "%s %s %s\n" "$total" "${#words[@]}" "${#records[@]}"
'
