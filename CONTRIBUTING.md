# Contributing to Binit

Binit manages Bash plugins and prepares verified shell initialization files.
Start with a local plugin or a focused bug fix. The
[0.1 release plan](docs/release-0.1.md) describes work that still needs evidence
before it can ship.

## Development

Use Linux, Bash 5.2 or newer, Rust with edition 2024 support, Python 3.12 or
newer, Git, ShellCheck, and GNU coreutils. Native completion also needs a C
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

The demo opens a separate shell. `exit` returns to the parent. It writes its
state under `.local-demo/`; it leaves startup files alone.

On a desktop with a systemd user manager, `make build` and `make verify` run
the same work in scopes with a 3 GiB memory limit and two CPUs. Direct commands
above also work in CI and containers. Choose resource limits appropriate to
the machine before running a full build.

The verification script checks formatting and ShellCheck, then runs Clippy,
Rust integration tests, Python tests, and real terminal smoke tests. Keep
tests focused on behavior: argument handling, shell state preservation, and
recovery are more useful than matching generated implementation text.

The native menu tests use a real Bash PTY with a terminal emulator. They check
the visible screen and command-line state, including cancellation, paste,
and terminal control characters. Read [the architecture](docs/architecture.md)
before changing the completion bridge. `CC` chooses the C compiler and
`BINIT_BASH_INCLUDE` overrides the Bash header directory. A manager/runtime
build without the native module is available through `--no-default-features`.

## Plugins and runtime changes

The [plugin interface](docs/plugins.md) documents manifests and public Bash
hooks. `examples/plugins/workspace/` is a complete local plugin. Plugin code
runs with the current user's authority. A content hash detects edits to a
snapshot; it does not make untrusted Bash safe to execute.

Keep startup free of downloads. Preserve the caller's exit status and shell
options when changing shared hooks. Changes to generation or project trust
handling need a failure case that proves the previous state remains usable.

## Performance work

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
