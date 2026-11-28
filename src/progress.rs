use crate::terminal_ui;
use std::io::{self, IsTerminal, Write};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::{Arc, Mutex};

use std::thread;
use std::time::Duration;

#[derive(Clone, Copy)]
enum Activity {
    Sample { current: usize, total: usize },
    Phase(&'static str),
}

pub(super) struct Progress {
    stop: mpsc::Sender<()>,
    thread: Option<thread::JoinHandle<()>>,
    activity: Arc<Mutex<Option<Activity>>>,
}

impl Progress {
    pub(super) fn start(label: &'static str) -> Option<Self> {
        if !terminal_ui::ansi(io::stdout().is_terminal() && io::stderr().is_terminal())
            || std::env::var_os("BINIT_PROGRESS").is_some_and(|value| value == "0")
        {
            return None;
        }
        let (stop, receive) = mpsc::channel();
        let activity = Arc::new(Mutex::new(None::<Activity>));
        let displayed = Arc::clone(&activity);
        let color = terminal_ui::color(true);
        let thread = thread::Builder::new()
            .spawn(move || {
                if receive.recv_timeout(Duration::from_millis(150))
                    != Err(RecvTimeoutError::Timeout)
                {
                    return;
                }
                for frame in ['|', '/', '-', '\\'].into_iter().cycle() {
                    let detail = displayed.lock().ok().and_then(|activity| *activity);
                    let mut stderr = io::stderr().lock();
                    let text = match detail {
                        Some(Activity::Sample { current, total }) => {
                            format!("{label}  sample {current}/{total}")
                        }
                        Some(Activity::Phase(phase)) => format!("{label}  {phase}"),
                        None => label.to_owned(),
                    };
                    let limit = terminal_ui::width(true, libc::STDERR_FILENO).saturating_sub(3);
                    let end = text
                        .char_indices()
                        .nth(limit)
                        .map_or(text.len(), |(index, _)| index);
                    let (accent, reset) = if color {
                        ("\x1b[36m", "\x1b[0m")
                    } else {
                        ("", "")
                    };
                    if write!(stderr, "\r\x1b[2K{accent}{frame}{reset} {}", &text[..end])
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
        Some(Self {
            stop,
            thread: Some(thread),
            activity,
        })
    }

    pub(super) fn sample(&self, current: usize, total: usize) {
        if let Ok(mut activity) = self.activity.lock() {
            *activity = Some(Activity::Sample { current, total });
        }
    }

    pub(super) fn phase(&self, phase: &'static str) {
        if let Ok(mut activity) = self.activity.lock() {
            *activity = Some(Activity::Phase(phase));
        }
    }

    pub(super) fn finish(self) {
        drop(self);
    }
}

impl Drop for Progress {
    fn drop(&mut self) {
        let _ = self.stop.send(());
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}
