# Binit

Manage Bash plugins and complete commands in a navigable menu. Binit keeps reviewed plugin snapshots and lets you roll back a change. Its Bash runtime shares hooks and loads Git's full Bash completion definitions on the first request.

The 0.1 release is in development. Current functionality and the remaining
gates are described separately in the [release requirements](docs/release-0.1.md).

The [local checkpoint](docs/stability.md) records the frozen optional Bash
engine recipe, verification commands and compatibility disposition.

## Plugin lines

Declare plugins in `~/.bashrc`, after Binit's init line:

```bash
eval "$(binit init)"
binit plugin ~/src/my-plugin                      # directory with plugin.toml
binit plugin ~/src/tool files=init.sh,comp.bash load=command commands=tool
binit plugin ~/src/thing id=thing requires=git load=prompt
binit plugin ~/src/nvm id=nvm load=wait           # after the first prompt
```

Run `binit sync` in that shell, then open a new one with `exec bash`. Sync
pins the declared sources into a verified generation; shell startup reads only
that generation and never touches the network. A shell whose lines differ from
the last sync prints one notice at its first prompt. `load=wait` is Zinit's
`wait`: the prompt appears first and the plugin loads before the next key is
read (at the first prompt when the native module is off). A source is an absolute
local directory or a Git repository.

### Git sources

```bash
binit plugin user/repo                         # https://github.com/user/repo.git
binit plugin user/repo ver=v1.2.0 files=init.bash
binit plugin https://gitlab.com/a/b.git id=b ver=main
binit plugin git@github.com:user/repo.git
```

`ver` is a tag, a branch or a full 40-character commit; without it the remote
default branch is used (a remote without one needs `ver`). `https://` URLs
cannot carry a user name or token; use a Git credential helper. `binit sync` clones what is missing and pins each line
to the full commit its `ver` resolves to. A line keeps its pinned commit
while its id, source and ver stay the same; other keys can change freely. A new
id, a different ver or a different source resolves the current commit. Only
`binit update` moves an existing pin. `binit update [ID...]` fetches and moves
the pins; a full-commit `ver` never moves. `binit update --check [ID...]`
prints `ID  OLD -> NEW  (N commits)` with the new commit subjects and changes
nothing. `binit inspect ID` shows the URL, `ver` and pinned commit. Mirrors
live in `$state/sources`.

State lives in `$BINIT_STATE`, else `$XDG_DATA_HOME/binit`, else
`~/.local/share/binit`. `--state DIRECTORY` overrides all three. The
[plugin interface](docs/plugins.md) lists the line keys and defaults.

## Try it

Source builds require Linux, Rust and Cargo, a C compiler, and Bash/Readline
development headers. The runtime targets Bash 5.2+; local verification uses
Bash 5.3.20. Bundled Git features use Git and GNU `timeout`.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the test environment and direct
commands for containers. The Make targets use a systemd user manager.

```bash
make build
make demo
```

Builds use the local Cargo cache. Run `cargo fetch --locked` first on a machine without the dependencies. Build and verification scopes cap memory at 3 GiB, disable swap, and limit CPU use to two cores.

The demo opens a separate Bash with state under `.local-demo/`. It preserves installed plugins between runs. Try `git status --sho` followed by Tab; branch completion also works in repositories with branches. Inside the shell, run:

```bash
binit profile
binit trust examples/project
cd examples/project
printf '%s\n' "$PROJECT_MESSAGE"
cd ../..
exit
```

`exit` returns to the parent shell. The demo does not install into shell startup files.

## Completion

Press Tab to open the menu when several candidates match. Tab and Shift-Tab
move through candidates; arrows and hjkl move within the grid. Press `/` to
filter, Enter to insert, or Esc to cancel. Enter accepts a completion without
executing the command. The menu redraws in place and reuses Bash's completion
providers and quoting.

```bash
binit completion
binit completion off
binit completion on
```

These commands affect the current shell. Custom macros and unrelated Tab
functions remain installed.
Resizing closes the menu while preserving input. Unsupported terminals and
very large result sets use normal Readline completion.
`BINIT_COMPLETION_MENU=0 make demo` starts with the native module disabled.

## Manage plugins with the catalog

The catalog commands below are the alternative to plugin lines. They install
plugins that every shell opened from the generation loads. From the project
root, inside the demo:

```bash
binit plugins
binit add examples/plugins/workspace
binit inspect workspace
binit shell
```

The workspace plugin adds a project-type segment such as `[Rust]` when entering a directory. Its source is ordinary Bash, with a manifest declaring when to load it.

```bash
binit disable workspace
binit enable workspace
binit update workspace
binit remove workspace
```

Changes apply to a new shell opened with `binit shell`. Running shells keep their current plugins. Updates read the recorded local source only when requested; enable and disable use the pinned snapshot. Removing a plugin preserves its original source and older generations. Dependency-breaking changes leave the current plan intact.

`binit apply` refreshes the runtime while retaining pinned plugins. Demo
startup applies a new build automatically for its next shell.

Existing Bash scripts can be installed through a user-owned TOML declaration;
the upstream checkout needs no manifest. Select files and use `load = "command"`
with `commands = ["tool"]` to source them on first use. `binit load ID` loads a
pending plugin explicitly in the current shell. See [declarations and first-use
loading](docs/plugins.md#existing-bash-scripts) for examples and scope limits.

`binit add PATH --disabled` installs a plugin for later activation. `binit prepare CONFIG` replaces the complete catalog from a configuration file; ordinary demo startup preserves the catalog. Use `BINIT_DEMO_STATE=/absolute/path make demo` for a separate demo configuration.

## Included plugins

| Plugin | Behavior |
| --- | --- |
| `git-prompt` | Reads branch data in a bounded background worker. Refreshes at most every two seconds; results appear at a following prompt. |
| `git-completion` | Loads Git's installed Bash completion script on first use, including branches and options. Existing completion definitions take precedence. |
| `project-context` | Applies explicitly trusted project variables on directory entry and restores their previous scalar/export state on exit. |

Project profiles use `.binit-project.toml`. Values are literal data. Approval is bound to the canonical file path and its SHA-256 digest; edited content needs approval again. Changed files are checked on the next directory entry or after `binit trust`.

Local plugins are trusted Bash code with the shell's full authority. Their content hashes detect changes to a prepared generation; they do not establish the author's identity. See [the plugin interface](docs/plugins.md) for the supported contract.

Git completion requires a readable script at `/usr/share/git/completion/git-completion.bash`, `/usr/share/bash-completion/completions/git`, or `/etc/bash_completion.d/git`.

## Inspect and recover

`binit profile` reports per-plugin load time and cumulative hook time in microseconds. `make bench` compares a clean Bash with the three-plugin environment using a real PTY. It covers startup and command execution separately, including builtin loops and process creation. Results go to `evidence/bench.json`. See [performance scope](docs/performance.md) for what these measurements establish.

```bash
target/release/binit status --state .local-demo
target/release/binit rollback --state .local-demo
BINIT_SAFE=1 make demo
```

Rollback selects the previous verified generation for new shells. Recovery mode skips initialization. Failed plugin loads may leave partial shell changes, so recovery starts a new shell.

## Run a script

```bash
target/release/binit run script.bash argument
target/release/binit run --engine .cache/engines/pgo/bash script.bash argument
```

`run` uses `/bin/bash` by default. An explicit `--engine` selects a local Bash
executable for that invocation. Script arguments and exit status pass through;
plugin state is not required. The experimental builds have not met the release
performance gate.

The current prototype loads local packages and source declarations. The [0.1 release plan](docs/release-0.1.md) includes pinned Git sources, installation, and further native interactive features. The experimental interpreter has not met the 25% execution target.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and [the benchmark procedure](docs/benchmarks.md) for engine experiments. Original Binit code is under [MIT](LICENSE); [GNU-linked artifacts have additional distribution requirements](docs/licensing.md).
