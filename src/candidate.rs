use super::*;

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Candidate {
    schema: u32,
    generation: String,
    base_generation: Option<String>,
    config: PathBuf,
    config_sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    prepared_by: Option<checks::FileIdentity>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    check: Option<CheckRun>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct CheckRun {
    id: String,
    receipt_sha256: Option<String>,
}

fn selected_id(state: &Path) -> Result<Option<String>> {
    match fs::symlink_metadata(state.join("current")) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
        Ok(_) => Ok(Some(pointer(state, "current")?)),
    }
}

fn selected(state: &Path) -> Result<Option<String>> {
    let current = selected_id(state)?;
    if let Some(id) = &current {
        verify_generation(state, id)?;
    }
    Ok(current)
}

fn load_record(state: &Path) -> Result<Candidate> {
    private_dir(state)?;
    let path = state.join("candidate.json");
    let metadata = fs::symlink_metadata(&path).map_err(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            "no candidate prepared; run binit candidate prepare CONFIG".to_owned()
        } else {
            error.to_string()
        }
    })?;
    if !metadata.is_file()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o7777 != 0o600
    {
        return Err("candidate record must be a private regular file".into());
    }
    let candidate: Candidate = serde_json::from_slice(&read_regular(&path)?)?;
    if candidate.schema != 1
        || !valid_digest(&candidate.generation)
        || candidate
            .base_generation
            .as_deref()
            .is_some_and(|generation| !valid_digest(generation))
        || !valid_digest(&candidate.config_sha256)
        || !candidate.config.is_absolute()
        || candidate
            .prepared_by
            .as_ref()
            .is_some_and(|identity| !identity.path.is_absolute() || !valid_digest(&identity.sha256))
        || candidate.check.as_ref().is_some_and(|run| {
            !valid_digest(&run.id)
                || run
                    .receipt_sha256
                    .as_deref()
                    .is_some_and(|digest| !valid_digest(digest))
        })
    {
        return Err("invalid candidate record".into());
    }
    Ok(candidate)
}

fn load(state: &Path) -> Result<Candidate> {
    let candidate = load_record(state)?;
    verify_generation(state, &candidate.generation)?;
    Ok(candidate)
}

fn catalog_by_id(lock: &Lock) -> BTreeMap<String, manager::Installed> {
    manager::catalog(lock)
        .into_iter()
        .map(|entry| (entry.plugin.id.clone(), entry))
        .collect()
}

fn package_changed(base: &Lock, candidate: &Lock, id: &str) -> bool {
    let prefix = format!("plugins/{id}/");
    let before = base.files.range(prefix.clone()..);
    let after = candidate.files.range(prefix.clone()..);
    before
        .take_while(|(path, _)| path.starts_with(&prefix))
        .ne(after.take_while(|(path, _)| path.starts_with(&prefix)))
}

fn describe_change(
    old: &manager::Installed,
    new: &manager::Installed,
    content: bool,
) -> Result<Vec<String>> {
    let mut changes = Vec::new();
    let before = serde_json::to_value(&old.plugin)?;
    let after = serde_json::to_value(&new.plugin)?;
    for (field, value) in after.as_object().ok_or("invalid plugin metadata")? {
        if field != "schema" && value != &before[field] {
            changes.push(format!("{field}: {} -> {value}", before[field]));
        }
    }
    // These fields are omitted from the serialized form when their lists are empty.
    for field in ["bin", "commands"] {
        if after.get(field).is_none() && before.get(field).is_some() {
            changes.push(format!("{field}: {} -> []", before[field]));
        }
    }
    if old.enabled != new.enabled {
        changes.push(format!("enabled: {} -> {}", old.enabled, new.enabled));
    }
    let commit =
        |entry: &manager::Installed| entry.origin.as_ref().map(|origin| origin.commit.clone());
    if commit(old) != commit(new) {
        changes.push(format!(
            "revision: {} -> {}",
            commit(old).as_deref().unwrap_or("-"),
            commit(new).as_deref().unwrap_or("-")
        ));
    }
    if old.source != new.source
        || old.declaration != new.declaration
        || old.asset != new.asset
        || old.origin.as_ref().map(|origin| &origin.url)
            != new.origin.as_ref().map(|origin| &origin.url)
    {
        changes.push("source or declaration changed".into());
    }
    if content {
        changes.push("package contents changed".into());
    }
    Ok(changes)
}

fn diff(state: &Path) -> Result<()> {
    let record = load_record(state)?;
    let candidate = verify_generation_checks(state, &record.generation)?;
    let base = record
        .base_generation
        .as_deref()
        .map(|id| verify_generation_checks(state, id))
        .transpose()?;
    let current = selected_id(state)?;
    let before = base.as_ref().map(catalog_by_id).unwrap_or_default();
    let after = catalog_by_id(&candidate);
    let ids = before.keys().chain(after.keys()).collect::<BTreeSet<_>>();
    let mut rows = Vec::new();
    for id in ids {
        match (before.get(id), after.get(id)) {
            (None, Some(entry)) => rows.push(format!(
                "+ {id}  {}  {}",
                entry.plugin.load, entry.plugin.entry,
            )),
            (Some(_), None) => rows.push(format!("- {id}")),
            (Some(old), Some(new)) => {
                let content = base
                    .as_ref()
                    .is_some_and(|lock| package_changed(lock, &candidate, id));
                let changes = describe_change(old, new, content)?;
                if !changes.is_empty() {
                    rows.push(format!("~ {id}"));
                    rows.extend(changes.into_iter().map(|change| format!("  {change}")));
                }
            }
            (None, None) => unreachable!("ids come from the verified catalogs"),
        }
    }
    if let Some(base) = &base {
        let order = |lock: &Lock| {
            lock.plugins
                .iter()
                .map(|plugin| plugin.id.clone())
                .collect::<Vec<_>>()
        };
        if order(base) != order(&candidate) {
            rows.push(format!(
                "load order: {} -> {}",
                order(base).join(", "),
                order(&candidate).join(", ")
            ));
        }
    }
    println!(
        "base       {}",
        record.base_generation.as_deref().unwrap_or("-")
    );
    println!("candidate  {}", record.generation);
    if current != record.base_generation && current.as_deref() != Some(&record.generation) {
        println!("state      stale base");
    }
    if rows.is_empty() {
        println!("no plugin changes");
    } else {
        for row in rows {
            println!("{}", releases::plain_text(&row));
        }
    }
    Ok(())
}

fn prepare_candidate(state: &Path, config: &Path) -> Result<()> {
    let _guard = manager::MutationGuard::acquire(state)?;
    let base_generation = selected(state)?;
    let config = fs::canonicalize(config)?;
    let bytes = read_regular(&config)?;
    let generation = install_config(&config, &bytes, state)?;
    let candidate = Candidate {
        schema: 1,
        generation,
        base_generation,
        config,
        config_sha256: hash(&bytes),
        prepared_by: Some(checks::manager_identity()?),
        check: None,
    };
    atomic_write(
        &state.join("candidate.json"),
        &serde_json::to_vec_pretty(&candidate)?,
    )?;
    println!("candidate  {}", candidate.generation);
    println!("state      prepared");
    Ok(())
}

fn save(state: &Path, candidate: &Candidate) -> Result<()> {
    atomic_write(
        &state.join("candidate.json"),
        &serde_json::to_vec_pretty(candidate)?,
    )
}

fn subject(candidate: &Candidate) -> Result<checks::Subject> {
    Ok(checks::Subject {
        generation: candidate.generation.clone(),
        base_generation: candidate.base_generation.clone(),
        config: Some(checks::FileIdentity {
            path: candidate.config.clone(),
            sha256: candidate.config_sha256.clone(),
        }),
        manager: candidate
            .prepared_by
            .clone()
            .ok_or("candidate predates executable binding; prepare it again")?,
    })
}

fn existing_directory(path: &Path) -> Result<()> {
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.is_dir()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o7777 != 0o700
    {
        return Err("check evidence must use private directories".into());
    }
    Ok(())
}

fn run_root(state: &Path, run: &CheckRun) -> Result<PathBuf> {
    let parent = state.join("check-runs");
    existing_directory(&parent)?;
    let root = parent.join(&run.id);
    existing_directory(&root)?;
    Ok(root)
}

fn receipt(state: &Path, candidate: &Candidate) -> Result<(checks::Receipt, PathBuf)> {
    let run = candidate
        .check
        .as_ref()
        .ok_or("candidate has not been checked")?;
    let expected = run
        .receipt_sha256
        .as_deref()
        .ok_or("candidate checks are incomplete")?;
    let root = run_root(state, run)?;
    let path = root.join("receipt.json");
    let metadata = fs::symlink_metadata(&path)?;
    if !metadata.is_file()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o7777 != 0o600
    {
        return Err("check receipt must be a private regular file".into());
    }
    let bytes = read_regular_limit(&path, checks::MAX_RECEIPT_BYTES)?;
    if hash(&bytes) != expected {
        return Err("candidate check receipt changed".into());
    }
    Ok((serde_json::from_slice(&bytes)?, root))
}

fn state_label(state: &Path, candidate: &Candidate) -> Result<&'static str> {
    let Some(run) = &candidate.check else {
        return Ok("prepared");
    };
    if run.receipt_sha256.is_none() {
        return Ok("incomplete");
    }
    let (receipt, _) = receipt(state, candidate)?;
    if !receipt.passed() {
        return Ok("failed");
    }
    if receipt.inputs_current(&subject(candidate)?).is_err() {
        return Ok("stale");
    }
    let current = selected(state)?;
    if current.as_deref() == Some(&candidate.generation) {
        return Ok("selected");
    }
    if current != candidate.base_generation {
        return Ok("stale");
    }
    Ok("checked")
}

fn check_candidate(state: &Path, plan: &Path) -> Result<()> {
    if std::env::var_os("__binit_in_check").is_some() {
        return Err("candidate checks cannot be nested".into());
    }
    let _guard = manager::MutationGuard::acquire(state)?;
    let state = fs::canonicalize(state)?;
    let mut candidate = load(&state)?;
    if selected(&state)? != candidate.base_generation {
        return Err("selected reference changed; prepare the candidate again".into());
    }
    let subject = subject(&candidate)?;
    let parent = state.join("check-runs");
    private_dir(&parent)?;
    let id = hash(&unique().to_le_bytes());
    let root = parent.join(&id);
    fs::DirBuilder::new().mode(0o700).create(&root)?;
    // Invalidate prior acceptance before any fixture execution or plan parsing.
    candidate.check = Some(CheckRun {
        id,
        receipt_sha256: None,
    });
    save(&state, &candidate)?;
    let checked = checks::run(&subject, &state, plan, &root, false)?;
    let bytes = read_regular_limit(&root.join("receipt.json"), checks::MAX_RECEIPT_BYTES)?;
    candidate
        .check
        .as_mut()
        .ok_or("missing check run")?
        .receipt_sha256 = Some(hash(&bytes));
    save(&state, &candidate)?;
    println!("candidate  {}", candidate.generation);
    println!(
        "checks     {}",
        if checked.passed() { "passed" } else { "failed" }
    );
    if !checked.passed() {
        return Err(checked.failure_reason().into());
    }
    Ok(())
}

/// The identities a reviewer saw: selection proceeds only if the stored candidate still matches them.
struct Reviewed {
    base: String,
    generation: String,
    receipt: String,
}

fn reviewed(args: &[String]) -> Result<Reviewed> {
    let (mut base, mut generation, mut receipt) = (None, None, None);
    for pair in args.chunks(2) {
        let [flag, value] = pair else {
            return Err("each review flag needs a value".into());
        };
        let slot = match flag.as_str() {
            "--base" => &mut base,
            "--generation" => &mut generation,
            "--receipt" => &mut receipt,
            _ => return Err(format!("unknown select flag {flag}").into()),
        };
        if slot.replace(value.clone()).is_some() {
            return Err(format!("{flag} given twice").into());
        }
    }
    match (base, generation, receipt) {
        (Some(base), Some(generation), Some(receipt)) => Ok(Reviewed {
            base,
            generation,
            receipt,
        }),
        _ => Err("select needs --base, --generation and --receipt together".into()),
    }
}

fn select_candidate(state: &Path, review: Option<&Reviewed>) -> Result<()> {
    let _guard = manager::MutationGuard::acquire(state)?;
    let state = fs::canonicalize(state)?;
    let candidate = load(&state)?;
    let current = selected(&state)?;
    if current != candidate.base_generation && current.as_deref() != Some(&candidate.generation) {
        return Err("selected reference changed; prepare the candidate again".into());
    }
    let (receipt, root) = receipt(&state, &candidate)?;
    receipt.validate_selection(&subject(&candidate)?, &state, &root)?;
    if let Some(reference) = &candidate.base_generation {
        verify_generation(&state, reference)?;
    }
    verify_generation(&state, &candidate.generation)?;
    if let Some(review) = review {
        let receipt = candidate
            .check
            .as_ref()
            .and_then(|run| run.receipt_sha256.as_deref());
        if candidate.base_generation.as_deref() != Some(review.base.as_str())
            || candidate.generation != review.generation
            || receipt != Some(review.receipt.as_str())
        {
            return Err(
                "the stored candidate differs from the reviewed candidate; review it again".into(),
            );
        }
    }
    if current.as_deref() != Some(&candidate.generation) {
        select_generation(&state, &candidate.generation)?;
    }
    println!("selected {}; new shells use it", candidate.generation);
    Ok(())
}

pub(super) fn command(state: &Path, args: &[String]) -> Result<()> {
    match args {
        [action, config] if action == "prepare" => prepare_candidate(state, Path::new(config)),
        [action, plan] if action == "check" => check_candidate(state, Path::new(plan)),
        [action] if action == "diff" => diff(state),
        [action] if action == "select" => select_candidate(state, None),
        [action, review @ ..] if action == "select" => {
            select_candidate(state, Some(&reviewed(review)?))
        }
        [action] if action == "status" => {
            let candidate = load(state)?;
            println!("candidate  {}", candidate.generation);
            println!("state      {}", state_label(state, &candidate)?);
            Ok(())
        }
        [action, flag] if action == "status" && flag == "--json" => {
            let candidate = load(state)?;
            let mut value = serde_json::to_value(&candidate)?;
            value
                .as_object_mut()
                .ok_or("invalid candidate status")?
                .insert("state".to_owned(), state_label(state, &candidate)?.into());
            println!("{}", serde_json::to_string_pretty(&value)?);
            Ok(())
        }
        _ => Err("usage: binit candidate prepare CONFIG | check PLAN | diff \
             | select [--base GEN --generation GEN --receipt SHA256] | status [--json]"
            .into()),
    }
}
