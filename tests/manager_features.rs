use std::fs;
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

struct Fixture {
    directory: tempfile::TempDir,
    state: PathBuf,
    bin: PathBuf,
    evidence: PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let directory = tempfile::Builder::new()
            .prefix(".scratch-manager-features-")
            .tempdir_in(env!("CARGO_MANIFEST_DIR"))
            .unwrap();
        let state = directory.path().join("state");
        let bin = directory.path().join("bin");
        let evidence = directory.path().join("evidence");
        fs::create_dir(&bin).unwrap();
        fs::create_dir(&evidence).unwrap();
        fs::create_dir(evidence.join("active")).unwrap();
        Self {
            directory,
            state,
            bin,
            evidence,
        }
    }

    fn env(&self, command: &mut Command) {
        command
            .env("HOME", self.directory.path())
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("BINIT_FIXTURE_EVIDENCE", &self.evidence)
            .env("PATH", format!("{}:/usr/bin:/bin", self.bin.display()))
            .env_remove("BASH_ENV")
            .env_remove("ENV");
    }

    fn binit(&self, args: &[&str]) -> Output {
        let mut command = Command::new(env!("CARGO_BIN_EXE_binit"));
        command.args(args).arg("--state").arg(&self.state);
        self.env(&mut command);
        command.output().unwrap()
    }

    fn declare(&self, lines: &[Vec<String>]) {
        let mut bytes = Vec::new();
        for line in lines {
            bytes.extend_from_slice(format!("{}\0", line.len()).as_bytes());
            for field in line {
                bytes.extend_from_slice(field.as_bytes());
                bytes.push(0);
            }
        }
        let mut command = Command::new(env!("CARGO_BIN_EXE_binit"));
        command
            .arg("declare")
            .arg("--state")
            .arg(&self.state)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        self.env(&mut command);
        let mut child = command.spawn().unwrap();
        child.stdin.take().unwrap().write_all(&bytes).unwrap();
        success(child.wait_with_output().unwrap());
    }

    fn fake(&self, name: &str, body: &str) {
        let path = self.bin.join(name);
        fs::write(&path, body).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
    }

    fn current(&self) -> String {
        fs::read_to_string(self.state.join("current")).unwrap()
    }
}

fn success(output: Output) -> String {
    assert!(
        output.status.success(),
        "stdout: {}\nstderr: {}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap()
}

fn git(directory: &Path, args: &[&str]) {
    let output = Command::new("/usr/bin/git")
        .arg("-C")
        .arg(directory)
        .args([
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
        ])
        .args(args)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .output()
        .unwrap();
    success(output);
}

fn repository(f: &Fixture, name: &str) -> (PathBuf, PathBuf) {
    let work = f.directory.path().join(format!("work-{name}"));
    let remote = f.directory.path().join(format!("remote-{name}.git"));
    fs::create_dir(&work).unwrap();
    git(&work, &["init", "--quiet", "--initial-branch=main"]);
    fs::write(work.join("init.bash"), format!("BINIT_VALUE_{name}=old\n")).unwrap();
    git(&work, &["add", "init.bash"]);
    git(&work, &["commit", "--quiet", "-m", "initial"]);
    git(
        &work,
        &[
            "clone",
            "--quiet",
            "--bare",
            work.to_str().unwrap(),
            remote.to_str().unwrap(),
        ],
    );
    (work, remote)
}

fn update(work: &Path, remote: &Path, name: &str) {
    fs::write(work.join("init.bash"), format!("BINIT_VALUE_{name}=new\n")).unwrap();
    git(work, &["add", "init.bash"]);
    git(work, &["commit", "--quiet", "-m", "update"]);
    git(
        work,
        &[
            "push",
            "--quiet",
            remote.to_str().unwrap(),
            "HEAD:refs/heads/main",
        ],
    );
}

fn wrapped_git(f: &Fixture) {
    f.fake(
        "git",
        r##"#!/bin/sh
fetch=0
for item do [ "$item" = fetch ] && fetch=1; done
if [ "$fetch" = 1 ]; then
  mkdir "$BINIT_FIXTURE_EVIDENCE/active/$$" || exit 90
  n=$(find "$BINIT_FIXTURE_EVIDENCE/active" -mindepth 1 -maxdepth 1 -type d | wc -l)
  printf 'start:%s:%s\n' "$$" "$n" >> "$BINIT_FIXTURE_EVIDENCE/events"
  sleep 0.25
  /usr/bin/git "$@"; status=$?
  printf 'end:%s\n' "$$" >> "$BINIT_FIXTURE_EVIDENCE/events"
  rmdir "$BINIT_FIXTURE_EVIDENCE/active/$$"
  exit "$status"
fi
exec /usr/bin/git "$@"
"##,
    );
}

fn line(remote: &Path, id: &str, hook: bool) -> Vec<String> {
    let mut fields = vec![
        format!("file://{}", remote.display()),
        format!("id={id}"),
        "files=init.bash".into(),
    ];
    if hook {
        fields.push("atclone=printf 'hook:%s\\n' \"$BINIT_PLUGIN_ID\" >> \"$BINIT_FIXTURE_EVIDENCE/events\"".into());
    }
    fields
}

#[test]
fn sync_bounds_parallel_fetch_and_runs_hooks_after_all_fetches_in_line_order() {
    let f = Fixture::new();
    success(f.binit(&["init"]));
    let repos: Vec<_> = ["one", "two", "three"]
        .iter()
        .map(|id| repository(&f, id))
        .collect();
    f.declare(
        &repos
            .iter()
            .zip(["one", "two", "three"])
            .map(|((_, remote), id)| line(remote, id, true))
            .collect::<Vec<_>>(),
    );
    wrapped_git(&f);
    success(f.binit(&["sync", "--jobs", "2"]));
    let events = fs::read_to_string(f.evidence.join("events")).unwrap();
    let lines: Vec<_> = events.lines().collect();
    let starts: Vec<_> = lines
        .iter()
        .filter_map(|line| line.strip_prefix("start:"))
        .collect();
    assert_eq!(starts.len(), 6, "{events}");
    let peak = starts
        .iter()
        .map(|line| {
            line.rsplit(':')
                .next()
                .unwrap()
                .trim()
                .parse::<usize>()
                .unwrap()
        })
        .max()
        .unwrap();
    assert_eq!(peak, 2, "{events}");
    let last_end = lines
        .iter()
        .rposition(|line| line.starts_with("end:"))
        .unwrap();
    let hooks: Vec<_> = lines
        .iter()
        .enumerate()
        .filter(|(_, line)| line.starts_with("hook:"))
        .collect();
    assert_eq!(
        hooks.iter().map(|(_, line)| **line).collect::<Vec<_>>(),
        ["hook:one", "hook:two", "hook:three"]
    );
    assert!(hooks.iter().all(|(index, _)| *index > last_end), "{events}");
}

#[test]
fn updates_deduplicate_mirrors_and_failure_preserves_the_current_generation() {
    let f = Fixture::new();
    success(f.binit(&["init"]));
    let (work, remote) = repository(&f, "one");
    f.declare(&[
        line(&remote, "first", false),
        line(&remote, "second", false),
    ]);
    wrapped_git(&f);
    success(f.binit(&["sync", "--jobs", "2"]));
    let initial = f.current();
    update(&work, &remote, "one");
    fs::write(f.evidence.join("events"), "").unwrap();
    success(f.binit(&["update", "--check", "--jobs", "2"]));
    assert_eq!(f.current(), initial);
    let events = fs::read_to_string(f.evidence.join("events")).unwrap();
    assert_eq!(
        events
            .lines()
            .filter(|line| line.starts_with("start:"))
            .count(),
        2,
        "{events}"
    );
    success(f.binit(&["update", "--jobs", "2"]));
    assert_ne!(f.current(), initial);
    let stable = f.current();
    fs::rename(&remote, f.directory.path().join("unavailable.git")).unwrap();
    assert!(!f.binit(&["update", "--jobs", "2"]).status.success());
    assert_eq!(f.current(), stable);
    success(f.binit(&["init"]));
}

#[test]
fn invalid_job_counts_fail_before_any_git_command() {
    let f = Fixture::new();
    success(f.binit(&["init"]));
    f.fake(
        "git",
        "#!/bin/sh\nprintf called >> \"$BINIT_FIXTURE_EVIDENCE/called\"\nexit 1\n",
    );
    for args in [
        vec!["sync", "--jobs", "0"],
        vec!["sync", "--jobs", "9"],
        vec!["update", "--jobs", "bad"],
    ] {
        assert!(!f.binit(&args).status.success());
    }
    assert!(!f.evidence.join("called").exists());
}

#[test]
fn ssh_acquisitions_remain_foreground_and_are_serialized() {
    let f = Fixture::new();
    success(f.binit(&["init"]));
    f.declare(&[
        vec!["ssh://git@example.invalid/one".into(), "id=one".into()],
        vec!["git@example.invalid:two".into(), "id=two".into()],
    ]);
    f.fake(
        "git",
        r##"#!/bin/sh
for arg do
  case "$arg" in ssh://*|git@*:*) exec ssh-fixture ;; esac
done
exec /usr/bin/git "$@"
"##,
    );
    f.fake(
        "ssh-fixture",
        r##"#!/bin/sh
printf 'start:%s\n' "$(ps -o pgid= -p $$ | tr -d ' ')" >> "$BINIT_FIXTURE_EVIDENCE/ssh-events"
sleep 0.25
printf 'end\n' >> "$BINIT_FIXTURE_EVIDENCE/ssh-events"
exit 1
"##,
    );
    assert!(!f.binit(&["sync", "--jobs", "2"]).status.success());
    let events = fs::read_to_string(f.evidence.join("ssh-events")).unwrap();
    let pgid = unsafe { libc::getpgrp() };
    assert_eq!(
        events.lines().collect::<Vec<_>>(),
        [
            format!("start:{pgid}"),
            "end".to_owned(),
            format!("start:{pgid}"),
            "end".to_owned(),
        ],
        "{events}"
    );
}

#[test]
fn file_globs_copy_sorted_local_and_git_files_and_reject_unsafe_selections() {
    use std::os::unix::fs::symlink;
    let f = Fixture::new();
    success(f.binit(&["init"]));
    let local = f.directory.path().join("local");
    fs::create_dir(&local).unwrap();
    fs::create_dir(local.join("parts")).unwrap();
    fs::write(local.join("parts/b.bash"), "B=local\n").unwrap();
    fs::write(local.join("parts/a.bash"), "A=local\n").unwrap();
    let (work, remote) = repository(&f, "glob");
    fs::create_dir(work.join("parts")).unwrap();
    fs::write(work.join("parts/b.bash"), "B=git\n").unwrap();
    fs::write(work.join("parts/a.bash"), "A=git\n").unwrap();
    git(&work, &["add", "parts"]);
    git(&work, &["commit", "--quiet", "-m", "parts"]);
    git(
        &work,
        &[
            "push",
            "--quiet",
            remote.to_str().unwrap(),
            "HEAD:refs/heads/main",
        ],
    );
    let local_source = local.to_str().unwrap().to_owned();
    let remote_source = format!("file://{}", remote.display());
    f.declare(&[
        vec![
            local_source.clone(),
            "id=local".into(),
            "files=parts/*.bash".into(),
        ],
        vec![
            remote_source.clone(),
            "id=gitglob".into(),
            "files=parts/?.bash".into(),
        ],
    ]);
    success(f.binit(&["sync", "--jobs", "2"]));
    let current = f.current();
    let generation = f.state.join("generations").join(current.trim());
    for (id, expected) in [("local", "local"), ("gitglob", "git")] {
        let root = generation.join("plugins").join(id);
        let entry = fs::read_to_string(root.join("entry.bash")).unwrap();
        assert!(entry.find("a.bash").unwrap() < entry.find("b.bash").unwrap());
        assert_eq!(
            fs::read_to_string(root.join("files/parts/a.bash")).unwrap(),
            format!("A={expected}\n")
        );
    }
    symlink("a.bash", local.join("parts/link.bash")).unwrap();
    for selection in [
        "files=parts/*.bash",
        "files=parts/no*.bash",
        "files=../*.bash",
        "files=parts/a.bash,parts/a.bash",
    ] {
        f.declare(&[vec![
            local_source.clone(),
            "id=local".into(),
            selection.into(),
        ]]);
        assert!(!f.binit(&["sync"]).status.success(), "{selection}");
        assert_eq!(f.current(), current);
    }
    symlink("a.bash", work.join("parts/link.bash")).unwrap();
    git(&work, &["add", "parts/link.bash"]);
    git(&work, &["commit", "--quiet", "-m", "symlink"]);
    git(
        &work,
        &[
            "push",
            "--quiet",
            remote.to_str().unwrap(),
            "HEAD:refs/heads/main",
        ],
    );
    for selection in [
        "files=parts/*.bash",
        "files=parts/no*.bash",
        "files=parts/a.bash,parts/?.bash",
    ] {
        f.declare(&[vec![
            remote_source.clone(),
            "id=gitglob".into(),
            "ver=main".into(),
            selection.into(),
        ]]);
        assert!(!f.binit(&["sync"]).status.success(), "{selection}");
        assert_eq!(f.current(), current);
    }
}

#[test]
fn release_discovery_uses_bounded_metadata_and_lists_matching_assets() {
    let f = Fixture::new();
    f.fake("curl", "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$BINIT_FIXTURE_EVIDENCE/args\"\nprintf '%s' \"$BINIT_FIXTURE_JSON\"\n");
    let digest = "a".repeat(64);
    let body = format!(
        r#"{{"tag_name":"v1.2.3","assets":[{{"name":"tool-linux-amd64.tar.gz","size":123,"digest":"sha256:{digest}"}},{{"name":"tool-darwin-arm64.zip","size":456,"digest":null}}]}}"#
    );
    let mut command = Command::new(env!("CARGO_BIN_EXE_binit"));
    command
        .args([
            "releases",
            "example/tool",
            "--ver",
            "v1.2.3",
            "--asset",
            "*linux*",
        ])
        .env("BINIT_FIXTURE_JSON", body);
    f.env(&mut command);
    let output = success(command.output().unwrap());
    assert!(output.contains("release  example/tool v1.2.3"), "{output}");
    assert!(
        output.contains("tool-linux-amd64.tar.gz\t123 bytes\tlinux/x86_64"),
        "{output}"
    );
    assert!(output.contains(&digest), "{output}");
    assert!(!output.contains("darwin"));
    let arguments = fs::read_to_string(f.evidence.join("args")).unwrap();
    assert!(arguments.contains("https://api.github.com/repos/example/tool/releases/tags/v1.2.3"));
    assert!(arguments.contains("=https"));
}

#[test]
fn release_discovery_rejects_bad_metadata_and_invalid_arguments() {
    let f = Fixture::new();
    f.fake("curl", "#!/bin/sh\nprintf '%s' \"$BINIT_FIXTURE_JSON\"\n");
    for body in [
        "not json",
        "{\"tag_name\":\"v1\\u001b\",\"assets\":[]}",
        "{\"tag_name\":\"v1\",\"assets\":[{\"name\":\"bad\\u001b[2J\",\"size\":1}]}",
        "{\"tag_name\":\"v1\",\"assets\":[{\"name\":\"evil\\u202egz.rat\",\"size\":1}]}",
    ] {
        let mut command = Command::new(env!("CARGO_BIN_EXE_binit"));
        command
            .args(["releases", "example/repo"])
            .env("BINIT_FIXTURE_JSON", body);
        f.env(&mut command);
        assert!(!command.output().unwrap().status.success(), "{body}");
    }
    for args in [
        vec!["releases", "../repo"],
        vec!["releases", "example/repo", "--ver"],
        vec!["releases", "example/repo", "--asset", "*", "--asset", "*"],
    ] {
        let mut command = Command::new(env!("CARGO_BIN_EXE_binit"));
        command.args(args);
        f.env(&mut command);
        assert!(!command.output().unwrap().status.success());
    }
}

#[test]
fn release_platform_filter_lists_all_matching_assets() {
    let f = Fixture::new();
    f.fake("curl", "#!/bin/sh\nprintf '%s' \"$BINIT_FIXTURE_JSON\"\n");
    let body = r#"{"tag_name":"v1","assets":[{"name":"a-linux-amd64.tar.gz","size":1},{"name":"b-linux-x86_64.zip","size":2},{"name":"c-linux-arm64.zip","size":3}]}"#;
    let mut command = Command::new(env!("CARGO_BIN_EXE_binit"));
    command
        .args(["releases", "example/tool", "--platform", "linux/x86_64"])
        .env("BINIT_FIXTURE_JSON", body);
    f.env(&mut command);
    let output = success(command.output().unwrap());
    assert!(output.contains("a-linux-amd64"));
    assert!(output.contains("b-linux-x86_64"));
    assert!(!output.contains("c-linux-arm64"));
}
