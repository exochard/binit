# Interactive performance

`make bench-interactive` compares bare Bash, an empty Binit configuration, and
seven explicitly selected development plugins. All scenarios use the same
Bash executable and vi editing mode. Prepare the editor dependencies through
`make demo` and install the development Python requirements first. Plugin
sources come from the directory in `BINIT_PLUGIN_ROOT`, default
`../binit-plugins`; the development plugin sources ship in the source release
archive.

```bash
make build
make bench-interactive
```

The raw results are written to `.cache/interactive-benchmark.json`. To compare a previous
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
[interpreter benchmark](benchmarks.md) has separate workloads and gate.

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

## Cursor-position probe

The terminal tests cover a cursor-position response delayed by 80 ms. The
focused picker honors its 120 ms probe deadline instead of aborting after one
50 ms idle poll. Cancelling the menu preserves the input; missing responses
take the bounded fallback path.
