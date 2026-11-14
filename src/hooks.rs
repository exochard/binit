use super::*;
#[cfg(test)]
use crate::process::OUTPUT_LIMIT;
use std::process::Command;
#[cfg(test)]
use std::process::Stdio;
use std::time::Duration;
#[cfg(test)]
use std::time::Instant;

fn selected_bash(value: Option<std::ffi::OsString>) -> Result<std::ffi::OsString> {
    match value {
        Some(value) if Path::new(&value).is_absolute() => Ok(value),
        Some(_) => Err("BINIT_BASH must be an absolute executable path".into()),
        None => Ok("/bin/bash".into()),
    }
}

pub(super) fn run(script: &str, directory: &Path, id: &str, event: &str) -> Result<()> {
    run_with_timeout(script, directory, id, event, Duration::from_secs(120))
}

fn run_with_timeout(
    script: &str,
    directory: &Path,
    id: &str,
    event: &str,
    timeout: Duration,
) -> Result<()> {
    let bash = selected_bash(std::env::var_os("BINIT_BASH"))?;
    let mut command = Command::new(bash);
    for (name, _) in
        std::env::vars_os().filter(|(name, _)| name.as_encoded_bytes().starts_with(b"GIT_"))
    {
        command.env_remove(name);
    }
    command
        .args(["--noprofile", "--norc", "-c", script])
        .current_dir(directory)
        .env("BINIT_PLUGIN_ID", id)
        .env("BINIT_HOOK_EVENT", event)
        .env_remove("BASH_ENV")
        .env_remove("ENV")
        .env("GIT_DIR", directory.join(".git"))
        .env("GIT_WORK_TREE", directory)
        .env(
            "GIT_CEILING_DIRECTORIES",
            directory.parent().ok_or("hook staging parent missing")?,
        );
    let result = crate::process::run(&mut command, timeout)
        .map_err(|error| format!("{id}: {event} could not start: {error}"))?;
    if result.interrupted != 0 {
        return Err(format!("{id}: {event} interrupted by signal {}", result.interrupted).into());
    }
    if result.timed_out {
        return Err(format!("{id}: {event} timed out after {}s", timeout.as_secs()).into());
    }
    if !result.status.success() {
        let output = if result.stderr.bytes.is_empty() {
            &result.stdout.bytes
        } else {
            &result.stderr.bytes
        };
        return Err(format!(
            "{id}: {event} failed ({}): {}",
            result.status,
            String::from_utf8_lossy(output)
        )
        .into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hook_runs_in_staging_and_reports_failures() {
        let dir = tempfile::tempdir().unwrap();
        run(
            "printf '%s:%s' \"$BINIT_PLUGIN_ID\" \"$BINIT_HOOK_EVENT\" > result",
            dir.path(),
            "sample",
            "atclone",
        )
        .unwrap();
        assert_eq!(
            fs::read_to_string(dir.path().join("result")).unwrap(),
            "sample:atclone"
        );
        assert!(
            run("printf bad >&2; exit 7", dir.path(), "sample", "atpull")
                .unwrap_err()
                .to_string()
                .contains("bad")
        );
    }

    #[test]
    fn timeout_stops_background_work() {
        let dir = tempfile::tempdir().unwrap();
        let err = run_with_timeout(
            "(sleep 1; touch escaped) & wait",
            dir.path(),
            "sample",
            "atclone",
            Duration::from_millis(100),
        )
        .unwrap_err();
        assert!(err.to_string().contains("timed out"));
        std::thread::sleep(Duration::from_millis(1100));
        assert!(!dir.path().join("escaped").exists());
    }

    #[test]
    fn failure_output_is_bounded() {
        let dir = tempfile::tempdir().unwrap();
        let err = run(
            "printf '%040000d' 0 >&2; exit 1",
            dir.path(),
            "sample",
            "atpull",
        )
        .unwrap_err()
        .to_string();
        assert!(err.len() < OUTPUT_LIMIT + 200);
    }

    #[test]
    fn interpreter_selection_rejects_relative_and_empty_paths() {
        assert!(selected_bash(Some("bash".into())).is_err());
        assert!(selected_bash(Some("".into())).is_err());
        assert_eq!(selected_bash(None).unwrap(), "/bin/bash");
        assert_eq!(
            selected_bash(Some("/path with spaces/bash".into())).unwrap(),
            "/path with spaces/bash"
        );
    }

    #[test]
    fn interrupt_child() {
        let Some(directory) = std::env::var_os("BINIT_HOOK_INTERRUPT_FIXTURE") else {
            return;
        };
        let error = run_with_timeout(
            "(sleep 1; touch escaped) & touch ready; wait",
            Path::new(&directory),
            "sample",
            "atclone",
            Duration::from_secs(5),
        )
        .unwrap_err();
        assert!(error.to_string().contains("interrupted by signal"));
    }

    #[test]
    fn interrupt_stops_owned_hook_group() {
        let dir = tempfile::tempdir().unwrap();
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "hooks::tests::interrupt_child", "--nocapture"])
            .env("BINIT_HOOK_INTERRUPT_FIXTURE", dir.path())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let started = Instant::now();
        while !dir.path().join("ready").exists() && started.elapsed() < Duration::from_secs(5) {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(dir.path().join("ready").exists(), "hook did not start");
        // This exact child was created by the test above.
        assert_eq!(
            unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGINT) },
            0
        );
        assert!(child.wait().unwrap().success());
        std::thread::sleep(Duration::from_millis(1100));
        assert!(!dir.path().join("escaped").exists());
    }
}
