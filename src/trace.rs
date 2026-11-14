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
                if !discard && (looks_like_marker(&prefix) || overflow) {
                    result.invalid_records += 1;
                    result.complete = false;
                }
                prefix.clear();
                tabs = 0;
                discard = false;
                overflow = false;
                traced_line = false;
                continue;
            }
            if discard {
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
    let source = Path::new(path);
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
        issues: if capture.complete { Vec::new() } else { vec!["trace capture ended incomplete".to_owned()] },
    };
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
            if let Some(last) = stack.last() {
                if record.depth > last.depth + 1 {
                    report.complete = false;
                    let (source, _) = normalized(&record.source, generation, inputs);
                    report.issues.push(format!("function depth jumped from {} to {} at {}:{} ({})", last.depth, record.depth, source, record.line, record.function));
                    stack.clear();
                    previous = Some(record);
                    continue;
                }
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
