# New runtime performance targets

Freeze two distinct operations against the preserved `c80d4df` executable:
on-demand Git shortcut activation, and accepted commands while a watched
toolchain becomes ready. The [contract](../benchmarks/runtime-workflows.json)
sets their effects and measurement boundaries before production treatment.
The [validation receipt](../benchmarks/results/runtime-workflow-driver-x86_64.json)
freezes the working driver and records its independent review. Neither target
has a qualified improvement yet. Production treatment has not started.

Untreated discovery found 22–28 ms first use with 64 aliases and 64 functions,
compared with about 1.3 ms for the functions alone. Twenty explicit dispatches
over eight watched paths took 108–112 ms. Catalog commands took about 3 ms.
Those observations favor the two runtime paths over small catalog savings.
They are diagnostic samples, separate from qualification.

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
20-pair runs. Keep every failed experiment and apply the existing preservation
bars, including latency tails. Startup and unload gains contribute nothing to
these targets. Instrumented diagnosis stays outside qualification timing.

The implementation should introduce no persistent worker or cache. Watch
metadata remains bounded by the existing 128-path limit. Native and fallback
behavior must pass matching Bash 5.2/5.3 checks; the
[milestone](modern-binit-milestone.md) retains its broader correctness and
independent-review requirements.

Run `scripts/bench_runtime_workflows.py` with explicit candidate, reference,
interpreter, pinned Git source, fresh work directory and output paths. Work
and raw receipts belong under `tmp/`. The driver checks exact definitions
against a direct-source control and retains each failed experiment.

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
workflows close directly from their checked empty prompt.

Eight focused tests pass in each native and fallback Bash 5.2/5.3 configuration.
Six deliberate behavior faults are rejected. The second identical-binary
control contains 160 checked observations across two complete 20-pair runs.
Its median and p95 comparisons remain within 5% in each run. The first control
retains single-run increases, including the watch p95 increase; none repeat
in the same field.

The full control used the retained driver version in the receipt. The final
version adds a read-only native-status check before timing and retains closing
terminal output afterward. Independent AST comparison confirms unchanged
timed actions, fixtures and golden controls. A short final-version control
passes all four observations. Primary workload eligibility requires active
native completion on Bash 5.3; fallback checks establish behavior separately.
The driver's numerical target flag covers these workloads only. Full product
qualification still requires the milestone's preservation and review gates.
