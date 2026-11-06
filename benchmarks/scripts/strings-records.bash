set -euo pipefail
total=0
for ((i=0; i<BENCH_COUNT; i++)); do
    record=" user_$((i % 73)):group_name:status_ready "
    record=${record//_/-}
    record=${record^^}
    record=${record:1:${#record}-2}
    printf -v line '%06d %s' "$((i % 100000))" "$record"
    ((total += ${#line}))
done
printf '%s\n' "$total"
