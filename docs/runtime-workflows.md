# New runtime performance targets

Freeze two distinct operations against the preserved `c80d4df` executable:
on-demand Git shortcut activation, and accepted commands while a watched
toolchain becomes ready. The [contract](../benchmarks/runtime-workflows.json)
sets their effects and measurement boundaries before production treatment.
Its validation driver remains to be implemented and frozen. Neither target
has a qualified improvement yet.

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
