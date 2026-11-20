# Changelog

## 0.1.0 - 2026-11-20

The first release, for Linux x86_64 with glibc 2.39 or newer and GNU Bash 5.2
or later. The built-in editor module targets Bash 5.3 with Readline 8.3; other
supported Bash versions keep Readline's completion.

### Added

- Plugin lines in `~/.bashrc` and `binit sync`, which pins the declared local,
  Git, release-asset and snippet sources into a verified generation. Shell
  startup reads only that generation and never uses the network.
- `binit rollback` selects the previous verified generation for new shells, and
  `BINIT_SAFE=1` starts a recovery shell that skips initialization.
- Candidate generations and reviewed candidate checks, so a configuration can be
  prepared and tested before new shells use it.
- Load timing per plugin: at startup, at the first prompt, right after the
  prompt (`load=wait`), on first use of a command or completion, on a timer, on
  an event or when a watched file changes. `binit unload` removes the
  functions, aliases, prompt segments and completions a plugin added, if they
  are unchanged; assignments, exports and other side effects stay.
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

### Measured

On one Linux machine with the same 50 plugins, a new shell was ready to type in
22.1 ms, against 31.2 ms for Bash-it and 69.2 ms for Oh My Bash. Against
ble.sh editing the same command lines, the slowest keystroke (p99) took
0.26 ms against 29.9 ms, and cancelling a 1,024-item completion menu took
13.9 ms against 34.1 ms. Binit won all 34 scored comparisons in both runs; the
results archive attached to the release has each one.

### Known limits

- Linux x86_64 only. ARM64 builds are not part of 0.1.
- Under heavy host load, interactive test runs have occasionally seen a shell
  stop answering after Ctrl-C. The cause is unknown; 4,800 later shells did not
  reproduce it.
