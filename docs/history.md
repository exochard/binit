# History panel

`binit_complete editor history on` enables the history panel and inline
suggestions in the current shell. `off` removes the panel and restores the
bindings it still owns. The history plugin calls these commands when loaded.

While typing, the panel shows up to five matching entries below the last row of
the input. Matching ignores ASCII letter case, removes exact duplicates, and
searches at most the 1,000 newest in-memory Bash history entries. Suggestion
keys are described in [editor integration](editor.md).

Tab focuses the panel in **NAVIGATE** mode. Use `j` and `k` or the arrow keys
to choose an entry; Enter inserts it without executing it. `/` changes to
**SEARCH** mode, where letters including `j` and `k` filter the entries. Esc
returns from SEARCH to NAVIGATE; Esc again closes the panel without changing
the command. Ctrl-R opens SEARCH directly, including from an empty line.

To use ordinary completion when history matches, press Tab, Esc, then Tab.
When no history matches, or the panel does not fit on the screen, Tab invokes
the existing completion binding directly. Custom Tab, Enter, and Ctrl-R
bindings take precedence over the panel.

The live panel is drawn with moves relative to the cursor, so it needs no
terminal replies and stays below wrapped input. When the prompt is on the
bottom row, the screen scrolls to make room. Accepting or interrupting the line
clears it. The focused panel opened by Tab or Ctrl-R uses a cursor-position
reply and falls back to ordinary Readline behavior if a reply is unavailable.

The panel does not appear for `read -e`, secondary prompts, input containing
newlines or other control characters, or when the input and the panel would
not fit on the screen. Resizing closes a focused panel without changing the
command line. Bash keeps control of vi and Emacs editing outside the panel.
