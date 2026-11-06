set -euo pipefail
total=0
for ((i=0; i<BENCH_COUNT; i++)); do
    value=$(printf '%s:%04d' record "$((i % 10000))")
    ((total += ${#value}))
done
printf '%s\n' "$total"
