.PHONY: build verify demo bench

export RUSTC_WRAPPER :=

build:
	@cargo build --release --locked --offline

verify:
	@bash scripts/verify.bash

demo:
	@bash scripts/demo.bash

bench:
	@python3 scripts/terminal.py bench
