use crate::Result;
use std::ffi::{CString, OsStr, OsString};
use std::io::{self, Read, Seek, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{ChildStdout, Command, ExitStatus, Stdio};
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

const PROMPT_OUTPUT_LIMIT: usize = 512;
const PROMPT_TIMEOUT: Duration = Duration::from_secs(1);
const HIGHLIGHT_CONTEXT_LIMIT: usize = 16_384;
const HIGHLIGHT_TOKEN_LIMIT: usize = 24;

#[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
fn prompt_permissions(command: &mut Command) {
    #[cfg(target_arch = "x86_64")]
    let architecture = 0xc000_003e;
    #[cfg(target_arch = "aarch64")]
    let architecture = 0xc000_00b7;
    let load = libc::BPF_LD | libc::BPF_W | libc::BPF_ABS;
    let eq = libc::BPF_JMP | libc::BPF_JEQ | libc::BPF_K;
    let ret = libc::BPF_RET | libc::BPF_K;
    let denied = libc::SECCOMP_RET_ERRNO | libc::EPERM as u32;
    let instruction = |code: u32, k, jt, jf| libc::sock_filter {
        code: code as u16,
        jt,
        jf,
        k,
    };
    let mut filter = vec![
        instruction(
            load,
            std::mem::offset_of!(libc::seccomp_data, arch) as u32,
            0,
            0,
        ),
        instruction(eq, architecture, 1, 0),
        instruction(ret, libc::SECCOMP_RET_KILL_PROCESS, 0, 0),
        instruction(
            load,
            std::mem::offset_of!(libc::seccomp_data, nr) as u32,
            0,
            0,
        ),
    ];
    #[cfg(target_arch = "x86_64")]
    filter.extend([
        instruction(
            libc::BPF_JMP | libc::BPF_JSET | libc::BPF_K,
            0x4000_0000,
            0,
            1,
        ),
        instruction(ret, denied, 0, 0),
    ]);
    for syscall in [
        libc::SYS_socket,
        libc::SYS_connect,
        libc::SYS_io_uring_setup,
        libc::SYS_setsid,
        libc::SYS_setpgid,
    ] {
        filter.extend([
            instruction(eq, syscall as u32, 0, 1),
            instruction(ret, denied, 0, 0),
        ]);
    }
    filter.push(instruction(ret, libc::SECCOMP_RET_ALLOW, 0, 0));
    // Apply after Command assigns the group and before exec. The kernel copies the filter.
    unsafe {
        command.pre_exec(move || {
            let program = libc::sock_fprog {
                len: filter.len() as u16,
                filter: filter.as_ptr().cast_mut(),
            };
            if libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1 as libc::c_ulong, 0, 0, 0) == -1
                || libc::prctl(
                    libc::PR_SET_SECCOMP,
                    libc::SECCOMP_MODE_FILTER as libc::c_ulong,
                    &program,
                    0,
                    0,
                ) == -1
            {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        });
    }
}

#[cfg(not(any(target_arch = "x86_64", target_arch = "aarch64")))]
fn prompt_permissions(command: &mut Command) {
    unsafe {
        command.pre_exec(|| Err(io::Error::from(io::ErrorKind::Unsupported)));
    }
}

fn prompt_output(
    mut output: ChildStdout,
    pid: u32,
    started: Instant,
) -> io::Result<Option<Vec<u8>>> {
    let fd = output.as_raw_fd();
    if unsafe { libc::fcntl(fd, libc::F_SETFL, libc::O_NONBLOCK) } == -1 {
        return Err(io::Error::last_os_error());
    }
    let mut bytes = Vec::new();
    let mut buffer = [0; PROMPT_OUTPUT_LIMIT + 1];
    let mut eof = false;
    let mut exited = false;
    while started.elapsed() < PROMPT_TIMEOUT {
        let mut poll = [
            libc::pollfd {
                fd: libc::STDIN_FILENO,
                events: libc::POLLIN,
                revents: 0,
            },
            libc::pollfd {
                fd: if eof { -1 } else { fd },
                events: libc::POLLIN,
                revents: 0,
            },
        ];
        if unsafe { libc::poll(poll.as_mut_ptr(), 2, 10) } < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(error);
        }
        if poll[0].revents != 0 {
            return Ok(None);
        }
        if poll[1].revents != 0 {
            match output.read(&mut buffer) {
                Ok(0) => eof = true,
                Ok(count) if bytes.len() + count <= PROMPT_OUTPUT_LIMIT => {
                    bytes.extend_from_slice(&buffer[..count]);
                }
                Ok(_) => return Ok(None),
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::Interrupted | io::ErrorKind::WouldBlock
                    ) => {}
                Err(error) => return Err(error),
            }
        }
        if !exited {
            // Keep the leader's PID reserved until all group members are stopped.
            let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
            if unsafe {
                libc::waitid(
                    libc::P_PID,
                    pid,
                    &mut info,
                    libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
                )
            } < 0
            {
                let error = io::Error::last_os_error();
                if error.kind() == io::ErrorKind::Interrupted {
                    continue;
                }
                return Err(error);
            }
            exited = unsafe { info.si_pid() } != 0;
        }
        if eof && exited {
            return Ok(Some(bytes));
        }
    }
    Ok(None)
}

pub(super) fn run_prompt_worker(arguments: &[OsString]) -> Result<()> {
    let Some((program, arguments)) = arguments.split_first() else {
        return Ok(());
    };
    if !Path::new(program).is_absolute() {
        return Ok(());
    }
    let started = Instant::now();
    let mut command = Command::new(program);
    command
        .args(arguments)
        .env_remove("BASH_ENV")
        .env_remove("ENV")
        .env_remove("SHELLOPTS")
        .env_remove("BASHOPTS")
        .stdin(Stdio::null());
    run_local_worker(command, started)
}

fn run_local_worker(mut command: Command, started: Instant) -> Result<()> {
    command
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .process_group(0);
    prompt_permissions(&mut command);
    let mut child = command.spawn()?;
    let mut group = OwnedGroup(Some(child.id() as libc::pid_t));
    let result = prompt_output(
        child.stdout.take().expect("piped stdout"),
        child.id(),
        started,
    );
    group.stop();
    let status = child.wait()?;
    if let Some(bytes) = result?
        && status.success()
        && !bytes.contains(&0)
    {
        io::stdout().write_all(&bytes)?;
    }
    Ok(())
}

fn receive_worker_frame(
    input: &mut UnixStream,
    started: Instant,
    mut bytes: &mut [u8],
) -> io::Result<()> {
    while !bytes.is_empty() {
        let remaining = PROMPT_TIMEOUT.saturating_sub(started.elapsed());
        if remaining.is_zero() {
            return Err(io::ErrorKind::TimedOut.into());
        }
        input.set_read_timeout(Some(remaining))?;
        match input.read(bytes) {
            Ok(0) => return Err(io::ErrorKind::UnexpectedEof.into()),
            Ok(count) => bytes = &mut bytes[count..],
            Err(error) if error.kind() == io::ErrorKind::Interrupted => {}
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

pub(super) fn run_highlight_worker() -> Result<()> {
    let started = Instant::now();
    let socket = unsafe { libc::fcntl(libc::STDIN_FILENO, libc::F_DUPFD_CLOEXEC, 3) };
    if socket < 0 {
        return Err(io::Error::last_os_error().into());
    }
    let mut input = UnixStream::from(unsafe { OwnedFd::from_raw_fd(socket) });
    let mut header = [0; 2];
    receive_worker_frame(&mut input, started, &mut header)?;
    let size = usize::from(u16::from_le_bytes(header));
    if size == 0 || size > HIGHLIGHT_CONTEXT_LIMIT {
        return Ok(());
    }
    let mut bytes = vec![0; size];
    receive_worker_frame(&mut input, started, &mut bytes)?;
    let descriptor = unsafe { libc::memfd_create(c"binit-highlight".as_ptr(), libc::MFD_CLOEXEC) };
    if descriptor < 0 {
        return Err(io::Error::last_os_error().into());
    }
    let mut context = unsafe { std::fs::File::from_raw_fd(descriptor) };
    context.write_all(&bytes)?;
    context.rewind()?;
    let mut command = Command::new(std::env::current_exe()?);
    command
        .arg("__highlight-paths")
        .env_clear()
        .stdin(Stdio::from(context));
    run_local_worker(command, started)
}

fn accessible_executable(path: &Path) -> bool {
    if !path.is_file() {
        return false;
    }
    CString::new(path.as_os_str().as_bytes()).is_ok_and(|name| unsafe {
        libc::faccessat(libc::AT_FDCWD, name.as_ptr(), libc::X_OK, libc::AT_EACCESS) == 0
    })
}

pub(super) fn resolve_highlight_paths() -> Result<()> {
    let mut frame = Vec::new();
    io::stdin()
        .lock()
        .take(HIGHLIGHT_CONTEXT_LIMIT as u64 + 1)
        .read_to_end(&mut frame)?;
    if frame.len() > HIGHLIGHT_CONTEXT_LIMIT || frame.last() != Some(&0) {
        return Ok(());
    }
    let mut fields = frame[..frame.len() - 1].split(|&byte| byte == 0);
    let (Some(search), Some(path), Some(home)) = (fields.next(), fields.next(), fields.next())
    else {
        return Ok(());
    };
    let home = Path::new(OsStr::from_bytes(home));
    let mut results = Vec::new();
    for item in fields {
        let Some((&kind, name)) = item.split_first() else {
            return Ok(());
        };
        if results.len() == HIGHLIGHT_TOKEN_LIMIT || name.is_empty() {
            return Ok(());
        }
        let literal = Path::new(OsStr::from_bytes(name));
        let expanded;
        let target = if let Some(rest) = name.strip_prefix(b"~/") {
            if !home.is_absolute() {
                results.push(b'?');
                continue;
            }
            expanded = home.join(OsStr::from_bytes(rest));
            expanded.as_path()
        } else {
            literal
        };
        let result = match kind {
            b'c' => {
                let found = if name.contains(&b'/') {
                    accessible_executable(target)
                } else {
                    search == b"1"
                        && path.split(|&byte| byte == b':').any(|part| {
                            accessible_executable(&Path::new(OsStr::from_bytes(part)).join(literal))
                        })
                };
                if found { b'e' } else { b'm' }
            }
            b'p' => match target.try_exists() {
                Ok(true) => b'p',
                Ok(false) => b'n',
                Err(_) => b'?',
            },
            _ => return Ok(()),
        };
        results.push(result);
    }
    io::stdout().write_all(&results)?;
    Ok(())
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
