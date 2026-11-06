set -euo pipefail
total=0
pattern='^([a-z]+)-([0-9]+)\.(log|txt)$'
for ((i=0; i<BENCH_COUNT; i++)); do
    file="worker-$((i % 1009)).log"
    if [[ $file =~ $pattern ]]; then ((total += BASH_REMATCH[2] + 1)); fi
done
printf '%s\n' "$total"
