use super::*;
use std::os::fd::AsRawFd;

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Installed {
    pub(super) plugin: Plugin,
    pub(super) enabled: bool,
    pub(super) source: Option<PathBuf>,
}

pub(super) struct MutationGuard(fs::File);
impl MutationGuard {
    pub(super) fn acquire(state: &Path) -> Result<Self> {
        private_dir(state)?;
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(state.join(".mutation.lock"))?;
        let metadata = file.metadata()?;
        let uid = unsafe { libc::geteuid() };
        if !metadata.is_file()
            || metadata.uid() != uid
            || metadata.permissions().mode() & 0o077 != 0
        {
            return Err("mutation lock must be a private regular file".into());
        }
        // The descriptor owns the lock; process exit releases it after failures.
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err("another Binit change is running; retry when it finishes".into());
        }
        Ok(Self(file))
    }
}
impl Drop for MutationGuard {
    fn drop(&mut self) {
        unsafe { libc::flock(self.0.as_raw_fd(), libc::LOCK_UN) };
    }
}

struct Plan {
    catalog: Vec<Installed>,
    packages: BTreeMap<String, Vec<u8>>,
}

fn catalog(lock: &Lock) -> Vec<Installed> {
    if lock.schema == 1 {
        lock.plugins
            .iter()
            .cloned()
            .map(|plugin| Installed {
                plugin,
                enabled: true,
                source: None,
            })
            .collect()
    } else {
        lock.catalog.clone()
    }
}

fn current(state: &Path) -> Result<Plan> {
    if !state.join("current").exists() {
        return Ok(Plan {
            catalog: Vec::new(),
            packages: BTreeMap::new(),
        });
    }
    let id = pointer(state, "current")?;
    let lock = verify_generation(state, &id)?;
    let root = state.join("generations").join(id);
    let catalog = catalog(&lock);
    let mut packages = BTreeMap::new();
    for name in lock
        .files
        .keys()
        .filter(|name| name.starts_with("plugins/"))
    {
        packages.insert(name.clone(), read_regular(&root.join(name))?);
    }
    Ok(Plan { catalog, packages })
}

pub(super) fn package(
    path: &Path,
    enabled: bool,
) -> Result<(Installed, BTreeMap<String, Vec<u8>>)> {
    if !fs::symlink_metadata(path)?.is_dir() {
        return Err("plugin root must be a directory, not a symlink".into());
    }
    let source = fs::canonicalize(path)?;
    let mut files = BTreeMap::new();
    collect(&source, Path::new(""), &mut files)?;
    let plugin: Plugin = toml::from_str(std::str::from_utf8(
        files.get("plugin.toml").ok_or("missing plugin.toml")?,
    )?)?;
    validate_plugin(&plugin)?;
    if !files.contains_key(&plugin.entry) {
        return Err(format!("missing entry for {}", plugin.id).into());
    }
    let files = files
        .into_iter()
        .map(|(name, bytes)| (format!("plugins/{}/{name}", plugin.id), bytes))
        .collect();
    Ok((
        Installed {
            plugin,
            enabled,
            source: Some(source),
        },
        files,
    ))
}

fn announce(action: &str, id: &str, generation: &str) {
    println!(
        "{action}: {id}\nGeneration: {}\nApply: binit shell",
        &generation[..12]
    );
}

pub(super) fn add(state: &Path, path: &Path, enabled: bool) -> Result<()> {
    let _guard = MutationGuard::acquire(state)?;
    let mut plan = current(state)?;
    let (item, files) = package(path, enabled)?;
    let id = item.plugin.id.clone();
    if plan.catalog.iter().any(|entry| entry.plugin.id == id) {
        return Err(format!("plugin already installed: {id}; use binit update {id}").into());
    }
    plan.catalog.push(item);
    plan.packages.extend(files);
    let generation = publish(plan.catalog, plan.packages, state)?;
    announce(
        if enabled { "Added" } else { "Added disabled" },
        &id,
        &generation,
    );
    Ok(())
}

pub(super) fn change(state: &Path, id: &str, action: &str) -> Result<()> {
    let _guard = MutationGuard::acquire(state)?;
    let mut plan = current(state)?;
    let position = plan
        .catalog
        .iter()
        .position(|entry| entry.plugin.id == id)
        .ok_or_else(|| format!("plugin not installed: {id}"))?;
    match action {
        "enable" => plan.catalog[position].enabled = true,
        "disable" => plan.catalog[position].enabled = false,
        "remove" => {
            plan.catalog.remove(position);
            let prefix = format!("plugins/{id}/");
            plan.packages.retain(|name, _| !name.starts_with(&prefix));
        }
        _ => return Err("unknown plugin action".into()),
    }
    let generation = publish(plan.catalog, plan.packages, state)?;
    announce(
        match action {
            "enable" => "Enabled",
            "disable" => "Disabled",
            _ => "Removed",
        },
        id,
        &generation,
    );
    Ok(())
}

pub(super) fn update(state: &Path, id: &str, path: Option<&Path>) -> Result<()> {
    let _guard = MutationGuard::acquire(state)?;
    let mut plan = current(state)?;
    let position = plan
        .catalog
        .iter()
        .position(|entry| entry.plugin.id == id)
        .ok_or_else(|| format!("plugin not installed: {id}"))?;
    let old = &plan.catalog[position];
    let source = path
        .or(old.source.as_deref())
        .ok_or("legacy plugin has no source path; use binit update ID PATH")?;
    let (item, files) = package(source, old.enabled)?;
    if item.plugin.id != id {
        return Err(format!("update changes plugin ID from {id} to {}", item.plugin.id).into());
    }
    let prefix = format!("plugins/{id}/");
    plan.packages.retain(|name, _| !name.starts_with(&prefix));
    plan.packages.extend(files);
    plan.catalog[position] = item;
    let generation = publish(plan.catalog, plan.packages, state)?;
    announce("Updated", id, &generation);
    Ok(())
}

pub(super) fn list(state: &Path) -> Result<()> {
    let plan = current(state)?;
    if plan.catalog.is_empty() {
        println!("No plugins installed");
        return Ok(());
    }
    println!("{:<20} {:<12} LOAD", "PLUGIN", "NEXT_SHELL");
    for entry in plan.catalog {
        println!(
            "{:<20} {:<12} {}",
            entry.plugin.id,
            if entry.enabled { "enabled" } else { "disabled" },
            entry.plugin.load
        );
    }
    Ok(())
}

pub(super) fn inspect(state: &Path, id: &str) -> Result<()> {
    let generation = pointer(state, "current")?;
    let lock = verify_generation(state, &generation)?;
    let catalog = catalog(&lock);
    let entry = catalog
        .iter()
        .find(|entry| entry.plugin.id == id)
        .ok_or_else(|| format!("plugin not installed: {id}"))?;
    let file = format!("plugins/{id}/{}", entry.plugin.entry);
    let digest = lock
        .files
        .get(&file)
        .ok_or("plugin entry is missing from the lock")?;
    println!(
        "Plugin: {id}\nNext shell: {}\nLoad: {}\nRequires: {}\nDepends: {}\nSource: {}\nEntry SHA256: {digest}",
        if entry.enabled { "enabled" } else { "disabled" },
        entry.plugin.load,
        entry.plugin.requires.join(", "),
        entry.plugin.depends.join(", "),
        entry
            .source
            .as_deref()
            .map(|path| path.to_string_lossy())
            .unwrap_or_else(|| "legacy snapshot".into())
    );
    Ok(())
}

pub(super) fn shell(state: &Path) -> Result<()> {
    use std::os::unix::process::CommandExt;
    if std::env::var_os("BINIT_SAFE").is_some_and(|value| value == "1") {
        return Err(std::process::Command::new("bash")
            .args(["--noprofile", "--norc", "-i"])
            .exec()
            .into());
    }
    let id = pointer(state, "current")?;
    verify_generation(state, &id)?;
    let init = fs::canonicalize(state.join("generations").join(id).join("init.bash"))?;
    let error = std::process::Command::new("bash")
        .arg("--noprofile")
        .arg("--rcfile")
        .arg(init)
        .arg("-i")
        .exec();
    Err(error.into())
}

pub(super) fn apply(state: &Path) -> Result<()> {
    let _guard = MutationGuard::acquire(state)?;
    let plan = current(state)?;
    let generation = publish(plan.catalog, plan.packages, state)?;
    println!("Applied {generation}; start a new shell to use it");
    Ok(())
}
