# First-party plugins

Binit installs its manager, runtime and native capabilities. Plugins are
independent repositories and require explicit installation. Each repository
contains its manifest, Bash entry, license, configuration guide and compatibility
tests. There is no preset or automatic plugin installation.

| Repository | Purpose |
| --- | --- |
| `git-prompt` | Bounded background Git branch information |
| `git-completion` | First-use loading of installed Git completion definitions |
| `project-context` | Explicitly trusted project variables and restoration |
| `directory-jump` | Ranked navigation through visited directories |
| `history-search` | History selection through fzf without executing a selection |
| `syntax-highlighting` | Optional native advisory highlighting |
| `autosuggestions` | Optional native history suggestions |

Clone a chosen repository and declare its absolute path after initialization:

```bash
eval "$(binit init)"
binit plugin /absolute/path/to/directory-jump
```

Run `binit sync` and open a new shell. Once a repository is published, its
`OWNER/REPOSITORY` source uses the same pinned Git workflow as other plugins.
The public namespace and release URLs must be established before publication.

The editor plugins require compatible native capabilities. Highlighting remains
subject to the [editor qualification](editor.md); an activation package does not
establish production readiness. History search requires fzf. Directory navigation
keeps a bounded session history rather than writing visited paths to disk.

For an independent repository, run its compatibility tests against a selected
Binit binary. CI requires `BINIT_CORE_REPOSITORY` to identify that source. Create
release archives from a clean committed repository with:

```bash
python3 scripts/package_plugin.py /absolute/path/to/plugin /absolute/path/to/artifacts
```

The packager reads committed Git objects, applies source-package limits, and
records the commit and file digests. Archives are deterministic. Repository
metadata, uncommitted overrides and symlinks are excluded or rejected.

The manager checkout retains the three original plugin snapshots as development
fixtures for its existing demo and compatibility tests. Independent plugin
repositories are the maintained distribution sources; the manager installer
does not copy those fixtures into a user's environment.
