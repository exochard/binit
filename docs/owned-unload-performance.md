# Owned unload and generation preparation

Native owned unload is about 99% lower on the frozen 512-function workload.
Generation preparation also improves: paired elapsed time falls about 63%
for ten eager plugins and 72% for fifty or sixty-four. These results satisfy
the local lifecycle goal on x86_64 with stock GNU Bash 5.3.20. The
[receipt](../benchmarks/results/owned-unload-x86_64.json) contains every final
sample, along with the earlier failed treatments.

## Measurement

The unchanged lifecycle runner compares commit `ed38790` with the preserved
`3005de3` binary. Both have native completion enabled. Each of two runs has
twenty paired samples across all six frozen cases, producing 2,280 validated
records. Timings include prompt dispatch in a real Bash PTY. Preparation
includes manager launch and verified generation publication; caches are warm.

The host is shared. Case and variant order alternate under the existing
runner, and all qualifying samples execute in the same host context. Separate
sandbox diagnostics are retained as diagnostics. A twenty-sample p95 is the
nineteenth sorted value, so it has limited precision.

Each cell below lists run 1 / run 2 in milliseconds. Percentage reductions
come from the median of paired ratios, which can differ from a ratio of the
two marginal medians.

## Unload

| Workload | Before median | After median | After p95 | Paired reduction |
| --- | --- | --- | --- | --- |
| 10 eager plugins, 40 functions | 23.57 / 23.05 | 3.59 / 3.51 | 4.25 / 3.59 | 84.6% / 84.8% |
| 50 eager plugins, 200 functions | 155.59 / 153.49 | 36.03 / 35.84 | 39.68 / 36.96 | 76.8% / 76.5% |
| 64 eager plugins, 256 functions | 219.82 / 210.56 | 54.46 / 54.17 | 57.16 / 57.31 | 75.0% / 74.4% |
| One lazy plugin, 32 functions | 14.88 / 14.44 | 0.66 / 0.64 | 0.73 / 0.69 | 95.6% / 95.6% |
| One lazy plugin, 512 functions | 243.98 / 225.37 | 2.85 / 2.83 | 3.05 / 2.92 | 98.8% / 98.7% |

The target requires at least 75% lower paired median and p95 at most 50 ms
for the single 512-function plugin in each run. Both pass. The 64-plugin
case remains above 50 ms; per-plugin bookkeeping still matters there.

## Preparation

| Workload | Before median | After median | Before p95 | After p95 | Paired reduction |
| --- | --- | --- | --- | --- | --- |
| Empty | 15.98 / 15.61 | 13.67 / 13.38 | 19.89 / 16.08 | 16.19 / 14.45 | 14.1% / 14.5% |
| 10 eager plugins | 64.10 / 62.89 | 23.53 / 23.28 | 92.87 / 75.89 | 30.79 / 24.24 | 63.5% / 63.2% |
| 50 eager plugins | 263.02 / 256.44 | 79.89 / 71.68 | 348.92 / 318.24 | 103.68 / 104.81 | 71.8% / 71.7% |
| 64 eager plugins | 355.78 / 332.52 | 99.15 / 98.32 | 442.19 / 408.93 | 135.45 / 112.73 | 72.1% / 72.1% |
| Lazy, 32 functions | 22.40 / 20.46 | 13.85 / 13.40 | 27.96 / 21.99 | 17.51 / 15.91 | 34.6% / 34.4% |
| Lazy, 512 functions | 21.95 / 20.67 | 13.97 / 15.63 | 28.51 / 24.00 | 19.84 / 22.57 | 35.3% / 29.4% |

## Implementation and rejected treatments

Unload checks all owned functions in the calling shell through Bash's own
serializer. It preserves the original Bash fallback and completes the other
ownership checks before removal. Generation publication overlaps file syncs
with at most eight writers. Every file keeps its original descriptor through
sync; metadata follows successful joins. Verification still precedes selection
of the generation. A traced fixture produces identical generation contents and
file modes, retaining all seven file syncs.

The unload-only treatment exposed repeated preparation and activation p95
warnings. Four writers improved preparation medians but retained the
512-function preparation tail warning. Traces located the delay in file syncs;
slow samples clustered after the reference binary's long unload. Separating
writes from syncs did not remove it and was reverted. Stripping release debug
sections also failed to improve preparation and was reverted.

The final eight-writer comparison has no repeated regression above 5% in any
other paired median or p95. The first run's ten-plugin activation p95 rises
21%; it does not repeat in run 2. All samples remain in the receipt. Activation
and warm-command performance have no established improvement.

## Correctness and limits

Full verification passes 239 Python tests without skips, 148 Rust tests,
three native model tests and sixteen terminal smoke checks. Native-disabled
verification passes its full gate, with six expected native-only skips.
Matching Bash 5.2 passes 38 native checks and the fallback unload/recovery
checks. The changed C source also passes Clang-Tidy and actual ASan/UBSan
effects tests with leak detection enabled; cppcheck is unavailable.

Failure tests cover conservative unload refusal and preservation of the
selected generation after a write failure. Thread-spawn failure, worker panic
and filesystem sync errors were reviewed but not fault-injected. Per-file
syncs are preserved; power-loss persistence of directory entries remains
unqualified. ARM64 measurements and universal completion responsiveness remain
open. The optional interpreter's existing 25% release gate is unchanged.
