#!/usr/bin/env bash
set -euo pipefail
cd -- "${BASH_SOURCE[0]%/*}/.."
export BINIT_BASH=${BINIT_BASH:-$(command -v bash)}
export CARGO_TARGET_DIR=${CARGO_TARGET_DIR:-$PWD/target}
for verify_path in "$BINIT_BASH" "$CARGO_TARGET_DIR"; do
    case $verify_path in /*) ;; *) printf 'BINIT_BASH and CARGO_TARGET_DIR must be absolute paths\n' >&2; exit 2 ;; esac
done
verify_features=()
case ${BINIT_NATIVE:-1} in
    0) verify_features=(--no-default-features) ;;
    1) ;;
    *) printf 'BINIT_NATIVE must be 0 or 1\n' >&2; exit 2 ;;
esac
export BINIT_VERIFY_EVIDENCE=${BINIT_VERIFY_EVIDENCE:-$CARGO_TARGET_DIR/verification}
mkdir -p "$BINIT_VERIFY_EVIDENCE" "$PWD/tmp"
python3 - <<'PY'
import json, os, platform, subprocess
from pathlib import Path
def output(*args):
    return subprocess.check_output(args, text=True).strip()
Path(os.environ['BINIT_VERIFY_EVIDENCE'], 'environment.json').write_text(json.dumps({
    'commit': output('git', 'rev-parse', 'HEAD'), 'architecture': platform.machine(),
    'bash': os.environ['BINIT_BASH'], 'bash_version': output(os.environ['BINIT_BASH'], '--version'),
    'native': os.environ.get('BINIT_NATIVE', '1'), 'rustc': output('rustc', '--version'),
    'python': platform.python_version(), 'target': os.environ['CARGO_TARGET_DIR'],
    'bash_headers': os.environ.get('BINIT_BASH_INCLUDE', '/usr/include/bash'),
    'readline_headers': os.environ.get('BINIT_READLINE_INCLUDE', '/usr/include'),
    'compiler': output(os.environ.get('CC', 'cc'), '--version'),
}, indent=2) + '\n')
PY
cargo fmt --all -- --check
shellcheck -S warning runtime/*.bash plugins/*/plugin.bash examples/plugins/*/plugin.bash scripts/*.bash benchmarks/scripts/*.bash engines/bash/*.bash
for script in runtime/*.bash plugins/*/plugin.bash examples/plugins/*/plugin.bash scripts/*.bash benchmarks/scripts/*.bash engines/bash/*.bash; do "$BINIT_BASH" -O extglob -n "$script"; done
cargo clippy --locked --offline "${verify_features[@]}" --all-targets -- -D warnings
cargo test --locked --offline "${verify_features[@]}" -- --test-threads=2
if [[ -n ${BINIT_TEST_BINARY-} ]]; then
    if [[ $BINIT_TEST_BINARY != /* || ! -x $BINIT_TEST_BINARY ]]; then
        printf 'BINIT_TEST_BINARY must be an absolute executable path\n' >&2
        exit 2
    fi
else
    cargo build --release --locked --offline "${verify_features[@]}"
    BINIT_TEST_BINARY="$CARGO_TARGET_DIR/release/binit"
fi
export BINIT_TEST_BINARY
sha256sum -- "$BINIT_TEST_BINARY" > "$BINIT_VERIFY_EVIDENCE/test-binary.sha256"
if [[ ${BINIT_NATIVE:-1} == 1 ]]; then
    cmake -S native -B "$CARGO_TARGET_DIR/native-check" -DCMAKE_BUILD_TYPE=Debug -DBASH_INCLUDE_DIR="${BINIT_BASH_INCLUDE:-/usr/include/bash}" -DREADLINE_INCLUDE_DIR="${BINIT_READLINE_INCLUDE:-/usr/include}"
    cmake --build "$CARGO_TARGET_DIR/native-check" --parallel 2
    ctest --test-dir "$CARGO_TARGET_DIR/native-check" --output-on-failure
    export BINIT_TEST_MODULE=${BINIT_TEST_MODULE:-"$CARGO_TARGET_DIR/native-check/binit_complete.so"}
    if [[ $BINIT_TEST_MODULE != /* || ! -r $BINIT_TEST_MODULE ]]; then
        printf 'BINIT_TEST_MODULE must be an absolute readable file path\n' >&2
        exit 2
    fi
    sha256sum -- "$BINIT_TEST_MODULE" > "$BINIT_VERIFY_EVIDENCE/test-module.sha256"
    python3 -m unittest discover -s tests -p 'test_*.py'
else
    printf 'Native-disabled configuration: Python unit suites and wait fallback; native PTY suites belong to native-enabled jobs.\n'
    python3 - <<'PY'
import pathlib, unittest
suite = unittest.TestSuite()
loader = unittest.TestLoader()
for path in sorted(pathlib.Path('tests').glob('test_*.py')):
    if path.name not in ('test_completion.py', 'test_wait.py', 'test_editor.py', 'test_live_history.py', 'test_effects.py', 'test_history_contract.py'):
        suite.addTests(loader.discover('tests', pattern=path.name))
raise SystemExit(not unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful())
PY
    python3 -m unittest discover -s tests -p test_wait.py -k without_the_module
fi
python3 scripts/terminal.py smoke
