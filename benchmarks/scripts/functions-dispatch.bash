set -euo pipefail
accept=0
reject=0
classify() {
    local input=$1
    case $input in
        build:*) [[ $input == *:ready ]] && ((accept += 1)) || ((reject += 1)) ;;
        test:*) if [[ $input != *:skip ]]; then ((accept += 1)); else ((reject += 1)); fi ;;
        *) ((reject += 1)) ;;
    esac
}
for ((i=0; i<BENCH_COUNT; i++)); do
    case $((i % 4)) in
        0) classify build:ready ;;
        1) classify test:skip ;;
        2) classify test:ready ;;
        3) classify unknown ;;
    esac
done
printf '%s %s\n' "$accept" "$reject"
