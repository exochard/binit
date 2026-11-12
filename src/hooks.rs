use super::*;
use std::io::Read;
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::sync::atomic::{AtomicI32, Ordering};
use std::time::{Duration, Instant};

const OUTPUT_LIMIT: usize = 32 * 1024;
static SIGNAL_LOCK: Mutex<()> = Mutex::new(());
static INTERRUPTED: AtomicI32 = AtomicI32::new(0);

extern "C" fn capture_signal(signal: libc::c_int) {
    INTERRUPTED.store(signal, Ordering::Relaxed);
}

struct SignalGuard {
    int: libc::sigaction,
    term: libc::sigaction,
}

impl SignalGuard {
    fn install() -> Result<Self> {
        // sigaction and the lock-free integer store are signal-safe on the
        // supported Linux targets. The manager serializes hook executions.
        unsafe {
            let mut action: libc::sigaction = std::mem::zeroed();
            let mut int: libc::sigaction = std::mem::zeroed();
            let mut term: libc::sigaction = std::mem::zeroed();
            action.sa_sigaction = capture_signal as *const () as usize;
            libc::sigemptyset(&mut action.sa_mask);
            INTERRUPTED.store(0, Ordering::Relaxed);
            if libc::sigaction(libc::SIGINT, &action, &mut int) != 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            if libc::sigaction(libc::SIGTERM, &action, &mut term) != 0 {
                libc::sigaction(libc::SIGINT, &int, std::ptr::null_mut());
                return Err(std::io::Error::last_os_error().into());
            }
            Ok(Self { int, term })
        }
    }
}

impl Drop for SignalGuard {
    fn drop(&mut self) {
        unsafe {
            libc::sigaction(libc::SIGINT, &self.int, std::ptr::null_mut());
            libc::sigaction(libc::SIGTERM, &self.term, std::ptr::null_mut());
        }
    }
}

struct HookGroup(Option<libc::pid_t>);

impl HookGroup {
    fn stop(&mut self) {
        if let Some(group) = self.0.take() {
            unsafe {
                libc::kill(-group, libc::SIGKILL);
            }
        }
    }
}

impl Drop for HookGroup {
    fn drop(&mut self) {
        self.stop();
    }
}

fn selected_bash(value: Option<std::ffi::OsString>) -> Result<std::ffi::OsString> {
    match value {
        Some(value) if Path::new(&value).is_absolute() => Ok(value),
        Some(_) => Err("BINIT_BASH must be an absolute executable path".into()),
        None => Ok("/bin/bash".into()),
    }
}

fn drain(mut input: impl Read + Send + 'static) -> std::sync::mpsc::Receiver<Vec<u8>> {
    let (sender, receiver) = std::sync::mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut output = Vec::new();
        let mut buf = [0; 4096];
        while let Ok(n) = input.read(&mut buf) {
            if n == 0 {
                break;
            }
            let remaining = OUTPUT_LIMIT.saturating_sub(output.len());
            output.extend_from_slice(&buf[..n.min(remaining)]);
        }
        let _ = sender.send(output);
    });
    receiver
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
    let _lock = SIGNAL_LOCK
        .lock()
        .map_err(|_| "hook signal lock is poisoned")?;
    let signals = SignalGuard::install()?;
    let parent = unsafe { libc::getpid() };
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
        )
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    // The child is owned by this manager; terminate it if the manager exits.
    unsafe {
        command.pre_exec(move || {
            if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            if libc::getppid() != parent {
                return Err(std::io::Error::from_raw_os_error(libc::ESRCH));
            }
            Ok(())
        });
    }
    let mut child = command
        .spawn()
        .map_err(|e| format!("{id}: {event} could not start: {e}"))?;
    let pgid = child.id() as libc::pid_t;
    let mut group = HookGroup(Some(pgid));
    let out = drain(child.stdout.take().ok_or("hook stdout unavailable")?);
    let err = drain(child.stderr.take().ok_or("hook stderr unavailable")?);
    let started = Instant::now();
    let (status, timed_out, interrupted) = loop {
        let signal = INTERRUPTED.load(Ordering::Relaxed);
        if signal != 0 {
            group.stop();
            break (child.wait()?, false, signal);
        }
        if let Some(status) = child.try_wait()? {
            break (status, false, 0);
        }
        if started.elapsed() >= timeout {
            group.stop();
            break (child.wait()?, true, 0);
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    // Reap any ordinary background children from this hook's process group.
    group.stop();
    // A process that deliberately leaves the group can retain a pipe. It must
    // not prevent the manager from returning after the owned group is stopped.
    let stdout = out
        .recv_timeout(Duration::from_millis(200))
        .unwrap_or_default();
    let stderr = err
        .recv_timeout(Duration::from_millis(200))
        .unwrap_or_default();
    // Restore the manager's handlers before the final check: a later signal
    // then has its ordinary effect instead of being swallowed after success.
    drop(signals);
    let signal = interrupted.max(INTERRUPTED.load(Ordering::Relaxed));
    if signal != 0 {
        return Err(format!("{id}: {event} interrupted by signal {signal}").into());
    }
    if timed_out {
        return Err(format!("{id}: {event} timed out after {}s", timeout.as_secs()).into());
    }
    if !status.success() {
        let output = if stderr.is_empty() { &stdout } else { &stderr };
        return Err(format!(
            "{id}: {event} failed ({status}): {}",
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
