# Candidate checks

Use `binit candidate check checks.toml` after preparing a candidate, then
`binit candidate select` after the checks pass. See [candidates](candidates.md)
for the full sequence and status values.

An explicit check plan names reviewed Bash fixtures. Each sample runs in a
fresh private home and working directory, using a private state view of the
exact candidate generation. Manager commands in that shell use the view;
checking never selects the candidate in the real state directory.

```toml
schema = 1
files = ["fixture.txt"]

[[checks]]
id = "greet"
setup = "setup.bash"
script = "greet.bash"
samples = 3
timeout_ms = 2000
max_elapsed_ms = 1000
```

Script, setup and optional fixture paths are relative to the plan. Symlinks
and traversal are refused. Scripts and fixtures are copied before execution.
`BINIT_CHECK_INPUTS` names those copies; `BINIT_CHECK_WORK` names the sample's
working directory. Setup runs before candidate initialization. A check script
must return success to the checker to pass. Use a final assertion or `return`,
rather than `exit`: an exit stops the shell before completion is recorded.
Setup or initialization that exits early cannot earn a pass either. The
receipt records the script's return separately from the shell's exit status,
so a plugin's exit trap cannot turn a failed script into acceptance.

Limits bound an explicit verification run: at most 16 checks, 40 total
samples, 128 input files and 8 MiB of input contents, with at most 2 MiB per
file. Each timeout is at most 120 seconds. Captured output is bounded to
32 KiB per stream; receipts retain short escaped previews and capture flags.
The default temporary generation-view budget is 256 MiB. A declared
`max_generation_bytes` may increase it up to 8 GiB for larger configurations.
Views are copied and removed one sample at a time. Each check shell has a
fixed clean environment and uses the selected `BINIT_BASH` interpreter.

`native` defaults to whether the generation contains the native module. Set
`native = false` explicitly to check the supported native-disabled path.
That choice is part of the receipt; it does not qualify native features.

Elapsed limits concern observed check-process wall time, including launch and
monitoring. Script checks do not by themselves qualify real-terminal typing,
menu insertion or cancellation. Those need the corresponding terminal corpus.
Use generous process limits for behavior checks. Performance qualification
still needs paired measurements against a preserved baseline.

Receipts bind the prepared candidate and selected reference, configuration,
declared input bytes and policy, Binit executable, Bash executable and observed
initial runtime files. Selection rechecks those bindings. Changed, missing,
failed or incomplete evidence prevents selection. A new check attempt
invalidates the previous acceptance receipt before executing any fixture.

External resources used by trusted scripts remain their responsibility unless
declared. Binit does not discover or replay private command history. A private
home/state view is not a security sandbox; scripts retain the account's
authority. Receipts are operational evidence, not an attestation against the
account owner.

The run directory lives under `check-runs` in Binit's state directory. It
retains the input snapshot, per-sample results and final `receipt.json`.
The candidate record identifies that run and hashes its receipt. Output
previews are JSON-escaped and limited to 256 bytes per stream. Timeouts and
handled interruptions stop ordinary children in the owned process group;
programs that deliberately leave that group fall outside this cleanup rule.
