# History panel

`binit_complete editor history on` enables the history panel and inline
suggestions in the current shell. `off` removes the panel and restores the
bindings it still owns. The development plugin `history-search` calls these commands when loaded.

While typing, only the inline suggestion is shown. Tab keeps its completion
binding. Ctrl-R opens the history panel with a search query, including from an
empty line. Ctrl-R searches all in-memory Bash history by default and shows
up to 1,000 distinct matching commands, newest first. A more specific query
can find a command beyond the first 1,000 results. Matching ignores ASCII
letter case. See [editor integration](editor.md) for suggestion keys.

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
history navigation while they are visible. Live matches use the most recent
1,000 entries. In this mode, Tab, Esc, Tab
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

`BINIT_HISTORY_MATCH=words` matches all query terms in any order.
`BINIT_HISTORY_MATCH=fuzzy` allows intervening characters within each term.
The default is literal matching. Set options before enabling history.

Set `BINIT_HISTORY_CONTEXT=1` to capture directory, command timestamp and
exit status for new history entries. Set `BINIT_HISTORY_SCOPE=directory`
or `session` to filter the picker. These scopes use captured context from
the most recent 1,000 entries. Imported entries have no session context.

With `BINIT_HISTORY_DELETE=1`, press `x` while browsing, then `y` to remove
the selected live history entry. Esc cancels the confirmation. Deletion
closes the picker and leaves the input line intact.
