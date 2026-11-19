# Performance scope

Binit measures Bash execution separately from the time spent opening a shell. A prompt becoming ready sooner does not establish that Bash scripts run faster.

The [performance program](performance-program.md) covers startup and readiness,
scheduling, first-use activation and steady-state hooks. It also sets goals for
completion, editing and catalog scaling, with resource and recovery measurements.
Interpreter work retains its separate execution targets. A tool such as Git
has its own running time; record that separately from manager overhead.

`make bench` runs 20 pairs of clean Bash and Binit sessions, alternating their order. Both use the installed Bash executable and a real PTY. The Binit scenario loads three plugins and includes manager verification before entering the shell. The filesystem cache is warm; timings include host scheduling noise.

The execution cases cover a loop of Bash builtins and a loop of parameter expansions. Separate cases launch external commands or use command substitution. Their elapsed times include the return to the prompt. Raw scenario definitions and median/p95 results are written to `evidence/bench.json` when the command runs; the directory is not tracked.

The default execution engine is the system Bash. Experimental reference and PGO/LTO builds live beside it and can be selected with `binit run --engine PATH`. The [execution benchmark procedure](benchmarks.md) defines how those builds are compared; a faster prompt does not satisfy that gate. The optional interpreter must cut the frozen suite's geometric-mean time by 25% before promotion, as defined in the benchmark procedure.

Before claiming an execution improvement, compare equivalent programs with matching output and exit status. Report process creation separately from builtin work. An optimization must beat the unchanged Bash baseline over repeated runs; disabling useful behavior does not satisfy that target.

For Binit-native functions, prefer Bash builtins in frequently called hooks. Put expensive external work in bounded workers and reuse results where the inputs have not changed. Custom plugins use the same hooks as bundled plugins and appear in `binit profile`, so their costs stay visible.
