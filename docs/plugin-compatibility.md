# Existing plugin checks

The local x86_64 check on Bash 5.3.20 passed these cases:

| Source | Loading | Checked behavior |
| --- | --- | --- |
| fzf 0.74.5, installed Bash completion and key-binding scripts | First `fzf` call | The version command succeeds, the plugin becomes loaded, and `fzf-file-widget` exists. |
| Installed bash-completion | Startup | Initialization succeeds, `_init_completion` exists, and the version array remains available. |
| NVM 0.40.3 | First `nvm` call | The version command succeeds and the private Node directory remains empty. |

NVM came from commit
[`977563e97ddc66facf3a8e31c6cff01d236f09bd`](https://github.com/nvm-sh/nvm/blob/977563e97ddc66facf3a8e31c6cff01d236f09bd/nvm.sh),
tagged `v0.40.3`. Its script SHA-256 is
`390260ab9eb1da20e8bc0ebea2ee90f528d53e5e9f6e13b16717db4af454df9d`.
No upstream source is vendored here.

Run [the checker](../scripts/check_plugins.py) with local source paths. It
installs declarations into isolated state and exercises an interactive Bash
under a real PTY. Each result records hashes of the pinned files actually
loaded. The receipt is written under `evidence/`; raw local output stays out
of Git.

These checks cover initialization and first-use dispatch. Full fzf picker
interaction, Node installation, and other Bash versions remain separate
qualification work. Git's full completion definitions are exercised by the
default integration and terminal suites.
