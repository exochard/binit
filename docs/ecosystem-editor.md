# Common editor traces

The [editor contract](../benchmarks/ecosystem-editor.json) fixes a shared Bash
and terminal trace before measurement. It compares plain Readline, Binit's
native completion with live history enabled, and the pinned standalone ble.sh
editor. Plain Readline is a control with fewer editor features. A framework
without its own editor needs a separately checked pairing.

Each editor uses Emacs and vi modes with synthetic histories of 0, 64 and
2,048 entries. The oldest entry in the large history contains a unique marker.
Binit's live history searches at most the newest 1,000 entries, so that marker
is expected to be absent there. It remains a reported difference. A failed
search or interaction is retained as a failure and cannot become a timing row.

Large-candidate cancellation time starts at the first cancellation input,
including any Readline confirmation response, and ends at the accepted-command
prompt. For Binit and ble.sh, the observer waits for visible candidates before
sending the cancellation key. It sends Enter once the candidates are gone
and the cursor is back at the end of the original command. The receipt records
the time to this acknowledgment and the accepted-command latency separately.

The trace checks Unicode input, bracketed multiline paste, a wrapped prompt,
first and warm completion, a 1,024-candidate cancellation, and recovery after
interrupting a line. Captured argument bytes and return status establish the
accepted command. The ordinary slow completion function changes globals in the
calling shell; its behavior is recorded separately from the isolated provider.
The isolated case types while the provider is active and checks the input,
stale output and owned-process cleanup. The immediate resource and descriptor
inventories are retained as separate, non-atomic observations. A disappearing
descriptor makes an inventory partial. The first complete single-shell baseline sets the descriptor count. Cleanup
requires a complete single-shell observation at or below that count within 100 ms; every 5 ms
check is retained. Earlier failed observations remain failures.

In ble.sh vi insert mode, Ctrl-C enters normal mode and retains the line. The
recovery trace records that difference, deletes the line with `dd`, then uses
`i` to return to insert mode before the next command.

Input-response timings retain every terminal observation. Each of 20 pairs
contains 50 insertions and 50 deletions, giving 1,000 of each per case and run.
The runner reports p99 only with at least 1,000 observations of that key. The
2 ms observer quiet interval is outside the
reported response, while terminal and scheduler costs remain included. A
response can arrive in fragments more than 2 ms apart. If the inserted or
deleted character is not yet on screen at the first quiet interval, the
observer keeps reading for up to 2 s from the key. It measures to the last
output before a quiet interval with the character's effect visible. A
response visible at the first quiet interval keeps its original measurement.
Each case's receipt counts its late acknowledgments per key. Two
runs of 20 alternating blocks produce independent per-run distributions. Small
runs diagnose traces and cannot complete the corpus.

The retained Binit binary must load its matching native module from a newly
prepared private generation. The ble.sh entry and its built dependencies are
bound to the pinned corpus. Synthetic files and private homes stay under project
scratch. Resource observations are non-atomic snapshots with explicit missing
children, not peak-memory measurements.

The framework-plus-editor track loads ten identical startup plugins through
the real Binit, Bash-it and Oh My Bash loaders. Bash-it and Oh My Bash use
their existing matched-fixture overrides, including disabled themes and update
checks. The ble.sh pairings disable syntax, filename and variable highlighting
and syntax-driven automatic completion while retaining history suggestions.
Those selections target the same visible feature categories as the Binit
fixture; their menus, history windows and provider execution still differ.
Framework-only and editor-only controls must pass before a pairing can enter
the timed matrix. Failed paired effects remain visible as unequal behavior.

```bash
python3 scripts/bench_ecosystem_editor.py --sources tmp/ecosystem --binary target/release/binit --work tmp/ecosystem/editor-run --output tmp/ecosystem/editor-run.json
```

Before timing, the runner checks each standalone editor, then attempts the same
Binit-plus-ble.sh trace in both keymaps. An observed paired effect mismatch
leaves that pairing unqualified. A preparation, identity, timeout or harness
error rejects the corpus. A pairing that passes both joins the measured cases.
`--skip-pairing` is available for
short diagnostics and prevents completion of the corpus. The corpus does not
close the separate universal stateful-provider responsiveness or public-release
gates.

Each launch uses a private runtime directory in project scratch. The runner
retains terminal output through shell exit and rejects abnormal or forced exits.
Earlier receipts keep their original environment and close-observation scope.

The Binit plus ble.sh pairing probes a local `alpha_pair_probe` filename after
checking its enabled isolated provider and ble's Tab binding. Completion of that
file without a provider start identifies this feature mismatch. A passing pair
must observe the exact provider PID as a non-zombie owned descendant before
sending the cancellation input. A stalled Tab or
missing observation remains a failed measurement, not a compatibility decision.
