#!/usr/bin/env bash
set -euo pipefail
cd -- "${BASH_SOURCE[0]%/*}/.."
cargo fmt --all -- --check
shellcheck -S warning runtime/core.bash plugins/*/plugin.bash examples/plugins/*/plugin.bash scripts/*.bash benchmarks/scripts/*.bash engines/bash/*.bash
for script in runtime/core.bash plugins/*/plugin.bash examples/plugins/*/plugin.bash scripts/*.bash benchmarks/scripts/*.bash engines/bash/*.bash; do bash -O extglob -n "$script"; done
cargo clippy --locked --offline --all-targets -- -D warnings
cargo test --locked --offline -- --test-threads=2
cargo build --release --locked --offline
cmake -S native -B target/native-check -DCMAKE_BUILD_TYPE=Debug
cmake --build target/native-check --parallel 2
ctest --test-dir target/native-check --output-on-failure
export BINIT_TEST_MODULE="$PWD/target/native-check/binit_complete.so"
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/terminal.py smoke
