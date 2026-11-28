# Loading and shell effects

Plugin declarations can schedule work from the calling Bash:

```bash
binit plugin ~/src/a id=a load=timed delay_ms=500
binit plugin ~/src/b id=b load=event event=project-ready
binit plugin ~/src/c id=c load=watch watch="$HOME/.config/tool/settings"
binit plugin ~/src/d id=d load=timed delay_ms=500 when=tool_ready
```

Timed values are milliseconds, limited to one day. With the native module, timed work also dispatches at an idle top-level prompt
with an empty input line. Checks run at most four times per second. Work stays
in the calling shell; it is suppressed during `read -e`, traps and completion.
Without the module, a timed plugin loads on the first prompt or
`binit dispatch` after its deadline. An event plugin loads
when `binit emit EVENT` is called. A watch plugin loads after an observed change
to a declared file; checks run at prompts, on `binit dispatch`, and through native idle polling. Watched paths
are comma-separated absolute paths. A missing file is watched for creation.

`when=FUNCTION` names a shell function that receives the plugin ID. Until it
exists and returns success, the plugin stays pending. A timed or changed-file
trigger remains eligible and is retried on a later dispatch. An event must be
emitted again. Conditions and plugins run in the current shell with the user's
authority. Startup does no acquisition; plugin or condition code can still
perform actions of its own.

With `BINIT_WAIT_EDIT=1`, queued input can be displayed between wait
plugins even when an earlier condition stays pending. The waiting tail
still finishes before command execution. The earlier condition is tried
again at a later prompt.

## Lifecycle callbacks

Register existing shell functions before the plugin loads:

```bash
before_tool() { printf 'loading %s\n' "$1"; }
after_tool() { printf '%s: %s\n' "$1" "$2"; }
binit on before-load tool before_tool
binit on after-load tool after_tool
binit off after-load tool after_tool
```

Callbacks receive the plugin ID; after-load also receives `loaded` or `failed`.
A failing before-load callback prevents sourcing. A failing after-load callback
marks the load failed. Previously sourced shell effects may remain on failure.

## Reports and unload

Use `binit plugins --session` to compare live plugin state and load rules
with the catalog selected for the next shell.

Set `BINIT_LOGS=1` before `binit init` to keep the last 256 lifecycle events
in the current shell. `binit profile --timeline` and `binit logs` show the
same timeline. `binit logs --json` also gives the dropped event count.
Offsets start at Binit initialization. The module reads a monotonic clock.
Without it, the timeline uses Linux uptime at 10 ms resolution. A first
prompt-dispatch event measures the shell hook before terminal output.
The records carry plugin IDs, outcomes and fixed reason codes. Command
arguments, history and variable values stay out of the timeline.

`binit report ID` lists new simple-named functions and aliases observed during
the source call, and owned Binit prompt segments. It does not claim replacements of definitions that existed
before loading. `binit unload ID` first checks the recorded definitions and
declared completion specifications. It refuses a changed definition, a readonly
function or a loaded dependant before removing anything. Unchanged recorded functions,
aliases, owned prompt segments, declared completions and Binit prompt/directory subscriptions are
removed. A later `binit load ID` explicitly reloads an unloaded plugin.

Set `BINIT_REPORT_EFFECTS=1` before a plugin loads to compare its entry's
shell state before and after sourcing. The report adds changes to simple
functions, completion specifications, variables and Readline bindings. It
shows variable names and attributes. A change marked `modified` differs
from the state immediately after loading. Shell tracing can skip capture.

With capture enabled, unload can also restore ordinary functions that the
plugin replaced or removed. It keeps later edits and leaves functions with
overlapping or nested ownership in place. Variable and binding changes are
observations. A failed source can leave partial effects. Use a fresh recovery
shell when you need a clean shell.

In an initialized Bash, press Tab after `binit` to find commands and options.
Plugin IDs for `report` and `unload` come from the current shell. Management
commands such as `inspect` complete IDs from the selected generation.

`binit completions list` lists declared completion names and their owner.
`binit completions disable NAME` removes an owned specification;
`binit completions enable NAME` restores it. Both refuse to replace a
specification changed by the user. Other completion definitions are untouched.
