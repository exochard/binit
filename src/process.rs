use crate::Result;
use std::io::Read;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::process::{Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::{Arc, Mutex};
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
    pub pid: u32,
    pub status: ExitStatus,
    pub timed_out: bool,
    pub interrupted: i32,
    pub wall_ms: f64,
    pub stdout: Capture,
    pub stderr: Capture,
    pub trace: Option<crate::trace::Capture>,
}

struct TracePipe {
    input: UnixStream,
    output: OwnedFd,
    finished: Arc<AtomicBool>,
}

struct TraceFinished(Arc<AtomicBool>);

impl Drop for TraceFinished {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Relaxed);
    }
}

struct TraceInput {
    stream: UnixStream,
    finished: Arc<AtomicBool>,
}

impl Read for TraceInput {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        loop {
            match self.stream.read(buffer) {
                Err(error)
                    if matches!(
                        error.kind(),
                        std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
                    ) =>
                {
                    if self.finished.load(Ordering::Relaxed) {
                        return Err(std::io::Error::other(
                            "trace writer outlived the owned process group",
                        ));
                    }
                }
                result => return result,
            }
        }
    }
}

pub(super) fn run_with_trace(mut command: Command, timeout: Duration) -> Result<Output> {
    let (input, output) = UnixStream::pair()?;
    input.set_read_timeout(Some(Duration::from_millis(20)))?;
    // Keep the writer above stdio without choosing a number that could collide
    // with Command's private exec-error descriptor.
    let duplicated = unsafe { libc::fcntl(output.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 3) };
    if duplicated < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let owned_output = unsafe { OwnedFd::from_raw_fd(duplicated) };
    drop(output);
    let output = owned_output;
    let writer = output.as_raw_fd();
    command.env("__binit_trace_fd", writer.to_string());
    unsafe {
        command.pre_exec(move || {
            let flags = libc::fcntl(writer, libc::F_GETFD);
            if flags < 0 || libc::fcntl(writer, libc::F_SETFD, flags & !libc::FD_CLOEXEC) < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    run_inner(
        &mut command,
        timeout,
        Some(TracePipe {
            input,
            output,
            finished: Arc::new(AtomicBool::new(false)),
        }),
    )
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

fn drain(
    mut input: impl Read + Send + 'static,
    limit: usize,
) -> std::sync::mpsc::Receiver<Capture> {
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
            let remaining = limit.saturating_sub(output.len());
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
    run_inner(command, timeout, None)
}

fn run_inner(command: &mut Command, timeout: Duration, trace: Option<TracePipe>) -> Result<Output> {
    let _lock = SIGNAL_LOCK
        .lock()
        .map_err(|_| "process signal lock is poisoned")?;
    let signals = SignalGuard::install()?;
    let parent = unsafe { libc::getpid() };
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // Private interactive fixtures must not acquire the caller's terminal.
    // A new session also supplies the process group owned by this manager.
    unsafe {
        command.pre_exec(move || {
            if libc::setsid() < 0 {
                return Err(std::io::Error::last_os_error());
            }
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
    // A fixture can redirect xtrace onto either stream. Diagnostic runs drain
    // them without retaining previews, even when the trace channel is lost.
    let output_limit = if trace.is_some() { 0 } else { OUTPUT_LIMIT };
    let out = drain(
        child.stdout.take().ok_or("hook stdout unavailable")?,
        output_limit,
    );
    let err = drain(
        child.stderr.take().ok_or("hook stderr unavailable")?,
        output_limit,
    );
    let trace = trace.map(|pipe| {
        let guard = TraceFinished(pipe.finished.clone());
        let input = TraceInput {
            stream: pipe.input,
            finished: pipe.finished,
        };
        drop(pipe.output);
        (crate::trace::drain(input), guard)
    });
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
    if let Some((_, guard)) = &trace {
        guard.0.store(true, Ordering::Relaxed);
    }
    // A process that deliberately leaves the group can retain a pipe. It must
    // not prevent the manager from returning after the owned group is stopped.
    let stdout = out
        .recv_timeout(Duration::from_millis(200))
        .unwrap_or_default();
    let stderr = err
        .recv_timeout(Duration::from_millis(200))
        .unwrap_or_default();
    let trace = trace.map(|(receiver, _guard)| {
        receiver
            .recv_timeout(Duration::from_millis(200))
            .unwrap_or_default()
    });
    // Restore the manager's handlers before the final check: a later signal
    // then has its ordinary effect instead of being swallowed after success.
    drop(signals);
    let signal = interrupted.max(INTERRUPTED.load(Ordering::Relaxed));
    Ok(Output {
        pid: pgid as u32,
        status,
        timed_out,
        interrupted: signal,
        wall_ms: wall_started.elapsed().as_secs_f64() * 1000.0,
        stdout,
        stderr,
        trace,
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
        let capture = drain(BrokenPipe, OUTPUT_LIMIT)
            .recv_timeout(Duration::from_secs(1))
            .unwrap();
        assert!(!capture.complete);
    }
}
