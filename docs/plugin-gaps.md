# Development plugin gaps

The baseline is Binit 0.1.0 at `169096bdfa2c3ba35d47ca3d39f7a0fe4c7488b4`
and the seven independent plugin repos at `0.1.0-dev`. The tables use those
repos as the baseline. The copies under Binit's `plugins/` are examples.

Build rows carry a value rank. A build row with an approval condition is a
proposal. Keep existing defaults until the corresponding change is approved.

## Build order

| Rank | Plugin | User value |
| --- | --- | --- |
| 1 | Git prompt | See repository state at every prompt without waiting for a scan. |
| 2 | Git completion | Get useful Tab results for installed tools and Git aliases on first use. |
| 3 | History search | Retrieve a command from scattered words, then review it before running it. |
| 4 | Autosuggestions | Accept the useful part of a suggestion without retyping or deleting its tail. |
| 5 | Project context | Move between projects without carrying the previous project's environment. |
| 6 | Directory jump | Reach the intended directory with a few terms even when names repeat. |
| 7 | Syntax highlighting | Catch command and argument mistakes while typing. |

## 1. Git prompt

Plugin baseline `a6bdfe936c9cf13baf82477f4bd551da153247b6`.
The reference is [gitstatus's query protocol](https://github.com/romkatv/gitstatus/blob/039ac1b02698f2ebb3fe17d2c800b2aecc0245c8/gitstatus.plugin.zsh#L53-L138)
and its [index scan](https://github.com/romkatv/gitstatus/blob/039ac1b02698f2ebb3fe17d2c800b2aecc0245c8/src/repo.cc#L127).
Powerlevel10k's [formatter](https://github.com/romkatv/powerlevel10k/blob/50a35cc8d9f1488d18fb8aea6cddcfc31189cc9f/config/p10k-lean.zsh#L385-L454)
and [query dispatch](https://github.com/romkatv/powerlevel10k/blob/50a35cc8d9f1488d18fb8aea6cddcfc31189cc9f/internal/p10k.zsh#L4075-L4160)
show how it uses that protocol. Git's [porcelain format](https://github.com/git/git/blob/f7de81456940dd6f3274e461149bba12965de609/Documentation/git-status.adoc#L295)
provides a separate basis for the Bash implementation.

| Feature | Reference behavior | Binit today | Decision and reason |
| --- | --- | --- | --- |
| Branch, detached HEAD, tag | Protocol returns local branch, commit and an exact-HEAD tag. The formatter uses the tag or a short commit when detached. | Short branch or short commit from separate Git calls. | Build (1). Keep identity visible even when a detailed scan is unavailable. |
| Ahead and behind | Reports divergence from the configured upstream; the formatter shows nonzero counts. | No divergence. | Build (1). Read local tracking refs; omit counts when the upstream is unavailable. |
| Index and worktree | Reports separate staged, unstaged, untracked and conflicted counts, with limits and an unknown state for skipped scans. | No counts. | Build (1). Parse bounded porcelain v2 records and preserve unknown separately from zero. |
| Stash | Exposes a stash count and displays it when nonzero. | No stash information. | Build (1). Use the porcelain stash header where supported. |
| Operation and progress | The protocol exposes an action label. The formatter displays it verbatim. The cited interface has no separate progress fields. | No operation indicator. | Build (1). Detect rebase, merge, cherry-pick and bisect from worktree-aware Git state. Show step and total where a reliable progress record exists, such as a rebase with counters. |
| Prompt latency and huge repositories | Persistent daemon with cached index work, optional scan limits and asynchronous callbacks in Zsh. Powerlevel10k can use bounded waits as well as zero-wait queries. | One process substitution at a time with a two-second refresh interval. Polling can attempt a 1 ms read. A result appears at a later prompt, and typing does not cancel the worker. | Build (1, approved). Add a narrow prompt-worker interface. Cap aggregate time and bytes, cancel on pending input and reject results from an old directory or request. Publish complete data only. |
| Additional runtime | gitstatus supplies a separate daemon. | Git and GNU timeout are declared requirements. | Later. Evaluate an additional daemon only if the Git-only worker fails the frozen large-repository readiness cases. A dependency requires approval. |

Keep the existing branch-only display as the default. Offer rich status as an
explicit setting. Use local Git objects only, including in partial clones.
Start with one worker and separate identity from detailed work. In a huge
worktree, cancellation or a bounded scan can leave detail unknown. Record that
outcome and the time to valid status alongside prompt latency. The native
editor is needed for cancellable rich status. The existing branch-only path
remains available without it.

## 2. Git completion

Plugin baseline `c9c9288be6336533b46d8c2b63c222f3a865d86a`.
The references are bash-completion's [installation and discovery guide](https://github.com/scop/bash-completion/blob/b111f5c39d40af1228c5222ed1e1f564e1d739ec/README.md#L143),
[default-loader source](https://github.com/scop/bash-completion/blob/b111f5c39d40af1228c5222ed1e1f564e1d739ec/bash_completion#L3421-L3623),
and [Git's completion script](https://github.com/git/git/blob/f7de81456940dd6f3274e461149bba12965de609/contrib/completion/git-completion.bash#L4025-L4122).

| Feature | Reference behavior | Binit today | Decision and reason |
| --- | --- | --- | --- |
| Git first use | Git provides a normal Bash compspec and initializes its command-specific functions when sourced. | The `git` stub searches three installed paths and preserves an existing `gitk` spec. Existing `git` specs win. | Build (2). Keep the three-path fallback, quoting, candidate behavior and explicit `binit load git-completion`. |
| Any installed definition | The upstream default loader resolves the invoked command through ordered per-user, executable-adjacent and system locations. A loaded compspec returns 124 for retry. Some definitions use functions from bash-completion itself. | No Binit default loader. Matching specs are installed only for manifest names. | Build (2). Use the approved optional bash-completion package and Binit's existing isolated completion request. Existing command specs and custom default handlers take precedence. Keep standalone Git completion as the default. |
| Git aliases | Git subcommand aliases are expanded by the shipped Git definition. Its `__git_complete` can also register Git completion for shell aliases or wrappers. | Git subcommand behavior is inherited from the installed script. The plugin has no lazy registration for shell aliases or wrappers. | Build (2). Test subcommand aliases and offer explicit first-use bindings for named shell aliases and wrappers; let Git interpret its own aliases. |
| Cancellation and state | The upstream loader sources shell code and installs compspecs in the calling Bash. | Binit has a typing-cancellable worker for isolated providers. Git uses ordinary calling-shell completion. | Build (2). Move potentially slow path discovery out of the key path. Keep trusted definition sourcing explicit; it changes live Bash state. |
| Arbitrary calling-shell code | Installed definitions can source helpers, mutate shell state and run commands. | Ordinary Bash completion has the same authority. | Non-goal. Arbitrary installed scripts cannot be forcibly cancelled once running in the interactive Bash. A child can return typed candidates or a validated path, but its shell definitions do not transfer to the parent. |

Measure discovery, trusted load and first completion separately. A single
slow fixture for discovery must leave keystrokes intact. Accepting arbitrary
in-process scripts as uniformly cancellable would overstate the guarantee.

## 3. History search

Plugin baseline `5f8680faf7c15ec56fc3fe8b76e30a0e3db92f27`.
[History Search Multi Word](https://github.com/zdharma-continuum/history-search-multi-word/blob/55d8e061d52f4aa26aac8915d920d3b4f5649429/README.md#L5-L10)
documents word matching and its [widget source](https://github.com/zdharma-continuum/history-search-multi-word/blob/55d8e061d52f4aa26aac8915d920d3b4f5649429/history-search-multi-word.plugin.zsh#L26-L38)
shows the Ctrl-R binding. Atuin's local model is documented in its
[README](https://github.com/atuinsh/atuin/blob/01b1a0b3c14286e58ef0818dd4f2f8957d7e066b/README.md)
and implemented in the [entry schema](https://github.com/atuinsh/atuin/blob/01b1a0b3c14286e58ef0818dd4f2f8957d7e066b/crates/atuin-client/src/history.rs#L350),
[search and deletion code](https://github.com/atuinsh/atuin/blob/01b1a0b3c14286e58ef0818dd4f2f8957d7e066b/crates/atuin/src/command/client/search.rs)
and [database](https://github.com/atuinsh/atuin/blob/01b1a0b3c14286e58ef0818dd4f2f8957d7e066b/crates/atuin-client/src/database.rs).

| Feature | Reference behavior | Binit today | Decision and reason |
| --- | --- | --- | --- |
| Multi-word and fuzzy matching | History Search Multi Word finds entries containing all query words. Atuin also offers fuzzy search. | A single case-insensitive literal substring. Native search covers the 1,000 newest entries; `hs` searches a bounded history listing. | Build (3). Add opt-in all-terms matching and an explicit fuzzy mode to history only, with consistent `hs` and picker behavior. Keep ordinary completion filters literal. |
| Deduplication and insertion | Atuin groups matching commands. Its documented default keys can execute a selected command. | Newest-first deduplication. Enter inserts, and Esc can preserve the previous line. | Build (3). Preserve the most recent matching occurrence after filtering and keep the selection as input. |
| Context and filters | Atuin records directory, session, time and exit status; the query can filter by context. | Readline supplies command text and may supply a timestamp. The plugin does not record directory, session or status. | Build (3). Propose opt-in in-memory context for new commands, bounded to the search window. Record the directory at command entry and status at completion. Show older or imported rows as unknown where metadata is absent. |
| Delete one entry | Atuin can delete recorded rows, including sets matched by a search. | Binit has no picker deletion. | Build (3). Require an explicit action tied to a revalidated live history entry. Explain that the operation is session-local and how it interacts with history-file writes. |
| Indexing and sync | Atuin uses SQLite and offers optional sync. | A bounded Readline history scan. | Later for a larger local index. Establish need and obtain approval for a new storage dependency. Non-goal for sync or an account. |

The installed plugin README still describes the former live mode as normal.
Binit 0.1 defaults to search mode, with `BINIT_HISTORY_MODE=live` for the
former behavior. Keep that setting and the existing key ownership rules.
History collection must stay bounded. Any later indexing request must support
typing cancellation and reject answers for an obsolete query.

## 4. Autosuggestions

Plugin baseline `ed088d6063e251ea79370a93ae13cb84d9cee67b`.
The reference is [zsh-autosuggestions' configuration guide](https://github.com/zsh-users/zsh-autosuggestions/blob/85919cd1ffa7d2d5412f6d3fe437ebdbeeec4fc5/README.md#L20-L102),
its [widgets](https://github.com/zsh-users/zsh-autosuggestions/blob/85919cd1ffa7d2d5412f6d3fe437ebdbeeec4fc5/src/widgets.zsh),
[previous-command strategy](https://github.com/zsh-users/zsh-autosuggestions/blob/85919cd1ffa7d2d5412f6d3fe437ebdbeeec4fc5/src/strategies/match_prev_cmd.zsh)
and [asynchronous requests](https://github.com/zsh-users/zsh-autosuggestions/blob/85919cd1ffa7d2d5412f6d3fe437ebdbeeec4fc5/src/async.zsh).

| Feature | Reference behavior | Binit today | Decision and reason |
| --- | --- | --- | --- |
| Strategy order | History prefix is the default. Completion and match-previous-command can be ordered with it. The previous-command strategy checks predecessor history and falls back to the most recent match. | Newest prefix from up to 1,000 Readline entries. | Build (4). Keep prefix history as the default. Offer predecessor matching and opt-in completion from already registered isolated providers. |
| Whole and partial acceptance | Right or End accepts the whole suggestion. Forward-word can accept part using the widget's movement. | Ctrl-F, Right and End accept the visible whole suggestion in eligible maps; Ctrl-E also works in Emacs. Custom bindings win. | Build (4). Offer opt-in word and character acceptance. A character operation must avoid splitting a UTF-8 character and retain Readline undo and fallback actions. |
| Ignore, size and style | Per-strategy ignore patterns, optional buffer size ceiling and configurable suggestion color. | Fixed dim style. Input ceiling 4,096 bytes and suffix ceiling 1,024 bytes. Control-bearing content is skipped. | Build (4). Add strategy-specific ignore globs and an optional lower input limit. Accept named styles or validated color numbers and retain dim by default. |
| Asynchronous fetching | Supported Zsh versions fetch asynchronously by default. A new request closes the old result descriptor and cancels its process. | History scan runs synchronously from redisplay, with fixed limits. | Build (4). Use the cancellable worker for completion strategy and reject stale results. Qualify bounded history work in large histories before changing its execution model. |
| Arbitrary completions as ghosts | Zsh can run its own completion machinery in a child PTY. | Bash compspecs may require live shell definitions. Isolated Binit providers already have an explicit boundary. | Later. General calling-shell completion suggestions need a separate design for side effects, compatibility and cancellation. |

## 5. Project context

Plugin baseline `994b753d7ecfc65780688c5267bc08701ca31b99`.
The reference is [direnv's README](https://github.com/direnv/direnv/blob/3d0b4ae1ed83064a10ada052949596e7463a8768/README.md#L10-L81),
[trust and lookup](https://github.com/direnv/direnv/blob/3d0b4ae1ed83064a10ada052949596e7463a8768/internal/cmd/rc.go#L30),
[environment transition](https://github.com/direnv/direnv/blob/3d0b4ae1ed83064a10ada052949596e7463a8768/internal/cmd/cmd_export.go#L50-L121)
and [helper manual](https://github.com/direnv/direnv/blob/3d0b4ae1ed83064a10ada052949596e7463a8768/man/direnv-stdlib.1.md#L151).

| Feature | Reference behavior | Binit today | Decision and reason |
| --- | --- | --- | --- |
| Trust | Path and content hash determine approval. A changed file needs approval again. | Canonical path and SHA-256 bind a TOML profile to its trust record. Manager emits NUL records. | Build (5). Preserve the trust check and validate complete profile data before applying it. |
| Enter, leave and restoration | Bash hook recomputes at a prompt. direnv reverts the previous diff and applies the new environment. | Directory hook runs at the next prompt after a changed `PWD`. Restores prior scalar values and export state where compatible. Its regex checks also overwrite Bash's `BASH_REMATCH`. | Build (5). Preserve caller match state. Test absent, declared-but-unset, empty and exported values, failed transitions and incompatible types. Keep prompt-boundary semantics for compound commands such as `cd subdir && command`. |
| Nested projects | Locates the nearest profile up the directory tree. A profile can explicitly source its parent. | Exact directory only. | Build (5). Offer opt-in ancestor lookup. Treat the nearest untrusted profile as a boundary and restore the last applied frame when leaving. Keep exact-directory mode as the default. |
| PATH, Python and Node helpers | `PATH_add` prepends a path, `layout node` exposes local bins, and `layout python` can create and activate a venv. | Raw assignment of `PATH` is deliberately rejected. | Build (5). Propose typed helpers for prepending paths, activating an existing venv and adding `node_modules/.bin`. Bind them to the same reviewed digest. Keep path changes ordered and reversible. |
| Report | Logs names added, removed or changed from the environment diff. | A project label or an untrusted indicator. `binit trust` prints the approved profile. | Build (5). Provide an on-demand report of changed names and trust state without printing values. |
| Script evaluation and environment creation | Executes approved shell profiles and can invoke tools to prepare an environment. | Declarative project data. | Non-goal. Keep project data declarative. Venv creation and dependency installation belong to an explicit command. |

## 6. Directory jump

Plugin baseline `3c407a5f9b7a6dcd5a31237c1f87950a4b5a3234`.
The reference is [zoxide's user guide](https://github.com/ajeetdsouza/zoxide/blob/2f4bcebaa62a44742280172c9b4c2753266716d0/README.md#L71),
[score calculation](https://github.com/ajeetdsouza/zoxide/blob/2f4bcebaa62a44742280172c9b4c2753266716d0/src/db/dir.rs),
[aging](https://github.com/ajeetdsouza/zoxide/blob/2f4bcebaa62a44742280172c9b4c2753266716d0/src/db/mod.rs#L149),
[match rules](https://github.com/ajeetdsouza/zoxide/blob/2f4bcebaa62a44742280172c9b4c2753266716d0/src/db/stream.rs#L63)
and [query implementation](https://github.com/ajeetdsouza/zoxide/blob/2f4bcebaa62a44742280172c9b4c2753266716d0/src/cmd/query.rs#L18).

| Feature | Reference behavior | Binit today | Decision and reason |
| --- | --- | --- | --- |
| Frecency and aging | Recency multiplies visit rank. Total rank is periodically scaled down when it passes a ceiling. | Visit counts, capped at 128 directories. A lowest-score entry is evicted when the limit is exceeded. | Build (6). Offer opt-in recency weighting and bounded aging so the current default winner stays predictable. |
| Multi-keyword query | Ordered terms match path components, with the final term constrained to the last component. | One literal case-insensitive substring; an existing path takes precedence. | Build (6). Add ordered multi-term queries while preserving quoted single terms and direct paths. |
| Interactive selection | `zi` uses optional fzf. | `j --list` lists session entries without selecting. | Build (6). An explicit `j --interactive` can use a bounded numbered picker with arrow and hjkl movement in a terminal. Preserve the original directory on cancellation. No new picker dependency. |
| Excluded directories | Configurable globs exclude candidates from recording and results. | Only control-bearing paths are excluded. | Build (6). Add an empty-by-default exclude list checked at both recording and query time. |
| Persistence | Writes a local database. | Session-only and bounded. | Later. Propose explicit opt-in persistence with private atomic storage, locked updates and merge rules for concurrent shells. Keep session-only as the default. |

## 7. Syntax highlighting

Plugin baseline `9f4cbe061b0d2dfb478a17d989208dd92fbc0cc4`.
The references are Fast Syntax Highlighting's [README](https://github.com/zdharma-continuum/fast-syntax-highlighting/blob/4672ad5dd9ad68a7effc1476d65afb7c584ce2b3/README.md#L108),
[style and classifier source](https://github.com/zdharma-continuum/fast-syntax-highlighting/blob/4672ad5dd9ad68a7effc1476d65afb7c584ce2b3/fast-highlight)
and [theme guide](https://github.com/zdharma-continuum/fast-syntax-highlighting/blob/4672ad5dd9ad68a7effc1476d65afb7c584ce2b3/THEME_GUIDE.md).

| Feature | Reference behavior | Binit today | Decision and reason |
| --- | --- | --- | --- |
| Command kinds | Styles distinguish aliases, functions, builtins, reserved words, commands and unknown tokens. | The parser colors command names and keywords. An initial alias suppresses color rendering for that line. | Build (7). Add opt-in command classification using Bash's state and bounded asynchronous external resolution. Color an alias name without assuming how its expansion parses. |
| Paths and options | Path styles distinguish filesystem targets; options have their own styles. | Neither has a dedicated category. | Build (7). Classify options in argument position. Resolve literal paths off the keystroke path, with an unknown state until a result arrives. |
| Quoting, expansions, substitution, redirections, comments | Separate styles cover quotes and expansions. The highlighter recognizes nested substitutions, redirections and comments. | Strings, variables and comments have colors. The incremental parser can recognize nested commands but has no dedicated substitution or redirection styles. | Build (7). Add explicit token categories and fixtures for incomplete input. Use parser ranges and keep syntax advisory. |
| Bracket pairing | An enabled brackets highlighter marks nesting and paired brackets. | No pair indication. | Build (7). Match parser-classified delimiters at the cursor, with tests for quotes and substitutions. |
| Themes and Git rules | Theme files map categories to styles; command-specific chromas cover Git. | Fixed palette and no per-command rules. | Build (7). Provide validated style overrides and an opt-in Git rule for literal subcommand position. Keep the current palette as the default. Later for arbitrary rule scripts. |
| Long lines | Upstream implements an editor highlighter. The pinned sources do not establish a measured Bash long-line budget. | Incremental tree reuse with a 4,096-byte ceiling and bounded tree traversal depth. Redisplay still walks the parsed tree. | Build (7). Qualify wrapped, long and incomplete input at the current ceiling. Retain the ceiling until a larger budget is measured. |

## Approval before implementation

The Git prompt's native interface is approved. It provides one opt-in local
status worker per shell. The native input path cancels requests and returns
bounded data to the prompt plugin. Branch-only stays the default. Git and
GNU timeout are the declared requirements. The worker owns its process group
so cancellation also stops its Git subprocesses.

Review later changes separately if they require another native API or a
dependency, or change a visible default. General completion discovery needs
particular care because preserving an installed script's live Bash behavior
and cancelling that same script are different obligations.

## Verification and latency

Freeze workload fixtures before implementation. Run `make bench` and the
seven-plugin `scripts/bench_interactive.py --plugin-root` suite for integration.
Use `binit profile` to confirm each plugin loads and record its load and hook
microseconds. Keep the first visible prompt and the first acknowledged key
as separate measurements. The existing script fixes vi mode, reports p95
and builds both binary comparisons from one plugin root. Add an isolated
comparison for each plugin with separate pinned roots and raw p99 input
samples in Emacs and vi on Bash 5.2 and 5.3. Select live history explicitly
for the existing runner's live-panel probe so its fixture works with the
search-mode default.

Use identical fixture inputs, include A/A noise checks and alternate baseline
and candidate runs. Report sample counts, absolute times, paired differences
and intervals. An interval that cannot resolve a regression at the measured
noise floor calls for another run or an inconclusive result. Include the
feature's first useful result so an early prompt cannot hide a slow worker.
Use the same large-repo, long-line, history and cancellation fixtures on both
sides. Timing evidence from a different configuration cannot establish a
per-plugin result.

Add a failing regression test before each fix. Extend the plugin's
`tests/test_plugin.py` and use PTY cases for actual interactive behavior.
Verify prompt and key ownership, caller state and missing native
capabilities. Check ShellCheck at warning severity. For native changes,
run `scripts/verify.bash` with matching Bash 5.2 and 5.3 builds and run the
editor cases in both editing modes. Run one heavy job at a time. Each plugin
advances to `0.2.0-dev` after its user guide documents every option with its
default and an example, and its checks and latency comparison are recorded.
