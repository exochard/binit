use std::fs;
use std::process::Command;
use tempfile::TempDir;

#[test]
fn oversized_local_patterns_are_rejected_before_directory_scanning() {
    let temp = TempDir::new().unwrap();
    let root = temp.path();
    let source = root.join("source");
    fs::create_dir(&source).unwrap();
    std::os::unix::fs::symlink("missing", source.join("link")).unwrap();
    let declaration = root.join("plugin.toml");
    fs::write(
        &declaration,
        format!(
            "schema=1\nid='pattern'\npath='source'\nfiles=['{}']\nload='startup'\n",
            "*".repeat(1025)
        ),
    )
    .unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_binit"))
        .arg("add")
        .arg(declaration)
        .arg("--state")
        .arg(root.join("state"))
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("invalid declared file:"),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn local_checkout_metadata_is_not_packaged() {
    let temp = TempDir::new().unwrap();
    let plugin = temp.path().join("plugin");
    fs::create_dir_all(plugin.join(".git")).unwrap();
    fs::write(plugin.join(".git/unused"), vec![0; 2 * 1024 * 1024 + 1]).unwrap();
    fs::write(
        plugin.join("plugin.toml"),
        "schema=1\nid='metadata'\nentry='plugin.bash'\nload='startup'\n",
    )
    .unwrap();
    fs::write(plugin.join("plugin.bash"), ":\n").unwrap();
    let state = temp.path().join("state");
    let output = Command::new(env!("CARGO_BIN_EXE_binit"))
        .args(["add"])
        .arg(&plugin)
        .arg("--state")
        .arg(&state)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let generation = fs::read_to_string(state.join("current")).unwrap();
    assert!(
        !state
            .join("generations")
            .join(generation.trim())
            .join("plugins/metadata/.git")
            .exists()
    );
}
