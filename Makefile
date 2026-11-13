.PHONY: build verify demo bench bench-interactive bench-lifecycle

-include .local-demo-interactive/build.mk

export RUSTC_WRAPPER :=

build:
	@cargo build --release --locked --offline

verify:
	@bash scripts/verify.bash

demo:
	@bash scripts/demo.bash

bench:
	@python3 scripts/terminal.py bench

bench-interactive:
	@python3 scripts/bench_interactive.py --plugin-root "$${BINIT_PLUGIN_ROOT:-../binit-plugins}" --output .cache/interactive-benchmark.json

bench-lifecycle:
	@python3 scripts/bench_lifecycle.py --output .cache/lifecycle-benchmark.json
