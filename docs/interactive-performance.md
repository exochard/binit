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

Function recording runs as one native operation per plugin. It compares the
before snapshot with current functions and existing ownership, then stores
definitions using Bash's serializer. The original Bash loop remains the fallback.
The native-module presence check uses the builtin's status directly; it starts
no child shell and bypasses a function that shadows the builtin's name.

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

## Editor repair measurement, 2026-11-13

The [repair receipt](../benchmarks/results/interactive-editor-repair-x86_64.json)
records 20 alternating rounds after the wrapped-input and suggestion repairs,
including the fixes in `8197f99`. The workload is unchanged. Each configuration
has 220 matching character samples and 320 no-match samples.

| Metric | Bare Bash | Empty Binit | Seven plugins |
| --- | ---: | ---: | ---: |
| Usable startup, median | 10.862 ms | 14.638 ms | 23.111 ms |
| Matching typing, p95 | 0.059 ms | 0.049 ms | 0.926 ms |
| No-match typing, p95 | 0.064 ms | 0.049 ms | 0.149 ms |
| First history search, median | 0.077 ms | 0.076 ms | 1.930 ms |
| Unique file completion, median | 2.134 ms | 2.152 ms | 2.371 ms |
| First Git completion, median | — | — | 31.610 ms |
| Warm Git completion, median | — | — | 2.626 ms |

Matching typing remains below the 8 ms p95 budget in this workload. First Git
completion reaches 36.004 ms at p95; warm completion reaches 2.904 ms. This
run compares the repaired binary with bare Bash and an empty configuration.
It does not establish a paired change against the earlier binary. The same
shared-host and terminal-emulator limits apply.

## Function-recording measurement, 2026-11-13

The [overhead receipt](../benchmarks/results/interactive-overhead-x86_64.json)
contains two confirmation runs of 20 alternating rounds against `870f809`.
The workload, plugin sources and interpreter are unchanged. Each run has
220 matching character samples and 320 no-match samples per configuration.
Binary hashes and final source hashes identify the treatment.

| Metric | Run 1 before | Run 1 after | Run 2 before | Run 2 after |
| --- | ---: | ---: | ---: | ---: |
| Usable startup, median | 23.841 ms | 20.024 ms | 22.444 ms | 18.846 ms |
| Matching typing, p95 | 0.731 ms | 1.032 ms | 1.082 ms | 1.074 ms |
| No-match typing, p95 | 0.137 ms | 0.165 ms | 0.166 ms | 0.142 ms |
| First history search, median | 2.014 ms | 1.963 ms | 1.960 ms | 1.986 ms |
| Unique file completion, median | 2.340 ms | 2.353 ms | 2.234 ms | 2.280 ms |
| First Git completion, median | 31.997 ms | 6.530 ms | 31.531 ms | 6.278 ms |
| First Git completion, p95 | 42.930 ms | 10.271 ms | 53.966 ms | 8.351 ms |
| Warm Git completion, median | 2.695 ms | 2.635 ms | 2.418 ms | 2.327 ms |
| Builtin loop round trip, median | 7.117 ms | 7.318 ms | 7.211 ms | 7.271 ms |
| External command round trip, median | 5.507 ms | 5.673 ms | 5.197 ms | 5.209 ms |

Median paired startup ratios are 0.8364 and 0.8394: reductions of 16.4% and
16.1%. First Git completion ratios are 0.2015 and 0.2000, about 80% lower.
No metric has a median paired regression above 5% in both runs. Matching
typing has paired ratios of 1.0129 and 0.9872. Its p95 worsens in the first
run and improves slightly in the second; these measurements establish no
typing improvement.

Bare Bash startup medians are 11.260 and 11.117 ms; empty Binit reaches
14.637 and 14.106 ms. The seven-plugin configuration therefore still adds
about 7.7–8.8 ms over bare Bash. These figures describe feature overhead
with the same interpreter. The separate
[interpreter trials](overhead-experiments.md) did not pass their screen.

The terminal tests also cover a cursor-position response delayed by 80 ms.
The focused picker now honors its existing 120 ms probe deadline instead of
aborting after one 50 ms idle poll. Cancelling the menu preserves the input;
missing responses still take the bounded fallback path.
