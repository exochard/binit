# Pinned Bash ecosystem corpus

Use shared effects and small adapters for framework comparisons. Each adapter
must pass its effect checks before its timings enter the corpus. The
[source comparison](bash-ecosystem-comparison.md#comparable-performance-study)
defines the wider study; the initial implementation covers eager framework
loading. Editor and documented-default measurements remain pending.

The [source lock](../benchmarks/ecosystem.json) pins five public archives,
including ble.sh's contrib submodule. These are the revisions from the
existing source studies. They do not represent a claim about the latest
upstream versions. Archive hashes and exact extracted-file inventories are
checked before execution and after measurement. ble.sh's build outputs are
declared derivatives, hashed separately and checked for changes.

## Framework effects

The common fixture defines one function and one alias per plugin, appends its
identifier to a load-order string and exports a variable. The function records
literal arguments, changes directory and returns 37. Every function receives
an empty argument, quoting characters and a literal dollar expression. A warm
call verifies repeated use. Export visibility is checked in a child process;
alias evaluation must succeed.

| Subject | Actual loader | Control |
| --- | --- | --- |
| Binit | Prepared startup-plugin generation | Same Bash, directly sourced fixture |
| Bash-it | `bash_it.sh` with custom plugin directory | Same Bash, directly sourced fixture |
| Oh My Bash | `oh-my-bash.sh` with named custom plugins | Same Bash, directly sourced fixture |
| Zinit | `zinit load` with local plugin directories | Same Zsh, directly sourced fixture |

Each subject uses 0, 10 and 50 plugins. The zero-plugin Bash and Zsh rows are
bare-shell controls. Positive-count controls source the same script bytes
directly. Zinit rows belong to the Zsh group; their timings establish no
Bash-versus-Zsh execution result.

The matched fixture disables themes and Oh My Bash's update check, and selects
no bundled components. These are explicit overrides. Documented defaults need
separate adapters: Bash-it's `bobby` template theme and Oh My Bash's `font`
theme with its template components and update behavior. An update-disabled
configuration cannot be labelled unchanged defaults.

## Observation boundaries

Each launch receives a fresh private home and a constructed environment.
Sources and prepared generations use a warm filesystem cache. Preparation is
outside launch timing; Binit preparation time is reported separately. Existing
prompt hooks run before a common prompt suffix is appended.

Visible prompt time and usable input time are distinct. Zsh can print its
prompt before entering its editor. The harness waits for noncanonical input
mode, types `:` and completes that harmless command. Usable input requires
its execution acknowledgment followed by the next prompt. The acknowledgment
includes the terminal observer's 2 ms echo quiet interval, also recorded
separately. A separate command verifies required effects.

Readiness resource samples include the owned shell and its discovered
descendants. Approximate RSS uses resident pages from `/proc/PID/statm` and
the recorded page size. Shared mappings count in each process. Descriptor
counts and process states are separate, non-atomic observations. Disappearing
children remain explicit missing samples and mark totals partial. Zero
resident pages count as an observed zero, regardless of a separately read
state. These instantaneous samples do not measure startup process creation
or peak memory. Terminal output
is bounded at 8 MiB, with a 10-second phase timeout.

Full framework measurement requires two runs of 20 paired blocks. Subject
order reverses on alternate blocks. Raw observations and per-run summaries
remain separate. All measured executable, driver and fixture identities are
checked at pair boundaries and completion, including prepared-generation
files. A changed input fails the run and retains its partial evidence.

## Reproduce the framework stage

Acquire each repository's pinned archive through
`gh api repos/REPOSITORY/tarball/REVISION`, using names and SHA-256 values from
the source lock. Put archives under `tmp/ecosystem/NAME.tar.gz` and extract
each with its top-level archive directory removed into
`tmp/ecosystem/sources/NAME/`. Populate ble.sh's `contrib` directory from the
separately pinned contrib tree. `make` in the ble.sh source directory builds
the editor without installation.

With the development dependencies and a qualified Binit binary available:

```bash
python3 scripts/bench_ecosystem.py --sources tmp/ecosystem --binary target/release/binit --work tmp/ecosystem/framework-run --output tmp/ecosystem/framework-run.json
```

Use fresh work and output paths for each experiment. A short run can diagnose
an adapter but cannot satisfy the two-run measurement requirement. The
original Binit interpreter, interactive and lifecycle suites remain separate.

## Remaining coverage

Editor fixtures still need matched Bash/Readline, real terminal traces and
known history data. Include vi and Emacs, Unicode, multiline paste, wrapped
prompts and large candidate sets. First and warm completion need correct
insertion and cancellation checks; slow isolated providers and state-changing
providers retain separate cases. Typing p99 needs at least 1,000 input events
per case. Framework/editor pairings require compatibility evidence.

Unsupported and unequal cases belong in the result. For example, a framework
without an editor does not supply ble.sh's editor features, and a history
window limit must remain visible in a large-history comparison. This corpus
does not close either new 25% Binit workload target.
