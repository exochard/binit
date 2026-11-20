# Binit

Binit runs the plugins in your interactive Bash from a pinned, verified
snapshot and loads each one when it is needed. It also draws a completion
menu, history search and suggestions inside Bash itself, so the line you are
typing never waits on a slow plugin or completion.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/startup-dark.svg">
  <img alt="Binit is ready to type 1.4–3.1× sooner than Bash-it and Oh My Bash. Same 50 plugins. A .bashrc that sources them directly is ready 1.6× sooner than Binit. Log scale. Ready to type in a new shell, 50 plugins: vs Bash-it, 1.4× faster (Binit 22.4 ms · Bash-it 31.2 ms, scored); vs Oh My Bash, 3.1× faster (Binit 22.4 ms · Oh My Bash 69.8 ms, scored); vs a .bashrc sourcing them directly, 1.6× slower (Binit 22.4 ms · .bashrc 14.0 ms, reported); load=wait plugins, vs Bash-it, 1.1× faster (Binit 27.2 ms · Bash-it 31.2 ms, reported); load=wait plugins, vs Oh My Bash, 2.6× faster (Binit 27.2 ms · Oh My Bash 69.8 ms, reported). All 50 plugins loaded: vs Bash-it, 1.3× faster (Binit 33.1 ms · Bash-it 42.4 ms, scored); vs Oh My Bash, 2.7× faster (Binit 33.1 ms · Oh My Bash 88.3 ms, scored); vs a .bashrc sourcing them directly, 1.5× slower (Binit 33.1 ms · .bashrc 22.4 ms, reported); load=wait plugins, vs Bash-it, 1.1× faster (Binit 38.2 ms · Bash-it 42.4 ms, reported); load=wait plugins, vs Oh My Bash, 2.3× faster (Binit 38.2 ms · Oh My Bash 88.3 ms, reported)." src="docs/images/startup-light.svg" width="880">
</picture>

Binit, Bash-it and Oh My Bash load the same 50 plugins on one Linux machine. Bars to the right are
comparisons Binit wins and bars to the left are comparisons it loses; solid bars are scored and
outlined bars are reported without a verdict. [Performance](#performance) covers typing and
completion, and the [publication scoreboard](docs/publication-scoreboard.md) describes the method and
the verdict rules. Every number comes from the timing archive attached to each
[release](https://github.com/exochard/binit/releases).

## Install

Binit 0.1 runs on Linux x86_64 with glibc 2.39 or newer and GNU Bash 5.2 or
later. Download the archive and `SHA256SUMS` from the
[latest release](https://github.com/exochard/binit/releases/latest), then:

```bash
sha256sum -c SHA256SUMS --ignore-missing
tar -xzf binit-0.1.0-linux-x86_64.tar.gz
cd binit-0.1.0-linux-x86_64
python3 -I install.py --binary "$PWD/bin/binit"
```

The installer puts `binit` in `~/.local/bin` and prints the line to add to
`~/.bashrc`. It edits no startup file unless you pass `--rc-file`. Your system
shell stays as it is. [Installation](docs/install.md) covers source builds and
`--uninstall`.

## Declare plugins

Plugin lines go in `~/.bashrc`, after the init line:

```bash
eval "$(binit init)"
binit plugin user/repo ver=v1.2.0 files=init.bash       # a GitHub repository, pinned
binit plugin ~/src/tool load=command commands=tool      # loads on first use of `tool`
binit plugin ~/src/nvm id=nvm load=wait                 # loads right after the first prompt
```

Run `binit sync`, then `exec bash`. Sync fetches each source, pins it to a full
commit or checksum, and publishes a new generation with every file listed by
digest. A shell checks that generation before sourcing anything and never
touches the network at startup. Existing shells keep the code they started
with.

A plugin loads at startup, at the first prompt, right after the prompt appears,
on first use of a command or completion, on a timer, on an event or when a
watched file changes. Existing functions and aliases keep precedence.
[Plugin interface](docs/plugins.md) lists every source type and key;
[loading and shell effects](docs/runtime-features.md) covers scheduling and
unload.

## Complete and search

Tab opens a menu of Bash's own completions. Arrows, hjkl, Tab and Shift-Tab
move through it, `/` filters, Enter inserts and Esc cancels. As you type, the
newest matching history entry appears as a dim suggestion; Right, Ctrl-F or
End accepts it. Ctrl-R searches history and Enter inserts the match without
running it. Ctrl-Backspace deletes the previous word.

A completion that takes long can run in a separate process: typing cancels it
and the line stays responsive. See [isolated completion
providers](docs/completion-providers.md). `binit completion off` returns the
current shell to Readline's ordinary completion.

The built-in editor module targets Bash 5.3 with Readline 8.3. Other supported
Bash versions keep Readline's completion and the rest of Binit.
[Editing](docs/editor.md) and [history](docs/history.md) list every key.

## Update and recover

```bash
binit update --check     # show what would move, change nothing
binit update             # move Git pins to the commits their ver names
binit rollback           # use the previous generation in new shells
binit profile            # load and hook time per plugin
BINIT_SAFE=1 bash        # a shell that skips Binit entirely
```

A failed sync leaves the current generation in place. [Candidate
checks](docs/candidate-checks.md) run your own tests against a prepared
generation before any new shell uses it.

## Performance

Typing and completion are compared with ble.sh, which also draws its own completion menu inside
Bash, and with plain Readline as a control.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/typing-dark.svg">
  <img alt="Binit echoes keys 133–389× sooner than ble.sh and as fast as plain Readline. Time from a key to its echo on the command line, in Emacs and vi modes. Log scale. Typing a character, slowest 1 in 100 keystrokes (p99): vs ble.sh, 133–146× faster (Binit 0.20–0.21 ms · ble.sh 27.5–29.1 ms, scored); vs plain Readline, about the same, 0.98–1.20× (Binit 0.20–0.21 ms · Readline 0.20–0.24 ms, reported). Deleting a character, slowest 1 in 100 keystrokes (p99): vs ble.sh, 344–389× faster (Binit 0.21–0.25 ms · ble.sh 83.2–84.8 ms, scored); vs plain Readline, about the same, 0.91–1.04× (Binit 0.21–0.25 ms · Readline 0.21–0.23 ms, reported)." src="docs/images/typing-light.svg" width="880">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/completion-dark.svg">
  <img alt="Binit answers keys 109–116× sooner than ble.sh; plain Readline cancels a big menu 5.5–5.7× sooner. Completions that run in their own process, so typing can cancel them. Log scale. Typing while a slow completion runs in its own process, slowest 1 in 20 keys (p95): vs ble.sh, 109–116× faster (Binit 1.9–2.1 ms · ble.sh 224.2–226.0 ms, scored); vs plain Readline, 96–103× faster (Binit 1.9–2.1 ms · Readline 200.0–200.3 ms, reported). Accepting that completion, median: vs ble.sh, 85–144× faster (Binit 0.29–0.31 ms · ble.sh 26.3–41.8 ms, scored); vs plain Readline, 1.6× faster (Binit 0.29–0.31 ms · Readline 0.47–0.49 ms, reported). Cancelling a 1,024-item menu, median: vs ble.sh, 2.4–4.7× faster (Binit 13.7–13.8 ms · ble.sh 33.1–64.4 ms, scored); vs plain Readline, 5.5–5.7× slower (Binit 13.7–13.8 ms · Readline 2.4–2.5 ms, reported)." src="docs/images/completion-light.svg" width="880">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/in-shell-dark.svg">
  <img alt="Completion inside the shell: Binit ties plain Readline, then accepts it 1.2–1.4× slower. Such a provider changes shell state, so every editor waits for it. Reported, not scored. Log scale. A slow completion that has to run inside the shell, median: vs ble.sh, 1.2–1.3× faster (Binit 201.3–201.4 ms · ble.sh 246.4–258.1 ms, reported); vs plain Readline, about the same, 1.00× (Binit 201.3–201.4 ms · Readline 201.3 ms, reported). Accepting that in-shell completion, median: vs ble.sh, 67–88× faster (Binit 0.34–0.38 ms · ble.sh 23.4–31.4 ms, reported); vs plain Readline, 1.2–1.4× slower (Binit 0.34–0.38 ms · Readline 0.26–0.29 ms, reported)." src="docs/images/in-shell-light.svg" width="880">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/overhead-dark.svg">
  <img alt="Binit adds 8.4 ms to a new shell with 50 plugins; Zinit adds 87.2 ms to Zsh. Time over the same shell sourcing the same plugins directly. Reported, not scored. Ready to type: Binit adds 8.4 ms over Bash sourcing the plugins directly; Zinit adds 87.2 ms over Zsh doing the same. All 50 plugins loaded: Binit adds 10.7 ms over Bash sourcing the plugins directly; Zinit adds 316 ms over Zsh doing the same." src="docs/images/overhead-light.svg" width="880">
</picture>

### Where Binit is slower

- A `~/.bashrc` that sources the same 50 plugins directly is ready to type 8.4 ms sooner (14.0 ms
  against 22.4 ms). That is the cost of Binit's runtime, which checks the generation before
  sourcing anything. Zinit adds 87.2 ms over Zsh doing the same.
- Putting every plugin on `load=wait` makes the shell ready to type 4.8 ms later than loading them
  at startup (27.2 ms against 22.4 ms).
- Plain Readline returns after a cancelled 1,024-item completion 5.5 to 5.7 times sooner (2.4 to
  2.5 ms against 13.7 to 13.8 ms).
- A completion that has to run inside the shell takes as long as with plain Readline, and accepting
  it takes 0.34 to 0.38 ms against 0.26 to 0.29 ms.

### Not measured yet

- Compatibility with unmodified popular plugins. A development run passed the same four
  (bash-completion, fzf, Git completion and nvm) under Binit, Bash-it and Oh My Bash; it is not
  release evidence.
- Other machines, ARM64, and the time a terminal takes to draw.

## Limits

- Linux x86_64 only in 0.1.
- Plugins are Bash code with your shell's full authority. Pins and digests
  detect changed files; they do not vouch for an author.
- `binit unload` removes the functions, aliases, completions and prompt
  segments a plugin added, if they are unchanged. Variables, exports and other
  side effects stay.
- `binit run --engine` selects an experimental Bash build. It has not met its
  25% speed target, so `binit run` uses `/bin/bash` by default.

## Project

Binit is vibe-engineered: it is written with AI coding agents, and every
change lands with the tests and measurements behind it. The
[documentation](docs/README.md) explains how it works.
[CONTRIBUTING.md](CONTRIBUTING.md) has the build and test setup, and
[SECURITY.md](SECURITY.md) explains how to report a vulnerability.

Binit's original code is under the [MIT license](LICENSE). GNU Bash and
Readline keep their GPL terms; [licensing](docs/licensing.md) explains what
each release archive contains.
