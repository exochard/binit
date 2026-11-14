# Candidate generations

Prepare and inspect a local configuration without selecting it for new shells:

```bash
binit candidate prepare config.toml
binit candidate status
binit candidate status --json
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

Candidate behavior checks and evidence-based selection are subsequent parts
of the [modern Binit milestone](modern-binit-milestone.md). A prepared candidate
has not passed those checks. These commands do not yet offer candidate
selection. Existing `prepare`, `apply`, `update` and rollback retain their
documented selection behavior.
