use super::*;

pub(super) const MAX_RUN_REPORT_BYTES: u64 = 128 * 1024 * 1024;
const MAX_REPORT_BYTES: u64 = 16 * 1024 * 1024;

#[cfg(test)]
mod tests {
    #[test]
    fn namespace_writer_respects_remaining_budget() {
        let root = tempfile::tempdir().unwrap();
        let namespace = root.path().join("namespace.bin");
        let wrapper = include_str!("../runtime/check.bash");
        let prefix = wrapper.split("builtin printf 'setup\\n'").next().unwrap();
        let script = format!(
            "exec 3>/dev/null\n__binit_trace_fd=3\n{prefix}\n\
            declare -a __binit_ids=()\n\
            declare -A __binit_state=() __binit_reason=() __binit_owned_functions=()\n\
            for ((i=0;i<20;i++)); do __binit_ids+=(p$i); __binit_state[p$i]=loaded; __binit_reason[p$i]=reason; done\n\
            __binit_diag_emit\n"
        );
        for budget in [0, 127, 128, 256, 4096] {
            let result = std::process::Command::new(
                std::env::var("BINIT_BASH").unwrap_or_else(|_| "bash".to_owned()),
            )
            .args([
                "--noprofile",
                "--norc",
                "-c",
                &script,
                "fixture",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ])
            .arg(&namespace)
            .arg(budget.to_string())
            .env_remove("BASH_ENV")
            .output()
            .unwrap();
            assert!(
                result.status.success(),
                "{}",
                String::from_utf8_lossy(&result.stderr)
            );
            assert!(std::fs::metadata(&namespace).unwrap().len() <= budget);
            assert_eq!(super::namespace(root.path()).complete, budget == 4096);
        }
    }

    #[test]
    fn sidecar_budget_includes_namespace_bytes() {
        let root = crate::private_tempdir();
        let data = b"BINIT_DIAG_META\x001\x001\x00\x00end\x000\x000\x001\x00";
        std::fs::write(root.path().join("namespace.bin"), data).unwrap();
        let trace = crate::trace::analyze(Default::default(), 1, root.path(), root.path());
        let summary = super::save_report(root.path(), trace, 4096).unwrap();
        let report_bytes = std::fs::metadata(root.path().join("diagnostic.json"))
            .unwrap()
            .len();
        assert_eq!(summary.bytes, report_bytes);
        assert_eq!(summary.namespace_bytes, data.len() as u64);
        let second = crate::private_tempdir();
        std::fs::write(second.path().join("namespace.bin"), data).unwrap();
        let trace = crate::trace::analyze(Default::default(), 1, second.path(), second.path());
        assert!(super::save_report(second.path(), trace, data.len() as u64 + 16).is_err());
        assert!(!second.path().join("diagnostic.json").exists());
    }

    #[test]
    fn namespace_requires_terminal_delimiter() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("namespace.bin");
        let valid = b"BINIT_DIAG_META\x001\x001\x00\x00end\x000\x000\x001\x00";
        std::fs::write(&path, valid).unwrap();
        assert!(super::namespace(root.path()).complete);
        std::fs::write(&path, &valid[..valid.len() - 1]).unwrap();
        assert!(!super::namespace(root.path()).complete);
    }
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Summary {
    pub complete: bool,
    pub sha256: String,
    pub bytes: u64,
    pub namespace_bytes: u64,
}

#[derive(Default, Serialize)]
struct Namespace {
    complete: bool,
    states: Vec<PluginState>,
    functions: BTreeMap<String, String>,
    conflicts: Vec<Conflict>,
}

#[derive(Serialize)]
struct PluginState {
    plugin: String,
    state: String,
    reason: String,
}

#[derive(Serialize)]
struct Conflict {
    plugin: String,
    symbol: String,
    existing_owner: Option<String>,
    reason: String,
}

fn valid_callback(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 256
        && name.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_alphabetic()
                || byte == b'_'
                || (index > 0 && (byte.is_ascii_digit() || byte == b'-'))
        })
}

fn namespace(path: &Path) -> Namespace {
    let mut result = Namespace::default();
    let Ok(bytes) = read_regular_limit(&path.join("namespace.bin"), MAX_REPORT_BYTES) else {
        return result;
    };
    if !bytes.ends_with(b"\0") {
        return result;
    }
    let Ok(text) = std::str::from_utf8(&bytes) else {
        return result;
    };
    let fields: Vec<_> = text.split_terminator('\0').collect();
    if fields.len() < 8 || fields.len() % 4 != 0 || fields[..4] != ["BINIT_DIAG_META", "1", "1", ""]
    {
        return result;
    }
    let mut count = 0;
    let mut ended = false;
    for row in fields[4..].as_chunks::<4>().0 {
        if ended {
            return Namespace::default();
        }
        match row[0] {
            "state" if valid_name(row[1]) && row[2].len() <= 32 && row[3].len() <= 1024 => {
                result.states.push(PluginState {
                    plugin: row[1].into(),
                    state: row[2].into(),
                    reason: row[3].into(),
                });
                count += 1;
            }
            "function" if valid_name(row[1]) && valid_callback(row[2]) && row[3].is_empty() => {
                // Multiple claims are ambiguous and must never select a winner.
                if result
                    .functions
                    .insert(row[2].into(), row[1].into())
                    .is_some()
                {
                    return Namespace::default();
                }
                count += 1;
            }
            "end"
                if row[1].parse::<usize>().ok() == Some(count)
                    && row[2] == "0"
                    && row[3] == "1" =>
            {
                ended = true
            }
            _ => return Namespace::default(),
        }
        if count > 4096 {
            return Namespace::default();
        }
    }
    result.complete = ended;
    for state in &result.states {
        if let Some(symbol) = state.reason.strip_prefix("command already defined: ")
            && valid_callback(symbol)
        {
            result.conflicts.push(Conflict {
                plugin: state.plugin.clone(),
                symbol: symbol.into(),
                existing_owner: result.functions.get(symbol).cloned(),
                reason: state.reason.clone(),
            });
        }
    }
    result
}

pub(super) fn save_report(
    root: &Path,
    mut trace: crate::trace::Report,
    budget: u64,
) -> Result<Summary> {
    let namespace_bytes = fs::metadata(root.join("namespace.bin"))
        .map(|metadata| metadata.len())
        .unwrap_or(0);
    if namespace_bytes > MAX_REPORT_BYTES || namespace_bytes > budget {
        return Err("diagnostic namespace exceeded its retained-byte budget".into());
    }
    let namespace = namespace(root);
    let mut complete = trace.complete && namespace.complete;
    let limit = budget.saturating_sub(namespace_bytes).min(MAX_REPORT_BYTES);
    let mut value = serde_json::json!({
        "schema": 1,
        "complete": complete,
        "scope": "Instrumented Bash wall time; child-shell observations are separate and are not added to root time.",
        "trace": trace,
        "namespace": namespace,
    });
    let mut bytes = serde_json::to_vec_pretty(&value)?;
    if bytes.len() as u64 > limit {
        complete = false;
        trace.causes.clear();
        value = serde_json::json!({"schema":1,"complete":false,"error":"diagnostic report exceeded its retained-byte budget","trace":trace});
        bytes = serde_json::to_vec_pretty(&value)?;
        if bytes.len() as u64 > limit {
            return Err("diagnostic report budget exhausted".into());
        }
    }
    atomic_write(&root.join("diagnostic.json"), &bytes)?;
    Ok(Summary {
        complete,
        sha256: hash(&bytes),
        bytes: bytes.len() as u64,
        namespace_bytes,
    })
}

fn reports(root: &Path) -> Result<Vec<serde_json::Value>> {
    let directory = root.join("samples");
    if !directory.exists() {
        return Ok(Vec::new());
    }
    let mut paths = fs::read_dir(directory)?
        .map(|entry| entry.map(|entry| entry.path()))
        .collect::<std::io::Result<Vec<_>>>()?;
    paths.sort();
    let mut result = Vec::new();
    for path in paths {
        let report = path.join("diagnostic.json");
        if report.exists() {
            let bytes = read_regular_limit(&report, MAX_REPORT_BYTES)?;
            result.push(serde_json::from_slice(&bytes)?);
        }
    }
    Ok(result)
}

fn print_report(report: &serde_json::Value) {
    println!("timing      instrumented bash wall time");
    if let Some(phases) = report["trace"]["phases"].as_array() {
        for phase in phases.iter().filter(|phase| phase["process"] == 0) {
            let wall = phase["wall_us"].as_u64().unwrap_or(0);
            let attributed = phase["attributed_us"].as_u64().unwrap_or(0);
            println!(
                "phase       {}  wall_us={}  attributed_us={}  opaque_us={}",
                phase["name"].as_str().unwrap_or("unknown"),
                wall,
                attributed,
                wall.saturating_sub(attributed)
            );
        }
    }
    println!(
        "{:>10} {:>10}  {:<16} {:<24} SOURCE:LINE",
        "SELF_US", "TOTAL_US", "PLUGIN", "FUNCTION"
    );
    if let Some(causes) = report["trace"]["causes"].as_array() {
        let mut causes = causes
            .iter()
            .filter(|cause| cause["process"] == 0)
            .collect::<Vec<_>>();
        causes.sort_by_key(|cause| std::cmp::Reverse(cause["exclusive_us"].as_u64().unwrap_or(0)));
        for cause in causes.into_iter().take(20) {
            println!(
                "{:>10} {:>10}  {:<16} {:<24} {}:{}",
                cause["exclusive_us"],
                cause["inclusive_us"],
                cause["plugin"].as_str().unwrap_or("-"),
                cause["function"]
                    .as_str()
                    .unwrap_or("<shell>")
                    .escape_default(),
                cause["source"].as_str().unwrap_or("").escape_default(),
                cause["line"]
            );
        }
    }
    if let Some(conflicts) = report["namespace"]["conflicts"].as_array() {
        for conflict in conflicts {
            println!(
                "conflict    {} blocked by {}; existing owner {}",
                conflict["plugin"].as_str().unwrap_or("unknown"),
                conflict["symbol"].as_str().unwrap_or("unknown"),
                conflict["existing_owner"]
                    .as_str()
                    .unwrap_or("caller or unknown")
            );
        }
    }
}

pub(super) fn command(state: &Path, args: &[String]) -> Result<()> {
    let (plan, json, trace) = match args {
        [plan] => (Path::new(plan), false, true),
        [plan, option] if option == "--json" => (Path::new(plan), true, true),
        [plan, option] if option == "--no-trace" => (Path::new(plan), false, false),
        [plan, first, second]
            if (first == "--json" && second == "--no-trace")
                || (first == "--no-trace" && second == "--json") =>
        {
            (Path::new(plan), true, false)
        }
        _ => return Err("usage: binit diagnose PLAN [--json] [--no-trace]".into()),
    };
    if std::env::var_os("__binit_in_check").is_some() {
        return Err("diagnostic checks cannot be nested".into());
    }
    let _guard = manager::MutationGuard::acquire(state)?;
    let state = fs::canonicalize(state)?;
    let generation = pointer(&state, "current")?;
    verify_generation(&state, &generation)?;
    let subject = checks::Subject {
        generation: generation.clone(),
        base_generation: Some(generation.clone()),
        config: None,
        manager: checks::manager_identity()?,
    };
    let parent = state.join("diagnoses");
    private_dir(&parent)?;
    let id = hash(&unique().to_le_bytes());
    let root = parent.join(&id);
    fs::DirBuilder::new().mode(0o700).create(&root)?;
    let receipt = checks::run(&subject, &state, plan, &root, trace, None)?;
    let reports = reports(&root)?;
    if json {
        println!(
            "{}",
            serde_json::to_string_pretty(
                &serde_json::json!({"schema":1,"run":id,"passed":receipt.passed(),"receipt":receipt,"diagnostics":reports})
            )?
        );
    } else {
        println!(
            "diagnosis   {}",
            if receipt.passed() {
                "checks passed"
            } else {
                "checks failed or evidence incomplete"
            }
        );
        println!("generation  {generation}");
        if !trace {
            println!("tracing     off");
        }
        for report in &reports {
            print_report(report);
        }
        println!("run         {id}");
    }
    if !receipt.passed() {
        return Err(receipt.failure_reason().into());
    }
    Ok(())
}
