# Editor integration

The native completion module can display history suggestions in an interactive
Bash session. `binit_complete editor suggestions on` enables them and `off`
restores the bindings and redisplay hook it still owns. Ctrl-F accepts a
suggestion when it still has the standard forward-character binding. Custom
bindings take precedence.

Suggestions use up to 1,000 recent entries from the current shell's Readline
history. Text with terminal control characters is not displayed. The renderer
uses a single terminal line and leaves multiline inputs, wide suggestions and
prompts containing terminal controls other than SGR colors to Readline. Editor overlays do not run
while another builtin such as `read -e` owns the line reader.

Syntax highlighting is advisory: its parser cannot execute input. The patched
grammar passes the frozen syntax probe and its Bash dialect cases. The broader
terminal and editor qualification remains open. Initial command aliases are
rendered without colors because their expansion may change the parse context.
The default native build contains no syntax grammar and enabling highlighting
reports an error. To build the experiment, acquire the pinned grammar and
prepare its static tree-sitter runtime before the offline build. Preparation
requires tree-sitter CLI 0.26.9; it verifies the generated parser hash:

```bash
cargo install --locked tree-sitter-cli --version 0.26.9
python3 scripts/fetch_syntax.py .cache/syntax-source --runtime-prefix .cache/syntax-runtime
export BINIT_SYNTAX_SOURCE="$PWD/.cache/syntax-source"
export BINIT_SYNTAX_RUNTIME="$PWD/.cache/syntax-runtime"
```

The Cargo build and native CMake configuration verify the source hashes before
compilation. The prepared runtime is linked into the module; it does not need a
system tree-sitter library. In an interactive shell, call
`binit_complete editor highlighting on`. Use `off` to release the owned
redisplay hook. Syntax colors are advisory and never control execution.

Both the grammar and tree-sitter runtime use the MIT license. The pinned grammar
license is acquired with the source; release artifacts must retain the applicable
third-party notices.
