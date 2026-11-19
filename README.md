# Binit

Binit runs the plugins in your interactive Bash from a pinned, verified
snapshot and loads each one when it is needed. It also draws a completion
menu, history search and suggestions inside Bash itself, so the line you are
typing never waits on a slow plugin or completion.

The [publication scoreboard](docs/publication-scoreboard.md) compares Binit
with Bash-it and Oh My Bash loading the same 50 plugins, and with ble.sh editing
the same command lines. The results ship with each
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
