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

Grammar: `binit plugin SOURCE [KEY=VALUE...]`. Sources are absolute local directories, Git repositories, `release:OWNER/REPO` assets or `snippet:https://...` scripts. Common keys are `id`, `files`, `bin`, `load`, `commands`, `completions`, `requires` and `depends`. Acquisition and lifecycle keys are described below. List values are comma-separated and each key appears once. `load` is `startup` (default), `prompt`, `wait`, `command`, `completion`, `timed`, `event` or `watch`. See [scheduling and lifecycle](runtime-features.md) for `delay_ms`, `when`, `event` and `watch`.

- A directory with `plugin.toml` and no keys uses that manifest.
- Otherwise `id` defaults to the directory name, and `files` to the first existing of `ID.plugin.bash`, `ID.bash`, `init.bash`, `ID.sh`, `init.sh`. Sync fails and lists these names when none exists.
- The same line twice, as when the startup file is sourced again, does nothing.

`binit sync` sends the lines of the current shell to the manager, which records them under its lock (a NUL-delimited `declared` file in the state directory: each record is an argument count followed by the arguments), so another shell syncing at the same time cannot change them; it then rebuilds every line-managed plugin from them and publishes a generation. A failed sync leaves the current generation in place. Catalog plugins stay; an `id` shared with one is an error. Line-managed plugins reject `enable`, `disable` and `remove`; edit the line and sync. `binit update ID` re-snapshots a local line plugin.

At startup, a line whose exact arguments match a synced plugin activates it at once: `startup` plugins load in line order, `command` and `completion` plugins install their first-use stubs, `prompt` plugins load at the first prompt, `wait` plugins right after it is drawn. Existing functions, aliases, builtins and completion specifications still win. `load=wait` is Zinit's `wait` (turbo mode): the first prompt appears at once and the plugin loads before the next key is read, in line order with other `wait` plugins. Keys typed meanwhile are kept. `binit profile` shows `pending` until then. Without the native module (`BINIT_COMPLETION_MENU=0`, no `completion.so`) wait plugins load at the first prompt instead, like `prompt`. Output printed during a wait load lands on the prompt line; a plugin that changes `PS1` or key bindings takes effect from the next prompt or line.

A synced plugin with no matching line is `undeclared` and never loads, including as a dependency; its dependants become unavailable. Declare a dependency before the plugin that needs it.

When lines and generation differ, the first prompt prints `binit: N plugin lines changed since the last sync` and records the shell's lines in `declared`. Catalog plugins are unaffected by lines and activate at init.

`files=` accepts bounded `*` and `?` patterns within a path component. Matches
are sorted; duplicate or empty selections fail. Scans visit at most 8,192
entries and select at most 125 files; each pattern is limited to 1,024 bytes.
Paths and symlinks retain the normal
package restrictions.

## Commands on PATH

`bin=PATH[:NAME],...` exposes package files as commands, Zinit's `as"program"`. Paths follow the rules of `files=` (relative, no symlinks, no traversal, regular files, counted in the package limits); `NAME` defaults to the file's basename and must match `^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$`. A catalog `plugin.toml` or declaration takes the same `bin` list. A line may use `bin=` without `files=`: nothing is sourced and the default-file search is skipped.

```bash
binit plugin user/repo bin=git-foo files=foo.bash
binit plugin user/scripts bin=tool.sh:tool
```

Publishing copies each command of every enabled plugin to `bin/NAME` in the generation with mode 0700, the only executable files, and records the owning plugin in `lock.toml` (`[bin]`, name to plugin ID). Two enabled plugins with the same NAME fail the sync or publish naming both. Startup verification checks that `bin/` holds exactly the locked files, regular, owned by you and mode 0700. `init.bash` prepends `bin/` to `PATH` once (a re-sourced startup file does nothing; `BINIT_SAFE=1` skips init). Commands are generation-wide: a line removed from `.bashrc` keeps its command until the next `binit sync`, and the first-prompt notice says so. A command runs from its copy, so it cannot rely on sibling files. `binit inspect ID` lists them under `Programs:`.

## Release assets and snippets

Remote downloads require an explicit ID and SHA-256. A release also names an
exact tag and asset; there is no implicit latest-version lookup. For Linux
x86_64 with glibc:

```bash
binit plugin release:sharkdp/fd id=fd ver=v10.2.0 asset=fd-v10.2.0-x86_64-unknown-linux-gnu.tar.gz sha256=5f9030bcb0e1d03818521ed2e3d74fdb046480a45a4418ccff4f070241b4ed25 bin=fd-v10.2.0-x86_64-unknown-linux-gnu/fd:fd
binit plugin snippet:https://raw.githubusercontent.com/nvm-sh/nvm/977563e97ddc66facf3a8e31c6cff01d236f09bd/nvm.sh id=nvm sha256=390260ab9eb1da20e8bc0ebea2ee90f528d53e5e9f6e13b16717db4af454df9d load=command commands=nvm
```

`format=raw|tar|tar.gz|zip` overrides format detection from the asset suffix.
`bin=` selects exact paths within an archive and optionally renames commands.
`files=` can source selected Bash scripts alongside release commands. Snippets
source one downloaded Bash file; they do not accept `files=`, `bin=`, `ver=`,
`asset=` or `format=`. Loading policies and dependencies work as with Git lines.

Sync verifies the download before caching it in private `state/assets/SHA256`.
Identical lines retain their snapshot; other selections can use the verified
cache offline. `update --check` reports checksum-pinned sources without moving
them. Edit the source/tag/asset and expected digest, then sync to select new
content. Rollback restores the prior declaration and snapshot. Startup performs
no download or archive extraction.

Downloads and expanded archive contents are limited to 64 MiB, with 128 archive
entries. A selected executable may be at most 32 MiB; sourced files retain the
2 MiB limit. Archive links and special files are rejected, as are traversal,
duplicate paths and encrypted ZIP entries. TAR PAX/GNU extension headers are
currently unsupported. HTTPS redirects stay on HTTPS; URLs cannot carry user
credentials. Curl is required during acquisition.

## Clone and update hooks

`atclone=SCRIPT` prepares a newly acquired source before selected files are
snapshotted. `atpull=SCRIPT` runs on an accepted Git revision update, or an
explicit local-line update. A preview never executes hooks. Unchanged syncs
do not rerun clone hooks; checksum-pinned sources do not move on update.

```bash
binit plugin ~/src/tool files=completion.bash atclone='make completion.bash' atpull='make completion.bash'
```

Hooks run in a disposable source snapshot, leaving the original directory
unchanged. Git and local hook snapshots retain the existing 128-file/2 MiB
source limits. Each update starts from source again, so update hooks must be
self-contained. These snapshots have no Git worktree metadata. Inherited Git
context is removed; Git commands explicitly use staging's `.git` path and
working directory, preventing discovery of an enclosing repository even when
the state path contains a colon.

Hooks receive `BINIT_PLUGIN_ID` and `BINIT_HOOK_EVENT` (`atclone` or `atpull`).
They run through Bash without startup files, with closed stdin and a 120-second
limit. Failure, timeout or interruption prevents generation publication.
Ordinary child processes in the owned hook group are stopped. Deliberately
detached processes and external side effects cannot be rolled back: hooks are
trusted code with the user's authority. Hooks never run during shell startup.

Discover release assets with `binit releases OWNER/REPO --ver latest --platform host`.
Use `--asset GLOB` to narrow names. Discovery lists exact assets and available
published digests; it does not change a pin. `--platform` also accepts
`linux/x86_64` and `linux/arm64`.

`binit sync --jobs N` and `binit update --jobs N` bound independent Git acquisition
to 1–8 workers (default 4). SSH acquisitions remain serial and foreground for
authentication. Foreground SSH cleanup stops the direct Git process; its
descendants may outlive timeout or abrupt manager termination. Other Git
transports use owned process-group cleanup. Preparation hooks and generation
publication remain ordered.

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

Explicitly isolated providers can use `binit completions register NAME PROGRAM
[ARG...]`. The runtime supplies the worker helper and tracks the registration
for owned unload. See the [provider protocol](completion-providers.md) for
private stdin context, resource limits and the current qualification status.

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

`binit trust DIRECTORY` approves the file's current content and prints its digest, label and variables. `binit trust DIRECTORY --sha256 HEX` approves only content with the digest you reviewed, so a change made between review and approval is refused; use it for checkouts that other people or tools can write. Trust records belong to the selected private state directory. Project values use NUL-delimited transport and quoted assignment, without `eval`. Bash control variables such as `PATH` and `POSIXLY_CORRECT` are rejected. That list is not a sandbox: values such as `GIT_SSH_COMMAND`, `PAGER` or `NODE_OPTIONS` decide what other programs run, so approving a project file is as consequential as running its code. Arrays and integer variables are skipped, as are readonly variables and namerefs.

The runtime restores prior values and export declarations on the next directory change. If a variable changes to an incompatible type while the project is active, restoration leaves it intact and reports the conflict. Project profiles explicitly configure environment variables. External programs retain their normal permissions.

## Trust and loading failures

State directories must belong to the current user with mode 700. `binit init` verifies the selected generation's lock digest and every locked file before emitting a source command. After a full verification it stores the inode, size, times, mode and owner of each locked file; later starts compare those with `lstat` and hash again when anything differs. This skips repeated hashing; it protects against other users, not against processes running as you. Publishing a new plan changes an atomic generation pointer. Rollback verifies the previous generation before selecting it.

Sourced plugin code has full shell access and can bypass the interface. A failed `source` cannot be rolled back reliably. The runtime reports `failed`; use a new recovery shell if the plugin altered important state. An actor who can rewrite all same-user state is outside the integrity guarantee.

A fork of Bash would offer deeper hooks but would require maintaining interpreter changes. Keeping the runtime in Bash preserves the existing execution engine. The cost is that hook coordination remains cooperative, and arbitrary plugin code cannot be isolated inside the live shell.
