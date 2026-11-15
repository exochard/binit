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

## Measured template cases

The [measurement receipt](../benchmarks/results/ecosystem-defaults-x86_64.json)
contains two runs of 20 blocks for each case, after seven checked priming
launches. All 280 measured launches passed their effect and private-state
checks, and all 287 sessions, including the seven priming launches, completed
normal exits with status zero and complete terminal output. The first column
measures from launch until an executed command has returned to the prompt;
the second ends after the required-effect probe.
Values are median milliseconds, run 1 / run 2.

| Case | Accepted prompt | Required effects |
| --- | ---: | ---: |
| Bare Bash | 9.950 / 10.067 | 10.312 / 10.435 |
| Bash-it `bobby` template | 146.299 / 142.383 | 165.711 / 160.129 |
| Oh My Bash `font`, update not due | 61.797 / 59.821 | 66.233 / 64.208 |
| Oh My Bash `font`, due update declined | 61.805 / 60.317 | 65.949 / 64.835 |
| Standalone ble.sh trial | 235.574 / 239.950 | 484.417 / 490.873 |
| Bare Zsh | 4.650 / 4.714 | 5.240 / 5.317 |
| Zinit loader, no plugins | 13.948 / 14.219 | 15.241 / 15.508 |

These configurations perform different work. The standalone ble.sh probe checks
editor readiness; the framework cases check their declared template effects.
Binit has no universal installed default in this comparison. The receipt retains
all raw rows and p95 values, so the table is not an overall product ranking.

An earlier short smoke failed its source-identity check after ble.sh generated
cache files in the shared source tree. Its receipt and original observation
scope are retained alongside the completed result. Later runs use private
copies and check the source inventory. The measurement does not establish an
editor feature equivalence, a new 25% workload gain or release qualification.
The modern-Binit milestone remains unqualified while its other gates are open.
