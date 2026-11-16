# Binit documentation

Pages are grouped by what you came for. Evidence pages and experiment logs keep
their original paths, because receipts and frozen benchmark drivers refer to
them by path and hash.

## Using Binit

| Page | Covers |
| --- | --- |
| [Installation](install.md) | Build, verify and install for one user |
| [Source trial](source-trial.md) | Build from source and try Binit in a separate Bash |
| [Plugin interface](plugins.md) | Plugin lines in `~/.bashrc`, `binit sync`, sources and options |
| [Loading and shell effects](runtime-features.md) | Load timing, deferred commands, tracked effects and unload |
| [Candidate generations](candidates.md) | Prepare and inspect a configuration before selecting it |
| [Candidate checks](candidate-checks.md) | Run reviewed checks against a candidate |
| [Editor integration](editor.md) | Completion menu, suggestions and editing keys |
| [History panel](history.md) | History search and the inline panel |
| [Isolated completion providers](completion-providers.md) | Slow completions in a separate process, cancelled by typing |
| [Manager extensions](extensions.md) | Plugin-provided `manager.bash` commands |
| [First-party plugins](first-party.md) | What Binit installs and what stays a plugin |
| [Licensing](licensing.md) | MIT for Binit's code; GNU Bash is a separate GPL component |

## Design and scope

| Page | Covers |
| --- | --- |
| [Architecture](architecture.md) | Rust manager, verified generations and the native Bash module |
| [Qualification](qualification.md) | What Binit targets and how a change qualifies |
| [Release requirements](release-0.1.md) | The 0.1 gate list and its current state |
| [Modern-Binit milestone](modern-binit-milestone.md) | The eight outcomes and the landing that carries each |
| [Local checkpoint](stability.md) | Frozen engine recipe and verification commands |
| [Performance scope](performance.md) | Shell-opening time and Bash execution, measured apart |
| [Performance program](performance-program.md) | Lifecycle measurements and how results are kept |
| [Plugin manager acceptance](parity.md) | Comparison baseline against Zinit |
| [Zinit reference](zinit-reference.md) | Zinit behavior observed at a pinned commit |
| [Causal diagnostics](causal-diagnostics.md) | Contract for attributing time to plugins and hooks |

## Comparisons

| Page | Covers |
| --- | --- |
| [Publication scoreboard](publication-scoreboard.md) | The five frozen aspects, the verdict rules and the current result |
| [Ecosystem source comparison](bash-ecosystem-comparison.md) | Bash-it, Oh My Bash, ble.sh and Zinit read from source |
| [Ecosystem corpus](ecosystem-corpus.md) | Pinned sources, shared effects and adapters |
| [Template configurations](ecosystem-defaults.md) | Each framework's documented defaults |
| [Fresh-home framework track](ecosystem-framework-evidence.md) | Startup with a new home directory |
| [Primed framework track](ecosystem-primed.md) | Startup after each framework has prepared its state ([results](ecosystem-primed-evidence.md)) |
| [Editor traces](ecosystem-editor.md) | Keystroke and completion traces against ble.sh ([historical results](ecosystem-editor-evidence.md)) |
| [Interactive performance](interactive-performance.md) | Typing, history search and completion in a PTY |

## Measurement methods

| Page | Covers |
| --- | --- |
| [Runtime workflow targets](runtime-workflows.md) | Two user workflows frozen before treatment |
| [Bash execution benchmarks](benchmarks.md) | Eight script families run on unchanged Bash |
| [Eligible completion](eligible-completion.md) | External completion worker experiment |
| [Completion feasibility](completion-feasibility.md) | Why input-wait hooks cannot give universal responsiveness |

## Qualification evidence

Each page records one landing: the gates, exact artifacts and kept failures.

| Page | Landing |
| --- | --- |
| [Startup cost](startup-cost-evidence.md) | 50-plugin startup level with Bash-it; current scoreboard |
| [Editing keys](editor-keys-evidence.md) | Ctrl-Backspace word deletion |
| [Runtime preservation](runtime-preservation-evidence.md) | Deferred activation without a subshell |
| [Runtime workflows](runtime-workflows-evidence.md) | The two workflow targets |
| [Source trial](source-trial-evidence.md) | Fresh Ubuntu build from source |
| [Completion providers](completion-provider-evidence.md) | Isolated providers |
| [Causal diagnostics](causal-diagnostics-evidence.md) | At least 99.78% of the primary fixture interval attributed |
| [Candidate checks](candidate-checks-evidence.md) | Evidence-bound candidate selection |
| [Demo interaction](demo-interaction-evidence.md) | Default editor repair, walked in Kitty |
| [Owned unload](owned-unload-performance.md) | Native unload and generation preparation |
| [Existing plugins](plugin-compatibility.md) | Unmodified plugin checks |

## Engine experiments

Optional Bash interpreter work. Most treatments were rejected; each page states
its decision first.

| Page | Covers |
| --- | --- |
| [Bash execution research](performance-research.md) | Research and the measurement plan |
| [Execution cost budget](execution-cost-budget.md) | Where whole-suite time goes |
| [Arithmetic interpreter](arithmetic-scan-experiment.md) | Arithmetic fast paths |
| [Combined execution](execution-batch-experiment.md) | Batched execution changes |
| [Parser context](parser-context-experiment.md) | Parser context and command snapshots |
| [Syntax interface](syntax-interface-experiment.md) | Parser hooks and an incremental grammar for the editor |
| [Interpreter overhead](overhead-experiments.md) | Four trials that failed the 2% screen |
| [Closing pass](closing-performance-pass.md) | Final performance and stability pass |
