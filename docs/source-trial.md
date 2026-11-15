# Source trial

The trial builds Binit from source and opens a separate Bash with seven
explicitly selected plugins. It keeps its own history and does not edit shell
startup files. Core installation remains a separate opt-in operation.

This path requires a reviewed source bundle. Public download URLs are not
established yet. A bundle contains the core archive, separate archives for
the selected independent plugins, `sources.json` and `SHA256SUMS`. Its sender
must provide the expected manifest checksum through a trusted channel;
checksums inside an altered bundle do not authenticate its author.

## Create the bundle

From a committed core checkout, choose each independent plugin explicitly:

```bash
python3 scripts/package_sources.py . tmp/source-bundle \
  --plugin /path/to/git-prompt \
  --plugin /path/to/git-completion \
  --plugin /path/to/project-context \
  --plugin /path/to/directory-jump \
  --plugin /path/to/history-search \
  --plugin /path/to/syntax-highlighting \
  --plugin /path/to/autosuggestions
sha256sum tmp/source-bundle/sources.json
```

The output directory must be new. Each archive binds a complete commit and
file digests. Core untracked files are excluded; tracked modifications are
refused. Plugin repositories must be clean. Git replacement refs and local
worktree overrides cannot replace the committed bytes. Executable modes and
each repository's license are retained. Omitting every `--plugin` creates a
core-only bundle; it does not select the full trial.

## Prepare Linux dependencies

The declared qualification environment is Ubuntu 24.04 on x86_64. Install
build dependencies explicitly in that environment:

```bash
sudo apt-get update
sudo apt-get install -y build-essential bash-builtins libreadline-dev libclang-dev \
  pkg-config git bash-completion python3 python3-venv curl ca-certificates \
  cmake shellcheck nodejs
```

## Unpack the declared sources

Starting with only the received bundle, first compare its manifest checksum
with the expected value supplied by the sender. Then verify the archives
and extract the bootstrap verifier from its fixed path:

```bash
cd /path/to/source-bundle
sha256sum sources.json
sha256sum --check SHA256SUMS
core_archive=$(python3 -c 'import json; print(json.load(open("sources.json"))["core"]["artifact"])')
mkdir bootstrap
tar -xzf "$core_archive" -C bootstrap scripts/unpack_sources.py
python3 bootstrap/scripts/unpack_sources.py . trial
cd trial/core
```

Bootstrap extraction runs only after the trusted manifest and archive checks.
The complete unpacker rechecks the archives against `sources.json`. It checks
file digests, rejects links and escaping paths, and restores recorded modes.
An existing trial directory is refused. The full trial requires the seven
selected plugin IDs above; missing sources are named in the launch error.

## Prepare build tools

Use Rust with edition 2024 support, Cargo, and tree-sitter CLI 0.26.9. The
qualification recipe acquires Rust 1.99.1 explicitly into a local tools
directory. From the received core source tree on x86_64:

```bash
mkdir -p tmp/tools
curl --fail --location --output tmp/tools/rust.tar.xz \
  https://static.rust-lang.org/dist/rust-1.99.1-x86_64-unknown-linux-gnu.tar.xz
printf '%s  %s\n' \
  231fcae47ee950621374725b5fc3b5441d2fbda2955ac45dd7dc816d8876cfe7 \
  tmp/tools/rust.tar.xz | sha256sum --check
tar -xf tmp/tools/rust.tar.xz -C tmp/tools
bash tmp/tools/rust-1.99.1-x86_64-unknown-linux-gnu/install.sh \
  --prefix="$PWD/tmp/tools/rust" --disable-ldconfig \
  --components=rustc,cargo,rust-std-x86_64-unknown-linux-gnu
export PATH="$PWD/tmp/tools/rust/bin:$PWD/tmp/tools/tree-sitter/bin:$PATH"
cargo install --locked tree-sitter-cli --version 0.26.9 \
  --root "$PWD/tmp/tools/tree-sitter" --target-dir "$PWD/tmp/tree-sitter-build"
```

These are build dependencies; the commands do not install Binit. An existing
compatible Rust toolchain can be used instead.

## Open the trial

```bash
export BINIT_PLUGIN_ROOT="$(cd ../plugins && pwd)"
export BINIT_DEMO_ROOT="$PWD/tmp/demo"
export CARGO_TARGET_DIR="$PWD/tmp/build"
cargo fetch --locked
make demo
```

The first `make demo` explicitly acquires the pinned Bash grammar and
tree-sitter runtime, verifies their hashes, then builds Binit. Later launches
reuse the verified build inputs. Runtime startup itself performs no dependency
acquisition. `BINIT_PLUGIN_ROOT` makes the plugin location explicit; no sibling
repository search is needed. Trial history starts with two example commands
and persists only within the trial. Existing personal history is not imported.

Inside the trial, use Ctrl-R to open history search. Esc returns to navigation
and closes the panel. Type `git sta` and press Tab for Git completion; j/k
move through the menu and Enter selects. `binit profile` shows plugin readiness.
The Git completion plugin starts pending and loads on its first completion.
The native module supplies suggestions and advisory highlighting.

The project-context example requires explicit trust:

```bash
binit trust examples/project
cd examples/project
printf '%s\n' "$PROJECT_MESSAGE"
cd ../..
j --list
exit
```

`exit` returns to the parent shell. Launch `make demo` again to continue the
same trial. To start over, choose a new `BINIT_DEMO_ROOT`. Existing Readline
settings are preserved. The trial does not qualify every terminal or third-party
plugin; the [qualification inventory](qualification.md) records those gates.
