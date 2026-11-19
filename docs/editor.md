# Editor integration

The native completion module can display history suggestions in an interactive
Bash session. `binit_complete editor suggestions on` enables them and `off`
restores the bindings and redisplay hook it still owns.

A suggestion is the rest of the newest matching history entry, shown dim after
the cursor while the cursor is at the end of the line. Ctrl-F, Right and End
insert the whole suggestion in Emacs mode and in vi insert mode; Ctrl-E does in
Emacs mode too. Binit takes a key only while it has its standard binding:
forward-char, end-of-line, or the self-insert that vi insert mode gives Ctrl-F.
Without a suggestion the key keeps that action. Custom bindings take
precedence, and vi command mode shows no suggestion. While suggestions are on,
`bind -p` does not list the keys Binit has taken, because their functions have
no Readline name. When a line is accepted or
interrupted, the suggestion is erased, so scrollback shows only the command
that ran. Opening a completion or history picker also hides the suggestion;
the picker displays the actual input line. `binit editor` reports the enabled
features and native availability.

## Editing keys

Readline already gives Home, End, Delete, Ctrl-Delete, Ctrl-Left and
Ctrl-Right their usual meaning in Emacs mode and both vi modes, and Binit
leaves them alone. The one addition: End also accepts a visible suggestion, as
described above. Ctrl-Delete deletes from the cursor to the end of the word.

Kitty and several other terminals send Ctrl-H for Ctrl-Backspace, which
Readline treats as Backspace. When a shell starts with the native module,
which `BINIT_COMPLETION_MENU=0` turns off, Binit binds Ctrl-H
to `backward-kill-word` in Emacs mode and vi insert mode, so Ctrl-Backspace
deletes the word before the cursor, like Alt-Backspace in Emacs mode. One undo
restores it. Binit takes Ctrl-H only while it still deletes one character and
it can read the terminal's erase key as another byte. With Readline's default
`bind-tty-special-chars on`, the erase key is rebound at every prompt, so
`stty erase '^H'` restores character deletion even after startup. If Binit
could not take the key when it started, a later
`binit_complete editor keys on` changes nothing until `keys off` runs first.
A custom binding made before startup takes precedence, and
`binit_complete editor keys off` restores the original. It also replaces a
later binding of Ctrl-H to `backward-kill-word`, which looks the same as
Binit's. `binit editor` prints a `keys` row containing `ctrl-backspace word` while Binit owns
the key in the keymap of the current editing mode.

Words follow Readline's rule: letters and digits. In `fix-bug_x`,
Ctrl-Backspace removes only `x`, and in `/usr/local/share/` it removes
`share/`.

In the completion menu and the history panel, a lone Esc and the first byte of
an arrow key are the same byte until the next one arrives. Binit waits 10 ms
for it, as tmux does with `escape-time`, then treats the Esc as a key of its
own. Terminals write a whole key sequence at once, so the wait only matters
over links that split it, such as a slow SSH connection. There,
`BINIT_ESCAPE_MS=50` widens it; values from 0 to 1000 are accepted, and
anything else keeps 10. A sequence that arrives after the wait reaches the
command line as typed text. Ctrl-G closes either panel without waiting.

Suggestions use up to 1,000 recent entries from the current shell's Readline
history. Entries with terminal control characters are not displayed. The
overlays follow Readline's own line wrapping. On a wrapped line, highlighting
covers every row and the colors are rewritten as one run, so the terminal keeps
the line joined for selection. A suggestion that does not fit before the right
margin is cut with an ellipsis; accepting it still inserts all of it.

Prompt width is measured on the last line of the prompt. Text inside `\[` and
`\]` counts as zero width, so colors and title sequences are fine, and the
`show-mode-in-prompt` string is included. The overlays are left to Readline when
the prompt contains other unmarked escape sequences, when the line contains tabs
or other control characters, when the input and the history panel would not fit
on the screen, and while another builtin such as `read -e` owns the line reader.

Syntax highlighting is advisory: its parser cannot execute input. It colors
command names, keywords, strings, variables and comments. An unfinished
statement is colored while it is typed: in `for f in a b; do echo "$f"` the
keywords, the variable and the command are colored before `done` exists, a word
in command position after `then`, `do`, `;`, `|` or `&&` is colored as a
command, and an unterminated quote is shown as a string to the end of the line.
Initial command aliases are rendered without colors because their expansion may
change the parse context. Syntax highlighting is experimental.

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
