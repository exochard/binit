# User-local installation

Build and verify Binit from the checkout, then install the executable:

```bash
make build
python3 scripts/install.py
```

The default destination is `~/.local/bin/binit`. Installation prints the exact
startup line. It leaves startup files alone unless one is explicitly selected:

```bash
python3 scripts/install.py --rc-file ~/.bashrc
```

This adds a marked initialization block after backing up an existing file.
Put plugin declarations after that block. Run `binit sync` from an initialized
shell, then open a new shell to use its prepared generation.

`--prefix /absolute/path` selects another installation root. `--binary PATH`
selects a built executable, including one under a custom Cargo target
directory. An existing unrelated `bin/binit`, changed owned executable, or
modified startup block is refused. Symlink components and foreign-owned
target files are also refused.

The receipt is stored at `PREFIX/share/binit/install.json`. Repeating the
installation with unchanged inputs does nothing. An upgrade retains the
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

The native module is compiled into the manager for a particular Bash/Readline
combination. Mismatched shells reject that module and use the supported
fallback. Build with matching headers to enable native completion for another
supported Bash version; CONTRIBUTING.md describes those inputs.

This installer supports local use of the development build. Installation
does not grant release qualification: remote CI, ARM64 receipts and the
remaining product gates are recorded in [qualification](qualification.md).
