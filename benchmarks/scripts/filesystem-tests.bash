set -euo pipefail
total=0
for ((i=0; i<BENCH_COUNT; i++)); do
    for ((j=0; j<64; j++)); do
        file="$DATA_DIR/group_$((j / 16))/file_$j.txt"
        if [[ -f $file && -r $file && -s $file && ! -L $file ]]; then ((total += 1)); fi
    done
done
printf '%s\n' "$total"
