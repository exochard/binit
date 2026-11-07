use super::*;
use std::os::fd::AsRawFd;

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Installed {
    pub(super) plugin: Plugin,
    pub(super) enabled: bool,
    pub(super) source: Option<PathBuf>,
    /// The arguments of the `binit plugin` line that produced this entry.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub(super) declaration: Vec<String>,
    /// The Git source and the commit it is pinned to.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(super) origin: Option<Origin>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Origin {
    pub(super) url: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(super) ver: Option<String>,
    pub(super) commit: String,
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
                declaration: Vec::new(),
                origin: None,
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
    let (plugin, files) = package::read(path)?;
    installed(
        plugin,
        files,
        Some(fs::canonicalize(path)?),
        enabled,
        Vec::new(),
        None,
    )
}

fn installed(
    plugin: Plugin,
    files: BTreeMap<String, Vec<u8>>,
    source: Option<PathBuf>,
    enabled: bool,
    declaration: Vec<String>,
    origin: Option<Origin>,
) -> Result<(Installed, BTreeMap<String, Vec<u8>>)> {
    let files = files
        .into_iter()
        .map(|(name, bytes)| (format!("plugins/{}/{name}", plugin.id), bytes))
        .collect();
    Ok((
        Installed {
            plugin,
            enabled,
            source,
            declaration,
            origin,
        },
        files,
    ))
}

fn line_package(argv: &[String]) -> Result<(Installed, BTreeMap<String, Vec<u8>>)> {
    let (plugin, files) = package::line(argv)?;
    let source = Some(fs::canonicalize(&argv[0])?);
    installed(plugin, files, source, true, argv.to_vec(), None)
}

/// Pins a Git line to `commit` and builds its package from a checkout.
fn git_package(
    state: &Path,
    argv: &[String],
    remote: &git::Remote,
    mirror: &Path,
    commit: &str,
) -> Result<(Installed, BTreeMap<String, Vec<u8>>)> {
    let ver = git::line_ver(argv)?;
    let listing = git::list(mirror, commit)?;
    let paths = package::git_paths(argv, &remote.name, &|path| listing.has(path))?;
    let checkout = git::Checkout::create(state, mirror, &listing, paths.as_deref())?;
    let (plugin, files) = package::git_line(argv, checkout.root(), &remote.name)?;
    let origin = Origin {
        url: remote.url.clone(),
        ver,
        commit: commit.to_owned(),
    };
    installed(plugin, files, None, true, argv.to_vec(), Some(origin))
}

fn require_catalog(entry: &Installed) -> Result<()> {
    if entry.declaration.is_empty() {
        return Ok(());
    }
    Err(format!(
        "{} is managed by a binit plugin line; edit the line and run binit sync",
        entry.plugin.id
    )
    .into())
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
    require_catalog(&plan.catalog[position])?;
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

/// `binit update [--check] [ID...]`. The old `update ID [PATH]` form applies
/// to a catalog plugin, and a second argument is a PATH only when it is not
/// an installed plugin.
pub(super) fn update_command(state: &Path, args: &[String], check: bool) -> Result<()> {
    if !check && (1..=2).contains(&args.len()) {
        let plan = current(state)?;
        let catalog = plan
            .catalog
            .iter()
            .any(|entry| entry.plugin.id == args[0] && entry.declaration.is_empty());
        let installed = |id: &String| plan.catalog.iter().any(|entry| entry.plugin.id == *id);
        if catalog && (args.len() == 1 || !installed(&args[1])) {
            return update(state, &args[0], args.get(1).map(Path::new));
        }
    }
    update_lines(state, args, check)
}

fn update_lines(state: &Path, ids: &[String], check: bool) -> Result<()> {
    let _guard = MutationGuard::acquire(state)?;
    git::clean_leftovers(state);
    let mut plan = current(state)?;
    let mut targets = Vec::new();
    if ids.is_empty() {
        for (position, entry) in plan.catalog.iter().enumerate() {
            if entry.origin.is_some() {
                targets.push(position);
            }
        }
        if targets.is_empty() {
            println!("No Git plugins to update");
            return Ok(());
        }
    }
    for id in ids {
        let position = plan
            .catalog
            .iter()
            .position(|entry| entry.plugin.id == *id)
            .ok_or_else(|| format!("plugin not installed: {id}"))?;
        let entry = &plan.catalog[position];
        if entry.declaration.is_empty() {
            return Err(format!("{id} is a catalog plugin; use binit update {id} [PATH]").into());
        }
        if check && entry.origin.is_none() {
            return Err(format!("{id} is a local directory, not a Git plugin").into());
        }
        if !targets.contains(&position) {
            targets.push(position);
        }
    }
    let mut fetched = BTreeSet::new();
    let mut changes = Vec::new();
    // Results print after the generation is published, so a failure shows no
    // moves.
    let mut report = Vec::new();
    let mut say = |line: String| {
        if check {
            println!("{line}");
        } else {
            report.push(line);
        }
    };
    for position in targets {
        let entry = plan.catalog[position].clone();
        let id = entry.plugin.id.clone();
        let wrap = |error: Box<dyn std::error::Error>| -> Box<dyn std::error::Error> {
            git::line_error(&entry.declaration[0], error)
        };
        let unchanged_id = |item: &Installed| -> Result<()> {
            if item.plugin.id == id {
                return Ok(());
            }
            Err(wrap(
                format!(
                    "the plugin id would change from {id} to {}; edit the line (for example add id={id} and files=) and run binit sync",
                    item.plugin.id
                )
                .into(),
            ))
        };
        let Some(origin) = &entry.origin else {
            let (item, files) = line_package(&entry.declaration).map_err(wrap)?;
            unchanged_id(&item)?;
            say(format!("{id}  re-snapshotted"));
            changes.push((position, item, files));
            continue;
        };
        if origin.ver.as_deref().is_some_and(git::valid_commit) {
            say(format!(
                "{id}  pinned to commit; to track a branch again, remove ver= or set ver=BRANCH, then run binit sync"
            ));
            continue;
        }
        package::check_keys(&entry.declaration).map_err(wrap)?;
        let remote = git::parse_source(&entry.declaration[0])
            .map_err(wrap)?
            .ok_or("Git origin on a local line")?;
        let (mirror, created) = git::ensure(state, &remote).map_err(wrap)?;
        if created {
            fetched.insert(remote.url.clone());
        } else if fetched.insert(remote.url.clone()) {
            git::refresh(&mirror, &remote.url).map_err(wrap)?;
        }
        let commit = git::pin(&mirror, &remote.url, origin.ver.as_deref(), true).map_err(wrap)?;
        if commit == origin.commit {
            say(format!("{id}  up to date"));
            continue;
        }
        let moved = format!("{id}  {} -> {}", &origin.commit[..12], &commit[..12]);
        if check {
            let (count, log) = git::changes(&mirror, &origin.commit, &commit);
            match count {
                Some(1) => println!("{moved}  (1 commit)"),
                Some(count) => println!("{moved}  ({count} commits)"),
                None => println!("{moved}"),
            }
            for line in log {
                println!("{line}");
            }
        } else {
            let (item, files) =
                git_package(state, &entry.declaration, &remote, &mirror, &commit).map_err(wrap)?;
            unchanged_id(&item)?;
            say(moved);
            changes.push((position, item, files));
        }
    }
    if check {
        return Ok(());
    }
    if changes.is_empty() {
        for line in report {
            println!("{line}");
        }
        return Ok(());
    }
    for (position, item, files) in changes {
        // Remove by the installed ID, which the new package has too.
        let prefix = format!("plugins/{}/", plan.catalog[position].plugin.id);
        plan.packages.retain(|name, _| !name.starts_with(&prefix));
        plan.packages.extend(files);
        plan.catalog[position] = item;
    }
    let generation = publish(plan.catalog, plan.packages, state)?;
    for line in report {
        println!("{line}");
    }
    println!(
        "Generation: {}\nOpen a new shell to use it",
        &generation[..12]
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
    require_catalog(old)?;
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
        "Plugin: {id}\nNext shell: {}\nLoad: {}\nCommands: {}\nRequires: {}\nDepends: {}\nSource: {}\nEntry SHA256: {digest}",
        if entry.enabled { "enabled" } else { "disabled" },
        entry.plugin.load,
        entry.plugin.commands.join(", "),
        entry.plugin.requires.join(", "),
        entry.plugin.depends.join(", "),
        match (&entry.origin, entry.source.as_deref()) {
            (Some(origin), _) => origin.url.clone().into(),
            (None, Some(path)) => path.to_string_lossy(),
            (None, None) => "legacy snapshot".into(),
        }
    );
    if !entry.plugin.bin.is_empty() {
        let programs = entry
            .plugin
            .bin
            .iter()
            .filter_map(|item| item.rsplit_once(':'))
            .map(|(path, name)| format!("{name} ({})", path.strip_prefix("files/").unwrap_or(path)))
            .collect::<Vec<_>>();
        println!(
            "Programs: {}{}",
            programs.join(", "),
            if entry.enabled { "" } else { " (disabled)" }
        );
    }
    if let Some(origin) = &entry.origin {
        println!(
            "Ver: {}\nCommit: {}",
            origin.ver.as_deref().unwrap_or("default branch"),
            origin.commit
        );
    }
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

const DECLARED_MAX: u64 = 256 * 1024;
const DECLARED_LINES: usize = 64;
const DECLARED_ARGUMENTS: usize = 16;

/// Parses records of an argument count followed by that many arguments, each
/// terminated by NUL.
fn parse_declared(bytes: &[u8]) -> Result<Vec<Vec<String>>> {
    let text = std::str::from_utf8(bytes).map_err(|_| "declared plugin lines must be UTF-8")?;
    if text.is_empty() {
        return Ok(Vec::new());
    }
    let body = text
        .strip_suffix('\0')
        .ok_or("declared plugin lines are truncated")?;
    let mut fields = body.split('\0');
    let mut lines = Vec::new();
    while let Some(count) = fields.next() {
        if count.is_empty() || count.len() > 2 || !count.bytes().all(|b| b.is_ascii_digit()) {
            return Err("declared plugin lines are malformed".into());
        }
        let count: usize = count.parse()?;
        if count == 0 || count > DECLARED_ARGUMENTS || lines.len() == DECLARED_LINES {
            return Err("declared plugin lines exceed 64 lines or 16 arguments".into());
        }
        let mut line = Vec::with_capacity(count);
        for _ in 0..count {
            line.push(
                fields
                    .next()
                    .ok_or("declared plugin lines are truncated")?
                    .to_owned(),
            );
        }
        lines.push(line);
    }
    Ok(lines)
}

fn read_declared(state: &Path) -> Result<Vec<Vec<String>>> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(state.join("declared"))
        .map_err(|error| -> Box<dyn std::error::Error> {
            if error.kind() == std::io::ErrorKind::NotFound {
                "no declared plugin lines; run binit sync inside a shell that sourced binit init"
                    .into()
            } else {
                error.into()
            }
        })?;
    let metadata = file.metadata()?;
    if !metadata.is_file()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o077 != 0
    {
        return Err("declared list must be a private regular file owned by you".into());
    }
    if metadata.len() > DECLARED_MAX {
        return Err("declared list exceeds 256 KiB".into());
    }
    let mut bytes = Vec::new();
    file.take(DECLARED_MAX + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > DECLARED_MAX {
        return Err("declared list exceeds 256 KiB".into());
    }
    parse_declared(&bytes)
}

/// Stores the list sent by a shell after checking it, so `sync` never reads
/// a record this binary would refuse.
pub(super) fn declare(state: &Path) -> Result<()> {
    let mut bytes = Vec::new();
    std::io::stdin()
        .take(DECLARED_MAX + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > DECLARED_MAX {
        return Err("declared list exceeds 256 KiB".into());
    }
    parse_declared(&bytes)?;
    atomic_write(&state.join("declared"), &bytes)
}

pub(super) fn sync(state: &Path) -> Result<()> {
    let _guard = MutationGuard::acquire(state)?;
    git::clean_leftovers(state);
    let lines = read_declared(state)?;
    let mut plan = current(state)?;
    let previous = plan.packages.clone();
    let old: Vec<Installed> = plan
        .catalog
        .iter()
        .filter(|e| !e.declaration.is_empty())
        .cloned()
        .collect();
    for entry in &old {
        let prefix = format!("plugins/{}/", entry.plugin.id);
        plan.packages.retain(|name, _| !name.starts_with(&prefix));
    }
    plan.catalog.retain(|entry| entry.declaration.is_empty());
    let mut fetched = BTreeSet::new();
    for argv in &lines {
        let wrap = |error: Box<dyn std::error::Error>| -> Box<dyn std::error::Error> {
            git::line_error(&argv[0], error)
        };
        let (item, files) = match git::parse_source(&argv[0]).map_err(wrap)? {
            None => line_package(argv).map_err(wrap)?,
            // A line that is already synced keeps its pin and its snapshot.
            Some(remote) => match old
                .iter()
                .find(|entry| entry.origin.is_some() && entry.declaration == *argv)
            {
                Some(entry) => {
                    let prefix = format!("plugins/{}/", entry.plugin.id);
                    let files = previous
                        .iter()
                        .filter(|(name, _)| name.starts_with(&prefix))
                        .map(|(name, bytes)| (name.clone(), bytes.clone()))
                        .collect();
                    (entry.clone(), files)
                }
                None => {
                    package::check_keys(argv).map_err(wrap)?;
                    let ver = git::line_ver(argv).map_err(wrap)?;
                    // A line keeps its commit while its id, source and ver stay
                    // the same. Any other change is a new choice and resolves
                    // the current commit.
                    let wanted = argv[1..]
                        .iter()
                        .find_map(|a| a.strip_prefix("id="))
                        .unwrap_or(&remote.name);
                    let kept = old.iter().find_map(|entry| {
                        let origin = entry.origin.as_ref()?;
                        (entry.plugin.id == wanted
                            && git::normalize(&origin.url) == git::normalize(&remote.url)
                            && origin.ver == ver)
                            .then(|| origin.commit.clone())
                    });
                    let (mirror, created) = git::ensure(state, &remote).map_err(wrap)?;
                    let commit = match kept {
                        Some(commit) => commit,
                        None => {
                            if created {
                                fetched.insert(remote.url.clone());
                            } else if !ver.as_deref().is_some_and(git::valid_commit)
                                && fetched.insert(remote.url.clone())
                            {
                                git::refresh(&mirror, &remote.url).map_err(wrap)?;
                            }
                            let fresh = fetched.contains(&remote.url);
                            git::pin(&mirror, &remote.url, ver.as_deref(), fresh).map_err(wrap)?
                        }
                    };
                    git_package(state, argv, &remote, &mirror, &commit).map_err(wrap)?
                }
            },
        };
        let id = &item.plugin.id;
        if plan.catalog.iter().any(|entry| entry.plugin.id == *id) {
            return Err(format!(
                "plugin line {}: id {id} is already in use; add id=NAME",
                git::redact(&argv[0])
            )
            .into());
        }
        plan.catalog.push(item);
        plan.packages.extend(files);
    }
    let generation = publish(plan.catalog, plan.packages, state)?;
    println!(
        "Synced {} plugin line{}\nGeneration: {}\nOpen a new shell to use it",
        lines.len(),
        if lines.len() == 1 { "" } else { "s" },
        &generation[..12]
    );
    Ok(())
}
