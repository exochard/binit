# shellcheck shell=bash
binit completions register npm python3 "${BASH_SOURCE[0]%/*}/complete.py"
