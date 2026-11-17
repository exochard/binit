use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use tempfile::TempDir;

fn test_bash() -> std::ffi::OsString {
    std::env::var_os("BINIT_BASH").unwrap_or_else(|| "bash".into())
}

#[test]
fn shell_uses_the_selected_interpreter_in_normal_and_recovery_modes() {
    use std::io::Write;
    use std::process::Stdio;
    let f = Fixture::new();
    f.demo();
    let wrapper = f.write(
        "selected bash",
        "#!/bin/sh\nexport BINIT_SELECTED=yes\nexec \"$BINIT_TEST_REAL_BASH\" \"$@\"\n",
    );
    fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o700)).unwrap();
    for safe in ["0", "1"] {
        let mut child = Command::new(env!("CARGO_BIN_EXE_binit"))
            .args(["shell", "--state"])
            .arg(f.state())
            .env("BINIT_BASH", &wrapper)
            .env("BINIT_TEST_REAL_BASH", test_bash())
            .env("BINIT_SAFE", safe)
            .env("PS1", "")
            .env("HISTFILE", "/dev/null")
            .env_remove("BASH_ENV")
            .env_remove("ENV")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        child.stdin.take().unwrap().write_all(
            b"printf 'selected=%s active=%s\\n' \"$BINIT_SELECTED\" \"${__binit_active-0}\"\nexit 23\n"
        ).unwrap();
        let output = child.wait_with_output().unwrap();
        assert_eq!(output.status.code(), Some(23));
        let active = if safe == "1" { "0" } else { "1" };
        assert!(
            stdout(&output).contains(&format!("selected=yes active={active}")),
            "{}",
            stderr(&output)
        );
    }
    for invalid in ["", "bash", "--norc"] {
        let output = Command::new(env!("CARGO_BIN_EXE_binit"))
            .args(["shell", "--state"])
            .arg(f.state())
            .env("BINIT_BASH", invalid)
            .output()
            .unwrap();
        failure(output, "BINIT_BASH must be an absolute executable path");
    }
    let output = Command::new(env!("CARGO_BIN_EXE_binit"))
        .args(["shell", "--state"])
        .arg(f.state())
        .env("BINIT_BASH", "/no-such-binit-shell")
        .output()
        .unwrap();
    assert!(
        !output.status.success(),
        "missing interpreter must not fall back"
    );
}

#[test]
fn initialization_preserves_user_shell_configuration_against_clean_bash() {
    let f = Fixture::new();
    f.plugin("noop", "load='startup'", ":\n");
    success(f.prepare(&["noop"]));
    let observations = r#"
printf 'cwd=%s args=' "$PWD"; printf '<%s>' "$@"; printf '\n'
declare -p IFS SAMPLE sample_array
export -p | grep ' SAMPLE='
alias user_alias
declare -f user_function
trap -p DEBUG RETURN USR1
set +o
shopt -p extglob nullglob failglob globstar
"#;
    for (setup, expected_monitor) in [
        (
            "IFS=$' :\t'; export SAMPLE='two words'; sample_array=('one' 'two words')",
            None,
        ),
        (
            "IFS=; export SAMPLE=''; sample_array=([3]='sparse'); set -o pipefail; shopt -s nullglob globstar",
            None,
        ),
        (
            "export SAMPLE=present; sample_array=(); trap ':' DEBUG RETURN USR1; shopt -s extglob",
            None,
        ),
        (
            "export SAMPLE=present; sample_array=(); set +m",
            Some("set +o monitor"),
        ),
        (
            "export SAMPLE=present; sample_array=(); set -m",
            Some("set -o monitor"),
        ),
    ] {
        let prefix = format!(
            "{setup}\nalias user_alias='printf alias'; user_function() {{ printf '%s' user; }}\nset -- 'two words' '' '*.literal'\n"
        );
        let clean = f.bash(&format!("{prefix}{observations}\n:"));
        let configured = f.bash(&format!(
            "{prefix}source \"$BINIT_TEST_INIT\"\n{observations}\n:"
        ));
        assert!(
            clean.status.success() && configured.status.success(),
            "{}",
            stderr(&configured)
        );
        if let Some(expected) = expected_monitor {
            assert!(stdout(&clean).lines().any(|line| line == expected));
        }
        assert_eq!(stdout(&configured), stdout(&clean), "setup: {setup}");
    }
}

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
        let mut command = Command::new(test_bash());
        command
            .args(["--noprofile", "--norc", "-ic", script])
            .env("BINIT_TEST_INIT", self.generation().join("init.bash"))
            .env("BINIT_TEST_ROOT", self.root())
            .env("BINIT_TEST_BINARY", env!("CARGO_BIN_EXE_binit"))
            .env("TERM", "dumb")
            .env_remove("BASH_ENV")
            .env_remove("ENV")
            .current_dir(self.root());
        // Capture-only shells must not compete for the test runner's terminal.
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        command.output().unwrap()
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
fn init_stamp_skips_rehashing_only_while_metadata_is_unchanged() {
    let f = Fixture::new();
    f.plugin("sample", "load='startup'", "BINIT_SAMPLE=original\n");
    success(f.prepare(&["sample"]));
    let generation = f.generation();
    let id = generation.file_name().unwrap().to_str().unwrap().to_owned();
    let stamp = generation.with_file_name(format!("{id}.verified"));
    let plugin = generation.join("plugins/sample/plugin.bash");
    std::thread::sleep(std::time::Duration::from_millis(2200));
    // A locked file touched just before init still verifies, but is too recent to stamp.
    fs::File::options()
        .write(true)
        .open(&plugin)
        .unwrap()
        .set_times(fs::FileTimes::new().set_modified(std::time::SystemTime::now()))
        .unwrap();
    success(f.cli(&["init"]));
    assert!(!stamp.exists());
    std::thread::sleep(std::time::Duration::from_millis(2200));
    success(f.cli(&["init"]));
    assert!(stamp.exists());
    success(f.cli(&["init"]));
    // Same size and the original mtime: only ctime records the write, and init still rejects it.
    let modified = fs::metadata(&plugin).unwrap().modified().unwrap();
    fs::write(&plugin, "BINIT_SAMPLE=tampered\n").unwrap();
    fs::File::options()
        .write(true)
        .open(&plugin)
        .unwrap()
        .set_times(fs::FileTimes::new().set_modified(modified))
        .unwrap();
    failure(f.cli(&["init"]), "content changed");
    // The failed check removed the stamp, so the next start verifies in full again.
    assert!(!stamp.exists());
    failure(f.cli(&["init"]), "content changed");
}

#[test]
fn init_stamp_must_name_the_lock_and_the_generation_directory() {
    let f = Fixture::new();
    f.plugin("sample", "load='startup'", "BINIT_SAMPLE=original\n");
    success(f.prepare(&["sample"]));
    let generation = f.generation();
    let id = generation.file_name().unwrap().to_str().unwrap().to_owned();
    let stamp = generation.with_file_name(format!("{id}.verified"));
    std::thread::sleep(std::time::Duration::from_millis(2200));
    success(f.cli(&["init"]));
    // Without the lock and directory lines, the remaining lines all match, yet the stamp is refused.
    let text = fs::read_to_string(&stamp).unwrap();
    let kept: Vec<_> = text
        .lines()
        .filter(|line| {
            !line.ends_with(" lock.toml")
                && !line.ends_with(" .")
                && !line.ends_with(" plugins/sample/plugin.bash")
        })
        .collect();
    fs::write(&stamp, kept.join("\n") + "\n").unwrap();
    let plugin = generation.join("plugins/sample/plugin.bash");
    fs::write(&plugin, "BINIT_SAMPLE=tampered\n").unwrap();
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
    let output = Command::new(test_bash())
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
        "runtime-more.bash",
        "runtime-load.bash",
        "runtime-unload.bash",
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
        ("wait", "", "__binit_wait_dispatch"),
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

// First use must not depend on which serializer validates command stubs: the calling-shell
// serializer is only eligible without traps or function tracing, and a shell without the
// native module keeps the subshell path. Every context and module mode must agree. Each pair
// is applied before initialization and before first use, so stubs captured by one serializer
// are also validated by the other.
const FIRST_USE_CONTEXTS: [(&str, &str); 8] = [
    (":", ":"),
    (":", "trap ':' DEBUG"),
    (":", "set -T; trap ':' DEBUG"),
    (":", "trap ':' RETURN"),
    (":", "set -E; trap ':' ERR"),
    (":", "trap ':' INT"),
    (":", "trap 'trap - DEBUG' DEBUG"),
    ("trap ':' DEBUG", "trap - DEBUG"),
];

#[test]
fn first_use_effects_match_across_trap_contexts_and_native_availability() {
    let f = Fixture::new();
    fs::create_dir_all(f.root().join("target")).unwrap();
    let body = r#"BINIT_LOADS=$(( ${BINIT_LOADS:-0} + 1 ))
export BINIT_PLUGIN_EXPORT=loaded
cd -- "$BINIT_TEST_ROOT/target" || return 9
{ALIAS}
tool() { BINIT_ARGS=$(printf '<%s>' "$@"); return 37; }
other() {
    cat <<'EOF'
heredoc $literal
EOF
}
"#;
    for (prelude, plugin_alias, expected_alias) in [
        ("", "", "alias-absent alias-removed"),
        (
            "",
            "alias plugin_alias='printf first'",
            "alias-present alias-removed",
        ),
        (
            "alias foreign_alias='printf foreign'",
            "",
            "alias-absent alias-removed",
        ),
        (
            "alias foreign_alias='printf foreign'",
            "alias plugin_alias='printf owned'",
            "alias-present alias-removed",
        ),
    ] {
        f.plugin(
            "tools",
            "load='command'\ncommands=['tool', 'other']",
            &body.replace(
                "{ALIAS}",
                if plugin_alias.is_empty() {
                    ":"
                } else {
                    plugin_alias
                },
            ),
        );
        success(f.prepare(&["tools"]));
        for menu in ["1", "0"] {
            for (setup, context) in FIRST_USE_CONTEXTS {
                let script = r#"
BINIT_COMPLETION_MENU={MENU}
{PRELUDE}
foreign_fn() { :; }
{SETUP}
source "$BINIT_TEST_INIT"
[[ $(type -t binit_complete) == builtin ]] && native=yes || native=no
{CONTEXT}
tool 'one two' '' λ; status=$?
set +T +E; trap - DEBUG RETURN ERR INT
printf 'native=%s status=%s args=%s pwd=%s export=%s child=%s loads=%s state=%s\n' "$native" "$status" "$BINIT_ARGS" "${PWD##*/}" "$BINIT_PLUGIN_EXPORT" "$("$BASH" --noprofile --norc -c 'printf %s "$BINIT_PLUGIN_EXPORT"')" "$BINIT_LOADS" "${__binit_state[tools]}"
alias plugin_alias >/dev/null 2>&1 && printf 'alias-present ' || printf 'alias-absent '
other
tool() { : user change; }
binit unload tools >/dev/null 2>&1 && echo refused=no || echo refused=yes
[[ $(declare -f tool) == *'user change'* ]] && echo changed-kept
tool() { BINIT_ARGS=$(printf '<%s>' "$@"); return 37; }
binit unload tools; echo unload=$?
alias plugin_alias >/dev/null 2>&1 && echo alias-still || echo alias-removed
declare -F tool other >/dev/null || echo plugin-functions-removed
declare -F foreign_fn >/dev/null && echo foreign-function-kept
[[ -z "{PRELUDE}" ]] || { alias foreign_alias >/dev/null && echo foreign-alias-kept; }
"#
                .replace("{MENU}", menu)
                .replace("{PRELUDE}", prelude)
                .replace("{SETUP}", setup)
                .replace("{CONTEXT}", context);
                let text = success(f.bash(&script));
                let summary = text.split_whitespace().collect::<Vec<_>>().join(" ");
                let aliases: Vec<&str> = expected_alias.split(' ').collect();
                let expected = format!(
                    "native={} status=37 args=<one two><><λ> pwd=target export=loaded child=loaded loads=1 state=loaded {} heredoc $literal refused=yes changed-kept unload=0 {} plugin-functions-removed foreign-function-kept{}",
                    if menu == "1" && f.generation().join("completion.so").exists() {
                        "yes"
                    } else {
                        "no"
                    },
                    aliases[0],
                    aliases[1],
                    if prelude.is_empty() {
                        ""
                    } else {
                        " foreign-alias-kept"
                    }
                );
                assert_eq!(
                    summary, expected,
                    "menu={menu} setup={setup} context={context} alias={plugin_alias:?} prelude={prelude:?}"
                );
            }
        }
    }
}

#[test]
fn modified_stubs_and_conflicting_aliases_block_first_use_in_every_context() {
    let f = Fixture::new();
    f.plugin(
        "tools",
        "load='command'\ncommands=['tool', 'other']",
        "BINIT_LOADED=1\ntool() { printf loaded; }\nother() { printf overwritten; }\n",
    );
    success(f.prepare(&["tools"]));
    for menu in ["1", "0"] {
        for (setup, context) in FIRST_USE_CONTEXTS {
            for (change, observe, expected) in [
                ("other() { printf custom; }", "other", "custom"),
                (
                    "other() { __binit_invoke 'other' \"$@\" ; : edited; }",
                    "[[ $(declare -f other) == *edited* ]] && printf edited",
                    "edited",
                ),
                (
                    "other() { cat <<'EOF' >/dev/null 2>&1\n__binit_invoke 'other' \"$@\"\nEOF\n}",
                    "[[ $(declare -f other) == *EOF* ]] && printf heredoc",
                    "heredoc",
                ),
                ("alias other='printf aliased'", "other", "aliased"),
            ] {
                let script = r#"
BINIT_COMPLETION_MENU={MENU}
{SETUP}
source "$BINIT_TEST_INIT"
{CHANGE}
{CONTEXT}
tool; status=$?
set +T +E; trap - DEBUG RETURN ERR INT
printf 'status=%s loaded=%s state=%s reason=%s|' "$status" "${BINIT_LOADED-no}" "${__binit_state[tools]}" "${__binit_reason[tools]}"
{OBSERVE}
"#
                .replace("{MENU}", menu)
                .replace("{SETUP}", setup)
                .replace("{CHANGE}", change)
                .replace("{CONTEXT}", context)
                .replace("{OBSERVE}", observe);
                assert_eq!(
                    success(f.bash(&script)),
                    format!(
                        "status=1 loaded=no state=blocked reason=command already defined: other|{expected}"
                    ),
                    "menu={menu} setup={setup} context={context} change={change}"
                );
            }
            // Re-evaluating the stub's own text must still be recognized as the stub, whichever
            // serializer captured it at initialization and whichever validates it at first use.
            let script = r#"
BINIT_COMPLETION_MENU={MENU}
{SETUP}
source "$BINIT_TEST_INIT"
eval "$(declare -f other)"
{CONTEXT}
tool; status=$?
set +T +E; trap - DEBUG RETURN ERR INT
printf '|status=%s loaded=%s state=%s' "$status" "${BINIT_LOADED-no}" "${__binit_state[tools]}"
"#
            .replace("{MENU}", menu)
            .replace("{SETUP}", setup)
            .replace("{CONTEXT}", context);
            assert_eq!(
                success(f.bash(&script)),
                "loaded|status=0 loaded=1 state=loaded",
                "menu={menu} setup={setup} context={context} re-evaluated stub"
            );
        }
    }
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
        self.shell_env(lines, input, &[])
    }
    fn shell_env(&self, lines: &str, input: &str, envs: &[(&str, &str)]) -> Output {
        use std::io::Write;
        use std::process::Stdio;
        let rc = self.f.write("rc", &self.rc(lines));
        let mut child = self
            .isolate(
                Command::new(test_bash())
                    .args(["--noprofile", "--rcfile"])
                    .arg(&rc)
                    .arg("-i")
                    .stdin(Stdio::piped())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped()),
            )
            .env("BINIT_TEST_RC", &rc)
            .envs(envs.iter().copied())
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

fn fixture_digest(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    format!("{:x}", Sha256::digest(bytes))
}

fn fake_download(h: &Home, bytes: &[u8]) -> (String, String, String) {
    let payload = h.f.write("download", "");
    fs::write(&payload, bytes).unwrap();
    let curl = h.f.write("transport/curl", "#!/bin/sh\nprintf x >> \"$BINIT_DOWNLOAD_CALLS\"\nexec /bin/cat \"$BINIT_DOWNLOAD_FIXTURE\"\n");
    fs::set_permissions(&curl, fs::Permissions::from_mode(0o700)).unwrap();
    (
        format!(
            "{}:{}",
            curl.parent().unwrap().display(),
            std::env::var("PATH").unwrap()
        ),
        payload.to_str().unwrap().to_owned(),
        h.f.root()
            .join("download-calls")
            .to_str()
            .unwrap()
            .to_owned(),
    )
}

#[test]
fn snippets_pin_bytes_sync_offline_update_explicitly_and_rollback() {
    let h = Home::new();
    let body = b"SNIPPET_VALUE=one\n";
    let (path, payload, calls) = fake_download(&h, body);
    let envs = [
        ("PATH", path.as_str()),
        ("BINIT_DOWNLOAD_FIXTURE", payload.as_str()),
        ("BINIT_DOWNLOAD_CALLS", calls.as_str()),
    ];
    let line = format!(
        "binit plugin snippet:https://example.com/init.bash id=snippet sha256={}\n",
        fixture_digest(body)
    );
    success(h.shell_env(&line, "binit sync\n", &envs));
    assert!(
        stdout(&h.shell_env(&line, "echo value:$SNIPPET_VALUE\n", &envs)).contains("value:one")
    );
    let first = h.current();
    fs::write(&payload, b"SNIPPET_VALUE=two\n").unwrap();
    success(h.shell_env(&line, "binit sync\nbinit update --check\n", &envs));
    assert_eq!(h.current(), first);
    assert_eq!(fs::read_to_string(&calls).unwrap(), "x");
    let next = format!(
        "binit plugin snippet:https://example.com/init.bash id=snippet sha256={}\n",
        fixture_digest(b"SNIPPET_VALUE=two\n")
    );
    success(h.shell_env(&next, "binit sync\n", &envs));
    assert!(
        stdout(&h.shell_env(&next, "echo value:$SNIPPET_VALUE\n", &envs)).contains("value:two")
    );
    success(h.binit(&["rollback"]));
    assert_eq!(h.current(), first);
    assert!(
        stdout(&h.shell_env(&line, "echo value:$SNIPPET_VALUE\n", &envs)).contains("value:one")
    );
}

#[test]
fn releases_expose_large_programs_and_keep_apply_and_removal_transactional() {
    let h = Home::new();
    let mut bytes = b"#!/bin/sh\nprintf 'asset:%s\\n' \"$1\"\nexit 23\n#".to_vec();
    bytes.resize(3 * 1024 * 1024, b'x');
    let (path, payload, calls) = fake_download(&h, &bytes);
    let envs = [
        ("PATH", path.as_str()),
        ("BINIT_DOWNLOAD_FIXTURE", payload.as_str()),
        ("BINIT_DOWNLOAD_CALLS", calls.as_str()),
    ];
    let line = format!(
        "binit plugin release:example/tool id=tool ver=v1 asset=tool.bin sha256={} bin=tool.bin:downloaded-tool\n",
        fixture_digest(&bytes)
    );
    success(h.shell_env(&line, "binit sync\n", &envs));
    let output = h.shell_env(
        &line,
        "downloaded-tool 'two words'; echo result:$?\n",
        &envs,
    );
    assert!(
        stdout(&output).contains("asset:two words\nresult:23"),
        "{}",
        stderr(&output)
    );
    success(h.binit(&["apply"]));
    success(h.shell_env(&line, "binit sync\n", &envs));
    assert_eq!(fs::read_to_string(&calls).unwrap(), "x");
    let info = success(h.binit(&["inspect", "tool"]));
    assert!(info.contains(&fixture_digest(&bytes)) && info.contains("Format: raw"));
    success(h.shell_env("", "binit sync\n", &envs));
    assert!(
        stdout(&h.shell_env("", "command -v downloaded-tool || echo absent\n", &envs))
            .contains("absent")
    );
}

#[test]
fn bad_remote_content_and_cache_tampering_preserve_the_generation() {
    let h = Home::new();
    let body = b"VALUE=ok\n";
    let (path, payload, calls) = fake_download(&h, body);
    let envs = [
        ("PATH", path.as_str()),
        ("BINIT_DOWNLOAD_FIXTURE", payload.as_str()),
        ("BINIT_DOWNLOAD_CALLS", calls.as_str()),
    ];
    let line = format!(
        "binit plugin snippet:https://example.com/init id=one sha256={}\n",
        fixture_digest(body)
    );
    success(h.shell_env(&line, "binit sync\n", &envs));
    let first = h.current();
    let bad = format!(
        "binit plugin snippet:https://example.com/init id=two sha256={}\n",
        "0".repeat(64)
    );
    let output = h.shell_env(&bad, "binit sync\n", &envs);
    assert!(stderr(&output).contains("SHA-256 mismatch"));
    assert_eq!(h.current(), first);
    fs::write(
        h.f.state().join("assets").join(fixture_digest(body)),
        b"tampered",
    )
    .unwrap();
    let changed = line.replace("id=one", "id=two");
    let output = h.shell_env(&changed, "binit sync\n", &envs);
    assert!(stderr(&output).contains("cached remote source checksum mismatch"));
    assert_eq!(h.current(), first);
}

#[test]
fn clone_and_pull_hooks_prepare_isolated_local_sources_once() {
    let h = Home::new();
    let source = h.src("generated", "README", "source");
    let builder = Path::new(&source).join("build");
    fs::write(
        &builder,
        "#!/bin/sh\nprintf 'GENERATED=clone\\n' > init.bash\n",
    )
    .unwrap();
    fs::set_permissions(&builder, fs::Permissions::from_mode(0o700)).unwrap();
    let line = format!(
        "binit plugin {} id=generated files=init.bash atclone=./build atpull={}\n",
        sh_quote(&source),
        sh_quote("printf 'GENERATED=pull\\n' > init.bash")
    );
    success(h.shell(&line, "binit sync\n"));
    assert!(!Path::new(&source).join("init.bash").exists());
    assert!(stdout(&h.shell(&line, "echo generated:$GENERATED\n")).contains("generated:clone"));
    success(h.shell(&line, "binit sync\n"));
    success(h.binit(&["update", "generated"]));
    assert!(stdout(&h.shell(&line, "echo generated:$GENERATED\n")).contains("generated:pull"));
    assert!(!Path::new(&source).join("init.bash").exists());
}

#[test]
fn failing_hooks_leave_the_current_generation_and_source_untouched() {
    let h = Home::new();
    let source = h.src("plug", "init.bash", "VALUE=original\n");
    let line = format!("binit plugin {} id=plug\n", sh_quote(&source));
    success(h.shell(&line, "binit sync\n"));
    let first = h.current();
    let failing = format!(
        "binit plugin {} id=other atclone={}\n",
        sh_quote(&source),
        sh_quote("printf broken > init.bash; exit 9")
    );
    let output = h.shell(&failing, "binit sync\n");
    assert!(stderr(&output).contains("atclone"), "{}", stderr(&output));
    assert_eq!(h.current(), first);
    assert_eq!(
        fs::read_to_string(Path::new(&source).join("init.bash")).unwrap(),
        "VALUE=original\n"
    );
}

#[test]
fn release_archives_reject_duplicate_scripts_and_load_selected_content_once() {
    let h = Home::new();
    let mut archive = tar::Builder::new(Vec::new());
    for (name, body) in [
        ("bundle/init.bash", "COUNT=$(( ${COUNT:-0} + 1 ))\n"),
        ("bundle/tool", "#!/bin/sh\nprintf archive-ok\\n\n"),
    ] {
        let mut header = tar::Header::new_gnu();
        header.set_size(body.len() as u64);
        header.set_mode(0o755);
        header.set_cksum();
        archive
            .append_data(&mut header, name, body.as_bytes())
            .unwrap();
    }
    let tar = archive.into_inner().unwrap();
    let mut gzip = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
    std::io::Write::write_all(&mut gzip, &tar).unwrap();
    let bytes = gzip.finish().unwrap();
    let (path, payload, calls) = fake_download(&h, &bytes);
    let envs = [
        ("PATH", path.as_str()),
        ("BINIT_DOWNLOAD_FIXTURE", payload.as_str()),
        ("BINIT_DOWNLOAD_CALLS", calls.as_str()),
    ];
    let base = format!(
        "binit plugin release:owner/repo id=archive ver=v1 asset=bundle.tar.gz sha256={} bin=bundle/tool:archived-tool",
        fixture_digest(&bytes)
    );
    let duplicate = format!("{base} files=bundle/init.bash,bundle/init.bash\n");
    let failed = h.shell_env(&duplicate, "binit sync\n", &envs);
    assert!(
        stderr(&failed).contains("duplicate declared file"),
        "{}",
        stderr(&failed)
    );
    let line = format!("{base} files=bundle/init.bash\n");
    success(h.shell_env(&line, "binit sync\n", &envs));
    let output = h.shell_env(&line, "echo count:$COUNT\narchived-tool\n", &envs);
    assert!(stdout(&output).contains("count:1"), "{}", stderr(&output));
    assert!(stdout(&output).contains("archive-ok"));
    assert_eq!(fs::read_to_string(calls).unwrap(), "x");
}

#[test]
fn git_hooks_run_only_on_acquisition_and_accepted_updates_with_derived_ids() {
    let h = Home::new();
    let repo = Repo::new(&h, "hooked", "init.bash", "REVISION=one\n");
    let line = format!(
        "binit plugin {} files=init.bash atclone={} atpull={}\n",
        repo.url(),
        sh_quote("printf 'HOOK_ID=%s\\nHOOK_EVENT=clone\\n' \"$BINIT_PLUGIN_ID\" >> init.bash"),
        sh_quote("printf 'HOOK_EVENT=pull\\n' >> init.bash")
    );
    success(h.shell(&line, "binit sync\n"));
    let first = h.current();
    assert!(
        stdout(&h.shell(&line, "echo hook:$HOOK_ID:$HOOK_EVENT:$REVISION\n"))
            .contains("hook:hooked:clone:one")
    );
    repo.commit("init.bash", "REVISION=two\n", "second");
    success(h.binit(&["update", "--check"]));
    assert_eq!(h.current(), first);
    success(h.binit(&["update", "hooked"]));
    assert!(stdout(&h.shell(&line, "echo hook:$HOOK_EVENT:$REVISION\n")).contains("hook:pull:two"));
    success(h.binit(&["rollback"]));
    assert!(
        stdout(&h.shell(&line, "echo hook:$HOOK_EVENT:$REVISION\n")).contains("hook:clone:one")
    );
}

#[test]
fn interrupting_a_real_sync_hook_kills_background_children_and_preserves_state() {
    use std::process::Stdio;
    use std::time::{Duration, Instant};
    let h = Home::new();
    let source = h.src("original", "init.bash", "VALUE=original\n");
    success(h.shell(
        &format!("binit plugin {}\n", sh_quote(&source)),
        "binit sync\n",
    ));
    let before = h.current();
    let ready = h.f.root().join("ready");
    let escaped = h.f.root().join("must-not-be-written");
    let script = format!(
        "touch {}; (sleep 0.5; touch {}) & wait",
        sh_quote(ready.to_str().unwrap()),
        sh_quote(escaped.to_str().unwrap())
    );
    let atclone = format!("atclone={script}");
    declare_lines(&h, &[&[&source, "id=interrupted", &atclone]]);
    let mut child = Command::new(env!("CARGO_BIN_EXE_binit"))
        .arg("sync")
        .arg("--state")
        .arg(h.f.state())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let started = Instant::now();
    while !ready.exists() && started.elapsed() < Duration::from_secs(3) {
        assert!(
            child.try_wait().unwrap().is_none(),
            "hook exited before readiness"
        );
        std::thread::sleep(Duration::from_millis(5));
    }
    if !ready.exists() {
        let _ = child.kill();
        let _ = child.wait();
        panic!("hook readiness timeout");
    }
    unsafe {
        assert_eq!(libc::kill(child.id() as libc::pid_t, libc::SIGINT), 0);
    }
    let output = child.wait_with_output().unwrap();
    assert!(!output.status.success());
    assert_eq!(h.current(), before);
    std::thread::sleep(Duration::from_millis(650));
    assert!(
        !escaped.exists(),
        "ordinary hook child survived cancellation"
    );
}

#[test]
fn hook_git_commands_cannot_discover_an_unrelated_or_inherited_repository() {
    let h = Home::new();
    let source = h.src("isolated", "init.bash", "SAFE=before\n");
    let script = "if git rev-parse --show-toplevel >/dev/null 2>&1; then exit 19; fi; printf 'SAFE=isolated\\n' > init.bash";
    let line = format!(
        "binit plugin {} atclone={}\n",
        sh_quote(&source),
        sh_quote(script)
    );
    let git_dir = Path::new(env!("CARGO_MANIFEST_DIR")).join(".git");
    let work_tree = env!("CARGO_MANIFEST_DIR");
    success(h.shell_env(
        &line,
        "binit sync\n",
        &[
            ("GIT_DIR", git_dir.to_str().unwrap()),
            ("GIT_WORK_TREE", work_tree),
        ],
    ));
    assert!(stdout(&h.shell(&line, "echo safe:$SAFE\n")).contains("safe:isolated"));
    assert_eq!(
        fs::read_to_string(Path::new(&source).join("init.bash")).unwrap(),
        "SAFE=before\n"
    );
    let outer = tempfile::Builder::new()
        .prefix(".scratch-hook-state-")
        .tempdir_in(env!("CARGO_MANIFEST_DIR"))
        .unwrap();
    let colon = outer.path().join("state:colon");
    success(h.shell_env(
        &line,
        "binit sync\n",
        &[
            ("BINIT_STATE", colon.to_str().unwrap()),
            ("GIT_DIR", git_dir.to_str().unwrap()),
            ("GIT_WORK_TREE", work_tree),
        ],
    ));
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
        "binit plugin ./relative/dir; echo \"s1:$?\"\nbinit plugin 'ext::sh -c id'; echo \"s2:$?\"\nbinit plugin http://example.invalid/a/b; echo \"s3:$?\"\nbinit plugin {dir} bogus=1; echo \"s4:$?\"\nbinit plugin {dir} load=weird; echo \"s5:$?\"\nbinit plugin; echo \"s6:$?\"\nbinit plugin {dir} id=a id=b; echo \"s7:$?\"\nbinit plugin {dir} loose; echo \"s8:$?\"\nbinit plugin '~/x'; echo \"s9:$?\"\nbinit plugin -evil/repo; echo \"s10:$?\"\nbinit plugin {dir} ver=v1; echo \"s11:$?\"\n"
    );
    let output = h.shell(&lines, "echo alive\n");
    let text = stdout(&output);
    for n in 1..=11 {
        assert!(
            text.contains(&format!("s{n}:2")),
            "{text}{}",
            stderr(&output)
        );
    }
    assert!(text.contains("alive"));
    let message = stderr(&output);
    assert!(
        message
            .matches("the source must be an absolute path")
            .count()
            == 5,
        "{message}"
    );
    assert!(
        message.contains("ver applies to Git sources only"),
        "{message}"
    );
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

// Git sources. Every repository is a local directory reached by a file:// URL.
fn git_in(dir: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .args(args)
        .current_dir(dir)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_AUTHOR_NAME", "t")
        .env("GIT_AUTHOR_EMAIL", "t@example.invalid")
        .env("GIT_COMMITTER_NAME", "t")
        .env("GIT_COMMITTER_EMAIL", "t@example.invalid")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().to_owned()
}

struct Repo {
    dir: PathBuf,
}
impl Repo {
    fn new(h: &Home, name: &str, file: &str, body: &str) -> Self {
        let dir = h.f.root().join("remotes").join(name);
        fs::create_dir_all(&dir).unwrap();
        git_in(&dir, &["init", "-q", "-b", "main"]);
        let repo = Self { dir };
        repo.commit(file, body, "first");
        repo
    }
    fn commit(&self, file: &str, body: &str, subject: &str) -> String {
        let path = self.dir.join(file);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, body).unwrap();
        git_in(&self.dir, &["add", "-A"]);
        git_in(&self.dir, &["commit", "-q", "-m", subject]);
        self.head()
    }
    fn head(&self) -> String {
        git_in(&self.dir, &["rev-parse", "HEAD"])
    }
    fn url(&self) -> String {
        format!("file://{}", self.dir.display())
    }
}

#[test]
fn first_sync_pins_the_default_branch_and_the_shell_loads_the_snapshot() {
    let h = Home::new();
    let repo = Repo::new(&h, "plug", "init.bash", "BINIT_GIT_V=1\n");
    let first = repo.head();
    let lines = format!("binit plugin {}\n", repo.url());
    let output = h.shell(&lines, "binit sync\n");
    assert!(output.status.success(), "{}", stderr(&output));
    assert!(
        stdout(&output).contains("Synced 1 plugin line"),
        "{}",
        stderr(&output)
    );
    let info = success(h.binit(&["inspect", "plug"]));
    assert!(info.contains(&format!("Source: {}", repo.url())), "{info}");
    assert!(info.contains("Ver: default branch"), "{info}");
    assert!(info.contains(&format!("Commit: {first}")), "{info}");
    let mirrors: Vec<_> = fs::read_dir(h.f.state().join("sources"))
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    assert!(
        mirrors.len() == 1 && mirrors[0].ends_with("-plug"),
        "{mirrors:?}"
    );
    // The upstream repository is gone; the snapshot still loads.
    fs::remove_dir_all(&repo.dir).unwrap();
    let output = h.shell(&lines, "echo \"v:$BINIT_GIT_V\"\n");
    assert!(stdout(&output).contains("v:1"), "{}", stderr(&output));
    let checkouts = fs::read_dir(h.f.state().join("tmp")).map(|d| d.count());
    assert!(matches!(checkouts, Ok(0)), "{checkouts:?}");
}

#[test]
fn sync_keeps_pins_while_update_check_previews_and_update_applies() {
    let h = Home::new();
    let repo = Repo::new(&h, "plug", "init.bash", "BINIT_GIT_V=1\n");
    let first = repo.head();
    let lines = format!("binit plugin {}\n", repo.url());
    success(h.shell(&lines, "binit sync\n"));
    let pinned = h.current();
    let second = repo.commit("init.bash", "BINIT_GIT_V=2\n", "second change");
    repo.commit("init.bash", "BINIT_GIT_V=3\n", "third change");
    let third = repo.head();
    success(h.shell(&lines, "binit sync\n"));
    assert_eq!(h.current(), pinned);
    let preview = success(h.binit(&["update", "--check"]));
    assert!(
        preview.contains(&format!(
            "plug  {} -> {}  (2 commits)",
            &first[..12],
            &third[..12]
        )),
        "{preview}"
    );
    assert!(
        preview.contains("second change") && preview.contains("third change"),
        "{preview}"
    );
    assert!(preview.contains(&second[..7]), "{preview}");
    assert_eq!(h.current(), pinned);
    let output = h.shell(&lines, "echo \"v:$BINIT_GIT_V\"\n");
    assert!(stdout(&output).contains("v:1"));
    let applied = success(h.binit(&["update", "plug"]));
    assert!(applied.contains("plug  "), "{applied}");
    assert_ne!(h.current(), pinned);
    let info = success(h.binit(&["inspect", "plug"]));
    assert!(info.contains(&format!("Commit: {third}")), "{info}");
    let output = h.shell(&lines, "echo \"v:$BINIT_GIT_V\"\n");
    assert!(stdout(&output).contains("v:3"), "{}", stderr(&output));
    let again = success(h.binit(&["update", "--check", "plug"]));
    assert!(again.contains("plug  up to date"), "{again}");
    let current = h.current();
    success(h.binit(&["update"]));
    assert_eq!(h.current(), current);
}

#[test]
fn ver_selects_a_tag_and_a_full_commit_never_moves() {
    let h = Home::new();
    let repo = Repo::new(&h, "plug", "init.bash", "BINIT_GIT_V=1\n");
    let first = repo.head();
    git_in(&repo.dir, &["tag", "v1"]);
    repo.commit("init.bash", "BINIT_GIT_V=2\n", "later");
    let lines = format!(
        "binit plugin {url} ver=v1\nbinit plugin {url} id=exact ver={first}\n",
        url = repo.url()
    );
    let lines = lines.replacen("ver=v1", "id=tagged ver=v1", 1);
    success(h.shell(&lines, "binit sync\n"));
    for id in ["tagged", "exact"] {
        let info = success(h.binit(&["inspect", id]));
        assert!(info.contains(&format!("Commit: {first}")), "{info}");
    }
    assert!(success(h.binit(&["inspect", "tagged"])).contains("Ver: v1"));
    // A pinned commit needs no remote.
    git_in(&repo.dir, &["tag", "-f", "v1"]);
    let moved = repo.commit("init.bash", "BINIT_GIT_V=3\n", "third");
    git_in(&repo.dir, &["tag", "-f", "v1", &moved]);
    let hidden = h.f.root().join("remotes/hidden");
    fs::rename(&repo.dir, &hidden).unwrap();
    let output = h.binit(&["update", "--check", "exact"]);
    assert!(
        stdout(&output).contains("exact  pinned to commit"),
        "{}",
        stderr(&output)
    );
    fs::rename(&hidden, &repo.dir).unwrap();
    let output = success(h.binit(&["update"]));
    assert!(output.contains("exact  pinned to commit"), "{output}");
    assert!(output.contains("tagged  "), "{output}");
    let info = success(h.binit(&["inspect", "tagged"]));
    assert!(info.contains(&format!("Commit: {moved}")), "{info}");
    let info = success(h.binit(&["inspect", "exact"]));
    assert!(info.contains(&format!("Commit: {first}")), "{info}");
}

#[test]
fn default_files_are_chosen_inside_a_git_checkout() {
    let h = Home::new();
    let repo = Repo::new(&h, "plug", "init.bash", "BINIT_GIT_OTHER=1\n");
    repo.commit("plug.plugin.bash", "BINIT_GIT_PICK=1\n", "named entry");
    let lines = format!("binit plugin {}\n", repo.url());
    success(h.shell(&lines, "binit sync\n"));
    let output = h.shell(
        &lines,
        "echo \"p:${BINIT_GIT_PICK-}:${BINIT_GIT_OTHER-}\"\n",
    );
    assert!(stdout(&output).contains("p:1:"), "{}", stderr(&output));
    assert!(!stdout(&output).contains("p:1:1"));
}

#[test]
fn a_failing_fetch_leaves_the_generation_usable_with_a_clear_error() {
    let h = Home::new();
    let repo = Repo::new(&h, "plug", "init.bash", "BINIT_GIT_V=1\n");
    let lines = format!("binit plugin {}\n", repo.url());
    success(h.shell(&lines, "binit sync\n"));
    let current = h.current();
    fs::remove_dir_all(&repo.dir).unwrap();
    let output = h.binit(&["update"]);
    assert!(!output.status.success());
    let message = stderr(&output);
    assert!(
        message.contains("plugin line file://") && message.contains("git fetch failed"),
        "{message}"
    );
    assert_eq!(h.current(), current);
    let output = h.shell(&lines, "echo \"v:$BINIT_GIT_V\"\n");
    assert!(stdout(&output).contains("v:1"));
    let output = h.binit(&["update", "--check"]);
    assert!(!output.status.success());
    assert_eq!(h.current(), current);
}

#[test]
fn a_symlinked_selected_file_in_a_repository_is_rejected() {
    let h = Home::new();
    let repo = Repo::new(&h, "plug", "real.bash", "BINIT_GIT_V=1\n");
    symlink("real.bash", repo.dir.join("init.bash")).unwrap();
    git_in(&repo.dir, &["add", "-A"]);
    git_in(&repo.dir, &["commit", "-q", "-m", "link"]);
    let lines = format!("binit plugin {}\n", repo.url());
    let output = h.shell(&lines, "binit sync\necho \"status:$?\"\n");
    assert!(stdout(&output).contains("status:1"), "{}", stderr(&output));
    assert!(
        stderr(&output).contains("symlink in repository: init.bash"),
        "{}",
        stderr(&output)
    );
    failure(h.binit(&["inspect", "plug"]), "plugin not installed");
    let leftovers = fs::read_dir(h.f.state().join("tmp")).map_or(0, |d| d.count());
    assert_eq!(leftovers, 0);
}

#[test]
fn startup_runs_no_git_and_loads_synced_git_plugins() {
    let h = Home::new();
    let repo = Repo::new(&h, "plug", "init.bash", "BINIT_GIT_V=1\n");
    let lines = format!("binit plugin {}\n", repo.url());
    success(h.shell(&lines, "binit sync\n"));
    let marker = h.f.root().join("git-was-called");
    let fake = h.f.write(
        "fakebin/git",
        &format!("#!/bin/sh\n: > '{}'\nexit 1\n", marker.display()),
    );
    fs::set_permissions(&fake, fs::Permissions::from_mode(0o755)).unwrap();
    let path = format!(
        "{}:{}",
        fake.parent().unwrap().display(),
        std::env::var("PATH").unwrap()
    );
    let output = h.shell_env(&lines, "echo \"v:$BINIT_GIT_V\"\n", &[("PATH", &path)]);
    assert!(stdout(&output).contains("v:1"), "{}", stderr(&output));
    assert!(!marker.exists());
}

#[test]
fn update_resnapshots_a_local_line_plugin() {
    let h = Home::new();
    let dir = h.src("loc", "init.sh", "BINIT_LOCAL=1\n");
    let lines = format!("binit plugin {dir}\n");
    success(h.shell(&lines, "binit sync\n"));
    fs::write(format!("{dir}/init.sh"), "BINIT_LOCAL=2\n").unwrap();
    let output = h.shell(&lines, "echo \"l:$BINIT_LOCAL\"\n");
    assert!(stdout(&output).contains("l:1"));
    let output = success(h.binit(&["update", "loc"]));
    assert!(output.contains("loc  re-snapshotted"), "{output}");
    let output = h.shell(&lines, "echo \"l:$BINIT_LOCAL\"\n");
    assert!(stdout(&output).contains("l:2"), "{}", stderr(&output));
    failure(h.binit(&["update", "--check", "loc"]), "not a Git plugin");
}

// Findings from review of the Git sources.
fn plugin_package(repo: &Repo, id: &str, body: &str) {
    repo.commit(
        "plugin.toml",
        &format!("schema=1\nid='{id}'\nentry='plugin.bash'\nload='startup'\n"),
        "manifest",
    );
    repo.commit("plugin.bash", body, "entry");
}

// Sends records to `binit declare` the way a shell does.
fn declare_lines(h: &Home, lines: &[&[&str]]) {
    use std::io::Write;
    use std::process::Stdio;
    let mut bytes = Vec::new();
    for line in lines {
        bytes.extend(format!("{}\0", line.len()).bytes());
        for argument in *line {
            bytes.extend(argument.bytes());
            bytes.push(0);
        }
    }
    let mut command = Command::new(env!("CARGO_BIN_EXE_binit"));
    command.arg("declare").stdin(Stdio::piped());
    let mut child = h.isolate(&mut command).spawn().unwrap();
    child.stdin.take().unwrap().write_all(&bytes).unwrap();
    assert!(child.wait().unwrap().success());
}

#[test]
fn update_refuses_an_upstream_id_that_takes_over_another_plugin() {
    let h = Home::new();
    let alpha = Repo::new(&h, "alpha", "README", "a\n");
    plugin_package(&alpha, "alpha", "BINIT_ALPHA=1\n");
    let beta = Repo::new(&h, "beta", "README", "b\n");
    plugin_package(&beta, "beta", "BINIT_BETA=1\n");
    let lines = format!(
        "binit plugin {}\nbinit plugin {}\n",
        alpha.url(),
        beta.url()
    );
    success(h.shell(&lines, "binit sync\n"));
    let before = h.current();
    alpha.commit(
        "plugin.toml",
        "schema=1\nid='beta'\nentry='plugin.bash'\nload='startup'\n",
        "take over",
    );
    let output = h.binit(&["update"]);
    assert!(!output.status.success());
    let message = stderr(&output);
    assert!(
        message.contains("plugin id would change from alpha to beta"),
        "{message}"
    );
    assert!(!stdout(&output).contains("->"), "{}", stdout(&output));
    assert_eq!(h.current(), before);
    let output = h.shell(&lines, "echo \"b:$BINIT_BETA\"\n");
    assert!(stdout(&output).contains("b:1"), "{}", stderr(&output));
}

#[test]
fn repository_attributes_and_user_filters_never_touch_snapshot_bytes() {
    let h = Home::new();
    let body = "BINIT_GIT_V=1 # $Id$\n";
    let repo = Repo::new(&h, "plug", "init.bash", body);
    repo.commit(
        ".gitattributes",
        "* filter=evil\n*.bash ident\n",
        "attributes",
    );
    let marker = h.f.root().join("filter-ran");
    let config = h.f.write(
        "gitconfig",
        &format!(
            "[filter \"evil\"]\n\tsmudge = \"echo ran >> {m}; cat\"\n\tclean = cat\n\trequired = true\n",
            m = marker.display()
        ),
    );
    // The configuration really runs the filter for a normal checkout.
    let probe = h.f.root().join("probe");
    let output = Command::new("git")
        .args(["clone", "-q", "--", &repo.url(), probe.to_str().unwrap()])
        .env("GIT_CONFIG_GLOBAL", &config)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .output()
        .unwrap();
    assert!(output.status.success(), "{}", stderr(&output));
    assert!(marker.exists());
    assert_ne!(fs::read(probe.join("init.bash")).unwrap(), body.as_bytes());
    fs::remove_file(&marker).unwrap();
    let lines = format!("binit plugin {}\n", repo.url());
    let output = h.shell_env(
        &lines,
        "binit sync\n",
        &[
            ("GIT_CONFIG_GLOBAL", config.to_str().unwrap()),
            ("GIT_CONFIG_NOSYSTEM", "1"),
        ],
    );
    assert!(stdout(&output).contains("Synced 1"), "{}", stderr(&output));
    assert!(!marker.exists());
    let stored = fs::read(
        h.f.state()
            .join("generations")
            .join(h.current())
            .join("plugins/plug/files/init.bash"),
    )
    .unwrap();
    assert_eq!(stored, body.as_bytes());
}

#[test]
fn leftover_staging_and_checkout_directories_are_removed_by_the_next_sync() {
    let h = Home::new();
    let repo = Repo::new(&h, "plug", "init.bash", "BINIT_GIT_V=1\n");
    let lines = format!("binit plugin {}\n", repo.url());
    success(h.shell(&lines, "binit sync\n"));
    let state = h.f.state();
    let stale = [
        state.join("sources/.staging-12-345"),
        state.join("tmp/checkout-12-345"),
    ];
    for dir in &stale {
        fs::create_dir_all(dir.join("inner")).unwrap();
    }
    let keep = [
        state.join("sources/.staging-keep"),
        state.join("tmp/checkout-12-345-6"),
        state.join("tmp/other"),
    ];
    for dir in &keep {
        fs::create_dir_all(dir).unwrap();
    }
    let elsewhere = h.f.root().join("elsewhere");
    fs::create_dir_all(&elsewhere).unwrap();
    symlink(&elsewhere, state.join("sources/.staging-7-8")).unwrap();
    success(h.shell(&lines, "binit sync\n"));
    for dir in &stale {
        assert!(!dir.exists(), "{}", dir.display());
    }
    for dir in &keep {
        assert!(dir.exists(), "{}", dir.display());
    }
    assert!(elsewhere.exists());
    assert!(fs::symlink_metadata(state.join("sources/.staging-7-8")).is_ok());
    success(h.binit(&["update", "--check"]));
}

#[test]
fn editing_other_keys_of_a_synced_line_keeps_its_commit() {
    let h = Home::new();
    let repo = Repo::new(&h, "plug", "init.bash", "BINIT_GIT_V=1\n");
    let first = repo.head();
    let lines = format!("binit plugin {}\n", repo.url());
    success(h.shell(&lines, "binit sync\n"));
    repo.commit("init.bash", "BINIT_GIT_V=2\n", "newer");
    let edited = format!("binit plugin {} load=prompt\n", repo.url());
    success(h.shell(&edited, "binit sync\n"));
    let info = success(h.binit(&["inspect", "plug"]));
    assert!(info.contains(&format!("Commit: {first}")), "{info}");
    assert!(info.contains("Load: prompt"), "{info}");
    // The mirror may even be gone; the pin is kept and re-snapshotted.
    for entry in fs::read_dir(h.f.state().join("sources")).unwrap() {
        fs::remove_dir_all(entry.unwrap().path()).unwrap();
    }
    let again = format!("binit plugin {} load=startup\n", repo.url());
    success(h.shell(&again, "binit sync\n"));
    let info = success(h.binit(&["inspect", "plug"]));
    assert!(info.contains(&format!("Commit: {first}")), "{info}");
}

#[test]
fn urls_with_a_password_or_token_are_rejected_by_the_shell_and_the_manager() {
    let h = Home::new();
    let token = "https://user:hunter2@example.invalid/a/b";
    let output = h.shell(
        &format!("binit plugin {token}; echo \"s:$?\"\n"),
        "echo alive\n",
    );
    assert!(stdout(&output).contains("s:2"), "{}", stderr(&output));
    assert!(!stderr(&output).contains("hunter2"), "{}", stderr(&output));
    assert!(stderr(&output).contains("credential helper"));
    assert!(!h.declared().exists());
    declare_lines(&h, &[&[token]]);
    let output = h.binit(&["sync"]);
    assert!(!output.status.success());
    assert!(!stderr(&output).contains("hunter2"), "{}", stderr(&output));
    assert!(stderr(&output).contains("credential helper"));
    assert!(!h.f.state().join("sources").exists());
}

#[test]
fn a_full_commit_line_updates_without_the_remote() {
    let h = Home::new();
    let repo = Repo::new(&h, "plug", "init.bash", "BINIT_GIT_V=1\n");
    let first = repo.head();
    let lines = format!("binit plugin {} ver={first}\n", repo.url());
    success(h.shell(&lines, "binit sync\n"));
    let before = h.current();
    fs::remove_dir_all(&repo.dir).unwrap();
    for args in [&["update"][..], &["update", "plug"], &["update", "--check"]] {
        let output = success(h.binit(args));
        assert!(output.contains("plug  pinned to commit"), "{output}");
    }
    assert_eq!(h.current(), before);
}

#[test]
fn a_commit_that_is_on_no_branch_or_tag_is_rejected() {
    let h = Home::new();
    let repo = Repo::new(&h, "plug", "init.bash", "BINIT_GIT_V=1\n");
    let tree = git_in(&repo.dir, &["rev-parse", "HEAD^{tree}"]);
    let hidden = git_in(
        &repo.dir,
        &["commit-tree", &tree, "-p", "HEAD", "-m", "hidden"],
    );
    git_in(&repo.dir, &["update-ref", "refs/hidden/x", &hidden]);
    let lines = format!("binit plugin {} ver={hidden}\n", repo.url());
    let output = h.shell(&lines, "binit sync\necho \"status:$?\"\n");
    assert!(stdout(&output).contains("status:1"), "{}", stderr(&output));
    assert!(
        stderr(&output).contains("commit is not on any branch or tag of the remote"),
        "{}",
        stderr(&output)
    );
    failure(h.binit(&["inspect", "plug"]), "plugin not installed");
    // The same commit is fine once a branch contains it.
    git_in(&repo.dir, &["branch", "kept", &hidden]);
    let output = h.shell(&lines, "binit sync\n");
    assert!(stdout(&output).contains("Synced 1"), "{}", stderr(&output));
}

#[test]
fn bad_keys_fail_before_any_clone() {
    let h = Home::new();
    let repo = Repo::new(&h, "plug", "init.bash", "BINIT_GIT_V=1\n");
    let lines = format!("binit plugin {} files=../x\n", repo.url());
    let output = h.shell(&lines, "binit sync\necho \"status:$?\"\n");
    assert!(stdout(&output).contains("status:1"), "{}", stderr(&output));
    assert!(
        stderr(&output).contains("invalid declared file"),
        "{}",
        stderr(&output)
    );
    assert!(!h.f.state().join("sources").exists());
}

#[test]
fn a_failing_plugin_prints_no_moves() {
    let h = Home::new();
    let good = Repo::new(&h, "good", "init.bash", "BINIT_GOOD=1\n");
    let bad = Repo::new(&h, "bad", "init.bash", "BINIT_BAD=1\n");
    let lines = format!("binit plugin {}\nbinit plugin {}\n", good.url(), bad.url());
    success(h.shell(&lines, "binit sync\n"));
    let before = h.current();
    good.commit("init.bash", "BINIT_GOOD=2\n", "good moves");
    fs::remove_file(bad.dir.join("init.bash")).unwrap();
    symlink("/etc/hostname", bad.dir.join("init.bash")).unwrap();
    git_in(&bad.dir, &["add", "-A"]);
    git_in(&bad.dir, &["commit", "-q", "-m", "link"]);
    let output = h.binit(&["update"]);
    assert!(!output.status.success());
    assert!(
        stderr(&output).contains("symlink in repository"),
        "{}",
        stderr(&output)
    );
    assert!(!stdout(&output).contains("->"), "{}", stdout(&output));
    assert_eq!(h.current(), before);
}

#[test]
fn package_mode_rejects_a_symlink_anywhere_in_the_repository() {
    let h = Home::new();
    let repo = Repo::new(&h, "plug", "README", "r\n");
    plugin_package(&repo, "plug", "BINIT_GIT_V=1\n");
    symlink("README", repo.dir.join("docs-link")).unwrap();
    git_in(&repo.dir, &["add", "-A"]);
    git_in(&repo.dir, &["commit", "-q", "-m", "link"]);
    let lines = format!("binit plugin {}\n", repo.url());
    let output = h.shell(&lines, "binit sync\necho \"status:$?\"\n");
    assert!(stdout(&output).contains("status:1"), "{}", stderr(&output));
    assert!(
        stderr(&output).contains("symlink in repository: docs-link"),
        "{}",
        stderr(&output)
    );
}

#[test]
fn update_with_two_arguments_is_a_path_only_when_the_second_is_not_installed() {
    let h = Home::new();
    h.f.plugin("cat", "load='startup'", "X=1\n");
    let dir = h.f.root().join("cat");
    success(h.binit(&["add", dir.to_str().unwrap()]));
    let repo = Repo::new(&h, "plug", "init.bash", "BINIT_GIT_V=1\n");
    let lines = format!("binit plugin {}\n", repo.url());
    success(h.shell(&lines, "binit sync\n"));
    let before = h.current();
    failure(h.binit(&["update", "cat", "plug"]), "catalog plugin");
    assert_eq!(h.current(), before);
    h.f.write("cat/plugin.bash", "X=2\n");
    success(h.binit(&["update", "cat", dir.to_str().unwrap()]));
    assert_ne!(h.current(), before);
}

// Keep one source table for both checks: the Rust unit test reads it too.
#[test]
fn the_shell_and_the_manager_accept_the_same_sources() {
    let table =
        fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("src/git.rs")).unwrap();
    let body = table
        .split("// SOURCE-TABLE-BEGIN")
        .nth(1)
        .and_then(|rest| rest.split("// SOURCE-TABLE-END").next())
        .unwrap();
    let mut cases = Vec::new();
    for line in body.lines().map(str::trim) {
        let Some(entry) = line.strip_prefix("(\"").and_then(|l| l.strip_suffix("),")) else {
            continue;
        };
        let (source, accepted) = entry.rsplit_once("\", ").unwrap();
        cases.push((source.to_owned(), accepted == "true"));
    }
    assert!(cases.len() > 40, "{}", cases.len());
    let h = Home::new();
    let mut script = String::new();
    for (index, (source, _)) in cases.iter().enumerate() {
        script.push_str(&format!(
            "binit plugin {} 2>/dev/null; echo \"r{index}:$?\"\n",
            sh_quote(source)
        ));
    }
    let output = h.shell(&script, "");
    let text = stdout(&output);
    for (index, (source, accepted)) in cases.iter().enumerate() {
        let want = if *accepted { 0 } else { 2 };
        assert!(
            text.lines().any(|line| line == format!("r{index}:{want}")),
            "{source:?} should be {} by the shell\n{text}",
            if *accepted { "accepted" } else { "rejected" }
        );
    }
}

#[test]
fn https_userinfo_is_rejected_without_echoing_it_and_ssh_users_stay() {
    let h = Home::new();
    let token = "https://ghp_abcdef@example.invalid/a/b";
    let output = h.shell(
        &format!("binit plugin {token}; echo \"s:$?\"\nbinit plugin http://alice:pw0rd@h/a/b; echo \"t:$?\"\n"),
        "echo alive\n",
    );
    let text = stdout(&output);
    assert!(
        text.contains("s:2") && text.contains("t:2"),
        "{}",
        stderr(&output)
    );
    let errors = stderr(&output);
    assert!(errors.contains("credential helper"), "{errors}");
    assert!(
        !errors.contains("ghp_abcdef") && !errors.contains("pw0rd"),
        "{errors}"
    );
    assert!(!h.declared().exists());
    declare_lines(&h, &[&[token]]);
    let output = h.binit(&["sync"]);
    assert!(!output.status.success());
    let errors = stderr(&output);
    assert!(errors.contains("credential helper"), "{errors}");
    assert!(!errors.contains("ghp_abcdef"), "{errors}");
    assert!(!h.f.state().join("sources").exists());
    declare_lines(&h, &[&["http://alice:pw0rd@h/a/b"]]);
    let output = h.binit(&["sync"]);
    assert!(!stderr(&output).contains("pw0rd"), "{}", stderr(&output));
}

#[test]
fn non_ascii_whitespace_is_rejected_by_the_shell_and_the_manager() {
    let h = Home::new();
    let mut script = String::new();
    let sources = [
        "user/re\u{a0}po",
        "u@host:pa\u{a0}th",
        "https://h/a\u{2028}b",
    ];
    for (index, source) in sources.iter().enumerate() {
        script.push_str(&format!(
            "binit plugin {} 2>/dev/null; echo \"r{index}:$?\"\n",
            sh_quote(source)
        ));
    }
    let text = stdout(&h.shell(&script, ""));
    for index in 0..sources.len() {
        assert!(text.lines().any(|l| l == format!("r{index}:2")), "{text}");
    }
    for source in sources {
        declare_lines(&h, &[&[source]]);
        let output = h.binit(&["sync"]);
        assert!(!output.status.success(), "{source:?}");
        assert!(!h.f.state().join("sources").exists());
    }
}

#[test]
fn a_repository_name_that_is_not_an_id_fails_before_cloning() {
    let h = Home::new();
    let repo = Repo::new(&h, "repo.js", "init.bash", "BINIT_GIT_V=1\n");
    let lines = format!("binit plugin {} files=init.bash\n", repo.url());
    let output = h.shell(&lines, "binit sync\necho \"status:$?\"\n");
    assert!(stdout(&output).contains("status:1"), "{}", stderr(&output));
    assert!(
        stderr(&output).contains("add id=NAME"),
        "{}",
        stderr(&output)
    );
    assert!(!h.f.state().join("sources").exists());
}

#[test]
fn a_failed_publish_after_update_prints_no_moves() {
    let h = Home::new();
    let one = Repo::new(&h, "one", "README", "r\n");
    plugin_package(&one, "one", "BINIT_ONE=1\n");
    let other = Repo::new(&h, "other", "README", "r\n");
    other.commit(
        "plugin.toml",
        "schema=1\nid='other'\nentry='plugin.bash'\nload='command'\ncommands=['dup']\n",
        "manifest",
    );
    other.commit("plugin.bash", ":\n", "entry");
    let lines = format!("binit plugin {}\nbinit plugin {}\n", one.url(), other.url());
    success(h.shell(&lines, "binit sync\n"));
    let before = h.current();
    one.commit(
        "plugin.toml",
        "schema=1\nid='one'\nentry='plugin.bash'\nload='command'\ncommands=['dup']\n",
        "takes dup",
    );
    let output = h.binit(&["update"]);
    assert!(!output.status.success());
    assert!(
        stderr(&output).contains("duplicate command trigger"),
        "{}",
        stderr(&output)
    );
    assert!(!stdout(&output).contains("->"), "{}", stdout(&output));
    assert_eq!(h.current(), before);
}

#[test]
fn a_dangling_remote_head_only_breaks_lines_without_ver() {
    let h = Home::new();
    let repo = Repo::new(&h, "plug", "init.bash", "BINIT_GIT_V=1\n");
    git_in(&repo.dir, &["tag", "v1"]);
    git_in(&repo.dir, &["symbolic-ref", "HEAD", "refs/heads/gone"]);
    let lines = format!("binit plugin {} ver=v1\n", repo.url());
    success(h.shell(&lines, "binit sync\n"));
    let first = git_in(&repo.dir, &["rev-parse", "v1^{commit}"]);
    let info = success(h.binit(&["inspect", "plug"]));
    assert!(info.contains(&format!("Commit: {first}")), "{info}");
    let next = git_in(
        &repo.dir,
        &["commit-tree", "main^{tree}", "-p", "main", "-m", "next"],
    );
    git_in(&repo.dir, &["update-ref", "refs/heads/main", &next]);
    git_in(&repo.dir, &["tag", "-f", "v1", &next]);
    success(h.binit(&["update", "plug"]));
    let info = success(h.binit(&["inspect", "plug"]));
    assert!(info.contains(&format!("Commit: {next}")), "{info}");
    let before = h.current();
    declare_lines(&h, &[&[&repo.url()]]);
    failure(
        h.binit(&["sync"]),
        "the remote has no default branch; set ver=",
    );
    assert_eq!(h.current(), before);
}

#[test]
fn a_new_id_resolves_fresh_and_never_borrows_a_pin_from_the_same_repository() {
    let h = Home::new();
    let repo = Repo::new(&h, "plug", "init.bash", "BINIT_GIT_V=1\n");
    let first = repo.head();
    let line = |id: &str| {
        format!(
            "binit plugin {} id={id} files=init.bash ver=main\n",
            repo.url()
        )
    };
    success(h.shell(&(line("a") + &line("b")), "binit sync\n"));
    let second = repo.commit("init.bash", "BINIT_GIT_V=2\n", "second");
    success(h.binit(&["update", "a"]));
    let third = repo.commit("init.bash", "BINIT_GIT_V=3\n", "third");
    success(h.shell(&(line("c") + &line("b")), "binit sync\n"));
    let c = success(h.binit(&["inspect", "c"]));
    let b = success(h.binit(&["inspect", "b"]));
    assert!(c.contains(&format!("Commit: {third}")), "{c}");
    assert!(b.contains(&format!("Commit: {first}")), "{b}");
    assert_ne!(second, third);
}

#[test]
fn only_a_different_ver_or_id_resolves_again_while_other_keys_keep_the_pin() {
    let h = Home::new();
    let repo = Repo::new(&h, "plug", "init.bash", "BINIT_GIT_V=1\n");
    let first = repo.head();
    let line = |extra: &str| format!("binit plugin {} files=init.bash{extra}\n", repo.url());
    success(h.shell(&line(&format!(" ver={first}")), "binit sync\n"));
    let second = repo.commit("init.bash", "BINIT_GIT_V=2\n", "second");
    // Other keys change freely and keep the pin.
    success(h.shell(&line(&format!(" ver={first} load=prompt")), "binit sync\n"));
    let info = success(h.binit(&["inspect", "plug"]));
    assert!(info.contains(&format!("Commit: {first}")), "{info}");
    assert!(info.contains("Load: prompt"), "{info}");
    // Removing ver= is a different ver: the current commit is resolved.
    success(h.shell(&line(""), "binit sync\n"));
    let info = success(h.binit(&["inspect", "plug"]));
    assert!(info.contains(&format!("Commit: {second}")), "{info}");
    // A branch pin stays while an unrelated key changes.
    let third = repo.commit("init.bash", "BINIT_GIT_V=3\n", "third");
    success(h.shell(&line(" load=startup"), "binit sync\n"));
    let info = success(h.binit(&["inspect", "plug"]));
    assert!(info.contains(&format!("Commit: {second}")), "{info}");
    success(h.binit(&["update", "plug"]));
    let info = success(h.binit(&["inspect", "plug"]));
    assert!(info.contains(&format!("Commit: {third}")), "{info}");
}

#[test]
fn spelling_the_same_source_differently_keeps_the_pin_without_the_remote() {
    for suffix in ["/", ".git", ".git/"] {
        let h = Home::new();
        let repo = Repo::new(&h, "plug", "init.bash", "BINIT_GIT_V=1\n");
        let first = repo.head();
        let line = |url: &str| format!("binit plugin {url} files=init.bash\n");
        success(h.shell(&line(&repo.url()), "binit sync\n"));
        repo.commit("init.bash", "BINIT_GIT_V=2\n", "second");
        let respelled = format!("{}{suffix}", repo.url());
        success(h.shell(&line(&respelled), "binit sync\n"));
        let info = success(h.binit(&["inspect", "plug"]));
        assert!(
            info.contains(&format!("Commit: {first}")),
            "{suffix} {info}"
        );
    }
}

#[test]
fn rejected_sources_with_credentials_are_never_echoed() {
    let sources = [
        "oauth2:glpat-SECRET@gitlab.com:o/r.git",
        "x:SECRET@host:path",
        "https:/u:SECRET@h/x",
        "https://u:SECRET@h/x",
        "ssh://ghp_SECRET@h/x",
        "ghp_SECRET@github.com:o/r",
    ];
    let fixed = "binit: plugin line rejected: a source with credentials is not allowed; use a Git credential helper or an SSH key";
    for source in sources {
        let h = Home::new();
        let output = h.shell(
            &format!("binit plugin {}\n", sh_quote(source)),
            "echo alive\n",
        );
        let all = format!("{}{}", stdout(&output), stderr(&output));
        assert!(!all.contains("SECRET"), "{source}: {all}");
        assert!(stderr(&output).contains(fixed), "{source}: {all}");
        declare_lines(&h, &[&[source]]);
        let output = h.binit(&["sync"]);
        assert!(!output.status.success(), "{source}");
        let all = format!("{}{}", stdout(&output), stderr(&output));
        assert!(!all.contains("SECRET"), "{source}: {all}");
        assert!(stderr(&output).contains(fixed), "{source}: {all}");
        assert!(!h.f.state().join("sources").exists());
    }
}

#[test]
fn wait_plugins_from_a_manifest_stay_pending_until_dispatched() {
    let f = Fixture::new();
    f.plugin("slow", "load='wait'", "BINIT_SLOW=loaded\n");
    success(f.prepare(&["slow"]));
    let text = success(f.bash(
        r#"
source "$BINIT_TEST_INIT"
[[ ${__binit_state[slow]} == pending && -z ${BINIT_SLOW-} && $__binit_wait_pending == 1 ]] || exit 2
binit profile | awk '$1 == "slow" {print "before:" $2}'
false
__binit_wait_dispatch
[[ $? == 0 ]] || exit 3
[[ ${__binit_state[slow]} == loaded && $BINIT_SLOW == loaded && $__binit_wait_pending == 0 ]] || exit 4
binit profile | awk '$1 == "slow" {print "after:" $2}'
"#,
    ));
    assert!(text.contains("before:pending"), "{text}");
    assert!(text.contains("after:loaded"), "{text}");
}

#[test]
fn wait_lines_load_by_the_first_prompt_without_the_native_module() {
    let h = Home::new();
    let one = h.src("one", "init.sh", "BINIT_ORDER+=one\n");
    let two = h.src("two", "init.sh", "BINIT_ORDER+=two\n");
    let lines = format!(
        "BINIT_ORDER=\nbinit plugin {two} id=two load=wait\nbinit plugin {one} id=one load=wait\n"
    );
    let sync = h.shell(&lines, "binit sync\n");
    assert!(
        stdout(&sync).contains("Synced 2 plugin lines"),
        "{}{}",
        stdout(&sync),
        stderr(&sync)
    );
    let output = h.shell(&lines, "echo \"order:$BINIT_ORDER\"\nbinit profile\n");
    let text = stdout(&output);
    assert!(text.contains("order:twoone"), "{text}{}", stderr(&output));
    for id in ["one", "two"] {
        assert!(
            text.lines()
                .any(|l| l.split_whitespace().take(2).eq([id, "loaded"])),
            "{id}: {text}"
        );
    }
    let bad = h.shell("binit plugin /x load=later\n", "");
    assert!(
        stderr(&bad).contains(
            "load must be startup, prompt, wait, command, completion, timed, event or watch"
        ),
        "{}",
        stderr(&bad)
    );
}

fn program_source(h: &Home) -> String {
    let script = "#!/bin/sh\nprintf 'ran:%s\\n' \"$*\"\nexit 3\n";
    h.src("tools", "bin/mytool", script);
    h.src("tools", "other.sh", script)
}

fn generation_dir(h: &Home) -> PathBuf {
    h.f.state().join("generations").join(h.current())
}

#[test]
fn bin_lines_expose_commands_with_arguments_status_and_rename() {
    let h = Home::new();
    let tools = program_source(&h);
    let lines = format!("binit plugin {tools} bin=bin/mytool,other.sh:renamed\n");
    success(h.shell(&lines, "binit sync\n"));
    let generation = generation_dir(&h);
    for name in ["mytool", "renamed"] {
        let mode = fs::metadata(generation.join("bin").join(name))
            .unwrap()
            .permissions()
            .mode();
        assert_eq!(mode & 0o7777, 0o700, "{name}");
    }
    assert_eq!(
        fs::metadata(generation.join("plugins/tools/files/other.sh"))
            .unwrap()
            .permissions()
            .mode()
            & 0o7777,
        0o600
    );
    // A bin-only line sources nothing.
    let entry = fs::read_to_string(generation.join("plugins/tools/entry.bash")).unwrap();
    assert!(!entry.contains("source \""), "{entry}");
    let output = h.shell(
        &lines,
        "mytool a 'b c'; echo \"status:$?\"\nrenamed x; echo \"status:$?\"\ncommand -v mytool\ncommand -v other.sh || echo no-other\n",
    );
    let text = stdout(&output);
    assert!(text.contains("ran:a b c\n"), "{text}{}", stderr(&output));
    assert!(text.contains("ran:x\n"), "{text}");
    assert_eq!(text.matches("status:3").count(), 2, "{text}");
    assert!(
        text.contains(&format!("{}/bin/mytool", generation.display())),
        "{text}"
    );
    assert!(text.contains("no-other"), "{text}");
    let inspect = success(h.binit(&["inspect", "tools"]));
    assert!(
        inspect.contains("Programs: mytool (bin/mytool), renamed (other.sh)"),
        "{inspect}"
    );
}

#[test]
fn a_git_line_can_source_files_and_expose_a_command() {
    let h = Home::new();
    let repo = Repo::new(&h, "foo", "foo.bash", "BINIT_FOO=sourced\n");
    repo.commit("git-foo", "#!/bin/sh\necho foo-ran \"$1\"\n", "command");
    let lines = format!("binit plugin {} bin=git-foo files=foo.bash\n", repo.url());
    success(h.shell(&lines, "binit sync\n"));
    let output = h.shell(&lines, "echo \"foo:$BINIT_FOO\"\ngit-foo arg\n");
    let text = stdout(&output);
    assert!(text.contains("foo:sourced"), "{text}{}", stderr(&output));
    assert!(text.contains("foo-ran arg"), "{text}");
}

#[test]
fn duplicate_command_names_are_rejected_naming_both_plugins() {
    let h = Home::new();
    let one = h.src("one", "tool.sh", "#!/bin/sh\n");
    let two = h.src("two", "run.sh", "#!/bin/sh\n");
    let good = format!("binit plugin {one} bin=tool.sh:same\n");
    success(h.shell(&good, "binit sync\n"));
    let previous = h.current();
    let lines = format!("{good}binit plugin {two} bin=run.sh:same\n");
    let output = h.shell(&lines, "binit sync\n");
    let text = stderr(&output);
    assert!(
        text.contains("duplicate command same") && text.contains(" one ") && text.contains(" two"),
        "{text}"
    );
    assert_eq!(h.current(), previous);
}

#[test]
fn symlinked_traversing_and_invalid_bin_paths_are_rejected() {
    let h = Home::new();
    let tools = program_source(&h);
    std::os::unix::fs::symlink("other.sh", PathBuf::from(&tools).join("link.sh")).unwrap();
    let good = h.src("good", "init.sh", "BINIT_GOOD=1\n");
    let base = format!("binit plugin {good}\n");
    success(h.shell(&base, "binit sync\n"));
    let previous = h.current();
    for bad in [
        "bin=link.sh",
        "bin=../outside",
        "bin=/etc/passwd",
        "bin=other.sh:",
        "bin=other.sh:-bad",
        "bin=other.sh:a/b",
        "bin=",
        "bin=missing.sh",
    ] {
        let output = h.shell(
            &format!("{base}binit plugin {tools} {bad}\n"),
            "binit sync\necho \"status:$?\"\n",
        );
        assert!(
            stdout(&output).contains("status:1"),
            "{bad}: {}{}",
            stdout(&output),
            stderr(&output)
        );
        assert_eq!(h.current(), previous, "{bad}");
    }
}

#[test]
fn a_tampered_bin_directory_fails_verification_at_startup() {
    let h = Home::new();
    let tools = program_source(&h);
    let lines = format!("binit plugin {tools} bin=bin/mytool\n");
    success(h.shell(&lines, "binit sync\n"));
    let program = generation_dir(&h).join("bin/mytool");
    let original = fs::read(&program).unwrap();
    let check = |message: &str| {
        failure(h.binit(&["init"]), message);
        let output = h.shell(&lines, "command -v mytool || echo no-command\n");
        assert!(stderr(&output).contains(message), "{}", stderr(&output));
        assert!(
            stdout(&output).contains("no-command"),
            "{}",
            stdout(&output)
        );
    };
    fs::set_permissions(&program, fs::Permissions::from_mode(0o755)).unwrap();
    check("unexpected owner or mode: bin/mytool");
    fs::set_permissions(&program, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(&program, b"#!/bin/sh\necho evil\n").unwrap();
    check("content changed: bin/mytool");
    fs::write(&program, &original).unwrap();
    success(h.binit(&["init"]));
    let extra = generation_dir(&h).join("bin/extra");
    fs::write(&extra, b"#!/bin/sh\n").unwrap();
    fs::set_permissions(&extra, fs::Permissions::from_mode(0o700)).unwrap();
    check("unexpected file in bin: extra");
}

#[test]
fn disabling_a_catalog_plugin_removes_its_command() {
    let h = Home::new();
    let dir = h.src(
        "cat",
        "plugin.toml",
        "schema=1\nid='cat'\nentry='plugin.bash'\nload='startup'\nbin=['tool.sh:ctool']\n",
    );
    h.src("cat", "plugin.bash", ":\n");
    h.src("cat", "tool.sh", "#!/bin/sh\necho ctool-ran\n");
    success(h.binit(&["add", &dir]));
    let run = "ctool || echo no-ctool\n";
    assert!(stdout(&h.shell("", run)).contains("ctool-ran"));
    let inspect = success(h.binit(&["inspect", "cat"]));
    assert!(inspect.contains("Programs: ctool (tool.sh)"), "{inspect}");
    success(h.binit(&["disable", "cat"]));
    assert!(!generation_dir(&h).join("bin").exists());
    assert!(stdout(&h.shell("", run)).contains("no-ctool"));
    success(h.binit(&["enable", "cat"]));
    assert!(stdout(&h.shell("", run)).contains("ctool-ran"));
}

#[test]
fn the_bin_directory_joins_path_once_across_resourcing() {
    let h = Home::new();
    let tools = program_source(&h);
    let lines = format!("binit plugin {tools} bin=bin/mytool\n");
    success(h.shell(&lines, "binit sync\n"));
    let output = h.shell(
        &lines,
        "source \"$BINIT_TEST_RC\"\nsource \"$BINIT_TEST_RC\"\necho \"path:$PATH\"\n",
    );
    let text = stdout(&output);
    let bin = format!("{}/bin", generation_dir(&h).display());
    let path = text.lines().find_map(|l| l.strip_prefix("path:")).unwrap();
    assert_eq!(path.split(':').filter(|p| *p == bin).count(), 1, "{path}");
    assert!(path.starts_with(&bin), "{path}");
    // Recovery mode skips init, so PATH stays untouched.
    let safe = h.shell_env(&lines, "echo \"path:$PATH\"\n", &[("BINIT_SAFE", "1")]);
    assert!(!stdout(&safe).contains(&bin), "{}", stdout(&safe));
}
