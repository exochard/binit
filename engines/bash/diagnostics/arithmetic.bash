#!/usr/bin/env bash
# Diagnostic only; not compiler training or release validation.
set -u
total=0
for ((counter=0; counter<1500000; counter++)); do
    ((total = (total + counter * 3 + 7) % 1000003))
done
printf '%s\n' "$total"
