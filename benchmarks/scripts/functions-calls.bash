set -euo pipefail
total=0
add() { local n=$1; ((total += n + 1)); }
dispatch() { local index=$1; add "$((index % 67))"; }
for ((i=0; i<BENCH_COUNT; i++)); do dispatch "$i"; done
printf '%s\n' "$total"
