# Plugin interface

The manager snapshots local plugins through `add`, `update`, or a complete `prepare`. Opening a shell verifies and sources the resulting plan. Bash keeps its normal parsing and command execution.

## Lifecycle

`binit add PATH` installs a local package directory or a source declaration file. Add `--disabled` to keep it inactive. `binit plugins` lists the settings for the next shell; `binit profile` describes the running shell, including reasons for unavailable plugins. `binit inspect ID` shows the source, command triggers and entry digest.

`enable` and `disable` operate on the stored snapshot, even if its original source has moved. `update ID` captures changes from the recorded source; `update ID PATH` selects a new source with the same plugin ID. `remove` drops the package from the current catalog while preserving its source directory and earlier generations.

Every successful change selects a verified generation. Invalid dependency graphs leave the current generation intact. One filesystem lock serializes catalog mutations and rollback. Rollback restores the earlier catalog together with its code. Schema-1 generations remain readable and migrate on the next change.

Use `binit shell` to open a new Bash with the selected generation. Existing shells keep their own state. `prepare CONFIG` imports the complete catalog, including disabled packages.

## Package

Each plugin directory contains `plugin.toml` and a Bash entry file:

```toml
schema = 1
id = "example"
entry = "plugin.bash"
load = "prompt"
requires = ["git"]
depends = []
```

`load` accepts `startup`, `prompt`, `completion`, or `command`. Completion plugins declare `completions = ["command"]`; first-use plugins declare `commands = ["tool"]`. Dependencies must be enabled and form an acyclic graph. An explicit dependency can load a plugin earlier than its usual trigger.

An existing completion specification blocks the corresponding completion plugin, including when another plugin depends on it. Dependants become unavailable. Plugins must use unique function names and respect the runtime's `__binit_` namespace.

Package files must be regular files. Symlinks are rejected. Limits are 128 files and 2 MiB per package, with directory nesting capped at 16 levels. The entry path must stay inside the package. Unknown manifest fields fail validation with an error.

## Existing Bash scripts

Keep a declaration outside an upstream checkout, for example `tools.toml`:

```toml
schema = 1
id = "tools"
path = "../checkout"
files = ["init.sh", "shell/completion.bash"]
load = "command"
commands = ["tool"]
```

Install it with `binit add tools.toml`. Configuration files used by `prepare`
can also name declarations in their plugin paths. The upstream directory
needs no Binit manifest.

`path` is relative to the declaration, or absolute. Paths are literal: tilde,
environment variables and globs are not expanded. `files` selects between
one and 125 regular Bash scripts. They are copied with their relative layout
and sourced in listed order. Symlinks at any selected path component are
rejected. The package limit includes the declaration and generated files.

`update ID` rereads the recorded declaration and selected source files.
Enable, disable, apply and rollback use pinned snapshots. `$BASH_SOURCE`
points into that snapshot; scripts must have their required external tools
and resources available. Scripts are sourced inside a loader function, so
top-level `declare` follows Bash function scope. Persistent declarations need
`declare -g` or ordinary assignments.

## First-use loading

A `command` plugin stays pending until one of its declared names is called.
The loader removes its own wrappers, sources the plugin once in the calling
Bash, then invokes the requested function or executable with the original
arguments. Its return status passes through. Exports and directory changes
remain in that shell. Ordinary commands acquire no dispatcher or `DEBUG` trap.

Names start with a letter and contain at most 64 letters, digits, underscores
or hyphens. Existing functions, aliases, builtins and keywords block the
plugin before it loads; `binit profile` reports the conflict. External
executables can be triggers. Defining a function or alias for another trigger
after initialization also blocks loading, preserving the custom definition
before the upstream script can replace it.

`command tool` bypasses a function wrapper, as it normally does in Bash.
Calls in a subshell, pipeline or command substitution initialize that child;
the parent retains its pending state. A trigger invokes functions or
executables; aliases created during loading are subject to Bash's normal
parse-time expansion.

Inside an initialized shell, `binit load ID` loads a pending plugin explicitly.
Loaded plugins are not sourced again. A failed or blocked plugin reports its
reason rather than retrying automatically. Source failures may leave partial
effects, as described below.

## Hooks

```bash
example_prompt() {
    binit_segment example "ready"
}
binit_on prompt example_prompt
```

`binit_on` registers a callback while the plugin loads. Events are `prompt` and `directory`. Callbacks receive the previous command's status as `$1` and the current directory as `$2`. A directory event runs when the next prompt observes a changed `$PWD`.

`binit_segment NAME VALUE` adds a prompt segment. Control characters are removed and values are limited to 80 bytes under the C locale. Segment text is stored as data in a prompt variable. Shell expansion reads that variable when drawing the prompt. If `promptvars` is disabled, the runtime leaves `PS1` intact.

The runtime prepends one dispatcher to the existing `PROMPT_COMMAND`. It returns the saved command status so the original first hook sees that status. It leaves `DEBUG` traps and shell options intact. Hook failures do not stop later callbacks.

## Project data

```toml
label = "example"

[env]
PROJECT_MODE = "development"
```

Run `binit trust DIRECTORY` after reviewing the file. Trust records belong to the selected private state directory. Project values use NUL-delimited transport and quoted assignment, without `eval`. Bash control variables such as `PATH` and `POSIXLY_CORRECT` are rejected. Arrays and integer variables are skipped, as are readonly variables and namerefs.

The runtime restores prior values and export declarations on the next directory change. If a variable changes to an incompatible type while the project is active, restoration leaves it intact and reports the conflict. Project profiles explicitly configure environment variables. External programs retain their normal permissions.

## Trust and loading failures

State directories must belong to the current user with mode 700. `binit init` verifies the selected generation's lock digest and every locked file before emitting a source command. Publishing a new plan changes an atomic generation pointer. Rollback verifies the previous generation before selecting it.

Sourced plugin code has full shell access and can bypass the interface. A failed `source` cannot be rolled back reliably. The runtime reports `failed`; use a new recovery shell if the plugin altered important state. An actor who can rewrite all same-user state is outside the integrity guarantee.

A fork of Bash would offer deeper hooks but would require maintaining interpreter changes. Keeping the runtime in Bash preserves the existing execution engine. The cost is that hook coordination remains cooperative, and arbitrary plugin code cannot be isolated inside the live shell.
