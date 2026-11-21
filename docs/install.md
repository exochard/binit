# User-local installation

## Install the release

Binit 0.1.1 requires Linux, glibc 2.34 or newer, and GNU Bash 5.2 or later.
Download the archive for `x86_64` or `aarch64` and `SHA256SUMS` from the
[release](https://github.com/exochard/binit/releases/tag/v0.1.1).

```bash
sha256sum -c SHA256SUMS --ignore-missing
tar -xzf "binit-0.1.1-linux-$(uname -m).tar.gz"
cd "binit-0.1.1-linux-$(uname -m)"
python3 -I install.py --binary "$PWD/bin/binit"
```

`--binary` points the installer at the archive's executable; without it the
installer looks for a source build in `target/release/`. Installation prints
the startup line to add to `~/.bashrc`.

## Install from source

Prerequisites: Rust and Cargo, a C compiler, and the Bash and Readline
development headers for the default native module. Without the headers the
build fails; build with `--no-default-features` to skip the module.

```bash
cargo fetch --locked
make build
python3 scripts/install.py
```

`make build` runs Cargo with `--offline`, so `cargo fetch --locked` must
download the dependencies first.

The default destination is `~/.local/bin/binit`. Installation prints the exact
startup line. It leaves startup files alone unless one is explicitly selected:

```bash
python3 scripts/install.py --rc-file ~/.bashrc
```

This adds a marked initialization block after backing up an existing file.
Put plugin declarations after that block. Run `binit sync` from an initialized
shell, then open a new shell to use its prepared generation.

`--prefix PATH` selects another installation root; relative and `~` paths are
accepted. `--binary PATH`
selects a built executable, including one under a custom Cargo target
directory. An existing unrelated `bin/binit`, changed owned executable, or
modified startup block is refused. Symlink components are refused, as is any
path component owned by neither you nor root.

The receipt is stored at `PREFIX/share/binit/install.json`. With the default
prefix that directory, `~/.local/share/binit`, is also Binit's default state
directory. Repeating the installation with unchanged inputs does nothing. An upgrade retains the
previous executable as a timestamped backup. Startup-file and receipt write
failures restore the prior installation. Backups remain available for review.

## Uninstall

```bash
python3 scripts/install.py --uninstall
```

Use the same `--prefix` if a custom root was installed. Uninstall removes the
unchanged recorded executable and exact owned initialization block. It keeps
plugin state, backups and custom declarations. Remove or guard those plugin
declarations separately if Binit is no longer on PATH. Changed files are
preserved and reported instead of being overwritten or deleted.

## Supported builds

The archives include modules for Bash 5.2 with Readline 8.2 and Bash 5.3 with
Readline 8.3. A shell with a different combination uses ordinary Readline
completion. Build with matching headers to enable the native editor for
another supported combination. See [Contributing](../CONTRIBUTING.md) for the
build inputs.
