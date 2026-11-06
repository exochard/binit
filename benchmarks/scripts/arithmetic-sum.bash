set -euo pipefail
sum=0
for ((i=0; i<BENCH_COUNT; i++)); do
    ((sum += (i * 13 + 7) % 997))
done
printf '%s\n' "$sum"
