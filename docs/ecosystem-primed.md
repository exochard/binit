# Primed private framework state

This track measures configured framework launches after each configuration has
completed a full, effect-checked launch in its own private home. Binit generation
preparation and that initial launch occur before timing. Zinit's first private
initialization also occurs before timing. The fresh-home results remain in
[framework evidence](ecosystem-framework-evidence.md).

The runner uses the pinned [corpus](ecosystem-corpus.md), its adapters and its
common plugin effects. It reuses each configuration's private home on every
measured launch. An inventory checks file contents, modes, links and empty
directories before and after each launch. Any change fails the run and retains
its observations. This rule keeps a disappearing initialized directory from
silently turning a primed launch into a fresh one.

Each successful launch must accept an executed command and show the next
prompt. A separate probe checks every declared plugin effect. Two runs of 20
alternating blocks cover 0, 10 and 50 plugins for each pinned adapter and its
same-interpreter direct-source control. Short runs are diagnostics. Raw timings,
priming observations, tool and configuration identities, resource samples and
partial failures are retained in the receipt. Resource observations have the
non-atomic and missing-child limits of the original framework corpus.

Even the zero-plugin Binit case must expose its public `binit profile` command
and the selected generation identified during preparation. An empty or failed
initialization cannot pass as the zero-plugin direct-shell control.
Each framework case also checks the expected loader location and a callable
framework command; Zinit additionally requires its private state directory.

```bash
python3 scripts/bench_ecosystem_primed.py --sources tmp/ecosystem --binary target/release/binit --work tmp/ecosystem/primed-run --output tmp/ecosystem/primed-run.json
```

The paired fixture uses explicit framework overrides. It does not measure the
documented defaults, editor equivalence or Binit's two new runtime targets.
Zinit runs in Zsh and uses its own direct-source control. No overall resource
ranking follows from the readiness samples.

Each launch uses a private runtime directory in project scratch. The runner
retains terminal output through shell exit and rejects abnormal or forced exits.
Earlier receipts keep their original environment and close-observation scope.
