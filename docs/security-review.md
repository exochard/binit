# Security review, 2026-11-17

An independent reviewer read the release candidate's source without building
or running it, and without having taken part in writing it. It checked
generation integrity, state directory writes, the deferred runtime loader,
plugin acquisition, project trust, the native module and the `binit init`
output that shells evaluate.

## Model

Binit protects a user's shell from other users. The state directory and every
generation inside it must belong to the user with mode 700, generations are
content addressed, and `binit init` verifies a generation before printing its
source line. A process running as the same user can rewrite that state and is
outside the guarantee. The init stamp, which lets `binit init` skip hashing an
unchanged generation, is a speed-up within that model, not a control against
the user's own processes.

The review found no critical or high issue and no way for another user to get
modified code run at shell start.

## Findings

| Severity | Finding | Status |
| --- | --- | --- |
| Medium-low | A plan sourced by a relative path resolved plugin entries, `bin/` and `completion.so` against the current directory after a later `cd`. | Fixed: `init.bash` makes its directory absolute when sourced. Test `a_plan_sourced_by_a_relative_path_ignores_later_directory_changes`. |
| Medium | `binit trust` approved content without showing it, so a change between review and approval was approved too. | Fixed: `trust` prints the label and variables it approves, and `--sha256 HEX` approves only reviewed content. Test `trust_shows_the_approved_content_and_refuses_unreviewed_changes`. |
| Low-medium | The project variable denylist read as a safety boundary, but values such as `GIT_SSH_COMMAND` or `PAGER` decide what other programs run. | Documented in [plugins](plugins.md#project-data): approving a project file is as consequential as running its code. |
| Low | An interrupted publication leaves `generations/.staging-*` and `.write-*` files behind. | Open: disk use only. |
| Low | Full verification checks locked files and `bin/`, but an extra unlocked file elsewhere in a generation is not rejected. | Open: needs write access as the user. |
| Low | The manager binary that the plan calls is pinned by path, not by digest. | Open: install Binit in a directory only you or root can write. |
| Low | A Git hook checkout may contain a `.git` tree entry that hooks calling `git` would read. | Open: affects `atclone`/`atpull` hooks that run `git`. |
| Low | `curl` runs without `--globoff`, and secrets in URL query strings are stored and echoed. | Open: the SHA-256 pin keeps integrity. |
| Low | Every `init.bash` honours a test variable that redirects the state directory to another directory the user owns. | Open: the manager still requires a private, user-owned directory. |

The reviewer also listed plausible issues it could not prove: unchecked
ancestors of the state directory, directory modes under an unusual umask, a
64-bit mirror key, a keystroke dropped when Readline's input buffer is full,
and deferred runtime files sourced long after verification. Each needs write
access as the user, a crafted environment or about 2^64 work.

## Checked and sound

Generation verification (digests, symlink and owner checks, modes, `bin/` set
equality, size limits), the stamp's coverage and parsing, atomic writes and
exclusive file creation, the 32-writer publication path and its error
handling, pointer updates under the mutation lock, quoting of everything
`binit init` prints, source parsing and credential rejection, Git and release
asset acquisition (no shell, hooks disabled, HTTPS only, SHA-256 required,
archives unpacked in memory with path checks), the trust record's binding to
path and content, and the native module's buffer bounds and seccomp filter.
