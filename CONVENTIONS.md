# Binit conventions

## Versioning

Binit uses `MAJOR.MINOR.PATCH`. Each part is a whole number of any width,
so `0.10.0` follows `0.9.0`. Choose the highest part that applies to the
release and reset the parts to its right to zero.

| Part | Meaning | Example |
| --- | --- | --- |
| Major | `1.0.0` establishes the supported interfaces. Later major releases signal an incompatible change to that contract. | A change that requires users to rewrite existing plugin declarations. |
| Minor | Adds a capability or substantially improves an existing workflow. This covers the CLI, terminal interface, configuration, and plugin interface. | Per-plugin completion controls or a history panel with search and preview. |
| Patch | Corrects behavior or improves performance and packaging while preserving working configurations and plugins. | Restore argument highlighting after a simple alias or reduce startup cost. |

Keep patch upgrades compatible within each minor release line, including
before `1.0.0`. A breaking change during `0.x` needs at least a minor bump.
Document the migration and test the upgrade on an existing installation.
The compatibility contract covers documented commands, configuration,
plugin interfaces, and saved state.

A large compatible upgrade can headline a minor release. Use its title and
[changelog](CHANGELOG.md) to describe the main improvement. For a release
with new completion controls and a startup fix, bump the minor version once
and record both changes.

Independently distributed plugins have their own versions. When a plugin
depends on a new core capability, document the minimum Binit version it
requires. The bundled editor uses the core version.

## Release identity

Identify development previews by branch and commit. Set the release number
when the release scope is agreed. The Git tag `vMAJOR.MINOR.PATCH`,
`binit --version`, archive names, and changelog must agree for a public
release. The [archive builder](scripts/build-release.bash) reads the number
from the built executable. Give corrections to a published release a new
version.

## Branches

| Branch | Purpose |
| --- | --- |
| `main` | Long-lived integration branch for reviewed changes with passing checks. The `vX.Y.Z` tag identifies the exact published commit. |
| `release-X.Y.Z` | Temporary candidate cut from `main` once the release scope is settled. Qualify the exact candidate and integrate its release changes into `main`. |

Name topic branches `intent/area/outcome`. Start from `main` and keep each
branch focused on one outcome. Use lowercase words separated by hyphens
within each part. The prefix groups topics by name.

| Topic example | Evidence for integration |
| --- | --- |
| `feat/manager/completion-admin` | Show the documented workflow. For changes to managed state, show that a failed operation leaves the current generation usable and that rollback restores the previous one. |
| `fix/editor/alias-argument-highlighting` | Reproduce the behavior on the baseline and pass the regression check with the fix. Exercise the interaction in both Emacs and vi modes. |
| `perf/runtime/ready-to-type` | Freeze the workload and show paired before-and-after measurements with the same visible behavior and plugin effects. |
| `exp/editor/async-scheduler` | Record the hypothesis, how to test it, and the result. Bring a proven change into a feature or performance topic for review. |
| `docs/project/branch-conventions` | Check links, examples, and claims against the current tree. |
| `ci/project/checks-on-pr` | Check which events invoke the intended jobs and exercise the workflow on a test change. |

Use `manager` for commands and stored state, `runtime` for shell startup
and loading, and `editor` for interactive behavior. Name work that crosses
areas by the outcome users see. The branch contains the complete change.
Close a topic when it merges or when the investigation ends.

## Development

Use a concise conventional commit subject such as
`fix: preserve completion arguments` and use plain Bash terminology. Keep
generated builds and raw machine logs out of Git. See [Contributing](CONTRIBUTING.md).

Keep shell startup free of network operations. Preserve the caller's exit
status and shell options when changing shared hooks. Changes to plugin
activation must preserve generation verification and rollback. See the
[plugin interface](docs/plugins.md).

Exercise native editor changes on the supported Bash versions in Emacs and
vi modes with the project's [verification script](scripts/verify.bash).
Freeze the validation workload before optimizing, keep compiler training
separate, and back performance claims with complete paired measurements.
See the [benchmark procedure](docs/benchmarks.md).
