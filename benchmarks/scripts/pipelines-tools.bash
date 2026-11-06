set -euo pipefail
for ((i=0; i<BENCH_COUNT; i++)); do
    grep ':ready$' "$DATA_DIR/records.txt" | cut -d: -f2 | sort -n | tail -n 1
done
