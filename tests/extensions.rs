use std::fs;
use std::process::Command;
use tempfile::TempDir;

fn invoke(args: &[&str], root: &std::path::Path) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_binit"))
        .args(args)
        .arg("--state")
        .arg(root.join("state"))
        .output()
        .unwrap()
}

#[test]
fn extension_executes_only_enabled_verified_code_and_preserves_arguments() {
    let temp = TempDir::new().unwrap();
    let root = temp.path();
    let source = root.join("plugin");
    fs::create_dir(&source).unwrap();
    fs::write(
        source.join("plugin.toml"),
        "schema=1\nid='recipe'\nentry='plugin.bash'\nload='startup'\n",
    )
    .unwrap();
    fs::write(source.join("plugin.bash"), ":\n").unwrap();
    fs::write(
        source.join("manager.bash"),
        "printf 'id=%s' \"$BINIT_PLUGIN_ID\"; printf '<%s>' \"$@\"; exit 23\n",
    )
    .unwrap();
    assert!(
        invoke(&["add", source.to_str().unwrap()], root)
            .status
            .success()
    );
    let result = invoke(
        &["extension", "recipe", "two words", "", "$(touch sentinel)"],
        root,
    );
    assert_eq!(result.status.code(), Some(23));
    assert_eq!(
        String::from_utf8(result.stdout).unwrap(),
        "id=recipe<two words><><$(touch sentinel)>"
    );
    assert!(!root.join("sentinel").exists());
    let options = invoke(
        &["extension", "recipe", "--help", "--jobs", "2", "--check"],
        root,
    );
    assert_eq!(options.status.code(), Some(23));
    assert_eq!(
        String::from_utf8(options.stdout).unwrap(),
        "id=recipe<--help><--jobs><2><--check>"
    );
    let options = Command::new(env!("CARGO_BIN_EXE_binit"))
        .args(["extension", "recipe", "--state"])
        .arg(root.join("state"))
        .args(["--", "--state", "literal-extension-option"])
        .output()
        .unwrap();
    assert_eq!(options.status.code(), Some(23));
    assert_eq!(
        String::from_utf8(options.stdout).unwrap(),
        "id=recipe<--state><literal-extension-option>"
    );

    assert!(invoke(&["disable", "recipe"], root).status.success());
    let result = invoke(&["extension", "recipe"], root);
    assert!(!result.status.success());
    assert!(String::from_utf8_lossy(&result.stderr).contains("not enabled"));
    assert!(invoke(&["enable", "recipe"], root).status.success());
    let generation = fs::read_to_string(root.join("state/current")).unwrap();
    fs::write(
        root.join("state/generations")
            .join(generation.trim())
            .join("plugins/recipe/manager.bash"),
        "exit 0\n",
    )
    .unwrap();
    assert!(!invoke(&["extension", "recipe"], root).status.success());
}
