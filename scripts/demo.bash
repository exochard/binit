#!/usr/bin/env bash
set -euo pipefail
cd -- "${BASH_SOURCE[0]%/*}/.."
prototype_root=$PWD
prototype_binary=$prototype_root/target/release/binit
prototype_state=${BINIT_DEMO_STATE:-$prototype_root/.local-demo}
if [[ ! -x $prototype_binary ]]; then printf 'Run make build first.\n' >&2; exit 1; fi
if [[ ! -f $prototype_state/current ]]; then
    "$prototype_binary" prepare config/demo.toml --state "$prototype_state"
fi
printf 'Binit shell. Run binit plugins or binit profile. Use exit to return.\n'
exec "$prototype_binary" shell --state "$prototype_state"
