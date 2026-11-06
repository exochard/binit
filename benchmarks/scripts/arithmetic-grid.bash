set -euo pipefail
value=17
for ((i=0; i<BENCH_COUNT; i++)); do
    for ((j=0; j<32; j++)); do
        if (( (i + j) % 3 )); then
            value=$(((value * 33 + j) % 65521))
        else
            value=$(((value + i + 1) % 65521))
        fi
    done
done
printf '%s\n' "$value"
