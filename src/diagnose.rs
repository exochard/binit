use super::*;

pub(super) const MAX_RUN_REPORT_BYTES: u64 = 128 * 1024 * 1024;
const MAX_REPORT_BYTES: u64 = 16 * 1024 * 1024;

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Summary {
    pub complete: bool,
    pub sha256: String,
    pub bytes: u64,
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
    let Ok(bytes) = read_regular(&path.join("namespace.bin")) else {
        return result;
    };
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
    for row in fields[4..].chunks_exact(4) {
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
        if let Some(symbol) = state.reason.strip_prefix("command already defined: ") {
            if valid_callback(symbol) {
                result.conflicts.push(Conflict {
                    plugin: state.plugin.clone(),
                    symbol: symbol.into(),
                    existing_owner: result.functions.get(symbol).cloned(),
                    reason: state.reason.clone(),
                });
            }
        }
    }
    result
}

pub(super) fn save_report(
    root: &Path,
    mut trace: crate::trace::Report,
    budget: u64,
) -> Result<Summary> {
    let namespace = namespace(root);
    let mut complete = trace.complete && namespace.complete;
    let limit = budget.min(MAX_REPORT_BYTES);
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
    println!("Timing: instrumented Bash wall time");
    if let Some(phases) = report["trace"]["phases"].as_array() {
        for phase in phases.iter().filter(|phase| phase["process"] == 0) {
            let wall = phase["wall_us"].as_u64().unwrap_or(0);
            let attributed = phase["attributed_us"].as_u64().unwrap_or(0);
            println!(
                "Phase: {}  wall_us={}  attributed_us={}  opaque_us={}",
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
                "Conflict: {} blocked by {}; existing owner: {}",
                conflict["plugin"].as_str().unwrap_or("unknown"),
                conflict["symbol"].as_str().unwrap_or("unknown"),
                conflict["existing_owner"]
                    .as_str()
                    .unwrap_or("caller or unknown")
            );
        }
    }
    println!("Inclusive totals include children; sum exclusive time only.");
}

pub(super) fn command(state: &Path, args: &[String]) -> Result<()> {
    let (plan, json) = match args {
        [plan] => (Path::new(plan), false),
        [plan, option] if option == "--json" => (Path::new(plan), true),
        _ => return Err("usage: binit diagnose PLAN [--json]".into()),
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
    let receipt = checks::run(&subject, &state, plan, &root, true)?;
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
            "Diagnosis: {}",
            if receipt.passed() {
                "checks passed"
            } else {
                "checks failed or evidence incomplete"
            }
        );
        println!("Generation: {generation}");
        for report in &reports {
            print_report(report);
        }
        println!("Run: {id}");
    }
    if !receipt.passed() {
        return Err(receipt.failure_reason().into());
    }
    Ok(())
}
