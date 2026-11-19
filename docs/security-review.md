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

## Installer and update review, 2026-11-18

A second independent reviewer read the installer, `update`, `update --check`,
candidate checks and selection, and publication, again without building or
running them. It found no critical or high issue.

| Severity | Finding | Status |
| --- | --- | --- |
| Medium | The source-trial bootstrap ran `python3 -c` inside the received bundle before its checks, so a planted `json.py` there would run first. | Fixed: the documented steps run Python with `-I`. The recipe test covers a planted module. |
| Low-medium | `update --check` printed remote commit subjects as received, so terminal controls in a subject could rewrite the review output or set the title. | Fixed: controls and bidirectional marks print as `?`. Test `update_check_prints_remote_commit_subjects_without_terminal_controls`. `--check` and the update fetch separately, so a branch can move between review and update. |
| Low | A kill between the installer's binary, startup-block and receipt writes leaves a state that both install and uninstall refuse, without recovery steps. Only an exception restores the prior state. | Open. |
| Low | The installer checks ownership and links but not permissions, so a group- or world-writable `bin/` is accepted; the plan calls the manager by path. | Open: install in a directory only you or root can write. |
| Low | Any symlinked ancestor of the install paths is refused, including a symlinked home or a managed `.bashrc`. | Open: fails closed. |
| Low | The first `binit init` takes the mutation lock without waiting, so shells opened together on first use can start without Binit. | Open. |
| Low | A sync during a binary replacement can record a `binit (deleted)` path in the plan until the next apply. | Open. |
| Low | A hostile Git remote can fill the disk within the 120 s fetch limit; there is no object-size cap. | Open: denial of service only. |

The reviewer found the mutation lock, the candidate receipt and base checks,
publication renames, the init stamp, Git and release acquisition, manifest
identity on update, and the installer's atomic writes sound.

## Whole-project audit, 2026-11-18

A third independent review audited the whole manager,
runtime, native module and installer, with proofs of concept run
only in scratch state. It found one medium issue and no critical or high one.

| Severity | Finding | Status |
| --- | --- | --- |
| Medium | Manager errors reached the terminal as received. A plugin manifest's id, a failed build hook's output and Git's stderr tail could carry terminal controls such as an OSC 52 clipboard write or a title change into `binit add` or `binit sync` output. | Fixed: every manager error prints through one filter that turns controls and bidirectional marks into `?`; continuation lines are indented so hook output cannot pass for Binit's own lines. Tests `errors_print_plugin_supplied_text_without_terminal_controls` and `continuation_lines_cannot_pass_for_binit_output`. |
| Medium | A follow-up adversarial review found the same class outside errors: `inspect` printed package program paths and local source paths as received, and `trust` printed the project path and a label with bidirectional marks. | Fixed at output: `inspect` and `trust` print these names through the same plain-text filter. Validation is unchanged, so file names with tabs or right-to-left marks still install and existing generations still verify. Test `review_output_prints_package_and_project_names_without_terminal_controls`. |
| Medium | A Git branch name shown by the Git prompt plugin kept UTF-8 C1 controls and bidirectional marks, because `binit_segment` removed only C0 controls and DEL. | Fixed: `binit_segment` also removes the UTF-8 forms of C1 controls and the bidirectional marks the manager filters, repeating until nothing changes so a removal cannot join its neighbours into a new sequence. Test `prompt_segments_drop_c1_controls_and_bidirectional_marks`, including nested and cross-rule cases. |
| Low | A lone C1 byte such as `0x9b` outside a UTF-8 sequence stays in a prompt segment. A UTF-8 terminal shows it as an invalid byte; a terminal that honours 8-bit C1 controls would read it as CSI. | Accepted: removing bytes 0x80-0x9f outright would break valid UTF-8 text, whose continuation bytes use that range. |
| Low | The native completion menu escaped most bidirectional marks but not U+061C, U+200E or U+200F. | Fixed, with a case in `native/test_menu.c`. |

It re-checked the open items above and found none worse than recorded. It
also confirmed that completion candidates with control characters display as
caret notation through both the native menu and stock Readline; bidirectional
marks in candidates still display as received, a visual effect only.
