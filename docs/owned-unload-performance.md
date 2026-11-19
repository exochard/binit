# Owned unload and generation preparation

Native owned unload and generation preparation have their own lifecycle
measurements, described in the [performance program](performance-program.md)
and run by `make bench-lifecycle`. Measured comparisons are published with each
release at https://github.com/exochard/binit/releases.

## Implementation

Unload checks all owned functions in the calling shell through Bash's own
serializer. It preserves the original Bash fallback and completes the other
ownership checks before removal. Generation publication overlaps file syncs
with at most eight writers. Every file keeps its original descriptor through
sync; metadata follows successful joins. Verification still precedes selection
of the generation. A traced fixture produces identical generation contents and
file modes, retaining all seven file syncs.

## Correctness and limits

Failure tests cover conservative unload refusal and preservation of the
selected generation after a write failure. Thread-spawn failure, worker panic
and filesystem sync errors are not fault-injected. Per-file syncs are
preserved; power-loss persistence of directory entries remains unqualified.
The measurements cover x86_64 with stock GNU Bash 5.3 only. ARM64 and
universal completion responsiveness are not measured. Activation and
warm-command performance have no established improvement from this change. The
optional interpreter's 25% gate is unchanged.
