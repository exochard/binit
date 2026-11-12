# Interactive performance

`make bench-interactive` compares bare Bash, an empty Binit configuration, and
the seven explicitly selected first-party plugins. All scenarios use the same
Bash executable and vi editing mode. Prepare the editor dependencies through
`make demo` and install the development Python requirements first. Plugin
sources come from `../binit-plugins`, or `BINIT_PLUGIN_ROOT`.

```bash
make build
make bench-interactive
```

The raw receipt is `.cache/interactive-benchmark.json`. To compare a previous
binary in the same run:

```bash
python3 scripts/bench_interactive.py --plugin-root ../binit-plugins \
  --compare-binary /path/to/previous/binit --samples 20 \
  --output .cache/interactive-comparison.json
```

## What the workload measures

Each round reverses scenario order. Every shell starts in the same synthetic
Git repository, with a private home and 1,000 history entries. The PTY has
120 columns and 32 rows; a terminal emulator answers cursor-position requests.
No user history or shell startup files enter the workload. Filesystem caches
are warm.

Visible startup ends at the first prompt. Usable startup also includes the
response to a typed character. Per-character samples cover a matching history
prefix and a query without matches. Input latency ends at the last terminal
byte before a 2 ms quiet interval; the quiet interval itself is excluded.
These timings include the emulator and scheduler, rather than physical display
refresh. They are diagnostic measurements on a shared host.

The run also measures the first history search and unique filename completion.
Git completion has separate first-use and warm samples, only for configurations
with a Git provider. History is disabled for those completion measurements to
reach the ordinary completion binding directly. Plugin profile states are
checked before and after Git's first load. A failed plugin aborts the run.

Command cases execute 10,000 builtins and 20 external `true` commands. Their
round trips include prompt hooks. They expose interactive overhead, rather
than a change to Bash's execution engine. The
[interpreter benchmark](benchmarks.md) has separate workloads and release gates.

## Snapshot cost

Plugin loading records newly introduced functions and aliases for conservative
unload. The native module collects their names directly from Bash and uses the
same function serializer as `declare -f`. This avoids starting child shells for
each snapshot and function body. The runtime retains its Bash implementation
when native support is unavailable. Snapshotting never executes a function.

The serializer tests compare exact output for quoted bodies and here-documents,
including redirected functions. Invalid or readonly output targets are rejected
before mutation. Existing ownership and unload integration tests apply to both
paths.

## Local result, 2026-11-12

The [receipt](../benchmarks/results/interactive-seven-plugins-x86_64.json)
records 20 alternating rounds on x86_64 with Bash 5.3.20. It includes both
binaries' hashes and plugin source hashes, plus raw samples. Matching typing
has 220 character samples per configuration; the no-match query has 320.
Native completion is enabled and its visible menu is checked in both
seven-plugin configurations before accepting Git completion measurements.

| Metric | Bare Bash | Empty Binit | Seven plugins before | Seven plugins after |
| --- | ---: | ---: | ---: | ---: |
| Usable startup, median | 11.548 ms | 14.804 ms | 64.607 ms | 24.178 ms |
| Matching typing, p95 | 0.050 ms | 0.047 ms | 1.008 ms | 0.924 ms |
| No-match typing, p95 | 0.054 ms | 0.049 ms | 0.139 ms | 0.138 ms |
| First history search, median | 0.075 ms | 0.084 ms | 2.087 ms | 2.062 ms |
| Unique file completion, median | 2.189 ms | 2.235 ms | 2.421 ms | 2.406 ms |
| First Git completion, median | — | — | 77.907 ms | 33.449 ms |
| Warm Git completion, median | — | — | 2.785 ms | 2.675 ms |
| Builtin loop round trip, median | 6.739 ms | 6.816 ms | 7.018 ms | 6.976 ms |
| External command round trip, median | 4.858 ms | 5.098 ms | 5.502 ms | 5.533 ms |

The median paired startup ratio is 0.3714, a 62.9% reduction;
all 20 pairs improved. First Git completion has a paired ratio of
0.4325, a 56.7% reduction, with all 20 pairs improved. The
seven-plugin shell still adds about 12.6 ms over bare Bash at startup. It adds
features and prompt work rather than accelerating script execution.

Matching typing stays below the 8 ms p95 budget in this workload. First Git
completion reaches 38.355 ms at p95; warm completion reaches 3.101 ms.
Bare Bash's incremental history search and Binit's history panel provide
different interfaces, so their search timings describe their respective
response costs. These local measurements do not qualify other terminals or
architectures.
