# Development plugins

Binit installs its manager, runtime and native capabilities. Plugins require
explicit installation; there is no preset or automatic plugin installation.

Binit 0.1 has no featured plugin set. Seven development plugins exist, and
their sources ship inside the source release archive. They are not released as
standalone plugins yet. Each has a manifest, a Bash entry, a license, a
configuration guide and compatibility tests.

| Plugin | Purpose |
| --- | --- |
| `git-prompt` | Bounded background Git branch information |
| `git-completion` | First-use loading of installed Git completion definitions |
| `project-context` | Explicitly trusted project variables and restoration |
| `directory-jump` | Ranked navigation through visited directories |
| `history-search` | Live native history rows and modal search without executing a selection |
| `syntax-highlighting` | Optional native advisory highlighting |
| `autosuggestions` | Optional native history suggestions |

`git-prompt`, `git-completion` and `project-context` are also in the source
checkout under `plugins/`. They serve as examples a user can read and load by
path. Declare the absolute path of an extracted plugin after initialization:

```bash
eval "$(binit init)"
binit plugin /absolute/path/to/git-prompt
```

Run `binit sync` and open a new shell.

The editor plugins require compatible native capabilities. Highlighting is
experimental; see [editing](editor.md). History search uses the native module. Directory
navigation keeps a bounded session history rather than writing visited paths to
disk.

To package a plugin source tree, run from a clean committed repository:

```bash
python3 scripts/package_plugin.py /absolute/path/to/plugin /absolute/path/to/artifacts
```

The packager reads committed Git objects, applies source-package limits, and
records the commit and file digests. Archives are deterministic. Repository
metadata, uncommitted overrides and symlinks are excluded or rejected.
