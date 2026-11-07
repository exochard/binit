# Architecture

Binit owns its core feature behavior and interface. Bash supplies shell
semantics, including programmable completion. Readline supplies command-line
editing and the final quoting and insertion of completion candidates.

The Rust manager prepares immutable generations. A generation contains its
runtime and plugin snapshots, plus the native completion module when enabled
at build time. Every file is listed by digest. Shell startup verifies that
generation before sourcing code or loading the module. Updates produce a new
generation; existing shells keep their current code.

## Completion

The native module keeps Bash's existing completion providers. It obtains the
processed match list through Readline, then runs Binit's own selection model
and bounded terminal renderer. Selecting an item returns it to Readline for
quoting and insertion. Opening the menu does not launch a picker process.
Filtering scans the captured candidates in memory.

This separation avoids maintaining another shell-word parser. It also keeps
providers such as Git completion usable through the standard Bash interface.
The public plugin contract remains [the Bash hooks and manifest](plugins.md).
The C module's internal interfaces are private.

The menu supports Tab and Shift-Tab, arrows and hjkl navigation, and a filter.
Enter inserts a candidate. Esc preserves the original input and cursor.
Resizing closes the menu while preserving input. Terminal control characters
in candidates are escaped for display; insertion retains the original value.
Readline handles pasted text after the menu closes.

The module temporarily changes completion settings while obtaining matches
and restores them before returning. Disabling it restores Tab bindings it
still owns. Custom macros and unrelated Tab functions remain installed.
Callback code stays mapped for
the shell's lifetime because Readline can retain user-created bindings after
the builtin is removed.

## Deferred plugin loading

`load=wait` plugins load after the first prompt is drawn. When the runtime
finds wait plugins pending at the first prompt hook, it asks the module to
install an `rl_pre_input_hook` (`binit_complete wait`). Bash 5.3 leaves that
hook unset; it only uses `rl_startup_hook` for push-line. Readline calls it
after drawing the prompt and before reading input, so typed keys stay in the
terminal buffer and appear afterwards.

The hook chains any earlier hook and does nothing unless
`__binit_wait_pending` is nonzero, `interactive_shell` is set, and
`executing_builtin`, `running_trap` and `parse_and_execute_level` are all
zero. A running builtin covers `read -e`; evaluation inside `bind -x`, a
nested parse or `eval` raises the parse level; a trap sets `running_trap`.
Only the top-level command reader passes. It then runs `__binit_wait_dispatch`
as `bind -x` runs its commands: an unwind frame, saved and restored parser
state, `SEVAL_NOHIST`, Readline signals cleared and set again. The previous
exit status and `$_` are restored, and `rl_forced_update_display` redraws the
prompt and typed input. The dispatch clears the pending flag first, so it runs
at most once.

Disabling or unloading the module restores the previous hook if Binit's is
still current. If the module is absent, or the hook did not run for a prompt,
the next prompt hook loads the pending plugins, so they load at the first
prompt at the latest.

## Alternatives considered

The test budgets below cover a feasibility experiment, not the complete
implementation.

| Approach | Prior art | Decisive test and budget | Decision |
| --- | --- | --- | --- |
| Native module using Readline's completion machinery | [GNU Readline](https://www.gnu.org/software/bash/manual/html_node/Readline-Interaction.html) | Quoting, provider retry, cursor restoration, and a stable viewport; two hours | Selected. The initial transport and screen tests pass. |
| Shell adapter with a separate picker | [fzf-tab-completion](https://github.com/lincheney/fzf-tab-completion) | Existing completion specifications and nested quoting; two hours | Deferred. Reconstructing word context and provider dispatch adds another compatibility surface. |
| Full replacement editor | [Flyline](https://github.com/HalFrgrd/flyline) | Job control, external terminal programs, and vi editing; two hours | Deferred. Core feature ownership does not yet require replacing command-line editing. |
| Modified Readline distributed with a private Bash | [Readline completion implementation](https://git.savannah.gnu.org/cgit/readline.git/tree/complete.c) | Run the feature with an unmodified system Bash; one hour | Deferred. It would make the private interpreter a requirement. |

The first renderer experiment used Readline's message area for a multiline
menu. Screen tests showed repeated rows and scrollback growth, so it was
replaced by a renderer with an explicit viewport. That test remains part of
the acceptance criteria.

## Interpreter work

The optional interpreter is independent of the interactive module. Compiler
training uses separate workloads, and unchanged validation scripts remain
frozen. The [25% execution target](release-0.1.md) is a release condition.
Native UI work does not establish an interpreter speedup.
