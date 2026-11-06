.PHONY: build verify demo bench

SCOPE = systemd-run --user --scope -p CollectMode=inactive-or-failed -p MemoryMax=3G -p MemorySwapMax=0 -p CPUQuota=200% -p RuntimeMaxSec=900 nice -n 19

build:
	bash scripts/resources.bash
	$(SCOPE) cargo build --release --locked --offline

verify:
	bash scripts/resources.bash
	$(SCOPE) bash scripts/verify.bash

demo:
	bash scripts/demo.bash

bench:
	bash scripts/resources.bash
	$(SCOPE) python3 scripts/terminal.py bench
