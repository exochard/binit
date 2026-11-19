# Runtime workflow performance

Two runtime operations have their own measurements: on-demand Git shortcut
activation, and accepted commands while a watched toolchain becomes ready.
The [workflow contract](../benchmarks/runtime-workflows.json) sets their
effects and measurement boundaries. Both met their target: the median paired
ratio against the reference was 0.14 for the Git shortcut and 0.33 for the
watched toolchain, in each of two complete 20-pair runs.

The shortcut workload uses a pinned Git alias file and real Git behavior.
Declared metadata helpers provide the original framework's descriptive calls.
The measured endpoint includes representative alias and function use after
activation, so deferred work cannot escape the interval. Its benefit occurs
when shortcuts become available; subsequent Git execution has a separate cost.

The watch workload accepts 20 commands. A marker created by command 11 must
cause activation at the normal prompt before command 12. That command checks
the ready function's arguments and calling-shell effects. Another file change
must leave the source count at one. Suspending observation cannot pass these
effects.

Each workload needs at least 25% lower paired elapsed time in both complete
20-pair runs against a preserved reference executable. Failed experiments are
kept and latency tails are checked. Startup and unload gains contribute nothing
to these targets. Instrumented diagnosis stays outside qualification timing.

A treatment should introduce no persistent worker or cache. Native and
fallback behavior are described in [architecture](architecture.md) and must
pass matching Bash 5.2 and 5.3 checks.

## Driver

Run `scripts/bench_runtime_workflows.py` with explicit candidate, reference,
interpreter, pinned Git source, fresh work directory and output paths. Work
and raw output belong under `tmp/`. The driver checks exact definitions
against a direct-source control and retains each failed experiment.

```bash
python3 scripts/bench_runtime_workflows.py --candidate /path/to/candidate \
  --reference /path/to/reference --git-source /path/to/git-source \
  --work tmp/workflows --output tmp/workflows.json
```

Watch commands retain their exact mutation schedule. A prompt hook placed
after Binit dispatch reports source count at every command boundary; command
11 must report one before command 12 is submitted. A separate direct-source
control checks the same arguments and caller effects. Full caller-state
audits run outside the 20-command interval. Every paired observation requires
matching normalized fixture identities, followed by a run-end input audit.

Shell teardown follows the timed interval. Every accepted observation retains
closing terminal output and requires exit status zero, without forced
termination or truncated output. A negative control completes the workflow
and then exits with status 37; that observation must be rejected. Successful
workflows close directly from their checked empty prompt. Primary workload
eligibility requires active native completion on Bash 5.3; fallback checks
establish behavior separately.
