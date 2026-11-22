# Isolated completion providers

An isolated provider runs outside the interactive Bash process. Typing cancels
its request and keeps the input on the command line. Bash retains ordinary
completion functions for providers that change calling-shell state.

Providers were checked on x86_64 with Bash 5.2 and 5.3.

## Register a provider

Inside an initialized Binit shell:

```bash
binit completions register task /absolute/path/to/task-provider --project local
binit completions list
binit completions disable task
binit completions enable task
binit completions unregister task
```

Here `--project local` is an argument for the provider, passed to it literally.
`PROGRAM` can be an executable path or a command found on `PATH`. Registration
resolves it to an absolute path. Remaining arguments are fixed arguments,
passed literally on every request. They are ordinary process arguments;
completion input is delivered privately through stdin.

A foreign completion specification wins. Registration refuses to replace
it. A matching registration from the same owner can be updated. Unregistering
also refuses to remove a later foreign binding. Disabling keeps the provider
configuration so it can be enabled again.

A completion plugin declares its command names in `plugin.toml` and registers
providers from its entry script. Loading and unloading remain ordinary
calling-shell operations. The external provider starts after registration.
Owned unload removes the registration and its fixed arguments; a later
manual takeover prevents that unload from removing the new provider.

The optional [npm task example](../examples/plugins/npm-tasks/plugin.toml)
reads script names from `package.json` in the current directory. It completes
the third word of `npm run`, such as `npm run t`. It is a protocol example,
not a replacement for complete npm command and option coverage. It requires
Python 3 and runs no npm commands.

## Manage a plugin's completions

```bash
binit completions list --plugin ID
binit completions disable --plugin ID
binit completions enable --plugin ID
```

The list shows each owned command with its plugin ID and state. The selector
includes lazy stubs and isolated providers declared by the plugin. Enabling a
stub leaves the plugin pending until completion first needs it.

A plugin-wide change checks every binding first. If a command has a different
definition, Binit reports that command and leaves the group as it was. These
controls apply to the current shell. A fresh shell uses the generation's
defaults.

## Input and output

The provider inherits the request's working directory. Stdin is a sealed,
anonymous file containing NUL-terminated fields in this order:

| Field | Content |
| --- | --- |
| 0 | `binit-completion-v1` |
| 1 | Bash `COMP_LINE` |
| 2 | `COMP_POINT`, decimal byte offset |
| 3 | `COMP_CWORD`, decimal index |
| 4 | `COMP_TYPE` |
| 5 | `COMP_KEY`, or empty when unset |
| 6 | `COMP_WORDBREAKS` |
| 7 | Decimal word count |
| 8 onward | Each `COMP_WORDS` element, including empty elements |

These are Bash's completion fields. Word-break and quoting behavior follows
Bash; a provider must handle the command syntax it claims to support. Input
does not appear in process arguments or exported `COMP_*` variables. Fixed
registration arguments still appear in the provider's process arguments.

Write each complete candidate followed by a NUL byte to stdout and exit zero.
The final candidate also needs its terminator. Binit accepts results only
after a successful exit and EOF. Nonzero exits and invalid output discard the
whole result. Stderr is discarded. Readline performs insertion and filename
quoting through the existing completion menu or ordinary completion binding.

## Execution contract

Providers must be single-process programs. Process and thread creation are
denied by a Linux syscall filter. A script can use builtins or replace itself
with another executable; pipelines and subprocess helpers are outside this
contract. The provider retains filesystem and network access as the current
user, so registration is for reviewed code.

| Resource | Limit |
| --- | --- |
| Combined request context and exported environment | 64 KiB |
| Candidate output | 1 MiB |
| Candidates | 4,096 |
| Bytes per candidate | 4,096 |
| Request deadline | 2 seconds |
| Worker CPU time | 2 seconds |
| Worker address space | 256 MiB |
| Worker descriptors | 32 |
| Active workers per shell | 1 |
| Native parent descriptors for the request | 2 |

Inherited lower resource limits remain lower. Core dumps are disabled.
The worker cannot raise its limits or clear its parent-exit signal. Binit
cancels on pending input, interruption or timeout, and rejects results when
the line, cursor, directory or generation has changed. Kernel scheduling and
uninterruptible I/O prevent a hard real-time termination guarantee; the
response target is measured over the frozen corpus.

The environment omits `LD_*`, `DYLD_*`, `GLIBC_TUNABLES` and `GCONV_PATH`
before the helper starts, as well as exported `COMP_*` fields. Other exported
variables count toward the context budget. The interactive shell's values
remain intact.

Registration requires the matching native module. A native-disabled build
refuses registration without changing existing completion specifications.
The current implementation requires Linux `close_range`, anonymous sealed
memory files and GNU's spawn close-from operation. Binit 0.1 supports Linux x86_64.
