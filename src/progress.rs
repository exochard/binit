use std::io::{self, IsTerminal, Write};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::thread;
use std::time::Duration;

pub(super) struct Progress {
    stop: mpsc::Sender<()>,
    thread: thread::JoinHandle<()>,
}

impl Progress {
    pub(super) fn start(label: &'static str) -> Option<Self> {
        if !io::stdout().is_terminal()
            || !io::stderr().is_terminal()
            || std::env::var_os("TERM").is_none_or(|term| term == "dumb" || term.is_empty())
            || std::env::var_os("BINIT_PROGRESS").is_some_and(|value| value == "0")
        {
            return None;
        }
        let (stop, receive) = mpsc::channel();
        let thread = thread::Builder::new()
            .spawn(move || {
                if receive.recv_timeout(Duration::from_millis(150))
                    != Err(RecvTimeoutError::Timeout)
                {
                    return;
                }
                for frame in ['|', '/', '-', '\\'].into_iter().cycle() {
                    let mut stderr = io::stderr().lock();
                    if write!(stderr, "\r\x1b[2K{frame} {label}")
                        .and_then(|()| stderr.flush())
                        .is_err()
                    {
                        break;
                    }
                    drop(stderr);
                    if receive.recv_timeout(Duration::from_millis(100))
                        != Err(RecvTimeoutError::Timeout)
                    {
                        break;
                    }
                }
                let mut stderr = io::stderr().lock();
                let _ = stderr.write_all(b"\r\x1b[2K").and_then(|()| stderr.flush());
            })
            .ok()?;
        Some(Self { stop, thread })
    }

    pub(super) fn finish(self) {
        let _ = self.stop.send(());
        let _ = self.thread.join();
    }
}
