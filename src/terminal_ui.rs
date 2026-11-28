pub(super) fn ansi(terminal: bool) -> bool {
    terminal && std::env::var_os("TERM").is_some_and(|term| !term.is_empty() && term != "dumb")
}

pub(super) fn color(terminal: bool) -> bool {
    ansi(terminal) && std::env::var_os("NO_COLOR").is_none_or(|value| value.is_empty())
}

pub(super) fn width(terminal: bool, fd: std::os::fd::RawFd) -> usize {
    if terminal {
        let mut size = std::mem::MaybeUninit::<libc::winsize>::uninit();
        // A successful ioctl initializes the window size.
        if unsafe { libc::ioctl(fd, libc::TIOCGWINSZ, size.as_mut_ptr()) } == 0 {
            let columns = unsafe { size.assume_init() }.ws_col;
            if columns != 0 {
                return columns.into();
            }
        }
    }
    std::env::var("COLUMNS")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|width| *width > 0)
        .unwrap_or(80)
}

pub(super) fn styled(text: &str, code: &str, enabled: bool) -> String {
    if enabled {
        format!("\x1b[{code}m{text}\x1b[0m")
    } else {
        text.into()
    }
}
