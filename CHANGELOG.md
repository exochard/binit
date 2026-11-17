# Changelog

## Unreleased: 0.1.0

Nothing is released yet. This entry lists what the first release will contain,
as qualified on Linux x86-64 with Bash 5.2 and 5.3.

### Added

- Plugin lines in `~/.bashrc` and `binit sync`, which pins the declared local,
  Git, release-asset and snippet sources into a verified generation. Shell
  startup reads only that generation and never uses the network.
- `binit rollback` selects the previous verified generation for new shells, and
  `BINIT_SAFE=1` starts a recovery shell that skips initialization.
- Candidate generations and reviewed candidate checks, so a configuration can be
  prepared and tested before new shells use it.
- Load timing per plugin (`load=command`, `load=prompt`, `load=wait`).
  `binit unload` removes the functions, aliases, prompt segments and
  completions a plugin added, if they are unchanged; assignments, exports and
  other side effects stay.
- A native completion menu that reuses Bash's completion definitions and
  quoting, with filtering, grid movement and hjkl keys. Esc closes it after
  waiting 10 ms for the rest of a split key sequence (`BINIT_ESCAPE_MS`).
- History search and history suggestions through the same native module, plus
  optional syntax highlighting when the build includes a grammar.
- Isolated completion providers that run outside the shell process, so typing
  cancels a slow completion instead of waiting for it.
- `binit profile` reports load and hook time per plugin; `binit diagnose`
  reruns reviewed fixtures in a private copy and attributes their time.
- Ctrl-Backspace deletes the previous word in Emacs and vi insert modes,
  including terminals that send Ctrl-H for it.

### Known limits

- Cancelling a completion menu of 1,024 candidates takes about 52.8 ms, slower
  than ble.sh's 33–44 ms on the same trace. A fix is in progress; the
  [publication scoreboard](docs/publication-scoreboard.md) has every result.
- Only Linux x86-64 is qualified; ARM64 is not.
- Under heavy host load, interactive test runs have occasionally seen a shell
  stop answering after Ctrl-C. The cause is unknown; 4,800 later shells did not
  reproduce it.
