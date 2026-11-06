# Bash execution benchmarks

The suite measures unchanged Bash scripts. It covers eight families with two
scripts each: arithmetic, strings, arrays, function dispatch, pattern
matching, filesystem work, pipelines, and command substitution. Timing
includes interpreter startup and external processes started by the script.
Every timed process receives the same fixed environment and fixture data.

## Build the interpreters

Requirements: Python 3.12+, GCC with LTO and PGO support, GNU Make, binutils,
`patch`, `gpgv`, util-linux `script`, glibc locale sources, and the usual Bash
build dependencies.
Source URLs and SHA-256 hashes
are pinned in `engines/bash/source.json`. Downloads must also pass GNU
signature verification with the recorded signer. The patched source tree is
also checked against `engines/bash/tree.sha256` before each build, including
when it is cached. Sources and build outputs
stay in `.cache/engines/`.

```bash
python3 scripts/engine.py build reference
python3 scripts/engine.py build pgo
python3 scripts/engine.py test reference
python3 scripts/engine.py test pgo
```

Run builds under suitable resource limits. The reference uses `-O2 -g`;
the candidate uses `-O3 -g -flto=2` and profile feedback. Both use the same
source version and configure options, including `--without-bash-malloc`.
Neither is installed. Profiles come from `engines/bash/train.bash`, which
does not run validation scripts.

Use a separate name to compare compiler variants without replacing an
existing build:

```bash
python3 scripts/engine.py build pgo --name pgo-o2 --optimization O2
python3 scripts/engine.py test pgo --name pgo-o2
```

`--optimization` accepts `O2` or `O3` for LTO and PGO. The default remains
`O3`; reference and profiling builds retain their fixed flags. Every build
requires a fresh directory, including after an interrupted build. Choose a
new name for a retry. Existing directories and directory symlinks are rejected.

Upstream tests run with a private home and an idle terminal. Required locales
are generated under the cache. A test-only `locale` wrapper adds those private
names to `locale -a`, allowing upstream checks to discover them through
`LOCPATH`. Host locale configuration stays untouched. The test command fails
on diff output even though upstream's `run-all` normally exits zero.

Calibration chooses a count for each script, targeting 300 ms and confirming
at least 200 ms in repeated baseline executions. `benchmarks/suite.lock.json`
freezes those counts, expected outputs, and script hashes before optimization.
The committed lock is part of the experiment. The initial calibration used
`python3 scripts/benchmark.py freeze --engine .cache/engines/reference/bash`.
Recalibration creates a new
experiment and requires a reviewed change; the runner refuses to overwrite
it. On a different machine, retain the old lock before generating a new one.

## Run and compare

Stop competing CPU work and use a consistent power mode before measurement.
The complete run takes several minutes:

```bash
python3 scripts/benchmark.py run \
  --engine reference=.cache/engines/reference/bash \
  --engine candidate=.cache/engines/pgo/bash \
  --engine system=/bin/bash \
  --output .cache/engines/results.json
python3 scripts/benchmark.py compare .cache/engines/results.json
```

Each run shuffles case order and engine order with a recorded deterministic
schedule. Each sample executes every case on every engine. The runner checks
stdout and stderr hashes, exit status, and the resulting work-directory tree.
Input fixtures are checked after each engine invocation. A mismatch stops the
experiment. Timing excludes fixture preparation and output verification.
The candidate must retain the reference's dynamic exports for loadable
builtins. Result files include raw durations and interpreter digests; partial runs
remain available for diagnosis.

For each case and run, take the median of paired candidate/baseline elapsed
time ratios. Each case has equal weight in the geometric mean; each family
contains two cases. The release threshold is a ratio at or below 0.75 in all
three runs against each baseline. This means a 25% reduction in elapsed time.

At least three families must have ratios below 1 in every run. A family ratio
above 1.05 in two or more runs is a repeatable regression and fails the gate.
Baseline medians must remain at least 200 ms per case. Thirty samples per run
and three complete runs are mandatory. Build provenance must agree with the
measured binaries. Missing evidence leaves `release_gate_pass` false.

Use `--samples 3 --runs 1` for an initial trial. It cannot pass the release
gate. Keep raw logs out of Git and publish a compact result with enough
provenance for another contributor to repeat it. Passing this timing gate
alone does not establish Bash compatibility or satisfy the release plan.
`compare` exits with status 1 when the gate fails, including for a short trial.

## Profile

`python3 scripts/engine.py build profile` creates an unmodified `-pg` build
for GNU gprof. Run `engines/bash/profile.bash` with it from a scratch directory,
then inspect the resulting `gmon.out`. This diagnostic workload is separate
from the fixed timing suite. Instrumented elapsed times cannot qualify a
release candidate.

## Initial PGO trial

The 2026-11-06 x86_64 trial used three paired samples in one run on a Ryzen 7
9800X3D, with GCC 16.2.1 and glibc 2.44. The geometric mean elapsed-time ratio
was 0.8747 against the unmodified reference and 0.8599 against installed Bash.
That corresponds to reductions of 12.5% and 14.0% respectively. The required
ratio is 0.75. This trial does not qualify the candidate.

| Family | Time reduction against reference |
| --- | ---: |
| Arithmetic | 21.9% |
| Strings | 22.4% |
| Arrays | 18.6% |
| Functions | 19.0% |
| Patterns | -5.1% |
| Filesystem | 12.8% |
| Pipelines | 0.7% |
| Command substitution | 5.7% |

The [receipt](../benchmarks/results/pgo-x86_64.json) includes raw durations,
binary digests, and the fixed-suite digest. A negative reduction means the
candidate took longer. Three samples cannot establish a repeatable regression;
the pattern result needs further investigation. Upstream compatibility tests
also report differences, including differences in the unmodified reference.
Their causes must be resolved before distributing an engine.

## Further local experiments

Four short trials on the same machine screened changes to name hashing and
compiler optimization. Each used three pairs in one run and the unchanged
validation suite. A candidate needed a paired elapsed-time ratio at or below
0.98 against its incremental baseline before further confirmation. This
screen is separate from the 0.75 release requirement.

| Treatment | Incremental baseline | Paired ratio | Screen |
| --- | --- | ---: | --- |
| Reuse a name hash across variable contexts, O2 | Unmodified O2 | 0.9925 | Failed |
| Unroll the hash loop, O2 | Unmodified O2 | 1.0036 | Failed |
| Use multiplication for the equivalent hash step, O2 | Unmodified O2 | 0.9818 | Failed |
| Hash multiplication with PGO | Unmodified O3 PGO | 0.9872 | Failed |
| O2 PGO | Unmodified O3 PGO | 1.0211 | Failed |
| Compare short keys inline, with hash multiplication and PGO | Unmodified O3 PGO | 0.9839 | Failed |

No local C patch was selected. The original PGO recipe remains the default.
The [diagnostic receipt](../benchmarks/results/engine-trials-x86_64.json)
contains binary and source digests, per-family ratios and raw durations.
Experimental patches remain local. The receipt records exploratory
measurements; accepted binaries still require the full release procedure.

The early O2 trials used different CPU affinity from the later PGO trials.
Compare treatments within each trial. These small samples can change the
ranking between baselines. Compute paired ratios directly from samples.
Dividing separately summarized ratios can change the ranking.

The multiplication and short-key hash checks passed comparison with the
original implementation on 50,000 generated byte strings, plus hash-table
operations. The short-key check also covered actual hash collisions and
early NULs under ASan/UBSan. The O2 PGO and multiplication/PGO builds retained
the reference's dynamic exports. Their upstream tests reported the same
three difference groups as the unmodified reference: help formatting, trap
listing order and out-of-range printf precision. They remain compatibility
failures. The rejected short-key candidate did not receive the full upstream
suite.

## Research-led allocation screens

The [performance research](performance-research.md) ranks five candidates
against primary paper records and the pinned GNU source. Two isolated O2
allocation treatments were tested on the unchanged suite. In-place quote
removal reached a paired ratio of 0.9838 against reference; guarded transfer
of assignment results reached 1.0075. Both missed the fixed 0.98 screen.
Neither patch is selected. The
[receipt](../benchmarks/results/expansion-trials-x86_64.json) preserves raw
paired durations, source/patch provenance and semantic check digests.

`scripts/engine_trial.py` reproduces these experimental builds from tracked
patches and plans in `engines/bash/experiments/`. It checks the authenticated
base and the complete patched tree, then marks the result as an experiment.
These builds cannot qualify for release. The default source lock, compiler
training and validation counts remain unchanged.
