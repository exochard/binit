# Candidate generations

Prepare and inspect a local configuration without selecting it for new shells:

```bash
binit candidate prepare config.toml
binit candidate diff
binit candidate check checks.toml
binit candidate status
binit candidate status --json
binit candidate select
```

Preparation snapshots the configuration's plugins and installs a verified
generation. It records the selected reference and configuration digest. It
does not source the plugin code or change the `current` or `previous` pointer.
Preparing a new candidate replaces the candidate record after successful
installation; a failed preparation retains the earlier record.

Status verifies the installed candidate's files before reporting it. The JSON
form includes the full candidate and reference generation identifiers, the
configuration path and its SHA-256. A first candidate has no reference until
a generation has been selected through an existing manager command.

`diff` compares verified snapshots against the recorded base. It shows added
and removed plugins, changes to their entry points or load rules, and whether
their package files changed. A first candidate is compared with an empty base.
If the active selection has moved, the diff still uses the recorded base and
shows that it is stale.

`check` runs the explicit [check plan](candidate-checks.md) and saves its receipt.
The selected generation stays in place throughout checking. `select` requires
a complete passing receipt whose declared inputs and runtime still match.
Successful selection makes the candidate current for new shells and retains
the former selection for rollback. Repeating selection preserves that previous
reference.

A later `prepare` replaces the stored candidate, so a reviewer can bind
selection to the candidate they inspected:

```bash
binit candidate select --base BASE_GEN --generation CANDIDATE_GEN --receipt RECEIPT_SHA256
```

The three values are `base_generation`, `generation` and
`check.receipt_sha256` from `status --json`. Under the mutation lock,
immediately before the pointer update, selection compares them with the
stored candidate and refuses any difference, leaving the selection unchanged.
A first candidate has no base and can only be selected without this binding.

Status reports `prepared`, `incomplete`, `failed`, `checked`, `stale` or
`selected`. `checked` describes the last receipt with matching known inputs;
selection also probes the current runtime's library resolution. A new attempt
invalidates earlier acceptance before running checks. Existing `prepare`,
`apply`, `update` and rollback retain their documented selection behavior.

Managed fixtures inherit no controlling terminal. Their standard input is
empty and output is captured; a terminal interaction fixture must create its
own PTY. Ctrl-C and declared timeouts stop the owned process group.
