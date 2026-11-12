# Contributing to Binit

Binit manages Bash plugins and prepares verified shell initialization files.
Start with a local plugin or a focused bug fix. The
[0.1 release plan](docs/release-0.1.md) describes work that still needs evidence
before it can ship.

## Development

Use Linux, Bash 5.2 or newer, Rust with edition 2024 support, Python 3.12 or
newer, Git, curl, ShellCheck, and GNU coreutils. Native completion also needs a C
compiler and Bash/Readline development headers. Verification uses CMake and
the pinned Python tools in `requirements-dev.txt`. On Debian or Ubuntu, the
header packages are `bash-builtins` and `libreadline-dev`.
Install the Rust `clippy` and `rustfmt` components. From the checkout:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
source .venv/bin/activate
cargo fetch --locked
cargo build --release --locked --offline
bash scripts/verify.bash
bash scripts/demo.bash
```

The demo opens a separate vi-mode Bash with all seven independent plugins.
Clone their repositories under `../binit-plugins`, or set `BINIT_PLUGIN_ROOT`.
State and private history live under `.local-demo-interactive/`; `exit` returns
to the parent shell. Existing startup files and Readline settings are preserved.
The first editor dependency preparation requires tree-sitter CLI 0.26.9; builds
then reuse the verified local source/runtime configuration. `BINIT_DEMO_STATE`
opens a custom catalog through the existing fixture demo path.

`make build`, `make verify` and `make bench` run their tools directly. The Make
targets disable inherited Rust compiler wrappers by default. To select a working
cache explicitly, use `make build RUSTC_WRAPPER=/path/to/sccache`. Global Cargo
settings remain unchanged.

The verification script checks formatting and ShellCheck, then runs Clippy,
Rust integration tests, Python tests, and real terminal smoke tests. Keep
tests focused on behavior: argument handling, shell state preservation, and
recovery are more useful than matching generated implementation text.

`BINIT_BASH=/absolute/path/to/bash` selects the interpreter used by `binit shell`
and the terminal harness, including recovery sessions. It does not change the
default interpreter for `binit run`. When building the native module, set
`BINIT_BASH_INCLUDE` to that interpreter's installed Bash headers and
`BINIT_READLINE_INCLUDE` to the directory containing its matching
`readline/readline.h`. A module compiled for another interpreter is not a
supported combination.

Use an absolute `CARGO_TARGET_DIR` for each interpreter/compiler configuration.
Verification exports `BINIT_TEST_BINARY` and `BINIT_TEST_MODULE` from that
directory and writes tool identities to `verification/environment.json` there.
`BINIT_NATIVE=0 bash scripts/verify.bash` qualifies the native-disabled build:
Rust and Python unit checks, first-prompt loading fallback, and terminal smoke.
Native completion and post-prompt PTY checks run in native-enabled jobs.
See [qualification](docs/qualification.md) for CI coverage and remaining gates.

The native menu tests use a real Bash PTY with a terminal emulator. They check
the visible screen and command-line state, including cancellation, paste,
and terminal control characters. Read [the architecture](docs/architecture.md)
before changing the completion bridge. `CC` chooses the C compiler and
`BINIT_BASH_INCLUDE` overrides the Bash header directory. A manager/runtime
build without the native module is available through `--no-default-features`.

To check existing plugins, pass reviewed local scripts to the compatibility
checker. It uses a private home and state directory and writes file hashes
and results to `evidence/plugin-compatibility.json`:

```bash
python3 scripts/check_plugins.py --fzf /usr/share/fzf --bash-completion /usr/share/bash-completion/bash_completion --nvm /path/to/nvm.sh
```

Each option is independent. The checker initializes NVM and queries its
version; it does not install Node. See [the compatibility receipt](docs/plugin-compatibility.md)
for the locally qualified sources.

## Plugins and runtime changes

The [plugin interface](docs/plugins.md) documents manifests and public Bash
hooks. `examples/plugins/workspace/` is a complete local plugin. Plugin code
runs with the current user's authority. A content hash detects edits to a
snapshot; it does not make untrusted Bash safe to execute.

Keep startup free of downloads. Preserve the caller's exit status and shell
options when changing shared hooks. Changes to generation or project trust
handling need a failure case that proves the previous state remains usable.

## Performance work

`make bench-interactive` compares bare Bash with empty and seven-plugin Binit
configurations in a real PTY. See [interactive performance](docs/interactive-performance.md)
for timing boundaries and same-run binary comparisons.

Read [the benchmark procedure](docs/benchmarks.md) before changing an engine
or validation script. It explains how to build the pinned GNU Bash sources
and run the fixed suite. Compiler training uses separate code. A short trial
is useful for diagnosis; only the complete paired runs can satisfy the release
gate.

## Sending a change

Use a branch and a concise conventional commit subject, for example
`fix: preserve completion arguments`. Explain the behavior before and after
the change, and include the exact checks run. Use ordinary Bash terminology
for names. Put generated builds and raw machine logs under ignored paths.

Binit's original code is under the [MIT license](LICENSE). Contributions use
that license. GNU Bash and other third-party components retain their own
licenses; downloading a source archive does not relicense it.
