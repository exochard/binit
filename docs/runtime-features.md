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

`binit report ID` lists new simple-named functions and aliases observed during
the source call, and owned Binit prompt segments. It does not claim replacements of definitions that existed
before loading. `binit unload ID` first checks the recorded definitions and
declared completion specifications. It refuses a changed definition, a readonly
function or a loaded dependant before removing anything. Unchanged recorded functions,
aliases, owned prompt segments, declared completions and Binit prompt/directory subscriptions are
removed. A later `binit load ID` explicitly reloads an unloaded plugin.

Unload does not reverse arbitrary assignments, exports, directory changes,
traps, key bindings, processes, filesystem changes or definitions a plugin
overwrote. A failed source can leave partial effects. Open a fresh recovery
shell when complete reversal is required.

`binit completions list` lists declared completion names and their owner.
`binit completions disable NAME` removes an owned specification;
`binit completions enable NAME` restores it. Both refuse to replace a
specification changed by the user. Other completion definitions are untouched.
