#!/usr/bin/env bash
set -euo pipefail
free -g
if command -v nvidia-smi >/dev/null; then
    nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader
fi
