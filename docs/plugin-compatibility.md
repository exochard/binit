# Existing plugin checks

`scripts/check_plugins.py` checks that existing Bash plugins load through Binit:

| Source | Loading | Checked behavior |
| --- | --- | --- |
| fzf, installed Bash completion and key-binding scripts | First `fzf` call | The version command succeeds, the plugin becomes loaded, and `fzf-file-widget` exists. |
| Installed bash-completion | Startup | Initialization succeeds, `_init_completion` exists, and the version array remains available. |
| NVM (pinned `nvm.sh`) | First `nvm` call | The version command succeeds and the private Node directory remains empty; nothing is downloaded. |

Git's full completion definitions are exercised by the default integration and
terminal suites.

Run [the checker](../scripts/check_plugins.py) with local source paths:

```bash
python3 scripts/check_plugins.py --fzf DIR --bash-completion FILE --nvm FILE
```

`--fzf` takes the directory holding `key-bindings.bash` and `completion.bash`,
`--bash-completion` the `bash_completion` script, and `--nvm` a reviewed, pinned
`nvm.sh`. `--output` overrides the receipt path. No upstream source is vendored.

The checker installs declarations into isolated state and exercises an
interactive Bash under a real PTY. Each result records hashes of the pinned
files actually loaded. The receipt is written to the local, untracked
`evidence/plugin-compatibility.json`.

These checks cover initialization and first-use dispatch. Full fzf picker
interaction, Node installation and other Bash versions are not covered.
