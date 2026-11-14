# Causal diagnostics

Implementation contract, frozen before the diagnostic treatment.

Add `binit diagnose PLAN [--json]` for reviewed fixtures against the selected
generation. Reuse the candidate check runner's private home, copied generation
and input bindings. Keep the selected generation and candidate record intact.
This is an explicit reproduction; it does not replay private history or
attach to an existing shell. `binit profile` and `binit report` stay supported.

The report must identify slow source locations and function calls across
plugin loading, lifecycle callbacks and Bash function completion providers.
It must show exclusive time separately from inclusive time. A caller's total
includes its children; those totals cannot be added together.

## Mechanism and alternatives

| Approach | Prior art and difference | Rejection test | Decision |
| --- | --- | --- | --- |
| Explicit spans at owned runtime calls | [OpenTelemetry spans](https://opentelemetry.io/docs/concepts/signals/traces/) provide parent/child structure. Runtime load/hook seams already exist. | Miss a slow function completion provider delegated to Bash. | Insufficient alone: observing provider internals would need additional adapters. |
| Filtered shell trace in the fixture child | [Bash xtrace](https://www.gnu.org/software/bash/manual/html_node/The-Set-Builtin.html) and [bashprof](https://github.com/sstephenson/bashprof/tree/79f7e692191c2db98ce52bee9b8e7b96684af3e7) already support shell profiling. Binit adds generation-bound fixtures and an argument-discarding collector. | Lose provider effects, retain argument text, exceed declared bounds or misattribute nested work. | Selected for implementation. A real-PTY probe preserves provider effects and identifies a 250 ms provider wait. |
| Repeated runs with inputs removed | [Delta debugging](https://www.debuggingbook.org/html/DeltaDebugger.html) reduces failure-inducing inputs. Applying it to plugin sets would test interactions. | Dependency removal changes the workload instead of isolating a cause. | Deferred: useful for later counterfactual checks, with higher execution cost. |

These mechanisms are established prior art. The architecture choice is an
engineering judgment. The initial probe is feasibility evidence, not product
qualification. Its provider changes globals and the working directory; it
also supplies completion results and returns nonzero. Tracing preserves those
effects and the cancelled input line.

## Capture contract

Only the explicit fixture child enables xtrace. Normal shell startup and
completion acquire no tracing wrappers or global DEBUG/RETURN traps. The
collector receives a dedicated inherited descriptor. It retains metadata
before the final tab. A bounded streaming classifier matches fixed tracing
control words after that tab, discarding each command byte without retaining
argument text. Observed xtrace disabling, PS4 or BASH_XTRACEFD changes and
`exec` make the capture incomplete; their following intervals stay opaque.
`exec` is conservative because its redirections can close the trace descriptor.
Never write a raw xtrace stream to disk.

The PS4 prefix starts with `+__BINIT_DIAG__` and has these tab-separated fields:

```text
timestamp_us pid subshell_depth source_depth function source line phase
```

Use `${EPOCHREALTIME/./}`, `${BASHPID}`, `${BASH_SUBSHELL}` and
`${#BASH_SOURCE[@]}`. Source depth includes sourced files even outside a
function. Bash leaves `FUNCNAME` empty there, then exposes the pending source
frames when a function starts; its length cannot measure contiguous ancestry.
Emit only `${FUNCNAME[0]-}`; joining the stack would depend
on the fixture's IFS. `source` comes from `${BASH_SOURCE[0]-}`. `phase` is
`setup`, `init`, `check` or `metadata`. The command text follows the final tab.
Repeated leading plus signs are allowed. None of these fields contains
argument values or a captured command line.

Bound each sample to 8,192 retained records, 4 KiB per metadata prefix,
64 MiB of incoming trace bytes and depth 64. Continue draining after a limit
so a writer cannot block the manager. Mark the capture incomplete on limits,
malformed prefixes or read failure. Partial evidence remains inspectable.
Each diagnostic sidecar is at most 16 MiB; retained sidecars total at most
128 MiB per run, including namespace metadata. The namespace writer checks
field lengths before writing and reserves room for its header and trailer.
Exceeding either budget fails the diagnostic run.

Use the existing owned-process timeout and interruption handling. Drain
stdout/stderr without retaining diagnostic previews: a fixture can redirect
raw xtrace onto either stream. Ordinary candidate checks retain their output
previews. Only the dedicated writer's
close-on-exec flag is cleared in the child; do not overwrite an arbitrary
descriptor that might belong to process launch machinery.

## Attribution and uncertainty

Analyze root-shell intervals between successive root records. Reconstruct
function ancestry from depth and entry/return transitions. Charge each
interval once to an exclusive leaf, then add it to inclusive ancestors.
Normalize generation and fixture source paths in reports. Keep source line
locations and plugin ownership where the snapshot path establishes it.

Child-shell records are separate process observations. They are never added
to root wall time. A root gap containing child activity is opaque at that
level: substitutions are traced after expansion, so charging the gap to the
previous statement could blame the wrong line. Expose that uncertainty and
the child observations. Reject backward clocks and impossible depths.

At normal wrapper termination, write a separate bounded metadata file using
NUL-separated fields. Record plugin states/reasons and existing function
ownership keys, without definition bodies. Report a declared-command conflict
with its blocked plugin and the known existing owner; use unknown when no
owner is established. Do not infer every possible namespace conflict.

An interrupted shell or an observed tracing-control change leaves incomplete
evidence. Return failure and retain that evidence. A final-state check alone
cannot prove uninterrupted tracing, and the fixed-word classifier cannot
establish continuity against arbitrary shell code or descriptor manipulation.
The attribution contract requires reviewed fixtures that preserve the tracing
channel; completeness describes the observed protocol under that assumption.
Trusted fixtures have the
user's authority; this is not a security sandbox or a hostile-code attestation.

## Verification frozen before implementation

The separate `benchmarks/diagnostics.json` corpus fixes effects and targets.
Primary load/callback/provider cases take at least 200 ms. Attribute at least
95% of each controlled root interval to correct observed sites, with planted
causes present and no double counting. Test child activity separately; never
launder opaque time into an attributed cause.

Measure observed and unobserved whole-command elapsed time in two complete
20-pair runs. The observer's p95 overhead must remain at most 20% in each
primary case. Record wall time and kernel-reported resource use separately.
These measurements do not count as the broader milestone's two new 25%
performance gains.

Public checks must exercise normal success and failed fixtures. Include
changed inputs, argument privacy, bounded overflow, timeout/interruption,
spaces and Unicode paths, and state preservation. A conflict is a failed
diagnostic with an actionable report. Exercise a real completion provider in
a PTY as well as the controlled fixture runner.

Run full verification on native and native-disabled configurations. Qualify
the changed behavior with matching Bash 5.2/5.3, review the process and
attribution code independently, and preserve failed trials. The original
lifecycle suite still requires two 20-pair runs without repeated median or
p95 regression above 5% against preserved `c80d4df`.
