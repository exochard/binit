use serde::Serialize;
use std::collections::BTreeMap;
use std::io::{self, Read};
use std::path::Path;
use std::sync::mpsc::{self, Receiver};

const MAX_RECORDS: usize = 8192;
const MAX_PREFIX: usize = 4096;
const MAX_INGRESS: u64 = 64 * 1024 * 1024;
const MAX_DEPTH: usize = 64;
const MARKER: &[u8] = b"__BINIT_DIAG__";

#[derive(Clone, Debug)]
struct Record {
    timestamp: u64,
    pid: u32,
    depth: usize,
    function: String,
    source: String,
    line: u32,
    phase: String,
    control_change: bool,
}

// Match only fixed control words while discarding command bytes. No argument
// text or argument-derived identifier survives this streaming classifier.
#[derive(Default)]
struct ControlScan {
    mode: u8,
    length: usize,
    matches: u32,
    plus: bool,
    minus: bool,
    has_x: bool,
    changed: bool,
}

impl ControlScan {
    const WORDS: [&'static [u8]; 21] = [
        b"set",
        b"builtin",
        b"command",
        b"+o",
        b"xtrace",
        b"--",
        b"unset",
        b"PS4",
        b"BASH_XTRACEFD",
        b"exec",
        b"-p",
        b"declare",
        b"local",
        b"typeset",
        b"readonly",
        b"export",
        b"printf",
        b"read",
        b"-v",
        b"-",
        b"-o",
    ];

    fn byte(&mut self, byte: u8) {
        if self.mode == 4 || self.changed {
            return;
        }
        if byte.is_ascii_whitespace() {
            self.end_word();
            return;
        }
        if self.length == 0 {
            if self.mode == 3 && matches!(byte, b'\'' | b'"') {
                return;
            }
            self.matches = (1 << Self::WORDS.len()) - 1;
            self.plus = byte == b'+';
            self.minus = byte == b'-';
            self.has_x = false;
        }
        if matches!(self.mode, 0 | 3) && byte == b'=' && (self.word(7) || self.word(8)) {
            self.changed = true;
        }
        for (i, word) in Self::WORDS.iter().enumerate() {
            if word.get(self.length) != Some(&byte) {
                self.matches &= !(1 << i);
            }
        }
        self.has_x |= byte == b'x';
        self.length = self.length.saturating_add(1);
    }

    fn word(&self, index: usize) -> bool {
        self.matches & (1 << index) != 0 && self.length == Self::WORDS[index].len()
    }

    fn end_word(&mut self) {
        if self.length == 0 {
            return;
        }
        match self.mode {
            0 => {
                if self.word(0) {
                    self.mode = 1;
                } else if self.word(6) || (11..=15).any(|index| self.word(index)) || self.word(17) {
                    self.mode = 3;
                } else if self.word(16) {
                    self.mode = 5;
                } else if self.word(9) {
                    self.changed = true;
                    self.mode = 4;
                } else if !(self.word(1) || self.word(2) || self.word(5) || self.word(10)) {
                    self.mode = 4;
                }
            }
            1 => {
                if self.word(5) {
                    self.mode = 4;
                } else if self.word(19) {
                    self.changed = true;
                    self.mode = 4;
                } else if self.word(20) {
                    self.mode = 7;
                } else if self.word(3) {
                    self.mode = 2;
                } else if self.plus && self.has_x {
                    self.changed = true;
                } else if !self.plus && !self.minus {
                    self.mode = 4;
                }
            }
            2 => {
                self.changed |= self.word(4);
                self.mode = 1;
            }
            3 => {
                self.changed |= self.word(7) || self.word(8);
            }
            5 => {
                self.mode = if self.word(18) { 6 } else { 4 };
            }
            6 => {
                self.changed |= self.word(7) || self.word(8);
                self.mode = 4;
            }
            7 => self.mode = 1,
            _ => {}
        }
        self.length = 0;
    }
}

#[derive(Debug, Default)]
pub(super) struct Capture {
    complete: bool,
    received_bytes: u64,
    records: Vec<Record>,
    invalid_records: usize,
    limit_reached: bool,
}

#[derive(Debug, Serialize)]
pub(super) struct Report {
    pub complete: bool,
    pub received_bytes: u64,
    pub records: usize,
    pub invalid_records: usize,
    pub limit_reached: bool,
    pub processes: usize,
    pub phases: Vec<Phase>,
    pub causes: Vec<Cause>,
    pub issues: Vec<String>,
}

#[derive(Debug, Serialize)]
pub(super) struct Phase {
    pub process: u32,
    pub name: String,
    pub wall_us: u64,
    pub attributed_us: u64,
    pub unattributed_us: u64,
}

#[derive(Debug, Serialize)]
pub(super) struct Cause {
    pub process: u32,
    pub phase: String,
    pub function: String,
    pub source: String,
    pub line: u32,
    pub plugin: Option<String>,
    pub exclusive_us: u64,
    pub inclusive_us: u64,
    pub calls: u64,
    pub kind: String,
}

pub(super) fn drain(input: impl Read + Send + 'static) -> Receiver<Capture> {
    let (sender, receiver) = mpsc::channel();
    std::thread::spawn(move || {
        let _ = sender.send(capture(input));
    });
    receiver
}

fn capture(mut input: impl Read) -> Capture {
    let mut result = Capture {
        complete: true,
        ..Capture::default()
    };
    let mut prefix = Vec::with_capacity(256);
    let mut tabs = 0usize;
    let mut discard = false;
    let mut overflow = false;
    let mut traced_line = false;
    let mut record_index = None;
    let mut control = ControlScan::default();
    let mut bytes = [0u8; 8192];
    loop {
        let n = match input.read(&mut bytes) {
            Ok(0) => break,
            Ok(n) => n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) => {
                result.complete = false;
                break;
            }
        };
        for &byte in &bytes[..n] {
            result.received_bytes = result.received_bytes.saturating_add(1);
            if result.received_bytes > MAX_INGRESS {
                result.complete = false;
                result.limit_reached = true;
                continue;
            }
            if byte == b'\n' {
                control.end_word();
                if let Some(index) = record_index {
                    let record: &mut Record = &mut result.records[index];
                    if control.changed && record.phase != "metadata" {
                        record.control_change = true;
                        result.complete = false;
                    }
                }
                if !discard && (looks_like_marker(&prefix) || overflow) {
                    result.invalid_records += 1;
                    result.complete = false;
                }
                prefix.clear();
                tabs = 0;
                discard = false;
                overflow = false;
                traced_line = false;
                record_index = None;
                control = ControlScan::default();
                continue;
            }
            if discard {
                if record_index.is_some() {
                    control.byte(byte);
                }
                continue;
            }
            if byte == b'\t' {
                tabs += 1;
            }
            if tabs == 9 {
                if looks_like_marker(&prefix) {
                    traced_line = true;
                    match parse_record(&prefix) {
                        Some(record) if result.records.len() < MAX_RECORDS => {
                            record_index = Some(result.records.len());
                            result.records.push(record)
                        }
                        Some(_) => {
                            result.complete = false;
                            result.limit_reached = true;
                        }
                        None => {
                            result.complete = false;
                            result.invalid_records += 1;
                            if let Ok(text) = std::str::from_utf8(&prefix) {
                                let fields: Vec<_> = text.split('\t').collect();
                                if [3, 4].iter().any(|&i| {
                                    fields
                                        .get(i)
                                        .and_then(|s| s.parse::<usize>().ok())
                                        .is_some_and(|depth| depth > MAX_DEPTH)
                                }) {
                                    result.limit_reached = true;
                                }
                            }
                        }
                    }
                }
                discard = true;
            } else if prefix.len() < MAX_PREFIX {
                prefix.push(byte);
                let pluses = prefix.iter().take_while(|&&b| b == b'+').count();
                if pluses == 0
                    || (pluses < prefix.len()
                        && !MARKER.starts_with(&prefix[pluses..])
                        && !prefix[pluses..].starts_with(MARKER))
                {
                    prefix.clear();
                    discard = true;
                }
            } else {
                result.complete = false;
                result.limit_reached = true;
                overflow = true;
                discard = true;
            }
        }
    }
    if traced_line || (!prefix.is_empty() && !discard && looks_like_marker(&prefix)) {
        result.complete = false;
        result.invalid_records += 1;
    }
    result
}

fn looks_like_marker(prefix: &[u8]) -> bool {
    let start = prefix.iter().take_while(|&&b| b == b'+').count();
    start > 0 && (prefix[start..].starts_with(MARKER) || MARKER.starts_with(&prefix[start..]))
}

fn parse_record(prefix: &[u8]) -> Option<Record> {
    let text = std::str::from_utf8(prefix).ok()?;
    let text = text.trim_start_matches('+');
    let mut fields = text.split('\t');
    if fields.next()? != "__BINIT_DIAG__" {
        return None;
    }
    let timestamp = fields.next()?.parse().ok()?;
    let pid = fields.next()?.parse().ok()?;
    let subshell: usize = fields.next()?.parse().ok()?;
    let depth: usize = fields.next()?.parse().ok()?;
    let function = fields.next()?.to_owned();
    let source = fields.next()?.to_owned();
    let line = fields.next()?.parse().ok()?;
    let phase = fields.next()?;
    if fields.next().is_some()
        || pid == 0
        || subshell > MAX_DEPTH
        || depth > MAX_DEPTH
        || !matches!(phase, "setup" | "init" | "check" | "metadata")
    {
        return None;
    }
    Some(Record {
        timestamp,
        pid,
        depth,
        function,
        source,
        line,
        phase: phase.to_owned(),
        control_change: false,
    })
}

#[derive(Clone)]
struct Frame {
    depth: usize,
    function: String,
    source: String,
    line: u32,
}

fn normalized(path: &str, generation: &Path, inputs: &Path) -> (String, Option<String>) {
    let mut cleaned = std::path::PathBuf::new();
    for component in Path::new(path).components() {
        match component {
            std::path::Component::ParentDir => {
                if !cleaned.pop() {
                    return (path.to_owned(), None);
                }
            }
            std::path::Component::CurDir => {}
            other => cleaned.push(other.as_os_str()),
        }
    }
    let source = cleaned.as_path();
    if let Ok(relative) = source.strip_prefix(generation) {
        let mut parts = relative.components();
        let plugin = match (parts.next(), parts.next()) {
            (Some(a), Some(b)) if a.as_os_str() == "plugins" => {
                Some(b.as_os_str().to_string_lossy().into_owned())
            }
            _ => None,
        };
        return (relative.to_string_lossy().into_owned(), plugin);
    }
    if let Ok(relative) = source.strip_prefix(inputs) {
        return (relative.to_string_lossy().into_owned(), None);
    }
    (path.to_owned(), None)
}

type CauseKey = (u32, String, String, String, u32, Option<String>);

pub(super) fn analyze(capture: Capture, root_pid: u32, generation: &Path, inputs: &Path) -> Report {
    let mut report = Report {
        complete: capture.complete,
        received_bytes: capture.received_bytes,
        records: capture.records.len(),
        invalid_records: capture.invalid_records,
        limit_reached: capture.limit_reached,
        processes: 0,
        phases: Vec::new(),
        causes: Vec::new(),
        issues: if capture.complete {
            Vec::new()
        } else {
            vec!["trace capture ended incomplete".to_owned()]
        },
    };
    if capture.records.iter().any(|record| record.control_change) {
        report
            .issues
            .push("tracing control changed; the following interval is unobserved".to_owned());
    }
    let mut by_pid: BTreeMap<u32, Vec<&Record>> = BTreeMap::new();
    for record in &capture.records {
        by_pid.entry(record.pid).or_default().push(record);
    }
    report.processes = by_pid.len();
    let mut ordinals = BTreeMap::new();
    ordinals.insert(root_pid, 0);
    for (i, pid) in by_pid.keys().filter(|&&pid| pid != root_pid).enumerate() {
        ordinals.insert(*pid, (i + 1) as u32);
    }
    if !by_pid.contains_key(&root_pid) {
        report.complete = false;
        report.issues.push("no root-shell records".to_owned());
    }
    let child_windows: Vec<(u64, u64)> = by_pid
        .iter()
        .filter(|(pid, _)| **pid != root_pid)
        .filter_map(|(_, records)| {
            let min = records.iter().map(|r| r.timestamp).min()?;
            let max = records.iter().map(|r| r.timestamp).max()?;
            Some((min, max))
        })
        .collect();
    let mut phases: BTreeMap<(u32, String), (u64, u64)> = BTreeMap::new();
    let mut causes: BTreeMap<CauseKey, (u64, u64, u64)> = BTreeMap::new();
    for (pid, records) in by_pid {
        let process = ordinals[&pid];
        let mut stack: Vec<Frame> = Vec::new();
        let mut previous: Option<&Record> = None;
        for record in records {
            if let Some(prev) = previous {
                if record.timestamp < prev.timestamp {
                    report.complete = false;
                    report.issues.push("trace clock moved backward".to_owned());
                    stack.clear();
                    previous = Some(record);
                    continue;
                }
                let elapsed = record.timestamp - prev.timestamp;
                let phase = phases.entry((process, prev.phase.clone())).or_default();
                phase.0 = phase.0.saturating_add(elapsed);
                let invalid_jump = stack.last().is_some_and(|f| record.depth > f.depth + 1);
                let opaque = invalid_jump
                    || prev.control_change
                    || (pid == root_pid
                        && child_windows.iter().any(|&(start, end)| {
                            start <= record.timestamp && end >= prev.timestamp
                        }));
                if !opaque && !stack.is_empty() {
                    phase.1 = phase.1.saturating_add(elapsed);
                    for (index, frame) in stack.iter().enumerate() {
                        let (source, plugin) = normalized(&frame.source, generation, inputs);
                        let key = (
                            process,
                            prev.phase.clone(),
                            frame.function.clone(),
                            source,
                            frame.line,
                            plugin,
                        );
                        let value = causes.entry(key).or_default();
                        value.1 = value.1.saturating_add(elapsed);
                        if index + 1 == stack.len() {
                            value.0 = value.0.saturating_add(elapsed);
                        }
                    }
                }
            }
            while stack.last().is_some_and(|f| f.depth > record.depth) {
                stack.pop();
            }
            let new_entry = stack.last().is_none_or(|f| f.depth < record.depth);
            if let Some(last) = stack.last()
                && record.depth > last.depth + 1
            {
                report.complete = false;
                let (source, _) = normalized(&record.source, generation, inputs);
                report.issues.push(format!(
                    "function depth jumped from {} to {} at {}:{} ({})",
                    last.depth, record.depth, source, record.line, record.function
                ));
                stack.clear();
                previous = Some(record);
                continue;
            }
            if new_entry {
                stack.push(Frame {
                    depth: record.depth,
                    function: record.function.clone(),
                    source: record.source.clone(),
                    line: record.line,
                });
                let (source, plugin) = normalized(&record.source, generation, inputs);
                let key = (
                    process,
                    record.phase.clone(),
                    record.function.clone(),
                    source,
                    record.line,
                    plugin,
                );
                causes.entry(key).or_default().2 += 1;
            } else if let Some(frame) = stack.last_mut() {
                if frame.function != record.function {
                    // A same-depth switch is an observed return and entry.
                    frame.function.clone_from(&record.function);
                    let (source, plugin) = normalized(&record.source, generation, inputs);
                    let key = (
                        process,
                        record.phase.clone(),
                        record.function.clone(),
                        source,
                        record.line,
                        plugin,
                    );
                    causes.entry(key).or_default().2 += 1;
                }
                frame.source.clone_from(&record.source);
                frame.line = record.line;
            }
            previous = Some(record);
        }
    }
    report.phases = phases
        .into_iter()
        .map(|((process, name), (wall_us, attributed_us))| Phase {
            process,
            name,
            wall_us,
            attributed_us,
            unattributed_us: wall_us - attributed_us,
        })
        .collect();
    report.causes = causes
        .into_iter()
        .map(
            |(
                (process, phase, function, source, line, plugin),
                (exclusive_us, inclusive_us, calls),
            )| Cause {
                process,
                phase,
                function,
                source,
                line,
                plugin,
                exclusive_us,
                inclusive_us,
                calls,
                kind: "function".to_owned(),
            },
        )
        .collect();
    report
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn restored_tracing_controls_leave_opaque_intervals() {
        for command in [
            "set +x",
            "set -",
            "builtin set +vx",
            "command set +o xtrace",
            "PS4=private-value",
            "BASH_XTRACEFD=7",
            "unset PS4",
            "builtin exec",
            "declare PS4=private-value",
            "declare 'PS4=private value'",
            "local BASH_XTRACEFD=7",
            "printf -v PS4 private-value",
            "read -r PS4",
        ] {
            let result = report(
                &[
                    line(0, 1, 0, "", "check", command),
                    line(250000, 1, 0, "", "check", ": restored"),
                ]
                .concat(),
            );
            assert!(!result.complete, "{command}");
            assert_eq!(result.phases[0].unattributed_us, 250000, "{command}");
            assert_eq!(result.phases[0].attributed_us, 0);
        }
        for command in [
            "set -e",
            "set -- +x",
            "set positional +x",
            "set -o errexit positional +x",
            ": 'set +x'",
            "unset ordinary",
            "set +o",
        ] {
            assert!(
                report(&line(0, 1, 0, "", "check", command)).complete,
                "{command}"
            );
        }
        assert!(report(&line(0, 1, 0, "", "metadata", "set +x")).complete);
    }

    #[test]
    fn source_ownership_resolves_parent_components() {
        assert_eq!(
            normalized(
                "/g/plugins/p/../q/init.bash",
                Path::new("/g"),
                Path::new("/i")
            ),
            ("plugins/q/init.bash".to_owned(), Some("q".to_owned()))
        );
        assert_eq!(
            normalized(
                "/g/plugins/p/../../../outside.bash",
                Path::new("/g"),
                Path::new("/i")
            )
            .1,
            None
        );
    }

    fn line(t: u64, pid: u32, depth: usize, function: &str, phase: &str, command: &str) -> String {
        format!(
            "+__BINIT_DIAG__\t{t}\t{pid}\t0\t{depth}\t{function}\t/g/plugins/p/main.bash\t3\t{phase}\t{command}\n"
        )
    }
    fn report(text: &str) -> Report {
        analyze(
            capture(Cursor::new(text.as_bytes())),
            1,
            Path::new("/g"),
            Path::new("/i"),
        )
    }
    #[test]
    fn bash_source_ancestry_is_contiguous() {
        let directory = tempfile::tempdir().unwrap();
        let inner = directory.path().join("inner.bash");
        let outer = directory.path().join("outer.bash");
        std::fs::write(&inner, "leaf() { : leaf; }; leaf\n").unwrap();
        std::fs::write(&outer, "source \"$1\"\n: returned\n").unwrap();
        // Use the production prefix so the regression crosses the Bash/parser seam.
        let prefix = include_str!("../runtime/check.bash")
            .lines()
            .nth(1)
            .unwrap();
        let script = format!(
            "{prefix}\nPS4=$__binit_diag_expected_ps4\nset -x\nsource \"$1\" \"$2\"\n: done\n"
        );
        let output = std::process::Command::new(
            std::env::var("BINIT_BASH").unwrap_or_else(|_| "bash".to_owned()),
        )
        .args(["--noprofile", "--norc", "-c", &script, "fixture"])
        .arg(&outer)
        .arg(&inner)
        .env_remove("BASH_ENV")
        .output()
        .unwrap();
        assert!(output.status.success());
        let captured = capture(Cursor::new(output.stderr));
        assert!(captured.complete);
        let pid = captured.records[0].pid;
        let result = analyze(captured, pid, directory.path(), directory.path());
        assert!(result.complete, "{:?}", result.issues);
        assert!(result.causes.iter().any(|cause| cause.function == "leaf"));
        assert_eq!(
            result
                .causes
                .iter()
                .map(|cause| cause.exclusive_us)
                .sum::<u64>(),
            result.phases.iter().map(|phase| phase.wall_us).sum::<u64>()
        );
    }
    #[test]
    fn discards_arguments_and_continuations() {
        let secret = "private-value".repeat(10000);
        let input = format!(
            "{}continuation {secret}\n{}",
            line(1, 1, 0, "", "setup", &secret),
            line(2, 1, 0, "", "setup", "ok")
        );
        let capture = capture(Cursor::new(input));
        assert!(capture.complete);
        assert_eq!(capture.records.len(), 2);
        assert!(!format!("{capture:?}").contains("private-value"));
    }
    #[test]
    fn nested_and_recursive_intervals_are_exclusive() {
        let input = [
            line(0, 1, 1, "outer", "init", ""),
            line(10, 1, 2, "outer", "init", ""),
            line(30, 1, 1, "outer", "init", ""),
            line(40, 1, 1, "outer", "check", ""),
        ]
        .concat();
        let result = report(&input);
        assert!(result.complete);
        let init = result.phases.iter().find(|p| p.name == "init").unwrap();
        assert_eq!((init.wall_us, init.attributed_us), (40, 40));
        assert_eq!(
            result.causes.iter().map(|c| c.exclusive_us).sum::<u64>(),
            40
        );
        assert_eq!(
            result.causes.iter().map(|c| c.inclusive_us).sum::<u64>(),
            60
        );
        assert_eq!(result.causes.iter().map(|c| c.calls).sum::<u64>(), 2);
        assert_eq!(result.causes[0].plugin.as_deref(), Some("p"));
    }
    #[test]
    fn child_and_reversed_clocks_are_not_charged() {
        let input = [
            line(10, 1, 1, "root", "check", ""),
            line(15, 2, 1, "child", "check", ""),
            line(20, 2, 1, "child", "check", ""),
            line(30, 1, 1, "root", "check", ""),
            line(29, 1, 1, "root", "check", ""),
        ]
        .concat();
        let result = report(&input);
        assert!(!result.complete);
        let root = result.phases.iter().find(|p| p.process == 0).unwrap();
        assert_eq!((root.wall_us, root.unattributed_us), (20, 20));
        assert_eq!(
            result
                .phases
                .iter()
                .find(|p| p.process == 1)
                .unwrap()
                .attributed_us,
            5
        );
    }
    #[test]
    fn invalid_truncated_and_limits_are_incomplete() {
        let broken = "+__BINIT_DIAG__\tbad\t1\t0\t1\tf\ts\t2\tcheck\tcmd\n";
        assert!(!report(broken).complete);
        assert!(!report("+__BINIT_DIAG__\t123").complete);
        let many = line(1, 1, 1, "f", "check", "").repeat(MAX_RECORDS + 1);
        let result = report(&many);
        assert!(result.limit_reached);
        assert_eq!(result.records, MAX_RECORDS);
        let jump = [
            line(1, 1, 1, "f", "check", ""),
            line(2, 1, 4, "g", "check", ""),
            line(3, 1, 4, "g", "check", ""),
        ]
        .concat();
        assert!(!report(&jump).complete);
        assert!(!Capture::default().complete);
    }
}
