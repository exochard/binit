# Performance scope

Binit measures Bash execution separately from the time spent opening a shell. A prompt becoming ready sooner does not establish that Bash scripts run faster.

The first targets are long scripts and loops. For interactive work, measure parsing and word expansion, command lookup, process launch, and the return to the prompt; a tool such as Git also has its own running time. Record that separately. Compare everyday workloads before claiming a general improvement.

`make bench` runs 20 pairs of clean Bash and Binit sessions, alternating their order. Both use the installed Bash executable and a real PTY. The Binit path includes manager verification before entering the shell. The filesystem cache is warm; timings include host scheduling noise.

The execution cases cover a loop of Bash builtins and a loop of parameter expansions. Separate cases launch external commands or use command substitution. Their elapsed times include the return to the prompt. Raw scenario definitions and median/p95 results are saved in `evidence/bench.json`.

The default execution engine is the system Bash. Experimental reference and PGO/LTO builds live beside it and can be selected with `binit run --engine PATH`. The [execution benchmark procedure](benchmarks.md) defines how those builds are compared; a faster prompt does not satisfy that gate. The [0.1 release requirements](release-0.1.md) retain the interpreter target alongside the plugin workflow.

Before claiming an execution improvement, compare equivalent programs with matching output and exit status. Report process creation separately from builtin work. An optimization must beat the unchanged Bash baseline over repeated runs; disabling useful behavior does not satisfy that target.

For Binit-native functions, prefer Bash builtins in frequently called hooks. Put expensive external work in bounded workers and reuse results where the inputs have not changed. Custom plugins use the same hooks as bundled plugins and appear in `binit profile`, so their costs stay visible.
