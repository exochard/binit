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

Before distributing binaries, the release process must inventory dependency
licenses and provide the required source and notices for every included
component. A release artifact's contents determine those obligations; the
repository's MIT license does not replace third-party terms.
