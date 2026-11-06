set -euo pipefail
total=0
for ((i=0; i<BENCH_COUNT; i++)); do
    while IFS=: read -r name number state; do
        if [[ $state == ready ]]; then ((total += number + ${#name})); fi
    done < "$DATA_DIR/records.txt"
done
printf '%s\n' "$total"
