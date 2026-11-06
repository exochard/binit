set -euo pipefail
total=0
for ((i=0; i<BENCH_COUNT; i++)); do
    last=$(cat "$DATA_DIR/records.txt" | tail -n 1)
    number=${last#*:}
    number=${number%%:*}
    ((total += number))
done
printf '%s\n' "$total"
