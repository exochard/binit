# Performance program

Measure the Bash environment through its full lifecycle. Keep separate results
for plugin management, interactive response and interpreter execution. Each
workstream below has its own workload and engineering target; a faster result
in one cannot compensate for a regression in another.

The first cost is fixture coverage. Synthetic plugins isolate manager work,
while pinned ecosystem fixtures expose behavior that tiny plugins miss. Both
are needed. The existing seven-plugin terminal workload and sixteen-script
interpreter suite remain frozen.

## Scope and prior art

Zinit's performance mechanisms include deferred loading through Turbo,
tracked loading versus `light`, cached snippets and compilation. Its completion
administration and parallel updates cover costs outside shell startup.
`times -a` reports loading times and loading moments. These mechanisms define
useful comparison fields, rather than a single startup score. See the
[pinned Zinit README](https://github.com/zdharma-continuum/zinit/blob/d7b10fcab51ea034aa7df5d241604b3e6bf46449/README.md).

The upstream [plugin-manager benchmark](https://github.com/zdharma-continuum/pm-perf-test/blob/2d4c87c13bf4166cc7622ffa0e03fcd340ceb90f/README.md)
loads 28 Zsh plugins. Its documented substitutions for unsupported build hooks
show why fixture equivalence matters. Those results do not establish Binit's
relative speed. A future Zinit adapter needs equivalent plugin bodies, checked
effects and the same readiness boundaries. Each shell also needs its own bare
baseline. Retain tracking and verification when comparing supported defaults.

Binit already supports timed, event and watch triggers alongside command and
completion loading. Their existence does not establish responsiveness under
load. Bash source runs in the calling shell; a manager cannot safely preempt
an arbitrary state-changing plugin midway through its body. Scheduling work
must distinguish bookkeeping from the largest synchronous plugin interval.

## Workloads and engineering targets

The numeric targets are new optimization goals on recorded reference machines.
They are goals, not measured results.
Freeze each missing fixture before implementing its treatment. A field stays
open until its workload and evidence exist on the required configuration.

| Field | Required workload | Engineering target | Current coverage |
| --- | --- | --- | --- |
| Startup and readiness | Empty, seven real plugins, eager/deferred equivalents; record first visible prompt, first acknowledged input and all-required-plugin readiness | Seven-plugin added overhead: median ≤5 ms and p95 ≤10 ms against bare Bash | Existing terminal suite; complete readiness timeline pending |
| Deferred scheduling | 64 due plugins; dependency chains, timed/conditional/event/watch triggers; queued input and interrupts | Scheduler bookkeeping p95 ≤1 ms; pause between plugins when input is queued; zero lost keys | Functional checks exist; latency fixture pending |
| First-use activation | Small and 512-function plugins; actual command/completion dependencies; empty and Unicode arguments | Added first-command p95 ≤2 ms against direct source-and-call; warm commands have no repeatable >5% regression | New lifecycle suite for command triggers; provider/dependency fixtures pending |
| Steady-state hooks | 0/10/64 hooks; unchanged directory, directory change and large Git working trees | Added prompt/hook p95 ≤1 ms in the unchanged-directory case; expose plugin-owned external work separately | Existing command round trips; hook-count and repository-size matrix pending |
| Completion | Provider generation, menu open, filtering and insertion measured separately; 100/1,000/10,000 candidates; delayed provider | First Git p95 ≤10 ms; warm p95 ≤5 ms; cancel eligible isolated providers within 16 ms at p95 | Git terminal measurements exist; scalable and cancellable-provider fixtures pending |
| Editing and history | 1k/10k/100k history entries; duplicate/no-match cases; 80-byte/4-KiB/64-KiB lines; multiline paste, wide characters | Typing p95 ≤1 ms and p99 ≤8 ms in the declared interactive set; keep the existing ≤8 ms p95 limit | Existing 1k-history workload; stress matrix pending |
| Catalog and ownership scaling | 0/10/50/64 plugins, 4 functions each; one plugin with 32/512 functions; file/byte-size and dependency-depth axes | At 64 small plugins, activation p95 ≤75 ms; owned unload of 512 functions p95 ≤50 ms | New lifecycle suite; byte-size and dependency-depth axes pending |
| Acquisition and generations | Cached local preparation, apply, rollback and update preview; pinned Git/snippet/release fixtures with fixed transfer delay and jobs 1/4 | Local 64-plugin preparation p95 ≤500 ms; ≥2× acquisition throughput with 4 workers on the fixed delayed fixture | New local preparation timing; acquisition and transaction timings pending |
| Resources | Resident/peak memory, child processes and descriptors; 100 load/unload cycles; repeated completion and editing | Zero manager-spawned children in ordinary unchanged-prompt/typing paths; declare a retained-memory bound before optimization and rule out monotonic growth across 3 runs | Pending; elapsed timings do not measure resources |
| Recovery | Corrupt/missing cache, interrupted acquisition, failed source, rollback and terminal handoff | Recover the previous verified generation after every injected failure; metadata-only rollback p95 ≤250 ms on the fixed 64-plugin fixture | Functional recovery tests; timed recovery and terminal-handoff matrix pending |
| Diagnostics | Per-plugin load and hook costs, phase breakdown and an uninstrumented control | Attribute ≥95% of elapsed time in a ≥200 ms fixture without double counting; report observer cost separately | Basic `profile` exists; phase attribution pending |
| Interpreter execution | Existing eight families plus separate parser/expansion stress and real-script diagnostic corpora | Preserve the 25% reduction requirement in all three complete runs and every compatibility gate | Existing frozen suite has not met the target; broader diagnostic corpus pending |

The catalog limit is 64 plugins (`src/main.rs`). A 100/500-plugin experiment
requires a separate capacity decision with declared resource bounds. A larger
benchmark must not bypass that validation or silently raise the product limit.

## Executable lifecycle suite

The case definitions live in [lifecycle.json](../benchmarks/lifecycle.json).
They cover 0/10/50/64 eager plugins and lazy plugins with 32 or 512 functions.
`scripts/bench_lifecycle.py` measures:

- `prepare`: manager process launch through verified local generation publication.
- `activate`: initialization in an already-running interactive Bash through the next prompt.
- `first_command`: deferred source-and-call through the next prompt.
- `warm_commands`: 1,000 status-checked calls in the same shell through the next prompt.
- `unload`: owned removal through the next prompt.

These PTY round trips include prompt dispatch and the terminal observer. They
reuse the existing vi-mode terminal driver. `activate` excludes Bash process
startup, so it must not be presented as the existing usable-startup metric.
The fixed environment uses a private home and warm filesystem caches. Fresh
generation state is not a cold-page-cache measurement.

The direct-Bash control sources identical fixture bodies. It omits catalog
verification and ownership protection, so it is a cost reference rather than
a feature-equivalent competing manager. Its unload simply removes the known
fixture functions. Optimization claims use the optional previous Binit binary,
which must have the same native-module availability.

All timings are accepted only after behavior checks. The suite checks load
counts and definitions; lazy calls also verify empty/Unicode arguments, return
status 37, exports in a child process and a directory change in the calling
shell. Every warm call must retain its status, with a checked invocation count.
Unload must remove every fixture function while preserving an unrelated one.
A no-op initializer fails rather than earning a
shorter timing. Records include binary hashes, fixture identities and the plan
and driver digests, rechecked before completion along with the fixture files.
Summaries stay separate by run. Failure preserves an
incomplete receipt, and existing output files are never overwritten.

```bash
make build
make bench-lifecycle
python3 scripts/bench_lifecycle.py --compare-binary /path/to/previous/binit \
  --samples 20 --runs 2 --output .cache/lifecycle-comparison.json
```

`make bench-lifecycle` writes `.cache/lifecycle-benchmark.json`; use a new
output path for subsequent runs. A quick diagnostic can select cases:

```bash
python3 scripts/bench_lifecycle.py --cases eager-10 lazy-512 \
  --samples 3 --runs 1 --output .cache/lifecycle-screen.json
```

## Evidence and promotion

Every report names the feature configuration, architecture and interpreter.
Record native-enabled and native-disabled builds separately, using matching
Bash headers. Preserve all raw samples, including slower tails. Missing
fields are unmeasured, with no inferred pass.

Added overhead means the distribution of paired differences:
`Binit time - direct-control time` for the same run, case and sample. Take
the requested percentile of those differences; subtracting two independently
computed p95 values does not measure p95 overhead. Keep negative differences
visible. Before/after optimization ratios likewise pair the same sample IDs.

For manager/editor optimization, screen a frozen treatment against a preserved
binary before confirmation. Require at least 2% lower median paired elapsed
time in its named target in each of two 20-pair runs. Check other applicable
metrics for repeated regressions above 5%, including p95 when latency is the
target. Report tail behavior even when the treatment targets a median.
Absolute engineering goals need their own recorded results. Shared-host measurements
remain diagnostic; remote/ARM64 and terminal qualification remain separate.

Do not average workstreams into an overall score. First-prompt deferral cannot
earn an improvement if usable input or required feature readiness regresses.
Keep provider work distinct from menu rendering. Acquisition numbers use
controlled local fixtures, with no startup downloads. Instrumented profiles
explain a result; the uninstrumented run supplies its timing evidence.

Interpreter promotion retains its unchanged 30-pair × 3-run procedure and source
provenance requirements. Added diagnostic scripts do not replace failed cases
or enter PGO training. The new lifecycle runner always reports
`release_qualified: false`; completing its selected cases closes only that
measurement, not this entire program.

## Limits

The lifecycle suite does not yet measure scheduler latency, memory or
slow-provider cancellation. Each optimization needs a pinned ecosystem fixture
as well as its isolated case, because attractive synthetic numbers can hide
real plugins that still stall.

## Alternatives

| Approach | Prior art | Decisive test and cost | Decision |
| --- | --- | --- | --- |
| Extend only the seven-plugin startup table | Existing Binit interactive suite | One hour to show the 512-function unload cost and its before/after change | Retain as a regression workload; insufficient coverage alone |
| Separate lifecycle matrix with per-field budgets | Zinit tracking/light, scheduling and load-moment reports | One small public-CLI fixture must fail a no-op initializer and expose scaling; about two hours | Selected; adds fixture maintenance and separate timing boundaries |
| Start with a Zinit-versus-Binit ranking | Upstream 28-plugin performance test | Port and verify equivalent plugin effects and readiness; at least one day | Defer until equivalence is demonstrated; raw shell timings alone cannot answer it |
