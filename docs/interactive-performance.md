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
untracked fixture files and defaults to 30. `--after-setting BINIT_NAME=VALUE`
applies a setting to the candidate shell only. `--project-profile` starts
every shell in a directory with a trusted project profile. `--long-line`
seeds a 3,400-byte command by bracketed paste and times 23 more keys typed
at its end, where the line wraps across several terminal rows.

The receipt includes two baseline arms, the candidate, and each shell's
`binit profile` row. It reports startup medians and keystroke p99.
Intervals resample entire paired rounds to keep the keystrokes from a shell
together. Compare the candidate difference to the baseline arms' variation
before drawing a conclusion about a small change.

## Development plugins at 0.2.0-dev

The seven development plugins moved from 0.1.0-dev to 0.2.0-dev to close the
build rows in [the plugin gaps](plugin-gaps.md). This section records their
checks and timings on one shared Linux workstation. Every comparison runs
the same Bash executable on both sides, so it shows whether a change moved
first-prompt or keystroke latency. These numbers compare Binit builds with
each other. They do not rank Binit against other shells, frameworks or
plugin managers.

### Verification

The native module was built against each interpreter's own headers: Bash
5.2.15 with Readline 8.2, and Bash 5.3.20 with the system Readline 8.3.

| Check | Bash 5.2.15 | Bash 5.3.20 |
| --- | ---: | ---: |
| Rust tests | 179 passed | 179 passed |
| Native C tests | 3 passed | 3 passed |
| Python and PTY tests, Emacs and vi | 493 run, 9 skipped | 493 run, 8 skipped |
| Terminal smoke checks | 16 passed | 16 passed |
| Standalone plugin tests | 54 passed | 54 passed |
| ShellCheck, warning severity | clean | clean |

Seven skips on both interpreters need optional pinned sources that the
checkout does not include: six ble.sh editor cases and one runtime workflow
case. One registration case runs only in native-disabled builds. Readline
8.2 lacks the signal-state marker that one resize case inspects, which
accounts for the ninth skip on Bash 5.2. The native C tests also passed under
AddressSanitizer and UndefinedBehaviorSanitizer with the syntax grammar
compiled in.

An earlier full Bash 5.3 run of the same native code failed one test, which
passed in the runs above. `test_interrupt_during_idle_load_recovers_at_prompt` sends Ctrl-C
during a timed idle load and sometimes sees no new prompt within its 5-second
window. Ten runs of its module failed once on this build and once with Binit
0.1.0, so the failure predates these plugins. Its cause is still open.

### Reading the comparisons

The native changes landed in this order: prompt workers, history controls,
suggestion strategies, project helpers, then syntax classes. Each plugin's
before arm runs the 0.1.0-dev plugin on the build that preceded its native
change. Its after arm runs the 0.2.0-dev plugin on the build that added it.
Git prompt starts from Binit 0.1.0. Git completion and directory jump changed
no native code, so both of their arms use one build.

A cell gives the before and after values, then the paired change with its
95% interval and the 95% interval between two runs of the unchanged baseline
(A/A). First prompt is the median time to the first prompt. Keystroke p99
pools every timed key in an arm. A result is clear when the change's interval
ends no more than 0.1 ms above the A/A interval and the A/A interval is
narrower than 1 ms. An interval entirely above zero is an increase. Any other
cell is inconclusive: at that run's noise floor, the data cannot exclude a
regression larger than 0.1 ms. Fifteen noisy cells were repeated with 36
rounds, and four syntax cells with 100. Both passes are listed under
[repeated cells](#repeated-cells).

### Seven plugins together

`scripts/bench_interactive.py` loaded all seven plugins with default settings
in vi mode and live history. The before arm is Binit 0.1.0 with the
0.1.0-dev plugins. The after arm is the final build with the 0.2.0-dev
plugins. Each arm ran six times with 20 rounds, alternating which arm went
first. These arms are separate runs rather than paired rounds. Each cell
shows the median across the six runs, then the lowest and highest run.

| Measurement (ms) | Bash 5.2.15 before | Bash 5.2.15 after | Bash 5.3.20 before | Bash 5.3.20 after |
| --- | ---: | ---: | ---: | ---: |
| First prompt, median | 8.294 (8.014–11.043) | 9.999 (9.036–13.011) | 26.269 (19.819–35.843) | 24.441 (22.585–30.685) |
| First key ready, median | 8.473 (8.169–11.462) | 10.154 (9.181–13.188) | 26.574 (19.945–36.093) | 24.637 (22.735–30.932) |
| Matching keystroke, p95 | 1.130 (1.008–2.299) | 1.111 (0.977–1.419) | 1.739 (0.979–4.265) | 1.065 (0.983–1.727) |
| Non-matching keystroke, p95 | 0.230 (0.197–0.903) | 0.249 (0.194–0.801) | 0.694 (0.197–3.238) | 0.229 (0.178–1.292) |
| First history search, median | 2.245 (1.991–2.929) | 2.284 (1.902–3.124) | 2.921 (1.979–4.156) | 2.027 (1.883–3.123) |
| First Git completion, median | 7.463 (6.962–9.668) | 7.296 (6.748–9.728) | 8.528 (6.786–11.966) | 7.229 (6.632–9.146) |
| Warm Git completion, median | 2.992 (2.614–3.490) | 2.665 (2.373–3.484) | 3.517 (2.843–5.523) | 2.979 (2.726–3.673) |
| Bare Bash first prompt | 2.154 (2.034–2.763) | 2.304 (2.034–2.783) | 18.322 (14.959–27.646) | 16.719 (14.886–23.257) |
| Empty Binit first prompt | 4.875 (4.689–5.925) | 4.778 (4.417–5.950) | 23.322 (17.280–34.795) | 19.550 (16.810–24.442) |

These runs shared the host with other work, at a load average of 9–11, so
the Bash 5.3 columns spread widely: bare Bash alone took 14.959–27.646 ms
before and 14.886–23.257 ms after. On Bash 5.2 the first prompt moved
8.294 → 9.999 ms. The benchmark runs inside a Git repository, and that rise
matches the Git prompt's paired cost of about 1.7 ms below. Keystroke and
completion ranges overlap on both interpreters.

`make bench` times the core runtime with the three plugins in
`config/demo.toml`. Each cell gives the paired overhead median from two runs
per arm, separated by a semicolon. These ran on the build before the final
Git prompt and editor fixes. On the final build, two alternating pairs on the
loaded host gave startup overheads of 4.705–4.858 ms before and
4.563–4.832 ms after on Bash 5.3. On Bash 5.2 they gave 4.698–4.730 ms
before and 4.787–4.942 ms after. Two pairs cannot settle a difference of
that size.

| Paired overhead, median (ms) | Bash 5.2.15 before → after | Bash 5.3.20 before → after |
| --- | ---: | ---: |
| Startup | 4.880 → 4.852; 4.906 → 4.926 | 4.879 → 4.516; 4.526 → 4.789 |
| First command round trip | 0.144 → 0.133; 0.111 → 0.106 | 0.143 → 0.146; 0.110 → 0.105 |
| 10,000 builtins | -0.040 → -0.057; 0.417 → 0.220 | 0.458 → 0.275; 0.165 → 0.293 |
| 5,000 parameter expansions | -0.007 → -0.033; 0.162 → 0.103 | 0.351 → 0.260; 0.197 → 0.015 |
| 20 external commands | 0.745 → 0.638; 0.800 → 0.796 | 0.717 → 0.566; 0.673 → 0.552 |
| 20 command substitutions | 1.559 → 1.656; 1.321 → 1.334 | 1.423 → 1.038; 1.223 → 1.246 |

### 1. Git prompt

Status mode adds the branch or exact tag, the short commit when detached,
divergence from the local upstream ref, and counts for stashes, conflicts,
staged, unstaged and untracked files. It also shows rebase, merge,
cherry-pick, revert and bisect, with step counts for a rebase.

Each prompt in a repository starts a lookup and waits up to 50 ms for it, so
the segment follows `cd` and the command that just ran. In status mode one
worker per shell scans. A scan that outlasts the wait keeps running, stops
after 700 ms or 16,384 records, and is cancelled by typing. Directories
outside a repository start no lookup.

| Setting | Default | Opt-in |
| --- | --- | --- |
| `BINIT_GIT_PROMPT_MODE` | `branch`, which works without the native module | `status`, which needs prompt workers |
| `BINIT_GIT_PROMPT_WAIT_MS` | `50` | `0` to `1000`; `0` never waits and shows each result one prompt later |

The standalone suite passed 13 tests on each Bash. They cover the segment on
the prompt right after `cd`, after a command that changes the status, and
after leaving a repository; a slow lookup that does not hold the prompt in
Emacs and vi; and the single messages for a missing native module or an
invalid wait. Four PTY cases cover the worker. Typing cancels it in Emacs and
vi, output over its limit is rejected, and it cannot open a network socket.
The fourth covers the wait. A fast result returns at once, and a slow one
stays for a later prompt. A key typed during the wait ends it and reaches
Readline unchanged, Ctrl-R included, and the terminal settings are restored.
A request started right after a cancelled one still runs.

| Bash | Mode | Settings | First prompt (ms) | Change; A/A | Keystroke p99 (ms) | Change; A/A | Result |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| 5.2.15 | Emacs | Default (branch) | 7.466 → 9.757 | +1.909 [+1.564, +2.625]; A/A [-0.449, +0.180] | 1.016 → 0.714 | -0.302 [-0.583, +0.114]; A/A [-0.304, +0.983] | increase: first prompt; inconclusive: keystroke p99 |
| 5.2.15 | Emacs | Status | 7.179 → 22.073 | +15.182 [+13.525, +16.198]; A/A [-0.253, +0.606] | 2.010 → 0.949 | -1.061 [-1.947, +0.027]; A/A [-1.546, +0.619] | increase: first prompt; inconclusive: keystroke p99 |
| 5.2.15 | vi | Default (branch) | 5.720 → 7.314 | +1.713 [+1.582, +1.852]; A/A [-0.139, +0.083] | 0.069 → 0.066 | -0.003 [-0.023, +0.011]; A/A [-0.019, +0.005] | increase: first prompt |
| 5.2.15 | vi | Status | 5.655 → 20.377 | +14.539 [+14.453, +14.685]; A/A [-0.221, +0.022] | 0.254 → 0.229 | -0.025 [-0.355, +0.523]; A/A [-0.373, +0.852] | increase: first prompt; inconclusive: keystroke p99 |
| 5.3.20 | Emacs | Default (branch) | 16.394 → 17.887 | +1.520 [+1.366, +1.704]; A/A [-0.284, +0.058] | 0.051 → 0.052 | +0.000 [-0.007, +0.013]; A/A [-0.005, +0.004] | increase: first prompt |
| 5.3.20 | Emacs | Status | 16.503 → 31.267 | +14.861 [+14.482, +15.234]; A/A [-0.302, +0.435] | 0.054 → 0.052 | -0.002 [-0.007, +0.006]; A/A [-0.001, +0.008] | increase: first prompt |
| 5.3.20 | vi | Default (branch) | 16.918 → 18.878 | +1.726 [+1.349, +2.128]; A/A [-0.176, +0.278] | 0.070 → 0.065 | -0.005 [-0.101, +0.021]; A/A [-0.120, +0.026] | increase: first prompt |
| 5.3.20 | vi | Status | 17.575 → 31.854 | +14.400 [+13.715, +14.871]; A/A [-0.636, +0.176] | 0.084 → 0.095 | +0.011 [-0.013, +0.042]; A/A [-0.013, +0.041] | increase: first prompt |
| 5.3.20 | vi | Status, 20,000 untracked files | 16.937 → 66.235 | +49.393 [+49.101, +49.627]; A/A [-0.274, +0.283] | 0.067 → 0.095 | +0.028 [-0.004, +0.188]; A/A [-0.011, +0.001] | increase: first prompt; inconclusive: keystroke p99 |

Rounds per cell: 30, 60.

The first prompt in a repository now includes the lookup. On the 30-file
fixture that adds 1.5–1.9 ms for the branch and 14.4–15.2 ms for status. No
keystroke increase was resolved; three Bash 5.2 cells had A/A intervals over
1 ms wide and stay inconclusive. The 20,000-file row ran 60 rounds. Its scan
outlasts the wait, so the first prompt pays the full 50 ms and the segment
appears at a later prompt only if the scan finishes before a key. Set
`BINIT_GIT_PROMPT_WAIT_MS=0` for the earlier behavior, where no prompt waits.

### 2. Git completion

The default mode still loads Git's own completion on first use, and Git
subcommand aliases complete through it. The installed mode is opt-in. It
registers a default handler after the first prompt. On the first Tab for a
command, a cancellable worker finds that command's installed definition, then
the shell loads the bash-completion helpers and the definition. Named shell
aliases and wrappers can use Git's completion.

| Setting | Default | Opt-in |
| --- | --- | --- |
| Plugin declaration | Git completion only | `files=installed.bash,discovery.bash,plugin.bash load=wait`, or `installed.toml` |
| `BINIT_BASH_COMPLETION_FILE` | First of the three standard bash-completion paths | An absolute path |
| `BINIT_GIT_COMPLETION_ALIASES` | Empty array | `(g git_worktree)` |

The standalone suite passed 12 tests on each Bash. Those tests cover
precedence for existing specifications and handlers, typing that cancels
discovery, vi mode with the real package, and a single message when installed
mode lacks native support.

| Bash | Mode | Settings | First prompt (ms) | Change; A/A | First key ready (ms) | Change; A/A | Keystroke p99 (ms) | Change; A/A | Result |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 5.2.15 | Emacs | Default (Git) | 3.561 → 3.707 | +0.031 [-0.030, +0.130]; A/A [-0.047, +0.090] | 4.931 → 5.089 | +0.029 [-0.026, +0.151]; A/A [-0.045, +0.160] | 0.063 → 0.055 | -0.008 [-0.012, -0.003]; A/A [-0.006, +0.005] | clear |
| 5.2.15 | Emacs | Installed | 3.771 → 3.439 | -0.314 [-0.405, -0.236]; A/A [-0.163, +0.021] | 5.115 → 5.668 | +0.618 [+0.409, +0.765]; A/A [-0.173, +0.072] | 0.059 → 0.055 | -0.004 [-0.008, +0.001]; A/A [-0.006, +0.005] | increase: first key ready |
| 5.2.15 | vi | Default (Git) | 3.724 → 3.745 | -0.007 [-0.094, +0.064]; A/A [-0.097, +0.085] | 5.150 → 5.095 | +0.003 [-0.108, +0.075]; A/A [-0.104, +0.169] | 0.058 → 0.054 | -0.004 [-0.010, +0.006]; A/A [-0.006, +0.017] | clear |
| 5.2.15 | vi | Installed | 3.620 → 3.406 | -0.302 [-0.385, -0.165]; A/A [-0.096, +0.118] | 4.961 → 5.840 | +0.724 [+0.325, +0.882]; A/A [-0.095, +0.119] | 0.053 → 0.055 | +0.002 [-0.001, +0.005]; A/A [-0.001, +0.028] | increase: first key ready |
| 5.3.20 | Emacs | Default (Git) | 13.922 → 13.658 | -0.086 [-0.608, +0.226]; A/A [-0.266, +0.634] | 15.405 → 14.994 | -0.110 [-0.732, +0.227]; A/A [-0.279, +0.662] | 0.056 → 0.058 | +0.002 [-0.006, +0.010]; A/A [-0.007, +0.004] | clear |
| 5.3.20 | Emacs | Installed | 13.824 → 13.406 | -0.484 [-0.882, -0.117]; A/A [-0.568, +0.258] | 15.192 → 15.826 | +0.770 [+0.295, +1.113]; A/A [-0.556, +0.433] | 0.056 → 0.054 | -0.002 [-0.006, +0.005]; A/A [-0.004, +0.007] | increase: first key ready |
| 5.3.20 | vi | Default (Git) | 13.599 → 13.608 | +0.133 [-0.163, +0.406]; A/A [-0.251, +0.140] | 14.935 → 14.944 | +0.097 [-0.097, +0.391]; A/A [-0.269, +0.170] | 0.059 → 0.058 | -0.001 [-0.007, +0.002]; A/A [-0.007, +0.002] | inconclusive: first prompt; inconclusive: first key ready |
| 5.3.20 | vi | Installed | 13.995 → 13.218 | -0.518 [-1.039, +0.129]; A/A [-0.451, +0.603] | 15.341 → 15.758 | +0.546 [+0.125, +1.322]; A/A [-0.477, +0.840] | 0.061 → 0.060 | -0.001 [-0.008, +0.009]; A/A [-0.010, +0.003] | inconclusive: first prompt; increase: first key ready |

Rounds per cell: 30.

Installed mode moves registration after the prompt. The first prompt arrives
0.30–0.52 ms earlier, and the first acknowledged key arrives 0.55–0.77 ms
later. All four first-key intervals sit above zero. That later first key is
the cost of the opt-in mode. In the default mode, the seven-plugin first Git
Tab measured 6.538 → 6.540 ms on Bash 5.2 and 6.471 → 6.408 ms on Bash 5.3.
The Bash 5.3 vi default cell is inconclusive for first prompt and first key.

### 3. History search

Searches can require every word, or match each word as a fuzzy subsequence.
New commands can record their directory, time and exit status in memory, so
searches can be limited to the current directory or session. Browse mode can
delete one entry after a `y` confirmation. Deletion only touches Bash's
in-memory history.

| Setting | Default | Opt-in |
| --- | --- | --- |
| `BINIT_HISTORY_MODE` | `search` (Ctrl-R) | `live` |
| `BINIT_HISTORY_MATCH` | `literal` | `words`, `fuzzy` |
| `BINIT_HISTORY_CONTEXT` | `0` | `1` |
| `BINIT_HISTORY_SCOPE` | `all` | `directory`, `session` |
| `BINIT_HISTORY_DELETE` | `0` | `1` |

The standalone suite passed 6 tests on each Bash. Three PTY cases cover the
native side: word search in Emacs and fuzzy search in vi, confirmed deletion
that neither inserts nor runs the entry, and an exit status recorded before
the next command is read. The opt-in rows enable words, context and deletion
together.

| Bash | Mode | Settings | First prompt (ms) | Change; A/A | Keystroke p99 (ms) | Change; A/A | Result |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| 5.2.15 | Emacs | Default | 4.240 → 4.256 | +0.030 [-0.009, +0.126]; A/A [+0.007, +0.059] | 0.492 → 0.930 | +0.438 [-0.860, +1.794]; A/A [-1.494, +0.012] | inconclusive: keystroke p99; repeated below |
| 5.2.15 | Emacs | Words, context, delete | 3.387 → 3.425 | +0.041 [-0.024, +0.064]; A/A [-0.038, +0.074] | 0.146 → 0.136 | -0.010 [-0.116, +0.012]; A/A [-0.117, +0.002] | clear |
| 5.2.15 | vi | Default | 3.886 → 3.866 | +0.020 [-0.084, +0.070]; A/A [-0.069, +0.027] | 0.131 → 0.129 | -0.002 [-0.008, +0.007]; A/A [-0.005, +0.024] | clear |
| 5.2.15 | vi | Words, context, delete | 3.474 → 3.421 | +0.024 [-0.065, +0.113]; A/A [-0.085, +0.043] | 0.138 → 0.134 | -0.003 [-0.033, +0.023]; A/A [-0.033, +0.013] | clear |
| 5.3.20 | Emacs | Default | 16.038 → 16.130 | +0.051 [-0.111, +0.239]; A/A [-0.171, +0.331] | 0.357 → 0.181 | -0.176 [-1.750, +0.026]; A/A [-1.776, +0.018] | inconclusive: keystroke p99 |
| 5.3.20 | Emacs | Words, context, delete | 13.933 → 14.028 | +0.072 [-0.093, +0.218]; A/A [-0.151, +0.169] | 2.054 → 1.077 | -0.978 [-2.242, +0.584]; A/A [-1.630, +0.571] | inconclusive: keystroke p99 |
| 5.3.20 | vi | Default | 14.919 → 14.996 | +0.239 [-0.053, +0.328]; A/A [-0.263, +0.129] | 0.588 → 0.240 | -0.347 [-1.613, +0.036]; A/A [-1.858, +1.177] | inconclusive: first prompt; inconclusive: keystroke p99; repeated below |
| 5.3.20 | vi | Words, context, delete | 12.743 → 12.753 | +0.085 [+0.007, +0.220]; A/A [-0.345, +0.289] | 0.132 → 0.130 | -0.002 [-0.013, +0.012]; A/A [-0.012, +0.008] | increase: first prompt |

Rounds per cell: 30.

The opt-in settings add 0.02–0.09 ms to the first prompt. All four point
estimates are above zero, and the Bash 5.3 vi interval lies above zero inside
a wider A/A interval. Repeats cleared the Bash 5.2 Emacs and Bash 5.3 vi
default cells. Two Bash 5.3 Emacs keystroke cells, one default and one
opt-in, stay inconclusive because their A/A intervals exceed 1 ms.

### 4. Autosuggestions

Strategies run in a configured order: `history`, `previous` for the entry
that followed the last command, and `completion` for one safe candidate from
an isolated registered provider. Completion previews start when the editor is
idle, and typing cancels them. Ctrl-F and Right can accept a word or a
character, keeping UTF-8 characters whole. Style, an input limit and ignore
patterns are configurable.

| Setting | Default | Opt-in |
| --- | --- | --- |
| `BINIT_SUGGESTION_STRATEGIES` | `history` | Any order of `history`, `previous`, `completion` |
| `BINIT_SUGGESTION_ACCEPT` | `full` | `word`, `character` |
| `BINIT_SUGGESTION_STYLE` | `dim` | `plain`, `bold`, `underline` |
| `BINIT_SUGGESTION_LIMIT` | `4096` input bytes | 1–4096 |
| `BINIT_SUGGESTION_IGNORE` | Empty | A glob such as `'git push*'` |
| `BINIT_SUGGESTION_COMPLETION_IGNORE` | Empty | A glob such as `'git *'` |

The standalone suite passed 4 tests on each Bash, including one clear error
for an invalid strategy and one for a missing native module. Five PTY cases
cover word and character acceptance in Emacs and vi, predecessor matching,
isolated-provider previews and their cancellation, and ignore patterns and
styles under the input limit. The opt-in rows use all three strategies with
word acceptance.

| Bash | Mode | Settings | First prompt (ms) | Change; A/A | Keystroke p99 (ms) | Change; A/A | Result |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| 5.2.15 | Emacs | Default | 3.228 → 3.255 | +0.028 [-0.091, +0.141]; A/A [-0.108, +0.112] | 0.142 → 0.132 | -0.010 [-0.037, +0.001]; A/A [-0.037, +0.005] | clear |
| 5.2.15 | Emacs | Three strategies, word accept | 3.253 → 3.207 | -0.009 [-0.106, +0.029]; A/A [-0.093, +0.050] | 0.146 → 0.141 | -0.005 [-0.027, +0.019]; A/A [-0.037, +0.021] | clear |
| 5.2.15 | vi | Default | 3.210 → 3.201 | -0.013 [-0.034, +0.003]; A/A [-0.059, +0.040] | 0.149 → 0.147 | -0.001 [-0.262, +0.031]; A/A [-0.297, +0.010] | clear |
| 5.2.15 | vi | Three strategies, word accept | 3.236 → 3.208 | +0.005 [-0.078, +0.055]; A/A [-0.060, +0.076] | 0.137 → 0.135 | -0.002 [-0.021, +0.008]; A/A [-0.018, +0.017] | clear |
| 5.3.20 | Emacs | Default | 12.469 → 12.484 | +0.079 [-0.162, +0.150]; A/A [-0.116, +0.179] | 0.139 → 0.148 | +0.009 [-0.008, +0.029]; A/A [-0.007, +0.021] | clear |
| 5.3.20 | Emacs | Three strategies, word accept | 12.476 → 12.355 | -0.039 [-0.498, +0.017]; A/A [-0.171, +0.079] | 0.134 → 0.131 | -0.003 [-0.024, +0.012]; A/A [-0.027, +0.010] | clear |
| 5.3.20 | vi | Default | 12.684 → 12.461 | -0.024 [-0.352, +0.118]; A/A [-0.172, +0.134] | 0.139 → 0.143 | +0.004 [-0.011, +0.021]; A/A [-0.019, +0.012] | clear |
| 5.3.20 | vi | Three strategies, word accept | 12.455 → 12.571 | -0.052 [-0.135, +0.107]; A/A [-0.163, +0.191] | 0.139 → 0.140 | +0.001 [-0.014, +0.020]; A/A [-0.021, +0.011] | clear |

Rounds per cell: 24.

Every autosuggestion cell is clear.

### 5. Project context

Ancestor lookup is opt-in, and a nested profile still takes precedence even
while it awaits approval. Profiles can prepend directories, activate an
existing Python virtual environment and add `node_modules/.bin`. Each helper
path must resolve inside the approved directory. `binit-project-status`
reports the selected profile, its trust state and the variables it changed,
without printing their values. Leaving a project restores each variable's
value, unset state and export attribute, and the caller's `BASH_REMATCH`.

| Setting | Default | Opt-in |
| --- | --- | --- |
| `BINIT_PROJECT_SCOPE` | `exact` | `ancestors` |
| Profile `path_prepend`, `python_venv`, `node_bin` | Empty, unset, `false` | Paths inside the profile directory |

The standalone suite passed 8 tests on each Bash. They cover ancestor
restoration and export attributes, an untrusted nested profile that stops the
search, caller match state, declared-unset and nameref variables, symlink
escape, and a PTY `cd` into and out of a project.

| Bash | Mode | Settings | First prompt (ms) | Change; A/A | Keystroke p99 (ms) | Change; A/A | Result |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| 5.2.15 | Emacs | Default (exact) | 4.931 → 4.857 | -0.002 [-0.145, +0.078]; A/A [-0.147, +0.083] | 2.010 → 0.458 | -1.552 [-2.312, +0.471]; A/A [-1.628, +0.730] | inconclusive: keystroke p99; repeated below |
| 5.2.15 | Emacs | Ancestors | 5.999 → 5.741 | -0.041 [-0.318, +0.069]; A/A [-0.359, +0.125] | 0.080 → 0.109 | +0.029 [-0.023, +0.160]; A/A [-0.027, +0.012] | inconclusive: keystroke p99 |
| 5.2.15 | Emacs | Trusted profile | 5.898 → 5.741 | -0.127 [-0.250, -0.031]; A/A [-0.117, +0.133] | 0.062 → 0.057 | -0.005 [-0.012, +0.004]; A/A [-0.011, +0.011] | clear |
| 5.2.15 | vi | Default (exact) | 5.051 → 4.946 | -0.101 [-0.212, +0.180]; A/A [-0.344, +0.048] | 0.074 → 0.290 | +0.216 [-0.053, +0.453]; A/A [-0.061, +0.272] | inconclusive: first prompt; inconclusive: keystroke p99; repeated below |
| 5.2.15 | vi | Ancestors | 5.853 → 5.855 | +0.073 [-0.126, +0.221]; A/A [-0.342, +0.085] | 1.785 → 1.934 | +0.149 [-1.288, +1.024]; A/A [-1.042, +1.119] | inconclusive: first prompt; inconclusive: keystroke p99; repeated below |
| 5.2.15 | vi | Trusted profile | 5.721 → 5.634 | -0.117 [-0.184, -0.028]; A/A [-0.074, +0.043] | 0.054 → 0.056 | +0.002 [-0.007, +0.007]; A/A [-0.008, +0.009] | clear |
| 5.3.20 | Emacs | Default (exact) | 14.448 → 13.976 | -0.190 [-0.412, +0.083]; A/A [-0.351, +0.268] | 0.053 → 0.053 | +0.000 [-0.065, +0.009]; A/A [-0.069, +0.051] | clear |
| 5.3.20 | Emacs | Ancestors | 14.729 → 14.060 | -0.183 [-0.625, +0.130]; A/A [-0.366, +0.274] | 0.051 → 0.052 | +0.001 [-0.006, +0.007]; A/A [-0.008, +0.004] | clear |
| 5.3.20 | Emacs | Trusted profile | 27.882 → 27.816 | +0.247 [-0.180, +1.635]; A/A [-1.355, +0.734] | 1.948 → 1.365 | -0.582 [-1.148, +0.300]; A/A [-1.062, +0.494] | inconclusive: first prompt; inconclusive: keystroke p99; repeated below |
| 5.3.20 | vi | Default (exact) | 14.414 → 13.824 | -0.296 [-0.486, +0.009]; A/A [-0.570, +0.054] | 0.050 → 0.050 | -0.001 [-0.006, +0.004]; A/A [-0.000, +0.010] | clear |
| 5.3.20 | vi | Ancestors | 14.859 → 14.936 | +0.282 [-0.378, +0.477]; A/A [-0.271, +0.201] | 0.055 → 0.058 | +0.003 [-0.009, +0.014]; A/A [-0.003, +0.008] | inconclusive: first prompt |
| 5.3.20 | vi | Trusted profile | 17.942 → 17.331 | -0.325 [-0.982, +0.208]; A/A [-0.498, +0.811] | 0.928 → 1.073 | +0.145 [-0.282, +0.821]; A/A [+0.000, +0.525] | inconclusive: first prompt; inconclusive: keystroke p99; repeated below |

Rounds per cell: 24.

With a trusted profile, the first prompt on Bash 5.2 dropped by 0.12–0.13 ms,
and both intervals lie below zero. Ordinary variables no longer start a
subshell. The ancestor scan has a measured cost. Its 36-round repeat on Bash 5.2 vi
measured +0.078 ms on the first prompt, with an interval of
[+0.047, +0.116]. The trusted-profile cells cleared on repeat. Three cells
remain inconclusive. On Bash 5.2 vi, default keystroke p99 rose in both runs
(+0.216, then +0.077 ms), each with a tail wider than its A/A interval. The
others are Bash 5.2 Emacs ancestor keystroke p99 and Bash 5.3 vi ancestor
first prompt.

### 6. Directory jump

`j client api` matches its terms in order against path components. Frecency
ranking is opt-in. Excluded globs apply when visits are recorded and when
results are listed. `j --interactive` opens a picker that moves with `j`/`k`,
the arrows, and `h`/`l` for the first and last entry. History holds up to 128
directories and stays in the current shell's memory.

| Setting | Default | Opt-in |
| --- | --- | --- |
| `BINIT_JUMP_RANK` | `visits` | `frecency` |
| `BINIT_JUMP_EXCLUDE` | Empty array | `("$HOME/.cache" '/tmp/*')` |

The standalone suite passed 7 tests on each Bash, including Enter and Esc in
the picker in both editing modes.

| Bash | Mode | Settings | First prompt (ms) | Change; A/A | Keystroke p99 (ms) | Change; A/A | Result |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- |
| 5.2.15 | Emacs | Default (visits) | 4.263 → 4.296 | -0.010 [-0.074, +0.035]; A/A [-0.081, -0.000] | 0.053 → 0.047 | -0.006 [-0.013, +0.001]; A/A [-0.012, +0.003] | clear |
| 5.2.15 | Emacs | Frecency | 4.278 → 4.352 | +0.016 [-0.035, +0.034]; A/A [-0.051, +0.088] | 0.051 → 0.051 | -0.000 [-0.011, +0.008]; A/A [-0.012, +0.004] | clear |
| 5.2.15 | vi | Default (visits) | 4.321 → 4.270 | -0.006 [-0.055, +0.036]; A/A [-0.124, +0.055] | 0.057 → 0.048 | -0.008 [-0.013, +0.004]; A/A [-0.013, +0.006] | clear |
| 5.2.15 | vi | Frecency | 6.683 → 6.109 | -0.021 [-0.654, +0.102]; A/A [-0.271, +0.064] | 0.942 → 0.805 | -0.137 [-1.172, +0.553]; A/A [-1.118, +0.364] | inconclusive: keystroke p99; repeated below |
| 5.3.20 | Emacs | Default (visits) | 15.300 → 14.887 | -0.080 [-0.326, +0.314]; A/A [-0.112, +0.567] | 0.060 → 1.668 | +1.608 [-0.005, +3.766]; A/A [-0.002, +0.314] | inconclusive: keystroke p99; repeated below |
| 5.3.20 | Emacs | Frecency | 14.685 → 14.496 | +0.104 [-0.541, +0.245]; A/A [-0.483, +0.191] | 0.057 → 0.054 | -0.003 [-0.008, +0.004]; A/A [-0.011, -0.001] | clear |
| 5.3.20 | vi | Default (visits) | 13.379 → 13.410 | -0.074 [-0.150, +0.115]; A/A [-0.254, +0.087] | 0.047 → 0.052 | +0.005 [-0.002, +0.010]; A/A [-0.006, +0.008] | clear |
| 5.3.20 | vi | Frecency | 13.508 → 13.365 | -0.023 [-0.212, +0.022]; A/A [-0.287, -0.011] | 0.050 → 0.052 | +0.002 [-0.005, +0.006]; A/A [-0.007, +0.007] | clear |

Rounds per cell: 24.

Both arms use one build, and the plugin registers only a directory hook, so
it does no work on a keystroke. The Bash 5.3 Emacs default keystroke cell
measured +1.608 ms in its first run and −0.041 ms on repeat. The repeat's A/A
interval spans 2 ms, so the cell stays inconclusive. The Bash 5.2 vi frecency
cell cleared on repeat.

### 7. Syntax highlighting

Extended mode adds categories for aliases, functions, builtins, external and
missing commands, existing and missing paths, options, expansions,
substitutions, redirections, the bracket next to the cursor, and literal Git
subcommands. Command names and literal paths are checked while the editor is
idle, at most 24 tokens per line, and typing cancels a pending check. Any
category's style can be overridden. Lines longer than 4,096 bytes fall back
to Readline's display.

| Setting | Default | Opt-in |
| --- | --- | --- |
| `BINIT_HIGHLIGHT_EXTENDED` | `0` | `1` |
| `BINIT_HIGHLIGHT_STYLES` | Empty | `'alias=1;36,path=4;32,bracket=7;35'` |

The standalone suite passed 4 tests on each Bash, including one diagnostic
for a missing native module and validation of settings and palettes. Four PTY
cases cover token classes and the Git rule, idle resolution in Emacs and vi,
cancellation by typing in both modes, and bracket matching with a long line
in both modes. Each cell here also edits the end of a wrapped 3,400-byte
line.

| Bash | Mode | Settings | First prompt (ms) | Change; A/A | Keystroke p99 (ms) | Change; A/A | Wrapped-line p99 (ms) | Change; A/A | Result |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 5.2.15 | Emacs | Default | 3.765 → 3.772 | +0.037 [-0.086, +0.163]; A/A [+0.000, +0.078] | 0.145 → 0.162 | +0.016 [-0.012, +0.045]; A/A [-0.027, +0.029] | 10.588 → 10.516 | -0.071 [-0.159, +0.098]; A/A [-0.112, +0.546] | clear |
| 5.2.15 | Emacs | Extended | 3.671 → 3.648 | -0.004 [-0.096, +0.038]; A/A [+0.003, +0.061] | 0.188 → 0.201 | +0.013 [-0.019, +0.033]; A/A [-0.027, +0.074] | 10.829 → 10.943 | +0.114 [-0.408, +0.593]; A/A [-0.222, +2.075] | inconclusive: wrapped-line p99 |
| 5.2.15 | vi | Default | 3.822 → 3.791 | -0.025 [-0.145, +0.037]; A/A [-0.047, +0.048] | 1.510 → 0.171 | -1.339 [-2.225, +0.023]; A/A [-2.241, +0.238] | 11.972 → 10.581 | -1.391 [-4.443, +0.230]; A/A [-4.449, +0.218] | inconclusive: keystroke p99; inconclusive: wrapped-line p99; repeated below |
| 5.2.15 | vi | Extended | 3.879 → 3.949 | -0.021 [-0.122, +0.120]; A/A [-0.130, +0.173] | 0.264 → 0.206 | -0.058 [-1.480, +0.043]; A/A [-1.479, +0.670] | 12.953 → 10.835 | -2.119 [-18.762, +0.835]; A/A [-16.533, +6.900] | inconclusive: keystroke p99; inconclusive: wrapped-line p99; repeated below |
| 5.3.20 | Emacs | Default | 13.757 → 13.562 | -0.178 [-0.791, +0.723]; A/A [-0.168, +0.480] | 0.159 → 0.168 | +0.008 [-0.029, +0.040]; A/A [-0.020, +0.033] | 10.707 → 10.613 | -0.094 [-0.950, +0.020]; A/A [-0.753, +0.090] | inconclusive: first prompt; repeated below |
| 5.3.20 | Emacs | Extended | 14.812 → 14.496 | +0.265 [-0.191, +0.588]; A/A [-0.141, +0.918] | 0.172 → 0.189 | +0.017 [-0.013, +0.037]; A/A [-0.025, +0.014] | 11.439 → 11.391 | -0.048 [-0.915, +0.767]; A/A [-1.063, +1.007] | inconclusive: first prompt; inconclusive: wrapped-line p99; repeated below |
| 5.3.20 | vi | Default | 14.050 → 14.326 | +0.106 [-0.347, +0.903]; A/A [-0.123, +0.492] | 0.173 → 0.146 | -0.027 [-0.047, +0.018]; A/A [-0.040, +0.030] | 10.388 → 10.444 | +0.057 [-0.340, +0.764]; A/A [-0.451, +0.108] | inconclusive: first prompt; inconclusive: wrapped-line p99; repeated below |
| 5.3.20 | vi | Extended | 15.026 → 14.534 | +0.109 [-0.899, +0.701]; A/A [-0.759, +0.366] | 0.173 → 0.177 | +0.003 [-0.023, +0.024]; A/A [-0.027, +0.027] | 10.541 → 10.486 | -0.055 [-1.049, +0.004]; A/A [-1.064, +0.655] | inconclusive: first prompt; inconclusive: wrapped-line p99; repeated below |

Rounds per cell: 24.

On Bash 5.2, every first-prompt change lies between −0.05 and +0.04 ms. The
final-build repeat of the 5.2 vi extended cell cleared all three metrics. On
Bash 5.3, first prompts in the baseline varied by up to ±0.9 ms between runs,
so the 24- and 36-round cells stayed open. The 100-round runs below settle
default mode: +0.102 ms in Emacs and −0.012 ms in vi, both clear. Extended
mode measured +0.129 [+0.007, +0.327] ms in Emacs, an interval just above
zero, and −0.148 ms in vi. Its 36-round repeat on Bash 5.3 Emacs also raised
keystroke p99 by +0.044 [+0.019, +0.060] ms. The host was busier during the
100-round extended runs, as their baseline values show, which left their
keystroke and wrapped-line cells inconclusive.

Wrapped-line p99 is 10.4–12 ms in both arms of the quieter runs, so the
0.1.0-dev plugin had the same cost at the end of a long line. On the final
build, the change is clear in four cells: Bash 5.2 vi extended
(+0.029 [−0.133, +0.216] ms), Bash 5.3 Emacs default in both repeats
(−0.095 and −0.002 ms) and Bash 5.3 vi extended (+0.021 ms). In the other
wrapped-line cells, a few tail samples set p99 and leave the change
inconclusive.

| Bash | Mode | Settings | Rounds | First prompt (ms) | Change; A/A | Keystroke p99 (ms) | Change; A/A | Wrapped-line p99 (ms) | Change; A/A |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 5.3.20 | Emacs | Default | 100 | 14.553 → 14.922 | +0.102 [-0.030, +0.264]; A/A [-0.091, +0.275] (clear) | 0.168 → 0.157 | -0.011 [-0.031, +0.017]; A/A [-0.018, +0.106] (clear) | 10.534 → 10.533 | -0.002 [-0.116, +0.064]; A/A [-0.120, +0.074] (clear) |
| 5.3.20 | vi | Default | 100 | 15.680 → 15.611 | -0.012 [-0.183, +0.130]; A/A [-0.324, +0.065] (clear) | 0.205 → 0.171 | -0.034 [-0.137, +0.003]; A/A [-0.138, +0.006] (clear) | 11.048 → 11.094 | +0.046 [-2.256, +6.642]; A/A [-1.658, +3.402] (inconclusive) |
| 5.3.20 | Emacs | Extended | 100 | 16.403 → 16.672 | +0.129 [+0.007, +0.327]; A/A [-0.100, +0.363] (increase) | 0.748 → 0.715 | -0.033 [-0.400, +0.323]; A/A [-0.506, +0.222] (inconclusive) | 17.762 → 17.603 | -0.158 [-2.745, +3.864]; A/A [-4.412, +1.513] (inconclusive) |
| 5.3.20 | vi | Extended | 100 | 19.668 → 19.293 | -0.148 [-0.520, +0.319]; A/A [-0.592, +0.390] (clear) | 1.813 → 1.783 | -0.030 [-0.458, +0.369]; A/A [-0.029, +1.432] (inconclusive) | 22.907 → 23.987 | +1.080 [-2.781, +3.631]; A/A [-1.926, +4.309] (inconclusive) |

The final build changes how the input is repainted when it holds newlines or
tabs, and it keeps a word's resolved color while a newer lookup runs. The
same eight cells were rerun with only the after build replaced. First prompt
and wrapped-line p99 show no resolved increase. Keystroke p99 is clear in five
cells and inconclusive in one. The two Bash 5.2 extended cells resolved
+0.015 [+0.003, +0.028] ms in
Emacs and +0.034 [+0.012, +0.071] ms in vi. The earlier build measured
+0.013 [−0.019, +0.033] ms for the Emacs cell, so these likely resolve the
extended-mode cost that was already there.

| Bash | Mode | Settings | First prompt (ms) | Change; A/A | Keystroke p99 (ms) | Change; A/A | Wrapped-line p99 (ms) | Change; A/A | Result |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 5.2.15 | Emacs | Default | 4.317 → 4.317 | +0.018 [-0.093, +0.067]; A/A [-0.038, +0.112] | 0.172 → 0.167 | -0.005 [-0.040, +0.013]; A/A [-0.041, +0.018] | 10.647 → 10.688 | +0.041 [-0.115, +0.125]; A/A [-0.196, +0.114] | clear |
| 5.2.15 | Emacs | Extended | 4.373 → 4.432 | +0.041 [-0.206, +0.136]; A/A [-0.108, +0.072] | 0.174 → 0.189 | +0.015 [+0.003, +0.028]; A/A [-0.012, +0.017] | 10.760 → 10.711 | -0.049 [-1.536, +3.623]; A/A [-1.481, +3.909] | increase: keystroke p99; inconclusive: wrapped-line p99 |
| 5.2.15 | vi | Default | 4.720 → 4.511 | -0.041 [-0.200, +0.123]; A/A [-0.141, +0.117] | 0.190 → 0.176 | -0.013 [-0.031, -0.001]; A/A [-0.011, +1.204] | 12.836 → 11.802 | -1.033 [-2.453, +2.160]; A/A [-2.941, +0.930] | inconclusive: keystroke p99; inconclusive: wrapped-line p99 |
| 5.2.15 | vi | Extended | 4.358 → 4.434 | +0.038 [-0.079, +0.190]; A/A [-0.220, +0.103] | 0.164 → 0.197 | +0.034 [+0.012, +0.071]; A/A [-0.006, +0.026] | 10.656 → 11.008 | +0.351 [-2.593, +1.632]; A/A [-2.973, +1.238] | increase: keystroke p99; inconclusive: wrapped-line p99 |
| 5.3.20 | Emacs | Default | 14.793 → 14.855 | -0.029 [-0.365, +0.355]; A/A [-0.360, +0.544] | 0.154 → 0.166 | +0.012 [-0.017, +0.030]; A/A [-0.039, +0.005] | 10.772 → 10.897 | +0.126 [-0.076, +1.862]; A/A [-0.142, +0.268] | inconclusive: wrapped-line p99 |
| 5.3.20 | Emacs | Extended | 14.898 → 15.076 | -0.138 [-0.677, +0.300]; A/A [-0.711, +0.219] | 0.141 → 0.149 | +0.007 [-0.013, +0.022]; A/A [-0.042, -0.002] | 10.613 → 10.634 | +0.021 [-0.077, +0.103]; A/A [-0.082, +0.298] | clear |
| 5.3.20 | vi | Default | 14.390 → 14.540 | +0.183 [-0.276, +0.499]; A/A [-0.155, +0.318] | 0.135 → 0.132 | -0.004 [-0.034, +0.020]; A/A [-0.035, +0.009] | 10.467 → 10.498 | +0.031 [-0.048, +0.116]; A/A [-0.051, +0.090] | inconclusive: first prompt |
| 5.3.20 | vi | Extended | 16.117 → 16.232 | +0.164 [-0.373, +0.498]; A/A [-0.367, +0.294] | 0.154 → 0.164 | +0.009 [-0.021, +0.048]; A/A [-0.026, +0.042] | 10.446 → 10.550 | +0.104 [-0.080, +0.873]; A/A [-0.099, +1.202] | inconclusive: first prompt; inconclusive: wrapped-line p99 |

Rounds per cell: 24.

### Repeated cells

The first-pass values stay in the tables above. Repeats ran 36 rounds on the
same builds, except syntax highlighting. Its repeats used the final build,
which differs from the first-pass build by one string in the history deletion
prompt. The project-context repeats used the timing driver after a fix to its
prompt detection, which changes no timed step.

| Cell | Metric | First run (ms): change; A/A | Repeat (ms): change; A/A | Repeat result |
| --- | --- | ---: | ---: | --- |
| history-search, 5.3.20 vi, default | First prompt | +0.239 [-0.053, +0.328]; A/A [-0.263, +0.129] (30) | -0.094 [-0.448, +0.498]; A/A [-0.074, +0.408] (36) | clear |
| history-search, 5.3.20 vi, default | Keystroke p99 | -0.347 [-1.613, +0.036]; A/A [-1.858, +1.177] (30) | -0.001 [-0.073, +0.021]; A/A [-0.050, +0.034] (36) | clear |
| history-search, 5.2.15 emacs, default | First prompt | +0.030 [-0.009, +0.126]; A/A [+0.007, +0.059] (30) | -0.010 [-0.077, +0.100]; A/A [-0.072, +0.111] (36) | clear |
| history-search, 5.2.15 emacs, default | Keystroke p99 | +0.438 [-0.860, +1.794]; A/A [-1.494, +0.012] (30) | +0.009 [-0.018, +0.039]; A/A [-0.021, +0.118] (36) | clear |
| project-context, 5.2.15 emacs, default | First prompt | -0.002 [-0.145, +0.078]; A/A [-0.147, +0.083] (24) | -0.017 [-0.058, +0.032]; A/A [-0.026, +0.025] (36) | clear |
| project-context, 5.2.15 emacs, default | Keystroke p99 | -1.552 [-2.312, +0.471]; A/A [-1.628, +0.730] (24) | +0.002 [-0.003, +0.006]; A/A [-0.003, +0.009] (36) | clear |
| project-context, 5.2.15 vi, default | First prompt | -0.101 [-0.212, +0.180]; A/A [-0.344, +0.048] (24) | +0.051 [-0.031, +0.152]; A/A [-0.037, +0.069] (36) | clear |
| project-context, 5.2.15 vi, default | Keystroke p99 | +0.216 [-0.053, +0.453]; A/A [-0.061, +0.272] (24) | +0.077 [-0.002, +0.819]; A/A [-0.004, +0.617] (36) | inconclusive |
| project-context, 5.2.15 vi, BINIT_PROJECT_SCOPE=ancestors | First prompt | +0.073 [-0.126, +0.221]; A/A [-0.342, +0.085] (24) | +0.078 [+0.047, +0.116]; A/A [-0.076, +0.048] (36) | increase |
| project-context, 5.2.15 vi, BINIT_PROJECT_SCOPE=ancestors | Keystroke p99 | +0.149 [-1.288, +1.024]; A/A [-1.042, +1.119] (24) | +0.003 [-0.003, +0.008]; A/A [-0.002, +0.010] (36) | clear |
| project-context, 5.3.20 emacs, trusted profile | First prompt | +0.247 [-0.180, +1.635]; A/A [-1.355, +0.734] (24) | -0.136 [-0.291, +0.041]; A/A [-0.456, +0.168] (36) | clear |
| project-context, 5.3.20 emacs, trusted profile | Keystroke p99 | -0.582 [-1.148, +0.300]; A/A [-1.062, +0.494] (24) | -0.000 [-0.006, +0.003]; A/A [-0.004, +0.009] (36) | clear |
| project-context, 5.3.20 vi, trusted profile | First prompt | -0.325 [-0.982, +0.208]; A/A [-0.498, +0.811] (24) | +0.099 [-0.293, +0.548]; A/A [-0.065, +0.583] (36) | clear |
| project-context, 5.3.20 vi, trusted profile | Keystroke p99 | +0.145 [-0.282, +0.821]; A/A [+0.000, +0.525] (24) | -0.003 [-0.010, +0.004]; A/A [-0.006, +0.009] (36) | clear |
| directory-jump, 5.2.15 vi, BINIT_JUMP_RANK=frecency | First prompt | -0.021 [-0.654, +0.102]; A/A [-0.271, +0.064] (24) | +0.014 [-0.038, +0.049]; A/A [-0.030, +0.046] (36) | clear |
| directory-jump, 5.2.15 vi, BINIT_JUMP_RANK=frecency | Keystroke p99 | -0.137 [-1.172, +0.553]; A/A [-1.118, +0.364] (24) | +0.000 [-0.006, +0.007]; A/A [-0.006, +0.006] (36) | clear |
| directory-jump, 5.3.20 emacs, default | First prompt | -0.080 [-0.326, +0.314]; A/A [-0.112, +0.567] (24) | -0.246 [-0.413, +0.054]; A/A [-0.216, +0.174] (36) | clear |
| directory-jump, 5.3.20 emacs, default | Keystroke p99 | +1.608 [-0.005, +3.766]; A/A [-0.002, +0.314] (24) | -0.041 [-1.611, +0.394]; A/A [-1.987, +0.003] (36) | inconclusive |
| syntax-highlighting, 5.2.15 vi, default | First prompt | -0.025 [-0.145, +0.037]; A/A [-0.047, +0.048] (24) | -0.012 [-0.037, +0.016]; A/A [-0.077, +0.031] (36) | clear |
| syntax-highlighting, 5.2.15 vi, default | Keystroke p99 | -1.339 [-2.225, +0.023]; A/A [-2.241, +0.238] (24) | +0.046 [-0.022, +1.302]; A/A [-0.025, +0.845] (36) | inconclusive |
| syntax-highlighting, 5.2.15 vi, default | Wrapped-line p99 | -1.391 [-4.443, +0.230]; A/A [-4.449, +0.218] (24) | -0.972 [-5.100, +1.444]; A/A [-4.505, +5.465] (36) | inconclusive |
| syntax-highlighting, 5.2.15 vi, BINIT_HIGHLIGHT_EXTENDED=1 | First prompt | -0.021 [-0.122, +0.120]; A/A [-0.130, +0.173] (24) | -0.045 [-0.089, +0.033]; A/A [-0.079, +0.039] (36) | clear |
| syntax-highlighting, 5.2.15 vi, BINIT_HIGHLIGHT_EXTENDED=1 | Keystroke p99 | -0.058 [-1.480, +0.043]; A/A [-1.479, +0.670] (24) | +0.001 [-0.012, +0.016]; A/A [-0.038, +0.010] (36) | clear |
| syntax-highlighting, 5.2.15 vi, BINIT_HIGHLIGHT_EXTENDED=1 | Wrapped-line p99 | -2.119 [-18.762, +0.835]; A/A [-16.533, +6.900] (24) | +0.029 [-0.133, +0.216]; A/A [-0.159, +0.219] (36) | clear |
| syntax-highlighting, 5.3.20 emacs, default | First prompt | -0.178 [-0.791, +0.723]; A/A [-0.168, +0.480] (24) | +0.349 [+0.182, +0.727]; A/A [-0.094, +0.729] (36) | increase |
| syntax-highlighting, 5.3.20 emacs, default | Keystroke p99 | +0.008 [-0.029, +0.040]; A/A [-0.020, +0.033] (24) | -0.001 [-0.032, +0.017]; A/A [-0.033, +0.030] (36) | clear |
| syntax-highlighting, 5.3.20 emacs, default | Wrapped-line p99 | -0.094 [-0.950, +0.020]; A/A [-0.753, +0.090] (24) | -0.095 [-0.310, +0.215]; A/A [-0.295, +0.158] (36) | clear |
| syntax-highlighting, 5.3.20 emacs, BINIT_HIGHLIGHT_EXTENDED=1 | First prompt | +0.265 [-0.191, +0.588]; A/A [-0.141, +0.918] (24) | +0.078 [-0.184, +0.651]; A/A [-0.385, +0.696] (36) | inconclusive |
| syntax-highlighting, 5.3.20 emacs, BINIT_HIGHLIGHT_EXTENDED=1 | Keystroke p99 | +0.017 [-0.013, +0.037]; A/A [-0.025, +0.014] (24) | +0.044 [+0.019, +0.060]; A/A [-0.011, +0.039] (36) | increase |
| syntax-highlighting, 5.3.20 emacs, BINIT_HIGHLIGHT_EXTENDED=1 | Wrapped-line p99 | -0.048 [-0.915, +0.767]; A/A [-1.063, +1.007] (24) | +0.009 [-0.306, +0.320]; A/A [-0.306, +1.207] (36) | inconclusive |
| syntax-highlighting, 5.3.20 vi, default | First prompt | +0.106 [-0.347, +0.903]; A/A [-0.123, +0.492] (24) | +0.098 [-0.256, +0.360]; A/A [-0.282, +0.180] (36) | inconclusive |
| syntax-highlighting, 5.3.20 vi, default | Keystroke p99 | -0.027 [-0.047, +0.018]; A/A [-0.040, +0.030] (24) | -0.016 [-0.029, +0.013]; A/A [-0.034, +0.004] (36) | clear |
| syntax-highlighting, 5.3.20 vi, default | Wrapped-line p99 | +0.057 [-0.340, +0.764]; A/A [-0.451, +0.108] (24) | +0.066 [-0.095, +0.265]; A/A [-0.102, +0.103] (36) | inconclusive |
| syntax-highlighting, 5.3.20 vi, BINIT_HIGHLIGHT_EXTENDED=1 | First prompt | +0.109 [-0.899, +0.701]; A/A [-0.759, +0.366] (24) | +0.501 [-0.064, +0.725]; A/A [+0.021, +0.483] (36) | inconclusive |
| syntax-highlighting, 5.3.20 vi, BINIT_HIGHLIGHT_EXTENDED=1 | Keystroke p99 | +0.003 [-0.023, +0.024]; A/A [-0.027, +0.027] (24) | +0.011 [-0.011, +0.028]; A/A [-0.019, +0.019] (36) | clear |
| syntax-highlighting, 5.3.20 vi, BINIT_HIGHLIGHT_EXTENDED=1 | Wrapped-line p99 | -0.055 [-1.049, +0.004]; A/A [-1.064, +0.655] (24) | +0.021 [-0.184, +0.106]; A/A [-0.267, +0.050] (36) | clear |

### Deferred work

| Plugin | Deferred | Reason |
| --- | --- | --- |
| Git prompt | A persistent status daemon or index cache | Status costs 14–15 ms per prompt in a small repository and the full 50 ms wait on the 20,000-file fixture. A cached daemon could answer sooner, but it is a new runtime dependency that needs its own approval. |
| Git completion | Cancelling installed definitions once they run | Definitions change live Bash state, so a running script cannot be stopped without losing that state. Discovery can be cancelled, and sourcing stays an explicit step. |
| History search | A larger persistent index, sync and accounts | A larger index needs a new storage dependency and approval. Search stays bounded to the newest 1,000 entries. Sync and accounts are out of scope. |
| Autosuggestions | Previews from arbitrary shell completion, and asynchronous history | Shell completion functions can have side effects and need live definitions, so previews use isolated providers only. The history strategy stays synchronous with fixed limits until large histories are qualified. |
| Project context | Running shell profiles and creating environments | Profiles stay declarative. Creating a virtual environment or installing dependencies belongs to an explicit command. |
| Directory jump | Persistent directory history | Persistence needs private atomic storage, locked updates and merge rules for concurrent shells. History stays in the session. |
| Syntax highlighting | Rule scripts and lines over 4,096 bytes | Rule scripts would run code while typing. The ceiling stays until a larger line budget is measured. |

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
