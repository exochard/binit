# History panel

`binit_complete editor history on` enables the history panel and inline
suggestions in the current shell. `off` removes the panel and restores the
bindings it still owns. The development plugin `history-search` calls these commands when loaded.

While typing, only the inline suggestion is shown. Tab keeps its completion
binding. Ctrl-R opens the history panel with a search query, including from an
empty line. Matching ignores ASCII letter case, removes exact duplicates, and
searches at most the 1,000 newest in-memory Bash history entries. Suggestion
keys are described in [editor integration](editor.md).

While searching, the panel's last line shows the query and the position, as in
`history  al▏  1/37   ?`. Letters including `j` and `k` extend the query. Enter
inserts the selected command without executing it. Esc keeps the matches and
switches to browsing, shown as `history  1/37   ?`: `j` and `k` or the arrow
keys choose an entry, and `/` searches again. Esc while browsing closes the
panel without changing the command. `?` shows the keys for the current state
on a second line; `?` again hides it.

Custom Tab, Enter, and Ctrl-R bindings take precedence. `binit editor` prints
one row each for the menu, suggestions, history, highlighting and the keys
Binit binds.

The older live panel remains available explicitly with
`binit_complete editor history live`, or `BINIT_HISTORY_MODE=live` before
loading the history plugin. It shows up to five matches and gives Tab to
history navigation while they are visible. In this mode, Tab, Esc, Tab
reaches ordinary completion. `BINIT_HISTORY_MODE=search` selects the default.
The live mode preserves configurations that depend on that display and binding.

The live panel is drawn with moves relative to the cursor, so it needs no
terminal replies and stays below wrapped input. When the prompt is on the
bottom row, the screen scrolls to make room. Accepting or interrupting the line
clears it. The focused panel uses a cursor-position
reply and falls back to ordinary Readline behavior if a reply is unavailable.

The panel does not appear for `read -e`, secondary prompts, input containing
newlines or other control characters, or when the input and the panel would
not fit on the screen. Resizing closes a focused panel without changing the
command line. Bash keeps control of vi and Emacs editing outside the panel.
