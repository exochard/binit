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

The [foundation receipt](../benchmarks/results/candidate-preparation-x86_64.json)
records matching generation IDs, file contents and modes across the previous
preparation path, the refactored path and candidate preparation at identical
paths. Two complete 20-pair runs of the unchanged lifecycle suite show no
repeated regression above 5% in another median or p95. This is a preservation
check; it claims no new performance gain and does not close the broader milestone.
The 32-function unload p95 rises 5.5% in the first run and falls 5.5% in the
second. Both remain in the raw data. The runner times the existing lifecycle
commands; candidate-specific check latency needs separate workloads.
