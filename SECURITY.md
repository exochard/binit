# Security

Report a vulnerability privately through GitHub's **Report a vulnerability**
button on this repository's Security tab. Please do not open a public issue for
it. Include the Binit version, your Bash version and the steps that show the
problem.

## Scope

Binit runs plugin code with the full authority of your shell. A plugin you
declare is trusted code; its content hash detects changes to a prepared
generation but does not establish who wrote it. Reports about that trust model
itself are design feedback rather than vulnerabilities.

Reports in scope include:

- a generation that passes verification after its files were changed;
- shell startup reading anything outside the selected generation, or using the
  network;
- project profiles applying values that were not approved for the file's
  current digest;
- credentials from a Git source URL appearing in output, logs or receipts;
- an isolated completion worker escaping its resource limits or outliving its
  cancellation.

Supported versions are the 0.1.x releases and the default branch.
