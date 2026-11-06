# Binit

Install custom Bash plugins and control when they load. Binit keeps reviewed plugin snapshots and lets you roll back a change. Its Bash runtime shares hooks and loads Git's full Bash completion definitions on the first request.

## Try it

Requires Linux, Rust and Cargo, Bash 5.2+, Python 3.12+, ShellCheck, Git, and GNU `timeout`. Verification currently runs on Bash 5.3.20. The Make targets also use a systemd user manager; direct commands for other environments are in [CONTRIBUTING.md](CONTRIBUTING.md).

```bash
make build
make verify
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

## Manage plugins

From the project root, inside the demo:

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

The current prototype loads local plugin directories. The [0.1 release plan](docs/release-0.1.md) includes pinned Git sources, broader loading policies, and qualification of an optional Flyline provider with Readline fallback. Its first gate is a measured improvement in Bash execution; the prototype has no demonstrated interpreter speedup yet.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and [the benchmark procedure](docs/benchmarks.md) for engine experiments. Original Binit code is under [MIT](LICENSE). Third-party components retain their own licenses.
