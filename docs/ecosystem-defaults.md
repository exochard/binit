# Template configurations and project roles

The [frozen policy](../benchmarks/ecosystem-defaults.json) records the selected
upstream templates before measurement. Bash-it runs its silent installer in a
private copy and checks the generated `bobby` template and the complete default
profile. Oh My Bash sources its unchanged `font` template with the documented
component arrays and update checks enabled. Both use private homes.

The Oh My Bash not-due case primes its update timestamp in an untimed launch.
The due case starts with an expired timestamp and answers `n` to the actual
update prompt. The measured interval includes the automated response. Declining
keeps that case offline and does not measure an update. Synthetic private history
is reset outside the measured interval. The environment also keeps Git from
discovering a repository outside the case directory.

ble.sh is measured as a standalone editor source trial. Zinit is measured as a
Zsh loader with no selected plugins. Binit has an explicit seven-plugin source
trial, but no single installed default that can be substituted here. Bare Bash
and Zsh are separate interpreter controls. The cases perform different work;
they do not establish feature equivalence or an overall product ranking.

The ble.sh trial runs a private copy of the pinned build. Its existing upstream
runtime cache is recorded but not copied; the private trial creates its own
cache. The source-tree inventory still checks every existing derivative before
and after each run, including that previously generated cache.

Each measured case follows a successful, effect-checked priming launch. The
runner verifies the default selections and callable behavior again on every
sample. It retains the raw observations, initial and post-launch private state,
and failures. Approximate readiness resources retain the non-atomic and
missing-child limits of the framework corpus. A complete track uses two runs of
20 alternating blocks; short runs are diagnostics.

The selected defaults can source system Bash-completion files. Their presence
and hashes are recorded separately from the pinned upstream archives.

```bash
python3 scripts/bench_ecosystem_defaults.py --sources tmp/ecosystem --work tmp/ecosystem/defaults-run --output tmp/ecosystem/defaults-run.json
```

Each launch uses a private runtime directory in project scratch. The runner
retains terminal output through shell exit and rejects abnormal or forced exits.
Earlier receipts keep their original environment and close-observation scope.
