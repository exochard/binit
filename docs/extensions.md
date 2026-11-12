# Manager extensions

An enabled plugin can provide a `manager.bash` script at its package root.
Run it explicitly with `binit extension ID [ARG...]`. The manager verifies the
selected generation before executing that script with Bash. Arguments and exit
status pass through; Bash startup files are suppressed. Manager `--state` must
appear before a `--` separator. Arguments after that separator pass through
unchanged, including an extension option named `--state`. `BINIT_PLUGIN_ID` and
`BINIT_EXTENSION_STATE` identify the plugin and private state directory.

Extensions run in a separate Bash process. They cannot directly change the
calling shell. They retain the user's authority and must use the normal manager
commands for validated generation changes. The script is a pinned package file;
editing it inside a generation invalidates that generation.

Use normal `atclone` and `atpull` hooks for build recipes that prepare package
files. These hooks run in disposable staging and must leave selected outputs
within the package limits. Runtime `binit on before-load` and `after-load`
callbacks provide integration in the calling shell. See
[loading and effects](runtime-features.md) for ownership and failure behavior.
