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

## Per-plugin comparisons

Use `scripts/bench_plugin.py` to compare one plugin with the same Bash
interpreter and the same history fixture. The two plugin directories can
point to different committed snapshots.

```bash
python3 scripts/bench_plugin.py --plugin git-prompt \
  --before-binary /path/to/baseline/binit --after-binary /path/to/updated/binit \
  --before-plugin /path/to/baseline/git-prompt --after-plugin /path/to/updated/git-prompt \
  --mode emacs --samples 30 --output tmp/git-prompt-emacs.json
```

Run again with `--mode vi` and the matching Binit binary for each Bash
version. Set `--git-prompt-mode status` to measure rich status. The default
is `branch`. `--git-files` controls the number of
untracked fixture files and defaults to 30.

The receipt includes two baseline arms, the candidate, and each shell's
`binit profile` row. It reports startup medians and keystroke p99.
Intervals resample entire paired rounds to keep the keystrokes from a shell
together. Compare the candidate difference to the baseline arms' variation
before drawing a conclusion about a small change.

### Git prompt development comparison

The reference used Binit 0.1.0 and git-prompt 0.1.0-dev in branch mode.
Each row has 30 paired rounds on a shared host. The 20,000-file row has 60.
First-prompt values are medians. All timings are in milliseconds.

| Bash | Editing mode | Candidate mode | First prompt, before → after | Keystroke p99, before → after |
| --- | --- | --- | ---: | ---: |
| 5.2.15 | Emacs | Branch | 4.625 → 4.645 | 0.055 → 0.054 |
| 5.2.15 | Emacs | Status | 4.668 → 4.628 | 0.057 → 0.052 |
| 5.2.15 | vi | Branch | 4.632 → 4.656 | 0.066 → 0.059 |
| 5.2.15 | vi | Status | 4.688 → 4.660 | 0.052 → 0.053 |
| 5.3.20 | Emacs | Branch | 13.840 → 13.678 | 0.051 → 0.054 |
| 5.3.20 | Emacs | Status | 13.733 → 13.715 | 0.052 → 0.054 |
| 5.3.20 | vi | Branch | 13.741 → 13.903 | 0.050 → 0.051 |
| 5.3.20 | vi | Status | 13.937 → 13.683 | 0.050 → 0.051 |
| 5.3.20 | vi | Status, 20,000 untracked files | 13.728 → 13.618 | 0.051 → 0.050 |

The paired intervals resolved no increase in first-prompt time or keystroke
p99. The large fixture showed `[trunk status?]` at the next prompt after
950 ms idle, which preserves the branch when the scan exceeds its record
limit. The separate seven-plugin run on Bash 5.3.20 and vi measured a
15.713 → 15.529 ms first acknowledged key, with 20 runs per configuration.

### Git completion development comparison

Each row uses the same Binit binary on both sides and 30 paired rounds.
The reference is git-completion 0.1.0-dev with its Git-only declaration.
For the installed mode, the candidate uses `installed.toml` and
`--after-state loaded`. A terminal-screen probe times the first acknowledged
key, including the post-prompt registration.

| Bash | Editing mode | Candidate mode | First prompt, before → after | First key ready, before → after | Keystroke p99, before → after |
| --- | --- | --- | ---: | ---: | ---: |
| 5.2.15 | Emacs | Git | 3.561 → 3.707 | 4.931 → 5.089 | 0.063 → 0.055 |
| 5.2.15 | Emacs | Installed | 3.771 → 3.439 | 5.115 → 5.668 | 0.059 → 0.055 |
| 5.2.15 | vi | Git | 3.724 → 3.745 | 5.150 → 5.095 | 0.058 → 0.054 |
| 5.2.15 | vi | Installed | 3.620 → 3.406 | 4.961 → 5.840 | 0.053 → 0.055 |
| 5.3.20 | Emacs | Git | 13.922 → 13.658 | 15.405 → 14.994 | 0.056 → 0.058 |
| 5.3.20 | Emacs | Installed | 13.824 → 13.406 | 15.192 → 15.826 | 0.056 → 0.054 |
| 5.3.20 | vi | Git | 13.599 → 13.608 | 14.935 → 14.944 | 0.059 → 0.058 |
| 5.3.20 | vi | Installed | 13.995 → 13.218 | 15.341 → 15.758 | 0.061 → 0.060 |

The paired intervals resolved no increase in first-prompt time or keystroke
p99. The opt-in mode increased the first acknowledged key median by
0.4–0.9 ms across these absolute measurements. Git's first Tab in separate
20-run seven-plugin measurements was 5.958 → 6.020 ms. Warm Tab was
2.294 → 2.309 ms. Those runs used Bash 5.3.20 in vi mode.

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
