#!/usr/bin/env bash
set -euo pipefail
cd -- "${BASH_SOURCE[0]%/*}/.."
prototype_root=$PWD
prototype_binary=${BINIT_TEST_BINARY:-$prototype_root/target/release/binit}
prototype_state=${BINIT_DEMO_STATE:-$prototype_root/.local-demo}
if [[ ! -x $prototype_binary ]]; then printf 'Run make build first.\n' >&2; exit 1; fi
if [[ ${BINIT_SAFE-0} != 1 ]]; then
    if [[ ! -f $prototype_state/current ]]; then
        "$prototype_binary" prepare config/demo.toml --state "$prototype_state"
    else
        "$prototype_binary" apply --state "$prototype_state" >/dev/null
    fi
fi
if [[ ${BINIT_SAFE-0} == 1 ]]; then
    printf 'Binit recovery shell. Use exit to return.\n'
else
    printf 'Binit shell. Run binit plugins or binit profile. Use exit to return.\n'
fi
exec "$prototype_binary" shell --state "$prototype_state"
