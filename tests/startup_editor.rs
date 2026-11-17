#![cfg(feature = "completion-menu")]

use std::fs;
use std::process::Command;
use tempfile::TempDir;

#[test]
fn startup_plugins_can_activate_native_editor_features() {
    let temp = TempDir::new().unwrap();
    let plugin = temp.path().join("editor");
    fs::create_dir(&plugin).unwrap();
    fs::write(
        plugin.join("plugin.toml"),
        "schema=1\nid='editor'\nentry='plugin.bash'\nload='startup'\n",
    )
    .unwrap();
    fs::write(
        plugin.join("plugin.bash"),
        "binit_complete editor suggestions on\n",
    )
    .unwrap();
    let state = temp.path().join("state");
    let binary = env!("CARGO_BIN_EXE_binit");
    let add = Command::new(binary)
        .arg("add")
        .arg(&plugin)
        .arg("--state")
        .arg(&state)
        .output()
        .unwrap();
    assert!(
        add.status.success(),
        "{}",
        String::from_utf8_lossy(&add.stderr)
    );
    let init = Command::new(binary)
        .arg("init")
        .arg("--state")
        .arg(&state)
        .output()
        .unwrap();
    assert!(init.status.success());
    let bash = std::env::var_os("BINIT_BASH").unwrap_or_else(|| "/bin/bash".into());
    let output = Command::new(bash)
        .args(["--noprofile", "--norc", "-ic"])
        .arg(format!(
            "{}\nbinit profile\n",
            String::from_utf8(init.stdout).unwrap()
        ))
        .env_remove("BASH_ENV")
        .env_remove("ENV")
        .env_remove("BINIT_SAFE")
        .env("BINIT_COMPLETION_MENU", "1")
        // Editor features refuse an unset or dumb TERM; do not inherit the caller's.
        .env("TERM", "xterm")
        .env("INPUTRC", "/dev/null")
        .env("HISTFILE", "/dev/null")
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&output.stdout);
    assert!(
        text.lines()
            .any(|line| line.split_whitespace().take(2).eq(["editor", "loaded"])),
        "{text}\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
