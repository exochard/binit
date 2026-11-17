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

fn selected(state: &Path) -> Result<Option<String>> {
    match fs::symlink_metadata(state.join("current")) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
        Ok(_) => {
            let generation = pointer(state, "current")?;
            verify_generation(state, &generation)?;
            Ok(Some(generation))
        }
    }
}

fn load(state: &Path) -> Result<Candidate> {
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
    verify_generation(state, &candidate.generation)?;
    Ok(candidate)
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

fn select_candidate(state: &Path) -> Result<()> {
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
        [action] if action == "select" => select_candidate(state),
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
        _ => Err(
            "usage: binit candidate prepare CONFIG | check PLAN | select | status [--json]".into(),
        ),
    }
}
