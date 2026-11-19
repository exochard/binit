# Binit documentation

## How Binit works

You keep plugin lines in `~/.bashrc`, after `eval "$(binit init)"`:

```bash
eval "$(binit init)"
binit plugin user/repo ver=v1.2.0 files=init.bash
binit plugin ~/src/tool load=command commands=tool
```

`binit sync` reads those lines, pins every source (a Git commit, a
checksum-pinned release asset or a local directory) and publishes a
*generation*: an immutable snapshot of the runtime, the plugin files and their
load plan, with every file listed by digest. A shell verifies the selected
generation before it sources anything and never touches the network at
startup. Updates build a new generation, so running shells keep their code and
`binit rollback` brings the previous one back for new shells.

Each plugin loads when its line says: at startup, at the first prompt, right
after the prompt appears (`wait`), on first use of a command or completion, on
a timer, an event or a watched file. Binit tracks the functions, aliases,
completions and prompt segments a plugin adds, so `binit profile` can report
what each one costs and `binit unload` can remove what it owns.

The `binit` executable embeds a native module for Bash 5.3 with Readline 8.3.
It draws the completion menu, history search and suggestions inside Bash and
can run a slow completion in a separate process that typing cancels. Other
supported Bash versions keep Readline's ordinary completion. Your system shell
and login shell stay as they are; installation is per user.

## Start here

| Page | Covers |
| --- | --- |
| [Installation](install.md) | Install the release or a source build for one user, and uninstall |
| [Plugin interface](plugins.md) | Plugin lines, `binit sync`, sources, keys and package manifests |
| [Loading and shell effects](runtime-features.md) | Timers, events, watched files, reports and unload |
| [Source trial](source-trial.md) | Build from the source release and try Binit in a separate Bash |

## Editing and completion

| Page | Covers |
| --- | --- |
| [Editing](editor.md) | Completion menu, suggestions and editing keys |
| [History](history.md) | History search and the inline panel |
| [Isolated completion providers](completion-providers.md) | Slow completions in a separate process, cancelled by typing |
| [Eligible completion](eligible-completion.md) | Which providers can run isolated, and their limits |

## Checking and recovering

| Page | Covers |
| --- | --- |
| [Candidate generations](candidates.md) | Prepare and inspect a configuration before selecting it |
| [Candidate checks](candidate-checks.md) | Run declared checks against a candidate before it goes live |
| [Diagnosing slow startup](causal-diagnostics.md) | `binit diagnose`: which plugin or hook took the time |
| [Manager extensions](extensions.md) | Plugin-provided `manager.bash` commands |
| [Development plugins](first-party.md) | The plugins in the source release and how to load one |
| [Existing plugins](plugin-compatibility.md) | Checking unmodified Git completion, bash-completion, NVM and fzf |

## Design

| Page | Covers |
| --- | --- |
| [Architecture](architecture.md) | The Rust manager, verified generations and the native Bash module |
| [Security review](security-review.md) | What independent reviews checked, found and fixed |
| [Licensing](licensing.md) | MIT for Binit's code, GNU terms for Bash and Readline, and what each release archive holds |

## How performance is measured

Measured results for each release are published on the
[releases page](https://github.com/exochard/binit/releases). These pages
describe the method.

| Page | Covers |
| --- | --- |
| [Publication scoreboard](publication-scoreboard.md) | The scored aspects, competitors, statistics and verdict rules |
| [Ecosystem corpus](ecosystem-corpus.md) | Pinned Bash-it, Oh My Bash, ble.sh and Zinit sources, shared effects and adapters |
| [Primed framework track](ecosystem-primed.md) | Startup after each framework has prepared its state |
| [Template configurations](ecosystem-defaults.md) | Each framework's documented defaults |
| [Editor traces](ecosystem-editor.md) | Keystroke and completion traces against ble.sh |
| [Interactive performance](interactive-performance.md) | Typing, history search and completion in a terminal |
| [Performance scope](performance.md) | Shell-opening time and Bash execution, measured apart |
| [Lifecycle measurements](performance-program.md) | Preparation, activation, lazy commands and unload |
| [Owned unload](owned-unload-performance.md) | How unload finds what a plugin owns |
| [Runtime workflows](runtime-workflows.md) | Git shortcut activation and a watched toolchain |
| [Bash execution benchmarks](benchmarks.md) | The fixed script suite and the optional interpreter build |
