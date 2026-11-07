# Plugin interface

Plugins are declared as lines in `~/.bashrc` and pinned by `binit sync`. The catalog commands (`add`, `update`, `prepare`) are an alternative that snapshots local plugins without any startup-file lines. Opening a shell verifies and sources the resulting plan. Bash keeps its normal parsing and command execution.

## Plugin lines

```bash
eval "$(binit init)"
binit plugin ~/src/my-plugin
binit plugin ~/src/tool files=init.sh,comp.bash load=command commands=tool
binit plugin ~/src/thing id=thing requires=git load=prompt
```

Then run `binit sync` and open a new shell with `exec bash`. The first `binit init` creates an empty generation in the state directory (`--state`, else `$BINIT_STATE`, else `$XDG_DATA_HOME/binit`, else `~/.local/share/binit`; each must be absolute).

Grammar: `binit plugin SOURCE [KEY=VALUE...]`. `SOURCE` is an absolute directory after tilde expansion or a Git source (below); anything else fails with status 2 and the shell continues. Keys are `id`, `files`, `load`, `commands`, `completions`, `requires`, `depends` and, for Git sources, `ver`; list values are comma-separated and each key appears once. `load` is `startup` (default), `prompt`, `wait`, `command` or `completion`.

- A directory with `plugin.toml` and no keys uses that manifest.
- Otherwise `id` defaults to the directory name, and `files` to the first existing of `ID.plugin.bash`, `ID.bash`, `init.bash`, `ID.sh`, `init.sh`. Sync fails and lists these names when none exists.
- The same line twice, as when the startup file is sourced again, does nothing.

`binit sync` stores the lines of the current shell (a NUL-delimited `declared` file in the state directory: each record is an argument count followed by the arguments), then rebuilds every line-managed plugin from them and publishes a generation. A failed sync leaves the current generation in place. Catalog plugins stay; an `id` shared with one is an error. Line-managed plugins reject `enable`, `disable` and `remove`; edit the line and sync. `binit update ID` re-snapshots a local line plugin.

At startup, a line whose exact arguments match a synced plugin activates it at once: `startup` plugins load in line order, `command` and `completion` plugins install their first-use stubs, `prompt` plugins load at the first prompt, `wait` plugins right after it is drawn. Existing functions, aliases, builtins and completion specifications still win. `load=wait` is Zinit's `wait` (turbo mode): the first prompt appears at once and the plugin loads before the next key is read, in line order with other `wait` plugins. Keys typed meanwhile are kept. `binit profile` shows `pending` until then. Without the native module (`BINIT_COMPLETION_MENU=0`, no `completion.so`) wait plugins load at the first prompt instead, like `prompt`. Output printed during a wait load lands on the prompt line; a plugin that changes `PS1` or key bindings takes effect from the next prompt or line.

A synced plugin with no matching line is `undeclared` and never loads, including as a dependency; its dependants become unavailable. Declare a dependency before the plugin that needs it.

When lines and generation differ, the first prompt prints `binit: N plugin lines changed since the last sync; run binit sync` and records the shell's lines in `declared`. Catalog plugins are unaffected by lines and activate at init.

## Commands on PATH

`bin=PATH[:NAME],...` exposes package files as commands, Zinit's `as"program"`. Paths follow the rules of `files=` (relative, no symlinks, no traversal, regular files, counted in the package limits); `NAME` defaults to the file's basename and must match `^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$`. A catalog `plugin.toml` or declaration takes the same `bin` list. A line may use `bin=` without `files=`: nothing is sourced and the default-file search is skipped.

```bash
binit plugin user/repo bin=git-foo files=foo.bash
binit plugin user/scripts bin=tool.sh:tool
```

Publishing copies each command of every enabled plugin to `bin/NAME` in the generation with mode 0700, the only executable files, and records the owning plugin in `lock.toml` (`[bin]`, name to plugin ID). Two enabled plugins with the same NAME fail the sync or publish naming both. Startup verification checks that `bin/` holds exactly the locked files, regular, owned by you and mode 0700. `init.bash` prepends `bin/` to `PATH` once (a re-sourced startup file does nothing; `BINIT_SAFE=1` skips init). Commands are generation-wide: a line removed from `.bashrc` keeps its command until the next `binit sync`, and the first-prompt notice says so. A command runs from its copy, so it cannot rely on sibling files. `binit inspect ID` lists them under `Programs:`.

## Git sources

A source is one of `user/repo` (expands to `https://github.com/user/repo.git`), `https://...`, `ssh://...`, `file://...` or `user@host:path`. A source that starts with `-`, contains `::`, whitespace or control characters, or uses another scheme (`http`, `git`, `ext`, `fd`) is rejected. `id` defaults to the repository name without `.git`; add `id=NAME` when that is not a valid ID. A URL with a password or token (`https://user:TOKEN@host/...`) is rejected; use a Git credential helper or an SSH key.

`ver` is a tag, a branch or a full 40-character commit. Without it, the remote default branch is used; a remote whose HEAD points at a missing branch fails only for lines without `ver`, with "the remote has no default branch; set ver=". Sources are printable ASCII. An `https://` URL may not carry a user name, password or token (use a Git credential helper); `ssh://` and `user@host:path` keep their user, which must match `[a-z_][a-z0-9_-]{0,31}`. A rejected source that carries credentials is never echoed; the error is a fixed message.

- `binit sync` clones sources it has not seen and pins each new line to the full commit its `ver` (or the default branch) resolves to. A line keeps its pinned commit while its id, source and ver stay the same; other keys can change freely, and sync does not contact the remote for them. A new id, a different ver (including removing `ver=`) or a different source resolves the current commit. Only `binit update` moves an existing pin. Spellings of one URL (`user/repo`, `https://github.com/user/repo`, a trailing `.git` or `/`, and the case of an https or ssh host) count as the same source. A full-commit `ver` must be reachable from a branch or tag of the remote.
- `binit update [ID...]` fetches, resolves each `ver` again and publishes a generation with the new pins. Without IDs it covers every Git line plugin. A full-commit `ver` never moves, needs no fetch and is reported as `ID  pinned to commit; to track a branch again, remove ver= or set ver=BRANCH, then run binit sync`. An update that would change a plugin's ID is refused. All plugins are resolved before anything is published, so one failure leaves the current generation unchanged.
- `binit update --check [ID...]` fetches and prints `ID  OLD12 -> NEW12  (N commits)` followed by up to 20 `git log --oneline` lines, or `ID  up to date`. It publishes nothing and changes no pin.
- `binit inspect ID` shows the URL, `ver` and pinned commit.

Mirrors are bare clones in `$state/sources/<hash of URL>-<repo>`; a private ref `refs/binit/HEAD` follows the remote default branch when the remote has one. The files a line needs are read from the pinned commit as raw blobs (no attributes, filters or hooks apply) into a temporary directory under `$state/tmp` and built like a local directory, so the same limits apply: no symlinks in selected files, 128 files, 2 MiB. Git runs without a shell or prompts, with hooks and submodules off and a 120 second limit per command. Git stays in the terminal's foreground process group, so Ctrl-C stops it; sync and update remove leftover `sources/.staging-*` and `tmp/checkout-*` directories. Shell startup runs no Git and uses no network.

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

`load` accepts `startup`, `prompt`, `wait`, `completion`, or `command`. Completion plugins declare `completions = ["command"]`; first-use plugins declare `commands = ["tool"]`. Dependencies must be enabled and form an acyclic graph. An explicit dependency can load a plugin earlier than its usual trigger.

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

The runtime prepends one dispatcher to the existing `PROMPT_COMMAND`. It returns the saved command status so the original first hook sees that status. It leaves `DEBUG` traps and shell options intact. Hook failures do not stop later callbacks. The first dispatch also adds the segment prefix to `PS1`, so a `PS1` assigned later in the startup file keeps it. A later `PROMPT_COMMAND="history -a; $PROMPT_COMMAND"` keeps the dispatcher but runs it after `history -a`, so hooks then see that command's status; a plain `PROMPT_COMMAND=...` assignment drops the dispatcher.

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
