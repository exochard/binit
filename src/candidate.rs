use super::*;

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Candidate {
    schema: u32,
    generation: String,
    base_generation: Option<String>,
    config: PathBuf,
    config_sha256: String,
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
    };
    atomic_write(
        &state.join("candidate.json"),
        &serde_json::to_vec_pretty(&candidate)?,
    )?;
    println!("Candidate: {}", candidate.generation);
    println!("State: prepared");
    Ok(())
}

pub(super) fn command(state: &Path, args: &[String]) -> Result<()> {
    match args {
        [action, config] if action == "prepare" => prepare_candidate(state, Path::new(config)),
        [action] if action == "status" => {
            let candidate = load(state)?;
            println!("Candidate: {}", candidate.generation);
            println!("State: prepared");
            Ok(())
        }
        [action, flag] if action == "status" && flag == "--json" => {
            println!("{}", serde_json::to_string_pretty(&load(state)?)?);
            Ok(())
        }
        _ => Err("usage: binit candidate prepare CONFIG | status [--json]".into()),
    }
}
