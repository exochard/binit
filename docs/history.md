# History panel

`binit_complete editor history on` enables the history panel and inline
suggestions in the current shell. `off` removes the panel and restores the
bindings it still owns. The history plugin calls these commands when loaded.

While typing, only the inline suggestion is shown. Tab keeps its completion
binding. Ctrl-R opens history in **SEARCH** mode, including from an empty
line. Matching ignores ASCII letter case, removes exact duplicates, and
searches at most the 1,000 newest in-memory Bash history entries. Suggestion
keys are described in [editor integration](editor.md).

In SEARCH, letters including `j` and `k` filter entries. Enter inserts the
selected command without executing it. Esc changes to **NAVIGATE** mode:
use `j` and `k` or the arrow keys to choose an entry, and `/` to search again.
Esc from NAVIGATE closes the panel without changing the command.

Custom Tab, Enter, and Ctrl-R bindings take precedence. `binit editor` reports
whether history, suggestions and highlighting are enabled, alongside native
module availability and completion status.

The previous live panel remains available explicitly with
`binit_complete editor history live`, or `BINIT_HISTORY_MODE=live` before
loading the history plugin. It shows up to five matches and gives Tab to
history navigation while they are visible. In this mode, Tab, Esc, Tab
reaches ordinary completion. `BINIT_HISTORY_MODE=search` selects the default.
The live mode preserves existing configurations and frozen measurement
workloads that depend on that display and binding.

The live panel is drawn with moves relative to the cursor, so it needs no
terminal replies and stays below wrapped input. When the prompt is on the
bottom row, the screen scrolls to make room. Accepting or interrupting the line
clears it. The focused panel uses a cursor-position
reply and falls back to ordinary Readline behavior if a reply is unavailable.

The panel does not appear for `read -e`, secondary prompts, input containing
newlines or other control characters, or when the input and the panel would
not fit on the screen. Resizing closes a focused panel without changing the
command line. Bash keeps control of vi and Emacs editing outside the panel.
