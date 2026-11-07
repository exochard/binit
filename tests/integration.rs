use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use tempfile::TempDir;

#[test]
fn run_preserves_script_arguments_environment_directory_and_status() {
    use std::ffi::OsString;
    use std::os::unix::ffi::OsStringExt;
    let fixture = Fixture::new();
    let script = fixture.write(
        "script.bash",
        "printf '%s\\0' \"$PWD\" \"$BINIT_TEST_VALUE\" \"$@\"; exit 37\n",
    );
    let raw = OsString::from_vec(vec![b'x', 0xff]);
    let result = Command::new(env!("CARGO_BIN_EXE_binit"))
        .arg("run")
        .arg(script)
        .args(["--help", "--state", "two words", "$(touch forbidden)"])
        .arg(&raw)
        .env("BINIT_TEST_VALUE", "present")
        .current_dir(fixture.root())
        .output()
        .unwrap();
    assert_eq!(result.status.code(), Some(37));
    assert!(result.stderr.is_empty());
    let mut expected = format!(
        "{}\0present\0--help\0--state\0two words\0$(touch forbidden)\0",
        fixture.root().display()
    )
    .into_bytes();
    expected.extend_from_slice(&[b'x', 0xff, 0]);
    assert_eq!(result.stdout, expected);
    assert!(!fixture.root().join("forbidden").exists());
    assert!(!fixture.state().exists());
}

#[test]
fn run_uses_only_the_explicitly_selected_engine() {
    let fixture = Fixture::new();
    let engine = fixture.write(
        "engine",
        "#!/bin/bash\nexport BINIT_TEST_ENGINE=selected\nexec /bin/bash \"$@\"\n",
    );
    fs::set_permissions(&engine, fs::Permissions::from_mode(0o700)).unwrap();
    fixture.write(
        "-script.bash",
        "printf '%s:%s' \"$BINIT_TEST_ENGINE\" \"$1\"\n",
    );
    let result = Command::new(env!("CARGO_BIN_EXE_binit"))
        .arg("run")
        .arg("--engine")
        .arg(engine)
        .args(["--", "-script.bash", "argument"])
        .current_dir(fixture.root())
        .output()
        .unwrap();
    assert!(result.status.success(), "{:?}", result.stderr);
    assert_eq!(result.stdout, b"selected:argument");
}

#[test]
fn run_reports_missing_arguments_and_engine_failures() {
    for args in [
        vec!["run"],
        vec!["run", "--engine"],
        vec!["run", "--engine", ""],
        vec!["run", "--engine", "/no-such-binit-engine", "script.bash"],
    ] {
        let result = Command::new(env!("CARGO_BIN_EXE_binit"))
            .args(args)
            .output()
            .unwrap();
        assert!(!result.status.success());
        assert!(result.stdout.is_empty());
        assert!(String::from_utf8_lossy(&result.stderr).starts_with("binit: "));
    }
}

struct Fixture {
    dir: TempDir,
}

#[test]
fn apply_keeps_disabled_plugins_and_their_pinned_source() {
    let f = Fixture::new();
    f.plugin("sample", "load='startup'", "original() { :; }\n");
    assert!(f.prepare(&["sample"]).status.success());
    assert!(f.cli(&["disable", "sample"]).status.success());
    f.write("sample/plugin.bash", "unreviewed() { :; }\n");
    assert!(f.cli(&["apply"]).status.success());
    let current = fs::read_to_string(f.state().join("current")).unwrap();
    let package = f
        .state()
        .join("generations")
        .join(current)
        .join("plugins/sample/plugin.bash");
    assert_eq!(fs::read_to_string(package).unwrap(), "original() { :; }\n");
    let result = f.cli(&["plugins"]);
    assert!(result.status.success());
    assert!(String::from_utf8_lossy(&result.stdout).contains("disabled"));
}

#[test]
#[cfg(feature = "completion-menu")]
fn native_module_is_pinned_and_verified_with_the_generation() {
    let f = Fixture::new();
    assert!(f.cli(&["apply"]).status.success());
    let current = fs::read_to_string(f.state().join("current")).unwrap();
    let module = f
        .state()
        .join("generations")
        .join(current)
        .join("completion.so");
    assert!(fs::metadata(&module).unwrap().len() > 0);
    fs::write(module, b"changed").unwrap();
    let result = f.cli(&["status"]);
    assert!(!result.status.success());
    assert!(String::from_utf8_lossy(&result.stderr).contains("content changed: completion.so"));
}
impl Fixture {
    fn new() -> Self {
        let scratch = Path::new(env!("CARGO_MANIFEST_DIR")).join(".scratch-tests");
        fs::create_dir_all(&scratch).unwrap();
        Self {
            dir: tempfile::tempdir_in(scratch).unwrap(),
        }
    }
    fn root(&self) -> &Path {
        self.dir.path()
    }
    fn state(&self) -> PathBuf {
        self.root().join("state")
    }
    fn write(&self, name: &str, contents: &str) -> PathBuf {
        let path = self.root().join(name);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, contents).unwrap();
        path
    }
    fn cli(&self, args: &[&str]) -> Output {
        Command::new(env!("CARGO_BIN_EXE_binit"))
            .args(args)
            .arg("--state")
            .arg(self.state())
            .output()
            .unwrap()
    }
    fn plugin(&self, id: &str, options: &str, body: &str) {
        self.write(
            &format!("{id}/plugin.toml"),
            &format!("schema=1\nid='{id}'\nentry='plugin.bash'\n{options}\n"),
        );
        self.write(&format!("{id}/plugin.bash"), body);
    }
    fn prepare(&self, ids: &[&str]) -> Output {
        let mut config = "schema=1\n".to_owned();
        for id in ids {
            config.push_str(&format!("[[plugins]]\npath='{id}'\n"));
        }
        let path = self.write("config.toml", &config);
        self.cli(&["prepare", path.to_str().unwrap()])
    }
    fn demo(&self) {
        let config = Path::new(env!("CARGO_MANIFEST_DIR")).join("config/demo.toml");
        success(self.cli(&["prepare", config.to_str().unwrap()]));
    }
    fn generation(&self) -> PathBuf {
        self.state()
            .join("generations")
            .join(fs::read_to_string(self.state().join("current")).unwrap())
    }
    fn bash(&self, script: &str) -> Output {
        Command::new("bash")
            .args(["--noprofile", "--norc", "-ic", script])
            .env("BINIT_TEST_INIT", self.generation().join("init.bash"))
            .env("BINIT_TEST_ROOT", self.root())
            .env("BINIT_TEST_BINARY", env!("CARGO_BIN_EXE_binit"))
            .env("TERM", "dumb")
            .env_remove("BASH_ENV")
            .env_remove("ENV")
            .current_dir(self.root())
            .output()
            .unwrap()
    }
}
fn success(output: Output) -> String {
    assert!(
        output.status.success(),
        "stdout={}\nstderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap()
}
fn failure(output: Output, message: &str) {
    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr).contains(message),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn prepared_plugins_are_snapshots_and_content_changes_are_detected() {
    let f = Fixture::new();
    f.plugin("sample", "load='startup'", "BINIT_SAMPLE=original\n");
    success(f.prepare(&["sample"]));
    f.write("sample/plugin.bash", "BINIT_SAMPLE=changed\n");
    assert_eq!(
        success(f.bash("source \"$BINIT_TEST_INIT\"; printf %s \"$BINIT_SAMPLE\"")),
        "original"
    );
    fs::write(
        f.generation().join("plugins/sample/plugin.bash"),
        "BINIT_SAMPLE=tampered\n",
    )
    .unwrap();
    failure(f.cli(&["init"]), "content changed");
}

#[test]
fn rollback_and_identical_prepare_preserve_the_previous_generation() {
    let f = Fixture::new();
    f.plugin("sample", "load='startup'", "BINIT_SAMPLE=one\n");
    success(f.prepare(&["sample"]));
    let first = f.generation();
    f.write("sample/plugin.bash", "BINIT_SAMPLE=two\n");
    success(f.prepare(&["sample"]));
    success(f.prepare(&["sample"]));
    success(f.cli(&["rollback"]));
    assert_eq!(f.generation(), first);
}

#[test]
fn dependency_order_and_missing_or_cyclic_dependencies() {
    let f = Fixture::new();
    f.plugin("zbase", "load='startup'", "BINIT_ORDER=base\n");
    f.plugin(
        "achild",
        "load='startup'\ndepends=['zbase']",
        "BINIT_ORDER+=:child\n",
    );
    success(f.prepare(&["achild", "zbase"]));
    assert_eq!(
        success(f.bash("source \"$BINIT_TEST_INIT\"; printf %s \"$BINIT_ORDER\"")),
        "base:child"
    );
    failure(f.prepare(&["achild"]), "missing dependency");
    f.plugin("zbase", "load='startup'\ndepends=['achild']", ":\n");
    failure(f.prepare(&["achild", "zbase"]), "dependency cycle");
}

#[test]
fn duplicate_ids_and_unknown_manifest_fields_are_rejected() {
    let f = Fixture::new();
    f.plugin("sample", "load='startup'", ":\n");
    failure(f.prepare(&["sample", "sample"]), "duplicate plugin");
    f.plugin("sample", "load='startup'\nnetwork=false", ":\n");
    failure(f.prepare(&["sample"]), "unknown field");
}

#[test]
fn traversal_and_symlinked_packages_are_rejected() {
    let f = Fixture::new();
    f.plugin("sample", "load='startup'", ":\n");
    f.write(
        "sample/plugin.toml",
        "schema=1\nid='sample'\nentry='../outside.bash'\nload='startup'\n",
    );
    failure(f.prepare(&["sample"]), "invalid plugin");
    f.plugin("sample", "load='startup'", ":\n");
    symlink(
        f.root().join("config.toml"),
        f.root().join("sample/linked.bash"),
    )
    .unwrap();
    failure(f.prepare(&["sample"]), "symlinks");
}

#[test]
fn public_state_directory_and_symlinked_generation_are_rejected() {
    let f = Fixture::new();
    fs::create_dir(f.state()).unwrap();
    fs::set_permissions(f.state(), fs::Permissions::from_mode(0o755)).unwrap();
    f.plugin("sample", "load='startup'", ":\n");
    failure(f.prepare(&["sample"]), "private directory");
    fs::set_permissions(f.state(), fs::Permissions::from_mode(0o700)).unwrap();
    success(f.prepare(&["sample"]));
    let generation = f.generation();
    let moved = f.state().join("saved");
    fs::rename(&generation, &moved).unwrap();
    symlink(moved, generation).unwrap();
    failure(f.cli(&["init"]), "private directory");
}

#[test]
fn recovery_and_noninteractive_sourcing_do_not_initialize() {
    let f = Fixture::new();
    f.demo();
    assert_eq!(success(f.bash("BINIT_SAFE=1; source \"$BINIT_TEST_INIT\"; declare -F binit >/dev/null && exit 1; printf safe")), "safe");
    let output = Command::new("bash")
        .args([
            "--noprofile",
            "--norc",
            "-c",
            "source \"$1\"; declare -F binit >/dev/null && exit 1; printf untouched",
            "test",
        ])
        .arg(f.generation().join("init.bash"))
        .output()
        .unwrap();
    assert_eq!(success(output), "untouched");
}

#[test]
fn prompt_hooks_keep_status_and_existing_array_hooks_and_install_once() {
    let f = Fixture::new();
    f.demo();
    let output = f.bash(
        r#"
        PROMPT_COMMAND=('BINIT_OLD_STATUS=$?' 'BINIT_OLD_SECOND=yes')
        trap ':' DEBUG
        before_trap=$(trap -p DEBUG)
        before_options=$-
        source "$BINIT_TEST_INIT"
        source "$BINIT_TEST_INIT"
        [[ ${#PROMPT_COMMAND[@]} == 3 && ${PROMPT_COMMAND[0]} == __binit_prompt_dispatch ]] || exit 1
        [[ $(trap -p DEBUG) == "$before_trap" && $- == "$before_options" ]] || exit 2
        false
        __binit_prompt_dispatch
        eval "${PROMPT_COMMAND[1]}"
        eval "${PROMPT_COMMAND[2]}"
        [[ $BINIT_LAST_STATUS == 1 && $BINIT_OLD_STATUS == 1 && $BINIT_OLD_SECOND == yes ]] || exit 3
        printf preserved
    "#,
    );
    assert_eq!(success(output), "preserved");
}

#[test]
fn scalar_prompt_hooks_and_nounset_are_supported() {
    let f = Fixture::new();
    f.demo();
    assert_eq!(
        success(f.bash(
            r#"
        set -u
        PROMPT_COMMAND='BINIT_OLD_STATUS=$?'
        source "$BINIT_TEST_INIT"
        false
        __binit_prompt_dispatch
        eval "${PROMPT_COMMAND[1]}"
        [[ $BINIT_OLD_STATUS == 1 ]] || exit 1
        printf preserved
    "#
        )),
        "preserved"
    );
}

#[test]
fn completion_loads_on_first_request_and_existing_completions_win() {
    let f = Fixture::new();
    f.demo();
    assert_eq!(
        success(f.bash(
            r#"
        source "$BINIT_TEST_INIT"
        [[ ${__binit_state[git-completion]} == pending ]] || exit 1
        COMP_WORDS=(git sta); COMP_CWORD=1
        COMP_LINE='git sta'; COMP_POINT=${#COMP_LINE}
        __binit_complete; result=$?
        [[ $result == 124 && ${__binit_state[git-completion]} == loaded ]] || exit 2
        spec=$(complete -p git)
        [[ $spec =~ -F[[:space:]]([^[:space:]]+) ]] || exit 3
        "${BASH_REMATCH[1]}" git sta git
        [[ ${COMPREPLY[*]} == *stash* && ${COMPREPLY[*]} == *status* ]] || exit 4
        printf lazy
    "#
        )),
        "lazy"
    );
    assert_eq!(
        success(f.bash(
            r#"
        old_complete() { COMPREPLY=(existing); }
        complete -F old_complete git
        source "$BINIT_TEST_INIT"
        [[ $(complete -p git) == *old_complete* ]] || exit 1
        printf kept
    "#
        )),
        "kept"
    );
}

#[test]
fn unavailable_and_failing_plugins_leave_a_usable_shell() {
    let f = Fixture::new();
    f.plugin(
        "unavailable",
        "load='startup'\nrequires=['binit-nonexistent-9f32']",
        "exit 99\n",
    );
    f.plugin("failing", "load='startup'", "return 2\n");
    success(f.prepare(&["unavailable", "failing"]));
    assert_eq!(success(f.bash(r#"
        source "$BINIT_TEST_INIT"
        [[ ${__binit_state[unavailable]} == unavailable && ${__binit_state[failing]} == failed ]] || exit 1
        printf usable
    "#)), "usable");
}

#[test]
fn project_trust_is_bound_to_content_and_data_is_literal() {
    let f = Fixture::new();
    f.demo();
    f.write("project/.binit-project.toml", "label='sample'\n[env]\nPROJECT_MODE='active'\nPROJECT_EXPORTED='during'\nPROJECT_LITERAL='$(touch injection-marker)'\n");
    failure(
        f.cli(&["project", f.root().join("project").to_str().unwrap()]),
        "No such file",
    );
    success(f.cli(&["trust", f.root().join("project").to_str().unwrap()]));
    assert_eq!(
        success(f.bash(
            r#"
        PROJECT_MODE=before
        export PROJECT_EXPORTED=before
        source "$BINIT_TEST_INIT"
        cd project
        __binit_prompt_dispatch
        [[ $PROJECT_MODE == active && $PROJECT_LITERAL == '$(touch injection-marker)' ]] || exit 1
        cd ..
        __binit_prompt_dispatch
        [[ $PROJECT_MODE == before && ! -v PROJECT_LITERAL ]] || exit 2
        [[ $(declare -p PROJECT_MODE) == 'declare -- '* ]] || exit 3
        [[ $(declare -p PROJECT_EXPORTED) == 'declare -x '* ]] || exit 4
        printf literal
    "#
        )),
        "literal"
    );
    assert!(!f.root().join("project/injection-marker").exists());
    f.write(
        "project/.binit-project.toml",
        "[env]\nPROJECT_MODE='changed'\n",
    );
    failure(
        f.cli(&["project", f.root().join("project").to_str().unwrap()]),
        "content changed",
    );
}

#[test]
fn protected_project_variables_and_symlinked_profiles_are_rejected() {
    let f = Fixture::new();
    for key in [
        "PATH",
        "POSIXLY_CORRECT",
        "BASH_ENV",
        "IFS",
        "HISTFILE",
        "BINIT_STATE_DIR",
        "LD_PRELOAD",
        "A[0]",
    ] {
        f.write(
            "project/.binit-project.toml",
            &format!("[env]\n'{key}'='unsafe'\n"),
        );
        failure(
            f.cli(&["trust", f.root().join("project").to_str().unwrap()]),
            "unsupported project variable",
        );
    }
    fs::rename(
        f.root().join("project/.binit-project.toml"),
        f.root().join("profile.toml"),
    )
    .unwrap();
    symlink(
        f.root().join("profile.toml"),
        f.root().join("project/.binit-project.toml"),
    )
    .unwrap();
    failure(
        f.cli(&["trust", f.root().join("project").to_str().unwrap()]),
        "regular file",
    );
}

#[test]
fn integer_project_variables_cannot_evaluate_profile_data() {
    let f = Fixture::new();
    f.demo();
    f.write(
        "project/.binit-project.toml",
        "[env]\nPROJECT_NUMBER='$(touch integer-injection)'\n",
    );
    success(f.cli(&["trust", f.root().join("project").to_str().unwrap()]));
    assert_eq!(
        success(f.bash(
            r#"
        declare -i PROJECT_NUMBER=42
        source "$BINIT_TEST_INIT"
        cd project
        __binit_prompt_dispatch
        [[ $PROJECT_NUMBER == 42 ]] || exit 1
        printf kept
    "#
        )),
        "kept"
    );
    assert!(!f.root().join("project/integer-injection").exists());
}

#[test]
fn prompt_segment_data_does_not_become_prompt_code() {
    let f = Fixture::new();
    f.demo();
    assert_eq!(
        success(f.bash(
            r#"
        source "$BINIT_TEST_INIT"
        binit_segment test '$(touch prompt-injection)'
        __binit_prompt_dispatch
        expanded=${PS1@P}
        [[ $expanded == *'$(touch prompt-injection)'* ]] || exit 1
        printf literal
    "#
        )),
        "literal"
    );
    assert!(!f.root().join("prompt-injection").exists());
}

#[test]
fn completion_dependencies_respect_existing_user_specs() {
    let f = Fixture::new();
    f.plugin(
        "completion",
        "load='completion'\ncompletions=['git']",
        "replacement_complete() { :; }\ncomplete -F replacement_complete git\n",
    );
    f.plugin(
        "consumer",
        "load='startup'\ndepends=['completion']",
        "BINIT_CONSUMER=loaded\n",
    );
    success(f.prepare(&["completion", "consumer"]));
    assert_eq!(success(f.bash(r#"
        existing_complete() { :; }
        complete -F existing_complete git
        source "$BINIT_TEST_INIT"
        [[ $(complete -p git) == *existing_complete* ]] || exit 1
        [[ ${__binit_state[completion]} == blocked && ${__binit_state[consumer]} == unavailable ]] || exit 2
        [[ ! -v BINIT_CONSUMER ]] || exit 3
        printf kept
    "#)), "kept");
}

#[test]
fn declared_unset_project_variables_keep_their_export_attributes() {
    let f = Fixture::new();
    f.demo();
    f.write(
        "project/.binit-project.toml",
        "[env]\nPROJECT_EXPORTED='during'\nPROJECT_DECLARED='during'\n",
    );
    success(f.cli(&["trust", f.root().join("project").to_str().unwrap()]));
    assert_eq!(
        success(f.bash(
            r#"
        export PROJECT_EXPORTED
        declare PROJECT_DECLARED
        source "$BINIT_TEST_INIT"
        cd project
        __binit_prompt_dispatch
        [[ $PROJECT_EXPORTED == during && $PROJECT_DECLARED == during ]] || exit 1
        cd ..
        __binit_prompt_dispatch
        [[ ! -v PROJECT_EXPORTED && ! -v PROJECT_DECLARED ]] || exit 2
        [[ $(declare -p PROJECT_EXPORTED) == 'declare -x PROJECT_EXPORTED' ]] || exit 3
        [[ $(declare -p PROJECT_DECLARED) == 'declare -- PROJECT_DECLARED' ]] || exit 4
        printf restored
    "#
        )),
        "restored"
    );
}

#[test]
fn unset_prompt_hooks_and_disabled_prompt_expansion_keep_shell_options() {
    let f = Fixture::new();
    f.demo();
    assert_eq!(
        success(f.bash(
            r#"
        unset PROMPT_COMMAND
        set -u
        shopt -u promptvars
        PS1='plain> '
        source "$BINIT_TEST_INIT"
        [[ $PS1 == 'plain> ' ]] || exit 1
        shopt -q promptvars && exit 2
        __binit_prompt_dispatch
        printf preserved
    "#
        )),
        "preserved"
    );
}

#[test]
fn plugin_add_update_and_inspect_use_reviewed_snapshots() {
    let f = Fixture::new();
    f.plugin("custom", "load='startup'", "BINIT_CUSTOM=one\n");
    let source = f.root().join("custom");
    success(f.cli(&["add", source.to_str().unwrap()]));
    assert!(success(f.cli(&["plugins"])).contains("custom"));
    let info = success(f.cli(&["inspect", "custom"]));
    assert!(info.contains("Entry SHA256:"));
    assert!(info.contains(source.to_str().unwrap()));
    f.write("custom/plugin.bash", "BINIT_CUSTOM=two\n");
    assert_eq!(
        success(f.bash("source \"$BINIT_TEST_INIT\"; printf %s \"$BINIT_CUSTOM\"")),
        "one"
    );
    success(f.cli(&["update", "custom"]));
    assert_eq!(
        success(f.bash("source \"$BINIT_TEST_INIT\"; printf %s \"$BINIT_CUSTOM\"")),
        "two"
    );
}

#[test]
fn disabled_plugins_can_be_enabled_without_the_original_source() {
    let f = Fixture::new();
    f.plugin("custom", "load='startup'", "BINIT_CUSTOM=yes\n");
    let source = f.root().join("custom");
    success(f.cli(&["add", source.to_str().unwrap(), "--disabled"]));
    assert!(success(f.cli(&["plugins"])).contains("disabled"));
    fs::rename(source, f.root().join("moved-source")).unwrap();
    assert_eq!(
        success(f.bash("source \"$BINIT_TEST_INIT\"; printf %s \"${BINIT_CUSTOM-absent}\"")),
        "absent"
    );
    success(f.cli(&["enable", "custom"]));
    assert_eq!(
        success(f.bash("source \"$BINIT_TEST_INIT\"; printf %s \"$BINIT_CUSTOM\"")),
        "yes"
    );
    success(f.cli(&["disable", "custom"]));
    assert_eq!(
        success(f.bash("source \"$BINIT_TEST_INIT\"; printf %s \"${BINIT_CUSTOM-absent}\"")),
        "absent"
    );
    success(f.cli(&["remove", "custom"]));
    assert_eq!(success(f.cli(&["plugins"])).trim(), "No plugins installed");
}

#[test]
fn dependency_breaking_changes_leave_the_current_generation_intact() {
    let f = Fixture::new();
    f.plugin("base", "load='startup'", "BINIT_BASE=yes\n");
    f.plugin(
        "consumer",
        "load='startup'\ndepends=['base']",
        "BINIT_CONSUMER=yes\n",
    );
    success(f.prepare(&["base", "consumer"]));
    let original = f.generation();
    failure(f.cli(&["disable", "base"]), "missing dependency");
    assert_eq!(f.generation(), original);
    failure(f.cli(&["remove", "base"]), "missing dependency");
    assert_eq!(f.generation(), original);
    success(f.cli(&["disable", "consumer"]));
    success(f.cli(&["disable", "base"]));
    assert_eq!(success(f.bash("source \"$BINIT_TEST_INIT\"; printf %s \"${BINIT_BASE-absent}:${BINIT_CONSUMER-absent}\"")), "absent:absent");
}

#[test]
fn failed_adds_and_updates_do_not_publish_partial_state() {
    let f = Fixture::new();
    f.plugin("custom", "load='startup'", "BINIT_CUSTOM=good\n");
    success(f.prepare(&["custom"]));
    let original = f.generation();
    failure(
        f.cli(&["add", f.root().join("custom").to_str().unwrap()]),
        "already installed",
    );
    assert_eq!(f.generation(), original);
    f.write(
        "custom/plugin.toml",
        "schema=1\nid='changed'\nentry='plugin.bash'\nload='startup'\n",
    );
    failure(f.cli(&["update", "custom"]), "changes plugin ID");
    assert_eq!(f.generation(), original);
    f.write(
        "custom/plugin.toml",
        "schema=1\nid='custom'\nentry='../escape.bash'\nload='startup'\n",
    );
    failure(f.cli(&["update", "custom"]), "invalid plugin");
    assert_eq!(f.generation(), original);
    assert_eq!(
        success(f.bash("source \"$BINIT_TEST_INIT\"; printf %s \"$BINIT_CUSTOM\"")),
        "good"
    );
}

#[test]
fn rollback_restores_catalog_settings_and_plugin_code() {
    let f = Fixture::new();
    f.plugin("custom", "load='startup'", "BINIT_CUSTOM=good\n");
    success(f.prepare(&["custom"]));
    success(f.cli(&["disable", "custom"]));
    assert!(success(f.cli(&["plugins"])).contains("disabled"));
    success(f.cli(&["rollback"]));
    assert!(success(f.cli(&["plugins"])).contains("enabled"));
    assert_eq!(
        success(f.bash("source \"$BINIT_TEST_INIT\"; printf %s \"$BINIT_CUSTOM\"")),
        "good"
    );
}

#[test]
fn prepare_retains_disabled_packages_for_later_activation() {
    let f = Fixture::new();
    f.plugin("custom", "load='startup'", "BINIT_CUSTOM=yes\n");
    let config = f.write(
        "config.toml",
        "schema=1\n[[plugins]]\npath='custom'\nenabled=false\n",
    );
    success(f.cli(&["prepare", config.to_str().unwrap()]));
    assert!(success(f.cli(&["plugins"])).contains("disabled"));
    success(f.cli(&["enable", "custom"]));
    assert_eq!(
        success(f.bash("source \"$BINIT_TEST_INIT\"; printf %s \"$BINIT_CUSTOM\"")),
        "yes"
    );
}

#[test]
fn mutation_lock_prevents_lost_updates() {
    use std::os::fd::AsRawFd;
    let f = Fixture::new();
    f.plugin("custom", "load='startup'", ":\n");
    success(f.prepare(&["custom"]));
    let original = f.generation();
    let lock = fs::File::open(f.state().join(".mutation.lock")).unwrap();
    assert_eq!(
        unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
        0
    );
    failure(f.cli(&["disable", "custom"]), "another Binit change");
    assert_eq!(f.generation(), original);
    drop(lock);
    success(f.cli(&["disable", "custom"]));
}

#[test]
fn native_git_completion_handles_branches_options_and_preserves_gitk() {
    let f = Fixture::new();
    f.demo();
    assert_eq!(success(f.bash(r#"
        export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
        git init --quiet --initial-branch=main repo || exit 1
        cd repo
        git -c user.name=BinitTest -c user.email=fixtures@localhost -c core.hooksPath=/dev/null -c commit.gpgsign=false commit --quiet --allow-empty -m fixture || exit 2
        git branch feature/binit || exit 3
        old_gitk() { :; }
        complete -F old_gitk gitk
        source "$BINIT_TEST_INIT"
        COMP_WORDS=(git switch feature/); COMP_CWORD=2
        COMP_LINE='git switch feature/'; COMP_POINT=${#COMP_LINE}
        __binit_complete
        [[ $? == 124 ]] || exit 4
        [[ $(complete -p gitk) == *old_gitk* ]] || exit 5
        spec=$(complete -p git)
        [[ $spec =~ -F[[:space:]]([^[:space:]]+) ]] || exit 6
        callback=${BASH_REMATCH[1]}
        "$callback" git feature/ switch
        [[ ${COMPREPLY[*]} == *feature/binit* ]] || exit 7
        COMP_WORDS=(git log --one); COMP_CWORD=2
        COMP_LINE='git log --one'; COMP_POINT=${#COMP_LINE}
        "$callback" git --one log
        [[ ${COMPREPLY[*]} == *--oneline* ]] || exit 8
        printf native
    "#)), "native");
}

#[test]
fn schema_one_generations_migrate_without_losing_plugins() {
    use sha2::{Digest, Sha256};
    let f = Fixture::new();
    f.plugin("custom", "load='startup'", "BINIT_CUSTOM=legacy\n");
    success(f.prepare(&["custom"]));
    let original = f.generation();
    let mut lock: toml::Value =
        toml::from_str(&fs::read_to_string(original.join("lock.toml")).unwrap()).unwrap();
    let table = lock.as_table_mut().unwrap();
    table.insert("schema".into(), toml::Value::Integer(1));
    table.remove("catalog");
    table
        .get_mut("files")
        .unwrap()
        .as_table_mut()
        .unwrap()
        .remove("completion.so");
    let encoded = toml::to_string_pretty(&lock).unwrap();
    let id = format!("{:x}", Sha256::digest(encoded.as_bytes()));
    let legacy = f.state().join("generations").join(&id);
    fs::create_dir_all(legacy.join("plugins/custom")).unwrap();
    fs::set_permissions(&legacy, fs::Permissions::from_mode(0o700)).unwrap();
    for file in [
        "init.bash",
        "runtime.bash",
        "plugins/custom/plugin.toml",
        "plugins/custom/plugin.bash",
    ] {
        fs::copy(original.join(file), legacy.join(file)).unwrap();
    }
    fs::write(legacy.join("lock.toml"), encoded).unwrap();
    fs::write(f.state().join("current"), &id).unwrap();
    assert!(success(f.cli(&["plugins"])).contains("custom"));
    success(f.cli(&["disable", "custom"]));
    success(f.cli(&["enable", "custom"]));
    assert_eq!(
        success(f.bash("source \"$BINIT_TEST_INIT\"; printf %s \"$BINIT_CUSTOM\"")),
        "legacy"
    );
    failure(
        f.cli(&["update", "custom"]),
        "legacy plugin has no source path",
    );
}

#[test]
fn declarations_pin_selected_scripts_in_order_and_support_lifecycle_changes() {
    let f = Fixture::new();
    f.write("upstream/first.sh", "BINIT_SEQUENCE=first\n");
    f.write("upstream/second 'file.sh", "BINIT_SEQUENCE+=:second\n");
    f.write("upstream/unselected.sh", "return 99\n");
    let declaration = f.write(
        "declarations/tools.toml",
        r#"
schema=1
id='tools'
path='../upstream'
files=['first.sh', "second 'file.sh"]
load='startup'
"#,
    );
    success(f.cli(&["add", declaration.to_str().unwrap()]));
    assert!(!f.root().join("upstream/plugin.toml").exists());
    assert!(
        !f.generation()
            .join("plugins/tools/files/unselected.sh")
            .exists()
    );
    assert!(
        f.generation()
            .join("plugins/tools/declaration.toml")
            .is_file()
    );
    f.write("upstream/first.sh", "BINIT_SEQUENCE=changed\n");
    success(f.cli(&["disable", "tools"]));
    success(f.cli(&["enable", "tools"]));
    let script = "source \"$BINIT_TEST_INIT\"; printf %s \"$BINIT_SEQUENCE\"";
    assert_eq!(success(f.bash(script)), "first:second");
    success(f.cli(&["update", "tools"]));
    assert_eq!(success(f.bash(script)), "changed:second");
    success(f.cli(&["rollback"]));
    assert_eq!(success(f.bash(script)), "first:second");
    success(f.prepare(&["declarations/tools.toml"]));
    assert_eq!(success(f.bash(script)), "changed:second");
}

#[test]
fn first_use_runs_once_in_the_calling_shell_and_preserves_arguments_and_status() {
    let f = Fixture::new();
    f.write("after/marker", "");
    f.plugin(
        "tools",
        "load='command'\ncommands=['tool', 'helper']",
        r#"
BINIT_LOADS=$(( ${BINIT_LOADS:-0} + 1 ))
id=upstream
name=upstream
helper() { printf helper; }
tool() { export BINIT_CALLED=present; cd "$BINIT_TEST_ROOT/after"; printf '<%s>' "$@"; return 37; }
"#,
    );
    success(f.prepare(&["tools"]));
    assert_eq!(
        success(f.bash(
            r#"
set -u
trap ':' DEBUG
before=$(trap -p DEBUG)
source "$BINIT_TEST_INIT"
[[ ${__binit_state[tools]} == pending && ! -v BINIT_LOADS ]] || exit 2
tool 'two words' '$(touch forbidden)' --flag
status=$?
[[ $status == 37 && $BINIT_LOADS == 1 && $BINIT_CALLED == present ]] || exit 3
[[ $PWD == "$BINIT_TEST_ROOT/after" && $id == upstream && $name == upstream ]] || exit 4
[[ $(trap -p DEBUG) == "$before" ]] || exit 5
helper
[[ $BINIT_LOADS == 1 && ${__binit_state[tools]} == loaded ]] || exit 6
"#
        )),
        "<two words><$(touch forbidden)><--flag>helper"
    );
    assert!(!f.root().join("forbidden").exists());
}

#[test]
fn first_use_can_initialize_an_external_command_without_defining_a_function() {
    let f = Fixture::new();
    let executable = f.write(
        "bin/tool",
        "#!/bin/bash\nprintf '%s:%s' \"$BINIT_EXTERNAL\" \"$1\"; exit 23\n",
    );
    fs::set_permissions(executable, fs::Permissions::from_mode(0o700)).unwrap();
    f.plugin(
        "tools",
        "load='command'\ncommands=['tool']\nrequires=['tool']",
        "export BINIT_EXTERNAL=initialized\n",
    );
    success(f.prepare(&["tools"]));
    assert_eq!(
        success(f.bash(
            r#"
PATH="$BINIT_TEST_ROOT/bin:$PATH"
source "$BINIT_TEST_INIT"
tool 'two words'; status=$?
[[ $status == 23 && ${__binit_state[tools]} == loaded ]] || exit 2
[[ $(type -t tool) == file ]] || exit 3
"#
        )),
        "initialized:two words"
    );
}

#[test]
fn first_use_preserves_ifs_and_ignores_debug_stdout_when_tracking_functions() {
    for prelude in ["IFS=:", ":"] {
        let f = Fixture::new();
        for name in ["tool", "helper"] {
            let file = f.write(
                &format!("bin/{name}"),
                "#!/bin/bash\nprintf %s \"$BINIT_EXTERNAL\"\n",
            );
            fs::set_permissions(file, fs::Permissions::from_mode(0o700)).unwrap();
        }
        f.plugin("base", "load='prompt'", ":\n");
        f.plugin("tools", "load='command'\ncommands=['tool','helper']\ndepends=['base']\nrequires=['true','printf']", "BINIT_SAVED_IFS=$IFS; export BINIT_EXTERNAL=loaded\n");
        success(f.prepare(&["base", "tools"]));
        success(f.bash(&format!(
            r#"
FUNCNEST=20
PATH="$BINIT_TEST_ROOT/bin:$PATH"
{prelude}
set -T
trap 'printf "TRACE:%s\n" "$BASH_COMMAND"' DEBUG
source "$BINIT_TEST_INIT"
IFS=:
tool || exit 2
helper || exit 3
[[ $BINIT_SAVED_IFS == : && $IFS == : && $- == *T* ]] || exit 4
[[ ${{__binit_state[tools]}} == loaded && $(type -t tool) == *file* ]] || exit 5
"#
        )));
    }
}

#[test]
fn source_and_hook_scopes_do_not_shadow_ordinary_plugin_variables() {
    for (load, options, trigger) in [
        ("startup", "", ":"),
        ("prompt", "", "__binit_prompt_dispatch"),
        (
            "completion",
            "completions=['scope_tool']",
            "COMP_WORDS=(scope_tool); __binit_complete; [[ $? == 124 ]] || exit 2",
        ),
    ] {
        let f = Fixture::new();
        f.plugin("scope", &format!("load='{load}'\n{options}"), "id=upstream; command=upstream; name=upstream; status=upstream; callback=upstream; value=upstream\n");
        success(f.prepare(&["scope"]));
        success(f.bash(&format!(r#"
set -u
source "$BINIT_TEST_INIT"
{trigger}
[[ $id == upstream && $command == upstream && $name == upstream && $status == upstream && $callback == upstream && $value == upstream ]] || exit 3
"#)));
    }
}

#[test]
fn existing_functions_aliases_builtins_and_keywords_block_first_use_loading() {
    for (name, prelude) in [
        ("tool", "tool() { :; }"),
        ("tool", "alias tool='printf original'"),
        ("printf", ":"),
        ("if", ":"),
    ] {
        let f = Fixture::new();
        f.plugin(
            "tools",
            &format!("load='command'\ncommands=['{name}']"),
            "BINIT_SHOULD_NOT_LOAD=1\n",
        );
        success(f.prepare(&["tools"]));
        success(f.bash(&format!(
            r#"
{prelude}
set -T
trap 'printf "TRACE:%s\n" "$BASH_COMMAND"' DEBUG
before=$(type -- {name})
source "$BINIT_TEST_INIT"
[[ ${{__binit_state[tools]}} == blocked && ! -v BINIT_SHOULD_NOT_LOAD ]] || exit 2
[[ $(type -- {name}) == "$before" ]] || exit 3
[[ ${{__binit_reason[tools]}} == 'command already defined: {name}' ]] || exit 4
"#
        )));
    }
}

#[test]
fn first_use_preserves_replaced_sibling_functions_and_detects_readonly_stubs() {
    let f = Fixture::new();
    f.plugin(
        "tools",
        "load='command'\ncommands=['tool', 'other']",
        "tool() { printf loaded; }; other() { printf overwritten; }\n",
    );
    success(f.prepare(&["tools"]));
    assert_eq!(
        success(f.bash(
            r#"
source "$BINIT_TEST_INIT"
other() { printf custom; }
tool && exit 2
[[ ${__binit_state[tools]} == blocked ]] || exit 3
other
"#
        )),
        "custom"
    );
    success(f.bash(r#"
FUNCNEST=20
source "$BINIT_TEST_INIT"
readonly -f tool
tool && exit 2
[[ ${__binit_state[tools]} == blocked && ${__binit_reason[tools]} == 'readonly command: tool' ]] || exit 3
"#));
}

#[test]
fn failed_loads_report_a_reason_and_explicit_loading_uses_the_same_state() {
    let f = Fixture::new();
    f.plugin(
        "failure",
        "load='command'\ncommands=['broken']",
        "return 17\n",
    );
    f.plugin(
        "missing",
        "load='command'\ncommands=['absent']\nrequires=['binit_missing_requirement']",
        ":\n",
    );
    f.plugin(
        "tools",
        "load='command'\ncommands=['tool']",
        "BINIT_LOADS=$(( ${BINIT_LOADS:-0} + 1 )); tool() { :; }\n",
    );
    success(f.prepare(&["failure", "missing", "tools"]));
    success(f.bash(r#"
source "$BINIT_TEST_INIT"
broken && exit 2
[[ ${__binit_state[failure]} == failed && ${__binit_reason[failure]} == 'entry returned 17' ]] || exit 3
declare -F broken >/dev/null && exit 4
absent && exit 5
[[ ${__binit_reason[missing]} == 'missing command: binit_missing_requirement' ]] || exit 6
binit load tools || exit 7
tool
binit load tools || exit 8
[[ $BINIT_LOADS == 1 ]] || exit 9
binit load '$(touch forbidden)' && exit 10
[[ $? == 2 ]] || exit 11
"#));
    assert!(!f.root().join("forbidden").exists());
}

#[test]
fn declaration_boundary_failures_and_duplicate_triggers_preserve_the_generation() {
    let f = Fixture::new();
    f.plugin("original", "load='startup'", ":\n");
    success(f.prepare(&["original"]));
    let original = f.generation();
    f.write("upstream/valid.sh", ":\n");
    symlink(
        f.root().join("upstream/valid.sh"),
        f.root().join("upstream/link.sh"),
    )
    .unwrap();
    symlink(
        f.root().join("upstream"),
        f.root().join("upstream/linked-directory"),
    )
    .unwrap();
    for (files, expected) in [
        ("[]", "between 1 and 125"),
        ("['../outside.sh']", "invalid declared file"),
        ("['./valid.sh']", "invalid declared file"),
        ("['link.sh']", "symlink in declared file"),
        ("['linked-directory/valid.sh']", "symlink in declared file"),
        ("['valid.sh','valid.sh']", "duplicate declared file"),
    ] {
        let declaration = f.write(
            "adapter.toml",
            &format!("schema=1\nid='adapter'\npath='upstream'\nfiles={files}\nload='startup'\n"),
        );
        failure(f.cli(&["add", declaration.to_str().unwrap()]), expected);
        assert_eq!(f.generation(), original);
    }
    f.plugin("one", "load='command'\ncommands=['tool']", ":\n");
    f.plugin("two", "load='command'\ncommands=['tool']", ":\n");
    failure(f.prepare(&["one", "two"]), "duplicate command trigger");
    assert_eq!(f.generation(), original);
}

#[test]
fn startup_conflicts_and_dependencies_preserve_first_use_ownership() {
    let f = Fixture::new();
    f.plugin("startup", "load='startup'", "tool() { printf original; }\n");
    f.plugin(
        "lazy",
        "load='command'\ncommands=['tool']",
        "BINIT_SHOULD_NOT_LOAD=1\n",
    );
    success(f.prepare(&["startup", "lazy"]));
    assert_eq!(
        success(f.bash(
            r#"
source "$BINIT_TEST_INIT"
[[ ${__binit_state[lazy]} == blocked && ! -v BINIT_SHOULD_NOT_LOAD ]] || exit 2
tool
"#
        )),
        "original"
    );
    f.plugin("base", "load='prompt'", "BINIT_SEQUENCE=base\n");
    f.plugin(
        "dependent",
        "load='command'\ncommands=['dependent']\ndepends=['base']",
        "BINIT_SEQUENCE+=:dependent; dependent() { printf %s \"$BINIT_SEQUENCE\"; }\n",
    );
    success(f.prepare(&["base", "dependent"]));
    assert_eq!(
        success(f.bash("source \"$BINIT_TEST_INIT\"; dependent")),
        "base:dependent"
    );
}

// Plugin lines in a startup file. Every command here runs with HOME,
// XDG_DATA_HOME and BINIT_STATE inside the fixture.
struct Home {
    f: Fixture,
}
fn sh_quote(text: &str) -> String {
    format!("'{}'", text.replace('\'', "'\\''"))
}
impl Home {
    fn new() -> Self {
        let home = Self { f: Fixture::new() };
        fs::create_dir_all(home.f.root().join("home")).unwrap();
        fs::create_dir_all(home.f.root().join("xdg")).unwrap();
        home
    }
    fn isolate<'a>(&self, command: &'a mut Command) -> &'a mut Command {
        command
            .env("HOME", self.f.root().join("home"))
            .env("XDG_DATA_HOME", self.f.root().join("xdg"))
            .env("BINIT_STATE", self.f.state())
            .env("TERM", "dumb")
            .env_remove("BASH_ENV")
            .env_remove("ENV")
            .current_dir(self.f.root())
    }
    fn binit(&self, args: &[&str]) -> Output {
        let mut command = Command::new(env!("CARGO_BIN_EXE_binit"));
        command.args(args);
        self.isolate(&mut command).output().unwrap()
    }
    fn src(&self, name: &str, file: &str, body: &str) -> String {
        self.f
            .write(&format!("src/{name}/{file}"), body)
            .parent()
            .unwrap()
            .to_str()
            .unwrap()
            .to_owned()
    }
    fn rc(&self, lines: &str) -> String {
        format!(
            "eval \"$({} init)\"\n{lines}",
            sh_quote(env!("CARGO_BIN_EXE_binit"))
        )
    }
    // Runs an interactive shell that reads input as it would at a prompt.
    // A pipe keeps forked children from moving the shell's input offset.
    fn shell(&self, lines: &str, input: &str) -> Output {
        use std::io::Write;
        use std::process::Stdio;
        let rc = self.f.write("rc", &self.rc(lines));
        let mut child = self
            .isolate(
                Command::new("bash")
                    .args(["--noprofile", "--rcfile"])
                    .arg(&rc)
                    .arg("-i")
                    .stdin(Stdio::piped())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped()),
            )
            .env("BINIT_TEST_RC", &rc)
            .spawn()
            .unwrap();
        child
            .stdin
            .take()
            .unwrap()
            .write_all(input.as_bytes())
            .unwrap();
        child.wait_with_output().unwrap()
    }
    fn declared(&self) -> PathBuf {
        self.f.state().join("declared")
    }
    fn current(&self) -> String {
        fs::read_to_string(self.f.state().join("current")).unwrap()
    }
}

fn stdout(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned()
}
fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}
const NOTICE: &str = "changed since the last sync; run binit sync";

#[test]
fn default_state_follows_binit_state_then_xdg_then_home() {
    let h = Home::new();
    let root = h.f.root();
    let run = |envs: &[(&str, Option<&str>)], args: &[&str]| {
        let mut command = Command::new(env!("CARGO_BIN_EXE_binit"));
        command
            .args(args)
            .env("HOME", root.join("home"))
            .env("XDG_DATA_HOME", root.join("xdg"))
            .env_remove("BINIT_STATE");
        for (name, value) in envs {
            match value {
                Some(value) => command.env(name, value),
                None => command.env_remove(name),
            };
        }
        command.output().unwrap()
    };
    let explicit = root.join("explicit");
    success(run(&[("BINIT_STATE", explicit.to_str())], &["init"]));
    assert!(explicit.join("current").is_file());
    success(run(&[], &["init"]));
    assert!(root.join("xdg/binit/current").is_file());
    success(run(&[("XDG_DATA_HOME", None)], &["init"]));
    assert!(root.join("home/.local/share/binit/current").is_file());
    // A relative XDG_DATA_HOME is ignored.
    let other = root.join("other");
    success(run(
        &[
            ("XDG_DATA_HOME", Some("relative")),
            ("HOME", other.to_str()),
        ],
        &["init"],
    ));
    assert!(other.join(".local/share/binit/current").is_file());
    assert!(!root.join("relative").exists());
    // --state wins over the environment.
    let flag = root.join("flag");
    success(run(
        &[("BINIT_STATE", explicit.to_str())],
        &["plugins", "--state", flag.to_str().unwrap()],
    ));
    failure(
        run(&[("BINIT_STATE", Some("relative"))], &["plugins"]),
        "BINIT_STATE must be an absolute path",
    );
    failure(
        run(
            &[("XDG_DATA_HOME", None), ("HOME", Some("relative"))],
            &["plugins"],
        ),
        "HOME must be an absolute path",
    );
    assert!(!Path::new("relative").exists());
}

#[test]
fn init_publishes_an_empty_generation_only_when_none_exists() {
    let h = Home::new();
    let first = success(h.binit(&["init"]));
    assert!(first.starts_with("source '"), "{first}");
    let generation = h.current();
    assert!(fs::metadata(h.f.state()).unwrap().permissions().mode() & 0o077 == 0);
    assert_eq!(success(h.binit(&["plugins"])), "No plugins installed\n");
    assert_eq!(success(h.binit(&["init"])), first);
    assert_eq!(h.current(), generation);
    assert!(!h.f.state().join("previous").exists());
    // The printed line sources into a working shell.
    let output = h.shell("", "declare -F binit >/dev/null && echo ready\n");
    assert!(stdout(&output).contains("ready"), "{}", stderr(&output));
    assert!(!stderr(&output).contains("binit:"), "{}", stderr(&output));
}

#[test]
fn lines_wait_for_sync_then_load_in_the_next_shell() {
    let h = Home::new();
    let hello = h.src("hello", "hello.plugin.bash", "BINIT_HELLO=loaded\n");
    let tool = h.src(
        "tool",
        "tool.sh",
        "tool() { printf 'tool-ran:%s\\n' \"$*\"; }\n",
    );
    h.src("tool", "comp.bash", ":\n");
    let thing = h.src("thing", "init.sh", "BINIT_THING=at-prompt\n");
    let lines = format!(
        "binit plugin {hello}\nbinit plugin {tool} files=tool.sh,comp.bash load=command commands=tool\nbinit plugin {thing} id=thing requires=bash load=prompt\nprintf 'rc:%s\\n' \"${{BINIT_THING-unset}}\"\n"
    );
    let probe = "printf 'hello:%s thing:%s\\n' \"${BINIT_HELLO-unset}\" \"${BINIT_THING-unset}\"\n";
    let before = h.shell(&lines, probe);
    assert!(
        stdout(&before).contains("hello:unset thing:unset"),
        "{}",
        stderr(&before)
    );
    assert!(
        stderr(&before).contains(&format!("binit: 3 plugin lines {NOTICE}")),
        "{}",
        stderr(&before)
    );
    let mut expected = Vec::new();
    for argv in [
        vec![hello.as_str()],
        vec![
            tool.as_str(),
            "files=tool.sh,comp.bash",
            "load=command",
            "commands=tool",
        ],
        vec![thing.as_str(), "id=thing", "requires=bash", "load=prompt"],
    ] {
        expected.extend_from_slice(format!("{}\0", argv.len()).as_bytes());
        for argument in argv {
            expected.extend_from_slice(format!("{argument}\0").as_bytes());
        }
    }
    assert_eq!(fs::read(h.declared()).unwrap(), expected);
    assert_eq!(
        fs::metadata(h.declared()).unwrap().permissions().mode() & 0o777,
        0o600
    );

    let synced = h.shell(&lines, "binit sync\n");
    let text = stdout(&synced);
    assert!(
        text.contains("Synced 3 plugin lines"),
        "{text}{}",
        stderr(&synced)
    );
    assert!(text.contains("Open a new shell to use it"), "{text}");
    // The file was just written from this shell, so it need not be rewritten.
    fs::remove_file(h.declared()).unwrap();

    let after = h.shell(
        &lines,
        &format!("{probe}tool a b\nprintf 'again:%s\\n' \"$BINIT_HELLO\"\nbinit profile\n"),
    );
    let text = stdout(&after);
    assert!(text.contains("rc:unset"), "{text}");
    assert!(
        text.contains("hello:loaded thing:at-prompt"),
        "{text}{}",
        stderr(&after)
    );
    assert!(text.contains("tool-ran:a b"), "{text}");
    for (id, state) in [("hello", "loaded"), ("thing", "loaded"), ("tool", "loaded")] {
        assert!(
            text.lines()
                .any(|l| l.split_whitespace().take(2).eq([id, state])),
            "{id}: {text}"
        );
    }
    assert!(!stderr(&after).contains(NOTICE), "{}", stderr(&after));
    assert!(!h.declared().exists());
}

#[test]
fn command_and_completion_lines_install_stubs_without_loading() {
    let h = Home::new();
    let tool = h.src(
        "tool",
        "init.sh",
        "BINIT_TOOL_LOADED=1\ntool() { echo real; }\n",
    );
    let comp = h.src(
        "comp",
        "comp.bash",
        "BINIT_COMP_LOADED=1\n_comp() { COMPREPLY=(done); }\ncomplete -F _comp comp\n",
    );
    let lines = format!(
        "binit plugin {tool} load=command commands=tool\nbinit plugin {comp} load=completion completions=comp\n"
    );
    success(h.shell(&lines, "binit sync\n"));
    let output = h.shell(
        &lines,
        "declare -F tool >/dev/null && echo stub\ncomplete -p comp\necho \"loaded:${BINIT_TOOL_LOADED-no}${BINIT_COMP_LOADED-no}\"\ntool\necho \"after:${BINIT_TOOL_LOADED-no}\"\n",
    );
    let text = stdout(&output);
    assert!(text.contains("stub"), "{text}{}", stderr(&output));
    assert!(text.contains("complete -F __binit_complete comp"), "{text}");
    assert!(text.contains("loaded:nono"), "{text}");
    assert!(text.contains("real") && text.contains("after:1"), "{text}");
}

#[test]
fn existing_definitions_still_win_over_plugin_lines() {
    let h = Home::new();
    let tool = h.src("tool", "init.sh", "BINIT_TOOL_LOADED=1\n");
    let lines = format!("binit plugin {tool} load=command commands=tool\n");
    success(h.shell(&lines, "binit sync\n"));
    let output = h.shell(
        &format!("tool() {{ echo mine; }}\n{lines}"),
        "tool\nbinit profile\n",
    );
    let text = stdout(&output);
    assert!(text.contains("mine"), "{text}");
    assert!(text.contains("command already defined: tool"), "{text}");
}

#[test]
fn removed_lines_become_undeclared_and_dependants_unavailable() {
    let h = Home::new();
    let base = h.src("base", "init.sh", "BINIT_BASE=1\n");
    let child = h.src("child", "init.sh", "BINIT_CHILD=1\n");
    let both = format!("binit plugin {base}\nbinit plugin {child} depends=base\n");
    success(h.shell(&both, "binit sync\n"));
    let output = h.shell(
        &format!("binit plugin {child} depends=base\n"),
        "echo \"vars:${BINIT_BASE-unset}${BINIT_CHILD-unset}\"\nbinit profile\n",
    );
    let text = stdout(&output);
    assert!(text.contains("vars:unsetunset"), "{text}");
    assert!(
        stderr(&output).contains(&format!("binit: 1 plugin line {NOTICE}")),
        "{}",
        stderr(&output)
    );
    let row = |id: &str| text.lines().find(|l| l.starts_with(id)).unwrap().to_owned();
    assert!(row("base").contains("undeclared"), "{text}");
    assert!(
        row("child").contains("unavailable")
            && row("child").contains("dependency unavailable: base"),
        "{text}"
    );
    // The shell's own list replaced the stored one.
    assert_eq!(
        fs::read(h.declared()).unwrap(),
        format!("2\0{child}\0depends=base\0").into_bytes()
    );
}

#[test]
fn a_dependency_must_be_declared_before_its_dependant() {
    let h = Home::new();
    let base = h.src("base", "init.sh", "BINIT_BASE=1\n");
    let child = h.src("child", "init.sh", "BINIT_CHILD=1\n");
    let lines = format!("binit plugin {base}\nbinit plugin {child} depends=base\n");
    success(h.shell(&lines, "binit sync\n"));
    let output = h.shell(
        &lines,
        "echo \"vars:${BINIT_BASE-unset}${BINIT_CHILD-unset}\"\n",
    );
    assert!(stdout(&output).contains("vars:11"), "{}", stderr(&output));
    let reversed = format!("binit plugin {child} depends=base\nbinit plugin {base}\n");
    let output = h.shell(&reversed, "binit profile\n");
    let text = stdout(&output);
    let child_row = text.lines().find(|l| l.starts_with("child")).unwrap();
    assert!(child_row.contains("unavailable"), "{text}");
}

#[test]
fn changed_keys_are_reported_and_do_not_activate_the_old_plugin() {
    let h = Home::new();
    let one = h.src("one", "init.sh", "BINIT_ONE=1\n");
    success(h.shell(&format!("binit plugin {one}\n"), "binit sync\n"));
    let output = h.shell(
        &format!("binit plugin {one} load=prompt\n"),
        "echo \"one:${BINIT_ONE-unset}\"\n",
    );
    assert!(stdout(&output).contains("one:unset"), "{}", stderr(&output));
    assert!(
        stderr(&output).contains(&format!("binit: 1 plugin line {NOTICE}")),
        "{}",
        stderr(&output)
    );
}

#[test]
fn default_files_are_chosen_in_order_and_failure_names_the_candidates() {
    let h = Home::new();
    let pick = h.src("pick", "init.sh", "BINIT_PICK=init.sh\n");
    h.src("pick", "pick.bash", "BINIT_PICK=pick.bash\n");
    h.src("pick", "init.bash", "BINIT_PICK=init.bash\n");
    let lines = format!("binit plugin {pick}\n");
    success(h.shell(&lines, "binit sync\n"));
    let output = h.shell(&lines, "echo \"pick:$BINIT_PICK\"\n");
    assert!(
        stdout(&output).contains("pick:pick.bash"),
        "{}",
        stderr(&output)
    );
    let previous = h.current();

    let empty = h.src("empty", "README", "nothing\n");
    let output = h.shell(&format!("{lines}binit plugin {empty}\n"), "binit sync\n");
    let message = stderr(&output);
    for candidate in [
        "empty.plugin.bash",
        "empty.bash",
        "init.bash",
        "empty.sh",
        "init.sh",
    ] {
        assert!(message.contains(candidate), "{message}");
    }
    assert_eq!(h.current(), previous);
}

#[test]
fn a_directory_with_plugin_toml_and_no_keys_uses_its_manifest() {
    let h = Home::new();
    let dir = h.src("manifest", "plugin.bash", "BINIT_MANIFEST=1\n");
    h.f.write(
        "src/manifest/plugin.toml",
        "schema=1\nid='named'\nentry='plugin.bash'\nload='startup'\n",
    );
    let lines = format!("binit plugin {dir}\n");
    let synced = h.shell(&lines, "binit sync\n");
    assert!(
        stdout(&synced).contains("Synced 1 plugin line"),
        "{}",
        stderr(&synced)
    );
    let output = h.shell(&lines, "echo \"m:$BINIT_MANIFEST\"\nbinit profile\n");
    assert!(stdout(&output).contains("m:1"), "{}", stderr(&output));
    assert!(stdout(&output).contains("named"));
}

#[test]
fn invalid_lines_are_rejected_and_the_shell_continues() {
    let h = Home::new();
    let dir = h.src("ok", "init.sh", ":\n");
    let lines = format!(
        "binit plugin relative/dir; echo \"s1:$?\"\nbinit plugin user/repo; echo \"s2:$?\"\nbinit plugin https://example.invalid/a/b; echo \"s3:$?\"\nbinit plugin {dir} bogus=1; echo \"s4:$?\"\nbinit plugin {dir} load=weird; echo \"s5:$?\"\nbinit plugin; echo \"s6:$?\"\nbinit plugin {dir} id=a id=b; echo \"s7:$?\"\nbinit plugin {dir} loose; echo \"s8:$?\"\nbinit plugin '~/x'; echo \"s9:$?\"\n"
    );
    let output = h.shell(&lines, "echo alive\n");
    let text = stdout(&output);
    for n in 1..=9 {
        assert!(
            text.contains(&format!("s{n}:2")),
            "{text}{}",
            stderr(&output)
        );
    }
    assert!(text.contains("alive"));
    let message = stderr(&output);
    assert!(
        message.matches("Git sources are not supported yet").count() == 3,
        "{message}"
    );
    assert!(message.contains("must be an absolute path"), "{message}");
    assert!(message.contains("unknown key: bogus"), "{message}");
    assert!(!message.contains("changed since"), "{message}");
    assert!(!h.declared().exists());
}

#[test]
fn a_failed_sync_leaves_the_current_generation_untouched() {
    let h = Home::new();
    let good = h.src("good", "init.sh", "BINIT_GOOD=1\n");
    let lines = format!("binit plugin {good}\n");
    success(h.shell(&lines, "binit sync\n"));
    let previous = h.current();
    let missing = h.f.root().join("src/missing");
    let output = h.shell(
        &format!("{lines}binit plugin {}\n", missing.display()),
        "binit sync\necho \"status:$?\"\n",
    );
    assert!(stdout(&output).contains("status:1"), "{}", stderr(&output));
    assert!(
        stderr(&output).contains("plugin line"),
        "{}",
        stderr(&output)
    );
    assert_eq!(h.current(), previous);
    success(h.binit(&["status"]));
    let output = h.shell(&lines, "echo \"good:$BINIT_GOOD\"\n");
    assert!(stdout(&output).contains("good:1"));
}

#[test]
fn line_ids_cannot_collide_with_catalog_plugins_and_catalog_plugins_stay_active() {
    let h = Home::new();
    h.f.plugin("shared", "load='startup'", "BINIT_CATALOG=1\n");
    let catalog = h.f.root().join("shared");
    success(h.binit(&["add", catalog.to_str().unwrap()]));
    let line = h.src("other", "init.sh", "BINIT_LINE=1\n");
    let lines = format!("binit plugin {line} id=shared\n");
    let output = h.shell(&lines, "binit sync\n");
    assert!(
        stderr(&output).contains("id shared is already in use"),
        "{}",
        stderr(&output)
    );
    // A catalog plugin loads without any line, and a line plugin beside it.
    let fine = format!("binit plugin {line} id=other\n");
    success(h.shell(&fine, "binit sync\n"));
    let output = h.shell("", "echo \"vars:${BINIT_CATALOG-no}${BINIT_LINE-no}\"\n");
    assert!(stdout(&output).contains("vars:1no"), "{}", stderr(&output));
    // Plugin lines own their entries; catalog commands refuse to edit them.
    failure(
        h.binit(&["remove", "other"]),
        "managed by a binit plugin line",
    );
    failure(h.binit(&["disable", "other"]), "binit sync");
    success(h.binit(&["disable", "shared"]));
    // Syncing keeps the catalog entry and drops the line.
    success(h.shell("", "binit sync\n"));
    assert!(success(h.binit(&["plugins"])).contains("shared"));
    assert!(!success(h.binit(&["plugins"])).contains("other"));
}

#[test]
fn paths_with_spaces_quotes_brackets_and_newlines_survive_sync() {
    let h = Home::new();
    let dir = h.src("sp ace]'\nq", "init.sh", "BINIT_WEIRD=1\n");
    let lines = format!("binit plugin {} id=weird\n", sh_quote(&dir));
    let output = h.shell(&lines, "binit sync\n");
    assert!(
        stdout(&output).contains("Synced 1 plugin line"),
        "{}",
        stderr(&output)
    );
    assert_eq!(
        fs::read(h.declared()).unwrap(),
        format!("2\0{dir}\0id=weird\0").into_bytes()
    );
    let output = h.shell(&lines, "echo \"w:$BINIT_WEIRD\"\n");
    assert!(stdout(&output).contains("w:1"), "{}", stderr(&output));
    assert!(!stderr(&output).contains(NOTICE), "{}", stderr(&output));
}

#[test]
fn sourcing_the_startup_file_again_changes_nothing() {
    let h = Home::new();
    let counter = h.src(
        "counter",
        "init.sh",
        "BINIT_COUNT=$((${BINIT_COUNT-0} + 1))\n",
    );
    let lines = format!("binit plugin {counter}\n");
    success(h.shell(&lines, "binit sync\n"));
    let output = h.shell(
        &lines,
        "source \"$BINIT_TEST_RC\"\nsource \"$BINIT_TEST_RC\"\necho \"count:$BINIT_COUNT lines:${#__binit_line_counts[@]} prompts:${#PROMPT_COMMAND[@]}\"\n",
    );
    let text = stdout(&output);
    assert!(
        text.contains("count:1 lines:1 prompts:1"),
        "{text}{}",
        stderr(&output)
    );
    assert!(!stderr(&output).contains(NOTICE), "{}", stderr(&output));
}

#[test]
fn a_prefixed_prompt_command_still_runs_the_dispatcher() {
    let h = Home::new();
    let prompt = h.src("later", "init.sh", "BINIT_LATER=ran\n");
    let lines = format!(
        "PROMPT_COMMAND=\"history -a; $PROMPT_COMMAND\"\nbinit plugin {prompt} id=later load=prompt\n"
    );
    success(h.shell(&lines, "binit sync\n"));
    let output = h.shell(&lines, "echo \"later:${BINIT_LATER-unset}\"\n");
    assert!(
        stdout(&output).contains("later:ran"),
        "{}{}",
        stdout(&output),
        stderr(&output)
    );
}

#[test]
fn a_later_ps1_assignment_keeps_the_prompt_prefix() {
    let h = Home::new();
    let output = h.shell(
        "PS1='mine> '\n",
        "printf 'ps1:%s\\n' \"$PS1\"\nfalse\nprintf 'status:%s\\n' \"$BINIT_LAST_STATUS\"\n",
    );
    let text = stdout(&output);
    assert!(
        text.contains("ps1:${BINIT_PROMPT_TEXT}mine> "),
        "{text}{}",
        stderr(&output)
    );
    assert!(text.contains("status:1"), "{text}");
}

#[test]
fn sync_refuses_unsafe_or_malformed_declared_files() {
    let h = Home::new();
    success(h.binit(&["init"]));
    let before = h.current();
    failure(h.binit(&["sync"]), "no declared plugin lines");
    let write = |bytes: &[u8], mode: u32| {
        let _ = fs::remove_file(h.declared());
        fs::write(h.declared(), bytes).unwrap();
        fs::set_permissions(h.declared(), fs::Permissions::from_mode(mode)).unwrap();
    };
    write(b"1\0/x\0", 0o644);
    failure(h.binit(&["sync"]), "private regular file");
    write(&vec![b'a'; 256 * 1024 + 1], 0o600);
    failure(h.binit(&["sync"]), "256 KiB");
    write(&b"1\0/x\0".repeat(65), 0o600);
    failure(h.binit(&["sync"]), "64 lines");
    write(b"1\0\xff\0", 0o600);
    failure(h.binit(&["sync"]), "UTF-8");
    write(b"2\0/x\0", 0o600);
    failure(h.binit(&["sync"]), "truncated");
    fs::remove_file(h.declared()).unwrap();
    let target = h.f.write("elsewhere", "0");
    fs::set_permissions(&target, fs::Permissions::from_mode(0o600)).unwrap();
    symlink(&target, h.declared()).unwrap();
    assert!(!h.binit(&["sync"]).status.success());
    assert_eq!(h.current(), before);
}
