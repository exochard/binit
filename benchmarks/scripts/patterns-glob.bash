set -euo pipefail
shopt -s extglob nullglob
total=0
for ((i=0; i<BENCH_COUNT; i++)); do
    files=("$DATA_DIR"/group_*/file_+([0-9]).txt)
    for file in "${files[@]}"; do
        if [[ $file == */file_1*.txt ]]; then ((total += 1)); fi
    done
done
printf '%s\n' "$total"
