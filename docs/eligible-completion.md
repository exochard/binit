# Eligible completion experiment

Test an external worker behind an explicitly registered Bash completion
function. The function keeps Readline's normal quoting and insertion path.
Its native wait can return as soon as terminal input arrives, leaving those
bytes for Readline. This boundary costs a process per request and requires
providers to accept explicit context. Worker-local changes stay in the worker.

The frozen [experiment](../benchmarks/eligible-completion.json) measures the
input-to-line-update interval, including termination and reaping. The probe
establishes a usable boundary. The implementation below adds output parsing,
resource enforcement and public registration; production qualification is
recorded separately. Ordinary completion functions retain their calling-shell
behavior.

## Architecture choice

The defaults were an external command, a fork of the current shell, and a
long-lived helper. Ten constraints widened the search: explicit context,
inherited functions, no process, existing editor hooks, cooperative providers,
precomputed results, no Bash changes, a separate editor, persistent caching,
and interruptible Bash evaluation. Duplicate worker variants were combined.
Threads executing Bash were rejected because they share interpreter state.
Private interpreter changes remain outside this experiment's authority.

| Mechanism | Difference and prior art | Cheapest rejecting test | Estimated effort |
| --- | --- | --- | --- |
| External command, default | A completion function waits on input and an owned process. Existing command providers need an interruptible wait. | Real PTY cancellation exceeds 16 ms p95 or loses input. | 1 hour |
| Forked shell snapshot | Reuses functions but cannot transfer shell mutations. [zsh-async](https://github.com/mafredri/zsh-async/blob/ee1d11b68c38dec24c22b1c51a45e8a815a79756/README.md) documents a separate forked environment. This mechanism exists. | A provider relying on a caller mutation behaves differently. Already excluded for ordinary providers. | 15 minutes |
| Persistent helper | Amortizes startup at the cost of stale state and another lifetime. zsh-async workers demonstrate the mechanism. | Change provider configuration between requests and inspect the next result. | 1 hour |
| Cooperative polling | Providers explicitly yield when input arrives. [ble.sh](https://github.com/akinomyoga/ble.sh/blob/152942758f567e51fd17bdc7b3783d27b3c476aa/lib/core-complete.sh#L1205) checks input during completion work. | A blocking external command prevents the next poll. | 15 minutes |
| Precomputed index | Moves work before Tab, with an invalidation contract. Partial substitute for providers whose inputs can be indexed. | Change a dynamic input after indexing and complete it. | 1 hour |

The external-command probe is the next experiment. It preserves the existing
editor boundary and has a direct failure criterion. The earlier
[feasibility result](completion-feasibility.md) already rules out a
Readline wait hook interrupting arbitrary synchronous Bash providers.
This comparison establishes mechanisms, not relative product performance.

## Limits that must be resolved

A process group gives a cancellation target. It does not impose a descendant
count, and a descendant can leave that group. A production design must either
enforce an eligible worker's no-descendant contract or provide a separately
qualified containment mechanism. Per-user process limits would affect other
shell jobs and are unsuitable for this boundary.

The proposed byte and time bounds are in the frozen experiment. Address-space
and descriptor bounds belong to the child. Shell-wide resource settings must
remain untouched. Even SIGKILL cannot promise a hard deadline for a process
blocked in uninterruptible kernel I/O; the measured cancellation target is a
p95 over the declared corpus.

The probe is deliberately narrow. It starts one known sleep process, polls
the terminal without reading it, kills and reaps its owned child, and returns
through the ordinary completion function. It does not expose a product API.

## Feasibility result, 2026-11-14

Both matching interpreters pass the four input cases, with 40 samples per
case and treatment. The 320 probe cancellations preserve every tested line
and cursor position. Each has a successful-spawn acknowledgment followed by
matching kill and reap receipts for the owned child. The synchronous controls
retain their expected delay. Raw [Bash 5.2](../benchmarks/results/eligible-completion-5.2.json)
and [Bash 5.3](../benchmarks/results/eligible-completion-5.3.json) records bind
the interpreter, modules and worker executable to their hashes.

| Input case | Bash 5.2 probe p95 | Bash 5.3 probe p95 |
| --- | --- | --- |
| Typed suffix | 0.193 ms | 0.175 ms |
| Bracketed paste | 0.175 ms | 0.165 ms |
| Cursor movement | 0.164 ms | 0.152 ms |
| Coalesced Tab and suffix | 0.214 ms | 0.254 ms |

Values are rounded to three decimal places. Control p95 values range from
195.771 to 200.938 ms. Measurement ends when the Readline capture binding has
written the line and cursor, after provider return and child cleanup. It
includes the capture binding's dispatch cost and file observation. Neither
treatment supplies candidates; stale candidate insertion remains untested.
These synthetic delays do not count toward the two new workload improvements.

Independent review found that the initial readiness marker preceded worker
creation, so failed launches could appear responsive. The
[earlier 5.2 run](../benchmarks/results/eligible-completion-pre-ack-5.2.json)
and [earlier 5.3 run](../benchmarks/results/eligible-completion-pre-ack-5.3.json)
are retained as insufficient evidence. A separate
[initial smoke run](../benchmarks/results/eligible-completion-harness-error.json)
exposes a wrong expected paste cursor position and incomplete line clearing
between cursor-movement cases. The corrected harness clears the entire line.

The strengthened measurement rejects an
[immediately exiting worker](../benchmarks/results/eligible-completion-negative-fast.json).
A [non-executable worker](../benchmarks/results/eligible-completion-negative-spawn.json)
leaves an incomplete receipt with its error and terminal evidence. Existing
output files are refused. A second independent review accepted these
corrections within the synthetic-worker scope.

The prototype builds with warnings treated as errors against both header
sets. GCC's analyzer and Clang's analyzer pass. Clang-tidy did not run because
this probe has no compilation database; cppcheck is unavailable. Sanitizers
did not run because the probe has no standalone instrumented test target.
Full native/fallback product gates remain required when implementation lands.

## Reproduce the probe

Use the dependencies in [CONTRIBUTING.md](../CONTRIBUTING.md). Set
`BINIT_BASH`, `BINIT_BASH_INCLUDE` and `BINIT_READLINE_INCLUDE` to matching
interpreter/header paths. `BINIT_TEST_MODULE` must name the matching native
menu module built by `scripts/verify.bash`. Repeat with each interpreter.
The output file must be new.

```bash
mkdir -p tmp/eligible-completion
cc -std=c17 -O2 -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Werror \
  -fPIC -shared -fvisibility=hidden \
  -I"$BINIT_BASH_INCLUDE" -I"$BINIT_BASH_INCLUDE/include" \
  -I"$BINIT_BASH_INCLUDE/builtins" -I"$BINIT_READLINE_INCLUDE" \
  benchmarks/probes/eligible_wait.c -o tmp/eligible-completion/probe.so
python3 scripts/probe_eligible_completion.py \
  --bash "$BINIT_BASH" --menu-module "$BINIT_TEST_MODULE" \
  --probe-module tmp/eligible-completion/probe.so --worker "$(command -v sleep)" \
  --work tmp/eligible-completion/run --output tmp/eligible-completion/receipt.json
```

## Implementation boundary under qualification

The native request launches the Binit helper with an absolute executable
path supplied by the runtime. The runtime registration API must supply that
path itself. Accepting a caller-selected helper as a public option would let
it bypass the helper's limits.

The boundary begins before helper execution. The native launcher omits
`LD_*`, `DYLD_*`, `GLIBC_TUNABLES` and `GCONV_PATH` from the worker environment.
An independently reviewed constructor regression demonstrates why: loader
code can create processes before a helper reaches its own entry point.
Those settings are omitted for the provider too. Other exported variables
are included within the combined 64 KiB context budget. The interactive
shell's environment remains intact.

The helper applies a 256 MiB address-space limit, two CPU seconds and 32 open
descriptors. Existing lower limits remain lower. Core dumps are disabled.
It closes inherited descriptors above stderr and arranges SIGKILL when the
expected parent exits. A Linux syscall filter blocks process and thread
creation, including alternate x86 syscall conventions. Providers cannot
replace the resource limits or clear the parent-exit signal. This makes
single-process executables eligible; programs that require subprocesses or
threads need their ordinary execution path.

The [kernel's seccomp documentation](https://www.kernel.org/doc/html/latest/userspace-api/seccomp_filter.html),
checked on 2026-11-14, requires checking syscall architecture and enabling
`no_new_privs` before unprivileged filter installation. The helper does both.
The provider retains filesystem and network access as the current user.
This is an operational resource boundary for reviewed providers.

The native side accepts complete NUL-delimited output after successful exit,
with a 1 MiB total limit and at most 4,096 candidates of at most 4,096 bytes
each. It discards partial results on failure. Pending input, timeout or a
poll interruption cancels the owned process. Before updating `COMPREPLY`,
it rechecks the line, cursor and directory identity, along with the current
generation. Existing Readline quoting and menu insertion handle the result.

The implementation currently requires Linux `close_range` support and the
GNU spawn close-from operation. Unsupported platforms fail closed. ARM64
execution evidence remains open. The private helper and request dispatch
are under qualification. The [registration API](completion-providers.md)
now selects the runtime's manager path. Completion context travels through a
bounded socketpair and becomes sealed anonymous stdin before provider exec;
exported `COMP_*` fields are excluded. Full matching native/fallback gates
and quantitative cancellation measurements must pass before this capability
lands as a qualified product feature.

## Measure the public API

`scripts/bench_completion_provider.py` exercises `binit completions register`
in initialized shells. Each interpreter gets two complete 20-pair runs of
the four frozen input cases. Pair order alternates between an isolated
provider and a synchronous control that performs the same 200 ms delay.
The control discards output; the isolated provider would return stale
candidates if allowed to finish. This measures input response, not useful
completion throughput or either new 25% workload target.

Each run first requires a successful public completion. For ordinary input
cases, timing starts only after the provider acknowledges its PID and the
controller verifies it is Bash's sole direct child. Coalesced Tab and suffix
can prevent launch; those rows establish input handling and idle cleanup,
not termination of an acknowledged worker. Every row records the resulting
line and cursor, child cleanup and parent descriptor count. Active workers
also report their resource limits. The controller checks artifact identities
before and after each run and refuses optimized Python execution, which
would remove its qualification assertions.

Use the matching release binary and native module from verification:

```bash
python3 scripts/bench_completion_provider.py --bash /path/to/bash \
  --binary target/release/binit --module target/native-check/binit_complete.so \
  --work tmp/provider-measurement --output tmp/provider-measurement.json
```

The output must be new. Incomplete runs retain their records and failure.
Smaller `--pairs` or `--runs` values are smoke checks and carry an explicit
unqualified sample-count field. Apply the existing 16 ms p95 response limit,
zero lost input or stale insertion, and resource bounds to each full run.
