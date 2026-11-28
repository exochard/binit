# Changelog

Changes are grouped by release following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

### Added

- Added `binit install` and `binit uninstall`. The executable handles setup
  and reads installation receipts from earlier releases.
- Added a command guide and per-command help in initialized shells.
- Added per-plugin completion controls, a summary of recorded effects, and
  support for reviewing the plugin changes in a candidate generation.
- Added `binit candidate restore ID --pick` to stage a plugin snapshot from a
  verified generation for review.
- Added `binit plugins --session` to compare the running shell's plugins with
  the next shell's configuration, including why a plugin could not load.
- Added `binit sync --stage` to prepare the current plugin declarations for
  candidate review before selecting them for new shells.
- Added an opt-in timeline for plugin loads and hooks. Set `BINIT_LOGS=1`
  before initialization, then run `binit logs` to inspect the session.
- Added optional minimum Binit versions for plugin packages.
- Added cancellable prompt workers, configurable history and suggestions,
  and richer Bash syntax highlighting in grammar-enabled builds.
- Added an opt-in wait policy that lets Readline process queued input between
  deferred plugin loads.

### Changed

- Made bare `binit update` refresh declared local plugins along with Git pins.
- Showed sample numbers during interactive candidate checks and reported
  cancellation after Ctrl-C. Set `BINIT_PROGRESS=0` for plain output.
- Reduced the work of creating and checking a generation's startup
  verification stamp.
- Extended Ctrl-R to search the shell's full in-memory history and show up to
  1,000 distinct matches, newest first.

### Fixed

- Preserved queued input during a native prompt wait and restored terminal
  state on cancellation.
- Preserved argument highlighting after simple aliases.
- Made version flags work in initialized shells.
- Loaded verified plugin declarations in `binit shell` even when their original
  sources are unavailable.

## 0.1.1

- Include native editor modules for Bash 5.2 with Readline 8.2 and Bash 5.3
  with Readline 8.3.
- Build Linux archives on AlmaLinux 9 for glibc 2.34 or newer. Add an ARM64
  archive alongside x86_64.
- Show full names in the completion menu for Git branches and other
  non-filename candidates.
- Print the version with `binit --version` or `binit -V`. Keep completion
  commands quiet when Bash has no completion specification for the command.

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
22.4 ms, against 31.2 ms for Bash-it and 69.8 ms for Oh My Bash. Against
ble.sh editing the same command lines, the slowest keystroke (p99) took
0.21 ms against 28.6 ms, and cancelling a 1,024-item completion menu took
13.7 ms against 33.1 ms. Binit won all 34 scored comparisons in both runs; the
results archive attached to the release has each one.

### Known limits

- Linux x86_64 only. ARM64 builds are not part of 0.1.
- Under heavy host load, interactive test runs have occasionally seen a shell
  stop answering after Ctrl-C. The cause is unknown; 4,800 later shells did not
  reproduce it.
