# Eligible completion

An eligible provider is an external worker behind an explicitly registered
Bash completion function. The function keeps Readline's normal quoting and
insertion path. Its native wait returns as soon as terminal input arrives,
leaving those bytes for Readline. The boundary costs a process per request and
requires providers to accept explicit context. Worker-local changes stay in
the worker; ordinary completion functions keep their calling-shell behavior.

A Readline input-wait hook cannot interrupt arbitrary synchronous Bash
completion functions, so only external workers qualify. Threads executing Bash
are not an option because they share interpreter state, and a forked shell
snapshot cannot carry shell mutations back to the caller.

The [contract](../benchmarks/eligible-completion.json) declares the bounds:
input-to-line-update interval, measured including termination and reaping, must
stay under 16 ms at the 95th percentile, with no lost input and no stale
insertion.

## Limits

A process group gives a cancellation target but does not bound descendants,
and a descendant can leave the group. An eligible worker must therefore be a
single process, enforced by the helper described below. Per-user process limits
would affect other shell jobs and are not used. Shell-wide resource settings
stay untouched.

Even SIGKILL cannot promise a hard deadline for a process blocked in
uninterruptible kernel I/O; the cancellation target is a p95 over the declared
input cases.

## Probe

`benchmarks/probes/eligible_wait.c` is a deliberately narrow probe. It starts
one known sleep process, polls the terminal without reading it, kills and
reaps its owned child, and returns through the ordinary completion function.
It exposes no product API. The probe covers four input cases: typed suffix,
bracketed paste, cursor movement, and coalesced Tab and suffix. A synchronous
control that waits out the full delay shows the cost of not cancelling.

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

## Qualified implementation boundary

The native request launches the Binit helper with an absolute executable
path supplied by the runtime. The runtime registration API must supply that
path itself. Accepting a caller-selected helper as a public option would let
it bypass the helper's limits.

The boundary begins before helper execution. The native launcher omits
`LD_*`, `DYLD_*`, `GLIBC_TUNABLES` and `GCONV_PATH` from the worker environment.
Loader code can create processes before a helper reaches its own entry
point, so those settings are omitted for the provider too. Other exported variables
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

The [kernel's seccomp documentation](https://www.kernel.org/doc/html/latest/userspace-api/seccomp_filter.html)
requires checking syscall architecture and enabling
`no_new_privs` before unprivileged filter installation. The helper does both.
The provider retains filesystem and network access as the current user.
This is an operational resource boundary for reviewed providers.

The native side accepts complete NUL-delimited output after successful exit,
with a 1 MiB total limit and at most 4,096 candidates of at most 4,096 bytes
each. It discards partial results on failure. Pending input, timeout or a
poll interruption cancels the owned process. Before updating `COMPREPLY`,
it rechecks the line, cursor and directory identity, along with the current
generation. Existing Readline quoting and menu insertion handle the result.

The implementation requires Linux `close_range` support and the GNU spawn
close-from operation. Unsupported platforms fail closed. Binit 0.1 supports
Linux x86_64. The [registration API](completion-providers.md) selects the
runtime's manager path. Completion context travels through a bounded
socketpair and becomes sealed anonymous stdin before provider exec; exported
`COMP_*` fields are excluded.

## Measure the public API

`scripts/bench_completion_provider.py` exercises `binit completions register`
in initialized shells. Each interpreter gets two complete 20-pair runs of
the four input cases. Pair order alternates between an isolated
provider and a synchronous control that performs the same 200 ms delay.
The control discards output; the isolated provider would return stale
candidates if allowed to finish. This measures input response, not useful
completion throughput.

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
