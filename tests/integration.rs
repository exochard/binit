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
