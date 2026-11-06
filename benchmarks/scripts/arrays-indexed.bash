set -euo pipefail
total=0
items=()
for ((i=0; i<BENCH_COUNT; i++)); do
    items=()
    for ((j=0; j<32; j++)); do items+=("item_$((i + j))"); done
    for item in "${items[@]}"; do ((total += ${#item})); done
done
printf '%s %s\n' "$total" "${#items[@]}"
