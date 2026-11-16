# Publication scoreboard

Binit is published only after it wins or ties the comparisons it is meant to
lead. The [frozen policy](../benchmarks/publication-scoreboard.json) fixes the
five aspects, the competitors, the statistics and the verdict rules. It was
committed before any comparison run of the `f6885a9` candidate, and
`scripts/scoreboard.py` applies it to complete receipts without changing it.

| Aspect | Measure | Scored against |
| --- | --- | --- |
| First usable input, 50 plugins | median time to an acknowledged command in the primed framework corpus | Bash-it, Oh My Bash |
| All 50 plugin effects ready | median time until the effect probe passes, deferred work included | Bash-it, Oh My Bash |
| Keystroke p99, today's default editor | insert and delete p99 in the editor corpus, every mode and history | ble.sh |
| Typing and cancellation during slow completion | isolated input response p95, cancellation and accept medians | ble.sh |
| Unmodified popular plugins | declared observable effects per plugin and framework | not measured yet |

Each run gives a ratio of Binit to the competitor, with a 95% interval from a
paired block bootstrap over the measured pairs. A run wins when the upper
bound is below 0.95. It ties when the upper bound is at most 1.05, and loses
when the lower bound is above 1.05. An interval wider than 0.2 is
inconclusive. The editor aspects also tie when the whole difference interval
stays within 1 ms. Two runs that agree decide a comparison, and a win with a
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

The editor corpus runs on the candidate without `BINIT_HISTORY_MODE`, so
Binit uses its default search-mode history. The historical editor receipt
used the live panel and stays historical.

## Current result

At `c94c8c8` four aspects are measured. First usable input and all effects
ready tie with Bash-it and win against Oh My Bash. Keystroke p99 wins against
ble.sh in every cell. Typing and cancellation during slow completion loses:
input response and accept win, but cancelling a large completion takes about
52.8 ms in Binit against 33–44 ms in ble.sh. Unmodified popular plugins are not
measured. The gate needs four measured aspects that all win or tie, so
publication stays closed. The [startup-cost evidence](startup-cost-evidence.md)
has every comparison.
