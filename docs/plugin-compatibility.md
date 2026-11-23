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

## Stock distribution Bash

Pass an extracted release binary and a cached local Docker image to the
stock Bash smoke check. Use an image whose glibc meets the binary's
requirement.

```bash
python3 scripts/smoke_stock_bash.py \
  --image ubuntu:24.04 --binary tmp/archive/bin/binit \
  --work tmp/ubuntu-stock-smoke
```

The shell uses the image's stock Bash in an offline container with private
temporary state. The check verifies which native mode loaded and exercises
the first deferred command and Tab in Emacs and vi. The receipt records the
image ID, Bash and glibc versions, and binary hash. Repeat with a fresh work
directory for each image.

## Real plugin workflow timing

Run [the workflow benchmark](../scripts/bench_plugin_workflows.py) from the
development environment described in [Contributing](../CONTRIBUTING.md).
It starts private Bash sessions with the same bash-completion, Git
completion, fzf, and NVM code. The subjects are direct sourcing, Binit,
Bash-it, and Oh My Bash. A second Binit subject defers NVM until its first
call. Every subject uses Readline in Emacs mode.

```bash
python3 scripts/bench_plugin_workflows.py \
  --sources tmp/framework-sources \
  --plugin-sources tmp/plugin-sources \
  --binary target/release/binit \
  --work tmp/workflow-cases --output tmp/workflow-results.json
```

Place the Bash-it and Oh My Bash archives named `bash-it.tar.gz` and
`oh-my-bash.tar.gz` under `--sources`, with their extracted trees under
`sources/bash-it` and `sources/oh-my-bash` in that directory. The versions
and archive hashes are in [the framework lock](../benchmarks/ecosystem.json).
Place the locked fzf tarball, its two scripts as `fzf-completion.bash` and
`fzf-key-bindings.bash`, and the locked NVM script as `nvm.sh` under
`--plugin-sources`. See [the plugin lock](../benchmarks/plugin-corpus.json)
for their hashes. The harness uses the system completion scripts by default.
`--bash-completion` and `--git-completion` select other reviewed local files.

The default run alternates subject order across two runs of 20 pairs.
Preparation stays outside the timed shell. Each case records the first
visible prompt, the first accepted command, and when the startup
registration checks finish. The terminal lists Git candidates, opens
fzf with Ctrl-R, and selects and runs a seeded history command. The
first and second `nvm --version` calls each have their own duration.
NVM starts with a private empty directory for Node versions.

The receipt contains raw times, input digests, and the effect counts for
each subject and run. A failed or missing case suppresses that run's
timing summary for its subject. Compare the results on the same host and
Bash. This profile uses an uncached Binit generation at launch and plain
Readline for the editor. The release editor and default-configuration
comparisons have their own profiles.
