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

Builds are experimental; building and testing an engine does not promote it.

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
The committed lock is part of the experiment. The calibration command is
`python3 scripts/benchmark.py freeze --engine .cache/engines/reference/bash`.
Recalibration creates a new
experiment; the runner refuses to overwrite
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
contains two cases. The promotion threshold is a ratio at or below 0.75 in all
three runs against each baseline. This means a 25% reduction in elapsed time.

At least three families must have ratios below 1 in every run. A family ratio
above 1.05 in two or more runs is a repeatable regression and fails the gate.
Baseline medians must remain at least 200 ms per case. Thirty samples per run
and three complete runs are mandatory. Build provenance must agree with the
measured binaries. Missing evidence leaves `release_gate_pass` false.

Use `--samples 3 --runs 1` for an initial trial. It cannot pass the
gate. Keep raw logs out of Git and publish a compact result with enough
provenance for another contributor to repeat it. Passing this timing gate
alone does not establish Bash compatibility.
`compare` exits with status 1 when the gate fails, including for a short trial.

## Profile

`python3 scripts/engine.py build profile` creates an unmodified `-pg` build
for GNU gprof. Run `engines/bash/profile.bash` with it from a scratch directory,
then inspect the resulting `gmon.out`. This diagnostic workload is separate
from the fixed timing suite. Instrumented elapsed times cannot qualify a
candidate.

## Status

The optional interpreter has not met the 25% gate, and no local Bash patch is
selected. The default execution engine remains the system Bash.
