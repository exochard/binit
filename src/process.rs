use crate::Result;
use std::io::Read;
use std::os::unix::process::CommandExt;
use std::process::{Command, ExitStatus, Stdio};
use std::sync::Mutex;
use std::sync::atomic::{AtomicI32, Ordering};
use std::time::{Duration, Instant};

pub(super) const OUTPUT_LIMIT: usize = 32 * 1024;
static SIGNAL_LOCK: Mutex<()> = Mutex::new(());
static INTERRUPTED: AtomicI32 = AtomicI32::new(0);
extern "C" fn capture_signal(signal: libc::c_int) {
    INTERRUPTED.store(signal, Ordering::Relaxed);
}

#[derive(Default)]
pub(super) struct Capture {
    pub bytes: Vec<u8>,
    pub truncated: bool,
    pub complete: bool,
}

pub(super) struct Output {
    pub status: ExitStatus,
    pub timed_out: bool,
    pub interrupted: i32,
    pub wall_ms: f64,
    pub stdout: Capture,
    pub stderr: Capture,
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

struct OwnedGroup(Option<libc::pid_t>);

impl OwnedGroup {
    fn stop(&mut self) {
        if let Some(group) = self.0.take() {
            unsafe {
                libc::kill(-group, libc::SIGKILL);
            }
        }
    }
}

impl Drop for OwnedGroup {
    fn drop(&mut self) {
        self.stop();
    }
}

fn drain(mut input: impl Read + Send + 'static) -> std::sync::mpsc::Receiver<Capture> {
    let (sender, receiver) = std::sync::mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut output = Vec::new();
        let mut truncated = false;
        let mut buf = [0; 4096];
        let complete = loop {
            let n = match input.read(&mut buf) {
                Ok(n) => n,
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(_) => break false,
            };
            if n == 0 {
                break true;
            }
            let remaining = OUTPUT_LIMIT.saturating_sub(output.len());
            truncated |= n > remaining;
            output.extend_from_slice(&buf[..n.min(remaining)]);
        };
        let _ = sender.send(Capture {
            bytes: output,
            truncated,
            complete,
        });
    });
    receiver
}

pub(super) fn run(command: &mut Command, timeout: Duration) -> Result<Output> {
    let _lock = SIGNAL_LOCK
        .lock()
        .map_err(|_| "process signal lock is poisoned")?;
    let signals = SignalGuard::install()?;
    let parent = unsafe { libc::getpid() };
    command
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
    let wall_started = Instant::now();
    let mut child = command.spawn()?;
    let pgid = child.id() as libc::pid_t;
    let mut group = OwnedGroup(Some(pgid));
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
    // Stop ordinary background children in this owned process group.
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
    Ok(Output {
        status,
        timed_out,
        interrupted: signal,
        wall_ms: wall_started.elapsed().as_secs_f64() * 1000.0,
        stdout,
        stderr,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn failed_capture_is_never_reported_complete() {
        struct BrokenPipe;
        impl Read for BrokenPipe {
            fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
                Err(std::io::Error::other("capture failed"))
            }
        }
        let capture = drain(BrokenPipe)
            .recv_timeout(Duration::from_secs(1))
            .unwrap();
        assert!(!capture.complete);
    }
}
