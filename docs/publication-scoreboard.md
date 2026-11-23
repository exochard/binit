# Publication scoreboard

Binit is published only after it wins or ties the comparisons it is meant to
lead. The [policy](../benchmarks/publication-scoreboard.json) fixes the
five aspects, the competitors, the statistics and the verdict rules, and
`scripts/scoreboard.py` applies it to complete receipts without changing it.

| Aspect | Measure | Scored against |
| --- | --- | --- |
| First usable input, 50 plugins | median time to an acknowledged command in the primed framework corpus | Bash-it, Oh My Bash |
| All 50 plugin effects ready | median time until the effect probe passes, deferred work included | Bash-it, Oh My Bash |
| Keystroke p99, today's default editor | insert and delete p99 in the editor corpus, every mode and history | ble.sh |
| Typing and cancellation during slow completion | isolated input response p95, cancellation and accept medians | ble.sh |
| Unmodified popular plugins | declared observable effects per plugin and framework | not measured yet |

Each run gives a ratio of Binit to the competitor, with a 95% interval from a
paired block bootstrap over the measured pairs: 10,000 resamples with
replacement, the same block indices for both subjects, and a fixed seed from
the policy. The statistics are the median and the nearest-rank p95 and p99.

The editor aspects have a 1 ms difference floor. A comparison is within the
floor when the whole difference interval lies within plus or minus 1 ms. The
verdict is decided in this order:

1. An interval wider than 0.2 is a tie when within the floor, otherwise
   inconclusive.
2. A win needs the upper bound below 0.95.
3. Within the floor, the result is a tie.
4. A loss needs the lower bound above 1.05 and the difference lower bound
   above the floor.
5. Otherwise an upper bound of at most 1.05 is a tie, and anything else is
   inconclusive.

Two runs that agree decide a comparison, and a win with a
tie counts as a tie. One losing comparison loses its aspect.

Publication needs at least four measured aspects, all wins or ties.

## Rows that are reported, not scored

The `binit-wait` row loads the same 50 plugins with `load=wait`. Its prompt
appears before the plugins load, and the work it defers is charged to the
first acknowledged command and to effect readiness. Its verdicts are shown
beside the eager row, never in its place, so an earlier visible prompt cannot
count as a faster first input.

Zinit runs in Zsh. Its cost over a directly sourced Zsh control is reported
beside Binit's cost over the same Bash control and is not scored. Plain
Readline is the editor control; it has no history panel or suggestions. The
state-changing slow provider runs in the shell for every editor, so its
timings are reported for each subject without a verdict.

The editor corpus runs without `BINIT_HISTORY_MODE`, so Binit uses its default
search-mode history.

The [real plugin workflow benchmark](plugin-compatibility.md#real-plugin-workflow-timing)
reports startup and first use with observable Git, fzf, and NVM effects.
Its receipts use a separate workload and score no publication aspects.

## Current result

The scored result for each release is published with it on the
[releases page](https://github.com/exochard/binit/releases).
