# Licensing

Binit's original source is covered by the [MIT license](../LICENSE).
Contributions use that license. GNU components retain their own terms.

The native completion module uses GNU Bash and Readline interfaces. Its
source is MIT licensed, which is GPL compatible. Distribution of a module
combined with GPL components must follow the applicable GPL requirements,
including notices and corresponding source. GNU describes this distinction
in its [plugin licensing guidance](https://www.gnu.org/licenses/gpl-faq.html#GPLAndPlugins).
The [GPL version 3 text](../LICENSES/GPL-3.0-or-later.txt) is included for GNU
components.

Optional Bash interpreter builds use the pinned GNU sources and patches in
`engines/bash/source.json`. Any distributed interpreter needs its corresponding
source and build instructions. The benchmark cache is a local build directory.

## Release archives

`binit-0.1.0-linux-x86_64.tar.gz` holds `bin/binit`, `install.py`, `README.md`,
`LICENSE`, `LICENSES/` (the GPL text and, under
`LICENSES/dependencies/<crate>-<version>/`, the license files of every Rust
dependency) and `dependency-notices.json`. It contains no Bash or Readline
copy: the executable embeds the native module, compiled against the headers of
Arch Linux Bash 5.3.20-1 and Readline 8.3.6-1, and runs with the system's Bash
and Readline.

`binit-0.1.0-gnu-sources.tar.gz` holds the corresponding GNU sources for those
headers: the upstream Bash and Readline tarballs, point patches, signatures,
and the matching PKGBUILD and BUILDINFO recipes. The repository's MIT license
does not replace third-party terms.
