set -euo pipefail
total=0
for ((i=0; i<BENCH_COUNT; i++)); do
    path="/srv/projects/package_$((i % 97))/src/module_$((i % 41)).bash"
    file=${path##*/}
    base=${file%.bash}
    parent=${path%/*}
    ((total += ${#base} + ${#parent}))
done
printf '%s\n' "$total"
