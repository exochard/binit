set -euo pipefail
for ((i=0; i<BENCH_COUNT; i++)); do
    cat "$DATA_DIR/records.txt" | {
        total=0
        while IFS=: read -r _name number state; do
            if [[ $state == ready ]]; then ((total += number)); fi
        done
        printf '%s\n' "$total"
    }
done
