set -euo pipefail
declare -A counts=()
for ((i=0; i<BENCH_COUNT; i++)); do
    key="group_$((i % 127))"
    counts[$key]=$(( ${counts[$key]:-0} + 1 ))
done
total=0
for value in "${counts[@]}"; do ((total += value)); done
printf '%s %s\n' "$total" "${#counts[@]}"
